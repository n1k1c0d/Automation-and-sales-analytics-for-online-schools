import pytest
from sqlalchemy.orm import Session

from app.services.managers import create_manager


@pytest.mark.parametrize(
    ("name", "team", "field"),
    [
        ("", "Demo", "name"),
        (" \t\n", "Demo", "name"),
        ("Synthetic Manager", " \t", "team"),
    ],
)
def test_create_manager_rejects_blank_fields(name: str, team: str, field: str) -> None:
    with Session() as session:
        with pytest.raises(ValueError, match=rf"^Manager {field} cannot be blank\.$"):
            create_manager(session, name=name, team=team)

        assert not session.new
