// Filters, search and the five views the playbook names.
//
// Pure functions over the session list, for the same reason the reducer is
// pure: a view that decides what to show is a decision, and decisions belong
// somewhere a test can reach without a browser.

export type ViewMode = "PROGRAM" | "FACULTY" | "TEACHER" | "STUDENT" | "ROOM";

/** The metadata the grid needs to label, filter and group a session. */
export interface SessionMeta {
  sessionId: string;
  courseCode: string;
  courseName: string;
  sectionCode: string;
  teacherIds: string[];
  teacherNames: string[];
  /** Plural, because a shared or common-core course serves more than one
   *  programme. Filtering matches *any* of them: with a single `programId`
   *  the course vanished from every programme but the first, which is the
   *  ordinary case for a service course rather than an exotic one. */
  programIds: string[];
  facultyIds: string[];
  studentIds: string[];
  durationSlots: number;
}

export interface Filters {
  view: ViewMode;
  /** The program / faculty / teacher / student / room being looked at. Empty
   *  means "everything", which is a legitimate way to look at a timetable and
   *  not a missing selection. */
  subjectId: string;
  search: string;
}

export const NO_FILTERS: Filters = { view: "PROGRAM", subjectId: "", search: "" };

function matchesView(meta: SessionMeta, filters: Filters, roomId: string | null): boolean {
  if (!filters.subjectId) return true;
  switch (filters.view) {
    case "PROGRAM":
      return meta.programIds.includes(filters.subjectId);
    case "FACULTY":
      return meta.facultyIds.includes(filters.subjectId);
    case "TEACHER":
      return meta.teacherIds.includes(filters.subjectId);
    case "STUDENT":
      return meta.studentIds.includes(filters.subjectId);
    case "ROOM":
      return roomId === filters.subjectId;
    default:
      return true;
  }
}

function matchesSearch(meta: SessionMeta, search: string): boolean {
  if (!search.trim()) return true;
  const needle = search.trim().toLowerCase();
  // Course code, course name, section and teacher names. Deliberately not
  // student names: a coordinator typing a surname into a timetable search is
  // usually looking for a course, and matching students would surface every
  // class a namesake attends.
  return [
    meta.courseCode,
    meta.courseName,
    meta.sectionCode,
    ...meta.teacherNames,
  ]
    .filter(Boolean)
    .some((field) => field.toLowerCase().includes(needle));
}

export function visibleSessions(
  metas: SessionMeta[],
  filters: Filters,
  roomOf: (sessionId: string) => string | null,
): SessionMeta[] {
  return metas.filter(
    (meta) =>
      matchesView(meta, filters, roomOf(meta.sessionId)) &&
      matchesSearch(meta, filters.search),
  );
}

/**
 * Whether a session is hidden by the current filter.
 *
 * Used to warn before a change request: a move can create a conflict in a
 * session the coordinator cannot currently see, and submitting without
 * knowing that is how a "safe" change breaks another programme.
 */
export function isHidden(
  sessionId: string,
  visible: SessionMeta[],
): boolean {
  return !visible.some((meta) => meta.sessionId === sessionId);
}
