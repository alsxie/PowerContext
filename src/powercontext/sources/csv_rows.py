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

"""Row-level captured CSV Sources with exact primary-key identity."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"


def _require_document_id(value: str) -> None:
    if not value or not value.strip() or value != value.strip():
        raise ValueError("document_id must be non-empty without surrounding whitespace")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    _require_document_id(document_id)
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary key value must not be blank")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("CSV row source id exceeds the identity limit")  # noqa: TRY003
    return identity


class CSVDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _consistent(self) -> CSVRowInput:
        _require_document_id(self.document_id)
        if len(self.columns) != len(self.values) or len(set(self.columns)) != len(self.columns):
            raise ValueError("columns and values must align with unique columns")  # noqa: TRY003
        if self.primary_key not in self.columns:
            raise ValueError("primary key must name a column")  # noqa: TRY003
        if self.values[self.columns.index(self.primary_key)] != self.primary_key_value:
            raise ValueError("primary key value must match its column")  # noqa: TRY003
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


class CSVRowSource(Source):
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _identity(self) -> CSVRowSource:
        if self.source_id != self.name or self.source_id != csv_row_source_id(self.document_id, self.primary_key_value):
            raise ValueError("CSV row identity mismatch")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:  # noqa: C901
    _require_document_id(value.document_id)
    if not value.primary_key:
        raise ValueError("primary_key must be non-empty")  # noqa: TRY003
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be UTF-8") from error  # noqa: TRY003
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"malformed CSV: {error}") from error  # noqa: TRY003
    if not records or not records[0]:
        raise ValueError("CSV header is missing")  # noqa: TRY003
    header = tuple(records[0])
    if any(not column.strip() for column in header) or len(set(header)) != len(header):
        raise ValueError("CSV header must contain unique non-blank columns")  # noqa: TRY003
    if value.primary_key not in header:
        raise ValueError("primary key is not a header column")  # noqa: TRY003
    key_index = header.index(value.primary_key)
    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError("CSV record width does not match the header")  # noqa: TRY003
        key = record[key_index]
        if not key.strip() or key in seen:
            raise ValueError("primary key values must be non-blank and unique")  # noqa: TRY003
        seen.add(key)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=key,
                columns=header,
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            materialization=SourceMaterialization.CAPTURED,
            source_id=identity,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSVRowSourceAdapter(),
    version="1",
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
