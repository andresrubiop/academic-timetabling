"""`ConflictPriorityProvider` — whose clash the optimizer should resolve first.

The third and last of the AI contracts named in ADR-0001, and like the other
two it lives in the framework-free domain so a learned model can implement it
without OR-Tools or SQLAlchemy in scope.

What this contract is *not*
===========================

It is not a way to decide which clashes matter. That is already decided,
categorically and lexicographically: a clash no registrar action can resolve
is `STUDENT_NO_OVERLAP_UNRESOLVABLE` and HARD, a clash someone could fix by
re-sectioning is `STUDENT_NO_OVERLAP_MANDATORY` and MEDIUM, three tiers lower.
One unresolvable student outranks any number of resolvable ones by
construction, and no number this contract returns can trade them off
(`students.py`, ADR-0010).

What a provider does is **rank within a category**: among students whose
clashes are all resolvable, whom should the search help first? That is a real
question with no categorical answer, and it is the one place in the objective
where a learned model has something to add.

`criticality()` lives here rather than in `students.py` because it *is* the
default implementation of this contract. Keeping it beside the feasibility
report was also a small import cycle waiting to happen: `evaluate` needs the
pair weighting, `students` needs `evaluate`.

Two properties the types enforce
================================

**Additive over students.** A provider scores one enrollment at a time. The
weight of a session pair is then the sum over the students who need both,
which is exactly the identity the CP-SAT encoding relies on: one reified
Boolean per distinct pair with an integer multiplicity, rather than one per
(student, pair). A max, a percentile, or anything superlinear silently breaks
that and makes the model an order of magnitude larger for the same answer.

**A priority may never be zero.** `MIN_PRIORITY` is 1 and
`validate_priorities` rejects anything below it. A zero-weight term contributes
nothing, so a provider returning zero would leave the optimizer perfectly happy
to strand that student — which is a model switching off a MEDIUM constraint by
arithmetic. CLAUDE.md says AI guides search and does not override HARD
constraints; this is the same rule one tier down, and it is worth stating in
code because a trained model reaches zero by gradient descent, not by
intention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Container, Mapping, Protocol, runtime_checkable

from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    StudentEnrollmentSpec,
    classify_student_conflict,
)

__all__ = [
    "CRITICALITY_SCALE",
    "MAX_PRIORITY",
    "MIN_PRIORITY",
    "ConflictPriorityProvider",
    "HeuristicConflictPriority",
    "PriorityKey",
    "PriorityRequest",
    "conflict_pair_weights",
    "criticality",
    "enrollment_key",
    "resolve_priorities",
    "validate_priorities",
]


#: Criticality is reported as an integer so it can be summed into a CP-SAT
#: objective without floating point. Three decimal places of resolution, the
#: same convention as `WEIGHT_SCALE` for human weights.
CRITICALITY_SCALE = 1000


def criticality(enrollment: StudentEnrollmentSpec) -> int:
    """How badly this student needs a clash on this enrollment resolved.

    Built from *how many ways out the student has*, which is the only thing
    that makes one resolvable clash worse than another:

    ``base = 1 / (1 + alternative sections + alternative offerings)``

    A student with no alternatives scores 1; with one, a half; with three, a
    quarter. That is a real quantity — the reciprocal of the student's options
    — rather than a number chosen to produce a desired ranking.

    Two multipliers, each justified rather than tuned:

    * **mandatory x2** — an elective can be dropped, so the student retains a
      move that the mandatory case does not offer.
    * **repeat x2 per additional attempt, capped** — a second attempt means the
      student has already lost a period to this course, and a third means two.
      The cap exists because the harm of another delay does not keep doubling
      indefinitely, and an uncapped multiplier would let one extreme record
      dominate an entire faculty.

    Additive over students by construction: this function reads one enrollment
    and returns a number, so summing over the students sharing a session pair
    is exactly the pair's weight.
    """
    options = (
        enrollment.alternative_section_count + enrollment.alternative_offering_count
    )
    score = CRITICALITY_SCALE / (1 + options)

    if enrollment.is_mandatory:
        score *= 2.0

    attempts = max(enrollment.attempt_number, 2 if enrollment.is_repeat else 1)
    if attempts > 1:
        score *= min(2.0 ** (attempts - 1), 8.0)

    return int(round(score))


#: An enrollment is identified by the student and the offering they want. There
#: is no surrogate id in the snapshot, and this pair is what `Enrollment` is
#: unique on in the schema.
PriorityKey = tuple[str, str]

#: A priority of zero removes the student from the objective entirely. See the
#: module docstring: that is a model switching a constraint off, not ranking.
MIN_PRIORITY = 1

#: Headroom over the heuristic's own maximum (16 000 = scale x mandatory x the
#: attempt cap) without letting a provider return a number large enough to
#: overflow the tier arithmetic once it is multiplied by a human weight and
#: summed over every pair in a faculty.
MAX_PRIORITY = 1_000_000


def enrollment_key(enrollment: StudentEnrollmentSpec) -> PriorityKey:
    return (enrollment.student_id, enrollment.offering_id)


@dataclass(frozen=True, slots=True)
class PriorityRequest:
    """What a provider is asked about.

    A snapshot, like `SearchState`: a provider must not be able to reach into
    the solver, and one that behaved differently depending on solver internals
    could not be reproduced.
    """

    instance: ProblemInstance
    #: Restrict the answer to these students. Empty means all of them.
    focus_student_ids: frozenset[str] = frozenset()

    def enrollments(self) -> tuple[StudentEnrollmentSpec, ...]:
        rows = self.instance.snapshot.enrollments
        if not self.focus_student_ids:
            return tuple(rows)
        return tuple(e for e in rows if e.student_id in self.focus_student_ids)


@runtime_checkable
class ConflictPriorityProvider(Protocol):
    """Ranks students within a conflict category. Decides nothing else.

    A provider may not classify a conflict, may not decide feasibility, and
    may not place anything. Classification is `classify_student_conflict`,
    feasibility is CP-SAT's, and placement is the solver's.
    """

    #: Stable identifier. Recorded on the run so a schedule's ranking can be
    #: traced back to whatever produced it.
    code: str

    def priorities(self, request: PriorityRequest) -> Mapping[PriorityKey, int]: ...


@dataclass(frozen=True, slots=True)
class HeuristicConflictPriority:
    """The default: `criticality()`, one enrollment at a time.

    The baseline every learned provider is compared against, and the thing
    that runs when no model is ACTIVE — which, per CLAUDE.md, is the normal
    state, because no model activates automatically.
    """

    code: str = "HEURISTIC_CRITICALITY"

    def priorities(self, request: PriorityRequest) -> Mapping[PriorityKey, int]:
        out: dict[PriorityKey, int] = {}
        for enrollment in request.enrollments():
            key = enrollment_key(enrollment)
            # Two rows for the same (student, offering) would be a snapshot
            # bug rather than something to average over; take the stronger
            # claim and let the schema's uniqueness catch the cause.
            score = max(MIN_PRIORITY, criticality(enrollment))
            out[key] = max(out.get(key, 0), min(score, MAX_PRIORITY))
        return out


def validate_priorities(
    values: Mapping[PriorityKey, int], request: PriorityRequest
) -> Mapping[PriorityKey, int]:
    """Reject a provider's answer rather than quietly repairing it.

    Clamping silently would let a model that has learned to output zero look
    like it is working. The provider is wrong, and the run should say so
    before a schedule is built on it.
    """
    for key, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"priority for {key} is {value!r}: priorities must be integers, "
                "because they are summed into a CP-SAT objective and floating "
                "point would make the tier value irreproducible"
            )
        if value < MIN_PRIORITY:
            raise ValueError(
                f"priority for {key} is {value}: a priority below {MIN_PRIORITY} "
                "removes the student from the objective, which switches off a "
                "constraint rather than ranking it (CLAUDE.md — AI guides "
                "search, it does not override constraints)"
            )
        if value > MAX_PRIORITY:
            raise ValueError(
                f"priority for {key} is {value}, above MAX_PRIORITY "
                f"({MAX_PRIORITY}). Summed over a faculty and multiplied by a "
                "human weight this overflows the tier arithmetic."
            )

    known = {enrollment_key(e) for e in request.enrollments()}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(
            f"provider returned priorities for {len(unknown)} enrollment(s) that "
            f"are not in the request, first {unknown[0]}: a provider answers "
            "about the instance it was given"
        )
    return values


def resolve_priorities(
    request: PriorityRequest,
    provider: ConflictPriorityProvider | None = None,
) -> Mapping[PriorityKey, int]:
    """Every enrollment in the request gets a priority, validated.

    A provider that omits an enrollment does not thereby delete it: the
    heuristic answers for whatever the provider left out. Omission is the
    other way a model could zero somebody out, and it would be harder to see
    than returning zero.
    """
    fallback = HeuristicConflictPriority()
    if provider is None:
        return fallback.priorities(request)

    supplied = validate_priorities(dict(provider.priorities(request)), request)
    out = dict(fallback.priorities(request))
    out.update(supplied)
    return out


def conflict_pair_weights(
    instance: ProblemInstance,
    classification: str,
    *,
    priorities: Mapping[PriorityKey, int] | None = None,
    known_session_ids: Container[str] | None = None,
) -> Mapping[tuple[str, str], int]:
    """Session pair -> total weight of the students who need both.

    **One function, two callers, by design.** The CP-SAT objective and the
    solver-free `evaluate.ScoreVector` both need this number, and they have
    already disagreed once — by exactly 1000x, which made two runs
    incomparable while both looked correct and broke the LNS acceptance rule
    silently. Two loops computing the same quantity in two layers is the
    mechanism; the only fix that holds is for there to be one loop.

    Aggregated by *distinct pair* with an integer weight rather than one entry
    per (student, pair): the weight is additive over students, so the
    aggregation is exact, and at institutional scale the naive reading is an
    order of magnitude more Booleans for the same objective value.

    ``priorities`` omitted means every incidence counts 1 — a plain incidence
    count, which is what `WeightSource.UNIFORM` asks for. Supplied, each
    incidence counts ``p(a) + p(b)``: the harm of a clash is a property of
    both enrollments, not of whichever one happens to be listed first.
    """
    by_student: dict[str, list[StudentEnrollmentSpec]] = {}
    for enrollment in instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    weights: dict[tuple[str, str], int] = {}
    for enrollments in by_student.values():
        for i, a in enumerate(enrollments):
            for b in enrollments[i + 1 :]:
                if classify_student_conflict(a, b) != classification:
                    continue
                if priorities is None:
                    incidence = 1
                else:
                    incidence = priorities.get(
                        enrollment_key(a), MIN_PRIORITY
                    ) + priorities.get(enrollment_key(b), MIN_PRIORITY)
                for sa in a.session_ids:
                    for sb in b.session_ids:
                        if sa == sb:
                            continue
                        if known_session_ids is not None and (
                            sa not in known_session_ids or sb not in known_session_ids
                        ):
                            continue
                        key = (min(sa, sb), max(sa, sb))
                        weights[key] = weights.get(key, 0) + incidence
    return weights
