"""Turning an uploaded file into headings and rows.

Infrastructure, not domain: `csv` and `openpyxl` live here so that validating
a row stays testable without either.

Both readers return the same shape -- a list of headings and a list of
`(row_number, {heading: cell})` -- so nothing downstream knows or cares which
format the operator uploaded. The row number is 1-based and counts the header,
so it matches the gutter in the operator's own spreadsheet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = ["ParseError", "ParsedFile", "parse_bytes", "MAX_ROWS"]

#: A cap, independent of what the file claims. A spreadsheet with a million
#: rows is either a mistake or a way to hold this shared host's memory, and
#: refusing it with a number is kinder than dying halfway through.
MAX_ROWS = 20_000

#: Excel's own limit is far higher, but a file wider than this is not a data
#: file, and the width is multiplied by every row when building the report.
MAX_COLUMNS = 200


class ParseError(ValueError):
    """The file could not be read at all. Distinct from a row being wrong."""


@dataclass(frozen=True, slots=True)
class ParsedFile:
    headings: tuple[str, ...]
    #: (row_number, {heading: raw cell}). Row numbers are 1-based including
    #: the header, so the first data row is 2 -- what the spreadsheet shows.
    rows: tuple[tuple[int, Mapping[str, object]], ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _clean_headings(raw: Sequence[object]) -> tuple[str, ...]:
    headings = [("" if h is None else str(h)).strip() for h in raw]
    # Trailing empty columns are what a spreadsheet produces when somebody
    # deletes a column's contents but not the column. Dropping them here stops
    # every row carrying a phantom "" key.
    while headings and not headings[-1]:
        headings.pop()
    if not headings:
        raise ParseError("the file has no column headings in its first row")
    if len(headings) > MAX_COLUMNS:
        raise ParseError(
            f"the file has {len(headings)} columns, more than the {MAX_COLUMNS} "
            "this importer will read; that is not a data file"
        )
    duplicates = sorted({h for h in headings if headings.count(h) > 1 and h})
    if duplicates:
        raise ParseError(
            "these column headings appear more than once, so a cell would be "
            "ambiguous: " + ", ".join(duplicates)
        )
    return tuple(headings)


def _is_blank(values: Mapping[str, object]) -> bool:
    return all(v is None or str(v).strip() == "" for v in values.values())


def _parse_csv(data: bytes) -> ParsedFile:
    # utf-8-sig, because a CSV exported from Excel on Windows carries a BOM
    # and without this the first heading arrives as '﻿Código' and matches
    # nothing.
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes any byte string
        raise ParseError("the file is not readable as UTF-8 or Latin-1 text")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        # A single-column file gives the sniffer nothing to go on. Comma is
        # the reasonable default and a one-column file parses the same either
        # way.
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError("the file is empty") from None

    headings = _clean_headings(header)
    rows: list[tuple[int, Mapping[str, object]]] = []
    for number, raw in enumerate(reader, start=2):
        values = {h: (raw[i] if i < len(raw) else None) for i, h in enumerate(headings)}
        if _is_blank(values):
            continue  # a trailing newline is not a row
        rows.append((number, values))
        if len(rows) > MAX_ROWS:
            raise ParseError(
                f"the file has more than {MAX_ROWS} rows; split it and import "
                "in parts"
            )
    return ParsedFile(headings, tuple(rows))


def _parse_xlsx(data: bytes) -> ParsedFile:
    try:
        from openpyxl import load_workbook
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency present
        raise ParseError("XLSX support is not installed on this server") from exc

    try:
        # read_only for memory: a workbook with formatting on every cell is
        # several times its data size otherwise. data_only so a formula cell
        # yields its computed value rather than "=SUM(...)".
        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types
        raise ParseError(
            "this does not look like a readable .xlsx file"
        ) from exc

    try:
        sheet = workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        try:
            header = next(iterator)
        except StopIteration:
            raise ParseError("the first sheet is empty") from None

        headings = _clean_headings(header)
        rows: list[tuple[int, Mapping[str, object]]] = []
        for number, raw in enumerate(iterator, start=2):
            values = {
                h: (raw[i] if i < len(raw) else None) for i, h in enumerate(headings)
            }
            if _is_blank(values):
                continue
            rows.append((number, values))
            if len(rows) > MAX_ROWS:
                raise ParseError(
                    f"the file has more than {MAX_ROWS} rows; split it and "
                    "import in parts"
                )
        return ParsedFile(headings, tuple(rows))
    finally:
        workbook.close()


def parse_bytes(data: bytes, filename: str) -> ParsedFile:
    """Read an uploaded file. Chooses the reader by extension.

    By extension rather than by sniffing content: an operator who renames a
    `.xlsx` to `.csv` has made a mistake worth telling them about, and
    guessing would import it as one enormous single-column row instead.
    """
    if not data:
        raise ParseError("the uploaded file is empty")

    lowered = filename.lower()
    if lowered.endswith(".csv") or lowered.endswith(".txt"):
        return _parse_csv(data)
    if lowered.endswith(".xlsx") or lowered.endswith(".xlsm"):
        return _parse_xlsx(data)
    raise ParseError(
        f"cannot tell what kind of file {filename!r} is; upload a .csv or .xlsx"
    )
