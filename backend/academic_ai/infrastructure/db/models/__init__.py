"""SQLAlchemy models for the academic domain.

Importing this package registers every table on ``Base.metadata``. Alembic's
``env.py`` and the test fixtures both depend on that, so any new model module
must be imported here — `tests/test_schema_registry.py` fails if one is
missing.
"""

from academic_ai.infrastructure.db.models.ai_registry import (
    AIModel,
    AIModelVersion,
    FeatureSchema,
    ModelDeployment,
    ModelEvaluation,
)
from academic_ai.infrastructure.db.models.audit import AuditLog, Notification
from academic_ai.infrastructure.db.models.auth import (
    UserCredential,
    UserScopeAssignment,
    UserSession,
)
from academic_ai.infrastructure.db.models.availability import Availability, Preference
from academic_ai.infrastructure.db.models.constraints import (
    ConstraintType,
    PlanningConstraint,
)
from academic_ai.infrastructure.db.models.course import (
    AcademicCompatibilityGroup,
    AcademicCompatibilityGroupMember,
    Course,
    CourseEquivalence,
)
from academic_ai.infrastructure.db.models.curriculum import (
    Curriculum,
    CurriculumCourse,
    CurriculumLevel,
    CurriculumVersion,
)
from academic_ai.infrastructure.db.models.enrollment import Enrollment
from academic_ai.infrastructure.db.models.facilities import (
    Building,
    Campus,
    Room,
    RoomFeature,
    RoomFeatureAssignment,
    TravelTime,
)
from academic_ai.infrastructure.db.models.governance import (
    Approval,
    ApprovalStep,
    ApprovalWorkflow,
    ChangeRequest,
    ReoptimizationRequest,
)
from academic_ai.infrastructure.db.models.offering import (
    ClassSessionAllowedRoom,
    ClassSession,
    CourseOffering,
    CourseOfferingProgram,
    Section,
    SectionTeacher,
    SessionCoTeachingGroup,
    SessionRequiredFeature,
    SessionTeacher,
)
from academic_ai.infrastructure.db.models.optimization import (
    OptimizationMetric,
    OptimizationRun,
    Recommendation,
)
from academic_ai.infrastructure.db.models.organization import (
    Department,
    Faculty,
    Institution,
    Program,
)
from academic_ai.infrastructure.db.models.people import (
    Student,
    StudentCohort,
    StudentCohortMembership,
    Teacher,
    User,
)
from academic_ai.infrastructure.db.models.period import AcademicPeriod, Timeslot
from academic_ai.infrastructure.db.models.scenario import Scenario, ScenarioPerturbation
from academic_ai.infrastructure.db.models.schedule import (
    Schedule,
    ScheduleAssignment,
    ScheduleVersion,
)

__all__ = [
    "AIModel",
    "AIModelVersion",
    "UserCredential",
    "UserScopeAssignment",
    "UserSession",
    "FeatureSchema",
    "ModelDeployment",
    "ModelEvaluation",
    "ReoptimizationRequest",
    "AcademicCompatibilityGroup",
    "AcademicCompatibilityGroupMember",
    "AcademicPeriod",
    "Approval",
    "ApprovalStep",
    "ApprovalWorkflow",
    "AuditLog",
    "Availability",
    "Building",
    "Campus",
    "ChangeRequest",
    "ClassSession",
    "ClassSessionAllowedRoom",
    "ConstraintType",
    "Course",
    "CourseEquivalence",
    "CourseOffering",
    "CourseOfferingProgram",
    "Curriculum",
    "CurriculumCourse",
    "CurriculumLevel",
    "CurriculumVersion",
    "Department",
    "Enrollment",
    "Faculty",
    "Institution",
    "Notification",
    "OptimizationMetric",
    "OptimizationRun",
    "PlanningConstraint",
    "Preference",
    "Program",
    "Recommendation",
    "Room",
    "RoomFeature",
    "RoomFeatureAssignment",
    "Scenario",
    "ScenarioPerturbation",
    "Schedule",
    "ScheduleAssignment",
    "ScheduleVersion",
    "Section",
    "SectionTeacher",
    "SessionCoTeachingGroup",
    "SessionRequiredFeature",
    "SessionTeacher",
    "Student",
    "StudentCohort",
    "StudentCohortMembership",
    "Teacher",
    "Timeslot",
    "TravelTime",
    "User",
]
