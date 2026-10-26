"""The timetable, its legal drop targets, and what a move would do.

Two endpoints, and the split between them is the point of ADR-0012.

`GET /schedule` returns the accepted placement **and each session's domain** —
where it may legally go, from `compute_domains()`, the same function the
solver uses. The client renders those as available cells and greys out the
rest. It does not derive them: a second feasibility implementation in
TypeScript would drift from CP-SAT, and the drift would appear as a placement
the UI allowed and the solver rejected.

`POST /schedule/impact` answers "what would this move do" and **writes
nothing**. It is the only thing standing between a slipped hand and a change
request, so it returns numbers a coordinator could not have worked out by
looking: who is affected, which clashes appear and disappear, per-tier score
delta, stability delta.

Neither endpoint can modify the accepted schedule. Applying a change is a
`ChangeRequest` and a separate approval (SESSION 12).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    AffectedCountsModel,
    AlternativeModel,
    AlternativesRequest,
    AlternativesResponse,
    ConflictChangeModel,
    ImpactRequest,
    ImpactResponse,
    PlacementModel,
    ProposedMoveModel,
    ReferenceLabelModel,
    ReferencesModel,
    ScheduleViewResponse,
    SessionDomainModel,
    SessionMetaModel,
    SlotModel,
)
from academic_ai.domain.scheduling.domains import compute_domains
from academic_ai.domain.scheduling.impact import ProposedMove, analyse_impact
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/schedule", tags=["schedule"])
log = logging.getLogger("academic_ai.api.schedule")


def _instance_for(session: Session, academic_period_id: str):
    """The problem instance for a period, read from the database.

    Shares the optimizer's loader on purpose. A preview computed against a
    different instance from the one the solver would use is worse than no
    preview: it is confidently wrong rather than absent, and a coordinator has
    no way to tell the two apart.
    """
    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance
    from academic_ai.infrastructure.repositories import (
        SnapshotLoadError,
        load_constraints,
        load_snapshot,
    )

    try:
        snapshot, diagnostics = load_snapshot(session, academic_period_id)
    except SnapshotLoadError as exc:
        # 404 rather than 500: the period is readable or it is not, and the
        # message says which part is missing.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    constraints = load_constraints(session, academic_period_id)
    profile = "MINIMAL_DISRUPTION" if snapshot.base_assignments else "GREENFIELD"
    return ProblemInstance(
        snapshot=snapshot,
        constraints=constraints,
        objective_profile=OBJECTIVE_PROFILES[profile],
        diagnostics=diagnostics,
    )


@router.get("", response_model=ScheduleViewResponse)
def get_schedule(
    academic_period_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_PROGRAM_SCHEDULE)),
) -> ScheduleViewResponse:
    """The current placement, where each session may legally go, and what
    each one is called."""
    from academic_ai.infrastructure.repositories import (
        load_reference_labels,
        load_session_labels,
    )

    instance = _instance_for(session, academic_period_id)
    domains, _ = compute_domains(instance.snapshot, instance.constraints)
    labels = load_session_labels(session, academic_period_id)
    references = load_reference_labels(session, academic_period_id)

    placement = [
        PlacementModel(
            session_id=b.session_id, start_slot_id=b.start_slot_id, room_id=b.room_id
        )
        for b in instance.snapshot.base_assignments
    ]
    locked = {
        c.code for c in instance.constraints if c.code == "ASSIGNMENT_LOCKED"
    }
    return ScheduleViewResponse(
        placement=placement,
        # The grid itself. The client drew a week by parsing slot ids as text
        # until SESSION 29, which worked for the fixtures and produced a
        # single cell for every real period.
        slots=[
            SlotModel(
                slot_id=slot.id,
                day=slot.day_of_week.value,
                in_day_index=slot.in_day_index,
                label=f"{slot.start_minute // 60:02d}:{slot.start_minute % 60:02d}",
                start_minute=slot.start_minute,
                is_schedulable=slot.is_schedulable,
            )
            for slot in instance.snapshot.slots
        ],
        domains=[
            SessionDomainModel(
                session_id=session_id,
                start_slot_ids=sorted(domain.start_slot_ids),
                room_ids=sorted(domain.room_ids),
                # A session with exactly one option is not "locked", it is
                # constrained. Reporting it as locked would tell a coordinator
                # somebody pinned it, which is a different fact.
                is_locked=bool(locked) and session_id in locked,
            )
            for session_id, domain in sorted(domains.items())
        ],
        # Labels for whatever the read found. A session with no label is left
        # out rather than filled with a placeholder: the client already knows
        # how to fall back to the session id, and inventing a name here would
        # make a data gap look like a real course code.
        sessions=[
            SessionMetaModel(
                session_id=label.session_id,
                course_code=label.course_code,
                course_name=label.course_name,
                section_code=label.section_code,
                teacher_ids=list(label.teacher_ids),
                teacher_names=list(label.teacher_names),
                program_ids=list(label.program_ids),
                faculty_ids=list(label.faculty_ids),
                student_ids=list(label.student_ids),
                duration_slots=label.duration_slots,
            )
            for label in sorted(labels.values(), key=lambda l: l.session_id)
        ],
        # Names for the view selector. The programme, faculty and room filters
        # listed UUIDs until SESSION 31 — they worked, and nobody could use
        # them.
        references=ReferencesModel(
            programs=[
                ReferenceLabelModel(id=r.id, code=r.code, name=r.name)
                for r in references.programs
            ],
            faculties=[
                ReferenceLabelModel(id=r.id, code=r.code, name=r.name)
                for r in references.faculties
            ],
            rooms=[
                ReferenceLabelModel(id=r.id, code=r.code, name=r.name)
                for r in references.rooms
            ],
        ),
    )


@router.post("/impact", response_model=ImpactResponse)
def analyse(
    payload: ImpactRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.EDIT_DRAFT_SCHEDULE)),
) -> ImpactResponse:
    """What these moves would do. Writes nothing, by construction.

    `EDIT_DRAFT_SCHEDULE` rather than `PUBLISH_SCHEDULE`: asking what a move
    would do is part of drafting. Committing to it is the change request, and
    that is where the stronger permission belongs.
    """
    instance = _instance_for(session, payload.academic_period_id)

    placement = {
        p.session_id: PlacedSession(p.session_id, p.start_slot_id, p.room_id)
        for p in payload.placement
    }
    moves = [
        ProposedMove(m.session_id, m.to_start_slot_id, m.to_room_id)
        for m in payload.moves
    ]

    unknown = sorted({m.session_id for m in moves} - set(placement))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no placement supplied for session(s): {', '.join(unknown[:5])}",
        )

    report = analyse_impact(instance, placement, moves)
    log.info(
        "impact analysed",
        extra={
            "user_id": principal.user_id,
            "moves": len(moves),
            "new_conflicts": len(report.new_conflicts),
        },
    )

    return _to_impact_response(report, report.moves)


@router.post("/alternatives", response_model=AlternativesResponse)
def alternatives(
    payload: AlternativesRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.EDIT_DRAFT_SCHEDULE)),
) -> AlternativesResponse:
    """Other places the session could go, ranked.

    The SIMULATE ALTERNATIVES action from ADR-0012. It answers "this one is
    awkward — what else?", so a proposed move is required: without one there
    is nothing to be an alternative *to*.

    Candidates come from the session's **domain** — the same
    `compute_domains()` the solver uses — so every option offered is one the
    solver would accept as a placement. They are then ranked by measured
    impact, lexicographically, because that is the only ordering this project
    admits: a scalar score would reintroduce the cross-tier exchange rate
    ADR-0010 rejects.

    Nothing is applied. Choosing an alternative swaps the *proposal*, and the
    coordinator still has to create a change request.
    """
    instance = _instance_for(session, payload.academic_period_id)
    domains, _ = compute_domains(instance.snapshot, instance.constraints)

    placement = {
        p.session_id: PlacedSession(p.session_id, p.start_slot_id, p.room_id)
        for p in payload.placement
    }
    primary = payload.moves[0]
    domain = domains.get(primary.session_id)
    if domain is None or primary.session_id not in placement:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no placement or domain for session {primary.session_id!r}",
        )

    current = placement[primary.session_id]
    room = primary.to_room_id or current.room_id

    scored = []
    for slot_id in sorted(domain.start_slot_ids):
        # The proposed slot and the current one are not alternatives to
        # themselves.
        if slot_id in {primary.to_start_slot_id, current.start_slot_id}:
            continue
        move = ProposedMove(primary.session_id, slot_id, room)
        report = analyse_impact(instance, placement, [move])
        scored.append((report, move))

    # Lexicographic, then "fewest new conflicts", then slot id so the list is
    # stable across identical requests -- a ranking that reshuffles between
    # two identical calls reads as a bug.
    scored.sort(
        key=lambda pair: (
            tuple(pair[0].score_delta),
            len(pair[0].new_conflicts),
            len(pair[0].occupancy_clashes),
            pair[1].to_start_slot_id,
        )
    )

    chosen = scored[: payload.max_alternatives]
    note = None
    if len(chosen) < payload.max_alternatives:
        note = (
            f"only {len(chosen)} alternative(s) exist: the session's domain "
            "does not allow more"
        )

    return AlternativesResponse(
        alternatives=[
            AlternativeModel(
                label=f"move to {move.to_start_slot_id}",
                moves=[
                    ProposedMoveModel(
                        session_id=move.session_id,
                        to_start_slot_id=move.to_start_slot_id,
                        to_room_id=move.to_room_id,
                    )
                ],
                impact=_to_impact_response(report, [move]),
            )
            for report, move in chosen
        ],
        note=note,
    )



def _to_impact_response(report, moves) -> ImpactResponse:
    """One mapping, two endpoints. Written once so `impact` and `alternatives`
    cannot come to disagree about what a report looks like."""
    return ImpactResponse(
        moves=[
            ProposedMoveModel(
                session_id=m.session_id,
                to_start_slot_id=m.to_start_slot_id,
                to_room_id=m.to_room_id,
            )
            for m in moves
        ],
        affected=AffectedCountsModel(
            students=report.affected.students,
            teachers=report.affected.teachers,
            programs=report.affected.programs,
            faculties=report.affected.faculties,
            rooms=report.affected.rooms,
            sessions=report.affected.sessions,
        ),
        new_conflicts=[_conflict(c) for c in report.new_conflicts],
        resolved_conflicts=[_conflict(c) for c in report.resolved_conflicts],
        score_delta=list(report.score_delta),
        tier_codes=list(report.tier_codes),
        stability_delta=report.stability_delta,
        occupancy_clashes=list(report.occupancy_clashes),
        summary=report.summary(),
        is_improvement=report.is_improvement,
        is_neutral=report.is_neutral,
    )


def _conflict(c) -> ConflictChangeModel:
    return ConflictChangeModel(
        student_id=c.student_id,
        session_a=c.session_a,
        session_b=c.session_b,
        classification=c.classification,
    )

