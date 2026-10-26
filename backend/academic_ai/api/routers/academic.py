"""Academic administration: institutions down to enrollments (SESSION 14).

Every entity here is registered as a `CrudConfig` and built by the one
generic router in `api/crud.py`. Nothing entity-specific is hand-written
beyond the configuration — search fields, filter fields, sort fields, and how
"delete" should behave for that table's lifecycle.

Retire semantics, entity by entity
===================================

Most tables carry `status: LifecycleStatus` (DRAFT/ACTIVE/SUSPENDED/RETIRED)
and retire straightforwardly. A few carry a different vocabulary because they
are not simple reference data:

* `CourseEquivalence` / `AcademicCompatibilityGroup` carry an *approval*
  status (PROPOSED/UNDER_REVIEW/APPROVED/REJECTED/REVOKED). Deleting an
  approved equivalence does not mean it never existed — a curriculum built on
  it needs to know it was withdrawn, not silently vanished — so DELETE moves
  it to REVOKED.
* `AcademicPeriod` retires to ARCHIVED, its own vocabulary for "over".
* `CourseOffering` / `Section` retire to CANCELLED, theirs.
* `Enrollment` retires to DROPPED — a student leaving a class is a DROPPED
  enrolment, not a deleted one, which is also what ADR-0005 (enrollment as an
  immutable record) already requires.
* `PlanningConstraint` has `is_active: bool` rather than a status enum;
  DELETE clears it.
* `ClassSession` has no lifecycle column of its own — it is a definition
  ("this section meets N times a week"), not a record with history to
  preserve — so DELETE is a real delete. It is still safe: a session already
  scheduled is referenced by `schedule_assignment` under the database's
  default `RESTRICT`, so deleting one that has been placed is refused with a
  409, not silently orphaned.
"""

from __future__ import annotations

from fastapi import APIRouter

from academic_ai.api.crud import (
    CrudConfig,
    RetireConfig,
    ScopeRule,
    build_crud_router,
)
from academic_ai.domain.enums import (
    AcademicApprovalStatus,
    AcademicPeriodStatus,
    CourseStatus,
    EnrollmentStatus,
    LifecycleStatus,
    OfferingStatus,
    SectionStatus,
)
from academic_ai.infrastructure.db.models import (
    AcademicCompatibilityGroup,
    AcademicPeriod,
    Building,
    Campus,
    ClassSession,
    Course,
    CourseEquivalence,
    CourseOffering,
    CourseOfferingProgram,
    Curriculum,
    CurriculumVersion,
    Department,
    Enrollment,
    Faculty,
    Institution,
    PlanningConstraint,
    Program,
    Room,
    Section,
    Student,
    Teacher,
)
from academic_ai.security.rbac import Permission

__all__ = ["router"]

_LIFECYCLE_ACTIVE = frozenset(
    {LifecycleStatus.DRAFT, LifecycleStatus.ACTIVE, LifecycleStatus.SUSPENDED}
)
_lifecycle_retire = RetireConfig(
    "status", LifecycleStatus.RETIRED, _LIFECYCLE_ACTIVE
)

_CONFIGS: tuple[CrudConfig, ...] = (
    CrudConfig(
        model=Institution,
        prefix="/institutions",
        tags=("institutions",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status",),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Faculty,
        prefix="/faculties",
        tags=("faculties",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "institution_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Department,
        scope=ScopeRule(column="faculty_id", kind="faculty"),
        prefix="/departments",
        tags=("departments",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "faculty_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Program,
        scope=ScopeRule(column="faculty_id", kind="faculty"),
        prefix="/programs",
        tags=("programs",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name", "degree_title"),
        filter_fields=("status", "faculty_id", "department_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Curriculum,
        scope=ScopeRule(column="program_id", kind="program"),
        prefix="/curricula",
        tags=("curricula",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "program_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=CurriculumVersion,
        scope=ScopeRule(
            column="curriculum_id",
            kind="program",
            parent=Curriculum,
            parent_column="program_id",
        ),
        prefix="/curriculum-versions",
        tags=("curricula",),
        permission=Permission.MANAGE_INSTITUTION,
        filter_fields=("status", "curriculum_id"),
        sortable_fields=("version_number", "valid_from", "id"),
        default_sort="-version_number",
        retire=_lifecycle_retire,
        validate_create=lambda data: _require_ordered_dates(
            data.get("valid_from"), data.get("valid_until")
        ),
        validate_update=lambda data: _require_ordered_dates(
            data.get("valid_from"), data.get("valid_until")
        ),
    ),
    CrudConfig(
        model=Course,
        scope=ScopeRule(
            column="department_id",
            kind="faculty",
            parent=Department,
            parent_column="faculty_id",
        ),
        prefix="/courses",
        tags=("courses",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "department_id", "institution_id"),
        sortable_fields=("code", "name", "credits", "id"),
        default_sort="code",
        retire=RetireConfig(
            "status",
            CourseStatus.RETIRED,
            frozenset({CourseStatus.DRAFT, CourseStatus.ACTIVE}),
        ),
    ),
    CrudConfig(
        model=CourseEquivalence,
        scope=ScopeRule(column="scope_program_id", kind="program"),
        prefix="/course-equivalences",
        tags=("courses",),
        permission=Permission.MANAGE_INSTITUTION,
        filter_fields=("status", "source_course_id", "target_course_id"),
        sortable_fields=("id",),
        # An equivalence *approval* is one of the five decisions CLAUDE.md
        # reserves to humans (APPROVE_ACADEMIC_EQUIVALENCE), so registering a
        # proposal here never sets status to APPROVED -- validate_create
        # refuses it, and the actual approval is SESSION 12's workflow.
        validate_create=lambda data: _refuse_pre_approved(
            data.get("status"), AcademicApprovalStatus
        ),
        retire=RetireConfig(
            "status",
            AcademicApprovalStatus.REVOKED,
            frozenset(
                {
                    AcademicApprovalStatus.PROPOSED,
                    AcademicApprovalStatus.UNDER_REVIEW,
                    AcademicApprovalStatus.APPROVED,
                }
            ),
        ),
    ),
    CrudConfig(
        model=AcademicCompatibilityGroup,
        prefix="/compatibility-groups",
        tags=("courses",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "institution_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        validate_create=lambda data: _refuse_pre_approved(
            data.get("status"), AcademicApprovalStatus
        ),
        retire=RetireConfig(
            "status",
            AcademicApprovalStatus.REVOKED,
            frozenset(
                {
                    AcademicApprovalStatus.PROPOSED,
                    AcademicApprovalStatus.UNDER_REVIEW,
                    AcademicApprovalStatus.APPROVED,
                }
            ),
        ),
    ),
    CrudConfig(
        model=Teacher,
        scope=ScopeRule(
            column="department_id",
            kind="faculty",
            parent=Department,
            parent_column="faculty_id",
        ),
        prefix="/teachers",
        tags=("people",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "full_name", "email"),
        filter_fields=("status", "department_id", "contract_type"),
        sortable_fields=("code", "full_name", "id"),
        default_sort="full_name",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Student,
        scope=ScopeRule(column="program_id", kind="program"),
        prefix="/students",
        tags=("people",),
        permission=Permission.MANAGE_ENROLLMENT,
        search_fields=("code", "full_name", "email"),
        filter_fields=("status", "program_id"),
        sortable_fields=("code", "full_name", "id"),
        default_sort="full_name",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Campus,
        prefix="/campuses",
        tags=("facilities",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "institution_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Building,
        prefix="/buildings",
        tags=("facilities",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "campus_id"),
        sortable_fields=("code", "name", "id"),
        default_sort="code",
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=Room,
        prefix="/rooms",
        tags=("facilities",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "building_id", "campus_id", "room_type"),
        sortable_fields=("code", "name", "capacity", "id"),
        default_sort="code",
        validate_create=lambda data: _require_positive(data.get("capacity"), "capacity"),
        validate_update=lambda data: _require_positive(data.get("capacity"), "capacity"),
        retire=_lifecycle_retire,
    ),
    CrudConfig(
        model=AcademicPeriod,
        prefix="/periods",
        tags=("periods",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code", "name"),
        filter_fields=("status", "institution_id"),
        sortable_fields=("code", "start_date", "id"),
        default_sort="-start_date",
        validate_create=lambda data: _require_ordered_dates(
            data.get("start_date"), data.get("end_date"), required=True
        ),
        validate_update=lambda data: _require_ordered_dates(
            data.get("start_date"), data.get("end_date")
        ),
        retire=RetireConfig(
            "status",
            AcademicPeriodStatus.ARCHIVED,
            frozenset(
                {
                    AcademicPeriodStatus.PLANNING,
                    AcademicPeriodStatus.ACTIVE,
                    AcademicPeriodStatus.CLOSED,
                }
            ),
        ),
    ),
    CrudConfig(
        model=CourseOffering,
        scope=ScopeRule(
            column="id",
            kind="program",
            link=CourseOfferingProgram,
            link_key="course_offering_id",
            link_owner="program_id",
        ),
        prefix="/offerings",
        tags=("offerings",),
        permission=Permission.MANAGE_INSTITUTION,
        filter_fields=("status", "course_id", "academic_period_id", "campus_id"),
        sortable_fields=("id",),
        retire=RetireConfig(
            "status",
            OfferingStatus.CANCELLED,
            frozenset(
                {OfferingStatus.PLANNED, OfferingStatus.OPEN, OfferingStatus.CLOSED}
            ),
        ),
    ),
    CrudConfig(
        model=Section,
        scope=ScopeRule(
            column="course_offering_id",
            kind="program",
            link=CourseOfferingProgram,
            link_key="course_offering_id",
            link_owner="program_id",
        ),
        prefix="/sections",
        tags=("offerings",),
        permission=Permission.MANAGE_INSTITUTION,
        search_fields=("code",),
        filter_fields=("status", "course_offering_id", "academic_period_id"),
        sortable_fields=("code", "id"),
        default_sort="code",
        validate_create=lambda data: _require_positive(data.get("capacity"), "capacity"),
        validate_update=lambda data: _require_positive(data.get("capacity"), "capacity"),
        retire=RetireConfig(
            "status",
            SectionStatus.CANCELLED,
            frozenset(
                {SectionStatus.PLANNED, SectionStatus.OPEN, SectionStatus.CLOSED}
            ),
        ),
    ),
    CrudConfig(
        model=ClassSession,
        scope=ScopeRule(
            column="section_id",
            kind="program",
            parent=Section,
            parent_column="course_offering_id",
            link=CourseOfferingProgram,
            link_key="course_offering_id",
            link_owner="program_id",
        ),
        prefix="/sessions",
        tags=("offerings",),
        permission=Permission.MANAGE_INSTITUTION,
        filter_fields=("section_id", "academic_period_id", "session_type"),
        sortable_fields=("sequence_number", "id"),
        default_sort="sequence_number",
        validate_create=lambda data: _require_positive(
            data.get("duration_slots"), "duration_slots"
        ),
        validate_update=lambda data: _require_positive(
            data.get("duration_slots"), "duration_slots"
        ),
        # No status column: this is a definition, not a record with history
        # to preserve. A session already placed is protected by the database's
        # own RESTRICT foreign key from schedule_assignment, not by this layer.
        retire=None,
    ),
    CrudConfig(
        model=Enrollment,
        scope=ScopeRule(
            column="course_offering_id",
            kind="program",
            link=CourseOfferingProgram,
            link_key="course_offering_id",
            link_owner="program_id",
        ),
        prefix="/enrollments",
        tags=("enrollments",),
        permission=Permission.MANAGE_ENROLLMENT,
        filter_fields=("status", "student_id", "course_offering_id", "academic_period_id"),
        sortable_fields=("id",),
        retire=RetireConfig(
            "status",
            EnrollmentStatus.DROPPED,
            frozenset(
                {
                    EnrollmentStatus.REQUESTED,
                    EnrollmentStatus.ENROLLED,
                    EnrollmentStatus.WAITLISTED,
                }
            ),
        ),
    ),
    CrudConfig(
        model=PlanningConstraint,
        scope=ScopeRule(column="scope_program_id", kind="program"),
        prefix="/constraints",
        tags=("constraints",),
        permission=Permission.MANAGE_CONSTRAINTS,
        filter_fields=("is_active", "constraint_type_id", "academic_period_id"),
        sortable_fields=("id",),
        retire=RetireConfig("is_active", False, frozenset({True})),
    ),
)


def _require_ordered_dates(start, end, *, required: bool = False) -> None:
    """`end` must not be before `start`.

    Applied to both AcademicPeriod (start_date/end_date) and
    CurriculumVersion (valid_from/valid_until) — same shape, same mistake to
    catch before it becomes a 500 from a CHECK constraint the administrator
    filling in the form never sees the wording of.
    """
    if required and start is None:
        raise ValueError("a start date is required")
    if start is not None and end is not None and end < start:
        raise ValueError("the end date cannot be before the start date")


def _require_positive(value, field_name: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")


def _refuse_pre_approved(status_value, enum_cls) -> None:
    """A proposal cannot be filed already approved.

    APPROVE_ACADEMIC_EQUIVALENCE is one of the five decisions CLAUDE.md
    reserves to humans, and a review that happens through this form field
    rather than through SESSION 12's approval workflow is the same rule
    broken through the back door.
    """
    if status_value is None:
        return
    approved = getattr(enum_cls, "APPROVED", None)
    if approved is not None and str(status_value) == str(approved.value):
        raise ValueError(
            "a proposal cannot be created already APPROVED; approval goes "
            "through the change-request workflow, not this field"
        )


router = APIRouter()
for _config in _CONFIGS:
    router.include_router(build_crud_router(_config))
