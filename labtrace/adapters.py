from __future__ import annotations

import csv
import io
import json
from html.parser import HTMLParser
from typing import Any

from .contracts import LabError


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.table is not None:
                raise LabError("nested_table", "html")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def decode(data: bytes) -> str:
    if len(data) > 8_000_000:
        raise LabError("page_too_large", str(len(data)))
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LabError("invalid_encoding", "utf-8 required") from None


def json_rows(text: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LabError("invalid_json", str(exc)) from None
    if isinstance(value, dict):
        value = value.get("rows")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise LabError("invalid_rows", "json rows must be objects")
    return value


def csv_rows(text: str) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if not reader.fieldnames or any(not item or not item.strip() for item in reader.fieldnames):
            raise LabError("invalid_header", "csv")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise LabError("duplicate_header", "csv")
        rows = list(reader)
    except csv.Error as exc:
        raise LabError("invalid_csv", str(exc)) from None
    if any(None in item or any(value is None for value in item.values()) for item in rows):
        raise LabError("invalid_column_count", "csv")
    return rows


def html_rows(text: str) -> list[dict[str, str]]:
    parser = TableParser()
    parser.feed(text)
    parser.close()
    if len(parser.tables) != 1 or not parser.tables[0]:
        raise LabError("invalid_table", "one table required")
    header, *data = parser.tables[0]
    if not header or not all(header) or len(set(header)) != len(header):
        raise LabError("invalid_header", "html")
    if any(len(row) != len(header) for row in data):
        raise LabError("invalid_column_count", "html")
    return [dict(zip(header, row)) for row in data]


def extract(data: bytes, format_name: str, *, limit: int) -> list[dict[str, Any]]:
    text = decode(data)
    if format_name == "json":
        rows = json_rows(text)
    elif format_name == "csv":
        rows = csv_rows(text)
    elif format_name == "html":
        rows = html_rows(text)
    else:
        raise LabError("invalid_format", format_name)
    if not rows:
        raise LabError("empty_page", format_name)
    if len(rows) > limit:
        raise LabError("page_row_limit", str(len(rows)))
    return rows


def map_fields(row: dict[str, Any], field_map: dict[str, str], required: tuple[str, ...]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for field in required:
        source = field_map.get(field, field)
        if source not in row:
            raise LabError("missing_field", source)
        mapped[field] = row[source]
    for field in ("replicate", "control", "note"):
        source = field_map.get(field, field)
        if source in row:
            mapped[field] = row[source]
    return mapped
