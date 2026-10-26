"""Upload, preview, confirm.

The flow the playbook names, with the file held between the two steps so that
**confirm re-derives the plan rather than replaying the preview**. Between
looking at a preview and pressing confirm, somebody else may have created the
very room the file is about; applying a stored plan would insert a duplicate
the preview promised was a creation. So the upload is kept for a short while
and confirm parses and validates it again.

The file is held in Redis with a TTL rather than on disk: the API has a
read-only root filesystem (SESSION 8), any replica must be able to serve the
confirm for an upload another replica received, and an upload nobody confirms
should expire by itself rather than accumulating.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid

import redis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    ImportDiagnosticModel,
    ImportPreviewResponse,
    ImportResultResponse,
    ImportSpecModel,
    RowPlanModel,
)
from academic_ai.application.imports import (
    ImportError_,
    ImportPreview,
    OnError,
    apply_plan,
    plan_import,
)
from academic_ai.domain.imports import IMPORT_SPECS, spec_for
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/imports", tags=["imports"])
log = logging.getLogger("academic_ai.api.imports")

#: How long an unconfirmed upload survives. Long enough for an operator to
#: read a four-hundred-row report and decide; short enough that a forgotten
#: upload is not still sitting in memory tomorrow.
_UPLOAD_TTL_SECONDS = 30 * 60

#: Refused before reading. 20k rows of plausible academic data is a few
#: megabytes; anything far past that is a mistake or a way to spend this
#: shared host's memory.
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def _redis() -> redis.Redis:
    from academic_ai.api.deps import _redis_client

    return _redis_client()


def _diag(d) -> ImportDiagnosticModel:
    return ImportDiagnosticModel(
        row_number=d.row_number,
        severity=d.severity.value,
        message=d.message,
        column=d.column,
        value=d.value,
        describe=d.describe(),
    )


def _to_preview_response(
    preview: ImportPreview, token: str | None
) -> ImportPreviewResponse:
    return ImportPreviewResponse(
        token=token,
        spec_code=preview.spec_code,
        entity=preview.entity,
        filename=preview.filename,
        headings=list(preview.headings),
        summary=preview.summary(),
        is_importable=preview.is_importable,
        creates=preview.creates,
        updates=preview.updates,
        unchanged=preview.unchanged,
        rejected=preview.rejected,
        file_diagnostics=[_diag(d) for d in preview.file_diagnostics],
        rows=[
            RowPlanModel(
                row_number=p.row_number,
                outcome=p.outcome.value,
                diagnostics=[_diag(d) for d in p.diagnostics],
            )
            # Capped: a four-hundred-row report is readable, a twenty-thousand
            # row one is a download. The counts above stay exact either way,
            # and every rejected row is kept because those are the ones the
            # operator has to act on.
            for p in _rows_worth_showing(preview)
        ],
        rows_truncated=len(preview.plans) > len(_rows_worth_showing(preview)),
    )


def _rows_worth_showing(preview: ImportPreview, limit: int = 500) -> list:
    rejected = [p for p in preview.plans if p.is_rejected]
    if len(rejected) >= limit:
        return rejected[:limit]
    others = [p for p in preview.plans if not p.is_rejected]
    return rejected + others[: limit - len(rejected)]


@router.get("/specs", response_model=list[ImportSpecModel])
def list_specs(
    principal: Principal = Depends(require(Permission.MANAGE_INSTITUTION)),
) -> list[ImportSpecModel]:
    """What can be imported, and what each file must contain.

    Served so an operator can be shown the expected columns *before* they
    build a file, rather than discovering them from rejection messages.
    """
    del principal
    return [
        ImportSpecModel(
            code=spec.code,
            entity=spec.entity,
            description=spec.description,
            required_columns=list(spec.required_headings),
            optional_columns=[
                c.heading for c in spec.columns if not c.required
            ],
            natural_key=list(spec.natural_key),
        )
        for spec in sorted(IMPORT_SPECS.values(), key=lambda s: s.code)
    ]


@router.post("/{spec_code}/preview", response_model=ImportPreviewResponse)
async def preview(
    spec_code: str,
    file: UploadFile = File(...),
    institution_id: str | None = Form(default=None),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_INSTITUTION)),
) -> ImportPreviewResponse:
    """Parse and validate an upload. Writes nothing to the database."""
    try:
        spec_for(spec_code)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB; "
                "split it and import in parts"
            ),
        )

    scope = {}
    if institution_id:
        try:
            scope["institution_id"] = uuid.UUID(institution_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="institution_id is not a valid identifier",
            ) from None

    try:
        result = plan_import(
            session, spec_code, data, file.filename or "upload", scope=scope
        )
    except ImportError_ as exc:
        # 422: the request is well-formed and the *file* is not.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    token = None
    if result.is_importable:
        token = uuid.uuid4().hex
        try:
            _redis().set(
                f"academic_ai:import:{token}",
                json.dumps(
                    {
                        "spec_code": spec_code,
                        "filename": file.filename or "upload",
                        "data": base64.b64encode(data).decode(),
                        "institution_id": institution_id,
                        "uploaded_by": principal.user_id,
                    }
                ),
                ex=_UPLOAD_TTL_SECONDS,
            )
        except redis.RedisError as exc:  # pragma: no cover - environment
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the upload could not be held for confirmation; try again",
            ) from exc

    log.info(
        "import previewed",
        extra={
            "spec": spec_code,
            "user_id": principal.user_id,
            "rows": len(result.plans),
            "rejected": result.rejected,
        },
    )
    return _to_preview_response(result, token)


@router.post("/{spec_code}/confirm", response_model=ImportResultResponse)
def confirm(
    spec_code: str,
    token: str = Form(...),
    on_error: str = Form(default=OnError.REJECT_FILE),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_INSTITUTION)),
) -> ImportResultResponse:
    """Apply a previewed upload, re-validating it first.

    The plan is re-derived rather than replayed, so a row that was a creation
    when the operator looked is re-checked against the database as it is now.
    The response reports what actually happened, which may differ from the
    preview -- and saying so is the point.
    """
    if on_error not in (OnError.REJECT_FILE, OnError.SKIP_ROWS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"on_error must be {OnError.REJECT_FILE} or {OnError.SKIP_ROWS}",
        )

    try:
        raw = _redis().get(f"academic_ai:import:{token}")
    except redis.RedisError as exc:  # pragma: no cover - environment
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the upload store is unreachable",
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "this upload has expired or was already used; upload the file "
                "again to get a fresh preview"
            ),
        )

    stored = json.loads(raw)
    if stored["spec_code"] != spec_code:
        # A token minted for a teachers file must not be confirmable as rooms.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="this upload was previewed as a different kind of import",
        )

    scope = {}
    if stored.get("institution_id"):
        scope["institution_id"] = uuid.UUID(stored["institution_id"])

    try:
        replanned = plan_import(
            session,
            spec_code,
            base64.b64decode(stored["data"]),
            stored["filename"],
            scope=scope,
        )
    except ImportError_ as exc:  # pragma: no cover - it parsed once already
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    result = apply_plan(
        session,
        replanned,
        actor_user_id=principal.user_id,
        on_error=on_error,
    )

    if result.committed:
        # One use per token. Confirming twice would import the file twice --
        # harmless for rows matched by natural key, but the second run would
        # report "0 created, 400 unchanged" and leave the operator wondering
        # which run was real.
        try:
            _redis().delete(f"academic_ai:import:{token}")
        except redis.RedisError:  # pragma: no cover - environment
            log.warning("could not clear import token", extra={"token": token})

    log.info(
        "import confirmed",
        extra={
            "spec": spec_code,
            "user_id": principal.user_id,
            "committed": result.committed,
            "rows_created": result.created,
            "rows_updated": result.updated,
        },
    )
    return ImportResultResponse(
        committed=result.committed,
        summary=result.summary(),
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        skipped=result.skipped,
        refusal=result.refusal,
        preview=_to_preview_response(replanned, None),
    )
