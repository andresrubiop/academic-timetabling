"use client";

// The interactive timetable (SESSION 10).
//
// This component owns the wiring and nothing else: it holds the reducer,
// performs the fetches, and turns their answers into events. Every decision --
// what may be dropped where, what an action means, whether a move is an
// improvement -- lives in the reducer or on the server, per ADR-0012.

import { useCallback, useEffect, useMemo, useReducer, useState } from "react";

import { ImpactPanel } from "../../components/ImpactPanel";
import { PeriodPicker } from "../../components/PeriodPicker";
import { ConfirmDialog, LoadingState } from "../../components/States";
import { TimetableGrid, type Slot } from "../../components/TimetableGrid";
import { Toolbar } from "../../components/Toolbar";
import {
  analyseImpact,
  createChangeRequest,
  fetchAlternatives,
  fetchAvailability,
  fetchSchedule,
  saveAvailability,
  type ScheduleReferences,
} from "../../lib/api/client";
import {
  NO_FILTERS,
  isHidden,
  visibleSessions,
  type Filters,
  type SessionMeta,
} from "../../lib/interaction/filters";
import { initialState, previewPlacement, reduce } from "../../lib/interaction/machine";

/** A title somebody two steps up the chain reads before anything else. */
function summariseProposal(
  moves: { sessionId: string }[],
  metas: Record<string, SessionMeta>,
): string {
  if (moves.length === 1) {
    const meta = metas[moves[0].sessionId];
    return `Mover ${meta?.courseCode ?? moves[0].sessionId}`;
  }
  return `Mover ${moves.length} sesiones`;
}

/**
 * The label for a session the server sent no metadata for.
 *
 * Until SESSION 22 this was *every* session: the grid named classes by UUID
 * because nothing sent it anything else. It survives only as the fallback for
 * a session the label read did not cover, and it shows the id rather than
 * inventing a plausible course code — a made-up name is worse than an ugly
 * one, because nobody can tell it is missing.
 */
function unlabelled(sessionId: string): SessionMeta {
  return {
    sessionId,
    courseCode: sessionId.slice(0, 8),
    courseName: sessionId,
    sectionCode: "—",
    teacherIds: [],
    teacherNames: [],
    programIds: [],
    facultyIds: [],
    studentIds: [],
    durationSlots: 1,
  };
}

export default function TimetablePage() {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [zoom, setZoom] = useState(1);
  const [periodId, setPeriodId] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [serverMetas, setServerMetas] = useState<Record<string, SessionMeta>>({});
  const [serverSlots, setServerSlots] = useState<Slot[]>([]);
  // Availability of the resource currently being looked at. Only teachers
  // and rooms have one; a programme is not something that is "unavailable".
  const [unavailable, setUnavailable] = useState<Set<string>>(new Set());
  const [isEditing, setEditing] = useState(false);
  const [savingState, setSavingState] = useState<string | null>(null);
  const [references, setReferences] = useState<ScheduleReferences>({
    programs: [],
    faculties: [],
    rooms: [],
  });
  // A change request is visible to everyone in the approval chain the moment
  // it exists. Filing one is not undoable by clicking again, so it gets a
  // confirmation -- and it is the only action on this page that does.
  const [isConfirming, setConfirming] = useState(false);

  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("period");
    if (fromUrl) setPeriodId(fromUrl);
  }, []);

  // Chosen from the picker rather than typed into the address bar. The URL is
  // updated too, so what is on screen and what the address bar says agree —
  // and so the link can be sent to somebody.
  const choosePeriod = useCallback((period: { id: string; name: string }) => {
    setPeriodId(period.id);
    const url = new URL(window.location.href);
    url.searchParams.set("period", period.id);
    window.history.replaceState(null, "", url.toString());
  }, []);

  useEffect(() => {
    if (!periodId) return;
    let cancelled = false;
    fetchSchedule(periodId)
      .then(({ accepted, domains, metas, slots: grid, references: refs }) => {
        if (cancelled) return;
        setServerMetas(metas);
        setServerSlots(grid);
        setReferences(refs);
        dispatch({ type: "LOAD", accepted, domains });
      })
      .catch((error: Error) => {
        if (!cancelled) setLoadError(error.message);
      });
    return () => {
      cancelled = true;
    };
  }, [periodId]);

  // A drop puts the machine in ANALYSING; this performs the analysis and
  // reports it back as an event. The fetch lives here rather than in the
  // reducer so the reducer stays pure and testable.
  useEffect(() => {
    if (state.status !== "ANALYSING" || !periodId) return;
    let cancelled = false;
    analyseImpact(periodId, Object.values(state.accepted), state.pendingMoves)
      .then((impact) => {
        if (!cancelled) dispatch({ type: "IMPACT_READY", impact });
      })
      .catch((error: Error) => {
        if (!cancelled) dispatch({ type: "IMPACT_FAILED", error: error.message });
      });
    return () => {
      cancelled = true;
    };
  }, [state.status, state.pendingMoves, state.accepted, periodId]);

  // SIMULATING asks the server for other slots the session could take. Like
  // the impact fetch, it lives here rather than in the reducer so the reducer
  // stays pure.
  useEffect(() => {
    if (state.status !== "SIMULATING" || !periodId) return;
    let cancelled = false;
    fetchAlternatives(periodId, Object.values(state.accepted), state.pendingMoves)
      .then(({ alternatives }) => {
        if (!cancelled) dispatch({ type: "ALTERNATIVES_READY", alternatives });
      })
      .catch((error: Error) => {
        if (!cancelled) dispatch({ type: "IMPACT_FAILED", error: error.message });
      });
    return () => {
      cancelled = true;
    };
  }, [state.status, state.pendingMoves, state.accepted, periodId]);

  const slots: Slot[] = useMemo(() => {
    // The period's grid, as the server sent it.
    //
    // This used to parse the day and the row out of the slot id with
    // `/^([a-z]+)(\d+)$/` — which matches the fixtures' `mon0` and nothing a
    // database produces. Against real ids every slot became day "DAY", row 0,
    // so a week of thirty slots collapsed into a single cell holding every
    // class. The grid rendered, and was useless.
    if (serverSlots.length > 0) return serverSlots;

    // Fallback for a backend that does not send the grid yet: the slots the
    // domains mention, in id order, with no day structure. Enough to see the
    // classes, honest about being a degraded view.
    const ids = new Set<string>();
    for (const domain of Object.values(state.domains)) {
      domain.startSlotIds.forEach((id) => ids.add(id));
    }
    return Array.from(ids)
      .sort()
      .map((slotId, index) => ({
        slotId,
        day: "—",
        inDayIndex: index,
        label: `#${index + 1}`,
      }));
  }, [serverSlots, state.domains]);

  const metas = useMemo(() => {
    const out: Record<string, SessionMeta> = {};
    for (const sessionId of Object.keys(state.domains)) {
      out[sessionId] = serverMetas[sessionId] ?? unlabelled(sessionId);
    }
    return out;
  }, [state.domains, serverMetas]);

  // Room id -> its code, for the block. The grid carried `room_id` in the
  // placement from SESSION 10 and never had a name to print beside it.
  const roomNames = useMemo(() => {
    const out: Record<string, string> = {};
    for (const room of references.rooms) out[room.id] = room.label;
    return out;
  }, [references.rooms]);

  // The resource being looked at, when it is one that can be unavailable.
  const owner: { kind: "teacher" | "room"; id: string } | null =
    filters.subjectId && (filters.view === "TEACHER" || filters.view === "ROOM")
      ? {
          kind: filters.view === "TEACHER" ? "teacher" : "room",
          id: filters.subjectId,
        }
      : null;

  useEffect(() => {
    if (!owner || !periodId) {
      setUnavailable(new Set());
      setEditing(false);
      return;
    }
    let cancelled = false;
    fetchAvailability(owner.kind, owner.id, periodId)
      .then((found) => {
        if (!cancelled) setUnavailable(new Set(found.unavailableSlotIds));
      })
      .catch(() => {
        if (!cancelled) setUnavailable(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, [owner?.kind, owner?.id, periodId]);

  const placement = previewPlacement(state);
  const roomOf = useCallback(
    (sessionId: string) => placement[sessionId]?.roomId ?? null,
    [placement],
  );

  // The options for the current view's filter, derived from what is actually
  // on the grid. The dropdown was hard-coded empty from SESSION 10 to
  // SESSION 22 — a control that cannot be used is worse than an absent one,
  // because it reads as broken rather than unbuilt.
  const subjects = useMemo(() => {
    // Named options, from the server. These listed raw UUIDs until SESSION
    // 31 — the filters worked perfectly and nobody could use them, which is
    // the same objection this file already recorded about teacher ids and had
    // never applied to programmes, faculties or rooms.
    switch (filters.view) {
      case "PROGRAM":
        return references.programs;
      case "FACULTY":
        return references.faculties;
      case "ROOM":
        return references.rooms;
      case "TEACHER": {
        // Built from what is on the grid rather than from a reference list:
        // the people teaching this period are the useful set, not every
        // teacher the institution employs.
        const seen = new Map<string, string>();
        for (const meta of Object.values(metas)) {
          meta.teacherIds.forEach((id, index) =>
            seen.set(id, meta.teacherNames[index] || id),
          );
        }
        return Array.from(seen, ([id, label]) => ({ id, label })).sort((a, b) =>
          a.label.localeCompare(b.label),
        );
      }
      default:
        // STUDENT: deliberately empty. A dropdown of every student in the
        // period is both unusable and a roll-call of who is enrolled.
        return [];
    }
  }, [filters.view, references, metas]);

  const visible = useMemo(
    () => visibleSessions(Object.values(metas), filters, roomOf),
    [metas, filters, roomOf],
  );
  const visibleIds = useMemo(
    () => new Set(visible.map((m) => m.sessionId)),
    [visible],
  );

  // Sessions this change touches that the current filter is hiding. Submitting
  // without knowing is how a "safe" change breaks another programme.
  const hiddenAffected = useMemo(() => {
    const touched = new Set<string>();
    for (const conflict of state.impact?.newConflicts ?? []) {
      touched.add(conflict.sessionA);
      touched.add(conflict.sessionB);
    }
    for (const sessionId of state.impact?.occupancyClashes ?? []) {
      touched.add(sessionId);
    }
    return Array.from(touched).filter((id) => isHidden(id, visible));
  }, [state.impact, visible]);

  if (!periodId) {
    // Offer the choice rather than describe it. This used to say "open this
    // page with ?period=<uuid>", which asked for an id the interface showed
    // nowhere — a dead end wearing an empty state's clothes.
    return (
      <div className="page">
        <h1 className="page__title">Horario</h1>
        <PeriodPicker onChoose={choosePeriod} />
      </div>
    );
  }

  const hasLoaded = Object.keys(state.domains).length > 0;

  return (
    <div className="timetable">
      <Toolbar
        filters={filters}
        zoom={zoom}
        subjects={subjects}
        onFilters={setFilters}
        onZoom={setZoom}
      />
      {loadError && (
        <p className="notice notice--error" role="alert">
          No se pudo cargar el horario: {loadError}
        </p>
      )}
      {state.lastChangeRequestId && (
        // `role="status"`: the outcome of an action the user took, announced
        // rather than only shown.
        <p className="notice notice--success" role="status">
          Solicitud {state.lastChangeRequestId} creada. El horario publicado no
          cambia hasta que alguien la apruebe.
        </p>
      )}
      {!hasLoaded && !loadError && (
        <LoadingState label="Cargando el horario del periodo…" rows={6} />
      )}
      {owner && (
        <div className="notice" role="status">
          <strong>
            {filters.view === "TEACHER" ? "Docente" : "Aula"}:{" "}
            {subjects.find((s) => s.id === filters.subjectId)?.label ?? ""}
          </strong>{" "}
          · {visible.length} clase(s) · {unavailable.size} hora(s) no
          disponible(s)
          {" "}
          <button
            type="button"
            className="button"
            onClick={() => setEditing((editing) => !editing)}
          >
            {isEditing ? "Terminar" : "Editar horas no disponibles"}
          </button>
          {isEditing && (
            <>
              {" "}
              <button
                type="button"
                className="button button--primary"
                disabled={savingState === "saving"}
                onClick={async () => {
                  setSavingState("saving");
                  try {
                    await saveAvailability(
                      owner.kind,
                      owner.id,
                      periodId,
                      Array.from(unavailable),
                    );
                    setSavingState("saved");
                  } catch (cause) {
                    setSavingState(
                      cause instanceof Error ? cause.message : "no se guardó",
                    );
                  }
                }}
              >
                {savingState === "saving" ? "Guardando…" : "Guardar"}
              </button>
              {/* What clicking does, said where the clicking happens. */}
              <span className="session__meta">
                {" "}
                Haz clic en una hora para marcarla como no disponible. Es una
                restricción HARD: el optimizador no colocará nada ahí.
              </span>
            </>
          )}
          {savingState && savingState !== "saving" && (
            <span className="session__meta"> · {savingState === "saved" ? "guardado" : savingState}</span>
          )}
        </div>
      )}
      <div className="timetable__body">
        <TimetableGrid
          state={state}
          slots={slots}
          metas={metas}
          roomNames={roomNames}
          unavailableSlotIds={unavailable}
          onToggleAvailability={
            isEditing && owner
              ? (slotId) =>
                  setUnavailable((current) => {
                    const next = new Set(current);
                    if (next.has(slotId)) next.delete(slotId);
                    else next.add(slotId);
                    setSavingState(null);
                    return next;
                  })
              : undefined
          }
          visibleSessionIds={visibleIds}
          zoom={zoom}
          dispatch={dispatch}
        />
        <ImpactPanel
          status={state.status}
          impact={state.impact}
          error={state.error}
          hiddenAffected={hiddenAffected}
          onCancel={() => dispatch({ type: "CANCEL" })}
          onSimulate={() => dispatch({ type: "SIMULATE_ALTERNATIVES" })}
          onCreateChangeRequest={() => setConfirming(true)}
          alternatives={state.alternatives}
          onChooseAlternative={(index) =>
            dispatch({ type: "CHOOSE_ALTERNATIVE", index })
          }
          onDismissError={() => dispatch({ type: "DISMISS_ERROR" })}
        />
      </div>

      {isConfirming && (
        <ConfirmDialog
          title="¿Solicitar este cambio?"
          confirmLabel="Solicitar el cambio"
          body={
            <div>
              <p>{summariseProposal(state.pendingMoves, metas)}.</p>
              {/* What the person is actually agreeing to: not that the
                  timetable changes, but that a request goes to a named chain
                  of people. Saying so here is the difference between an
                  informed decision and a button press. */}
              <p className="metric__detail">
                Se creará una solicitud que otras personas deberán aprobar. El
                horario publicado no cambia ahora.
              </p>
              {(state.impact?.newConflicts.length ?? 0) > 0 && (
                <p className="state state--error">
                  Introduce {state.impact?.newConflicts.length} conflicto(s)
                  nuevo(s).
                </p>
              )}
            </div>
          }
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            // SESSION 18 gave this an endpoint. The reducer moves to
            // SUBMITTING first, so a second click cannot file the same
            // proposal twice while the first is in flight.
            dispatch({ type: "CREATE_CHANGE_REQUEST" });
            createChangeRequest(
              periodId,
              summariseProposal(state.pendingMoves, metas),
              state.pendingMoves,
            )
              .then((result) =>
                dispatch({
                  type: "CHANGE_REQUEST_CREATED",
                  changeRequestId: result.code,
                }),
              )
              .catch((error: Error) =>
                dispatch({ type: "SUBMIT_FAILED", error: error.message }),
              );
          }}
        />
      )}
    </div>
  );
}
