"""Audited payroll workbook exports with RBAC organization scoping."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.exporting.excel import payroll_workbook, tabular_workbook
from app.models.org import OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult
from app.payroll.social_tax import ContributionKind

router = APIRouter(prefix="/api/exports", tags=["exports"])

_MAX_EXPORT_ROWS = 10_000


def _valid_period_or_422(period: str) -> None:
    try:
        year, month = (int(value) for value in period.split("-"))
        date(year, month, 1)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="period must be a valid YYYY-MM month"
        ) from None


def _current_result_version():
    latest = aliased(PayrollResult)
    return (
        select(func.max(latest.version))
        .where(
            latest.batch_id == PayrollResult.batch_id,
            latest.batch_version == PayrollResult.batch_version,
            latest.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )


def _locked_current_results_statement(
    period: str,
    org_scope: frozenset[int] | None,
    *columns,
):
    statement = (
        select(*columns)
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .join(OrgUnit, OrgUnit.id == PayrollResult.org_unit_id)
        .where(
            PayrollBatch.period == period,
            PayrollBatch.status == BatchStatus.LOCKED,
            PayrollResult.batch_version == PayrollBatch.version,
            PayrollResult.version == _current_result_version(),
        )
        .order_by(OrgUnit.code, PayrollResult.emp_no_snapshot, PayrollResult.id)
        .limit(_MAX_EXPORT_ROWS + 1)
    )
    if org_scope is not None:
        # Empty scopes remain empty: never turn a lack of assignments into an
        # unrestricted export.
        statement = statement.where(PayrollResult.org_unit_id.in_(list(org_scope)))
    return statement


def _payroll_export_statement(period: str, org_scope: frozenset[int] | None):
    return _locked_current_results_statement(
        period,
        org_scope,
        PayrollBatch.period,
        PayrollResult.emp_no_snapshot,
        PayrollResult.employee_name_snapshot,
        OrgUnit.code,
        OrgUnit.name,
        PayrollResult.department,
        PayrollResult.actual_attendance_days,
        PayrollResult.gross,
        PayrollResult.deposit,
        PayrollResult.net,
        PayrollResult.carry_forward,
    )


def _workbook_rows(rows):
    return [
        (
            result_period,
            emp_no,
            name,
            org_code,
            org_name,
            department.value,
            attendance_days,
            gross,
            deposit,
            net,
            carry_forward,
        )
        for (
            result_period,
            emp_no,
            name,
            org_code,
            org_name,
            department,
            attendance_days,
            gross,
            deposit,
            net,
            carry_forward,
        ) in rows
    ]


def _workbook_response(content: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Exports can include payroll and PII.  Browsers/proxies must not
            # retain a downloadable copy beyond the authenticated request.
            "Cache-Control": "no-store",
        },
    )


def _intersect_scopes(
    first: frozenset[int] | None, second: frozenset[int] | None
) -> frozenset[int] | None:
    if first is None:
        return second
    if second is None:
        return first
    return first & second


def _pii_export_scope(session: Session, principal: Principal) -> frozenset[int] | None:
    """Restrict regulated-file rows to the intersection of export and PII grants."""
    if not principal.has_permission(Perm.EMPLOYEE_PII):
        raise HTTPException(
            status_code=403,
            detail="Regulatory and bank exports require employee:pii permission",
        )
    return _intersect_scopes(
        resolve_permission_org_scope(session, principal, Perm.EXPORT_DATA),
        resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_PII),
    )


def _regulatory_export_statement(period: str, org_scope: frozenset[int] | None):
    return _locked_current_results_statement(
        period,
        org_scope,
        PayrollBatch.period,
        PayrollResult.emp_no_snapshot,
        PayrollResult.employee_name_snapshot,
        PayrollResult.id_card_snapshot,
        PayrollResult.social_city_snapshot,
        PayrollResult.bank_account_snapshot,
        PayrollResult.net,
        PayrollResult.lines,
        PayrollResult.input_snapshot,
    )


def _decimal_or_export_error(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise HTTPException(status_code=409, detail=f"Invalid locked payroll value for {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(
            status_code=409, detail=f"Invalid locked payroll value for {field}"
        ) from None
    if not parsed.is_finite():
        raise HTTPException(status_code=409, detail=f"Invalid locked payroll value for {field}")
    return parsed


def _tax_snapshot_amount(snapshot: object, field: str) -> Decimal:
    if not isinstance(snapshot, Mapping):
        raise HTTPException(
            status_code=409, detail="Locked payroll result has invalid input snapshot"
        )
    tax_withholding = snapshot.get("tax_withholding")
    if not isinstance(tax_withholding, Mapping):
        raise HTTPException(
            status_code=409, detail="Locked payroll result has invalid tax snapshot"
        )
    if field not in tax_withholding or tax_withholding[field] is None:
        raise HTTPException(
            status_code=409, detail=f"Locked payroll result has no tax value for {field}"
        )
    amount = _decimal_or_export_error(tax_withholding[field], field)
    if amount < 0:
        raise HTTPException(
            status_code=409, detail=f"Locked payroll result has invalid tax value for {field}"
        )
    return amount


def _social_contribution_totals(snapshot: object) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Read explicit social-fund values from a locked result snapshot.

    Zero calculation lines are intentionally omitted from payslips.  The
    snapshot is therefore authoritative for regulatory files: absent data is a
    corrupted/legacy result and must block export instead of becoming a false
    zero declaration.
    """

    if not isinstance(snapshot, Mapping):
        raise HTTPException(
            status_code=409, detail="Locked payroll result has invalid input snapshot"
        )
    contributions = snapshot.get("social_contributions")
    if not isinstance(contributions, Mapping):
        raise HTTPException(
            status_code=409, detail="Locked payroll result has no social contribution snapshot"
        )
    amounts: dict[ContributionKind, tuple[Decimal, Decimal]] = {}
    for kind in ContributionKind:
        raw = contributions.get(kind.value)
        if not isinstance(raw, Mapping) or "employee" not in raw or "employer" not in raw:
            raise HTTPException(
                status_code=409,
                detail=f"Locked payroll result has no social contribution value for {kind.value}",
            )
        employee = _decimal_or_export_error(raw["employee"], f"{kind.value} employee")
        employer = _decimal_or_export_error(raw["employer"], f"{kind.value} employer")
        if employee < 0 or employer < 0:
            raise HTTPException(
                status_code=409,
                detail=f"Locked payroll result has invalid social contribution for {kind.value}",
            )
        amounts[kind] = (employee, employer)
    social_kinds = tuple(kind for kind in ContributionKind if kind is not ContributionKind.HOUSING)
    social_employee = sum((amounts[kind][0] for kind in social_kinds), Decimal(0))
    social_employer = sum((amounts[kind][1] for kind in social_kinds), Decimal(0))
    housing_employee, housing_employer = amounts[ContributionKind.HOUSING]
    return social_employee, social_employer, housing_employee, housing_employer


def _require_nonblank_value(rows: list[tuple], column_index: int, field: str) -> None:
    missing_count = sum(
        1 for row in rows if not isinstance(row[column_index], str) or not row[column_index].strip()
    )
    if missing_count:
        raise HTTPException(
            status_code=422,
            detail=f"{field} export is blocked: {missing_count} employee(s) have no {field}",
        )


def _social_insurance_rows(rows: list[tuple]) -> list[tuple[object, ...]]:
    _require_nonblank_value(rows, 3, "identity card")
    _require_nonblank_value(rows, 4, "social city")
    return [
        (
            period,
            emp_no,
            name,
            id_card,
            social_city,
            *_social_contribution_totals(snapshot),
        )
        for (
            period,
            emp_no,
            name,
            id_card,
            social_city,
            _bank_account,
            _net,
            _lines,
            snapshot,
        ) in rows
    ]


def _individual_income_tax_rows(rows: list[tuple]) -> list[tuple[object, ...]]:
    _require_nonblank_value(rows, 3, "identity card")
    return [
        (
            period,
            emp_no,
            name,
            id_card,
            social_city,
            _tax_snapshot_amount(snapshot, "current_taxable_income"),
            _tax_snapshot_amount(snapshot, "current_employee_contribution"),
            _tax_snapshot_amount(snapshot, "current_tax_withheld"),
        )
        for period, emp_no, name, id_card, social_city, _bank_account, _net, lines, snapshot in rows
    ]


def _bank_payment_rows(rows: list[tuple]) -> list[tuple[object, ...]]:
    _require_nonblank_value(rows, 5, "bank account")
    return [
        (period, emp_no, name, bank_account, net)
        for (
            period,
            emp_no,
            name,
            _id_card,
            _social_city,
            bank_account,
            net,
            _lines,
            _snapshot,
        ) in rows
    ]


def _record_regulatory_export(
    session: Session,
    *,
    principal: Principal,
    action: str,
    period: str,
    row_count: int,
) -> None:
    audit.record(
        session,
        action=action,
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        detail={"period": period, "rows": row_count, "format": "generic-xlsx"},
    )
    session.commit()


@router.get("/payroll")
def export_payroll(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.EXPORT_DATA)),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    _valid_period_or_422(period)
    org_scope = resolve_permission_org_scope(session, principal, Perm.EXPORT_DATA)
    rows = session.execute(_payroll_export_statement(period, org_scope)).all()
    if len(rows) > _MAX_EXPORT_ROWS:
        raise HTTPException(status_code=413, detail="export exceeds the 10,000-row safety limit")
    content = payroll_workbook(_workbook_rows(rows))
    audit.record(
        session,
        action="export.payroll",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        detail={"period": period, "rows": len(rows), "format": "xlsx"},
    )
    session.commit()
    return _workbook_response(content, f"payroll-{period}.xlsx")


@router.get("/social-insurance")
def export_social_insurance(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.EXPORT_DATA)),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Export a generic, non-official social-insurance reconciliation workbook."""
    _valid_period_or_422(period)
    rows = [
        tuple(row)
        for row in session.execute(
            _regulatory_export_statement(period, _pii_export_scope(session, principal))
        ).all()
    ]
    if len(rows) > _MAX_EXPORT_ROWS:
        raise HTTPException(status_code=413, detail="export exceeds the 10,000-row safety limit")
    content = tabular_workbook(
        sheet_title="Social Insurance",
        headers=(
            "Period",
            "Employee No",
            "Employee Name",
            "Identity Card",
            "Social City",
            "Employee Social Insurance",
            "Employer Social Insurance",
            "Employee Housing Fund",
            "Employer Housing Fund",
        ),
        rows=_social_insurance_rows(rows),
        text_columns={0, 1, 2, 3, 4},
    )
    _record_regulatory_export(
        session,
        principal=principal,
        action="export.social_insurance",
        period=period,
        row_count=len(rows),
    )
    return _workbook_response(content, f"social-insurance-{period}.xlsx")


@router.get("/individual-income-tax")
def export_individual_income_tax(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.EXPORT_DATA)),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Export a generic monthly IIT reconciliation workbook, not a filing declaration."""
    _valid_period_or_422(period)
    rows = [
        tuple(row)
        for row in session.execute(
            _regulatory_export_statement(period, _pii_export_scope(session, principal))
        ).all()
    ]
    if len(rows) > _MAX_EXPORT_ROWS:
        raise HTTPException(status_code=413, detail="export exceeds the 10,000-row safety limit")
    content = tabular_workbook(
        sheet_title="Individual Income Tax",
        headers=(
            "Period",
            "Employee No",
            "Employee Name",
            "Identity Card",
            "Social City",
            "Current Taxable Income",
            "Employee Contribution",
            "IIT Withholding",
        ),
        rows=_individual_income_tax_rows(rows),
        text_columns={0, 1, 2, 3, 4},
    )
    _record_regulatory_export(
        session,
        principal=principal,
        action="export.individual_income_tax",
        period=period,
        row_count=len(rows),
    )
    return _workbook_response(content, f"individual-income-tax-{period}.xlsx")


@router.get("/bank-payment")
def export_bank_payment(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.EXPORT_DATA)),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Export a generic bank-payment workbook; bank-specific layouts remain configurable."""
    _valid_period_or_422(period)
    rows = [
        tuple(row)
        for row in session.execute(
            _regulatory_export_statement(period, _pii_export_scope(session, principal))
        ).all()
    ]
    if len(rows) > _MAX_EXPORT_ROWS:
        raise HTTPException(status_code=413, detail="export exceeds the 10,000-row safety limit")
    content = tabular_workbook(
        sheet_title="Bank Payment",
        headers=("Period", "Employee No", "Employee Name", "Bank Account", "Payment Amount"),
        rows=_bank_payment_rows(rows),
        text_columns={0, 1, 2, 3},
    )
    _record_regulatory_export(
        session,
        principal=principal,
        action="export.bank_payment",
        period=period,
        row_count=len(rows),
    )
    return _workbook_response(content, f"bank-payment-{period}.xlsx")
