"""Running a scenario: apply the overlay, repair minimally, report the diff.

The pipeline the playbook specifies, in one place:

    base data
      -> apply_overlay          (the scenario's declarative changes)
      -> solve or repair        (CP-SAT, minimally disruptive)
      -> diff_schedules         (what moved, who it reached)

**Minimal disruption is the objective, not a post-hoc filter.** The instance
is built with the `MINIMAL_DISRUPTION` profile, which prices movement away
from the accepted schedule in its own tier, and the repair is scoped to the
region the overlay touched. Re-solving the whole period and then picking the
answer that happens to move least would be a different, worse thing: it
optimises for the best timetable and then hopes.

Isolation
=========

A scenario writes nothing. `run_scenario` takes base data and an overlay and
returns a report. The accepted schedule is not a parameter it can modify, and
there is no code path from here to a `schedule_version` row. Publishing a
scenario's result is a change request and an approval (SESSION 12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from academic_ai.domain.scheduling.diff import ScheduleDiff, diff_schedules
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.overlay import (
    OverlayError,
    affected_sessions,
    apply_overlay,
)
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    EffectiveConstraint,
    Overlay,
    ProblemInstance,
)

__all__ = ["ScenarioOutcome", "build_scenario_instance", "run_scenario"]

log = logging.getLogger("academic_ai.whatif")


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """What a what-if produced, and whether to believe it."""

    scenario_id: str | None
    instance: ProblemInstance
    placement: Mapping[str, PlacedSession]
    diff: ScheduleDiff
    solver_status: str | None = None
    #: True only when a schedule was produced *and* survived independent
    #: validation. A scenario whose repair failed still returns a report --
    #: "this cannot be absorbed" is the most useful answer a what-if can give
    #: -- but it must not be mistaken for a plan.
    is_solved: bool = False
    failure_reason: str | None = None
    affected_region: frozenset[str] = frozenset()
    freed_sessions: int = 0

    @property
    def is_absorbable(self) -> bool:
        return self.is_solved and not self.diff.occupancy_clashes


def build_scenario_instance(
    base: BaseSnapshot,
    overlay: Overlay,
    *,
    constraints: Sequence[EffectiveConstraint] = (),
    objective_profile: str = "MINIMAL_DISRUPTION",
) -> ProblemInstance:
    """Base data plus the overlay, as an instance the solver can take.

    `MINIMAL_DISRUPTION` by default, and that default is the whole point: it
    carries a `SCHEDULE_INSTABILITY` tier, so moving a session away from the
    accepted schedule has a price. Under `GREENFIELD` the optimizer is
    indifferent between the accepted timetable and a completely different one
    of equal quality, and a what-if would routinely return the latter.
    """
    perturbed = apply_overlay(base, overlay)
    return ProblemInstance(
        snapshot=perturbed,
        constraints=tuple(constraints),
        objective_profile=OBJECTIVE_PROFILES[objective_profile],
        scenario_id=overlay.scenario_id,
    )


def run_scenario(
    base: BaseSnapshot,
    overlay: Overlay,
    accepted: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    *,
    constraints: Sequence[EffectiveConstraint] = (),
    time_limit_seconds: float = 30.0,
    random_seed: int = 0,
    repair_iterations: int = 12,
    widen_region: bool = True,
) -> ScenarioOutcome:
    """Apply a scenario and repair the smallest region that absorbs it.

    ``accepted`` is the schedule in force. It is read, never written.
    """
    # Imported here rather than at module scope: this module is application
    # code and is imported by the API, and OR-Tools costs ~0.4s and ~200 MB to
    # import. The web process should pay that only if it actually solves.
    from academic_ai.optimization.lns import LnsConfig, optimise
    from academic_ai.optimization.lns.region import region_policies
    from academic_ai.optimization.solve import SolverConfig, solve

    if not isinstance(accepted, Mapping):
        accepted = {p.session_id: p for p in accepted}

    try:
        instance = build_scenario_instance(base, overlay, constraints=constraints)
    except OverlayError as exc:
        # A scenario that cannot be applied is a report, not a crash: the
        # coordinator who wrote it needs to be told which step is wrong.
        empty = ProblemInstance(
            snapshot=base,
            constraints=tuple(constraints),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
            scenario_id=overlay.scenario_id,
        )
        return ScenarioOutcome(
            scenario_id=overlay.scenario_id,
            instance=empty,
            placement=dict(accepted),
            diff=ScheduleDiff(),
            failure_reason=str(exc),
        )

    region = affected_sessions(base, overlay)
    log.info(
        "scenario applied",
        extra={
            "scenario_id": overlay.scenario_id,
            "steps": len(overlay.perturbations),
            "region": len(region),
        },
    )

    # The accepted placement, carried into the perturbed instance as the
    # starting point. Sessions the overlay created have nowhere to start from
    # and are simply absent -- the solver places them.
    live = {s.id for s in instance.snapshot.sessions}
    start = {sid: p for sid, p in accepted.items() if sid in live}

    outcome = solve(
        instance,
        SolverConfig(time_limit_seconds=time_limit_seconds, random_seed=random_seed),
    )
    if not outcome.is_solved:
        return ScenarioOutcome(
            scenario_id=overlay.scenario_id,
            instance=instance,
            placement=start,
            diff=diff_schedules(instance, start, start),
            solver_status=outcome.solver_status.value if outcome.solver_status else None,
            failure_reason=(
                outcome.failure_reason
                or "the scenario could not be absorbed: no schedule satisfies it"
            ),
            affected_region=region,
        )

    placement = {
        a.session_id: PlacedSession(a.session_id, a.start_slot_id, a.room_id)
        for a in outcome.assignments
    }

    # Repair around the region. The first solve produced *a* schedule; this
    # spends a bounded budget pulling it back towards the accepted one, using
    # neighbourhoods scoped to what the scenario touched rather than to the
    # whole period.
    freed = 0
    if region and repair_iterations > 0:
        policies = region_policies(region)
        if not widen_region:
            policies = policies[:1]
        lns = optimise(
            instance,
            placement,
            LnsConfig(
                iterations=repair_iterations,
                repair_seconds=min(5.0, time_limit_seconds / 4),
                seed=random_seed,
                policies=policies,  # type: ignore[arg-type]
            ),
            SolverConfig(random_seed=random_seed),
        )
        if lns.placement:
            placement = dict(lns.placement)
        freed = sum(r.neighborhood_size for r in getattr(lns, "iterations", ()))

    return ScenarioOutcome(
        scenario_id=overlay.scenario_id,
        instance=instance,
        placement=placement,
        diff=diff_schedules(instance, start, placement),
        solver_status=outcome.solver_status.value if outcome.solver_status else None,
        is_solved=True,
        affected_region=region,
        freed_sessions=freed,
    )
