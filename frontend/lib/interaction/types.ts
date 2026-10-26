// The vocabulary of the drag interaction (ADR-0012).
//
// Kept separate from the reducer so the reducer file is only transitions, and
// so a component can import a type without importing the state machine.

/** Where a session currently sits, or is proposed to sit. */
export interface Placement {
  sessionId: string;
  startSlotId: string;
  roomId: string | null;
}

/** One session, moved. What a completed drag produces. */
export interface ProposedMove {
  sessionId: string;
  toStartSlotId: string;
  /** `null` means "leave the room alone" — a move along the time axis must
   *  not silently unassign a room. */
  toRoomId: string | null;
}

/** Counts, because the preview is read at a glance. */
export interface AffectedCounts {
  students: number;
  teachers: number;
  programs: number;
  faculties: number;
  rooms: number;
  sessions: number;
}

/** A named clash, not a number. "3 new conflicts" says investigate; a name
 *  says what to do. */
export interface ConflictChange {
  studentId: string;
  sessionA: string;
  sessionB: string;
  classification: string;
}

export interface ImpactReport {
  moves: ProposedMove[];
  affected: AffectedCounts;
  newConflicts: ConflictChange[];
  resolvedConflicts: ConflictChange[];
  /** Per tier, best-first. Negative is better. Never collapsed to a scalar:
   *  that would reintroduce the cross-tier exchange rate ADR-0010 rejects. */
  scoreDelta: number[];
  tierCodes: string[];
  stabilityDelta: number;
  /** A warning, not a feasibility verdict — CP-SAT owns that. */
  occupancyClashes: string[];
  summary: string;
}

/**
 * What a session is allowed to occupy, as computed by the *server* from
 * `compute_domains()` — the same function the solver uses.
 *
 * The client renders this and never derives it. A second feasibility
 * implementation in TypeScript would drift from CP-SAT, and the drift would
 * appear as a placement the UI allowed and the solver rejected.
 */
export interface SessionDomain {
  sessionId: string;
  startSlotIds: string[];
  roomIds: string[];
  isLocked: boolean;
}

export type InteractionStatus =
  | "VIEWING"
  | "DRAGGING"
  | "ANALYSING"
  | "PREVIEWING"
  | "SIMULATING"
  | "SUBMITTING"
  | "FAILED";

/** An alternative placement the server proposes when asked to simulate. */
export interface Alternative {
  label: string;
  moves: ProposedMove[];
  impact: ImpactReport;
}

export interface InteractionState {
  status: InteractionStatus;
  /** The accepted schedule. Only ever replaced wholesale by a fresh load —
   *  never edited by this reducer. See ADR-0012. */
  accepted: Readonly<Record<string, Placement>>;
  domains: Readonly<Record<string, SessionDomain>>;
  /** The session under the pointer, while dragging. */
  draggingSessionId: string | null;
  /** The proposal awaiting a decision. Discarded on CANCEL. */
  pendingMoves: ProposedMove[];
  impact: ImpactReport | null;
  alternatives: Alternative[];
  /** Set only in FAILED. Never contains a stack trace. */
  error: string | null;
  /** Set after a change request is created, so the UI can show it pending. */
  lastChangeRequestId: string | null;
}

export type InteractionEvent =
  | { type: "LOAD"; accepted: Placement[]; domains: SessionDomain[] }
  | { type: "GRAB"; sessionId: string }
  | { type: "RELEASE_OUTSIDE" }
  | { type: "DROP"; startSlotId: string; roomId: string | null }
  | { type: "IMPACT_READY"; impact: ImpactReport }
  | { type: "IMPACT_FAILED"; error: string }
  | { type: "CANCEL" }
  | { type: "SIMULATE_ALTERNATIVES" }
  | { type: "ALTERNATIVES_READY"; alternatives: Alternative[] }
  | { type: "CHOOSE_ALTERNATIVE"; index: number }
  | { type: "CREATE_CHANGE_REQUEST" }
  | { type: "CHANGE_REQUEST_CREATED"; changeRequestId: string }
  | { type: "SUBMIT_FAILED"; error: string }
  | { type: "DISMISS_ERROR" };
