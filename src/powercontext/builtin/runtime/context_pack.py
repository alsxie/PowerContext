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

"""Context Pack: chunked, digest-checked prepared context with prefix deltas.

A :class:`ContextPack` carries prepared context text as an ordered tuple of byte chunks.
The wire form is canonical JSON (sorted keys, ``,``/``:`` separators, ASCII escapes) with
standard Base64 chunks. A :class:`ContextPackDelta` lets a receiver rebuild the next pack
from a pack it already holds: it keeps ``prefix_items`` leading chunks of the base and
appends the transmitted suffix chunks. The receiver only needs the serialized bytes; every
decode re-derives the digest and refuses anything that does not match.

Decoders accept any JSON whitespace layout, but ``byte_count`` is checked against the
canonical serialization length, which is what :meth:`ContextPack.to_bytes` produces.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "aligned_context_pack",
    "apply_context_pack_delta",
]

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

# Structural cut points for prepared context text. A chunk always ends right after a
# volatile JSON key whose value tends to change between consecutive requests (for example
# a Memory revision), and always starts at an item boundary, so a change inside one item
# or citation leaves every earlier chunk byte-identical and reusable as a delta prefix.
_HARD_CUT_AFTER = (b'"revision":',)
_HARD_CUT_BEFORE = (b'{"citation":',)
_SOFT_CUT_AFTER = (b"\n",)


class ContextPackError(ValueError):
    """A Context Pack or delta violates the wire protocol or chunking contract."""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def _encode_items(items: Sequence[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object, field: str) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError(f"{field} must be a list")  # noqa: TRY003
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError(f"{field} entries must be Base64 strings")  # noqa: TRY003
        try:
            item = base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError(f"{field} entry is not valid Base64") from error  # noqa: TRY003
        if base64.b64encode(item).decode("ascii") != value:
            raise ContextPackError(f"{field} entry is not canonical Base64")  # noqa: TRY003
        items.append(item)
    return tuple(items)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # ``json.loads`` silently keeps the last duplicate; an ambiguous object is not a valid wire form.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("wire payload contains a duplicate JSON key")  # noqa: TRY003
        result[key] = value
    return result


def _load_object(data: bytes, fields: frozenset[str], schema: str) -> dict[str, Any]:
    if not isinstance(data, bytes | bytearray | memoryview):
        raise ContextPackError("wire payload must be bytes")  # noqa: TRY003
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_unique_object)
    except ContextPackError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ContextPackError("wire payload is not UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError("wire payload must be a JSON object")  # noqa: TRY003
    if set(payload) != fields:
        raise ContextPackError("wire payload fields do not match the schema")  # noqa: TRY003
    if payload["schema"] != schema:
        raise ContextPackError("unsupported wire schema")  # noqa: TRY003
    return payload


def _digest_of(items: Sequence[bytes]) -> str:
    unsigned = _canonical({"schema": PACK_SCHEMA, "items": _encode_items(items)})
    return "sha256:" + hashlib.sha256(unsigned).hexdigest()


def _require_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    if isinstance(items, bytes | bytearray | str):
        raise ContextPackError("items must be an iterable of bytes chunks")  # noqa: TRY003
    try:
        result = tuple(items)
    except TypeError as error:
        raise ContextPackError("items must be an iterable of bytes chunks") from error  # noqa: TRY003
    if not all(type(item) is bytes for item in result):
        raise ContextPackError("every chunk must be bytes")  # noqa: TRY003
    return result


def _require_prefix(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
    return value


def _require_chunk_bytes(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
    return value


@dataclass(frozen=True)
class ContextPack:
    """Complete prepared context as ordered byte chunks plus integrity metadata."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        chunks = _require_items(items)
        digest = _digest_of(chunks)
        encoded = _encode_items(chunks)
        size = 0
        # ``byte_count`` counts itself; iterate to the fixed point (converges in <= 2 steps).
        while True:
            payload = {"schema": PACK_SCHEMA, "items": encoded, "digest": digest, "byte_count": size}
            measured = len(_canonical(payload))
            if measured == size:
                return cls(items=chunks, digest=digest, byte_count=size)
            size = measured

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Deterministically split ``content`` into valid UTF-8 chunks of at most ``chunk_bytes``."""
        if not isinstance(content, str):
            raise ContextPackError("content must be text")  # noqa: TRY003
        limit = _require_chunk_bytes(chunk_bytes)
        return cls.build(_chunk(content.encode("utf-8"), limit))

    def to_bytes(self) -> bytes:
        return _canonical({
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _load_object(data, _PACK_FIELDS, PACK_SCHEMA)
        items = _decode_items(payload["items"], "items")
        digest = payload["digest"]
        byte_count = payload["byte_count"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ContextPackError("digest must be sha256:<hex>")  # noqa: TRY003
        if type(byte_count) is not int:
            raise ContextPackError("byte_count must be an integer")  # noqa: TRY003
        pack = cls.build(items)
        if pack.digest != digest:
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        if pack.byte_count != byte_count:
            raise ContextPackError("pack byte_count does not match its serialization")  # noqa: TRY003
        return pack

    def verify(self) -> None:
        """Raise :class:`ContextPackError` unless the pack fields are self-consistent."""
        expected = ContextPack.build(_require_items(self.items))
        if expected.digest != self.digest or expected.byte_count != self.byte_count:
            raise ContextPackError("pack metadata does not match its items")  # noqa: TRY003

    def text(self) -> str:
        """Decode the reassembled chunks as UTF-8 text."""
        try:
            return b"".join(self.items).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("pack content is not valid UTF-8") from error  # noqa: TRY003


@dataclass(frozen=True)
class ContextPackDelta:
    """Keep ``prefix_items`` chunks of the base pack and append ``appended`` to reach the target."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        # Bounds are checked against a concrete base in ``apply_context_pack_delta``.
        object.__setattr__(self, "appended", _require_items(self.appended))

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        return _canonical({
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _load_object(data, _DELTA_FIELDS, DELTA_SCHEMA)
        for field in ("base_digest", "target_digest"):
            value = payload[field]
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ContextPackError(f"{field} must be sha256:<hex>")  # noqa: TRY003
        prefix = _require_prefix(payload["prefix_items"])
        appended = _decode_items(payload["appended"], "appended")
        return cls(payload["base_digest"], payload["target_digest"], prefix, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild and verify the target pack from ``base`` and ``delta``."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply requires a ContextPack and a ContextPackDelta")  # noqa: TRY003
    base.verify()
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the base pack")  # noqa: TRY003
    prefix = _require_prefix(delta.prefix_items)
    if prefix > len(base.items):
        raise ContextPackError("prefix_items exceeds the base chunk count")  # noqa: TRY003
    target = ContextPack.build((*base.items[:prefix], *_require_items(delta.appended)))
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack does not match the target digest")  # noqa: TRY003
    return target


def aligned_context_pack(base: ContextPack, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
    """Chunk ``content`` so that it shares the longest possible chunk prefix with ``base``.

    Leading base chunks are reused verbatim while ``content`` keeps matching them; the
    remaining suffix is chunked with the regular :meth:`ContextPack.from_content` rules.
    Reused chunks are complete UTF-8 sequences, so the suffix starts on a character boundary.

    The canonical :meth:`ContextPack.from_content` chunking is returned instead whenever it
    reuses at least as many base bytes and its delta against ``base`` is no larger, so the
    target equals the canonical pack (same items and digest) whenever that costs nothing.
    """
    if not isinstance(content, str):
        raise ContextPackError("content must be text")  # noqa: TRY003
    limit = _require_chunk_bytes(chunk_bytes)
    data = content.encode("utf-8")
    reused: list[bytes] = []
    position = 0
    for item in base.items:
        if not item or len(item) > limit or not data.startswith(item, position):
            break
        try:
            item.decode("utf-8")
        except UnicodeDecodeError:
            break
        reused.append(item)
        position += len(item)
    aligned = ContextPack.build((*reused, *_chunk(data[position:], limit)))
    canonical = ContextPack.build(_chunk(data, limit))
    if canonical == aligned:
        return canonical
    if _shared_prefix_bytes(base, canonical) >= _shared_prefix_bytes(base, aligned) and len(
        ContextPackDelta.between(base, canonical).to_bytes()
    ) <= len(ContextPackDelta.between(base, aligned).to_bytes()):
        return canonical
    return aligned


def _shared_prefix_bytes(base: ContextPack, target: ContextPack) -> int:
    shared = 0
    for left, right in zip(base.items, target.items, strict=False):
        if left != right:
            break
        shared += len(left)
    return shared


def _cut_points(data: bytes) -> tuple[set[int], set[int]]:
    hard: set[int] = set()
    soft: set[int] = set()
    for marker in _HARD_CUT_AFTER:
        hard.update(match.end() for match in re.finditer(re.escape(marker), data))
    for marker in _HARD_CUT_BEFORE:
        hard.update(match.start() for match in re.finditer(re.escape(marker), data))
    for marker in _SOFT_CUT_AFTER:
        soft.update(match.end() for match in re.finditer(re.escape(marker), data))
    hard.discard(0)
    hard.discard(len(data))
    soft.discard(0)
    return hard, soft


def _chunk(data: bytes, limit: int) -> tuple[bytes, ...]:
    if not data:
        return ()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("content is not valid UTF-8") from error  # noqa: TRY003
    hard, soft = _cut_points(data)
    hard_sorted = sorted(hard)
    chunks: list[bytes] = []
    start = 0
    size = len(data)
    next_hard = 0
    while start < size:
        while next_hard < len(hard_sorted) and hard_sorted[next_hard] <= start:
            next_hard += 1
        window_end = min(size, start + limit)
        if next_hard < len(hard_sorted) and hard_sorted[next_hard] <= window_end:
            end = hard_sorted[next_hard]
        elif window_end == size:
            end = size
        else:
            end = _soft_end(data, start, window_end, soft)
        chunks.append(data[start:end])
        start = end
    return tuple(chunks)


def _soft_end(data: bytes, start: int, window_end: int, soft: set[int]) -> int:
    # Prefer the last line boundary in the window, unless it would leave a tiny chunk.
    minimum = start + max(1, (window_end - start) // 2)
    for end in range(window_end, minimum - 1, -1):
        if end in soft:
            return end
    for end in range(window_end, start, -1):
        if (data[end] & 0xC0) != 0x80:
            return end
    raise ContextPackError("chunk_bytes is smaller than a single UTF-8 character")  # noqa: TRY003
