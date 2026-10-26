"""The constraint catalog — every constraint the platform knows how to state.

This module is the executable form of SESSION 2's classification. It is the
single source of truth for the `constraint_type` table: the migration seeds
that table *from here*, and `tests/test_constraint_catalog.py` fails if the
two ever diverge, in the same spirit as ``alembic check``.

Reading the classification
==========================

``default_category`` answers "how bad is violating this?", and
``is_category_overridable`` answers "who is allowed to change that answer?".
The split matters more than it looks:

* **HARD, not overridable** — physical or safety reality. A person is not in
  two rooms at once; a chemistry lab without fume hoods is a hazard, not a
  timetable. No `PlanningConstraint`, no human, and certainly no AI may
  downgrade one, and from SESSION 2 on the database enforces that rather
  than trusting the application (see the composite-FK guard on
  ``planning_constraint``).
* **HARD, overridable** — the institution's default answer to a genuine
  judgement call. Overfilling a room by two seats, or publishing a timetable
  that strands a repeating student, are things universities really do decide
  to do. Downgrading one requires a named user and a written reason
  (``override_requires_authority``), and the constraint does not disappear:
  it moves into tier 1 of the objective, where the solver minimises it ahead
  of everything else.
* **MEDIUM** — institutional policy. Optimised, weighted by whoever imposed
  the policy.
* **SOFT** — stated wishes. Optimised last, weighted by whoever stated them.

``enforcement`` answers "where is it actually checked?", and it is not a
formality. ``TEACHER_MAX_WEEKLY_HOURS`` is a real institutional limit that no
choice of start slot or room can affect, because who teaches what is fixed
input in SESSIONS 2-5 (ADR-0008). Handing it to CP-SAT would let a staffing
mistake come back as "your timetable is infeasible". It is a precondition.

``reads`` is the machine-checkable version of that argument: a constraint
whose ``reads`` is ``NONE`` is invariant under the decision variables, and
`tests/test_constraint_catalog.py` asserts that every such constraint is a
precondition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.diagnostics import DiagnosticCode
from academic_ai.domain.scheduling.enums import (
    Enforcement,
    TermUnit,
    VariableDependency,
    ViolationMeasure,
    WeightSource,
)

__all__ = [
    "CONSTRAINT_CATALOG",
    "PLAYBOOK_COVERAGE",
    "Coverage",
    "ConstraintSpec",
    "downgradable_hard_codes",
    "objective_bearing_codes",
    "solver_hard_codes",
]

#: JSON Schema fragment reused by the limit-style constraints.
_POSITIVE_INT = {"type": "integer", "exclusiveMinimum": 0}


@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    """One catalog entry.

    The first seven fields are exactly the columns of `ConstraintType`; the
    rest is model knowledge that the database has no column for and that
    SESSION 3's builder needs in order to be *unable* to get it wrong.
    """

    code: str
    name: str
    description: str
    default_category: ConstraintCategory
    is_category_overridable: bool
    parameter_schema: Mapping[str, Any] | None
    parameter_schema_version: str

    #: Where this is checked. SOLVER constraints become CP-SAT constraints
    #: (HARD) or objective terms (MEDIUM/SOFT); PRECONDITION constraints are
    #: checked while building the instance.
    enforcement: Enforcement
    #: Which decision variables the constraint reads.
    reads: VariableDependency
    #: What the constraint quantifies over, in words. This is the sentence a
    #: reviewer checks the adapter against.
    quantified_over: str
    #: Why this category, in one sentence. The playbook forbids unjustified
    #: classification as firmly as it forbids unjustified weights.
    rationale: str
    #: Unit of the objective term this contributes when its effective
    #: category is MEDIUM or SOFT. ``None`` only for constraints that can
    #: never be an objective (HARD and not overridable).
    term_unit: TermUnit | None = None
    #: Where the term's weight comes from. Never invented by the platform.
    weight_source: WeightSource = WeightSource.UNIFORM
    #: The dimension of the violation. INDICATOR (a dimensionless count of
    #: violated instances) is the default and the only measure that may be
    #: mixed inside a tier; anything else may only be summed with its own
    #: dimension. Without this an adapter author is free to choose between
    #: "over capacity" and "over capacity by 14 seats" and both look correct.
    violation_measure: ViolationMeasure = ViolationMeasure.INDICATOR
    #: Diagnostics this constraint may cause the builder to emit.
    diagnostics: tuple[DiagnosticCode, ...] = ()
    #: Notes for the SESSION 3 adapter author about the traps in this one.
    adapter_notes: str = ""

    def __post_init__(self) -> None:
        if self.enforcement is Enforcement.SOLVER and self.reads.is_invariant:
            raise ValueError(
                f"{self.code}: reads=NONE means no assignment can change whether "
                "this holds; it must be a PRECONDITION, not a SOLVER constraint"
            )
        # A constraint contributes an objective term only if the solver sees
        # it at all *and* its category can be something other than HARD.
        can_become_objective = self.enforcement is Enforcement.SOLVER and (
            self.default_category is not ConstraintCategory.HARD
            or self.is_category_overridable
        )
        if can_become_objective and self.term_unit is None:
            raise ValueError(
                f"{self.code}: can be MEDIUM/SOFT but declares no term_unit, so "
                "downgrading it would silently delete it from the objective"
            )
        if not can_become_objective and self.term_unit is not None:
            raise ValueError(
                f"{self.code}: cannot contribute an objective term — it is "
                "either enforced outside the solver, or HARD and not overridable"
            )
        if self.term_unit is None and not self.violation_measure.is_dimensionless:
            raise ValueError(
                f"{self.code}: declares a magnitude but contributes no term, "
                "so the magnitude is measured by nothing"
            )


def _spec(**kwargs: Any) -> tuple[str, ConstraintSpec]:
    spec = ConstraintSpec(parameter_schema_version="1", **kwargs)
    return spec.code, spec


_HARD = ConstraintCategory.HARD
_MEDIUM = ConstraintCategory.MEDIUM
_SOFT = ConstraintCategory.SOFT
_SOLVER = Enforcement.SOLVER
_PRECONDITION = Enforcement.PRECONDITION
_START = VariableDependency.START
_ROOM = VariableDependency.ROOM
_BOTH = VariableDependency.START_AND_ROOM
_NONE = VariableDependency.NONE
_INCIDENCE = TermUnit.STUDENT_CONFLICT_INCIDENCE
_WEIGHTED = TermUnit.WEIGHTED_VIOLATION
_CHANGED = TermUnit.CHANGED_ASSIGNMENT


_SPECS: tuple[tuple[str, ConstraintSpec], ...] = (
    # =====================================================================
    # HARD — physical and safety reality. Never overridable.
    # =====================================================================
    _spec(
        code="SESSION_WITHIN_DAY_GRID",
        name="Session fits inside one day of the grid",
        description=(
            "A session occupies duration_slots consecutive in-day slots, all "
            "of which must exist in the same day."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each schedulable ClassSession",
        rationale=(
            "A class cannot continue past the end of the day. Enforced by "
            "domain restriction, so an unplaceable session is named "
            "individually instead of poisoning the whole model."
        ),
        diagnostics=(
            DiagnosticCode.SESSION_LONGER_THAN_ANY_DAY,
            DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START,
        ),
        adapter_notes=(
            "Use the in-day index, never the week-dense one: the week-dense "
            "index is contiguous across the midnight boundary and would let a "
            "3-slot Friday-evening class run into Saturday morning."
        ),
    ),
    _spec(
        code="SESSION_SLOTS_SCHEDULABLE",
        name="Session occupies only schedulable slots",
        description=(
            "Every slot a session occupies must have is_schedulable = true. "
            "Blocked bands (lunch, assembly) are dense rows, not holes."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each schedulable ClassSession × each occupied slot",
        rationale=(
            "An institution-wide blocked band is a decision already taken; "
            "the timetable does not get to reopen it."
        ),
        adapter_notes=(
            "It is not enough to check the *start* slot. A 3-slot class "
            "starting at 11:00 spans a blocked 12:00 even though 11:00 is "
            "schedulable — this is the whole reason the grid is dense."
        ),
    ),
    _spec(
        code="SESSION_ROOM_PRESENCE",
        name="Room assigned exactly when required",
        description=(
            "A session with requires_room must receive a room; one without "
            "must not receive one."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_PRECONDITION,
        reads=_NONE,
        quantified_over="each schedulable ClassSession",
        rationale=(
            "Mirrors the room_presence_matches_requirement CHECK. Without it "
            "'is this version complete enough to publish?' is undecidable."
        ),
    ),
    _spec(
        code="SECTION_SESSIONS_NO_OVERLAP",
        name="Sessions of one section do not overlap",
        description=(
            "Two ClassSessions of the same Section may not occupy overlapping "
            "slots. A section is one group of students; its lecture and its "
            "lab are attended by the same bodies."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each Section x each pair of its schedulable sessions",
        rationale=(
            "Physical, and independent of enrollment data. Without it a "
            "lecture with teacher T1 in room R1 and a lab with T2 in R2 may "
            "be placed in the same slot: TEACHER_NO_OVERLAP does not fire "
            "(different teachers), ROOM_NO_OVERLAP does not fire (different "
            "rooms), and the student path does not fire either whenever the "
            "enrollments are unplaced - which is the majority case at "
            "planning time. The result is a schedule that is OPTIMAL, "
            "publishable, and impossible to attend."
        ),
        diagnostics=(),
        adapter_notes=(
            "The model's only enrollment-independent backstop for student "
            "clashes, and the cheapest constraint in the catalog: "
            "sum over sections of C(n,2) pairs, where n is 1-3 in practice. "
            "Post it even when the section has no enrollments at all. It is "
            "NOT subsumed by the student constraints - those depend on "
            "`resolve_effective_section` succeeding, and it deliberately does "
            "not for an unplaced enrollment in a multi-section offering."
        ),
    ),
    _spec(
        code="TEACHER_NO_OVERLAP",
        name="A teacher is in one place at a time",
        description=(
            "Two sessions sharing a time-occupying teacher may not overlap."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each teacher × each pair of their sessions on one day",
        rationale="A person cannot be in two rooms at once.",
        adapter_notes=(
            "Membership comes from SessionTeacher filtered by "
            "TIME_OCCUPYING_TEACHER_ROLES (PRIMARY, ASSISTANT). A SUBSTITUTE "
            "must not enter this constraint. Read that frozenset from the "
            "model module rather than re-listing the roles here."
        ),
    ),
    _spec(
        code="ROOM_NO_OVERLAP",
        name="A room holds one class at a time",
        description=(
            "Two sessions in the same room may not overlap, unless they "
            "belong to the same co-teaching group, in which case they are one "
            "class being taught once."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each room × each pair of sessions placed in it on one day",
        rationale="Two classes in one room is not a timetable.",
        adapter_notes=(
            "The co-teaching exemption is not optional: ADR-0004 requires "
            "group members to share a room, so a naive pairwise NoOverlap "
            "makes every merged offering infeasible. Group by "
            "class_session.room_occupancy_key (COALESCE(co_teaching_group_id, "
            "id)) and post NoOverlap over occupancy keys, not sessions. The "
            "database enforces the same rule with a GiST exclusion on that "
            "key, so a solution violating it cannot even be persisted."
        ),
    ),
    _spec(
        code="ROOM_FEATURE_REQUIREMENT",
        name="Room provides the required capabilities",
        description=(
            "For every SessionRequiredFeature, the assigned room must provide "
            "that feature in at least the required quantity."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session × each required feature",
        rationale=(
            "A chemistry lab without fume hoods is a hazard, not a schedule. "
            "This is the one capability rule that is a safety matter, so it "
            "is not the institution's to relax."
        ),
        diagnostics=(
            DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM,
            DiagnosticCode.CO_TEACHING_FEATURE_AGGREGATION_AMBIGUOUS,
        ),
        adapter_notes=(
            "Aggregation across a co-teaching group depends on the feature: "
            "capabilities (a fume hood, a projector) are shared, so take the "
            "max; per-seat resources (workstations) are consumed, so sum. "
            "RoomFeature.is_additive decides, and defaults to false."
        ),
    ),
    _spec(
        code="ROOM_ACTIVE",
        name="Only active rooms may be used",
        description="A room with lifecycle status other than ACTIVE is unusable.",
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session requiring a room",
        rationale=(
            "A SUSPENDED room is under renovation and a RETIRED one may no "
            "longer exist. Enforced by domain restriction."
        ),
    ),
    _spec(
        code="CO_TEACHING_SAME_TIMESLOT",
        name="Co-taught sessions start together",
        description=(
            "All members of a SessionCoTeachingGroup share one start slot."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each co-teaching group",
        rationale=(
            "The group *is* one class taught once (ADR-0004). Members at "
            "different times are not merged, they are duplicated."
        ),
        diagnostics=(
            DiagnosticCode.CO_TEACHING_GROUP_NOT_APPROVED,
            DiagnosticCode.CO_TEACHING_GROUP_SPANS_PERIODS,
        ),
        adapter_notes=(
            "Members must also share duration_slots; if they differ the "
            "group is malformed, because 'same start' plus 'different length' "
            "is not one class. Reject it while building, do not truncate."
        ),
    ),
    _spec(
        code="CO_TEACHING_SAME_ROOM",
        name="Co-taught sessions share a room",
        description="All members of a SessionCoTeachingGroup share one room.",
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each co-teaching group",
        rationale="Same reason as CO_TEACHING_SAME_TIMESLOT: it is one class.",
        adapter_notes=(
            "Seat demand for the shared room is the *sum* over members "
            "(bodies add up), while shared capabilities are the max — see "
            "ROOM_CAPACITY and ROOM_FEATURE_REQUIREMENT."
        ),
    ),
    _spec(
        code="TEACHER_CAMPUS_TRAVEL",
        name="A teacher can physically reach their next class",
        description=(
            "Consecutive sessions of one teacher on one day, in rooms on "
            "different campuses, must be separated by at least the declared "
            "travel time."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each teacher × each ordered pair of their sessions on one day",
        rationale="Teleportation is not in the contract.",
        diagnostics=(DiagnosticCode.TRAVEL_TIME_UNDECLARED,),
        adapter_notes=(
            "TravelTime is directional (A->B need not equal B->A), so the "
            "origin is whichever session ends first — which depends on the "
            "start variables and cannot be decided while building. Post both "
            "orientations, each guarded by the ordering literal. A missing "
            "TravelTime row makes the constraint vacuous rather than "
            "infinite: emit TRAVEL_TIME_UNDECLARED, never assume zero "
            "silently."
        ),
    ),
    _spec(
        code="TEACHER_AVAILABILITY",
        name="Teacher declared unavailability is respected",
        description=(
            "A session may not occupy a slot where one of its time-occupying "
            "teachers has an UNAVAILABLE Availability row."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each session × each of its teachers × each occupied slot",
        rationale=(
            "Availability is a statement about capability, not preference "
            "(that is what Preference is for). It is not overridable because "
            "the honest way to relax it is to change the availability data "
            "for a named teacher and slot, which is auditable — demoting the "
            "constraint type would silently ignore everyone's."
        ),
        diagnostics=(DiagnosticCode.TEACHER_AVAILABILITY_UNDECLARED,),
        adapter_notes=(
            "Deny-list: absence of a row means available. A teacher with no "
            "rows at all is therefore indistinguishable from one available "
            "every hour of the week — emit TEACHER_AVAILABILITY_UNDECLARED, "
            "because default-available reliably produces schedules teachers "
            "reject. Check every occupied slot, not just the start."
        ),
    ),
    _spec(
        code="ROOM_AVAILABILITY",
        name="Room declared unavailability is respected",
        description=(
            "A session may not occupy a slot where its room has an "
            "UNAVAILABLE Availability row."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each session × each candidate room × each occupied slot",
        rationale="A room booked for an exam or closed for repair is not free.",
        diagnostics=(DiagnosticCode.ROOM_AVAILABILITY_UNDECLARED,),
    ),
    _spec(
        code="ASSIGNMENT_LOCKED",
        name="Locked assignments do not move",
        description=(
            "An assignment carried over from the base version with "
            "is_locked = true keeps its slot and room."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each locked assignment in the base version",
        rationale=(
            "A lock is an explicit human act inside one version. The way to "
            "relax it is to unlock it, which is visible; demoting the "
            "constraint type would quietly unlock everything at once."
        ),
        diagnostics=(DiagnosticCode.LOCKED_ASSIGNMENTS_CONFLICT,),
        adapter_notes=(
            "Fix by domain restriction. Two locked assignments that already "
            "conflict make the instance infeasible before search starts — "
            "detect it and report LOCKED_ASSIGNMENTS_CONFLICT naming both, "
            "rather than returning a bare INFEASIBLE."
        ),
    ),
    _spec(
        code="SESSION_PINNED",
        name="Durably pinned session",
        description=(
            "A session pinned by institutional decision keeps the slot and "
            "room named in the constraint parameters, across versions."
        ),
        default_category=_HARD,
        is_category_overridable=False,
        parameter_schema={
            "type": "object",
            "properties": {
                "timeslot_id": {"type": "string", "format": "uuid"},
                "room_id": {"type": "string", "format": "uuid"},
            },
            "anyOf": [
                {"required": ["timeslot_id"]},
                {"required": ["room_id"]},
            ],
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each active pin constraint",
        rationale=(
            "'The Dean froze this class for the year' is a durable "
            "institutional fact and belongs in PlanningConstraint. It is "
            "deliberately *not* ScheduleAssignment.is_locked copied forward: "
            "is_locked is a version-scoped optimizer input, and propagating "
            "it between versions would make an institutional decision "
            "indistinguishable from a planner's scratch note."
        ),
        adapter_notes=(
            "Scope the constraint with scope_section_id (plus parameters "
            "naming the session sequence) or scope_course_id. A pin naming a "
            "slot outside the period's grid is a data error, not an "
            "infeasibility."
        ),
    ),
    # =====================================================================
    # HARD — the institution's default answer to a real judgement call.
    # Overridable by a named human with a written reason; when downgraded,
    # lands in objective tier 1.
    # =====================================================================
    _spec(
        code="ROOM_CAPACITY",
        violation_measure=ViolationMeasure.MAGNITUDE_SEATS,
        name="Room is big enough",
        description=(
            "Assigned room capacity must be at least the session's seat "
            "demand: the larger of the section's size and its declared "
            "minimum, summed across a co-teaching group."
        ),
        default_category=_HARD,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session (or co-teaching group) requiring a room",
        rationale=(
            "Fire codes make this physical, but universities really do seat "
            "42 students in a 40-seat room, and pretending otherwise pushes "
            "planners into falsifying room capacities — which is worse, "
            "because the falsified number is then used forever. Overridable, "
            "with a named approver."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        diagnostics=(
            DiagnosticCode.ROOM_CAPACITY_UNSATISFIABLE,
            DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM,
        ),
        adapter_notes=(
            "seat_demand(session) = max(section.capacity, resolved active "
            "enrollments, min_room_capacity or 0). Resolved enrollments "
            "include ones the registrar has not placed when the offering has "
            "exactly one section — see variables.resolve_effective_section. "
            "For a group, sum the members: bodies add up."
        ),
    ),
    _spec(
        code="ROOM_CAMPUS_MATCH",
        name="Room is on the offering's campus",
        description=(
            "A session's room must be on the campus its CourseOffering names."
        ),
        default_category=_HARD,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session requiring a room",
        rationale=(
            "Students enrolled in a campus-scoped offering expect to be on "
            "that campus. Overridable because some institutions deliberately "
            "run split-campus offerings."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="ROOM_TYPE_ALLOWED",
        name="Room type suits the session",
        description=(
            "The assigned room's type must be in the allowed set for the "
            "session's type and delivery mode."
        ),
        default_category=_HARD,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {
                "allowed_room_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                }
            },
            "required": ["allowed_room_types"],
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session requiring a room",
        rationale=(
            "A LAB in a CLASSROOM is usually wrong and occasionally fine, "
            "which is exactly what 'HARD but overridable' is for. An ONLINE "
            "session that still requires a room needs a VIRTUAL one."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="STUDENT_NO_OVERLAP_UNRESOLVABLE",
        name="A student with no alternative is never double-booked",
        description=(
            "Two sessions may not overlap when some student holds active "
            "mandatory enrollments in both and neither can be moved to "
            "another section, so no action by anyone resolves the clash."
        ),
        default_category=_HARD,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each student × each pair of their unresolvable mandatory sessions",
        rationale=(
            "This is the population the platform exists to protect: a student "
            "repeating a mandatory course with one section has no move to "
            "make, so the clash is not an inconvenience, it is a year lost. "
            "Kept HARD rather than heavily weighted so that the failure mode "
            "is a visible INFEASIBLE demanding a human decision, not a "
            "quietly optimal schedule that sacrifices them. Overridable "
            "because an institution may consciously accept the harm — and "
            "then the solver minimises it above everything else."
        ),
        term_unit=_INCIDENCE,
        weight_source=WeightSource.CRITICALITY_PROVIDER,
        diagnostics=(DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS,),
        adapter_notes=(
            "'No alternative' is computed from resolved enrollments and the "
            "count of OPEN sections with remaining space — not from cohorts, "
            "which are never a conflict authority. Enrollment status must be "
            "one of the active ones (REQUESTED, ENROLLED, WAITLISTED)."
        ),
    ),
    _spec(
        code="STUDENT_CAMPUS_TRAVEL",
        name="A student can physically reach their next class",
        description=(
            "Consecutive sessions of one student on one day, on different "
            "campuses, must be separated by at least the declared travel time."
        ),
        default_category=_HARD,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each student × each ordered pair of their sessions on one day",
        rationale=(
            "Physically identical to the teacher case, but institutions do "
            "publish timetables that leave students to arrive late, so the "
            "decision to accept that is theirs to record."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        diagnostics=(DiagnosticCode.TRAVEL_TIME_UNDECLARED,),
    ),
    # =====================================================================
    # MEDIUM — institutional policy.
    # =====================================================================
    _spec(
        code="STUDENT_NO_OVERLAP_MANDATORY",
        name="Mandatory student conflicts are minimised",
        description=(
            "Two sessions overlapping while a student holds active mandatory "
            "enrollments in both, where the student could in principle move "
            "to another section."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each student × each pair of their mandatory sessions",
        rationale=(
            "Not HARD: at realistic scale, forbidding every mandatory clash "
            "makes almost every instance INFEASIBLE, and 'no solution exists' "
            "is the least useful true statement a planner can be handed. "
            "Minimised in the tier immediately below unresolvable harm."
        ),
        term_unit=_INCIDENCE,
        weight_source=WeightSource.CRITICALITY_PROVIDER,
        adapter_notes=(
            "Count incidences — (student, session pair) — not pairs. One "
            "clash affecting sixty students is sixty times worse than one "
            "affecting one, and counting pairs would say they are equal."
        ),
    ),
    _spec(
        code="STUDENT_NO_OVERLAP_ELECTIVE",
        name="Elective student conflicts are minimised",
        description=(
            "As above, where at least one of the two enrollments is not "
            "mandatory."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each student × each pair of their sessions, one non-mandatory",
        rationale=(
            "A student can drop an elective; the clash costs them choice "
            "rather than progression."
        ),
        term_unit=_INCIDENCE,
        weight_source=WeightSource.CRITICALITY_PROVIDER,
    ),
    _spec(
        code="TEACHER_MAX_DAILY_HOURS",
        violation_measure=ViolationMeasure.MAGNITUDE_MINUTES,
        name="Teacher daily teaching limit",
        description=(
            "Total teaching minutes for a teacher on one day may not exceed "
            "their max_daily_hours."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each teacher × each day",
        rationale=(
            "Contractual or protective policy, and genuinely a function of "
            "the schedule: which day a session lands on is a decision."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        adapter_notes=(
            "A NULL max_daily_hours means no limit recorded — unconstrained, "
            "not zero."
        ),
    ),
    _spec(
        code="TEACHER_MAX_CONSECUTIVE_SLOTS",
        violation_measure=ViolationMeasure.MAGNITUDE_SLOTS,
        name="Teacher consecutive-teaching limit",
        description=(
            "A teacher may not teach more than N consecutive slots without a "
            "break."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {"max_consecutive_slots": _POSITIVE_INT},
            "required": ["max_consecutive_slots"],
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each teacher × each day × each window of N+1 slots",
        rationale="Protective policy with no natural home on the Teacher row.",
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="TEACHER_MAX_TEACHING_DAYS",
        violation_measure=ViolationMeasure.MAGNITUDE_DAYS,
        name="Teacher weekly distribution",
        description=(
            "A teacher's sessions should occupy at most N distinct days."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {"max_teaching_days": _POSITIVE_INT},
            "required": ["max_teaching_days"],
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each teacher",
        rationale=(
            "Concentrating a part-time teacher's load into fewer days is a "
            "common institutional commitment, especially for HOURLY and "
            "VISITING contracts."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="STUDENT_MAX_DAILY_HOURS",
        violation_measure=ViolationMeasure.MAGNITUDE_MINUTES,
        name="Student daily load limit",
        description=(
            "Total class minutes for a student on one day may not exceed the "
            "configured limit."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {"max_daily_minutes": _POSITIVE_INT},
            "required": ["max_daily_minutes"],
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each student × each day",
        rationale="Protective policy, scoped by programme in practice.",
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="SECTION_DAY_SPREAD",
        violation_measure=ViolationMeasure.MAGNITUDE_DAYS,
        name="A section's sessions are spread across the week",
        description=(
            "The sessions of one section should fall on distinct days, and at "
            "most max_sessions_per_day of them may share a day."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {
                "max_sessions_per_day": _POSITIVE_INT,
                "min_distinct_days": _POSITIVE_INT,
            },
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each section × each day",
        rationale=(
            "Four hours of one subject on Monday and nothing after is "
            "pedagogically poor but not impossible, and some intensive "
            "formats want exactly that. Policy, not physics."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        adapter_notes=(
            "Co-taught members share a start, so applying this per section "
            "already covers the group; do not double-count it per group."
        ),
    ),
    _spec(
        code="SCHEDULE_STABILITY",
        name="Minimal disruption to the base version",
        description=(
            "The number of assignments whose slot or room differs from the "
            "base schedule version."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each session present in the base version",
        rationale=(
            "Once a version is published, students and teachers have planned "
            "around it, so every change has a cost outside the model. Only "
            "meaningful when there is a base version — a greenfield build has "
            "nothing to be stable relative to, and the objective profile says "
            "so explicitly rather than letting the term evaluate to zero and "
            "look satisfied."
        ),
        term_unit=_CHANGED,
        weight_source=WeightSource.UNIFORM,
    ),
    _spec(
        code="TEACHER_MAX_WEEKLY_HOURS",
        name="Teacher weekly workload limit",
        description=(
            "Total teaching minutes assigned to a teacher across the week may "
            "not exceed their max_weekly_hours."
        ),
        default_category=_MEDIUM,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_PRECONDITION,
        reads=_NONE,
        quantified_over="each teacher",
        rationale=(
            "A real institutional limit that the timetable cannot break or "
            "repair. Because SessionTeacher is fixed input in SESSIONS 2-5 "
            "(ADR-0008) and durations are fixed, a teacher's weekly total is "
            "determined before the solver starts: it is the same number for "
            "every feasible schedule. Posting it to CP-SAT would return "
            "INFEASIBLE for an over-allocated teacher and blame the "
            "timetable for a staffing decision. Checked as a precondition, "
            "reported as TEACHER_WEEKLY_LOAD_EXCEEDED naming the teacher. It "
            "becomes a genuine solver constraint on the day teacher "
            "assignment becomes a decision variable, and not before."
        ),
        adapter_notes=(
            "Downgrading this one does not move it into the objective — "
            "nothing in the objective could act on it. It downgrades the "
            "*precondition* instead: MEDIUM blocks the run, SOFT reports "
            "TEACHER_WEEKLY_LOAD_EXCEEDED and continues."
        ),
    ),
    # =====================================================================
    # SOFT — stated wishes and schedule quality.
    # =====================================================================
    _spec(
        code="TEACHER_GAP_MINIMISATION",
        violation_measure=ViolationMeasure.MAGNITUDE_SLOTS,
        name="Teacher idle gaps are minimised",
        description=(
            "Idle slots between a teacher's first and last session on a day."
        ),
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each teacher × each day",
        rationale="Quality of working life; never a reason to fail a schedule.",
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        adapter_notes=(
            "Count *idle* slots between first and last, not the span: a "
            "teacher with one 4-slot class has a 4-slot span and no gap."
        ),
    ),
    _spec(
        code="STUDENT_GAP_MINIMISATION",
        violation_measure=ViolationMeasure.MAGNITUDE_SLOTS,
        name="Student idle gaps are minimised",
        description=(
            "Idle slots between a student's first and last session on a day."
        ),
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_START,
        quantified_over="each student × each day",
        rationale="Quality of student life; never a reason to fail a schedule.",
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
        adapter_notes=(
            "Cost is per student, so this scales with enrollment and is the "
            "most expensive soft term in the model. SESSION 4 should expect "
            "to aggregate it by cohort for large instances — as a "
            "*performance* approximation of an objective, which is legitimate, "
            "unlike using cohorts for conflicts, which is not."
        ),
    ),
    _spec(
        code="ROOM_CHANGE_MINIMISATION",
        name="Room changes are minimised",
        description=(
            "Consecutive sessions of the same section, or of the same "
            "student, placed in different rooms or buildings."
        ),
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema={
            "type": "object",
            "properties": {
                "penalise_building_change": {"type": "boolean"},
                "penalise_room_change": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each section (or student) × each consecutive session pair",
        rationale=(
            "Moving rooms between back-to-back classes wastes minutes. "
            "Distinct from campus travel, which is HARD because it is "
            "physically impossible rather than merely annoying."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="ROOM_CAPACITY_FIT",
        violation_measure=ViolationMeasure.MAGNITUDE_SEATS,
        name="Rooms are not wastefully oversized",
        description=(
            "Unused seats when a session is placed in a room much larger than "
            "its demand."
        ),
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_ROOM,
        quantified_over="each session requiring a room",
        rationale=(
            "Twelve students in a 300-seat auditorium is legal, wasteful, and "
            "blocks the one class that needed it. Soft, because the only cost "
            "is opportunity."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_CONSTRAINT_WEIGHT,
    ),
    _spec(
        code="TEACHER_PREFERENCE",
        name="Teacher preferences are satisfied",
        description=(
            "Violations of Preference rows owned by a teacher: preferred and "
            "avoided slots, days, rooms and campuses, early/late bands, "
            "compactness and daily-hour wishes."
        ),
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each teacher preference row",
        rationale=(
            "A preference is a wish, and the schema keeps it structurally "
            "separate from Availability so it can never be promoted into a "
            "hard constraint by accident. 'The professor prefers mornings' "
            "must not become 'the schedule is infeasible'."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_PREFERENCE_WEIGHT,
        adapter_notes=(
            "Preference.weight is relative *within* a kind and carries no "
            "cross-kind meaning; do not compare a PREFERRED_ROOM weight of 5 "
            "with an AVOID_DAY weight of 5 as though they were the same "
            "quantity. Normalise per kind before summing."
        ),
    ),
    _spec(
        code="PROGRAM_PREFERENCE",
        name="Programme preferences are satisfied",
        description="Violations of Preference rows owned by a programme.",
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each programme preference row",
        rationale=(
            "Separate type from TEACHER_PREFERENCE so an institution can say "
            "that programme-level planning outranks individual wishes, or the "
            "reverse. That statement has nowhere else to live: the scope "
            "columns on PlanningConstraint do not distinguish owner class."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_PREFERENCE_WEIGHT,
    ),
    _spec(
        code="COHORT_PREFERENCE",
        name="Cohort preferences are satisfied",
        description="Violations of Preference rows owned by a student cohort.",
        default_category=_SOFT,
        is_category_overridable=True,
        parameter_schema=None,
        enforcement=_SOLVER,
        reads=_BOTH,
        quantified_over="each cohort preference row",
        rationale=(
            "Cohorts are legitimate here — this is a stated group wish, not a "
            "conflict claim. Cohorts are never a conflict authority; "
            "Enrollment is."
        ),
        term_unit=_WEIGHTED,
        weight_source=WeightSource.HUMAN_PREFERENCE_WEIGHT,
    ),
)


#: The catalog, keyed by code. Frozen at import.
CONSTRAINT_CATALOG: Mapping[str, ConstraintSpec] = MappingProxyType(dict(_SPECS))


#: Which catalog entries answer each item on the SESSION 2 checklist.
#: `tests/test_constraint_catalog.py` asserts this covers the list exactly, so
#: the session cannot quietly skip one.
@dataclass(frozen=True, slots=True)
class Coverage:
    """How one item on the SESSION 2 checklist is answered.

    Split into two fields because "analysed" and "constrained" are different
    claims and conflating them lets the catalog certify enforcement it does
    not provide. ``deferred_to`` names the session that will close the gap, so
    a deferral is a commitment rather than an omission.
    """

    constrained_by: tuple[str, ...] = ()
    deferred_to: str | None = None
    note: str = ""


#: Which catalog entries answer each item on the SESSION 2 checklist.
#: `tests/test_constraint_catalog.py` asserts this covers the list exactly, so
#: the session cannot quietly skip one.
PLAYBOOK_COVERAGE: Mapping[str, Coverage] = MappingProxyType(
    {
        "teacher overlap": Coverage(("TEACHER_NO_OVERLAP",)),
        "room overlap": Coverage(("ROOM_NO_OVERLAP",)),
        "student overlap": Coverage(
            (
                "STUDENT_NO_OVERLAP_UNRESOLVABLE",
                "STUDENT_NO_OVERLAP_MANDATORY",
                "STUDENT_NO_OVERLAP_ELECTIVE",
                "SECTION_SESSIONS_NO_OVERLAP",
            )
        ),
        "teacher availability": Coverage(("TEACHER_AVAILABILITY",)),
        "room capacity": Coverage(("ROOM_CAPACITY", "ROOM_CAPACITY_FIT")),
        "room capabilities": Coverage(
            ("ROOM_FEATURE_REQUIREMENT", "ROOM_TYPE_ALLOWED", "ROOM_ACTIVE")
        ),
        "session duration": Coverage(
            ("SESSION_WITHIN_DAY_GRID", "SESSION_SLOTS_SCHEDULABLE")
        ),
        "locked assignments": Coverage(("ASSIGNMENT_LOCKED", "SESSION_PINNED")),
        "campus travel": Coverage(("TEACHER_CAMPUS_TRAVEL", "STUDENT_CAMPUS_TRAVEL")),
        "shared courses": Coverage(
            (
                "CO_TEACHING_SAME_TIMESLOT",
                "CO_TEACHING_SAME_ROOM",
                "ROOM_CAMPUS_MATCH",
            )
        ),
        "repeated-course students": Coverage(
            ("STUDENT_NO_OVERLAP_UNRESOLVABLE", "STUDENT_NO_OVERLAP_MANDATORY"),
            deferred_to="SESSION 5",
            note=(
                "A repeat with no alternative section is already UNRESOLVABLE "
                "and therefore HARD. A repeat that *does* have alternatives is "
                "MANDATORY, where repeat status is meant to raise the weight "
                "of the incidence through CRITICALITY_PROVIDER — and that "
                "provider is uniform until SESSION 5 builds it. So repeat "
                "status is carried on the enrollment and read by the "
                "classification, but its ranking effect is inert today. "
                "SESSION 5 closes this, and the deferral is stated here rather "
                "than left to be discovered."
            ),
        ),
        "single-section criticality": Coverage(
            ("STUDENT_NO_OVERLAP_UNRESOLVABLE",),
            deferred_to="SESSION 5",
            note=(
                "Having exactly one section is precisely what makes "
                "`has_alternative` false, so the pairwise-clash aspect is "
                "fully constrained here. Criticality as a *ranking quantity* "
                "over many students is SESSION 5's."
            ),
        ),
        "teacher workload": Coverage(
            ("TEACHER_MAX_WEEKLY_HOURS",),
            note=(
                "Analysed and found invariant under the decision variables: "
                "who teaches what is fixed input (ADR-0008), so no choice of "
                "slot or room changes a weekly total. Enforced as a "
                "PRECONDITION, not optimised. This is coverage by analysis, "
                "not by constraint."
            ),
        ),
        "daily limits": Coverage(
            ("TEACHER_MAX_DAILY_HOURS", "STUDENT_MAX_DAILY_HOURS",
             "TEACHER_MAX_CONSECUTIVE_SLOTS")
        ),
        "weekly distribution": Coverage(
            ("TEACHER_MAX_TEACHING_DAYS", "SECTION_DAY_SPREAD")
        ),
        "student gaps": Coverage(("STUDENT_GAP_MINIMISATION",)),
        "teacher gaps": Coverage(("TEACHER_GAP_MINIMISATION",)),
        "room changes": Coverage(("ROOM_CHANGE_MINIMISATION",)),
        "preferred slots": Coverage(
            ("TEACHER_PREFERENCE", "PROGRAM_PREFERENCE", "COHORT_PREFERENCE")
        ),
        "schedule stability": Coverage(("SCHEDULE_STABILITY",)),
    }
)


def solver_hard_codes() -> frozenset[str]:
    """Constraints CP-SAT must enforce when no override is in play.

    SESSION 3's adapter registry must be complete over this set: a HARD
    constraint with no adapter is a constraint that silently does not exist.
    """
    return frozenset(
        spec.code
        for spec in CONSTRAINT_CATALOG.values()
        if spec.default_category is ConstraintCategory.HARD
        and spec.enforcement is Enforcement.SOLVER
    )


def objective_bearing_codes() -> frozenset[str]:
    """Constraints that contribute an objective term when not HARD."""
    return frozenset(
        spec.code for spec in CONSTRAINT_CATALOG.values() if spec.term_unit is not None
    )


def downgradable_hard_codes() -> frozenset[str]:
    """HARD constraints a named human may demote to MEDIUM or SOFT."""
    return frozenset(
        spec.code
        for spec in CONSTRAINT_CATALOG.values()
        if spec.default_category is ConstraintCategory.HARD
        and spec.is_category_overridable
    )
