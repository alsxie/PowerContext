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
import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.page_retrieval import rank_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FTS_POOL = 100


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


def _normalize_page_text(raw: str | None) -> str:
    # Layout whitespace is not content: collapse horizontal runs and trim each line,
    # which keeps every word and number while bounding the Memory entry size.
    text = (raw or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = (_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(lines).strip()


def _extract_page(page: Any) -> str:
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

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
        texts = [_extract_page(page) for page in reader.pages]
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"PDF cannot be parsed completely: {error}") from error  # noqa: TRY003
    if not texts:
        raise ValueError("PDF has no pages")  # noqa: TRY003
    return tuple(
        PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=index, text=text)
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
    return tuple(records[keys[index]].citation for index in order[:limit])


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
