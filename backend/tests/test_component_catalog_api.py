from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.approval import SalaryAdjustment, SalaryAdjustmentStatus
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.comp import SalaryComponentDef
from app.models.employee import Employee
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


def _user(session, username: str, role_codes: list[str]) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for role_code in role_codes:
        role = session.scalars(select(Role).where(Role.code == role_code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _employee(session, *, suffix: str) -> Employee:
    store = OrgUnit(
        code=f"COMP_STORE_{suffix}",
        name=f"Component store {suffix}",
        type=OrgType.STORE,
        city="Guangzhou",
    )
    session.add(store)
    session.flush()
    employee = Employee(
        emp_no=f"COMP_EMP_{suffix}",
        name=f"Component employee {suffix}",
        org_unit_id=store.id,
    )
    session.add(employee)
    session.flush()
    return employee


def _component(
    client,
    headers: dict[str, str],
    *,
    code: str,
    name: str | None = None,
    component_type: str = "BASE",
    sort_order: int = 0,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "code": code,
        "name": name or code.title(),
        "component_type": component_type,
        "sort_order": sort_order,
    }
    payload.update(extra)
    response = client.post("/api/salary-components", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _deactivate(
    client, headers: dict[str, str], component_id: int, *, reason: str = "Catalog cleanup"
):
    return client.post(
        f"/api/salary-components/{component_id}/deactivate",
        headers=headers,
        json={"reason": reason},
    )


def _initial_structure(
    client,
    headers: dict[str, str],
    employee_id: int,
    items: list[dict[str, object]],
):
    return client.put(
        f"/api/employees/{employee_id}/initial-structure",
        headers=headers,
        json={"effective_from": "2026-01-01", "items": items},
    )


def _persist_payroll_result(session, employee: Employee) -> None:
    batch = PayrollBatch(
        period="2026-01",
        attendance_start=date(2026, 1, 1),
        attendance_end=date(2026, 1, 31),
        status=BatchStatus.CONFIRMED,
        version=1,
    )
    session.add(batch)
    session.flush()
    session.add(
        PayrollResult(
            batch_id=batch.id,
            employee_id=employee.id,
            batch_version=1,
            version=1,
            org_unit_id=employee.org_unit_id,
            department=employee.department,
            actual_attendance_days=Decimal("22"),
            gross=Decimal("5000"),
            deposit=Decimal("0"),
            net=Decimal("5000"),
            carry_forward=Decimal("0"),
            rule_version="component-catalog-test",
            input_snapshot={},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    session.flush()


def test_catalog_defaults_to_active_and_supports_inactive_and_all_filters(client, db_session):
    _user(db_session, "component-catalog-hr", ["GROUP_HR"])
    headers = _token(client, "component-catalog-hr")
    active = _component(client, headers, code="ACTIVE_BASE", sort_order=10)
    inactive = _component(client, headers, code="OLD_BASE", sort_order=20)

    deactivated = _deactivate(client, headers, inactive["id"], reason="Retired pay policy")
    assert deactivated.status_code == 200, deactivated.text

    default_items = client.get("/api/salary-components", headers=headers)
    inactive_items = client.get(
        "/api/salary-components", headers=headers, params={"status": "inactive"}
    )
    all_items = client.get("/api/salary-components", headers=headers, params={"status": "all"})
    invalid_filter = client.get(
        "/api/salary-components", headers=headers, params={"status": "retired"}
    )

    assert default_items.status_code == 200, default_items.text
    assert [item["id"] for item in default_items.json()] == [active["id"]]
    assert [item["id"] for item in inactive_items.json()] == [inactive["id"]]
    assert [item["id"] for item in all_items.json()] == [active["id"], inactive["id"]]
    assert invalid_filter.status_code == 422

    active_item = default_items.json()[0]
    inactive_item = inactive_items.json()[0]
    assert active_item["is_active"] is True
    assert active_item["deactivated_at"] is None
    assert active_item["calculation_locked"] is False
    assert active_item["updated_at"]
    assert inactive_item["is_active"] is False
    assert inactive_item["deactivated_at"]
    assert inactive_item["calculation_locked"] is False

    duplicate = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": " old_base ", "name": "Replacement", "component_type": "BASE"},
    )
    assert duplicate.status_code == 409
    duplicate_detail = str(duplicate.json()["detail"])
    assert "restore" in duplicate_detail.lower() or "恢复" in duplicate_detail


def test_component_patch_is_optimistic_rejects_noops_and_edits_all_unlocked_metadata(
    client, db_session
):
    _user(db_session, "component-edit-hr", ["GROUP_HR"])
    headers = _token(client, "component-edit-hr")
    component = _component(
        client,
        headers,
        code="MEAL_ALLOWANCE",
        name="Meal allowance",
        component_type="ALLOWANCE",
        allowance_kind="FIXED",
    )
    expected_updated_at = component["updated_at"]
    endpoint = f"/api/salary-components/{component['id']}"

    missing_version = client.patch(endpoint, headers=headers, json={"name": "Meal benefit"})
    empty = client.patch(
        endpoint,
        headers=headers,
        json={"expected_updated_at": expected_updated_at},
    )
    same_value = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_updated_at": expected_updated_at,
            "name": component["name"],
        },
    )
    assert missing_version.status_code == 422
    assert empty.status_code == 422
    assert same_value.status_code == 422

    updated = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_updated_at": expected_updated_at,
            "name": "Meal and commute benefit",
            "taxable": False,
            "in_social_base": True,
            "in_housing_base": True,
            "prorate_by_attendance": True,
            "allowance_kind": "FLOATING",
            "sort_order": 30,
        },
    )
    assert updated.status_code == 200, updated.text
    assert {
        "name": updated.json()["name"],
        "taxable": updated.json()["taxable"],
        "in_social_base": updated.json()["in_social_base"],
        "in_housing_base": updated.json()["in_housing_base"],
        "prorate_by_attendance": updated.json()["prorate_by_attendance"],
        "allowance_kind": updated.json()["allowance_kind"],
        "sort_order": updated.json()["sort_order"],
    } == {
        "name": "Meal and commute benefit",
        "taxable": False,
        "in_social_base": True,
        "in_housing_base": True,
        "prorate_by_attendance": True,
        "allowance_kind": "FLOATING",
        "sort_order": 30,
    }
    assert updated.json()["updated_at"] != expected_updated_at

    stale = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_updated_at": expected_updated_at,
            "name": "Stale overwrite",
        },
    )
    assert stale.status_code == 409


def test_deactivate_and_restore_require_reasons_are_idempotent_and_audited(client, db_session):
    hr = _user(db_session, "component-lifecycle-hr", ["GROUP_HR"])
    headers = _token(client, hr.username)
    component = _component(client, headers, code="LIFECYCLE_BASE")

    missing_reason = client.post(
        f"/api/salary-components/{component['id']}/deactivate",
        headers=headers,
        json={},
    )
    blank_reason = _deactivate(client, headers, component["id"], reason="   ")
    assert missing_reason.status_code == 422
    assert blank_reason.status_code == 422

    first_deactivation = _deactivate(
        client,
        headers,
        component["id"],
        reason="  Superseded by the 2026 catalog  ",
    )
    assert first_deactivation.status_code == 200, first_deactivation.text
    assert first_deactivation.json()["is_active"] is False
    deactivated_at = first_deactivation.json()["deactivated_at"]

    repeated_deactivation = _deactivate(
        client,
        headers,
        component["id"],
        reason="Repeated lifecycle request",
    )
    assert repeated_deactivation.status_code == 200, repeated_deactivation.text
    assert repeated_deactivation.json()["is_active"] is False
    assert repeated_deactivation.json()["deactivated_at"] == deactivated_at

    restore_endpoint = f"/api/salary-components/{component['id']}/restore"
    missing_restore_reason = client.post(restore_endpoint, headers=headers, json={})
    blank_restore_reason = client.post(
        restore_endpoint,
        headers=headers,
        json={"reason": "   "},
    )
    assert missing_restore_reason.status_code == 422
    assert blank_restore_reason.status_code == 422

    restored = client.post(
        restore_endpoint,
        headers=headers,
        json={"reason": "  Approved for renewed use  "},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["is_active"] is True
    assert restored.json()["deactivated_at"] is None
    restored_updated_at = restored.json()["updated_at"]

    repeated_restore = client.post(
        restore_endpoint,
        headers=headers,
        json={"reason": "Repeated lifecycle request"},
    )
    assert repeated_restore.status_code == 200, repeated_restore.text
    assert repeated_restore.json()["is_active"] is True
    assert repeated_restore.json()["updated_at"] == restored_updated_at

    lifecycle_audits = list(
        db_session.scalars(
            select(AuditLog)
            .where(
                AuditLog.actor_user_id == hr.id,
                AuditLog.target_type == "salary_component_def",
                AuditLog.target_id == component["id"],
                AuditLog.action.in_(("component.deactivate", "component.restore")),
            )
            .order_by(AuditLog.id)
        ).all()
    )
    deactivation_audit = next(
        row for row in lifecycle_audits if row.action == "component.deactivate"
    )
    restore_audit = next(row for row in lifecycle_audits if row.action == "component.restore")
    assert deactivation_audit.detail["reason"] == "Superseded by the 2026 catalog"
    assert deactivation_audit.detail["before"]["is_active"] is True
    assert deactivation_audit.detail["after"]["is_active"] is False
    assert restore_audit.detail["reason"] == "Approved for renewed use"
    assert restore_audit.detail["before"]["is_active"] is False
    assert restore_audit.detail["after"]["is_active"] is True


def test_catalog_marks_calculation_metadata_locked_after_payroll_use(client, db_session):
    _user(db_session, "component-lock-hr", ["GROUP_HR"])
    headers = _token(client, "component-lock-hr")
    employee = _employee(db_session, suffix="LOCK")
    fresh = _component(client, headers, code="FRESH_BASE", sort_order=10)
    used = _component(client, headers, code="USED_BASE", sort_order=20)
    initialized = _initial_structure(
        client,
        headers,
        employee.id,
        [{"component_id": used["id"], "amount": "5000"}],
    )
    assert initialized.status_code == 201, initialized.text
    _persist_payroll_result(db_session, employee)

    listed = client.get("/api/salary-components", headers=headers)

    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()}
    assert by_id[fresh["id"]]["calculation_locked"] is False
    assert by_id[used["id"]]["calculation_locked"] is True


def test_pending_salary_adjustment_blocks_component_deactivation(client, db_session):
    hr = _user(db_session, "component-pending-hr", ["GROUP_HR"])
    headers = _token(client, hr.username)
    employee = _employee(db_session, suffix="PENDING")
    component = _component(client, headers, code="PENDING_BASE")
    adjustment = SalaryAdjustment(
        employee_id=employee.id,
        org_unit_id=employee.org_unit_id,
        component_id=component["id"],
        amount=Decimal("5500"),
        effective_from=date(2026, 8, 1),
        reason="Pending approved route",
        attachment_url="https://files.example.test/adjustments/pending.pdf",
        requester_id=hr.id,
        status=SalaryAdjustmentStatus.PENDING,
        before_snapshot={"record_exists": False},
    )
    db_session.add(adjustment)
    db_session.flush()

    response = _deactivate(client, headers, component["id"], reason="Retire old component")

    assert response.status_code == 409
    assert "pending" in str(response.json()["detail"]).lower()
    active_ids = {
        item["id"] for item in client.get("/api/salary-components", headers=headers).json()
    }
    assert component["id"] in active_ids
    stored_component = db_session.get(SalaryComponentDef, component["id"])
    assert stored_component is not None and stored_component.is_deleted is False


def test_inactive_component_keeps_existing_payroll_input_but_blocks_new_references(
    client, db_session
):
    _user(db_session, "component-reference-hr", ["GROUP_HR"])
    headers = _token(client, "component-reference-hr")
    existing_employee = _employee(db_session, suffix="EXISTING")
    new_employee = _employee(db_session, suffix="NEW")
    component = _component(client, headers, code="LEGACY_BASE")
    initialized = _initial_structure(
        client,
        headers,
        existing_employee.id,
        [{"component_id": component["id"], "amount": "5000"}],
    )
    assert initialized.status_code == 201, initialized.text

    deactivated = _deactivate(client, headers, component["id"], reason="No longer selectable")
    assert deactivated.status_code == 200, deactivated.text

    existing = client.get(
        f"/api/employees/{existing_employee.id}/structure",
        headers=headers,
        params={"on_date": "2026-06-01"},
    )
    assert existing.status_code == 200, existing.text
    assert existing.json()["compa"]["total"] == "5000.00"
    assert existing.json()["items"][0]["component_id"] == component["id"]

    new_structure = _initial_structure(
        client,
        headers,
        new_employee.id,
        [{"component_id": component["id"], "amount": "5000"}],
    )
    new_adjustment = client.post(
        "/api/salary-adjustments",
        headers=headers,
        json={
            "employee_id": existing_employee.id,
            "component_id": component["id"],
            "amount": "5500",
            "effective_from": "2026-08-01",
            "reason": "Attempt to reuse retired component",
            "attachment_url": "https://files.example.test/adjustments/retired.pdf",
        },
    )
    assert new_structure.status_code == 409
    assert new_adjustment.status_code == 409
    assert "inactive" in str(new_structure.json()["detail"]).lower()
    assert "inactive" in str(new_adjustment.json()["detail"]).lower()


def test_structure_history_includes_record_revision_and_component_identity_in_stable_order(
    client, db_session
):
    from app.comp.service import set_component_amount

    _user(db_session, "component-history-hr", ["GROUP_HR"])
    headers = _token(client, "component-history-hr")
    employee = _employee(db_session, suffix="HISTORY")
    position = _component(
        client,
        headers,
        code="POSITION_PAY",
        name="Position pay",
        component_type="POSITION",
        sort_order=20,
    )
    base = _component(
        client,
        headers,
        code="BASE_PAY",
        name="Base pay",
        component_type="BASE",
        sort_order=10,
    )
    initialized = _initial_structure(
        client,
        headers,
        employee.id,
        [
            {"component_id": position["id"], "amount": "300"},
            {"component_id": base["id"], "amount": "5000"},
        ],
    )
    assert initialized.status_code == 201, initialized.text

    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=base["id"],
        amount=Decimal("5200"),
        effective_from=date(2026, 1, 1),
    )
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=position["id"],
        amount=Decimal("400"),
        effective_from=date(2026, 3, 1),
    )
    db_session.commit()
    deactivated = _deactivate(
        client,
        headers,
        base["id"],
        reason="Preserve history while retiring component",
    )
    assert deactivated.status_code == 200, deactivated.text

    response = client.get(
        f"/api/employees/{employee.id}/structure/history",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    history = response.json()
    assert [
        (
            item["component_code"],
            item["effective_from"],
            item["revision"],
            item["amount"],
        )
        for item in history
    ] == [
        ("BASE_PAY", "2026-01-01", 1, "5000.00"),
        ("BASE_PAY", "2026-01-01", 2, "5200.00"),
        ("POSITION_PAY", "2026-01-01", 1, "300.00"),
        ("POSITION_PAY", "2026-03-01", 1, "400.00"),
    ]
    assert all(item["id"] > 0 for item in history)
    assert all(item["component_name"] for item in history)
    assert all(item["component_type"] in {"BASE", "POSITION"} for item in history)
    assert [item["component_is_active"] for item in history] == [False, False, True, True]
