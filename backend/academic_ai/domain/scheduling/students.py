"""Student Conflict Graph, criticality, and feasibility (SESSION 5).

Enrollment is authoritative for student conflicts. Everything here is derived
from `Enrollment` alone; cohorts are never a conflict authority (ADR-0009).

Where the "one outranks many" rule actually lives
=================================================

The playbook's headline rule is that *one student with a repeated mandatory
course and only one section may be more critical than many students with
multiple alternatives*. It is tempting to implement that as a very large
weight. That would be wrong, and it is not what this module does.

The rule is already enforced **by category**, lexicographically:

* a clash no registrar action can resolve is `STUDENT_NO_OVERLAP_UNRESOLVABLE`,
  which is HARD;
* a clash someone could fix by re-sectioning is `STUDENT_NO_OVERLAP_MANDATORY`,
  which is MEDIUM and sits three tiers lower.

So one unresolvable student already outranks any number of resolvable ones, by
construction, and no weight can trade them off. Trying to reproduce that with
numbers would mean inventing an exchange rate between "a student loses their
year" and "twenty students are inconvenienced" — precisely the arbitrary
weighting the playbook forbids and ADR-0010 rejects.

What criticality does, then, is **rank within a category**: among the students
whose clashes are all resolvable, whom should the optimizer help first? That
is a genuine question with no categorical answer, and it is what a weight is
for.

Additivity is a contract, not a convenience
===========================================

`criticality()` — which lives in `priority.py`, as the default implementation
of `ConflictPriorityProvider`, and is re-exported here — must be **additive
over students**. The conflict encoding
aggregates by distinct session pair with an integer multiplicity — one
reified Boolean per pair rather than per (student, pair) — which is only
valid if the weight of a pair is the *sum* of its students' criticalities. A
max, a percentile, or anything superlinear breaks that identity and makes the
CP-SAT model an order of magnitude larger for the same answer.
`tests/test_students.py` asserts the identity directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from academic_ai.domain.scheduling.evaluate import occupied_cells
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.priority import CRITICALITY_SCALE, criticality
from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    StudentEnrollmentSpec,
    classify_student_conflict,
)

__all__ = [
    "CRITICALITY_SCALE",
    "ConflictEdge",
    "FeasibilityReport",
    "FeasibilityScope",
    "StudentConflictGraph",
    "build_conflict_graph",
    "criticality",
    "feasibility",
    "student_feasibility",
]

class FeasibilityScope(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    STUDENT = "STUDENT"
    PROGRAM = "PROGRAM"
    FACULTY = "FACULTY"
    INSTITUTION = "INSTITUTION"


def _has_no_alternative(enrollment: StudentEnrollmentSpec) -> bool:
    return not enrollment.has_alternative


# ---------------------------------------------------------------------------
# The conflict graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConflictEdge:
    """Two sessions that at least one student needs to attend both of.

    ``weight`` is the summed criticality of those students, which is exactly
    the integer multiplicity the CP-SAT encoding needs.
    """

    session_a: str
    session_b: str
    student_ids: frozenset[str]
    classification: str
    weight: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.session_a, self.session_b)

    @property
    def affected_students(self) -> int:
        return len(self.student_ids)

    @property
    def is_unresolvable(self) -> bool:
        return self.classification == "STUDENT_NO_OVERLAP_UNRESOLVABLE"


@dataclass(frozen=True, slots=True)
class StudentConflictGraph:
    """Sessions as nodes, shared students as edges.

    Built from enrollments and **independent of any schedule**: it says which
    sessions *would* clash if placed together, which is what makes it useful
    for choosing a neighbourhood before knowing where anything sits.
    """

    edges: tuple[ConflictEdge, ...]
    session_ids: frozenset[str]

    def edges_of(self, session_id: str) -> tuple[ConflictEdge, ...]:
        return tuple(
            e for e in self.edges if session_id in (e.session_a, e.session_b)
        )

    def degree(self, session_id: str) -> int:
        return len(self.edges_of(session_id))

    def pressure(self, session_id: str) -> int:
        """Total criticality riding on this session."""
        return sum(e.weight for e in self.edges_of(session_id))

    def adjacency(self) -> Mapping[str, frozenset[str]]:
        out: dict[str, set[str]] = {sid: set() for sid in self.session_ids}
        for edge in self.edges:
            out[edge.session_a].add(edge.session_b)
            out[edge.session_b].add(edge.session_a)
        return {k: frozenset(v) for k, v in out.items()}

    def components(self) -> tuple[frozenset[str], ...]:
        """Connected components.

        A clash can only be resolved by moving sessions within one component,
        so a component is the smallest unit worth re-deciding — which is what
        `ConflictComponentDestroy` exploits and what SESSION 7's GNN will
        learn over.
        """
        adjacency = self.adjacency()
        seen: set[str] = set()
        found: list[frozenset[str]] = []
        for root in sorted(adjacency):
            if root in seen:
                continue
            component: set[str] = set()
            frontier = [root]
            while frontier:
                current = frontier.pop()
                if current in component:
                    continue
                component.add(current)
                frontier.extend(sorted(adjacency[current] - component))
            seen |= component
            found.append(frozenset(component))
        return tuple(found)

    def realised(
        self, instance: ProblemInstance, placement: Mapping[str, PlacedSession]
    ) -> tuple[ConflictEdge, ...]:
        """The edges that are actual clashes in a given schedule.

        The graph says what *could* clash; this says what *does*. SESSION 6
        needs the difference to explain why a schedule is bad rather than
        merely that it is.
        """
        cells = {sid: occupied_cells(instance, p) for sid, p in placement.items()}
        return tuple(
            edge
            for edge in self.edges
            if edge.session_a in cells
            and edge.session_b in cells
            and cells[edge.session_a] & cells[edge.session_b]
        )


def build_conflict_graph(instance: ProblemInstance) -> StudentConflictGraph:
    """Derive the graph from enrollments.

    An enrollment that could not be resolved to sessions contributes nothing —
    it has no sessions to clash. That is a real gap and the importer reports
    it (`UNPLACED_ENROLLMENT_AMBIGUOUS`); silently treating such a student as
    conflict-free is the failure ADR-0009 warns about, so it must stay visible
    upstream rather than be papered over here.
    """
    by_student: dict[str, list[StudentEnrollmentSpec]] = {}
    for enrollment in instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    scheduled = {s.id for s in instance.snapshot.scheduled_sessions()}
    accumulated: dict[tuple[str, str], dict[str, object]] = {}

    for student_id, enrollments in by_student.items():
        for i, a in enumerate(enrollments):
            for b in enrollments[i + 1 :]:
                classification = classify_student_conflict(a, b)
                weight = criticality(a) + criticality(b)
                for sa in a.session_ids:
                    for sb in b.session_ids:
                        if sa == sb or sa not in scheduled or sb not in scheduled:
                            continue
                        key = (min(sa, sb), max(sa, sb))
                        entry = accumulated.setdefault(
                            key,
                            {
                                "students": set(),
                                "weight": 0,
                                "classification": classification,
                            },
                        )
                        entry["students"].add(student_id)  # type: ignore[union-attr]
                        entry["weight"] = int(entry["weight"]) + weight
                        # The worst classification on an edge wins: if any
                        # student on it cannot be re-sectioned, the edge is
                        # unresolvable however many others could move.
                        if classification == "STUDENT_NO_OVERLAP_UNRESOLVABLE":
                            entry["classification"] = classification

    edges = tuple(
        ConflictEdge(
            session_a=key[0],
            session_b=key[1],
            student_ids=frozenset(entry["students"]),  # type: ignore[arg-type]
            classification=str(entry["classification"]),
            weight=int(entry["weight"]),
        )
        for key, entry in sorted(accumulated.items())
    )
    return StudentConflictGraph(edges=edges, session_ids=frozenset(scheduled))


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeasibilityReport:
    """Whether a scope's students can actually take what they enrolled in."""

    scope: FeasibilityScope
    scope_id: str
    students: int
    affected_students: int
    unresolvable_students: int
    unresolvable_incidences: int
    mandatory_incidences: int
    elective_incidences: int
    total_criticality: int

    @property
    def is_feasible(self) -> bool:
        """Feasible means no student is stranded.

        Resolvable clashes do not make a scope infeasible: somebody can move a
        student. Only the unresolvable ones mean a student cannot take what
        they are enrolled in no matter what anyone does.
        """
        return self.unresolvable_students == 0

    @property
    def affected_fraction(self) -> float:
        return self.affected_students / self.students if self.students else 0.0


def student_feasibility(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession],
    student_id: str,
) -> FeasibilityReport:
    return _report(
        instance, placement, FeasibilityScope.STUDENT, student_id, {student_id}
    )


def feasibility(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    scope: FeasibilityScope = FeasibilityScope.INSTITUTION,
    scope_id: str = "",
    *,
    students: Mapping[str, str] | None = None,
) -> tuple[FeasibilityReport, ...]:
    """Feasibility at the requested level.

    ``students`` maps student id -> the id of the scope they belong to
    (programme, faculty). It is supplied by the caller rather than derived
    here, because `BaseSnapshot` deliberately carries only what the optimizer
    needs and the programme/faculty tree lives in the database.
    """
    if not isinstance(placement, Mapping):
        placement = {p.session_id: p for p in placement}

    all_students = {e.student_id for e in instance.snapshot.enrollments}
    if scope is FeasibilityScope.INSTITUTION or students is None:
        return (
            _report(instance, placement, scope, scope_id or "INSTITUTION", all_students),
        )

    grouped: dict[str, set[str]] = {}
    for student_id in all_students:
        grouped.setdefault(students.get(student_id, "UNKNOWN"), set()).add(student_id)
    return tuple(
        _report(instance, placement, scope, group_id, members)
        for group_id, members in sorted(grouped.items())
    )


def _report(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession],
    scope: FeasibilityScope,
    scope_id: str,
    members: set[str],
) -> FeasibilityReport:
    graph = build_conflict_graph(instance)
    realised = graph.realised(instance, placement)

    affected: set[str] = set()
    stranded: set[str] = set()
    unresolvable = mandatory = elective = 0
    total_criticality = 0

    by_student: dict[str, list[StudentEnrollmentSpec]] = {}
    for enrollment in instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    cells = {sid: occupied_cells(instance, p) for sid, p in placement.items()}
    for student_id in members:
        enrollments = by_student.get(student_id, [])
        for i, a in enumerate(enrollments):
            for b in enrollments[i + 1 :]:
                classification = classify_student_conflict(a, b)
                clashing = any(
                    sa != sb
                    and sa in cells
                    and sb in cells
                    and cells[sa] & cells[sb]
                    for sa in a.session_ids
                    for sb in b.session_ids
                )
                if not clashing:
                    continue
                affected.add(student_id)
                total_criticality += criticality(a) + criticality(b)
                if classification == "STUDENT_NO_OVERLAP_UNRESOLVABLE":
                    unresolvable += 1
                    stranded.add(student_id)
                elif classification == "STUDENT_NO_OVERLAP_MANDATORY":
                    mandatory += 1
                else:
                    elective += 1

    return FeasibilityReport(
        scope=scope,
        scope_id=scope_id,
        students=len(members),
        affected_students=len(affected),
        unresolvable_students=len(stranded),
        unresolvable_incidences=unresolvable,
        mandatory_incidences=mandatory,
        elective_incidences=elective,
        total_criticality=total_criticality,
    )
