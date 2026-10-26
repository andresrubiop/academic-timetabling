"""Domain enumerations.

These are the vocabulary of the academic domain. They are framework-free
(plain `enum.Enum`) and are mapped onto native PostgreSQL enum types by the
infrastructure layer.

Convention: every member's *value* equals its *name*. The value is what is
persisted, so renaming a member is a migration, not a refactor.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AIModelStatus",
    "AcademicApprovalStatus",
    "AcademicPeriodStatus",
    "ApprovalAction",
    "AuditAction",
    "AvailabilityStatus",
    "ChangeRequestStatus",
    "ConstraintCategory",
    "CourseStatus",
    "DayOfWeek",
    "EnrollmentKind",
    "EnrollmentStatus",
    "LifecycleStatus",
    "Modality",
    "NotificationChannel",
    "NotificationKind",
    "OfferingStatus",
    "OptimizationRunStatus",
    "PreferenceKind",
    "ProposalSource",
    "RecommendationKind",
    "RecommendationStatus",
    "RequirementType",
    "RoomType",
    "ScheduleScope",
    "ScheduleVersionStatus",
    "ScenarioStatus",
    "SectionStatus",
    "ServiceRole",
    "SessionType",
    "SolverStatus",
    "TeacherContractType",
    "TeacherRole",
    "UserRole",
]


class _StrEnum(str, Enum):
    """String-valued enum whose value always equals its name."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# Generic lifecycle
# ---------------------------------------------------------------------------


class LifecycleStatus(_StrEnum):
    """Lifecycle for reference/catalog entities that are never hard-deleted.

    Academic records are not deleted; they are retired. See DATABASE.md
    "Lifecycles and deletion policy".
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class DayOfWeek(_StrEnum):
    """ISO-8601 weekday names. Ordinal is available via :meth:`iso_index`."""

    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"

    @property
    def iso_index(self) -> int:
        """1 = Monday .. 7 = Sunday, matching ISO-8601 and `date.isoweekday()`."""
        return _ISO_WEEKDAY_INDEX[self]


_ISO_WEEKDAY_INDEX = {
    DayOfWeek.MONDAY: 1,
    DayOfWeek.TUESDAY: 2,
    DayOfWeek.WEDNESDAY: 3,
    DayOfWeek.THURSDAY: 4,
    DayOfWeek.FRIDAY: 5,
    DayOfWeek.SATURDAY: 6,
    DayOfWeek.SUNDAY: 7,
}


class AcademicPeriodStatus(_StrEnum):
    """An academic period moves forward only; it never returns to PLANNING."""

    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


# ---------------------------------------------------------------------------
# Facilities
# ---------------------------------------------------------------------------


class RoomType(_StrEnum):
    CLASSROOM = "CLASSROOM"
    COMPUTER_LAB = "COMPUTER_LAB"
    SCIENCE_LAB = "SCIENCE_LAB"
    WORKSHOP = "WORKSHOP"
    AUDITORIUM = "AUDITORIUM"
    STUDIO = "STUDIO"
    FIELD = "FIELD"
    VIRTUAL = "VIRTUAL"
    OTHER = "OTHER"


class Modality(_StrEnum):
    ONSITE = "ONSITE"
    ONLINE = "ONLINE"
    HYBRID = "HYBRID"


# ---------------------------------------------------------------------------
# Curriculum and courses
# ---------------------------------------------------------------------------


class CourseStatus(_StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class RequirementType(_StrEnum):
    """How a curriculum version requires a course.

    MANDATORY courses drive `Enrollment.is_mandatory` and therefore
    StudentConflictCriticality (SESSION 5).
    """

    MANDATORY = "MANDATORY"
    ELECTIVE = "ELECTIVE"
    OPTIONAL = "OPTIONAL"


class ServiceRole(_StrEnum):
    """Why a program is served by a given course offering.

    This is *operational* sharing for one period, as opposed to
    `RequirementType`, which is the standing academic plan.
    """

    CORE = "CORE"
    COMMON_CORE = "COMMON_CORE"
    ELECTIVE = "ELECTIVE"
    SERVICE = "SERVICE"
    SHARED = "SHARED"


class ProposalSource(_StrEnum):
    """Where an academic proposal originated.

    AI_SUGGESTION exists so that an AI-proposed equivalence or compatibility
    is distinguishable from a human one and can never be silently treated as
    approved (CLAUDE.md — Human authority).
    """

    HUMAN = "HUMAN"
    AI_SUGGESTION = "AI_SUGGESTION"
    IMPORT = "IMPORT"


class AcademicApprovalStatus(_StrEnum):
    """Approval lifecycle for academic equivalence / compatibility.

    Only APPROVED rows may be consumed by the optimizer.
    """

    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


# ---------------------------------------------------------------------------
# Offerings
# ---------------------------------------------------------------------------


class OfferingStatus(_StrEnum):
    PLANNED = "PLANNED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SectionStatus(_StrEnum):
    PLANNED = "PLANNED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SessionType(_StrEnum):
    """Kind of weekly meeting block. A Section may mix types (e.g. a lecture
    block plus a lab block), which is why Session is a separate entity."""

    LECTURE = "LECTURE"
    LAB = "LAB"
    PRACTICE = "PRACTICE"
    SEMINAR = "SEMINAR"
    WORKSHOP = "WORKSHOP"
    TUTORING = "TUTORING"
    EXAM = "EXAM"


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class UserRole(_StrEnum):
    """Institutional roles (playbook SESSION 12). RBAC enforcement is
    SESSION 8/12; the vocabulary is fixed here because approvals and audit
    already reference it."""

    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    DEAN = "DEAN"
    DIRECTOR = "DIRECTOR"
    COORDINATOR = "COORDINATOR"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


class TeacherContractType(_StrEnum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    HOURLY = "HOURLY"
    VISITING = "VISITING"


class TeacherRole(_StrEnum):
    PRIMARY = "PRIMARY"
    ASSISTANT = "ASSISTANT"
    SUBSTITUTE = "SUBSTITUTE"


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


class EnrollmentKind(_StrEnum):
    """Why the student is taking this course.

    REPEATED is the single most important value in this module: SESSION 5
    requires that a student repeating a mandatory course with only one
    available section can outrank many students who have alternatives.
    """

    CURRENT_LEVEL = "CURRENT_LEVEL"
    REPEATED = "REPEATED"
    ADVANCED = "ADVANCED"
    ELECTIVE = "ELECTIVE"


class EnrollmentStatus(_StrEnum):
    REQUESTED = "REQUESTED"
    ENROLLED = "ENROLLED"
    WAITLISTED = "WAITLISTED"
    DROPPED = "DROPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_active(self) -> bool:
        """Active enrollments are the ones that generate student conflicts."""
        return self in _ACTIVE_ENROLLMENT_STATUSES


_ACTIVE_ENROLLMENT_STATUSES = frozenset(
    {
        EnrollmentStatus.REQUESTED,
        EnrollmentStatus.ENROLLED,
        EnrollmentStatus.WAITLISTED,
    }
)


# ---------------------------------------------------------------------------
# Availability, preferences, constraints
# ---------------------------------------------------------------------------


class AvailabilityStatus(_StrEnum):
    """Capability to be present. Feeds HARD constraints only."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class PreferenceKind(_StrEnum):
    """Soft desires. These never become HARD constraints (see
    `PlanningConstraint` for anything that must be enforced)."""

    PREFERRED_TIMESLOT = "PREFERRED_TIMESLOT"
    AVOID_TIMESLOT = "AVOID_TIMESLOT"
    PREFERRED_DAY = "PREFERRED_DAY"
    AVOID_DAY = "AVOID_DAY"
    PREFERRED_ROOM = "PREFERRED_ROOM"
    PREFERRED_CAMPUS = "PREFERRED_CAMPUS"
    COMPACT_SCHEDULE = "COMPACT_SCHEDULE"
    MAX_DAILY_HOURS = "MAX_DAILY_HOURS"
    NO_EARLY_MORNING = "NO_EARLY_MORNING"
    NO_LATE_EVENING = "NO_LATE_EVENING"


class ConstraintCategory(_StrEnum):
    """Constraint severity.

    HARD constraints are inviolable and are the exclusive authority of
    CP-SAT; no AI component may relax one (CLAUDE.md — Optimization
    principles). SESSION 2 assigns each constraint type its category.
    """

    HARD = "HARD"
    MEDIUM = "MEDIUM"
    SOFT = "SOFT"


# ---------------------------------------------------------------------------
# Schedules and scenarios
# ---------------------------------------------------------------------------


class ScheduleScope(_StrEnum):
    INSTITUTION = "INSTITUTION"
    FACULTY = "FACULTY"
    PROGRAM = "PROGRAM"


class ScheduleVersionStatus(_StrEnum):
    """At most one PUBLISHED version may exist per Schedule at any time
    (enforced by a partial unique index)."""

    DRAFT = "DRAFT"
    PROVISIONAL = "PROVISIONAL"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"

    @property
    def is_mutable(self) -> bool:
        """Only DRAFT versions may have their assignments edited in place."""
        return self is ScheduleVersionStatus.DRAFT


class ScenarioStatus(_StrEnum):
    OPEN = "OPEN"
    EVALUATED = "EVALUATED"
    PROMOTED = "PROMOTED"
    DISCARDED = "DISCARDED"


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


class OptimizationRunStatus(_StrEnum):
    """Progress states required by playbook SESSION 9."""

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    BUILDING_MODEL = "BUILDING_MODEL"
    SEARCHING_FEASIBLE = "SEARCHING_FEASIBLE"
    FEASIBLE_FOUND = "FEASIBLE_FOUND"
    OPTIMIZING = "OPTIMIZING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SolverStatus(_StrEnum):
    """Solver outcome, kept distinct from run progress.

    INFEASIBLE (proven no solution) must never be conflated with UNKNOWN or
    TIMEOUT (search gave up) — playbook SESSION 3.
    """

    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"


class RecommendationKind(_StrEnum):
    """Playbook SESSION 6 recommendation vocabulary."""

    MOVE_SESSION = "MOVE_SESSION"
    CHANGE_ROOM = "CHANGE_ROOM"
    CHANGE_INSTRUCTOR = "CHANGE_INSTRUCTOR"
    CREATE_SECTION = "CREATE_SECTION"
    USE_SHARED_OFFERING = "USE_SHARED_OFFERING"
    RELAX_PREFERENCE = "RELAX_PREFERENCE"
    PROPOSE_NEGOTIATION = "PROPOSE_NEGOTIATION"
    SPLIT_SECTION = "SPLIT_SECTION"
    MERGE_OFFERINGS = "MERGE_OFFERINGS"


class RecommendationStatus(_StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


# ---------------------------------------------------------------------------
# AI model registry
# ---------------------------------------------------------------------------


class AIModelStatus(_StrEnum):
    """Playbook SESSION 7. No model version becomes ACTIVE automatically;
    activation is a human action and at most one version per model may be
    ACTIVE (partial unique index)."""

    EXPERIMENTAL = "EXPERIMENTAL"
    TESTING = "TESTING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DISABLED = "DISABLED"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class ChangeRequestStatus(_StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COUNTERPROPOSED = "COUNTERPROPOSED"
    WITHDRAWN = "WITHDRAWN"
    APPLIED = "APPLIED"


class ApprovalAction(_StrEnum):
    """Playbook SESSION 12 action vocabulary."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    COUNTERPROPOSE = "COUNTERPROPOSE"
    REQUEST_AI_ALTERNATIVE = "REQUEST_AI_ALTERNATIVE"
    REQUEST_REOPTIMIZATION = "REQUEST_REOPTIMIZATION"


class NotificationKind(_StrEnum):
    PROVISIONAL_SCHEDULE = "PROVISIONAL_SCHEDULE"
    PUBLISHED_SCHEDULE = "PUBLISHED_SCHEDULE"
    SCHEDULE_CHANGED = "SCHEDULE_CHANGED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REQUEST_APPROVED = "REQUEST_APPROVED"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    COUNTERPROPOSAL = "COUNTERPROPOSAL"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    AI_RECOMMENDATION = "AI_RECOMMENDATION"
    OPTIMIZATION_COMPLETE = "OPTIMIZATION_COMPLETE"


class NotificationChannel(_StrEnum):
    """Only WEB is required initially (playbook SESSION 17); the others exist
    so the channel abstraction does not need a migration later."""

    WEB = "WEB"
    EMAIL = "EMAIL"
    PUSH = "PUSH"


class AuditAction(_StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REVOKE = "REVOKE"
    PUBLISH = "PUBLISH"
    ACTIVATE = "ACTIVATE"
    DEACTIVATE = "DEACTIVATE"
    IMPORT = "IMPORT"
    OPTIMIZE = "OPTIMIZE"
    LOGIN = "LOGIN"
