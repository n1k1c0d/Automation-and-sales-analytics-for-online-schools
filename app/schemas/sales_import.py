from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

SOURCE_SYSTEM_PATTERN = r"^[a-z0-9_]{1,64}$"
type SourceSystem = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=SOURCE_SYSTEM_PATTERN)
]


def _utc_timestamp(value: object, info: ValidationInfo) -> datetime:
    if isinstance(value, datetime):
        zone = (info.context or {}).get("excel_timezone")
        if value.utcoffset() is None:
            if zone is None:
                raise ValueError("Pass --excel-timezone for Excel date cells.")
            value = value.replace(tzinfo=zone, fold=0)
            if value.utcoffset() != value.replace(fold=1).utcoffset():
                raise ValueError(
                    "Ambiguous or nonexistent local time. Use text with a UTC offset."
                )
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "Use a valid ISO 8601 timestamp with a UTC offset."
            ) from exc
    else:
        raise ValueError(
            "Use an Excel date cell or an ISO 8601 timestamp with a UTC offset."
        )
    if timestamp.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset.")
    try:
        return timestamp.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Timestamp is outside the supported UTC range.") from exc


type Timestamp = Annotated[datetime, BeforeValidator(_utc_timestamp)]
type ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
type NonBlankText = Annotated[str, StringConstraints(min_length=1)]


class SalesRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    external_id: ExternalId


class ManagerRow(SalesRow):
    name: NonBlankText
    team: NonBlankText
    created_at: Timestamp


class LeadRow(SalesRow):
    manager_external_id: ExternalId
    source: NonBlankText
    created_at: Timestamp
    status: Literal["new", "in_progress", "won", "lost"]


class LeadEventRow(SalesRow):
    lead_external_id: ExternalId


class LessonRow(LeadEventRow):
    scheduled_at: Timestamp
    held_at: Timestamp | None = None
    status: Literal["scheduled", "completed", "cancelled", "no_show"]

    @model_validator(mode="after")
    def check_held_at(self) -> Self:
        if (self.status == "completed") != (self.held_at is not None):
            raise ValueError("held_at is required for completed lessons only.")
        return self


class BankApplicationRow(LeadEventRow):
    submitted_at: Timestamp | None = None
    status: Literal["submitted", "approved", "declined", "cancelled"]

    @model_validator(mode="after")
    def check_submitted_at(self) -> Self:
        if self.status != "cancelled" and self.submitted_at is None:
            raise ValueError(
                "submitted_at is required unless the application is cancelled."
            )
        return self


class PaymentRow(LeadEventRow):
    amount_rub: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False
    )
    paid_at: Timestamp | None = None
    status: Literal["pending", "paid", "refunded"]

    @model_validator(mode="after")
    def check_paid_at(self) -> Self:
        if (self.status != "pending") != (self.paid_at is not None):
            raise ValueError("paid_at is required for paid and refunded payments only.")
        return self


ROW_MODELS: dict[str, type[SalesRow]] = {
    "managers": ManagerRow,
    "leads": LeadRow,
    "lessons": LessonRow,
    "bank_applications": BankApplicationRow,
    "payments": PaymentRow,
}
