"""Import provisional current employees from the latest historical names.

Revision ID: r7a0b3d9e864
Revises: q6f9a2c8d753
Create Date: 2026-07-21 00:05:00.000000

The legacy salary cache has no employee number or other durable identity key.
This data-only migration implements the user's explicit decision to treat each
unique name in the latest historical period as one provisional employee.  The
generated employee number is visibly marked so a later roster migration can
replace it with a verified identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256

import sqlalchemy as sa

from alembic import op

revision: str = "r7a0b3d9e864"
down_revision: str | None = "q6f9a2c8d753"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EMPLOYEE_NUMBER_PREFIX = "LEGACY-NAME-"
_HOURLY_POSITION_MARKERS = ("兼职", "小时工", "寒假工", "暑假工")
_LABOR_POSITION_MARKERS = ("劳务",)
_SPECIAL_POSITION_MARKERS = (
    "店长实习",
    "厨师长实习",
    "储备",
    "洗碗",
    "寒假工",
    "暑假工",
)


def _employee_number(name: str) -> str:
    digest = sha256(name.encode("utf-8")).hexdigest()[:16].upper()
    return f"{_EMPLOYEE_NUMBER_PREFIX}{digest}"


def _parse_hire_date(raw_value: object, *, employee_name: str) -> date | None:
    if raw_value in (None, ""):
        return None
    value = str(raw_value).strip()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid legacy hire date for provisional employee {employee_name}: {value}."
        ) from exc


def _employment_profile(position: str | None) -> tuple[str, bool]:
    normalized = position or ""
    if any(marker in normalized for marker in _LABOR_POSITION_MARKERS):
        employment_type = "LABOR"
    elif any(marker in normalized for marker in _HOURLY_POSITION_MARKERS):
        employment_type = "PART_TIME_HOURLY"
    else:
        employment_type = "FULL_TIME"
    is_special = any(marker in normalized for marker in _SPECIAL_POSITION_MARKERS)
    return employment_type, is_special


def _latest_candidates(bind: sa.Connection) -> list[sa.Row]:
    latest_period = bind.scalar(sa.text("""
            SELECT max(period)
            FROM salary_record
            WHERE source = 'HISTORICAL'
            """))
    if latest_period is None:
        return []

    candidates = list(
        bind.execute(
            sa.text("""
                SELECT salary.name,
                       salary.org_unit_id,
                       salary.fields,
                       organization.city
                FROM salary_record AS salary
                JOIN org_unit AS organization ON organization.id = salary.org_unit_id
                WHERE salary.source = 'HISTORICAL'
                  AND salary.period = :latest_period
                ORDER BY salary.name
                """),
            {"latest_period": latest_period},
        )
    )
    names = [row.name for row in candidates]
    if len(names) != len(set(names)):
        raise RuntimeError(
            f"Latest historical period {latest_period} contains duplicate employee names; "
            "name-based provisional import is ambiguous."
        )
    return candidates


def _employee_rows(candidates: list[sa.Row]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    generated_numbers: set[str] = set()
    for candidate in candidates:
        name = str(candidate.name).strip()
        if not name:
            raise RuntimeError("Name-based provisional import found a blank employee name.")
        if len(name) > 64:
            raise RuntimeError(f"Employee name exceeds 64 characters: {name}.")
        if candidate.org_unit_id is None:
            raise RuntimeError(f"Provisional employee {name} has no organization mapping.")

        fields = candidate.fields if isinstance(candidate.fields, dict) else {}
        raw_position = fields.get("职位")
        position = str(raw_position).strip() if raw_position not in (None, "") else None
        if position is not None and len(position) > 64:
            raise RuntimeError(f"Position exceeds 64 characters for {name}: {position}.")
        employment_type, is_special = _employment_profile(position)
        employee_number = _employee_number(name)
        if employee_number in generated_numbers:
            raise RuntimeError(f"Generated employee number collision for {name}.")
        generated_numbers.add(employee_number)

        rows.append(
            {
                "emp_no": employee_number,
                "name": name,
                "org_unit_id": candidate.org_unit_id,
                "employment_type": employment_type,
                "status": "ACTIVE",
                "hire_date": _parse_hire_date(fields.get("入职日期"), employee_name=name),
                "department": "OTHER",
                "position_title": position,
                "is_special_position": is_special,
                "social_city": candidate.city,
            }
        )
    return rows


def _assert_no_employee_conflicts(bind: sa.Connection, rows: list[dict[str, object]]) -> None:
    candidate_names = {str(row["name"]) for row in rows}
    candidate_numbers = {str(row["emp_no"]) for row in rows}
    conflicting_names: list[str] = []
    conflicting_numbers: list[str] = []
    for name, emp_no in bind.execute(sa.text("SELECT name, emp_no FROM employee")):
        if name in candidate_names:
            conflicting_names.append(name)
        if emp_no in candidate_numbers or emp_no.startswith(_EMPLOYEE_NUMBER_PREFIX):
            conflicting_numbers.append(emp_no)
    if conflicting_names:
        raise RuntimeError(
            "Name-based provisional import conflicts with existing employee names: "
            + ", ".join(sorted(conflicting_names))
        )
    if conflicting_numbers:
        raise RuntimeError(
            "Name-based provisional import found reserved employee numbers: "
            + ", ".join(sorted(conflicting_numbers))
        )


def upgrade() -> None:
    bind = op.get_bind()
    rows = _employee_rows(_latest_candidates(bind))
    if not rows:
        return
    _assert_no_employee_conflicts(bind, rows)

    bind.execute(
        sa.text("""
            INSERT INTO employee
                (emp_no, name, org_unit_id, job_grade_id, employment_type, status,
                 hire_date, probation_end, leave_date, social_city, id_card, bank_account,
                 department, position_title, is_special_position)
            VALUES
                (:emp_no, :name, :org_unit_id, NULL, :employment_type, :status,
                 :hire_date, NULL, NULL, :social_city, NULL, NULL,
                 :department, :position_title, :is_special_position)
            """),
        rows,
    )
    bind.execute(
        sa.text("""
                UPDATE salary_record AS salary
                SET employee_id = employee.id
                FROM employee
                WHERE salary.source = 'HISTORICAL'
                  AND salary.employee_id IS NULL
                  AND salary.name = employee.name
                  AND employee.emp_no LIKE :prefix
                """),
        {"prefix": f"{_EMPLOYEE_NUMBER_PREFIX}%"},
    )

    created = bind.scalar(
        sa.text("SELECT count(*) FROM employee WHERE emp_no LIKE :prefix"),
        {"prefix": f"{_EMPLOYEE_NUMBER_PREFIX}%"},
    )
    if created != len(rows):
        raise RuntimeError(
            f"Expected {len(rows)} provisional employees after import, found {created}."
        )


def downgrade() -> None:
    bind = op.get_bind()
    generated_ids = list(
        bind.scalars(
            sa.text("SELECT id FROM employee WHERE emp_no LIKE :prefix ORDER BY id"),
            {"prefix": f"{_EMPLOYEE_NUMBER_PREFIX}%"},
        )
    )
    for employee_id in generated_ids:
        bind.execute(
            sa.text("""
                UPDATE salary_record
                SET employee_id = NULL
                WHERE source = 'HISTORICAL' AND employee_id = :employee_id
                """),
            {"employee_id": employee_id},
        )
        # Other business tables keep restrictive foreign keys.  If payroll,
        # attendance, users, or adjustments now reference this provisional
        # employee, deletion fails transactionally instead of losing data.
        bind.execute(
            sa.text("DELETE FROM employee WHERE id = :employee_id"),
            {"employee_id": employee_id},
        )
