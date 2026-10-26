"use client";

// The side panel. The only thing standing between a slipped hand and a change
// request (ADR-0012), so it shows what a coordinator could not have worked out
// by looking rather than a "looks fine" badge.
//
// In Spanish, like the rest of the interface: the page declares `lang="es"`,
// and a screen reader reading English strings in a Spanish voice is not a
// cosmetic problem.

import type {
  Alternative,
  ImpactReport,
  InteractionStatus,
} from "../lib/interaction/types";
import { LoadingState } from "./States";

interface Props {
  status: InteractionStatus;
  impact: ImpactReport | null;
  error: string | null;
  hiddenAffected: string[];
  alternatives?: Alternative[];
  onChooseAlternative?: (index: number) => void;
  onCancel: () => void;
  onSimulate: () => void;
  onCreateChangeRequest: () => void;
  onDismissError: () => void;
}

function Delta({ value }: { value: number }) {
  // Negative is better, everywhere in this system. Rendering a bare "-12" as
  // bad would invert the meaning, so the direction is spelled out in words
  // and the colour only agrees with them.
  if (value === 0) return <span className="badge">sin cambio</span>;
  const better = value < 0;
  return (
    <span className={`badge ${better ? "badge--ok" : "badge--danger"}`}>
      {better ? "mejor" : "peor"} en {Math.abs(value).toLocaleString("es")}
    </span>
  );
}

export function ImpactPanel({
  status,
  impact,
  error,
  hiddenAffected,
  alternatives = [],
  onChooseAlternative,
  onCancel,
  onSimulate,
  onCreateChangeRequest,
  onDismissError,
}: Props) {
  if (status === "VIEWING" || status === "DRAGGING") {
    return (
      <aside className="side-panel" aria-label="Impacto del cambio">
        {/* The keyboard route is stated, not left to be discovered. An
            affordance nobody is told about is an affordance nobody has. */}
        <p className="state">
          Arrastra una clase, o selecciónala con Tab y pulsa Enter, para ver
          qué pasaría si se moviera. Nada cambia hasta que lo pidas.
        </p>
      </aside>
    );
  }

  if (status === "FAILED") {
    return (
      <aside className="side-panel" aria-label="Impacto del cambio">
        <div className="state state--error" role="alert">
          <p className="state__title">No se pudo analizar el movimiento</p>
          <p>{error}</p>
          <p>El horario no ha cambiado.</p>
          <button type="button" className="button" onClick={onDismissError}>
            Descartar
          </button>
        </div>
      </aside>
    );
  }

  if (status === "ANALYSING" || status === "SIMULATING") {
    return (
      <aside className="side-panel" aria-label="Impacto del cambio">
        <h2 className="side-panel__title">
          {status === "ANALYSING"
            ? "Calculando el efecto…"
            : "Buscando alternativas…"}
        </h2>
        <LoadingState
          label={
            status === "ANALYSING"
              ? "Calculando el efecto del movimiento"
              : "Buscando alternativas"
          }
        />
        <button type="button" className="button" onClick={onCancel}>
          Cancelar
        </button>
      </aside>
    );
  }

  if (!impact) return <aside className="side-panel" aria-label="Impacto del cambio" />;

  const { affected } = impact;
  const counts: [number, string][] = [
    [affected.students, "estudiantes"],
    [affected.teachers, "docentes"],
    [affected.programs, "programas"],
    [affected.faculties, "facultades"],
    [affected.rooms, "aulas"],
    [affected.sessions, "sesiones movidas"],
  ];

  return (
    <aside className="side-panel" aria-label="Impacto del cambio">
      <h2 className="side-panel__title">Si haces este cambio</h2>
      <p>{impact.summary}</p>

      {error && (
        <p className="state state--error" role="alert">
          No se pudo enviar: {error}. La propuesta sigue aquí.
        </p>
      )}

      <section className="side-panel__section">
        <h3 className="side-panel__label">A quién afecta</h3>
        <ul className="stat-list">
          {counts.map(([value, label]) => (
            <li key={label}>
              <div className="stat-list__value">{value}</div>
              <div className="stat-list__label">{label}</div>
            </li>
          ))}
        </ul>
      </section>

      {impact.newConflicts.length > 0 && (
        <section className="side-panel__section">
          <h3 className="side-panel__label">
            Conflictos nuevos ({impact.newConflicts.length})
          </h3>
          {/* Named, not counted. "3 conflictos nuevos" says investigate; a
              name says what to do. */}
          <ul className="plain-list">
            {impact.newConflicts.slice(0, 8).map((c, i) => (
              <li key={i}>
                {c.studentId}: {c.sessionA} / {c.sessionB}
                {c.classification.endsWith("UNRESOLVABLE") && (
                  <strong> — no se puede resolver con secciones</strong>
                )}
              </li>
            ))}
            {impact.newConflicts.length > 8 && (
              <li>…y {impact.newConflicts.length - 8} más</li>
            )}
          </ul>
        </section>
      )}

      {impact.resolvedConflicts.length > 0 && (
        <section className="side-panel__section">
          <h3 className="side-panel__label">
            Resueltos ({impact.resolvedConflicts.length})
          </h3>
          <ul className="plain-list">
            {impact.resolvedConflicts.slice(0, 8).map((c, i) => (
              <li key={i}>
                {c.studentId}: {c.sessionA} / {c.sessionB}
              </li>
            ))}
          </ul>
        </section>
      )}

      {impact.occupancyClashes.length > 0 && (
        <p className="state state--error" role="alert">
          Pone {impact.occupancyClashes.join(", ")} en el mismo lugar a la misma
          hora. El optimizador rechazaría esto.
        </p>
      )}

      {hiddenAffected.length > 0 && (
        // A move can break a class the current filter hides. Submitting
        // without knowing that is how a "safe" change breaks another
        // programme.
        <p className="state">
          <span className="badge badge--warning">Fuera de tu vista</span>{" "}
          Afecta a {hiddenAffected.length} sesión(es) que tu filtro oculta:{" "}
          {hiddenAffected.slice(0, 5).join(", ")}.
        </p>
      )}

      <section className="side-panel__section">
        <h3 className="side-panel__label">Objetivo, nivel por nivel</h3>
        {/* Per tier, never one number: a scalar would reintroduce the
            cross-tier exchange rate ADR-0010 rejects. */}
        <table className="table table--numeric">
          <tbody>
            {impact.tierCodes.map((code, i) => (
              <tr key={code}>
                <td>{code}</td>
                <td>
                  <Delta value={impact.scoreDelta[i] ?? 0} />
                </td>
              </tr>
            ))}
            <tr>
              <td>Sesiones movidas respecto a lo publicado</td>
              <td>
                {impact.stabilityDelta > 0 ? "+" : ""}
                {impact.stabilityDelta}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      {alternatives.length > 0 && (
        <section className="side-panel__section">
          <h3 className="side-panel__label">Alternativas, mejor primero</h3>
          {/* Ranked lexicographically by the server. Choosing one swaps the
              proposal; it does not apply anything. */}
          <ul className="plain-list">
            {alternatives.map((alternative, index) => (
              <li key={alternative.label}>
                <button
                  type="button"
                  className="button button--block"
                  onClick={() => onChooseAlternative?.(index)}
                >
                  <span>
                    <strong>{alternative.label}</strong>
                    <br />
                    <span className="session__meta">
                      {alternative.impact.summary}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="side-panel__actions">
        <button type="button" className="button" onClick={onCancel}>
          Cancelar
        </button>
        <button type="button" className="button" onClick={onSimulate}>
          Ver alternativas
        </button>
        <button
          type="button"
          className="button button--primary"
          onClick={onCreateChangeRequest}
          disabled={status === "SUBMITTING"}
        >
          {status === "SUBMITTING" ? "Enviando…" : "Solicitar el cambio"}
        </button>
      </div>
      <p className="metric__detail">
        Una solicitud de cambio necesita aprobación antes de que el horario
        publicado cambie.
      </p>
    </aside>
  );
}
