"""Create the initial sales schema.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managers",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("team", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name="ck_managers_name_nonblank"),
        sa.CheckConstraint("team ~ '[^[:space:]]'", name="ck_managers_team_nonblank"),
        sa.CheckConstraint(
            "isfinite(created_at)", name="ck_managers_created_at_finite"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_managers")),
    )
    op.create_table(
        "leads",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("manager_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'new'"), nullable=False),
        sa.CheckConstraint("source ~ '[^[:space:]]'", name="ck_leads_source_nonblank"),
        sa.CheckConstraint(
            "status IN ('new', 'in_progress', 'won', 'lost')", name="ck_leads_status"
        ),
        sa.CheckConstraint("isfinite(created_at)", name="ck_leads_created_at_finite"),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["managers.id"],
            name=op.f("fk_leads_manager_id_managers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )
    op.create_index("ix_leads_created_at", "leads", ["created_at"], unique=False)
    op.create_index(
        "ix_leads_manager_id_created_at",
        "leads",
        ["manager_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_leads_source_created_at", "leads", ["source", "created_at"], unique=False
    )
    op.create_table(
        "bank_applications",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status = 'cancelled' OR submitted_at IS NOT NULL",
            name="ck_bank_applications_submitted_at_status",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'approved', 'declined', 'cancelled')",
            name="ck_bank_applications_status",
        ),
        sa.CheckConstraint(
            "submitted_at IS NULL OR isfinite(submitted_at)",
            name="ck_bank_applications_submitted_at_finite",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_bank_applications_lead_id_leads"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bank_applications")),
    )
    op.create_index(
        "ix_bank_applications_lead_id_submitted_at",
        "bank_applications",
        ["lead_id", "submitted_at"],
        unique=False,
    )
    op.create_index(
        "ix_bank_applications_submitted_at",
        "bank_applications",
        ["submitted_at"],
        unique=False,
    )
    op.create_table(
        "lessons",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(status = 'completed') = (held_at IS NOT NULL)",
            name="ck_lessons_held_at_status",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
            name="ck_lessons_status",
        ),
        sa.CheckConstraint(
            "isfinite(scheduled_at) AND (held_at IS NULL OR isfinite(held_at))",
            name="ck_lessons_dates_finite",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_lessons_lead_id_leads"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lessons")),
    )
    op.create_index("ix_lessons_held_at", "lessons", ["held_at"], unique=False)
    op.create_index(
        "ix_lessons_lead_id_held_at", "lessons", ["lead_id", "held_at"], unique=False
    )
    op.create_index(
        "ix_lessons_scheduled_at", "lessons", ["scheduled_at"], unique=False
    )
    op.create_table(
        "payments",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("amount_rub", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(status IN ('paid', 'refunded')) = (paid_at IS NOT NULL)",
            name="ck_payments_paid_at_status",
        ),
        sa.CheckConstraint(
            "amount_rub > 0 AND amount_rub <> 'NaN'::numeric",
            name="ck_payments_amount_rub_positive",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'refunded')", name="ck_payments_status"
        ),
        sa.CheckConstraint(
            "paid_at IS NULL OR isfinite(paid_at)", name="ck_payments_paid_at_finite"
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_payments_lead_id_leads"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(
        "ix_payments_lead_id_paid_at", "payments", ["lead_id", "paid_at"], unique=False
    )
    op.create_index("ix_payments_paid_at", "payments", ["paid_at"], unique=False)


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_table("lessons")
    op.drop_table("bank_applications")
    op.drop_table("leads")
    op.drop_table("managers")
