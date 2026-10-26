"""Decision variables of the university timetabling problem.

Formal statement
================

Let ``P`` be one `AcademicPeriod`. Everything below is scoped to ``P``.

**Index sets**

* ``D``  — days of the week that have at least one `Timeslot` in ``P``.
* ``T``  — timeslots of ``P``. Each ``t ∈ T`` has a day ``day(t) ∈ D``, an
  in-day index ``i(t) ≥ 0`` dense from 0, a week-dense index ``g(t)``, and a
  flag ``schedulable(t)``.
* ``S``  — the `ClassSession` rows of ``P`` with ``requires_timeslot = True``.
  **This is the schedulable unit** — not Course, not Offering, not Section.
* ``R``  — the `Room` rows available to ``P``.
* ``K``  — teachers, ``E`` — students, ``G`` — co-teaching groups.

**Decision variables** — there are exactly two.

.. math::

    \\mathrm{start}(s) \\in T,\\quad s \\in S
    \\qquad
    \\mathrm{room}(s) \\in R,\\quad s \\in S,\\ \\mathrm{requires\\_room}(s)

``start(s)`` is the timeslot the session *begins* in. A session of length
``d(s) = duration\\_slots(s)`` then occupies the ``d(s)`` consecutive in-day
slots starting at ``i(start(s))``:

.. math::

    \\mathrm{occ}(s) = \\{\\, t \\in T :
        day(t) = day(\\mathrm{start}(s)) \\land
        i(\\mathrm{start}(s)) \\le i(t) < i(\\mathrm{start}(s)) + d(s) \\,\\}

Occupancy is expressed over the **in-day** index rather than the week-dense
one on purpose: arithmetic on ``i`` runs out of slots at the end of a day, so
a session can never straddle midnight. That property is a consequence of the
grid being dense (ADR-0006) — with holes for lunch instead of blocked rows,
``i + d - 1`` would silently jump the gap.

Sessions with ``requires_timeslot = False`` (asynchronous work) generate no
variable at all and do not block publication.

**Encoding.** CP-SAT wants both shapes, so SESSION 3 should carry both and
channel them: Booleans ``x[s,t] = 1`` iff ``start(s) = t`` with
``Σ_t x[s,t] = 1``, and an integer ``start(s)`` linked by
``start(s) = Σ_t g(t)·x[s,t]``. Availability, preferences and per-slot
occupancy are naturally Boolean; ordering, gaps and travel time are naturally
integer. Same for ``y[s,r]`` and ``room(s)``.

**Domain restriction, not constraint.** Every rule that depends only on a
single session — the session must fit inside its day, land on schedulable
slots, use a room of an allowed type on the right campus with the right
features and enough seats — is applied by *shrinking the variable's domain*
rather than by posting a constraint. This is not an optimisation detail: an
empty domain is a **provable, localised infeasibility** that names one
session, whereas the same fact discovered inside the solver returns a bare
INFEASIBLE for the whole institution. See
`diagnostics.DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from academic_ai.domain.scheduling.enums import DecisionVariable

__all__ = [
    "NON_DECISION_INPUTS",
    "DaySlot",
    "feasible_start_indices",
    "occupied_in_day_indices",
    "resolve_effective_section",
]


@dataclass(frozen=True, slots=True)
class DaySlot:
    """One slot of one day, as the variable machinery needs to see it."""

    in_day_index: int
    global_index: int
    is_schedulable: bool


def feasible_start_indices(
    day_slots: Sequence[DaySlot], duration_slots: int
) -> tuple[int, ...]:
    """In-day indices at which a session of ``duration_slots`` may start.

    A start is feasible only when every one of the ``duration_slots``
    consecutive slots from it exists in this day *and* is schedulable. The
    contiguity requirement is what makes a blocked lunch band actually block:
    a three-slot class cannot begin at 11:00 if 12:00 is unschedulable, even
    though 11:00, 13:00 and 14:00 are all fine individually.

    Returns in-day indices; the caller maps them to timeslot identities.
    """
    if duration_slots <= 0:
        raise ValueError(f"duration_slots must be positive, got {duration_slots}")

    by_index = {slot.in_day_index: slot for slot in day_slots}
    starts: list[int] = []
    for slot in sorted(day_slots, key=lambda s: s.in_day_index):
        window = range(slot.in_day_index, slot.in_day_index + duration_slots)
        if all(idx in by_index and by_index[idx].is_schedulable for idx in window):
            starts.append(slot.in_day_index)
    return tuple(starts)


def occupied_in_day_indices(start_in_day_index: int, duration_slots: int) -> range:
    """The in-day slots a session occupies. Half-open, like the DB range."""
    if duration_slots <= 0:
        raise ValueError(f"duration_slots must be positive, got {duration_slots}")
    return range(start_in_day_index, start_in_day_index + duration_slots)


def resolve_effective_section(
    section_id: str | None, offering_section_ids: Sequence[str]
) -> str | None:
    """Which section an enrollment effectively occupies.

    Sectioning is **not** a decision variable (ADR-0009): the registrar
    places students, and the optimizer reads that placement. But an
    enrollment with ``section_id IS NULL`` is still real demand, and at
    planning time most enrollments are unplaced — so a conflict model that
    only looked at placed enrollments would be nearly vacuous and would
    invert exactly the criticality ranking `test_student_criticality_inputs`
    exists to protect.

    There is one case where the placement is *deducible* rather than
    *decidable*: an offering with exactly one section. That is not a choice
    the optimizer is making, it is arithmetic, and it covers the majority of
    real offerings. Resolving it recovers most of the student-conflict model
    without touching the registrar's authority.

    Returns the section id, or ``None`` when the enrollment is unplaced and
    the offering has several sections — which the caller must report as
    `DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS`, never swallow.
    """
    if section_id is not None:
        return section_id
    if len(offering_section_ids) == 1:
        return offering_section_ids[0]
    return None


#: What the optimizer is **not** allowed to decide in SESSIONS 2–5, and the
#: decision that fixed it that way.
#:
#: This mapping is the honest boundary of the model. Every entry is something
#: a timetabling paper would happily make a variable and that this platform
#: deliberately does not, because doing so would move an authority away from
#: the human who currently holds it. Widening the model means deleting an
#: entry here, in a session that says so out loud.
NON_DECISION_INPUTS: Mapping[str, str] = MappingProxyType(
    {
        "enrollment.section_id": (
            "Sectioning is registrar-authoritative (ADR-0009). If it ever "
            "becomes a variable it must live in its own version-scoped entity "
            "and never write back to `enrollment`."
        ),
        "session_teacher": (
            "Teacher assignment is data, authoritative at session level "
            "(ADR-0008). CHANGE_INSTRUCTOR is a recommendation (SESSION 6) or "
            "a scenario perturbation (SESSION 11), never a solver variable in "
            "SESSIONS 2-5."
        ),
        "class_session.duration_slots": (
            "How long a class runs is an academic decision recorded on the "
            "course; the solver places it, it does not shorten it."
        ),
        "class_session.requires_timeslot": (
            "Whether a session is scheduled at all is a delivery-mode "
            "decision, not something to optimise away."
        ),
        "class_session.co_teaching_group_id": (
            "Merging offerings requires an APPROVED AcademicCompatibilityGroup "
            "(ADR-0004) — a human academic act. Scenarios propose merges; the "
            "solver never invents one."
        ),
        "section.capacity": (
            "Section size is a registrar decision and an input to room "
            "capacity, not an outcome of room availability."
        ),
        "timeslot grid": (
            "The grid is fixed per period (ADR-0006). The solver chooses "
            "within it and never reshapes it."
        ),
    }
)


#: The complete set of decision variables, for the completeness tests.
DECISION_VARIABLES: frozenset[DecisionVariable] = frozenset(DecisionVariable)
