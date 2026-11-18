import argparse
import csv
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

type Cell = str | datetime | Decimal | None
type Row = dict[str, Cell]
type Dataset = dict[str, list[Row]]

DEFAULT_START_DATE = date(2026, 6, 1)
DEFAULT_OUTPUT_DIR = Path("data/synthetic/generated")
LEAD_WINDOW_DAYS = 90
MANAGER_COUNT = 8
LEAD_SOURCES = ("organic_search", "paid_search", "social", "referral", "webinar")
IMPORT_COLUMNS = ("source_system", "external_id")
TABLE_COLUMNS = {
    "managers": ("id", "name", "team", "created_at", *IMPORT_COLUMNS),
    "leads": (
        "id",
        "manager_id",
        "source",
        "created_at",
        "status",
        *IMPORT_COLUMNS,
        "manager_external_id",
    ),
    "lessons": (
        "id",
        "lead_id",
        "scheduled_at",
        "held_at",
        "status",
        *IMPORT_COLUMNS,
        "lead_external_id",
    ),
    "bank_applications": (
        "id",
        "lead_id",
        "submitted_at",
        "status",
        *IMPORT_COLUMNS,
        "lead_external_id",
    ),
    "payments": (
        "id",
        "lead_id",
        "amount_rub",
        "paid_at",
        "status",
        *IMPORT_COLUMNS,
        "lead_external_id",
    ),
}


def _identity(table: str, source_system: str, external_id: str) -> dict[str, str]:
    return {
        "id": str(
            uuid5(NAMESPACE_URL, f"urn:eduflow:{source_system}:{table}:{external_id}")
        ),
        "source_system": source_system,
        "external_id": external_id,
    }


def _event_identity(table: str, lead: dict[str, str], sequence: int) -> dict[str, str]:
    external_id = f"{lead['external_id']}-{sequence:02d}"
    return {
        **_identity(table, lead["source_system"], external_id),
        "lead_id": lead["id"],
        "lead_external_id": lead["external_id"],
    }


def _generate_lessons(
    rng: Random, lead: dict[str, str], created_at: datetime
) -> tuple[list[Row], datetime | None]:
    if rng.random() >= 0.80:
        return [], None

    attempts = 2 if rng.random() < 0.23 else 1
    lessons: list[Row] = []
    first_held_at = None
    scheduled_at = created_at
    for sequence in range(1, attempts + 1):
        scheduled_at += timedelta(days=rng.randint(1, 8), hours=rng.randint(0, 6))
        status = rng.choices(
            ("completed", "cancelled", "no_show", "scheduled"), weights=(70, 12, 10, 8)
        )[0]
        held_at = None
        if status == "completed":
            held_at = scheduled_at + timedelta(minutes=rng.randint(-10, 15))
            if first_held_at is None:
                first_held_at = held_at
        lessons.append(
            {
                **_event_identity("lessons", lead, sequence),
                "scheduled_at": scheduled_at,
                "held_at": held_at,
                "status": status,
            }
        )
    return lessons, first_held_at


def _generate_bank_applications(
    rng: Random, lead: dict[str, str], first_held_at: datetime
) -> tuple[list[Row], datetime | None]:
    if rng.random() >= 0.58:
        return [], None

    attempts = 2 if rng.random() < 0.20 else 1
    applications: list[Row] = []
    approval_reference = None
    event_at = first_held_at
    for sequence in range(1, attempts + 1):
        event_at += timedelta(days=rng.randint(1, 5), hours=rng.randint(0, 4))
        status = rng.choices(
            ("submitted", "approved", "declined", "cancelled"), weights=(10, 52, 26, 12)
        )[0]
        submitted_at = (
            None if status == "cancelled" and rng.random() < 0.6 else event_at
        )
        if status == "approved":
            approval_reference = event_at
        applications.append(
            {
                **_event_identity("bank_applications", lead, sequence),
                "submitted_at": submitted_at,
                "status": status,
            }
        )
    return applications, approval_reference


def _generate_payments(
    rng: Random,
    lead: dict[str, str],
    first_held_at: datetime,
    approval_reference: datetime | None,
) -> list[Row]:
    weights = (25, 10, 58, 7) if approval_reference else (55, 9, 30, 6)
    status = rng.choices(("none", "pending", "paid", "refunded"), weights=weights)[0]
    if status == "none":
        return []

    price_kopecks = rng.choice((4_900_000, 6_900_000, 8_900_000, 11_900_000))
    if status == "paid" and rng.random() < 0.25:
        amounts = (price_kopecks // 2, price_kopecks - price_kopecks // 2)
    else:
        amounts = (price_kopecks,)

    paid_at = None
    if status != "pending":
        paid_at = (approval_reference or first_held_at) + timedelta(
            days=rng.randint(1, 14)
        )

    payments: list[Row] = []
    for sequence, amount in enumerate(amounts, start=1):
        payments.append(
            {
                **_event_identity("payments", lead, sequence),
                "amount_rub": Decimal(f"{amount // 100}.{amount % 100:02d}"),
                "paid_at": paid_at,
                "status": status,
            }
        )
        if sequence < len(amounts) and paid_at is not None:
            paid_at += timedelta(days=rng.randint(3, 10))
    return payments


def _lead_status(rng: Random, lessons: list[Row], payments: list[Row]) -> str:
    payment_statuses = {payment["status"] for payment in payments}
    if "paid" in payment_statuses:
        return "won"
    if "refunded" in payment_statuses:
        return "lost"
    if "pending" in payment_statuses or any(
        row["status"] == "scheduled" for row in lessons
    ):
        return "in_progress"
    if not lessons:
        return rng.choices(("new", "in_progress", "lost"), weights=(30, 35, 35))[0]
    return rng.choices(("in_progress", "lost"), weights=(40, 60))[0]


def generate_dataset(
    *, lead_count: int = 500, seed: int = 42, start_date: date = DEFAULT_START_DATE
) -> Dataset:
    """Build repeatable sales records with valid links and no personal data."""
    if lead_count < 1:
        raise ValueError("--leads must be positive.")
    if seed < 0:
        raise ValueError("--seed must be non-negative.")

    rng = Random(seed)
    source_system = f"synthetic_{seed}"
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    dataset: Dataset = {table: [] for table in TABLE_COLUMNS}
    managers = [
        _identity("managers", source_system, f"M{number:02d}")
        for number in range(1, MANAGER_COUNT + 1)
    ]
    for index, identity in enumerate(managers):
        dataset["managers"].append(
            {
                **identity,
                "name": f"Demo Manager {index + 1:02d}",
                "team": ("Demo North", "Demo South")[index % 2],
                "created_at": start,
            }
        )

    for number in range(1, lead_count + 1):
        lead = _identity("leads", source_system, f"L{number:06d}")
        manager = rng.choice(managers)
        created_at = start + timedelta(
            days=rng.randrange(LEAD_WINDOW_DAYS), minutes=rng.randint(6 * 60, 18 * 60)
        )
        source = rng.choices(LEAD_SOURCES, weights=(25, 30, 20, 15, 10))[0]
        lessons, first_held_at = _generate_lessons(rng, lead, created_at)
        payments: list[Row] = []
        if first_held_at is not None:
            applications, approval_reference = _generate_bank_applications(
                rng, lead, first_held_at
            )
            payments = _generate_payments(rng, lead, first_held_at, approval_reference)
            dataset["bank_applications"].extend(applications)

        dataset["leads"].append(
            {
                **lead,
                "manager_id": manager["id"],
                "manager_external_id": manager["external_id"],
                "created_at": created_at,
                "source": source,
                "status": _lead_status(rng, lessons, payments),
            }
        )
        dataset["lessons"].extend(lessons)
        dataset["payments"].extend(payments)
    return dataset


def write_csv_dataset(dataset: Dataset, output_dir: Path) -> None:
    """Publish the complete dataset, or accept an identical existing export."""
    output_dir = output_dir.absolute()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".eduflow-", dir=output_dir.parent) as temporary:
        staged = Path(temporary) / "dataset"
        staged.mkdir()
        for table, columns in TABLE_COLUMNS.items():
            with (staged / f"{table}.csv").open(
                "w", encoding="utf-8", newline=""
            ) as file:
                writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                for row in dataset[table]:
                    values = {
                        field: value.isoformat(timespec="seconds")
                        if isinstance(value, datetime)
                        else value
                        for field, value in row.items()
                    }
                    writer.writerow(values)

        try:
            staged.rename(output_dir)
        except OSError:
            if not output_dir.exists():
                raise
            for candidate in staged.iterdir():
                existing = output_dir / candidate.name
                if (
                    not existing.is_file()
                    or existing.read_bytes() != candidate.read_bytes()
                ):
                    raise FileExistsError(
                        f"{output_dir} already contains different files. Choose another --output-dir."
                    ) from None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic EduFlow sales dataset."
    )
    parser.add_argument(
        "--leads", type=int, default=500, help="Number of leads (default: 500)."
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed (default: 42)."
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=DEFAULT_START_DATE,
        help="First day of the 90-day lead window (default: 2026-06-01).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR.as_posix()}).",
    )
    args = parser.parse_args()

    try:
        dataset = generate_dataset(
            lead_count=args.leads, seed=args.seed, start_date=args.start_date
        )
        write_csv_dataset(dataset, args.output_dir)
    except (ValueError, OverflowError, OSError) as error:
        parser.error(str(error))

    print(f"Dataset available at {args.output_dir.resolve()}")
    for table, rows in dataset.items():
        print(f"{table}.csv: {len(rows)} rows")


if __name__ == "__main__":
    main()
