# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# 作者：FDE WorkMate  # noqa: RUF003

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


def _event(node: str, sequence: int, available: bool, reason: str = "lifecycle") -> SourceLifecycleEvent:
    return SourceLifecycleEvent(node_id=node, revision=1, sequence=sequence, available=available, reason=reason)


async def _graph(runtime, key: str):
    scope = (await runtime.scopes.create(ScopeDraft(title="Lifecycle", summary="DAG", idempotency_key=key))).scope_id
    lifecycle = runtime.lifecycle.for_scope(scope)
    refs = {}
    for name in ("alpha", "beta"):
        receipt = await runtime.sources.for_scope(scope).capture(
            CaptureSource(source_id=name, content=f"origin {name}", metadata={})
        )
        refs[name] = receipt.source_ref
        await lifecycle.register_source(name, receipt.source_ref, revision=1)
    memory = runtime.memory.for_scope(scope)
    for name, parents, policy in (
        ("first", ("alpha",), "all"),
        ("second", ("alpha", "beta"), "any"),
        ("third", ("first", "second"), "all"),
    ):
        result = await memory.remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{name} lifecycle fact"),))
        )
        refs[name] = result.entry.citation
        await lifecycle.register_memory(name, result.entry.citation, parents=parents, policy=policy)
    created = await runtime.records.for_scope(scope).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": "gamma", "summary": "gamma summary", "detail": "gamma detail"}),
    )
    refs["topic"] = ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)
    await lifecycle.register_topic("topic", refs["topic"], parents=("third",), policy="all")
    return scope, lifecycle, refs


async def _states(runtime, scope) -> dict[str, str]:
    page = await runtime.memory.for_scope(scope).list(include_inactive=True)
    return {record.entry.text.split()[0]: record.state for record in page.entries}


def test_interrupted_native_write_is_completed_by_retrying_the_same_event(monkeypatch) -> None:
    async def run() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope, lifecycle, _ = await _graph(runtime, "retry")
            original = MemoryService.forget
            calls = {"count": 0}

            async def failing_forget(self, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("simulated native write failure")  # noqa: TRY003
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(MemoryService, "forget", failing_forget)
            with pytest.raises(RuntimeError):
                await lifecycle.apply(_event("alpha", 1, False))
            assert (await _states(runtime, scope))["first"] == "active"

            assert await lifecycle.apply(_event("alpha", 1, False)) == ["beta", "second"]
            assert await _states(runtime, scope) == {"first": "inactive", "second": "active", "third": "inactive"}
            before = (await runtime.memory.for_scope(scope).list()).memory_ref
            assert await lifecycle.apply(_event("alpha", 1, False)) == ["beta", "second"]
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == before

            topic = await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="gamma", limit=5))
            assert not topic.hits
            await lifecycle.apply(_event("alpha", 2, True))
            topic = await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="gamma", limit=5))
            assert topic.hits
            assert set((await _states(runtime, scope)).values()) == {"active"}

    asyncio.run(run())


def test_registration_identity_events_and_scope_isolation() -> None:
    async def run() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope_a, lifecycle_a, refs = await _graph(runtime, "a")
            scope_b, lifecycle_b, _ = await _graph(runtime, "b")

            # Identical registrations are idempotent; different bindings conflict.
            await lifecycle_a.register_source("alpha", refs["alpha"], revision=1)
            await lifecycle_a.register_memory("first", refs["first"], parents=("alpha",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_source("alpha", refs["alpha"], revision=2)
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_memory("first", refs["first"], parents=("alpha",), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_memory("other", refs["first"], parents=("missing",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_memory("dup", refs["first"], parents=("alpha", "alpha"), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_memory("bad", refs["first"], parents=("alpha",), policy="most")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_topic("topic2", refs["topic"], parents=(), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_source(
                    "ghost", refs["alpha"].model_copy(update={"source_id": "nope"}), revision=1
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_topic(
                    "ghost-topic", refs["topic"].model_copy(update={"revision": 99}), parents=("alpha",), policy="all"
                )

            # Withdrawing in one Scope leaves the same node ids in another Scope untouched.
            assert "first" not in await lifecycle_a.apply(_event("alpha", 1, False))
            assert (await _states(runtime, scope_b))["first"] == "active"
            assert await lifecycle_b.resolve("first", purpose="generation") is not None
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.resolve("first", purpose="generation")
            assert await lifecycle_a.resolve("first", purpose="audit") == refs["first"]
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.resolve("first", purpose="training")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.resolve("unknown", purpose="audit")

            # Events only target Sources, with exact revisions and increasing sequences.
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.apply(_event("first", 1, False))
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.apply(_event("alpha", 1, True))
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.apply(_event("alpha", 1, False, reason="changed"))
            await lifecycle_a.apply(_event("alpha", 3, True))
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.apply(_event("alpha", 2, False))
            assert set((await _states(runtime, scope_a)).values()) == {"active"}

    asyncio.run(run())
    for invalid in (
        {"node_id": " ", "revision": 1, "sequence": 1, "available": True, "reason": "x"},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": True, "reason": "  "},
        {"node_id": "x", "revision": True, "sequence": 1, "available": True, "reason": "x"},
        {"node_id": "x", "revision": 1, "sequence": 0, "available": True, "reason": "x"},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": 1, "reason": "x"},
    ):
        with pytest.raises(ValidationError):
            SourceLifecycleEvent(**invalid)  # ty: ignore[invalid-argument-type]
