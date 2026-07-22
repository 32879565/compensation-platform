"""DingTalk-only manager review of employee-level payroll details."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import create_access_token, hash_password
from app.dingtalk.client import DingTalkClient
from app.dingtalk.manager_security import (
    ManagerReviewTokenError,
    create_manager_review_token,
    decode_manager_review_token,
)
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    DingTalkDelivery,
    DingTalkDeliveryKind,
    DingTalkDeliveryStatus,
)
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import (
    BatchConfirmation,
    CompDispute,
    ConfirmStatus,
    PayrollResult,
)

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


def _review_world(session):
    seed_rbac(session)
    group = OrgUnit(code="MR-GROUP", name="Group", type=OrgType.GROUP)
    store = OrgUnit(
        code="MR-STORE", name="Review Store", type=OrgType.STORE, parent=group, city="Guangzhou"
    )
    session.add_all([group, store])
    session.flush()

    manager = User(
        username="dingtalk-only-manager",
        password_hash=hash_password("StrongPass123!"),
        dingtalk_user_id="ding-manager-1",
        login_enabled=False,
    )
    role = session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    session.add(manager)
    session.flush()
    session.add_all(
        [
            UserRole(user_id=manager.id, role_id=role.id),
            UserReviewScope(
                user_id=manager.id,
                org_unit_id=store.id,
                department=Department.DINING,
            ),
        ]
    )

    dining_employee = Employee(
        emp_no="MR-001",
        name="Dining Employee",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    kitchen_employee = Employee(
        emp_no="MR-002",
        name="Kitchen Employee",
        org_unit_id=store.id,
        department=Department.KITCHEN,
    )
    batch = PayrollBatch(
        period="2026-07",
        attendance_start=date(2026, 6, 26),
        attendance_end=date(2026, 7, 25),
        status=BatchStatus.PENDING_STORE_CONFIRM,
        version=1,
    )
    session.add_all([dining_employee, kitchen_employee, batch])
    session.flush()

    def result(employee: Employee, department: Department, amount: str) -> PayrollResult:
        return PayrollResult(
            batch_id=batch.id,
            batch_version=batch.version,
            employee_id=employee.id,
            version=1,
            org_unit_id=store.id,
            department=department,
            emp_no_snapshot=employee.emp_no,
            employee_name_snapshot=employee.name,
            actual_attendance_days=Decimal("22"),
            statutory_holiday_days=Decimal("1"),
            statutory_holiday_worked_days=Decimal("1"),
            gross=Decimal(amount),
            deposit=Decimal("0"),
            net=Decimal(amount),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("0"),
            rule_version="v4",
            input_snapshot={},
            lines=[
                {"code": "ATTEND_WAGE", "name": "Attendance wage", "amount": amount},
                {"code": "HOUSING", "name": "Housing allowance", "amount": "500.00"},
            ],
            exceptions=[],
            warnings=[],
            has_error=False,
        )

    dining_confirmation = BatchConfirmation(
        batch_id=batch.id,
        batch_version=batch.version,
        org_unit_id=store.id,
        department=Department.DINING,
    )
    kitchen_confirmation = BatchConfirmation(
        batch_id=batch.id,
        batch_version=batch.version,
        org_unit_id=store.id,
        department=Department.KITCHEN,
    )
    delivery = DingTalkDelivery(
        batch_id=batch.id,
        batch_version=batch.version,
        org_unit_id=store.id,
        period_snapshot=batch.period,
        org_unit_name_snapshot=store.name,
        department=Department.DINING,
        recipient_user_id=manager.id,
        kind=DingTalkDeliveryKind.PAYROLL_REVIEW,
        status=DingTalkDeliveryStatus.SANDBOXED,
        attempt_count=1,
        idempotency_key="manager-review-test",
    )
    session.add_all(
        [
            result(dining_employee, Department.DINING, "6000.00"),
            result(kitchen_employee, Department.KITCHEN, "7000.00"),
            dining_confirmation,
            kitchen_confirmation,
            delivery,
        ]
    )
    session.commit()
    return {
        "manager": manager,
        "store": store,
        "batch": batch,
        "dining_employee": dining_employee,
        "kitchen_employee": kitchen_employee,
        "dining_confirmation": dining_confirmation,
        "kitchen_confirmation": kitchen_confirmation,
        "delivery": delivery,
    }


class _LoginClient:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def resolve_login_code(self, auth_code: str):
        assert auth_code == "one-time-code"
        return type("Identity", (), {"user_id": self.user_id})()


def _exchange(client, monkeypatch, world, *, provider_user_id: str = "ding-manager-1") -> str:
    from app.routers import manager_review

    monkeypatch.setattr(
        manager_review,
        "get_dingtalk_client",
        lambda: _LoginClient(provider_user_id),
    )
    response = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["access_token"]


def test_manager_review_token_is_not_an_admin_token_and_is_delivery_bound():
    token = create_manager_review_token(user_id=7, delivery_id=11, batch_version=3)
    claims = decode_manager_review_token(token)
    assert (claims.user_id, claims.delivery_id, claims.batch_version) == (7, 11, 3)

    with pytest.raises(ManagerReviewTokenError):
        decode_manager_review_token(create_access_token(7))
    with pytest.raises(ManagerReviewTokenError):
        decode_manager_review_token(token, expected_delivery_id=12)


def test_login_code_is_sent_in_post_body_and_never_in_provider_url(monkeypatch):
    client = DingTalkClient(
        client_id="client-id",
        client_secret="client-secret-value",
        agent_id=123,
    )
    requests = []
    monkeypatch.setattr(client, "access_token", lambda **_kwargs: ("app-token", 3000))

    def perform(request):
        requests.append(request)
        return {"errcode": 0, "result": {"userid": "ding-user-1"}}

    monkeypatch.setattr(client, "_perform", perform)
    identity = client.resolve_login_code("one-time-auth-code")

    assert identity.user_id == "ding-user-1"
    assert "one-time-auth-code" not in requests[0].full_url
    assert json.loads(requests[0].data.decode("utf-8")) == {"code": "one-time-auth-code"}


def test_dingtalk_only_manager_sees_exact_employee_scope_and_can_raise_item_dispute(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)

    # The manager is not allowed into the HR/admin application.
    login = client.post(
        "/api/auth/login",
        json={"username": world["manager"].username, "password": "StrongPass123!"},
    )
    assert login.status_code == 401

    token = _exchange(client, monkeypatch, world)
    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(
        f"/api/manager-review/reviews/{world['delivery'].review_public_id}",
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.headers["cache-control"] == "no-store"
    payload = detail.json()
    assert payload["store_name"] == "Review Store"
    assert payload["department"] == "DINING"
    assert [row["employee_id"] for row in payload["employees"]] == [
        world["dining_employee"].id
    ]
    assert payload["employees"][0]["employee_name"] == "Dining Employee"
    assert payload["employees"][0]["lines"] == [
        {"code": "ATTEND_WAGE", "name": "Attendance wage", "amount": "6000.00"},
        {"code": "HOUSING", "name": "Housing allowance", "amount": "500.00"},
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Kitchen Employee" not in serialized
    assert "id_card" not in serialized and "bank_account" not in serialized

    created = client.post(
        f"/api/manager-review/reviews/{world['delivery'].review_public_id}/disputes",
        headers=headers,
        json={
            "employee_id": world["dining_employee"].id,
            "salary_item": "ATTEND_WAGE",
            "opinion": "Attendance days should be checked.",
        },
    )
    assert created.status_code == 201, created.text
    dispute = db_session.scalars(select(CompDispute)).one()
    assert dispute.raised_by == world["manager"].id
    assert dispute.employee_id == world["dining_employee"].id
    db_session.refresh(world["dining_confirmation"])
    db_session.refresh(world["batch"])
    assert world["dining_confirmation"].status is ConfirmStatus.DISPUTED
    assert world["batch"].status is BatchStatus.HAS_DISPUTE


def test_manager_review_rejects_wrong_dingtalk_identity_without_disclosing_assignment(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    from app.routers import manager_review

    monkeypatch.setattr(
        manager_review,
        "get_dingtalk_client",
        lambda: _LoginClient("different-user"),
    )
    response = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unable to authorize this payroll review."}


def test_manager_can_confirm_only_the_delivery_store_department(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    token = _exchange(client, monkeypatch, world)
    response = client.post(
        f"/api/manager-review/reviews/{world['delivery'].review_public_id}/confirm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(world["dining_confirmation"])
    db_session.refresh(world["kitchen_confirmation"])
    assert world["dining_confirmation"].status is ConfirmStatus.CONFIRMED
    assert world["kitchen_confirmation"].status is ConfirmStatus.PENDING
