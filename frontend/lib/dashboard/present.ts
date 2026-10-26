/**
 * Turning dashboard metrics into something a person can read at a glance.
 *
 * Pure, and in its own module, because these are the decisions worth being
 * sure about: what order the panels come in, what a missing value looks like,
 * and which colour a severity earns. Each of them is wrong in a way that is
 * invisible in a screenshot and obvious to somebody relying on the screen.
 */

import type { DashboardMetric, MetricSeverity } from "../api/client";

/** Severity ranks, worst first. */
const SEVERITY_RANK: Record<MetricSeverity, number> = {
  PROBLEM: 0,
  ATTENTION: 1,
  OK: 2,
};

/**
 * Panels in the order somebody should read them.
 *
 * Problems first. A dashboard sorted by widget code puts the one thing that
 * needs doing today underneath eleven panels that are fine, which is how a
 * dashboard becomes something people stop scrolling through.
 *
 * Ties keep the server's order rather than falling back to the title: the
 * server groups related widgets, and re-sorting alphabetically inside a
 * severity would break that grouping for no gain. `Array.prototype.sort` is
 * stable, so this is just what it already does -- stated because relying on
 * it silently is how it gets "tidied up" later.
 */
export function orderMetrics(metrics: DashboardMetric[]): DashboardMetric[] {
  return [...metrics].sort(
    (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity],
  );
}

/** The badge class for a severity. */
export function badgeClass(severity: MetricSeverity): string {
  switch (severity) {
    case "PROBLEM":
      return "badge badge--danger";
    case "ATTENTION":
      return "badge badge--warning";
    default:
      return "badge badge--ok";
  }
}

/** What the badge says. Colour is never the only carrier: a red dot and an
 *  amber dot are the same dot to a colour-blind reader. */
export function severityLabel(severity: MetricSeverity): string {
  switch (severity) {
    case "PROBLEM":
      return "Problema";
    case "ATTENTION":
      return "Atención";
    default:
      return "Correcto";
  }
}

/**
 * The number, formatted, or a statement that there is not one.
 *
 * An unavailable metric never renders as `0`. A zero is an answer -- "nobody
 * is unassigned" -- and "we could not compute it" is a different answer that
 * happens to look identical once it reaches a card.
 */
export function formatValue(metric: DashboardMetric): string {
  if (metric.isUnavailable) return "No disponible";
  if (metric.value === null) return "—";
  return metric.value.toLocaleString("es");
}

/**
 * Column keys for a LIST widget, in first-seen order across every row.
 *
 * Taken from all rows rather than the first, because a row that carries an
 * extra key would otherwise have that column silently dropped -- and the
 * extra key tends to be the interesting one (a reason, a count) attached to
 * exactly the rows that have a problem.
 */
export function columnsOf(rows: Record<string, unknown>[]): string[] {
  const out: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!out.includes(key)) out.push(key);
    }
  }
  return out;
}

/** A cell value as text. `null`/`undefined` become an em dash rather than the
 *  string "null", which is a leak of the wire format into somebody's screen. */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Widgets whose content is names rather than a number get the wider card. */
export function isWide(metric: DashboardMetric): boolean {
  return metric.kind === "LIST" || metric.kind === "TIER_BREAKDOWN";
}
