from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, MetaData, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    type_annotation_map = {datetime: DateTime(timezone=True)}


class ExternalIdentity:
    source_system: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)


def external_identity_constraints(
    table: str,
) -> tuple[CheckConstraint, UniqueConstraint]:
    return (
        CheckConstraint(
            "(source_system IS NULL AND external_id IS NULL) OR "
            "(source_system IS NOT NULL AND external_id IS NOT NULL "
            "AND source_system ~ '^[a-z0-9_]{1,64}$' "
            "AND char_length(external_id) BETWEEN 1 AND 255 "
            "AND external_id !~ '^[[:space:]]|[[:space:]]$')",
            name=f"ck_{table}_external_identity",
        ),
        UniqueConstraint(
            "source_system", "external_id", name=f"uq_{table}_external_identity"
        ),
    )
