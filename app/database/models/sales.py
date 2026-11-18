from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base


class Manager(Base):
    __tablename__ = "managers"
    __table_args__ = (
        CheckConstraint("name ~ '[^[:space:]]'", name="ck_managers_name_nonblank"),
        CheckConstraint("team ~ '[^[:space:]]'", name="ck_managers_team_nonblank"),
        CheckConstraint("isfinite(created_at)", name="ck_managers_created_at_finite"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text)
    team: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP")
    )

    leads: Mapped[list[Lead]] = relationship(
        back_populates="manager", passive_deletes="all"
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("source ~ '[^[:space:]]'", name="ck_leads_source_nonblank"),
        CheckConstraint("isfinite(created_at)", name="ck_leads_created_at_finite"),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'won', 'lost')", name="ck_leads_status"
        ),
        Index("ix_leads_created_at", "created_at"),
        Index("ix_leads_manager_id_created_at", "manager_id", "created_at"),
        Index("ix_leads_source_created_at", "source", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    manager_id: Mapped[UUID] = mapped_column(
        ForeignKey("managers.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text, server_default=text("'new'"))

    manager: Mapped[Manager] = relationship(back_populates="leads")
    lessons: Mapped[list[Lesson]] = relationship(
        back_populates="lead", passive_deletes="all"
    )
    bank_applications: Mapped[list[BankApplication]] = relationship(
        back_populates="lead", passive_deletes="all"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="lead", passive_deletes="all"
    )


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
            name="ck_lessons_status",
        ),
        CheckConstraint(
            "isfinite(scheduled_at) AND (held_at IS NULL OR isfinite(held_at))",
            name="ck_lessons_dates_finite",
        ),
        CheckConstraint(
            "(status = 'completed') = (held_at IS NOT NULL)",
            name="ck_lessons_held_at_status",
        ),
        Index("ix_lessons_lead_id_held_at", "lead_id", "held_at"),
        Index("ix_lessons_scheduled_at", "scheduled_at"),
        Index("ix_lessons_held_at", "held_at"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    lead_id: Mapped[UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    scheduled_at: Mapped[datetime]
    held_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="lessons")


class BankApplication(Base):
    __tablename__ = "bank_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted', 'approved', 'declined', 'cancelled')",
            name="ck_bank_applications_status",
        ),
        CheckConstraint(
            "submitted_at IS NULL OR isfinite(submitted_at)",
            name="ck_bank_applications_submitted_at_finite",
        ),
        CheckConstraint(
            "status = 'cancelled' OR submitted_at IS NOT NULL",
            name="ck_bank_applications_submitted_at_status",
        ),
        Index("ix_bank_applications_lead_id_submitted_at", "lead_id", "submitted_at"),
        Index("ix_bank_applications_submitted_at", "submitted_at"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    lead_id: Mapped[UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    submitted_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="bank_applications")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'paid', 'refunded')", name="ck_payments_status"
        ),
        CheckConstraint(
            "amount_rub > 0 AND amount_rub <> 'NaN'::numeric",
            name="ck_payments_amount_rub_positive",
        ),
        CheckConstraint(
            "paid_at IS NULL OR isfinite(paid_at)", name="ck_payments_paid_at_finite"
        ),
        CheckConstraint(
            "(status IN ('paid', 'refunded')) = (paid_at IS NOT NULL)",
            name="ck_payments_paid_at_status",
        ),
        Index("ix_payments_lead_id_paid_at", "lead_id", "paid_at"),
        Index("ix_payments_paid_at", "paid_at"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    lead_id: Mapped[UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    paid_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="payments")
