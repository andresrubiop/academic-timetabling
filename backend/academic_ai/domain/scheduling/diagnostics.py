"""Named diagnostics.

A diagnostic is a statement about the *instance*, not about the schedule.

The failure this module exists to prevent is silence. Almost every entry
below describes a situation where the model still solves, still returns
OPTIMAL, and is still wrong — because a constraint the reader believes is
enforced turned out to be vacuous. "No availability was ever recorded for
this teacher" and "this teacher is available at every hour of the week" are
the same input to the solver and completely different facts about the world;
only a diagnostic can tell them apart.

Three of these were opened by name in SESSION 1 and are closed here:

* ``TEACHER_AVAILABILITY_UNDECLARED`` — default-available silently produces
  schedules teachers reject (STATE.md, "Default availability semantics").
* ``SESSION_DURATION_NOT_EXPRESSIBLE`` — a 45-minute session on a 60-minute
  grid must be named, not rounded (ADR-0006).
* ``UNPLACED_ENROLLMENT_AMBIGUOUS`` — unplaced enrollments are real demand
  and must not vanish from the conflict model (ADR-0009).

The ``IMPORT_*`` codes exist for the same reason one level out. An importer
that silently drops a constraint kind it does not understand produces an
instance the user believes is faithful and is not — and the schedule that
comes back will look fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from academic_ai.domain.scheduling.enums import DiagnosticSeverity

__all__ = [
    "DIAGNOSTIC_SEVERITY",
    "Diagnostic",
    "DiagnosticCode",
]


class DiagnosticCode(str, Enum):
    """Every diagnostic the problem builder may emit.

    Closed set on purpose: SESSION 6's Feasibility Analyzer and SESSION 9's
    API both have to render these, and a free-text diagnostic is a string
    nobody can route, translate or count.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    # --- the instance cannot be solved as stated ---------------------------
    GRID_HAS_NO_SCHEDULABLE_SLOTS = "GRID_HAS_NO_SCHEDULABLE_SLOTS"
    SESSION_HAS_NO_FEASIBLE_START = "SESSION_HAS_NO_FEASIBLE_START"
    SESSION_HAS_NO_FEASIBLE_ROOM = "SESSION_HAS_NO_FEASIBLE_ROOM"
    SESSION_LONGER_THAN_ANY_DAY = "SESSION_LONGER_THAN_ANY_DAY"
    ROOM_CAPACITY_UNSATISFIABLE = "ROOM_CAPACITY_UNSATISFIABLE"
    CO_TEACHING_GROUP_NOT_APPROVED = "CO_TEACHING_GROUP_NOT_APPROVED"
    CO_TEACHING_GROUP_SPANS_PERIODS = "CO_TEACHING_GROUP_SPANS_PERIODS"
    CONSTRAINT_ADAPTER_MISSING = "CONSTRAINT_ADAPTER_MISSING"
    TEACHER_WEEKLY_LOAD_EXCEEDED = "TEACHER_WEEKLY_LOAD_EXCEEDED"
    LOCKED_ASSIGNMENTS_CONFLICT = "LOCKED_ASSIGNMENTS_CONFLICT"

    # --- solvable, but something the reader trusts is vacuous or partial ---
    TEACHER_AVAILABILITY_UNDECLARED = "TEACHER_AVAILABILITY_UNDECLARED"
    ROOM_AVAILABILITY_UNDECLARED = "ROOM_AVAILABILITY_UNDECLARED"
    TRAVEL_TIME_UNDECLARED = "TRAVEL_TIME_UNDECLARED"
    UNPLACED_ENROLLMENT_AMBIGUOUS = "UNPLACED_ENROLLMENT_AMBIGUOUS"
    SESSION_HAS_NO_TEACHER = "SESSION_HAS_NO_TEACHER"
    SESSION_DURATION_NOT_EXPRESSIBLE = "SESSION_DURATION_NOT_EXPRESSIBLE"
    SECTION_CONTACT_HOURS_MISMATCH = "SECTION_CONTACT_HOURS_MISMATCH"
    CO_TEACHING_FEATURE_AGGREGATION_AMBIGUOUS = (
        "CO_TEACHING_FEATURE_AGGREGATION_AMBIGUOUS"
    )
    HARD_CONSTRAINT_DOWNGRADED = "HARD_CONSTRAINT_DOWNGRADED"
    OBJECTIVE_TERM_OMITTED_BY_PROFILE = "OBJECTIVE_TERM_OMITTED_BY_PROFILE"

    # --- import (FET and other external sources) ---------------------------
    IMPORT_CONSTRAINT_KIND_UNMAPPED = "IMPORT_CONSTRAINT_KIND_UNMAPPED"
    IMPORT_CRITICALITY_UNAVAILABLE = "IMPORT_CRITICALITY_UNAVAILABLE"
    IMPORT_COHORT_TREATED_AS_STUDENT = "IMPORT_COHORT_TREATED_AS_STUDENT"
    IMPORT_ROOM_CAPACITY_UNBOUNDED = "IMPORT_ROOM_CAPACITY_UNBOUNDED"
    IMPORT_ACTIVITY_INACTIVE = "IMPORT_ACTIVITY_INACTIVE"
    IMPORT_BASE_SCHEDULE_DETECTED = "IMPORT_BASE_SCHEDULE_DETECTED"

    # --- informational -----------------------------------------------------
    UNPLACED_ENROLLMENT_RESOLVED = "UNPLACED_ENROLLMENT_RESOLVED"
    SESSION_NOT_SCHEDULED_BY_DESIGN = "SESSION_NOT_SCHEDULED_BY_DESIGN"
    OVERLAY_APPLIED = "OVERLAY_APPLIED"


#: Severity of each code. A separate mapping rather than an enum attribute so
#: that the exhaustiveness test below can compare two independent lists — an
#: attribute could not be forgotten, but it also could not be *audited*.
DIAGNOSTIC_SEVERITY: Mapping[DiagnosticCode, DiagnosticSeverity] = MappingProxyType(
    {
        DiagnosticCode.GRID_HAS_NO_SCHEDULABLE_SLOTS: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.SESSION_LONGER_THAN_ANY_DAY: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.ROOM_CAPACITY_UNSATISFIABLE: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.CO_TEACHING_GROUP_NOT_APPROVED: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.CO_TEACHING_GROUP_SPANS_PERIODS: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.CONSTRAINT_ADAPTER_MISSING: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.TEACHER_WEEKLY_LOAD_EXCEEDED: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.LOCKED_ASSIGNMENTS_CONFLICT: DiagnosticSeverity.BLOCKING,
        DiagnosticCode.TEACHER_AVAILABILITY_UNDECLARED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.ROOM_AVAILABILITY_UNDECLARED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.TRAVEL_TIME_UNDECLARED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.SESSION_HAS_NO_TEACHER: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.SESSION_DURATION_NOT_EXPRESSIBLE: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.SECTION_CONTACT_HOURS_MISMATCH: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.CO_TEACHING_FEATURE_AGGREGATION_AMBIGUOUS: (
            DiagnosticSeverity.DEGRADED
        ),
        DiagnosticCode.HARD_CONSTRAINT_DOWNGRADED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.OBJECTIVE_TERM_OMITTED_BY_PROFILE: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.IMPORT_CONSTRAINT_KIND_UNMAPPED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.IMPORT_CRITICALITY_UNAVAILABLE: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.IMPORT_COHORT_TREATED_AS_STUDENT: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.IMPORT_ROOM_CAPACITY_UNBOUNDED: DiagnosticSeverity.DEGRADED,
        DiagnosticCode.IMPORT_ACTIVITY_INACTIVE: DiagnosticSeverity.INFORMATIONAL,
        DiagnosticCode.IMPORT_BASE_SCHEDULE_DETECTED: DiagnosticSeverity.INFORMATIONAL,
        DiagnosticCode.UNPLACED_ENROLLMENT_RESOLVED: DiagnosticSeverity.INFORMATIONAL,
        DiagnosticCode.SESSION_NOT_SCHEDULED_BY_DESIGN: DiagnosticSeverity.INFORMATIONAL,
        DiagnosticCode.OVERLAY_APPLIED: DiagnosticSeverity.INFORMATIONAL,
    }
)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One diagnostic occurrence.

    ``subject`` names the entity the diagnostic is about (a teacher id, a
    session id) so SESSION 6 can turn it into a recommendation and SESSION 9
    can link to it, rather than printing a sentence.
    """

    code: DiagnosticCode
    message: str
    subject_kind: str | None = None
    subject_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> DiagnosticSeverity:
        return DIAGNOSTIC_SEVERITY[self.code]

    @property
    def is_blocking(self) -> bool:
        return self.severity is DiagnosticSeverity.BLOCKING
