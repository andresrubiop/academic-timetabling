"""The seven importable entities the playbook names.

Each is a description, not code: which headings a file may carry, what type
each cell holds, which columns are required, and -- the part that makes an
import usable by a registrar rather than by a developer -- which columns are
*references* resolved by natural key.

Headings carry Spanish aliases throughout. The files these imports exist to
consume come out of an Ecuadorian university's own systems and are written by
people who work in Spanish; requiring `department_code` in a file whose author
would write `Departamento` means the import only accepts files somebody has
already reformatted for it, which is most of the work it was meant to save.
"""

from __future__ import annotations

from academic_ai.domain.imports.spec import (
    ColumnSpec,
    ColumnType,
    ImportSpec,
    Lookup,
)

__all__ = ["IMPORT_SPECS", "spec_for"]


def _positive(field: str):
    def _check(value: object) -> None:
        if isinstance(value, int) and value <= 0:
            raise ValueError(f"{field} must be greater than zero")

    return _check


def _non_negative(field: str):
    def _check(value: object) -> None:
        if isinstance(value, int) and value < 0:
            raise ValueError(f"{field} cannot be negative")

    return _check


_TEACHERS = ImportSpec(
    code="teachers",
    entity="Teacher",
    description="Teaching staff, matched on their institutional code.",
    natural_key=("institution_id", "code"),
    create_only_defaults={"status": "ACTIVE"},
    columns=(
        ColumnSpec("Código", "code", required=True, aliases=("code", "codigo")),
        ColumnSpec(
            "Nombre", "full_name", required=True, aliases=("full_name", "name", "nombre")
        ),
        ColumnSpec("Email", "email", aliases=("email", "correo")),
        ColumnSpec(
            "Departamento",
            "department_code",
            type=ColumnType.REFERENCE,
            aliases=("department", "department_code", "departamento"),
            lookup=Lookup("department", "department_id"),
        ),
        ColumnSpec(
            "Contrato",
            "contract_type",
            type=ColumnType.ENUM,
            choices=("FULL_TIME", "PART_TIME", "HOURLY", "VISITING"),
            aliases=("contract", "contract_type", "contrato"),
        ),
        ColumnSpec(
            "Horas semanales",
            "max_weekly_hours",
            type=ColumnType.INTEGER,
            aliases=("max_weekly_hours", "horas_semanales"),
            validate=_positive("max_weekly_hours"),
        ),
        ColumnSpec(
            "Horas diarias",
            "max_daily_hours",
            type=ColumnType.INTEGER,
            aliases=("max_daily_hours", "horas_diarias"),
            validate=_positive("max_daily_hours"),
        ),
    ),
)

_STUDENTS = ImportSpec(
    code="students",
    entity="Student",
    description="Enrolled students, matched on their institutional code.",
    natural_key=("institution_id", "code"),
    create_only_defaults={"status": "ACTIVE"},
    columns=(
        ColumnSpec("Código", "code", required=True, aliases=("code", "codigo")),
        ColumnSpec(
            "Nombre", "full_name", required=True, aliases=("full_name", "name", "nombre")
        ),
        ColumnSpec("Email", "email", aliases=("email", "correo")),
        ColumnSpec(
            "Programa",
            "program_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("program", "program_code", "programa", "carrera"),
            lookup=Lookup("program", "program_id"),
        ),
        ColumnSpec(
            "Fecha de admisión",
            "admission_date",
            type=ColumnType.DATE,
            aliases=("admission_date", "fecha_admision", "fecha de admision"),
        ),
    ),
)

_COURSES = ImportSpec(
    code="courses",
    entity="Course",
    description="The course catalogue, matched on institution and code.",
    natural_key=("institution_id", "code"),
    create_only_defaults={"status": "ACTIVE"},
    columns=(
        ColumnSpec("Código", "code", required=True, aliases=("code", "codigo")),
        ColumnSpec(
            "Nombre", "name", required=True, aliases=("name", "nombre", "asignatura")
        ),
        ColumnSpec(
            "Departamento",
            "department_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("department", "department_code", "departamento"),
            lookup=Lookup("department", "department_id"),
        ),
        ColumnSpec(
            "Créditos",
            "credits",
            type=ColumnType.INTEGER,
            required=True,
            aliases=("credits", "creditos"),
            validate=_non_negative("credits"),
        ),
        ColumnSpec(
            "Horas teoría",
            "theory_hours",
            type=ColumnType.INTEGER,
            aliases=("theory_hours", "horas_teoria", "horas teoria"),
            validate=_non_negative("theory_hours"),
        ),
        ColumnSpec(
            "Horas práctica",
            "practice_hours",
            type=ColumnType.INTEGER,
            aliases=("practice_hours", "horas_practica", "horas practica"),
            validate=_non_negative("practice_hours"),
        ),
        ColumnSpec(
            "Horas laboratorio",
            "lab_hours",
            type=ColumnType.INTEGER,
            aliases=("lab_hours", "horas_laboratorio"),
            validate=_non_negative("lab_hours"),
        ),
    ),
)

_ROOMS = ImportSpec(
    code="rooms",
    entity="Room",
    description="Teaching spaces, matched on building and code.",
    natural_key=("building_id", "code"),
    create_only_defaults={"status": "ACTIVE"},
    columns=(
        ColumnSpec("Código", "code", required=True, aliases=("code", "codigo", "aula")),
        ColumnSpec("Nombre", "name", required=True, aliases=("name", "nombre")),
        ColumnSpec(
            "Edificio",
            "building_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("building", "building_code", "edificio", "bloque"),
            # `campus_id` rides along: it is NOT NULL on room and bound to the
            # building's campus by a composite foreign key, so naming the
            # building has already named the campus.
            lookup=Lookup(
                "building", "building_id", carry={"campus_id": "campus_id"}
            ),
        ),
        ColumnSpec(
            "Tipo",
            "room_type",
            type=ColumnType.ENUM,
            required=True,
            choices=(
                "CLASSROOM", "COMPUTER_LAB", "SCIENCE_LAB", "WORKSHOP",
                "AUDITORIUM", "STUDIO", "FIELD", "VIRTUAL", "OTHER",
            ),
            aliases=("type", "room_type", "tipo"),
        ),
        ColumnSpec(
            "Capacidad",
            "capacity",
            type=ColumnType.INTEGER,
            required=True,
            aliases=("capacity", "capacidad"),
            validate=_positive("capacity"),
        ),
    ),
)

_OFFERINGS = ImportSpec(
    code="offerings",
    entity="CourseOffering",
    description=(
        "What is taught in a period. Matched on course, period, campus and "
        "variant -- ADR-0004's offering identity, so an import cannot create "
        "the ambiguity the schema exists to prevent."
    ),
    natural_key=("course_id", "academic_period_id", "campus_id", "variant_code"),
    create_only_defaults={"status": "PLANNED"},
    columns=(
        ColumnSpec(
            "Asignatura",
            "course_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("course", "course_code", "asignatura", "materia"),
            lookup=Lookup("course", "course_id"),
        ),
        ColumnSpec(
            "Periodo",
            "period_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("period", "period_code", "periodo", "período"),
            lookup=Lookup("academic period", "academic_period_id"),
        ),
        ColumnSpec(
            "Campus",
            "campus_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("campus", "campus_code", "sede"),
            lookup=Lookup("campus", "campus_id"),
        ),
        ColumnSpec(
            "Modalidad",
            "modality",
            type=ColumnType.ENUM,
            required=True,
            choices=("ONSITE", "ONLINE", "HYBRID"),
            aliases=("modality", "modalidad"),
        ),
        ColumnSpec(
            "Variante",
            "variant_code",
            aliases=("variant", "variant_code", "variante"),
        ),
    ),
)

_ENROLLMENTS = ImportSpec(
    code="enrollments",
    entity="Enrollment",
    description=(
        "Who is taking what. Matched on student and offering, so re-importing "
        "a register does not enrol anybody twice."
    ),
    natural_key=("student_id", "course_offering_id"),
    create_only_defaults={"status": "REQUESTED"},
    columns=(
        ColumnSpec(
            "Estudiante",
            "student_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("student", "student_code", "estudiante"),
            lookup=Lookup("student", "student_id"),
        ),
        ColumnSpec(
            "Asignatura",
            "course_code",
            type=ColumnType.REFERENCE,
            required=True,
            aliases=("course", "course_code", "asignatura", "materia"),
            lookup=Lookup("course", "course_id"),
        ),
        ColumnSpec(
            "Obligatoria",
            "is_mandatory",
            type=ColumnType.BOOLEAN,
            aliases=("is_mandatory", "mandatory", "obligatoria"),
        ),
        ColumnSpec(
            "Tipo",
            "enrollment_kind",
            type=ColumnType.ENUM,
            choices=("CURRENT_LEVEL", "REPEATED", "ADVANCED", "ELECTIVE"),
            aliases=("kind", "enrollment_kind", "tipo"),
        ),
        ColumnSpec(
            "Intento",
            "attempt_number",
            type=ColumnType.INTEGER,
            aliases=("attempt", "attempt_number", "intento"),
            validate=_positive("attempt_number"),
        ),
    ),
)

_AVAILABILITY = ImportSpec(
    code="availability",
    entity="Availability",
    description=(
        "When a teacher or a room cannot be used. Matched on the subject and "
        "the timeslot, so re-importing a declaration does not stack duplicate "
        "rows on the same hour."
    ),
    natural_key=("teacher_id", "room_id", "timeslot_id"),
    create_only_defaults={},
    columns=(
        ColumnSpec(
            "Docente",
            "teacher_code",
            type=ColumnType.REFERENCE,
            aliases=("teacher", "teacher_code", "docente", "profesor"),
            lookup=Lookup("teacher", "teacher_id"),
        ),
        ColumnSpec(
            "Aula",
            "room_code",
            type=ColumnType.REFERENCE,
            aliases=("room", "room_code", "aula"),
            lookup=Lookup("room", "room_id"),
        ),
        ColumnSpec(
            "Día",
            "day_of_week",
            type=ColumnType.ENUM,
            required=True,
            choices=(
                "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
                "FRIDAY", "SATURDAY", "SUNDAY",
            ),
            aliases=("day", "day_of_week", "dia", "día"),
        ),
        ColumnSpec(
            "Hora inicio",
            "start_minute",
            type=ColumnType.INTEGER,
            required=True,
            aliases=("start_minute", "start", "hora_inicio", "hora inicio"),
            validate=_non_negative("start_minute"),
        ),
        ColumnSpec(
            "Estado",
            "status",
            type=ColumnType.ENUM,
            required=True,
            choices=("AVAILABLE", "UNAVAILABLE"),
            aliases=("status", "estado"),
        ),
        ColumnSpec("Motivo", "reason", aliases=("reason", "motivo")),
    ),
)


IMPORT_SPECS: dict[str, ImportSpec] = {
    spec.code: spec
    for spec in (
        _TEACHERS,
        _STUDENTS,
        _COURSES,
        _ROOMS,
        _OFFERINGS,
        _ENROLLMENTS,
        _AVAILABILITY,
    )
}


def spec_for(code: str) -> ImportSpec:
    spec = IMPORT_SPECS.get(code)
    if spec is None:
        available = ", ".join(sorted(IMPORT_SPECS))
        raise KeyError(f"no import spec {code!r}; available: {available}")
    return spec
