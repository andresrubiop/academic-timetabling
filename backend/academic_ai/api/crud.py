"""A generic CRUD router, built once and applied to every reference entity.

SESSION 14 lists twenty-odd nouns — institutions, faculties, departments,
programs, curricula, courses, teachers, students, campuses, buildings, rooms,
periods, offerings, sections, sessions, enrollments, constraints — and each
needs the same shape: list with search/filter/sort/pagination, read one,
create, update, retire. Hand-writing that shape twenty times is not twenty
small pieces of work, it is one piece of work copied twenty times, and a bug
fixed in one copy stays a bug in the other nineteen.

So there is exactly one implementation. Each entity supplies a `CrudConfig` —
the model, which columns are searchable, filterable, sortable, and how "delete"
should behave for this table — and `build_crud_router` does the rest.

Schemas are derived from the SQLAlchemy model, not hand-authored per entity.
`id`, timestamps and the actor-mixin columns are excluded from what a client
may send: `id` and the timestamps are the database's to assign, and
`created_by_user_id` / `updated_by_user_id` come from the authenticated
principal, never from the request body — a client-supplied actor column would
let anyone attribute a row to anyone.

Retire, not delete
===================

CLAUDE.md and DATABASE.md are explicit: academic records are retired, not
deleted, and SESSION 13 found a real bug in the one place this was not
enforced at the database level (a published schedule version could be
dropped outright). `CrudConfig.retire` follows the same rule at the API layer
for every reference table that has a lifecycle column: `DELETE` sets the
status to its retired value rather than removing the row, so "this room no
longer exists" and "this room never existed" stay distinguishable in every
FK that still points at it.

A handful of tables in this session are pure join rows with no lifecycle of
their own (`CourseOfferingProgram`, `SectionTeacher`, ...) — for those,
`retire=None` and `DELETE` is a real delete, because there is no state for
the row to retire *to*.
"""

import datetime as dt
import logging
import decimal
import enum
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy import Column, and_, asc, desc, false as sa_false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.security.rbac import Permission, Principal

log = logging.getLogger("academic_ai.api.crud")

__all__ = ["CrudConfig", "Page", "RetireConfig", "build_crud_router"]

#: Columns that are the database's or the auth layer's to set, never the
#: client's. Excluded from every generated Create/Update schema.
_SERVER_OWNED = frozenset(
    {"id", "created_at", "updated_at", "created_by_user_id", "updated_by_user_id"}
)

#: Cap, independent of what a caller asks for. An unbounded page size is a way
#: to make one request do the work a scan should have done, on a host with
#: other tenants sharing the same database.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def _python_type(column: Column) -> Any:
    """The type a Pydantic field should validate this column as.

    Enums are read off the column's own Python type when SQLAlchemy exposes
    one (our `pg_enum` columns do), which is what makes an invalid status
    value a 422 instead of a 500 from a database CHECK.
    """
    try:
        py_type = column.type.python_type
    except NotImplementedError:
        return Any  # JSONB and similar: validated by the database, not here

    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        return py_type
    if py_type is decimal.Decimal:
        return float
    return py_type


def _field_default(column: Column) -> Any:
    if column.nullable:
        return None
    if column.default is not None or column.server_default is not None:
        return None  # still optional in the schema; the DB supplies the value
    return ...  # required


def _schema_from_columns(
    name: str, columns: Sequence[Column], *, optional: bool = False
) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for column in columns:
        if column.name in _SERVER_OWNED:
            continue
        py_type = _python_type(column)
        nullable_type = py_type | None if column.nullable else py_type
        default = None if optional else _field_default(column)
        fields[column.name] = (nullable_type if optional else py_type, default)
    return create_model(  # type: ignore[call-overload, no-any-return]
        name, __config__=ConfigDict(extra="forbid"), **fields
    )


def _read_schema(name: str, columns: Sequence[Column]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for column in columns:
        py_type = _python_type(column)
        fields[column.name] = (py_type | None if column.nullable else py_type, ...)
    return create_model(name, __config__=ConfigDict(from_attributes=True), **fields)


class Page(BaseModel):
    """One page of results, and enough about the whole set to build a pager.

    ``total`` is a second query. Cheap next to the list query for any table
    this project has, and a page that cannot say how many pages exist is a
    pager with no last button.
    """

    items: list[Any]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


@dataclass(frozen=True, slots=True)
class RetireConfig:
    """How "delete" retires a row instead of removing it."""

    field: str
    retired_value: object
    #: The value that means "not retired", so a retire on an already-retired
    #: row can say so instead of silently succeeding a second time.
    active_values: frozenset[object] = frozenset()


@dataclass(frozen=True, slots=True)
class ScopeRule:
    """Which faculties or programmes own a row of this entity.

    **A row's owners are a set.** That is the shape the rule is built around,
    because the hardest case genuinely is one: a `CourseOffering` serves every
    programme listed against it in `course_offering_program`, and a shared or
    common-core course serves several. Direct and one-hop ownership are then
    just the cases where the set happens to hold at most one — rather than
    three separate mechanisms that have to agree.

    The three shapes, composed from the same three fields:

    * **Direct** — `column` holds the owner. `Department.faculty_id`.
    * **One hop** — `column` is a foreign key to `parent`, which holds the
      owner in `parent_column`. `Course.department_id` -> `Department.faculty_id`.
    * **Shared** — `column` (optionally after the hop) identifies a
      `CourseOffering`, and `link` maps it to every programme it serves.
      `Section.course_offering_id`; `ClassSession.section_id` hops through
      `Section` first.

    Membership is **any overlap, never all**. A course shared by three
    programmes is the business of all three coordinators; requiring all three
    would mean only an institution-wide role could touch a shared course,
    which is exactly the course most likely to need a programme's own
    coordinator to look at it. It is the same rule approval routing uses, for
    the same reason.
    """

    #: The column on this model: the owner, the FK to `parent`, or the link key.
    column: str
    #: What the owner ids point at.
    kind: str  # "faculty" | "program"
    #: Set for a one-hop rule: the model `column` points at.
    parent: Any | None = None
    #: The column on `parent` holding the owner, or the link key.
    parent_column: str | None = None
    #: Set for a shared rule: the link table mapping an offering to programmes.
    link: Any | None = None
    #: The link table's column pointing back at the offering.
    link_key: str | None = None
    #: The link table's column holding the owner.
    link_owner: str | None = None

    def _key_of(self, session: Session, row) -> Any | None:
        """The value `column` resolves to, after any parent hop."""
        value = getattr(row, self.column, None)
        if value is None or self.parent is None:
            return value
        parent_row = session.get(self.parent, value)
        if parent_row is None:  # pragma: no cover - a foreign key guarantees it
            return None
        return getattr(parent_row, self.parent_column, None)

    def owners_of(self, session: Session, row) -> tuple[Any, ...]:
        """Every faculty or programme that owns this row.

        Empty means **owned by nobody**, which is a real answer rather than a
        failure: a `Teacher` with no department belongs to no faculty, and an
        offering serving no programme is a data problem. Such rows stay
        visible to any permission-holder — hiding them would put them out of
        reach of everyone but an institution-wide role, including whoever has
        to fix them.
        """
        key = self._key_of(session, row)
        if key is None:
            return ()
        if self.link is None:
            return (key,)
        return tuple(
            session.scalars(
                select(getattr(self.link, self.link_owner)).where(
                    getattr(self.link, self.link_key) == key
                )
            ).all()
        )

    def covers(self, jurisdiction, owners) -> bool:
        """Any overlap. An unowned row is covered by everybody."""
        owners = tuple(owners)
        if not owners:
            return True
        if self.kind == "faculty":
            return any(jurisdiction.covers_faculty(str(o)) for o in owners)
        return jurisdiction.covers_any_program([str(o) for o in owners])

    def restrict(self, stmt, model, jurisdiction):
        """Narrow a list query to what this jurisdiction reaches."""
        allowed = (
            jurisdiction.faculty_ids
            if self.kind == "faculty"
            else jurisdiction.program_ids
        )
        column = getattr(model, self.column)
        if not allowed:
            # Deny by default. Unowned rows are the exception, and only where
            # the column itself can be null -- for a hop or a link the row is
            # unowned because the *far end* is empty, which no `IS NULL` here
            # can express, so those are excluded rather than guessed at.
            if self.parent is None and self.link is None:
                return stmt.where(column.is_(None))
            return stmt.where(sa_false())

        ids = [uuid.UUID(a) for a in allowed]
        if self.link is not None:
            keys = select(getattr(self.link, self.link_key)).where(
                getattr(self.link, self.link_owner).in_(ids)
            )
            if self.parent is not None:
                keys = select(self.parent.id).where(
                    getattr(self.parent, self.parent_column).in_(keys)
                )
            return stmt.where(or_(column.is_(None), column.in_(keys)))

        if self.parent is None:
            return stmt.where(or_(column.is_(None), column.in_(ids)))

        owners = select(self.parent.id).where(
            getattr(self.parent, self.parent_column).in_(ids)
        )
        return stmt.where(or_(column.is_(None), column.in_(owners)))


@dataclass(frozen=True, slots=True)
class CrudConfig:
    """What one entity needs from the generic router."""

    model: type
    prefix: str
    tags: tuple[str, ...]
    #: The permission for every operation. A finer split (read vs write) is
    #: not needed yet: every entity here is reference data one administrative
    #: role maintains, and `require()` already refuses a non-holder before the
    #: handler runs.
    permission: Permission
    #: String/text columns `q` searches, case-insensitively, as substrings.
    search_fields: tuple[str, ...] = ()
    #: Columns a caller may filter on by exact match, via `?field=value`.
    filter_fields: tuple[str, ...] = ()
    #: Columns a caller may sort on, via `?sort=field` or `?sort=-field`.
    sortable_fields: tuple[str, ...] = ("id",)
    default_sort: str = "id"
    #: None means DELETE is a real delete — only correct for join rows with
    #: no lifecycle of their own.
    retire: RetireConfig | None = None
    #: Extra validation beyond what the column types already give for free,
    #: e.g. "end_date must be after start_date". Raises `ValueError`.
    validate_create: Callable[[dict], None] | None = None
    validate_update: Callable[[dict], None] | None = None
    #: Friendlier text for a unique-constraint violation than psycopg's raw
    #: message, which names the constraint, not the field, and is not
    #: something an administrator filling in a form should have to parse.
    conflict_message: str = "a record with these values already exists"
    #: How a row of this entity is owned, for row-level authorisation.
    #:
    #: `None` means **institution-wide by decision**, not by omission: every
    #: entity in `academic.py` states one or the other, so a new entity added
    #: without thinking about it is a visible blank rather than a silent
    #: default. See SECURITY.md for which entities are which and why.
    scope: ScopeRule | None = None


def _coerce_filter_value(column: Column, key: str, value: str) -> Any:
    """A query-string value, typed to match the column it filters.

    Booleans and enums need their own reading: `bool("false")` is truthy in
    Python, and an enum column needs its *value* looked up, not its
    constructor called on a bare string.
    """
    py_type = _python_type(column)
    if py_type is Any:
        return value
    if py_type is bool:
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{value!r} is not a valid value for {key}: expected true/false",
        )
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        try:
            return py_type(value)
        except ValueError:
            allowed = ", ".join(m.value for m in py_type)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{value!r} is not a valid {key}; allowed: {allowed}",
            ) from None
    try:
        return py_type(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{value!r} is not a valid value for {key}",
        ) from None


def build_crud_router(config: CrudConfig) -> APIRouter:
    model = config.model
    columns = list(model.__table__.columns)
    by_name = {c.name: c for c in columns}
    entity = model.__name__

    CreateSchema = _schema_from_columns(f"{entity}Create", columns)
    UpdateSchema = _schema_from_columns(f"{entity}Update", columns, optional=True)
    ReadSchema = _read_schema(f"{entity}Read", columns)

    router = APIRouter(prefix=config.prefix, tags=list(config.tags))
    auth = require(config.permission)

    def _get_or_404(session: Session, row_id: uuid.UUID):
        row = session.get(model, row_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{entity} {row_id} not found",
            )
        return row

    def _refuse_out_of_scope(session: Session, principal: Principal, row) -> None:
        """Refuse a row this principal has no authority over.

        404, not 403. A 403 confirms the row exists, which tells a coordinator
        the id of a record in a faculty they cannot see — and ids are guessable
        in a way the contents are not. The caller learns exactly what they
        would have learned had the row not existed.
        """
        if config.scope is None:
            return
        if config.scope.covers(
            principal.jurisdiction, config.scope.owners_of(session, row)
        ):
            return
        log.info(
            "row refused for scope",
            extra={"entity": entity, "user_id": principal.user_id},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{entity} {getattr(row, 'id', '')} not found",
        )

    def _refuse_scope_value(session: Session, principal: Principal, value) -> None:
        """Refuse a *proposed* owner outside this principal's authority.

        403 rather than 404 here: the caller supplied the id, so nothing is
        disclosed by saying they may not use it, and "not found" for a faculty
        they can see the name of would be actively confusing.
        """
        if config.scope is None or value is None:
            return
        # The payload names whatever `column` names -- a department for a
        # course, an offering for a section -- not the owner. Resolving it the
        # same way a row is resolved means a create that puts a section in
        # another programme's offering is refused, rather than accepted and
        # then invisible to the person who created it.
        owners = config.scope.owners_of(
            session, SimpleNamespace(**{config.scope.column: value})
        )
        if config.scope.covers(principal.jurisdiction, owners):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"you have no authority over the {config.scope.kind} this "
                f"{entity} would belong to"
            ),
        )

    def _restrict_to_scope(stmt, principal: Principal):
        """Narrow a list query to what this principal may see.

        Filtering rather than refusing: a list that 404s because one row is
        out of reach is useless, and a coordinator listing programmes wants
        theirs. An empty jurisdiction yields no rows, which is the deny-by-
        default rule doing its job rather than a bug.
        """
        if config.scope is None:
            return stmt
        jurisdiction = principal.jurisdiction
        if jurisdiction.is_institution_wide:
            return stmt
        return config.scope.restrict(stmt, model, jurisdiction)

    def _apply_filters(stmt, filters: dict[str, str]):
        clauses = []
        for key, value in filters.items():
            if key not in config.filter_fields:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{entity} cannot be filtered by {key!r}",
                )
            column = by_name[key]
            typed = _coerce_filter_value(column, key, value)
            clauses.append(column == typed)
        return stmt.where(and_(*clauses)) if clauses else stmt

    def _apply_search(stmt, q: str | None):
        if not q or not config.search_fields:
            return stmt
        needle = f"%{q}%"
        clauses = [
            func.lower(by_name[field]).like(func.lower(needle))
            for field in config.search_fields
        ]
        return stmt.where(or_(*clauses))

    def _apply_sort(stmt, sort: str | None):
        raw = sort or config.default_sort
        field_name = raw.lstrip("-")
        if field_name not in config.sortable_fields:
            allowed = ", ".join(config.sortable_fields)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{entity} cannot be sorted by {field_name!r}; allowed: {allowed}",
            )
        column = by_name[field_name]
        direction = desc if raw.startswith("-") else asc
        # Secondary key on id: without it, rows tied on the sort column can
        # swap pages between two requests to the same page number, and a
        # coordinator paging through five hundred rooms sees one twice and
        # never sees another.
        return stmt.order_by(direction(column), asc(by_name["id"]))

    _RESERVED_QUERY_PARAMS = frozenset({"q", "sort", "page", "page_size"})

    @router.get("", response_model=Page)
    def list_(
        request: Request,
        q: str | None = Query(default=None, description="search"),
        sort: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        session: Session = Depends(get_session),
        principal: Principal = Depends(auth),
    ) -> Page:
        # The identity is used: `_restrict_to_scope` narrows the query to what
        # this principal has authority over. Until SESSION 24 this line read
        # `del principal`, and every holder of the permission saw every row.
        # Filters ride on whatever query params are left over, rather than a
        # named parameter per filterable column: the set of filterable
        # columns differs per entity, and FastAPI has no way to generate a
        # different function signature per router at import time.
        filters = {
            key: value
            for key, value in request.query_params.items()
            if key not in _RESERVED_QUERY_PARAMS
        }
        stmt = _restrict_to_scope(select(model), principal)
        stmt = _apply_filters(stmt, filters)
        stmt = _apply_search(stmt, q)
        stmt = _apply_sort(stmt, sort)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = session.scalar(count_stmt) or 0

        rows = session.scalars(
            stmt.offset((page - 1) * page_size).limit(page_size)
        ).all()
        return Page(
            items=[ReadSchema.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    @router.get("/{row_id}", response_model=ReadSchema)
    def get(
        row_id: uuid.UUID,
        session: Session = Depends(get_session),
        principal: Principal = Depends(auth),
    ):
        row = _get_or_404(session, row_id)
        _refuse_out_of_scope(session, principal, row)
        return row

    @router.post("", response_model=ReadSchema, status_code=status.HTTP_201_CREATED)
    def create(
        payload: CreateSchema,  # type: ignore[valid-type]
        session: Session = Depends(get_session),
        principal: Principal = Depends(auth),
    ):
        _refuse_scope_value(
            session,
            principal,
            getattr(payload, config.scope.column, None) if config.scope else None,
        )
        # `exclude_unset` so a field the client omitted stays omitted. Dumping
        # everything would pass `extra=None` explicitly, which *overrides* the
        # column's own default instead of letting it apply -- the row then
        # violates its NOT NULL, or arrives with None where the database would
        # have put `{}`.
        data = payload.model_dump(exclude_unset=True)
        if config.validate_create is not None:
            try:
                config.validate_create(data)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
                ) from exc

        row = model(**data)
        if hasattr(row, "created_by_user_id"):
            row.created_by_user_id = uuid.UUID(principal.user_id)
        if hasattr(row, "updated_by_user_id"):
            row.updated_by_user_id = uuid.UUID(principal.user_id)
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            # A friendlier message than psycopg's, which names the constraint
            # and not the field, and is not something an administrator filling
            # in a form should have to parse.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=config.conflict_message
            ) from exc
        # Read the row back. Server-side defaults -- `status`, `extra`, the
        # timestamps -- are computed by PostgreSQL, so without this the
        # response carries None for every column the client did not send and
        # the database filled in.
        session.refresh(row)
        return row

    @router.patch("/{row_id}", response_model=ReadSchema)
    def update(
        row_id: uuid.UUID,
        payload: UpdateSchema,  # type: ignore[valid-type]
        session: Session = Depends(get_session),
        principal: Principal = Depends(auth),
    ):
        row = _get_or_404(session, row_id)
        _refuse_out_of_scope(session, principal, row)
        data = payload.model_dump(exclude_unset=True)
        if config.scope is not None and config.scope.column in data:
            # Moving a row *out* of your own jurisdiction is a way to give it
            # away and lose sight of it; moving one *in* is a way to take it.
            # Both are refused by checking the new owner as well as the old.
            _refuse_scope_value(session, principal, data[config.scope.column])
        if config.validate_update is not None:
            try:
                config.validate_update(data)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
                ) from exc

        for key, value in data.items():
            setattr(row, key, value)
        if hasattr(row, "updated_by_user_id"):
            row.updated_by_user_id = uuid.UUID(principal.user_id)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=config.conflict_message
            ) from exc
        session.refresh(row)
        return row

    @router.delete("/{row_id}", response_model=ReadSchema)
    def delete(
        row_id: uuid.UUID,
        session: Session = Depends(get_session),
        principal: Principal = Depends(auth),
    ):
        """Retire, or truly delete for a config with no lifecycle to retire to.

        Returns the updated resource rather than 204: a caller retiring a row
        through this generic endpoint has no other way to see the status it
        ended up in, and a second round trip just to read it back is wasted on
        a shared host.
        """
        row = _get_or_404(session, row_id)
        _refuse_out_of_scope(session, principal, row)
        retire = config.retire
        if retire is None:
            session.delete(row)
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                # A row still referenced elsewhere (a class session that has
                # already been scheduled, say) is protected by a foreign key
                # at the database level regardless of what this layer does.
                # Reported as a 409 rather than the 500 an unhandled
                # IntegrityError would otherwise produce.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{entity} {row_id} is still referenced elsewhere and cannot be deleted",
                ) from exc
            return row

        current = getattr(row, retire.field)
        if retire.active_values and current not in retire.active_values:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{entity} {row_id} is already {current}",
            )
        setattr(row, retire.field, retire.retired_value)
        if hasattr(row, "updated_by_user_id"):
            row.updated_by_user_id = uuid.UUID(principal.user_id)
        session.flush()
        session.refresh(row)
        return row

    return router
