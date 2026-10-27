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
import csv
import io
import json
import random
from urllib.parse import quote

import pytest

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceDefinitionRegistry, project_source_for_transport
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.models import SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

REGISTRY = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
METADATA_KEYS = ["document_id", "primary_key", "primary_key_value", "columns", "row"]


def _parse(content: bytes, primary_key: str = "id", document_id: str = "doc") -> tuple[CSVRowInput, ...]:
    return parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content))


def _values(content: bytes, primary_key: str = "id") -> list[tuple[str, ...]]:
    return [row.values for row in _parse(content, primary_key)]


def _reference(  # noqa: C901 - one linear specification of the documented parsing rules.
    document_id: str, primary_key: str, content: bytes
) -> tuple[tuple[tuple[str, ...], ...], ...] | None:
    """Specification of row parsing written directly against ``csv.reader(strict=True)``."""
    if not document_id or document_id != document_id.strip() or not primary_key:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text.removeprefix("\ufeff")
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        return None
    if not records or not records[0]:
        return None
    header = records[0]
    if any(not column.strip() for column in header) or len(set(header)) != len(header) or primary_key not in header:
        return None
    key_index = header.index(primary_key)
    rows, seen = [], set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            return None
        key = record[key_index]
        if not key.strip() or key in seen:
            return None
        seen.add(key)
        if len(f"csv:{quote(document_id, safe='')}:{quote(key, safe='')}") > MAX_SOURCE_ID_LENGTH:
            return None
        rows.append((tuple(header), tuple(record)))
    return tuple(rows)


_ATOMS = [
    *("a", "b", "1", "01", " ", "\t", "x y", " k ", "é", "中文", "😀", ":", "%", "%3A", "/", ",", '"', '""'),
    *("\r", "\n", "\r\n", "\x00", "\u2028", "\ufeff", "\u3000", "", "", "", "null", "true", "1.50", "0x1"),
]


def _field(rng: random.Random) -> str:
    return "".join(rng.choice(_ATOMS) for _ in range(rng.choice([0, 1, 1, 2, 3])))


def _encode(rng: random.Random, value: str) -> str:
    mode = rng.random()
    if any(character in value for character in ',"\r\n') or mode < 0.3:
        return '"' + value.replace('"', '""') + '"'
    if mode < 0.33:
        return value + '"x'  # deliberately malformed quoting
    return value


def _random_document(rng: random.Random) -> tuple[str, str, bytes]:  # noqa: C901 - one random case generator.
    width = rng.randint(1, 4)
    header = [rng.choice(["id", " id ", "name", "note", "x", "id", "", " ", "é"]) for _ in range(width)]
    if rng.random() < 0.7:
        header = ["id", "name", "note", " sp "][:width]
    newline = rng.choice(["\n", "\r\n", "\r"])
    lines = [",".join(_encode(rng, column) for column in header)]
    for position in range(rng.randint(0, 6)):
        choice = rng.random()
        if choice < 0.08:
            lines.append("")
            continue
        if choice < 0.12:
            lines.append('""')
            continue
        record_width = width if rng.random() > 0.08 else rng.randint(0, width + 1)
        values = [_field(rng) for _ in range(record_width)]
        if values and rng.random() < 0.7:
            values[0] = rng.choice([str(position), f" {position} ", f"k:{position}", f"%25{position}", "é1", "1", ""])
        lines.append(",".join(_encode(rng, value) for value in values))
    data = (newline.join(lines) + (newline if rng.random() < 0.7 else "")).encode()
    damage = rng.random()
    if damage < 0.15:
        data = b"\xef\xbb\xbf" + data
    elif damage < 0.18:
        data += b"\xff"
    elif damage < 0.20:
        data = b"\xed\xa0\x80" + data
    elif damage < 0.22:
        data = b""
    elif damage < 0.23:
        data = b"\xef\xbb\xbf"
    document_id = rng.choice(["doc", "doc:1", "d%2F", "文档", " doc", "doc ", "", "a" * 250, "d/x"])
    primary_key = rng.choice(["id", "id", " id ", "name", "missing", ""]) if rng.random() < 0.3 else header[0]
    return document_id, primary_key, data


async def _assert_row_round_trip(row: CSVRowInput) -> None:
    source = await REGISTRY.resolve(row)
    assert isinstance(source, CSVRowSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert source.name == source.source_id == csv_row_source_id(row.document_id, row.primary_key_value)
    assert await REGISTRY.read(source) == row
    observation = project_source_for_transport(REGISTRY, source)
    assert observation.name == source.name
    evidence = observation.projections[0].value
    mapping = dict(zip(row.columns, row.values, strict=True))
    assert evidence["content"] == json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
    assert list(evidence["metadata"]) == METADATA_KEYS
    assert list(evidence["metadata"]["row"]) == list(row.columns)


def test_random_documents_match_csv_reader_reference() -> None:
    rng = random.Random(20260101)  # noqa: S311 - reproducible differential cases.
    outcomes = {"valid": 0, "invalid": 0}

    async def scenario() -> None:
        for _ in range(500):
            document_id, primary_key, data = _random_document(rng)
            expected = _reference(document_id, primary_key, data)
            try:
                rows = parse_csv_document(
                    CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=data)
                )
            except ValueError:
                actual = None
            else:
                actual = tuple((row.columns, row.values) for row in rows)
                for row in rows:
                    assert (row.document_id, row.primary_key) == (document_id, primary_key)
                    assert row.primary_key_value == row.values[row.columns.index(primary_key)]
                    await _assert_row_round_trip(row)
            assert actual == expected, (document_id, primary_key, data)
            outcomes["invalid" if actual is None else "valid"] += 1

    asyncio.run(scenario())
    # The generator must exercise both accepted and rejected documents.
    assert outcomes["valid"] >= 50
    assert outcomes["invalid"] >= 50


def test_byte_order_mark_is_not_part_of_the_first_column() -> None:
    rows = _parse(b"\xef\xbb\xbfid,name\r\n1,a\r\n")
    assert rows[0].columns == ("id", "name")
    assert rows[0].values == ("1", "a")


def test_only_one_leading_byte_order_mark_is_removed() -> None:
    with pytest.raises(ValueError, match="primary key is not a header column"):
        _parse(b"\xef\xbb\xbf\xef\xbb\xbfid,name\n1,a\n")


def test_quoted_fields_keep_embedded_newlines_and_separators() -> None:
    assert _values(b'id,note\r\n1,"line one\r\nline two, \xe2\x80\xa8 ""quoted"""\r\n2,"a\rb"\n') == [
        ("1", 'line one\r\nline two, \u2028 "quoted"'),
        ("2", "a\rb"),
    ]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_blank_records_are_skipped(newline: bytes) -> None:
    content = newline.join([b"id,name", b"", b"1,a", b"", b"", b"2,b", b""])
    assert _values(content) == [("1", "a"), ("2", "b")]


def test_values_are_kept_verbatim() -> None:
    assert _values(b"id,v,e\n007,1.50,\n 8 , x ,null\n") == [("007", "1.50", ""), (" 8 ", " x ", "null")]


@pytest.mark.parametrize("content", [b"id,name", b"id,name\n", b"id,name\r\n\r\n\n"])
def test_header_only_document_has_no_rows(content: bytes) -> None:
    assert _parse(content) == ()


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf", b"\nid,name\n1,a\n"])
def test_missing_header_is_rejected(content: bytes) -> None:
    with pytest.raises(ValueError, match="CSV header is missing"):
        _parse(content)


@pytest.mark.parametrize(
    "content",
    [b"id,name\n1,a,\n", b"id,name\n1\n", b'id,name\n""\n', b"id,name\n \n", b"id,name\n1,a\n2,b,c\n"],
)
def test_width_mismatch_rejects_the_whole_document(content: bytes) -> None:
    with pytest.raises(ValueError, match="width"):
        _parse(content)


@pytest.mark.parametrize(
    "content",
    [b"id,name\n1,a\n1,b\n", b"id,name\n1,a\n,b\n", b"id,name\n \t,a\n", "id,name\n\u3000,a\n".encode()],
)
def test_duplicate_or_blank_primary_keys_reject_the_whole_document(content: bytes) -> None:
    with pytest.raises(ValueError, match="primary key values"):
        _parse(content)


def test_keys_differing_only_by_whitespace_are_distinct() -> None:
    rows = _parse(b"id,name\n1,a\n 1,b\n1 ,c\n")
    assert [row.primary_key_value for row in rows] == ["1", " 1", "1 "]
    assert len({csv_row_source_id(row.document_id, row.primary_key_value) for row in rows}) == 3


@pytest.mark.parametrize("header", [b"id,", b"id, ", b"id,name,id", b"id,\t"])
def test_header_columns_must_be_unique_and_non_blank(header: bytes) -> None:
    with pytest.raises(ValueError, match="unique non-blank columns"):
        _parse(header + b"\n")


def test_whitespace_column_names_are_exact() -> None:
    rows = _parse(b" id ,name\n1,a\n", primary_key=" id ")
    assert rows[0].columns == (" id ", "name")
    assert rows[0].primary_key == " id "
    with pytest.raises(ValueError, match="primary key is not a header column"):
        _parse(b" id ,name\n1,a\n", primary_key="id")
    # " id" and "id" are different columns, so this header is unique.
    assert _parse(b"id, id\n1,2\n")[0].columns == ("id", " id")


@pytest.mark.parametrize(
    "content", [b"id\n\xff\n", b"id\n\xed\xa0\x80\n", b"id\n\xc0\xaf\n", "id\ncaf\xe9\n".encode("latin-1")]
)
def test_invalid_utf8_is_rejected(content: bytes) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        _parse(content)


@pytest.mark.parametrize("content", [b'id,name\n1,"a"b\n', b'id,name\n1,"a', b'id,name\n"1\n'])
def test_malformed_quoting_is_rejected(content: bytes) -> None:
    with pytest.raises(ValueError, match="malformed CSV"):
        _parse(content)


def test_quotes_inside_unquoted_fields_are_literal() -> None:
    assert _values(b'id,name\n1,a"b\n2, "c"\n') == [("1", 'a"b'), ("2", ' "c"')]


@pytest.mark.parametrize(
    ("document_id", "primary_key"), [("", "id"), (" doc", "id"), ("doc ", "id"), ("d\u00a0", "id"), ("doc", "")]
)
def test_invalid_document_id_or_primary_key_is_rejected(document_id: str, primary_key: str) -> None:
    with pytest.raises(ValueError, match=r"document_id|primary_key"):
        _parse(b"id\n1\n", primary_key=primary_key, document_id=document_id)


def test_identity_percent_encodes_both_parts_injectively() -> None:
    assert csv_row_source_id("doc:one", "a:b") == "csv:doc%3Aone:a%3Ab"
    assert csv_row_source_id("文档", "é/😀") == f"csv:{quote('文档', safe='')}:{quote('é/😀', safe='')}"
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a%3Ab", "c") != csv_row_source_id("a:b", "c")
    rng = random.Random(7)  # noqa: S311 - reproducible identity pairs.
    alphabet = ["a", ":", "%", "3", "A", "/", " ", "é", "%3A", "::"]
    owners: dict[str, tuple[str, str]] = {}
    for _ in range(2000):
        document_id = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4))).strip() or "d"
        key = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
        if not key.strip():
            continue
        identity = csv_row_source_id(document_id, key)
        assert owners.setdefault(identity, (document_id, key)) == (document_id, key)


def test_identity_is_limited_to_256_encoded_characters() -> None:
    prefix = "csv:d:"
    longest = "x" * (MAX_SOURCE_ID_LENGTH - len(prefix))
    assert len(csv_row_source_id("d", longest)) == MAX_SOURCE_ID_LENGTH == 256
    with pytest.raises(ValueError, match="identity limit"):
        csv_row_source_id("d", longest + "x")
    # The limit applies after encoding: each "é" becomes six characters.
    with pytest.raises(ValueError, match="identity limit"):
        csv_row_source_id("d", "é" * 42)
    with pytest.raises(ValueError, match="identity limit"):
        _parse(b"id\n1\n" + b"x" * 300 + b"\n")


def test_row_input_must_be_consistent() -> None:
    with pytest.raises(ValueError, match="align"):
        CSVRowInput(document_id="d", primary_key="id", primary_key_value="1", columns=("id", "id"), values=("1", "1"))
    with pytest.raises(ValueError, match="name a column"):
        CSVRowInput(document_id="d", primary_key="id", primary_key_value="1", columns=("k",), values=("1",))
    with pytest.raises(ValueError, match="match its column"):
        CSVRowInput(document_id="d", primary_key="id", primary_key_value="2", columns=("id",), values=("1",))


def test_projection_is_compact_json_with_exactly_five_metadata_keys() -> None:
    row = CSVRowInput(
        document_id="文档",
        primary_key=" id ",
        primary_key_value=" 1 ",
        columns=(" id ", "名称", "empty"),
        values=(" 1 ", 'é, "q"', ""),
    )

    async def scenario() -> None:
        source = await REGISTRY.resolve(row)
        evidence = REGISTRY.project(source, TEXT_EVIDENCE_PROJECTION_KEY)
        payload = evidence if isinstance(evidence, dict) else evidence.model_dump()
        assert payload["source_type"] == "csv-row"
        assert payload["source_id"] == csv_row_source_id("文档", " 1 ")
        assert payload["content"] == '{" id ":" 1 ","名称":"é, \\"q\\"","empty":""}'
        assert list(payload["metadata"]) == METADATA_KEYS
        assert payload["metadata"] == {
            "document_id": "文档",
            "primary_key": " id ",
            "primary_key_value": " 1 ",
            "columns": [" id ", "名称", "empty"],
            "row": {" id ": " 1 ", "名称": 'é, "q"', "empty": ""},
        }
        await _assert_row_round_trip(row)

    asyncio.run(scenario())


def test_source_rejects_identity_mismatch() -> None:
    with pytest.raises(ValueError, match="identity mismatch"):
        CSVRowSource(
            name="csv:d:2",
            materialization=SourceMaterialization.CAPTURED,
            source_id="csv:d:2",
            document_id="d",
            primary_key="id",
            primary_key_value="1",
            columns=("id",),
            values=("1",),
        )
