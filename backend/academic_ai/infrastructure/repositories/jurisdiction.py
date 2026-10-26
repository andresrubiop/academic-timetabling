"""Reading what a person is responsible for.

Expansion happens **here**, once, rather than at each authorisation check: a
dean assigned a faculty is returned holding every programme in it. A check that
walked the hierarchy would put a database query inside an authorisation
decision, and queries inside authorisation decisions get cached, and cached
authorisation decisions go stale exactly when somebody is removed from a post.
"""

from __future__ import annotations

import logging
import uuid
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import UserRole
from academic_ai.security.jurisdiction import Breadth, Jurisdiction, breadth_for

__all__ = ["load_jurisdiction", "load_jurisdictions"]

log = logging.getLogger("academic_ai.repositories.jurisdiction")


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def load_jurisdiction(
    session: Session, user_id: uuid.UUID | str, role: UserRole
) -> Jurisdiction:
    """What this user may act on, given their role and their assignments.

    An institution-wide role short-circuits: it is not made wider or narrower
    by an assignment, and reading the table for it would be a query on every
    request by the accounts that make the most of them.
    """
    breadth = breadth_for(role)
    if breadth is Breadth.INSTITUTION:
        return Jurisdiction(breadth=breadth)

    return load_jurisdictions(session, [user_id], role).get(
        str(user_id), Jurisdiction(breadth=breadth)
    )


def load_jurisdictions(
    session: Session, user_ids: Sequence[uuid.UUID | str], role: UserRole
) -> Mapping[str, Jurisdiction]:
    """The same, for several users at once.

    Approval routing asks "which of these coordinators is affected by this
    change", which is one question about many people. Asking it one user at a
    time is a query per approver on a page that lists them all.
    """
    from academic_ai.infrastructure.db.models import Program, UserScopeAssignment

    breadth = breadth_for(role)
    ids = [_as_uuid(u) for u in user_ids]
    if not ids:
        return {}
    if breadth is Breadth.INSTITUTION:
        return {str(u): Jurisdiction(breadth=breadth) for u in ids}

    faculties: dict[str, set[str]] = {str(u): set() for u in ids}
    programs: dict[str, set[str]] = {str(u): set() for u in ids}

    for user_id, faculty_id, program_id in session.execute(
        select(
            UserScopeAssignment.app_user_id,
            UserScopeAssignment.faculty_id,
            UserScopeAssignment.program_id,
        ).where(UserScopeAssignment.app_user_id.in_(ids))
    ):
        key = str(user_id)
        if faculty_id is not None:
            faculties[key].add(str(faculty_id))
        if program_id is not None:
            programs[key].add(str(program_id))

    # Faculty -> its programmes, in one query for every faculty mentioned.
    assigned_faculties = {f for s in faculties.values() for f in s}
    by_faculty: dict[str, list[str]] = {}
    if assigned_faculties:
        for program_id, faculty_id in session.execute(
            select(Program.id, Program.faculty_id).where(
                Program.faculty_id.in_([_as_uuid(f) for f in assigned_faculties])
            )
        ):
            by_faculty.setdefault(str(faculty_id), []).append(str(program_id))

    out: dict[str, Jurisdiction] = {}
    for key in faculties:
        expanded = set(programs[key])
        for faculty_id in faculties[key]:
            expanded.update(by_faculty.get(faculty_id, ()))
        out[key] = Jurisdiction(
            breadth=breadth,
            faculty_ids=frozenset(faculties[key]),
            program_ids=frozenset(expanded),
        )

    log.info(
        "jurisdictions loaded",
        extra={
            "users": len(out),
            "role": role.value,
            # Never the ids themselves: this line is written on every
            # authenticated request that needs a scope.
            "assigned": sum(1 for j in out.values() if not j.is_empty),
        },
    )
    return out
