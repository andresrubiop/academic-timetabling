"use client";

/**
 * One dashboard panel.
 *
 * Renders whatever the server sent and interprets nothing. The severity, the
 * value and the rows are all decided by `domain/dashboards/widgets.py`; a
 * threshold re-implemented here would drift from the one the platform
 * actually acts on, and the drift would show as a card saying everything is
 * fine while a rule elsewhere says it is not.
 */

import type { DashboardMetric } from "../lib/api/client";
import {
  badgeClass,
  cellText,
  columnsOf,
  formatValue,
  severityLabel,
} from "../lib/dashboard/present";

export function MetricCard({ metric }: { metric: DashboardMetric }) {
  return (
    <section className="panel" aria-labelledby={`w-${metric.widgetCode}`}>
      <div className="panel__header">
        <h2 className="panel__title" id={`w-${metric.widgetCode}`}>
          {metric.title}
        </h2>
        {/* The word, not only the colour. */}
        <span className={badgeClass(metric.severity)}>
          {severityLabel(metric.severity)}
        </span>
      </div>
      <div className="panel__body">
        {metric.kind === "LIST" || metric.kind === "TIER_BREAKDOWN" ? (
          <MetricRows metric={metric} />
        ) : (
          <p className="metric__value">
            {formatValue(metric)}
            {metric.unit && !metric.isUnavailable && (
              <span className="metric__unit">{metric.unit}</span>
            )}
          </p>
        )}
        {/* The detail is where a count becomes actionable: 44 sessions is a
            number, "44 sessions with nobody committed to teach them" is a
            task. */}
        {metric.detail && <p className="metric__detail">{metric.detail}</p>}
      </div>
    </section>
  );
}

function MetricRows({ metric }: { metric: DashboardMetric }) {
  if (metric.isUnavailable) {
    return <p className="metric__detail">No disponible.</p>;
  }
  if (metric.rows.length === 0) {
    // Not an empty table with headers over nothing: the absence is the
    // finding, and it should read as one.
    return <p className="metric__detail">Nada que mostrar.</p>;
  }

  const columns = columnsOf(metric.rows);
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column.replace(/_/g, " ")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {metric.rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{cellText(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
