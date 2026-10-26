"""SESSION 18: the write path SESSION 10 and SESSION 12 were both waiting on.

Three properties are checked here rather than trusted, and each is the reason
the code is shaped the way it is.

**The impact is recomputed, never accepted.** A request carrying its own
impact analysis would let somebody submit a change claiming it affects nobody
-- and since the routing is derived *from* the impact, that would also pick
the shortest approval chain. So the client sends moves and the server works
out what they do.

**The scenario is written at submission, not before.** A coordinator exploring
five options should not leave five rows behind; the row appears when somebody
commits to one, because a change request has to point at something.

**Nothing applies anything.** A submitted request changes no timetable, and an
approved one changes no timetable either. Applying is a separate act, and
keeping it separate is what makes the chain mean something rather than a delay
before an inevitability.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.application.change_requests import (
    ChangeRequestError,
    decide,
    load_state,
    submit_change_request,
)
from academic_ai.domain.enums import (
    ApprovalAction,
    ChangeRequestStatus,
    Modality,
    NotificationKind,
    ScheduleScope,
    ScheduleVersionStatus,
    SessionType,
    TeacherRole,
    UserRole,
)
from academic_ai.domain.governance import next_pending_step
from academic_ai.domain.scheduling.impact import ProposedMove
from academic_ai.infrastructure.db.models import (
    ApprovalStep,
    ApprovalWorkflow,
    ChangeRequest,
    ClassSession,
    CourseOffering,
    Notification,
    Scenario,
    ScenarioPerturbation,
    Schedule,
    ScheduleAssignment,
    ScheduleVersion,
    Section,
    SessionTeacher,
    User,
)
from academic_ai.security.rbac import ActorKind, Principal


def _principal(user: User, kind: ActorKind = ActorKind.HUMAN) -> Principal:
    return Principal(user_id=str(user.id), role=user.primary_role, kind=kind)


def _user(session: Session, role: UserRole, email: str) -> User:
    user = User(email=email, full_name=role.value.title(), primary_role=role)
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def published(session: Session, world):
    """A published timetable with two placed sessions, and a free slot to
    move one into."""
    offering = CourseOffering(
        course_id=world.course.id,
        academic_period_id=world.period.id,
        campus_id=world.campus.id,
        modality=Modality.ONSITE,
    )
    session.add(offering)
    session.flush()
    section = Section(
        course_offering_id=offering.id,
        academic_period_id=world.period.id,
        code="A",
        capacity=30,
    )
    session.add(section)
    session.flush()

    sessions = []
    for index in (1, 2):
        class_session = ClassSession(
            section_id=section.id,
            academic_period_id=world.period.id,
            sequence_number=index,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=1,
        )
        session.add(class_session)
        session.flush()
        session.add(
            SessionTeacher(
                class_session_id=class_session.id,
                teacher_id=world.teacher.id,
                role=TeacherRole.PRIMARY,
            )
        )
        sessions.append(class_session)

    schedule = Schedule(
        institution_id=world.institution.id,
        academic_period_id=world.period.id,
        scope=ScheduleScope.INSTITUTION,
        code="SCH",
        name="Horario",
    )
    session.add(schedule)
    session.flush()
    version = ScheduleVersion(
        schedule_id=schedule.id,
        institution_id=world.institution.id,
        academic_period_id=world.period.id,
        version_number=1,
        status=ScheduleVersionStatus.DRAFT,
    )
    session.add(version)
    session.flush()

    slots = list(world.timeslots)
    for offset, class_session in enumerate(sessions):
        slot = slots[offset]
        session.add(
            ScheduleAssignment(
                schedule_version_id=version.id,
                class_session_id=class_session.id,
                academic_period_id=world.period.id,
                start_timeslot_id=slot.id,
                day_of_week=slot.day_of_week,
                start_slot_index=slot.day_slot_index,
                duration_slots=1,
                requires_room=True,
                room_id=world.room.id,
            )
        )
    session.flush()

    # Published last: assignments are immutable once a version leaves DRAFT.
    version.status = ScheduleVersionStatus.PUBLISHED
    version.published_at = dt.datetime.now(dt.timezone.utc)
    version.published_by_user_id = world.admin.id
    session.flush()

    return {"version": version, "sessions": sessions, "slots": slots}


@pytest.fixture()
def chain(session: Session, world):
    workflow = ApprovalWorkflow(
        institution_id=world.institution.id,
        code="WITHIN_PROGRAM",
        name="Within programme",
        applies_to_scope=ScheduleScope.PROGRAM,
    )
    session.add(workflow)
    session.flush()
    for number, role in ((1, UserRole.COORDINATOR), (2, UserRole.DIRECTOR)):
        session.add(
            ApprovalStep(
                approval_workflow_id=workflow.id,
                step_number=number,
                required_role=role,
            )
        )
    session.flush()
    return workflow


@pytest.fixture()
def requester(session: Session) -> User:
    return _user(session, UserRole.COORDINATOR, "req@example.test")


def _move(published, target_index: int = 3) -> ProposedMove:
    return ProposedMove(
        session_id=str(published["sessions"][0].id),
        to_start_slot_id=str(published["slots"][target_index].id),
        to_room_id=None,
    )


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


class TestSubmitting:
    def test_a_request_is_created_and_routed(
        self, session: Session, world, published, chain, requester
    ) -> None:
        result = submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Mover Cálculo I al martes",
        )
        assert result.status is ChangeRequestStatus.SUBMITTED
        assert result.chain_code == "WITHIN_PROGRAM"
        assert result.code.startswith("CR-")
        assert result.rationale, "the routing must say why it chose this chain"

    def test_the_impact_is_computed_not_taken_from_the_caller(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """Nothing in the request body describes the impact. A client-supplied
        one would let somebody claim a change affects nobody -- and the
        routing is derived from the impact."""
        result = submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Movimiento",
        )
        stored = session.get(ChangeRequest, uuid.UUID(result.change_request_id))
        assert stored.impact["affected"]["sessions"] == 1
        assert "scope_rationale" in stored.impact
        assert isinstance(stored.impact["score_delta"], list)

    def test_the_scenario_is_written_with_its_perturbations(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """The row appears at submission, because a change request has to
        point at something."""
        result = submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Movimiento",
        )
        stored = session.get(ChangeRequest, uuid.UUID(result.change_request_id))
        scenario = session.get(Scenario, stored.scenario_id)
        assert scenario is not None

        perturbations = session.scalars(
            select(ScenarioPerturbation).where(
                ScenarioPerturbation.scenario_id == scenario.id
            )
        ).all()
        assert len(perturbations) == 1
        assert perturbations[0].kind == "MOVE_SESSION"

    def test_submitting_changes_no_timetable(
        self, session: Session, world, published, chain, requester
    ) -> None:
        before = {
            str(a.class_session_id): str(a.start_timeslot_id)
            for a in session.scalars(select(ScheduleAssignment))
        }
        submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Movimiento",
        )
        after = {
            str(a.class_session_id): str(a.start_timeslot_id)
            for a in session.scalars(select(ScheduleAssignment))
        }
        assert after == before

    def test_an_empty_request_is_refused(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """It would go through an approval chain to change nothing."""
        with pytest.raises(ChangeRequestError, match="at least one move"):
            submit_change_request(
                session,
                _principal(requester),
                academic_period_id=world.period.id,
                moves=[],
                title="Nada",
            )

    def test_a_request_with_no_title_is_refused(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """Somebody two steps up the chain sees it before anything else."""
        with pytest.raises(ChangeRequestError, match="title"):
            submit_change_request(
                session,
                _principal(requester),
                academic_period_id=world.period.id,
                moves=[_move(published)],
                title="   ",
            )

    def test_moving_a_session_that_is_not_placed_is_refused(
        self, session: Session, world, published, chain, requester
    ) -> None:
        with pytest.raises(ChangeRequestError, match="not in the published"):
            submit_change_request(
                session,
                _principal(requester),
                academic_period_id=world.period.id,
                moves=[ProposedMove(str(uuid.uuid4()), str(published["slots"][2].id))],
                title="Fantasma",
            )

    def test_with_no_configured_chain_the_request_is_refused(
        self, session: Session, world, published, requester
    ) -> None:
        """No `chain` fixture here. The refusal is SESSION 12's requirement,
        not a gap in it."""
        with pytest.raises(ChangeRequestError, match="no approval workflow"):
            submit_change_request(
                session,
                _principal(requester),
                academic_period_id=world.period.id,
                moves=[_move(published)],
                title="Sin cadena",
            )

    def test_the_next_approver_is_notified_and_nobody_else(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """SESSION 17's first real emitter. The director is *not* told yet --
        notifying them a week early trains them to ignore the message that
        eventually matters."""
        coordinator = _user(session, UserRole.COORDINATOR, "c2@example.test")
        director = _user(session, UserRole.DIRECTOR, "d@example.test")

        submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Movimiento",
        )
        recipients = {
            n.recipient_user_id
            for n in session.scalars(
                select(Notification).where(
                    Notification.kind == NotificationKind.APPROVAL_REQUIRED
                )
            )
        }
        assert coordinator.id in recipients
        assert director.id not in recipients
        assert requester.id not in recipients, "the requester was told about their own"


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


class TestDeciding:
    def _submit(self, session, world, published, requester) -> ChangeRequest:
        result = submit_change_request(
            session,
            _principal(requester),
            academic_period_id=world.period.id,
            moves=[_move(published)],
            title="Movimiento",
        )
        return session.get(ChangeRequest, uuid.UUID(result.change_request_id))

    def test_the_chain_is_walked_to_approval(
        self, session: Session, world, published, chain, requester
    ) -> None:
        request = self._submit(session, world, published, requester)
        coordinator = _user(session, UserRole.COORDINATOR, "c3@example.test")
        director = _user(session, UserRole.DIRECTOR, "d3@example.test")

        state = decide(
            session, _principal(coordinator), request.id, ApprovalAction.APPROVE
        )
        assert state.status is ChangeRequestStatus.UNDER_REVIEW

        state = decide(
            session, _principal(director), request.id, ApprovalAction.APPROVE
        )
        assert state.status is ChangeRequestStatus.APPROVED

    def test_approval_changes_no_timetable_either(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """Applying is a separate act. Keeping it separate is what makes the
        chain mean something rather than a delay before an inevitability."""
        request = self._submit(session, world, published, requester)
        before = {
            str(a.class_session_id): str(a.start_timeslot_id)
            for a in session.scalars(select(ScheduleAssignment))
        }
        decide(
            session,
            _principal(_user(session, UserRole.COORDINATOR, "c4@example.test")),
            request.id,
            ApprovalAction.APPROVE,
        )
        decide(
            session,
            _principal(_user(session, UserRole.DIRECTOR, "d4@example.test")),
            request.id,
            ApprovalAction.APPROVE,
        )
        after = {
            str(a.class_session_id): str(a.start_timeslot_id)
            for a in session.scalars(select(ScheduleAssignment))
        }
        assert after == before

    def test_the_requester_cannot_decide_their_own(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """A coordinator who raises a request and approves it has produced a
        record of an approval that never happened."""
        request = self._submit(session, world, published, requester)
        with pytest.raises(ChangeRequestError, match="cannot also decide"):
            decide(
                session, _principal(requester), request.id, ApprovalAction.APPROVE
            )

    def test_the_wrong_role_cannot_satisfy_a_step(
        self, session: Session, world, published, chain, requester
    ) -> None:
        request = self._submit(session, world, published, requester)
        director = _user(session, UserRole.DIRECTOR, "d5@example.test")
        with pytest.raises(ChangeRequestError, match="requires COORDINATOR"):
            decide(session, _principal(director), request.id, ApprovalAction.APPROVE)

    def test_an_ai_may_not_approve(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """Guarded twice on purpose -- an approval reaching the state machine
        without passing RBAC is exactly the case worth catching."""
        request = self._submit(session, world, published, requester)
        model = _user(session, UserRole.COORDINATOR, "model@example.test")
        with pytest.raises(ChangeRequestError, match="must be given by a person"):
            decide(
                session,
                _principal(model, ActorKind.AI_MODEL),
                request.id,
                ApprovalAction.APPROVE,
            )

    def test_a_rejection_must_say_why_and_ends_the_request(
        self, session: Session, world, published, chain, requester
    ) -> None:
        request = self._submit(session, world, published, requester)
        coordinator = _user(session, UserRole.COORDINATOR, "c6@example.test")

        with pytest.raises(ChangeRequestError, match="must say why"):
            decide(
                session, _principal(coordinator), request.id, ApprovalAction.REJECT
            )

        state = decide(
            session,
            _principal(coordinator),
            request.id,
            ApprovalAction.REJECT,
            comment="choca con el laboratorio",
        )
        assert state.status is ChangeRequestStatus.REJECTED

    def test_the_requester_is_told_the_outcome(
        self, session: Session, world, published, chain, requester
    ) -> None:
        request = self._submit(session, world, published, requester)
        coordinator = _user(session, UserRole.COORDINATOR, "c7@example.test")
        decide(
            session,
            _principal(coordinator),
            request.id,
            ApprovalAction.REJECT,
            comment="no cabe",
        )
        kinds = {
            n.kind
            for n in session.scalars(
                select(Notification).where(
                    Notification.recipient_user_id == requester.id
                )
            )
        }
        assert NotificationKind.REQUEST_REJECTED in kinds

    def test_asking_for_help_records_but_does_not_advance(
        self, session: Session, world, published, chain, requester
    ) -> None:
        """Consuming a step would let a request reach APPROVED because
        somebody asked a question."""
        request = self._submit(session, world, published, requester)
        coordinator = _user(session, UserRole.COORDINATOR, "c8@example.test")

        state = decide(
            session,
            _principal(coordinator),
            request.id,
            ApprovalAction.REQUEST_AI_ALTERNATIVE,
        )
        pending = next_pending_step(state)
        assert pending is not None and pending.step.step_number == 1

    def test_the_decision_trail_is_kept(
        self, session: Session, world, published, chain, requester
    ) -> None:
        request = self._submit(session, world, published, requester)
        coordinator = _user(session, UserRole.COORDINATOR, "c9@example.test")
        decide(session, _principal(coordinator), request.id, ApprovalAction.APPROVE)

        state = load_state(session, request.id)
        assert [d.actor_user_id for d in state.decisions] == [str(coordinator.id)]
        assert state.decisions[0].action is ApprovalAction.APPROVE


class TestApprovalRoutingNarrows:
    """SESSION 23: ask the coordinators it affects, not all of them.

    `_affected_by_role` returned *every* coordinator until now, because the
    schema carried no programme-to-user mapping. SESSION 18 recorded that as a
    deliberate widening rather than an oversight; `user_scope_assignment` is
    the mapping it was waiting for.
    """

    def _coordinator(self, session, suffix: str):
        import uuid as _uuid

        from academic_ai.domain.enums import UserRole
        from academic_ai.infrastructure.db.models import User

        user = User(
            email=f"coord-{suffix}-{_uuid.uuid4().hex[:6]}@example.test",
            full_name=f"Coordinador {suffix}",
            primary_role=UserRole.COORDINATOR,
        )
        session.add(user)
        session.flush()
        return user

    def _assign(self, session, user, program_id) -> None:
        from academic_ai.infrastructure.db.models import UserScopeAssignment

        session.add(
            UserScopeAssignment(app_user_id=user.id, program_id=program_id)
        )
        session.flush()

    class _Request:
        """Just enough of a ChangeRequest for the routing function."""

        def __init__(self, impact: dict) -> None:
            self.impact = impact
            self.code = "CR-TEST"

    def test_only_the_affected_programmes_coordinators_are_asked(
        self, session, world
    ) -> None:
        from academic_ai.application.change_requests import _affected_by_role

        mine = self._coordinator(session, "mine")
        theirs = self._coordinator(session, "theirs")
        self._assign(session, mine, world.program.id)
        self._assign(session, theirs, world.other_program.id)

        asked = _affected_by_role(
            session,
            self._Request({"affected_program_ids": [str(world.program.id)]}),
        )["COORDINATOR"]

        assert str(mine.id) in asked
        assert str(theirs.id) not in asked

    def test_a_cross_programme_change_asks_both(self, session, world) -> None:
        from academic_ai.application.change_requests import _affected_by_role

        mine = self._coordinator(session, "mine")
        theirs = self._coordinator(session, "theirs")
        self._assign(session, mine, world.program.id)
        self._assign(session, theirs, world.other_program.id)

        asked = _affected_by_role(
            session,
            self._Request(
                {
                    "affected_program_ids": [
                        str(world.program.id),
                        str(world.other_program.id),
                    ]
                }
            ),
        )["COORDINATOR"]

        assert str(mine.id) in asked
        assert str(theirs.id) in asked

    def test_narrowing_to_nobody_falls_back_to_everybody(
        self, session, world
    ) -> None:
        # The property that matters most. A `requires_all_affected` step with
        # an empty signatory list is satisfied by nobody signing — narrowing
        # to zero does not make a change harder to approve, it makes the step
        # vanish. Asking too many people is recoverable; a change passing a
        # step nobody had to sign is not, and it looks identical to working.
        from academic_ai.application.change_requests import _affected_by_role

        unassigned = self._coordinator(session, "unassigned")

        asked = _affected_by_role(
            session,
            self._Request({"affected_program_ids": [str(world.other_program.id)]}),
        )["COORDINATOR"]

        assert str(unassigned.id) in asked

    def test_a_change_recording_no_programmes_asks_everybody(
        self, session, world
    ) -> None:
        # An older request stored before the ids were kept, or one whose
        # impact genuinely touched none. Either way, erring wide.
        from academic_ai.application.change_requests import _affected_by_role

        mine = self._coordinator(session, "mine")
        self._assign(session, mine, world.program.id)

        asked = _affected_by_role(session, self._Request({}))["COORDINATOR"]
        assert str(mine.id) in asked
