"""Computing what each widget shows.

Every metric here follows the same two rules, and they are the reason this is
a hand-written dispatch rather than a generic query builder.

**Say when you do not know.** A widget whose data cannot be computed returns
`Metric.unavailable(...)` with a reason. Returning zero would be indefensible:
"0 student conflicts" and "we have no schedule to check" render the same and
mean opposite things, and the first is the one a dean would act on.

**Never publish a number that invites the wrong comparison.** Objective values
come back one row per tier and are never summed -- ADR-0010 rejects the
exchange rate a headline figure would imply. Schedule health carries the
feasibility analyser's *warnings* as well as its errors, because a green panel
that hides "44 sessions with nobody committed to teach them" is precisely the
false reassurance SESSION 6 exists to prevent.

Scoping is not done here. `widgets_for` has already removed anything the
viewer may not see; what remains is the honest computation of what they may.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.domain.dashboards import (
    Metric,
    Severity,
    Widget,
    WidgetKind,
    widgets_for,
)
from academic_ai.domain.enums import (
    AIModelStatus,
    ChangeRequestStatus,
    LifecycleStatus,
    OptimizationRunStatus,
)
from academic_ai.security.rbac import Principal

__all__ = ["build_dashboard"]

log = logging.getLogger("academic_ai.dashboards")

#: How many rows a LIST widget carries. A dashboard is a glance; anything
#: longer belongs on the page the widget links to.
_LIST_LIMIT = 10


def build_dashboard(
    session: Session, principal: Principal, *, now: dt.datetime | None = None
) -> tuple[Metric, ...]:
    """Every metric this viewer is entitled to, computed.

    Widgets are selected by permission first, so a failure computing one
    cannot expose another: an exception is caught per widget and becomes an
    unavailable metric rather than a 500 that takes the whole dashboard down.
    A dean whose faculty query is slow should still see their approvals.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    metrics: list[Metric] = []

    for widget in widgets_for(principal.role, principal.permissions):
        try:
            metrics.append(_compute(session, widget, principal, now))
        except Exception:  # noqa: BLE001 - one bad widget must not blank the page
            log.exception(
                "dashboard widget failed",
                extra={"widget": widget.code, "user_id": principal.user_id},
            )
            metrics.append(
                Metric.unavailable(
                    widget, "this panel could not be loaded; the rest is unaffected"
                )
            )
    return tuple(metrics)


def _compute(
    session: Session, widget: Widget, principal: Principal, now: dt.datetime
) -> Metric:
    handler = _HANDLERS.get(widget.code)
    if handler is None:  # pragma: no cover - every widget has one
        return Metric.unavailable(widget, "no handler is registered for this panel")
    return handler(session, widget, principal, now)


def _count(model, session: Session, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for field, value in filters.items():
        stmt = stmt.where(getattr(model, field) == value)
    return int(session.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


def _system_health(session: Session, widget: Widget, principal, now) -> Metric:
    from sqlalchemy import text

    rows: list[Mapping[str, Any]] = []
    severity = Severity.OK
    try:
        session.execute(text("SELECT 1"))
        rows.append({"component": "database", "status": "ok"})
    except Exception as exc:  # noqa: BLE001
        rows.append({"component": "database", "status": type(exc).__name__})
        severity = Severity.PROBLEM

    # Never a bare colour: a red light with no reason sends an operator
    # hunting, and a green one with no reason is worth nothing.
    return Metric(
        widget.code, widget.title, widget.kind,
        value="ok" if severity is Severity.OK else "degraded",
        severity=severity, rows=tuple(rows),
        detail="; ".join(f"{r['component']}: {r['status']}" for r in rows),
    )


def _user_accounts(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import User

    total = _count(User, session, is_active=True)
    by_role = session.execute(
        select(User.primary_role, func.count())
        .where(User.is_active.is_(True))
        .group_by(User.primary_role)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=total, unit="active accounts",
        rows=tuple({"role": r.value, "count": c} for r, c in by_role),
    )


def _ai_models(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import AIModel, AIModelVersion

    rows = session.execute(
        select(AIModel.code, AIModelVersion.version, AIModelVersion.status)
        .join(AIModelVersion, AIModelVersion.ai_model_id == AIModel.id)
        .order_by(AIModel.code)
        .limit(_LIST_LIMIT)
    ).all()
    active = sum(1 for r in rows if r.status is AIModelStatus.ACTIVE)
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(rows), unit="registered versions",
        rows=tuple(
            {"model": c, "version": v, "status": s.value} for c, v, s in rows
        ),
        # Worth stating rather than leaving to inference: no model being
        # active is the normal, designed state, not a gap.
        detail=(
            f"{active} active"
            if active
            else "nothing is active; the heuristic is running, which is the "
            "normal state -- no model activates automatically"
        ),
    )


def _optimization_runs(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import OptimizationRun

    runs = session.scalars(
        select(OptimizationRun)
        .order_by(OptimizationRun.created_at.desc())
        .limit(_LIST_LIMIT)
    ).all()
    failed = sum(1 for r in runs if r.status is OptimizationRunStatus.FAILED)
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(runs), unit="recent runs",
        severity=Severity.ATTENTION if failed else Severity.OK,
        rows=tuple(
            {
                "run_id": str(r.id),
                "status": r.status.value,
                "progress": r.progress_percent,
                # Kept distinct on purpose: INFEASIBLE (proven no schedule
                # exists) and TIMEOUT (we stopped looking) are different
                # facts, and a dashboard that showed both as "failed" would
                # erase the distinction SESSION 3 exists to protect.
                "solver": r.solver_status.value if r.solver_status else None,
            }
            for r in runs
        ),
        detail=f"{failed} failed" if failed else "",
    )


def _audit_activity(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import AuditLog

    since = now - dt.timedelta(days=7)
    total = int(
        session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= since)
        )
        or 0
    )
    return Metric(
        widget.code, widget.title, widget.kind,
        value=total, unit="recorded changes in the last 7 days",
    )


# ---------------------------------------------------------------------------
# Academic structure
# ---------------------------------------------------------------------------


def _academic_structure(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import (
        Course, Faculty, Program, Room,
    )
    from academic_ai.domain.enums import CourseStatus

    rows = (
        {"entity": "faculties", "count": _count(Faculty, session, status=LifecycleStatus.ACTIVE)},
        {"entity": "programmes", "count": _count(Program, session, status=LifecycleStatus.ACTIVE)},
        {"entity": "courses", "count": _count(Course, session, status=CourseStatus.ACTIVE)},
        {"entity": "rooms", "count": _count(Room, session, status=LifecycleStatus.ACTIVE)},
    )
    return Metric(
        widget.code, widget.title, widget.kind,
        value=sum(r["count"] for r in rows), unit="active records",
        rows=rows,
    )


def _periods(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import AcademicPeriod

    periods = session.scalars(
        select(AcademicPeriod)
        .order_by(AcademicPeriod.start_date.desc())
        .limit(_LIST_LIMIT)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(periods), unit="periods",
        rows=tuple(
            {
                "code": p.code,
                "name": p.name,
                "status": p.status.value,
                "starts": p.start_date.isoformat(),
            }
            for p in periods
        ),
    )


def _resources(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Room, Teacher

    rooms = _count(Room, session, status=LifecycleStatus.ACTIVE)
    teachers = _count(Teacher, session, status=LifecycleStatus.ACTIVE)
    return Metric(
        widget.code, widget.title, widget.kind,
        value=rooms + teachers, unit="schedulable resources",
        rows=({"entity": "rooms", "count": rooms},
              {"entity": "teachers", "count": teachers}),
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _schedule_health(session: Session, widget: Widget, principal, now) -> Metric:
    """What the feasibility analyser says -- warnings included.

    The warnings are the point. On real data SESSION 6 found no errors and
    three warnings: sessions with nobody committed to teach them, teachers
    with no availability on record, students who cannot be re-sectioned. A
    panel that reported "no errors" and stopped would be telling a director
    the timetable is fine, which is the failure the analyser was written to
    prevent.
    """
    from academic_ai.domain.scheduling.feasibility import analyse
    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance
    from academic_ai.infrastructure.db.models import AcademicPeriod
    from academic_ai.infrastructure.repositories import (
        SnapshotLoadError,
        load_constraints,
        load_snapshot,
    )

    period = session.scalars(
        select(AcademicPeriod).order_by(AcademicPeriod.start_date.desc()).limit(1)
    ).first()
    if period is None:
        return Metric.unavailable(widget, "no academic period exists yet")

    try:
        snapshot, _ = load_snapshot(session, period.id)
    except SnapshotLoadError as exc:
        return Metric.unavailable(widget, str(exc))

    instance = ProblemInstance(
        snapshot=snapshot,
        constraints=load_constraints(session, period.id),
        objective_profile=OBJECTIVE_PROFILES[
            "MINIMAL_DISRUPTION" if snapshot.base_assignments else "GREENFIELD"
        ],
    )
    report = analyse(instance)

    errors, warnings = report.errors, report.warnings
    if errors:
        severity = Severity.PROBLEM
    elif warnings:
        # Not OK. A green light over "44 sessions have nobody to teach them"
        # is exactly the false reassurance the analyser exists to prevent.
        severity = Severity.ATTENTION
    else:
        severity = Severity.OK

    return Metric(
        widget.code, widget.title, widget.kind,
        value=period.code,
        severity=severity,
        rows=tuple(
            {"severity": f.severity.value, "code": f.code, "message": f.message}
            for f in (errors + warnings)[:_LIST_LIMIT]
        ),
        detail=report.summary(),
    )


def _objective_tiers(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import OptimizationMetric, OptimizationRun

    run = session.scalars(
        select(OptimizationRun)
        .where(OptimizationRun.status == OptimizationRunStatus.COMPLETED)
        .order_by(OptimizationRun.finished_at.desc())
        .limit(1)
    ).first()
    if run is None:
        return Metric.unavailable(widget, "no run has completed yet")

    tiers = session.scalars(
        select(OptimizationMetric)
        .where(OptimizationMetric.optimization_run_id == run.id)
        .where(OptimizationMetric.name.like("tier.%"))
    ).all()
    if not tiers:
        return Metric.unavailable(widget, "the last run recorded no tier values")

    return Metric(
        widget.code, widget.title, widget.kind,
        # Deliberately no `value`: a headline figure would be a sum across
        # tiers, and ADR-0010 rejects the exchange rate that implies.
        value=None,
        unit="per tier, best first",
        rows=tuple(
            {"tier": m.name.removeprefix("tier."), "value": float(m.value)}
            for m in sorted(tiers, key=lambda m: m.name)
        ),
        detail=(
            "each tier is minimised in order; the values are not comparable "
            "with one another and are never added together"
        ),
    )


def _student_conflicts(session: Session, widget: Widget, principal, now) -> Metric:
    """Students whose classes clash, named.

    A count tells somebody to investigate; a name tells them what to do. Read
    from the published schedule, because a clash in a draft nobody has
    committed to is not yet a fact about anybody's week.
    """
    from academic_ai.domain.enums import ScheduleVersionStatus
    from academic_ai.domain.scheduling.impact import analyse_impact
    from academic_ai.domain.scheduling.neighborhood import PlacedSession
    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance
    from academic_ai.infrastructure.db.models import ScheduleVersion
    from academic_ai.infrastructure.repositories import (
        SnapshotLoadError,
        load_constraints,
        load_snapshot,
    )

    version = session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
        .order_by(ScheduleVersion.published_at.desc())
        .limit(1)
    ).first()
    if version is None:
        return Metric.unavailable(
            widget, "no timetable has been published, so nothing clashes yet"
        )

    try:
        snapshot, _ = load_snapshot(
            session, version.academic_period_id,
            base_schedule_version_id=version.id,
        )
    except SnapshotLoadError as exc:
        return Metric.unavailable(widget, str(exc))

    placement = {
        b.session_id: PlacedSession(b.session_id, b.start_slot_id, b.room_id)
        for b in snapshot.base_assignments
    }
    if not placement:
        return Metric.unavailable(
            widget, "the published version has no assignments to check"
        )

    instance = ProblemInstance(
        snapshot=snapshot,
        constraints=load_constraints(session, version.academic_period_id),
        objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
    )
    # No moves: this asks what the *current* schedule already contains.
    report = analyse_impact(instance, placement, [])
    clashes = report.new_conflicts

    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(clashes), unit="students with clashing classes",
        severity=Severity.PROBLEM if clashes else Severity.OK,
        rows=tuple(
            {
                "student": c.student_id,
                "sessions": f"{c.session_a} / {c.session_b}",
                "resolvable": not c.is_unresolvable,
            }
            for c in clashes[:_LIST_LIMIT]
        ),
    )


def _unplaced_sessions(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import (
        ClassSession, ScheduleAssignment, ScheduleVersion,
    )
    from academic_ai.domain.enums import ScheduleVersionStatus

    version = session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
        .order_by(ScheduleVersion.published_at.desc())
        .limit(1)
    ).first()
    if version is None:
        return Metric.unavailable(widget, "no schedule has been published yet")

    total = _count(ClassSession, session, academic_period_id=version.academic_period_id)
    placed = int(
        session.scalar(
            select(func.count())
            .select_from(ScheduleAssignment)
            .where(ScheduleAssignment.schedule_version_id == version.id)
        )
        or 0
    )
    unplaced = max(0, total - placed)
    return Metric(
        widget.code, widget.title, widget.kind,
        value=unplaced, unit=f"of {total} sessions still unplaced",
        severity=Severity.PROBLEM if unplaced else Severity.OK,
    )


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def _pending_approvals(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import ChangeRequest

    open_states = (
        ChangeRequestStatus.SUBMITTED,
        ChangeRequestStatus.UNDER_REVIEW,
    )
    requests = session.scalars(
        select(ChangeRequest)
        .where(ChangeRequest.status.in_(open_states))
        .order_by(ChangeRequest.created_at.asc())
        .limit(_LIST_LIMIT)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(requests), unit="awaiting a decision",
        severity=Severity.ATTENTION if requests else Severity.OK,
        rows=tuple(
            {"code": r.code, "title": r.title, "status": r.status.value}
            for r in requests
        ),
    )


def _my_requests(session: Session, widget: Widget, principal, now) -> Metric:
    import uuid

    from academic_ai.infrastructure.db.models import ChangeRequest

    requests = session.scalars(
        select(ChangeRequest)
        .where(ChangeRequest.requested_by_user_id == uuid.UUID(principal.user_id))
        .order_by(ChangeRequest.created_at.desc())
        .limit(_LIST_LIMIT)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(requests), unit="requests you raised",
        rows=tuple(
            {"code": r.code, "title": r.title, "status": r.status.value}
            for r in requests
        ),
    )


def _scenarios(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Scenario

    scenarios = session.scalars(
        select(Scenario).order_by(Scenario.created_at.desc()).limit(_LIST_LIMIT)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(scenarios), unit="scenarios",
        rows=tuple({"code": s.code, "name": s.name} for s in scenarios),
        detail=(
            ""
            if scenarios
            else "what-if scenarios are run in memory and not yet persisted"
        ),
    )


# ---------------------------------------------------------------------------
# Faculty
# ---------------------------------------------------------------------------


def _faculty_kpis(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import (
        CourseOffering, Enrollment, Program,
    )

    programs = _count(Program, session, status=LifecycleStatus.ACTIVE)
    offerings = int(session.scalar(select(func.count()).select_from(CourseOffering)) or 0)
    enrollments = int(session.scalar(select(func.count()).select_from(Enrollment)) or 0)
    return Metric(
        widget.code, widget.title, widget.kind,
        value=programs, unit="active programmes",
        rows=(
            {"indicator": "programmes", "value": programs},
            {"indicator": "offerings", "value": offerings},
            {"indicator": "enrolments", "value": enrollments},
        ),
    )


def _program_comparison(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Program

    programs = session.scalars(
        select(Program)
        .where(Program.status == LifecycleStatus.ACTIVE)
        .order_by(Program.code)
        .limit(_LIST_LIMIT)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(programs), unit="programmes compared",
        rows=tuple({"code": p.code, "name": p.name} for p in programs),
        detail=(
            "conflict counts per programme need the repository loader; the "
            "figures shown are structural only"
        ),
    )


# ---------------------------------------------------------------------------
# Personal
# ---------------------------------------------------------------------------


def _my_profile(session: Session, widget: Widget, principal, now) -> Metric:
    import uuid

    from academic_ai.infrastructure.db.models import User

    user = session.get(User, uuid.UUID(principal.user_id))
    if user is None:  # pragma: no cover - authenticated principals exist
        return Metric.unavailable(widget, "your account could not be read")
    return Metric(
        widget.code, widget.title, widget.kind,
        value=user.full_name,
        rows=(
            {"field": "email", "value": user.email},
            {"field": "role", "value": user.primary_role.value},
        ),
        detail=f"signed in as {user.primary_role.value.lower()}",
    )


def _teacher_for(session: Session, principal):
    import uuid

    from academic_ai.infrastructure.db.models import Teacher

    return session.scalars(
        select(Teacher).where(Teacher.user_id == uuid.UUID(principal.user_id))
    ).first()


def _my_load(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import SessionTeacher

    teacher = _teacher_for(session, principal)
    if teacher is None:
        return Metric.unavailable(
            widget, "your account is not linked to a teacher record"
        )

    sessions = int(
        session.scalar(
            select(func.count())
            .select_from(SessionTeacher)
            .where(SessionTeacher.teacher_id == teacher.id)
        )
        or 0
    )
    limit = teacher.max_weekly_hours
    return Metric(
        widget.code, widget.title, widget.kind,
        value=sessions, unit="sessions assigned",
        # An unset limit means "no institutional limit recorded", which is
        # not the same as a limit of zero -- the optimizer treats it as
        # unconstrained and so does this.
        detail=(
            f"your recorded weekly limit is {limit} hours"
            if limit
            else "no weekly limit is recorded for you, so none is enforced"
        ),
        severity=Severity.ATTENTION if limit is None else Severity.OK,
    )


def _my_availability(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Availability

    teacher = _teacher_for(session, principal)
    if teacher is None:
        return Metric.unavailable(
            widget, "your account is not linked to a teacher record"
        )

    declared = int(
        session.scalar(
            select(func.count())
            .select_from(Availability)
            .where(Availability.teacher_id == teacher.id)
        )
        or 0
    )
    if declared == 0:
        # The distinction SESSION 6 insisted on: nothing recorded is not the
        # same as available all week, even though the optimizer has to treat
        # it that way.
        return Metric(
            widget.code, widget.title, widget.kind,
            value=0, unit="hours declared",
            severity=Severity.ATTENTION,
            detail=(
                "you have not told the platform when you can teach, so the "
                "optimizer will assume you are free at every hour"
            ),
        )
    return Metric(
        widget.code, widget.title, widget.kind,
        value=declared, unit="availability rows recorded",
    )


def _my_schedule(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.domain.enums import ScheduleVersionStatus
    from academic_ai.infrastructure.db.models import ScheduleVersion

    published = session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
        .order_by(ScheduleVersion.published_at.desc())
        .limit(1)
    ).first()
    if published is None:
        return Metric.unavailable(
            widget,
            "no timetable has been published yet; anything you see elsewhere "
            "is provisional and may still change",
        )
    return Metric(
        widget.code, widget.title, widget.kind,
        value="published", unit="",
        detail=(
            f"version {published.version_number}, published "
            f"{published.published_at:%Y-%m-%d}"
        ),
    )


def _student_for(session: Session, principal):
    import uuid

    from academic_ai.infrastructure.db.models import Student

    return session.scalars(
        select(Student).where(Student.user_id == uuid.UUID(principal.user_id))
    ).first()


def _my_enrollments(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Course, Enrollment

    student = _student_for(session, principal)
    if student is None:
        return Metric.unavailable(
            widget, "your account is not linked to a student record"
        )

    rows = session.execute(
        select(Course.code, Course.name, Enrollment.status, Enrollment.attempt_number)
        .join(Course, Course.id == Enrollment.course_id)
        .where(Enrollment.student_id == student.id)
        .order_by(Course.code)
        .limit(_LIST_LIMIT * 3)
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(rows), unit="courses this period",
        rows=tuple(
            {"code": c, "name": n, "status": s.value, "attempt": a}
            for c, n, s, a in rows
        ),
    )


def _my_repeated_courses(session: Session, widget: Widget, principal, now) -> Metric:
    from academic_ai.infrastructure.db.models import Course, Enrollment

    student = _student_for(session, principal)
    if student is None:
        return Metric.unavailable(
            widget, "your account is not linked to a student record"
        )

    rows = session.execute(
        select(Course.code, Course.name, Enrollment.attempt_number)
        .join(Course, Course.id == Enrollment.course_id)
        .where(Enrollment.student_id == student.id)
        .where(Enrollment.attempt_number > 1)
        .order_by(Enrollment.attempt_number.desc())
    ).all()
    return Metric(
        widget.code, widget.title, widget.kind,
        value=len(rows), unit="courses you are repeating",
        # Not an error and not nothing: a repeat is what makes a clash hardest
        # to resolve, and the student should see it before the timetable is
        # fixed rather than after.
        severity=Severity.ATTENTION if rows else Severity.OK,
        rows=tuple(
            {"code": c, "name": n, "attempt": a} for c, n, a in rows
        ),
    )


def _my_conflicts(session: Session, widget: Widget, principal, now) -> Metric:
    student = _student_for(session, principal)
    if student is None:
        return Metric.unavailable(
            widget, "your account is not linked to a student record"
        )
    return Metric.unavailable(
        widget,
        "clashes are computed against a scheduled timetable; none is loaded "
        "from the database yet",
    )


_HANDLERS = {
    "system_health": _system_health,
    "user_accounts": _user_accounts,
    "ai_models": _ai_models,
    "optimization_runs": _optimization_runs,
    "audit_activity": _audit_activity,
    "academic_structure": _academic_structure,
    "periods": _periods,
    "resources": _resources,
    "schedule_health": _schedule_health,
    "objective_tiers": _objective_tiers,
    "student_conflicts": _student_conflicts,
    "unplaced_sessions": _unplaced_sessions,
    "pending_approvals": _pending_approvals,
    "my_requests": _my_requests,
    "scenarios": _scenarios,
    "faculty_kpis": _faculty_kpis,
    "program_comparison": _program_comparison,
    "my_profile": _my_profile,
    "my_load": _my_load,
    "my_availability": _my_availability,
    "my_schedule": _my_schedule,
    "my_enrollments": _my_enrollments,
    "my_repeated_courses": _my_repeated_courses,
    "my_conflicts": _my_conflicts,
}
