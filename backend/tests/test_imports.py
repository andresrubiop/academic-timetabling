"""SESSION 15: CSV/XLSX imports, and the ways a bad file could get through.

The playbook's requirement is *never silently import malformed rows*, and the
word doing the work is **silently**. Rejecting a four-hundred-row file over
three bad rows wastes the 397; importing the 397 without saying which three
were dropped leaves the registrar guessing. Neither is acceptable, so the
default refuses the whole file and says how many rows were bad, and skipping
them is available but has to be asked for -- and reports every skip.

The other half is idempotency. A registrar who re-runs last week's export
should see "unchanged", not four hundred duplicates, because that is the only
thing that makes an import usable more than once.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

# The confirm step holds the uploaded file in Redis so that a preview and its
# confirmation can be served by different API replicas. Point it at the same
# loopback instance the queue tests use.
os.environ.setdefault(
    "REDIS_URL", "redis://:dev_only_not_a_secret@127.0.0.1:6382/2"
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from academic_ai.api.app import create_app
from academic_ai.api.deps import get_session, get_settings
from academic_ai.application.imports import (
    ImportError_,
    OnError,
    apply_plan,
    plan_import,
)
from academic_ai.domain.enums import UserRole
from academic_ai.domain.imports import (
    IMPORT_SPECS,
    ColumnSpec,
    ColumnType,
    RowOutcome,
    coerce_value,
    spec_for,
)
from academic_ai.infrastructure.db.models import Room, Teacher, User, UserCredential
from academic_ai.infrastructure.imports import ParseError, parse_bytes
from academic_ai.security.passwords import hash_password
from academic_ai.security.settings import SecuritySettings

PASSWORD = "a perfectly reasonable passphrase"


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


class TestReadingFiles:
    def test_a_comma_file_and_a_semicolon_file_both_parse(self) -> None:
        """Excel in a Spanish locale exports semicolons. Requiring commas
        means requiring the operator to reformat before importing."""
        comma = parse_bytes(_csv("Código,Nombre\nA1,Aula 1\n"), "a.csv")
        semi = parse_bytes(_csv("Código;Nombre\nA1;Aula 1\n"), "a.csv")
        assert comma.headings == semi.headings == ("Código", "Nombre")
        assert comma.rows == semi.rows

    def test_a_utf8_bom_does_not_corrupt_the_first_heading(self) -> None:
        """A CSV exported from Excel on Windows carries a BOM, and without
        handling it the first heading arrives invisibly mangled and matches
        nothing."""
        data = "﻿Código,Nombre\nA1,Aula 1\n".encode("utf-8")
        parsed = parse_bytes(data, "a.csv")
        assert parsed.headings[0] == "Código"

    def test_row_numbers_match_the_spreadsheet_gutter(self) -> None:
        """1-based and counting the header, so "row 3" means what the operator
        sees in row 3."""
        parsed = parse_bytes(_csv("Código\nA1\nA2\n"), "a.csv")
        assert [n for n, _ in parsed.rows] == [2, 3]

    def test_trailing_blank_rows_are_not_rows(self) -> None:
        parsed = parse_bytes(_csv("Código\nA1\n\n\n"), "a.csv")
        assert parsed.row_count == 1

    def test_a_duplicate_heading_is_refused(self) -> None:
        """Two columns with one name make a cell ambiguous, and picking one
        silently would import whichever the reader happened to hit last."""
        with pytest.raises(ParseError, match="more than once"):
            parse_bytes(_csv("Código,Código\nA1,A2\n"), "a.csv")

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(ParseError, match="empty"):
            parse_bytes(b"", "a.csv")

    def test_an_unknown_extension_is_refused_rather_than_guessed(self) -> None:
        """An operator who renamed an .xlsx to .csv has made a mistake worth
        being told about; guessing would import it as one enormous row."""
        with pytest.raises(ParseError, match="cannot tell"):
            parse_bytes(_csv("a,b\n1,2\n"), "data.dat")

    def test_an_xlsx_file_parses(self) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Código", "Nombre", "Capacidad"])
        sheet.append(["A1", "Aula 1", 30])
        buffer = io.BytesIO()
        workbook.save(buffer)

        parsed = parse_bytes(buffer.getvalue(), "aulas.xlsx")
        assert parsed.headings == ("Código", "Nombre", "Capacidad")
        assert parsed.rows[0][1]["Capacidad"] == 30

    def test_a_corrupt_xlsx_is_refused_with_a_readable_message(self) -> None:
        with pytest.raises(ParseError, match="readable .xlsx"):
            parse_bytes(b"not a workbook at all", "aulas.xlsx")


# ---------------------------------------------------------------------------
# Cell coercion
# ---------------------------------------------------------------------------


class TestCoercion:
    def test_a_spreadsheet_integer_arrives_as_thirty_not_thirty_point_zero(
        self,
    ) -> None:
        """Spreadsheets hand back 30.0 for a cell showing 30, and int() on
        that string fails on data that is perfectly fine."""
        column = ColumnSpec("Capacidad", "capacity", ColumnType.INTEGER)
        assert coerce_value(column, "30.0", 2) == (30, None)

    def test_spanish_booleans_are_understood(self) -> None:
        column = ColumnSpec("Obligatoria", "is_mandatory", ColumnType.BOOLEAN)
        assert coerce_value(column, "Sí", 2)[0] is True
        assert coerce_value(column, "no", 2)[0] is False

    def test_both_date_conventions_parse(self) -> None:
        column = ColumnSpec("Fecha", "admission_date", ColumnType.DATE)
        iso = coerce_value(column, "2026-03-01", 2)[0]
        local = coerce_value(column, "01/03/2026", 2)[0]
        assert iso == local

    def test_a_bad_value_yields_a_diagnostic_and_no_value(self) -> None:
        """There is never a value *and* an error, so a caller cannot
        accidentally use something that failed to parse."""
        column = ColumnSpec("Capacidad", "capacity", ColumnType.INTEGER)
        value, diagnostic = coerce_value(column, "treinta", 7)
        assert value is None
        assert diagnostic is not None
        assert diagnostic.row_number == 7
        assert "whole number" in diagnostic.message

    def test_an_invalid_enum_lists_what_is_accepted(self) -> None:
        column = ColumnSpec(
            "Tipo", "room_type", ColumnType.ENUM, choices=("CLASSROOM", "LAB")
        )
        _, diagnostic = coerce_value(column, "aula", 3)
        assert "CLASSROOM" in diagnostic.message

    def test_a_missing_required_cell_is_an_error(self) -> None:
        column = ColumnSpec("Código", "code", required=True)
        _, diagnostic = coerce_value(column, "  ", 4)
        assert diagnostic.is_error

    def test_a_missing_optional_cell_is_neither_value_nor_error(self) -> None:
        column = ColumnSpec("Email", "email")
        assert coerce_value(column, "", 4) == (None, None)

    def test_a_diagnostic_names_the_heading_the_operator_typed(self) -> None:
        """An error naming `department_id` about a file whose heading is
        `Departamento` is one the person holding the file cannot act on."""
        column = ColumnSpec(
            "Departamento", "department_code", ColumnType.REFERENCE, required=True
        )
        _, diagnostic = coerce_value(column, "", 5)
        assert diagnostic.column == "Departamento"
        assert "Departamento" in diagnostic.describe()


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


class TestSpecs:
    def test_all_seven_playbook_entities_are_importable(self) -> None:
        assert set(IMPORT_SPECS) == {
            "teachers", "students", "courses", "enrollments",
            "rooms", "offerings", "availability",
        }

    def test_spanish_and_english_headings_both_match(self) -> None:
        spec = spec_for("rooms")
        assert spec.column_for("Capacidad") is spec.column_for("capacity")

    def test_a_missing_required_column_is_reported_before_any_row(self) -> None:
        """A file missing its `Código` column produces four hundred identical
        row errors otherwise, which buries the one fact that matters."""
        spec = spec_for("rooms")
        assert "Código" in spec.missing_headings(["Nombre", "Capacidad"])

    def test_every_spec_has_a_natural_key(self) -> None:
        """Without one there is no way to tell a re-import from a duplicate."""
        for spec in IMPORT_SPECS.values():
            assert spec.natural_key, f"{spec.code} has no natural key"


# ---------------------------------------------------------------------------
# Planning against a real database
# ---------------------------------------------------------------------------


class TestPlanning:
    def test_a_clean_file_plans_creates(self, session: Session, world) -> None:
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"N1,Aula Nueva 1,{world.building.code},CLASSROOM,30\n"
            f"N2,Aula Nueva 2,{world.building.code},COMPUTER_LAB,25\n"
        )
        preview = plan_import(session, "rooms", data, "aulas.csv")
        assert preview.is_importable
        assert preview.creates == 2
        assert preview.rejected == 0
        assert "2 new" in preview.summary()

    def test_an_unresolvable_reference_names_the_code_the_operator_typed(
        self, session: Session, world
    ) -> None:
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            "N1,Aula,NO-EXISTE,CLASSROOM,30\n"
        )
        preview = plan_import(session, "rooms", data, "aulas.csv")
        assert preview.rejected == 1
        message = preview.plans[0].errors[0].describe()
        assert "NO-EXISTE" in message
        assert "building" in message

    def test_a_bad_row_does_not_reject_its_neighbours_at_plan_time(
        self, session: Session, world
    ) -> None:
        """Planning reports every row. What to do about the bad ones is the
        confirm step's decision, not the planner's."""
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"OK1,Buena,{world.building.code},CLASSROOM,30\n"
            f"BAD,Mala,{world.building.code},CLASSROOM,cero\n"
            f"OK2,Buena 2,{world.building.code},CLASSROOM,20\n"
        )
        preview = plan_import(session, "rooms", data, "aulas.csv")
        assert preview.creates == 2
        assert preview.rejected == 1
        assert preview.plans[1].row_number == 3

    def test_two_rows_with_the_same_identity_are_both_flagged(
        self, session: Session, world
    ) -> None:
        """Importing both would leave whichever came last silently winning."""
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"SAME,Primera,{world.building.code},CLASSROOM,30\n"
            f"SAME,Segunda,{world.building.code},CLASSROOM,40\n"
        )
        preview = plan_import(session, "rooms", data, "aulas.csv")
        assert preview.rejected == 1
        assert "duplicates row 2" in preview.plans[1].errors[0].message

    def test_a_missing_column_is_a_file_level_problem(
        self, session: Session, world
    ) -> None:
        preview = plan_import(
            session, "rooms", _csv("Código,Nombre\nA1,Aula\n"), "aulas.csv"
        )
        assert not preview.is_importable
        assert preview.file_diagnostics
        assert "Capacidad" in preview.file_diagnostics[0].message

    def test_an_unused_column_is_a_warning_not_a_refusal(
        self, session: Session, world
    ) -> None:
        """An export with an extra column is normal; refusing it would make
        the operator edit a file that is otherwise fine."""
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad,Notas\n"
            f"N9,Aula,{world.building.code},CLASSROOM,30,algo\n"
        )
        preview = plan_import(session, "rooms", data, "aulas.csv")
        assert preview.is_importable
        assert any(
            "will be ignored" in d.message for d in preview.file_diagnostics
        )

    def test_an_unreadable_file_raises_rather_than_previewing(
        self, session: Session, world
    ) -> None:
        with pytest.raises(ImportError_):
            plan_import(session, "rooms", b"", "aulas.csv")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    ROWS = "Código,Nombre,Edificio,Tipo,Capacidad\n{}"

    def _file(self, world, capacity: int = 30) -> bytes:
        return _csv(
            self.ROWS.format(
                f"IDEM,Aula Idempotente,{world.building.code},CLASSROOM,{capacity}\n"
            )
        )

    def test_reimporting_an_unchanged_file_reports_unchanged(
        self, session: Session, world
    ) -> None:
        """A registrar re-running last week's export should see "unchanged",
        not four hundred duplicates. It is the only thing that makes an import
        usable more than once."""
        first = plan_import(session, "rooms", self._file(world), "a.csv")
        apply_plan(session, first)

        second = plan_import(session, "rooms", self._file(world), "a.csv")
        assert second.creates == 0
        assert second.unchanged == 1
        assert second.updates == 0

    def test_a_changed_value_reports_an_update(
        self, session: Session, world
    ) -> None:
        apply_plan(session, plan_import(session, "rooms", self._file(world), "a.csv"))
        second = plan_import(
            session, "rooms", self._file(world, capacity=99), "a.csv"
        )
        assert second.updates == 1
        assert second.creates == 0

    def test_applying_twice_creates_one_row(
        self, session: Session, world
    ) -> None:
        apply_plan(session, plan_import(session, "rooms", self._file(world), "a.csv"))
        apply_plan(session, plan_import(session, "rooms", self._file(world), "a.csv"))
        rooms = session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "IDEM")
        ).all()
        assert len(rooms) == 1

    def test_an_import_does_not_reactivate_a_retired_row(
        self, session: Session, world
    ) -> None:
        """`status` is a create-only default. An import must not silently
        reactivate a room somebody retired, or reset a status a human set."""
        from academic_ai.domain.enums import LifecycleStatus

        apply_plan(session, plan_import(session, "rooms", self._file(world), "a.csv"))
        room = session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "IDEM")
        ).one()
        room.status = LifecycleStatus.RETIRED
        session.flush()

        apply_plan(
            session, plan_import(session, "rooms", self._file(world, 55), "a.csv")
        )
        session.refresh(room)
        assert room.status is LifecycleStatus.RETIRED
        assert room.capacity == 55, "the other fields still updated"


# ---------------------------------------------------------------------------
# Never silently importing malformed rows
# ---------------------------------------------------------------------------


class TestApplying:
    def _mixed(self, world) -> bytes:
        return _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"G1,Buena 1,{world.building.code},CLASSROOM,30\n"
            f"B1,Mala,{world.building.code},CLASSROOM,cero\n"
            f"G2,Buena 2,{world.building.code},CLASSROOM,20\n"
        )

    def test_the_default_refuses_the_whole_file_and_says_how_many_were_bad(
        self, session: Session, world
    ) -> None:
        preview = plan_import(session, "rooms", self._mixed(world), "a.csv")
        result = apply_plan(session, preview)

        assert not result.committed
        assert "1 of 3" in result.refusal
        assert session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "G1")
        ).first() is None, "a good row was written despite the refusal"

    def test_skipping_bad_rows_must_be_asked_for_and_is_reported(
        self, session: Session, world
    ) -> None:
        preview = plan_import(session, "rooms", self._mixed(world), "a.csv")
        result = apply_plan(session, preview, on_error=OnError.SKIP_ROWS)

        assert result.committed
        assert result.created == 2
        assert result.skipped == 1
        assert "1 skipped" in result.summary()

    def test_a_file_level_problem_refuses_regardless_of_on_error(
        self, session: Session, world
    ) -> None:
        """With no `Capacidad` column there is no sense in which any row is
        importable, so skipping bad rows cannot rescue it."""
        preview = plan_import(
            session, "rooms", _csv("Código,Nombre\nA1,Aula\n"), "a.csv"
        )
        result = apply_plan(session, preview, on_error=OnError.SKIP_ROWS)
        assert not result.committed

    def test_the_actor_is_recorded_on_created_rows(
        self, session: Session, world
    ) -> None:
        data = _csv(
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"ACT,Aula,{world.building.code},CLASSROOM,30\n"
        )
        apply_plan(
            session,
            plan_import(session, "rooms", data, "a.csv"),
            actor_user_id=str(world.admin.id),
        )
        room = session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "ACT")
        ).one()
        assert room.created_by_user_id == world.admin.id


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> SecuritySettings:
    return SecuritySettings(
        secret_key="t" * 64,
        environment="test",
        allowed_hosts=(),
        cors_origins=(),
        access_token_minutes=30,
        refresh_token_days=7,
    )


@pytest.fixture()
def importer(session: Session) -> User:
    user = User(
        email="importer@example.test", full_name="Importer", primary_role=UserRole.ADMIN
    )
    session.add(user)
    session.flush()
    session.add(
        UserCredential(app_user_id=user.id, password_hash=hash_password(PASSWORD))
    )
    session.flush()
    return user


@pytest.fixture()
def client(session: Session, settings: SecuritySettings):
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "importer@example.test", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestOverHttp:
    def test_the_specs_are_published_before_anybody_builds_a_file(
        self, client: TestClient, importer: User
    ) -> None:
        """So an operator can be shown the expected columns rather than
        discovering them from rejection messages."""
        token = _token(client)
        response = client.get("/api/v1/imports/specs", headers=_auth(token))
        assert response.status_code == 200
        codes = {s["code"] for s in response.json()}
        assert "rooms" in codes
        rooms = next(s for s in response.json() if s["code"] == "rooms")
        assert "Capacidad" in rooms["required_columns"]
        assert rooms["natural_key"]

    def test_preview_writes_nothing_and_returns_a_token(
        self, client: TestClient, importer: User, session: Session, world
    ) -> None:
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"H1,Aula HTTP,{world.building.code},CLASSROOM,30\n"
        ).encode()
        response = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["creates"] == 1
        assert body["token"], "no token to confirm with"
        assert session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "H1")
        ).first() is None, "preview wrote to the database"

    def test_preview_then_confirm_imports(
        self, client: TestClient, importer: User, session: Session, world
    ) -> None:
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"H2,Aula HTTP 2,{world.building.code},CLASSROOM,44\n"
        ).encode()
        preview = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        ).json()

        confirmed = client.post(
            "/api/v1/imports/rooms/confirm",
            data={"token": preview["token"]},
            headers=_auth(token),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["committed"] is True
        assert confirmed.json()["created"] == 1

        room = session.scalars(
            __import__("sqlalchemy").select(Room).where(Room.code == "H2")
        ).one()
        assert room.capacity == 44

    def test_a_token_cannot_be_reused(
        self, client: TestClient, importer: User, world
    ) -> None:
        """Confirming twice would report "0 created, 1 unchanged" the second
        time and leave the operator wondering which run was real."""
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"H3,Aula,{world.building.code},CLASSROOM,30\n"
        ).encode()
        preview = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        ).json()
        client.post(
            "/api/v1/imports/rooms/confirm",
            data={"token": preview["token"]},
            headers=_auth(token),
        )
        second = client.post(
            "/api/v1/imports/rooms/confirm",
            data={"token": preview["token"]},
            headers=_auth(token),
        )
        assert second.status_code == 404

    def test_a_token_for_one_kind_cannot_confirm_another(
        self, client: TestClient, importer: User, world
    ) -> None:
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"H4,Aula,{world.building.code},CLASSROOM,30\n"
        ).encode()
        preview = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        ).json()
        response = client.post(
            "/api/v1/imports/teachers/confirm",
            data={"token": preview["token"]},
            headers=_auth(token),
        )
        assert response.status_code == 422

    def test_an_unknown_spec_is_404(
        self, client: TestClient, importer: User
    ) -> None:
        token = _token(client)
        response = client.post(
            "/api/v1/imports/unicorns/preview",
            files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")},
            headers=_auth(token),
        )
        assert response.status_code == 404

    def test_a_bad_file_is_422_with_a_readable_reason(
        self, client: TestClient, importer: User
    ) -> None:
        token = _token(client)
        response = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("a.dat", b"whatever", "application/octet-stream")},
            headers=_auth(token),
        )
        assert response.status_code == 422
        assert "cannot tell" in response.json()["detail"]

    def test_a_file_with_bad_rows_gets_a_token_but_refuses_to_commit(
        self, client: TestClient, importer: User, world
    ) -> None:
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"R1,Buena,{world.building.code},CLASSROOM,30\n"
            f"R2,Mala,{world.building.code},CLASSROOM,cero\n"
        ).encode()
        preview = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        ).json()
        assert preview["rejected"] == 1
        # The rejected row is in the report, with its reason.
        rejected = [r for r in preview["rows"] if r["outcome"] == "REJECTED"]
        assert rejected and rejected[0]["diagnostics"]

        result = client.post(
            "/api/v1/imports/rooms/confirm",
            data={"token": preview["token"]},
            headers=_auth(token),
        ).json()
        assert result["committed"] is False
        assert "1 of 2" in result["refusal"]

    def test_skipping_bad_rows_is_available_over_http(
        self, client: TestClient, importer: User, world
    ) -> None:
        token = _token(client)
        data = (
            "Código,Nombre,Edificio,Tipo,Capacidad\n"
            f"S1,Buena,{world.building.code},CLASSROOM,30\n"
            f"S2,Mala,{world.building.code},CLASSROOM,cero\n"
        ).encode()
        preview = client.post(
            "/api/v1/imports/rooms/preview",
            files={"file": ("aulas.csv", data, "text/csv")},
            headers=_auth(token),
        ).json()
        result = client.post(
            "/api/v1/imports/rooms/confirm",
            data={"token": preview["token"], "on_error": "SKIP_ROWS"},
            headers=_auth(token),
        ).json()
        assert result["committed"] is True
        assert result["created"] == 1
        assert result["skipped"] == 1

    def test_a_student_cannot_import_anything(
        self, client: TestClient, session: Session
    ) -> None:
        student = User(
            email="nope@example.test", full_name="S", primary_role=UserRole.STUDENT
        )
        session.add(student)
        session.flush()
        session.add(
            UserCredential(app_user_id=student.id, password_hash=hash_password(PASSWORD))
        )
        session.flush()
        token = client.post(
            "/api/v1/auth/login",
            json={"email": "nope@example.test", "password": PASSWORD},
        ).json()["access_token"]

        response = client.get("/api/v1/imports/specs", headers=_auth(token))
        assert response.status_code == 403
