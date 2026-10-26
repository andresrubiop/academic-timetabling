import { describe, expect, it } from "vitest";

import {
  NO_FILTERS,
  isHidden,
  visibleSessions,
  type Filters,
  type SessionMeta,
} from "../filters";

const METAS: SessionMeta[] = [
  {
    sessionId: "S1",
    courseCode: "MAT101",
    courseName: "Cálculo I",
    sectionCode: "A",
    teacherIds: ["T1"],
    teacherNames: ["Ana Pérez"],
    programIds: ["P1"],
    facultyIds: ["F1"],
    studentIds: ["E1", "E2"],
    durationSlots: 1,
  },
  {
    sessionId: "S2",
    courseCode: "FIS201",
    courseName: "Física II",
    sectionCode: "B",
    teacherIds: ["T2"],
    teacherNames: ["Luis Gómez"],
    programIds: ["P2"],
    facultyIds: ["F1"],
    studentIds: ["E2"],
    durationSlots: 2,
  },
];

const ROOMS: Record<string, string | null> = { S1: "R1", S2: "R2" };
const roomOf = (id: string) => ROOMS[id] ?? null;

function filters(overrides: Partial<Filters> = {}): Filters {
  return { ...NO_FILTERS, ...overrides };
}

describe("views", () => {
  it("no subject means everything, not nothing", () => {
    // An unfiltered timetable is a legitimate way to look at one, not a
    // missing selection.
    expect(visibleSessions(METAS, filters(), roomOf)).toHaveLength(2);
  });

  it.each([
    ["PROGRAM", "P1", ["S1"]],
    ["FACULTY", "F1", ["S1", "S2"]],
    ["TEACHER", "T2", ["S2"]],
    ["STUDENT", "E2", ["S1", "S2"]],
    ["ROOM", "R1", ["S1"]],
  ] as const)("%s view filters to its subject", (view, subjectId, expected) => {
    const visible = visibleSessions(
      METAS,
      filters({ view, subjectId }),
      roomOf,
    );
    expect(visible.map((m) => m.sessionId)).toEqual(expected);
  });
});

describe("search", () => {
  it("matches a course code", () => {
    expect(
      visibleSessions(METAS, filters({ search: "mat101" }), roomOf),
    ).toHaveLength(1);
  });

  it("matches a course name with accents as typed", () => {
    expect(
      visibleSessions(METAS, filters({ search: "Física" }), roomOf),
    ).toHaveLength(1);
  });

  it("matches a teacher name", () => {
    expect(
      visibleSessions(METAS, filters({ search: "gómez" }), roomOf),
    ).toHaveLength(1);
  });

  it("does not match student ids", () => {
    // A coordinator typing a surname is looking for a course; matching
    // students would surface every class a namesake attends.
    expect(visibleSessions(METAS, filters({ search: "E2" }), roomOf)).toHaveLength(
      0,
    );
  });

  it("whitespace-only search shows everything", () => {
    expect(
      visibleSessions(METAS, filters({ search: "   " }), roomOf),
    ).toHaveLength(2);
  });

  it("combines with the view rather than replacing it", () => {
    const visible = visibleSessions(
      METAS,
      filters({ view: "PROGRAM", subjectId: "P1", search: "FIS" }),
      roomOf,
    );
    expect(visible).toHaveLength(0);
  });
});

describe("hidden sessions", () => {
  it("reports a session the current filter conceals", () => {
    // A move can create a conflict in a session the coordinator cannot see.
    // Submitting without knowing that is how a "safe" change breaks another
    // programme.
    const visible = visibleSessions(
      METAS,
      filters({ view: "PROGRAM", subjectId: "P1" }),
      roomOf,
    );
    expect(isHidden("S2", visible)).toBe(true);
    expect(isHidden("S1", visible)).toBe(false);
  });
});

describe("a course shared by two programmes (SESSION 22)", () => {
  // The reason `programIds` is plural. A common-core course serves several
  // programmes, and with a single `programId` it appeared in the first and
  // silently vanished from the rest -- which is the ordinary case for a
  // service course, not an exotic one.
  const shared: SessionMeta = {
    sessionId: "shared",
    courseCode: "MAT-101",
    courseName: "Cálculo",
    sectionCode: "A",
    teacherIds: [],
    teacherNames: [],
    programIds: ["P1", "P2"],
    facultyIds: ["F1", "F2"],
    studentIds: [],
    durationSlots: 1,
  };

  it("shows under either programme", () => {
    for (const programId of ["P1", "P2"]) {
      expect(
        visibleSessions([shared], { ...NO_FILTERS, subjectId: programId }, () => null),
      ).toHaveLength(1);
    }
  });

  it("shows under either faculty", () => {
    for (const facultyId of ["F1", "F2"]) {
      expect(
        visibleSessions(
          [shared],
          { ...NO_FILTERS, view: "FACULTY", subjectId: facultyId },
          () => null,
        ),
      ).toHaveLength(1);
    }
  });

  it("is still hidden from a programme it does not serve", () => {
    expect(
      visibleSessions([shared], { ...NO_FILTERS, subjectId: "P9" }, () => null),
    ).toHaveLength(0);
  });
});
