from __future__ import annotations

import io
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.importing.parser import clean_text, parse_money
from app.models.attendance import AttendanceRecord, PerformanceRecord
from app.models.employee import Employee
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api", tags=["attendance"])

_PERIOD = r"^\d{4}-\d{2}$"


def _visible_employee(session: Session, principal: Principal, employee_id: int) -> Employee:
    emp = EmployeeRepository(session, org_scope=principal_scope(principal)).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    return emp


# ------------------- 考勤 -------------------
class AttendanceBody(BaseModel):
    expected_days: Decimal = Field(ge=0, le=31)
    actual_days: Decimal = Field(ge=0, le=31)
    overtime_hours: Decimal = Field(default=Decimal(0), ge=0, le=744)
    leave_days: Decimal = Field(default=Decimal(0), ge=0, le=31)
    late_count: int = Field(default=0, ge=0)
    early_leave_count: int = Field(default=0, ge=0)


class AttendanceOut(BaseModel):
    employee_id: int
    period: str
    expected_days: Decimal
    actual_days: Decimal
    overtime_hours: Decimal
    leave_days: Decimal
    late_count: int
    early_leave_count: int

    model_config = {"from_attributes": True}


def _upsert_attendance(
    session: Session, employee_id: int, period: str, body: AttendanceBody
) -> AttendanceRecord:
    rec = session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.period == period
        )
    ).first()
    if rec is None:
        rec = AttendanceRecord(employee_id=employee_id, period=period, **body.model_dump())
        session.add(rec)
    else:
        for field, value in body.model_dump().items():
            setattr(rec, field, value)
    session.flush()
    return rec


@router.put("/employees/{employee_id}/attendance/{period}", response_model=AttendanceOut)
def set_attendance(
    employee_id: int,
    period: str,
    body: AttendanceBody,
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_WRITE)),
    session: Session = Depends(get_session),
) -> AttendanceRecord:
    if not _period_ok(period):
        raise HTTPException(status_code=422, detail="周期格式应为 YYYY-MM")
    _visible_employee(session, principal, employee_id)
    rec = _upsert_attendance(session, employee_id, period, body)
    audit.record(
        session,
        action="attendance.set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"period": period},
    )
    session.commit()
    return rec


@router.get("/attendance", response_model=list[AttendanceOut])
def list_attendance(
    period: str = Query(..., pattern=_PERIOD),
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_READ)),
    session: Session = Depends(get_session),
) -> list[AttendanceRecord]:
    scope = principal_scope(principal)
    stmt = (
        select(AttendanceRecord)
        .join(Employee, Employee.id == AttendanceRecord.employee_id)
        .where(AttendanceRecord.period == period, Employee.is_deleted.is_(False))
    )
    if scope is not None:
        stmt = stmt.where(Employee.org_unit_id.in_(scope))
    return list(session.scalars(stmt).all())


# ------------------- 绩效 -------------------
class PerformanceBody(BaseModel):
    coefficient: Decimal = Field(default=Decimal("1.000"), ge=0, le=5)
    score: Decimal | None = Field(default=None, ge=0, le=100)
    remark: str | None = Field(default=None, max_length=255)


class PerformanceOut(BaseModel):
    employee_id: int
    period: str
    coefficient: Decimal
    score: Decimal | None
    remark: str | None

    model_config = {"from_attributes": True}


@router.put("/employees/{employee_id}/performance/{period}", response_model=PerformanceOut)
def set_performance(
    employee_id: int,
    period: str,
    body: PerformanceBody,
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_WRITE)),
    session: Session = Depends(get_session),
) -> PerformanceRecord:
    if not _period_ok(period):
        raise HTTPException(status_code=422, detail="周期格式应为 YYYY-MM")
    _visible_employee(session, principal, employee_id)
    rec = session.scalars(
        select(PerformanceRecord).where(
            PerformanceRecord.employee_id == employee_id,
            PerformanceRecord.period == period,
        )
    ).first()
    if rec is None:
        rec = PerformanceRecord(employee_id=employee_id, period=period, **body.model_dump())
        session.add(rec)
    else:
        for field, value in body.model_dump().items():
            setattr(rec, field, value)
    session.flush()
    audit.record(
        session,
        action="performance.set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"period": period},
    )
    session.commit()
    return rec


# ------------------- Excel 导入（按工号匹配员工，组织范围内 upsert）-------------------
class AttendanceImportResult(BaseModel):
    matched: int
    skipped: list[str]  # 未匹配/越权的工号


@router.post("/attendance/import", response_model=AttendanceImportResult)
async def import_attendance(
    period: str = Query(..., pattern=_PERIOD),
    file: UploadFile = File(...),
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_WRITE)),
    session: Session = Depends(get_session),
) -> AttendanceImportResult:
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx/.xlsm 文件")
    content = await file.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限")

    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="解析失败") from None

    repo = EmployeeRepository(session, org_scope=principal_scope(principal))
    matched = 0
    skipped: list[str] = []
    for sheet in wb.worksheets:
        headers = [clean_text(c.value) for c in sheet[1]]
        col = {h: i for i, h in enumerate(headers)}
        if "工号" not in col:
            continue
        for row in sheet.iter_rows(min_row=2):
            emp_no = clean_text(row[col["工号"]].value) if col.get("工号") is not None else ""
            if not emp_no:
                continue
            emp = repo.by_emp_no(emp_no)
            if emp is None:  # 不存在或超出组织范围 → 跳过（越权保护）
                skipped.append(emp_no)
                continue
            body = AttendanceBody(
                expected_days=_cell(row, col, "应出勤") or Decimal(0),
                actual_days=_cell(row, col, "实出勤") or Decimal(0),
                overtime_hours=_cell(row, col, "加班") or Decimal(0),
                leave_days=_cell(row, col, "请假") or Decimal(0),
            )
            _upsert_attendance(session, emp.id, period, body)
            matched += 1
    wb.close()
    audit.record(
        session,
        action="attendance.import",
        actor=(principal.user_id, principal.username),
        detail={"period": period, "matched": matched, "skipped": len(skipped)},
    )
    session.commit()
    return AttendanceImportResult(matched=matched, skipped=skipped)


def _period_ok(period: str) -> bool:
    import re

    return bool(re.fullmatch(_PERIOD, period))


def _cell(row, col: dict[str, int], name: str) -> Decimal | None:
    idx = col.get(name)
    if idx is None or idx >= len(row):
        return None
    return parse_money(row[idx].value)
