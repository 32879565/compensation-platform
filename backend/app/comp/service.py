"""薪资结构（生效日期化）与薪档带宽（compa-ratio）服务。"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.comp import ComponentType, EmployeeSalaryStructure, SalaryComponentDef
from app.models.grade import SalaryBand


class StructureError(Exception):
    pass


class BandStatus(enum.StrEnum):
    IN_BAND = "IN_BAND"
    OVER = "OVER"  # 超上限
    UNDER = "UNDER"  # 低于下限
    NO_BAND = "NO_BAND"  # 无带宽可比


def current_structure(
    session: Session, employee_id: int, on_date: date
) -> list[EmployeeSalaryStructure]:
    """某日生效的薪资结构记录：effective_from<=on_date 且 (effective_to 空 或 > on_date)。"""
    stmt = (
        select(EmployeeSalaryStructure)
        .where(
            EmployeeSalaryStructure.employee_id == employee_id,
            EmployeeSalaryStructure.effective_from <= on_date,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > on_date),
        )
        .order_by(EmployeeSalaryStructure.component_id)  # 确定性行序
    )
    return list(session.scalars(stmt).all())


def set_component_amount(
    session: Session,
    *,
    employee_id: int,
    component_id: int,
    amount: Decimal,
    effective_from: date,
) -> EmployeeSalaryStructure:
    """生效日期化地设置组件金额：关闭旧开放记录、插入新开放记录。

    - 同日修正：直接改金额。
    - 生效日须不早于当前开放记录的生效日（不支持向历史中间插入）。
    """
    open_rec = session.scalars(
        select(EmployeeSalaryStructure).where(
            EmployeeSalaryStructure.employee_id == employee_id,
            EmployeeSalaryStructure.component_id == component_id,
            EmployeeSalaryStructure.effective_to.is_(None),
        )
    ).first()

    if open_rec is not None:
        if open_rec.effective_from == effective_from:
            open_rec.amount = amount  # 同日修正
            session.flush()
            return open_rec
        if effective_from <= open_rec.effective_from:
            raise StructureError("生效日期须晚于当前生效记录的生效日")
        open_rec.effective_to = effective_from  # 关闭旧记录

    new_rec = EmployeeSalaryStructure(
        employee_id=employee_id,
        component_id=component_id,
        amount=amount,
        effective_from=effective_from,
        effective_to=None,
    )
    session.add(new_rec)
    session.flush()
    return new_rec


def structure_total(session: Session, employee_id: int, on_date: date) -> Decimal:
    """某日固定薪资合计（非扣款组件求和），用于带宽比对。"""
    recs = current_structure(session, employee_id, on_date)
    if not recs:
        return Decimal(0)
    comp_types = {
        cid: ctype
        for cid, ctype in session.execute(
            select(SalaryComponentDef.id, SalaryComponentDef.component_type).where(
                SalaryComponentDef.id.in_({r.component_id for r in recs})
            )
        ).all()
    }
    total = Decimal(0)
    for r in recs:
        if comp_types.get(r.component_id) != ComponentType.DEDUCTION:
            total += r.amount
    return total


def band_for(session: Session, job_grade_id: int, on_date: date) -> SalaryBand | None:
    """取该职级在 on_date 当日生效的最新带宽（effective_from<=on_date 里最新一条）。"""
    return session.scalars(
        select(SalaryBand)
        .where(
            SalaryBand.job_grade_id == job_grade_id,
            SalaryBand.is_deleted.is_(False),
            SalaryBand.effective_from <= on_date,
        )
        .order_by(SalaryBand.effective_from.desc())
        .limit(1)
    ).first()


@dataclass(frozen=True)
class CompaResult:
    total: Decimal
    band_status: BandStatus
    compa_ratio: Decimal | None  # total / band_mid，无带宽为 None
    band_min: Decimal | None
    band_mid: Decimal | None
    band_max: Decimal | None


def compa_ratio(
    session: Session, *, employee_id: int, job_grade_id: int | None, on_date: date
) -> CompaResult:
    total = structure_total(session, employee_id, on_date)
    band = band_for(session, job_grade_id, on_date) if job_grade_id is not None else None
    if band is None:
        return CompaResult(total, BandStatus.NO_BAND, None, None, None, None)

    if total > band.band_max:
        status = BandStatus.OVER
    elif total < band.band_min:
        status = BandStatus.UNDER
    else:
        status = BandStatus.IN_BAND
    ratio = (total / band.band_mid) if band.band_mid else None
    return CompaResult(total, status, ratio, band.band_min, band.band_mid, band.band_max)
