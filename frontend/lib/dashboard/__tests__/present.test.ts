/**
 * SESSION 19: how a dashboard reads.
 *
 * SESSION 16 built the widgets; nothing rendered them. These cover the
 * presentation decisions that are invisible in a screenshot and obvious to
 * whoever depends on the screen.
 */

import { describe, expect, it } from "vitest";

import type { DashboardMetric } from "../../api/client";
import {
  badgeClass,
  cellText,
  columnsOf,
  formatValue,
  isWide,
  orderMetrics,
  severityLabel,
} from "../present";

function metric(over: Partial<DashboardMetric> = {}): DashboardMetric {
  return {
    widgetCode: "W",
    title: "Widget",
    kind: "COUNT",
    value: 0,
    unit: null,
    severity: "OK",
    rows: [],
    detail: null,
    isUnavailable: false,
    ...over,
  };
}

describe("panel order", () => {
  it("puts problems first and healthy panels last", () => {
    // A dashboard sorted by widget code buries the one thing that needs doing
    // today under eleven panels that are fine.
    const ordered = orderMetrics([
      metric({ widgetCode: "ok", severity: "OK" }),
      metric({ widgetCode: "problem", severity: "PROBLEM" }),
      metric({ widgetCode: "attention", severity: "ATTENTION" }),
    ]);
    expect(ordered.map((m) => m.widgetCode)).toEqual([
      "problem",
      "attention",
      "ok",
    ]);
  });

  it("keeps the server's order within a severity", () => {
    // The server groups related widgets; re-sorting alphabetically inside a
    // severity would break the grouping for no gain.
    const ordered = orderMetrics([
      metric({ widgetCode: "zulu", severity: "OK" }),
      metric({ widgetCode: "alpha", severity: "OK" }),
    ]);
    expect(ordered.map((m) => m.widgetCode)).toEqual(["zulu", "alpha"]);
  });

  it("does not mutate what it was given", () => {
    const input = [
      metric({ widgetCode: "ok", severity: "OK" }),
      metric({ widgetCode: "bad", severity: "PROBLEM" }),
    ];
    orderMetrics(input);
    expect(input.map((m) => m.widgetCode)).toEqual(["ok", "bad"]);
  });
});

describe("severity", () => {
  it("maps each severity to its own badge", () => {
    expect(badgeClass("PROBLEM")).toContain("danger");
    expect(badgeClass("ATTENTION")).toContain("warning");
    expect(badgeClass("OK")).toContain("ok");
  });

  it("carries a word as well as a colour", () => {
    // A red dot and an amber dot are the same dot to a colour-blind reader.
    expect(severityLabel("PROBLEM")).toBe("Problema");
    expect(severityLabel("ATTENTION")).toBe("Atención");
    expect(severityLabel("OK")).toBe("Correcto");
  });
});

describe("values", () => {
  it("never renders an uncomputed metric as zero", () => {
    // "Nobody is unassigned" and "we could not tell you" must not look
    // identical once they reach a card.
    expect(formatValue(metric({ isUnavailable: true, value: 0 }))).toBe(
      "No disponible",
    );
  });

  it("renders a real zero as zero", () => {
    expect(formatValue(metric({ value: 0 }))).toBe("0");
  });

  it("groups large numbers", () => {
    expect(formatValue(metric({ value: 12345 }))).not.toBe("12345");
  });

  it("renders a missing value as a dash, not as null", () => {
    expect(formatValue(metric({ value: null }))).toBe("—");
  });
});

describe("list rows", () => {
  it("takes columns from every row, not just the first", () => {
    // The extra key tends to be the interesting one, attached to exactly the
    // rows that have a problem.
    expect(
      columnsOf([{ name: "A" }, { name: "B", reason: "sin docente" }]),
    ).toEqual(["name", "reason"]);
  });

  it("keeps first-seen order", () => {
    expect(columnsOf([{ b: 1, a: 2 }])).toEqual(["b", "a"]);
  });

  it("handles no rows", () => {
    expect(columnsOf([])).toEqual([]);
  });

  it("renders an absent cell as a dash rather than the word null", () => {
    expect(cellText(null)).toBe("—");
    expect(cellText(undefined)).toBe("—");
    expect(cellText(0)).toBe("0");
    expect(cellText(false)).toBe("false");
  });
});

describe("card width", () => {
  it("gives name-bearing widgets the wider card", () => {
    // A name wrapped over three lines is not a name.
    expect(isWide(metric({ kind: "LIST" }))).toBe(true);
    expect(isWide(metric({ kind: "TIER_BREAKDOWN" }))).toBe(true);
    expect(isWide(metric({ kind: "COUNT" }))).toBe(false);
  });
});
