/**
 * SESSION 19: moving a class with the keyboard.
 *
 * The timetable's drag was mouse-only, which is not a rough edge -- a
 * coordinator who cannot use a mouse could not perform the one action the
 * page exists for. These cover the movement rules, which are pure so they can
 * be tested without a browser (the only way anybody would test them).
 */

import { describe, expect, it } from "vitest";

import {
  daysOf,
  describeCell,
  firstAllowedCell,
  interpretKey,
  keyboardMove,
  nextCell,
  rowsOf,
  type GridCell,
  type KeyboardContext,
} from "../keyboard";

const CELLS: GridCell[] = [
  { slotId: "mon0", day: "MON", inDayIndex: 0 },
  { slotId: "mon1", day: "MON", inDayIndex: 1 },
  { slotId: "mon2", day: "MON", inDayIndex: 2 },
  { slotId: "tue0", day: "TUE", inDayIndex: 0 },
  // No TUE row 1 -- a hole, the shape a period with a blocked hour has.
  { slotId: "tue2", day: "TUE", inDayIndex: 2 },
];

const at = (slotId: string) => CELLS.find((c) => c.slotId === slotId)!;

describe("grid geometry", () => {
  it("keeps days in the order the grid renders them", () => {
    expect(daysOf(CELLS)).toEqual(["MON", "TUE"]);
  });

  it("orders rows numerically", () => {
    expect(rowsOf(CELLS)).toEqual([0, 1, 2]);
  });
});

describe("arrow movement", () => {
  it("moves down a day", () => {
    expect(nextCell(CELLS, at("mon0"), "down")?.slotId).toBe("mon1");
  });

  it("moves across days", () => {
    expect(nextCell(CELLS, at("mon0"), "right")?.slotId).toBe("tue0");
  });

  it("stops at the edge rather than wrapping", () => {
    // Wrapping from Friday evening to Monday morning saves one keypress and
    // costs the user their sense of where they are.
    expect(nextCell(CELLS, at("mon0"), "up")).toBeNull();
    expect(nextCell(CELLS, at("tue0"), "right")).toBeNull();
  });

  it("skips over a hole rather than stopping at it", () => {
    // A gap in the timetable is not a wall.
    expect(nextCell(CELLS, at("tue0"), "down")?.slotId).toBe("tue2");
  });

  it("returns null when the hole reaches the edge", () => {
    expect(nextCell(CELLS, at("tue2"), "down")).toBeNull();
  });

  it("is reversible", () => {
    const down = nextCell(CELLS, at("mon0"), "down")!;
    expect(nextCell(CELLS, down, "up")?.slotId).toBe("mon0");
  });

  it("returns null for a cell that is not in the grid", () => {
    expect(
      nextCell(CELLS, { slotId: "x", day: "FRI", inDayIndex: 9 }, "down"),
    ).toBeNull();
  });
});

describe("starting position", () => {
  it("starts on the first cell the session may actually use", () => {
    // Rather than the top-left of the week, which is usually somewhere the
    // class cannot go.
    expect(firstAllowedCell(CELLS, ["tue0", "tue2"])?.slotId).toBe("tue0");
  });

  it("returns null when nothing is allowed", () => {
    expect(firstAllowedCell(CELLS, [])).toBeNull();
  });
});

describe("key interpretation", () => {
  it.each([
    ["ArrowUp", "up"],
    ["ArrowDown", "down"],
    ["ArrowLeft", "left"],
    ["ArrowRight", "right"],
  ] as const)("%s moves %s", (key, direction) => {
    expect(interpretKey(key)).toEqual({ kind: "move", direction });
  });

  it.each(["Enter", " "])("%s drops", (key) => {
    expect(interpretKey(key)).toEqual({ kind: "drop" });
  });

  it("Escape cancels", () => {
    expect(interpretKey("Escape")).toEqual({ kind: "cancel" });
  });

  it("returns null for a key it does not handle", () => {
    // The component must not swallow keys it has no use for -- Tab and the
    // browser's own shortcuts still have to work.
    expect(interpretKey("Tab")).toBeNull();
    expect(interpretKey("a")).toBeNull();
  });
});

describe("what a screen reader is told", () => {
  it("says where the cursor is and whether it can be used", () => {
    // The visual highlight is invisible to the person who most needs to know
    // where the cursor is.
    const message = describeCell(at("mon1"), "09:00", true);
    expect(message).toContain("MON");
    expect(message).toContain("09:00");
    expect(message).toContain("Disponible");
  });

  it("says so when the cell cannot be dropped on", () => {
    // Moving to a cell you cannot use and being told nothing is the same as
    // the cursor not moving.
    expect(describeCell(at("mon1"), "09:00", false)).toContain("No disponible");
  });
});

describe("the keyboard interaction", () => {
  const context = (over: Partial<KeyboardContext> = {}): KeyboardContext => ({
    sessionId: "s1",
    displayName: "MAT101",
    cells: CELLS,
    cursor: null,
    isLocked: false,
    ownSlotId: "mon0",
    allowedSlotIds: ["mon0", "mon1", "tue0"],
    labelOf: (slotId) => `label:${slotId}`,
    ...over,
  });

  it("leaves keys it does not handle to the browser", () => {
    // Tab above all: swallowing it would trap focus in the grid.
    expect(keyboardMove("Tab", context()).handled).toBe(false);
    expect(keyboardMove("ArrowDown", context()).handled).toBe(false);
  });

  it("Enter picks the class up and starts on its own slot", () => {
    const outcome = keyboardMove("Enter", context());
    expect(outcome.events).toEqual([{ type: "GRAB", sessionId: "s1" }]);
    expect(outcome.cursor?.slotId).toBe("mon0");
    expect(outcome.announcement).toContain("MAT101");
  });

  it("starts on the first legal slot when its own is no longer legal", () => {
    const outcome = keyboardMove(
      "Enter",
      context({ ownSlotId: "mon2", allowedSlotIds: ["tue0"] }),
    );
    expect(outcome.cursor?.slotId).toBe("tue0");
  });

  it("refuses to pick up a locked class, out loud", () => {
    // Silence is indistinguishable from a broken page.
    const outcome = keyboardMove("Enter", context({ isLocked: true }));
    expect(outcome.events).toEqual([]);
    expect(outcome.announcement).toContain("bloqueada");
  });

  it("says so when a class has nowhere it may go", () => {
    const outcome = keyboardMove("Enter", context({ allowedSlotIds: [] }));
    expect(outcome.events).toEqual([]);
    expect(outcome.announcement).toContain("no tiene ningún horario");
  });

  it("arrows move the cursor and dispatch nothing", () => {
    // A drag in progress writes nothing. Same rule as the pointer path
    // (ADR-0012), and the keyboard path must not have found a way round it.
    const outcome = keyboardMove("ArrowDown", context({ cursor: at("mon0") }));
    expect(outcome.events).toEqual([]);
    expect(outcome.cursor?.slotId).toBe("mon1");
  });

  it("announces whether the cell it moved to can be used", () => {
    // Moving to a cell you cannot use and being told nothing is the same as
    // the cursor not moving.
    expect(
      keyboardMove("ArrowDown", context({ cursor: at("mon1") })).announcement,
    ).toContain("No disponible");
  });

  it("keeps the cursor at the edge", () => {
    const outcome = keyboardMove("ArrowUp", context({ cursor: at("mon0") }));
    expect(outcome.cursor?.slotId).toBe("mon0");
    expect(outcome.announcement).toContain("Borde");
  });

  it("Enter on an allowed cell proposes the move", () => {
    const outcome = keyboardMove("Enter", context({ cursor: at("mon1") }));
    expect(outcome.events).toEqual([
      { type: "DROP", startSlotId: "mon1", roomId: null },
    ]);
    expect(outcome.cursor).toBeNull();
    // It says the move is a proposal, because it is: nothing is saved until
    // a change request is approved.
    expect(outcome.announcement).toContain("sin guardar");
  });

  it("dispatches NOTHING when dropped on a cell the server disallows", () => {
    // The one rule this module exists to hold. The reducer must never be
    // handed a move outside the server-computed domain -- a second
    // feasibility implementation on this side would drift from CP-SAT, and
    // the drift would appear as a placement the UI allowed and the solver
    // rejected.
    const outcome = keyboardMove("Enter", context({ cursor: at("mon2") }));
    expect(outcome.events).toEqual([]);
    expect(outcome.cursor?.slotId).toBe("mon2");
    expect(outcome.announcement).toContain("dominio calculado");
  });

  it("Escape puts the class back", () => {
    const outcome = keyboardMove("Escape", context({ cursor: at("mon1") }));
    expect(outcome.events).toEqual([{ type: "RELEASE_OUTSIDE" }]);
    expect(outcome.cursor).toBeNull();
  });

  it("emits only events the state machine already knows", () => {
    // The keyboard path added no vocabulary: `machine.ts` is unchanged, so
    // ADR-0012's CRITICAL-rule tests still cover both input devices.
    const known = new Set(["GRAB", "DROP", "RELEASE_OUTSIDE"]);
    const outcomes = [
      keyboardMove("Enter", context()),
      keyboardMove("Enter", context({ cursor: at("mon1") })),
      keyboardMove("Escape", context({ cursor: at("mon1") })),
    ];
    for (const outcome of outcomes) {
      for (const event of outcome.events) expect(known.has(event.type)).toBe(true);
    }
  });
});
