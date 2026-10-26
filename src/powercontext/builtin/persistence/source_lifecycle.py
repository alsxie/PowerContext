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

"""Durable Source lifecycle bindings, events and derived availability."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import false, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
    SOURCES_TABLE,
)

LifecycleNodeKind = Literal["source", "memory", "topic"]
LifecyclePolicy = Literal["all", "any"]

_NODES = SOURCE_LIFECYCLE_NODES_TABLE
_EVENTS = SOURCE_LIFECYCLE_EVENTS_TABLE


@dataclass(frozen=True)
class LifecycleNodeRecord:
    """One registered node of a Scope's evidence DAG."""

    node_id: str
    kind: LifecycleNodeKind
    reference: dict[str, Any]
    available: bool
    artifact_id: str | None = None
    entry_id: str | None = None
    lifecycle_revision: int | None = None
    last_sequence: int | None = None
    policy: LifecyclePolicy | None = None
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleEventRecord:
    """One accepted Source lifecycle event."""

    node_id: str
    sequence: int
    revision: int
    available: bool
    reason: str


def canonical_reference(reference: Mapping[str, Any]) -> str:
    return json.dumps(reference, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SourceLifecycleRepository:
    """Persist one evidence DAG per Scope; callers serialize writes per Scope."""

    def __init__(self, database: AsyncDatabase) -> None:
        self.database = database

    async def nodes(self, scope_id: str, /) -> tuple[LifecycleNodeRecord, ...]:
        """Return all nodes in registration (topological) order."""

        async with self.database.transaction() as connection:
            rows = (
                (
                    await connection.execute(
                        select(_NODES).where(_NODES.c.scope_id == scope_id).order_by(_NODES.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_node_record(row) for row in rows)

    async def source_exists(self, scope_id: str, source_type: str, source_id: str, /) -> bool:
        async with self.database.transaction() as connection:
            row = (
                await connection.execute(
                    select(SOURCES_TABLE.c.source_id).where(
                        SOURCES_TABLE.c.scope_id == scope_id,
                        SOURCES_TABLE.c.source_type == source_type,
                        SOURCES_TABLE.c.source_id == source_id,
                    )
                )
            ).one_or_none()
        return row is not None

    async def insert_node(self, scope_id: str, node: LifecycleNodeRecord, /) -> None:
        async with self.database.transaction() as connection:
            last = (
                await connection.execute(select(func.max(_NODES.c.ordinal)).where(_NODES.c.scope_id == scope_id))
            ).scalar_one_or_none()
            await connection.execute(
                insert(_NODES).values(
                    scope_id=scope_id,
                    node_id=node.node_id,
                    ordinal=(0 if last is None else int(last)) + 1,
                    kind=node.kind,
                    reference=canonical_reference(node.reference),
                    artifact_id=node.artifact_id,
                    entry_id=node.entry_id,
                    lifecycle_revision=node.lifecycle_revision,
                    last_sequence=node.last_sequence,
                    policy=node.policy,
                    parents=None if node.kind == "source" else json.dumps(list(node.parents)),
                    available=node.available,
                )
            )

    async def event(self, scope_id: str, node_id: str, sequence: int, /) -> LifecycleEventRecord | None:
        async with self.database.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(_EVENTS).where(
                            _EVENTS.c.scope_id == scope_id,
                            _EVENTS.c.node_id == node_id,
                            _EVENTS.c.sequence == sequence,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return LifecycleEventRecord(
            node_id=row["node_id"],
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=row["reason"],
        )

    async def record_event(
        self,
        scope_id: str,
        event: LifecycleEventRecord,
        states: Mapping[str, bool],
        /,
    ) -> None:
        """Atomically store the event, the Source cursor and every changed derived state."""

        async with self.database.transaction() as connection:
            await connection.execute(
                insert(_EVENTS).values(
                    scope_id=scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                )
            )
            await connection.execute(
                update(_NODES)
                .where(_NODES.c.scope_id == scope_id, _NODES.c.node_id == event.node_id)
                .values(last_sequence=event.sequence)
            )
            for node_id, available in states.items():
                await connection.execute(
                    update(_NODES)
                    .where(_NODES.c.scope_id == scope_id, _NODES.c.node_id == node_id)
                    .values(available=available)
                )


async def suppressed_topic_artifact_ids(connection: AsyncConnection, scope_id: str, /) -> frozenset[str]:
    """Return Topic Memory identities whose registered evidence is currently unavailable."""

    rows = await connection.execute(
        select(_NODES.c.artifact_id).where(
            _NODES.c.scope_id == scope_id,
            _NODES.c.kind == "topic",
            _NODES.c.available == false(),
        )
    )
    return frozenset(str(value) for value in rows.scalars() if value is not None)


def _node_record(row: Mapping[str, Any]) -> LifecycleNodeRecord:
    parents = row["parents"]
    return LifecycleNodeRecord(
        node_id=row["node_id"],
        kind=row["kind"],
        reference=json.loads(row["reference"]),
        available=bool(row["available"]),
        artifact_id=row["artifact_id"],
        entry_id=row["entry_id"],
        lifecycle_revision=None if row["lifecycle_revision"] is None else int(row["lifecycle_revision"]),
        last_sequence=None if row["last_sequence"] is None else int(row["last_sequence"]),
        policy=row["policy"],
        parents=() if parents is None else tuple(json.loads(parents)),
    )
