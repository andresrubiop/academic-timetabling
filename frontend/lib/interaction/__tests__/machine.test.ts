// SESSION 10: the drag interaction, tested where the rule lives.
//
// The playbook marks one thing CRITICAL: drag/drop must not immediately
// modify the accepted schedule. `TestNothingWritesToAccepted` below asserts
// that over *every* event type rather than over the ones somebody remembered
// to check, so a future transition that edits `accepted` fails here instead
// of shipping.
//
// The reducer is pure, so none of this needs a browser, a network or a DOM.

import { describe, expect, it } from "vitest";

import {
  availableActions,
  canDrop,
  initialState,
  previewPlacement,
  reduce,
} from "../machine";
import type {
  Alternative,
  ImpactReport,
  InteractionEvent,
  InteractionState,
  Placement,
  SessionDomain,
} from "../types";

const PLACEMENTS: Placement[] = [
  { sessionId: "S1", startSlotId: "mon0", roomId: "R1" },
  { sessionId: "S2", startSlotId: "mon2", roomId: "R2" },
  { sessionId: "S3", startSlotId: "mon3", roomId: "R1" },
];

const DOMAINS: SessionDomain[] = [
  {
    sessionId: "S1",
    startSlotIds: ["mon0", "mon1", "mon2"],
    roomIds: ["R1", "R2"],
    isLocked: false,
  },
  {
    sessionId: "S2",
    startSlotIds: ["mon1", "mon2"],
    roomIds: ["R2"],
    isLocked: false,
  },
  // Locked: a published session a coordinator pinned. Must not be draggable.
  {
    sessionId: "S3",
    startSlotIds: ["mon0", "mon3"],
    roomIds: ["R1"],
    isLocked: true,
  },
];

function impact(overrides: Partial<ImpactReport> = {}): ImpactReport {
  return {
    moves: [],
    affected: {
      students: 0,
      teachers: 0,
      programs: 0,
      faculties: 0,
      rooms: 0,
      sessions: 0,
    },
    newConflicts: [],
    resolvedConflicts: [],
    scoreDelta: [0, 0],
    tierCodes: ["STUDENT_UNRESOLVABLE", "STUDENT_MANDATORY_CONFLICTS"],
    stabilityDelta: 0,
    occupancyClashes: [],
    summary: "no measured effect",
    ...overrides,
  };
}

function loaded(): InteractionState {
  return reduce(initialState, {
    type: "LOAD",
    accepted: PLACEMENTS,
    domains: DOMAINS,
  });
}

/** Walk to PREVIEWING the way a user would. */
function previewing(): InteractionState {
  let state = loaded();
  state = reduce(state, { type: "GRAB", sessionId: "S1" });
  state = reduce(state, { type: "DROP", startSlotId: "mon1", roomId: "R1" });
  return reduce(state, { type: "IMPACT_READY", impact: impact() });
}

// ---------------------------------------------------------------------------
// The CRITICAL rule
// ---------------------------------------------------------------------------

describe("nothing writes to the accepted schedule", () => {
  // Every event, with plausible payloads. LOAD is excluded and tested
  // separately: it is the one transition that legitimately replaces
  // `accepted`, and it does so with a fresh read from the server.
  const EVENTS: InteractionEvent[] = [
    { type: "GRAB", sessionId: "S1" },
    { type: "RELEASE_OUTSIDE" },
    { type: "DROP", startSlotId: "mon1", roomId: "R1" },
    { type: "IMPACT_READY", impact: impact() },
    { type: "IMPACT_FAILED", error: "boom" },
    { type: "CANCEL" },
    { type: "SIMULATE_ALTERNATIVES" },
    { type: "ALTERNATIVES_READY", alternatives: [] },
    { type: "CHOOSE_ALTERNATIVE", index: 0 },
    { type: "CREATE_CHANGE_REQUEST" },
    { type: "CHANGE_REQUEST_CREATED", changeRequestId: "cr-1" },
    { type: "SUBMIT_FAILED", error: "nope" },
    { type: "DISMISS_ERROR" },
  ];

  // Every reachable state, so an event is tried from places it is ignored as
  // well as places it acts.
  function states(): InteractionState[] {
    const base = loaded();
    const dragging = reduce(base, { type: "GRAB", sessionId: "S1" });
    const analysing = reduce(dragging, {
      type: "DROP",
      startSlotId: "mon1",
      roomId: "R1",
    });
    const preview = reduce(analysing, {
      type: "IMPACT_READY",
      impact: impact(),
    });
    const simulating = reduce(preview, { type: "SIMULATE_ALTERNATIVES" });
    const submitting = reduce(preview, { type: "CREATE_CHANGE_REQUEST" });
    const failed = reduce(analysing, { type: "IMPACT_FAILED", error: "x" });
    return [base, dragging, analysing, preview, simulating, submitting, failed];
  }

  it.each(EVENTS.map((e) => [e.type, e] as const))(
    "%s never changes `accepted`, from any state",
    (_name, event) => {
      for (const state of states()) {
        const before = JSON.stringify(state.accepted);
        const after = reduce(state, event);
        expect(JSON.stringify(after.accepted)).toBe(before);
      }
    },
  );

  it("LOAD is the only transition that replaces `accepted`", () => {
    const state = previewing();
    const reloaded = reduce(state, {
      type: "LOAD",
      accepted: [{ sessionId: "S1", startSlotId: "mon2", roomId: "R2" }],
      domains: DOMAINS,
    });
    expect(reloaded.accepted.S1.startSlotId).toBe("mon2");
    // ...and it discards any proposal in flight, because the server's answer
    // supersedes it.
    expect(reloaded.pendingMoves).toEqual([]);
    expect(reloaded.status).toBe("VIEWING");
  });

  it("a completed drag leaves the session where it was", () => {
    const state = previewing();
    expect(state.accepted.S1.startSlotId).toBe("mon0");
    expect(state.pendingMoves[0].toStartSlotId).toBe("mon1");
  });

  it("creating a change request still does not move anything", () => {
    let state = previewing();
    state = reduce(state, { type: "CREATE_CHANGE_REQUEST" });
    state = reduce(state, {
      type: "CHANGE_REQUEST_CREATED",
      changeRequestId: "cr-7",
    });
    expect(state.accepted.S1.startSlotId).toBe("mon0");
    expect(state.lastChangeRequestId).toBe("cr-7");
    expect(state.pendingMoves).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The flow the playbook specifies
// ---------------------------------------------------------------------------

describe("drag -> scenario -> impact -> preview -> action", () => {
  it("a drop goes to ANALYSING, never straight to a change", () => {
    let state = loaded();
    state = reduce(state, { type: "GRAB", sessionId: "S1" });
    expect(state.status).toBe("DRAGGING");
    state = reduce(state, { type: "DROP", startSlotId: "mon1", roomId: "R1" });
    expect(state.status).toBe("ANALYSING");
    expect(state.pendingMoves).toHaveLength(1);
  });

  it("offers exactly the three actions the playbook names", () => {
    expect(availableActions(previewing())).toEqual([
      "CANCEL",
      "SIMULATE_ALTERNATIVES",
      "CREATE_CHANGE_REQUEST",
    ]);
  });

  it("analyses even a move with no measured effect", () => {
    // No fast path. Skipping the preview when nothing seems affected makes
    // the interaction inconsistent exactly where a user is least careful.
    const state = previewing();
    expect(state.status).toBe("PREVIEWING");
    expect(state.impact?.summary).toBe("no measured effect");
  });

  it("CANCEL discards the proposal and returns to VIEWING", () => {
    const state = reduce(previewing(), { type: "CANCEL" });
    expect(state.status).toBe("VIEWING");
    expect(state.pendingMoves).toEqual([]);
    expect(state.impact).toBeNull();
  });

  it("SIMULATE_ALTERNATIVES returns to PREVIEWING with options", () => {
    let state = reduce(previewing(), { type: "SIMULATE_ALTERNATIVES" });
    expect(state.status).toBe("SIMULATING");

    const alternatives: Alternative[] = [
      {
        label: "Tuesday 09:00",
        moves: [{ sessionId: "S1", toStartSlotId: "mon2", toRoomId: "R2" }],
        impact: impact({ summary: "resolves 1 conflict", scoreDelta: [0, -1] }),
      },
    ];
    state = reduce(state, { type: "ALTERNATIVES_READY", alternatives });
    expect(state.status).toBe("PREVIEWING");
    expect(state.alternatives).toHaveLength(1);
  });

  it("choosing an alternative swaps the proposal, it does not apply it", () => {
    let state = reduce(previewing(), { type: "SIMULATE_ALTERNATIVES" });
    state = reduce(state, {
      type: "ALTERNATIVES_READY",
      alternatives: [
        {
          label: "later",
          moves: [{ sessionId: "S1", toStartSlotId: "mon2", toRoomId: "R2" }],
          impact: impact({ summary: "better" }),
        },
      ],
    });
    state = reduce(state, { type: "CHOOSE_ALTERNATIVE", index: 0 });

    expect(state.status).toBe("PREVIEWING");
    expect(state.pendingMoves[0].toStartSlotId).toBe("mon2");
    expect(state.accepted.S1.startSlotId).toBe("mon0");
  });

  it("CREATE_CHANGE_REQUEST needs a proposal", () => {
    const empty = loaded();
    expect(reduce(empty, { type: "CREATE_CHANGE_REQUEST" }).status).toBe(
      "VIEWING",
    );
  });
});

// ---------------------------------------------------------------------------
// Drop targets come from the server
// ---------------------------------------------------------------------------

describe("the client never decides feasibility", () => {
  it("allows a cell inside the server-computed domain", () => {
    expect(canDrop(loaded(), "S1", "mon1", "R1")).toBe(true);
  });

  it("refuses a slot outside the domain", () => {
    expect(canDrop(loaded(), "S1", "mon3", "R1")).toBe(false);
  });

  it("refuses a room outside the domain", () => {
    expect(canDrop(loaded(), "S2", "mon1", "R1")).toBe(false);
  });

  it("refuses everything for a locked session", () => {
    expect(canDrop(loaded(), "S3", "mon0", "R1")).toBe(false);
  });

  it("refuses a session it has no domain for", () => {
    // Absent data must not read as "anything goes".
    expect(canDrop(loaded(), "UNKNOWN", "mon0", "R1")).toBe(false);
  });

  it("a locked session cannot even be grabbed", () => {
    const state = reduce(loaded(), { type: "GRAB", sessionId: "S3" });
    expect(state.status).toBe("VIEWING");
    expect(state.draggingSessionId).toBeNull();
  });

  it("dropping on an illegal cell returns to VIEWING without an error", () => {
    // The user let go over a greyed-out cell. That is not a failure.
    let state = reduce(loaded(), { type: "GRAB", sessionId: "S1" });
    state = reduce(state, { type: "DROP", startSlotId: "mon3", roomId: "R1" });
    expect(state.status).toBe("VIEWING");
    expect(state.pendingMoves).toEqual([]);
    expect(state.error).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

describe("preview rendering", () => {
  it("overlays the proposal without touching accepted", () => {
    const state = previewing();
    const preview = previewPlacement(state);
    expect(preview.S1.startSlotId).toBe("mon1");
    expect(state.accepted.S1.startSlotId).toBe("mon0");
  });

  it("a time-only move keeps the room", () => {
    let state = loaded();
    state = reduce(state, { type: "GRAB", sessionId: "S1" });
    state = reduce(state, { type: "DROP", startSlotId: "mon1", roomId: null });
    expect(previewPlacement(state).S1.roomId).toBe("R1");
  });

  it("with no proposal, the preview is the accepted schedule", () => {
    const state = loaded();
    expect(previewPlacement(state)).toEqual(state.accepted);
  });
});

// ---------------------------------------------------------------------------
// Failures
// ---------------------------------------------------------------------------

describe("failure handling", () => {
  it("a failed analysis is dismissible back to VIEWING", () => {
    let state = loaded();
    state = reduce(state, { type: "GRAB", sessionId: "S1" });
    state = reduce(state, { type: "DROP", startSlotId: "mon1", roomId: "R1" });
    state = reduce(state, { type: "IMPACT_FAILED", error: "server said no" });

    expect(state.status).toBe("FAILED");
    expect(availableActions(state)).toEqual(["DISMISS_ERROR"]);

    state = reduce(state, { type: "DISMISS_ERROR" });
    expect(state.status).toBe("VIEWING");
    expect(state.pendingMoves).toEqual([]);
  });

  it("a failed submission returns to PREVIEWING so the drag is not lost", () => {
    let state = reduce(previewing(), { type: "CREATE_CHANGE_REQUEST" });
    state = reduce(state, { type: "SUBMIT_FAILED", error: "conflict" });
    expect(state.status).toBe("PREVIEWING");
    expect(state.pendingMoves).toHaveLength(1);
    expect(state.error).toBe("conflict");
  });

  it("events arriving in the wrong state are ignored, not crashes", () => {
    // A late IMPACT_READY after the user cancelled, for instance.
    const state = loaded();
    expect(reduce(state, { type: "IMPACT_READY", impact: impact() })).toBe(
      state,
    );
    expect(reduce(state, { type: "DROP", startSlotId: "mon1", roomId: null })).toBe(
      state,
    );
  });
});
