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

"""Source lifecycle public API: call styles, propagation, restart and retry."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from pathlib import Path

import pytest

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import (
    ScopedSourceLifecycleService,
    SourceLifecycleApplication,
    SourceLifecycleError,
    SourceLifecycleEvent,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef

_FAIL_MEMORY_WRITES = (
    "CREATE TRIGGER fail_memory BEFORE INSERT ON pc_artifacts "
    "WHEN NEW.family='memory' BEGIN SELECT RAISE(ABORT,'simulated'); END"
)


def _event(node: str, sequence: int, available: bool, *, revision: int = 3, reason: str = "r") -> SourceLifecycleEvent:
    return SourceLifecycleEvent(node_id=node, revision=revision, sequence=sequence, available=available, reason=reason)


def _runtime(path: Path | None = None):
    database = SQLiteConfig() if path is None else SQLiteConfig(url=f"sqlite+aiosqlite:///{path}")
    return open_builtin_runtime(BuiltinConfig(database=database))


async def _scope(runtime, key: str) -> str:
    return (await runtime.scopes.create(ScopeDraft(title=key, summary="s", idempotency_key=key))).scope_id


async def _capture(runtime, scope: str, name: str) -> SourceRef:
    receipt = await runtime.sources.for_scope(scope).capture(
        CaptureSource(source_id=name, content=f"Origin {name}", metadata={})
    )
    return receipt.source_ref


async def _remember(runtime, scope: str, name: str) -> MemoryCitation:
    result = await runtime.memory.for_scope(scope).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{name} fixture evidence"),))
    )
    return result.entry.citation


async def _topic_ref(runtime, scope: str, name: str) -> ArtifactRef:
    created = await runtime.records.for_scope(scope).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": name, "summary": f"{name} supported summary", "detail": f"{name} detail"}),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


async def _src(runtime, lifecycle, scope: str, node: str) -> SourceRef:
    ref = await _capture(runtime, scope, node)
    await lifecycle.register_source(node, ref, revision=3)
    return ref


async def _mem(runtime, lifecycle, scope: str, node: str, parents, policy) -> MemoryCitation:
    citation = await _remember(runtime, scope, node)
    await lifecycle.register_memory(node, citation, parents=parents, policy=policy)
    return citation


async def _topic(runtime, lifecycle, scope: str, node: str, parents, policy) -> ArtifactRef:
    ref = await _topic_ref(runtime, scope, node)
    await lifecycle.register_topic(node, ref, parents=parents, policy=policy)
    return ref


async def _memory_states(runtime, scope: str) -> dict[str, tuple[str, str]]:
    entries = (await runtime.memory.for_scope(scope).list(include_inactive=True)).entries
    return {e.entry.text.split()[0]: (e.state, e.entry.entry_version_id) for e in entries}


async def _active_memory(runtime, scope: str) -> set[str]:
    return {e.entry.text.split()[0] for e in (await runtime.memory.for_scope(scope).list()).entries}


def test_public_methods_have_no_positional_only_parameters() -> None:
    methods = (
        SourceLifecycleApplication.for_scope,
        ScopedSourceLifecycleService.register_source,
        ScopedSourceLifecycleService.register_memory,
        ScopedSourceLifecycleService.register_topic,
        ScopedSourceLifecycleService.apply,
        ScopedSourceLifecycleService.resolve,
    )
    for method in methods:
        kinds = {parameter.kind for parameter in inspect.signature(method).parameters.values()}
        assert inspect.Parameter.POSITIONAL_ONLY not in kinds, method.__qualname__


def test_every_public_method_accepts_keyword_and_positional_calls() -> None:
    async def run() -> None:
        async with _runtime() as runtime:
            scope = await _scope(runtime, "call-styles")
            positional = runtime.lifecycle.for_scope(scope)
            keyword = runtime.lifecycle.for_scope(scope_id=scope)
            assert positional.scope_id == keyword.scope_id == scope

            source_a = await _capture(runtime, scope, "source-a")
            source_b = await _capture(runtime, scope, "source-b")
            assert await positional.register_source("source-a", source_a, revision=3) is True
            assert await keyword.register_source(node_id="source-b", source_ref=source_b, revision=3) is True
            # Replays through the other call style stay idempotent.
            assert await keyword.register_source(node_id="source-a", source_ref=source_a, revision=3) is True
            assert await positional.register_source("source-b", source_b, revision=3) is True

            memory_a = await _remember(runtime, scope, "memory-a")
            memory_b = await _remember(runtime, scope, "memory-b")
            assert await positional.register_memory("memory-a", memory_a, parents=("source-a",), policy="all")
            assert await keyword.register_memory(
                node_id="memory-b", citation=memory_b, parents=("source-a", "source-b"), policy="any"
            )
            assert await keyword.register_memory(
                node_id="memory-a", citation=memory_a, parents=("source-a",), policy="all"
            )

            topic_a = await _topic_ref(runtime, scope, "topic-a")
            topic_b = await _topic_ref(runtime, scope, "topic-b")
            assert await positional.register_topic("topic-a", topic_a, parents=("memory-a",), policy="any")
            assert await keyword.register_topic(
                node_id="topic-b", artifact_ref=topic_b, parents=("memory-b",), policy="all"
            )
            assert await keyword.register_topic(
                node_id="topic-a", artifact_ref=topic_a, parents=("memory-a",), policy="any"
            )

            withdrawn = ["memory-b", "source-b", "topic-b"]
            assert await positional.apply(_event("source-a", 1, False)) == withdrawn
            assert await keyword.apply(event=_event("source-a", 1, False)) == withdrawn
            assert await keyword.apply(event=_event("source-a", 2, True)) == sorted([
                "memory-a",
                "memory-b",
                "source-a",
                "source-b",
                "topic-a",
                "topic-b",
            ])

            assert await positional.resolve("memory-a", purpose="generation") == memory_a
            assert await keyword.resolve(node_id="memory-a", purpose="audit") == memory_a
            assert await keyword.resolve(node_id="topic-b", purpose="generation") == topic_b
            assert await keyword.resolve(node_id="source-a", purpose="generation") == source_a

            with pytest.raises(SourceLifecycleError):
                await keyword.resolve(node_id="missing", purpose="audit")
            with pytest.raises(TypeError):
                await keyword.register_source("source-c", source_a, 3)  # revision stays keyword-only

    asyncio.run(run())


def test_sequence_semantics_allow_gaps_and_replays() -> None:
    async def run() -> None:
        async with _runtime() as runtime:
            scope = await _scope(runtime, "sequence")
            lifecycle = runtime.lifecycle.for_scope(scope)
            await _src(runtime, lifecycle, scope, "a")
            assert await lifecycle.apply(_event("a", 5, False)) == []
            assert await lifecycle.apply(_event("a", 5, False)) == []
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("a", 5, False, reason="other"))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("a", 4, False))
            assert await lifecycle.apply(_event("a", 9, True)) == ["a"]
            # Replaying an older recorded event is a no-op that reports current state.
            assert await lifecycle.apply(_event("a", 5, False)) == ["a"]
            assert await lifecycle.apply(_event("a", 10, True)) == ["a"]
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("a", 11, True, revision=4))

    asyncio.run(run())


def test_registration_rejects_unreadable_or_conflicting_bindings() -> None:
    async def run() -> None:
        async with _runtime() as runtime:
            scope = await _scope(runtime, "registration")
            other = await _scope(runtime, "registration-other")
            lifecycle = runtime.lifecycle.for_scope(scope)
            ref = await _src(runtime, lifecycle, scope, "a")
            citation = await _mem(runtime, lifecycle, scope, "m", ("a",), "all")

            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("b", ref, revision=True)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("m", ref, revision=3)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("ghost", SourceRef(source_type="content", source_id="nope"), revision=1)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("o", await _capture(runtime, other, "o"), revision=1)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("str", citation, parents="a", policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("alias", citation, parents=("a",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory(
                    "foreign", await _remember(runtime, other, "foreign"), parents=("a",), policy="all"
                )
            fake = MemoryCitation.model_validate({
                **citation.model_dump(mode="json"),
                "entry_version_id": "mev_" + "0" * 26,
            })
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("fake", fake, parents=("a",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic(
                    "t0",
                    ArtifactRef(family="topic-memory", artifact_id="art_missing", revision=1),
                    parents=("m",),
                    policy="all",
                )
            topic = await _topic(runtime, lifecycle, scope, "t", ("m",), "all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic("t", topic, parents=("a",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("m", 1, False))

    asyncio.run(run())


def test_diamond_any_all_propagation_keeps_entry_versions() -> None:
    async def run() -> None:
        async with _runtime() as runtime:
            scope = await _scope(runtime, "diamond")
            lifecycle = runtime.lifecycle.for_scope(scope)
            for source in ("s1", "s2", "s3"):
                await _src(runtime, lifecycle, scope, source)
            await _mem(runtime, lifecycle, scope, "ma", ("s1",), "all")
            await _mem(runtime, lifecycle, scope, "mb", ("s1", "s2"), "any")
            await _mem(runtime, lifecycle, scope, "mc", ("ma", "mb"), "all")
            await _mem(runtime, lifecycle, scope, "md", ("mc", "s3"), "any")
            await _topic(runtime, lifecycle, scope, "ta", ("mc",), "all")
            await _topic(runtime, lifecycle, scope, "tb", ("md", "mb"), "all")
            everything = ["ma", "mb", "mc", "md", "s1", "s2", "s3", "ta", "tb"]
            original = await _memory_states(runtime, scope)

            assert await lifecycle.apply(_event("s1", 1, False)) == ["mb", "md", "s2", "s3", "tb"]
            assert await lifecycle.apply(_event("s3", 1, False)) == ["mb", "s2"]
            assert await lifecycle.apply(_event("s2", 1, False)) == []
            withdrawn = await _memory_states(runtime, scope)
            assert {state for state, _ in withdrawn.values()} == {"inactive"}
            topics = await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="ta", limit=5))
            assert not topics.hits

            assert await lifecycle.apply(_event("s2", 2, True)) == ["mb", "s2"]
            assert await lifecycle.apply(_event("s1", 2, True)) == sorted(set(everything) - {"s3"})
            assert await lifecycle.apply(_event("s3", 2, True)) == everything
            restored = await _memory_states(runtime, scope)
            assert {state for state, _ in restored.values()} == {"active"}
            assert {k: v[1] for k, v in restored.items()} == {k: v[1] for k, v in original.items()}

    asyncio.run(run())


def test_state_persists_across_restart_with_scope_isolation(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.db"

    async def first() -> tuple[str, str]:
        async with _runtime(path) as runtime:
            one, two = await _scope(runtime, "one"), await _scope(runtime, "two")
            for scope in (one, two):
                lifecycle = runtime.lifecycle.for_scope(scope)
                await _src(runtime, lifecycle, scope, "root")
                await _mem(runtime, lifecycle, scope, "fact", ("root",), "all")
                await _topic(runtime, lifecycle, scope, "topic", ("fact",), "all")
            assert await runtime.lifecycle.for_scope(one).apply(_event("root", 1, False)) == []
            assert await _active_memory(runtime, one) == set()
            assert await _active_memory(runtime, two) == {"fact"}
            search = SearchTopicMemoryRequest(query="topic", limit=5)
            assert not (await runtime.topic_memory.for_scope(one).search(search)).hits
            assert (await runtime.topic_memory.for_scope(two).search(search)).hits
            return one, two

    async def second(one: str, two: str) -> None:
        async with _runtime(path) as runtime:
            lifecycle_one = runtime.lifecycle.for_scope(scope_id=one)
            lifecycle_two = runtime.lifecycle.for_scope(scope_id=two)
            with pytest.raises(SourceLifecycleError):
                await lifecycle_one.resolve(node_id="topic", purpose="generation")
            assert await lifecycle_two.resolve(node_id="topic", purpose="generation")
            assert await lifecycle_one.apply(_event("root", 1, False)) == []
            assert await lifecycle_two.apply(_event("root", 1, False)) == []
            assert await lifecycle_one.apply(_event("root", 2, True)) == ["fact", "root", "topic"]

    one, two = asyncio.run(first())
    asyncio.run(second(one, two))


def test_storage_failure_during_propagation_is_completed_by_retry(tmp_path: Path) -> None:
    path = tmp_path / "interrupt.db"

    async def fail_withdraw() -> tuple[str, dict[str, MemoryCitation]]:
        async with _runtime(path) as runtime:
            scope = await _scope(runtime, "interrupt")
            lifecycle = runtime.lifecycle.for_scope(scope)
            await _src(runtime, lifecycle, scope, "r")
            citations = {name: await _mem(runtime, lifecycle, scope, name, ("r",), "all") for name in ("x", "y")}
            before = (await runtime.memory.for_scope(scope).list()).memory_ref
            with sqlite3.connect(path) as connection:
                connection.execute(_FAIL_MEMORY_WRITES)
            with pytest.raises(Exception):  # noqa: B017
                await lifecycle.apply(_event("r", 1, False))
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == before
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER fail_memory")
            return scope, citations

    async def retry_after_restart(scope: str) -> None:
        async with _runtime(path) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(_event("r", 1, False)) == []
            assert await _active_memory(runtime, scope) == set()
            after = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert await lifecycle.apply(_event("r", 1, False)) == []
            assert (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref == after

    async def retry_in_process(scope: str, citations: dict[str, MemoryCitation]) -> None:
        async with _runtime(path) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            with sqlite3.connect(path) as connection:
                connection.execute(_FAIL_MEMORY_WRITES)
            with pytest.raises(Exception):  # noqa: B017
                await lifecycle.apply(_event("r", 2, True))
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER fail_memory")
            assert await lifecycle.apply(_event("r", 2, True)) == ["r", "x", "y"]
            assert await _active_memory(runtime, scope) == {"x", "y"}
            for name, citation in citations.items():
                read = await runtime.memory.for_scope(scope).get(GetMemoryEntryRequest(citation=citation))
                assert read.entry.text == f"{name} fixture evidence"
                assert await lifecycle.resolve(name, purpose="generation") == citation

    scope, citations = asyncio.run(fail_withdraw())
    asyncio.run(retry_after_restart(scope))
    asyncio.run(retry_in_process(scope, citations))


def test_failed_registration_reconcile_is_completed_by_reregistering(tmp_path: Path) -> None:
    path = tmp_path / "register.db"

    async def run() -> None:
        async with _runtime(path) as runtime:
            scope = await _scope(runtime, "register-retry")
            lifecycle = runtime.lifecycle.for_scope(scope)
            await _src(runtime, lifecycle, scope, "r")
            await lifecycle.apply(_event("r", 1, False))
            citation = await _remember(runtime, scope, "late")
            with sqlite3.connect(path) as connection:
                connection.execute(_FAIL_MEMORY_WRITES)
            with pytest.raises(Exception):  # noqa: B017
                await lifecycle.register_memory("late", citation, parents=("r",), policy="all")
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER fail_memory")
            assert (
                await lifecycle.register_memory(node_id="late", citation=citation, parents=("r",), policy="all")
                is False
            )
            assert await _active_memory(runtime, scope) == set()

    asyncio.run(run())
