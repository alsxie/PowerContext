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

"""Pi v3 session branch history as immutable captured event Sources."""

from __future__ import annotations

import hashlib
import json
from os import PathLike
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

#: Source Definition name of one captured Pi session event.
PI_EVENT_SOURCE_NAME = "pi-event"
#: Connector name that imports one Pi session branch.
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
_CHECKPOINT_SCHEMA = "powercontext.pi-session-history.v1"


class PiSessionInput(BaseModel):
    """Raw Pi v3 JSONL session bytes and the optional leaf whose branch is imported."""

    model_config = ConfigDict(frozen=True)
    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi event of a branch, with its original JSON record and rendered text."""

    model_config = ConfigDict(frozen=True)
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured Pi event; the event text is derived from ``record`` instead of stored twice.

    ``content`` is only kept when an input's text differs from the text derived from its record,
    so large tool results are not duplicated inside the persisted Source payload.
    """

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    record: dict[str, JsonValue]
    content: str | None = None


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable Source name of one Pi event.

    The readable ``pi:<session_id>:<event_id>`` form is percent-encoded; names over the
    Source identity limit fall back to ``pi-h:<sha256>`` of both identifiers.
    """
    direct = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(direct) <= MAX_SOURCE_ID_LENGTH:
        return direct
    digest = hashlib.sha256(json.dumps([session_id, event_id], ensure_ascii=False).encode()).hexdigest()
    return f"pi-h:{digest}"


def _reject_constant(value: str) -> None:
    raise ValueError(f"Pi session line contains non-standard JSON constant {value}")  # noqa: TRY003


def _load(content: bytes) -> tuple[str, dict[str, dict[str, JsonValue]], list[str]]:  # noqa: C901
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session must be UTF-8") from error  # noqa: TRY003
    records = []
    # JSONL lines end only at "\n"; str.splitlines() would also split on U+2028, U+2029, U+0085 and
    # other characters that JSON writers emit verbatim inside string values.
    for line in text.split("\n"):
        if not line.strip(" \t\r"):
            continue
        try:
            record = json.loads(line, parse_constant=_reject_constant)
        except json.JSONDecodeError as error:
            raise ValueError("Pi session line is not JSON") from error  # noqa: TRY003
        except RecursionError as error:
            raise ValueError("Pi session line is nested too deeply") from error  # noqa: TRY003
        if not isinstance(record, dict):
            raise ValueError("Pi session line must be a JSON object")  # noqa: TRY003, TRY004
        records.append(record)
    if not records:
        raise ValueError("Pi session is empty")  # noqa: TRY003
    header = records[0]
    session_id = header.get("id")
    if (
        header.get("type") != "session"
        or type(header.get("version")) is not int
        or header["version"] != 3
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise ValueError("Pi session header must be type=session version=3 with an id")  # noqa: TRY003
    nodes: dict[str, dict[str, JsonValue]] = {}
    order: list[str] = []
    for record in records[1:]:
        if record.get("type") == "session":
            raise ValueError("Pi session must contain exactly one session header")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError("Pi entry must declare parentId")  # noqa: TRY003
        node_id, parent = record.get("id"), record["parentId"]
        if not isinstance(node_id, str) or not node_id or node_id in nodes:
            raise ValueError("Pi entry ids must be unique non-empty strings")  # noqa: TRY003
        if parent is not None and (not isinstance(parent, str) or parent not in nodes):
            raise ValueError("Pi entry parent must reference an earlier entry")  # noqa: TRY003
        nodes[node_id] = record
        order.append(node_id)
    return session_id, nodes, order


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            text
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(text := block.get("text"), str)
        )
    return ""


def _visible(record: dict[str, JsonValue]) -> tuple[str, str] | None:  # noqa: C901
    kind = record.get("type")
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str):
            return None
        if role == "toolResult":
            body = (
                f"[tool_result {message.get('toolName')} {message.get('toolCallId')}] {_text(message.get('content'))}"
            )
        elif role == "bashExecution":
            body = f"$ {message.get('command', '')}\n{message.get('output', '')}"
        else:
            content = message.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif block.get("type") == "toolCall":
                        arguments = json.dumps(
                            block.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        parts.append(f"[tool_call {block.get('name')} {block.get('id')}] {arguments}")
            body = "\n".join(parts)
    elif kind in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        role, body = str(kind), summary if isinstance(summary, str) else ""
    elif kind == "custom_message":
        role, body = "custom", _text(record.get("content"))
    else:
        return None
    return (role, body) if body.strip() else None


def _branch(nodes: dict[str, dict[str, JsonValue]], order: list[str], leaf_id: str | None) -> list[str]:
    if leaf_id is None:
        if not order:
            return []
        leaf_id = order[-1]
    if leaf_id not in nodes:
        raise ValueError("Pi leaf does not exist")  # noqa: TRY003
    chain, seen, current = [], set(), leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("Pi entry graph contains a cycle")  # noqa: TRY003
        seen.add(current)
        chain.append(current)
        current = nodes[current].get("parentId")  # type: ignore[assignment]
    return chain[::-1]


def _content(record: dict[str, JsonValue]) -> str | None:
    visible = _visible(record)
    return None if visible is None else f"{visible[0]}: {visible[1]}"


def _event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    visible = _visible(record)
    if visible is None:
        return None
    role, body = visible
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],  # type: ignore[arg-type]
        parent_id=record.get("parentId"),  # type: ignore[arg-type]
        event_type=record["type"],  # type: ignore[arg-type]
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the visible events on the branch ending at ``leaf_id`` (default: last entry).

    The whole session is validated first: UTF-8 JSONL objects, one v3 session header,
    unique entry ids and parents that reference earlier entries. Any violation or an
    unknown leaf raises ``ValueError`` and no events are returned.
    """
    session_id, nodes, order = _load(value.content)
    events = (_event(session_id, nodes[node_id]) for node_id in _branch(nodes, order, value.leaf_id))
    return tuple(event for event in events if event is not None)


class PiEventSourceAdapter:
    """Resolve Pi event inputs to captured Sources and read them back unchanged."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        fields = value.model_dump()
        if fields["content"] == _content(value.record):
            fields["content"] = None
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            **fields,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        fields = source.model_dump(include=set(PiEventInput.model_fields))
        fields["content"] = _source_content(source)
        return PiEventInput.model_validate(fields)


def _source_content(source: PiEventSource) -> str:
    if source.content is not None:
        return source.content
    derived = _content(source.record)
    if derived is None:
        raise ValueError("Pi event Source record has no visible content")  # noqa: TRY003
    return derived


class PiEventTextEvidenceProjection:
    """Project a Pi event to ``role: text`` evidence with its branch identity metadata."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=_source_content(source),
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


#: Source Definition for captured Pi events with a text-evidence projection.
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PiEventSourceAdapter(), version="1", projections=(PiEventTextEvidenceProjection(),)
)


def _digest(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _imported(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema") != _CHECKPOINT_SCHEMA
        or not isinstance(checkpoint.get("imported"), dict)
        or not all(isinstance(value, str) for value in checkpoint["imported"].values())
    ):
        raise ValueError("unrecognized Pi session checkpoint")  # noqa: TRY003
    return dict(checkpoint["imported"])  # type: ignore[arg-type]


class PiSessionConnector:
    """Import the visible events of one Pi session file branch idempotently.

    The checkpoint records the digest of every imported event; a later run submits
    only new branch events and fails before submitting anything if an imported
    record was rewritten.
    """

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | PathLike[str], leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        imported = _imported(session.checkpoint)
        session_id, nodes, order = _load(self.path.read_bytes())
        branch = _branch(nodes, order, self.leaf_id)
        # Detect rewrites of any previously imported record before submitting anything.
        for node_id in order:
            identity = pi_event_source_id(session_id, node_id)
            if identity in imported and imported[identity] != _digest(nodes[node_id]):
                raise ValueError(f"imported Pi event {node_id!r} was rewritten")  # noqa: TRY003
        pending = [
            event
            for event in (_event(session_id, nodes[node_id]) for node_id in branch)
            if event is not None and pi_event_source_id(session_id, event.event_id) not in imported
        ]
        updated = dict(imported)
        for event in pending:
            identity = pi_event_source_id(session_id, event.event_id)
            outcome = await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            if outcome.source_ref is not None:
                updated[identity] = _digest(event.record)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"schema": _CHECKPOINT_SCHEMA, "imported": updated},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
