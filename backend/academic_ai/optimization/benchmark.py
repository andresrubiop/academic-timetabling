"""Synthetic instances at scale, and the numbers a run produces on them.

SESSION 4 is forbidden from claiming an LNS improvement "without reproducible
evidence". This module is what makes that possible: a generator that produces
the *same* instance for the same seed and size, plus a recorder for the
quantities a comparison actually needs — model size, per-tier optima, wall
time, and whether the solver proved anything.

Deliberately modest. These are not real timetables; they are instances whose
size is known and whose feasibility is arranged, so that a change in solver
strategy shows up as a change in the numbers rather than as noise from a
different problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

from academic_ai.domain.enums import DayOfWeek, SessionType
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    PeriodSpec,
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)
from academic_ai.optimization.solve import SolveOutcome, SolverConfig, solve
from academic_ai.optimization.variables import build_variables

__all__ = ["BenchmarkCase", "BenchmarkResult", "generate", "run_case", "STANDARD_CASES"]

_DAYS = (
    DayOfWeek.MONDAY,
    DayOfWeek.TUESDAY,
    DayOfWeek.WEDNESDAY,
    DayOfWeek.THURSDAY,
    DayOfWeek.FRIDAY,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    code: str
    sections: int
    teachers: int
    rooms: int
    students: int
    slots_per_day: int = 8
    days: int = 5
    sessions_per_section: int = 2
    courses_per_student: int = 4
    seed: int = 0


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    case: str
    sessions: int
    start_variables: int
    room_variables: int
    solver_status: str
    wall_seconds: float
    build_seconds: float
    tier_values: tuple[tuple[str, int], ...]
    valid: bool
    assignments: int

    def as_row(self) -> str:  # pragma: no cover - display only
        return (
            f"{self.case:<12} sessions={self.sessions:<5} "
            f"start_vars={self.start_variables:<7} room_vars={self.room_variables:<7} "
            f"{self.solver_status:<10} build={self.build_seconds:6.2f}s "
            f"solve={self.wall_seconds:6.2f}s valid={self.valid}"
        )


def generate(case: BenchmarkCase) -> ProblemInstance:
    """A deterministic synthetic institution.

    Feasibility is arranged rather than hoped for: rooms and slots are
    provisioned generously enough that a schedule exists, so a benchmark
    measures search cost rather than accidentally measuring how fast the
    solver proves an impossible instance impossible.
    """
    rng = _Lcg(case.seed)

    slots = tuple(
        SlotSpec(
            id=f"d{d}s{i}",
            day_of_week=_DAYS[d % len(_DAYS)],
            in_day_index=i,
            global_index=d * case.slots_per_day + i,
            start_minute=(8 + i) * 60,
            is_schedulable=True,
        )
        for d in range(case.days)
        for i in range(case.slots_per_day)
    )

    rooms = tuple(
        RoomSpec(
            id=f"R{r}",
            campus_id="C1",
            capacity=60,
            room_type="CLASSROOM",
            has_declared_availability=True,
        )
        for r in range(case.rooms)
    )

    teachers = tuple(
        TeacherSpec(id=f"T{t}", has_declared_availability=True)
        for t in range(case.teachers)
    )

    sessions: list[SessionSpec] = []
    for s in range(case.sections):
        teacher = f"T{s % case.teachers}"
        for k in range(case.sessions_per_section):
            sessions.append(
                SessionSpec(
                    id=f"S{s}-{k}",
                    section_id=f"SEC{s}",
                    offering_id=f"OFF{s}",
                    course_id=f"CRS{s}",
                    campus_id="C1",
                    duration_slots=1 + (k % 2),
                    session_type=SessionType.LECTURE,
                    teacher_ids=(teacher,),
                    seat_demand=25,
                )
            )

    by_offering: dict[str, list[str]] = {}
    for session in sessions:
        by_offering.setdefault(session.offering_id, []).append(session.id)

    enrollments: list[StudentEnrollmentSpec] = []
    for student in range(case.students):
        chosen = {rng.below(case.sections) for _ in range(case.courses_per_student)}
        for offering_index in chosen:
            offering = f"OFF{offering_index}"
            enrollments.append(
                StudentEnrollmentSpec(
                    student_id=f"E{student}",
                    offering_id=offering,
                    session_ids=tuple(by_offering[offering]),
                    is_mandatory=True,
                    # Every course has an alternative offering in this
                    # synthetic institution, so clashes are MEDIUM rather than
                    # HARD and the instance stays feasible at scale.
                    alternative_offering_count=1,
                )
            )

    return ProblemInstance(
        snapshot=BaseSnapshot(
            period=PeriodSpec(
                id="P", slot_minutes=60, days=_DAYS[: min(case.days, len(_DAYS))]
            ),
            slots=slots,
            rooms=rooms,
            teachers=teachers,
            sessions=tuple(sessions),
            enrollments=tuple(enrollments),
        ),
        constraints=(),
        objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
    )


class _Lcg:
    """A tiny deterministic generator.

    `random` seeded globally would make one benchmark depend on whatever ran
    before it, which is the opposite of what this module is for.
    """

    def __init__(self, seed: int) -> None:
        self._state = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)

    def below(self, bound: int) -> int:
        self._state = (
            self._state * 6364136223846793005 + 1442695040888963407
        ) & ((1 << 64) - 1)
        return (self._state >> 33) % max(bound, 1)


def run_case(case: BenchmarkCase, config: SolverConfig | None = None) -> BenchmarkResult:
    config = config or SolverConfig(time_limit_seconds=30.0)
    instance = generate(case)

    build_started = time.monotonic()
    variables = build_variables(instance)
    build_seconds = time.monotonic() - build_started

    outcome: SolveOutcome = solve(instance, config)

    return BenchmarkResult(
        case=case.code,
        sessions=len(instance.snapshot.scheduled_sessions()),
        start_variables=sum(len(v) for v in variables.start_lit.values()),
        room_variables=sum(len(v) for v in variables.room_lit.values()),
        solver_status=(
            outcome.solver_status.value if outcome.solver_status else "BLOCKED"
        ),
        wall_seconds=outcome.wall_seconds,
        build_seconds=build_seconds,
        tier_values=tuple((t.code, t.value) for t in outcome.tiers),
        valid=bool(outcome.validation and outcome.validation.is_valid),
        assignments=len(outcome.assignments),
    )


#: Three scales. Small is the regression guard; the larger two are where the
#: encoding decisions in OPTIMIZER.md §6 stop being theoretical.
STANDARD_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase("small", sections=10, teachers=5, rooms=4, students=50),
    BenchmarkCase("medium", sections=60, teachers=25, rooms=20, students=400),
    BenchmarkCase("large", sections=200, teachers=80, rooms=60, students=2000),
)


def main(cases: Sequence[BenchmarkCase] = STANDARD_CASES) -> None:  # pragma: no cover
    for case in cases:
        print(run_case(case).as_row())


if __name__ == "__main__":  # pragma: no cover
    main()


# ---------------------------------------------------------------------------
# CP-SAT versus CP-SAT + LNS
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """One head-to-head run, at one seed.

    The playbook forbids claiming an LNS improvement without reproducible
    evidence, so everything needed to reproduce the claim travels with it: the
    case, the seed, both budgets, and both score vectors.
    """

    case: str
    seed: int
    baseline_score: tuple[int, ...]
    lns_score: tuple[int, ...]
    tier_codes: tuple[str, ...]
    baseline_seconds: float
    lns_seconds: float
    accepted: int
    iterations: int
    lns_valid: bool

    @property
    def improved(self) -> bool:
        return self.lns_score < self.baseline_score

    def delta(self) -> str:  # pragma: no cover - display only
        for code, a, b in zip(self.tier_codes, self.baseline_score, self.lns_score):
            if a != b:
                return f"{code} {a}->{b}"
        return "unchanged"

    def as_row(self) -> str:  # pragma: no cover - display only
        return (
            f"{self.case:<8} seed={self.seed}  "
            f"cpsat={self.baseline_seconds:5.1f}s  lns={self.lns_seconds:5.1f}s  "
            f"accepted={self.accepted:>2}/{self.iterations:<3} "
            f"valid={self.lns_valid}  {self.delta()}"
        )


def compare(
    case: BenchmarkCase,
    *,
    baseline_seconds: float = 20.0,
    iterations: int = 24,
    repair_seconds: float = 2.0,
    seed: int = 0,
) -> ComparisonResult:
    """Solve with plain CP-SAT, then improve with LNS from that same solution.

    LNS starts from the baseline's own result rather than from scratch, so the
    comparison isolates what the neighbourhood search adds. Starting it from a
    different solution would measure two different searches and attribute the
    difference to LNS.
    """
    from academic_ai.domain.scheduling.evaluate import evaluate
    from academic_ai.domain.scheduling.neighborhood import PlacedSession
    from academic_ai.optimization.lns import LnsConfig, optimise

    instance = generate(case)

    started = time.monotonic()
    outcome = solve(
        instance, SolverConfig(time_limit_seconds=baseline_seconds, random_seed=seed)
    )
    baseline_seconds_actual = time.monotonic() - started
    placement = {
        a.session_id: PlacedSession(a.session_id, a.start_slot_id, a.room_id)
        for a in outcome.assignments
    }
    baseline_score = evaluate(instance, placement)

    started = time.monotonic()
    lns = optimise(
        instance,
        placement,
        LnsConfig(iterations=iterations, repair_seconds=repair_seconds, seed=seed),
        SolverConfig(random_seed=seed),
    )
    lns_seconds = time.monotonic() - started

    return ComparisonResult(
        case=case.code,
        seed=seed,
        baseline_score=baseline_score.values,
        lns_score=lns.score.values,
        tier_codes=baseline_score.tier_codes,
        baseline_seconds=baseline_seconds_actual,
        lns_seconds=lns_seconds,
        accepted=lns.accepted_count,
        iterations=len(lns.iterations),
        lns_valid=lns.validation_ok,
    )
