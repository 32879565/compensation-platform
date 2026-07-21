"""Publish an HR-confirmed final-payroll workbook into the review workflow.

This is deliberately separate from the calculation engine: gross, net and
line items are immutable evidence supplied by HR, not values that may be
silently recalculated from mutable master data.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importing.header_rules import CURRENT_IMPORT_FIELDS, MONEY_FIELDS
from app.importing.parser import parse_money
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import BatchConfirmation, PayrollResult
from app.models.salary import ImportBatch, ImportStatus, SalaryRecord, SalarySource
from app.payroll.guards import lock_payroll_input_mutation

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
_MAX_NUMERIC_14_2 = Decimal("999999999999.99")
_IMPORT_RULE_VERSION = "IMPORT-v1"
_IMPORT_WARNING = "此结果来自人事确认的 Excel 导入，未经过系统核算引擎"


class ImportPublishError(Exception):
    """A safe, actionable validation or state-transition error."""


@dataclass(frozen=True)
class ImportPublishSummary:
    import_batch_id: int
    payroll_batch_id: int
    batch_version: int
    employees: int
    scopes: int
    already_published: bool


@dataclass(frozen=True)
class _ValidatedRecord:
    source: SalaryRecord
    employee: Employee
    department: Department
    gross: Decimal
    net: Decimal
    deposit: Decimal
    carry_forward: Decimal
    actual_attendance_days: Decimal
    statutory_holiday_days: Decimal
    statutory_holiday_worked_days: Decimal
    lines: list[dict[str, str]]


_DEPARTMENT_ALIASES: dict[str, Department] = {
    "厅面": Department.DINING,
    "前厅": Department.DINING,
    "DINING": Department.DINING,
    "店长": Department.DINING,
    "厨房": Department.KITCHEN,
    "后厨": Department.KITCHEN,
    "KITCHEN": Department.KITCHEN,
    "厨房经理": Department.KITCHEN,
}

_LINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("综合薪资", "COMPREHENSIVE"),
    ("基本薪资", "BASE"),
    ("出勤工资", "ATTEND_WAGE"),
    ("加班工资", "OVERTIME"),
    ("法定节假日工资", "HOLIDAY"),
    ("固定补贴", "FIXED_ALLOWANCE"),
    ("浮动补贴", "FLOATING_ALLOWANCE"),
    ("补贴", "ALLOWANCE"),
    ("房补", "HOUSING"),
    ("上月补发", "PREV_MAKEUP"),
    ("上月补扣", "PREV_DEDUCT"),
    ("社保", "SOCIAL_INSURANCE"),
    ("公积金", "HOUSING_FUND"),
    ("个税", "INCOME_TAX"),
    ("其他扣款", "DEDUCTION"),
    ("押金", "DEPOSIT"),
    ("结转下月", "CARRY_FORWARD"),
)


def _quantize(value: Decimal) -> Decimal:
    try:
        rounded = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("金额范围超出系统上限（最多12位整数、2位小数）") from None
    if not rounded.is_finite() or abs(rounded) > _MAX_NUMERIC_14_2:
        raise ValueError("金额范围超出系统上限（最多12位整数、2位小数）")
    return rounded


def _validate_numeric_fields(fields: dict[str, Any]) -> None:
    """Validate every supported numeric cell, including non-projected line items."""
    for field in sorted(MONEY_FIELDS):
        selected = _field_value(fields, (field,))
        if selected is None:
            continue
        _selected_field, raw = selected
        value = parse_money(raw)
        if value is None:
            raise ValueError(f"金额字段『{field}』无法解析")
        _quantize(value)


def _validate_supported_fields(fields: dict[str, Any]) -> None:
    unsupported = sorted(str(field) for field in fields if str(field) not in CURRENT_IMPORT_FIELDS)
    if unsupported:
        visible = unsupported[:10]
        suffix = f"；另有 {len(unsupported) - len(visible)} 个字段" if len(unsupported) > 10 else ""
        raise ValueError("工资记录包含模板未支持的字段：" + "、".join(visible) + suffix)


def _field_value(fields: dict[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    for name in names:
        value = fields.get(name)
        if value is not None and str(value).strip() != "":
            return name, value
    return None


def _required_decimal(fields: dict[str, Any], names: tuple[str, ...], label: str) -> Decimal:
    selected = _field_value(fields, names)
    if selected is None:
        raise ValueError(f"缺少{label}")
    field, raw = selected
    value = parse_money(raw)
    if value is None:
        raise ValueError(f"金额字段『{field}』无法解析")
    return _quantize(value)


def _optional_decimal(
    fields: dict[str, Any], names: tuple[str, ...], *, default: Decimal = _ZERO
) -> Decimal:
    selected = _field_value(fields, names)
    if selected is None:
        return default
    field, raw = selected
    value = parse_money(raw)
    if value is None:
        raise ValueError(f"金额字段『{field}』无法解析")
    return _quantize(value)


def _review_department(fields: dict[str, Any], employee: Employee) -> Department:
    selected = _field_value(fields, ("复核部门", "部门", "所属部门"))
    if selected is None:
        if employee.department in {Department.DINING, Department.KITCHEN}:
            return employee.department
        raise ValueError("缺少复核部门，请填写厅面或厨房")
    _field, raw = selected
    normalized = "".join(str(raw).split()).upper()
    department = _DEPARTMENT_ALIASES.get(normalized)
    if department is None:
        raise ValueError("复核部门必须填写厅面或厨房")
    if employee.department in {Department.DINING, Department.KITCHEN}:
        if department != employee.department:
            raise ValueError("复核部门与员工主数据部门不一致")
        return employee.department
    return department


def _line_items(fields: dict[str, Any], *, gross: Decimal, net: Decimal) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for field, code in _LINE_FIELDS:
        if _field_value(fields, (field,)) is None:
            continue
        amount = _optional_decimal(fields, (field,))
        lines.append(
            {
                "code": code,
                "category": field,
                "formula": f"Excel导入：{field}",
                "amount": str(amount),
            }
        )
    lines.extend(
        [
            {
                "code": "GROSS",
                "category": "应发工资",
                "formula": "Excel导入：应发工资",
                "amount": str(gross),
            },
            {
                "code": "NET",
                "category": "实发工资",
                "formula": "Excel导入：实发工资",
                "amount": str(net),
            },
        ]
    )
    return lines


def _validate_records(
    records: list[SalaryRecord],
    employees: dict[int, Employee],
    org_units: dict[int, OrgUnit],
    *,
    period: str,
    days_in_month: int,
) -> list[_ValidatedRecord]:
    validated: list[_ValidatedRecord] = []
    errors: list[str] = []
    seen_employees: set[int] = set()
    for record in records:
        identifier = record.emp_no or str(record.employee_id or record.id)
        try:
            if record.period != period:
                raise ValueError("计薪月份与导入批次不一致")
            if record.employee_id is None or record.org_unit_id is None:
                raise ValueError("未关联员工或门店")
            if record.employee_id in seen_employees:
                raise ValueError("同一员工存在重复工资记录")
            employee = employees.get(record.employee_id)
            if employee is None:
                raise ValueError("员工主数据不存在或已删除")
            if employee.org_unit_id != record.org_unit_id:
                raise ValueError("员工当前所属门店与已确认工资记录不一致，请重新导入并确认")
            current_org = org_units.get(employee.org_unit_id)
            if (
                current_org is None
                or current_org.is_deleted
                or current_org.type != OrgType.STORE
                or current_org.status != "ACTIVE"
            ):
                raise ValueError("员工当前所属组织必须是有效营业门店")
            fields = record.fields if isinstance(record.fields, dict) else {}
            _validate_supported_fields(fields)
            _validate_numeric_fields(fields)
            department = _review_department(fields, employee)
            gross = _required_decimal(fields, ("应发工资", "合计工资"), "应发工资")
            net = _required_decimal(fields, ("实发工资",), "实发工资")
            deposit = _optional_decimal(fields, ("押金",))
            carry_forward = _optional_decimal(fields, ("结转下月",))
            actual_days = _optional_decimal(
                fields,
                ("实际计薪出勤天数", "实际出勤天数", "出勤天数", "折算天数"),
            )
            worked_holiday_days = _optional_decimal(
                fields,
                ("法定节假日出勤天数", "法定出勤天数", "法定出勤"),
            )
            statutory_days = _optional_decimal(
                fields, ("法定节假日天数",), default=worked_holiday_days
            )
            if any(value < 0 for value in (actual_days, statutory_days, worked_holiday_days)):
                raise ValueError("出勤天数不可以为负数")
            if any(value > days_in_month for value in (actual_days, statutory_days)):
                raise ValueError("出勤天数不可以超过当月天数")
            if worked_holiday_days > statutory_days:
                raise ValueError("法定节假日出勤天数不可以超过法定节假日天数")
            if gross < 0 or net < 0:
                raise ValueError("应发工资和实发工资不可以为负数")
            if deposit < 0 or carry_forward < 0:
                raise ValueError("押金和结转下月金额不可以为负数")
            validated.append(
                _ValidatedRecord(
                    source=record,
                    employee=employee,
                    department=department,
                    gross=gross,
                    net=net,
                    deposit=deposit,
                    carry_forward=carry_forward,
                    actual_attendance_days=actual_days,
                    statutory_holiday_days=statutory_days,
                    statutory_holiday_worked_days=worked_holiday_days,
                    lines=_line_items(fields, gross=gross, net=net),
                )
            )
            seen_employees.add(record.employee_id)
        except ValueError as exc:
            errors.append(f"工号『{identifier}』：{exc}")
    if errors:
        visible = errors[:20]
        suffix = f"；另有 {len(errors) - len(visible)} 条错误" if len(errors) > len(visible) else ""
        raise ImportPublishError("；".join(visible) + suffix)
    return validated


def _period_dates(period: str) -> tuple[date, date]:
    try:
        year_text, month_text = period.split("-", maxsplit=1)
        year, month = int(year_text), int(month_text)
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)
    except (TypeError, ValueError):
        raise ImportPublishError("计薪月份无效") from None


def _published_summary(session: Session, imported: ImportBatch) -> ImportPublishSummary:
    if imported.published_batch_id is None or imported.published_batch_version is None:
        raise ImportPublishError("导入批次的发布关联不完整，请联系系统管理员")
    payroll_batch = session.get(PayrollBatch, imported.published_batch_id)
    if payroll_batch is None:
        raise ImportPublishError("已发布的薪资批次不存在，请联系系统管理员")
    if payroll_batch.version != imported.published_batch_version:
        raise ImportPublishError("该导入已发布到历史版本；请上传修正后的新文件")
    employees = (
        session.scalar(
            select(func.count())
            .select_from(PayrollResult)
            .where(
                PayrollResult.source_import_batch_id == imported.id,
                PayrollResult.batch_id == payroll_batch.id,
                PayrollResult.batch_version == imported.published_batch_version,
            )
        )
        or 0
    )
    scopes = (
        session.scalar(
            select(func.count())
            .select_from(BatchConfirmation)
            .where(
                BatchConfirmation.batch_id == payroll_batch.id,
                BatchConfirmation.batch_version == imported.published_batch_version,
            )
        )
        or 0
    )
    if employees <= 0 or scopes <= 0:
        raise ImportPublishError("已发布的薪资复核数据不完整，请联系系统管理员")
    return ImportPublishSummary(
        import_batch_id=imported.id,
        payroll_batch_id=payroll_batch.id,
        batch_version=payroll_batch.version,
        employees=int(employees),
        scopes=int(scopes),
        already_published=True,
    )


def publish_import_for_review(session: Session, imported: ImportBatch) -> ImportPublishSummary:
    """Create one immutable payroll-result round from a confirmed import.

    The transaction is serialized with calculation and source correction.  All
    rows are validated before the payroll batch is created or changed.
    """

    lock_payroll_input_mutation(session)
    session.refresh(imported, with_for_update=True)
    if imported.source != SalarySource.IMPORT:
        raise ImportPublishError("仅人事 Excel 导入可以推送复核")
    if imported.status != ImportStatus.CONFIRMED:
        raise ImportPublishError("请先确认导入数据，再推送复核")
    if imported.published_batch_id is not None:
        return _published_summary(session, imported)
    if not imported.period:
        raise ImportPublishError("导入批次缺少计薪月份")

    records = list(
        session.scalars(
            select(SalaryRecord)
            .where(
                SalaryRecord.import_batch_id == imported.id,
                SalaryRecord.source == SalarySource.IMPORT,
            )
            .order_by(SalaryRecord.id)
            .with_for_update()
        ).all()
    )
    if not records:
        raise ImportPublishError("导入批次没有可推送的工资数据")
    employee_ids = {record.employee_id for record in records if record.employee_id is not None}
    employees = {
        employee.id: employee
        for employee in session.scalars(
            select(Employee)
            .where(Employee.id.in_(employee_ids), Employee.is_deleted.is_(False))
            .with_for_update()
        ).all()
    }
    org_unit_ids = {employee.org_unit_id for employee in employees.values()}
    org_units = {
        org_unit.id: org_unit
        for org_unit in session.scalars(
            select(OrgUnit).where(OrgUnit.id.in_(org_unit_ids)).with_for_update()
        ).all()
    }
    attendance_start, attendance_end = _period_dates(imported.period)
    validated = _validate_records(
        records,
        employees,
        org_units,
        period=imported.period,
        days_in_month=attendance_end.day,
    )

    payroll_batch = session.scalars(
        select(PayrollBatch).where(PayrollBatch.period == imported.period).with_for_update()
    ).first()
    if payroll_batch is None:
        payroll_batch = PayrollBatch(
            period=imported.period,
            attendance_start=attendance_start,
            attendance_end=attendance_end,
            status=BatchStatus.DRAFT,
        )
        session.add(payroll_batch)
        session.flush()
    elif payroll_batch.status != BatchStatus.DRAFT:
        raise ImportPublishError("同月薪资批次不是草稿状态，不能导入覆盖；请先按流程解锁/重开")

    current_results = (
        session.scalar(
            select(func.count())
            .select_from(PayrollResult)
            .where(
                PayrollResult.batch_id == payroll_batch.id,
                PayrollResult.batch_version == payroll_batch.version,
            )
        )
        or 0
    )
    current_confirmations = (
        session.scalar(
            select(func.count())
            .select_from(BatchConfirmation)
            .where(
                BatchConfirmation.batch_id == payroll_batch.id,
                BatchConfirmation.batch_version == payroll_batch.version,
            )
        )
        or 0
    )
    if current_results or current_confirmations:
        raise ImportPublishError("当前草稿版本已有工资结果，不能与 Excel 导入混合")

    prior_results = list(
        session.scalars(
            select(PayrollResult)
            .where(
                PayrollResult.batch_id == payroll_batch.id,
                PayrollResult.employee_id.in_({row.employee.id for row in validated}),
            )
            .with_for_update()
        ).all()
    )
    max_versions: dict[int, int] = {}
    identity_results: dict[int, PayrollResult] = {}
    for prior in prior_results:
        max_versions[prior.employee_id] = max(max_versions.get(prior.employee_id, 0), prior.version)
        current_identity = identity_results.get(prior.employee_id)
        if current_identity is None or (prior.batch_version, prior.version) > (
            current_identity.batch_version,
            current_identity.version,
        ):
            identity_results[prior.employee_id] = prior

    scopes: set[tuple[int, Department]] = set()
    for row in validated:
        prior_identity = (
            identity_results.get(row.employee.id) if payroll_batch.version > 1 else None
        )
        result = PayrollResult(
            batch_id=payroll_batch.id,
            batch_version=payroll_batch.version,
            employee_id=row.employee.id,
            source_import_batch_id=imported.id,
            version=max_versions.get(row.employee.id, 0) + 1,
            org_unit_id=row.source.org_unit_id,
            department=row.department,
            emp_no_snapshot=(
                prior_identity.emp_no_snapshot
                if prior_identity is not None
                else row.source.emp_no or row.employee.emp_no
            ),
            employee_name_snapshot=(
                prior_identity.employee_name_snapshot
                if prior_identity is not None
                else row.employee.name
            ),
            id_card_snapshot=(
                prior_identity.id_card_snapshot
                if prior_identity is not None
                else row.employee.id_card
            ),
            bank_account_snapshot=(
                prior_identity.bank_account_snapshot
                if prior_identity is not None
                else row.employee.bank_account
            ),
            social_city_snapshot=(
                prior_identity.social_city_snapshot
                if prior_identity is not None
                else row.employee.social_city
            ),
            actual_attendance_days=row.actual_attendance_days,
            statutory_holiday_days=row.statutory_holiday_days,
            statutory_holiday_worked_days=row.statutory_holiday_worked_days,
            gross=row.gross,
            deposit=row.deposit,
            net=row.net,
            carry_forward=row.carry_forward,
            deferred_deductions=_ZERO,
            deferred_deposit=_ZERO,
            rule_version=_IMPORT_RULE_VERSION,
            input_snapshot={
                "origin": "EXTERNAL_IMPORT",
                "import_batch_id": imported.id,
                "salary_record_id": row.source.id,
                "period": imported.period,
                "source_field_names": sorted(str(name) for name in row.source.fields),
            },
            lines=row.lines,
            exceptions=[],
            warnings=[_IMPORT_WARNING],
            has_error=False,
        )
        session.add(result)
        if row.source.org_unit_id is None:  # guarded during validation; keeps typing explicit
            raise ImportPublishError("工资记录缺少门店范围")
        scopes.add((row.source.org_unit_id, row.department))
    session.flush()

    for org_unit_id, department in sorted(scopes, key=lambda scope: (scope[0], scope[1].value)):
        session.add(
            BatchConfirmation(
                batch_id=payroll_batch.id,
                batch_version=payroll_batch.version,
                org_unit_id=org_unit_id,
                department=department,
            )
        )
    now = datetime.now(UTC)
    payroll_batch.status = BatchStatus.PENDING_STORE_CONFIRM
    payroll_batch.calculated_at = now
    imported.published_batch_id = payroll_batch.id
    imported.published_batch_version = payroll_batch.version
    imported.published_at = now
    session.flush()
    return ImportPublishSummary(
        import_batch_id=imported.id,
        payroll_batch_id=payroll_batch.id,
        batch_version=payroll_batch.version,
        employees=len(validated),
        scopes=len(scopes),
        already_published=False,
    )
