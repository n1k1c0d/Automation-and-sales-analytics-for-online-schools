from datetime import date
from pathlib import Path

from sqlalchemy import Engine

from app.analytics.sales import ReportPeriod, load_sales_report
from app.services.sales_import import import_sales_data
from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset

EXPECTED_STANDARD_KPIS = {
    "leads": 500,
    "scheduled_lessons": 456,
    "completed_lessons": 335,
    "bank_application_leads": 154,
    "paying_leads": 105,
    "paid_amount_rub": "8506500.00",
    "average_paid_check_rub": "81014.29",
    "cohort_leads": 300,
    "cohort_bank_application_leads": 162,
    "cohort_paying_leads": 118,
    "lesson_to_bank_application_pct": "54.00",
    "lesson_to_payment_pct": "39.33",
}


def test_standard_dataset_matches_all_reference_kpis(
    isolated_postgres_engine: Engine, tmp_path: Path
) -> None:
    path = tmp_path / "reference"
    write_csv_dataset(generate_dataset(), path)
    imported = import_sales_data(
        isolated_postgres_engine, path, source_system="synthetic_42"
    )
    assert not imported.issues
    assert sum(counts["inserted"] for counts in imported.tables.values()) == 1394

    period = ReportPeriod(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 8, 31),
        cutoff_date=date(2026, 10, 31),
    )
    with isolated_postgres_engine.connect() as connection:
        report = load_sales_report(connection, period)
    assert report.total.model_dump(mode="json") == EXPECTED_STANDARD_KPIS
