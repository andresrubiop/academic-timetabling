"""SESSION 20: what the repository read actually reads.

SESSION 18 replaced the stubs with `infrastructure/repositories/`, and the end
-to-end proof was a seeded period that loaded and solved. That proves the happy
path and nothing else: at the start of this session the three modules sat at
78-81%, and the uncovered lines were not obscure. They were the availability
join, the no-teacher diagnostic, ADR-0009's refusal to guess a section, the
entire configured-constraint loop, and the skip that stops a step-less
workflow approving everything.

Each of those is a place where the wrong answer is *plausible* -- a snapshot
that loads, solves, and quietly ignores a teacher's declared unavailability
looks exactly like one that does not.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AvailabilityStatus,
    ConstraintCategory,
    EnrollmentKind,
    EnrollmentStatus,
    ProposalSource,
    ScheduleScope,
    ScheduleVersionStatus,
    SessionType,
    Modality,
    UserRole,
)
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG
from academic_ai.domain.scheduling.diagnostics import DiagnosticCode
from academic_ai.infrastructure.db.models import (
    AcademicCompatibilityGroup,
    ApprovalStep,
    ApprovalWorkflow,
    Availability,
    ClassSession,
    ConstraintType,
    Enrollment,
    PlanningConstraint,
    RoomFeature,
    RoomFeatureAssignment,
    ScheduleAssignment,
    Section,
    SessionCoTeachingGroup,
    SessionRequiredFeature,
    SessionTeacher,
)
from academic_ai.infrastructure.repositories.constraints import load_constraints
from academic_ai.infrastructure.repositories.snapshot import (
    SnapshotLoadError,
    load_snapshot,
)
from academic_ai.infrastructure.repositories.workflows import load_chains


def _snapshot(session: Session, period_id):
    snapshot, diagnostics = load_snapshot(session, period_id)
    return snapshot, {d.code for d in diagnostics}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class TestTheLoaderRefusesRatherThanImprovises:
    def test_an_unknown_period_is_refused(self, session: Session) -> None:
        with pytest.raises(SnapshotLoadError, match="no academic period"):
            load_snapshot(session, uuid.uuid4())

    def test_a_period_with_no_grid_is_refused_by_name(
        self, session: Session, world
    ) -> None:
        # An empty grid would otherwise produce a snapshot with no slots, and
        # the solver would report every session unplaceable -- a true
        # statement about a false premise. The refusal names the period so the
        # answer is "configure the grid", not "the optimizer is broken".
        from academic_ai.infrastructure.db.models import AcademicPeriod, Timeslot

        session.query(Timeslot).filter(
            Timeslot.academic_period_id == world.period.id
        ).delete()
        session.flush()

        with pytest.raises(SnapshotLoadError, match=world.period.code):
            load_snapshot(session, world.period.id)

    def test_a_period_with_no_sessions_is_refused(
        self, session: Session, offered
    ) -> None:
        session.delete(offered.class_session)
        session.flush()
        with pytest.raises(SnapshotLoadError, match="no class sessions"):
            load_snapshot(session, offered.period.id)


# --------------------------------------------------------------------------
# Availability — a HARD constraint's input
# --------------------------------------------------------------------------


class TestAvailability:
    def test_a_teachers_declared_unavailability_reaches_the_snapshot(
        self, session: Session, offered
    ) -> None:
        # The failure this catches is the quiet one: a snapshot that loads and
        # solves while ignoring the row is indistinguishable from a correct
        # one until somebody is scheduled at a time they said they cannot
        # teach.
        blocked = offered.timeslots[0]
        session.add(
            Availability(
                teacher_id=offered.teacher.id,
                timeslot_id=blocked.id,
                status=AvailabilityStatus.UNAVAILABLE,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        teacher = next(t for t in snapshot.teachers if t.id == str(offered.teacher.id))
        assert str(blocked.id) in teacher.unavailable_slot_ids

    def test_an_available_row_blocks_nothing(
        self, session: Session, offered
    ) -> None:
        # Declaring availability is not declaring unavailability. Reading
        # every row as a block would empty the teacher's calendar.
        session.add(
            Availability(
                teacher_id=offered.teacher.id,
                timeslot_id=offered.timeslots[0].id,
                status=AvailabilityStatus.AVAILABLE,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        teacher = next(t for t in snapshot.teachers if t.id == str(offered.teacher.id))
        assert teacher.unavailable_slot_ids == frozenset()
        # ...but it is recorded that the teacher declared *something*, which is
        # how the model tells "available all week" from "never said".
        assert teacher.has_declared_availability is True

    def test_a_rooms_unavailability_reaches_the_snapshot(
        self, session: Session, offered
    ) -> None:
        blocked = offered.timeslots[1]
        session.add(
            Availability(
                room_id=offered.room.id,
                timeslot_id=blocked.id,
                status=AvailabilityStatus.UNAVAILABLE,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        room = next(r for r in snapshot.rooms if r.id == str(offered.room.id))
        assert str(blocked.id) in room.unavailable_slot_ids


# --------------------------------------------------------------------------
# Room features
# --------------------------------------------------------------------------


class TestFeatures:
    def _feature(self, session: Session, world, code: str) -> RoomFeature:
        feature = RoomFeature(
            institution_id=world.institution.id, code=code, name=code.title()
        )
        session.add(feature)
        session.flush()
        return feature

    def test_a_rooms_features_and_quantities_are_read(
        self, session: Session, offered
    ) -> None:
        feature = self._feature(session, offered, "PROJECTOR")
        session.add(
            RoomFeatureAssignment(
                room_id=offered.room.id, room_feature_id=feature.id, quantity=2
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        room = next(r for r in snapshot.rooms if r.id == str(offered.room.id))
        # The quantity matters, not just the presence: a session needing two
        # of something is not satisfied by a room with one.
        assert room.features["PROJECTOR"] == 2

    def test_a_sessions_required_features_are_read(
        self, session: Session, offered
    ) -> None:
        feature = self._feature(session, offered, "LAB_BENCH")
        session.add(
            SessionRequiredFeature(
                class_session_id=offered.class_session.id,
                room_feature_id=feature.id,
                min_quantity=12,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        spec = next(
            s for s in snapshot.sessions if s.id == str(offered.class_session.id)
        )
        assert spec.required_features["LAB_BENCH"] == 12


# --------------------------------------------------------------------------
# Diagnostics the loader is responsible for
# --------------------------------------------------------------------------


class TestDiagnostics:
    def test_a_session_with_nobody_to_teach_it_is_reported_not_refused(
        self, session: Session, offered
    ) -> None:
        # SESSION 6's whole category: solvable, but somebody should know. It
        # is scheduled anyway, because a timetable missing a lecturer is a
        # staffing problem, not an infeasible model.
        session.query(SessionTeacher).filter(
            SessionTeacher.class_session_id == offered.class_session.id
        ).delete()
        session.flush()

        snapshot, codes = _snapshot(session, offered.period.id)
        assert DiagnosticCode.SESSION_HAS_NO_TEACHER in codes
        assert len(snapshot.sessions) == 1

    def test_co_teaching_membership_is_read(
        self, session: Session, offered
    ) -> None:
        # A co-teaching group cannot exist without an approved academic
        # compatibility group behind it -- the schema binds them, which is why
        # the loader can set `is_approved=True` without checking anything.
        # CLAUDE.md puts academic equivalence among the decisions no AI
        # recommendation approves, and this is where that is made structural.
        compatibility = AcademicCompatibilityGroup(
            institution_id=offered.institution.id,
            code="CO-COMPAT",
            name="Grupo compatible",
            proposal_source=ProposalSource.HUMAN,
        )
        session.add(compatibility)
        session.flush()

        group = SessionCoTeachingGroup(
            academic_period_id=offered.period.id,
            academic_compatibility_group_id=compatibility.id,
            code="CO-1",
        )
        session.add(group)
        session.flush()
        offered.class_session.co_teaching_group_id = group.id
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        assert len(snapshot.co_teaching_groups) == 1
        assert snapshot.co_teaching_groups[0].member_session_ids == (
            str(offered.class_session.id),
        )


# --------------------------------------------------------------------------
# ADR-0009 — the loader does not guess a section
# --------------------------------------------------------------------------


class TestUnplacedEnrolments:
    def _enrol(self, session: Session, world, *, section=None) -> Enrollment:
        row = Enrollment(
            student_id=world.student.id,
            course_offering_id=world.offering.id,
            section_id=section.id if section is not None else None,
            course_id=world.course.id,
            academic_period_id=world.period.id,
            curriculum_version_id=world.curriculum_version.id,
            curriculum_course_id=world.curriculum_course.id,
            is_mandatory=True,
            enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
            attempt_number=1,
            status=EnrollmentStatus.ENROLLED,
        )
        session.add(row)
        session.flush()
        return row

    def test_one_section_makes_an_unplaced_enrolment_unambiguous(
        self, session: Session, offered
    ) -> None:
        # With a single section there is nothing to guess: the student
        # occupies its sessions whichever way you read the row.
        self._enrol(session, offered)

        snapshot, codes = _snapshot(session, offered.period.id)
        assert len(snapshot.enrollments) == 1
        assert snapshot.enrollments[0].session_ids == (
            str(offered.class_session.id),
        )
        assert DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS not in codes

    def test_several_sections_means_the_loader_refuses_to_choose(
        self, session: Session, offered
    ) -> None:
        # ADR-0009: sectioning is not an optimizer decision. Picking a section
        # here would be the optimizer deciding one, quietly, in a loader --
        # and the student would be counted against sessions nobody assigned
        # them to.
        second = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=2,
            status=offered.section.status,
        )
        session.add(second)
        session.flush()
        session.add(
            ClassSession(
                section_id=second.id,
                academic_period_id=offered.period.id,
                sequence_number=1,
                session_type=SessionType.LECTURE,
                delivery_mode=Modality.ONSITE,
                duration_slots=2,
                requires_timeslot=True,
                requires_room=True,
            )
        )
        session.flush()
        self._enrol(session, offered)

        snapshot, codes = _snapshot(session, offered.period.id)
        assert snapshot.enrollments == ()
        # Dropped, but never silently.
        assert DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS in codes

    def test_an_enrolment_naming_its_section_is_resolved_either_way(
        self, session: Session, offered
    ) -> None:
        second = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=2,
            status=offered.section.status,
        )
        session.add(second)
        session.flush()
        self._enrol(session, offered, section=offered.section)

        snapshot, codes = _snapshot(session, offered.period.id)
        assert len(snapshot.enrollments) == 1
        assert DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS not in codes

    def test_alternatives_are_counted_without_the_students_own_section(
        self, session: Session, offered
    ) -> None:
        # `criticality()` divides by the number of ways out. Counting the
        # student's own section as an alternative to itself would make every
        # student look one option less stuck than they are.
        second = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=2,
            status=offered.section.status,
        )
        session.add(second)
        session.flush()
        self._enrol(session, offered, section=offered.section)

        snapshot, _ = _snapshot(session, offered.period.id)
        assert snapshot.enrollments[0].alternative_section_count == 1


# --------------------------------------------------------------------------
# Configured constraints
# --------------------------------------------------------------------------


class TestConstraintRead:
    def _type(self, session: Session, code: str):
        """A `constraint_type` row for a code the **catalog** knows.

        Invented codes are refused by `EffectiveConstraint`, which is the
        point: the catalog is the floor of HARD enforcement, and a configured
        row naming a constraint that does not exist is a configuration error,
        not a new constraint.
        """
        entry = CONSTRAINT_CATALOG[code]
        row = ConstraintType(
            code=code,
            name=code.title(),
            default_category=entry.default_category,
            is_category_overridable=entry.is_category_overridable,
            is_builtin=True,
        )
        session.add(row)
        session.flush()
        return row

    def test_a_configured_row_is_read_with_its_weight_and_parameters(
        self, session: Session, world
    ) -> None:
        # The whole loop was uncovered: nothing in the suite had ever read a
        # `planning_constraint` row.
        kind = self._type(session, "TEACHER_MAX_DAILY_HOURS")
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=world.period.id,
                parameters={"max_hours": 4},
                weight=2.5,
                is_active=True,
            )
        )
        session.flush()

        loaded = load_constraints(session, world.period.id)
        constraint = next(c for c in loaded if c.code == "TEACHER_MAX_DAILY_HOURS")
        assert constraint.parameters == {"max_hours": 4}
        assert constraint.weight == pytest.approx(2.5)

    def test_no_override_means_the_catalogs_own_category(
        self, session: Session, world
    ) -> None:
        # CLAUDE.md: the catalog is the floor of HARD enforcement, not the
        # configured rows. An absent override means "no opinion", never "off".
        kind = self._type(session, "ROOM_ACTIVE")
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=world.period.id,
                weight=1.0,
                is_active=True,
            )
        )
        session.flush()

        loaded = load_constraints(session, world.period.id)
        constraint = next(c for c in loaded if c.code == "ROOM_ACTIVE")
        assert constraint.effective_category is ConstraintCategory.HARD

    def test_an_override_is_honoured_and_carries_who_and_why(
        self, session: Session, world
    ) -> None:
        # A downgrade with no name against it is an anonymous decision to
        # enforce something less strictly.
        kind = self._type(session, "SECTION_DAY_SPREAD")
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=world.period.id,
                category_override=ConstraintCategory.SOFT,
                overridden_by_user_id=world.admin.id,
                override_reason="Aprobado por consejo de facultad",
                weight=1.0,
                is_active=True,
            )
        )
        session.flush()

        loaded = load_constraints(session, world.period.id)
        constraint = next(c for c in loaded if c.code == "SECTION_DAY_SPREAD")
        assert constraint.effective_category is ConstraintCategory.SOFT
        assert constraint.overridden_by_user_id == str(world.admin.id)
        assert constraint.override_reason

    def test_an_institution_wide_row_applies_to_every_period(
        self, session: Session, world
    ) -> None:
        # `academic_period_id IS NULL` means "everywhere", and a loader that
        # only read period rows would silently drop the institution's
        # standing policy.
        kind = self._type(session, "STUDENT_MAX_DAILY_HOURS")
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=None,
                weight=1.0,
                is_active=True,
            )
        )
        session.flush()

        codes = {c.code for c in load_constraints(session, world.period.id)}
        assert "STUDENT_MAX_DAILY_HOURS" in codes

    def test_an_inactive_row_is_still_read_and_says_so(
        self, session: Session, world
    ) -> None:
        # Reading it and reporting `is_active=False` keeps the decision with
        # the caller. Filtering here would hide from the audit trail that a
        # row exists and was switched off.
        kind = self._type(session, "TEACHER_MAX_CONSECUTIVE_SLOTS")
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=world.period.id,
                weight=1.0,
                is_active=False,
            )
        )
        session.flush()

        loaded = load_constraints(session, world.period.id)
        constraint = next(c for c in loaded if c.code == "TEACHER_MAX_CONSECUTIVE_SLOTS")
        assert constraint.is_active is False


# --------------------------------------------------------------------------
# Approval workflows
# --------------------------------------------------------------------------


class TestWorkflowRead:
    def _workflow(
        self, session: Session, world, code: str, *, steps: int, scope=None
    ) -> ApprovalWorkflow:
        workflow = ApprovalWorkflow(
            institution_id=world.institution.id,
            code=code,
            name=code,
            applies_to_scope=scope or ScheduleScope.PROGRAM,
            is_active=True,
        )
        session.add(workflow)
        session.flush()
        for number in range(1, steps + 1):
            session.add(
                ApprovalStep(
                    approval_workflow_id=workflow.id,
                    step_number=number,
                    required_role=UserRole.COORDINATOR,
                    requires_all_affected=False,
                )
            )
        session.flush()
        return workflow

    def test_a_configured_workflow_becomes_a_chain(
        self, session: Session, world
    ) -> None:
        self._workflow(session, world, "W-A", steps=2)
        chains = load_chains(session, world.institution.id)
        assert len(chains[ScheduleScope.PROGRAM].steps) == 2

    def test_a_workflow_with_no_steps_is_skipped_rather_than_trusted(
        self, session: Session, world
    ) -> None:
        # This is the one that matters. A chain with no steps approves every
        # request it is given without anybody looking; skipping it leaves the
        # scope with no chain, and `resolve_chain` then refuses -- which is
        # the safe answer, and a visible one.
        self._workflow(session, world, "W-EMPTY", steps=0)
        assert ScheduleScope.PROGRAM not in load_chains(session, world.institution.id)

    def test_two_workflows_for_one_scope_resolve_deterministically(
        self, session: Session, world
    ) -> None:
        # A routing decision that changed between two identical requests would
        # be indistinguishable from a bug. Lower code wins, every time.
        self._workflow(session, world, "W-ZZ", steps=3)
        self._workflow(session, world, "W-AA", steps=1)

        chains = load_chains(session, world.institution.id)
        assert chains[ScheduleScope.PROGRAM].code == "W-AA"

    def test_an_inactive_workflow_is_not_a_chain(
        self, session: Session, world
    ) -> None:
        workflow = self._workflow(session, world, "W-OFF", steps=1)
        workflow.is_active = False
        session.flush()
        assert load_chains(session, world.institution.id) == {}

    def test_a_scope_with_no_workflow_produces_nothing(
        self, session: Session, world
    ) -> None:
        # Deliberately not a default chain: a fallback runs whenever
        # configuration is missing and fails silently, because a change routed
        # to the wrong authorities looks exactly like one routed to the right
        # ones.
        self._workflow(session, world, "W-P", steps=1, scope=ScheduleScope.PROGRAM)
        chains = load_chains(session, world.institution.id)
        assert ScheduleScope.FACULTY not in chains


# --------------------------------------------------------------------------
# The placement a re-optimisation starts from
# --------------------------------------------------------------------------


class TestBaseAssignments:
    """`load_snapshot` can start from an existing timetable. Which one it
    picks is a correctness question, not a convenience."""

    def _assign(self, session: Session, world, version, *, locked: bool = False):
        slot = world.timeslots[0]
        row = ScheduleAssignment(
            schedule_version_id=version.id,
            class_session_id=world.class_session.id,
            academic_period_id=world.period.id,
            start_timeslot_id=slot.id,
            day_of_week=slot.day_of_week,
            start_slot_index=slot.day_slot_index,
            duration_slots=world.class_session.duration_slots,
            requires_room=True,
            room_id=world.room.id,
            is_locked=locked,
        )
        session.add(row)
        session.flush()
        return row

    def test_no_version_means_no_base_placement(
        self, session: Session, offered
    ) -> None:
        # Nothing published and nothing named: a first solve, starting from
        # empty. Not an error.
        snapshot, _ = _snapshot(session, offered.period.id)
        assert snapshot.base_assignments == ()

    def test_a_named_version_becomes_the_base(
        self, session: Session, offered
    ) -> None:
        self._assign(session, offered, offered.version)

        snapshot, _ = load_snapshot(
            session, offered.period.id, base_schedule_version_id=offered.version.id
        )
        assert len(snapshot.base_assignments) == 1
        assert snapshot.base_assignments[0].session_id == str(
            offered.class_session.id
        )

    def test_a_locked_assignment_stays_locked_through_the_read(
        self, session: Session, offered
    ) -> None:
        # A lock that survives to the database and is lost on the way back is
        # worse than no lock: the coordinator sees it pinned and the solver
        # moves it.
        self._assign(session, offered, offered.version, locked=True)

        snapshot, _ = load_snapshot(
            session, offered.period.id, base_schedule_version_id=offered.version.id
        )
        assert snapshot.base_assignments[0].is_locked is True

    def test_the_published_version_is_used_when_none_is_named(
        self, session: Session, offered
    ) -> None:
        import datetime as dt

        self._assign(session, offered, offered.version)
        offered.version.status = ScheduleVersionStatus.PUBLISHED
        offered.version.published_at = dt.datetime.now(dt.timezone.utc)
        offered.version.published_by_user_id = offered.admin.id
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        assert len(snapshot.base_assignments) == 1

    def test_a_draft_is_never_picked_up_by_recency(
        self, session: Session, offered
    ) -> None:
        # The refusal that matters here. Re-optimising against whichever draft
        # happened to be newest would make the result depend on who last
        # pressed save -- two runs of the same request, different answers, no
        # error anywhere.
        self._assign(session, offered, offered.version)
        assert offered.version.status is not ScheduleVersionStatus.PUBLISHED

        snapshot, _ = _snapshot(session, offered.period.id)
        assert snapshot.base_assignments == ()


class TestUnreadableConstraintRows:
    def test_a_row_naming_a_constraint_the_catalog_does_not_know_is_skipped(
        self, session: Session, world
    ) -> None:
        # Reachable through SESSION 14's CRUD API: `constraint_type` is an
        # ordinary table, and nothing stops a row naming a code the optimizer
        # has never heard of. Skipping it -- loudly -- beats refusing to load
        # the period, which would take the whole timetable down over one bad
        # configuration row.
        kind = ConstraintType(
            code="NOT_IN_THE_CATALOG",
            name="Invented",
            default_category=ConstraintCategory.MEDIUM,
            is_category_overridable=True,
            is_builtin=False,
        )
        session.add(kind)
        session.flush()
        session.add(
            PlanningConstraint(
                constraint_type_id=kind.id,
                academic_period_id=world.period.id,
                weight=1.0,
                is_active=True,
            )
        )
        session.flush()

        loaded = load_constraints(session, world.period.id)
        assert all(c.code != "NOT_IN_THE_CATALOG" for c in loaded)


class TestAllowedRooms:
    """SESSION 41: which rooms a class may use.

    `SessionSpec.allowed_room_ids` has existed since SESSION 2 and the FET
    reader has computed it since the FET work; the schema had nowhere to store
    it, so every restriction was dropped on import and `compute_domains`
    offered every room for every class.
    """

    def test_a_restriction_reaches_the_instance(
        self, session: Session, offered
    ) -> None:
        from academic_ai.infrastructure.db.models import ClassSessionAllowedRoom

        session.add(
            ClassSessionAllowedRoom(
                class_session_id=offered.class_session.id,
                room_id=offered.room.id,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        spec = next(
            s for s in snapshot.sessions if s.id == str(offered.class_session.id)
        )
        assert spec.allowed_room_ids == frozenset({str(offered.room.id)})

    def test_no_rows_means_unrestricted_not_forbidden(
        self, session: Session, offered
    ) -> None:
        # The common case, and the reason this is a join table: a class that
        # may go anywhere has no rows and costs nothing. An empty set that
        # meant "no room is allowed" would make every ordinary session
        # unplaceable.
        snapshot, _ = _snapshot(session, offered.period.id)
        spec = next(
            s for s in snapshot.sessions if s.id == str(offered.class_session.id)
        )
        assert spec.allowed_room_ids == frozenset()

    def test_it_narrows_the_domain_the_grid_renders(
        self, session: Session, offered, world
    ) -> None:
        # The point of storing it. `compute_domains` is what the timetable
        # offers as a drop target and what the solver searches; a restriction
        # that does not reach it is a note in a table.
        from academic_ai.domain.scheduling.domains import compute_domains
        from academic_ai.infrastructure.db.models import (
            ClassSessionAllowedRoom,
            Room,
        )
        from academic_ai.infrastructure.repositories import load_constraints

        second = Room(
            building_id=offered.building.id,
            campus_id=offered.campus.id,
            code="OTRA-1",
            name="Otra aula",
            room_type=offered.room.room_type,
            capacity=offered.room.capacity,
        )
        session.add(second)
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        constraints = load_constraints(session, offered.period.id)
        before, _ = compute_domains(snapshot, constraints)
        wide = before[str(offered.class_session.id)].room_ids
        assert str(second.id) in wide, "the second room was not a candidate anyway"

        session.add(
            ClassSessionAllowedRoom(
                class_session_id=offered.class_session.id,
                room_id=offered.room.id,
            )
        )
        session.flush()

        snapshot, _ = _snapshot(session, offered.period.id)
        after, _ = compute_domains(snapshot, constraints)
        narrowed = after[str(offered.class_session.id)].room_ids
        assert str(second.id) not in narrowed
        assert str(offered.room.id) in narrowed

    def test_the_same_room_cannot_be_listed_twice(
        self, session: Session, offered
    ) -> None:
        # It would double that room's weight in any count of how restricted a
        # class is.
        from sqlalchemy.exc import IntegrityError

        from academic_ai.infrastructure.db.models import ClassSessionAllowedRoom

        session.add(
            ClassSessionAllowedRoom(
                class_session_id=offered.class_session.id,
                room_id=offered.room.id,
            )
        )
        session.flush()
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    ClassSessionAllowedRoom(
                        class_session_id=offered.class_session.id,
                        room_id=offered.room.id,
                    )
                )
                session.flush()
