/**
 * SESSION 32: a class that runs two hours occupies two hours.
 *
 * The grid painted a session only in the slot it starts in, so an hour a
 * class runs through looked free. In the imported period that was 295 of 399
 * sessions. An hour that looks free and is not is worse than no grid at all.
 *
 * The rule is extracted here because it is arithmetic over the week, and the
 * two mistakes it invites — running past the end of a day, and counting a
 * continuation as another class — are both invisible in a screenshot.
 */

import { describe, expect, it } from "vitest";

interface Cell {
  slotId: string;
  day: string;
  inDayIndex: number;
}

/** The slots a session covers after the one it starts in. */
export function continuationSlots(
  slots: Cell[],
  days: string[],
  startSlotId: string,
  duration: number,
): string[] {
  const ordered = [...slots].sort(
    (a, b) =>
      days.indexOf(a.day) - days.indexOf(b.day) || a.inDayIndex - b.inDayIndex,
  );
  const start = ordered.findIndex((s) => s.slotId === startSlotId);
  if (start < 0 || duration <= 1) return [];
  const covered: string[] = [];
  for (let step = 1; step < duration; step++) {
    const next = ordered[start + step];
    if (!next || next.day !== ordered[start].day) break;
    covered.push(next.slotId);
  }
  return covered;
}

const WEEK: Cell[] = [
  { slotId: "mon0", day: "MONDAY", inDayIndex: 0 },
  { slotId: "mon1", day: "MONDAY", inDayIndex: 1 },
  { slotId: "mon2", day: "MONDAY", inDayIndex: 2 },
  { slotId: "tue0", day: "TUESDAY", inDayIndex: 0 },
  { slotId: "tue1", day: "TUESDAY", inDayIndex: 1 },
];
const DAYS = ["MONDAY", "TUESDAY"];

describe("how far a class reaches", () => {
  it("a one-hour class covers nothing extra", () => {
    expect(continuationSlots(WEEK, DAYS, "mon0", 1)).toEqual([]);
  });

  it("a two-hour class covers the hour after it", () => {
    expect(continuationSlots(WEEK, DAYS, "mon0", 2)).toEqual(["mon1"]);
  });

  it("a three-hour class covers two", () => {
    expect(continuationSlots(WEEK, DAYS, "mon0", 3)).toEqual(["mon1", "mon2"]);
  });

  it("never runs past the end of its own day", () => {
    // The mistake the ordering invites: `mon2` + 1 is `tue0`, and a class
    // that appears to continue into Tuesday morning is a class nobody can
    // find.
    expect(continuationSlots(WEEK, DAYS, "mon2", 2)).toEqual([]);
    expect(continuationSlots(WEEK, DAYS, "mon1", 3)).toEqual(["mon2"]);
  });

  it("stops at the end of the grid", () => {
    expect(continuationSlots(WEEK, DAYS, "tue1", 3)).toEqual([]);
  });

  it("returns nothing for a slot that is not in the grid", () => {
    expect(continuationSlots(WEEK, DAYS, "fri9", 2)).toEqual([]);
  });

  it("a continuation is never the starting slot itself", () => {
    // Counting it would draw the class twice in its own first hour, and
    // double every long session for anybody counting blocks.
    for (const duration of [2, 3, 4]) {
      expect(continuationSlots(WEEK, DAYS, "mon0", duration)).not.toContain(
        "mon0",
      );
    }
  });
});
