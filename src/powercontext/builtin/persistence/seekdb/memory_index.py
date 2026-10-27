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

"""seekdb hybrid Memory search: FULLTEXT and vector candidates in one native statement."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pyobvector import VECTOR
from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

FTS_CHANNEL = "fts"
VECTOR_CHANNEL = "vector"

# Each branch keeps the predicates, ordering and LIMIT of the frozen single-channel
# statements, so the database applies scope, artifact, tag and candidate-limit
# filters per channel before the rows leave the engine. UNION ALL does not order
# the combined rows; ``split_hybrid_rows`` restores each channel's ranking.
_FTS_BRANCH_SQL = """
SELECT
    'fts' AS channel,
    f.memory_artifact_id,
    f.head_revision,
    f.entry_id,
    f.entry_version_id,
    v.text,
    MATCH (f.searchable_text) AGAINST (:fts_query) AS score,
    NULL AS distance
FROM pc_memory_entry_heads AS f
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = f.scope_id
 AND v.memory_artifact_id = f.memory_artifact_id
 AND v.entry_version_id = f.entry_version_id
WHERE f.scope_id = :scope_id
  AND f.memory_artifact_id IN :memory_artifact_ids
  AND MATCH (f.searchable_text) AGAINST (:fts_query)
{tag_predicate}
ORDER BY score DESC, f.memory_artifact_id, f.entry_id, f.entry_version_id
LIMIT :candidate_limit
"""
_VECTOR_BRANCH_SQL = """
SELECT
    'vector' AS channel,
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    v.text,
    NULL AS score,
    l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
{tag_predicate}
ORDER BY l2_distance(m.embedding, :query_vector){approximate}
LIMIT :candidate_limit
"""


def hybrid_candidate_sql(*, tagged: bool) -> str:
    """Return the single hybrid candidate statement text.

    Tagged searches drop ``APPROXIMATE`` exactly like the frozen vector channel so
    the tag predicate is applied before the top-k cut instead of after it.
    """

    fts = _FTS_BRANCH_SQL.format(tag_predicate=memory_tag_sql("f") if tagged else "")
    vector = _VECTOR_BRANCH_SQL.format(
        tag_predicate=memory_tag_sql("m") if tagged else "",
        approximate="" if tagged else " APPROXIMATE",
    )
    return f"({fts.strip()})\nUNION ALL\n({vector.strip()})"


def hybrid_candidate_statement(dimension: int, *, tagged: bool) -> TextClause:
    expanding = ("memory_artifact_ids", "tag_keys", "tag_hashes") if tagged else ("memory_artifact_ids",)
    return text(hybrid_candidate_sql(tagged=tagged)).bindparams(
        *(bindparam(name, expanding=True) for name in expanding),
        bindparam("query_vector", type_=VECTOR(dimension)),
    )


def _row_key(row: Mapping[Any, Any]) -> tuple[str, str, str]:
    return (str(row["memory_artifact_id"]), str(row["entry_id"]), str(row["entry_version_id"]))


def split_hybrid_rows(
    rows: Iterable[Mapping[Any, Any]],
) -> tuple[list[Mapping[Any, Any]], list[Mapping[Any, Any]]]:
    """Split UNION ALL rows by channel and restore each channel's SQL ordering."""

    fts: list[Mapping[Any, Any]] = []
    vector: list[Mapping[Any, Any]] = []
    for row in rows:
        channel = str(row["channel"])
        if channel == FTS_CHANNEL:
            fts.append(row)
        elif channel == VECTOR_CHANNEL:
            vector.append(row)
        else:
            raise ValueError(f"unknown hybrid candidate channel {channel!r}")  # noqa: TRY003
    fts.sort(key=lambda row: (-float(row["score"]), *_row_key(row)))
    vector.sort(key=lambda row: (float(row["distance"]), *_row_key(row)))
    return fts, vector


class SeekDBHybridMemoryIndex(CompositeMemoryIndex):
    """FTS and vector projections whose hybrid candidates come from one statement.

    FTS-only and vector-only searches, writes, completeness checks and hydration
    are delegated unchanged to the composed single-channel indexes.
    """

    def __init__(self, fts: OceanBaseMemoryFTSIndex, vector: OceanBaseMemoryVectorIndex) -> None:
        super().__init__(fts, vector)
        self._vector = vector
        dimension = vector.profile.dimension
        self._statement = hybrid_candidate_statement(dimension, tagged=False)
        self._tagged_statement = hybrid_candidate_statement(dimension, tagged=True)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode != "hybrid":
            return await super().search(connection, scope_id, request)
        profile = self._vector.profile
        if request.embedding_profile != profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            await connection.execute(
                self._statement if request.tag_filter is None else self._tagged_statement,
                {
                    "scope_id": scope_id,
                    "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                    "fts_query": request.analyzed_query,
                    "query_vector": validate_embedding(request.query_vector, dimension=profile.dimension),
                    "candidate_limit": request.candidate_limit,
                    **memory_tag_parameters(request.tag_filter),
                },
            )
        ).mappings()
        fts, vector = split_hybrid_rows(rows)
        return MemorySearchChannels(
            fts=memory_channel_hits(fts, request.memories),
            vector=memory_channel_hits(vector, request.memories),
        )
