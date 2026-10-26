"""CP-SAT variables, built from the domains the model already computed.

Nothing here re-derives a domain. `domains.compute_domains()` is the single
place that decides where a session may start and which rooms it may use, and
this module turns that answer into variables. Re-deriving it would give the
solver a second, silently divergent opinion about what is feasible.

Encoding
========

Both shapes CP-SAT wants, channelled together:

* ``start_lit[s][t]`` — one Boolean per feasible start slot, with
  ``ExactlyOne``. Availability, preferences and per-slot reasoning are
  naturally Boolean.
* ``start[s]`` — an integer, ``Σ_t occ(t)·start_lit[s][t]``. Ordering, gaps
  and travel time are naturally integer.
* ``room_lit[s][r]`` / ``room[s]`` — the same pair for rooms.

The occupancy index
===================

Intervals are posted over a **synthetic** occupancy index,
``day_ordinal · stride + in_day_index``, rather than the stored
``global_index``. The stride exceeds the longest day, so two sessions on
different days can never produce overlapping ranges no matter what the stored
week index happens to contain.

That matters because the whole no-overlap story rests on it. Domain
restriction guarantees a session fits inside its own day, so ranges
``[occ, occ + duration)`` stay within one day; combined with a stride wider
than any day, ranges on different days are disjoint by construction. Teacher,
room, section and student disjunctions can then be plain ``AddNoOverlap``
calls instead of O(n²) pairwise reasoning — and a Friday-evening class can
never be found to "overlap" Monday morning because somebody numbered the week
oddly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ortools.sat.python import cp_model

from academic_ai.domain.enums import DayOfWeek
from academic_ai.domain.scheduling.domains import SessionDomain, compute_domains
from academic_ai.domain.scheduling.priority import (
    ConflictPriorityProvider,
    PriorityKey,
    PriorityRequest,
    resolve_priorities,
)
from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
)

__all__ = ["ModelVariables", "build_variables"]


@dataclass(slots=True)
class ModelVariables:
    """Every variable the adapters may touch, and the index they share."""

    model: cp_model.CpModel
    instance: ProblemInstance
    domains: Mapping[str, SessionDomain]

    slots: Mapping[str, SlotSpec]
    rooms: Mapping[str, RoomSpec]
    sessions: Mapping[str, SessionSpec]

    #: session -> slot id -> literal "this session starts here"
    start_lit: dict[str, dict[str, cp_model.IntVar]] = field(default_factory=dict)
    #: session -> integer occupancy index of its start
    start: dict[str, cp_model.IntVar] = field(default_factory=dict)
    #: session -> room id -> literal "this session is in this room"
    room_lit: dict[str, dict[str, cp_model.IntVar]] = field(default_factory=dict)
    #: session -> interval over the occupancy index
    interval: dict[str, cp_model.IntervalVar] = field(default_factory=dict)
    #: (session, room) -> optional interval, present iff the room is chosen
    room_interval: dict[tuple[str, str], cp_model.IntervalVar] = field(
        default_factory=dict
    )
    #: slot id -> occupancy index
    occupancy_index: dict[str, int] = field(default_factory=dict)
    #: How far apart consecutive days are on the occupancy index.
    stride: int = 0
    #: session -> day ordinal of its start. Built on demand by `day_var`.
    day: dict[str, cp_model.IntVar] = field(default_factory=dict)
    #: (student, offering) -> conflict priority, already validated. Resolved
    #: once per build: a provider must see one consistent instance, and asking
    #: it repeatedly would also let a nondeterministic model make the model
    #: itself irreproducible.
    priorities: Mapping[PriorityKey, int] = field(default_factory=dict)
    #: Whatever produced `priorities`. Recorded so a schedule's ranking can be
    #: traced to its source.
    priority_source: str = "HEURISTIC_CRITICALITY"

    def day_var(self, session_id: str) -> cp_model.IntVar:
        """The day ordinal a session lands on, as an integer variable.

        The occupancy index is a **disjunction** axis: it exists so that two
        sessions on different days can never produce overlapping ranges, and
        the stride between days is deliberately arbitrary. Any constraint about
        *elapsed time* — travel between campuses, a gap between classes — is
        therefore only meaningful within one day, and needs this variable to
        say so. Reading a distance off the occupancy index across a day
        boundary measures the stride, not the night.

        Channelled from the start literals rather than derived by dividing the
        occupancy index: the literals already exist, the channel is linear, and
        CP-SAT propagates it far better than an integer division.

        Created lazily. Most instances are single-campus and never ask.
        """
        existing = self.day.get(session_id)
        if existing is not None:
            return existing
        lits = self.start_lit[session_id]
        ordinals = {
            slot_id: self.occupancy_index[slot_id] // self.stride for slot_id in lits
        }
        var = self.model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(sorted(set(ordinals.values()))),
            f"day[{session_id}]",
        )
        self.model.Add(
            var == sum(ordinals[slot_id] * lit for slot_id, lit in lits.items())
        )
        self.day[session_id] = var
        return var

    def scheduled_ids(self) -> tuple[str, ...]:
        """Sessions that actually have variables."""
        return tuple(self.start)

    def sessions_of_teacher(self, teacher_id: str) -> tuple[str, ...]:
        return tuple(
            sid
            for sid in self.start
            if teacher_id in self.sessions[sid].teacher_ids
        )

    def slot_for_index(self, index: int) -> SlotSpec | None:
        for slot_id, occ in self.occupancy_index.items():
            if occ == index:
                return self.slots[slot_id]
        return None

    def day_of(self, slot_id: str) -> DayOfWeek:
        return self.slots[slot_id].day_of_week


def build_variables(
    instance: ProblemInstance,
    *,
    priority_provider: ConflictPriorityProvider | None = None,
) -> ModelVariables:
    """Construct the CP-SAT variables for one instance.

    Sessions whose domain is empty still get no variables: an empty domain is
    a blocking diagnostic that must have stopped the run before this point,
    and inventing a variable for an unplaceable session would convert a
    precise "this class cannot be placed" into a global INFEASIBLE.
    """
    snapshot = instance.snapshot
    domains, _ = compute_domains(snapshot, instance.constraints)

    slots = {s.id: s for s in snapshot.slots}
    rooms = {r.id: r for r in snapshot.rooms}
    sessions = {s.id: s for s in snapshot.sessions}

    # Resolved here, before any variable exists, so the objective and the
    # solver-free scorer are handed the same numbers rather than each asking
    # the provider separately.
    priorities = resolve_priorities(PriorityRequest(instance), priority_provider)

    day_order = {day: i for i, day in enumerate(snapshot.period.days)}
    # From the largest in-day index, not the slot *count*. A grid with a hole
    # in it (a deleted timeslot row, an overlay that removed a slot) has more
    # indices than slots, and a count-derived stride then makes two different
    # days share an occupancy value — which shows up as an unexplainable
    # INFEASIBLE, never as a wrong schedule, so no validator would catch it.
    stride = (
        max((slot.in_day_index for slot in snapshot.slots), default=0) + 2
    )

    occupancy_index = {
        slot.id: day_order[slot.day_of_week] * stride + slot.in_day_index
        for slot in snapshot.slots
        if slot.day_of_week in day_order
    }

    variables = ModelVariables(
        model=cp_model.CpModel(),
        instance=instance,
        domains=domains,
        slots=slots,
        rooms=rooms,
        sessions=sessions,
        occupancy_index=occupancy_index,
        stride=stride,
        priorities=priorities,
        priority_source=(
            priority_provider.code if priority_provider is not None
            else "HEURISTIC_CRITICALITY"
        ),
    )
    model = variables.model

    for session_id, domain in domains.items():
        if domain.is_empty:
            continue
        session = sessions[session_id]

        # --- start ---------------------------------------------------------
        lits = {
            slot_id: model.NewBoolVar(f"start[{session_id},{slot_id}]")
            for slot_id in domain.start_slot_ids
        }
        model.AddExactlyOne(lits.values())
        variables.start_lit[session_id] = lits

        candidates = sorted(occupancy_index[s] for s in domain.start_slot_ids)
        start = model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(candidates), f"start[{session_id}]"
        )
        model.Add(
            start
            == sum(
                occupancy_index[slot_id] * lit for slot_id, lit in lits.items()
            )
        )
        variables.start[session_id] = start

        variables.interval[session_id] = model.NewFixedSizeIntervalVar(
            start, session.duration_slots, f"interval[{session_id}]"
        )

        # --- room ----------------------------------------------------------
        if not domain.needs_room:
            continue
        room_lits = {
            room_id: model.NewBoolVar(f"room[{session_id},{room_id}]")
            for room_id in domain.room_ids
        }
        model.AddExactlyOne(room_lits.values())
        variables.room_lit[session_id] = room_lits

        for room_id, lit in room_lits.items():
            variables.room_interval[(session_id, room_id)] = (
                model.NewOptionalFixedSizeIntervalVar(
                    start,
                    session.duration_slots,
                    lit,
                    f"occupies[{session_id},{room_id}]",
                )
            )

    _hint_from_base(variables)
    return variables


def _hint_from_base(v: ModelVariables) -> int:
    """Seed the search with the schedule we are repairing.

    Re-optimisation starts from a timetable that already works — an imported
    FET file, or the previously published version. Handing CP-SAT that
    assignment as a hint means it begins from a known-good point instead of
    rediscovering the institution's whole timetable from nothing, which on a
    real 399-activity instance is the difference between a solution and a
    60-second UNKNOWN.

    A hint is advice, never a constraint: values that are no longer in a
    session's domain (because the thing being repaired is precisely what
    changed) are skipped rather than forced. `ASSIGNMENT_LOCKED` is what makes
    a placement binding.
    """
    hinted = 0
    for base in v.instance.snapshot.base_assignments:
        lits = v.start_lit.get(base.session_id)
        if not lits:
            continue
        if base.start_slot_id in lits:
            for slot_id, lit in lits.items():
                v.model.AddHint(lit, slot_id == base.start_slot_id)
            hinted += 1
        room_lits = v.room_lit.get(base.session_id)
        if base.room_id and room_lits and base.room_id in room_lits:
            for room_id, lit in room_lits.items():
                v.model.AddHint(lit, room_id == base.room_id)
    return hinted
