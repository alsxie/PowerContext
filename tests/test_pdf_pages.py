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
import hashlib
import io

import pytest

from powercontext.sources import SourceDefinitionRegistry, project_source_for_transport
from powercontext.sources.page_retrieval import rank_pages
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    pdf_page_source_id,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

pypdf = pytest.importorskip("pypdf")


def _blank_pdf(pages: int, *, title: str | None = None, encrypt: bool = False) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=200)
    if title:
        writer.add_metadata({"/Title": title})
    if encrypt:
        writer.encrypt(user_password="", owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_blank_pages_keep_order_digest_and_revision_identity() -> None:
    content = _blank_pdf(2)
    pages = parse_pdf_document(PDFDocumentInput(document_id="doc", content=content))
    assert [(page.page_number, page.text) for page in pages] == [(1, ""), (2, "")]
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(content).hexdigest()}
    revised = parse_pdf_document(PDFDocumentInput(document_id="doc", content=_blank_pdf(2, title="v2")))
    assert pages[0].document_sha256 != revised[0].document_sha256


def test_empty_user_password_is_readable() -> None:
    pages = parse_pdf_document(PDFDocumentInput(document_id="locked", content=_blank_pdf(1, encrypt=True)))
    assert len(pages) == 1


@pytest.mark.parametrize("raw", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_invalid_documents_raise_value_error(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="bad", content=raw))


def test_blank_document_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id="  ", content=b"%PDF")


def test_source_identity_and_text_projection() -> None:
    digest = "a" * 64
    page = PDFPageInput(document_id="Report 2022", document_sha256=digest, page_number=3, text="Net revenue 10")
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))

    async def scenario() -> None:
        source = await registry.resolve(page)
        assert source.name == pdf_page_source_id("Report 2022", digest, 3)
        assert await registry.read(source) == page
        evidence = project_source_for_transport(registry, source).projection(TEXT_EVIDENCE_PROJECTION_KEY)
        assert evidence["source_type"] == "pdf-page"
        assert evidence["content"] == "Report 2022\nNet revenue 10"
        assert evidence["metadata"] == {"document_id": "Report 2022", "document_sha256": digest, "page_number": 3}

    asyncio.run(scenario())
    assert len(pdf_page_source_id("x" * 1000, digest, 1)) <= 256
    assert pdf_page_source_id("x" * 1000, digest, 1) != pdf_page_source_id("y" * 1000, digest, 1)


def test_rank_pages_prefers_named_document_and_statement_pages() -> None:
    texts = [
        "ACME_2022_10K\nConsolidated Statement of Cash Flows\n2022 2021\nCapital spending (5) (4)\nDepreciation 3 2",
        "ACME_2022_10K\nOur products are sold worldwide.",
        "OTHER_2022_10K\nConsolidated Statement of Cash Flows\n2022 2021\nCapital spending (9) (8)\nDepreciation 1 1",
    ]
    order = rank_pages("What is the FY2022 capex for Acme based on the cash flow statement?", texts)
    assert order[0] == 0
