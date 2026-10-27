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
import builtins
import io
import re
import unicodedata
from collections import Counter
from types import SimpleNamespace

import pytest

from powercontext.limits import MAX_MEMORY_ENTRY_TEXT_BYTES
from powercontext.sources import page_retrieval
from powercontext.sources.page_retrieval import rank_pages
from powercontext.sources.pdf_pages import (
    PDFDocumentInput,
    _fit_memory_entry,
    _normalize_page_text,
    parse_pdf_document,
    pdf_page_source_id,
    search_pdf_pages,
)

_TOKEN = re.compile(r"[a-z]+|\d+")


def _tokens(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


def _entry_bytes(document_id: str, text: str) -> int:
    return len(unicodedata.normalize("NFC", f"{document_id}\n{text}").strip().encode("utf-8"))


def test_memory_entry_limit_is_the_canonical_limit() -> None:
    from powercontext.builtin.artifacts.memory.canonical import normalize_text

    normalize_text("x" * MAX_MEMORY_ENTRY_TEXT_BYTES)
    with pytest.raises(ValueError):
        normalize_text("x" * (MAX_MEMORY_ENTRY_TEXT_BYTES + 1))


def test_page_text_is_nfc_normalized() -> None:
    assert _normalize_page_text("Cafe\u0301  \u00a0 r\u00e9sum\u00e9\r\n") == "Caf\u00e9 r\u00e9sum\u00e9"


def test_small_page_is_left_unchanged() -> None:
    text = "Revenue \u2014 \u201cnet\u201d sales , 2022 : 1,234"
    assert _fit_memory_entry("DOC", text) == text


def test_dense_page_is_compacted_losslessly_when_possible() -> None:
    line = "Net sales \u2014 \u201cAmericas\u201d ( 1,234.5 ) , 2022 ; 2021 \u2019s"
    text = "\n".join(f"{line} {index}" for index in range(160))
    assert _entry_bytes("REPORT_2022_10K", text) > MAX_MEMORY_ENTRY_TEXT_BYTES
    fitted = _fit_memory_entry("REPORT_2022_10K", text)
    assert _entry_bytes("REPORT_2022_10K", fitted) <= MAX_MEMORY_ENTRY_TEXT_BYTES
    assert _tokens(fitted) == _tokens(text)


def test_overfull_page_drops_only_the_longest_tokens() -> None:
    words = ["abcdefghij"[: 2 + index % 9] for index in range(1250)]
    text = " ".join(words)
    assert _entry_bytes("DOC", text) > MAX_MEMORY_ENTRY_TEXT_BYTES
    fitted = _fit_memory_entry("DOC", text)
    assert _entry_bytes("DOC", fitted) <= MAX_MEMORY_ENTRY_TEXT_BYTES
    kept, original = _tokens(fitted), _tokens(text)
    assert not kept - original
    assert sum(kept.values()) >= 0.9 * sum(original.values())
    dropped = original - kept
    shortest_dropped = min(len(token) for token in dropped)
    assert all(len(token) <= shortest_dropped for token in kept)


def test_long_document_id_still_fits_and_oversized_id_is_rejected() -> None:
    document_id = "R" * 7000
    fitted = _fit_memory_entry(document_id, "alpha beta gamma " * 200)
    assert _entry_bytes(document_id, fitted) <= MAX_MEMORY_ENTRY_TEXT_BYTES
    assert fitted
    with pytest.raises(ValueError):
        _fit_memory_entry("R" * (MAX_MEMORY_ENTRY_TEXT_BYTES + 1), "")


def _text_pdf(lines_per_page: list[list[bytes]], *, compress: bool = True) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject  # ty: ignore[unresolved-import]

    writer = pypdf.PdfWriter()
    font = writer._add_object(
        DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
    )
    for lines in lines_per_page:
        page = writer.add_blank_page(width=2000, height=4000)
        body = b"BT /F1 6 Tf 10 3990 Td " + b" ".join(b"(" + line + b") Tj 0 -7 Td" for line in lines) + b" ET"
        stream = DecodedStreamObject()
        stream.set_data(body)
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode() if compress else stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_dense_pdf_page_parses_within_the_memory_entry_limit() -> None:
    dense = [b"Revenue 1234 , cost of sales 567 ; operating income 890 . " * 3 for _ in range(50)]
    raw = _text_pdf([[b"Cover page"], dense])
    pages = parse_pdf_document(PDFDocumentInput(document_id="DENSE_2022_10K", content=raw))
    assert [page.page_number for page in pages] == [1, 2]
    assert pages[0].text == "Cover page"
    assert _entry_bytes("DENSE_2022_10K", pages[1].text) <= MAX_MEMORY_ENTRY_TEXT_BYTES
    # Spaces and punctuation go first; every word and number survives.
    assert sum(_tokens(pages[1].text).values()) == 50 * 3 * 9


def _flate_body_span(raw: bytes, page_index: int) -> tuple[int, int]:
    starts = [match.end() for match in re.finditer(rb"/FlateDecode[^>]*>>\s*stream\r?\n", raw)]
    start = starts[page_index]
    return start, raw.index(b"endstream", start)


def test_damaged_content_streams_fail_atomically() -> None:
    raw = _text_pdf([[b"Revenue 1234 page one"], [b"Net income 5678 page two"]])
    assert [page.text for page in parse_pdf_document(PDFDocumentInput(document_id="ok", content=raw))] == [
        "Revenue 1234 page one",
        "Net income 5678 page two",
    ]
    start, end = _flate_body_span(raw, 1)
    garbled = raw[: start + 2] + bytes(byte ^ 0x5A for byte in raw[start + 2 : end - 1]) + raw[end - 1 :]
    truncated = raw[: start + 4] + raw[end - 1 :]
    references = re.findall(rb"/Contents (\d+) 0 R", raw)
    dangling = raw.replace(b"/Contents " + references[-1] + b" 0 R", b"/Contents 999 0 R")
    for damaged in (garbled, truncated, dangling):
        with pytest.raises(ValueError):
            parse_pdf_document(PDFDocumentInput(document_id="damaged", content=damaged))


class _FakeMemory:
    """The public scoped Memory surface used by ``search_pdf_pages``."""

    def __init__(self, pages: builtins.list[tuple[str, str, int, str]]) -> None:
        self.records = [
            SimpleNamespace(
                entry=SimpleNamespace(
                    entry_id=f"e{index}",
                    entry_version_id="v1",
                    text=f"{document_id}\n{text}",
                    sources=(SimpleNamespace(source_id=pdf_page_source_id(document_id, digest, page)),),
                ),
                citation=(document_id, digest, page),
            )
            for index, (document_id, digest, page, text) in enumerate(pages)
        ]

    async def list(self) -> SimpleNamespace:
        return SimpleNamespace(entries=self.records)

    async def search(self, request: object) -> SimpleNamespace:
        return SimpleNamespace(hits=[])


def test_search_returns_distinct_pages_across_document_revisions() -> None:
    pytest.importorskip("powercontext.builtin.runtime")
    old, new = "a" * 64, "b" * 64
    long_id = "L" * 400
    pages = [
        (document_id, digest, page, f"Legal proceedings and litigation claims page {page}")
        for document_id in ("ACME_2022_10K", long_id)
        for digest in (old, new)
        for page in range(1, 6)
    ]
    citations = asyncio.run(search_pdf_pages(_FakeMemory(pages), "legal proceedings", limit=20))
    locations = [(document_id, page) for document_id, _, page in citations]
    assert len(citations) == 10
    assert len(set(locations)) == len(locations)
    limited = asyncio.run(search_pdf_pages(_FakeMemory(pages), "legal proceedings", limit=3))
    assert len(limited) == 3


def _statement_pages() -> list[str]:
    numbers = " ".join(str(value) for value in range(100, 140))
    return [
        f"CO_2022_10K\nConsolidated Statement of Income\nNet sales {numbers}",
        f"CO_2022_10K\nConsolidated Balance Sheet\nProperty plant and equipment net {numbers}",
        f"CO_2022_10K\nConsolidated Statement of Cash Flows\nPurchases of property plant equipment {numbers}",
        "CO_2022_10K\nIndex to Consolidated Financial Statements\n"
        f"Consolidated Statement of Income\nConsolidated Balance Sheet\nConsolidated Statement of Cash Flows {numbers}",
    ] + [f"CO_2022_10K\nBusiness overview narrative number {index} about strategy." for index in range(30)]


def test_capital_intensity_question_favours_the_primary_statements() -> None:
    order = rank_pages("Is CO a capital-intensive business based on FY2022 data?", _statement_pages())
    assert set(order[:3]) == {0, 1, 2}


def test_excluding_acquisitions_asks_for_organic_change() -> None:
    texts = [
        "CO_2022_10K\nNote 3. Acquisitions and divestitures\nWe acquired a business; acquisition costs were 12.",
        "CO_2022_10K\nSales change by business segment\nOrganic sales 1.2 Acquisitions 0.5 Translation (3.9)",
    ] + [f"CO_2022_10K\nOther narrative {index}" for index in range(20)]
    order = rank_pages("If we exclude the impact of M&A, which segment dragged down CO's growth in 2022?", texts)
    assert order[0] == 1


def test_light_stemming_and_possessives() -> None:
    assert page_retrieval._stem("proceedings") == page_retrieval._stem("proceeding") == "proceed"
    assert page_retrieval._stem("cyclicality") == page_retrieval._stem("cyclical")
    query, _ = page_retrieval._query("What is ACME's FY2022 revenue?")
    assert "s" not in query
    assert "fy" not in query


def test_vocabulary_has_no_question_specific_entries() -> None:
    tuned = ("banker", "internet", "providers", "lost", "connection")
    assert not set(tuned) & page_retrieval._STOPWORDS
    patterns = " ".join(pattern for pattern, _ in page_retrieval._EXPANSIONS)
    for phrase in ("production rate", "cyclical", "securities registered", "retain"):
        assert phrase not in patterns
