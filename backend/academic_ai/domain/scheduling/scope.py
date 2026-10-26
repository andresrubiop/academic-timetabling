"""Constraint scope and precedence.

`planning_constraint` carries seven scope columns, so several rows per
constraint code is the *normal* case: "seat 42 students in this one 40-seat
auditorium, approved by the Dean" is a real thing an institution says. Until
this module existed the model had nowhere to put that, so a scoped row
collapsed into an institution-wide downgrade — which is strictly worse than
rejecting it, because it silently relaxes a HARD constraint everywhere.

The performance consequence is as bad as the correctness one. Room domain
pruning (`domains.py`) is what keeps the room variable at a handful of
candidates per session instead of every room in the institution. An
institution-wide downgrade of `ROOM_CAPACITY`, `ROOM_TYPE_ALLOWED` or
`ROOM_CAMPUS_MATCH` switches that pruning off for *every* session and
multiplies the room-variable count by an order of magnitude. Overridability
and domain restriction are in direct tension, and scope is what keeps the
tension local.

Precedence is most-specific-wins, and a tie is an error rather than a
first-match: two equally specific overrides of the same constraint are a
contradiction in the institution's own configuration, and quietly picking one
would make the timetable depend on row order.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping

__all__ = [
    "SCOPE_PRECEDENCE",
    "ConstraintScope",
    "ScopeContext",
]

#: Least specific first. The index in this tuple *is* the specificity rank, so
#: adding a scope dimension means deciding where it sits in the order — which
#: is a decision someone should have to make deliberately.
SCOPE_PRECEDENCE: tuple[str, ...] = (
    "faculty_id",
    "program_id",
    "department_id",
    "room_id",
    "teacher_id",
    "course_id",
    "section_id",
)


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """What a constraint is being resolved *for*.

    Supplied by the adapter at the point of use: a room-capacity limit is
    resolved per (session, candidate room), a teacher limit per teacher.
    Fields left ``None`` simply never match a scope that names them.
    """

    faculty_id: str | None = None
    program_id: str | None = None
    department_id: str | None = None
    room_id: str | None = None
    teacher_id: str | None = None
    course_id: str | None = None
    section_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConstraintScope:
    """Which part of the institution a constraint instance applies to.

    All fields ``None`` means institution-wide, which is the common case and
    the reason the schema's unique key needs ``NULLS NOT DISTINCT``.
    """

    faculty_id: str | None = None
    program_id: str | None = None
    department_id: str | None = None
    room_id: str | None = None
    teacher_id: str | None = None
    course_id: str | None = None
    section_id: str | None = None

    @property
    def is_institution_wide(self) -> bool:
        return all(getattr(self, name) is None for name in SCOPE_PRECEDENCE)

    @property
    def specificity(self) -> int:
        """Higher wins. 0 is institution-wide.

        The rank is the position of the *most specific* dimension named, plus
        one — so a scope naming both a programme and a section is as specific
        as the section, which is the intuitive reading: the narrower claim is
        what makes it narrow.
        """
        rank = 0
        for index, name in enumerate(SCOPE_PRECEDENCE, start=1):
            if getattr(self, name) is not None:
                rank = index
        return rank

    @property
    def named_dimensions(self) -> tuple[str, ...]:
        return tuple(
            name for name in SCOPE_PRECEDENCE if getattr(self, name) is not None
        )

    def matches(self, context: ScopeContext) -> bool:
        """Whether this scope applies in ``context``.

        Every dimension the scope names must agree. A scope naming a dimension
        the context leaves unknown does **not** match: an unknown is not a
        wildcard, and treating it as one is how a section-scoped override
        leaks onto every section.
        """
        for name in self.named_dimensions:
            if getattr(context, name) != getattr(self, name):
                return False
        return True

    def as_key(self) -> tuple[tuple[str, str], ...]:
        """Hashable identity, for deduplication and for the instance key."""
        return tuple(
            (name, getattr(self, name))
            for name in SCOPE_PRECEDENCE
            if getattr(self, name) is not None
        )

    def describe(self) -> str:
        if self.is_institution_wide:
            return "institution-wide"
        return ", ".join(f"{n}={getattr(self, n)}" for n in self.named_dimensions)

    @classmethod
    def from_row(cls, row: Mapping[str, str | None]) -> ConstraintScope:
        """Build from a `planning_constraint` row's ``scope_*`` columns."""
        return cls(
            **{
                name: row.get(f"scope_{name}")
                for name in (f.name for f in fields(cls))
            }
        )
