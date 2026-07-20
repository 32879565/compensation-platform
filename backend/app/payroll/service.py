"""把 DB 中的薪资结构/考勤/绩效装配成引擎 v2 输入并预览核算（不落库；落库在 S13c）。"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.comp.service import current_structure
from app.models.attendance import AttendanceRecord
from app.models.comp import SalaryComponentDef
from app.models.employee import Employee
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    PayrollResult,
    RuleConfig,
    StructureComponent,
    compute,
)


def _period_start(period: str) -> date:
    year, month = period.split("-")
    return date(int(year), int(month), 1)


def _days_in_month(period: str) -> Decimal:
    year, month = (int(x) for x in period.split("-"))
    return Decimal(calendar.monthrange(year, month)[1])


def _same_period(d: date | None, period: str) -> bool:
    return d is not None and f"{d.year:04d}-{d.month:02d}" == period


def build_input(
    session: Session, employee: Employee, period: str
) -> tuple[EmployeeInput, list[int]]:
    """装配引擎输入；返回 (输入, 无法解析的组件 id 列表)。"""
    on_date = _period_start(period)
    ess = current_structure(session, employee.id, on_date)
    comp_meta = {
        cid: (code, ctype, akind)
        for cid, code, ctype, akind in session.execute(
            select(
                SalaryComponentDef.id,
                SalaryComponentDef.code,
                SalaryComponentDef.component_type,
                SalaryComponentDef.allowance_kind,
            ).where(SalaryComponentDef.id.in_({r.component_id for r in ess} or {0}))
        ).all()
    }
    missing = sorted({r.component_id for r in ess if r.component_id not in comp_meta})
    structure = [
        StructureComponent(
            comp_meta[r.component_id][0],
            comp_meta[r.component_id][1],
            r.amount,
            comp_meta[r.component_id][2],
        )
        for r in ess
        if r.component_id in comp_meta
    ]

    att = session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id, AttendanceRecord.period == period
        )
    ).first()
    attendance = (
        Attendance(
            expected_days=att.expected_days,
            actual_days=att.actual_days,
            worked_hours=att.worked_hours,
            rest_days=att.rest_days,
            overtime_hours=att.overtime_hours,
            holiday_worked_days=att.holiday_worked_days,
        )
        if att
        else None
    )

    is_new = _same_period(employee.hire_date, period)
    is_hire_or_leave = is_new or _same_period(employee.leave_date, period)
    # v2 简化：入职晚于周期首日则视为不享法定节假日（缺法定日历，属已知简化）
    holiday_eligible = employee.hire_date is None or employee.hire_date <= on_date

    inp = EmployeeInput(
        employee_id=employee.id,
        period=period,
        days_in_month=_days_in_month(period),
        employment_type=employee.employment_type,
        department=employee.department,
        is_special_position=employee.is_special_position,
        structure=structure,
        attendance=attendance,
        # v2：法定节假日总天数需法定日历配置，暂缺→0（引擎已支持，配置后传入）
        statutory_holiday_days=Decimal("0"),
        holiday_eligible=holiday_eligible,
        is_new_employee=is_new,
        is_hire_or_leave_month=is_hire_or_leave,
    )
    return inp, missing


def preview(
    session: Session, employee: Employee, period: str, cfg: RuleConfig | None = None
) -> PayrollResult:
    inp, missing = build_input(session, employee, period)
    result = compute(inp, cfg)
    if missing:
        result.exceptions.append(f"存在无法解析的薪资组件(id={missing})，已阻断出账")
    return result
