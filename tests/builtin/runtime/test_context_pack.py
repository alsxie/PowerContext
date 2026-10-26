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

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    aligned_context_pack,
    apply_context_pack_delta,
)


def _context(revision: int, extra: str = "") -> str:
    items = ",".join(
        '{"citation":{"memory_ref":{"family":"memory","artifact_id":"memory","revision":'
        f'{revision}}},"entry_id":"e{index}"}},"content":"{"word " * 40}{index}"}}'
        for index in range(6)
    )
    return f'Header line.\n\nBEGIN\n{{"items":[{items}]}}\n{extra}END'


@pytest.mark.parametrize("limit", [1, 3, 7, 64, 512])
def test_chunks_are_bounded_valid_and_lossless(limit: int) -> None:
    text = "alpha \u6d77\u6d0b beta\n" * 20 + _context(1)
    pack = ContextPack.from_content(text, chunk_bytes=limit) if limit >= 3 else None
    if pack is None:
        with pytest.raises(ContextPackError):
            ContextPack.from_content(text, chunk_bytes=limit)
        return
    assert b"".join(pack.items).decode("utf-8") == text
    assert all(0 < len(item) <= limit for item in pack.items)
    for item in pack.items:
        item.decode("utf-8")
    assert ContextPack.from_content(text, chunk_bytes=limit) == pack


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_chunk_limit_must_be_positive_int(limit: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=limit)  # type: ignore[arg-type]


def test_empty_content_is_empty_pack() -> None:
    assert ContextPack.from_content("") == ContextPack.build(())


def test_revision_change_keeps_leading_chunk_reusable() -> None:
    base = ContextPack.from_content(_context(1))
    target_text = _context(2)
    target = aligned_context_pack(base, target_text)
    delta = ContextPackDelta.between(base, target)
    assert delta.prefix_items >= 1
    assert sum(len(item) for item in base.items[: delta.prefix_items]) > len("Header line.\n\nBEGIN\n")
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )
    assert b"".join(rebuilt.items).decode() == target_text


def test_tail_change_reuses_all_unchanged_items() -> None:
    base = ContextPack.from_content(_context(1))
    target = aligned_context_pack(base, _context(1, extra="note\n"))
    delta = ContextPackDelta.between(base, target)
    assert delta.prefix_items >= len(base.items) - 1
    assert len(delta.to_bytes()) < base.byte_count // 3


def test_alignment_reuses_foreign_chunking() -> None:
    text = _context(3)
    base = ContextPack.build((text[:10].encode(), text[10:400].encode(), text[400:].encode()))
    target = aligned_context_pack(base, text + "!")
    assert target.items[:2] == base.items[:2]
    assert b"".join(target.items).decode() == text + "!"


def test_unchanged_content_delta_has_no_appended_chunks() -> None:
    base = ContextPack.from_content(_context(1))
    delta = ContextPackDelta.between(base, aligned_context_pack(base, _context(1)))
    assert delta.appended == ()
    assert delta.prefix_items == len(base.items)


def test_tampered_base_is_rejected() -> None:
    base = ContextPack.build((b"a",))
    forged = ContextPack(items=(b"b",), digest=base.digest, byte_count=base.byte_count)
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(forged, ContextPackDelta.between(base, base))


def test_non_canonical_base64_is_rejected() -> None:
    payload = ContextPack.build((b"a",)).to_bytes().replace(b'"YQ=="', b'"YR=="')
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(payload)
