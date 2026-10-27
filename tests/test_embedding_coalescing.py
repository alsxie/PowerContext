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

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class Gated:
    profile = EmbeddingProfile(profile_id="coalescing-tests", model="local", dimension=1)

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.release = asyncio.Event()
        self.fail_on = fail_on

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        self.calls.append(texts)
        await self.release.wait()
        if self.fail_on & set(texts):
            raise RuntimeError("boom")
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=1, input_tokens=len(texts)),
        )


def test_late_waiter_shares_inflight_request_with_cache_disabled() -> None:
    async def scenario() -> None:
        base = Gated()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("a", "bb")))
        await asyncio.sleep(0)
        late = asyncio.create_task(model.embed(("bb", "bb", "a")))
        await asyncio.sleep(0)
        base.release.set()
        assert (await first).vectors == ((1.0,), (2.0,))
        late_result = await late
        assert late_result.vectors == ((2.0,), (2.0,), (1.0,))
        assert late_result.usage == InferenceUsage(requests=1, input_tokens=2)
        assert base.calls == [("a", "bb")]
        await model.embed(("a",))
        assert base.calls == [("a", "bb"), ("a",)]

    asyncio.run(scenario())


def test_failed_batch_does_not_affect_independent_batch() -> None:
    async def scenario() -> None:
        base = Gated(fail_on=frozenset({"bad"}))
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        mixed = asyncio.create_task(model.embed(("ok", "bad")))
        good = asyncio.create_task(model.embed(("ok",)))
        await asyncio.sleep(0)
        base.release.set()
        with pytest.raises(RuntimeError):
            await mixed
        assert (await good).vectors == ((2.0,),)
        base.fail_on = frozenset()
        assert (await model.embed(("bad", "ok"))).usage.requests == 1
        assert base.calls == [("ok",), ("bad",), ("bad",)]

    asyncio.run(scenario())


def test_cancel_during_window_keeps_other_waiter() -> None:
    async def scenario() -> None:
        base = Gated()
        base.release.set()
        model = CoalescingEmbeddingModel(base, coalesce_ms=20)
        cancelled = asyncio.create_task(model.embed(("x",)))
        survivor = asyncio.create_task(model.embed(("x", "y")))
        await asyncio.sleep(0)
        cancelled.cancel()
        result = await survivor
        assert result.vectors == ((1.0,), (1.0,))
        assert result.usage.requests == 1
        assert base.calls == [("x", "y")]

    asyncio.run(scenario())


class _ProviderAbort(BaseException):
    """A non-``Exception`` failure raised by a provider."""


@pytest.mark.parametrize("error_type", [GeneratorExit, _ProviderAbort])
def test_base_exception_releases_waiters_without_caching(error_type: type[BaseException]) -> None:
    class Aborting(Gated):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            if "bad" in texts:
                self.calls.append(texts)
                raise error_type
            return await super().embed(texts)

    async def scenario() -> None:
        base = Aborting()
        base.release.set()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        first = asyncio.create_task(model.embed(("ok", "bad")))
        shared = asyncio.create_task(model.embed(("bad",)))
        good = asyncio.create_task(model.embed(("ok",)))
        _, pending = await asyncio.wait({first, shared, good}, timeout=1.0)
        assert not pending
        for task in (first, shared):
            assert isinstance(task.exception(), error_type)
        assert good.result().vectors == ((2.0,),)
        assert base.calls == [("ok",), ("bad",)]
        assert model._inflight == {}
        assert not model._tasks
        retry = asyncio.create_task(model.embed(("bad",)))
        _, pending = await asyncio.wait({retry}, timeout=1.0)
        assert not pending
        assert isinstance(retry.exception(), error_type)
        assert base.calls == [("ok",), ("bad",), ("bad",)]

    asyncio.run(scenario())


def test_profile_tracks_wrapped_model() -> None:
    base = Gated()
    model = CoalescingEmbeddingModel(base)
    assert model.profile == Gated.profile
    replacement = EmbeddingProfile(profile_id="coalescing-tests-v2", model="local", dimension=1)
    base.profile = replacement
    assert model.profile == replacement


def test_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(Gated(), max_batch_size=0)
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(Gated(), coalesce_ms=-1)
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(Gated(), cache_size=-1)
