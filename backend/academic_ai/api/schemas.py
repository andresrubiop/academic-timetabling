"""Request and response models.

Separate from the ORM on purpose. A response model that *is* the ORM row
serialises whatever columns exist, so adding `password_hash` to a table would
publish it, and adding any column would silently change the API contract.
Every field here was chosen to be sent.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

__all__ = [
    "AffectedCountsModel",
    "ChangeRequestStateResponse2",
    "DecideRequest",
    "SubmitChangeRequest",
    "SubmitChangeRequestResponse",
    "ChannelStatusModel",
    "MarkReadRequest",
    "MarkReadResponse",
    "NotificationListResponse",
    "NotificationModel",
    "DashboardResponse",
    "MetricModel",
    "ImportDiagnosticModel",
    "ImportPreviewResponse",
    "ImportResultResponse",
    "ImportSpecModel",
    "RowPlanModel",
    "ApprovalActionRequest",
    "ChainStepModel",
    "ChangeRequestStateResponse",
    "DecisionModel",
    "RoutingResponse",
    "ScopeAssessmentModel",
    "AlternativeModel",
    "AlternativesRequest",
    "AlternativesResponse",
    "CancelResponse",
    "ConflictChangeModel",
    "ImpactRequest",
    "ImpactResponse",
    "PlacementModel",
    "ProposedMoveModel",
    "ScheduleViewResponse",
    "SessionDomainModel",
    "HealthResponse",
    "LoginRequest",
    "MeResponse",
    "RefreshRequest",
    "RunListResponse",
    "RunResponse",
    "SubmitRunRequest",
    "SubmitRunResponse",
    "TokenResponse",
]


class LoginRequest(BaseModel):
    # A plain string, not `EmailStr`. Validating the shape here would answer
    # 422 for a malformed address and 401 for a wrong password, so the login
    # endpoint would respond differently depending on the *form* of the input
    # -- which is the inconsistency the auth layer exists to avoid. An address
    # that is not an address simply matches no row.
    #
    # It also happens that `EmailStr` rejects the RFC 2606 `.test` TLD that
    # exists precisely for testing, but the reasoning above stands on its own.
    email: str
    # No max_length matching the password policy: the policy applies to
    # *setting* a password. A cap here that mirrored it would tell an attacker
    # the shape of the rule. The hasher enforces its own byte cap.
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    user_id: str
    role: str
    permissions: list[str]


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, dict[str, object]] = {}


class SubmitRunRequest(BaseModel):
    academic_period_id: str
    base_schedule_version_id: str | None = None
    scenario_id: str | None = None
    # Bounded. An unbounded time limit on a shared host is a way to hold 8 CPU
    # for as long as the submitter likes.
    time_limit_seconds: float = Field(default=60.0, gt=0, le=3600)
    random_seed: int = Field(default=0, ge=0)


class SubmitRunResponse(BaseModel):
    run_id: str
    status: str
    queue_position: int


class RunResponse(BaseModel):
    run_id: str
    status: str
    progress_percent: int
    progress_message: str | None = None
    solver_status: str | None = None
    objective_value: float | None = None
    failure_reason: str | None = None
    created_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    heartbeat_at: dt.datetime | None = None
    attempts: int = 0
    cancel_requested: bool = False
    is_terminal: bool = False


class RunListResponse(BaseModel):
    runs: list[RunResponse]


class CancelResponse(BaseModel):
    run_id: str
    status: str
    cancel_requested: bool
    detail: str


# --- SESSION 10: the drag preview -------------------------------------------


class ProposedMoveModel(BaseModel):
    session_id: str
    to_start_slot_id: str
    #: `null` means "leave the room alone". A move along the time axis must not
    #: silently unassign a room.
    to_room_id: str | None = None


class PlacementModel(BaseModel):
    session_id: str
    start_slot_id: str
    room_id: str | None = None


class ImpactRequest(BaseModel):
    academic_period_id: str
    placement: list[PlacementModel]
    # Bounded. Impact analysis is O(students x pairs); an unbounded batch is a
    # way to hold a worker on a shared host.
    moves: list[ProposedMoveModel] = Field(min_length=0, max_length=50)


class AffectedCountsModel(BaseModel):
    students: int = 0
    teachers: int = 0
    programs: int = 0
    faculties: int = 0
    rooms: int = 0
    sessions: int = 0


class ConflictChangeModel(BaseModel):
    student_id: str
    session_a: str
    session_b: str
    classification: str


class ImpactResponse(BaseModel):
    """What the preview renders. Produced without writing anything."""

    moves: list[ProposedMoveModel]
    affected: AffectedCountsModel
    new_conflicts: list[ConflictChangeModel]
    resolved_conflicts: list[ConflictChangeModel]
    #: Per tier, best-first. Negative is better. Never a scalar -- see
    #: ADR-0010 and ADR-0012.
    score_delta: list[int]
    tier_codes: list[str]
    stability_delta: int
    occupancy_clashes: list[str]
    summary: str
    is_improvement: bool
    is_neutral: bool


class SessionDomainModel(BaseModel):
    """Where a session may go, computed by the server.

    The client renders this and never derives it: a second feasibility
    implementation in the browser would drift from CP-SAT (ADR-0012).
    """

    session_id: str
    start_slot_ids: list[str]
    room_ids: list[str]
    is_locked: bool = False


class SessionMetaModel(BaseModel):
    """What a session is called, for the grid.

    Separate from `SessionDomainModel` because the two answer different
    questions and come from different reads: a domain is what the *solver*
    computed, a label is what a *person* needs to recognise the class. The
    scheduling model carries no display strings at all, deliberately.
    """

    session_id: str
    course_code: str
    course_name: str
    section_code: str
    teacher_ids: list[str] = []
    teacher_names: list[str] = []
    #: Several, because a shared course serves more than one programme.
    #: Filtering has to match any of them or the course disappears from the
    #: programme that takes it as a service.
    program_ids: list[str] = []
    faculty_ids: list[str] = []
    student_ids: list[str] = []
    duration_slots: int = 1


class SlotModel(BaseModel):
    """One cell of the week, as the grid should draw it.

    Added in SESSION 29. The client used to derive the day and the row from
    the *text* of the slot id with `/^([a-z]+)(\\d+)$/` — which matches the
    fixtures' `mon0` and nothing a database produces. Against real UUIDs every
    slot parsed as day "DAY", row 0, so the whole week collapsed into a single
    cell and every class landed in it.

    The day and the index are facts about the period's grid; the server has
    them and had simply never sent them.
    """

    slot_id: str
    #: `MONDAY`, `TUESDAY`, … — the domain enum's own value, not an index, so
    #: a period that does not start its week on Monday still renders.
    day: str
    #: Position within the day, which is what orders the rows.
    in_day_index: int
    #: What to print in the time column: `08:00`.
    label: str
    start_minute: int
    is_schedulable: bool = True


class ReferenceLabelModel(BaseModel):
    """One option a filter can be set to."""

    id: str
    code: str = ""
    name: str


class ReferencesModel(BaseModel):
    """What the view selector offers, by name.

    A lookup rather than fields repeated on every session: a period with 399
    sessions across 12 programmes would carry each programme's name 399 times,
    and the client only ever needs the set.
    """

    programs: list[ReferenceLabelModel] = []
    faculties: list[ReferenceLabelModel] = []
    rooms: list[ReferenceLabelModel] = []


class ScheduleViewResponse(BaseModel):
    placement: list[PlacementModel]
    domains: list[SessionDomainModel]
    #: The period's grid. Empty for a client built against the older shape,
    #: which then falls back to parsing ids as it always did.
    slots: list[SlotModel] = []
    #: Names for the view selector. Without these the programme, faculty and
    #: room filters listed UUIDs — the filters worked and were unusable.
    references: ReferencesModel = ReferencesModel()
    #: Added in SESSION 22. Defaults to empty so a client built against the
    #: earlier shape still parses this response -- the grid degrades to
    #: session ids, which is exactly what it did before.
    sessions: list[SessionMetaModel] = []


# --- SESSION 11: alternatives ------------------------------------------------


class AlternativesRequest(BaseModel):
    academic_period_id: str
    placement: list[PlacementModel]
    #: The move the coordinator proposed. Alternatives are answers to "this
    #: one is awkward -- what else?", so there has to be a `this one`.
    moves: list[ProposedMoveModel] = Field(min_length=1, max_length=10)
    max_alternatives: int = Field(default=3, ge=1, le=8)


class AlternativeModel(BaseModel):
    label: str
    moves: list[ProposedMoveModel]
    impact: ImpactResponse


class AlternativesResponse(BaseModel):
    alternatives: list[AlternativeModel]
    #: Set when fewer than asked for were found, so the UI can say why rather
    #: than showing a short list that looks like the whole answer.
    note: str | None = None


# --- SESSION 12: governance --------------------------------------------------


class ScopeAssessmentModel(BaseModel):
    """Why this request needs the chain it got.

    Returned on every routing answer so a coordinator who expected two steps
    and got four can see what widened it, rather than concluding the system is
    arbitrary.
    """

    scope: str
    programs: int
    faculties: int
    rationale: str


class ChainStepModel(BaseModel):
    step_number: int
    required_role: str
    requires_all_affected: bool
    description: str = ""
    #: Who is still outstanding on this step. Only meaningful for a step that
    #: requires every affected authority.
    outstanding: list[str] = []


class RoutingResponse(BaseModel):
    """The chain a request would take, worked out before it is submitted.

    Shown at submission time so the requester knows who will see it. A chain
    revealed only after submission makes every escalation feel like a
    surprise.
    """

    assessment: ScopeAssessmentModel
    chain_code: str
    steps: list[ChainStepModel]


class ApprovalActionRequest(BaseModel):
    action: str
    comment: str = ""
    #: Required for a step that needs every affected authority: which of them
    #: this actor is standing for.
    on_behalf_of: str | None = None
    counterproposal: dict[str, object] | None = None


class DecisionModel(BaseModel):
    step_number: int
    actor_user_id: str
    actor_role: str
    action: str
    comment: str = ""
    on_behalf_of: str | None = None


class ChangeRequestStateResponse(BaseModel):
    request_id: str
    status: str
    is_terminal: bool
    chain_code: str
    pending_step: ChainStepModel | None = None
    permitted_roles: list[str]
    decisions: list[DecisionModel]
    assessment: ScopeAssessmentModel | None = None


# --- SESSION 15: imports -----------------------------------------------------


class ImportSpecModel(BaseModel):
    """What a file must contain, served before anybody builds one."""

    code: str
    entity: str
    description: str = ""
    required_columns: list[str]
    optional_columns: list[str]
    #: The fields that identify a row across imports, which is what makes
    #: re-importing the same file an update rather than a duplicate.
    natural_key: list[str]


class ImportDiagnosticModel(BaseModel):
    row_number: int
    severity: str
    message: str
    column: str | None = None
    value: str | None = None
    #: The whole thing as one sentence, so a UI can render a list without
    #: reassembling it and getting the wording subtly different.
    describe: str


class RowPlanModel(BaseModel):
    row_number: int
    outcome: str
    diagnostics: list[ImportDiagnosticModel] = []


class ImportPreviewResponse(BaseModel):
    """What this file would do. Nothing has been written."""

    #: Present only when the file is importable. Confirming needs it.
    token: str | None = None
    spec_code: str
    entity: str
    filename: str
    headings: list[str]
    summary: str
    is_importable: bool
    creates: int
    updates: int
    unchanged: int
    rejected: int
    file_diagnostics: list[ImportDiagnosticModel] = []
    rows: list[RowPlanModel] = []
    #: True when `rows` is a sample. The counts above stay exact regardless,
    #: and every rejected row is included before any accepted one.
    rows_truncated: bool = False


class ImportResultResponse(BaseModel):
    committed: bool
    summary: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    refusal: str | None = None
    #: The re-derived plan the import actually acted on, which may differ from
    #: what the operator previewed if the database changed in between.
    preview: ImportPreviewResponse


# --- SESSION 16: dashboards --------------------------------------------------


class MetricModel(BaseModel):
    widget_code: str
    title: str
    kind: str
    value: object | None = None
    unit: str = ""
    severity: str = "OK"
    rows: list[dict] = []
    detail: str = ""
    #: True when the number could not be computed. Rendered as "not
    #: available", never as zero -- "0 conflicts" and "we did not check"
    #: look identical and mean opposite things.
    is_unavailable: bool = False


class DashboardResponse(BaseModel):
    role: str
    #: Only what this viewer is entitled to. A widget they may not see is
    #: absent rather than present-and-empty: an empty panel titled "Faculty
    #: indicators" tells them such a thing exists and they are not allowed
    #: it, which is a small leak and an unkind one.
    metrics: list[MetricModel]


# --- SESSION 17: notifications -----------------------------------------------


class NotificationModel(BaseModel):
    id: str
    kind: str
    channel: str
    title: str
    body: str | None = None
    payload: dict | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    #: Whether somebody is expected to *do* something. Drives ordering: a
    #: request waiting on you outranks a run that finished.
    is_actionable: bool = False
    is_read: bool = False
    created_at: dt.datetime | None = None


class NotificationListResponse(BaseModel):
    items: list[NotificationModel]
    total: int
    unread: int
    #: Unread and actionable. The number a bell should show -- counting
    #: everything unread makes the badge a permanent decoration.
    unread_actionable: int


class MarkReadRequest(BaseModel):
    #: Omit to mark everything read. Ids belonging to somebody else simply
    #: match nothing; a 404 would confirm the id exists.
    notification_ids: list[str] | None = None


class MarkReadResponse(BaseModel):
    marked: int
    unread: int


class ChannelStatusModel(BaseModel):
    channel: str
    is_available: bool
    detail: str = ""


# --- SESSION 18: change requests ---------------------------------------------


class SubmitChangeRequest(BaseModel):
    academic_period_id: str
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    #: The moves being proposed. The impact is **recomputed server-side** from
    #: these -- a client-supplied impact would let somebody submit a change
    #: claiming it affects nobody, and the routing is derived from the impact.
    moves: list[ProposedMoveModel] = Field(min_length=1, max_length=50)


class SubmitChangeRequestResponse(BaseModel):
    change_request_id: str
    code: str
    status: str
    chain_code: str
    scope: str
    #: Why the request needs the chain it got, so a coordinator who expected
    #: two steps and got four can see what widened it.
    rationale: str
    impact: ImpactResponse
    notified: int = 0


class DecideRequest(BaseModel):
    action: str
    comment: str = ""
    #: Which affected authority this actor stands for, on a step that needs
    #: every one of them.
    on_behalf_of: str | None = None
    counterproposal: dict | None = None


class ChangeRequestStateResponse2(BaseModel):
    request_id: str
    code: str
    title: str
    status: str
    is_terminal: bool
    chain_code: str
    pending_step: ChainStepModel | None = None
    decisions: list[DecisionModel] = []
    impact: dict = {}


class ChangeRequestSummaryModel(BaseModel):
    """One row of the request list.

    Deliberately not the full state: a list is read to decide *which* request
    to open, and loading every chain and every decision for a page of twenty
    is work nobody asked for.
    """

    request_id: str
    code: str
    title: str
    status: str
    academic_period_id: str
    requested_by_user_id: str
    created_at: str
    #: Enough of the impact to choose from: how far it reaches and what it
    #: costs. The numbers a person scans before opening anything.
    summary: str = ""
    scope: str = ""
    affected_students: int = 0
    new_conflicts: int = 0
    #: True when this request is the caller's own. A list narrowed by scope
    #: must still show you what you submitted, or your own request vanishes
    #: the moment it reaches somebody else's programme.
    is_mine: bool = False


class ChangeRequestListResponse(BaseModel):
    requests: list[ChangeRequestSummaryModel]


# --- SESSION 24: scope assignments -------------------------------------------


class ScopeGrantModel(BaseModel):
    """One assignment. Exactly one of `faculty_id` / `program_id` is set."""

    assignment_id: str
    user_id: str
    faculty_id: str | None = None
    program_id: str | None = None
    note: str = ""


class ScopeGrantsResponse(BaseModel):
    scopes: list[ScopeGrantModel]


class ScopeGrantRequest(BaseModel):
    """Name a faculty **or** a programme, never both.

    Not an enum plus an id: the pair would let a faculty id be sent as a
    programme, and the database would accept it because both are uuids.
    """

    faculty_id: str | None = None
    program_id: str | None = None
    #: Why this person holds this scope. An assignment nobody can explain is
    #: one nobody dares remove.
    note: str = ""


# --- SESSION 33: changing your own password ----------------------------------


class ChangePasswordRequest(BaseModel):
    """The current password is required even when a change is being forced.

    Otherwise the holder of a stolen access token could set a new password and
    lock the real owner out — turning a token theft into an account theft.
    """

    current_password: str
    new_password: str


# --- SESSION 33: user accounts -----------------------------------------------


class UserAccountModel(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    must_change_password: bool = False
    #: Returned **only** when an account is created, and never stored or
    #: logged. The administrator has one chance to pass it on.
    temporary_password: str | None = None


class UserListResponse(BaseModel):
    users: list[UserAccountModel]


class CreateUserRequest(BaseModel):
    email: str
    full_name: str = ""
    role: str


class SetRoleRequest(BaseModel):
    role: str


class SetActiveRequest(BaseModel):
    is_active: bool


# --- SESSION 34: applying an approved change ---------------------------------


class AppliedChangeModel(BaseModel):
    """What applying produced. Names both versions, because the useful
    question afterwards is "what did the timetable become, and from what"."""

    change_request_id: str
    code: str
    schedule_version_id: str
    version_number: int
    superseded_version_id: str
    moved_sessions: int


# --- SESSION 38: availability ------------------------------------------------


class AvailabilityModel(BaseModel):
    """When an owner cannot be used, for one period.

    `has_declared` is not "is available every hour". Absence of a row means
    available, so a calendar nobody has filled in and a calendar somebody
    filled in as fully free look identical in the slot list and are different
    facts — the analyser reports the first as a diagnostic.
    """

    owner_kind: str
    owner_id: str
    unavailable_slot_ids: list[str] = []
    has_declared: bool = False
    reason: str = ""


class SetAvailabilityRequest(BaseModel):
    """The whole set of blocked slots, not a change to it.

    A per-slot toggle would let a dropped request leave somebody blocked on an
    hour they had just cleared, with neither side aware.
    """

    academic_period_id: str
    unavailable_slot_ids: list[str] = []
    reason: str = ""


# --- SESSION 40: dividing a long class ---------------------------------------


class SplitSessionRequest(BaseModel):
    """How many hours the first meeting keeps. The rest become the second."""

    first_part_slots: int


class SplitResultModel(BaseModel):
    original_session_id: str
    new_session_id: str
    original_slots: int
    new_slots: int
