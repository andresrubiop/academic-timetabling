// The drag interaction, as a pure reducer (ADR-0012).
//
// No `fetch`, no DOM, no timers. That is what makes the rule the playbook
// marks CRITICAL — *drag/drop must not immediately modify the accepted
// schedule* — checkable by a test over the transition table instead of by
// code review.
//
// The invariant, stated once:
//
//     No event returns a state whose `accepted` differs from the one it was
//     given, except `LOAD`, which replaces it with a fresh read from the
//     server.
//
// `assertNeverMutatesAccepted` in the tests asserts exactly that, over every
// event type, so a future transition that edits `accepted` fails CI rather
// than shipping.

import type {
  InteractionEvent,
  InteractionState,
  Placement,
  ProposedMove,
  SessionDomain,
} from "./types";

export const initialState: InteractionState = {
  status: "VIEWING",
  accepted: {},
  domains: {},
  draggingSessionId: null,
  pendingMoves: [],
  impact: null,
  alternatives: [],
  error: null,
  lastChangeRequestId: null,
};

function byId<T extends { sessionId: string }>(rows: T[]): Record<string, T> {
  return Object.fromEntries(rows.map((row) => [row.sessionId, row]));
}

/**
 * Whether a session may be dropped on this cell.
 *
 * Reads the server-computed domain. It does **not** reason about teachers,
 * rooms or students: that is CP-SAT's job, and a second implementation here
 * would drift from it. A cell this returns `false` for is genuinely
 * unavailable; a cell it returns `true` for still goes through impact
 * analysis before anything happens.
 */
export function canDrop(
  state: InteractionState,
  sessionId: string,
  startSlotId: string,
  roomId: string | null,
): boolean {
  const domain: SessionDomain | undefined = state.domains[sessionId];
  if (!domain || domain.isLocked) return false;
  if (!domain.startSlotIds.includes(startSlotId)) return false;
  if (roomId !== null && !domain.roomIds.includes(roomId)) return false;
  return true;
}

/** The placement the UI should render: accepted, with any pending proposal
 *  overlaid. The overlay is presentational only and is discarded on CANCEL. */
export function previewPlacement(
  state: InteractionState,
): Record<string, Placement> {
  const preview = { ...state.accepted };
  for (const move of state.pendingMoves) {
    const current = preview[move.sessionId];
    if (!current) continue;
    preview[move.sessionId] = {
      sessionId: move.sessionId,
      startSlotId: move.toStartSlotId,
      roomId: move.toRoomId ?? current.roomId,
    };
  }
  return preview;
}

export function reduce(
  state: InteractionState,
  event: InteractionEvent,
): InteractionState {
  switch (event.type) {
    // The only event that replaces `accepted`, and it does so with a fresh
    // read from the server rather than with anything the user did.
    case "LOAD":
      return {
        ...initialState,
        accepted: byId(event.accepted),
        domains: byId(event.domains),
        lastChangeRequestId: state.lastChangeRequestId,
      };

    case "GRAB":
      if (state.status !== "VIEWING") return state;
      if (state.domains[event.sessionId]?.isLocked) return state;
      return { ...state, status: "DRAGGING", draggingSessionId: event.sessionId };

    // Releasing outside the grid abandons the drag. Nothing was proposed, so
    // there is nothing to discard.
    case "RELEASE_OUTSIDE":
      if (state.status !== "DRAGGING") return state;
      return { ...state, status: "VIEWING", draggingSessionId: null };

    case "DROP": {
      if (state.status !== "DRAGGING" || !state.draggingSessionId) return state;
      const sessionId = state.draggingSessionId;
      if (!canDrop(state, sessionId, event.startSlotId, event.roomId)) {
        // An illegal target is not an error state — the user simply let go
        // over a greyed-out cell. Returning to VIEWING is what they expect.
        return { ...state, status: "VIEWING", draggingSessionId: null };
      }
      const move: ProposedMove = {
        sessionId,
        toStartSlotId: event.startSlotId,
        toRoomId: event.roomId,
      };
      // Every drop is analysed, including the ones that look harmless. There
      // is deliberately no fast path: skipping the preview when nothing seems
      // affected makes the interaction inconsistent exactly where a user is
      // least careful.
      return {
        ...state,
        status: "ANALYSING",
        draggingSessionId: null,
        pendingMoves: [move],
        impact: null,
        alternatives: [],
        error: null,
      };
    }

    case "IMPACT_READY":
      if (state.status !== "ANALYSING" && state.status !== "SIMULATING") {
        return state;
      }
      return { ...state, status: "PREVIEWING", impact: event.impact };

    case "IMPACT_FAILED":
      if (state.status !== "ANALYSING" && state.status !== "SIMULATING") {
        return state;
      }
      return { ...state, status: "FAILED", error: event.error };

    // Discards the proposal entirely. `accepted` was never touched, so there
    // is nothing to roll back.
    case "CANCEL":
      return {
        ...state,
        status: "VIEWING",
        draggingSessionId: null,
        pendingMoves: [],
        impact: null,
        alternatives: [],
        error: null,
      };

    case "SIMULATE_ALTERNATIVES":
      if (state.status !== "PREVIEWING") return state;
      return { ...state, status: "SIMULATING" };

    case "ALTERNATIVES_READY":
      if (state.status !== "SIMULATING") return state;
      return {
        ...state,
        status: "PREVIEWING",
        alternatives: event.alternatives,
      };

    case "CHOOSE_ALTERNATIVE": {
      if (state.status !== "PREVIEWING") return state;
      const chosen = state.alternatives[event.index];
      if (!chosen) return state;
      // Swapping the proposal, not applying it. Still a preview, still
      // requires an explicit CREATE_CHANGE_REQUEST.
      return {
        ...state,
        pendingMoves: chosen.moves,
        impact: chosen.impact,
      };
    }

    case "CREATE_CHANGE_REQUEST":
      if (state.status !== "PREVIEWING" || state.pendingMoves.length === 0) {
        return state;
      }
      return { ...state, status: "SUBMITTING" };

    // The change request exists; the accepted schedule still does not change.
    // Approval is a separate act by a separate person (SESSION 12).
    case "CHANGE_REQUEST_CREATED":
      return {
        ...state,
        status: "VIEWING",
        pendingMoves: [],
        impact: null,
        alternatives: [],
        error: null,
        lastChangeRequestId: event.changeRequestId,
      };

    case "SUBMIT_FAILED":
      if (state.status !== "SUBMITTING") return state;
      // Back to PREVIEWING, not FAILED: the proposal is still good and the
      // user can retry without redoing the drag.
      return { ...state, status: "PREVIEWING", error: event.error };

    case "DISMISS_ERROR":
      return {
        ...state,
        status: state.status === "FAILED" ? "VIEWING" : state.status,
        pendingMoves: state.status === "FAILED" ? [] : state.pendingMoves,
        error: null,
      };

    default: {
      // Exhaustiveness: adding an event type without handling it fails to
      // compile rather than silently doing nothing.
      const unreachable: never = event;
      return unreachable;
    }
  }
}

/** Actions the UI should offer right now. Derived, so a button cannot appear
 *  in a state that would ignore it. */
export function availableActions(state: InteractionState): string[] {
  switch (state.status) {
    case "PREVIEWING":
      return ["CANCEL", "SIMULATE_ALTERNATIVES", "CREATE_CHANGE_REQUEST"];
    case "ANALYSING":
    case "SIMULATING":
    case "SUBMITTING":
      return ["CANCEL"];
    case "FAILED":
      return ["DISMISS_ERROR"];
    default:
      return [];
  }
}
