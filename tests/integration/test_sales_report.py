from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.orm import Session

from app.analytics.sales import ReportPeriod, load_sales_report
from app.database.models import BankApplication, Lead, Lesson, Manager, Payment

YEAR = 2030
MOSCOW = ZoneInfo("Europe/Moscow")


def _at(month: int, day: int, hour: int = 12) -> datetime:
    return datetime(YEAR, month, day, hour, tzinfo=MOSCOW)


def _period(start: int, end: int, cutoff: int) -> ReportPeriod:
    return ReportPeriod(
        start_date=date(YEAR, 6, start),
        end_date=date(YEAR, 6, end),
        cutoff_date=date(YEAR, 6, cutoff),
    )


@pytest.fixture
def report_data(postgres_engine: Engine) -> Iterator[tuple[Connection, UUID, UUID]]:
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(connection) as session:
                north = Manager(name="Synthetic North", team="Demo")
                south = Manager(name="Synthetic South", team="Demo")
                a = Lead(manager=north, source="webinar", created_at=_at(6, 1, 0))
                b = Lead(manager=south, source="referral", created_at=_at(6, 2))
                c = Lead(manager=north, source="referral", created_at=_at(5, 30))
                d = Lead(manager=south, source="webinar", created_at=_at(6, 8, 0))
                session.add_all([a, b, c, d])
                for lead, scheduled_at, held_at in (
                    (a, _at(6, 3), _at(6, 3)),
                    (a, _at(6, 5), _at(6, 5)),
                    (b, _at(6, 4), _at(6, 4)),
                    (c, _at(5, 31), _at(5, 31)),
                    (c, _at(6, 3, 23), _at(6, 4)),
                ):
                    session.add(
                        Lesson(
                            lead=lead,
                            scheduled_at=scheduled_at,
                            held_at=held_at,
                            status="completed",
                        )
                    )
                session.add(
                    Lesson(lead=b, scheduled_at=_at(6, 4, 14), status="cancelled")
                )
                session.add_all(
                    [
                        BankApplication(
                            lead=a, submitted_at=_at(6, 4), status="approved"
                        ),
                        BankApplication(
                            lead=a, submitted_at=_at(6, 5, 18), status="declined"
                        ),
                        BankApplication(
                            lead=b, submitted_at=_at(6, 4), status="submitted"
                        ),
                        BankApplication(lead=b, status="cancelled"),
                        BankApplication(
                            lead=c, submitted_at=_at(6, 5), status="cancelled"
                        ),
                        Payment(
                            lead=a,
                            amount_rub=Decimal("30000.00"),
                            paid_at=_at(6, 6),
                            status="paid",
                        ),
                        Payment(
                            lead=a,
                            amount_rub=Decimal("30000.00"),
                            paid_at=_at(6, 7),
                            status="paid",
                        ),
                        Payment(
                            lead=b,
                            amount_rub=Decimal("40000.00"),
                            paid_at=_at(6, 11, 0),
                            status="paid",
                        ),
                        Payment(
                            lead=b,
                            amount_rub=Decimal("2000.00"),
                            paid_at=_at(6, 6),
                            status="refunded",
                        ),
                        Payment(
                            lead=b, amount_rub=Decimal("1000.00"), status="pending"
                        ),
                    ]
                )
                session.flush()
                manager_ids = north.id, south.id
                session.commit()
            yield connection, *manager_ids
        finally:
            transaction.rollback()


def test_event_and_cohort_metrics_use_the_correct_populations(
    report_data: tuple[Connection, UUID, UUID],
) -> None:
    connection, _, _ = report_data
    connection.exec_driver_sql("SET LOCAL TIME ZONE 'America/New_York'")
    assert _at(6, 1, 0).astimezone(UTC).date() == date(YEAR, 5, 31)
    report = load_sales_report(connection, _period(1, 7, 10))
    metrics = report.total
    assert (metrics.leads, metrics.scheduled_lessons, metrics.completed_lessons) == (
        2,
        5,
        4,
    )
    assert (metrics.bank_application_leads, metrics.paying_leads) == (2, 1)
    assert (
        metrics.paid_amount_rub == metrics.average_paid_check_rub == Decimal("60000.00")
    )
    assert (
        metrics.cohort_leads,
        metrics.cohort_bank_application_leads,
        metrics.cohort_paying_leads,
    ) == (2, 1, 1)
    assert (
        metrics.lesson_to_bank_application_pct
        == metrics.lesson_to_payment_pct
        == Decimal("50.00")
    )

    assert [row.day for row in report.daily] == [
        date(YEAR, 6, day) for day in range(1, 8)
    ]
    assert report.daily[0].leads == 1
    assert report.daily[2].scheduled_lessons == 2
    assert report.daily[2].completed_lessons == 1
    assert report.daily[3].completed_lessons == 2
    assert report.daily[3].cohort_leads == 1
    assert report.daily[3].lesson_to_bank_application_pct == Decimal("0.00")
    assert report.daily[4].cohort_leads == 0
    assert sum(row.paying_leads for row in report.daily) == 2
    assert sum(row.bank_application_leads for row in report.daily) == 3
    assert sum(row.paid_amount_rub for row in report.daily) == metrics.paid_amount_rub


def test_cutoff_and_period_boundaries_are_independent(
    report_data: tuple[Connection, UUID, UUID],
) -> None:
    connection, _, _ = report_data
    current = load_sales_report(connection, _period(1, 7, 11)).total
    assert current.lesson_to_payment_pct == Decimal("100.00")
    assert current.paid_amount_rub == Decimal("60000.00")

    comparison = load_sales_report(connection, _period(8, 14, 14)).total
    assert (
        comparison.leads,
        comparison.completed_lessons,
        comparison.paying_leads,
    ) == (1, 0, 1)
    assert comparison.paid_amount_rub == Decimal("40000.00")
    assert comparison.lesson_to_payment_pct is None


def test_manager_and_source_breakdowns_and_filters(
    report_data: tuple[Connection, UUID, UUID],
) -> None:
    connection, north, south = report_data
    period = _period(1, 7, 10)
    report = load_sales_report(connection, period)
    managers = {row.manager_id: row for row in report.by_manager}
    assert set(managers) == {north, south}
    assert managers[north].manager_name == "Synthetic North"
    assert managers[north].completed_lessons == 3
    assert managers[south].lesson_to_payment_pct == Decimal("0.00")
    sources = {row.source: row for row in report.by_source}
    assert set(sources) == {"webinar", "referral"}
    assert sources["webinar"].paid_amount_rub == Decimal("60000.00")
    assert sources["referral"].paying_leads == 0

    combined = load_sales_report(
        connection, period, manager_id=north, source="referral"
    )
    assert (
        combined.total.leads,
        combined.total.completed_lessons,
        combined.total.cohort_leads,
    ) == (0, 1, 0)
    assert combined.total.lesson_to_payment_pct is None
    assert [row.manager_id for row in combined.by_manager] == [north]
    assert [row.source for row in combined.by_source] == ["referral"]


def test_empty_report_has_zero_counts_and_null_ratios(
    report_data: tuple[Connection, UUID, UUID],
) -> None:
    connection, _, _ = report_data
    report = load_sales_report(connection, _period(16, 17, 17))
    assert (
        report.total.leads
        == report.total.cohort_leads
        == report.total.paying_leads
        == 0
    )
    assert report.total.paid_amount_rub == Decimal("0.00")
    assert report.total.average_paid_check_rub is None
    assert report.total.lesson_to_bank_application_pct is None
    assert report.total.lesson_to_payment_pct is None
    assert not report.by_manager and not report.by_source
    assert [row.day for row in report.daily] == [date(YEAR, 6, 16), date(YEAR, 6, 17)]
    assert all(
        row.leads == 0 and row.average_paid_check_rub is None for row in report.daily
    )


def test_average_check_rounds_the_amount_per_paying_lead(
    report_data: tuple[Connection, UUID, UUID],
) -> None:
    connection, _, south = report_data
    lead_id = connection.execute(
        select(Lead.id).where(Lead.manager_id == south, Lead.source == "referral")
    ).scalar_one()
    connection.execute(
        insert(Payment).values(
            lead_id=lead_id,
            amount_rub=Decimal("88.77"),
            paid_at=_at(6, 7),
            status="paid",
        )
    )

    metrics = load_sales_report(connection, _period(1, 7, 10)).total
    assert metrics.paying_leads == 2
    assert metrics.paid_amount_rub == Decimal("60088.77")
    assert metrics.average_paid_check_rub == Decimal("30044.39")
