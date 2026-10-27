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
        ContextPack.from_content("text", chunk_bytes=limit)  # ty: ignore[invalid-argument-type]


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


def _rebuild(base: ContextPack, target: ContextPack) -> tuple[ContextPack, ContextPackDelta]:
    wire = ContextPackDelta.between(base, target).to_bytes()
    delta = ContextPackDelta.from_bytes(wire)
    return apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), delta), delta


def test_alignment_prefers_canonical_chunking_when_it_costs_nothing() -> None:
    # Reusing the base chunk ':' would split a marker the canonical chunking keeps whole,
    # yet both choices share the same base bytes and cost the same on the wire.
    base = ContextPack.build((b'{"citation"', b':"revision"', b":", "\u00e9b11ba".encode()))
    content = '{"citation":"revision"revision":a\n'
    canonical = ContextPack.from_content(content, chunk_bytes=11)
    target = aligned_context_pack(base, content, chunk_bytes=11)
    assert target == canonical
    rebuilt, delta = _rebuild(base, target)
    assert rebuilt.digest == delta.target_digest == canonical.digest
    assert rebuilt.text() == content


def test_alignment_keeps_reuse_when_canonical_chunking_costs_more() -> None:
    base = ContextPack.from_content("x" * 10 + '{"citation":1}')
    content = "x" * 10 + "yyyy"
    target = aligned_context_pack(base, content)
    canonical = ContextPack.from_content(content)
    assert target.items == (b"x" * 10, b"yyyy")
    assert len(ContextPackDelta.between(base, target).to_bytes()) < len(
        ContextPackDelta.between(base, canonical).to_bytes()
    )
    rebuilt, delta = _rebuild(base, target)
    assert rebuilt.digest == delta.target_digest == target.digest
    assert rebuilt.text() == content


def test_reconstructed_chain_matches_canonical_packs() -> None:
    base = ContextPack.from_content(_context(1))
    for content in (_context(2), _context(2, extra="note\n"), _context(3), _context(3), ""):
        rebuilt, delta = _rebuild(base, aligned_context_pack(base, content))
        assert rebuilt.digest == delta.target_digest == ContextPack.from_content(content).digest
        base = rebuilt


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"x","schema":"powercontext.context-pack.v1","items":[],"digest":"sha256:'
        + b"0" * 64
        + b'","byte_count":0}',
        b'{"byte_count":1,"byte_count":152,"digest":"DIGEST","items":[],"schema":"powercontext.context-pack.v1"}',
    ],
)
def test_pack_with_duplicate_keys_is_rejected(payload: bytes) -> None:
    payload = payload.replace(b"DIGEST", ContextPack.build(()).digest.encode())
    with pytest.raises(ContextPackError, match="duplicate"):
        ContextPack.from_bytes(payload)


def test_delta_with_duplicate_keys_is_rejected() -> None:
    base = ContextPack.from_content("a")
    wire = ContextPackDelta.between(base, ContextPack.from_content("ab")).to_bytes()
    assert ContextPackDelta.from_bytes(wire).prefix_items == 0
    duplicated = wire.replace(b'"prefix_items":0', b'"prefix_items":1,"prefix_items":0')
    with pytest.raises(ContextPackError, match="duplicate"):
        ContextPackDelta.from_bytes(duplicated)


def test_whitespace_separated_json_is_still_accepted() -> None:
    pack = ContextPack.from_content(_context(1))
    spaced = json.dumps(json.loads(pack.to_bytes()), indent=2).encode()
    assert ContextPack.from_bytes(spaced) == pack
    target = aligned_context_pack(pack, _context(2))
    delta = ContextPackDelta.between(pack, target)
    spaced_delta = json.dumps(json.loads(delta.to_bytes())).encode()
    assert apply_context_pack_delta(pack, ContextPackDelta.from_bytes(spaced_delta)) == target
