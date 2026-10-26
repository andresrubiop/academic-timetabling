"""What a person is responsible *for*, as opposed to what they may do.

`rbac.py` answers "may this kind of thing happen". This module answers "may it
happen **here**" — to this faculty, this programme. Conflating the two produces
a role that can edit every faculty because it could edit one, which is what
this platform did from SESSION 8 until now: `Principal` has carried empty
`program_ids` and `faculty_ids` fields the whole time, because nothing in the
schema said which programmes a coordinator coordinates.

Three scope concepts, deliberately distinct
===========================================

The word "scope" already appears twice in this codebase and both are about
*things*:

* `domain/governance/scope.py` — how far a **change** reaches, measured from
  its impact.
* `domain/scheduling/scope.py` — which rows a **constraint** applies to.

This one is about **people**, and it is the only one that answers a question
about authority. They share the `ScheduleScope` vocabulary
(INSTITUTION / FACULTY / PROGRAM) on purpose: a change assessed as reaching one
faculty and a dean responsible for one faculty should be describable in the
same words, or the routing code has to translate between two vocabularies that
mean the same thing.

The rule that makes this worth having
=====================================

**For a role whose authority comes from an assignment, no assignment means no
access — never universal access.** A `COORDINATOR` with no programmes
coordinates nothing. The opposite default is the one that appears to work: it
would let every existing account keep functioning after this shipped, and it
would mean the check never refused anything, which is indistinguishable from
not having written it.

That choice has a real cost, and it is the honest one: deploying this without
assigning scopes locks scoped roles out until somebody assigns them. The
refusal says so by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from academic_ai.domain.enums import UserRole

__all__ = [
    "ASSIGNABLE_BREADTHS",
    "Breadth",
    "Jurisdiction",
    "ROLE_BREADTH",
    "ScopeError",
    "breadth_for",
]


class Breadth(str, Enum):
    """How wide a role's authority reaches, before any assignment."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    #: Everything, by virtue of the role. Assignments are irrelevant.
    INSTITUTION = "INSTITUTION"
    #: The assigned faculties, and every programme inside them.
    FACULTY = "FACULTY"
    #: The assigned programmes, and nothing above them.
    PROGRAM = "PROGRAM"
    #: Only rows about this person. A teacher's own timetable, a student's own
    #: enrolments. Never authority over anybody else.
    OWN = "OWN"


#: Where a role's authority comes from.
#:
#: `DEAN` is faculty-wide because a dean's remit *is* a faculty; `DIRECTOR` and
#: `COORDINATOR` are programme-scoped for the same reason. `ADMIN` and
#: `SUPER_ADMIN` are institution-wide because the platform has to be
#: administrable before anybody has been assigned anything — otherwise the
#: first deployment has nobody who can create the assignments.
ROLE_BREADTH: Mapping[UserRole, Breadth] = {
    UserRole.SUPER_ADMIN: Breadth.INSTITUTION,
    UserRole.ADMIN: Breadth.INSTITUTION,
    UserRole.DEAN: Breadth.FACULTY,
    UserRole.DIRECTOR: Breadth.PROGRAM,
    UserRole.COORDINATOR: Breadth.PROGRAM,
    UserRole.TEACHER: Breadth.OWN,
    UserRole.STUDENT: Breadth.OWN,
}

#: The breadths that mean nothing without a row in `user_scope_assignment`.
ASSIGNABLE_BREADTHS = frozenset({Breadth.FACULTY, Breadth.PROGRAM})


class ScopeError(PermissionError):
    """Refused for *where*, not for *what*.

    Separate from `AuthorizationError` so the two can be told apart in a log:
    "this role cannot do that" and "this role cannot do that *here*" call for
    different fixes, and one of them is somebody forgetting to assign a scope.
    """


def breadth_for(role: UserRole) -> Breadth:
    """An unknown role gets `OWN`, the narrowest.

    A role added to the enum and forgotten here should lose access, not gain
    it. The alternative default is a new role that silently sees everything.
    """
    return ROLE_BREADTH.get(role, Breadth.OWN)


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    """The faculties and programmes one principal is responsible for.

    `program_ids` is **already expanded**: a dean assigned a faculty has every
    programme in it listed here. Expansion happens once, in the repository
    read, rather than at each check — a check that walks the hierarchy is a
    check that hits the database inside an authorisation decision, and those
    get cached, and then they get stale.
    """

    breadth: Breadth
    faculty_ids: frozenset[str] = frozenset()
    program_ids: frozenset[str] = frozenset()

    @property
    def is_institution_wide(self) -> bool:
        return self.breadth is Breadth.INSTITUTION

    @property
    def is_empty(self) -> bool:
        """Assigned nothing, where an assignment is what grants authority."""
        return (
            self.breadth in ASSIGNABLE_BREADTHS
            and not self.faculty_ids
            and not self.program_ids
        )

    def covers_program(self, program_id: str | None) -> bool:
        if self.is_institution_wide:
            return True
        if program_id is None:
            return False
        return program_id in self.program_ids

    def covers_faculty(self, faculty_id: str | None) -> bool:
        if self.is_institution_wide:
            return True
        if faculty_id is None:
            return False
        return faculty_id in self.faculty_ids

    def covers_any_program(self, program_ids: Iterable[str]) -> bool:
        """Whether any of these programmes is this principal's business.

        *Any*, not *all*, and the distinction is the whole design. A change
        that touches four programmes is the business of every coordinator who
        holds one of them — asking for all four would mean only an institution
        -wide role could ever be involved in a cross-programme change, which
        is precisely the change that most needs a programme's own coordinator
        to look at it.
        """
        if self.is_institution_wide:
            return True
        return any(p in self.program_ids for p in program_ids)


def jurisdiction_for(
    role: UserRole,
    *,
    faculty_ids: Iterable[str] = (),
    program_ids: Iterable[str] = (),
) -> Jurisdiction:
    """Build a jurisdiction from a role and its raw assignments.

    Assignments on an institution-wide or `OWN` role are kept rather than
    discarded: they are still true, and dropping them would make an audit of
    "who is assigned what" disagree with the table it was read from.
    """
    return Jurisdiction(
        breadth=breadth_for(role),
        faculty_ids=frozenset(faculty_ids),
        program_ids=frozenset(program_ids),
    )
