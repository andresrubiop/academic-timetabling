// The one place that talks to the backend.
//
// Kept apart from the state machine so the machine stays pure and testable
// (ADR-0012). Nothing here decides anything: it fetches, and hands the answer
// to the reducer as an event.

import type { SessionMeta } from "../interaction/filters";
import type {
  Alternative,
  ImpactReport,
  Placement,
  ProposedMove,
  SessionDomain,
} from "../interaction/types";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

/** Thrown with a message safe to show a user. Server 500s never carry a
 *  traceback (the API strips them), so this is already sanitised. */
export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token =
    typeof window === "undefined" ? null : window.localStorage.getItem("access_token");

  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    // The body is JSON with a `detail` when the API produced it, and
    // something else when a proxy did. Fall back rather than throwing while
    // handling an error.
    let detail = `request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep the fallback */
    }
    throw new ApiError(detail);
  }
  return (await response.json()) as T;
}

interface ScheduleViewWire {
  placement: { session_id: string; start_slot_id: string; room_id: string | null }[];
  domains: {
    session_id: string;
    start_slot_ids: string[];
    room_ids: string[];
    is_locked: boolean;
  }[];
  references?: {
    programs: { id: string; code: string; name: string }[];
    faculties: { id: string; code: string; name: string }[];
    rooms: { id: string; code: string; name: string }[];
  };
  slots?: {
    slot_id: string;
    day: string;
    in_day_index: number;
    label: string;
    start_minute: number;
    is_schedulable: boolean;
  }[];
  sessions?: {
    session_id: string;
    course_code: string;
    course_name: string;
    section_code: string;
    teacher_ids: string[];
    teacher_names: string[];
    program_ids: string[];
    faculty_ids: string[];
    student_ids: string[];
    duration_slots: number;
  }[];
}

/** One option the view selector can be set to. */
export interface FilterOption {
  id: string;
  label: string;
}

export interface ScheduleReferences {
  programs: FilterOption[];
  faculties: FilterOption[];
  rooms: FilterOption[];
}

export interface GridSlot {
  slotId: string;
  day: string;
  inDayIndex: number;
  label: string;
}

export async function fetchSchedule(
  academicPeriodId: string,
): Promise<{
  accepted: Placement[];
  domains: SessionDomain[];
  metas: Record<string, SessionMeta>;
  slots: GridSlot[];
  references: ScheduleReferences;
}> {
  const wire = await request<ScheduleViewWire>(
    `/schedule?academic_period_id=${encodeURIComponent(academicPeriodId)}`,
  );
  const metas: Record<string, SessionMeta> = {};
  // Optional on the wire, so a frontend deployed ahead of its backend still
  // renders a timetable -- it falls back to session ids, which is what the
  // grid did until SESSION 22.
  for (const s of wire.sessions ?? []) {
    metas[s.session_id] = {
      sessionId: s.session_id,
      courseCode: s.course_code,
      courseName: s.course_name,
      sectionCode: s.section_code,
      teacherIds: s.teacher_ids,
      teacherNames: s.teacher_names,
      programIds: s.program_ids,
      facultyIds: s.faculty_ids,
      studentIds: s.student_ids,
      durationSlots: s.duration_slots,
    };
  }
  // Names for the view selector. Without these the programme, faculty and
  // room filters listed UUIDs: they filtered correctly and nobody could use
  // them, which is the same comment this file already carried about teachers
  // and had never applied to the other three.
  const named = (rows?: { id: string; code: string; name: string }[]) =>
    (rows ?? []).map((r) => ({
      id: r.id,
      label: r.code && r.code !== r.name ? `${r.code} — ${r.name}` : r.name,
    }));

  return {
    references: {
      programs: named(wire.references?.programs),
      faculties: named(wire.references?.faculties),
      rooms: named(wire.references?.rooms),
    },
    // The period's own grid. Before SESSION 29 the client derived the day and
    // the row from the *text* of each slot id, which matched the fixtures'
    // `mon0` and nothing a database produces — so every real slot parsed as
    // day "DAY", row 0, and the entire week drew as one cell.
    slots: (wire.slots ?? []).map((s) => ({
      slotId: s.slot_id,
      day: s.day,
      inDayIndex: s.in_day_index,
      label: s.label,
    })),
    accepted: wire.placement.map((p) => ({
      sessionId: p.session_id,
      startSlotId: p.start_slot_id,
      roomId: p.room_id,
    })),
    domains: wire.domains.map((d) => ({
      sessionId: d.session_id,
      startSlotIds: d.start_slot_ids,
      roomIds: d.room_ids,
      isLocked: d.is_locked,
    })),
    metas,
  };
}

export async function analyseImpact(
  academicPeriodId: string,
  placement: Placement[],
  moves: ProposedMove[],
): Promise<ImpactReport> {
  const wire = await request<Record<string, any>>("/schedule/impact", {
    method: "POST",
    body: JSON.stringify({
      academic_period_id: academicPeriodId,
      placement: placement.map((p) => ({
        session_id: p.sessionId,
        start_slot_id: p.startSlotId,
        room_id: p.roomId,
      })),
      moves: moves.map((m) => ({
        session_id: m.sessionId,
        to_start_slot_id: m.toStartSlotId,
        to_room_id: m.toRoomId,
      })),
    }),
  });

  return {
    moves,
    affected: wire.affected,
    newConflicts: (wire.new_conflicts ?? []).map(toConflict),
    resolvedConflicts: (wire.resolved_conflicts ?? []).map(toConflict),
    scoreDelta: wire.score_delta ?? [],
    tierCodes: wire.tier_codes ?? [],
    stabilityDelta: wire.stability_delta ?? 0,
    occupancyClashes: wire.occupancy_clashes ?? [],
    summary: wire.summary ?? "",
  };
}

function toConflict(c: Record<string, string>) {
  return {
    studentId: c.student_id,
    sessionA: c.session_a,
    sessionB: c.session_b,
    classification: c.classification,
  };
}


export async function fetchAlternatives(
  academicPeriodId: string,
  placement: Placement[],
  moves: ProposedMove[],
): Promise<{ alternatives: Alternative[]; note: string | null }> {
  const wire = await request<Record<string, any>>("/schedule/alternatives", {
    method: "POST",
    body: JSON.stringify({
      academic_period_id: academicPeriodId,
      placement: placement.map((p) => ({
        session_id: p.sessionId,
        start_slot_id: p.startSlotId,
        room_id: p.roomId,
      })),
      moves: moves.map((m) => ({
        session_id: m.sessionId,
        to_start_slot_id: m.toStartSlotId,
        to_room_id: m.toRoomId,
      })),
    }),
  });

  return {
    note: wire.note ?? null,
    alternatives: (wire.alternatives ?? []).map((a: Record<string, any>) => {
      const altMoves: ProposedMove[] = (a.moves ?? []).map(
        (m: Record<string, string | null>) => ({
          sessionId: m.session_id as string,
          toStartSlotId: m.to_start_slot_id as string,
          toRoomId: (m.to_room_id ?? null) as string | null,
        }),
      );
      return {
        label: a.label as string,
        moves: altMoves,
        impact: {
          moves: altMoves,
          affected: a.impact.affected,
          newConflicts: (a.impact.new_conflicts ?? []).map(toConflict),
          resolvedConflicts: (a.impact.resolved_conflicts ?? []).map(toConflict),
          scoreDelta: a.impact.score_delta ?? [],
          tierCodes: a.impact.tier_codes ?? [],
          stabilityDelta: a.impact.stability_delta ?? 0,
          occupancyClashes: a.impact.occupancy_clashes ?? [],
          summary: a.impact.summary ?? "",
        } as ImpactReport,
      } as Alternative;
    }),
  };
}


export interface ChangeRequestResult {
  changeRequestId: string;
  code: string;
  status: string;
  chainCode: string;
  scope: string;
  rationale: string;
}

/**
 * File a change request from the pending proposal.
 *
 * Only the *moves* are sent. The impact is recomputed server-side, because a
 * client-supplied one would let somebody submit a change claiming it affects
 * nobody — and the approval chain is chosen from the impact.
 */
export async function createChangeRequest(
  academicPeriodId: string,
  title: string,
  moves: ProposedMove[],
  description = "",
): Promise<ChangeRequestResult> {
  const wire = await request<Record<string, any>>("/change-requests", {
    method: "POST",
    body: JSON.stringify({
      academic_period_id: academicPeriodId,
      title,
      description,
      moves: moves.map((m) => ({
        session_id: m.sessionId,
        to_start_slot_id: m.toStartSlotId,
        to_room_id: m.toRoomId,
      })),
    }),
  });
  return {
    changeRequestId: wire.change_request_id,
    code: wire.code,
    status: wire.status,
    chainCode: wire.chain_code,
    scope: wire.scope,
    rationale: wire.rationale,
  };
}


export type MetricSeverity = "OK" | "ATTENTION" | "PROBLEM";
export type MetricKind = "COUNT" | "LIST" | "STATUS" | "TIER_BREAKDOWN";

export interface DashboardMetric {
  widgetCode: string;
  title: string;
  kind: MetricKind;
  value: number | null;
  unit: string | null;
  severity: MetricSeverity;
  rows: Record<string, unknown>[];
  detail: string | null;
  /**
   * The server could not compute this one. Rendered as "not available"
   * rather than as a zero: a zero is an answer, and "we could not tell you"
   * is not the same answer.
   */
  isUnavailable: boolean;
}

export interface Dashboard {
  role: string;
  metrics: DashboardMetric[];
}

/**
 * The signed-in viewer's dashboard.
 *
 * No role parameter, by design on the server side: a dashboard is assembled
 * from the caller's own permissions, so there is no request that asks for
 * somebody else's screen.
 */
export async function fetchDashboard(): Promise<Dashboard> {
  const wire = await request<Record<string, any>>("/dashboard");
  return {
    role: wire.role,
    metrics: (wire.metrics ?? []).map((m: Record<string, any>) => ({
      widgetCode: m.widget_code,
      title: m.title,
      kind: m.kind,
      value: m.value ?? null,
      unit: m.unit ?? null,
      severity: m.severity,
      rows: m.rows ?? [],
      detail: m.detail ?? null,
      isUnavailable: Boolean(m.is_unavailable),
    })),
  };
}


export interface SignInResult {
  accessToken: string;
  refreshToken: string | null;
}

/**
 * Exchange an email and password for a token.
 *
 * Does not go through `request()`: that helper attaches the token we do not
 * have yet, and turns every failure into the same `ApiError`. Signing in needs
 * the status code, because 401 and 429 call for different words — "those
 * credentials are wrong" and "too many attempts, wait" are not the same
 * message, and showing the first for the second sends somebody to reset a
 * password that was never the problem.
 */
export async function signIn(
  email: string,
  password: string,
): Promise<SignInResult> {
  const response = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  if (response.status === 401) {
    // The API answers the same way for a wrong password, an unknown account
    // and a deactivated one, on purpose — different answers turn the endpoint
    // into an account enumerator. This message says no more than the server
    // does.
    throw new ApiError("Correo o contraseña incorrectos.");
  }
  if (response.status === 429) {
    const retry = response.headers.get("Retry-After");
    throw new ApiError(
      retry
        ? `Demasiados intentos. Vuelve a intentarlo en ${retry} segundos.`
        : "Demasiados intentos. Espera un momento antes de reintentar.",
    );
  }
  if (!response.ok) {
    throw new ApiError(`No se pudo iniciar sesión (${response.status}).`);
  }

  const body = await response.json();
  return {
    accessToken: body.access_token,
    refreshToken: body.refresh_token ?? null,
  };
}

export interface Me {
  userId: string;
  role: string;
  permissions: string[];
}

export async function fetchMe(): Promise<Me> {
  const wire = await request<Record<string, any>>("/auth/me");
  return {
    userId: wire.user_id,
    role: wire.role,
    permissions: wire.permissions ?? [],
  };
}

/** Revoke the refresh token server-side. Best effort: a failure here must not
 *  stop the local session being cleared, or a user who cannot reach the API
 *  cannot sign out. */
export async function signOut(refresh: string | null): Promise<void> {
  if (!refresh) return;
  try {
    await fetch(`${BASE}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
  } catch {
    /* see above */
  }
}


export interface AcademicPeriodSummary {
  id: string;
  code: string;
  name: string;
  status: string;
  startDate: string | null;
  endDate: string | null;
}

/**
 * The periods this viewer may see.
 *
 * The timetable belongs to a period, and until SESSION 29 the only way to
 * name one was to paste its UUID into the address bar — the navigation link
 * led to a screen asking for an id the interface never showed anywhere.
 *
 * Ordered by start date, most recent first: the period somebody wants is
 * almost always the current one, and an alphabetical list of codes puts
 * `2019-1` at the top forever.
 */
export async function fetchPeriods(): Promise<AcademicPeriodSummary[]> {
  const wire = await request<Record<string, any>>(
    "/periods?sort=-start_date&page_size=50",
  );
  return (wire.items ?? []).map((p: Record<string, any>) => ({
    id: p.id,
    code: p.code,
    name: p.name,
    status: p.status,
    startDate: p.start_date ?? null,
    endDate: p.end_date ?? null,
  }));
}


// --- SESSION 33: user accounts -----------------------------------------------

export interface UserAccount {
  userId: string;
  email: string;
  fullName: string;
  role: string;
  isActive: boolean;
  mustChangePassword: boolean;
  /** Present only on the response to *creating* an account. Shown once and
   *  never fetched again: it is not stored anywhere it could be read back. */
  temporaryPassword: string | null;
}

function toAccount(w: Record<string, any>): UserAccount {
  return {
    userId: w.user_id,
    email: w.email,
    fullName: w.full_name,
    role: w.role,
    isActive: w.is_active,
    mustChangePassword: Boolean(w.must_change_password),
    temporaryPassword: w.temporary_password ?? null,
  };
}

export async function fetchUsers(): Promise<UserAccount[]> {
  const wire = await request<Record<string, any>>("/users");
  return (wire.users ?? []).map(toAccount);
}

export async function createUser(
  email: string,
  fullName: string,
  role: string,
): Promise<UserAccount> {
  return toAccount(
    await request<Record<string, any>>("/users", {
      method: "POST",
      body: JSON.stringify({ email, full_name: fullName, role }),
    }),
  );
}

export async function setUserActive(
  userId: string,
  isActive: boolean,
): Promise<UserAccount> {
  return toAccount(
    await request<Record<string, any>>(`/users/${userId}/active`, {
      method: "PATCH",
      body: JSON.stringify({ is_active: isActive }),
    }),
  );
}

export interface ScopeGrant {
  assignmentId: string;
  userId: string;
  facultyId: string | null;
  programId: string | null;
  note: string;
}

function toGrant(w: Record<string, any>): ScopeGrant {
  return {
    assignmentId: w.assignment_id,
    userId: w.user_id,
    facultyId: w.faculty_id ?? null,
    programId: w.program_id ?? null,
    note: w.note ?? "",
  };
}

export async function fetchScopes(userId: string): Promise<ScopeGrant[]> {
  const wire = await request<Record<string, any>>(`/users/${userId}/scopes`);
  return (wire.scopes ?? []).map(toGrant);
}

export async function grantScope(
  userId: string,
  target: { programId?: string; facultyId?: string; note?: string },
): Promise<ScopeGrant> {
  return toGrant(
    await request<Record<string, any>>(`/users/${userId}/scopes`, {
      method: "POST",
      body: JSON.stringify({
        program_id: target.programId ?? null,
        faculty_id: target.facultyId ?? null,
        note: target.note ?? "",
      }),
    }),
  );
}

export async function revokeScope(
  userId: string,
  assignmentId: string,
): Promise<void> {
  await fetch(`${BASE}/users/${userId}/scopes/${assignmentId}`, {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json",
      ...(typeof window !== "undefined" &&
      window.localStorage.getItem("access_token")
        ? {
            Authorization: `Bearer ${window.localStorage.getItem("access_token")}`,
          }
        : {}),
    },
  });
}

/** Programmes, for the scope picker. Reuses the CRUD list endpoint. */
export async function fetchProgramOptions(): Promise<FilterOption[]> {
  const wire = await request<Record<string, any>>("/programs?page_size=100");
  return (wire.items ?? []).map((p: Record<string, any>) => ({
    id: p.id,
    label: p.code && p.code !== p.name ? `${p.code} — ${p.name}` : p.name,
  }));
}


// --- SESSION 35: change requests ---------------------------------------------

export interface ChangeRequestSummary {
  requestId: string;
  code: string;
  title: string;
  status: string;
  summary: string;
  scope: string;
  affectedStudents: number;
  newConflicts: number;
  isMine: boolean;
  createdAt: string;
}

export async function fetchChangeRequests(
  statusFilter?: string,
): Promise<ChangeRequestSummary[]> {
  const query = statusFilter
    ? `?status_filter=${encodeURIComponent(statusFilter)}`
    : "";
  const wire = await request<Record<string, any>>(`/change-requests${query}`);
  return (wire.requests ?? []).map((r: Record<string, any>) => ({
    requestId: r.request_id,
    code: r.code,
    title: r.title,
    status: r.status,
    summary: r.summary ?? "",
    scope: r.scope ?? "",
    affectedStudents: r.affected_students ?? 0,
    newConflicts: r.new_conflicts ?? 0,
    isMine: Boolean(r.is_mine),
    createdAt: r.created_at,
  }));
}

export interface AppliedChange {
  code: string;
  versionNumber: number;
  movedSessions: number;
}

/**
 * Publish a new timetable that includes this approved change.
 *
 * The endpoint has existed and been tested since SESSION 34 and nothing in
 * the browser called it, so the last step of the whole flow was curl.
 */
export async function applyChangeRequest(
  requestId: string,
): Promise<AppliedChange> {
  const wire = await request<Record<string, any>>(
    `/change-requests/${requestId}/apply`,
    { method: "POST" },
  );
  return {
    code: wire.code,
    versionNumber: wire.version_number,
    movedSessions: wire.moved_sessions,
  };
}

export async function decideChangeRequest(
  requestId: string,
  action: "APPROVE" | "REJECT",
  comment: string,
): Promise<void> {
  await request(`/change-requests/${requestId}/decide`, {
    method: "POST",
    body: JSON.stringify({ action, comment }),
  });
}


// --- SESSION 38: availability ------------------------------------------------

export interface Availability {
  ownerKind: string;
  ownerId: string;
  unavailableSlotIds: string[];
  /** False when nobody has filled this calendar in — different from "free
   *  every hour", and the analyser reports it as a diagnostic. */
  hasDeclared: boolean;
  reason: string;
}

function toAvailability(w: Record<string, any>): Availability {
  return {
    ownerKind: w.owner_kind,
    ownerId: w.owner_id,
    unavailableSlotIds: w.unavailable_slot_ids ?? [],
    hasDeclared: Boolean(w.has_declared),
    reason: w.reason ?? "",
  };
}

export async function fetchAvailability(
  ownerKind: "teacher" | "room",
  ownerId: string,
  academicPeriodId: string,
): Promise<Availability> {
  return toAvailability(
    await request<Record<string, any>>(
      `/availability/${ownerKind}/${ownerId}` +
        `?academic_period_id=${encodeURIComponent(academicPeriodId)}`,
    ),
  );
}

/** The whole calendar, not a change to it: what is absent is available. */
export async function saveAvailability(
  ownerKind: "teacher" | "room",
  ownerId: string,
  academicPeriodId: string,
  unavailableSlotIds: string[],
  reason = "",
): Promise<Availability> {
  return toAvailability(
    await request<Record<string, any>>(`/availability/${ownerKind}/${ownerId}`, {
      method: "PUT",
      body: JSON.stringify({
        academic_period_id: academicPeriodId,
        unavailable_slot_ids: unavailableSlotIds,
        reason,
      }),
    }),
  );
}
