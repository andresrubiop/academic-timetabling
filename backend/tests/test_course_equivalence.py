"""Equivalence and academic compatibility.

CLAUDE.md forbids AI from automatically approving academic equivalence,
cross-program or cross-faculty changes. These tests check that the *schema*
enforces it rather than a code path that a future session could forget.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AcademicApprovalStatus,
    ProposalSource,
)
from academic_ai.infrastructure.db.models import (
    AcademicCompatibilityGroup,
    AcademicCompatibilityGroupMember,
    CourseEquivalence,
)
from tests.conftest import AcademicWorld


class TestCourseEquivalence:
    def test_a_course_cannot_be_equivalent_to_itself(
        self, session: Session, world: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    CourseEquivalence(
                        source_course_id=world.course.id,
                        target_course_id=world.course.id,
                    )
                )
                session.flush()

    def test_equivalence_is_directional(
        self, session: Session, world: AcademicWorld
    ) -> None:
        """A->B and B->A are separate, independently revocable rows.

        Real equivalence is often one-way: a 4-credit course may satisfy a
        3-credit one without the converse holding.
        """
        forward = CourseEquivalence(
            source_course_id=world.course.id,
            target_course_id=world.other_course.id,
        )
        backward = CourseEquivalence(
            source_course_id=world.other_course.id,
            target_course_id=world.course.id,
        )
        session.add_all([forward, backward])
        session.flush()

        assert session.query(CourseEquivalence).count() == 2

        # Revoking one direction leaves the other untouched.
        forward.status = AcademicApprovalStatus.REVOKED
        session.flush()
        session.refresh(backward)
        assert backward.status is AcademicApprovalStatus.PROPOSED

    def test_duplicate_institution_wide_equivalence_is_rejected(
        self, session: Session, world: AcademicWorld
    ) -> None:
        """The NULLS NOT DISTINCT case.

        Both scope columns are NULL for an institution-wide equivalence. With
        PostgreSQL's default NULL semantics the unique key would not
        deduplicate at all and unlimited duplicates would be insertable —
        a governance hole for an *approved* academic equivalence.
        """
        session.add(
            CourseEquivalence(
                source_course_id=world.course.id,
                target_course_id=world.other_course.id,
            )
        )
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    CourseEquivalence(
                        source_course_id=world.course.id,
                        target_course_id=world.other_course.id,
                    )
                )
                session.flush()

    def test_a_scoped_equivalence_coexists_with_the_institution_wide_one(
        self, session: Session, world: AcademicWorld
    ) -> None:
        session.add_all(
            [
                CourseEquivalence(
                    source_course_id=world.course.id,
                    target_course_id=world.other_course.id,
                ),
                CourseEquivalence(
                    source_course_id=world.course.id,
                    target_course_id=world.other_course.id,
                    scope_program_id=world.program.id,
                ),
            ]
        )
        session.flush()
        assert session.query(CourseEquivalence).count() == 2

    def test_approval_requires_a_named_human_and_a_timestamp(
        self, session: Session, world: AcademicWorld
    ) -> None:
        """An AI-proposed equivalence cannot become APPROVED on its own."""
        equivalence = CourseEquivalence(
            source_course_id=world.course.id,
            target_course_id=world.other_course.id,
            proposal_source=ProposalSource.AI_SUGGESTION,
        )
        session.add(equivalence)
        session.flush()

        equivalence.status = AcademicApprovalStatus.APPROVED
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.flush()

    def test_approval_with_an_approver_is_accepted(
        self, session: Session, world: AcademicWorld
    ) -> None:
        equivalence = CourseEquivalence(
            source_course_id=world.course.id,
            target_course_id=world.other_course.id,
            proposal_source=ProposalSource.AI_SUGGESTION,
            status=AcademicApprovalStatus.APPROVED,
            approved_by_user_id=world.admin.id,
            approved_at=dt.datetime.now(dt.timezone.utc),
        )
        session.add(equivalence)
        session.flush()
        assert equivalence.proposal_source is ProposalSource.AI_SUGGESTION
        assert equivalence.approved_by_user_id == world.admin.id


class TestAcademicCompatibilityGroup:
    def test_group_approval_requires_a_human(
        self, session: Session, world: AcademicWorld
    ) -> None:
        group = AcademicCompatibilityGroup(
            institution_id=world.institution.id,
            code="CALC-SHARED",
            name="Cálculo compartido",
            proposal_source=ProposalSource.AI_SUGGESTION,
        )
        session.add(group)
        session.flush()

        group.status = AcademicApprovalStatus.APPROVED
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.flush()

    def test_a_group_has_at_most_one_canonical_member(
        self, session: Session, world: AcademicWorld
    ) -> None:
        group = AcademicCompatibilityGroup(
            institution_id=world.institution.id,
            code="CALC-SHARED",
            name="Cálculo compartido",
        )
        session.add(group)
        session.flush()

        session.add(
            AcademicCompatibilityGroupMember(
                group_id=group.id, course_id=world.course.id, is_canonical=True
            )
        )
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    AcademicCompatibilityGroupMember(
                        group_id=group.id,
                        course_id=world.other_course.id,
                        is_canonical=True,
                    )
                )
                session.flush()

    def test_non_canonical_members_are_unlimited(
        self, session: Session, world: AcademicWorld
    ) -> None:
        group = AcademicCompatibilityGroup(
            institution_id=world.institution.id,
            code="CALC-SHARED",
            name="Cálculo compartido",
        )
        session.add(group)
        session.flush()

        session.add_all(
            [
                AcademicCompatibilityGroupMember(
                    group_id=group.id, course_id=world.course.id, is_canonical=True
                ),
                AcademicCompatibilityGroupMember(
                    group_id=group.id,
                    course_id=world.other_course.id,
                    is_canonical=False,
                ),
            ]
        )
        session.flush()
        assert len(group.members) == 2
