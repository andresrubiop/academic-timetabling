"use client";

// Views, search, zoom. The controls the playbook names, and nothing that
// changes a schedule.

import type { Filters, ViewMode } from "../lib/interaction/filters";

const VIEWS: { value: ViewMode; label: string }[] = [
  { value: "PROGRAM", label: "programa" },
  { value: "FACULTY", label: "facultad" },
  { value: "TEACHER", label: "docente" },
  { value: "STUDENT", label: "estudiante" },
  { value: "ROOM", label: "aula" },
];

interface Props {
  filters: Filters;
  zoom: number;
  subjects: { id: string; label: string }[];
  onFilters: (next: Filters) => void;
  onZoom: (next: number) => void;
}

export function Toolbar({ filters, zoom, subjects, onFilters, onZoom }: Props) {
  return (
    // A toolbar landmark, so a keyboard user can jump to the controls instead
    // of tabbing in from the top of the page every time.
    <div className="toolbar" role="toolbar" aria-label="Vista del horario">
      <label className="field">
        Vista
        <select
          className="field__control"
          value={filters.view}
          onChange={(e) =>
            // Changing the axis clears the subject: a teacher id is not a
            // room id, and keeping it would filter to nothing while looking
            // like a bug.
            onFilters({
              ...filters,
              view: e.target.value as ViewMode,
              subjectId: "",
            })
          }
        >
          {VIEWS.map((view) => (
            <option key={view.value} value={view.value}>
              {view.label}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        Mostrando
        <select
          className="field__control"
          value={filters.subjectId}
          onChange={(e) => onFilters({ ...filters, subjectId: e.target.value })}
        >
          <option value="">todo</option>
          {subjects.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        Buscar
        <input
          className="field__control field__control--search"
          type="search"
          value={filters.search}
          placeholder="curso, sección o docente"
          onChange={(e) => onFilters({ ...filters, search: e.target.value })}
        />
      </label>

      <label className="field">
        {/* The current value is shown, because a slider with no number is a
            control you cannot return to a setting you liked. */}
        Zoom ({zoom.toFixed(1)}×)
        <input
          type="range"
          min={0.7}
          max={1.8}
          step={0.1}
          value={zoom}
          onChange={(e) => onZoom(Number(e.target.value))}
        />
      </label>
    </div>
  );
}
