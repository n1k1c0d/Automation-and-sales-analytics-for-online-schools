from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import BankApplication, Base, Lead, Lesson, Payment
from app.services.managers import create_manager

EVENT_AT = datetime(2026, 6, 4, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def lead_id(postgres_engine: Engine) -> UUID:
    with Session(postgres_engine) as session, session.begin():
        manager = create_manager(session, name="Synthetic Audit", team="Demo")
        lead = Lead(
            manager=manager,
            source="synthetic",
            created_at=EVENT_AT - timedelta(days=1),
        )
        session.add(lead)
        session.flush()
        return lead.id


def assert_rejected(engine: Engine, record: Base, constraint: str) -> None:
    with Session(engine) as session:
        session.add(record)
        with pytest.raises(IntegrityError) as error:
            session.flush()

    assert error.value.orig.diag.constraint_name == constraint


def test_lead_requires_an_existing_manager(postgres_engine: Engine) -> None:
    lead = Lead(manager_id=uuid4(), source="synthetic", created_at=EVENT_AT)
    assert_rejected(postgres_engine, lead, "fk_leads_manager_id_managers")


@pytest.mark.parametrize(
    ("status", "held_at", "constraint"),
    [
        ("unknown", None, "ck_lessons_status"),
        ("completed", None, "ck_lessons_held_at_status"),
        ("no_show", EVENT_AT, "ck_lessons_held_at_status"),
    ],
)
def test_lesson_rejects_invalid_status_or_date(
    postgres_engine: Engine,
    lead_id: UUID,
    status: str,
    held_at: datetime | None,
    constraint: str,
) -> None:
    lesson = Lesson(
        lead_id=lead_id, scheduled_at=EVENT_AT, held_at=held_at, status=status
    )
    assert_rejected(postgres_engine, lesson, constraint)


@pytest.mark.parametrize(
    ("status", "submitted_at", "constraint"),
    [
        ("unknown", EVENT_AT, "ck_bank_applications_status"),
        ("approved", None, "ck_bank_applications_submitted_at_status"),
    ],
)
def test_bank_application_rejects_invalid_status_or_date(
    postgres_engine: Engine,
    lead_id: UUID,
    status: str,
    submitted_at: datetime | None,
    constraint: str,
) -> None:
    application = BankApplication(
        lead_id=lead_id, submitted_at=submitted_at, status=status
    )
    assert_rejected(postgres_engine, application, constraint)


@pytest.mark.parametrize(
    ("status", "paid_at", "constraint"),
    [
        ("unknown", None, "ck_payments_status"),
        ("paid", None, "ck_payments_paid_at_status"),
        ("pending", EVENT_AT, "ck_payments_paid_at_status"),
        ("refunded", None, "ck_payments_paid_at_status"),
    ],
)
def test_payment_rejects_invalid_status_or_date(
    postgres_engine: Engine,
    lead_id: UUID,
    status: str,
    paid_at: datetime | None,
    constraint: str,
) -> None:
    payment = Payment(
        lead_id=lead_id, amount_rub=Decimal("10.00"), paid_at=paid_at, status=status
    )
    assert_rejected(postgres_engine, payment, constraint)


@pytest.mark.parametrize(
    "amount",
    [Decimal("0"), Decimal("-0.01"), Decimal("NaN")],
    ids=["zero", "negative", "nan"],
)
def test_payment_rejects_invalid_amount(
    postgres_engine: Engine, lead_id: UUID, amount: Decimal
) -> None:
    payment = Payment(lead_id=lead_id, amount_rub=amount, status="pending")
    assert_rejected(postgres_engine, payment, "ck_payments_amount_rub_positive")


def test_valid_events_are_persisted(postgres_engine: Engine, lead_id: UUID) -> None:
    held_at = EVENT_AT - timedelta(minutes=5)
    with Session(postgres_engine) as writer, writer.begin():
        writer.add_all(
            [
                Lesson(
                    lead_id=lead_id,
                    scheduled_at=EVENT_AT,
                    held_at=held_at,
                    status="completed",
                ),
                BankApplication(lead_id=lead_id, status="cancelled"),
                Payment(
                    lead_id=lead_id,
                    amount_rub=Decimal("0.01"),
                    paid_at=EVENT_AT,
                    status="refunded",
                ),
            ]
        )

    with Session(postgres_engine) as reader:
        lead = reader.get(Lead, lead_id)
        assert lead is not None
        assert [(lesson.status, lesson.held_at) for lesson in lead.lessons] == [
            ("completed", held_at)
        ]
        assert [
            (bank.status, bank.submitted_at) for bank in lead.bank_applications
        ] == [("cancelled", None)]
        assert [
            (payment.status, payment.paid_at, payment.amount_rub)
            for payment in lead.payments
        ] == [("refunded", EVENT_AT, Decimal("0.01"))]
