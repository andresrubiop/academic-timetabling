/**
 * Moving a class with the keyboard.
 *
 * The timetable's drag-and-drop was mouse-only, which is not a rough edge: a
 * coordinator who cannot use a mouse could not move a class *at all*. Every
 * other control on the page had a keyboard path; the one thing the page
 * exists to do did not.
 *
 * The flow mirrors the pointer one exactly, so the state machine is unchanged
 * (ADR-0012 and its tests still hold):
 *
 *     Tab to a class → Enter picks it up → arrows move the cursor
 *     → Enter drops → Escape puts it back
 *
 * The cursor lives in component state, not in the reducer. It is presentation
 * -- where a caret is sitting -- and putting it in the machine would mean the
 * "a drag never writes" tests had a new field to reason about for no gain.
 *
 * These functions are pure so the movement rules can be tested without a
 * browser, which is the only way anybody would test them.
 */

export interface GridCell {
  slotId: string;
  day: string;
  inDayIndex: number;
}

export type Direction = "up" | "down" | "left" | "right";

/** Ordered distinct days, in the order the grid renders them. */
export function daysOf(cells: GridCell[]): string[] {
  const seen: string[] = [];
  for (const cell of cells) {
    if (!seen.includes(cell.day)) seen.push(cell.day);
  }
  return seen;
}

/** Ordered distinct rows. */
export function rowsOf(cells: GridCell[]): number[] {
  return Array.from(new Set(cells.map((c) => c.inDayIndex))).sort(
    (a, b) => a - b,
  );
}

/**
 * The cell an arrow key moves to, or `null` at the edge.
 *
 * Deliberately **does not wrap**. Wrapping from Friday evening to Monday
 * morning saves one keypress and costs the user their sense of where they
 * are; stopping at the edge is what every spreadsheet does, and this is a
 * grid of hours.
 *
 * Holes in the grid -- a period with no Wednesday 11:00 -- are skipped over
 * rather than blocking movement, because a gap in the timetable is not a wall.
 */
export function nextCell(
  cells: GridCell[],
  from: GridCell,
  direction: Direction,
): GridCell | null {
  const days = daysOf(cells);
  const rows = rowsOf(cells);
  const dayIndex = days.indexOf(from.day);
  const rowIndex = rows.indexOf(from.inDayIndex);
  if (dayIndex < 0 || rowIndex < 0) return null;

  const step = direction === "up" || direction === "left" ? -1 : 1;
  const alongDays = direction === "left" || direction === "right";

  let d = dayIndex;
  let r = rowIndex;
  // Walk past holes. Bounded by the grid, so this cannot loop.
  for (let attempt = 0; attempt < (alongDays ? days.length : rows.length); attempt++) {
    if (alongDays) d += step;
    else r += step;
    if (d < 0 || d >= days.length || r < 0 || r >= rows.length) return null;

    const candidate = cells.find(
      (c) => c.day === days[d] && c.inDayIndex === rows[r],
    );
    if (candidate) return candidate;
  }
  return null;
}

/** The first cell a session could legally be dropped on, for starting the
 *  cursor somewhere useful rather than at the top-left of the week. */
export function firstAllowedCell(
  cells: GridCell[],
  allowedSlotIds: readonly string[],
): GridCell | null {
  return cells.find((c) => allowedSlotIds.includes(c.slotId)) ?? null;
}

/** What a key press means while a class is held. `null` means "not ours" --
 *  the component must not swallow keys it does not handle. */
export function interpretKey(
  key: string,
): { kind: "move"; direction: Direction } | { kind: "drop" | "cancel" } | null {
  switch (key) {
    case "ArrowUp":
      return { kind: "move", direction: "up" };
    case "ArrowDown":
      return { kind: "move", direction: "down" };
    case "ArrowLeft":
      return { kind: "move", direction: "left" };
    case "ArrowRight":
      return { kind: "move", direction: "right" };
    case "Enter":
    case " ":
      return { kind: "drop" };
    case "Escape":
      return { kind: "cancel" };
    default:
      return null;
  }
}

/**
 * What a screen reader is told as the cursor moves.
 *
 * Announced through a live region, because the visual highlight is invisible
 * to the person who most needs to know where the cursor is. It says whether
 * the cell can be dropped on, since moving to a cell you cannot use and being
 * told nothing is the same as the cursor not moving.
 */
export function describeCell(
  cell: GridCell,
  label: string,
  isAllowed: boolean,
): string {
  return `${cell.day} ${label}. ${
    isAllowed ? "Disponible. Pulsa Enter para mover aquí." : "No disponible."
  }`;
}

/**
 * The whole keyboard interaction, as a function.
 *
 * The component that renders the grid decides nothing else (ADR-0012), and
 * this is the piece where it would have been tempting to make an exception:
 * "which events does this keypress dispatch" is real logic, and left inside a
 * component it is testable only through a DOM. Pulling it out here means the
 * one thing that must not break -- that a keypress on a cell the server
 * disallows dispatches **nothing** -- is asserted directly.
 */
export interface KeyboardContext {
  sessionId: string;
  displayName: string;
  cells: GridCell[];
  /** `null` when nothing is being held by keyboard. */
  cursor: GridCell | null;
  isLocked: boolean;
  /** Where the session sits now, so the cursor starts there. */
  ownSlotId: string | null;
  /** The server-computed domain. Never a rule worked out on this side. */
  allowedSlotIds: readonly string[];
  labelOf: (slotId: string) => string;
}

export interface KeyboardOutcome {
  /** Whether the grid consumed the key. `false` leaves it to the browser --
   *  Tab above all must keep working. */
  handled: boolean;
  /** Events to dispatch, in order. The *same* events the pointer path sends,
   *  so the state machine never learned there are two input devices. */
  events: KeyboardEmission[];
  /** The cursor after this key. `null` means nothing is held. */
  cursor: GridCell | null;
  /** What to put in the live region, or `null` to leave it alone. */
  announcement: string | null;
}

export type KeyboardEmission =
  | { type: "GRAB"; sessionId: string }
  | { type: "DROP"; startSlotId: string; roomId: null }
  | { type: "RELEASE_OUTSIDE" };

const IGNORED: KeyboardOutcome = {
  handled: false,
  events: [],
  cursor: null,
  announcement: null,
};

export function keyboardMove(key: string, ctx: KeyboardContext): KeyboardOutcome {
  const action = interpretKey(key);
  if (action === null) return { ...IGNORED, cursor: ctx.cursor };

  const allowed = (slotId: string) => ctx.allowedSlotIds.includes(slotId);

  // Not holding anything yet: Enter picks the class up, and nothing else in
  // this function applies.
  if (ctx.cursor === null) {
    if (action.kind !== "drop") return { ...IGNORED, cursor: null };
    if (ctx.isLocked) {
      return {
        handled: true,
        events: [],
        cursor: null,
        // A locked class that simply does nothing when you press Enter is
        // indistinguishable from a broken page.
        announcement: `${ctx.displayName} está bloqueada y no se puede mover.`,
      };
    }

    const own = ctx.cells.find((c) => c.slotId === ctx.ownSlotId);
    // Start where the class actually is when that is still legal, so the
    // first arrow moves from somewhere the user recognises.
    const start =
      own && allowed(own.slotId) ? own : firstAllowedCell(ctx.cells, ctx.allowedSlotIds);

    if (!start) {
      return {
        handled: true,
        events: [],
        cursor: null,
        announcement: `${ctx.displayName} no tiene ningún horario disponible.`,
      };
    }
    return {
      handled: true,
      events: [{ type: "GRAB", sessionId: ctx.sessionId }],
      cursor: start,
      announcement:
        `${ctx.displayName} seleccionada. Flechas para moverla, Enter para ` +
        `soltarla, Escape para cancelar. ` +
        describeCell(start, ctx.labelOf(start.slotId), allowed(start.slotId)),
    };
  }

  if (action.kind === "cancel") {
    return {
      handled: true,
      events: [{ type: "RELEASE_OUTSIDE" }],
      cursor: null,
      announcement: "Movimiento cancelado. La sesión sigue en su horario.",
    };
  }

  if (action.kind === "move") {
    const next = nextCell(ctx.cells, ctx.cursor, action.direction);
    if (!next) {
      // Saying "edge" beats silence, which reads as a dropped keypress.
      return {
        handled: true,
        events: [],
        cursor: ctx.cursor,
        announcement: "Borde de la rejilla.",
      };
    }
    return {
      handled: true,
      events: [],
      cursor: next,
      announcement: describeCell(next, ctx.labelOf(next.slotId), allowed(next.slotId)),
    };
  }

  // Drop. The guard is the server's domain, and a refusal emits no event at
  // all: the reducer must never be handed a move the server did not allow.
  if (!allowed(ctx.cursor.slotId) || ctx.isLocked) {
    return {
      handled: true,
      events: [],
      cursor: ctx.cursor,
      announcement:
        `No se puede mover a ${ctx.cursor.day} ` +
        `${ctx.labelOf(ctx.cursor.slotId)}: no está en el dominio calculado ` +
        "por el servidor.",
    };
  }
  return {
    handled: true,
    events: [{ type: "DROP", startSlotId: ctx.cursor.slotId, roomId: null }],
    cursor: null,
    announcement:
      `Movida a ${ctx.cursor.day} ${ctx.labelOf(ctx.cursor.slotId)}. Propuesta, ` +
      "sin guardar: revisa el impacto antes de solicitar el cambio.",
  };
}
