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

"""Cross-call embedding request coalescing with a bounded vector cache.

``CoalescingEmbeddingModel`` wraps one embedding model shared by concurrent
callers on a single event loop. Identical texts share one provider request
whether they repeat within a call, arrive inside the coalescing window, or
arrive while a provider request is still running. Successful vectors are kept
in an LRU cache; failures are never cached.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Literal, TypeAlias

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile

_Namespace: TypeAlias = Literal["document", "query"]
_Key: TypeAlias = tuple[_Namespace, str]
_BatchOutcome: TypeAlias = tuple[dict[str, EmbeddingVector], InferenceUsage]


class _Batch:
    """One provider request for a bounded set of distinct texts."""

    __slots__ = ("future", "namespace", "texts")

    def __init__(self, namespace: _Namespace, loop: asyncio.AbstractEventLoop) -> None:
        self.namespace: _Namespace = namespace
        self.texts: list[str] = []
        self.future: asyncio.Future[_BatchOutcome] = loop.create_future()
        # Mark exceptions as retrieved so batches nobody awaits any more do not warn.
        self.future.add_done_callback(_consume_exception)


def _consume_exception(future: asyncio.Future[_BatchOutcome]) -> None:
    if not future.cancelled():
        future.exception()


class CoalescingEmbeddingModel:
    """Share provider embedding work across concurrent calls and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, int | float)
            or not coalesce_ms >= 0
            or coalesce_ms == float("inf")
        ):
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[_Key, EmbeddingVector] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[_Key, _Batch] = {}
        self._open: dict[_Namespace, _Batch] = {}
        self._timers: dict[_Namespace, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def model(self) -> EmbeddingModel:
        """Return the wrapped provider model."""

        return self._model

    @property
    def profile(self) -> EmbeddingProfile:
        """Return the wrapped provider's current embedding profile."""

        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text in input order, sharing provider work."""

        return await self._embed("document", tuple(texts))

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed retrieval queries, sharing document vectors for symmetric providers."""

        namespace: _Namespace = "query" if isinstance(self._model, QueryEmbeddingModel) else "document"
        return await self._embed(namespace, tuple(texts))

    async def _embed(self, namespace: _Namespace, texts: tuple[str, ...]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        self._bind_loop()
        cached: dict[str, EmbeddingVector] = {}
        waits: dict[int, _Batch] = {}
        for text in dict.fromkeys(texts):
            key: _Key = (namespace, text)
            vector = self._cache.get(key)
            if vector is not None:
                self._cache.move_to_end(key)
                cached[text] = vector
                continue
            batch = self._inflight.get(key)
            if batch is None:
                batch = self._enqueue(namespace, text)
            waits.setdefault(id(batch), batch)
        if self._coalesce_seconds == 0:
            self._dispatch_open(namespace)

        resolved = dict(cached)
        usages: list[InferenceUsage] = []
        for batch in waits.values():
            # Shield so cancelling this caller never cancels work other callers share.
            vectors, usage = await asyncio.shield(batch.future)
            resolved.update(vectors)
            usages.append(usage)
        return EmbeddingResult(vectors=tuple(resolved[text] for text in texts), usage=_sum_usage(usages))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # Pending work belongs to the previous loop; only the cache carries over.
            self._loop = loop
            self._inflight.clear()
            self._open.clear()
            self._timers.clear()
            self._tasks.clear()

    def _enqueue(self, namespace: _Namespace, text: str) -> _Batch:
        loop = self._loop
        assert loop is not None  # noqa: S101
        batch = self._open.get(namespace)
        if batch is None:
            batch = _Batch(namespace, loop)
            self._open[namespace] = batch
            if self._coalesce_seconds > 0:
                self._timers[namespace] = loop.call_later(self._coalesce_seconds, self._dispatch_open, namespace)
        batch.texts.append(text)
        self._inflight[(namespace, text)] = batch
        if len(batch.texts) >= self._max_batch_size:
            self._dispatch_open(namespace)
        return batch

    def _dispatch_open(self, namespace: _Namespace) -> None:
        timer = self._timers.pop(namespace, None)
        if timer is not None:
            timer.cancel()
        batch = self._open.pop(namespace, None)
        if batch is None:
            return
        task = asyncio.ensure_future(self._run(batch))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # Waiters receive any failure through the batch future; retrieve it here so a
        # re-raised non-``Exception`` failure does not also log "never retrieved".
        if not task.cancelled():
            task.exception()

    async def _run(self, batch: _Batch) -> None:
        texts = tuple(batch.texts)
        try:
            vectors, usage = await self._compute(batch.namespace, texts)
        except asyncio.CancelledError:
            self._release(batch)
            batch.future.cancel()
            raise
        except BaseException as error:
            self._release(batch)
            if not batch.future.done():
                batch.future.set_exception(error)
            if isinstance(error, Exception):
                return
            # GeneratorExit, KeyboardInterrupt, SystemExit, ...: waiters are released, then re-raise.
            raise
        mapping = dict(zip(texts, vectors, strict=True))
        if self._cache_size > 0:
            for text, vector in mapping.items():
                self._store((batch.namespace, text), vector)
        self._release(batch)
        if not batch.future.done():
            batch.future.set_result((mapping, usage))

    async def _compute(
        self, namespace: _Namespace, texts: tuple[str, ...]
    ) -> tuple[tuple[EmbeddingVector, ...], InferenceUsage]:
        if namespace == "query" and isinstance(self._model, QueryEmbeddingModel):
            result = await self._model.embed_query(texts)
        else:
            result = await self._model.embed(texts)
        vectors = tuple(result.vectors)
        if len(vectors) != len(texts):
            raise ValueError(  # noqa: TRY003
                f"embedding model returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors, result.usage

    def _release(self, batch: _Batch) -> None:
        for text in batch.texts:
            key: _Key = (batch.namespace, text)
            if self._inflight.get(key) is batch:
                del self._inflight[key]

    def _store(self, key: _Key, vector: EmbeddingVector) -> None:
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _sum_usage(usages: list[InferenceUsage]) -> InferenceUsage:
    """Sum provider usage once per distinct batch, keeping unknown token counts as ``None``."""

    def total(values: list[int | None]) -> int | None:
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    return InferenceUsage(
        requests=sum(usage.requests for usage in usages),
        input_tokens=total([usage.input_tokens for usage in usages]),
        output_tokens=total([usage.output_tokens for usage in usages]),
    )


__all__ = ["CoalescingEmbeddingModel"]
