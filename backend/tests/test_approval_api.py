from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.approval import service as approvals
from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
from app.models.approval import ApprovalBusinessType, ApprovalFlow
from app.models.auth import Permission, Role, RolePermission, User, UserOrgScope, UserRole
from app.models.comp import EmployeeSalaryStructure, SalaryComponentDef
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _user(session, username: str, roles: list[str], *, org_scope_id: int | None = None) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for code in roles:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    if org_scope_id is not None:
        session.add(UserOrgScope(user_id=user.id, org_unit_id=org_scope_id))
    session.flush()
    return user


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


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _employee_in_region(session) -> tuple[OrgUnit, OrgUnit, Employee]:
    region = OrgUnit(code="R1", name="Region", type=OrgType.REGION, city="Guangzhou")
    session.add(region)
    session.flush()
    store = OrgUnit(
        code="S1", name="Store", type=OrgType.STORE, city="Guangzhou", parent_id=region.id
    )
    session.add(store)
    session.flush()
    employee = Employee(emp_no="E1", name="Employee", org_unit_id=store.id)
    session.add(employee)
    session.flush()
    return region, store, employee


def _flow(client, headers: dict[str, str], region_id: int) -> dict:
    response = client.post(
        "/api/approval-flows",
        headers=headers,
        json={
            "code": "RAISE-DEFAULT",
            "name": "Salary raise approval",
            "business_type": "SALARY_ADJUSTMENT",
            "org_unit_id": region_id,
            "min_amount": "0",
            "steps": [
                {"step_order": 1, "name": "Regional HR", "role_code": "REGION_MANAGER"},
                {"step_order": 2, "name": "Group HR", "role_code": "GROUP_HR"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _component(client, headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "BASE", "name": "Base", "component_type": "BASE"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_adjustment(
    client,
    headers: dict[str, str],
    employee_id: int,
    component_id: int,
    *,
    reason: str = "Quarterly performance adjustment",
) -> dict:
    response = client.post(
        "/api/salary-adjustments",
        headers=headers,
        json={
            "employee_id": employee_id,
            "component_id": component_id,
            "amount": "5500",
            "effective_from": "2026-08-01",
            "reason": reason,
            "attachment_url": "https://files.example.test/adjustments/raise.pdf",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_salary_adjustment_requires_separate_approvers_and_writes_only_after_final_approval(
    client, db_session
):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    regional_approver = _user(
        db_session, "regional-approver", ["REGION_MANAGER"], org_scope_id=region.id
    )
    _group_hr = _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    _flow(client, group_headers, region.id)

    adjustment = _create_adjustment(
        client, _token(client, "requester"), employee.id, component["id"]
    )
    submitted = client.post(
        f"/api/salary-adjustments/{adjustment['id']}/submit", headers=_token(client, "requester")
    )
    assert submitted.status_code == 200, submitted.text
    instance_id = submitted.json()["approval_instance_id"]
    assert submitted.json()["status"] == "PENDING"
    assert db_session.scalars(select(EmployeeSalaryStructure)).all() == []

    self_approve = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=_token(client, "requester"),
        json={"decision": "APPROVE", "comment": "self approval attempt"},
    )
    assert self_approve.status_code == 403

    regional_todos = client.get(
        "/api/approval-instances/todos", headers=_token(client, "regional-approver")
    )
    assert regional_todos.status_code == 200
    assert [item["id"] for item in regional_todos.json()] == [instance_id]
    assert regional_todos.json()[0]["current_step_order"] == 1

    first_approval = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=_token(client, "regional-approver"),
        json={"decision": "APPROVE", "comment": "Regional review complete"},
    )
    assert first_approval.status_code == 200, first_approval.text
    assert first_approval.json()["status"] == "PENDING"
    assert first_approval.json()["current_step_order"] == 2
    assert db_session.scalars(select(EmployeeSalaryStructure)).all() == []

    group_todos = client.get("/api/approval-instances/todos", headers=group_headers)
    assert [item["id"] for item in group_todos.json()] == [instance_id]
    final_approval = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=group_headers,
        json={"decision": "APPROVE", "comment": "Group approval complete"},
    )
    assert final_approval.status_code == 200, final_approval.text
    assert final_approval.json()["status"] == "APPROVED"

    stored = db_session.scalars(select(EmployeeSalaryStructure)).one()
    assert stored.employee_id == employee.id
    assert stored.component_id == component["id"]
    assert stored.amount == Decimal("5500")
    assert stored.effective_from == date(2026, 8, 1)

    trajectory = client.get(f"/api/approval-instances/{instance_id}", headers=group_headers)
    assert trajectory.status_code == 200
    assert [item["actor_id"] for item in trajectory.json()["actions"]] == [
        regional_approver.id,
        _group_hr.id,
    ]


def test_final_salary_adjustment_approval_rechecks_component_activity(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "inactive-requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(
        db_session,
        "inactive-regional-approver",
        ["REGION_MANAGER"],
        org_scope_id=region.id,
    )
    _user(db_session, "inactive-group-hr", ["GROUP_HR"])
    group_headers = _token(client, "inactive-group-hr")
    component = _component(client, group_headers)
    _flow(client, group_headers, region.id)
    adjustment = _create_adjustment(
        client,
        _token(client, "inactive-requester"),
        employee.id,
        component["id"],
    )
    submitted = client.post(
        f"/api/salary-adjustments/{adjustment['id']}/submit",
        headers=_token(client, "inactive-requester"),
    )
    assert submitted.status_code == 200, submitted.text
    instance_id = submitted.json()["approval_instance_id"]
    first_approval = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=_token(client, "inactive-regional-approver"),
        json={"decision": "APPROVE", "comment": "Regional review complete"},
    )
    assert first_approval.status_code == 200, first_approval.text

    # Simulate an inactive catalog row inherited from a legacy deployment.
    # The lifecycle endpoint itself rejects deactivation while this request is
    # pending, but final approval must still defend against pre-existing data.
    stored_component = db_session.get(SalaryComponentDef, component["id"])
    assert stored_component is not None
    stored_component.is_deleted = True
    stored_component.deleted_at = datetime.now(UTC)
    db_session.commit()

    final_approval = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=group_headers,
        json={"decision": "APPROVE", "comment": "Group approval complete"},
    )

    assert final_approval.status_code == 409
    assert "inactive" in str(final_approval.json()["detail"]).lower()
    assert db_session.scalars(select(EmployeeSalaryStructure)).all() == []


def test_final_approval_persists_a_2000_character_adjustment_reason(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "regional-approver", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    _flow(client, group_headers, region.id)
    long_reason = "R" * 1500
    adjustment = _create_adjustment(
        client,
        _token(client, "requester"),
        employee.id,
        component["id"],
        reason=long_reason,
    )
    instance_id = client.post(
        f"/api/salary-adjustments/{adjustment['id']}/submit",
        headers=_token(client, "requester"),
    ).json()["approval_instance_id"]

    first_step = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=_token(client, "regional-approver"),
        json={"decision": "APPROVE", "comment": "Regional review complete"},
    )
    assert first_step.status_code == 200, first_step.text
    final_step = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=group_headers,
        json={"decision": "APPROVE", "comment": "Group approval complete"},
    )
    assert final_step.status_code == 200, final_step.text
    stored = db_session.scalars(select(EmployeeSalaryStructure)).one()
    assert stored.source_reason == long_reason


def test_rejection_is_terminal_and_never_mutates_salary_structure(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "regional-approver", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    _flow(client, group_headers, region.id)
    adjustment = _create_adjustment(
        client, _token(client, "requester"), employee.id, component["id"]
    )
    submitted = client.post(
        f"/api/salary-adjustments/{adjustment['id']}/submit", headers=_token(client, "requester")
    ).json()

    rejected = client.post(
        f"/api/approval-instances/{submitted['approval_instance_id']}/decisions",
        headers=_token(client, "regional-approver"),
        json={"decision": "REJECT", "comment": "Budget evidence is insufficient"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert db_session.scalars(select(EmployeeSalaryStructure)).all() == []

    detail = client.get(f"/api/salary-adjustments/{adjustment['id']}", headers=group_headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "REJECTED"


def test_salary_adjustment_cannot_be_created_outside_the_requesters_organization_scope(
    client, db_session
):
    region_one, _store_one, _employee_one = _employee_in_region(db_session)
    region_two = OrgUnit(code="R2", name="Other Region", type=OrgType.REGION, city="Shenzhen")
    db_session.add(region_two)
    db_session.flush()
    other_store = OrgUnit(
        code="S2", name="Other Store", type=OrgType.STORE, city="Shenzhen", parent_id=region_two.id
    )
    db_session.add(other_store)
    db_session.flush()
    other_employee = Employee(emp_no="E2", name="Other Employee", org_unit_id=other_store.id)
    db_session.add(other_employee)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region_one.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)

    response = client.post(
        "/api/salary-adjustments",
        headers=_token(client, "requester"),
        json={
            "employee_id": other_employee.id,
            "component_id": component["id"],
            "amount": "5500",
            "effective_from": "2026-08-01",
            "reason": "Attempt outside scope",
            "attachment_url": "https://files.example.test/adjustments/outside-scope.pdf",
        },
    )
    assert response.status_code == 404


def test_noop_salary_adjustment_is_rejected_before_it_can_enter_approval(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    initial = client.put(
        f"/api/employees/{employee.id}/structure/{component['id']}",
        headers=group_headers,
        json={"amount": "5500", "effective_from": "2026-08-01"},
    )
    assert initial.status_code == 200, initial.text

    response = client.post(
        "/api/salary-adjustments",
        headers=_token(client, "requester"),
        json={
            "employee_id": employee.id,
            "component_id": component["id"],
            "amount": "5500",
            "effective_from": "2026-08-01",
            "reason": "No-op request",
            "attachment_url": "https://files.example.test/adjustments/noop.pdf",
        },
    )
    assert response.status_code == 422
    assert "must change" in response.json()["detail"]


def test_signed_zero_adjustment_is_canonicalized_and_rejected_as_a_noop(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    assert (
        client.put(
            f"/api/employees/{employee.id}/structure/{component['id']}",
            headers=group_headers,
            json={"amount": "0.00", "effective_from": "2026-08-01"},
        ).status_code
        == 200
    )

    response = client.post(
        "/api/salary-adjustments",
        headers=_token(client, "requester"),
        json={
            "employee_id": employee.id,
            "component_id": component["id"],
            "amount": "-0.00",
            "effective_from": "2026-08-01",
            "reason": "Signed zero no-op",
            "attachment_url": "https://files.example.test/adjustments/zero.pdf",
        },
    )
    assert response.status_code == 422
    assert "must change" in response.json()["detail"]


def test_submit_rechecks_requester_organization_scope(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    requester = _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    _flow(client, group_headers, region.id)
    requester_headers = _token(client, "requester")
    adjustment = _create_adjustment(client, requester_headers, employee.id, component["id"])

    scope = db_session.scalars(
        select(UserOrgScope).where(UserOrgScope.user_id == requester.id)
    ).one()
    db_session.delete(scope)
    db_session.commit()

    response = client.post(
        f"/api/salary-adjustments/{adjustment['id']}/submit", headers=requester_headers
    )
    assert response.status_code == 404


def test_create_flow_rejects_overlapping_amount_ranges_at_the_same_organization(client, db_session):
    assert not approvals._ranges_overlap(
        Decimal("0"), Decimal("0"), Decimal("0.01"), Decimal("100")
    )
    region, _store, _employee = _employee_in_region(db_session)
    _user(db_session, "group-hr", ["GROUP_HR"])
    headers = _token(client, "group-hr")
    first = client.post(
        "/api/approval-flows",
        headers=headers,
        json={
            "code": "RANGE-A",
            "name": "Range A",
            "business_type": "SALARY_ADJUSTMENT",
            "org_unit_id": region.id,
            "min_amount": "0",
            "max_amount": "100",
            "steps": [{"step_order": 1, "name": "Group HR", "role_code": "GROUP_HR"}],
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/approval-flows",
        headers=headers,
        json={
            "code": "RANGE-B",
            "name": "Range B",
            "business_type": "SALARY_ADJUSTMENT",
            "org_unit_id": region.id,
            "min_amount": "50",
            "max_amount": "1000",
            "steps": [{"step_order": 1, "name": "Group HR", "role_code": "GROUP_HR"}],
        },
    )
    assert second.status_code == 422
    assert "overlaps" in second.json()["detail"]


def test_legacy_overlapping_flows_fail_closed_when_routing(db_session):
    region, _store, _employee = _employee_in_region(db_session)
    db_session.add_all(
        [
            ApprovalFlow(
                code="LEGACY-RANGE-A",
                name="Legacy A",
                business_type=ApprovalBusinessType.SALARY_ADJUSTMENT,
                org_unit_id=region.id,
                min_amount=Decimal("0"),
                max_amount=Decimal("100"),
                is_active=True,
            ),
            ApprovalFlow(
                code="LEGACY-RANGE-B",
                name="Legacy B",
                business_type=ApprovalBusinessType.SALARY_ADJUSTMENT,
                org_unit_id=region.id,
                min_amount=Decimal("50"),
                max_amount=Decimal("1000"),
                is_active=True,
            ),
        ]
    )
    db_session.flush()

    with pytest.raises(approvals.ApprovalError, match="ambiguous"):
        approvals.select_flow(
            db_session,
            business_type=ApprovalBusinessType.SALARY_ADJUSTMENT,
            org_unit_id=region.id,
            amount=Decimal("75"),
        )


def test_flow_list_is_limited_to_the_readers_organization_scope(client, db_session):
    region_one, _store_one, _employee_one = _employee_in_region(db_session)
    region_two = OrgUnit(code="R2", name="Other Region", type=OrgType.REGION, city="Shenzhen")
    db_session.add(region_two)
    db_session.flush()
    _user(db_session, "regional-reader", ["REGION_MANAGER"], org_scope_id=region_one.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    _flow(client, group_headers, region_one.id)
    other = client.post(
        "/api/approval-flows",
        headers=group_headers,
        json={
            "code": "OTHER-REGION",
            "name": "Other region approval",
            "business_type": "SALARY_ADJUSTMENT",
            "org_unit_id": region_two.id,
            "steps": [{"step_order": 1, "name": "Group HR", "role_code": "GROUP_HR"}],
        },
    )
    assert other.status_code == 201, other.text

    response = client.get("/api/approval-flows", headers=_token(client, "regional-reader"))
    assert response.status_code == 200, response.text
    assert [flow["code"] for flow in response.json()] == ["RAISE-DEFAULT"]


def test_approve_only_role_cannot_list_full_salary_adjustment_documents(client, db_session):
    region, _store, employee = _employee_in_region(db_session)
    _user(db_session, "requester", ["REGION_MANAGER"], org_scope_id=region.id)
    _user(db_session, "group-hr", ["GROUP_HR"])
    group_headers = _token(client, "group-hr")
    component = _component(client, group_headers)
    adjustment = _create_adjustment(
        client, _token(client, "requester"), employee.id, component["id"]
    )

    approve_only = Role(code="APPROVE_ONLY", name="Approve only", is_global_scope=False)
    db_session.add(approve_only)
    db_session.flush()
    approve_permission = db_session.scalars(
        select(Permission).where(Permission.code == Perm.ADJUSTMENT_APPROVE)
    ).one()
    db_session.add(RolePermission(role_id=approve_only.id, permission_id=approve_permission.id))
    approver = _user(db_session, "approve-only", [], org_scope_id=region.id)
    db_session.add(UserRole(user_id=approver.id, role_id=approve_only.id))
    db_session.commit()

    response = client.get("/api/salary-adjustments", headers=_token(client, "approve-only"))
    assert response.status_code == 403
    assert adjustment["id"] > 0
