"use client";

/**
 * Change requests, and the button that applies one.
 *
 * `POST /change-requests/{id}/apply` was written and tested in SESSION 34 and
 * nothing in the browser called it, so the last step of the platform's whole
 * flow — the one that actually moves the timetable — was curl. There was also
 * no list endpoint until SESSION 35: the only way to reach a request was to
 * have been handed its id.
 */

import { useCallback, useEffect, useState } from "react";

import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
} from "../../components/States";
import {
  applyChangeRequest,
  decideChangeRequest,
  fetchChangeRequests,
  type ChangeRequestSummary,
} from "../../lib/api/client";

const FILTERS = [
  { value: "", label: "todas" },
  { value: "SUBMITTED", label: "enviadas" },
  { value: "UNDER_REVIEW", label: "en revisión" },
  { value: "APPROVED", label: "aprobadas" },
  { value: "APPLIED", label: "aplicadas" },
  { value: "REJECTED", label: "rechazadas" },
];

function badgeFor(status: string): string {
  switch (status) {
    case "APPLIED":
      return "badge badge--ok";
    case "APPROVED":
      return "badge badge--accent";
    case "REJECTED":
      return "badge badge--danger";
    default:
      return "badge badge--warning";
  }
}

export default function RequestsPage() {
  const [requests, setRequests] = useState<ChangeRequestSummary[] | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [applying, setApplying] = useState<ChangeRequestSummary | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    fetchChangeRequests(filter || undefined)
      .then(setRequests)
      .catch((cause: Error) => setError(cause.message));
  }, [filter]);

  useEffect(() => load(), [load]);

  async function decide(
    request: ChangeRequestSummary,
    action: "APPROVE" | "REJECT",
  ) {
    setBusy(request.requestId);
    setError(null);
    try {
      await decideChangeRequest(
        request.requestId,
        action,
        action === "APPROVE" ? "Aprobado desde el panel" : "Rechazado desde el panel",
      );
      load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo registrar.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <h1 className="page__title">Solicitudes de cambio</h1>
      <p className="page__lede">
        Lo que alguien propuso mover, lo que costaría, y quién ha firmado.
        Aprobar es permiso para aplicar — aplicar publica un horario nuevo y
        deja el anterior como reemplazado.
      </p>

      <div className="toolbar" role="toolbar" aria-label="Filtro de solicitudes">
        <label className="field">
          Estado
          <select
            className="field__control"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            {FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {note && (
        <p className="notice notice--success" role="status">
          {note}
        </p>
      )}
      {error && (
        <p className="state state--error" role="alert">
          {error}
        </p>
      )}

      {requests === null ? (
        <LoadingState label="Cargando las solicitudes…" rows={3} />
      ) : requests.length === 0 ? (
        <EmptyState
          title="No hay solicitudes que mostrar"
          detail="Se crean desde el horario: mueve una clase, revisa el impacto y solicita el cambio."
        />
      ) : (
        <ul className="plain-list">
          {requests.map((request) => (
            <li key={request.requestId} className="panel">
              <div className="panel__header">
                <h2 className="panel__title">
                  {request.code} — {request.title}{" "}
                  <span className={badgeFor(request.status)}>
                    {request.status}
                  </span>
                  {request.isMine && <span className="badge">tuya</span>}
                </h2>
              </div>
              <div className="panel__body">
                {request.summary && <p>{request.summary}</p>}
                <ul className="stat-list">
                  <li>
                    <div className="stat-list__value">
                      {request.affectedStudents}
                    </div>
                    <div className="stat-list__label">estudiantes</div>
                  </li>
                  <li>
                    <div className="stat-list__value">{request.newConflicts}</div>
                    <div className="stat-list__label">conflictos nuevos</div>
                  </li>
                  <li>
                    <div className="stat-list__value">{request.scope || "—"}</div>
                    <div className="stat-list__label">alcance</div>
                  </li>
                </ul>

                <div className="side-panel__actions">
                  {["SUBMITTED", "UNDER_REVIEW"].includes(request.status) && (
                    <>
                      <button
                        type="button"
                        className="button"
                        disabled={busy === request.requestId}
                        onClick={() => decide(request, "APPROVE")}
                      >
                        Aprobar
                      </button>
                      <button
                        type="button"
                        className="button button--danger"
                        disabled={busy === request.requestId}
                        onClick={() => decide(request, "REJECT")}
                      >
                        Rechazar
                      </button>
                    </>
                  )}
                  {request.status === "APPROVED" && (
                    <button
                      type="button"
                      className="button button--primary"
                      onClick={() => setApplying(request)}
                    >
                      Aplicar al horario
                    </button>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {applying && (
        <ConfirmDialog
          title={`¿Aplicar ${applying.code}?`}
          confirmLabel="Publicar el horario nuevo"
          body={
            <div>
              <p>{applying.title}</p>
              {/* What applying actually is, rather than "are you sure". */}
              <p className="metric__detail">
                Se publica una versión nueva del horario y la actual queda como
                reemplazada. El horario que la gente lee cambia en ese momento,
                y quedará tu nombre en la publicación.
              </p>
            </div>
          }
          onCancel={() => setApplying(null)}
          onConfirm={async () => {
            const target = applying;
            setApplying(null);
            setError(null);
            setNote(null);
            try {
              const result = await applyChangeRequest(target.requestId);
              setNote(
                `${result.code} aplicada: versión ${result.versionNumber} publicada, ` +
                  `${result.movedSessions} sesión(es) movida(s).`,
              );
              load();
            } catch (cause) {
              // A stale approval lands here, and its message is the useful
              // part — it names both version numbers and says to re-analyse.
              setError(
                cause instanceof Error ? cause.message : "No se pudo aplicar.",
              );
            }
          }}
        />
      )}
    </div>
  );
}
