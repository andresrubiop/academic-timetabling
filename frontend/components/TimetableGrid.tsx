"use client";

// The week grid: drag and drop with a mouse, and the same move with a
// keyboard.
//
// The component decides nothing. It renders `previewPlacement(state)`, asks
// `canDrop` whether a cell is legal — which reads the server-computed domain,
// never a rule reimplemented here — and dispatches events. Every judgement
// lives in the reducer or on the server (ADR-0012).
//
// The keyboard path dispatches the *same* GRAB / DROP / RELEASE_OUTSIDE
// events as the pointer path, so the state machine is untouched and its
// CRITICAL-rule tests still cover both. The only extra state is where the
// cursor is sitting, which is presentation and belongs here.

import { useState } from "react";

import { canDrop, previewPlacement } from "../lib/interaction/machine";
import { keyboardMove, type GridCell } from "../lib/interaction/keyboard";
import {
  dayName,
  orderDays,
  programHue,
  segmentPosition,
  slotAtOffset,
  timeRange,
} from "../lib/interaction/labels";
import type { SessionMeta } from "../lib/interaction/filters";
import type { InteractionEvent, InteractionState } from "../lib/interaction/types";

export interface Slot {
  slotId: string;
  day: string;
  inDayIndex: number;
  label: string;
}

interface Props {
  state: InteractionState;
  slots: Slot[];
  metas: Record<string, SessionMeta>;
  /** Room id -> the name to print on a block. A timetable that does not say
   *  where a class is, is half a timetable. */
  roomNames?: Record<string, string>;
  /** Hours the resource being looked at cannot be used. Shaded, so a week
   *  read for one teacher shows why an hour is empty rather than leaving it
   *  indistinguishable from an hour nobody has filled. */
  unavailableSlotIds?: ReadonlySet<string>;
  /** When set, clicking a cell toggles its availability instead of dropping.
   *  The editor and the timetable are the same grid on purpose: a calendar
   *  edited on a different shape from the one it constrains is a calendar
   *  people fill in wrong. */
  onToggleAvailability?: (slotId: string) => void;
  visibleSessionIds: Set<string>;
  zoom: number;
  dispatch: (event: InteractionEvent) => void;
}

export function TimetableGrid({
  state,
  slots,
  metas,
  visibleSessionIds,
  zoom,
  dispatch,
  roomNames = {},
  unavailableSlotIds,
  onToggleAvailability,
}: Props) {
  // Where the keyboard cursor sits while a class is held, and what a screen
  // reader was last told. `null` means nothing is being moved by keyboard.
  const [cursor, setCursor] = useState<Slot | null>(null);
  // Which hour of a long class is being dragged, so dropping it puts the
  // class where the *grabbed hour* lands rather than shifting it forward.
  const [grabOffset, setGrabOffset] = useState(0);
  const [announcement, setAnnouncement] = useState("");

  const placement = previewPlacement(state);
  // Week order, not the order the slots arrived in.
  const days = orderDays(Array.from(new Set(slots.map((s) => s.day))));
  const rows = Array.from(new Set(slots.map((s) => s.inDayIndex))).sort(
    (a, b) => a - b,
  );

  /** One drawn piece of a class: which hour of it this is. */
  interface Segment {
    sessionId: string;
    index: number;
    duration: number;
  }

  // A class occupies every hour it runs, and each of those hours is a real
  // piece of it. Until SESSION 39 only the first hour was a block and the
  // rest were dim `↳` markers: the class read as one thing plus two
  // annotations, and only its first hour could be picked up.
  const byCell = new Map<string, Segment[]>();

  const orderedSlots = [...slots].sort(
    (a, b) =>
      days.indexOf(a.day) - days.indexOf(b.day) || a.inDayIndex - b.inDayIndex,
  );

  for (const [sessionId, placed] of Object.entries(placement)) {
    if (!visibleSessionIds.has(sessionId)) continue;
    const duration = Math.max(1, metas[sessionId]?.durationSlots ?? 1);
    const start = orderedSlots.findIndex((s) => s.slotId === placed.startSlotId);
    if (start < 0) continue;
    const startDay = orderedSlots[start].day;

    for (let index = 0; index < duration; index++) {
      const cell = orderedSlots[start + index];
      // A class does not run past the end of its own day; where the grid has
      // no slot for the hour, that hour is simply not drawn.
      if (!cell || cell.day !== startDay) break;
      const list = byCell.get(cell.slotId) ?? [];
      list.push({ sessionId, index, duration });
      byCell.set(cell.slotId, list);
    }
  }

  const dragging = state.draggingSessionId;
  const proposed = new Set(state.pendingMoves.map((m) => m.sessionId));
  const clashing = new Set(state.impact?.occupancyClashes ?? []);

  const slotAt = (cell: GridCell | null) =>
    cell ? (slots.find((s) => s.slotId === cell.slotId) ?? null) : null;

  // The component asks `keyboardMove` what a key means and does as it says.
  // Every decision -- whether the class may be picked up, where the cursor
  // goes, whether the drop is legal -- is in that function, tested without a
  // DOM, and dispatches only events the pointer path already used.
  function onSessionKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    sessionId: string,
    isLocked: boolean,
  ) {
    const isHeld = dragging === sessionId && cursor !== null;
    const outcome = keyboardMove(event.key, {
      sessionId,
      displayName: metas[sessionId]?.courseCode ?? sessionId,
      cells: slots,
      cursor: isHeld ? cursor : null,
      isLocked,
      ownSlotId: placement[sessionId]?.startSlotId ?? null,
      allowedSlotIds: state.domains[sessionId]?.startSlotIds ?? [],
      labelOf: (slotId) => slots.find((s) => s.slotId === slotId)?.label ?? slotId,
    });

    if (!outcome.handled) return;
    event.preventDefault();
    for (const emitted of outcome.events) dispatch(emitted);
    setCursor(slotAt(outcome.cursor));
    if (outcome.announcement !== null) setAnnouncement(outcome.announcement);
  }

  function cancelKeyboard() {
    dispatch({ type: "RELEASE_OUTSIDE" });
    setCursor(null);
    setAnnouncement("Movimiento cancelado. La sesión sigue en su horario.");
  }

  const cellHeight = Math.round(52 * zoom);

  return (
    <div className="grid-scroll">
      {/* Everything the cursor does is announced here. The visual highlight
          is invisible to the person who most needs to know where it is. */}
      <p aria-live="polite" className="visually-hidden">
        {announcement}
      </p>
      <table
        className="grid"
        style={{ fontSize: `${13 * zoom}px` }}
        // A grid is a grid. Screen readers get a real table rather than a
        // pile of divs pretending to be one.
        role="grid"
        aria-label="Horario semanal"
      >
        <thead>
          <tr>
            <th className="grid__head grid__head--time">
              <span className="visually-hidden">Hora</span>
            </th>
            {days.map((day) => (
              <th key={day} className="grid__head" scope="col">
                {dayName(day)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((rowIndex) => (
            <tr key={rowIndex}>
              <th className="grid__time" scope="row">
                {slots.find((s) => s.inDayIndex === rowIndex)?.label ?? ""}
              </th>
              {days.map((day) => {
                const slot = slots.find(
                  (s) => s.day === day && s.inDayIndex === rowIndex,
                );
                if (!slot) return <td key={day} className="grid__cell" />;

                const here = byCell.get(slot.slotId) ?? [];
                // Greyed out because the *server* said so, not because we
                // worked it out.
                const droppable =
                  dragging !== null &&
                  canDrop(state, dragging, slot.slotId, null);
                const isCursor = cursor?.slotId === slot.slotId;
                const isBlocked = unavailableSlotIds?.has(slot.slotId) ?? false;

                const classes = ["grid__cell"];
                if (isBlocked) classes.push("grid__cell--unavailable");
                if (onToggleAvailability) classes.push("grid__cell--editable");
                if (dragging) classes.push(droppable ? "grid__cell--droppable" : "grid__cell--blocked");
                if (isCursor) classes.push("grid__cell--cursor");

                return (
                  <td
                    key={day}
                    className={classes.join(" ")}
                    style={{ height: cellHeight }}
                    onDragOver={(e) => {
                      if (droppable) e.preventDefault();
                    }}
                    onDrop={(e) => {
                      e.preventDefault();
                      // Where the *class* starts, which is where the grabbed
                      // hour lands minus however far into the class it was.
                      const anchor = slotAtOffset(
                        slots,
                        slot.slotId,
                        -grabOffset,
                      );
                      if (anchor === null) return;
                      dispatch({
                        type: "DROP",
                        startSlotId: anchor,
                        // Room unchanged: this grid is the time axis. A room
                        // view drags rooms.
                        roomId: null,
                      });
                      setGrabOffset(0);
                    }}
                    aria-dropeffect={droppable ? "move" : "none"}
                    onClick={
                      onToggleAvailability
                        ? () => onToggleAvailability(slot.slotId)
                        : undefined
                    }
                    // The cell itself is the control while editing, so it has
                    // to say so and be reachable by keyboard.
                    role={onToggleAvailability ? "button" : undefined}
                    tabIndex={onToggleAvailability ? 0 : undefined}
                    aria-pressed={onToggleAvailability ? isBlocked : undefined}
                    aria-label={
                      onToggleAvailability
                        ? `${dayName(slot.day)} ${slot.label}: ${
                            isBlocked ? "no disponible" : "disponible"
                          }`
                        : undefined
                    }
                    onKeyDown={
                      onToggleAvailability
                        ? (event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              onToggleAvailability(slot.slotId);
                            }
                          }
                        : undefined
                    }
                  >
                    {here.length > 3 && (
                      // Only where it is needed. A count over two blocks is
                      // noise; over eleven it is the first thing somebody
                      // wants, because the cell scrolls and the rest are out
                      // of sight.
                      <span className="grid__count">{here.length} clases</span>
                    )}
                    {here.map(({ sessionId, index, duration }) => {
                      const meta = metas[sessionId];
                      const locked = Boolean(state.domains[sessionId]?.isLocked);
                      const held = dragging === sessionId && cursor !== null;
                      const roomId = placement[sessionId]?.roomId;
                      const room = roomId ? (roomNames[roomId] ?? null) : null;
                      const hue = programHue(meta?.programIds?.[0]);
                      const where = segmentPosition(index, duration);
                      const laterLabels = slots
                        .filter(
                          (s) =>
                            s.day === slot.day && s.inDayIndex > slot.inDayIndex,
                        )
                        .sort((a, b) => a.inDayIndex - b.inDayIndex)
                        .map((s) => s.label);
                      const range = timeRange(
                        slot.label,
                        laterLabels,
                        duration,
                      );

                      const blockClasses = ["session", `session--${where}`];
                      if (proposed.has(sessionId)) blockClasses.push("session--proposed");
                      if (clashing.has(sessionId)) blockClasses.push("session--clashing");
                      if (locked) blockClasses.push("session--locked");
                      if (held) blockClasses.push("session--held");

                      return (
                        <button
                          key={`${sessionId}-${index}`}
                          type="button"
                          className={blockClasses.join(" ")}
                          draggable={!locked}
                          aria-disabled={locked || undefined}
                          aria-pressed={held}
                          onDragStart={() => {
                            // Remember which hour was taken hold of.
                            setGrabOffset(index);
                            dispatch({ type: "GRAB", sessionId });
                          }}
                          onDragEnd={() => {
                            setGrabOffset(0);
                            dispatch({ type: "RELEASE_OUTSIDE" });
                          }}
                          onKeyDown={(event) =>
                            onSessionKeyDown(event, sessionId, locked)
                          }
                          onBlur={() => {
                            if (held) cancelKeyboard();
                          }}
                          title={[
                            meta?.courseName,
                            meta?.sectionCode && `Sección ${meta.sectionCode}`,
                            meta?.teacherNames?.join(", "),
                            room && `Aula ${room}`,
                            duration > 1 &&
                              `${range} · hora ${index + 1} de ${duration}`,
                          ]
                            .filter(Boolean)
                            .join("\n")}
                          style={
                            hue === null
                              ? undefined
                              : { borderLeftColor: `hsl(${hue} 55% 45%)` }
                          }
                        >
                          {where === "first" || where === "only" ? (
                            <>
                              <span className="session__code">
                                {meta?.courseCode ?? sessionId.slice(0, 8)}
                              </span>
                              {locked && (
                                <span aria-label="Bloqueada" title="Bloqueada">
                                  {" "}
                                  🔒
                                </span>
                              )}
                              {meta?.courseName &&
                                meta.courseName !== meta.courseCode && (
                                  <span className="session__name">
                                    <br />
                                    {meta.courseName}
                                  </span>
                                )}
                              <span className="session__meta">
                                <br />
                                {meta?.sectionCode}
                                {room && <> · {room}</>}
                              </span>
                              <span className="session__meta">
                                <br />
                                {meta?.teacherNames?.length
                                  ? meta.teacherNames.join(", ")
                                  : "sin docente"}
                                {duration > 1 && <> · {range}</>}
                              </span>
                            </>
                          ) : (
                            // A continuing hour repeats the code and says
                            // which hour it is, and nothing else. Repeating
                            // the whole block three times would triple the
                            // reading for one class; saying nothing left an
                            // hour that looked free.
                            <span className="session__meta">
                              {meta?.courseCode ?? ""} · {index + 1}/{duration}
                            </span>
                          )}
                          {proposed.has(sessionId) && where !== "middle" && (
                            <span className="session__flag">
                              <br />
                              propuesta — sin guardar
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
