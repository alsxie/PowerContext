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

"""Physical-page captured PDF Sources and evidence-page retrieval."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import unicodedata
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_MEMORY_ENTRY_TEXT_BYTES, MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.page_retrieval import rank_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FTS_POOL = 100
_PAGE_NAME = re.compile(r"pdf:[0-9a-f]{64}:(\d+[:#].+)", re.DOTALL)


def _require_document_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be non-empty and not blank")  # noqa: TRY003
    return value


def _require_digest(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
    return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable Source name for one physical page of one PDF revision."""
    _require_document_id(document_id)
    _require_digest(document_sha256)
    if page_number < 1:
        raise ValueError("page_number must start at 1")  # noqa: TRY003
    prefix = f"pdf:{document_sha256}:{page_number}"
    readable = f"{prefix}:{quote(document_id, safe='')}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    # Long identifiers fall back to a digest; '#' never appears in the quoted form.
    return f"{prefix}#{hashlib.sha256(document_id.encode('utf-8')).hexdigest()}"


def _page_location_key(source_id: str) -> str:
    """Return the revision-independent ``(document_id, page_number)`` part of a page name."""
    match = _PAGE_NAME.fullmatch(source_id)
    return match.group(1) if match else source_id


class PDFDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        return _require_document_id(value)


class PDFPageInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        return _require_document_id(value)

    @field_validator("document_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_digest(value)


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def _identity(self) -> PDFPageSource:
        if self.materialization is not SourceMaterialization.CAPTURED:
            raise ValueError("PDF page Sources are captured")  # noqa: TRY003
        if self.name != pdf_page_source_id(self.document_id, self.document_sha256, self.page_number):
            raise ValueError("PDF page identity mismatch")  # noqa: TRY003
        return self


_HORIZONTAL_SPACE = re.compile(r"[ \t\f\v\u00a0]+")
# Content tokens of page text: lowercase ASCII words and digit runs.
_TOKEN = re.compile(r"[a-z]+|\d+")
_ASCII_PUNCTUATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u2032": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2033": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u2022": "*",
    "\u00b7": "*",
    "\u2026": "...",
})
_SPACE_BY_PUNCTUATION = re.compile(r"(?<=[^\w\s]) | (?=[^\w\s])")


def _normalize_page_text(raw: str | None) -> str:
    # Layout whitespace is not content: collapse horizontal runs and trim each line,
    # which keeps every word and number while bounding the Memory entry size.
    text = (raw or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    lines = (_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(lines).strip()


def _entry_bytes(document_id: str, text: str) -> int:
    # Exactly what Memory stores for the text projection: NFC, stripped, UTF-8.
    return len(unicodedata.normalize("NFC", f"{document_id}\n{text}").strip().encode("utf-8"))


def _tokens(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


def _token_lines(text: str) -> list[list[str]]:
    lines = (_TOKEN.findall(line.lower()) for line in text.split("\n"))
    return [line for line in lines if line]


def _drop_longest_tokens(lines: list[list[str]], excess: int) -> str:
    # Each dropped token frees its bytes plus one separator; dropping the longest
    # tokens first loses the fewest words for the bytes that must go.
    positions = sorted(
        ((row, column) for row, line in enumerate(lines) for column in range(len(line))),
        key=lambda item: (-len(lines[item[0]][item[1]]), -item[0], -item[1]),
    )
    dropped = set()
    for row, column in positions:
        if excess <= 0:
            break
        dropped.add((row, column))
        excess -= len(lines[row][column].encode("utf-8")) + 1
    kept = (
        " ".join(token for column, token in enumerate(line) if (row, column) not in dropped)
        for row, line in enumerate(lines)
    )
    return "\n".join(line for line in kept if line)


def _fit_memory_entry(document_id: str, text: str) -> str:
    """Compact ``text`` only as far as needed for one Memory entry of the projection.

    The text projection ``DOCUMENT_ID\\nPAGE_TEXT`` becomes exactly one Memory entry,
    so a dense page must fit the entry byte limit. The lossless steps keep the
    page's word and number multiset unchanged; only if they are not enough are the
    longest tokens dropped, which keeps the most words.
    """
    limit = MAX_MEMORY_ENTRY_TEXT_BYTES
    if _entry_bytes(document_id, text) <= limit:
        return text
    if _entry_bytes(document_id, "") > limit:
        raise ValueError("document_id is too long for a page Memory entry")  # noqa: TRY003
    original = _tokens(text)
    for compact in (
        lambda value: value.translate(_ASCII_PUNCTUATION),
        lambda value: _SPACE_BY_PUNCTUATION.sub("", value),
        lambda value: "\n".join(" ".join(line) for line in _token_lines(value)),
    ):
        candidate = compact(text)
        if _tokens(candidate) != original:
            continue
        text = candidate
        if _entry_bytes(document_id, text) <= limit:
            return text
    lines = _token_lines(text)
    excess = _entry_bytes(document_id, "\n".join(" ".join(line) for line in lines)) - limit
    while True:
        text = _drop_longest_tokens(lines, excess)
        overflow = _entry_bytes(document_id, text) - limit
        if overflow <= 0:
            return text
        excess += overflow


# pypdf recovers from these only by dropping content, so the page would be partial.
_CONTENT_LOSS = re.compile(r"while decompressing|^Object \d+ \d+ not defined")


class _ContentLossRecorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.thread = threading.get_ident()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread and _CONTENT_LOSS.search(record.getMessage()):
            self.messages.append(record.getMessage())


@contextmanager
def _record_content_loss() -> Iterator[_ContentLossRecorder]:
    logger = logging.getLogger("pypdf")
    recorder = _ContentLossRecorder()
    level = logger.level
    logger.addHandler(recorder)
    if logger.getEffectiveLevel() > logging.WARNING:
        logger.setLevel(logging.WARNING)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(level)


def _complete_flate(stream: Any) -> bool:
    # pypdf decodes a damaged or cut-off Flate content stream to a silent prefix;
    # a complete stream decodes without error up to its end-of-stream marker.
    filters = stream.get("/Filter")
    first = filters[0] if isinstance(filters, list) and filters else filters
    if first != "/FlateDecode":
        return True
    decoder = zlib.decompressobj()
    try:
        while True:
            decoder.decompress(decoder.unconsumed_tail or stream._data, 1 << 20)
            if decoder.eof or not decoder.unconsumed_tail:
                return decoder.eof
    except zlib.error:
        return False


def _require_contents(page: Any) -> None:
    # A declared content stream must resolve and decode completely; otherwise the
    # page would silently read as blank or partial.
    from pypdf.generic import NullObject, StreamObject

    contents = page.get("/Contents")
    if contents is None:
        return
    resolved = contents.get_object()
    parts = list(resolved) if isinstance(resolved, list) else [contents]
    for part in parts:
        stream = part.get_object()
        if stream is None or isinstance(stream, NullObject):
            raise ValueError("PDF page content stream is missing")  # noqa: TRY003
        if isinstance(stream, StreamObject) and not _complete_flate(stream):
            raise ValueError("PDF page content stream is damaged")  # noqa: TRY003


def _extract_page(page: Any) -> str:
    _require_contents(page)
    try:
        return _normalize_page_text(page.extract_text())
    except Exception:
        # One retry in layout mode before the whole document is rejected.
        return _normalize_page_text(page.extract_text(extraction_mode="layout"))


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of a complete PDF, failing atomically."""
    _require_document_id(value.document_id)
    content = value.content
    if not content:
        raise ValueError("PDF content is empty")  # noqa: TRY003
    digest = hashlib.sha256(content).hexdigest()
    try:
        from pypdf import PasswordType, PdfReader

        with _record_content_loss() as recorder:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
            texts = [_extract_page(page) for page in reader.pages]
        if recorder.messages:
            raise ValueError(f"PDF content is damaged: {recorder.messages[0]}")  # noqa: TRY003, TRY301
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"PDF cannot be parsed completely: {error}") from error  # noqa: TRY003
    if not texts:
        raise ValueError("PDF has no pages")  # noqa: TRY003
    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=index,
            text=_fit_memory_entry(value.document_id, text),
        )
        for index, text in enumerate(texts, start=1)
    )


class PDFPageSourceAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` exact citations of distinct pages relevant to ``question``."""
    if limit < 1:
        raise ValueError("limit must be positive")  # noqa: TRY003
    if not question.strip():
        return ()
    from powercontext.builtin.runtime import SearchMemoryRequest

    page = await memory.list()
    records: dict[str, Any] = {}
    for record in page.entries:
        sources = record.entry.sources
        if sources:
            records.setdefault(sources[0].source_id, record)
    if not records:
        return ()
    keys = tuple(records)
    # Full-text Memory search contributes a rank prior; local ranking covers every page.
    hits = await memory.search(SearchMemoryRequest(query=question, limit=_FTS_POOL, mode="fts"))
    fts_rank = {(hit.entry_id, hit.entry_version_id): rank for rank, hit in enumerate(hits.hits)}
    prior = []
    for key in keys:
        rank = fts_rank.get((records[key].entry.entry_id, records[key].entry.entry_version_id))
        prior.append(0.0 if rank is None else 1.0 / (1.0 + rank / 10.0))
    order = rank_pages(question, tuple(records[key].entry.text for key in keys), prior)
    # Revisions of one document share (document_id, page_number); keep the best-ranked.
    seen: set[str] = set()
    citations = []
    for index in order:
        page_key = _page_location_key(keys[index])
        if page_key in seen:
            continue
        seen.add(page_key)
        citations.append(records[keys[index]].citation)
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
