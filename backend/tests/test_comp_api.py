from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Permission, Role, RolePermission, User, UserRole
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import AdjustmentRecord, PayrollResult
from app.payroll.batch_service import run_batch

pytestmark = pytest.mark.usefixtures("pg_engine")


def _user(session, username, roles):
    seed_rbac(session)
    u = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(u)
    session.flush()
    for code in roles:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=u.id, role_id=role.id))
    session.flush()
    return u


def _employee(session):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    session.add(store)
    session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    session.add(emp)
    session.flush()
    return emp


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


def _token(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "StrongPass123!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _reopened_batch_with_prior_result(session, emp):
    batch = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.DRAFT,
        version=2,
    )
    session.add(batch)
    session.flush()
    session.add(
        PayrollResult(
            batch_id=batch.id,
            employee_id=emp.id,
            batch_version=1,
            version=1,
            org_unit_id=emp.org_unit_id,
            department=emp.department,
            actual_attendance_days=Decimal("22"),
            gross=Decimal("5000"),
            deposit=Decimal("5000"),
            net=Decimal("5000"),
            carry_forward=Decimal("0"),
            rule_version="v2",
            input_snapshot={},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    session.flush()
    return batch


def test_component_crud_and_permission(client, db_session):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.post(
        "/api/salary-components",
        headers=h,
        json={"code": "BASE", "name": "基本薪资", "component_type": "BASE", "in_social_base": True},
    )
    assert r.status_code == 201
    assert r.json()["in_social_base"] is True
    assert r.json()["prorate_by_attendance"] is False
    assert len(client.get("/api/salary-components", headers=h).json()) == 1
    # 重复编码 409
    assert (
        client.post(
            "/api/salary-components",
            headers=h,
            json={"code": "BASE", "name": "x", "component_type": "BASE"},
        ).status_code
        == 409
    )


def test_component_requires_permission(client, db_session):
    _user(db_session, "emp", ["EMPLOYEE"])
    h = _token(client, "emp")
    assert client.get("/api/salary-components", headers=h).status_code == 403


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "taxable",
        "in_social_base",
        "in_housing_base",
        "prorate_by_attendance",
        "sort_order",
    ],
)
def test_component_update_rejects_explicit_null_for_nonnullable_fields(client, db_session, field):
    _user(db_session, f"hr-null-{field}", ["GROUP_HR"])
    headers = _token(client, f"hr-null-{field}")
    component = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": f"NULL_{field.upper()}", "name": "Null guard", "component_type": "BASE"},
    ).json()

    response = client.patch(
        f"/api/salary-components/{component['id']}",
        headers=headers,
        json={field: None},
    )

    assert response.status_code == 422
    fetched = client.get("/api/salary-components", headers=headers).json()
    assert (
        next(item for item in fetched if item["id"] == component["id"])[field] == component[field]
    )


def test_component_calculation_flags_are_immutable_after_payroll_use(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr-component-history", ["GROUP_HR"])
    headers = _token(client, "hr-component-history")
    component = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "HIST_BASE", "name": "Historical base", "component_type": "BASE"},
    ).json()
    initialized = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [{"component_id": component["id"], "amount": "5000"}],
        },
    )
    assert initialized.status_code == 201, initialized.text
    _reopened_batch_with_prior_result(db_session, employee)

    changed_basis = client.patch(
        f"/api/salary-components/{component['id']}",
        headers=headers,
        json={"taxable": False},
    )

    assert changed_basis.status_code == 409
    assert changed_basis.json()["detail"] == (
        "Calculation metadata is immutable after a component has been used in payroll; "
        "create a new effective-dated component instead"
    )
    renamed = client.patch(
        f"/api/salary-components/{component['id']}",
        headers=headers,
        json={"name": "Historical base label", "sort_order": 10},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Historical base label"
    assert renamed.json()["taxable"] is True


def test_adjustment_creator_can_read_component_catalog_without_salary_structure_access(
    client, db_session
):
    _user(db_session, "store_manager", ["STORE_MANAGER"])
    headers = _token(client, "store_manager")

    response = client.get("/api/salary-components", headers=headers)

    assert response.status_code == 200


def test_allowance_component_requires_a_kind_and_manual_evidence(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")

    missing_kind = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "MEAL", "name": "Meal", "component_type": "ALLOWANCE"},
    )
    assert missing_kind.status_code == 422
    assert (
        client.post(
            "/api/salary-components",
            headers=headers,
            json={
                "code": "BASE_WITH_KIND",
                "name": "Base",
                "component_type": "BASE",
                "allowance_kind": "FIXED",
            },
        ).status_code
        == 422
    )
    allowance = client.post(
        "/api/salary-components",
        headers=headers,
        json={
            "code": "MEAL",
            "name": "Meal",
            "component_type": "ALLOWANCE",
            "allowance_kind": "FIXED",
            "prorate_by_attendance": True,
        },
    ).json()
    assert allowance["prorate_by_attendance"] is True
    reclassified = client.patch(
        f"/api/salary-components/{allowance['id']}",
        headers=headers,
        json={"allowance_kind": "FLOATING"},
    )
    assert reclassified.status_code == 200
    assert reclassified.json()["allowance_kind"] == "FLOATING"

    prorated = client.patch(
        f"/api/salary-components/{allowance['id']}",
        headers=headers,
        json={"prorate_by_attendance": False},
    )
    assert prorated.status_code == 200
    assert prorated.json()["prorate_by_attendance"] is False

    base = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "NON_ALLOWANCE", "name": "Base", "component_type": "BASE"},
    ).json()
    invalid_non_allowance = client.patch(
        f"/api/salary-components/{base['id']}",
        headers=headers,
        json={"prorate_by_attendance": True},
    )
    assert invalid_non_allowance.status_code == 422

    missing_evidence = client.put(
        f"/api/employees/{employee.id}/structure/{allowance['id']}",
        headers=headers,
        json={"amount": "300", "effective_from": "2026-01-01"},
    )
    assert missing_evidence.status_code == 422
    recorded = client.put(
        f"/api/employees/{employee.id}/structure/{allowance['id']}",
        headers=headers,
        json={
            "amount": "300",
            "effective_from": "2026-01-01",
            "correction_reason": "Approved meal allowance policy",
            "attachment_url": "https://files.example.test/policy/meal.pdf",
        },
    )
    assert recorded.status_code == 200, recorded.text


def test_structure_evidence_is_not_exposed_through_audit_only_access(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    seed_rbac(db_session)
    audit_only_role = Role(code="AUDIT_ONLY", name="Audit only", is_global_scope=True)
    db_session.add(audit_only_role)
    db_session.flush()
    audit_permission_id = db_session.scalars(
        select(Permission.id).where(Permission.code == Perm.AUDIT_READ)
    ).one()
    db_session.add(RolePermission(role_id=audit_only_role.id, permission_id=audit_permission_id))
    db_session.flush()
    auditor = _user(db_session, "audit-only", ["AUDIT_ONLY"])
    hr_headers = _token(client, "hr")
    allowance = client.post(
        "/api/salary-components",
        headers=hr_headers,
        json={
            "code": "MEAL",
            "name": "Meal",
            "component_type": "ALLOWANCE",
            "allowance_kind": "FIXED",
        },
    ).json()

    response = client.put(
        f"/api/employees/{employee.id}/structure/{allowance['id']}",
        headers=hr_headers,
        json={
            "amount": "300",
            "effective_from": "2026-01-01",
            "correction_reason": "Sensitive allowance policy reason",
            "attachment_url": "https://files.example.test/policy/secret.pdf",
        },
    )
    assert response.status_code == 200, response.text

    audit_response = client.get(
        "/api/audit-logs",
        headers=_token(client, auditor.username),
        params={"action": "structure.set"},
    )
    assert audit_response.status_code == 200, audit_response.text
    detail = audit_response.json()["items"][0]["detail"]
    assert detail["has_reason"] is True
    assert detail["evidence_attached"] is True
    assert "reason" not in detail
    assert "attachment_url" not in detail
    assert "Sensitive allowance policy reason" not in str(detail)
    assert "secret.pdf" not in str(detail)


def test_set_and_get_structure_with_compa(client, db_session):
    emp = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    comp = client.post(
        "/api/salary-components",
        headers=h,
        json={"code": "BASE", "name": "基本", "component_type": "BASE"},
    ).json()
    r = client.put(
        f"/api/employees/{emp.id}/structure/{comp['id']}",
        headers=h,
        json={"amount": "5000", "effective_from": "2026-01-01"},
    )
    assert r.status_code == 200
    got = client.get(f"/api/employees/{emp.id}/structure?on_date=2026-06-01", headers=h)
    assert got.status_code == 200
    body = got.json()
    assert len(body["items"]) == 1
    assert body["compa"]["total"] == "5000.00"
    assert body["compa"]["band_status"] == "NO_BAND"  # 员工无职级带宽
    history = client.get(f"/api/employees/{emp.id}/structure/history", headers=h)
    assert history.status_code == 200, history.text
    view_actions = set(
        db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.target_type == "employee",
                AuditLog.target_id == emp.id,
                AuditLog.action.in_(("structure.view", "structure.history.view")),
            )
        ).all()
    )
    assert view_actions == {"structure.view", "structure.history.view"}


def test_initial_structure_sets_multiple_components_atomically(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    base = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "BASE", "name": "Base", "component_type": "BASE"},
    ).json()
    position = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "POSITION", "name": "Position", "component_type": "POSITION"},
    ).json()

    response = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [
                {"component_id": base["id"], "amount": "5000"},
                {"component_id": position["id"], "amount": "300"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert {item["component_id"] for item in response.json()} == {base["id"], position["id"]}

    retry = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [{"component_id": base["id"], "amount": "5000"}],
        },
    )
    assert retry.status_code == 409


def test_initial_manual_allowance_retains_controlled_evidence_reference(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    allowance = client.post(
        "/api/salary-components",
        headers=headers,
        json={
            "code": "MEAL",
            "name": "Meal",
            "component_type": "ALLOWANCE",
            "allowance_kind": "FIXED",
        },
    ).json()

    response = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [
                {
                    "component_id": allowance["id"],
                    "amount": "300",
                    "reason": "Approved meal allowance policy",
                    "attachment_url": "https://files.example.test/policy/meal.pdf",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == [
        {
            "component_id": allowance["id"],
            "amount": "300.00",
            "effective_from": "2026-01-01",
            "effective_to": None,
            "source_adjustment_id": None,
            "source_reason": "Approved meal allowance policy",
            "source_attachment_url": "https://files.example.test/policy/meal.pdf",
        }
    ]


@pytest.mark.parametrize(
    ("component_type", "allowance_kind"),
    [
        ("ALLOWANCE", "FIXED"),
        ("ALLOWANCE", "FLOATING"),
        ("HOUSING", None),
    ],
)
def test_initial_manual_allowance_and_housing_require_reason_and_evidence(
    client, db_session, component_type, allowance_kind
):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    component_payload = {
        "code": f"MANUAL_{component_type}_{allowance_kind or 'ROOM'}",
        "name": "Manual payroll component",
        "component_type": component_type,
    }
    if allowance_kind is not None:
        component_payload["allowance_kind"] = allowance_kind
    component = client.post(
        "/api/salary-components", headers=headers, json=component_payload
    ).json()

    missing_reason = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [
                {
                    "component_id": component["id"],
                    "amount": "300",
                    "attachment_url": "https://files.example.test/policy/manual.pdf",
                }
            ],
        },
    )
    missing_evidence = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [
                {
                    "component_id": component["id"],
                    "amount": "300",
                    "reason": "Approved manual payroll policy",
                }
            ],
        },
    )

    assert missing_reason.status_code == 422
    assert missing_evidence.status_code == 422

    recorded = client.put(
        f"/api/employees/{employee.id}/initial-structure",
        headers=headers,
        json={
            "effective_from": "2026-01-01",
            "items": [
                {
                    "component_id": component["id"],
                    "amount": "300",
                    "reason": "Approved manual payroll policy",
                    "attachment_url": "https://files.example.test/policy/manual.pdf",
                }
            ],
        },
    )

    assert recorded.status_code == 201, recorded.text
    assert recorded.json()[0]["source_reason"] == "Approved manual payroll policy"
    assert (
        recorded.json()[0]["source_attachment_url"]
        == "https://files.example.test/policy/manual.pdf"
    )


def test_manual_housing_legacy_setup_requires_reason_and_evidence(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    housing = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "ROOM", "name": "Room", "component_type": "HOUSING"},
    ).json()

    missing_reason = client.put(
        f"/api/employees/{employee.id}/structure/{housing['id']}",
        headers=headers,
        json={
            "amount": "300",
            "effective_from": "2026-01-01",
            "attachment_url": "https://files.example.test/policy/housing.pdf",
        },
    )
    missing_evidence = client.put(
        f"/api/employees/{employee.id}/structure/{housing['id']}",
        headers=headers,
        json={
            "amount": "300",
            "effective_from": "2026-01-01",
            "correction_reason": "Approved housing policy",
        },
    )

    assert missing_reason.status_code == 422
    assert missing_evidence.status_code == 422

    recorded = client.put(
        f"/api/employees/{employee.id}/structure/{housing['id']}",
        headers=headers,
        json={
            "amount": "300",
            "effective_from": "2026-01-01",
            "correction_reason": "Approved housing policy",
            "attachment_url": "https://files.example.test/policy/housing.pdf",
        },
    )

    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["source_reason"] == "Approved housing policy"
    assert (
        recorded.json()["source_attachment_url"] == "https://files.example.test/policy/housing.pdf"
    )


def test_existing_structure_cannot_add_an_unused_component_without_approval(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    base = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "BASE", "name": "Base", "component_type": "BASE"},
    ).json()
    position = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "POSITION", "name": "Position", "component_type": "POSITION"},
    ).json()
    assert (
        client.put(
            f"/api/employees/{employee.id}/structure/{base['id']}",
            headers=headers,
            json={"amount": "5000", "effective_from": "2026-01-01"},
        ).status_code
        == 200
    )

    bypass = client.put(
        f"/api/employees/{employee.id}/structure/{position['id']}",
        headers=headers,
        json={"amount": "300", "effective_from": "2026-01-01"},
    )
    assert bypass.status_code == 409
    assert "extended through a salary adjustment approval" in bypass.json()["detail"]


def test_reopened_structure_correction_is_audited_and_reconciled(client, db_session):
    emp = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    component = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "BASE", "name": "基本", "component_type": "BASE"},
    ).json()
    assert (
        client.put(
            f"/api/employees/{emp.id}/structure/{component['id']}",
            headers=headers,
            json={"amount": "5000", "effective_from": "2026-01-01"},
        ).status_code
        == 200
    )
    batch = _reopened_batch_with_prior_result(db_session, emp)

    missing_reason = client.put(
        f"/api/employees/{emp.id}/structure/{component['id']}",
        headers=headers,
        json={"amount": "5100", "effective_from": "2026-01-01"},
    )
    assert missing_reason.status_code == 422

    changed = client.put(
        f"/api/employees/{emp.id}/structure/{component['id']}",
        headers=headers,
        json={
            "amount": "5100",
            "effective_from": "2026-01-01",
            "correction_reason": "Correct omitted base salary",
            "attachment_url": "https://files.example.test/corrections/base.pdf",
        },
    )
    assert changed.status_code == 200, changed.text
    adjustment = db_session.scalars(select(AdjustmentRecord)).one()
    assert adjustment.item == "SALARY_STRUCTURE_SOURCE"
    assert adjustment.before_value["amount"] == "5000.00"
    assert adjustment.after_value["amount"] == "5100.00"
    assert adjustment.recompute_result == {"status": "PENDING_RERUN", "batch_version": 2}
    assert adjustment.attachment_url == "https://files.example.test/corrections/base.pdf"

    assert run_batch(db_session, batch) == 1
    db_session.refresh(adjustment)
    assert adjustment.recompute_result["status"] == "RECOMPUTED"
    assert adjustment.recompute_result["batch_version"] == 2


def test_existing_structure_change_requires_the_adjustment_approval_flow(client, db_session):
    employee = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    component = client.post(
        "/api/salary-components",
        headers=headers,
        json={"code": "BASE", "name": "基本", "component_type": "BASE"},
    ).json()
    assert (
        client.put(
            f"/api/employees/{employee.id}/structure/{component['id']}",
            headers=headers,
            json={"amount": "5000", "effective_from": "2026-01-01"},
        ).status_code
        == 200
    )
    _reopened_batch_with_prior_result(db_session, employee)

    response = client.put(
        f"/api/employees/{employee.id}/structure/{component['id']}",
        headers=headers,
        json={"amount": "5100", "effective_from": "2026-05-20"},
    )
    assert response.status_code == 409, response.text
    assert db_session.scalars(select(AdjustmentRecord)).all() == []
