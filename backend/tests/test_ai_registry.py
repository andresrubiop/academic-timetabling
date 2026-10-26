"""SESSION 7: the model registry, and the rule it exists to enforce.

The headline requirement is one sentence — *no model activates
automatically* — and almost every test here is a different way of trying to
get around it. That is deliberate. A rule stated once in a docstring and
never attacked is a rule nobody has checked.

The registry is enforced twice on purpose: `domain/ai/lifecycle.py` states it
in framework-free code a remote worker can run with no database, and the
schema states it again with a partial unique index and a CHECK. Both are
tested, because the interesting failure is the two of them disagreeing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.ai import (
    CompatibilityReport,
    FeatureSchemaRef,
    LifecycleError,
    ModelAction,
    ModelContract,
    ModelEvaluationRecord,
    ModelVersionState,
    OptimizerCompatibility,
    RuntimeEnvironment,
    apply_action,
    check_compatibility,
    permitted_actions,
)
from academic_ai.domain.enums import AIModelStatus
from academic_ai.infrastructure.db.models import (
    AIModel,
    AIModelVersion,
    FeatureSchema,
    ModelDeployment,
)

HASH_A = "a" * 32
HASH_B = "b" * 32


def _schema(version: str = "1.0.0", content_hash: str = HASH_A) -> FeatureSchemaRef:
    return FeatureSchemaRef(
        code="SCHEDULING_V1",
        version=version,
        content_hash=content_hash,
        feature_names=("criticality", "alternatives", "attempt"),
    )


def _runtime(**overrides) -> RuntimeEnvironment:
    kwargs = {
        "optimizer_version": "1.4.0",
        "model_api_version": "1",
        "feature_schema": _schema(),
    }
    kwargs.update(overrides)
    return RuntimeEnvironment(**kwargs)


def _evaluation(**overrides) -> ModelEvaluationRecord:
    kwargs = {
        "model_code": "CONFLICT_RANKER",
        "version": "1.0.0",
        "baseline_code": "HEURISTIC_CRITICALITY",
        "dataset_version": "2026-1",
        "score_delta": (0, -12),
        "tier_codes": ("STUDENT_UNRESOLVABLE", "STUDENT_MANDATORY_CONFLICTS"),
        "is_reproducible": True,
    }
    kwargs.update(overrides)
    return ModelEvaluationRecord(**kwargs)


def _ready(**overrides) -> ModelVersionState:
    """A version that has done everything short of being activated."""
    now = dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc)
    kwargs = {
        "model_code": "CONFLICT_RANKER",
        "version": "1.0.0",
        "status": AIModelStatus.READY,
        "has_artifact": True,
        "tested_at": now,
        "benchmarked_at": now,
    }
    kwargs.update(overrides)
    return ModelVersionState(**kwargs)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestNoModelActivatesAutomatically:
    def test_activation_without_an_actor_is_refused(self) -> None:
        """The whole session in one assertion.

        An unattributed activation is an automatic one with a blank where the
        name goes, so the actor is a required argument rather than a field
        somebody might forget to set.
        """
        with pytest.raises(LifecycleError, match="user id"):
            apply_action(_ready(), ModelAction.ACTIVATE)

    def test_activation_with_an_actor_records_who_and_when(self) -> None:
        state = apply_action(
            _ready(), ModelAction.ACTIVATE, actor_user_id="u-1"
        )
        assert state.status is AIModelStatus.ACTIVE
        assert state.activated_by_user_id == "u-1"
        assert state.activated_at is not None
        assert state.is_serving

    @pytest.mark.parametrize(
        "status",
        [
            AIModelStatus.EXPERIMENTAL,
            AIModelStatus.TESTING,
            AIModelStatus.ACTIVE,
            AIModelStatus.DEPRECATED,
            AIModelStatus.DISABLED,
        ],
    )
    def test_only_ready_can_be_activated(self, status: AIModelStatus) -> None:
        """READY is the state that means somebody ran TEST and BENCHMARK and
        looked at the numbers. A shortcut past it makes those states
        decorative, and the first use of the shortcut would be an incident."""
        with pytest.raises(LifecycleError, match="cannot ACTIVATE"):
            apply_action(
                _ready(status=status), ModelAction.ACTIVATE, actor_user_id="u-1"
            )

    def test_activation_needs_a_benchmark_even_from_ready(self) -> None:
        """READY without a benchmark means nobody compared this to the
        heuristic it is about to replace."""
        with pytest.raises(LifecycleError, match="BENCHMARK"):
            apply_action(
                _ready(benchmarked_at=None),
                ModelAction.ACTIVATE,
                actor_user_id="u-1",
            )

    def test_no_action_reaches_active_except_activate(self) -> None:
        """Exhaustive over the transition table, so a future edit that adds a
        second road to ACTIVE fails here rather than in production."""
        from academic_ai.domain.ai import ACTION_TRANSITIONS

        for action, table in ACTION_TRANSITIONS.items():
            for source, target in table.items():
                if source is target:
                    continue  # a no-op does not *reach* anything
                if target is AIModelStatus.ACTIVE:
                    assert action is ModelAction.ACTIVATE, (
                        f"{action.value} reaches ACTIVE from {source.value}; "
                        "only ACTIVATE may, and only ACTIVATE demands an actor"
                    )


class TestLifecycleOrder:
    def test_the_happy_path_end_to_end(self) -> None:
        state = ModelVersionState("CONFLICT_RANKER", "1.0.0")
        assert state.status is AIModelStatus.EXPERIMENTAL

        state = apply_action(state, ModelAction.UPLOAD)
        assert state.has_artifact
        assert state.status is AIModelStatus.EXPERIMENTAL, (
            "having a file is not evidence; UPLOAD must not advance the state"
        )

        state = apply_action(state, ModelAction.TEST)
        assert state.status is AIModelStatus.TESTING

        state = apply_action(
            state, ModelAction.BENCHMARK, metrics={"pairs_improved": 12}
        )
        assert state.status is AIModelStatus.READY
        assert state.metrics == {"pairs_improved": 12}

        state = apply_action(state, ModelAction.ACTIVATE, actor_user_id="u-1")
        assert state.is_serving

    def test_test_needs_an_artifact(self) -> None:
        """Testing a version with no artifact tests the heuristic and files
        the result under the model's name."""
        with pytest.raises(LifecycleError, match="artifact"):
            apply_action(
                ModelVersionState("M", "1.0.0"), ModelAction.TEST
            )

    def test_benchmark_needs_a_test(self) -> None:
        state = ModelVersionState("M", "1.0.0", has_artifact=True)
        state = apply_action(state, ModelAction.TEST)
        fresh = ModelVersionState("M", "2.0.0", has_artifact=True)
        with pytest.raises(LifecycleError, match="TEST"):
            apply_action(fresh, ModelAction.BENCHMARK)

    def test_rollback_deprecates_rather_than_returning_to_ready(self) -> None:
        """Setting it back to READY loses the fact that it was live and was
        withdrawn — the fact an investigation needs."""
        live = apply_action(_ready(), ModelAction.ACTIVATE, actor_user_id="u-1")
        rolled = apply_action(live, ModelAction.ROLLBACK)
        assert rolled.status is AIModelStatus.DEPRECATED
        assert rolled.activated_by_user_id == "u-1", (
            "who put it live is what an investigation into why it was "
            "withdrawn starts from"
        )

    def test_rollback_is_always_available_on_a_live_version(self) -> None:
        """A switch that only works when nothing is wrong is not a switch."""
        live = apply_action(_ready(), ModelAction.ACTIVATE, actor_user_id="u-1")
        assert ModelAction.ROLLBACK in permitted_actions(live)
        assert ModelAction.DISABLE in permitted_actions(live)

    def test_disable_is_reachable_from_every_status_that_is_not_already_it(
        self,
    ) -> None:
        """DISABLED is terminal, so re-disabling is refused rather than
        silently accepted: an idempotent no-op there would hide the caller bug
        that produced the second call."""
        from academic_ai.domain.ai import ACTION_TRANSITIONS, TERMINAL_STATUSES

        assert set(ACTION_TRANSITIONS[ModelAction.DISABLE]) == (
            set(AIModelStatus) - TERMINAL_STATUSES
        )

    def test_a_disabled_version_cannot_be_activated_again(self) -> None:
        disabled = apply_action(_ready(), ModelAction.DISABLE)
        assert disabled.status is AIModelStatus.DISABLED
        with pytest.raises(LifecycleError):
            apply_action(disabled, ModelAction.ACTIVATE, actor_user_id="u-1")

    def test_compare_changes_nothing(self) -> None:
        state = _ready()
        assert apply_action(state, ModelAction.COMPARE) == state

    def test_register_is_not_applied_to_an_existing_version(self) -> None:
        with pytest.raises(LifecycleError, match="creates a version"):
            apply_action(_ready(), ModelAction.REGISTER)

    def test_a_refusal_says_what_is_legal_instead(self) -> None:
        """"Invalid state transition" at 02:00 tells the reader nothing they
        did not already suspect."""
        with pytest.raises(LifecycleError) as exc:
            apply_action(
                ModelVersionState("M", "1.0.0"), ModelAction.ACTIVATE,
                actor_user_id="u-1",
            )
        assert "Legal here" in str(exc.value)
        assert "UPLOAD" in str(exc.value)


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


class TestCompatibility:
    @staticmethod
    def _check(**overrides) -> CompatibilityReport:
        kwargs = {
            "contract": ModelContract.CONFLICT_PRIORITY_PROVIDER,
            "model_api_version": "1",
            "feature_schema": _schema(),
            "optimizer_compatibility": OptimizerCompatibility("1.0.0", "2.0.0"),
            "runtime": _runtime(),
            "evaluations": (_evaluation(),),
        }
        kwargs.update(overrides)
        return check_compatibility(**kwargs)

    def test_a_matching_version_is_compatible(self) -> None:
        report = self._check()
        assert report.is_compatible
        assert report.describe() == "compatible"

    def test_a_schema_edited_without_a_version_bump_is_caught(self) -> None:
        """The failure the content hash exists for, and the only one that
        comparing versions cannot see."""
        report = self._check(feature_schema=_schema(content_hash=HASH_B))
        assert not report.is_compatible
        assert any("hash" in b for b in report.blocking)

    def test_a_different_schema_version_blocks(self) -> None:
        report = self._check(feature_schema=_schema(version="2.0.0"))
        assert not report.is_compatible
        assert any("different numbers" in b for b in report.blocking)

    def test_a_different_model_api_version_blocks(self) -> None:
        report = self._check(model_api_version="2")
        assert not report.is_compatible

    def test_an_optimizer_outside_the_range_blocks(self) -> None:
        report = self._check(
            optimizer_compatibility=OptimizerCompatibility("1.0.0", "1.3.0")
        )
        assert not report.is_compatible
        assert any("1.4.0" in b for b in report.blocking)

    def test_version_ranges_compare_numerically_not_as_strings(self) -> None:
        """String order puts 1.10.0 before 1.9.0, which would activate a
        model against an optimizer it was never validated on."""
        window = OptimizerCompatibility("1.9.0", "1.11.0")
        assert window.accepts("1.10.0")
        assert not window.accepts("1.8.0")
        assert not window.accepts("1.11.0"), "the upper bound is exclusive"

    def test_a_non_semantic_version_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError, match="semantic version"):
            OptimizerCompatibility("1.0").accepts("1.0.0")

    def test_a_contract_the_runtime_cannot_call_blocks(self) -> None:
        report = self._check(
            runtime=_runtime(
                supported_contracts=frozenset({ModelContract.NEIGHBORHOOD_POLICY})
            )
        )
        assert not report.is_compatible

    def test_no_evaluation_blocks_activation(self) -> None:
        """Activation replaces a heuristic whose behaviour is known with a
        model whose behaviour is not."""
        report = self._check(evaluations=())
        assert not report.is_compatible
        assert any("no evaluation" in b for b in report.blocking)

    def test_an_irreproducible_evaluation_does_not_count(self) -> None:
        report = self._check(evaluations=(_evaluation(is_reproducible=False),))
        assert not report.is_compatible
        assert any("reproducible" in b for b in report.blocking)

    def test_a_model_no_better_than_its_baseline_warns_rather_than_blocks(
        self,
    ) -> None:
        """A human may activate a model measured to be no better, on their own
        authority. That judgement belongs to them, not to this function — but
        they must be told."""
        report = self._check(evaluations=(_evaluation(score_delta=(0, 0)),))
        assert report.is_compatible
        assert any("no better" in w for w in report.warnings)

    def test_a_missing_optimizer_range_warns_rather_than_blocks(self) -> None:
        report = self._check(optimizer_compatibility=None)
        assert report.is_compatible
        assert any("optimizer_compatibility" in w for w in report.warnings)

    def test_every_blocking_reason_is_reported_not_just_the_first(self) -> None:
        """The caller is a person deciding. A list beats the first item."""
        report = self._check(
            model_api_version="9",
            feature_schema=_schema(version="7.0.0"),
            evaluations=(),
        )
        assert len(report.blocking) >= 3

    def test_a_tie_does_not_beat_the_baseline(self) -> None:
        assert not _evaluation(score_delta=(0, 0)).beats_baseline
        assert not _evaluation(score_delta=()).beats_baseline

    def test_improvement_is_lexicographic(self) -> None:
        """The first tier that moves decides. A worse top tier is not paid for
        by a better lower one — ADR-0010 rejects the exchange rate."""
        assert _evaluation(score_delta=(-1, 500)).beats_baseline
        assert not _evaluation(score_delta=(1, -500)).beats_baseline


# ---------------------------------------------------------------------------
# The schema states the same rules again
# ---------------------------------------------------------------------------


class TestSchemaEnforcesTheSameRules:
    def test_an_unknown_contract_is_rejected(self, session: Session) -> None:
        session.add(
            AIModel(
                code="ROGUE",
                name="Something else entirely",
                model_family="GNN",
                contract="DoesWhateverItLikes",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    @pytest.mark.parametrize(
        "contract",
        [c.value for c in ModelContract],
    )
    def test_every_domain_contract_is_accepted_by_the_schema(
        self, session: Session, contract: str
    ) -> None:
        """The CHECK and `ModelContract` must not drift apart: a contract the
        domain knows and the database rejects is a registry that cannot store
        what the optimizer can call."""
        session.add(
            AIModel(
                code=f"M_{contract}",
                name=contract,
                model_family="GNN",
                contract=contract,
            )
        )
        session.flush()

    def test_a_feature_schema_hash_must_be_real(self, session: Session) -> None:
        session.add(
            FeatureSchema(code="S", version="1.0.0", content_hash="short")
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_reproducible_evaluation_needs_a_seed_and_a_dataset(
        self, session: Session
    ) -> None:
        from academic_ai.infrastructure.db.models import ModelEvaluation

        model = AIModel(
            code="CONFLICT_RANKER",
            name="Conflict ranker",
            model_family="GNN",
            contract=ModelContract.CONFLICT_PRIORITY_PROVIDER.value,
        )
        session.add(model)
        session.flush()
        version = AIModelVersion(
            ai_model_id=model.id,
            version="1.0.0",
            model_api_version="1",
            feature_schema_version="1.0.0",
        )
        session.add(version)
        session.flush()
        session.add(
            ModelEvaluation(
                ai_model_version_id=version.id,
                baseline_code="HEURISTIC_CRITICALITY",
                is_reproducible=True,  # but no seed, no dataset version
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_only_one_deployment_may_be_open_per_model(
        self, session: Session, world
    ) -> None:
        """The mirror of the partial unique index on ACTIVE versions. Two live
        models answering one contract is the ambiguity the rule rules out."""
        model = AIModel(
            code="CONFLICT_RANKER",
            name="Conflict ranker",
            model_family="GNN",
            contract=ModelContract.CONFLICT_PRIORITY_PROVIDER.value,
        )
        session.add(model)
        session.flush()
        versions = []
        for v in ("1.0.0", "2.0.0"):
            version = AIModelVersion(
                ai_model_id=model.id,
                version=v,
                model_api_version="1",
                feature_schema_version="1.0.0",
            )
            session.add(version)
            versions.append(version)
        session.flush()

        session.add(
            ModelDeployment(
                ai_model_id=model.id,
                ai_model_version_id=versions[0].id,
                activated_by_user_id=world.admin.id,
            )
        )
        session.flush()
        session.add(
            ModelDeployment(
                ai_model_id=model.id,
                ai_model_version_id=versions[1].id,
                activated_by_user_id=world.admin.id,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_closed_deployment_frees_the_slot(
        self, session: Session, world
    ) -> None:
        model = AIModel(
            code="CONFLICT_RANKER",
            name="Conflict ranker",
            model_family="GNN",
            contract=ModelContract.CONFLICT_PRIORITY_PROVIDER.value,
        )
        session.add(model)
        session.flush()
        version = AIModelVersion(
            ai_model_id=model.id,
            version="1.0.0",
            model_api_version="1",
            feature_schema_version="1.0.0",
        )
        session.add(version)
        session.flush()

        first = ModelDeployment(
            ai_model_id=model.id,
            ai_model_version_id=version.id,
            activated_by_user_id=world.admin.id,
            started_at=dt.datetime(2026, 8, 16, tzinfo=dt.timezone.utc),
            ended_at=dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc),
            ended_by_user_id=world.admin.id,
            ended_reason="rolled back after the Tuesday timetable",
        )
        session.add(first)
        session.flush()
        session.add(
            ModelDeployment(
                ai_model_id=model.id,
                ai_model_version_id=version.id,
                activated_by_user_id=world.admin.id,
            )
        )
        session.flush()  # must not raise

    def test_a_deployment_cannot_end_before_it_starts(
        self, session: Session, world
    ) -> None:
        model = AIModel(
            code="CONFLICT_RANKER",
            name="Conflict ranker",
            model_family="GNN",
            contract=ModelContract.CONFLICT_PRIORITY_PROVIDER.value,
        )
        session.add(model)
        session.flush()
        version = AIModelVersion(
            ai_model_id=model.id,
            version="1.0.0",
            model_api_version="1",
            feature_schema_version="1.0.0",
        )
        session.add(version)
        session.flush()
        session.add(
            ModelDeployment(
                ai_model_id=model.id,
                ai_model_version_id=version.id,
                activated_by_user_id=world.admin.id,
                started_at=dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc),
                ended_at=dt.datetime(2026, 8, 16, tzinfo=dt.timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
