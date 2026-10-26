# ADR-0012: The drag interaction never writes to the accepted schedule

Date: 2026-08-18
Status: Accepted
Session: SESSION 10

## Context

SESSION 10 builds a drag-and-drop timetable. The playbook marks one rule
CRITICAL:

> drag/drop must not immediately modify the accepted schedule.
>
> Flow: drag → Scenario → ImpactAnalysis → preview → user action

The temptation this exists to forbid is the obvious implementation: move the
block, PATCH the assignment, show a toast. That produces a system where a
coordinator's hand slipping is indistinguishable from a decision, where the
published timetable changes without anybody approving it, and where "who moved
this class?" has no answer.

Three further constraints shape the design:

- **CP-SAT is the feasibility authority** (CLAUDE.md). The browser must never
  decide whether a placement is legal.
- **AI recommendations never self-apply**, and neither may a drag. Every
  change to an accepted schedule goes through the approval workflow.
- Scenarios are already modelled as **overlays** on base data (ADR-0007), and
  a drag is exactly one perturbation.

## Decision

A drag is a **proposal**, and proposals live in a scenario overlay. The
accepted schedule is written by exactly one thing: an approved
`ChangeRequest`.

The interaction is an explicit state machine, implemented as a pure reducer in
`frontend/lib/interaction/` with no network or DOM in it, so the rule above is
enforced by a test rather than by discipline.

```
                 ┌─────────┐
        ┌────────│ VIEWING │◄───────────────────────┐
        │        └─────────┘                        │
   grab │             ▲                             │ CANCEL / submitted
        ▼             │ release outside grid        │
  ┌──────────┐        │                             │
  │ DRAGGING │────────┘                             │
  └──────────┘                                      │
        │ drop on a cell                            │
        ▼                                           │
  ┌───────────┐   analysis fails   ┌────────┐       │
  │ ANALYSING │───────────────────►│ FAILED │───────┤
  └───────────┘                    └────────┘       │
        │ impact returned                           │
        ▼                                           │
  ┌────────────┐  SIMULATE   ┌────────────┐         │
  │ PREVIEWING │◄───────────►│ SIMULATING │         │
  └────────────┘             └────────────┘         │
        │ CREATE CHANGE REQUEST                     │
        ▼                                           │
  ┌────────────┐                                    │
  │ SUBMITTING │────────────────────────────────────┘
  └────────────┘
```

**No transition writes to the accepted schedule.** `SUBMITTING` creates a
`ChangeRequest`; approval is a separate act by a separate person, and is
SESSION 12's.

### What each state may touch

| State | Server calls | Writes |
|---|---|---|
| `VIEWING` | reads | none |
| `DRAGGING` | none | none — purely local |
| `ANALYSING` | `POST /schedule/impact` | a scenario overlay, discardable |
| `PREVIEWING` | none | none |
| `SIMULATING` | `POST /schedule/alternatives` | scenario overlays |
| `SUBMITTING` | `POST /change-requests` | a change request |
| `FAILED` | none | none |

`DRAGGING` making **no** server call is deliberate. A request per pointer-move
would put a database under a coordinator's mouse.

### The browser never decides feasibility

Legal drop targets are not computed in the client. The server returns each
session's **domain** — the slots and rooms it may occupy, from
`compute_domains()`, the same function the solver uses — and the UI greys out
everything else.

This matters more than it looks. If the client reimplemented "a teacher cannot
be in two places", the two implementations would drift, and the drift would
show up as a placement the UI allowed and the solver rejected. Rendering a
server-computed domain means the UI can be *incomplete* but never *wrong*: a
cell it greys out is genuinely unavailable, and a cell it allows still goes
through impact analysis before anything happens.

### Every drop is analysed, including the obviously fine ones

There is no fast path for a move that looks harmless. A move with zero impact
still shows a preview saying so, and still requires an explicit action.

The alternative — skipping the preview when nothing seems affected — makes the
interaction inconsistent exactly where a user is least careful, and "nothing
seems affected" is a judgement the client is not entitled to make.

## Consequences

**A slipped hand costs a click, not a timetable.** The worst outcome of a
mis-drag is a preview the user cancels.

**Every accepted change has a requester, a reason and an impact record**,
because the only door is the change request.

**The preview must be worth waiting for.** Impact analysis is a server round
trip on every drop, so it has to return something a coordinator could not
have worked out themselves: affected students, teachers, programmes,
faculties, rooms, conflicts created and resolved, per-tier score delta and
stability delta. A preview that said only "this is allowed" would be a
speed bump.

**Score delta is per tier, never a single number.** A scalar would
reintroduce the cross-tier exchange rate ADR-0010 rejects, and a UI showing
"+3" for a move that strands one student while pleasing forty would be
actively misleading.

**The state machine is testable without a browser.** It is a pure function of
(state, event); the DOM and `fetch` live outside it. "A drag never writes"
becomes an assertion over the transition table rather than a code review
promise.

**Optimistic UI is given up.** The block does not move until the user acts on
the preview. That is the point, and it is the cost.

## Alternatives considered

**Move immediately, offer undo.** Rejected: undo does not exist for a
published timetable that students have already read, and it gives no place to
record who decided what. It also makes the accepted schedule writable from a
pointer event, which is the thing the playbook marks CRITICAL.

**Validate in the client, submit only valid moves.** Rejected: it puts a
second feasibility implementation in TypeScript, which will drift from CP-SAT,
and CLAUDE.md makes CP-SAT the authority. Domains are rendered instead —
data from the authority, not a reimplementation of it.

**Auto-apply moves with no measurable impact.** Rejected: see above. It also
assumes impact analysis is complete, and "no impact we measured" is not "no
impact".

## Amendment (SESSION 19) — the keyboard path

The interaction above was mouse-only for nine sessions. That is not a rough
edge: every other control on the timetable had a keyboard route, and the one
action the page exists for did not, so a coordinator who cannot use a pointer
could not move a class at all.

**No new events.** Tab-to-focus, Enter to pick up, arrows to move, Enter to
drop, Escape to put back — dispatching the same `GRAB`, `DROP` and
`RELEASE_OUTSIDE` this ADR already defines. The state machine did not learn
that there are two input devices, so every guarantee asserted here (a drag
never writes, a proposal is discarded on CANCEL, the accepted schedule is
replaced only by a fresh load) covers the keyboard path without a new test
having to claim it separately.

**The cursor is not in the machine.** Where a caret is sitting is
presentation. Putting it in the reducer would give the "a drag never writes"
tests a new field to reason about and buy nothing.

**The decision is still not in the component.** `lib/interaction/keyboard.ts`
holds the interaction as a pure function — `keyboardMove(key, context)`
returning the events to dispatch, the next cursor, and what to announce. This
keeps the component decision-free, as this ADR requires, and it makes the rule
that matters assertable without a DOM: **a drop on a cell outside the
server-computed domain dispatches no event at all.** The alternative rejected
above — validating in the client — stays rejected: `keyboardMove` is handed
`allowedSlotIds` from the server and checks membership; it does not decide
feasibility, it reads it.

**The move is announced.** Cursor position, whether the cell can be used, the
edge of the grid, and the outcome of a drop all go to a live region. The
highlight that tells a sighted user where the cursor is tells a screen-reader
user nothing, and a cursor whose movement is silent is indistinguishable from
a page that has stopped responding.
