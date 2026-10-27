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

"""Behavior tests for the HTTP Memory reranker adapter."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"candidate {rank}",
        score=1.0 / rank,
        matched_by=("fts",),
    )


def _run(handler, candidates, limit, *, batch_size=32):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid", client=client, batch_size=batch_size)
            return await reranker.rerank("q", candidates, limit)

    return asyncio.run(scenario())


def _scoring(scores: dict[int, float], calls: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        items = [{"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(body["candidates"])]
        return httpx.Response(200, json={"scores": items})

    return handler


def test_cross_batch_scores_are_selected_globally_with_rank_ties() -> None:
    calls: list[dict] = []
    scores = {1: 0.10, 2: 0.90, 3: 0.95, 4: 0.90, 5: 0.95}
    decision = _run(_scoring(scores, calls), tuple(_hit(i) for i in range(1, 6)), 4, batch_size=2)
    assert decision.selected_ranks == (3, 5, 2, 4)
    assert decision.usage.requests == len(calls) == 3
    sent = [item for call in calls for item in call["candidates"]]
    assert sorted(item["rank"] for item in sent) == [1, 2, 3, 4, 5]
    assert all(item["text"] == _hit(item["rank"]).text for item in sent)
    assert max(len(call["candidates"]) for call in calls) <= 2


@pytest.mark.parametrize(("count", "batch_size"), [(1, 1), (1, 32), (5, 1), (6, 3), (7, 3), (40, 32)])
def test_usage_counts_requests_actually_sent(count: int, batch_size: int) -> None:
    calls: list[dict] = []
    scores = {rank: float(rank % 4) for rank in range(1, count + 1)}
    decision = _run(_scoring(scores, calls), tuple(_hit(i) for i in range(1, count + 1)), 1, batch_size=batch_size)
    assert decision.usage.requests == len(calls)
    assert all(len(call["candidates"]) <= batch_size for call in calls)


def test_http_status_errors_are_preserved() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _run(lambda _request: httpx.Response(503, json={"error": "down"}), (_hit(1),), 1)


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"scores": {"rank": 1, "score": 0.5}}',
        b'{"scores": [{"rank": 1, "score": 0.5}], "scores": [{"rank": 1, "score": 0.5}]}',
        b'{"scores": [{"rank": true, "score": 0.5}]}',
        b'{"scores": [{"rank": 1.0, "score": 0.5}]}',
        b'{"scores": [{"rank": 1, "score": "0.5"}]}',
        b'{"scores": [{"rank": 1, "score": true}]}',
        b'{"scores": [{"rank": 1, "score": -Infinity}]}',
        b'{"scores": [{"rank": 1}]}',
        b'{"scores": []}',
    ],
)
def test_rejects_malformed_responses(content: bytes) -> None:
    with pytest.raises(ValueError):
        _run(lambda _request: httpx.Response(200, content=content), (_hit(1),), 1)


def test_rejects_ranks_from_another_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ranks = [item["rank"] for item in body["candidates"]]
        return httpx.Response(200, json={"scores": [{"rank": rank + 1, "score": 1.0} for rank in ranks]})

    with pytest.raises(ValueError):
        _run(handler, (_hit(1), _hit(2)), 1, batch_size=1)


def test_rejects_invalid_configuration_and_limits() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    for batch_size in (0, -1, True):
        with pytest.raises(ValueError):
            HTTPMemoryReranker("https://invalid", client=client, batch_size=batch_size)
    with pytest.raises(ValueError):
        HTTPMemoryReranker("", client=client)
    calls: list[dict] = []
    with pytest.raises(ValueError):
        _run(_scoring({1: 1.0}, calls), (_hit(1),), 0)
    assert calls == []
    asyncio.run(client.aclose())
