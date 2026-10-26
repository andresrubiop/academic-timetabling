"""The problem instance, and the shape of the thing that builds it.

ADR-0007 fixes one rule that cannot be retrofitted: **the problem builder's
input is `(base data, overlay)`, never the database alone.** Half the
perturbations the platform must support — a teacher falling ill, a room
closing, a new section, a proposed merge — change base tables rather than
assignments, so a builder that reads the database directly has nowhere to put
a hypothetical except the real `availability` table. SESSION 10 creates a
scenario on every drag-and-drop, so that is not a rare case.

This module makes the rule structural rather than advisory. `ProblemBuilder`
takes ``overlay`` as a required keyword argument; the no-scenario case passes
`Overlay.EMPTY`. A builder written against a database session cannot satisfy
the protocol, and `tests/test_problem_contract.py` checks that it still
cannot.

Everything here is framework-free and hashable-ish by construction: the same
instance must be constructible from the database, from a fixture, or from a
JSON payload sent to a remote worker. `academic_ai.domain` may not import
SQLAlchemy or OR-Tools, and `tests/test_domain_purity.py` enforces it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol, Sequence, runtime_checkable

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek, Modality, SessionType
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG
from academic_ai.domain.scheduling.enums import Enforcement
from academic_ai.domain.scheduling.diagnostics import Diagnostic
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES, ObjectiveProfile
from academic_ai.domain.scheduling.scope import ConstraintScope, ScopeContext

__all__ = [
    "BaseAssignment",
    "BaseSnapshot",
    "CoTeachingGroupSpec",
    "ConstraintScope",
    "EffectiveConstraint",
    "ScopeContext",
    "Overlay",
    "Perturbation",
    "PeriodSpec",
    "ProblemBuilder",
    "ProblemInstance",
    "RoomSpec",
    "SessionSpec",
    "SlotSpec",
    "StudentEnrollmentSpec",
    "TeacherSpec",
    "classify_student_conflict",
]


# ---------------------------------------------------------------------------
# Entities, as the mathematical model needs to see them
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodSpec:
    id: str
    slot_minutes: int
    #: Days that have at least one slot.
    days: tuple[DayOfWeek, ...]


@dataclass(frozen=True, slots=True)
class SlotSpec:
    id: str
    day_of_week: DayOfWeek
    #: Dense within the day, from 0. Occupancy arithmetic uses this one.
    in_day_index: int
    #: Dense across the week. Ordering and gap features use this one.
    global_index: int
    start_minute: int
    is_schedulable: bool = True


@dataclass(frozen=True, slots=True)
class RoomSpec:
    id: str
    campus_id: str
    capacity: int
    room_type: str
    #: feature id -> quantity present.
    features: Mapping[str, int] = field(default_factory=dict)
    #: feature id -> whether the quantity is consumed per seat rather than
    #: shared. Decides how a co-teaching group aggregates its requirement.
    additive_features: frozenset[str] = frozenset()
    is_active: bool = True
    #: Slots in which the room may not be used (deny-list, as in the schema).
    unavailable_slot_ids: frozenset[str] = frozenset()
    has_declared_availability: bool = False


@dataclass(frozen=True, slots=True)
class TeacherSpec:
    id: str
    max_weekly_minutes: int | None = None
    max_daily_minutes: int | None = None
    #: Deny-list: absence means available. A teacher with no rows at all is
    #: indistinguishable from one available every hour, which is why
    #: `has_declared_availability` exists rather than being inferred from
    #: emptiness.
    unavailable_slot_ids: frozenset[str] = frozenset()
    has_declared_availability: bool = False


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """A `ClassSession` — the schedulable unit."""

    id: str
    section_id: str
    offering_id: str
    course_id: str
    #: The campus the offering names; ROOM_CAMPUS_MATCH compares against it.
    campus_id: str
    duration_slots: int
    session_type: SessionType = SessionType.LECTURE
    delivery_mode: Modality = Modality.ONSITE
    requires_timeslot: bool = True
    requires_room: bool = True
    #: Teachers whose time this session occupies (PRIMARY/ASSISTANT only —
    #: a SUBSTITUTE must not reach this list).
    teacher_ids: tuple[str, ...] = ()
    #: feature id -> minimum quantity.
    required_features: Mapping[str, int] = field(default_factory=dict)
    #: Rooms this session may use, when the institution restricts them.
    #: **Empty means unrestricted**, not "no room is allowed" — the common
    #: case is no restriction at all, and a set that defaulted to forbidding
    #: everything would make every ordinary session unplaceable.
    #: Populated from a room-type rule, or from an import that carries an
    #: explicit list (FET's Subject/Activity preferred rooms).
    allowed_room_ids: frozenset[str] = frozenset()
    #: Seats this session needs on its own, before any co-teaching merge.
    seat_demand: int = 0
    co_teaching_group_id: str | None = None
    #: The programmes this session's offering serves, and their faculties.
    #:
    #: Domain data, not labels: *how far a change reaches* is what
    #: `assess_scope` routes an approval on, and CLAUDE.md puts cross-program
    #: and cross-faculty changes among the decisions that need a higher
    #: authority. A course code would not belong here — that is presentation,
    #: and it lives in the repository's separate label read — but programme
    #: membership is a fact the optimizer's impact analysis is built on.
    #:
    #: Plural because a shared or common-core course serves several
    #: programmes, which is the ordinary case rather than an exotic one.
    program_ids: tuple[str, ...] = ()
    faculty_ids: tuple[str, ...] = ()
    #: How long the session really runs, in minutes, when that is known
    #: independently of the grid. A 45-minute class on a 60-minute grid is a
    #: fact about the curriculum that the grid cannot express, and ADR-0006
    #: requires it be named rather than rounded. ``None`` means the duration
    #: is defined by the grid and there is nothing to reconcile.
    contact_minutes: int | None = None

    @property
    def is_scheduled(self) -> bool:
        """Whether this session generates decision variables at all."""
        return self.requires_timeslot


@dataclass(frozen=True, slots=True)
class CoTeachingGroupSpec:
    id: str
    member_session_ids: tuple[str, ...]
    #: Backed by an APPROVED AcademicCompatibilityGroup. An unapproved group
    #: is a blocking diagnostic, never a merge the solver quietly performs.
    is_approved: bool = False


@dataclass(frozen=True, slots=True)
class StudentEnrollmentSpec:
    """One student's demand for one offering, already resolved to sessions.

    ``session_ids`` are the sessions of the section the student effectively
    occupies — the placed one, or the only one when the offering has a single
    section (see `variables.resolve_effective_section`). An enrollment that
    could not be resolved carries an empty tuple and must have produced
    `DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS`.
    """

    student_id: str
    offering_id: str
    session_ids: tuple[str, ...]
    is_mandatory: bool
    #: Sections of this offering the student could move to instead.
    alternative_section_count: int = 0
    #: Other open offerings of the same course the student is eligible for.
    #: ADR-0009 and SESSION 5 both list this as a resolution route, so a
    #: student with no alternative section but another offering available is
    #: not stranded — and classifying them UNRESOLVABLE would report a
    #: spurious INFEASIBLE for something a registrar can fix in a minute.
    alternative_offering_count: int = 0
    #: attempt_number > 1. Not part of the classification below; it is an
    #: input to SESSION 5's criticality weighting.
    is_repeat: bool = False
    #: 1 = first attempt. A third attempt is a student who has already lost
    #: two periods to this course, which criticality weighs more heavily than
    #: a second.
    attempt_number: int = 1
    #: 0..1, how much of the student's progression depends on passing this now
    #: — a final-year mandatory course scores higher than a first-year one.
    #: Supplied by the caller; SESSION 5 reads it, nothing computes it here.
    progression_risk: float = 0.0

    @property
    def is_resolved(self) -> bool:
        return bool(self.session_ids)

    @property
    def has_alternative(self) -> bool:
        """Whether *any* registrar action resolves a clash on this enrollment.

        Deliberately optimistic and static (ADR-0009 §5): it asks whether an
        alternative exists at all, not whether that alternative is free in the
        schedule finally produced. Making the HARD boundary schedule-dependent
        needs a disjunction over sections and is a different model. The
        optimism is recorded rather than hidden: SESSION 3's validator must
        check each MANDATORY incidence against the produced schedule and
        report the ones whose only alternative turned out to be busy.
        """
        return (
            self.alternative_section_count > 0
            or self.alternative_offering_count > 0
        )


def classify_student_conflict(
    a: StudentEnrollmentSpec, b: StudentEnrollmentSpec
) -> str:
    """Which constraint governs a clash between two of one student's enrollments.

    The three-way split, and why the boundaries sit where they do:

    * **UNRESOLVABLE** — both mandatory, and *neither* can be re-sectioned.
      No action by anyone resolves this clash, so the student simply cannot
      take both courses this period. This is the population the platform
      exists to protect and the only student constraint that is HARD.
    * **MANDATORY** — both mandatory, but at least one has somewhere else to
      go, so a registrar action could fix it. Bad, minimised aggressively,
      not fatal.
    * **ELECTIVE** — at least one enrollment is not mandatory; the student
      keeps the option of dropping it.

    A repeat attempt does not by itself make a clash unresolvable — a student
    repeating a course with four sections has three moves available. Repeats
    raise the *weight* of the incidence, through SESSION 5's criticality
    provider, not its category. Getting that backwards would flag thousands
    of resolvable repeats as fatal and hide the handful that really are.
    """
    if a.offering_id == b.offering_id:
        # Two sessions of the student's own section. Moving them apart is not
        # something re-sectioning can do — every alternative section contains
        # both sessions too, and they move together. Counting the alternatives
        # here would call the one definitionally unresolvable clash resolvable.
        # `SECTION_SESSIONS_NO_OVERLAP` also forbids it outright, independently
        # of whether this enrollment was ever placed.
        return "STUDENT_NO_OVERLAP_UNRESOLVABLE"
    if a.is_mandatory and b.is_mandatory:
        if not a.has_alternative and not b.has_alternative:
            return "STUDENT_NO_OVERLAP_UNRESOLVABLE"
        return "STUDENT_NO_OVERLAP_MANDATORY"
    return "STUDENT_NO_OVERLAP_ELECTIVE"


@dataclass(frozen=True, slots=True)
class BaseAssignment:
    """An assignment carried in from the version being re-optimised."""

    session_id: str
    start_slot_id: str
    room_id: str | None = None
    is_locked: bool = False


@dataclass(frozen=True, slots=True)
class EffectiveConstraint:
    """One catalog entry, resolved against the institution's configuration.

    ``effective_category`` is the resolution of
    ``category_override ?? constraint_type.default_category``. Constructing
    one with an override the catalog forbids raises — the database enforces
    the same rule with a composite foreign key, and this is the copy that runs
    on a remote worker holding no database connection.
    """

    code: str
    effective_category: ConstraintCategory
    #: Which part of the institution this instance applies to. Institution-wide
    #: by default. A scoped override stays local — see `scope.py` for why that
    #: matters to room-domain pruning as well as to correctness.
    scope: ConstraintScope = ConstraintScope()
    weight: float = 1.0
    parameters: Mapping[str, object] = field(default_factory=dict)
    is_active: bool = True
    #: Set when a human downgraded the catalog default. Both fields are
    #: required together by `override_requires_authority` in the schema.
    overridden_by_user_id: str | None = None
    override_reason: str | None = None

    def __post_init__(self) -> None:
        spec = CONSTRAINT_CATALOG.get(self.code)
        if spec is None:
            raise ValueError(f"unknown constraint code {self.code!r}")
        if self.effective_category is not spec.default_category:
            if not spec.is_category_overridable:
                raise ValueError(
                    f"{self.code}: the catalog forbids overriding this "
                    "category — it describes physical or safety reality, not "
                    "institutional policy"
                )
            if not (self.overridden_by_user_id and self.override_reason):
                raise ValueError(
                    f"{self.code}: changing a constraint's category is an "
                    "institutional act and must name the person who took it "
                    "and why"
                )
        if self.weight < 0:
            raise ValueError(f"{self.code}: weight must be non-negative")
        if (
            not self.is_active
            and spec.default_category is ConstraintCategory.HARD
            and not spec.is_category_overridable
        ):
            raise ValueError(
                f"{self.code}: cannot be deactivated. It is HARD and not "
                "overridable, so switching it off would relax physical reality "
                "with no approver, no reason and no audit trail — the one "
                "route the category system exists to close."
            )

    def applies_to(self, context: ScopeContext) -> bool:
        return self.is_active and self.scope.matches(context)

    @property
    def is_downgraded_hard(self) -> bool:
        """True when a HARD catalog default was demoted by a human."""
        spec = CONSTRAINT_CATALOG[self.code]
        return (
            spec.default_category is ConstraintCategory.HARD
            and self.effective_category is not ConstraintCategory.HARD
        )


# ---------------------------------------------------------------------------
# Overlay (ADR-0007)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Perturbation:
    """One declarative change applied on top of base data.

    Mirrors `ScenarioPerturbation`. ``kind`` is checked against
    `SCENARIO_PERTURBATION_KINDS` by the database; here it stays a plain
    string so the domain does not import the infrastructure module that
    defines it.
    """

    kind: str
    sequence_number: int
    targets: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Overlay:
    """A scenario's perturbations, or nothing.

    `Overlay.EMPTY` is the honest way to say "no what-if here". It exists so
    the builder signature can require an overlay unconditionally, which is
    what stops a database-only builder from ever being written.
    """

    scenario_id: str | None = None
    perturbations: tuple[Perturbation, ...] = ()

    def __post_init__(self) -> None:
        ordered = sorted(p.sequence_number for p in self.perturbations)
        if ordered != list(dict.fromkeys(ordered)):
            raise ValueError("perturbation sequence numbers must be unique")

    @property
    def is_empty(self) -> bool:
        return not self.perturbations

    def in_order(self) -> tuple[Perturbation, ...]:
        """Perturbations in application order; later ones may depend on earlier."""
        return tuple(sorted(self.perturbations, key=lambda p: p.sequence_number))

    def digest(self) -> str:
        """Stable digest of what this overlay does.

        Recorded on the run so two results can be compared without keeping
        the scenario row alive, and so a re-run with a mutated scenario is
        visibly a different experiment.
        """
        payload = [
            {
                "kind": p.kind,
                "sequence_number": p.sequence_number,
                "targets": dict(sorted(p.targets.items())),
                "payload": dict(sorted(p.payload.items(), key=lambda kv: kv[0])),
            }
            for p in self.in_order()
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


Overlay.EMPTY = Overlay()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The instance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaseSnapshot:
    """Everything read from base data for one period, before any overlay."""

    period: PeriodSpec
    slots: tuple[SlotSpec, ...] = ()
    rooms: tuple[RoomSpec, ...] = ()
    teachers: tuple[TeacherSpec, ...] = ()
    sessions: tuple[SessionSpec, ...] = ()
    co_teaching_groups: tuple[CoTeachingGroupSpec, ...] = ()
    enrollments: tuple[StudentEnrollmentSpec, ...] = ()
    #: (origin campus, destination campus) -> minutes. Directional.
    travel_minutes: Mapping[tuple[str, str], int] = field(default_factory=dict)
    base_assignments: tuple[BaseAssignment, ...] = ()

    def slots_by_day(self, day: DayOfWeek) -> tuple[SlotSpec, ...]:
        return tuple(
            sorted(
                (s for s in self.slots if s.day_of_week is day),
                key=lambda s: s.in_day_index,
            )
        )

    def scheduled_sessions(self) -> tuple[SessionSpec, ...]:
        return tuple(s for s in self.sessions if s.is_scheduled)


@dataclass(frozen=True, slots=True)
class ProblemInstance:
    """A fully resolved timetabling instance, ready to be modelled.

    ``overlay_digest`` and ``scenario_id`` travel with the instance because
    reproducibility is a stated requirement (SESSION 3 fixtures, SESSION 4
    "no LNS improvement without reproducible evidence"), and an instance that
    cannot say which what-if produced it is not reproducible.
    """

    snapshot: BaseSnapshot
    constraints: tuple[EffectiveConstraint, ...]
    objective_profile: ObjectiveProfile
    diagnostics: tuple[Diagnostic, ...] = ()
    scenario_id: str | None = None
    overlay_digest: str = ""

    def __post_init__(self) -> None:
        keys = [(c.code, c.scope.as_key()) for c in self.constraints]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "a constraint code may be resolved only once per scope; two "
                "rows with the same code and the same scope are a "
                "contradiction in the institution's configuration"
            )
        if self.objective_profile.code not in OBJECTIVE_PROFILES:
            raise ValueError(f"unknown objective profile {self.objective_profile.code!r}")
        if (
            self.objective_profile.code == "GREENFIELD"
            and self.snapshot.base_assignments
        ):
            raise ValueError(
                "GREENFIELD omits SCHEDULE_STABILITY, so an instance carrying "
                "base assignments would silently ignore them; use "
                "MINIMAL_DISRUPTION"
            )

    @property
    def is_blocked(self) -> bool:
        """Whether a blocking diagnostic means this must not reach the solver."""
        return any(d.is_blocking for d in self.diagnostics)

    def active(self) -> tuple[EffectiveConstraint, ...]:
        return tuple(c for c in self.constraints if c.is_active)

    def hard_codes(self) -> frozenset[str]:
        """Every HARD constraint this instance enforces.

        Derived from the **catalog**, not from `constraints`. A constraint
        nobody configured is not thereby switched off: absence means "no
        instance-specific parameters", and the catalog default applies.

        The alternative — reading only the rows that happen to exist — meant
        an instance with an empty constraint list enforced nothing at all,
        while `missing_adapters` cheerfully reported a complete adapter
        registry. The result would be a solver returning OPTIMAL for a
        timetable that double-books a teacher.
        """
        # Only an institution-wide row can take a code out of the enforced
        # set. A scoped override narrows where a constraint is relaxed; it
        # does not speak for the rest of the institution, and reading it as
        # though it did silently deleted HARD constraints — from the model and
        # from the validator, which reads this same set.
        institution_wide = {
            c.code: c for c in self.constraints if c.scope.is_institution_wide
        }
        enforced: set[str] = set()
        for code, spec in CONSTRAINT_CATALOG.items():
            if spec.enforcement is not Enforcement.SOLVER:
                continue
            configured = institution_wide.get(code)
            if configured is None:
                if spec.default_category is ConstraintCategory.HARD:
                    enforced.add(code)
            elif (
                configured.is_active
                and configured.effective_category is ConstraintCategory.HARD
            ):
                enforced.add(code)
        return frozenset(enforced)

    def downgraded_hard(self) -> tuple[EffectiveConstraint, ...]:
        """The constraints tier 1 of the objective collects."""
        return tuple(c for c in self.active() if c.is_downgraded_hard)

    def resolve(
        self, code: str, context: ScopeContext | None = None
    ) -> EffectiveConstraint | None:
        """The constraint instance governing ``code`` in ``context``.

        Most specific scope wins. A tie raises rather than picking one: two
        equally specific overrides of the same constraint contradict each
        other, and resolving by row order would make the timetable depend on
        insertion sequence.

        Returns ``None`` when nothing is configured, which means "the catalog
        default applies" — never "switched off".
        """
        context = context or ScopeContext()
        candidates = [
            c for c in self.constraints if c.code == code and c.applies_to(context)
        ]
        if not candidates:
            return None
        best = max(c.scope.specificity for c in candidates)
        winners = [c for c in candidates if c.scope.specificity == best]
        if len(winners) > 1:
            raise ValueError(
                f"{code}: {len(winners)} equally specific overrides match "
                f"{context} ({', '.join(w.scope.describe() for w in winners)}); "
                "the institution's configuration contradicts itself"
            )
        return winners[0]

    def category_for(
        self, code: str, context: ScopeContext | None = None
    ) -> ConstraintCategory:
        """Effective category of ``code`` here, falling back to the catalog."""
        resolved = self.resolve(code, context)
        if resolved is not None:
            return resolved.effective_category
        return CONSTRAINT_CATALOG[code].default_category

    def with_diagnostics(self, *diagnostics: Diagnostic) -> ProblemInstance:
        return replace(self, diagnostics=self.diagnostics + diagnostics)


@runtime_checkable
class ProblemBuilder(Protocol):
    """What SESSION 3 must implement.

    The signature is the point. ``overlay`` is required and keyword-only, so
    "just read the database" is not expressible — which is what ADR-0007
    means by a decision that cannot be retrofitted.
    """

    def build(
        self, *, base: BaseSnapshot, overlay: Overlay, objective_profile: str
    ) -> ProblemInstance: ...


def missing_adapters(
    instance: ProblemInstance, registered: Sequence[str]
) -> frozenset[str]:
    """HARD constraints of this instance that no adapter claims to implement.

    SESSION 3 must call this and refuse to solve if it returns anything. A
    HARD constraint with no adapter does not fail loudly — it silently is not
    enforced, and the solver returns a confident OPTIMAL for a schedule that
    double-books a room. This is the single most important safety property of
    the pluggable architecture, which is why it is a function here and not a
    convention there.
    """
    return instance.hard_codes() - frozenset(registered)
