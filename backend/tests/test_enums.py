"""Domain enum invariants.

These are cheap tests guarding an expensive mistake: the enum *value* is what
PostgreSQL stores, so a member whose value drifts from its name silently
changes what is persisted.
"""

from __future__ import annotations

import enum

import pytest

from academic_ai.domain import enums
from academic_ai.domain.enums import (
    DayOfWeek,
    EnrollmentStatus,
    ScheduleVersionStatus,
)

ALL_ENUMS = [
    obj
    for name in enums.__all__
    if isinstance(obj := getattr(enums, name), type) and issubclass(obj, enum.Enum)
]


def test_every_exported_enum_is_covered() -> None:
    assert len(ALL_ENUMS) == len(enums.__all__)


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_member_value_equals_member_name(enum_cls: type[enum.Enum]) -> None:
    for member in enum_cls:
        assert member.value == member.name, (
            f"{enum_cls.__name__}.{member.name} has value {member.value!r}; "
            "the value is what the database stores and must equal the name"
        )


def test_day_of_week_matches_iso_weekday() -> None:
    assert DayOfWeek.MONDAY.iso_index == 1
    assert DayOfWeek.SUNDAY.iso_index == 7
    assert [d.iso_index for d in DayOfWeek] == list(range(1, 8))


def test_active_enrollment_statuses_are_the_conflict_generating_ones() -> None:
    active = {s for s in EnrollmentStatus if s.is_active}
    assert active == {
        EnrollmentStatus.REQUESTED,
        EnrollmentStatus.ENROLLED,
        EnrollmentStatus.WAITLISTED,
    }
    # A dropped or completed enrollment must never create a conflict.
    assert not EnrollmentStatus.DROPPED.is_active
    assert not EnrollmentStatus.COMPLETED.is_active
    assert not EnrollmentStatus.FAILED.is_active


def test_only_draft_schedule_versions_are_mutable() -> None:
    mutable = {s for s in ScheduleVersionStatus if s.is_mutable}
    assert mutable == {ScheduleVersionStatus.DRAFT}
