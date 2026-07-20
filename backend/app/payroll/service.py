"""把 DB 中的薪资结构/考勤/绩效装配成引擎输入并预览核算（不落库；落库在 S13）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.comp.service import current_structure
from app.models.attendance import AttendanceRecord, PerformanceRecord
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


def build_input(
    session: Session, employee: Employee, period: str
) -> tuple[EmployeeInput, list[int]]:
    """装配引擎输入；返回 (输入, 无法解析的组件 id 列表)。"""
    on_date = _period_start(period)
    ess = current_structure(session, employee.id, on_date)
    comp_meta = {
        cid: (code, ctype)
        for cid, code, ctype in session.execute(
            select(
                SalaryComponentDef.id,
                SalaryComponentDef.code,
                SalaryComponentDef.component_type,
            ).where(SalaryComponentDef.id.in_({r.component_id for r in ess} or {0}))
        ).all()
    }
    # 缺 def 的结构组件不得静默丢弃（会少发且不报错）；收集缺失 id 供 preview 报异常
    missing = sorted({r.component_id for r in ess if r.component_id not in comp_meta})
    structure = [
        StructureComponent(comp_meta[r.component_id][0], comp_meta[r.component_id][1], r.amount)
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
            overtime_hours=att.overtime_hours,
            leave_days=att.leave_days,
        )
        if att
        else None
    )

    perf = session.scalars(
        select(PerformanceRecord).where(
            PerformanceRecord.employee_id == employee.id, PerformanceRecord.period == period
        )
    ).first()

    inp = EmployeeInput(
        employee_id=employee.id,
        period=period,
        employment_type=employee.employment_type,
        structure=structure,
        attendance=attendance,
        performance_coefficient=perf.coefficient if perf else None,
        # v1：试用期系数默认 1（试用期薪资通过结构生效日期化体现）；引擎已支持系数，
        # 业务确认试用期口径后在此按 employee.probation_end 传入。
        probation_coefficient=Decimal("1"),
    )
    return inp, missing


def preview(
    session: Session, employee: Employee, period: str, cfg: RuleConfig | None = None
) -> PayrollResult:
    inp, missing = build_input(session, employee, period)
    result = compute(inp, cfg)
    if missing:
        # 有结构组件无法解析（def 缺失）→ 报异常阻断出账，绝不静默少发
        result.exceptions.append(f"存在无法解析的薪资组件(id={missing})，已阻断出账")
    return result
