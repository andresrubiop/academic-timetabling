"""Who may do what, and the five things nobody may automate.

Framework-free: this is a decision table, and it has to be checkable by a
test, by the API layer, and eventually by an approval workflow that runs
somewhere else entirely. Importing FastAPI here would tie the institution's
authorisation rules to one web framework's request object.

The part that is not ordinary RBAC
==================================

CLAUDE.md lists five decisions that **an AI recommendation never
automatically approves**:

    academic equivalence; cross-program changes; cross-faculty changes;
    institutional policies; AI model activation.

`RESERVED_TO_HUMANS` is that list, as permissions. `authorise()` refuses any
of them whenever the actor is not a human, no matter what role the actor
carries. A service account with SUPER_ADMIN is still not a person, and the
rule is about *who decides*, not about how many privileges the decider holds.

This is the one place where adding a role to a table is not enough to grant
something — which is the point. A rule that a future maintainer can satisfy
by editing one row is a rule that will be satisfied by editing one row.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Mapping

from academic_ai.domain.enums import UserRole

if TYPE_CHECKING:  # pragma: no cover - types only
    from academic_ai.security.jurisdiction import Jurisdiction

__all__ = [
    "RESERVED_TO_HUMANS",
    "ROLE_PERMISSIONS",
    "ActorKind",
    "AuthorizationError",
    "Permission",
    "Principal",
    "authorise",
    "permissions_for",
]


class Permission(str, Enum):
    """One thing somebody might be allowed to do."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    # --- reading ---------------------------------------------------------
    VIEW_OWN_SCHEDULE = "VIEW_OWN_SCHEDULE"
    VIEW_PROGRAM_SCHEDULE = "VIEW_PROGRAM_SCHEDULE"
    VIEW_FACULTY_SCHEDULE = "VIEW_FACULTY_SCHEDULE"
    VIEW_INSTITUTION_SCHEDULE = "VIEW_INSTITUTION_SCHEDULE"
    VIEW_AUDIT_LOG = "VIEW_AUDIT_LOG"

    # --- planning --------------------------------------------------------
    EDIT_DRAFT_SCHEDULE = "EDIT_DRAFT_SCHEDULE"
    RUN_OPTIMIZATION = "RUN_OPTIMIZATION"
    PUBLISH_SCHEDULE = "PUBLISH_SCHEDULE"
    MANAGE_ENROLLMENT = "MANAGE_ENROLLMENT"
    MANAGE_CONSTRAINTS = "MANAGE_CONSTRAINTS"

    # --- administration --------------------------------------------------
    MANAGE_USERS = "MANAGE_USERS"
    MANAGE_INSTITUTION = "MANAGE_INSTITUTION"

    # --- the five CLAUDE.md reserves to humans ---------------------------
    APPROVE_ACADEMIC_EQUIVALENCE = "APPROVE_ACADEMIC_EQUIVALENCE"
    APPROVE_CROSS_PROGRAM_CHANGE = "APPROVE_CROSS_PROGRAM_CHANGE"
    APPROVE_CROSS_FACULTY_CHANGE = "APPROVE_CROSS_FACULTY_CHANGE"
    APPROVE_INSTITUTIONAL_POLICY = "APPROVE_INSTITUTIONAL_POLICY"
    ACTIVATE_AI_MODEL = "ACTIVATE_AI_MODEL"


#: CLAUDE.md, "Human authority", verbatim as permissions. A non-human
#: principal is refused these even holding SUPER_ADMIN.
RESERVED_TO_HUMANS = frozenset(
    {
        Permission.APPROVE_ACADEMIC_EQUIVALENCE,
        Permission.APPROVE_CROSS_PROGRAM_CHANGE,
        Permission.APPROVE_CROSS_FACULTY_CHANGE,
        Permission.APPROVE_INSTITUTIONAL_POLICY,
        Permission.ACTIVATE_AI_MODEL,
    }
)

_P = Permission

#: Cumulative by convention, but written out per role rather than by
#: inheritance. Inheritance chains make "what can a DIRECTOR do?" a question
#: you answer by tracing, and the answer to that question is the entire
#: purpose of the table.
ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = {
    UserRole.STUDENT: frozenset({_P.VIEW_OWN_SCHEDULE}),
    UserRole.TEACHER: frozenset(
        {_P.VIEW_OWN_SCHEDULE, _P.VIEW_PROGRAM_SCHEDULE}
    ),
    UserRole.COORDINATOR: frozenset(
        {
            _P.VIEW_OWN_SCHEDULE,
            _P.VIEW_PROGRAM_SCHEDULE,
            _P.EDIT_DRAFT_SCHEDULE,
            _P.RUN_OPTIMIZATION,
            _P.MANAGE_ENROLLMENT,
            _P.MANAGE_CONSTRAINTS,
        }
    ),
    UserRole.DIRECTOR: frozenset(
        {
            _P.VIEW_OWN_SCHEDULE,
            _P.VIEW_PROGRAM_SCHEDULE,
            _P.VIEW_FACULTY_SCHEDULE,
            _P.EDIT_DRAFT_SCHEDULE,
            _P.RUN_OPTIMIZATION,
            _P.PUBLISH_SCHEDULE,
            _P.MANAGE_ENROLLMENT,
            _P.MANAGE_CONSTRAINTS,
            _P.VIEW_AUDIT_LOG,
            # A director owns one program's academic content, so equivalence
            # within it is theirs. Anything crossing a program boundary is
            # not — that is the dean's, below.
            _P.APPROVE_ACADEMIC_EQUIVALENCE,
        }
    ),
    UserRole.DEAN: frozenset(
        {
            _P.VIEW_OWN_SCHEDULE,
            _P.VIEW_PROGRAM_SCHEDULE,
            _P.VIEW_FACULTY_SCHEDULE,
            _P.EDIT_DRAFT_SCHEDULE,
            _P.RUN_OPTIMIZATION,
            _P.PUBLISH_SCHEDULE,
            _P.MANAGE_ENROLLMENT,
            _P.MANAGE_CONSTRAINTS,
            _P.VIEW_AUDIT_LOG,
            _P.APPROVE_ACADEMIC_EQUIVALENCE,
            _P.APPROVE_CROSS_PROGRAM_CHANGE,
        }
    ),
    UserRole.ADMIN: frozenset(
        {
            _P.VIEW_OWN_SCHEDULE,
            _P.VIEW_PROGRAM_SCHEDULE,
            _P.VIEW_FACULTY_SCHEDULE,
            _P.VIEW_INSTITUTION_SCHEDULE,
            _P.VIEW_AUDIT_LOG,
            _P.EDIT_DRAFT_SCHEDULE,
            _P.RUN_OPTIMIZATION,
            _P.MANAGE_ENROLLMENT,
            _P.MANAGE_CONSTRAINTS,
            _P.MANAGE_USERS,
            # SESSION 14: institutions, faculties, departments, programs,
            # campuses, buildings, rooms, curricula and courses are reference
            # data an administrator maintains, not an academic decision.
            # MANAGE_INSTITUTION was declared in SESSION 8 and held by nobody
            # until this session's CRUD work needed a role below SUPER_ADMIN
            # who could actually use it.
            _P.MANAGE_INSTITUTION,
            # Deliberately NOT PUBLISH_SCHEDULE: administering the platform
            # and owning an academic decision are different jobs, and an
            # operator restoring a backup should not be able to publish a
            # timetable on the way past.
        }
    ),
    UserRole.SUPER_ADMIN: frozenset(Permission),
}


class ActorKind(str, Enum):
    """What kind of thing is asking.

    ``SERVICE`` is the optimizer worker, a scheduled job, an importer.
    ``AI_MODEL`` is a registered model acting through a provider contract.
    Neither is a person, and `RESERVED_TO_HUMANS` turns on exactly that
    distinction.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    HUMAN = "HUMAN"
    SERVICE = "SERVICE"
    AI_MODEL = "AI_MODEL"

    @property
    def is_human(self) -> bool:
        return self is ActorKind.HUMAN


@dataclass(frozen=True, slots=True)
class Principal:
    """Whoever is making this request."""

    user_id: str
    role: UserRole
    kind: ActorKind = ActorKind.HUMAN
    is_active: bool = True
    #: Scope handles, populated from `user_scope_assignment` since SESSION 23.
    #: Authorisation answers "may this *kind* of thing happen"; `jurisdiction`
    #: answers whether it may happen *here*. Conflating the two produces a
    #: role that can edit every faculty because it could edit one, which is
    #: what these fields being permanently empty meant from SESSION 8 to 23.
    #:
    #: `program_ids` is already expanded: a dean assigned a faculty carries
    #: every programme in it.
    program_ids: frozenset[str] = frozenset()
    faculty_ids: frozenset[str] = frozenset()
    #: Set while an administrator-issued temporary password is still in place.
    #: `require()` refuses every permission until it is changed, so the only
    #: reachable endpoints are the ones that let it be changed.
    must_change_password: bool = False

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    @property
    def jurisdiction(self) -> "Jurisdiction":
        """Where this principal's authority reaches.

        Derived rather than stored, so it cannot disagree with the role: a
        `Principal` built with a coordinator's role and an admin's breadth
        would be a privilege escalation constructible by a typo.
        """
        from academic_ai.security.jurisdiction import Jurisdiction, breadth_for

        return Jurisdiction(
            breadth=breadth_for(self.role),
            faculty_ids=self.faculty_ids,
            program_ids=self.program_ids,
        )


class AuthorizationError(PermissionError):
    """Refused. Says which permission and why, never what would have worked."""


def permissions_for(role: UserRole) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def authorise(principal: Principal, permission: Permission) -> None:
    """Raise unless this principal may do this. Returns ``None`` on success.

    Raising rather than returning a bool on purpose: a caller who forgets to
    check a returned ``False`` has written an authorisation bypass that looks
    exactly like working code, and it will pass every test that does not
    specifically try to break in.
    """
    if not principal.is_active:
        raise AuthorizationError(
            f"{principal.user_id} is deactivated and may not {permission.value}"
        )

    if permission in RESERVED_TO_HUMANS and not principal.kind.is_human:
        raise AuthorizationError(
            f"{permission.value} is reserved to a human decision-maker; "
            f"{principal.user_id} is a {principal.kind.value}. This is not a "
            "missing grant — no role makes it available to a non-human "
            "(CLAUDE.md, Human authority)."
        )

    if permission not in principal.permissions:
        raise AuthorizationError(
            f"{principal.role.value} may not {permission.value}"
        )
