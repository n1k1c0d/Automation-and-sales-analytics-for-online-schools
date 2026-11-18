from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset


def test_default_dataset_has_unique_keys_and_consistent_events() -> None:
    dataset = generate_dataset()
    assert len(dataset["leads"]) == 500
    assert len(dataset["managers"]) == 8
    assert len({lead["source"] for lead in dataset["leads"]}) == 5

    ids = [row["id"] for rows in dataset.values() for row in rows]
    assert len(set(ids)) == len(ids)
    for rows in dataset.values():
        assert {row["source_system"] for row in rows} == {"synthetic_42"}
        assert len({row["external_id"] for row in rows}) == len(rows)

    managers = {
        manager["id"]: manager["external_id"] for manager in dataset["managers"]
    }
    leads = {lead["id"]: lead for lead in dataset["leads"]}
    for lead in leads.values():
        assert managers[lead["manager_id"]] == lead["manager_external_id"]

    completed_at: dict[object, datetime] = {}
    for lesson in dataset["lessons"]:
        lead = leads[lesson["lead_id"]]
        assert lead["external_id"] == lesson["lead_external_id"]
        scheduled_at, created_at = lesson["scheduled_at"], lead["created_at"]
        assert isinstance(scheduled_at, datetime) and isinstance(created_at, datetime)
        assert scheduled_at.tzinfo is UTC and scheduled_at > created_at
        held_at = lesson["held_at"]
        if lesson["status"] == "completed":
            assert isinstance(held_at, datetime) and held_at > created_at
            lead_id = lesson["lead_id"]
            completed_at[lead_id] = min(held_at, completed_at.get(lead_id, held_at))
        else:
            assert held_at is None

    assert {lesson["status"] for lesson in dataset["lessons"]} == {
        "scheduled",
        "completed",
        "cancelled",
        "no_show",
    }
    for application in dataset["bank_applications"]:
        lead_id = application["lead_id"]
        assert leads[lead_id]["external_id"] == application["lead_external_id"]
        submitted_at = application["submitted_at"]
        if submitted_at is None:
            assert application["status"] == "cancelled"
        else:
            assert (
                isinstance(submitted_at, datetime)
                and submitted_at > completed_at[lead_id]
            )

    assert {payment["status"] for payment in dataset["payments"]} == {
        "pending",
        "paid",
        "refunded",
    }
    for payment in dataset["payments"]:
        lead_id = payment["lead_id"]
        assert leads[lead_id]["external_id"] == payment["lead_external_id"]
        amount = payment["amount_rub"]
        assert (
            isinstance(amount, Decimal)
            and amount > 0
            and amount.as_tuple().exponent == -2
        )
        paid_at = payment["paid_at"]
        if payment["status"] == "pending":
            assert paid_at is None
        else:
            assert isinstance(paid_at, datetime) and paid_at > completed_at[lead_id]

    for table in ("lessons", "bank_applications", "payments"):
        rows = dataset[table]
        assert len({row["lead_id"] for row in rows}) < len(rows)


def test_export_is_repeatable_and_preserves_existing_different_files(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    write_csv_dataset(generate_dataset(lead_count=12), first)
    before = {path.name: path.read_bytes() for path in first.glob("*.csv")}

    write_csv_dataset(generate_dataset(lead_count=12), first)
    write_csv_dataset(generate_dataset(lead_count=12), second)
    assert {path.name: path.read_bytes() for path in second.glob("*.csv")} == before

    with pytest.raises(FileExistsError, match="already contains different files"):
        write_csv_dataset(generate_dataset(lead_count=12, seed=43), first)

    assert {path.name: path.read_bytes() for path in first.glob("*.csv")} == before
