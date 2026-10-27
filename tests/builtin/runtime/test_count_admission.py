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

"""Count admission of Source-driven processing from durable journal heads and cursors."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, update

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.supervision import ArtifactProcessingLeaseRepository
from powercontext.builtin.persistence.tables import SHARED_TABLES, SOURCE_JOURNAL_HEADS_TABLE
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding, ArtifactProcessingSupervisor
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.builtin.runtime.processing_contracts import ArtifactProcessingWorkerCompletion
from powercontext.builtin.runtime.processing_discovery import pending_source_counts
from powercontext.builtin.sources.journal import SourceCursor

BINDING = "memory-binding"


def _profile(tmp_path, name):
    return SQLiteProfile.open(
        SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}"), tables=SHARED_TABLES
    )


async def _append(database, scope, count=1):
    """Simulate new journal identities: advance the head and mark the binding dirty."""

    heads = SOURCE_JOURNAL_HEADS_TABLE
    async with database.transaction() as connection:
        current = await connection.scalar(
            heads.select().with_only_columns(heads.c.position).where(heads.c.scope_id == scope)
        )
        if current is None:
            await connection.execute(insert(heads).values(scope_id=scope, position=count))
        else:
            await connection.execute(update(heads).where(heads.c.scope_id == scope).values(position=current + count))
        for _ in range(count):
            await ArtifactProcessingIntentRepository().mark_dirty(connection, scope, BINDING)


class _WindowLauncher:
    """Consume at most ``window`` journal positions per accepted invocation."""

    def __init__(self, database, window):
        self.database = database
        self.window = window
        self.assignments = []

    async def start(self, assignment):
        self.assignments.append(assignment)
        return self

    async def wait(self):
        work = self.assignments[-1]
        cursors = SourceCursorRepository()
        intents = ArtifactProcessingIntentRepository()
        async with self.database.transaction() as connection:
            await ArtifactProcessingLeaseRepository().require_fence(connection, work.fence)
            stored = await cursors.load(connection, work.scope_id, BINDING)
            after = 0 if stored is None else stored.cursor.sequence
            head = await connection.scalar(
                SOURCE_JOURNAL_HEADS_TABLE
                .select()
                .with_only_columns(SOURCE_JOURNAL_HEADS_TABLE.c.position)
                .where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == work.scope_id)
            )
            through = min(int(head), after + self.window)
            await cursors.save(
                connection,
                work.scope_id,
                BINDING,
                SourceCursor(sequence=through),
                expected_generation=None if stored is None else stored.generation,
            )
            row = await intents.load(connection, work.scope_id, BINDING, for_update=True)
            assert row is not None
            await intents.acknowledge(
                connection,
                work.scope_id,
                BINDING,
                work.claimed_request_generation,
                clean_generation=row.dirty_generation if through >= int(head) else None,
            )
        return ArtifactProcessingWorkerCompletion()

    async def terminate(self):
        return None


async def _settle(supervisor, seconds=0.2):
    await asyncio.sleep(seconds)
    async with asyncio.timeout(5):
        while supervisor.family_status["memory"]["used_workers"] or supervisor.family_status["memory"]["ready"]:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def test_runtime_thresholds_are_strict_optional_positive_integers():
    for field in ("memory_source_count_threshold", "topic_memory_source_count_threshold"):
        assert getattr(RuntimeConfig(), field) is None
        assert getattr(RuntimeConfig(**{field: 2}), field) == 2  # ty: ignore[invalid-argument-type]
        for value in (True, 0, -1, 1.5, "3"):
            with pytest.raises(ValidationError):
                RuntimeConfig(**{field: value})  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", [True, 0, -2, 1.0, "3"])
def test_binding_rejects_invalid_threshold(value):
    with pytest.raises(ValueError, match="source_count_threshold"):
        ArtifactProcessingBinding(BINDING, "memory", _WindowLauncher(None, 1), source_count_threshold=value)


def test_pending_count_starts_from_zero_and_follows_cursor(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "pending") as profile:
            database = profile.database
            await _append(database, "a", 4)
            async with database.transaction() as connection:
                assert await pending_source_counts(connection, ("a", "b"), BINDING) == {"a": 4, "b": 0}
                await SourceCursorRepository().save(
                    connection, "a", BINDING, SourceCursor(sequence=3), expected_generation=None
                )
                assert await pending_source_counts(connection, ("a",), BINDING) == {"a": 1}
                assert await pending_source_counts(connection, ("a",), "other-binding") == {"a": 4}

    asyncio.run(scenario())


def test_threshold_admits_one_window_and_rechecks_remaining_count(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "window") as profile:
            database = profile.database
            launcher = _WindowLauncher(database, window=1)
            binding = ArtifactProcessingBinding(BINDING, "memory", launcher, source_count_threshold=3)
            async with ArtifactProcessingSupervisor(
                database=database, bindings=(binding,), lease_mode="single-process"
            ) as supervisor:
                await _append(database, "scope", 2)
                supervisor.notify_sources()
                await _settle(supervisor)
                assert launcher.assignments == []
                await _append(database, "scope")
                supervisor.notify_sources()
                await _settle(supervisor)
                assert len(launcher.assignments) == 1
                # Two pending positions remain below the threshold after the window.
                await _settle(supervisor, 1.3)
                assert len(launcher.assignments) == 1
                await _append(database, "scope")
                supervisor.notify_sources()
                await _settle(supervisor)
                assert len(launcher.assignments) == 2
                async with database.transaction() as connection:
                    assert await pending_source_counts(connection, ("scope",), BINDING) == {"scope": 2}

    asyncio.run(scenario())


def test_unnotified_journal_writes_are_found_by_polling(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "poll") as profile:
            database = profile.database
            launcher = _WindowLauncher(database, window=10)
            binding = ArtifactProcessingBinding(BINDING, "memory", launcher, source_count_threshold=2)
            async with ArtifactProcessingSupervisor(
                database=database, bindings=(binding,), lease_mode="single-process"
            ) as supervisor:
                await _append(database, "scope", 2)
                async with asyncio.timeout(5):
                    while not launcher.assignments:  # noqa: ASYNC110
                        await asyncio.sleep(0.02)
                await _settle(supervisor)
                async with database.transaction() as connection:
                    assert await pending_source_counts(connection, ("scope",), BINDING) == {"scope": 0}

    asyncio.run(scenario())


def test_server_environment_supplies_count_thresholds(monkeypatch, tmp_path):
    from powercontext.server.settings import ServerSettings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_MEMORY_SOURCE_COUNT_THRESHOLD", "3")
    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_TOPIC_MEMORY_SOURCE_COUNT_THRESHOLD", "5")
    runtime = ServerSettings().runtime
    assert (runtime.memory_source_count_threshold, runtime.topic_memory_source_count_threshold) == (3, 5)
    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_MEMORY_SOURCE_COUNT_THRESHOLD", "0")
    with pytest.raises(ValidationError):
        ServerSettings()
