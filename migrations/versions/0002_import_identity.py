"""Add external identities for repeatable imports.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("managers", "leads", "lessons", "bank_applications", "payments")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("source_system", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("external_id", sa.Text(), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_external_identity",
            table,
            "(source_system IS NULL AND external_id IS NULL) OR "
            "(source_system IS NOT NULL AND external_id IS NOT NULL "
            "AND source_system ~ '^[a-z0-9_]{1,64}$' "
            "AND char_length(external_id) BETWEEN 1 AND 255 "
            "AND external_id !~ '^[[:space:]]|[[:space:]]$')",
        )
        op.create_unique_constraint(
            f"uq_{table}_external_identity", table, ["source_system", "external_id"]
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_constraint(f"uq_{table}_external_identity", table, type_="unique")
        op.drop_constraint(f"ck_{table}_external_identity", table, type_="check")
        op.drop_column(table, "external_id")
        op.drop_column(table, "source_system")
