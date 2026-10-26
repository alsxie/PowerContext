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

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.evidence_selection import split_segments
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

MEMORY_REF = ArtifactRef(family="memory", artifact_id="memory", revision=2)
FILLER = "General administrative notes. " * 60


def _hit(entry_id: str, text: str) -> MemoryHit:
    return MemoryHit(
        memory_ref=MEMORY_REF,
        entry_id=entry_id,
        entry_version_id=f"{entry_id}-v1",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def _build(query: str, *hits: MemoryHit, max_bytes: int = 1400, scope_id: str = "scope-a") -> PreparedContextBuild:
    return PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=True),
        current_scope_id=scope_id,
        memory_candidates=(PreparedMemoryCandidates(scope_id=scope_id, memory_ref=MEMORY_REF, hits=hits),),
    )


def _items(build: PreparedContextBuild) -> list[dict[str, object]]:
    assert build.context.content is not None
    return json.loads(build.context.content.splitlines()[-2])["items"]


def _assert_consistent(build: PreparedContextBuild, hits: tuple[MemoryHit, ...], max_bytes: int) -> None:
    content = build.context.content or ""
    assert build.context.content_bytes == len(content.encode("utf-8")) <= max_bytes
    texts = {hit.entry_id: hit.text for hit in hits}
    items = _items(build) if content else []
    assert len(items) == len(build.fragments) == len(build.origins)
    for item, fragment, origin in zip(items, build.fragments, build.origins, strict=True):
        assert origin == fragment.address
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(texts[fragment.address.entry_id])
        assert fragment.text == texts[fragment.address.entry_id][fragment.start : fragment.end]
        assert item == {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": MEMORY_REF.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }


def test_query_selects_late_literal_evidence_per_query() -> None:
    hits = (
        _hit("alpha", FILLER + "The cobalt sensor is calibrated at 37 degrees."),
        _hit("beta", FILLER + "The amber valve opens at 19 degrees."),
    )
    cobalt = _build("cobalt sensor calibration", *hits)
    amber = _build("when does the amber valve open", *hits)
    _assert_consistent(cobalt, hits, 1400)
    _assert_consistent(amber, hits, 1400)
    assert cobalt.fragments[0].address.entry_id == "alpha" and "37 degrees" in cobalt.fragments[0].text
    assert amber.fragments[0].address.entry_id == "beta" and "19 degrees" in amber.fragments[0].text


def test_one_entry_can_contribute_several_fragments() -> None:
    text = "Kiwi harvest rose sharply. " + FILLER + "Kiwi harvest volumes doubled in spring."
    hits = (_hit("only", text),)
    build = _build("kiwi harvest", *hits, max_bytes=1800)
    _assert_consistent(build, hits, 1800)
    assert len(build.fragments) == 2
    assert any("rose sharply" in fragment.text for fragment in build.fragments)
    assert any("doubled in spring" in fragment.text for fragment in build.fragments)


@pytest.mark.parametrize("max_bytes", [512, 700, 1000, 1401, 2400])
def test_unicode_and_line_separators_are_budgeted_on_the_full_output(max_bytes: int) -> None:
    hits = (
        _hit("one", "\u6d77\u6d0b\u89c2\u6d4b station records \u2028temperatures\u2029 daily\x85. " * 40),
        _hit("two", 'Quoted "station" values \\ escaped. ' * 30),
    )
    build = _build("\u6d77\u6d0b\u89c2\u6d4b station", *hits, max_bytes=max_bytes)
    _assert_consistent(build, hits, max_bytes)
    if max_bytes >= 1000:
        assert build.fragments


def test_budget_without_room_for_one_item_returns_an_empty_context() -> None:
    hits = (_hit("x" * 600, "Station records temperature."),)
    build = _build("station", *hits, max_bytes=512)
    assert build.context.status == "empty"
    assert build.fragments == () and build.origins == ()


def test_scope_identity_is_kept_per_group() -> None:
    hit = _hit("shared", "Glacier melt accelerated after 2010.")
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="glacier melt", max_bytes=2000, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(
            PreparedMemoryCandidates(scope_id="scope-b", memory_ref=MEMORY_REF, hits=(hit,)),
            PreparedMemoryCandidates(scope_id="scope-a", memory_ref=MEMORY_REF, hits=(hit,)),
        ),
    )
    scopes = [fragment.address.memory.scope_id for fragment in build.fragments]
    assert scopes and set(scopes) <= {"scope-a", "scope-b"}
    for item, origin in zip(_items(build), build.origins, strict=True):
        assert isinstance(origin, MemoryEntryAddress)
        assert item["citation"]["scope_id"] == origin.memory.scope_id


def test_missing_scope_is_rejected() -> None:
    with pytest.raises(PreparedContextInvariantError):
        _build("glacier", _hit("a", "Glacier melt."), scope_id="")


def test_default_prepare_is_unchanged() -> None:
    hits = (_hit("a", "A station records temperature."),)
    request = PrepareContextRequest(query="temperature", max_bytes=1200)
    groups = (PreparedMemoryCandidates(scope_id="scope-a", memory_ref=MEMORY_REF, hits=hits),)
    default = PreparedContextBuilder().build_scopes_result(
        request=request, current_scope_id="scope-a", memory_candidates=groups
    )
    explicit = PreparedContextBuilder().build_scopes_result(
        request=request.model_copy(update={"evidence_selection": False}),
        current_scope_id="scope-a",
        memory_candidates=groups,
    )
    assert default == explicit
    assert default.fragments == ()
    assert _items(default)[0]["content"] == "A station records temperature."


@pytest.mark.parametrize("value", [1, "true", None])
def test_evidence_selection_requires_a_json_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "q", "evidence_selection": value})


def test_segments_keep_decimals_and_split_cjk() -> None:
    text = "Width was 1.0-1.6 kb (P<0.05). Next sentence here.\u6d4b\u8bd5\u3002\u7ed3\u675f"
    pieces = [text[start:end] for start, end in split_segments(text)]
    assert pieces == ["Width was 1.0-1.6 kb (P<0.05).", "Next sentence here.\u6d4b\u8bd5\u3002", "\u7ed3\u675f"]
