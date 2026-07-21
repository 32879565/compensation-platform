"""S13c 薪资批次状态机端到端测试：核算→门店确认→异议→重算→锁定/解锁。"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.auth.service import build_principal, resolve_permission_org_scope
from app.comp.service import set_component_amount
from app.core.security import hash_password
from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule, PerformanceRecord
from app.models.audit import AuditLog
from app.models.auth import (
    Permission,
    Role,
    RolePermission,
    User,
    UserOrgScope,
    UserReviewScope,
    UserRole,
)
from app.models.comp import ComponentType, EmployeeSalaryStructure, SalaryComponentDef
from app.models.dingtalk import DingTalkDelivery, DingTalkDeliveryStatus
from app.models.employee import Department, Employee
from app.models.holiday import HolidayCalendarPeriod, HolidayWorkRecord, StatutoryHolidayDate
from app.models.org import OrgType, OrgUnit
from app.models.payroll_adjustment import (
    MonthlyPayrollAdjustment,
    MonthlyPayrollAdjustmentRevision,
    PayrollAdjustmentType,
)
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import EmployeeTaxYtdOpening, PayrollPolicy
from app.models.payroll_result import AdjustmentRecord, CompDispute, DisputeStatus, PayrollResult
from app.payroll.social_tax import ContributionKind

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _user(session, username, role_codes=(), scope_ids=(), review_scopes=()):
    seed_rbac(session)
    u = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(u)
    session.flush()
    for code in role_codes:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=u.id, role_id=role.id))
    for oid in scope_ids:
        session.add(UserOrgScope(user_id=u.id, org_unit_id=oid))
    for org_unit_id, department in review_scopes:
        session.add(
            UserReviewScope(
                user_id=u.id,
                org_unit_id=org_unit_id,
                department=department,
            )
        )
    session.flush()
    return u


def _token(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "StrongPass123!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _employee(
    session,
    store,
    emp_no,
    name,
    amount="5000",
    expected="22",
    rest="0",
    expected_days_rule_id=None,
):
    emp = Employee(
        emp_no=emp_no,
        name=name,
        org_unit_id=store.id,
        department=Department.OTHER,
        social_city=store.city,
        hire_date=date(2026, 1, 1),
    )
    session.add(emp)
    session.flush()
    comp = session.scalars(
        select(SalaryComponentDef).where(SalaryComponentDef.code == "COMP")
    ).first()
    if comp is None:
        comp = SalaryComponentDef(
            code="COMP", name="综合薪资", component_type=ComponentType.COMPREHENSIVE
        )
        session.add(comp)
        session.flush()
    set_component_amount(
        session,
        employee_id=emp.id,
        component_id=comp.id,
        amount=Decimal(amount),
        effective_from=date(2026, 1, 1),
    )
    session.add(
        AttendanceRecord(
            employee_id=emp.id,
            period="2026-05",
            generated_expected_days=(
                Decimal(expected) if expected_days_rule_id is not None else None
            ),
            expected_days_rule_id=expected_days_rule_id,
            expected_days=Decimal(expected),
            actual_days=Decimal(expected),
            rest_days=Decimal(rest),
        )
    )
    session.flush()
    return emp


def _inert_policy(city: str) -> PayrollPolicy:
    return PayrollPolicy(
        city=city,
        effective_from=date(2026, 1, 1),
        social_rules=[
            {
                "kind": kind.value,
                "employee_rate": "0",
                "employer_rate": "0",
                "base_min": "0",
                "base_max": None,
            }
            for kind in ContributionKind
        ],
        monthly_basic_deduction=Decimal("5000"),
        tax_brackets=[{"upper_bound": None, "rate": "0", "quick_deduction": "0"}],
        derived_income_rules=[
            {
                "code": "OVERTIME",
                "taxable": False,
                "in_social_base": False,
                "in_housing_base": False,
            },
            {
                "code": "HOLIDAY",
                "taxable": False,
                "in_social_base": False,
                "in_housing_base": False,
            },
        ],
        is_finalized=True,
    )


@pytest.fixture
def world(db_session):
    """两店两员工：广州店 emp1、深圳店 emp2，均 OTHER 部门。"""
    group = OrgUnit(code="G", name="集团", type=OrgType.GROUP)
    db_session.add(group)
    db_session.flush()
    r1 = OrgUnit(code="R1", name="广州", type=OrgType.REGION, parent_id=group.id)
    db_session.add(r1)
    db_session.flush()
    s1 = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, parent_id=r1.id, city="广州")
    s2 = OrgUnit(code="S2", name="深圳店", type=OrgType.STORE, parent_id=r1.id, city="深圳")
    db_session.add_all([s1, s2])
    db_session.flush()
    rule = ExpectedAttendanceRule(
        name="default test schedule",
        weekly_rest_days=[],
        monthly_expected_days=Decimal("22"),
        effective_from=date(2026, 1, 1),
    )
    db_session.add(rule)
    emp1 = _employee(db_session, s1, "E1", "张三")
    emp2 = _employee(db_session, s2, "E2", "李四")
    db_session.flush()
    opening_auditor = User(
        username="opening-auditor",
        password_hash=hash_password("StrongPass123!"),
    )
    db_session.add(opening_auditor)
    db_session.flush()
    for attendance in db_session.scalars(select(AttendanceRecord)).all():
        attendance.generated_expected_days = attendance.expected_days
        attendance.expected_days_rule_id = rule.id
    # A finalized empty calendar is the normal payroll precondition. Individual
    # tests add daily holiday/work records when their calculation requires them.
    db_session.add_all(
        [
            HolidayCalendarPeriod(period="2026-05", is_finalized=True),
            _inert_policy("广州"),
            _inert_policy("深圳"),
            *[
                EmployeeTaxYtdOpening(
                    employee_id=employee.id,
                    tax_year=2026,
                    through_period="2026-04",
                    employment_months_to_date=4,
                    taxable_income=Decimal("0"),
                    employee_contribution=Decimal("0"),
                    special_deduction=Decimal("0"),
                    tax_withheld=Decimal("0"),
                    evidence_ref="test tax opening",
                    is_finalized=True,
                    finalized_by=opening_auditor.id,
                    finalized_at=datetime.now(UTC),
                )
                for employee in (emp1, emp2)
            ],
        ]
    )
    db_session.flush()
    openings = {
        opening.employee_id: opening
        for opening in db_session.scalars(select(EmployeeTaxYtdOpening)).all()
    }
    return {
        "s1": s1,
        "s2": s2,
        "emp1": emp1,
        "emp2": emp2,
        "schedule_rule": rule,
        "tax_openings": openings,
    }


def _create_and_run(client, headers):
    r = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )
    assert r.status_code == 201, r.text
    batch_id = r.json()["id"]
    r = client.post(f"/api/batches/{batch_id}/run", headers=headers)
    assert r.status_code == 200, r.text
    return batch_id, r.json()


def _create_and_run_period(client, headers, *, period, attendance_start, attendance_end):
    r = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": period,
            "attendance_start": attendance_start,
            "attendance_end": attendance_end,
        },
    )
    assert r.status_code == 201, r.text
    batch_id = r.json()["id"]
    r = client.post(f"/api/batches/{batch_id}/run", headers=headers)
    assert r.status_code == 200, r.text
    return batch_id


def _store_reviewer_headers(client, session, world):
    """Create one explicitly scoped reviewer for each store in the test world."""
    reviewers = {}
    for index, store in enumerate((world["s1"], world["s2"]), start=1):
        username = f"sm{index}"
        _user(
            session,
            username,
            ["STORE_MANAGER"],
            scope_ids=[store.id],
            review_scopes=[(store.id, Department.OTHER)],
        )
        reviewers[store.id] = _token(client, username)
    return reviewers


def _scoped_payroll_lifecycle_role(session) -> None:
    """Create a non-global custom role with every lifecycle permission."""
    seed_rbac(session)
    role = Role(
        code="SCOPED_PAYROLL_LIFECYCLE",
        name="Scoped payroll lifecycle operator",
        is_global_scope=False,
    )
    session.add(role)
    session.flush()
    for permission_code in (Perm.PAYROLL_RUN, Perm.PAYROLL_APPROVE, Perm.PAYROLL_CORRECT):
        permission_id = session.scalars(
            select(Permission.id).where(Permission.code == permission_code)
        ).one()
        session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    session.flush()


def _newer_payroll_result_snapshot(
    session, result: PayrollResult, *, org_unit_id: int
) -> PayrollResult:
    """Persist a divergent later snapshot to exercise latest-version scope checks."""
    newer = PayrollResult(
        batch_id=result.batch_id,
        employee_id=result.employee_id,
        batch_version=result.batch_version,
        version=result.version + 1,
        org_unit_id=org_unit_id,
        department=result.department,
        actual_attendance_days=result.actual_attendance_days,
        statutory_holiday_days=result.statutory_holiday_days,
        statutory_holiday_worked_days=result.statutory_holiday_worked_days,
        gross=result.gross,
        deposit=result.deposit,
        net=result.net,
        carry_forward=result.carry_forward,
        deferred_deductions=result.deferred_deductions,
        deferred_deposit=result.deferred_deposit,
        rule_version=result.rule_version,
        input_snapshot=dict(result.input_snapshot),
        lines=list(result.lines),
        exceptions=list(result.exceptions),
        warnings=list(result.warnings),
        has_error=result.has_error,
    )
    session.add(newer)
    session.flush()
    return newer


def _copy_payroll_result_to_round(
    session, result: PayrollResult, *, batch_version: int, version: int
) -> PayrollResult:
    copied = PayrollResult(
        batch_id=result.batch_id,
        employee_id=result.employee_id,
        batch_version=batch_version,
        version=version,
        org_unit_id=result.org_unit_id,
        department=result.department,
        actual_attendance_days=result.actual_attendance_days,
        statutory_holiday_days=result.statutory_holiday_days,
        statutory_holiday_worked_days=result.statutory_holiday_worked_days,
        gross=result.gross,
        deposit=result.deposit,
        net=result.net,
        carry_forward=result.carry_forward,
        deferred_deductions=result.deferred_deductions,
        deferred_deposit=result.deferred_deposit,
        rule_version=result.rule_version,
        input_snapshot=dict(result.input_snapshot),
        lines=list(result.lines),
        exceptions=list(result.exceptions),
        warnings=list(result.warnings),
        has_error=result.has_error,
    )
    session.add(copied)
    session.flush()
    return copied


def test_create_batch_acquires_payroll_input_lock_before_staging_batch(
    client, db_session, monkeypatch
):
    from app.routers import batch as batch_router

    _user(db_session, "batch-lock-hr", ["GROUP_HR"])
    headers = _token(client, "batch-lock-hr")
    lock_calls = []

    def record_lock(session):
        assert session is db_session
        assert not any(isinstance(pending, PayrollBatch) for pending in session.new)
        lock_calls.append(session)

    monkeypatch.setattr(
        batch_router,
        "lock_payroll_input_mutation",
        record_lock,
        raising=False,
    )

    response = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )

    assert response.status_code == 201, response.text
    assert lock_calls == [db_session]


_GLOBAL_BATCH_LIFECYCLE_ACTIONS = (
    pytest.param("run", None, Perm.PAYROLL_RUN, id="run"),
    pytest.param("approve", None, Perm.PAYROLL_APPROVE, id="approve"),
    pytest.param("lock", None, Perm.PAYROLL_APPROVE, id="lock"),
    pytest.param("unlock", {"reason": "scope regression"}, Perm.PAYROLL_CORRECT, id="unlock"),
    pytest.param("reopen", {"reason": "scope regression"}, Perm.PAYROLL_CORRECT, id="reopen"),
)


def test_run_creates_results_and_confirmations(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    batch_id, body = _create_and_run(client, h)
    assert body["employees"] == 2
    assert body["status"] == "PENDING_STORE_CONFIRM"

    r = client.get(f"/api/batches/{batch_id}/results", headers=h)
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 2
    grosses = sorted(x["gross"] for x in results)
    assert grosses == ["5000.00", "5000.00"]
    assert all(x["version"] == 1 for x in results)
    assert all(x["statutory_holiday_pay"] == "0.00" for x in results)


def test_result_identity_remains_the_calculation_snapshot_after_masterdata_change(
    client, db_session, world
):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    batch_id, _ = _create_and_run(client, headers)

    initial = {
        row["employee_id"]: (row["emp_no"], row["employee_name"])
        for row in client.get(f"/api/batches/{batch_id}/results", headers=headers).json()
    }
    employee = world["emp1"]
    employee.emp_no = "RENAMED-NO"
    employee.name = "调店后姓名"
    db_session.commit()

    current = {
        row["employee_id"]: (row["emp_no"], row["employee_name"])
        for row in client.get(f"/api/batches/{batch_id}/results", headers=headers).json()
    }
    assert current[employee.id] == initial[employee.id]


def test_run_stages_only_exact_review_scope_deliveries_in_sandbox(client, db_session, world):
    hr = _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "store-one-reviewer",
        ["STORE_MANAGER"],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    _user(
        db_session,
        "store-two-reviewer",
        ["STORE_MANAGER"],
        review_scopes=[(world["s2"].id, Department.OTHER)],
    )

    batch_id, _body = _create_and_run(client, _token(client, hr.username))

    deliveries = list(
        db_session.scalars(
            select(DingTalkDelivery)
            .where(DingTalkDelivery.batch_id == batch_id)
            .order_by(DingTalkDelivery.org_unit_id)
        ).all()
    )
    assert [delivery.org_unit_id for delivery in deliveries] == [world["s1"].id, world["s2"].id]
    assert all(delivery.status is DingTalkDeliveryStatus.SANDBOXED for delivery in deliveries)
    audit_row = db_session.scalars(
        select(AuditLog).where(
            AuditLog.actor_user_id == hr.id,
            AuditLog.action == "dingtalk.review.stage",
        )
    ).one()
    assert audit_row.detail == {
        "sandbox": True,
        "routed": 2,
        "configuration_failures": 0,
        "existing": 0,
    }


def test_sensitive_batch_reads_are_audited(client, db_session, world):
    hr = _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    batch_id, _ = _create_and_run(client, headers)

    response = client.get("/api/batches", headers=headers)
    assert response.status_code == 200, response.text
    for endpoint in ("results", "confirmations", "disputes", "adjustments"):
        response = client.get(f"/api/batches/{batch_id}/{endpoint}", headers=headers)
        assert response.status_code == 200, response.text

    entries = db_session.scalars(
        select(AuditLog)
        .where(
            AuditLog.actor_user_id == hr.id,
            AuditLog.action.in_(
                [
                    "batch.list.view",
                    "batch.results.view",
                    "batch.confirmations.view",
                    "batch.disputes.view",
                    "batch.adjustments.view",
                ]
            ),
        )
        .order_by(AuditLog.id)
    ).all()
    assert [entry.action for entry in entries] == [
        "batch.list.view",
        "batch.results.view",
        "batch.confirmations.view",
        "batch.disputes.view",
        "batch.adjustments.view",
    ]
    assert all(entry.target_type == "payroll_batch" for entry in entries)
    assert entries[0].target_id is None
    assert all(entry.target_id == batch_id for entry in entries[1:])
    assert [entry.detail["returned"] for entry in entries] == [1, 2, 2, 0, 0]


def test_next_month_settles_prior_unpaid_wages_deferred_deduction_and_deposit(
    client, db_session, world
):
    """首月不足 600 时，下月一次结清工资、延后扣款和押金。"""
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee = world["emp1"]
    employee.department = Department.OTHER
    employee.is_special_position = False
    employee.hire_date = date(2026, 5, 1)
    tax_opening = world["tax_openings"][employee.id]
    tax_opening.is_finalized = False
    tax_opening.finalized_by = None
    tax_opening.finalized_at = None

    may_attendance = db_session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id,
            AttendanceRecord.period == "2026-05",
        )
    ).one()
    may_attendance.expected_days = Decimal("26")
    may_attendance.expected_days_adjust_reason = "test payroll exception"
    may_attendance.actual_days = Decimal("5")
    may_attendance.worked_hours = Decimal("45")

    comp = db_session.scalars(
        select(SalaryComponentDef).where(SalaryComponentDef.code == "COMP")
    ).one()
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=comp.id,
        amount=Decimal("650"),
        effective_from=date(2026, 5, 1),
    )
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=comp.id,
        amount=Decimal("1000"),
        effective_from=date(2026, 6, 1),
    )
    deferred_deduction = SalaryComponentDef(
        code="D1_DEFERRED_DEDUCTION",
        name="D1 deferred deduction",
        component_type=ComponentType.DEDUCTION,
    )
    db_session.add(deferred_deduction)
    db_session.flush()
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=deferred_deduction.id,
        amount=Decimal("25"),
        effective_from=date(2026, 5, 1),
    )
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=deferred_deduction.id,
        amount=Decimal("0"),
        effective_from=date(2026, 6, 1),
    )
    db_session.add(
        AttendanceRecord(
            employee_id=employee.id,
            period="2026-06",
            generated_expected_days=Decimal("26"),
            expected_days_rule_id=world["schedule_rule"].id,
            expected_days=Decimal("26"),
            expected_days_adjust_reason="test payroll exception",
            actual_days=Decimal("26"),
            worked_hours=Decimal("234"),
        )
    )
    db_session.add(HolidayCalendarPeriod(period="2026-06", is_finalized=True))
    db_session.flush()

    may_batch_id = _create_and_run_period(
        client,
        headers,
        period="2026-05",
        attendance_start="2026-05-01",
        attendance_end="2026-05-31",
    )
    may_result = next(
        result
        for result in client.get(f"/api/batches/{may_batch_id}/results", headers=headers).json()
        if result["employee_id"] == employee.id
    )
    assert may_result["gross"] == "125.00"
    assert may_result["deposit"] == "0.00"
    assert may_result["net"] == "0.00"
    assert may_result["carry_forward"] == "125.00"

    # Only an approved and locked May result may create a carry obligation for June.
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    for store in (world["s1"], world["s2"]):
        response = client.post(
            f"/api/batches/{may_batch_id}/confirm",
            headers=reviewer_headers[store.id],
            json={"org_unit_id": store.id, "department": "OTHER"},
        )
        assert response.status_code == 200, response.text
    response = client.post(f"/api/batches/{may_batch_id}/approve", headers=headers)
    assert response.status_code == 200, response.text
    response = client.post(f"/api/batches/{may_batch_id}/lock", headers=headers)
    assert response.status_code == 200, response.text

    june_batch_id = _create_and_run_period(
        client,
        headers,
        period="2026-06",
        attendance_start="2026-06-01",
        attendance_end="2026-06-30",
    )
    june_result = next(
        result
        for result in client.get(f"/api/batches/{june_batch_id}/results", headers=headers).json()
        if result["employee_id"] == employee.id
    )

    # June 1,000 + unpaid May wage 125 - deferred May deduction 25 - deposit 600.
    assert june_result["gross"] == "1125.00"
    assert june_result["deposit"] == "600.00"
    assert june_result["net"] == "500.00"
    assert june_result["carry_forward"] == "0.00"


def test_run_requires_all_started_prior_periods_to_be_locked(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    _create_and_run(client, headers)
    june_batch = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-06",
            "attendance_start": "2026-06-01",
            "attendance_end": "2026-06-30",
        },
    )
    assert june_batch.status_code == 201, june_batch.text

    response = client.post(f"/api/batches/{june_batch.json()['id']}/run", headers=headers)

    assert response.status_code == 409
    assert "earlier payroll batch" in response.json()["detail"].lower()


def test_lock_rejects_an_earlier_period_after_later_review_has_started(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    may_batch_id, _ = _create_and_run(client, headers)
    for store in (world["s1"], world["s2"]):
        assert (
            client.post(
                f"/api/batches/{may_batch_id}/confirm",
                headers=reviewer_headers[store.id],
                json={"org_unit_id": store.id, "department": "OTHER"},
            ).status_code
            == 200
        )
    assert client.post(f"/api/batches/{may_batch_id}/approve", headers=headers).status_code == 200
    db_session.add(
        PayrollBatch(
            period="2026-06",
            attendance_start=date(2026, 6, 1),
            attendance_end=date(2026, 6, 30),
            status=BatchStatus.PENDING_STORE_CONFIRM,
            version=1,
        )
    )
    db_session.flush()

    response = client.post(f"/api/batches/{may_batch_id}/lock", headers=headers)

    assert response.status_code == 409
    assert "later payroll batch" in response.json()["detail"].lower()


def test_run_blocks_when_a_leaver_has_an_unsettled_locked_carry(client, db_session, world):
    """A carry cannot silently disappear merely because its employee left."""
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    leaver = world["emp1"]
    leaver.leave_date = date(2026, 5, 31)
    prior_batch = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.LOCKED,
        version=1,
    )
    db_session.add(prior_batch)
    db_session.flush()
    db_session.add(
        PayrollResult(
            batch_id=prior_batch.id,
            employee_id=leaver.id,
            batch_version=1,
            version=1,
            org_unit_id=leaver.org_unit_id,
            department=leaver.department,
            actual_attendance_days=Decimal("5"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("125"),
            deposit=Decimal("0"),
            net=Decimal("0"),
            carry_forward=Decimal("125"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("600"),
            rule_version="v2",
            input_snapshot={"employee_id": leaver.id, "period": "2026-05"},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    continuing_employee = world["emp2"]
    db_session.add(
        AttendanceRecord(
            employee_id=continuing_employee.id,
            period="2026-06",
            generated_expected_days=Decimal("22"),
            expected_days_rule_id=world["schedule_rule"].id,
            expected_days=Decimal("22"),
            actual_days=Decimal("22"),
        )
    )
    db_session.add(HolidayCalendarPeriod(period="2026-06", is_finalized=True))
    db_session.flush()

    june_batch_id = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-06",
            "attendance_start": "2026-06-01",
            "attendance_end": "2026-06-30",
        },
    ).json()["id"]
    response = client.post(f"/api/batches/{june_batch_id}/run", headers=headers)

    assert response.status_code == 409
    assert "outstanding carry" in response.json()["detail"].lower()


def test_run_uses_latest_locked_result_before_checking_excluded_carry(client, db_session, world):
    """A later locked settlement clears an older carry for a departed employee."""
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    leaver = world["emp1"]
    leaver.leave_date = date(2026, 5, 31)

    def add_locked_result(period: str, carry_forward: str, deferred_deposit: str) -> None:
        month = int(period[-2:])
        prior_batch = PayrollBatch(
            period=period,
            attendance_start=date(2026, month, 1),
            attendance_end=date(2026, month, 28),
            status=BatchStatus.LOCKED,
            version=1,
        )
        db_session.add(prior_batch)
        db_session.flush()
        db_session.add(
            PayrollResult(
                batch_id=prior_batch.id,
                employee_id=leaver.id,
                batch_version=1,
                version=1,
                org_unit_id=leaver.org_unit_id,
                department=leaver.department,
                actual_attendance_days=Decimal("5"),
                statutory_holiday_days=Decimal("0"),
                statutory_holiday_worked_days=Decimal("0"),
                gross=Decimal(carry_forward),
                deposit=Decimal("0"),
                net=Decimal("0"),
                carry_forward=Decimal(carry_forward),
                deferred_deductions=Decimal("0"),
                deferred_deposit=Decimal(deferred_deposit),
                rule_version="v2",
                input_snapshot={"employee_id": leaver.id, "period": period},
                lines=[],
                exceptions=[],
                warnings=[],
                has_error=False,
            )
        )

    add_locked_result("2026-05", "125", "600")
    add_locked_result("2026-06", "0", "0")
    continuing_employee = world["emp2"]
    db_session.add(
        AttendanceRecord(
            employee_id=continuing_employee.id,
            period="2026-07",
            generated_expected_days=Decimal("22"),
            expected_days_rule_id=world["schedule_rule"].id,
            expected_days=Decimal("22"),
            actual_days=Decimal("22"),
        )
    )
    db_session.add(HolidayCalendarPeriod(period="2026-07", is_finalized=True))
    db_session.flush()

    july_batch = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-07",
            "attendance_start": "2026-07-01",
            "attendance_end": "2026-07-31",
        },
    )
    assert july_batch.status_code == 201, july_batch.text
    response = client.post(f"/api/batches/{july_batch.json()['id']}/run", headers=headers)

    assert response.status_code == 200, response.text


def test_batch_uses_finalized_daily_holiday_calendar_with_hire_boundary(client, db_session, world):
    """日历确认后，仅入职日及之后的法定日参与该员工的批次核算。"""
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee = world["emp1"]
    employee.hire_date = date(2026, 5, 2)
    tax_opening = world["tax_openings"][employee.id]
    tax_opening.is_finalized = False
    tax_opening.finalized_by = None
    tax_opening.finalized_at = None
    attendance = db_session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id,
            AttendanceRecord.period == "2026-05",
        )
    ).one()
    attendance.expected_days_adjust_reason = "test holiday basis"
    db_session.add_all(
        [
            StatutoryHolidayDate(
                holiday_date=date(2026, 5, 1),
                name="May Day Eve",
                eligible_employment_types=["FULL_TIME"],
            ),
            StatutoryHolidayDate(
                holiday_date=date(2026, 5, 2),
                name="May Day",
                eligible_employment_types=["FULL_TIME"],
            ),
            HolidayWorkRecord(
                employee_id=employee.id,
                holiday_date=date(2026, 5, 2),
                worked=True,
                reason="Approved shift record",
            ),
        ]
    )
    db_session.flush()

    batch_id, _ = _create_and_run(client, headers)
    result = next(
        item
        for item in client.get(f"/api/batches/{batch_id}/results", headers=headers).json()
        if item["employee_id"] == employee.id
    )

    # May 1 is before hire and excluded; May 2 is worked and paid at 3x:
    # 3000 / 22 * 3 = 409.09.
    assert result["has_error"] is False
    assert result["statutory_holiday_days"] == "1.00"
    assert result["statutory_holiday_worked_days"] == "1.00"
    holiday_line = next(line for line in result["lines"] if line["code"] == "HOLIDAY")
    assert holiday_line["amount"] == "409.09"
    assert result["statutory_holiday_pay"] == "409.09"


def test_full_happy_path_confirm_then_lock(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, h)

    # Final HR is a global reader/approver, never an unrestricted store reviewer.
    assert (
        client.post(
            f"/api/batches/{batch_id}/confirm",
            headers=h,
            json={"org_unit_id": world["s1"].id, "department": "OTHER"},
        ).status_code
        == 403
    )

    for store in (world["s1"], world["s2"]):
        r = client.post(
            f"/api/batches/{batch_id}/confirm",
            headers=reviewer_headers[store.id],
            json={"org_unit_id": store.id, "department": "OTHER"},
        )
        assert r.status_code == 200, r.text
    assert r.json()["batch_status"] == "PENDING_HR"

    r = client.post(f"/api/batches/{batch_id}/approve", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "CONFIRMED"

    r = client.post(f"/api/batches/{batch_id}/lock", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "LOCKED"


def test_batch_output_projects_lifecycle_dimensions_and_actor_metadata(client, db_session, world):
    hr = _user(db_session, "lifecycle-hr", ["GROUP_HR"])
    headers = _token(client, hr.username)
    reviewer_headers = _store_reviewer_headers(client, db_session, world)

    created = client.post(
        "/api/batches",
        headers=headers,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    initial_expected = {
        "id": batch_id,
        "period": "2026-05",
        "status": "DRAFT",
        "calculation_status": "PENDING",
        "store_confirmation_status": "NOT_STARTED",
        "hr_review_status": "NOT_STARTED",
        "lock_status": "UNLOCKED",
        "calculated_at": None,
        "hr_reviewed_by": None,
        "hr_reviewed_at": None,
        "locked_by": None,
        "locked_at": None,
    }
    assert {key: created.json()[key] for key in initial_expected} == initial_expected

    assert client.post(f"/api/batches/{batch_id}/run", headers=headers).status_code == 200
    calculated = client.get("/api/batches", headers=headers).json()[0]
    assert calculated["status"] == "PENDING_STORE_CONFIRM"
    assert calculated["calculation_status"] == "CALCULATED"
    assert calculated["store_confirmation_status"] == "PENDING"
    assert calculated["hr_review_status"] == "NOT_STARTED"
    assert calculated["lock_status"] == "UNLOCKED"
    assert calculated["calculated_at"] is not None

    for store in (world["s1"], world["s2"]):
        response = client.post(
            f"/api/batches/{batch_id}/confirm",
            headers=reviewer_headers[store.id],
            json={"org_unit_id": store.id, "department": "OTHER"},
        )
        assert response.status_code == 200, response.text
    pending_hr = client.get("/api/batches", headers=headers).json()[0]
    assert pending_hr["store_confirmation_status"] == "CONFIRMED"
    assert pending_hr["hr_review_status"] == "PENDING"

    assert client.post(f"/api/batches/{batch_id}/approve", headers=headers).status_code == 200
    approved = client.get("/api/batches", headers=headers).json()[0]
    assert approved["status"] == "CONFIRMED"
    assert approved["hr_review_status"] == "APPROVED"
    assert approved["hr_reviewed_by"] == hr.id
    assert approved["hr_reviewed_at"] is not None

    assert client.post(f"/api/batches/{batch_id}/lock", headers=headers).status_code == 200
    locked = client.get("/api/batches", headers=headers).json()[0]
    assert locked["lock_status"] == "LOCKED"
    assert locked["locked_by"] == hr.id
    assert locked["locked_at"] is not None

    unlocked = client.post(
        f"/api/batches/{batch_id}/unlock",
        headers=headers,
        json={"reason": "Recalculate the active review round"},
    )
    assert unlocked.status_code == 200, unlocked.text
    reopened = client.get("/api/batches", headers=headers).json()[0]
    assert reopened["version"] == 2
    assert reopened["status"] == "DRAFT"
    assert reopened["calculation_status"] == "PENDING"
    assert reopened["store_confirmation_status"] == "NOT_STARTED"
    assert reopened["hr_review_status"] == "NOT_STARTED"
    assert reopened["lock_status"] == "UNLOCKED"
    assert reopened["calculated_at"] is None
    assert reopened["hr_reviewed_by"] is None
    assert reopened["hr_reviewed_at"] is None
    assert reopened["locked_by"] is None
    assert reopened["locked_at"] is None


def test_adjustment_history_is_read_scoped_by_payroll_result_and_audited(client, db_session, world):
    hr = _user(db_session, "adjustment-hr", ["GROUP_HR"])
    headers = _token(client, hr.username)
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, headers)
    first_round_results = list(
        db_session.scalars(
            select(PayrollResult)
            .where(PayrollResult.batch_id == batch_id)
            .order_by(PayrollResult.employee_id)
        ).all()
    )
    for result in first_round_results:
        _copy_payroll_result_to_round(db_session, result, batch_version=2, version=2)
    batch = db_session.get(PayrollBatch, batch_id)
    batch.version = 2
    db_session.add_all(
        [
            AdjustmentRecord(
                batch_id=batch_id,
                batch_version=1,
                employee_id=world["emp1"].id,
                item="ATTEND_WAGE",
                before_value={"actual_days": "20"},
                after_value={"actual_days": "21"},
                reason="Historical attendance correction",
                applicant_id=101,
                approver_id=hr.id,
                attachment_url=None,
                recompute_result={"batch_version": 1, "gross": "4772.73"},
                created_at=datetime(2026, 5, 29, 10, 0, tzinfo=UTC),
            ),
            AdjustmentRecord(
                batch_id=batch_id,
                batch_version=1,
                employee_id=world["emp2"].id,
                item="OVERTIME",
                before_value={"overtime_hours": "0"},
                after_value={"overtime_hours": "1"},
                reason="Historical overtime correction",
                applicant_id=102,
                approver_id=hr.id,
                attachment_url="https://example.test/overtime-proof",
                recompute_result={"batch_version": 1, "gross": "5010.00"},
                created_at=datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
            ),
            AdjustmentRecord(
                batch_id=batch_id,
                batch_version=2,
                employee_id=world["emp1"].id,
                item="ATTEND_WAGE",
                before_value={"actual_days": "21"},
                after_value={"actual_days": "22"},
                reason="Current attendance correction",
                applicant_id=None,
                approver_id=hr.id,
                attachment_url=None,
                recompute_result={"batch_version": 2, "gross": "5000.00"},
                created_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    global_response = client.get(f"/api/batches/{batch_id}/adjustments", headers=headers)
    assert global_response.status_code == 200, global_response.text
    global_rows = global_response.json()
    assert len(global_rows) == 3
    assert {(row["batch_version"], row["is_current_version"]) for row in global_rows} == {
        (1, False),
        (2, True),
    }
    current = next(row for row in global_rows if row["is_current_version"])
    current_expected = {
        "id": current["id"],
        "batch_id": batch_id,
        "batch_version": 2,
        "is_current_version": True,
        "employee_id": world["emp1"].id,
        "dispute_id": None,
        "item": "ATTEND_WAGE",
        "before_value": {"actual_days": "21"},
        "after_value": {"actual_days": "22"},
        "reason": "Current attendance correction",
        "applicant_id": None,
        "approver_id": hr.id,
        "attachment_url": None,
        "recompute_result": {"batch_version": 2, "gross": "5000.00"},
    }
    assert {key: current[key] for key in current_expected} == current_expected
    assert current["created_at"] is not None

    store_one = client.get(
        f"/api/batches/{batch_id}/adjustments",
        headers=reviewer_headers[world["s1"].id],
    )
    assert store_one.status_code == 200, store_one.text
    assert {row["employee_id"] for row in store_one.json()} == {world["emp1"].id}
    assert {row["batch_version"] for row in store_one.json()} == {1, 2}

    store_two = client.get(
        f"/api/batches/{batch_id}/adjustments",
        headers=reviewer_headers[world["s2"].id],
    )
    assert store_two.status_code == 200, store_two.text
    assert [(row["employee_id"], row["batch_version"]) for row in store_two.json()] == [
        (world["emp2"].id, 1)
    ]

    audit_row = db_session.scalars(
        select(AuditLog)
        .where(
            AuditLog.actor_user_id == hr.id,
            AuditLog.action == "batch.adjustments.view",
        )
        .order_by(AuditLog.id.desc())
    ).first()
    assert audit_row.target_type == "payroll_batch"
    assert audit_row.target_id == batch_id
    assert audit_row.detail == {"batch_version": 2, "returned": 3}


def test_lock_blocked_until_all_confirmed(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, h)
    # 只确认一个门店
    client.post(
        f"/api/batches/{batch_id}/confirm",
        headers=reviewer_headers[world["s1"].id],
        json={"org_unit_id": world["s1"].id, "department": "OTHER"},
    )
    r = client.post(f"/api/batches/{batch_id}/lock", headers=h)
    assert r.status_code == 409
    assert "Cannot lock" in r.json()["detail"]


def test_store_manager_scope_enforced(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "sm1",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    h_hr = _token(client, "hr")
    batch_id, _ = _create_and_run(client, h_hr)

    h_sm = _token(client, "sm1")
    # 只能看到本店结果
    r = client.get(f"/api/batches/{batch_id}/results", headers=h_sm)
    assert r.status_code == 200
    res = r.json()
    assert len(res) == 1
    assert res[0]["employee_id"] == world["emp1"].id

    # 确认本店 OK
    r = client.post(
        f"/api/batches/{batch_id}/confirm",
        headers=h_sm,
        json={"org_unit_id": world["s1"].id, "department": "OTHER"},
    )
    assert r.status_code == 200
    # 确认他店被拒
    r = client.post(
        f"/api/batches/{batch_id}/confirm",
        headers=h_sm,
        json={"org_unit_id": world["s2"].id, "department": "OTHER"},
    )
    assert r.status_code == 404


def test_dispute_list_is_limited_to_the_reviewer_store_scope(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    hr_headers = _token(client, "hr")
    batch_id, _ = _create_and_run(client, hr_headers)

    for store, employee in ((world["s1"], world["emp1"]), (world["s2"], world["emp2"])):
        response = client.post(
            f"/api/batches/{batch_id}/disputes",
            headers=reviewer_headers[store.id],
            json={
                "employee_id": employee.id,
                "salary_item": "ATTEND_WAGE",
                "opinion": "Please verify attendance input",
            },
        )
        assert response.status_code == 201, response.text

    scoped_headers = reviewer_headers[world["s1"].id]
    scoped = client.get(f"/api/batches/{batch_id}/disputes", headers=scoped_headers)
    assert scoped.status_code == 200, scoped.text
    assert [dispute["employee_id"] for dispute in scoped.json()] == [world["emp1"].id]
    assert scoped.json()[0]["allowed_attendance_fields"] == ["actual_days", "expected_days"]

    global_reader = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers)
    assert global_reader.status_code == 200, global_reader.text
    assert {dispute["employee_id"] for dispute in global_reader.json()} == {
        world["emp1"].id,
        world["emp2"].id,
    }


def test_store_manager_without_explicit_review_scope_is_fail_closed(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(db_session, "unassigned-sm", ["STORE_MANAGER"], scope_ids=[world["s1"].id])
    h_hr = _token(client, "hr")
    batch_id, _ = _create_and_run(client, h_hr)

    h_sm = _token(client, "unassigned-sm")
    assert client.get("/api/batches", headers=h_sm).json() == []
    assert client.get(f"/api/batches/{batch_id}/results", headers=h_sm).status_code == 404
    assert (
        client.post(
            f"/api/batches/{batch_id}/confirm",
            headers=h_sm,
            json={"org_unit_id": world["s1"].id, "department": "OTHER"},
        ).status_code
        == 404
    )


def test_create_dispute_hides_unreviewable_targets_without_side_effects(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "scoped-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    _user(
        db_session,
        "unassigned-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
    )
    hr_headers = _token(client, "hr")
    scoped_headers = _token(client, "scoped-reviewer")
    unassigned_headers = _token(client, "unassigned-reviewer")
    batch_id, _ = _create_and_run(client, hr_headers)
    employee_without_result = _employee(db_session, world["s1"], "E3", "No result employee")

    before_dispute_count = db_session.scalar(select(func.count()).select_from(CompDispute))
    before_batch = db_session.get(PayrollBatch, batch_id)
    assert before_batch is not None
    before_batch_state = (
        before_batch.status,
        before_batch.version,
        before_batch.calculated_at,
        before_batch.locked_at,
        before_batch.locked_by,
    )
    before_attendance = db_session.execute(
        select(
            AttendanceRecord.employee_id,
            AttendanceRecord.period,
            AttendanceRecord.generated_expected_days,
            AttendanceRecord.expected_days,
            AttendanceRecord.actual_days,
            AttendanceRecord.worked_hours,
            AttendanceRecord.rest_days,
            AttendanceRecord.overtime_hours,
        ).order_by(AttendanceRecord.employee_id, AttendanceRecord.period)
    ).all()
    before_results = db_session.execute(
        select(
            PayrollResult.id,
            PayrollResult.batch_id,
            PayrollResult.batch_version,
            PayrollResult.employee_id,
            PayrollResult.version,
            PayrollResult.org_unit_id,
            PayrollResult.department,
            PayrollResult.actual_attendance_days,
            PayrollResult.gross,
            PayrollResult.net,
        ).order_by(PayrollResult.id)
    ).all()

    payload = {
        "salary_item": "ATTEND_WAGE",
        "opinion": "Please verify the attendance input.",
    }
    responses = [
        # No explicit review scope, despite the employee being in the user's store.
        client.post(
            f"/api/batches/{batch_id}/disputes",
            headers=unassigned_headers,
            json={**payload, "employee_id": world["emp1"].id},
        ),
        # An existing payroll result in another store is not reviewable.
        client.post(
            f"/api/batches/{batch_id}/disputes",
            headers=scoped_headers,
            json={**payload, "employee_id": world["emp2"].id},
        ),
        # An existing, in-scope employee that was not part of this batch.
        client.post(
            f"/api/batches/{batch_id}/disputes",
            headers=scoped_headers,
            json={**payload, "employee_id": employee_without_result.id},
        ),
        # A nonexistent employee must be indistinguishable from the cases above.
        client.post(
            f"/api/batches/{batch_id}/disputes",
            headers=scoped_headers,
            json={**payload, "employee_id": 999_999},
        ),
    ]

    for response in responses:
        assert response.status_code == 404, response.text
        assert response.content == responses[0].content

    persisted_batch = db_session.get(PayrollBatch, batch_id)
    assert persisted_batch is not None
    assert (
        persisted_batch.status,
        persisted_batch.version,
        persisted_batch.calculated_at,
        persisted_batch.locked_at,
        persisted_batch.locked_by,
    ) == before_batch_state
    assert db_session.scalar(select(func.count()).select_from(CompDispute)) == before_dispute_count
    assert (
        db_session.execute(
            select(
                AttendanceRecord.employee_id,
                AttendanceRecord.period,
                AttendanceRecord.generated_expected_days,
                AttendanceRecord.expected_days,
                AttendanceRecord.actual_days,
                AttendanceRecord.worked_hours,
                AttendanceRecord.rest_days,
                AttendanceRecord.overtime_hours,
            ).order_by(AttendanceRecord.employee_id, AttendanceRecord.period)
        ).all()
        == before_attendance
    )
    assert (
        db_session.execute(
            select(
                PayrollResult.id,
                PayrollResult.batch_id,
                PayrollResult.batch_version,
                PayrollResult.employee_id,
                PayrollResult.version,
                PayrollResult.org_unit_id,
                PayrollResult.department,
                PayrollResult.actual_attendance_days,
                PayrollResult.gross,
                PayrollResult.net,
            ).order_by(PayrollResult.id)
        ).all()
        == before_results
    )


def test_create_dispute_uses_latest_result_snapshot_scope(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "snapshot-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, "hr")
    reviewer_headers = _token(client, "snapshot-reviewer")
    batch_id, _ = _create_and_run(client, hr_headers)
    initial_results = {
        result.employee_id: result
        for result in db_session.scalars(
            select(PayrollResult).where(PayrollResult.batch_id == batch_id)
        )
    }

    # emp1's current organization remains in the reviewer's store, but its
    # current payroll snapshot moves outside it.  The v1 snapshot must not
    # authorize a dispute for v2.
    emp1_v1 = initial_results[world["emp1"].id]
    emp1_v2 = _newer_payroll_result_snapshot(db_session, emp1_v1, org_unit_id=world["s2"].id)
    assert world["emp1"].org_unit_id == world["s1"].id
    assert emp1_v1.org_unit_id == world["s1"].id
    assert emp1_v2.org_unit_id == world["s2"].id

    # Conversely, emp2's current organization and v1 remain outside the
    # reviewer's store, while the active snapshot moves into it.
    emp2_v1 = initial_results[world["emp2"].id]
    emp2_v2 = _newer_payroll_result_snapshot(db_session, emp2_v1, org_unit_id=world["s1"].id)
    assert world["emp2"].org_unit_id == world["s2"].id
    assert emp2_v1.org_unit_id == world["s2"].id
    assert emp2_v2.org_unit_id == world["s1"].id

    before_dispute_count = db_session.scalar(select(func.count()).select_from(CompDispute))
    before_batch = db_session.get(PayrollBatch, batch_id)
    assert before_batch is not None
    before_batch_state = (
        before_batch.status,
        before_batch.version,
        before_batch.calculated_at,
        before_batch.locked_at,
        before_batch.locked_by,
    )
    before_attendance = db_session.execute(
        select(
            AttendanceRecord.employee_id,
            AttendanceRecord.expected_days,
            AttendanceRecord.actual_days,
            AttendanceRecord.worked_hours,
            AttendanceRecord.rest_days,
            AttendanceRecord.overtime_hours,
        )
        .where(AttendanceRecord.employee_id.in_([world["emp1"].id, world["emp2"].id]))
        .order_by(AttendanceRecord.employee_id)
    ).all()
    before_results = db_session.execute(
        select(
            PayrollResult.id,
            PayrollResult.batch_id,
            PayrollResult.batch_version,
            PayrollResult.employee_id,
            PayrollResult.version,
            PayrollResult.org_unit_id,
            PayrollResult.department,
        )
        .where(PayrollResult.batch_id == batch_id)
        .order_by(PayrollResult.id)
    ).all()
    payload = {
        "salary_item": "ATTEND_WAGE",
        "opinion": "Verify the current payroll snapshot.",
    }

    out_of_scope = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={**payload, "employee_id": world["emp1"].id},
    )
    nonexistent = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={**payload, "employee_id": 999_999},
    )
    assert out_of_scope.status_code == nonexistent.status_code == 404
    assert out_of_scope.content == nonexistent.content
    assert db_session.scalar(select(func.count()).select_from(CompDispute)) == before_dispute_count

    persisted_batch = db_session.get(PayrollBatch, batch_id)
    assert persisted_batch is not None
    assert (
        persisted_batch.status,
        persisted_batch.version,
        persisted_batch.calculated_at,
        persisted_batch.locked_at,
        persisted_batch.locked_by,
    ) == before_batch_state
    assert (
        db_session.execute(
            select(
                AttendanceRecord.employee_id,
                AttendanceRecord.expected_days,
                AttendanceRecord.actual_days,
                AttendanceRecord.worked_hours,
                AttendanceRecord.rest_days,
                AttendanceRecord.overtime_hours,
            )
            .where(AttendanceRecord.employee_id.in_([world["emp1"].id, world["emp2"].id]))
            .order_by(AttendanceRecord.employee_id)
        ).all()
        == before_attendance
    )
    assert (
        db_session.execute(
            select(
                PayrollResult.id,
                PayrollResult.batch_id,
                PayrollResult.batch_version,
                PayrollResult.employee_id,
                PayrollResult.version,
                PayrollResult.org_unit_id,
                PayrollResult.department,
            )
            .where(PayrollResult.batch_id == batch_id)
            .order_by(PayrollResult.id)
        ).all()
        == before_results
    )

    allowed = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={**payload, "employee_id": world["emp2"].id},
    )
    assert allowed.status_code == 201, allowed.text
    assert db_session.get(CompDispute, allowed.json()["dispute_id"]).employee_id == world["emp2"].id


def test_finance_can_read_globally_but_cannot_finalize_or_lock(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(db_session, "finance", ["FINANCE"])
    h_hr = _token(client, "hr")
    h_finance = _token(client, "finance")
    batch_id, _ = _create_and_run(client, h_hr)

    r = client.get(f"/api/batches/{batch_id}/results", headers=h_finance)
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert client.post(f"/api/batches/{batch_id}/approve", headers=h_finance).status_code == 403
    assert client.post(f"/api/batches/{batch_id}/lock", headers=h_finance).status_code == 403


@pytest.mark.parametrize(("action", "payload", "permission"), _GLOBAL_BATCH_LIFECYCLE_ACTIONS)
def test_scoped_payroll_lifecycle_permissions_fail_closed_without_mutation(
    client, db_session, world, action, payload, permission
):
    _user(db_session, "hr", ["GROUP_HR"])
    _scoped_payroll_lifecycle_role(db_session)
    # AUDITOR grants unrelated global payroll read.  The lifecycle permissions
    # remain scoped and must not inherit that global role's reach.
    scoped_lifecycle_user = _user(
        db_session,
        "scoped-lifecycle",
        ["AUDITOR", "SCOPED_PAYROLL_LIFECYCLE"],
        scope_ids=[world["s1"].id],
    )
    principal = build_principal(db_session, scoped_lifecycle_user)
    assert principal.org_scope is None
    assert resolve_permission_org_scope(db_session, principal, permission) == frozenset(
        {world["s1"].id}
    )
    hr_headers = _token(client, "hr")
    scoped_headers = _token(client, "scoped-lifecycle")

    before_batch_count = db_session.scalar(select(func.count()).select_from(PayrollBatch))
    created = client.post(
        "/api/batches",
        headers=scoped_headers,
        json={
            "period": "2026-06",
            "attendance_start": "2026-06-01",
            "attendance_end": "2026-06-30",
        },
    )
    assert created.status_code == 403
    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == before_batch_count

    batch = client.post(
        "/api/batches",
        headers=hr_headers,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    before_state = (
        BatchStatus.DRAFT,
        1,
        None,
        None,
    )
    before_attendance = db_session.execute(
        select(
            AttendanceRecord.employee_id,
            AttendanceRecord.expected_days,
            AttendanceRecord.actual_days,
            AttendanceRecord.worked_hours,
            AttendanceRecord.rest_days,
            AttendanceRecord.overtime_hours,
        ).order_by(AttendanceRecord.employee_id)
    ).all()

    existing = client.post(
        f"/api/batches/{batch_id}/{action}", headers=scoped_headers, json=payload
    )
    missing = client.post(f"/api/batches/999999/{action}", headers=scoped_headers, json=payload)
    assert existing.status_code == missing.status_code == 403
    assert existing.json() == missing.json()

    persisted = db_session.get(PayrollBatch, batch_id)
    assert persisted is not None
    assert (
        persisted.status,
        persisted.version,
        persisted.locked_at,
        persisted.locked_by,
    ) == before_state
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0
    assert (
        db_session.execute(
            select(
                AttendanceRecord.employee_id,
                AttendanceRecord.expected_days,
                AttendanceRecord.actual_days,
                AttendanceRecord.worked_hours,
                AttendanceRecord.rest_days,
                AttendanceRecord.overtime_hours,
            ).order_by(AttendanceRecord.employee_id)
        ).all()
        == before_attendance
    )


def test_scoped_dispute_correction_uses_historical_result_org_scope(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _scoped_payroll_lifecycle_role(db_session)
    _user(
        db_session,
        "wrong-scope-corrector",
        ["SCOPED_PAYROLL_LIFECYCLE"],
        scope_ids=[world["s2"].id],
    )
    _user(db_session, "unassigned-corrector", ["SCOPED_PAYROLL_LIFECYCLE"])
    _user(
        db_session,
        "matching-snapshot-corrector",
        ["SCOPED_PAYROLL_LIFECYCLE"],
        scope_ids=[world["s1"].id],
    )
    hr_headers = _token(client, "hr")
    wrong_scope_headers = _token(client, "wrong-scope-corrector")
    no_scope_headers = _token(client, "unassigned-corrector")
    matching_scope_headers = _token(client, "matching-snapshot-corrector")
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, hr_headers)

    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers[world["s2"].id],
        json={
            "employee_id": world["emp2"].id,
            "salary_item": "ATTEND_WAGE",
            "opinion": "Verify historical attendance.",
        },
    )
    assert opened.status_code == 201, opened.text
    dispute_id = opened.json()["dispute_id"]

    original_snapshot = db_session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp2"].id,
        )
        .order_by(PayrollResult.version)
    ).one()
    latest_snapshot = _newer_payroll_result_snapshot(
        db_session, original_snapshot, org_unit_id=world["s1"].id
    )
    # The employee's current organization and the stale v1 snapshot both point
    # to s2.  A resolution must instead use the latest v2 result snapshot (s1).
    assert world["emp2"].org_unit_id == world["s2"].id
    assert original_snapshot.org_unit_id == world["s2"].id
    assert latest_snapshot.org_unit_id == world["s1"].id
    assert (
        db_session.scalar(
            select(PayrollResult.org_unit_id)
            .where(
                PayrollResult.batch_id == batch_id,
                PayrollResult.employee_id == world["emp2"].id,
            )
            .order_by(PayrollResult.version.desc())
            .limit(1)
        )
        == world["s1"].id
    )

    before_attendance = db_session.execute(
        select(
            AttendanceRecord.expected_days,
            AttendanceRecord.actual_days,
            AttendanceRecord.worked_hours,
            AttendanceRecord.rest_days,
            AttendanceRecord.overtime_hours,
        ).where(AttendanceRecord.employee_id == world["emp2"].id)
    ).one()
    before_result_versions = db_session.execute(
        select(PayrollResult.id, PayrollResult.version)
        .where(PayrollResult.batch_id == batch_id, PayrollResult.employee_id == world["emp2"].id)
        .order_by(PayrollResult.version)
    ).all()
    before_adjustment_count = db_session.scalar(select(func.count()).select_from(AdjustmentRecord))
    before_batch_status = db_session.get(PayrollBatch, batch_id).status

    payload = {
        "decision": "APPROVED",
        "resolution": "Attempted out-of-scope correction.",
        "attendance_changes": {"actual_days": "21"},
        "attachment_url": "https://evidence.example/out-of-scope-proof.pdf",
    }
    rejected_responses = []
    for headers in (wrong_scope_headers, no_scope_headers):
        response = client.post(
            f"/api/batches/disputes/{dispute_id}/resolve",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 404, response.text
        rejected_responses.append(response.json())
    assert rejected_responses[0] == rejected_responses[1]

    dispute = db_session.get(CompDispute, dispute_id)
    assert dispute is not None
    assert dispute.status is DisputeStatus.OPEN
    assert db_session.get(PayrollBatch, batch_id).status is before_batch_status
    assert (
        db_session.execute(
            select(
                AttendanceRecord.expected_days,
                AttendanceRecord.actual_days,
                AttendanceRecord.worked_hours,
                AttendanceRecord.rest_days,
                AttendanceRecord.overtime_hours,
            ).where(AttendanceRecord.employee_id == world["emp2"].id)
        ).one()
        == before_attendance
    )
    assert (
        db_session.execute(
            select(PayrollResult.id, PayrollResult.version)
            .where(
                PayrollResult.batch_id == batch_id,
                PayrollResult.employee_id == world["emp2"].id,
            )
            .order_by(PayrollResult.version)
        ).all()
        == before_result_versions
    )
    assert (
        db_session.scalar(select(func.count()).select_from(AdjustmentRecord))
        == before_adjustment_count
    )

    # A scoped corrector remains allowed only when its grant matches the latest
    # historical result snapshot, not the current employee or stale result row.
    allowed = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=matching_scope_headers,
        json={"decision": "REJECTED", "resolution": "Historical snapshot is in scope."},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"status": "REJECTED"}


def test_dispute_approved_recomputes_new_version(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "sm1",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    h_hr = _token(client, "hr")
    h_sm = _token(client, "sm1")
    # Special positions use the approved actual attendance days in the current
    # ruleset, rather than deriving them from hours or rest days.
    world["emp1"].is_special_position = True
    batch_id, _ = _create_and_run(client, h_hr)

    # 店长对 emp1 的出勤工资提异议
    r = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=h_sm,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": "ATTEND_WAGE",
            "opinion": "少算了2天休息",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["batch_status"] == "HAS_DISPUTE"
    dispute_id = r.json()["dispute_id"]

    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=h_hr)
    assert listed.status_code == 200, listed.text
    listed_dispute = next(item for item in listed.json() if item["id"] == dispute_id)
    assert listed_dispute["allowed_attendance_fields"] == ["actual_days", "expected_days"]

    # 人事同意：改源数据（休息天数 0→2）→ 自动重算
    r = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=h_hr,
        json={
            "decision": "APPROVED",
            "resolution": "核实后调整休息天数",
            "attendance_changes": {"actual_days": "20"},
            "attachment_url": "https://evidence.example/verified-attendance.pdf",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"

    # 源考勤已改
    att = db_session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == world["emp1"].id,
            AttendanceRecord.period == "2026-05",
        )
    ).first()
    assert att.actual_days == Decimal("20")

    # 生成了新版本结果（v2），旧版本保留
    versions = db_session.scalars(
        select(PayrollResult.version).where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
    ).all()
    assert set(versions) == {1, 2}

    # v2 出勤工资下降：5000/22×20 = 4545.45
    r = client.get(f"/api/batches/{batch_id}/results", headers=h_hr)
    latest = {x["employee_id"]: x for x in r.json()}
    assert latest[world["emp1"].id]["version"] == 2
    assert latest[world["emp1"].id]["gross"] == "4545.45"

    # 修改记录留痕
    adj = db_session.scalars(
        select(AdjustmentRecord).where(AdjustmentRecord.dispute_id == dispute_id)
    ).one()
    assert Decimal(adj.before_value["actual_days"]) == Decimal("22")
    assert Decimal(adj.after_value["actual_days"]) == Decimal("20")

    # 异议处理完 → 回到待确认
    batch_status = db_session.scalar(
        select(func.count()).select_from(PayrollResult).where(PayrollResult.batch_id == batch_id)
    )
    assert batch_status == 3  # emp1 两版 + emp2 一版


def test_dispute_rejected_no_recompute(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    _user(
        db_session,
        "sm1",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    h_hr = _token(client, "hr")
    h_sm = _token(client, "sm1")
    batch_id, _ = _create_and_run(client, h_hr)

    r = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=h_sm,
        json={"employee_id": world["emp1"].id, "salary_item": "ATTEND_WAGE", "opinion": "有疑问"},
    )
    dispute_id = r.json()["dispute_id"]
    r = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=h_hr,
        json={"decision": "REJECTED", "resolution": "核实无误，维持原核算"},
    )
    assert r.status_code == 200
    # 仍是 v1，无 v2
    versions = db_session.scalars(
        select(PayrollResult.version).where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
    ).all()
    assert set(versions) == {1}


def test_unlock_bumps_version_and_keeps_results(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, h)
    for store in (world["s1"], world["s2"]):
        client.post(
            f"/api/batches/{batch_id}/confirm",
            headers=reviewer_headers[store.id],
            json={"org_unit_id": store.id, "department": "OTHER"},
        )
    r = client.post(f"/api/batches/{batch_id}/approve", headers=h)
    assert r.status_code == 200
    client.post(f"/api/batches/{batch_id}/lock", headers=h)

    r = client.post(
        f"/api/batches/{batch_id}/unlock",
        headers=h,
        json={"reason": "发现社保数据需更正"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "DRAFT"
    assert r.json()["version"] == 2
    # 旧结果仍在
    total = db_session.scalar(
        select(func.count()).select_from(PayrollResult).where(PayrollResult.batch_id == batch_id)
    )
    assert total == 2

    r = client.put(
        f"/api/employees/{world['emp1'].id}/attendance/2026-05",
        headers=h,
        json={
            "expected_days": "22",
            "actual_days": "22",
            "rest_days": "0",
            "correction_reason": "无实际变动",
        },
    )
    assert r.status_code == 422

    missing_attachment = client.put(
        f"/api/employees/{world['emp1'].id}/attendance/2026-05",
        headers=h,
        json={
            "expected_days": "22",
            "actual_days": "21",
            "correction_reason": "复核后更正实际出勤",
        },
    )
    assert missing_attachment.status_code == 422

    irrelevant_source = client.put(
        f"/api/employees/{world['emp1'].id}/attendance/2026-05",
        headers=h,
        json={
            "expected_days": "22",
            "actual_days": "22",
            "rest_days": "1",
            "correction_reason": "尝试修改当前规则不使用的休息天数",
            "attachment_url": "https://example.test/attendance-proof",
        },
    )
    assert irrelevant_source.status_code == 422
    assert "rest_days" in irrelevant_source.json()["detail"]

    r = client.put(
        f"/api/employees/{world['emp1'].id}/attendance/2026-05",
        headers=h,
        json={
            "expected_days": "22",
            "actual_days": "21",
            "correction_reason": "复核后更正实际出勤",
            "attachment_url": "https://example.test/attendance-proof",
        },
    )
    assert r.status_code == 200, r.text
    direct_adjustment = db_session.scalars(
        select(AdjustmentRecord).where(
            AdjustmentRecord.batch_id == batch_id,
            AdjustmentRecord.dispute_id.is_(None),
        )
    ).one()
    assert Decimal(direct_adjustment.before_value["actual_days"]) == Decimal("22")
    assert Decimal(direct_adjustment.after_value["actual_days"]) == Decimal("21")
    assert direct_adjustment.recompute_result == {"status": "PENDING_RERUN", "batch_version": 2}

    r = client.post(f"/api/batches/{batch_id}/run", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING_STORE_CONFIRM"
    active_results = client.get(f"/api/batches/{batch_id}/results", headers=h).json()
    assert {result["batch_version"] for result in active_results} == {2}
    assert {result["version"] for result in active_results} == {2}
    db_session.refresh(direct_adjustment)
    assert direct_adjustment.recompute_result["status"] == "RECOMPUTED"
    assert direct_adjustment.recompute_result["batch_version"] == 2


def test_reopened_holiday_work_correction_is_audited_and_recomputed(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee = world["emp1"]
    db_session.add(
        StatutoryHolidayDate(
            holiday_date=date(2026, 5, 1),
            name="May Day",
            eligible_employment_types=["FULL_TIME"],
        )
    )
    db_session.flush()
    reviewer_headers = _store_reviewer_headers(client, db_session, world)
    batch_id, _ = _create_and_run(client, headers)
    for store in (world["s1"], world["s2"]):
        assert (
            client.post(
                f"/api/batches/{batch_id}/confirm",
                headers=reviewer_headers[store.id],
                json={"org_unit_id": store.id, "department": "OTHER"},
            ).status_code
            == 200
        )
    assert client.post(f"/api/batches/{batch_id}/approve", headers=headers).status_code == 200
    assert client.post(f"/api/batches/{batch_id}/lock", headers=headers).status_code == 200
    assert (
        client.post(
            f"/api/batches/{batch_id}/unlock",
            headers=headers,
            json={"reason": "Correct statutory holiday attendance"},
        ).status_code
        == 200
    )

    noop = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=headers,
        json={"worked": False, "correction_reason": "No source change"},
    )
    assert noop.status_code == 422
    missing_evidence = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=headers,
        json={
            "worked": True,
            "reason": "Verified holiday shift",
            "correction_reason": "Verified holiday shift after lock",
        },
    )
    assert missing_evidence.status_code == 422
    corrected = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=headers,
        json={
            "worked": True,
            "reason": "Verified holiday shift",
            "evidence_url": "https://example.test/holiday-proof",
            "correction_reason": "Verified holiday shift after lock",
        },
    )
    assert corrected.status_code == 200, corrected.text
    adjustment = db_session.scalars(
        select(AdjustmentRecord).where(
            AdjustmentRecord.batch_id == batch_id,
            AdjustmentRecord.employee_id == employee.id,
            AdjustmentRecord.item == "HOLIDAY_WORK_SOURCE",
        )
    ).one()
    assert adjustment.before_value == {"record_exists": False, "holiday_date": "2026-05-01"}
    assert adjustment.after_value["holiday_date"] == "2026-05-01"
    assert adjustment.after_value["worked"] is True
    assert adjustment.recompute_result == {"status": "PENDING_RERUN", "batch_version": 2}

    rerun = client.post(f"/api/batches/{batch_id}/run", headers=headers)
    assert rerun.status_code == 200, rerun.text
    db_session.refresh(adjustment)
    assert adjustment.recompute_result["status"] == "RECOMPUTED"
    assert adjustment.recompute_result["batch_version"] == 2


def test_reopened_holiday_work_rejects_a_date_not_eligible_for_the_employee(
    client, db_session, world
):
    _user(db_session, "hr-ineligible-holiday", ["GROUP_HR"])
    headers = _token(client, "hr-ineligible-holiday")
    employee = world["emp1"]
    db_session.add(
        StatutoryHolidayDate(
            holiday_date=date(2026, 5, 1),
            name="May Day",
            eligible_employment_types=["LABOR"],
        )
    )
    db_session.flush()
    batch_id, _ = _create_and_run(client, headers)
    reopened = client.post(
        f"/api/batches/{batch_id}/reopen",
        headers=headers,
        json={"reason": "Verify an ineligible holiday correction"},
    )
    assert reopened.status_code == 200, reopened.text

    corrected = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=headers,
        json={
            "worked": True,
            "reason": "Attempted holiday shift",
            "evidence_url": "https://example.test/ineligible-holiday-proof",
            "correction_reason": "Attempted ineligible correction",
        },
    )

    assert corrected.status_code == 422
    assert "cannot affect payroll" in corrected.json()["detail"]
    assert (
        db_session.scalars(
            select(HolidayWorkRecord).where(
                HolidayWorkRecord.employee_id == employee.id,
                HolidayWorkRecord.holiday_date == date(2026, 5, 1),
            )
        ).one_or_none()
        is None
    )


def test_reopened_batch_rejects_unauditable_calendar_definition_changes(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    batch_id, _ = _create_and_run(client, headers)
    assert (
        client.post(
            f"/api/batches/{batch_id}/reopen",
            headers=headers,
            json={"reason": "Correct a payroll source record"},
        ).status_code
        == 200
    )

    calendar_period = db_session.scalars(
        select(HolidayCalendarPeriod).where(HolidayCalendarPeriod.period == "2026-05")
    ).one()
    # Eliminate the ordinary finalized-calendar guard so these assertions prove
    # that the reopened-round audit guard is the one rejecting the operation.
    calendar_period.is_finalized = False
    db_session.flush()
    date_change = client.put(
        "/api/holiday-calendar/dates/2026-05-02",
        headers=headers,
        json={
            "holiday_date": "2026-05-02",
            "name": "Auditable boundary test",
            "eligible_employment_types": ["FULL_TIME"],
        },
    )
    finalize = client.post("/api/holiday-calendar/periods/2026-05/finalize", headers=headers)
    calendar_period.is_finalized = True
    db_session.flush()
    unfinalize = client.post("/api/holiday-calendar/periods/2026-05/unfinalize", headers=headers)

    for response in (date_change, finalize, unfinalize):
        assert response.status_code == 409
        assert "calendar" in response.json()["detail"].lower()


def test_dispute_material_requests_keep_append_only_events_and_accept_supplements(
    client, db_session, world
):
    _user(db_session, "hr_materials", ["GROUP_HR"])
    _user(
        db_session,
        "reviewer_materials",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, "hr_materials")
    reviewer_headers = _token(client, "reviewer_materials")
    batch_id, _ = _create_and_run(client, hr_headers)

    created = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": "ATTEND_WAGE",
            "opinion": "请复核考勤来源。",
        },
    )
    assert created.status_code == 201, created.text
    dispute_id = created.json()["dispute_id"]

    requested = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={"decision": "NEED_MORE", "resolution": "请补充已签字的考勤表。"},
    )
    assert requested.status_code == 200, requested.text
    assert requested.json() == {"status": "NEED_MORE"}

    supplemented = client.post(
        f"/api/batches/disputes/{dispute_id}/supplements",
        headers=reviewer_headers,
        json={
            "note": "已补充门店和员工双方签字版本。",
            "attachment_url": "https://evidence.example/signed-attendance.pdf",
        },
    )
    assert supplemented.status_code == 200, supplemented.text
    assert supplemented.json() == {"status": "OPEN"}

    disputes = client.get(f"/api/batches/{batch_id}/disputes", headers=reviewer_headers).json()
    dispute = next(item for item in disputes if item["id"] == dispute_id)
    assert dispute["resolution"] is None
    assert dispute["resolved_by"] is None
    assert dispute["resolved_at"] is None
    assert [event["event_type"] for event in dispute["events"]] == [
        "RAISED",
        "NEED_MORE",
        "SUPPLEMENTED",
    ]
    assert dispute["events"][1]["note"] == "请补充已签字的考勤表。"
    assert dispute["events"][2]["attachment_url"].endswith("signed-attendance.pdf")


def test_holiday_dispute_approval_changes_day_source_recomputes_and_closes(
    client, db_session, world
):
    _user(db_session, "hr-holiday-dispute", ["GROUP_HR"])
    _user(
        db_session,
        "holiday-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, "hr-holiday-dispute")
    reviewer_headers = _token(client, "holiday-reviewer")
    db_session.add(
        StatutoryHolidayDate(
            holiday_date=date(2026, 5, 1),
            name="May Day",
            eligible_employment_types=["FULL_TIME"],
        )
    )
    db_session.flush()
    batch_id, _ = _create_and_run(client, hr_headers)

    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": "HOLIDAY",
            "opinion": "The employee worked the statutory holiday.",
        },
    )
    assert opened.status_code == 201, opened.text
    dispute_id = opened.json()["dispute_id"]
    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers)
    dispute = next(item for item in listed.json() if item["id"] == dispute_id)
    assert dispute["correction_options"] == [
        {
            "kind": "HOLIDAY_WORK",
            "label": "法定节假日逐日出勤",
            "holiday_dates": [{"holiday_date": "2026-05-01", "worked": False}],
        }
    ]

    resolved = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "APPROVED",
            "resolution": "Verified the signed holiday shift.",
            "attachment_url": "https://evidence.example/holiday-shift.pdf",
            "source_correction": {
                "kind": "HOLIDAY_WORK",
                "holiday_date": "2026-05-01",
                "worked": True,
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    source = db_session.scalars(
        select(HolidayWorkRecord).where(
            HolidayWorkRecord.employee_id == world["emp1"].id,
            HolidayWorkRecord.holiday_date == date(2026, 5, 1),
        )
    ).one()
    assert source.worked is True
    assert source.recorded_by is not None
    results = db_session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
        .order_by(PayrollResult.version)
    ).all()
    assert [result.version for result in results] == [1, 2]
    assert (
        next(line for line in results[0].lines if line["code"] == "HOLIDAY")["amount"] == "136.36"
    )
    assert (
        next(line for line in results[1].lines if line["code"] == "HOLIDAY")["amount"] == "409.09"
    )
    adjustment = db_session.scalars(
        select(AdjustmentRecord).where(AdjustmentRecord.dispute_id == dispute_id)
    ).one()
    assert adjustment.item == "HOLIDAY"
    assert adjustment.before_value["worked"] is False
    assert adjustment.after_value["worked"] is True
    assert adjustment.applicant_id == db_session.get(CompDispute, dispute_id).raised_by
    assert adjustment.attachment_url.endswith("holiday-shift.pdf")


def test_performance_dispute_approval_changes_performance_source_and_recomputes(
    client, db_session, world
):
    _user(db_session, "hr-performance-dispute", ["GROUP_HR"])
    _user(
        db_session,
        "performance-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, "hr-performance-dispute")
    reviewer_headers = _token(client, "performance-reviewer")
    component = SalaryComponentDef(
        code="PERF_BONUS",
        name="Performance bonus",
        component_type=ComponentType.PERFORMANCE,
    )
    db_session.add(component)
    db_session.flush()
    set_component_amount(
        db_session,
        employee_id=world["emp1"].id,
        component_id=component.id,
        amount=Decimal("1000"),
        effective_from=date(2026, 1, 1),
    )
    db_session.add(
        PerformanceRecord(
            employee_id=world["emp1"].id,
            period="2026-05",
            coefficient=Decimal("1.000"),
            score=Decimal("80"),
            remark="Initial review",
        )
    )
    db_session.flush()
    batch_id, _ = _create_and_run(client, hr_headers)
    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": "PERF_BONUS",
            "opinion": "Approved coefficient was 1.2.",
        },
    )
    dispute_id = opened.json()["dispute_id"]
    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers).json()
    option = next(item for item in listed if item["id"] == dispute_id)["correction_options"]
    assert option == [
        {
            "kind": "PERFORMANCE",
            "label": "当月绩效记录",
            "coefficient": "1.000",
            "score": "80.00",
            "remark": "Initial review",
        }
    ]

    resolved = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "APPROVED",
            "resolution": "Matched the approved performance sheet.",
            "attachment_url": "https://evidence.example/performance.pdf",
            "source_correction": {
                "kind": "PERFORMANCE",
                "coefficient": "1.200",
                "score": "95.00",
                "remark": "Approved sheet",
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    performance = db_session.scalars(
        select(PerformanceRecord).where(
            PerformanceRecord.employee_id == world["emp1"].id,
            PerformanceRecord.period == "2026-05",
        )
    ).one()
    assert performance.coefficient == Decimal("1.200")
    latest = db_session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
        .order_by(PayrollResult.version.desc())
    ).first()
    assert (
        next(line for line in latest.lines if line["code"] == "PERF_BONUS")["amount"] == "1200.00"
    )


@pytest.mark.parametrize(
    ("salary_item", "adjustment_type", "new_amount"),
    [
        ("PREV_MAKEUP", PayrollAdjustmentType.PREV_MAKEUP, "175.00"),
        ("PREV_DEDUCT", PayrollAdjustmentType.PREV_DEDUCT, "75.00"),
    ],
)
def test_prior_period_adjustment_dispute_approval_reuses_revisioned_source(
    client, db_session, world, salary_item, adjustment_type, new_amount
):
    hr = _user(db_session, f"hr-{salary_item.lower()}", ["GROUP_HR"])
    _user(
        db_session,
        f"reviewer-{salary_item.lower()}",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, hr.username)
    reviewer_headers = _token(client, f"reviewer-{salary_item.lower()}")
    seeded = client.put(
        f"/api/payroll-adjustments/{world['emp1'].id}/2026-05/{adjustment_type.value}",
        headers=hr_headers,
        json={
            "amount": "100.00",
            "reason": "Initial approved carry adjustment",
            "attachment_url": "https://evidence.example/initial-adjustment.pdf",
            "taxable": False,
            "in_social_base": False,
            "in_housing_base": False,
        },
    )
    assert seeded.status_code == 200, seeded.text
    batch_id, _ = _create_and_run(client, hr_headers)
    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": salary_item,
            "opinion": "The approved source amount differs.",
        },
    )
    dispute_id = opened.json()["dispute_id"]
    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers).json()
    option = next(item for item in listed if item["id"] == dispute_id)["correction_options"]
    assert option[0]["kind"] == "MONTHLY_ADJUSTMENT"
    assert option[0]["adjustment_type"] == adjustment_type.value
    assert option[0]["amount"] == "100.00"

    resolved = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "APPROVED",
            "resolution": "Matched the signed prior-period statement.",
            "attachment_url": "https://evidence.example/revised-adjustment.pdf",
            "source_correction": {
                "kind": "MONTHLY_ADJUSTMENT",
                "amount": new_amount,
                "taxable": False,
                "in_social_base": False,
                "in_housing_base": False,
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    source = db_session.scalars(
        select(MonthlyPayrollAdjustment).where(
            MonthlyPayrollAdjustment.employee_id == world["emp1"].id,
            MonthlyPayrollAdjustment.period == "2026-05",
            MonthlyPayrollAdjustment.adjustment_type == adjustment_type,
        )
    ).one()
    assert source.amount == Decimal(new_amount)
    revisions = db_session.scalars(
        select(MonthlyPayrollAdjustmentRevision)
        .where(MonthlyPayrollAdjustmentRevision.adjustment_id == source.id)
        .order_by(MonthlyPayrollAdjustmentRevision.revision)
    ).all()
    assert [revision.revision for revision in revisions] == [1, 2]
    assert revisions[-1].changed_by == hr.id
    latest = db_session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
        .order_by(PayrollResult.version.desc())
    ).first()
    assert next(line for line in latest.lines if line["code"] == salary_item)["amount"] == (
        new_amount if salary_item == "PREV_MAKEUP" else f"-{new_amount}"
    )


@pytest.mark.parametrize(
    ("component_type", "code", "expected_kind"),
    [
        (ComponentType.ALLOWANCE, "MEAL", "SALARY_STRUCTURE"),
        (ComponentType.HOUSING, "HOUSE", "SALARY_STRUCTURE"),
        (ComponentType.DEDUCTION, "OTHER_DEDUCT", "SALARY_STRUCTURE"),
    ],
)
def test_structure_backed_dispute_approval_creates_effective_revision_and_recomputes(
    client, db_session, world, component_type, code, expected_kind
):
    hr = _user(db_session, f"hr-{code.lower()}", ["GROUP_HR"])
    _user(
        db_session,
        f"reviewer-{code.lower()}",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    component = SalaryComponentDef(
        code=code,
        name=code,
        component_type=component_type,
        allowance_kind="FIXED" if component_type is ComponentType.ALLOWANCE else None,
    )
    db_session.add(component)
    db_session.flush()
    set_component_amount(
        db_session,
        employee_id=world["emp1"].id,
        component_id=component.id,
        amount=Decimal("100.00"),
        effective_from=date(2026, 1, 1),
        source_reason="Initial approved source",
        source_attachment_url="https://evidence.example/initial-source.pdf",
    )
    hr_headers = _token(client, hr.username)
    reviewer_headers = _token(client, f"reviewer-{code.lower()}")
    batch_id, _ = _create_and_run(client, hr_headers)
    salary_item = (
        "HOUSING"
        if component_type is ComponentType.HOUSING
        else "DEDUCTION" if component_type is ComponentType.DEDUCTION else code
    )
    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": salary_item,
            "opinion": "The approved component amount is 150.",
        },
    )
    assert opened.status_code == 201, opened.text
    dispute_id = opened.json()["dispute_id"]
    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers).json()
    option = next(item for item in listed if item["id"] == dispute_id)["correction_options"]
    assert option[0]["kind"] == expected_kind
    assert option[0]["components"] == [
        {
            "component_id": component.id,
            "code": code,
            "name": code,
            "amount": "100.00",
        }
    ]

    resolved = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "APPROVED",
            "resolution": "Matched the signed structure approval.",
            "attachment_url": "https://evidence.example/revised-structure.pdf",
            "source_correction": {
                "kind": "SALARY_STRUCTURE",
                "component_id": component.id,
                "amount": "150.00",
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    revisions = db_session.scalars(
        select(EmployeeSalaryStructure)
        .where(
            EmployeeSalaryStructure.employee_id == world["emp1"].id,
            EmployeeSalaryStructure.component_id == component.id,
        )
        .order_by(EmployeeSalaryStructure.effective_from, EmployeeSalaryStructure.revision)
    ).all()
    assert revisions[-1].amount == Decimal("150.00")
    assert revisions[-1].effective_from == date(2026, 5, 1)
    assert revisions[-1].source_reason == "Matched the signed structure approval."
    assert revisions[-1].source_attachment_url.endswith("revised-structure.pdf")
    latest = db_session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
        .order_by(PayrollResult.version.desc())
    ).first()
    assert next(line for line in latest.lines if line["code"] == salary_item)["amount"] == (
        "-150.00" if salary_item == "DEDUCTION" else "150.00"
    )


def test_unsafe_tax_dispute_exposes_workflow_only_and_can_be_rejected(client, db_session, world):
    _user(db_session, "hr-tax-workflow", ["GROUP_HR"])
    _user(
        db_session,
        "tax-reviewer",
        ["STORE_MANAGER"],
        scope_ids=[world["s1"].id],
        review_scopes=[(world["s1"].id, Department.OTHER)],
    )
    hr_headers = _token(client, "hr-tax-workflow")
    reviewer_headers = _token(client, "tax-reviewer")
    # Force a visible tax line without changing the source-correction contract
    # under test; policy/YTD edits are intentionally never performed here.
    batch_id, _ = _create_and_run(client, hr_headers)
    result = db_session.scalars(
        select(PayrollResult).where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.employee_id == world["emp1"].id,
        )
    ).one()
    result.lines = [
        *result.lines,
        {"code": "IIT_WITHHOLDING", "category": "个税", "formula": "YTD", "amount": "-10.00"},
    ]
    db_session.flush()
    opened = client.post(
        f"/api/batches/{batch_id}/disputes",
        headers=reviewer_headers,
        json={
            "employee_id": world["emp1"].id,
            "salary_item": "IIT_WITHHOLDING",
            "opinion": "Review tax opening and policy.",
        },
    )
    dispute_id = opened.json()["dispute_id"]
    listed = client.get(f"/api/batches/{batch_id}/disputes", headers=hr_headers).json()
    option = next(item for item in listed if item["id"] == dispute_id)["correction_options"]
    assert option == [
        {
            "kind": "WORKFLOW",
            "label": "个税/社保专用来源流程",
            "workflow": "PAYROLL_POLICY_OR_TAX_OPENING",
            "reason": "该项目涉及政策或累计计税来源，必须在专用来源流程核验后驳回或要求补充材料。",
        }
    ]

    forbidden = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "APPROVED",
            "resolution": "Never directly edit tax output.",
            "attachment_url": "https://evidence.example/tax.pdf",
            "source_correction": {"kind": "PERFORMANCE", "coefficient": "1.2"},
        },
    )
    assert forbidden.status_code == 409
    rejected = client.post(
        f"/api/batches/disputes/{dispute_id}/resolve",
        headers=hr_headers,
        json={
            "decision": "REJECTED",
            "resolution": "Tax source checked in its dedicated workflow.",
        },
    )
    assert rejected.status_code == 200, rejected.text


def test_hr_can_reopen_an_unlocked_review_round_for_correction(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    batch_id, _ = _create_and_run(client, h)

    r = client.post(
        f"/api/batches/{batch_id}/reopen",
        headers=h,
        json={"reason": "发现源数据需要在门店确认前更正"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "DRAFT", "version": 2}

    r = client.post(f"/api/batches/{batch_id}/run", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING_STORE_CONFIRM"


def test_reopened_attendance_correction_rejects_employee_outside_original_cohort(
    client, db_session, world
):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    batch_id, _ = _create_and_run(client, headers)
    assert (
        client.post(
            f"/api/batches/{batch_id}/reopen",
            headers=headers,
            json={"reason": "Correct May attendance"},
        ).status_code
        == 200
    )
    future = client.post(
        "/api/employees",
        headers=headers,
        json={
            "emp_no": "FUTURE-CORRECTION",
            "name": "Future employee",
            "org_unit_id": world["s1"].id,
            "hire_date": "2027-01-01",
        },
    )
    assert future.status_code == 201, future.text

    rejected = client.put(
        f"/api/employees/{future.json()['id']}/attendance/2026-05",
        headers=headers,
        json={
            "expected_days": "22",
            "actual_days": "22",
            "correction_reason": "Should not enter a historical cohort",
        },
    )
    assert rejected.status_code == 422
    assert (
        db_session.scalar(
            select(AdjustmentRecord.id).where(
                AdjustmentRecord.batch_id == batch_id,
                AdjustmentRecord.employee_id == future.json()["id"],
            )
        )
        is None
    )
    assert client.post(f"/api/batches/{batch_id}/run", headers=headers).status_code == 200


def test_started_batch_blocks_backdated_employee_creation_and_update(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    _create_and_run(client, h)

    blocked = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "BACKDATED",
            "name": "Backdated employee",
            "org_unit_id": world["s1"].id,
            "hire_date": "2026-05-01",
        },
    )
    assert blocked.status_code == 409

    future = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "FUTURE",
            "name": "Future employee",
            "org_unit_id": world["s1"].id,
            "hire_date": "2027-01-01",
        },
    )
    assert future.status_code == 201, future.text
    assert (
        client.patch(
            f"/api/employees/{future.json()['id']}",
            headers=h,
            json={"hire_date": "2026-05-01"},
        ).status_code
        == 409
    )


def test_store_with_payroll_history_cannot_be_deleted(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    _create_and_run(client, h)

    assert client.delete(f"/api/org/{world['s1'].id}", headers=h).status_code == 409


def test_run_requires_permission(client, db_session, world):
    _user(db_session, "emp", ["EMPLOYEE"])
    h = _token(client, "emp")
    r = client.post(
        "/api/batches",
        headers=h,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )
    assert r.status_code == 403


def test_duplicate_period_rejected(client, db_session, world):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    _create_and_run(client, h)
    r = client.post(
        "/api/batches",
        headers=h,
        json={
            "period": "2026-05",
            "attendance_start": "2026-05-01",
            "attendance_end": "2026-05-31",
        },
    )
    assert r.status_code == 409
