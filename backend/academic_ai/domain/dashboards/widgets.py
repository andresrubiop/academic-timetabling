"""What a dashboard is made of, and who may see each piece.

A dashboard is where a number gets seen out of context, which makes it the
easiest place in this project to publish something misleading. Three rules are
built into the types rather than left to whoever writes the next widget.

**Permission decides, the role only suggests.** `ROLE_DASHBOARDS` says what a
DIRECTOR's screen is *for*; `Permission` says what this particular director may
actually see. Where the two disagree the permission wins and the widget is
simply absent -- not an error, not an empty box labelled with something they
cannot have. A dashboard is assembled from what the viewer holds, so adding a
widget to a role's layout can never grant access to its data.

**A metric that could not be computed says so.** `Metric.unavailable()` exists
because "0 conflicts" and "we did not check for conflicts" render identically
and mean opposite things. The first is reassurance; the second is silence
wearing reassurance's clothes.

**Objective values are per tier, never summed.** ADR-0010 rejects a cross-tier
exchange rate, and a headline figure of "4,004,000,000" reintroduces one
visually -- a single number invites comparison, and comparing across tiers is
exactly what the lexicographic objective exists to forbid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from academic_ai.domain.enums import UserRole
from academic_ai.security.rbac import Permission

__all__ = [
    "ROLE_DASHBOARDS",
    "WIDGETS",
    "Metric",
    "Severity",
    "Widget",
    "WidgetKind",
    "widgets_for",
]


class WidgetKind(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    #: A single number, with a label that says what it counts.
    COUNT = "COUNT"
    #: Named rows. Used wherever a count would tell somebody to investigate
    #: without telling them what.
    LIST = "LIST"
    #: Health with a reason attached, never a bare colour.
    STATUS = "STATUS"
    #: Objective values, one per tier. Never collapsed.
    TIER_BREAKDOWN = "TIER_BREAKDOWN"


class Severity(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    OK = "OK"
    #: Solvable, but somebody should know. SESSION 6's whole category: 44
    #: sessions with nobody committed to teach them is not an error and is
    #: not nothing.
    ATTENTION = "ATTENTION"
    PROBLEM = "PROBLEM"


@dataclass(frozen=True, slots=True)
class Widget:
    """One panel, and the permission that gates it."""

    code: str
    title: str
    kind: WidgetKind
    permission: Permission
    description: str = ""
    #: Whether this widget is inherently about the viewer rather than about
    #: the institution -- a teacher's own load, a student's own enrolments.
    #: Scoped widgets are safe for roles that may see nothing else.
    is_personal: bool = False


@dataclass(frozen=True, slots=True)
class Metric:
    """One rendered value, or an honest account of why there isn't one."""

    widget_code: str
    title: str
    kind: WidgetKind
    value: Any = None
    #: What the number counts, in words. "48" means nothing; "48 sessions
    #: still unplaced" means something.
    unit: str = ""
    severity: Severity = Severity.OK
    #: Named rows for a LIST, tier rows for a TIER_BREAKDOWN.
    rows: tuple[Mapping[str, Any], ...] = ()
    detail: str = ""
    #: True when the value could not be computed. Rendered as "not available",
    #: never as zero.
    is_unavailable: bool = False

    @classmethod
    def unavailable(cls, widget: Widget, reason: str) -> Metric:
        """A metric that could not be computed.

        Its own state rather than a zero, because "0 conflicts" and "we did
        not check" render identically and mean opposite things.
        """
        return cls(
            widget_code=widget.code,
            title=widget.title,
            kind=widget.kind,
            value=None,
            severity=Severity.ATTENTION,
            detail=reason,
            is_unavailable=True,
        )


_P = Permission
_K = WidgetKind

WIDGETS: Mapping[str, Widget] = {
    w.code: w
    for w in (
        # --- system ------------------------------------------------------
        Widget(
            "system_health", "System health", _K.STATUS, _P.MANAGE_INSTITUTION,
            "Database and job queue, with the reason when either is not well.",
        ),
        Widget(
            "user_accounts", "User accounts", _K.COUNT, _P.MANAGE_USERS,
            "Active accounts, by role.",
        ),
        Widget(
            "ai_models", "AI models", _K.LIST, _P.ACTIVATE_AI_MODEL,
            "Registered models and their status. Nothing is ACTIVE unless a "
            "named person activated it.",
        ),
        Widget(
            "optimization_runs", "Optimization runs", _K.LIST, _P.RUN_OPTIMIZATION,
            "Recent runs, their phase, and what the solver concluded.",
        ),
        Widget(
            "audit_activity", "Audit activity", _K.COUNT, _P.VIEW_AUDIT_LOG,
            "Recorded changes in the last seven days.",
        ),
        # --- academic structure -------------------------------------------
        Widget(
            "academic_structure", "Academic structure", _K.COUNT,
            _P.MANAGE_INSTITUTION,
            "Faculties, programmes, courses and rooms currently active.",
        ),
        Widget(
            "periods", "Academic periods", _K.LIST, _P.MANAGE_INSTITUTION,
            "Periods and where each one is in its lifecycle.",
        ),
        Widget(
            "resources", "Teaching resources", _K.COUNT, _P.MANAGE_INSTITUTION,
            "Rooms and teachers available to be scheduled.",
        ),
        # --- planning ------------------------------------------------------
        Widget(
            "schedule_health", "Schedule health", _K.STATUS, _P.VIEW_PROGRAM_SCHEDULE,
            "What the feasibility analyser says about the current period -- "
            "including the warnings that are not errors.",
        ),
        Widget(
            "objective_tiers", "Objective, tier by tier", _K.TIER_BREAKDOWN,
            _P.RUN_OPTIMIZATION,
            "The last run's value for each tier. Never one number: a single "
            "figure would invite comparing tiers the objective forbids "
            "comparing (ADR-0010).",
        ),
        Widget(
            "student_conflicts", "Student conflicts", _K.LIST,
            _P.VIEW_PROGRAM_SCHEDULE,
            "Students whose classes clash, named. A count tells somebody to "
            "investigate; a name tells them what to do.",
        ),
        Widget(
            "unplaced_sessions", "Unplaced sessions", _K.COUNT,
            _P.VIEW_PROGRAM_SCHEDULE,
            "Sessions the current schedule has not placed.",
        ),
        # --- governance -----------------------------------------------------
        Widget(
            "pending_approvals", "Waiting for you", _K.LIST, _P.VIEW_PROGRAM_SCHEDULE,
            "Change requests whose next step is yours.",
        ),
        Widget(
            "my_requests", "Your requests", _K.LIST, _P.EDIT_DRAFT_SCHEDULE,
            "Change requests you raised, and where each one has got to.",
        ),
        Widget(
            "scenarios", "Scenarios", _K.LIST, _P.RUN_OPTIMIZATION,
            "What-if scenarios explored against the current schedule.",
        ),
        # --- faculty / programme comparison ---------------------------------
        Widget(
            "faculty_kpis", "Faculty indicators", _K.COUNT, _P.VIEW_FACULTY_SCHEDULE,
            "Programmes, offerings and enrolment across the faculty.",
        ),
        Widget(
            "program_comparison", "Programmes side by side", _K.LIST,
            _P.VIEW_FACULTY_SCHEDULE,
            "Each programme's offerings and unresolved conflicts, so one that "
            "is struggling is visible next to one that is not.",
        ),
        # --- personal --------------------------------------------------------
        Widget(
            "my_profile", "Your profile", _K.STATUS, _P.VIEW_OWN_SCHEDULE,
            "Who the platform thinks you are.", is_personal=True,
        ),
        Widget(
            "my_load", "Your teaching load", _K.COUNT, _P.VIEW_OWN_SCHEDULE,
            "Sessions assigned to you, against your declared limits.",
            is_personal=True,
        ),
        Widget(
            "my_availability", "Your availability", _K.STATUS, _P.VIEW_OWN_SCHEDULE,
            "What you have told the platform about when you can teach -- and "
            "whether you have told it anything at all.",
            is_personal=True,
        ),
        Widget(
            "my_schedule", "Your schedule", _K.LIST, _P.VIEW_OWN_SCHEDULE,
            "Published where one is published, provisional otherwise, and "
            "labelled so the two are never confused.",
            is_personal=True,
        ),
        Widget(
            "my_enrollments", "Your courses", _K.LIST, _P.VIEW_OWN_SCHEDULE,
            "What you are enrolled in this period.", is_personal=True,
        ),
        Widget(
            "my_repeated_courses", "Courses you are repeating", _K.LIST,
            _P.VIEW_OWN_SCHEDULE,
            "Shown separately because a repeat is what makes a clash hardest "
            "to resolve, and you should see it before the timetable is fixed.",
            is_personal=True,
        ),
        Widget(
            "my_conflicts", "Clashes in your schedule", _K.LIST, _P.VIEW_OWN_SCHEDULE,
            "Classes you are enrolled in that overlap.", is_personal=True,
        ),
    )
}


#: What each role's screen is *for*. A preference, not an authority: every
#: widget is still gated by its own permission, so listing one here cannot
#: grant access to its data.
ROLE_DASHBOARDS: Mapping[UserRole, tuple[str, ...]] = {
    UserRole.SUPER_ADMIN: (
        "system_health", "user_accounts", "ai_models", "optimization_runs",
        "audit_activity", "academic_structure", "periods",
    ),
    UserRole.ADMIN: (
        "academic_structure", "user_accounts", "periods", "resources",
        "system_health",
    ),
    UserRole.DEAN: (
        "faculty_kpis", "student_conflicts", "pending_approvals",
        "program_comparison", "schedule_health",
    ),
    UserRole.DIRECTOR: (
        "schedule_health", "student_conflicts", "unplaced_sessions",
        "pending_approvals", "scenarios", "objective_tiers",
    ),
    UserRole.COORDINATOR: (
        "schedule_health", "unplaced_sessions", "student_conflicts",
        "optimization_runs", "objective_tiers", "my_requests",
    ),
    UserRole.TEACHER: (
        "my_profile", "my_load", "my_availability", "my_schedule",
    ),
    UserRole.STUDENT: (
        "my_profile", "my_enrollments", "my_repeated_courses",
        "my_schedule", "my_conflicts",
    ),
}


def widgets_for(role: UserRole, permissions: frozenset[Permission]) -> tuple[Widget, ...]:
    """The widgets this viewer actually gets.

    The role's layout filtered by what they hold. A widget they cannot see is
    **absent**, not present-and-empty: an empty panel labelled "Faculty
    indicators" tells a coordinator that faculty indicators exist and they are
    not allowed to see them, which is a small leak and an unkind one.
    """
    return tuple(
        widget
        for code in ROLE_DASHBOARDS.get(role, ())
        if (widget := WIDGETS.get(code)) is not None
        and widget.permission in permissions
    )
