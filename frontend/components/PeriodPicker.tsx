"use client";

/**
 * Choosing which period's timetable to look at.
 *
 * The timetable always belongs to a period — that is a real constraint, not a
 * missing default — but until now the only way to name one was to paste its
 * UUID into the address bar. The navigation link led to a screen asking for
 * an id the interface never displayed anywhere, which is a dead end wearing
 * an empty state's clothes.
 */

import { useEffect, useState } from "react";

import {
  fetchPeriods,
  type AcademicPeriodSummary,
} from "../lib/api/client";
import { EmptyState, ErrorState, LoadingState } from "./States";

export function PeriodPicker({
  onChoose,
}: {
  onChoose: (period: AcademicPeriodSummary) => void;
}) {
  const [periods, setPeriods] = useState<AcademicPeriodSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPeriods()
      .then((found) => {
        if (cancelled) return;
        setPeriods(found);
        // Exactly one is not a choice. Selecting it saves a click that only
        // ever has one answer — and the caller writes it into the URL, so the
        // address bar still says which period is on screen.
        if (found.length === 1) onChoose(found[0]);
      })
      .catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      });
    return () => {
      cancelled = true;
    };
  }, [onChoose]);

  if (error) {
    return (
      <ErrorState title="No se pudieron cargar los periodos" detail={error} />
    );
  }
  if (periods === null) {
    return <LoadingState label="Cargando los periodos académicos…" />;
  }
  if (periods.length === 0) {
    return (
      <EmptyState
        title="Todavía no hay periodos académicos"
        detail="Un horario pertenece siempre a un periodo. Crea uno antes de planificar."
      />
    );
  }

  return (
    <div className="panel">
      <div className="panel__header">
        <h2 className="panel__title">Elige un periodo</h2>
      </div>
      <div className="panel__body">
        <ul className="plain-list">
          {periods.map((period) => (
            <li key={period.id}>
              <button
                type="button"
                className="button button--block"
                onClick={() => onChoose(period)}
              >
                <span>
                  <strong>{period.name}</strong>
                  <br />
                  <span className="session__meta">
                    {period.code}
                    {period.startDate && ` · desde ${period.startDate}`}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
