"""AI model registry, audit log, and constraint override authority.

These three enforce CLAUDE.md's human-authority rules at the schema level:
no model activates itself, no audit entry can be rewritten, and no HARD
constraint is weakened anonymously.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AIModelStatus,
    AuditAction,
    ConstraintCategory,
)
from academic_ai.infrastructure.db.models import (
    AIModel,
    AIModelVersion,
    AuditLog,
    ConstraintType,
    PlanningConstraint,
)
from tests.conftest import AcademicWorld


@pytest.fixture()
def ai_model(session: Session) -> AIModel:
    model = AIModel(
        code="lns-ranker",
        name="LNS neighborhood ranker",
        model_family="gnn",
        contract="NeighborhoodPolicy",
    )
    session.add(model)
    session.flush()
    return model


def _version(model: AIModel, version: str, **overrides: object) -> AIModelVersion:
    kwargs: dict[str, object] = {
        "ai_model_id": model.id,
        "version": version,
        "model_api_version": "1",
        "feature_schema_version": "1",
        "status": AIModelStatus.EXPERIMENTAL,
    }
    kwargs.update(overrides)
    return AIModelVersion(**kwargs)


class TestAIModelActivation:
    def test_activation_requires_a_named_human(
        self, session: Session, ai_model: AIModel
    ) -> None:
        version = _version(ai_model, "1.0.0")
        session.add(version)
        session.flush()

        version.status = AIModelStatus.ACTIVE
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.flush()

    def test_activation_with_an_activator_is_accepted(
        self, session: Session, ai_model: AIModel, world: AcademicWorld
    ) -> None:
        version = _version(
            ai_model,
            "1.0.0",
            status=AIModelStatus.ACTIVE,
            activated_by_user_id=world.admin.id,
            activated_at=dt.datetime.now(dt.timezone.utc),
        )
        session.add(version)
        session.flush()
        assert version.status is AIModelStatus.ACTIVE

    def test_only_one_version_may_be_active_per_model(
        self, session: Session, ai_model: AIModel, world: AcademicWorld
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        session.add(
            _version(
                ai_model,
                "1.0.0",
                status=AIModelStatus.ACTIVE,
                activated_by_user_id=world.admin.id,
                activated_at=now,
            )
        )
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _version(
                        ai_model,
                        "2.0.0",
                        status=AIModelStatus.ACTIVE,
                        activated_by_user_id=world.admin.id,
                        activated_at=now,
                    )
                )
                session.flush()

    def test_many_non_active_versions_coexist(
        self, session: Session, ai_model: AIModel
    ) -> None:
        session.add_all(
            [
                _version(ai_model, "1.0.0", status=AIModelStatus.DEPRECATED),
                _version(ai_model, "2.0.0", status=AIModelStatus.TESTING),
                _version(ai_model, "3.0.0", status=AIModelStatus.READY),
            ]
        )
        session.flush()
        assert len(ai_model.versions) == 3


class TestAuditLogIsAppendOnly:
    def _entry(self, world: AcademicWorld) -> AuditLog:
        return AuditLog(
            actor_user_id=world.admin.id,
            action=AuditAction.UPDATE,
            entity_type="course",
            entity_id=world.course.id,
            before={"name": "old"},
            after={"name": "new"},
        )

    def test_entries_can_be_written(
        self, session: Session, world: AcademicWorld
    ) -> None:
        entry = self._entry(world)
        session.add(entry)
        session.flush()
        assert entry.id is not None

    def test_entries_cannot_be_updated(
        self, session: Session, world: AcademicWorld
    ) -> None:
        entry = self._entry(world)
        session.add(entry)
        session.flush()

        entry.after = {"name": "tampered"}
        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.flush()

    def test_entries_cannot_be_deleted(
        self, session: Session, world: AcademicWorld
    ) -> None:
        entry = self._entry(world)
        session.add(entry)
        session.flush()

        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.delete(entry)
                session.flush()

    def test_an_automated_actor_is_recorded_by_description(
        self, session: Session, world: AcademicWorld
    ) -> None:
        """Imports, migrations and the optimizer worker have no user."""
        entry = AuditLog(
            actor_user_id=None,
            actor_description="optimizer-worker",
            action=AuditAction.OPTIMIZE,
            entity_type="optimization_run",
        )
        session.add(entry)
        session.flush()
        assert entry.actor_user_id is None
        assert entry.actor_description == "optimizer-worker"


class TestConstraintOverrideAuthority:
    @pytest.fixture()
    def constraint_type(self, session: Session) -> ConstraintType:
        ct = ConstraintType(
            code="TEACHER_MAX_DAILY_HOURS",
            name="Teacher daily hour limit",
            default_category=ConstraintCategory.MEDIUM,
            is_category_overridable=True,
        )
        session.add(ct)
        session.flush()
        return ct

    def test_an_override_requires_an_authority_and_a_reason(
        self, session: Session, world: AcademicWorld, constraint_type: ConstraintType
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    PlanningConstraint(
                        constraint_type_id=constraint_type.id,
                        academic_period_id=world.period.id,
                        category_override=ConstraintCategory.SOFT,
                    )
                )
                session.flush()

    def test_a_documented_override_is_accepted(
        self, session: Session, world: AcademicWorld, constraint_type: ConstraintType
    ) -> None:
        constraint = PlanningConstraint(
            constraint_type_id=constraint_type.id,
            academic_period_id=world.period.id,
            category_override=ConstraintCategory.SOFT,
            overridden_by_user_id=world.admin.id,
            override_reason="Acuerdo de facultad 2026-03",
        )
        session.add(constraint)
        session.flush()
        assert constraint.category_override is ConstraintCategory.SOFT

    def test_no_override_needs_no_authority(
        self, session: Session, world: AcademicWorld, constraint_type: ConstraintType
    ) -> None:
        constraint = PlanningConstraint(
            constraint_type_id=constraint_type.id,
            academic_period_id=world.period.id,
        )
        session.add(constraint)
        session.flush()
        assert constraint.category_override is None

    def test_duplicate_institution_wide_constraints_are_rejected(
        self, session: Session, constraint_type: ConstraintType
    ) -> None:
        """All scope columns NULL — the NULLS NOT DISTINCT case again."""
        session.add(PlanningConstraint(constraint_type_id=constraint_type.id))
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    PlanningConstraint(constraint_type_id=constraint_type.id)
                )
                session.flush()
