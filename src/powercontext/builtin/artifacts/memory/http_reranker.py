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

"""HTTP scoring-service adapter for Memory reranking.

The service receives batches of ``{"rank", "text"}`` candidates that keep their one-based
rank in the complete coarse pool and must answer with exactly one finite score per rank.
Scores from every batch take part in one global selection: descending score, ties broken
by ascending original rank.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"
_RESPONSE_KEYS = frozenset({"scores"})
_SCORE_KEYS = frozenset({"rank", "score"})


class HTTPMemoryReranker:
    """Score Memory candidates with an HTTP service and select them globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("reranker endpoint must be a non-empty string")  # noqa: TRY003
        if not _is_int(batch_size) or batch_size < 1:
            raise ValueError("reranker batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("reranker policy_id must be a non-empty string")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and return the globally best original ranks."""

        _validate_bounds(len(candidates), limit)
        ranked = tuple(enumerate(candidates, start=1))
        scores: dict[int, float | int] = {}
        for start in range(0, len(ranked), self._batch_size):
            batch = ranked[start : start + self._batch_size]
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": hit.text} for rank, hit in batch],
                },
            )
            response.raise_for_status()
            scores.update(_parse_scores(response.content, expected=frozenset(rank for rank, _ in batch)))
        requests = math.ceil(len(ranked) / self._batch_size)
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(selected_ranks=tuple(selected), usage=InferenceUsage(requests=requests))


def _parse_scores(content: bytes, *, expected: frozenset[int]) -> dict[int, float | int]:
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("reranker response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or frozenset(payload) != _RESPONSE_KEYS:
        raise ValueError("reranker response must contain exactly 'scores'")  # noqa: TRY003
    records = payload["scores"]
    if not isinstance(records, list):
        raise ValueError("reranker 'scores' must be a list")  # noqa: TRY003, TRY004
    scores: dict[int, float | int] = {}
    for record in records:
        rank, score = _parse_record(record, expected=expected)
        if rank in scores:
            raise ValueError(f"reranker returned rank {rank} more than once")  # noqa: TRY003
        scores[rank] = score
    if len(scores) != len(expected):
        missing = sorted(expected.difference(scores))
        raise ValueError(f"reranker response is missing ranks {missing}")  # noqa: TRY003
    return scores


def _parse_record(record: object, *, expected: frozenset[int]) -> tuple[int, float | int]:
    if not isinstance(record, dict) or frozenset(record) != _SCORE_KEYS:
        raise ValueError("reranker score records must contain exactly 'rank' and 'score'")  # noqa: TRY003
    rank, score = record["rank"], record["score"]
    if not _is_int(rank):
        raise ValueError("reranker score rank must be an integer")  # noqa: TRY003
    if rank not in expected:
        raise ValueError(f"reranker returned rank {rank} outside the requested batch")  # noqa: TRY003
    if not _is_finite_number(score):
        raise ValueError(f"reranker score for rank {rank} must be a finite number")  # noqa: TRY003
    return rank, score


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"reranker response repeats key {key!r}")  # noqa: TRY003
        result[key] = value
    return result


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if not _is_int(limit) or limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
