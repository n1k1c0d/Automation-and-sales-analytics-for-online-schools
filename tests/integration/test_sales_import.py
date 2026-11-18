import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

from openpyxl import Workbook
from sqlalchemy import Engine, func, select

from app.database.models import Lead, Manager
from app.services.sales_import import ImportReport, import_sales_data
from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset

MANAGER_HEADER = ("external_id", "name", "team", "created_at")
LEAD_HEADER = ("external_id", "manager_external_id", "source", "created_at", "status")
MANAGER_ROW = ("0001", "Synthetic Manager", "Demo", "2026-06-01T08:00:00+00:00")
LEAD_ROW = ("0002", "0001", "webinar", "2026-06-02T08:00:00+00:00", "new")


def _write_csv(path: Path, header: tuple, rows: list[tuple]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def test_repeated_csv_import_preserves_all_keys(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    dataset = generate_dataset(lead_count=45, seed=71)
    export = tmp_path / "batch"
    write_csv_dataset(dataset, export)

    first = import_sales_data(postgres_engine, export, source_system="synthetic_71")
    assert not first.issues
    assert {table: counts["inserted"] for table, counts in first.tables.items()} == {
        table: len(rows) for table, rows in dataset.items()
    }
    with postgres_engine.connect() as conn:
        lead_ids = set(
            conn.scalars(select(Lead.id).where(Lead.source_system == "synthetic_71"))
        )

    repeated = import_sales_data(postgres_engine, export, source_system="synthetic_71")
    assert not repeated.issues
    for table, counts in repeated.tables.items():
        assert counts["duplicate"] == len(dataset[table]) and counts["inserted"] == 0
    with postgres_engine.connect() as conn:
        assert (
            set(
                conn.scalars(
                    select(Lead.id).where(Lead.source_system == "synthetic_71")
                )
            )
            == lead_ids
        )


def test_invalid_and_conflicting_rows_do_not_prevent_valid_csv_rows(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    _write_csv(tmp_path / "managers.csv", MANAGER_HEADER, [MANAGER_ROW])
    _write_csv(
        tmp_path / "leads.csv",
        LEAD_HEADER,
        [
            LEAD_ROW,
            ("", "0001", "webinar", LEAD_ROW[3], "new"),
            ("0003", "missing", "webinar", LEAD_ROW[3], "new"),
            ("0004", "0001", "webinar", LEAD_ROW[3], "unknown"),
            ("0005", "0001", "webinar", "2026-06-02T08:00:00", "new"),
        ],
    )
    _write_csv(
        tmp_path / "payments.csv",
        ("external_id", "lead_external_id", "amount_rub", "paid_at", "status"),
        [
            ("P1", "0002", "75.01", "2026-06-05T00:00:00Z", "paid"),
            ("P2", "0002", "75.011", "2026-06-05T00:00:00Z", "paid"),
            ("P3", "0002", "75.01", "2026-06-01T00:00:00Z", "paid"),
        ],
    )
    report = import_sales_data(postgres_engine, tmp_path, source_system="mixed_input")
    assert report.tables["leads"]["inserted"] == 1
    assert report.tables["payments"] == {
        "inserted": 1,
        "duplicate": 0,
        "conflict": 0,
        "invalid": 2,
    }
    assert [(issue.table, issue.row) for issue in report.issues] == [
        ("leads", 3),
        ("leads", 4),
        ("leads", 5),
        ("leads", 6),
        ("payments", 3),
        ("payments", 4),
    ]
    assert "before the lead" in report.issues[-1].message

    _write_csv(
        tmp_path / "leads.csv",
        LEAD_HEADER,
        [(LEAD_ROW[0], LEAD_ROW[1], "referral", LEAD_ROW[3], "new")],
    )
    report = import_sales_data(
        postgres_engine, tmp_path / "leads.csv", source_system="mixed_input"
    )
    assert report.tables["leads"]["conflict"] == 1
    with postgres_engine.connect() as conn:
        assert (
            conn.execute(
                select(Lead.source).where(
                    Lead.source_system == "mixed_input", Lead.external_id == "0002"
                )
            ).scalar_one()
            == "webinar"
        )


def test_xlsx_and_csv_reuse_the_same_text_identity(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    workbook = Workbook()
    managers = workbook.active
    managers.title = "managers"
    managers.append(MANAGER_HEADER)
    managers.append(MANAGER_ROW)
    managers.append((7, "Numeric identity", "Demo", MANAGER_ROW[3]))
    managers.append(("formula", "=1+1", "Demo", MANAGER_ROW[3]))
    leads = workbook.create_sheet("leads")
    leads.append(LEAD_HEADER)
    leads.append(LEAD_ROW)
    path = tmp_path / "sales.xlsx"
    workbook.save(path)
    workbook.close()

    report = import_sales_data(postgres_engine, path, source_system="excel_input")
    assert report.tables["managers"]["inserted"] == 1
    assert report.tables["leads"]["inserted"] == 1
    assert [issue.row for issue in report.issues] == [3, 4]
    assert "external_id" in report.issues[0].message
    assert "formulas" in report.issues[1].message

    _write_csv(tmp_path / "managers.csv", MANAGER_HEADER, [MANAGER_ROW])
    same = import_sales_data(
        postgres_engine, tmp_path / "managers.csv", source_system="excel_input"
    )
    other = import_sales_data(
        postgres_engine, tmp_path / "managers.csv", source_system="other_excel_input"
    )
    assert same.tables["managers"]["duplicate"] == 1
    assert other.tables["managers"]["inserted"] == 1


def test_native_excel_date_uses_an_explicit_time_zone(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "managers"
    sheet.append(MANAGER_HEADER)
    sheet.append((*MANAGER_ROW[:3], datetime(2026, 6, 1, 11)))
    path = tmp_path / "sales.xlsx"
    workbook.save(path)
    workbook.close()

    missing_zone = import_sales_data(
        postgres_engine, path, source_system="native_excel"
    )
    assert missing_zone.tables["managers"]["invalid"] == 1
    assert "--excel-timezone" in missing_zone.issues[0].message

    report = import_sales_data(
        postgres_engine,
        path,
        source_system="native_excel",
        excel_timezone="Europe/Moscow",
    )
    assert report.tables["managers"]["inserted"] == 1
    with postgres_engine.connect() as conn:
        assert conn.execute(
            select(Manager.created_at).where(Manager.source_system == "native_excel")
        ).scalar_one() == datetime(2026, 6, 1, 8, tzinfo=UTC)


def test_parallel_batches_with_reordered_rows_create_each_key_once(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    other = ("0003", "Second Manager", "Demo", MANAGER_ROW[3])
    _write_csv(first / "managers.csv", MANAGER_HEADER, [MANAGER_ROW, other])
    _write_csv(second / "managers.csv", MANAGER_HEADER, [other, MANAGER_ROW])
    barrier = Barrier(2)

    def run(path: Path) -> ImportReport:
        barrier.wait(timeout=10)
        return import_sales_data(
            postgres_engine, path, source_system="concurrent_input"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, path) for path in (first, second)]
        reports = [future.result(timeout=20) for future in futures]
    assert sum(report.tables["managers"]["inserted"] for report in reports) == 2
    assert sum(report.tables["managers"]["duplicate"] for report in reports) == 2
    assert not any(report.issues for report in reports)
    with postgres_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(Manager)
                .where(Manager.source_system == "concurrent_input")
            ).scalar_one()
            == 2
        )
