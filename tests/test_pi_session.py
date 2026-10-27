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
import json
from pathlib import Path
from typing import Any

import pytest

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.observations import SourceObservation
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiEventSource,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

HEADER = {"type": "session", "version": 3, "id": "s"}


def _raw(records: list[dict[str, Any]], *, header: dict[str, Any] | None = None, newline: str = "\n") -> bytes:
    lines = [json.dumps(record, ensure_ascii=False) for record in [header or HEADER, *records]]
    return (newline.join(lines) + newline).encode()


def _user(node_id: str, parent: str | None, text: str) -> dict[str, Any]:
    return {"type": "message", "id": node_id, "parentId": parent, "message": {"role": "user", "content": text}}


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_line_separators_inside_values_do_not_split_lines(separator: str) -> None:
    text = f"before{separator}after"
    raw = _raw([_user("a", None, text), _user("b", "a", "next")])
    assert separator.encode() in raw
    events = parse_pi_session(PiSessionInput(content=raw))
    assert [event.content for event in events] == [f"user: {text}", "user: next"]


def test_crlf_bom_and_blank_lines_are_tolerated() -> None:
    raw = b"\xef\xbb\xbf" + _raw([_user("a", None, "x\u2028y")], newline="\r\n") + b"\r\n \t\r\n"
    assert [event.content for event in parse_pi_session(PiSessionInput(content=raw))] == ["user: x\u2028y"]


def test_raw_separator_between_records_is_not_a_line_break() -> None:
    first, second = (json.dumps(record) for record in (HEADER, _user("a", None, "x")))
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=f"{first}\u2028{second}\n".encode()))


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "message", "id": "a", "message": {"role": "user", "content": "x"}}],
        [_user("a", None, "x"), {"type": "message", "id": "b", "message": {"role": "user", "content": "y"}}],
        [_user("a", None, "x"), {"type": "model_change", "id": "m"}],
        [_user("a", None, "x"), {"type": "session", "version": 3, "id": "s2"}],
        [_user("a", None, "x"), {"type": "session", "version": 3, "id": "s2", "parentId": "a"}],
    ],
)
def test_entries_must_declare_parent_and_header_is_unique(records: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=_raw(records)))


def test_null_parent_entries_are_roots() -> None:
    events = parse_pi_session(PiSessionInput(content=_raw([_user("a", None, "x"), _user("b", None, "y")])))
    assert [(event.event_id, event.parent_id) for event in events] == [("b", None)]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_json_constants_are_rejected(constant: str) -> None:
    line = '{"type":"message","id":"a","parentId":null,"message":{"role":"user","content":"x"},"cost":' + constant + "}"
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=(json.dumps(HEADER) + "\n" + line + "\n").encode()))


@pytest.mark.parametrize("session_id", ["", " ", "\t\u3000"])
def test_blank_session_ids_are_rejected(session_id: str) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=_raw([_user("a", None, "x")], header={**HEADER, "id": session_id})))


def test_deeply_nested_record_is_value_error() -> None:
    line = '{"type":"custom","id":"a","parentId":null,"data":' + "[" * 100_000 + "]" * 100_000 + "}"
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=(json.dumps(HEADER) + "\n" + line + "\n").encode()))


def _tool_result_session(text: str) -> bytes:
    result = {
        "type": "message",
        "id": "r",
        "parentId": "u",
        "message": {
            "role": "toolResult",
            "toolName": "read",
            "toolCallId": "c1",
            "content": [{"type": "text", "text": text}],
        },
    }
    return _raw([_user("u", None, "read the file"), result])


def test_source_payload_derives_content_from_record() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        text = "payload body " * 50
        events = parse_pi_session(PiSessionInput(content=_tool_result_session(text)))
        source = await registry.resolve(events[1])
        assert isinstance(source, PiEventSource)
        assert source.content is None
        assert text not in json.dumps(source.model_dump(exclude={"record"}), ensure_ascii=False)
        assert await registry.read(source) == events[1]
        projected = registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY)
        assert projected["content"] == f"toolResult: [tool_result read c1] {text}"
        assert projected["metadata"] == {
            "session_id": "s",
            "event_id": "r",
            "parent_id": "u",
            "event_type": "message",
            "role": "toolResult",
        }

    asyncio.run(scenario())


def test_explicit_content_that_differs_from_record_round_trips() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        event = PiEventInput(
            session_id="s",
            event_id="x",
            parent_id=None,
            event_type="custom",
            role="custom",
            content="custom: provided",
            record={"type": "custom", "id": "x", "parentId": None},
        )
        source = await registry.resolve(event)
        assert await registry.read(source) == event
        assert registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY)["content"] == "custom: provided"

    asyncio.run(scenario())


def test_two_million_character_tool_result_persists_through_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        text = ("line of tool output abc\n" * 90_000)[:2_000_000]
        events = parse_pi_session(PiSessionInput(content=_tool_result_session(text)))
        observations = [project_source_for_transport(registry, await registry.resolve(event)) for event in events]
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pi.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Pi", summary="Pi", idempotency_key="pi"))
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
            for observation in (*observations, observations[1]):
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
        async with open_builtin_contexts(config) as contexts:
            entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
        stored = {entry.source_ref.source_id: entry.source for entry in entries}
        assert stored == {observation.name: observation for observation in observations}
        big = stored[observations[1].name]
        assert isinstance(big, SourceObservation)
        assert big.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"] == f"toolResult: [tool_result read c1] {text}"
        assert await registry.read(PiEventSource.model_validate(big.payload)) == events[1]

    asyncio.run(scenario())
