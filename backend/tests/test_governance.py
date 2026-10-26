"""SESSION 12: who decides a change, and the ways that could go wrong.

The playbook's hard requirement is "do not hardcode one universal chain", and
the way this code honours it is by not deciding who approves anything. It
decides how far a change *reaches* -- a measurable fact -- and looks up the
chain an institution configured for that reach. `test_no_configured_chain_is_refused`
is the test that keeps it honest: a fallback default is a universal chain with
extra steps, and it fails silently, because a change routed to the wrong
authorities looks exactly like one routed to the right ones.

The rest are attempts to get an approval that should not exist: approving your
own request, approving out of order, one coordinator standing in for three,
and a model recording an APPROVE.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    ApprovalAction,
    ChangeRequestStatus,
    ScheduleScope,
    UserRole,
)
from academic_ai.domain.governance import (
    ApprovalDecision,
    Chain,
    ChainStep,
    GovernanceError,
    RequestState,
    apply_action,
    assess_scope,
    next_pending_step,
    permitted_actors,
    required_signatories,
    resolve_chain,
)
from academic_ai.domain.scheduling.impact import (
    AffectedCounts,
    ConflictChange,
    ImpactReport,
)

IMPACT = {"affected": {"students": 12}}


def _report(**kwargs) -> ImpactReport:
    counts = AffectedCounts(**kwargs.pop("affected", {}))
    return ImpactReport(moves=(), affected=counts, **kwargs)


def _chain(*steps: ChainStep, code: str = "C") -> Chain:
    return Chain(code=code, steps=steps)


WITHIN_PROGRAM = _chain(
    ChainStep(1, UserRole.COORDINATOR),
    ChainStep(2, UserRole.DIRECTOR),
    code="WITHIN_PROGRAM",
)
CROSS_PROGRAM = _chain(
    ChainStep(1, UserRole.COORDINATOR, requires_all_affected=True),
    ChainStep(2, UserRole.DIRECTOR),
    code="CROSS_PROGRAM",
)


def _state(chain: Chain = WITHIN_PROGRAM, **kwargs) -> RequestState:
    defaults = dict(
        request_id="cr-1",
        requester_user_id="requester",
        status=ChangeRequestStatus.SUBMITTED,
        chain=chain,
        impact=IMPACT,
    )
    defaults.update(kwargs)
    return RequestState(**defaults)


def _decide(step: int, actor: str, role: UserRole, action=ApprovalAction.APPROVE, **kw):
    return ApprovalDecision(
        step_number=step, actor_user_id=actor, actor_role=role, action=action, **kw
    )


# ---------------------------------------------------------------------------
# The chain is data
# ---------------------------------------------------------------------------


class TestChainsAreNotHardcoded:
    def test_no_configured_chain_is_refused_not_defaulted(self) -> None:
        """The requirement, as an assertion.

        A fallback default is a universal chain with extra steps: it runs
        whenever configuration is missing, which on the day somebody adds a
        faculty is every cross-faculty change in the institution.
        """
        assessment = assess_scope(_report(affected={"programs": 3, "faculties": 2}))
        with pytest.raises(GovernanceError, match="no approval workflow is configured"):
            resolve_chain(assessment, {ScheduleScope.PROGRAM: WITHIN_PROGRAM})

    def test_a_narrower_chain_does_not_stand_in_for_a_wider_one(self) -> None:
        """Substituting the programme chain would route a cross-programme
        change to somebody who cannot decide it, and report success."""
        assessment = assess_scope(_report(affected={"programs": 2, "faculties": 1}))
        assert assessment.scope is ScheduleScope.FACULTY
        with pytest.raises(GovernanceError):
            resolve_chain(assessment, {ScheduleScope.PROGRAM: WITHIN_PROGRAM})

    def test_the_refusal_names_what_is_missing_and_why(self) -> None:
        assessment = assess_scope(_report(affected={"programs": 3, "faculties": 2}))
        with pytest.raises(GovernanceError) as exc:
            resolve_chain(assessment, {})
        message = str(exc.value)
        assert "INSTITUTION" in message
        assert "faculties" in message

    def test_a_configured_chain_is_returned_unchanged(self) -> None:
        assessment = assess_scope(_report(affected={"programs": 1, "faculties": 1}))
        assert (
            resolve_chain(assessment, {ScheduleScope.PROGRAM: WITHIN_PROGRAM})
            is WITHIN_PROGRAM
        )

    def test_an_empty_chain_is_refused_at_construction(self) -> None:
        """It would approve every request it was given without anybody
        looking."""
        with pytest.raises(GovernanceError, match="no steps"):
            Chain(code="EMPTY", steps=())

    def test_duplicate_step_numbers_are_refused(self) -> None:
        with pytest.raises(GovernanceError, match="duplicate step"):
            Chain(
                code="X",
                steps=(ChainStep(1, UserRole.DIRECTOR), ChainStep(1, UserRole.DEAN)),
            )


class TestScopeIsDerivedFromImpact:
    def test_one_programme_stays_at_programme_scope(self) -> None:
        assert (
            assess_scope(_report(affected={"programs": 1, "faculties": 1})).scope
            is ScheduleScope.PROGRAM
        )

    def test_several_programmes_escalate_to_faculty(self) -> None:
        assert (
            assess_scope(_report(affected={"programs": 2, "faculties": 1})).scope
            is ScheduleScope.FACULTY
        )

    def test_several_faculties_escalate_to_institution(self) -> None:
        assert (
            assess_scope(_report(affected={"programs": 4, "faculties": 2})).scope
            is ScheduleScope.INSTITUTION
        )

    def test_an_unresolvable_conflict_escalates_on_harm_not_breadth(self) -> None:
        """A student who can no longer complete their year is not a
        programme-level administrative matter."""
        assessment = assess_scope(
            _report(
                affected={"programs": 1, "faculties": 1},
                new_conflicts=(
                    ConflictChange("ana", "S1", "S2", "STUDENT_NO_OVERLAP_UNRESOLVABLE"),
                ),
            )
        )
        assert assessment.scope is ScheduleScope.FACULTY
        assert "no re-sectioning" in assessment.rationale

    def test_a_resolvable_conflict_does_not_escalate(self) -> None:
        assessment = assess_scope(
            _report(
                affected={"programs": 1, "faculties": 1},
                new_conflicts=(
                    ConflictChange("ana", "S1", "S2", "STUDENT_NO_OVERLAP_MANDATORY"),
                ),
            )
        )
        assert assessment.scope is ScheduleScope.PROGRAM

    def test_the_assessment_explains_itself(self) -> None:
        """A coordinator who expected two steps and got four needs to see what
        widened it, not conclude the system is arbitrary."""
        assert assess_scope(_report(affected={"programs": 3, "faculties": 1})).rationale


# ---------------------------------------------------------------------------
# Approvals nobody should be able to get
# ---------------------------------------------------------------------------


class TestApprovalsThatMustBeRefused:
    def test_nobody_approves_their_own_request(self) -> None:
        """A director who proposes a change and then approves it has produced
        a record of an approval that never happened."""
        state = _state(requester_user_id="dir-1")
        with pytest.raises(GovernanceError, match="cannot also decide it"):
            apply_action(state, _decide(1, "dir-1", UserRole.COORDINATOR))

    def test_a_request_with_no_impact_analysis_cannot_advance(self) -> None:
        """The playbook: every request requires ImpactAnalysis. The column
        defaults to '{}', so without this a request could be approved on no
        evidence and look identical to one approved on plenty."""
        state = _state(impact={})
        with pytest.raises(GovernanceError, match="no impact analysis"):
            apply_action(state, _decide(1, "c1", UserRole.COORDINATOR))

    def test_steps_cannot_be_taken_out_of_order(self) -> None:
        """A later authority reviewing before an earlier one is reviewing a
        different request."""
        with pytest.raises(GovernanceError, match="waiting on step 1"):
            apply_action(_state(), _decide(2, "d1", UserRole.DIRECTOR))

    def test_the_wrong_role_cannot_satisfy_a_step(self) -> None:
        with pytest.raises(GovernanceError, match="requires COORDINATOR"):
            apply_action(_state(), _decide(1, "t1", UserRole.TEACHER))

    def test_an_ai_may_not_record_an_approval(self) -> None:
        """RBAC refuses this too. Guarded twice on purpose: an approval that
        reached here without passing RBAC is exactly the case worth catching."""
        with pytest.raises(GovernanceError, match="must be given by a person"):
            apply_action(
                _state(),
                _decide(1, "model-7", UserRole.COORDINATOR),
                actor_is_human=False,
            )

    def test_an_ai_may_still_suggest_alternatives(self) -> None:
        """Asking a model for options is guidance, which CLAUDE.md permits.
        Only the approval is forbidden."""
        result = apply_action(
            _state(),
            _decide(1, "model-7", UserRole.COORDINATOR,
                    action=ApprovalAction.REQUEST_AI_ALTERNATIVE),
            actor_is_human=False,
        )
        assert result.status is ChangeRequestStatus.UNDER_REVIEW

    def test_a_rejection_must_say_why(self) -> None:
        """A refusal with no reason cannot be answered, so the requester's only
        option is to submit it again."""
        with pytest.raises(GovernanceError, match="must say why"):
            apply_action(
                _state(), _decide(1, "c1", UserRole.COORDINATOR,
                                  action=ApprovalAction.REJECT)
            )

    def test_a_decided_request_cannot_be_acted_on_again(self) -> None:
        state = _state(status=ChangeRequestStatus.APPROVED)
        with pytest.raises(GovernanceError, match="already APPROVED"):
            apply_action(state, _decide(1, "c1", UserRole.COORDINATOR))


class TestEveryAffectedAuthorityMustSign:
    """The playbook's cross-program example: "affected Coordinators", plural."""

    def _cross(self, **kwargs) -> RequestState:
        return _state(
            CROSS_PROGRAM,
            required_signatories={1: frozenset({"c-maths", "c-physics", "c-chem"})},
            **kwargs,
        )

    def test_one_coordinator_does_not_satisfy_the_step(self) -> None:
        """A step that collected one approval and moved on would let a change
        damaging three programmes be waved through by the one it helps."""
        state = self._cross()
        state = apply_action(state, _decide(1, "c-maths", UserRole.COORDINATOR))
        pending = next_pending_step(state)
        assert pending is not None
        assert pending.step.step_number == 1
        assert pending.outstanding == frozenset({"c-physics", "c-chem"})

    def test_all_of_them_advance_it(self) -> None:
        state = self._cross()
        for coordinator in ("c-maths", "c-physics", "c-chem"):
            state = apply_action(state, _decide(1, coordinator, UserRole.COORDINATOR))
        pending = next_pending_step(state)
        assert pending is not None and pending.step.step_number == 2

    def test_an_uninvolved_coordinator_cannot_sign(self) -> None:
        with pytest.raises(GovernanceError, match="not an outstanding signatory"):
            apply_action(self._cross(), _decide(1, "c-history", UserRole.COORDINATOR))

    def test_an_unconfigured_signatory_set_blocks_rather_than_waves_through(
        self,
    ) -> None:
        """Nobody having said who must sign is not the same as nobody needing
        to. An unconfigured multi-party step must not silently become a
        single-party one."""
        state = _state(CROSS_PROGRAM, required_signatories={})
        pending = next_pending_step(state)
        assert pending is not None
        assert pending.step.step_number == 1
        assert pending.outstanding == frozenset({"<unspecified>"})

    def test_required_signatories_are_computed_from_who_is_affected(self) -> None:
        signatories = required_signatories(
            CROSS_PROGRAM, {"COORDINATOR": ["c-a", "c-b"], "DIRECTOR": ["d-1"]}
        )
        assert signatories == {1: frozenset({"c-a", "c-b"})}


# ---------------------------------------------------------------------------
# Walking the chain
# ---------------------------------------------------------------------------


class TestTheChainIsWalked:
    def test_the_happy_path_reaches_approved(self) -> None:
        state = _state()
        state = apply_action(state, _decide(1, "c1", UserRole.COORDINATOR))
        assert state.status is ChangeRequestStatus.UNDER_REVIEW
        state = apply_action(state, _decide(2, "d1", UserRole.DIRECTOR))
        assert state.status is ChangeRequestStatus.APPROVED
        assert next_pending_step(state) is None

    def test_a_rejection_ends_it_at_any_step(self) -> None:
        state = apply_action(
            _state(),
            _decide(1, "c1", UserRole.COORDINATOR,
                    action=ApprovalAction.REJECT, comment="clashes with the lab"),
        )
        assert state.status is ChangeRequestStatus.REJECTED
        assert state.is_terminal

    def test_a_counterproposal_returns_it_rather_than_rejecting_it(self) -> None:
        """Collapsing the two would lose the alternative the reviewer took the
        trouble to write."""
        state = apply_action(
            _state(),
            _decide(1, "c1", UserRole.COORDINATOR,
                    action=ApprovalAction.COUNTERPROPOSE, comment="try Thursday"),
        )
        assert state.status is ChangeRequestStatus.COUNTERPROPOSED
        assert not state.is_terminal

    @pytest.mark.parametrize(
        "action",
        [ApprovalAction.REQUEST_AI_ALTERNATIVE, ApprovalAction.REQUEST_REOPTIMIZATION],
    )
    def test_asking_for_help_does_not_consume_a_step(
        self, action: ApprovalAction
    ) -> None:
        """It is recorded and decides nothing. Consuming a step would let a
        request reach APPROVED because somebody asked a question."""
        state = apply_action(_state(), _decide(1, "c1", UserRole.COORDINATOR, action=action))
        pending = next_pending_step(state)
        assert pending is not None and pending.step.step_number == 1
        assert state.status is ChangeRequestStatus.UNDER_REVIEW

    def test_only_approve_can_reach_approved(self) -> None:
        """Exhaustive over the actions, so a future edit that adds a second
        road to APPROVED fails here rather than in production."""
        for action in ApprovalAction:
            state = _state(
                _chain(ChainStep(1, UserRole.DIRECTOR), code="ONE_STEP")
            )
            try:
                result = apply_action(
                    state,
                    _decide(1, "d1", UserRole.DIRECTOR, action=action,
                            comment="because"),
                )
            except GovernanceError:
                continue
            if result.status is ChangeRequestStatus.APPROVED:
                assert action is ApprovalAction.APPROVE, (
                    f"{action.value} reached APPROVED"
                )

    def test_permitted_actors_matches_the_pending_step(self) -> None:
        assert permitted_actors(_state()) == (UserRole.COORDINATOR,)
        state = apply_action(_state(), _decide(1, "c1", UserRole.COORDINATOR))
        assert permitted_actors(state) == (UserRole.DIRECTOR,)

    def test_a_terminal_request_permits_nobody(self) -> None:
        assert permitted_actors(_state(status=ChangeRequestStatus.APPROVED)) == ()

    def test_the_decision_trail_is_kept_whole(self) -> None:
        """A transition recorded only as its result cannot be checked against
        what it claimed to do."""
        state = _state()
        state = apply_action(state, _decide(1, "c1", UserRole.COORDINATOR))
        state = apply_action(state, _decide(2, "d1", UserRole.DIRECTOR))
        assert [d.actor_user_id for d in state.decisions] == ["c1", "d1"]
        assert all(d.at is not None for d in state.decisions)


# ---------------------------------------------------------------------------
# The schema states the same rules again
# ---------------------------------------------------------------------------


class TestSchemaEnforcement:
    def test_a_submitted_request_without_impact_is_rejected(
        self, session: Session, world
    ) -> None:
        """NOT NULL was not enough: the column defaults to '{}', so a request
        approved on no evidence would look identical to one approved on
        plenty."""
        from academic_ai.domain.enums import ScheduleVersionStatus
        from academic_ai.infrastructure.db.models import (
            ApprovalWorkflow,
            ChangeRequest,
            Schedule,
            ScheduleVersion,
        )

        workflow = ApprovalWorkflow(
            institution_id=world.institution.id,
            code="WP",
            name="Within programme",
            applies_to_scope=ScheduleScope.PROGRAM,
        )
        schedule = Schedule(
            institution_id=world.institution.id,
            academic_period_id=world.period.id,
            scope=ScheduleScope.PROGRAM,
            scope_program_id=world.program.id,
            code="SCH-1",
            name="Programme schedule",
        )
        session.add_all([workflow, schedule])
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

        session.add(
            ChangeRequest(
                academic_period_id=world.period.id,
                code="CR-1",
                title="Move MAT101",
                requested_by_user_id=world.admin.id,
                approval_workflow_id=workflow.id,
                base_schedule_version_id=version.id,
                status=ChangeRequestStatus.SUBMITTED,
                impact={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_draft_may_still_be_incomplete(
        self, session: Session, world
    ) -> None:
        """A DRAFT is a form somebody is still filling in."""
        from academic_ai.domain.enums import ScheduleVersionStatus
        from academic_ai.infrastructure.db.models import (
            ApprovalWorkflow,
            ChangeRequest,
            Schedule,
            ScheduleVersion,
        )

        workflow = ApprovalWorkflow(
            institution_id=world.institution.id,
            code="WP2",
            name="Within programme",
            applies_to_scope=ScheduleScope.PROGRAM,
        )
        schedule = Schedule(
            institution_id=world.institution.id,
            academic_period_id=world.period.id,
            scope=ScheduleScope.PROGRAM,
            scope_program_id=world.program.id,
            code="SCH-2",
            name="Programme schedule",
        )
        session.add_all([workflow, schedule])
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

        session.add(
            ChangeRequest(
                academic_period_id=world.period.id,
                code="CR-2",
                title="Draft",
                requested_by_user_id=world.admin.id,
                approval_workflow_id=workflow.id,
                base_schedule_version_id=version.id,
                status=ChangeRequestStatus.DRAFT,
                impact={},
            )
        )
        session.flush()  # must not raise

    def test_a_reoptimization_request_must_state_a_reason(
        self, session: Session, world
    ) -> None:
        """One with no reason cannot be prioritised against the others in the
        queue."""
        from academic_ai.infrastructure.db.models import ReoptimizationRequest

        session.add(
            ReoptimizationRequest(
                change_request_id=world.admin.id,  # any uuid; the CHECK fires first
                requested_by_user_id=world.admin.id,
                reason="   ",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
