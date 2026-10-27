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

"""seekdb hybrid candidates: SQL shape, row splitting and statement count without a database."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.fusion import fuse_rankings
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.persistence.oceanbase.profile import OceanBaseConfig
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import (
    SeekDBHybridMemoryIndex,
    hybrid_candidate_sql,
    hybrid_candidate_statement,
    split_hybrid_rows,
)
from powercontext.builtin.runtime import composition
from powercontext.builtin.tags import TagFilter

PROFILE = EmbeddingProfile(profile_id="hybrid-unit", model="unit", dimension=3, distance="l2", normalization="unit")
HEAD = ArtifactRef(family="memory", artifact_id="memory-a", revision=7)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConnection:
    """Records every statement; answers the hybrid statement with canned UNION ALL rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> _Result:
        self.executed.append((str(statement.compile(dialect=mysql.dialect())), dict(parameters or {})))
        return _Result(self.rows)


def _row(channel: str, entry: str, *, score: float | None = None, distance: float | None = None, revision: int = 3):
    return {
        "channel": channel,
        "memory_artifact_id": "memory-a",
        "head_revision": revision,
        "entry_id": entry,
        "entry_version_id": f"{entry}-v1",
        "text": f"text {entry}",
        "score": score,
        "distance": distance,
    }


def _request(mode: str = "hybrid", *, tag_filter: TagFilter | None = None, profile=PROFILE) -> MemorySearchRequest:
    return MemorySearchRequest(
        query="alpha beta",
        analyzed_query="alpha beta",
        memories=(HEAD,),
        candidate_limit=32,
        mode=mode,
        query_vector=(1.0, 0.0, 0.0),
        embedding_profile=profile,
        tag_filter=tag_filter,
    )


def _index(*, complete: bool = True) -> tuple[SeekDBHybridMemoryIndex, list[int]]:
    vector = OceanBaseMemoryVectorIndex(PROFILE)
    calls: list[int] = []

    async def vector_complete(_connection, _scope_id, _memories, _profile) -> bool:
        calls.append(1)
        return complete

    vector.vector_complete = vector_complete  # type: ignore[method-assign]
    return SeekDBHybridMemoryIndex(OceanBaseMemoryFTSIndex(), vector), calls


def test_hybrid_sql_contains_both_native_channels_with_pushed_down_filters() -> None:
    sql = hybrid_candidate_sql(tagged=False)
    assert "pc_memory_entry_heads" in sql
    assert "pc_memory_vector_entries" in sql
    assert "AGAINST" in sql.upper()
    assert "l2_distance" in sql.lower()
    assert sql.count("UNION ALL") == 1
    assert sql.count("LIMIT :candidate_limit") == 2
    assert sql.count("scope_id = :scope_id") == 2
    assert sql.count("memory_artifact_id IN :memory_artifact_ids") == 2
    assert "APPROXIMATE" in sql
    assert "pc_artifact_tags" not in sql


def test_tagged_hybrid_sql_filters_both_channels_before_exact_top_k() -> None:
    sql = hybrid_candidate_sql(tagged=True)
    assert sql.count("pc_artifact_tags") == 2
    assert "tags.target_id = f.entry_id" in sql
    assert "tags.target_id = m.entry_id" in sql
    # Like the frozen vector channel, the tag predicate must apply before the
    # top-k cut, so the tagged branch keeps the exact ordering.
    assert "APPROXIMATE" not in sql


@pytest.mark.parametrize("tagged", [False, True])
def test_hybrid_statement_compiles_with_expanding_parameters(tagged: bool) -> None:
    compiled = hybrid_candidate_statement(3, tagged=tagged).compile(dialect=mysql.dialect())
    assert "memory_artifact_ids" in compiled.binds
    assert compiled.binds["memory_artifact_ids"].expanding
    assert ("tag_keys" in compiled.binds) is tagged


def test_split_restores_channel_order_regardless_of_union_order() -> None:
    rows = [
        _row("vector", "e3", distance=0.5),
        _row("fts", "e2", score=1.0),
        _row("vector", "e1", distance=0.1),
        _row("fts", "e4", score=2.0),
        _row("fts", "e1", score=1.0),
        _row("vector", "e2", distance=0.1),
    ]
    fts, vector = split_hybrid_rows(rows)
    assert [row["entry_id"] for row in fts] == ["e4", "e1", "e2"]
    assert [row["entry_id"] for row in vector] == ["e1", "e2", "e3"]


def test_split_rejects_unknown_channel() -> None:
    with pytest.raises(ValueError, match="channel"):
        split_hybrid_rows([_row("other", "e1")])


def test_hybrid_search_executes_one_candidate_statement_and_uses_requested_ref() -> None:
    index, calls = _index()
    connection = _FakeConnection([
        _row("vector", "e2", distance=0.2),
        _row("fts", "e1", score=3.0),
        _row("vector", "e1", distance=0.1),
        {**_row("fts", "e9", score=9.0), "memory_artifact_id": "not-requested"},
    ])

    channels = asyncio.run(index.search(cast(AsyncConnection, connection), "scope-a", _request()))

    assert len(connection.executed) == 1
    assert calls == [1]
    statement, parameters = connection.executed[0]
    assert "UNION ALL" in statement
    assert parameters["scope_id"] == "scope-a"
    assert parameters["memory_artifact_ids"] == ("memory-a",)
    assert parameters["fts_query"] == "alpha beta"
    assert parameters["candidate_limit"] == 32
    assert [hit.entry_id for hit in channels.fts] == ["e1"]
    assert [hit.entry_id for hit in channels.vector] == ["e1", "e2"]
    # Rows from an older incremental projection keep their head_revision; the
    # returned reference is the validated request head.
    assert all(hit.memory_ref == HEAD for hit in (*channels.fts, *channels.vector))
    assert channels.fts[0].distance is None
    assert channels.vector[0].distance == pytest.approx(0.1)


def test_tagged_hybrid_search_binds_tag_parameters() -> None:
    index, _ = _index()
    connection = _FakeConnection([_row("fts", "e1", score=1.0)])
    asyncio.run(
        index.search(cast(AsyncConnection, connection), "scope-a", _request(tag_filter=TagFilter(tags=("selected",))))
    )
    statement, parameters = connection.executed[0]
    assert "pc_artifact_tags" in statement
    assert parameters["tag_keys"] == ("selected",)
    assert parameters["tag_minimum"] == 1


def test_hybrid_search_rejects_incomplete_vectors_and_profile_mismatch() -> None:
    index, _ = _index(complete=False)
    connection = _FakeConnection([])
    with pytest.raises(CapabilityNotSupportedError) as incomplete:
        asyncio.run(index.search(cast(AsyncConnection, connection), "scope-a", _request()))
    assert incomplete.value.capability == "vector"

    other = PROFILE.model_copy(update={"profile_id": "other"})
    with pytest.raises(CapabilityNotSupportedError) as mismatch:
        asyncio.run(index.search(cast(AsyncConnection, connection), "scope-a", _request(profile=other)))
    assert mismatch.value.capability == "embedding-profile"
    assert connection.executed == []


@pytest.mark.parametrize("mode", ["fts", "vector"])
def test_single_channel_modes_delegate_to_frozen_indexes(mode: str) -> None:
    index, _ = _index()
    connection = _FakeConnection([])
    asyncio.run(index.search(cast(AsyncConnection, connection), "scope-a", _request(mode)))
    assert len(connection.executed) == 1
    statement = connection.executed[0][0]
    assert "UNION ALL" not in statement
    assert ("AGAINST" in statement.upper()) is (mode == "fts")
    assert ("l2_distance" in statement.lower()) is (mode == "vector")


def test_split_channels_fuse_like_the_two_query_path() -> None:
    fts_rows = [_row("fts", "e1", score=2.0), _row("fts", "e3", score=1.0)]
    vector_rows = [_row("vector", "e2", distance=0.1), _row("vector", "e1", distance=0.3)]
    frozen = MemorySearchChannels(
        fts=memory_channel_hits(fts_rows, (HEAD,)), vector=memory_channel_hits(vector_rows, (HEAD,))
    )
    fts, vector = split_hybrid_rows([vector_rows[1], fts_rows[1], vector_rows[0], fts_rows[0]])
    split = MemorySearchChannels(fts=memory_channel_hits(fts, (HEAD,)), vector=memory_channel_hits(vector, (HEAD,)))
    assert fuse_rankings(fts=split.fts, vector=split.vector, limit=10) == fuse_rankings(
        fts=frozen.fts, vector=frozen.vector, limit=10
    )


class _Embedding:
    profile = PROFILE


def test_composition_selects_single_statement_hybrid_only_for_seekdb(tmp_path: Path) -> None:
    embedding = cast(Any, _Embedding())
    seekdb = composition._relational_memory_index(SeekDBConfig(path=tmp_path / "db"), embedding)
    assert isinstance(seekdb, SeekDBHybridMemoryIndex)
    assert seekdb.capabilities.hybrid
    oceanbase = composition._relational_memory_index(OceanBaseConfig.model_construct(), embedding)
    assert type(oceanbase) is CompositeMemoryIndex
    fts_only = composition._relational_memory_index(SeekDBConfig(path=tmp_path / "db"), None)
    assert type(fts_only) is CompositeMemoryIndex
    assert not fts_only.capabilities.vector
