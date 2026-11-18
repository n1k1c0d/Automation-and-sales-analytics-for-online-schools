import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.database.models import Base
from app.schemas.sales_import import ROW_MODELS
from app.services.sales_import_files import ImportRow, read_import_files

type Outcome = Literal["inserted", "duplicate", "conflict", "invalid"]

PARENT_FIELDS = {
    "leads": ("managers", "manager_external_id", "manager_id"),
    "lessons": ("leads", "lead_external_id", "lead_id"),
    "bank_applications": ("leads", "lead_external_id", "lead_id"),
    "payments": ("leads", "lead_external_id", "lead_id"),
}


@dataclass(frozen=True)
class RowIssue:
    table: str
    row: int
    outcome: Outcome
    message: str


@dataclass
class ImportReport:
    source_system: str
    tables: dict[str, dict[Outcome, int]] = field(default_factory=dict)
    issues: list[RowIssue] = field(default_factory=list)


def _row_values(
    connection: Connection,
    table: str,
    row: ImportRow,
    source_system: str,
    excel_timezone: ZoneInfo | None,
) -> dict[str, object]:
    if row.error:
        raise ValueError(row.error)
    file_source = row.values.get("source_system")
    if file_source is not None and file_source != source_system:
        raise ValueError("source_system does not match the selected source.")

    values = (
        ROW_MODELS[table]
        .model_validate(row.values, context={"excel_timezone": excel_timezone})
        .model_dump()
    )
    values["source_system"] = source_system
    if table in PARENT_FIELDS:
        parent_name, reference_field, foreign_key = PARENT_FIELDS[table]
        parent = Base.metadata.tables[parent_name]
        external_id = values.pop(reference_field)
        record = connection.execute(
            select(parent.c.id, parent.c.created_at).where(
                parent.c.source_system == source_system,
                parent.c.external_id == external_id,
            )
        ).one_or_none()
        if record is None:
            raise ValueError(f"{reference_field} could not be resolved in this source.")
        if parent_name == "leads":
            for column, value in values.items():
                if isinstance(value, datetime) and value < record.created_at:
                    raise ValueError(f"{column} occurs before the lead was created.")
        values[foreign_key] = record.id
    return values


def _store_row(
    connection: Connection, table: Table, values: dict[str, object]
) -> Outcome:
    with connection.begin_nested():
        statement = (
            insert(table)
            .values(values)
            .on_conflict_do_nothing(constraint=f"uq_{table.name}_external_identity")
            .returning(table.c.id)
        )
        if connection.execute(statement).scalar_one_or_none() is not None:
            return "inserted"

    existing = (
        connection.execute(
            select(table).where(
                table.c.source_system == values["source_system"],
                table.c.external_id == values["external_id"],
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None and all(
        existing[column] == value for column, value in values.items()
    ):
        return "duplicate"
    return "conflict"


def _validation_message(error: ValidationError) -> str:
    messages = []
    for detail in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        field = ".".join(str(part) for part in detail["loc"])
        messages.append(f"{field}: {detail['msg']}" if field else detail["msg"])
    return " ".join(messages)


def _import_table(
    connection: Connection,
    name: str,
    rows: list[ImportRow],
    report: ImportReport,
    excel_timezone: ZoneInfo | None,
) -> None:
    table = Base.metadata.tables[name]
    counts = report.tables[name] = dict.fromkeys(
        ("inserted", "duplicate", "conflict", "invalid"), 0
    )

    # Consistent lock order matters when two batches contain the same keys.
    for row in sorted(rows, key=lambda item: str(item.values.get("external_id") or "")):
        message = None
        try:
            values = _row_values(
                connection, name, row, report.source_system, excel_timezone
            )
            outcome = _store_row(connection, table, values)
        except ValidationError as error:
            outcome, message = "invalid", _validation_message(error)
        except ValueError as error:
            outcome, message = "invalid", str(error)
        except IntegrityError as error:
            constraint = getattr(
                getattr(error.orig, "diag", None), "constraint_name", None
            )
            outcome, message = (
                "invalid",
                f"Database constraint rejected the row: {constraint or 'unknown'}.",
            )
        if outcome == "conflict":
            message = "Existing external key has different data. The existing row was preserved."
        if message is not None:
            report.issues.append(RowIssue(name, row.number, outcome, message))
        counts[outcome] += 1


def import_sales_data(
    engine: Engine, path: Path, *, source_system: str, excel_timezone: str | None = None
) -> ImportReport:
    source_system = source_system.strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", source_system):
        raise ValueError(
            "source_system must contain 1–64 lowercase ASCII letters, digits or underscores."
        )

    try:
        zone = ZoneInfo(excel_timezone) if excel_timezone is not None else None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            "--excel-timezone must be a valid IANA time zone, for example Europe/Moscow."
        ) from exc

    tables = read_import_files(path)
    report = ImportReport(source_system)
    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="READ COMMITTED")
        with connection.begin():
            for name in ROW_MODELS:
                if name in tables:
                    _import_table(connection, name, tables[name], report, zone)
    table_order = {name: order for order, name in enumerate(ROW_MODELS)}
    report.issues.sort(key=lambda issue: (table_order[issue.table], issue.row))
    return report
