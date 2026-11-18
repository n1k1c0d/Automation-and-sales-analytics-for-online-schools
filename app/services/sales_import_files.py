import csv
from collections.abc import Generator, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.schemas.sales_import import ROW_MODELS

type RawRow = tuple[int, tuple[object, ...], str | None]


@dataclass(frozen=True)
class ImportRow:
    number: int
    values: dict[str, object]
    error: str | None = None


def _table_rows(table: str, rows: Iterator[RawRow]) -> list[ImportRow]:
    first_row = next(rows, None)
    if first_row is None:
        raise ValueError(f"{table}: header is missing.")
    _, raw_header, header_error = first_row
    header = tuple(
        value.strip() if isinstance(value, str) else value for value in raw_header
    )
    if header_error or not all(isinstance(name, str) and name for name in header):
        raise ValueError(f"{table}: header names must be non-empty text.")
    if len(set(header)) != len(header):
        raise ValueError(f"{table}: header names must be unique.")

    fields = ROW_MODELS[table].model_fields
    optional_columns = {"id", "source_system"}
    if table != "managers":
        optional_columns.add("manager_id" if table == "leads" else "lead_id")
    missing = {name for name, field in fields.items() if field.is_required()} - set(
        header
    )
    if missing:
        raise ValueError(f"{table}: missing columns: {', '.join(sorted(missing))}.")
    extras = set(header) - fields.keys() - optional_columns
    if extras:
        raise ValueError(f"{table}: unexpected columns: {', '.join(sorted(extras))}.")

    items: list[ImportRow] = []
    for number, values, error in rows:
        normalized = tuple(
            value.strip() or None if isinstance(value, str) else value
            for value in values
        )
        if error is None and all(value is None for value in normalized):
            continue
        if any(isinstance(value, str) and "\x00" in value for value in normalized):
            error = error or "Text cells must not contain NUL characters."
        if len(normalized) > len(header) and any(
            value is not None for value in normalized[len(header) :]
        ):
            error = error or "The row contains values beyond the header."
        items.append(
            ImportRow(number, dict(zip(header, normalized, strict=False)), error)
        )
    return items


def _csv_rows(path: Path) -> Generator[RawRow, None, None]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file, strict=True)
        while True:
            number = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                return
            yield number, tuple(row), None


def _read_csv(name: str, path: Path) -> list[ImportRow]:
    with closing(_csv_rows(path)) as rows:
        return _table_rows(name, rows)


def _worksheet_rows(worksheet) -> Iterator[RawRow]:
    for number, row in enumerate(worksheet.iter_rows(), start=1):
        values = [cell.value for cell in row]
        while values and values[-1] is None:
            values.pop()
        error = None
        if any(cell.data_type in {"f", "e"} for cell in row):
            error = (
                "Use plain cell values. Excel formulas and error cells are unsupported."
            )
        yield number, tuple(values), error


def _read_workbook(path: Path) -> dict[str, list[ImportRow]]:
    with path.open("rb") as file:
        try:
            workbook = load_workbook(
                file, read_only=True, data_only=False, keep_links=False
            )
        except (OSError, ValueError, TypeError, IndexError) as exc:
            raise ValueError("Could not read the Excel workbook.") from exc
        try:
            return {
                name: _table_rows(name, _worksheet_rows(workbook[name]))
                for name in ROW_MODELS
                if name in workbook.sheetnames
            }
        finally:
            workbook.close()


def read_import_files(path: Path) -> dict[str, list[ImportRow]]:
    try:
        if path.is_dir():
            tables = {
                name: _read_csv(name, file)
                for name in ROW_MODELS
                if (file := path / f"{name}.csv").is_file()
            }
        elif path.suffix.lower() == ".xlsx":
            tables = _read_workbook(path)
        elif path.suffix.lower() == ".csv" and path.stem in ROW_MODELS:
            tables = {path.stem: _read_csv(path.stem, path)}
        else:
            raise ValueError(
                "Use a directory of sales CSVs, a named sales CSV, or an .xlsx workbook."
            )
    except (
        BadZipFile,
        InvalidFileException,
        ParseError,
        DefusedXmlException,
        UnicodeError,
        csv.Error,
        KeyError,
    ) as exc:
        raise ValueError(
            f"Could not read {path.name}. Check its format and encoding."
        ) from exc

    if not tables:
        raise ValueError("No sales table files or sheets were found.")
    return tables
