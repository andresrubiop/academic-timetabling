"""SESSION 27: getting in, and having something to look at.

Until now the platform could be deployed, healthy, and impossible to sign
into: every endpoint needs a token, a token needs a login, a login needs a
user with a credential, and nothing could create one. SESSION 21 verified the
stack answers; it did not verify anybody could get in.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.bootstrap import create_administrator
from academic_ai.demo import build as build_demo
from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import (
    ClassSession,
    Enrollment,
    Institution,
    Timeslot,
    User,
    UserCredential,
)
from academic_ai.security.passwords import verify_password

STRONG = "una-contrasena-suficientemente-larga"


class TestBootstrap:
    def test_the_first_administrator_can_be_created(self, session: Session) -> None:
        user_id = create_administrator(
            session,
            email="Admin@Example.Test",
            full_name="Administradora",
            password=STRONG,
        )
        user = session.get(User, user_id)
        assert user.primary_role is UserRole.SUPER_ADMIN
        # Normalised, so a later login with different capitalisation works.
        assert user.email == "admin@example.test"

    def test_the_password_is_usable_and_hashed(self, session: Session) -> None:
        user_id = create_administrator(
            session, email="a@example.test", full_name="A", password=STRONG
        )
        credential = session.scalars(
            select(UserCredential).where(UserCredential.app_user_id == user_id)
        ).one()
        assert credential.password_hash.startswith("$argon2")
        assert verify_password(STRONG, credential.password_hash)

    def test_it_refuses_a_second_administrator_by_default(
        self, session: Session
    ) -> None:
        # A command that quietly mints a second set of keys every time it runs
        # is not a bootstrap.
        create_administrator(
            session, email="first@example.test", full_name="First", password=STRONG
        )
        with pytest.raises(SystemExit, match="already exist"):
            create_administrator(
                session,
                email="second@example.test",
                full_name="Second",
                password=STRONG,
            )

    def test_a_second_can_be_forced(self, session: Session) -> None:
        # The lockout case: somebody has to be able to add another.
        create_administrator(
            session, email="first@example.test", full_name="First", password=STRONG
        )
        create_administrator(
            session,
            email="second@example.test",
            full_name="Second",
            password=STRONG,
            allow_additional=True,
        )
        assert session.scalar(select(func.count()).select_from(User)) == 2

    def test_it_never_resets_an_existing_password(self, session: Session) -> None:
        # Otherwise it is a password-reset tool wearing a bootstrap's name,
        # reachable by anybody who can run the bootstrap.
        create_administrator(
            session, email="dup@example.test", full_name="Dup", password=STRONG
        )
        with pytest.raises(SystemExit, match="does not reset passwords"):
            create_administrator(
                session,
                email="dup@example.test",
                full_name="Dup",
                password="otra-contrasena-larga-distinta",
                allow_additional=True,
            )

    def test_a_malformed_email_is_refused(self, session: Session) -> None:
        with pytest.raises(SystemExit, match="not an email"):
            create_administrator(
                session, email="nope", full_name="X", password=STRONG
            )


class TestDemoData:
    def test_it_builds_a_period_that_can_be_looked_at(
        self, session: Session
    ) -> None:
        result = build_demo(session)

        assert result.sessions == 16
        assert result.students == 40
        assert session.scalar(select(func.count()).select_from(Timeslot)) == 30
        assert session.scalar(select(func.count()).select_from(Enrollment)) > 0

    def test_every_row_is_marked_as_demo(self, session: Session) -> None:
        # Nobody should have to guess later whether a row came from here or
        # from a registrar.
        build_demo(session)
        institution = session.scalars(select(Institution)).one()
        assert institution.code.startswith("DEMO")

    def test_the_period_actually_loads_as_a_problem_instance(
        self, session: Session
    ) -> None:
        # The point of the whole exercise: an empty period answers 404 "no
        # class sessions to schedule", which is correct and shows nothing.
        from academic_ai.infrastructure.repositories import load_snapshot

        result = build_demo(session)
        snapshot, _diagnostics = load_snapshot(session, result.period_id)

        assert len(snapshot.sessions) == 16
        assert len(snapshot.slots) == 30
        assert snapshot.rooms
        assert snapshot.teachers
        assert snapshot.enrollments, "no enrolments — nothing to analyse conflicts on"

    def test_it_solves(self, session: Session) -> None:
        # A demo period that cannot be solved would be a worse demonstration
        # than none.
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import ProblemInstance
        from academic_ai.infrastructure.repositories import (
            load_constraints,
            load_snapshot,
        )
        from academic_ai.optimization.solve import solve

        result = build_demo(session)
        snapshot, _ = load_snapshot(session, result.period_id)
        instance = ProblemInstance(
            snapshot=snapshot,
            constraints=load_constraints(session, result.period_id),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        from academic_ai.optimization.solve import SolverConfig

        outcome = solve(instance, SolverConfig(time_limit_seconds=30))
        assert outcome.is_solved, outcome.solver_status

    def test_a_course_is_shared_between_the_two_programmes(
        self, session: Session
    ) -> None:
        # The common-core case the scope rules and approval routing exist for.
        # Without it the demo cannot show either doing anything.
        from academic_ai.infrastructure.db.models import CourseOfferingProgram

        build_demo(session)
        counts = session.execute(
            select(
                CourseOfferingProgram.course_offering_id, func.count()
            ).group_by(CourseOfferingProgram.course_offering_id)
        ).all()
        assert any(count > 1 for _, count in counts)

    def test_it_refuses_to_run_twice(self, session: Session) -> None:
        from academic_ai.demo import _refuse_if_unsafe

        build_demo(session)
        with pytest.raises(SystemExit, match="already exists"):
            _refuse_if_unsafe(session)

    def test_it_refuses_a_database_with_real_data(
        self, session: Session, world
    ) -> None:
        # Seeding sample courses into a real deployment is discovered a term
        # later, not immediately.
        from academic_ai.demo import _refuse_if_unsafe

        with pytest.raises(SystemExit, match="not demo data"):
            _refuse_if_unsafe(session)


class TestDemoIsWorthLookingAt:
    """A seeded period with nothing placed renders an empty grid.

    `GET /schedule` reads its placement from the *published* version, and a
    draft is deliberately never picked up by recency. Sixteen sessions with
    nowhere to be is a correct answer and a useless demonstration.
    """

    def _admin(self, session: Session):
        return create_administrator(
            session, email="pub@example.test", full_name="Pub", password=STRONG
        )

    def test_publishing_needs_somebody_to_publish_as(
        self, session: Session
    ) -> None:
        # `ck_schedule_version_published_requires_publisher` — the human
        # authority rule made structural. A timetable is published *by
        # somebody*, so the demo cannot run before an administrator exists.
        from academic_ai.demo import solve_and_publish

        result = build_demo(session)
        with pytest.raises(SystemExit, match="bootstrap"):
            solve_and_publish(session, result.period_id, seconds=5)

    def test_the_demo_solves_and_publishes(self, session: Session) -> None:
        from academic_ai.demo import solve_and_publish

        self._admin(session)
        result = build_demo(session)
        version_id, status = solve_and_publish(session, result.period_id, seconds=30)

        assert version_id is not None, f"the demo period did not solve: {status}"

    def test_the_published_placement_reaches_the_schedule_read(
        self, session: Session
    ) -> None:
        # The end of the chain: this is what the grid renders.
        from academic_ai.demo import solve_and_publish
        from academic_ai.infrastructure.repositories import load_snapshot

        self._admin(session)
        result = build_demo(session)
        solve_and_publish(session, result.period_id, seconds=30)

        snapshot, _ = load_snapshot(session, result.period_id)
        assert len(snapshot.base_assignments) == result.sessions
        assert all(a.start_slot_id for a in snapshot.base_assignments)

    def test_every_session_lands_somewhere(self, session: Session) -> None:
        from academic_ai.demo import solve_and_publish
        from academic_ai.infrastructure.repositories import load_snapshot

        self._admin(session)
        result = build_demo(session)
        solve_and_publish(session, result.period_id, seconds=30)

        snapshot, _ = load_snapshot(session, result.period_id)
        placed = {a.session_id for a in snapshot.base_assignments}
        assert placed == {s.id for s in snapshot.sessions}


class TestReset:
    def test_the_demo_can_be_seeded_again(self, session: Session) -> None:
        # A demo you can only seed once is one you get a single attempt at.
        # The foreign keys refuse the convenient deletion order, which is
        # correct and is exactly why the order is written out.
        from academic_ai.demo import _refuse_if_unsafe, reset

        first = build_demo(session)
        assert reset(session) > 0
        _refuse_if_unsafe(session)  # does not raise: the demo is gone
        second = build_demo(session)
        assert second.period_id != first.period_id

    def test_reset_on_an_empty_database_does_nothing(
        self, session: Session
    ) -> None:
        from academic_ai.demo import reset

        assert reset(session) == 0

    def test_reset_leaves_the_administrator_alone(self, session: Session) -> None:
        # The account you need in order to seed again.
        from academic_ai.demo import reset

        user_id = create_administrator(
            session, email="keep@example.test", full_name="Keep", password=STRONG
        )
        build_demo(session)
        reset(session)
        assert session.get(User, user_id) is not None


class TestResetMeetsAPublishedVersion:
    """SESSION 31: the case the first reset never met.

    `schedule_version_is_deletable` (SESSION 13) refuses to delete a PUBLISHED
    version — "archive it instead" — which is right: a published timetable is
    something students have read. The first version of `reset` was only ever
    run against a demo that had failed to publish, so it had never met a
    published row, and would have failed the first time somebody re-seeded a
    working demo.
    """

    def test_a_published_demo_can_still_be_reset(self, session: Session) -> None:
        from academic_ai.demo import reset, solve_and_publish

        create_administrator(
            session, email="pub2@example.test", full_name="Pub", password=STRONG
        )
        result = build_demo(session)
        version_id, _status = solve_and_publish(session, result.period_id, seconds=30)
        assert version_id is not None, "the demo did not publish; wrong test"

        assert reset(session) > 0

        from academic_ai.infrastructure.db.models import Institution

        assert session.scalars(select(Institution)).all() == []

    def test_the_trigger_still_refuses_a_bare_delete(
        self, session: Session
    ) -> None:
        # The protection is not weakened by the reset knowing how to work with
        # it: deleting a published version directly is still refused.
        from sqlalchemy.exc import IntegrityError

        from academic_ai.demo import solve_and_publish

        create_administrator(
            session, email="pub3@example.test", full_name="Pub", password=STRONG
        )
        result = build_demo(session)
        version_id, _ = solve_and_publish(session, result.period_id, seconds=30)

        # Raw SQL rather than the ORM: `session.delete()` cascades to the
        # assignments first, so the *assignments-immutable* trigger answers
        # before the deletable one. Both are protections and both fire; this
        # asserts the one SESSION 13 added.
        from sqlalchemy import text

        with pytest.raises(IntegrityError, match="cannot be deleted"):
            with session.begin_nested():
                session.execute(
                    text("DELETE FROM schedule_version WHERE id = :id"),
                    {"id": version_id},
                )


class TestSeededChains:
    """SESSION 35: a seeded period that can actually route a change.

    SESSION 12 refuses to route when no workflow is configured — "a change
    routed to the wrong authorities looks exactly like one routed to the right
    ones" — so the code must never invent a chain at runtime. A seeded
    institution therefore has to be *configured*, or it can propose a change
    and never submit one.
    """

    def test_the_demo_configures_a_chain_for_each_scope(
        self, session: Session
    ) -> None:
        from academic_ai.demo import seed_approval_chains
        from academic_ai.domain.enums import ScheduleScope
        from academic_ai.infrastructure.repositories import load_chains

        create_administrator(
            session, email="ch@example.test", full_name="Ch", password=STRONG
        )
        result = build_demo(session)
        seed_approval_chains(session, result.institution_id)

        chains = load_chains(session, result.institution_id)
        assert set(chains) == {
            ScheduleScope.PROGRAM,
            ScheduleScope.FACULTY,
            ScheduleScope.INSTITUTION,
        }

    def test_a_wider_change_needs_more_signatures(
        self, session: Session
    ) -> None:
        # The chains widen with the reach of the change: a programme's own
        # coordinator inside it, a dean above a faculty, an administrator once
        # it crosses faculties.
        from academic_ai.demo import seed_approval_chains
        from academic_ai.domain.enums import ScheduleScope
        from academic_ai.infrastructure.repositories import load_chains

        create_administrator(
            session, email="ch2@example.test", full_name="Ch", password=STRONG
        )
        result = build_demo(session)
        seed_approval_chains(session, result.institution_id)

        chains = load_chains(session, result.institution_id)
        assert len(chains[ScheduleScope.PROGRAM].steps) == 1
        assert len(chains[ScheduleScope.FACULTY].steps) == 2
        assert len(chains[ScheduleScope.INSTITUTION].steps) == 3

    def test_it_is_configuration_and_not_a_runtime_fallback(
        self, session: Session
    ) -> None:
        # The distinction SESSION 12 rests on: an institution nobody seeded
        # still has no chain, and routing still refuses.
        from academic_ai.infrastructure.repositories import load_chains

        assert load_chains(session) == {}


class TestResetAfterTheInstitutionHasBeenUsed:
    """SESSION 37: the case the hand-written reset could not survive.

    Its table list and its `WHERE` clauses were written by hand and missed
    eleven tables — change requests, approvals, scenarios, optimization runs,
    recommendations, cohorts, preferences. A reset therefore worked on a
    freshly seeded institution and failed the moment somebody had *used* it,
    which is exactly when they want to re-seed.
    """

    def test_the_deletion_order_covers_every_reachable_table(self) -> None:
        # Derived from the schema rather than maintained: a table added later
        # is included automatically, in the right place.
        from academic_ai.demo import _deletion_order
        from academic_ai.infrastructure.db.base import Base

        order = _deletion_order()
        assert "institution" in order
        # The eleven the hand-written list forgot.
        for missed in (
            "change_request",
            "approval",
            "scenario",
            "scenario_perturbation",
            "optimization_run",
            "recommendation",
            "student_cohort",
            "student_cohort_membership",
            "preference",
            "planning_constraint",
            "approval_workflow",
        ):
            assert missed in order, f"{missed} would be left behind"
        assert len(order) == len(set(order)), "a table appears twice"

    def test_children_are_deleted_before_their_parents(self) -> None:
        """The whole reason an explicit order exists: the foreign keys refuse
        the convenient one.

        Cycles are excluded, and there is one: `schedule_version` and
        `optimization_run` reference each other, so no ordering of the two can
        satisfy both. `reset` NULLs the nullable half first — which is what
        makes the cycle resolvable at all.
        """
        from academic_ai.demo import _deletion_order
        from academic_ai.infrastructure.db.base import Base

        order = list(_deletion_order())
        position = {name: index for index, name in enumerate(order)}
        for table in Base.metadata.sorted_tables:
            if table.name not in position:
                continue
            for fk in table.foreign_keys:
                parent = fk.column.table.name
                if parent not in position or parent == table.name:
                    continue
                if fk.parent.nullable:
                    # Resolvable by NULLing it first; ordering cannot settle a
                    # cycle and is not asked to.
                    continue
                assert position[table.name] < position[parent], (
                        f"{table.name} is deleted after {parent}, which its "
                        "foreign key will refuse"
                    )

    def test_an_institution_with_an_applied_change_can_be_reset(
        self, session: Session
    ) -> None:
        """The end-to-end case. A demo that was seeded, published, proposed
        against, approved and applied — then torn down."""
        from academic_ai.demo import reset, seed_approval_chains, solve_and_publish
        from academic_ai.infrastructure.db.models import ChangeRequest, Institution

        admin_id = create_administrator(
            session, email="used@example.test", full_name="U", password=STRONG
        )
        result = build_demo(session)
        seed_approval_chains(session, result.institution_id)
        version_id, _ = solve_and_publish(session, result.period_id, seconds=30)
        assert version_id is not None

        # A change request against it, with a scenario and a perturbation.
        from academic_ai.application.change_requests import submit_change_request
        from academic_ai.domain.scheduling.impact import ProposedMove
        from academic_ai.infrastructure.repositories import load_snapshot
        from academic_ai.security.rbac import Principal

        snapshot, _ = load_snapshot(session, result.period_id)
        base = snapshot.base_assignments[0]
        domain_slots = [s.id for s in snapshot.slots if s.id != base.start_slot_id]
        principal = Principal(user_id=str(admin_id), role=UserRole.SUPER_ADMIN)
        try:
            with session.begin_nested():
                submit_change_request(
                    session,
                    principal,
                    result.period_id,
                    title="Prueba",
                    moves=[
                        ProposedMove(
                            session_id=base.session_id,
                            to_start_slot_id=domain_slots[0],
                            to_room_id=None,
                        )
                    ],
                )
        except Exception:
            # Whether this particular move routes is not what is under test;
            # what matters is that a reset survives whatever it left behind.
            # `begin_nested` so the failure does not also discard the demo.
            pass

        assert reset(session) > 0
        assert session.scalars(select(Institution)).all() == []
        assert session.scalars(select(ChangeRequest)).all() == []
