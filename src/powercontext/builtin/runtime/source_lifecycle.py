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

"""Propagate Source withdrawal and restoration through a caller-declared evidence DAG.

Each Scope owns one directed acyclic graph of Source, Memory and Topic Memory
nodes. Source nodes change only through validated lifecycle events; every
derived node is available when its parents satisfy its ``all``/``any``
policy. Availability is durable and drives native state: unsupported Memory
entries are deactivated (their body versions stay readable through exact
citations) and unsupported Topic Memory is filtered out of Topic search and
Prepared Context while its exact ArtifactRef remains readable.

Lifecycle state is committed before native Memory state is reconciled, so a
native write failure leaves a durable target that a retried (replayed) event
completes. Reconciliation only commits entries whose native state differs, so
a successful replay never adds a Memory revision.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryService
from powercontext.builtin.artifacts.memory.errors import MemoryLayerError
from powercontext.builtin.persistence.source_lifecycle import (
    LifecycleEventRecord,
    LifecycleNodeRecord,
    SourceLifecycleRepository,
)
from powercontext.builtin.persistence.tables import MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.errors import PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

__all__ = [
    "LifecyclePolicy",
    "LifecyclePurpose",
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "propagate_availability",
]

LifecyclePolicy = Literal["all", "any"]
LifecyclePurpose = Literal["generation", "audit"]

_POLICIES = frozenset({"all", "any"})
_PURPOSES = frozenset({"generation", "audit"})
_REFERENCE_ERRORS = (PowerContextError, BaseAccessError, LookupError, ValueError, ValidationError)
_WITHDRAWN_REASON = "source-lifecycle-withdrawn"
_RESTORED_REASON = "source-lifecycle-restored"


class SourceLifecycleError(ValueError):
    """Reject an invalid lifecycle registration, event or resolution."""


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")  # noqa: TRY003
    return value


class SourceLifecycleEvent(BaseModel):
    """One ordered availability change of a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: Annotated[StrictStr, Field(min_length=1, max_length=MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH)]
    revision: Annotated[StrictInt, Field(gt=0)]
    sequence: Annotated[StrictInt, Field(gt=0)]
    available: StrictBool
    reason: Annotated[StrictStr, Field(min_length=1)]

    @field_validator("node_id", "reason")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _non_blank(value)


def propagate_availability(
    nodes: Iterable[LifecycleNodeRecord],
    sources: Mapping[str, bool] | None = None,
) -> dict[str, bool]:
    """Recompute availability for nodes in topological (registration) order.

    ``sources`` overrides stored Source availability; derived nodes are always
    recomputed from their parents and policy.
    """

    overrides = {} if sources is None else sources
    states: dict[str, bool] = {}
    for node in nodes:
        if node.kind == "source":
            states[node.node_id] = overrides.get(node.node_id, node.available)
            continue
        support = (states[parent] for parent in node.parents)
        states[node.node_id] = all(support) if node.policy == "all" else any(support)
    return states


class SourceLifecycleApplication:
    """Select the Source lifecycle service for one Scope."""

    def __init__(self, runtime: BuiltinRuntime, repository: SourceLifecycleRepository | None) -> None:
        self._runtime = runtime
        self._repository = repository

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycleService:
        if self._repository is None:
            raise SourceLifecycleError("source lifecycle storage is not configured")  # noqa: TRY003
        return ScopedSourceLifecycleService(self._runtime, self._repository, scope_id)


class ScopedSourceLifecycleService:
    """Register one Scope's evidence DAG and apply Source lifecycle events to it."""

    def __init__(self, runtime: BuiltinRuntime, repository: SourceLifecycleRepository, scope_id: str) -> None:
        self._runtime = runtime
        self._repository = repository
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> bool:
        """Bind a captured Source; Sources start available. Return current availability."""

        node_id = _node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef")  # noqa: TRY003
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")  # noqa: TRY003
        async with self._write():
            nodes = await self._repository.nodes(self.scope_id)
            existing = _by_id(nodes).get(node_id)
            reference = source_ref.model_dump(mode="json")
            if existing is not None:
                _require_same(existing, kind="source", reference=reference, revision=revision)
                return existing.available
            if not await self._repository.source_exists(self.scope_id, source_ref.source_type, source_ref.source_id):
                raise SourceLifecycleError("source is not captured in this scope")  # noqa: TRY003
            node = LifecycleNodeRecord(
                node_id=node_id,
                kind="source",
                reference=reference,
                available=True,
                lifecycle_revision=revision,
            )
            await self._repository.insert_node(self.scope_id, node)
            return True

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: LifecyclePolicy,
    ) -> bool:
        """Bind one exact Memory entry citation and reconcile its native state immediately."""

        node_id = _node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation")  # noqa: TRY003
        parent_ids, policy = _derivation(node_id, parents, policy)
        async with self._write() as context:
            nodes = await self._repository.nodes(self.scope_id)
            reference = citation.model_dump(mode="json")
            existing = _by_id(nodes).get(node_id)
            if existing is not None:
                _require_same(existing, kind="memory", reference=reference, parents=parent_ids, policy=policy)
                await self._reconcile_memory(context.artifacts.memory, context.artifacts.memory_artifact_id, nodes)
                return existing.available
            _require_parents(nodes, parent_ids)
            if any(
                node.kind == "memory"
                and node.artifact_id == citation.memory_ref.artifact_id
                and node.entry_id == citation.entry_id
                for node in nodes
            ):
                raise SourceLifecycleError("memory entry is already bound to another node")  # noqa: TRY003
            await _require_readable_citation(context.artifacts.memory, context.artifacts.memory_artifact_id, citation)
            node = LifecycleNodeRecord(
                node_id=node_id,
                kind="memory",
                reference=reference,
                available=_supported(nodes, parent_ids, policy),
                artifact_id=citation.memory_ref.artifact_id,
                entry_id=citation.entry_id,
                policy=policy,
                parents=parent_ids,
            )
            await self._repository.insert_node(self.scope_id, node)
            await self._reconcile_memory(context.artifacts.memory, context.artifacts.memory_artifact_id, (*nodes, node))
            return node.available

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: LifecyclePolicy,
    ) -> bool:
        """Bind one exact Topic Memory revision; unsupported Topics leave native search."""

        node_id = _node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("artifact_ref must reference a topic-memory Artifact")  # noqa: TRY003
        parent_ids, policy = _derivation(node_id, parents, policy)
        async with self._write():
            nodes = await self._repository.nodes(self.scope_id)
            reference = artifact_ref.model_dump(mode="json")
            existing = _by_id(nodes).get(node_id)
            if existing is not None:
                _require_same(existing, kind="topic", reference=reference, parents=parent_ids, policy=policy)
                return existing.available
            _require_parents(nodes, parent_ids)
            if any(node.kind == "topic" and node.artifact_id == artifact_ref.artifact_id for node in nodes):
                raise SourceLifecycleError("topic memory is already bound to another node")  # noqa: TRY003
            try:
                await self._runtime._records().get_artifact_revision(
                    self.scope_id, artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
                )
            except _REFERENCE_ERRORS as error:
                raise SourceLifecycleError("topic memory revision is not readable") from error  # noqa: TRY003
            node = LifecycleNodeRecord(
                node_id=node_id,
                kind="topic",
                reference=reference,
                available=_supported(nodes, parent_ids, policy),
                artifact_id=artifact_ref.artifact_id,
                policy=policy,
                parents=parent_ids,
            )
            await self._repository.insert_node(self.scope_id, node)
            return node.available

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply or replay one Source event and return every available node id, sorted.

        Replaying any already recorded event (same sequence and content) is an
        idempotent no-op that re-reconciles native state; unseen lower sequences
        and same-sequence events with different content are rejected.
        """

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")  # noqa: TRY003
        async with self._write() as context:
            nodes = await self._repository.nodes(self.scope_id)
            source = _by_id(nodes).get(event.node_id)
            if source is None or source.kind != "source":
                raise SourceLifecycleError("event targets an unknown source")  # noqa: TRY003
            if event.revision != source.lifecycle_revision:
                raise SourceLifecycleError("event revision does not match the source lifecycle revision")  # noqa: TRY003
            record = LifecycleEventRecord(
                node_id=event.node_id,
                sequence=event.sequence,
                revision=event.revision,
                available=event.available,
                reason=event.reason,
            )
            stored = await self._repository.event(self.scope_id, event.node_id, event.sequence)
            if stored is not None:
                if stored != record:
                    raise SourceLifecycleError("event sequence was already used with different content")  # noqa: TRY003
                states = propagate_availability(nodes)
            else:
                if source.last_sequence is not None and event.sequence <= source.last_sequence:
                    raise SourceLifecycleError("event sequence must increase for each source")  # noqa: TRY003
                states = propagate_availability(nodes, {event.node_id: event.available})
                changed = {
                    node.node_id: states[node.node_id] for node in nodes if states[node.node_id] != node.available
                }
                await self._repository.record_event(self.scope_id, record, changed)
                nodes = tuple(_with_state(node, states[node.node_id]) for node in nodes)
            # Native state follows the committed lifecycle state; a failure here is
            # completed by replaying the same event.
            await self._reconcile_memory(context.artifacts.memory, context.artifacts.memory_artifact_id, nodes)
            return sorted(node_id for node_id, available in states.items() if available)

    async def resolve(self, node_id: str, *, purpose: LifecyclePurpose) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return the registered reference; ``generation`` requires the node to be available."""

        if purpose not in _PURPOSES:
            raise SourceLifecycleError("unknown resolution purpose")  # noqa: TRY003
        if not isinstance(node_id, str):
            raise SourceLifecycleError("unknown lifecycle node")  # noqa: TRY003
        async with self._runtime._scope_operation(self.scope_id):
            node = _by_id(await self._repository.nodes(self.scope_id)).get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown lifecycle node")  # noqa: TRY003
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("lifecycle node is unavailable for generation")  # noqa: TRY003
        return _reference_model(node)

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[Any]:
        """Enter the Scope context, then its single-writer lock, like other Runtime writes."""

        async with (
            self._runtime._context(self.scope_id, embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING) as context,
            self._runtime._locked(self.scope_id),
        ):
            yield context

    async def _reconcile_memory(
        self,
        service: MemoryService,
        memory_artifact_id: str,
        nodes: Iterable[LifecycleNodeRecord],
    ) -> None:
        targets = {
            node.entry_id: node.available
            for node in nodes
            if node.kind == "memory" and node.artifact_id == memory_artifact_id and node.entry_id is not None
        }
        if not targets:
            return
        current = await service.head(memory_artifact_id)
        states = {item.entry_id: item.state for item in current.content.manifest.entries}
        entries = {entry.entry_id: entry for entry in await service.entries(current)}
        withdraw = [
            entries[entry_id]
            for entry_id, available in targets.items()
            if not available and states.get(entry_id) == "active" and entry_id in entries
        ]
        restore = [
            entries[entry_id]
            for entry_id, available in targets.items()
            if available and states.get(entry_id) == "inactive" and entry_id in entries
        ]
        if withdraw:
            current = await service.forget(current, entries=withdraw, reason=_WITHDRAWN_REASON)
        if restore:
            if withdraw:
                refreshed = {entry.entry_id: entry for entry in await service.entries(current)}
                restore = [refreshed[entry.entry_id] for entry in restore]
            await service.reactivate(current, entries=restore, reason=_RESTORED_REASON)


def _node_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH:
        raise SourceLifecycleError("node_id must be a non-blank string of at most 128 characters")  # noqa: TRY003
    return value


def _derivation(node_id: str, parents: object, policy: object) -> tuple[tuple[str, ...], LifecyclePolicy]:
    if policy not in _POLICIES:
        raise SourceLifecycleError("policy must be 'all' or 'any'")  # noqa: TRY003
    if isinstance(parents, str | bytes) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents must be a sequence of node ids")  # noqa: TRY003
    parent_ids = tuple(_node_id(parent) for parent in parents)
    if not parent_ids:
        raise SourceLifecycleError("a derived node needs at least one parent")  # noqa: TRY003
    if len(set(parent_ids)) != len(parent_ids):
        raise SourceLifecycleError("parents must not repeat")  # noqa: TRY003
    if node_id in parent_ids:
        raise SourceLifecycleError("a node cannot depend on itself")  # noqa: TRY003
    return parent_ids, cast("LifecyclePolicy", policy)


def _by_id(nodes: Iterable[LifecycleNodeRecord]) -> dict[str, LifecycleNodeRecord]:
    return {node.node_id: node for node in nodes}


def _require_parents(nodes: Sequence[LifecycleNodeRecord], parents: tuple[str, ...]) -> None:
    # Parents must already exist and the graph is immutable, so a new node can
    # never close a cycle.
    known = _by_id(nodes)
    missing = [parent for parent in parents if parent not in known]
    if missing:
        raise SourceLifecycleError("parents must be registered before their derived node")  # noqa: TRY003


def _supported(nodes: Sequence[LifecycleNodeRecord], parents: tuple[str, ...], policy: LifecyclePolicy) -> bool:
    known = _by_id(nodes)
    support = (known[parent].available for parent in parents)
    return all(support) if policy == "all" else any(support)


def _require_same(
    existing: LifecycleNodeRecord,
    *,
    kind: str,
    reference: Mapping[str, Any],
    revision: int | None = None,
    parents: tuple[str, ...] = (),
    policy: str | None = None,
) -> None:
    if (
        existing.kind != kind
        or existing.reference != dict(reference)
        or existing.lifecycle_revision != revision
        or existing.parents != parents
        or existing.policy != policy
    ):
        raise SourceLifecycleError("node id is already registered with a different binding")  # noqa: TRY003


async def _require_readable_citation(service: MemoryService, memory_artifact_id: str, citation: MemoryCitation) -> None:
    try:
        if citation.memory_ref.artifact_id != memory_artifact_id:
            raise SourceLifecycleError("citation does not belong to this scope's Memory")  # noqa: TRY003, TRY301
        memory = await service.revision(citation.memory_ref)
        if not any(
            item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
            for item in memory.content.manifest.entries
        ):
            raise SourceLifecycleError("citation entry is not part of its Memory revision")  # noqa: TRY003, TRY301
        await service.validate_citation(citation)
    except SourceLifecycleError:
        raise
    except (*_REFERENCE_ERRORS, MemoryLayerError) as error:
        raise SourceLifecycleError("memory citation is not readable") from error  # noqa: TRY003


def _with_state(node: LifecycleNodeRecord, available: bool) -> LifecycleNodeRecord:
    return node if node.available == available else replace(node, available=available)


def _reference_model(node: LifecycleNodeRecord) -> SourceRef | MemoryCitation | ArtifactRef:
    if node.kind == "source":
        return SourceRef.model_validate(node.reference)
    if node.kind == "memory":
        return MemoryCitation.model_validate(node.reference)
    return ArtifactRef.model_validate(node.reference)
