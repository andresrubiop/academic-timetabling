"""Reading an institution's configured constraints into `EffectiveConstraint`.

The catalog is the authority on what a constraint *is*; `planning_constraint`
rows say what this institution decided about it. Absence is meaningful in one
direction only: a code with no row is enforced at its catalog default -- that
is `ProblemInstance.hard_codes()`'s rule and it is deliberate, because reading
only the rows that exist meant an unconfigured institution enforced nothing at
all while `missing_adapters()` reported a complete registry.

So this loads overrides, not the constraint set. A period with no
`planning_constraint` rows produces an empty tuple and a fully-enforced
instance, which is the correct reading.
"""

from __future__ import annotations

import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.scheduling.problem import EffectiveConstraint
from academic_ai.domain.scheduling.scope import ConstraintScope

__all__ = ["load_constraints"]

log = logging.getLogger("academic_ai.repositories.constraints")


def load_constraints(
    session: Session, academic_period_id: uuid.UUID | str
) -> tuple[EffectiveConstraint, ...]:
    """Every configured constraint that applies to this period.

    Institution-wide rows (those with no period) apply too: a policy set for
    the institution is not switched off by a new period existing.
    """
    from academic_ai.infrastructure.db.models import ConstraintType, PlanningConstraint

    period_id = (
        academic_period_id
        if isinstance(academic_period_id, uuid.UUID)
        else uuid.UUID(str(academic_period_id))
    )

    rows = session.execute(
        select(PlanningConstraint, ConstraintType.code, ConstraintType.default_category)
        .join(ConstraintType, ConstraintType.id == PlanningConstraint.constraint_type_id)
        .where(
            (PlanningConstraint.academic_period_id == period_id)
            | (PlanningConstraint.academic_period_id.is_(None))
        )
    ).all()

    constraints: list[EffectiveConstraint] = []
    for row, code, default_category in rows:
        try:
            constraints.append(
                EffectiveConstraint(
                    code=code,
                    # `category_override ?? default`, exactly as the domain
                    # documents. Constructing one with an override the catalog
                    # forbids raises, and the database enforces the same rule
                    # with a composite foreign key -- so a row that gets here
                    # and still fails is worth reporting rather than swallowing.
                    effective_category=row.category_override or default_category,
                    scope=ConstraintScope(
                        faculty_id=_opt(row.scope_faculty_id),
                        program_id=_opt(row.scope_program_id),
                        department_id=_opt(row.scope_department_id),
                        room_id=_opt(row.scope_room_id),
                        teacher_id=_opt(row.scope_teacher_id),
                        course_id=_opt(row.scope_course_id),
                        section_id=_opt(row.scope_section_id),
                    ),
                    weight=float(row.weight),
                    parameters=dict(row.parameters or {}),
                    is_active=row.is_active,
                    overridden_by_user_id=_opt(row.overridden_by_user_id),
                    override_reason=row.override_reason,
                )
            )
        except ValueError:
            log.exception(
                "a configured constraint could not be read and was skipped",
                extra={"code": code, "constraint_id": str(row.id)},
            )

    log.info(
        "constraints loaded",
        extra={"period_id": str(period_id), "configured": len(constraints)},
    )
    return tuple(constraints)


def _opt(value) -> str | None:
    return str(value) if value is not None else None
