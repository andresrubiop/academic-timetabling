"""Vocabulary of the mathematical scheduling model.

These enums are **not persisted** as PostgreSQL enum types, which is why they
live here rather than in `academic_ai.domain.enums`. They describe how the
model is *built*, not what the database stores. The one persisted enum the
model depends on — `ConstraintCategory` — stays in the domain vocabulary
module and is imported from there.

Convention, as in `domain/enums.py`: every member's value equals its name.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "WEIGHT_SCALE",
    "DecisionVariable",
    "DiagnosticSeverity",
    "Enforcement",
    "TermUnit",
    "VariableDependency",
    "ViolationMeasure",
    "WeightSource",
]

#: Human weights are stored as ``Numeric(8,3)`` / ``Numeric(6,3)`` — exactly
#: three decimals — but a CP-SAT objective must be integral. Multiplying by
#: 1000 is therefore lossless rather than a rounding policy, and the largest
#: representable weight (99 999 999) stays int64-safe against any realistic
#: violation count. SESSION 3 must use this constant and nothing else: a
#: second scaling factor elsewhere in the stack would make two runs
#: incomparable while both looked correct.
WEIGHT_SCALE = 1000


class _StrEnum(str, Enum):
    """String-valued enum whose value always equals its name."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DecisionVariable(_StrEnum):
    """The two things the optimizer actually decides.

    Deliberately short. Every additional decision variable enlarges the
    search space and, more importantly, moves an authority away from the
    people who currently hold it — see `variables.NON_DECISION_INPUTS` for
    what is *not* here and which ADR fixed it that way.
    """

    #: Which grid slot a `ClassSession` starts in.
    START_SLOT = "START_SLOT"
    #: Which `Room` a `ClassSession` occupies.
    ROOM = "ROOM"


class VariableDependency(_StrEnum):
    """Which decision variables a constraint reads.

    This is load-bearing rather than documentation. A constraint with
    dependency ``NONE`` is *invariant under the model*: no assignment the
    solver can produce will change whether it holds, so handing it to CP-SAT
    as a constraint is at best useless and at worst reports INFEASIBLE for
    something the solver could never have fixed. Such constraints must be
    checked as preconditions instead — see
    `catalog.TEACHER_MAX_WEEKLY_HOURS`.
    """

    NONE = "NONE"
    START = "START"
    ROOM = "ROOM"
    START_AND_ROOM = "START_AND_ROOM"

    @property
    def reads_start(self) -> bool:
        return self in (VariableDependency.START, VariableDependency.START_AND_ROOM)

    @property
    def reads_room(self) -> bool:
        return self in (VariableDependency.ROOM, VariableDependency.START_AND_ROOM)

    @property
    def is_invariant(self) -> bool:
        """True when no choice of decision variables can affect this constraint."""
        return self is VariableDependency.NONE


class Enforcement(_StrEnum):
    """Where a constraint is actually enforced."""

    #: Handed to CP-SAT: a hard constraint, or an objective term.
    SOLVER = "SOLVER"
    #: Checked before the model is built. Used for constraints that are
    #: invariant under the decision variables: the solver cannot repair them,
    #: so failing one means the *input data* is wrong, and saying "INFEASIBLE"
    #: would blame the timetable for a staffing decision.
    PRECONDITION = "PRECONDITION"


class TermUnit(_StrEnum):
    """What an objective term counts.

    Two terms may only be summed inside one tier if their units are
    commensurable. This is the rule that stops the platform inventing an
    exchange rate between "one student conflict" and "one unhappy teacher".
    """

    #: A (student, session-pair) collision. Countable, comparable.
    STUDENT_CONFLICT_INCIDENCE = "STUDENT_CONFLICT_INCIDENCE"
    #: One assignment differing from the base version.
    CHANGED_ASSIGNMENT = "CHANGED_ASSIGNMENT"
    #: A constraint violation scaled by a human-supplied weight.
    WEIGHTED_VIOLATION = "WEIGHTED_VIOLATION"


class ViolationMeasure(_StrEnum):
    """*How much* a violation is, dimensionally.

    `TermUnit` alone is too coarse to license summing. ``WEIGHTED_VIOLATION``
    covers seats over capacity, minutes over a daily limit, idle slots and
    wrong room types alike, so a tier of ``WEIGHTED_VIOLATION`` terms passes
    the commensurability check while asserting that one excess seat equals one
    excess minute — exactly the invented exchange rate `TermUnit` exists to
    forbid.

    ``INDICATOR`` is the normative default and the only measure that may be
    mixed freely inside a tier: counting *violated constraint instances* is
    dimensionless, so the human-supplied weight is the sole exchange rate and
    the platform invents nothing. A magnitude-bearing term carries a physical
    dimension and may only be summed with terms of the same dimension.

    Without this distinction two honest adapter authors reading one catalog
    entry produce different objectives — one penalising "the room is over
    capacity", the other "over capacity by 14 seats" — and no benchmark
    comparison in SESSION 4 means anything.
    """

    #: 1 per violated constraint instance. Dimensionless.
    INDICATOR = "INDICATOR"
    #: Seats demanded beyond what the room holds.
    MAGNITUDE_SEATS = "MAGNITUDE_SEATS"
    #: Minutes beyond a stated limit.
    MAGNITUDE_MINUTES = "MAGNITUDE_MINUTES"
    #: Grid slots — idle gaps, or slots beyond a limit.
    MAGNITUDE_SLOTS = "MAGNITUDE_SLOTS"
    #: Days beyond, or short of, a stated spread.
    MAGNITUDE_DAYS = "MAGNITUDE_DAYS"

    @property
    def is_dimensionless(self) -> bool:
        return self is ViolationMeasure.INDICATOR


class WeightSource(_StrEnum):
    """Where a term's numeric weight comes from.

    The playbook forbids arbitrary objective weights. Every term must
    therefore name a source, and ``PLATFORM`` is deliberately absent from
    this enum: there is no legitimate way for the platform to invent one.
    """

    #: Unweighted — a plain count. The only honest default.
    UNIFORM = "UNIFORM"
    #: `PlanningConstraint.weight`, set by whoever imposed the constraint.
    HUMAN_CONSTRAINT_WEIGHT = "HUMAN_CONSTRAINT_WEIGHT"
    #: `Preference.weight`, set by the teacher/programme/cohort that owns it.
    HUMAN_PREFERENCE_WEIGHT = "HUMAN_PREFERENCE_WEIGHT"
    #: Derived from student data by SESSION 5's StudentConflictCriticality
    #: (attempt number, available alternatives, progression risk). Until
    #: SESSION 5 exists this behaves as UNIFORM — it is a declared extension
    #: point, not a number this session invented.
    CRITICALITY_PROVIDER = "CRITICALITY_PROVIDER"


class DiagnosticSeverity(_StrEnum):
    """How much a diagnostic should be trusted to ruin someone's day."""

    #: The instance cannot be solved as stated. Reported instead of, not
    #: alongside, a solver status.
    BLOCKING = "BLOCKING"
    #: Solvable, but a constraint the reader probably believes is enforced
    #: is in fact vacuous or partial. The dangerous category.
    DEGRADED = "DEGRADED"
    #: Worth knowing; changes nothing about correctness.
    INFORMATIONAL = "INFORMATIONAL"
