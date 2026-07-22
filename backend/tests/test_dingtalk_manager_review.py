"""DingTalk-only manager review of employee-level payroll details."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import create_access_token, hash_password
from app.core.config import get_settings
from app.dingtalk.client import (
    DingTalkClient,
    DingTalkClientError,
    DingTalkOrganizationAccess,
    DingTalkOrganizationUser,
)
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
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncBatchStatus,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
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
from app.dingtalk.read_sync import (
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
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


class _LiveLoginClient(_LoginClient):
    def __init__(self, access: DingTalkOrganizationAccess):
        super().__init__(access.user.user_id)
        self.access = access
        self.fail_access = False

    def get_organization_access(self, user_id: str) -> DingTalkOrganizationAccess:
        if self.fail_access:
            raise DingTalkClientError("provider unavailable")
        assert user_id == self.access.user.user_id
        return self.access


def _make_live_review_world(session, world, settings):
    manager = world["manager"]
    employee = world["dining_employee"]
    store = world["store"]
    delivery = world["delivery"]
    provider_hash = blind_index_dingtalk_user_id(
        "ding-manager-1",
        key=settings.encryption_key,
    )
    manager.employee_id = employee.id
    manager.dingtalk_user_id_hash = provider_hash
    employee.dingtalk_user_id_hash = provider_hash
    store.dingtalk_dept_id = 700
    delivery.status = DingTalkDeliveryStatus.SENT
    delivery.dispatched_at = datetime.now(UTC)
    sync_batch = DingTalkOrgSyncBatch(
        status=DingTalkOrgSyncBatchStatus.APPLIED,
        snapshot_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        requested_by_user_id=manager.id,
        applied_by_user_id=manager.id,
        applied_at=datetime.now(UTC),
    )
    session.add(sync_batch)
    session.flush()
    session.add_all(
        [
            DingTalkOrgSyncItem(
                batch_id=sync_batch.id,
                row_key="STORE:700",
                kind=DingTalkOrgSyncItemKind.STORE,
                status=DingTalkOrgSyncItemStatus.APPLIED,
                remote_department_id=700,
                remote_department_name=store.name,
                remote_department_path=f"Group / {store.name}",
                proposed_org_unit_id=store.id,
                match_method="LINK|STABLE_DEPARTMENT_ID",
                baseline_fingerprint="b" * 64,
            ),
            DingTalkOrgSyncItem(
                batch_id=sync_batch.id,
                row_key="REVIEWER:700:DINING",
                kind=DingTalkOrgSyncItemKind.REVIEWER,
                status=DingTalkOrgSyncItemStatus.APPLIED,
                remote_department_id=700,
                remote_department_name=store.name,
                remote_department_path=f"Group / {store.name}",
                proposed_org_unit_id=store.id,
                proposed_employee_id=employee.id,
                department=Department.DINING,
                match_method="ASSIGN|STABLE_ID",
                applied_identity_proof=dingtalk_organization_identity_proof(
                    provider_hash,
                    key=settings.encryption_key,
                    tenant_id=settings.dingtalk_corp_id or "",
                    batch_public_id=sync_batch.public_id,
                    snapshot_hash=sync_batch.snapshot_hash,
                    remote_department_id=700,
                    org_unit_id=store.id,
                    department=Department.DINING.value,
                    employee_id=employee.id,
                ),
                baseline_fingerprint="c" * 64,
            ),
        ]
    )
    session.commit()
    return _LiveLoginClient(
        DingTalkOrganizationAccess(
            user=DingTalkOrganizationUser(
                user_id="ding-manager-1",
                name="Manager",
                job_number="MR-001",
                title="店长",
                active=True,
                department_ids=(701,),
            ),
            parent_department_paths=((701, 700, 1),),
        )
    )


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
    assert [row["employee_id"] for row in payload["employees"]] == [world["dining_employee"].id]
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


def test_manager_can_confirm_only_the_delivery_store_department(client, db_session, monkeypatch):
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


def _install_live_manager_review(monkeypatch, live_client):
    from app.routers import manager_review

    settings = get_settings().model_copy(
        update={
            "dingtalk_corp_id": "ding-test-corp",
            "dingtalk_dining_manager_titles": "店长",
            "dingtalk_kitchen_manager_titles": "厨房经理",
            "dingtalk_review_link_ttl_hours": 168,
        }
    )
    monkeypatch.setattr(manager_review, "get_settings", lambda: settings)
    monkeypatch.setattr(manager_review, "get_dingtalk_client", lambda: live_client)
    return settings


def test_live_manager_access_is_rechecked_after_token_and_rejects_unsynced_transfer(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    settings = get_settings().model_copy(update={"dingtalk_corp_id": "ding-test-corp"})
    live_client = _make_live_review_world(db_session, world, settings)
    _install_live_manager_review(monkeypatch, live_client)
    session_response = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )
    assert session_response.status_code == 200, session_response.text
    headers = {"Authorization": f"Bearer {session_response.json()['access_token']}"}
    review_url = f"/api/manager-review/reviews/{world['delivery'].review_public_id}"
    assert client.get(review_url, headers=headers).status_code == 200

    live_client.access = replace(
        live_client.access,
        user=replace(live_client.access.user, department_ids=(801,)),
        parent_department_paths=((801, 800, 1),),
    )

    rejected = client.get(review_url, headers=headers)
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "Unable to authorize this payroll review."}


def test_live_manager_access_rejects_ambiguous_multi_store_membership(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    settings = get_settings().model_copy(update={"dingtalk_corp_id": "ding-test-corp"})
    live_client = _make_live_review_world(db_session, world, settings)
    other_store = OrgUnit(
        code="MR-OTHER-STORE",
        name="Other Store",
        type=OrgType.STORE,
        dingtalk_dept_id=800,
        status="ACTIVE",
    )
    db_session.add(other_store)
    db_session.commit()
    live_client.access = replace(
        live_client.access,
        user=replace(live_client.access.user, department_ids=(701, 801)),
        parent_department_paths=((701, 700, 1), (801, 800, 1)),
    )
    _install_live_manager_review(monkeypatch, live_client)

    response = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )

    assert response.status_code == 401


def test_live_manager_link_expires_and_provider_failures_fail_closed(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    settings = get_settings().model_copy(update={"dingtalk_corp_id": "ding-test-corp"})
    live_client = _make_live_review_world(db_session, world, settings)
    configured = _install_live_manager_review(monkeypatch, live_client)
    world["delivery"].dispatched_at = datetime.now(UTC) - timedelta(
        hours=configured.dingtalk_review_link_ttl_hours + 1
    )
    db_session.commit()

    expired = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )
    assert expired.status_code == 401

    world["delivery"].dispatched_at = datetime.now(UTC)
    db_session.commit()
    live_client.fail_access = True
    unavailable = client.post(
        "/api/manager-review/session",
        json={
            "review_id": world["delivery"].review_public_id,
            "auth_code": "one-time-code",
        },
    )
    assert unavailable.status_code == 401


def test_manager_session_throttles_repeated_failed_provider_identities(
    client, db_session, monkeypatch
):
    world = _review_world(db_session)
    from app.routers import manager_review

    monkeypatch.setattr(
        manager_review,
        "get_dingtalk_client",
        lambda: _LoginClient("different-user"),
    )
    body = {
        "review_id": world["delivery"].review_public_id,
        "auth_code": "one-time-code",
    }

    for _attempt in range(get_settings().dingtalk_review_session_max_attempts):
        assert client.post("/api/manager-review/session", json=body).status_code == 401

    throttled = client.post("/api/manager-review/session", json=body)
    assert throttled.status_code == 429
    assert "retry-after" in throttled.headers
