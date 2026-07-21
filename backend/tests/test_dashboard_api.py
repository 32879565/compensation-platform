from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Permission, Role, RolePermission, User, UserOrgScope, UserRole
from app.models.budget import LaborBudget
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _user(session, username: str, roles: list[str], scope_ids: list[int] | None = None) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for role_code in roles:
        role = session.scalars(select(Role).where(Role.code == role_code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    for org_unit_id in scope_ids or []:
        session.add(UserOrgScope(user_id=user.id, org_unit_id=org_unit_id))
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _mixed_scope_dashboard_roles(session) -> None:
    seed_rbac(session)
    global_audit = Role(code="GLOBAL_AUDIT_ONLY", name="Global audit", is_global_scope=True)
    scoped_dashboard = Role(code="SCOPED_DASHBOARD", name="Scoped dashboard", is_global_scope=False)
    session.add_all([global_audit, scoped_dashboard])
    session.flush()
    audit_permission = session.scalars(
        select(Permission.id).where(Permission.code == Perm.AUDIT_READ)
    ).one()
    dashboard_permission = session.scalars(
        select(Permission.id).where(Permission.code == Perm.DASHBOARD_READ)
    ).one()
    session.add_all(
        [
            RolePermission(role_id=global_audit.id, permission_id=audit_permission),
            RolePermission(role_id=scoped_dashboard.id, permission_id=dashboard_permission),
        ]
    )
    session.flush()


def _result(
    batch: PayrollBatch,
    employee: Employee,
    *,
    gross: str,
    net: str,
    version: int = 1,
) -> PayrollResult:
    return PayrollResult(
        batch_id=batch.id,
        batch_version=batch.version,
        employee_id=employee.id,
        version=version,
        org_unit_id=employee.org_unit_id,
        department=employee.department,
        actual_attendance_days=Decimal("22"),
        gross=Decimal(gross),
        deposit=Decimal("0"),
        net=Decimal(net),
        carry_forward=Decimal("0"),
        rule_version="v2",
        input_snapshot={},
        lines=[],
        exceptions=[],
        warnings=[],
        has_error=False,
    )


def _seed_dashboard_data(session):
    group = OrgUnit(code="DASH_GROUP", name="Group", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    north = OrgUnit(code="DASH_NORTH", name="North", type=OrgType.REGION, parent_id=group.id)
    south = OrgUnit(code="DASH_SOUTH", name="South", type=OrgType.REGION, parent_id=group.id)
    session.add_all([north, south])
    session.flush()
    north_store = OrgUnit(
        code="DASH_NORTH_STORE", name="North store", type=OrgType.STORE, parent_id=north.id
    )
    south_store = OrgUnit(
        code="DASH_SOUTH_STORE", name="South store", type=OrgType.STORE, parent_id=south.id
    )
    session.add_all([north_store, south_store])
    session.flush()
    north_employee = Employee(
        emp_no="DASH-NORTH-1",
        name="North employee",
        org_unit_id=north_store.id,
        department=Department.DINING,
    )
    south_employee = Employee(
        emp_no="DASH-SOUTH-1",
        name="South employee",
        org_unit_id=south_store.id,
        department=Department.KITCHEN,
    )
    session.add_all([north_employee, south_employee])
    session.flush()
    current = PayrollBatch(
        period="2026-07",
        attendance_start=date(2026, 6, 26),
        attendance_end=date(2026, 7, 25),
        status=BatchStatus.LOCKED,
        version=1,
    )
    prior = PayrollBatch(
        period="2026-06",
        attendance_start=date(2026, 5, 26),
        attendance_end=date(2026, 6, 25),
        status=BatchStatus.LOCKED,
        version=1,
    )
    session.add_all([current, prior])
    session.flush()
    # Version 2 is the current corrected result; version 1 must not be
    # included in any dashboard aggregate.
    session.add_all(
        [
            _result(current, north_employee, gross="100", net="90", version=1),
            _result(current, north_employee, gross="120", net="108", version=2),
            _result(current, south_employee, gross="200", net="180"),
            _result(prior, north_employee, gross="90", net="81"),
        ]
    )
    session.add_all(
        [
            LaborBudget(
                org_unit_id=north_store.id,
                period=date(2026, 7, 1),
                headcount_budget=1,
                labor_cost_budget=Decimal("150"),
            ),
            LaborBudget(
                org_unit_id=south_store.id,
                period=date(2026, 7, 1),
                headcount_budget=1,
                labor_cost_budget=Decimal("180"),
            ),
            LaborBudget(
                org_unit_id=north_store.id,
                period=date(2026, 6, 1),
                headcount_budget=1,
                labor_cost_budget=Decimal("100"),
            ),
        ]
    )
    session.commit()
    return {"north": north, "north_store": north_store, "south_store": south_store}


def test_dashboard_uses_locked_current_results_and_store_budgets(client, db_session):
    orgs = _seed_dashboard_data(db_session)
    admin = _user(db_session, "dashboard-admin", ["GROUP_HR"])
    headers = _token(client, admin.username)

    response = client.get("/api/dashboard", headers=headers, params={"period": "2026-07"})
    assert response.status_code == 200, response.text
    body = response.json()
    metrics = body["metrics"]
    assert metrics["employee_count"] == 2
    assert metrics["actual_gross"] == "320.00"
    assert metrics["actual_net"] == "288.00"
    assert Decimal(metrics["average_gross"]) == Decimal("160")
    assert metrics["budget_headcount"] == 2
    assert metrics["budget_cost"] == "330.00"
    assert metrics["headcount_variance"] == 0
    assert metrics["cost_variance"] == "-10.00"
    assert [row["org_unit_id"] for row in body["store_ranking"]] == [
        orgs["south_store"].id,
        orgs["north_store"].id,
    ]
    assert body["trend"] == [
        {
            "period": "2026-06",
            "employee_count": 1,
            "actual_gross": "90.00",
            "budget_cost": "100.00",
        },
        {
            "period": "2026-07",
            "employee_count": 2,
            "actual_gross": "320.00",
            "budget_cost": "330.00",
        },
    ]
    assert db_session.scalars(
        select(AuditLog).where(
            AuditLog.actor_user_id == admin.id, AuditLog.action == "dashboard.view"
        )
    ).one()


def test_dashboard_is_limited_to_the_user_organization_scope(client, db_session):
    orgs = _seed_dashboard_data(db_session)
    _user(db_session, "north-manager", ["REGION_MANAGER"], [orgs["north"].id])

    response = client.get(
        "/api/dashboard", headers=_token(client, "north-manager"), params={"period": "2026-07"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics"]["employee_count"] == 1
    assert body["metrics"]["actual_gross"] == "120.00"
    assert body["metrics"]["budget_cost"] == "150.00"
    assert [row["org_unit_id"] for row in body["store_ranking"]] == [orgs["north_store"].id]


def test_unrelated_global_role_does_not_widen_scoped_dashboard(client, db_session):
    orgs = _seed_dashboard_data(db_session)
    _mixed_scope_dashboard_roles(db_session)
    _user(
        db_session,
        "mixed-dashboard",
        ["GLOBAL_AUDIT_ONLY", "SCOPED_DASHBOARD"],
        [orgs["north"].id],
    )

    response = client.get(
        "/api/dashboard",
        headers=_token(client, "mixed-dashboard"),
        params={"period": "2026-07"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["metrics"]["actual_gross"] == "120.00"


def test_dashboard_requires_permission_and_valid_period(client, db_session):
    _seed_dashboard_data(db_session)
    _user(db_session, "ordinary", ["EMPLOYEE"])
    headers = _token(client, "ordinary")

    assert (
        client.get("/api/dashboard", headers=headers, params={"period": "2026-07"}).status_code
        == 403
    )
    _user(db_session, "dashboard-admin", ["GROUP_HR"])
    assert (
        client.get(
            "/api/dashboard",
            headers=_token(client, "dashboard-admin"),
            params={"period": "2026-13"},
        ).status_code
        == 422
    )
