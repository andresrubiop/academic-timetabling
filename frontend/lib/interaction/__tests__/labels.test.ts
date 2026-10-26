/**
 * SESSION 36: how a timetable block reads.
 *
 * Every one of these is wrong in a way that looks fine in a screenshot: a day
 * named in the wrong language, an end time computed from an assumed slot
 * length, a week that starts on Saturday because that is the order the slots
 * arrived in.
 */

import { describe, expect, it } from "vitest";

import {
  dayName,
  orderDays,
  programHue,
  segmentPosition,
  shortDayName,
  slotAtOffset,
  timeRange,
} from "../labels";

describe("day names", () => {
  it("translates the week", () => {
    // The grid printed the domain enum's own value in an interface that is
    // otherwise entirely in Spanish.
    expect(dayName("MONDAY")).toBe("Lunes");
    expect(dayName("WEDNESDAY")).toBe("Miércoles");
  });

  it("leaves an unknown value alone", () => {
    // Still more useful printed than hidden behind a placeholder.
    expect(dayName("FERIADO")).toBe("FERIADO");
  });

  it("abbreviates for a narrow column", () => {
    expect(shortDayName("MONDAY")).toBe("Lun");
    expect(shortDayName("SATURDAY")).toBe("Sáb");
  });
});

describe("column order", () => {
  it("puts the week in week order whatever order it arrived in", () => {
    expect(orderDays(["FRIDAY", "MONDAY", "WEDNESDAY"])).toEqual([
      "MONDAY",
      "WEDNESDAY",
      "FRIDAY",
    ]);
  });

  it("keeps an unknown day last rather than first", () => {
    // The bug an `indexOf` of -1 invites: an unrecognised day sorts before
    // Monday and the week starts with it.
    expect(orderDays(["FERIADO", "MONDAY"])).toEqual(["MONDAY", "FERIADO"]);
  });

  it("does not mutate its input", () => {
    const input = ["FRIDAY", "MONDAY"];
    orderDays(input);
    expect(input).toEqual(["FRIDAY", "MONDAY"]);
  });
});

describe("the time a class runs", () => {
  it("shows only the start for a one-slot class", () => {
    expect(timeRange("07:00", [], 1)).toBe("07:00");
  });

  it("shows the range for a longer one", () => {
    // `2h` told a reader the length and not the end, and on a grid where a
    // class occupies rows it does not label, when it finishes is the thing
    // being looked for.
    expect(timeRange("07:00", ["08:00"], 2)).toBe("07:00–09:00");
    expect(timeRange("07:00", ["08:00", "09:00"], 3)).toBe("07:00–10:00");
  });

  it("falls back to the start when the grid does not reach", () => {
    // Rather than inventing an end: a class whose continuation runs off the
    // end of the day has no end this function can know.
    expect(timeRange("21:00", [], 3)).toBe("21:00");
  });

  it("does not invent an end from an unparseable label", () => {
    expect(timeRange("07:00", ["mañana"], 2)).toBe("07:00");
  });
});

describe("programme colour", () => {
  it("is stable for the same programme", () => {
    // Survives a reload and a re-import, so two people looking at the same
    // timetable see the same colours.
    expect(programHue("prog-a")).toBe(programHue("prog-a"));
  });

  it("differs between programmes", () => {
    expect(programHue("prog-a")).not.toBe(programHue("prog-b"));
  });

  it("is nothing for a session with no programme", () => {
    expect(programHue(undefined)).toBeNull();
    expect(programHue("")).toBeNull();
  });

  it("spreads ten programmes apart", () => {
    // The point of stepping by the golden angle rather than a round number:
    // with a round step, a handful of programmes land on near-identical
    // colours and the stripe stops distinguishing anything.
    const hues = Array.from({ length: 10 }, (_, i) => programHue(`programa-${i}`)!);
    const sorted = [...hues].sort((a, b) => a - b);
    const gaps = sorted.slice(1).map((h, i) => h - sorted[i]);
    expect(Math.min(...gaps)).toBeGreaterThan(5);
  });
});

describe("picking up an hour in the middle of a long class", () => {
  const WEEK = [
    { slotId: "mon0", day: "MONDAY", inDayIndex: 0 },
    { slotId: "mon1", day: "MONDAY", inDayIndex: 1 },
    { slotId: "mon2", day: "MONDAY", inDayIndex: 2 },
    { slotId: "tue0", day: "TUESDAY", inDayIndex: 0 },
  ];

  it("moves back by the offset that was grabbed", () => {
    // Grab the second hour of a three-hour class, drop it on mon2, and the
    // class starts at mon1. Without this, grabbing the middle of a long class
    // silently shifts it forward by an hour — the kind of error somebody
    // discovers a week later.
    expect(slotAtOffset(WEEK, "mon2", -1)).toBe("mon1");
    expect(slotAtOffset(WEEK, "mon2", -2)).toBe("mon0");
  });

  it("returns the same slot for the first hour", () => {
    expect(slotAtOffset(WEEK, "mon1", 0)).toBe("mon1");
  });

  it("refuses to start a class before the day begins", () => {
    // No sensible answer exists, so it gives none rather than wrapping into
    // the previous day.
    expect(slotAtOffset(WEEK, "mon0", -1)).toBeNull();
  });

  it("never crosses into another day", () => {
    expect(slotAtOffset(WEEK, "tue0", -1)).toBeNull();
  });

  it("returns null for a slot outside the grid", () => {
    expect(slotAtOffset(WEEK, "fri9", -1)).toBeNull();
  });
});

describe("where a segment sits in its class", () => {
  it("a one-hour class is a single piece", () => {
    expect(segmentPosition(0, 1)).toBe("only");
  });

  it("a longer one has a first, a middle and a last", () => {
    expect(segmentPosition(0, 3)).toBe("first");
    expect(segmentPosition(1, 3)).toBe("middle");
    expect(segmentPosition(2, 3)).toBe("last");
  });

  it("a two-hour class has no middle", () => {
    expect(segmentPosition(0, 2)).toBe("first");
    expect(segmentPosition(1, 2)).toBe("last");
  });
});
