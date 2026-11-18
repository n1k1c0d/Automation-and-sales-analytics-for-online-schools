from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import Manager
from app.services.managers import create_manager, get_manager


def test_postgres_connection_uses_a_test_database(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        database = connection.exec_driver_sql("SELECT current_database()").scalar_one()
    assert database.startswith("eduflow_test_")


def test_manager_is_persisted_and_read_in_a_new_session(
    postgres_engine: Engine,
) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        manager = create_manager(writer, name="  Synthetic Manager  ", team=" Demo ")
        manager_id = manager.id
        assert isinstance(manager_id, UUID)

    with Session(postgres_engine) as reader:
        saved = get_manager(reader, manager_id)
        assert saved is not None
        assert saved.name == "Synthetic Manager"
        assert saved.team == "Demo"
        assert saved.created_at.utcoffset() is not None


def test_missing_manager_returns_none(postgres_engine: Engine) -> None:
    with Session(postgres_engine) as session:
        assert get_manager(session, uuid4()) is None


def test_database_error_rolls_back_manager_write(postgres_engine: Engine) -> None:
    with (
        pytest.raises(IntegrityError) as rejected,
        Session(postgres_engine) as writer,
        writer.begin(),
    ):
        manager_id = create_manager(writer, name="Synthetic Rollback", team="Demo").id
        writer.add(Manager(name=" ", team="Demo"))

    assert rejected.value.orig.diag.constraint_name == "ck_managers_name_nonblank"
    with Session(postgres_engine) as reader:
        assert get_manager(reader, manager_id) is None
