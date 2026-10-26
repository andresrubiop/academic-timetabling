"""The constraint catalog is the executable form of SESSION 2's classification.

Two kinds of test here. The pure ones check that the catalog is internally
coherent — that no entry claims to be a solver constraint the solver cannot
affect, that no downgradable constraint would vanish from the objective when
downgraded. The database one checks that the seeded `constraint_type` table
still says what the catalog says, which is the data equivalent of
``alembic check``: changing a classification must go through a migration,
because a constraint's category is an institutional decision with an audit
trail rather than a constant somebody edits.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.catalog import (
    CONSTRAINT_CATALOG,
    PLAYBOOK_COVERAGE,
    ConstraintSpec,
    downgradable_hard_codes,
    objective_bearing_codes,
    solver_hard_codes,
)
from academic_ai.domain.scheduling.enums import Enforcement, TermUnit, WeightSource

SPECS = sorted(CONSTRAINT_CATALOG.values(), key=lambda s: s.code)

#: The 20 phenomena the playbook requires SESSION 2 to analyse, verbatim.
PLAYBOOK_ITEMS = (
    "teacher overlap",
    "room overlap",
    "student overlap",
    "teacher availability",
    "room capacity",
    "room capabilities",
    "session duration",
    "locked assignments",
    "campus travel",
    "shared courses",
    "repeated-course students",
    "single-section criticality",
    "teacher workload",
    "daily limits",
    "weekly distribution",
    "student gaps",
    "teacher gaps",
    "room changes",
    "preferred slots",
    "schedule stability",
)


class TestCoverage:
    def test_every_playbook_item_is_covered(self) -> None:
        assert tuple(PLAYBOOK_COVERAGE) == PLAYBOOK_ITEMS

    def test_coverage_names_only_real_constraints(self) -> None:
        for item, coverage in PLAYBOOK_COVERAGE.items():
            assert coverage.constrained_by, f"{item}: claimed as covered by nothing"
            for code in coverage.constrained_by:
                assert code in CONSTRAINT_CATALOG, f"{item}: unknown code {code}"

    def test_constrained_by_names_only_enforced_constraints(self) -> None:
        """A checklist item cannot claim enforcement from a constraint the
        solver never sees, unless the entry says so in its note."""
        for item, coverage in PLAYBOOK_COVERAGE.items():
            for code in coverage.constrained_by:
                spec = CONSTRAINT_CATALOG[code]
                if spec.enforcement is not Enforcement.SOLVER:
                    assert coverage.note.strip(), (
                        f"{item}: covered only by {code}, which is a "
                        f"{spec.enforcement} — that needs saying out loud"
                    )

    def test_every_deferral_names_a_session_and_a_reason(self) -> None:
        """A deferral is a commitment, not an omission."""
        for item, coverage in PLAYBOOK_COVERAGE.items():
            if coverage.deferred_to is not None:
                assert coverage.deferred_to.startswith("SESSION ")
                assert coverage.note.strip(), f"{item}: deferral needs a reason"

    def test_every_catalog_entry_is_reachable_from_the_checklist(self) -> None:
        """A constraint no checklist item claims is a constraint nobody asked
        for. It may still be legitimate, but it must be a deliberate addition
        rather than a leftover."""
        claimed = {
            code
            for coverage in PLAYBOOK_COVERAGE.values()
            for code in coverage.constrained_by
        }
        unclaimed = set(CONSTRAINT_CATALOG) - claimed
        # Structural constraints the checklist implies rather than names.
        expected_extras = {"SESSION_ROOM_PRESENCE", "ROOM_AVAILABILITY"}
        assert unclaimed == expected_extras


class TestInternalCoherence:
    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_solver_constraints_read_a_decision_variable(
        self, spec: ConstraintSpec
    ) -> None:
        """A constraint no assignment can affect must not reach CP-SAT.

        Handing one to the solver means a staffing or data error comes back as
        "your timetable is infeasible", which blames the timetable for
        something the timetable cannot fix.
        """
        if spec.enforcement is Enforcement.SOLVER:
            assert not spec.reads.is_invariant, (
                f"{spec.code}: reads=NONE but enforced by the solver"
            )

    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_preconditions_are_invariant_under_the_variables(
        self, spec: ConstraintSpec
    ) -> None:
        if spec.enforcement is Enforcement.PRECONDITION:
            assert spec.reads.is_invariant, (
                f"{spec.code}: checked as a precondition but depends on "
                f"{spec.reads} — the solver could have satisfied it"
            )

    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_downgradable_constraints_declare_an_objective_term(
        self, spec: ConstraintSpec
    ) -> None:
        """Downgrading must move a constraint, not delete it."""
        can_be_downgraded = (
            spec.enforcement is Enforcement.SOLVER and spec.is_category_overridable
        )
        if can_be_downgraded:
            assert spec.term_unit is not None, (
                f"{spec.code}: downgrading it would silently remove it from "
                "the objective entirely"
            )

    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_non_overridable_hard_constraints_carry_no_term(
        self, spec: ConstraintSpec
    ) -> None:
        if (
            spec.default_category is ConstraintCategory.HARD
            and not spec.is_category_overridable
        ):
            assert spec.term_unit is None

    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_every_entry_justifies_its_category(self, spec: ConstraintSpec) -> None:
        """The playbook forbids unjustified classification as firmly as it
        forbids unjustified weights."""
        assert spec.rationale.strip(), f"{spec.code}: no rationale"
        assert spec.quantified_over.strip(), f"{spec.code}: nothing quantified over"

    @pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
    def test_no_weight_is_invented_by_the_platform(
        self, spec: ConstraintSpec
    ) -> None:
        assert spec.weight_source in WeightSource
        assert not hasattr(WeightSource, "PLATFORM")

    def test_physical_reality_cannot_be_downgraded(self) -> None:
        """A person is not in two rooms at once, and no override changes that."""
        immutable = {
            "TEACHER_NO_OVERLAP",
            "ROOM_NO_OVERLAP",
            "SESSION_WITHIN_DAY_GRID",
            "SESSION_SLOTS_SCHEDULABLE",
            "ROOM_FEATURE_REQUIREMENT",
            "CO_TEACHING_SAME_TIMESLOT",
            "CO_TEACHING_SAME_ROOM",
            "TEACHER_AVAILABILITY",
            "ASSIGNMENT_LOCKED",
        }
        for code in immutable:
            spec = CONSTRAINT_CATALOG[code]
            assert spec.default_category is ConstraintCategory.HARD
            assert not spec.is_category_overridable, (
                f"{code} describes physical or contractual reality and must "
                "not be overridable"
            )

    def test_student_overlap_is_not_uniformly_hard(self) -> None:
        """SESSION 5: "Do not silently convert all student conflicts into HARD."

        Only the genuinely unresolvable case is HARD, and even that is
        overridable — an institution can decide to publish a timetable that
        strands a student, but it must decide it, in writing, by name.
        """
        unresolvable = CONSTRAINT_CATALOG["STUDENT_NO_OVERLAP_UNRESOLVABLE"]
        mandatory = CONSTRAINT_CATALOG["STUDENT_NO_OVERLAP_MANDATORY"]
        elective = CONSTRAINT_CATALOG["STUDENT_NO_OVERLAP_ELECTIVE"]

        assert unresolvable.default_category is ConstraintCategory.HARD
        assert unresolvable.is_category_overridable
        assert mandatory.default_category is not ConstraintCategory.HARD
        assert elective.default_category is not ConstraintCategory.HARD

    def test_student_conflict_terms_share_a_unit(self) -> None:
        for code in (
            "STUDENT_NO_OVERLAP_UNRESOLVABLE",
            "STUDENT_NO_OVERLAP_MANDATORY",
            "STUDENT_NO_OVERLAP_ELECTIVE",
        ):
            assert (
                CONSTRAINT_CATALOG[code].term_unit
                is TermUnit.STUDENT_CONFLICT_INCIDENCE
            )

    def test_teacher_weekly_load_is_a_precondition(self) -> None:
        """Who teaches what is fixed input (ADR-0008), so no choice of slot or
        room changes a teacher's weekly total. Asking CP-SAT to fix it would
        report INFEASIBLE for a staffing decision."""
        spec = CONSTRAINT_CATALOG["TEACHER_MAX_WEEKLY_HOURS"]
        assert spec.enforcement is Enforcement.PRECONDITION
        assert spec.reads.is_invariant

    def test_helper_sets_agree_with_the_catalog(self) -> None:
        assert solver_hard_codes() == {
            s.code
            for s in SPECS
            if s.default_category is ConstraintCategory.HARD
            and s.enforcement is Enforcement.SOLVER
        }
        assert objective_bearing_codes() == {
            s.code for s in SPECS if s.term_unit is not None
        }
        assert downgradable_hard_codes() == {
            s.code
            for s in SPECS
            if s.default_category is ConstraintCategory.HARD
            and s.is_category_overridable
        }

    def test_codes_are_unique_and_match_their_key(self) -> None:
        for key, spec in CONSTRAINT_CATALOG.items():
            assert key == spec.code

    def test_parameter_schemas_are_valid_json(self) -> None:
        for spec in SPECS:
            if spec.parameter_schema is not None:
                json.dumps(dict(spec.parameter_schema))


class TestSeededTableMatchesCatalog:
    """The data equivalent of ``alembic check``."""

    @pytest.fixture()
    def seeded(self, engine: Engine) -> dict[str, dict[str, object]]:
        from tests._migration_sql import catalog_seed_rows

        return {row["code"]: row for row in catalog_seed_rows()}

    def test_migration_seeds_every_catalog_entry(
        self, seeded: dict[str, dict[str, object]]
    ) -> None:
        assert set(seeded) == set(CONSTRAINT_CATALOG), (
            "the seeding migration and the catalog disagree about which "
            "constraints exist; changing the catalog requires a new migration"
        )

    def test_seeded_classification_matches_the_catalog(
        self, seeded: dict[str, dict[str, object]]
    ) -> None:
        for code, spec in CONSTRAINT_CATALOG.items():
            row = seeded[code]
            assert row["default_category"] == spec.default_category.value, (
                f"{code}: seeded as {row['default_category']}, catalog says "
                f"{spec.default_category.value}"
            )
            assert row["is_category_overridable"] == spec.is_category_overridable
            assert row["parameter_schema_version"] == spec.parameter_schema_version
            assert row["name"] == spec.name
            assert row["description"] == spec.description

    def test_seeded_parameter_schemas_match(
        self, seeded: dict[str, dict[str, object]]
    ) -> None:
        for code, spec in CONSTRAINT_CATALOG.items():
            expected = (
                None if spec.parameter_schema is None else dict(spec.parameter_schema)
            )
            assert seeded[code]["parameter_schema"] == expected, f"{code}"


@pytest.mark.db
class TestCatalogInTheDatabase:
    def test_applied_migration_loads_the_catalog(self, engine: Engine) -> None:
        """Runs the real seeding SQL against a real table.

        The metadata-built test schema has an empty `constraint_type`; this
        applies the migration's own rows to it and checks the enum casts and
        JSONB serialisation actually work — the parts a pure-Python comparison
        cannot reach.
        """
        from tests._migration_sql import catalog_seed_rows

        rows = catalog_seed_rows()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM constraint_type"))
            for row in rows:
                schema = row["parameter_schema"]
                conn.execute(
                    text(
                        "INSERT INTO constraint_type ("
                        " code, name, description, default_category,"
                        " is_category_overridable, parameter_schema,"
                        " parameter_schema_version, is_builtin"
                        ") VALUES ("
                        " :code, :name, :description,"
                        " CAST(:default_category AS constraint_category),"
                        " :is_category_overridable,"
                        " CAST(:parameter_schema AS jsonb),"
                        " :parameter_schema_version, TRUE)"
                    ),
                    {
                        **row,
                        "parameter_schema": (
                            None if schema is None else json.dumps(schema)
                        ),
                    },
                )
            stored = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    text(
                        "SELECT code, default_category::text, "
                        "is_category_overridable FROM constraint_type"
                    )
                )
            }
            conn.rollback()

        assert set(stored) == set(CONSTRAINT_CATALOG)
        for code, spec in CONSTRAINT_CATALOG.items():
            assert stored[code] == (
                spec.default_category.value,
                spec.is_category_overridable,
            )


class TestReviewFindings:
    """Regression guards for the SESSION 2 optimizer-agent review."""

    def test_sessions_of_one_section_cannot_overlap(self) -> None:
        """Without this, a lecture (T1/R1) and a lab (T2/R2) of the same
        section may share a slot: neither overlap constraint fires, and the
        student path is silent whenever the enrollments are unplaced — the
        majority case at planning time."""
        spec = CONSTRAINT_CATALOG["SECTION_SESSIONS_NO_OVERLAP"]
        assert spec.default_category is ConstraintCategory.HARD
        assert not spec.is_category_overridable
        assert spec.enforcement is Enforcement.SOLVER

    def test_magnitude_terms_declare_their_dimension(self) -> None:
        """`WEIGHTED_VIOLATION` alone would let a tier sum seats against
        minutes and call them commensurable."""
        from academic_ai.domain.scheduling.enums import ViolationMeasure

        assert (
            CONSTRAINT_CATALOG["ROOM_CAPACITY"].violation_measure
            is ViolationMeasure.MAGNITUDE_SEATS
        )
        assert (
            CONSTRAINT_CATALOG["TEACHER_MAX_DAILY_HOURS"].violation_measure
            is ViolationMeasure.MAGNITUDE_MINUTES
        )

    def test_a_term_less_constraint_cannot_declare_a_magnitude(self) -> None:
        from academic_ai.domain.scheduling.enums import ViolationMeasure

        for spec in SPECS:
            if spec.term_unit is None:
                assert spec.violation_measure is ViolationMeasure.INDICATOR, (
                    f"{spec.code}: declares a magnitude measured by nothing"
                )

    def test_session_room_presence_is_not_a_solver_constraint(self) -> None:
        """`room(s)` only exists for sessions that need a room, so no
        assignment the solver can produce violates it. Marked SOLVER it would
        force an adapter that posts nothing, which `missing_adapters` then
        demands."""
        spec = CONSTRAINT_CATALOG["SESSION_ROOM_PRESENCE"]
        assert spec.enforcement is Enforcement.PRECONDITION
        assert spec.reads.is_invariant
