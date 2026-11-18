from uuid import UUID

from sqlalchemy.orm import Session

from app.database.models import Manager


def create_manager(session: Session, *, name: str, team: str) -> Manager:
    """Add a manager to the caller's transaction and load its generated ID."""
    name = name.strip()
    team = team.strip()
    if not name:
        raise ValueError("Manager name cannot be blank.")
    if not team:
        raise ValueError("Manager team cannot be blank.")

    manager = Manager(name=name, team=team)
    session.add(manager)
    session.flush()
    return manager


def get_manager(session: Session, manager_id: UUID) -> Manager | None:
    """Return the manager with the given primary key."""
    return session.get(Manager, manager_id)
