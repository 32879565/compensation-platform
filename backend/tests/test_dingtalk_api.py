"""Sandbox DingTalk routing and manager-scoped compensation appeals."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings
from app.core.security import hash_password
from app.dingtalk import service as dingtalk_service
from app.dingtalk.client import DingTalkSendResult
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    AppealCorrectionWorkStatus,
    AppealStatus,
    CompAppealCorrectionWorkItem,
    DingTalkDelivery,
    DingTalkDeliveryKind,
)
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import AdjustmentRecord, BatchConfirmation, PayrollResult

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


def _user(session, username: str, roles: list[str], review_scopes=()) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for code in roles:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    for org_unit_id, department in review_scopes:
        session.add(
            UserReviewScope(
                user_id=user.id,
                org_unit_id=org_unit_id,
                department=department,
            )
        )
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_review_round(session):
    group = OrgUnit(code="DT_GROUP", name="Group", type=OrgType.GROUP)
    store = OrgUnit(
        code="DT_STORE", name="Store", type=OrgType.STORE, parent=group, city="Guangzhou"
    )
    session.add_all([group, store])
    session.flush()
    employee = Employee(
        emp_no="DT-1",
        name="Sensitive Employee Name",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    batch = PayrollBatch(
        period="2026-07",
        attendance_start=date(2026, 6, 26),
        attendance_end=date(2026, 7, 25),
        status=BatchStatus.PENDING_STORE_CONFIRM,
        version=1,
    )
    session.add_all([employee, batch])
    session.flush()
    session.add(
        PayrollResult(
            batch_id=batch.id,
            batch_version=batch.version,
            employee_id=employee.id,
            version=1,
            org_unit_id=store.id,
            department=Department.DINING,
            actual_attendance_days=Decimal("22"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("5678.90"),
            deposit=Decimal("0"),
            net=Decimal("5432.10"),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("0"),
            rule_version="v4",
            input_snapshot={},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    session.add(
        BatchConfirmation(
            batch_id=batch.id,
            batch_version=batch.version,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    session.commit()
    return store, employee, batch


def _appeal_flow(client, headers: dict[str, str]) -> None:
    response = client.post(
        "/api/approval-flows",
        headers=headers,
        json={
            "code": "COMP-APPEAL-DEFAULT",
            "name": "Compensation appeal review",
            "business_type": "COMP_APPEAL",
            "steps": [{"step_order": 1, "name": "Group HR", "role_code": "GROUP_HR"}],
        },
    )
    assert response.status_code == 201, response.text


def test_sandbox_delivery_routes_exact_store_department_and_appeal_is_scoped(client, db_session):
    store, employee, batch = _seed_review_round(db_session)
    hr = _user(db_session, "hr", ["GROUP_HR"])
    manager = _user(
        db_session,
        "dining-manager",
        ["STORE_MANAGER"],
        review_scopes=[(store.id, Department.DINING)],
    )
    outsider = _user(
        db_session,
        "kitchen-manager",
        ["STORE_MANAGER"],
        review_scopes=[(store.id, Department.KITCHEN)],
    )
    hr_headers = _token(client, hr.username)
    manager_headers = _token(client, manager.username)
    _appeal_flow(client, hr_headers)

    reviewer_mode = client.get("/api/dingtalk/mode", headers=manager_headers)
    assert reviewer_mode.status_code == 200
    assert reviewer_mode.json() == {"mode": "sandbox"}

    integration = client.get("/api/dingtalk/integration", headers=hr_headers)
    assert integration.status_code == 200
    assert integration.json() == {
        "mode": "sandbox",
        "credentials_configured": False,
        "app_id_configured": False,
        "public_base_url_configured": False,
        "ready_for_live": False,
        "read_sync_enabled": False,
        "read_sync_ready": False,
    }
    assert client.post("/api/dingtalk/integration/test", headers=hr_headers).status_code == 409

    staged = client.post(f"/api/dingtalk/batches/{batch.id}/review-deliveries", headers=hr_headers)
    assert staged.status_code == 201, staged.text
    assert staged.json() == {
        "routed": 1,
        "configuration_failures": 0,
        "existing": 0,
        "sandbox": True,
    }
    own_deliveries = client.get("/api/dingtalk/deliveries", headers=manager_headers)
    assert own_deliveries.status_code == 200, own_deliveries.text
    assert len(own_deliveries.json()) == 1
    delivery = own_deliveries.json()[0]
    assert delivery["recipient_user_id"] == manager.id
    assert delivery["department"] == "DINING"
    assert delivery["kind"] == "PAYROLL_REVIEW"
    assert delivery["status"] == "SANDBOXED"
    assert delivery["can_appeal"] is True
    assert "gross" not in delivery and "net" not in delivery and "employee_name" not in delivery
    assert (
        client.get("/api/dingtalk/deliveries", headers=_token(client, outsider.username)).json()
        == []
    )
    global_notification_reviewer = _user(
        db_session,
        "global-notification-reviewer",
        ["GROUP_HR", "STORE_MANAGER"],
    )
    all_deliveries = client.get(
        "/api/dingtalk/deliveries",
        headers=_token(client, global_notification_reviewer.username),
    )
    assert all_deliveries.status_code == 200
    assert len(all_deliveries.json()) == 1
    # The capability bit avoids presenting a false affordance to an operator
    # who can administer notifications but did not receive this delivery.
    assert all_deliveries.json()[0]["can_appeal"] is False

    forbidden = client.post(
        "/api/comp-appeals",
        headers=_token(client, outsider.username),
        json={"delivery_id": delivery["id"], "employee_id": employee.id, "reason": "Out of scope"},
    )
    assert forbidden.status_code == 404

    created = client.post(
        "/api/comp-appeals",
        headers=manager_headers,
        json={
            "delivery_id": delivery["id"],
            "employee_id": employee.id,
            "reason": "Please verify the attendance source.",
        },
    )
    assert created.status_code == 201, created.text
    appeal = created.json()
    assert appeal["status"] == "PENDING"
    instance_id = appeal["approval_instance_id"]
    assert instance_id is not None
    assert (
        client.get(f"/api/approval-instances/{instance_id}", headers=manager_headers).status_code
        == 200
    )

    # Appeals preserve the notification's immutable review round even if an
    # HR correction later opens a newer batch round before the decision.
    batch.version = 2
    db_session.commit()

    todos = client.get("/api/approval-instances/todos", headers=hr_headers)
    assert todos.status_code == 200
    assert [(todo["business_type"], todo["business_id"]) for todo in todos.json()] == [
        ("COMP_APPEAL", appeal["id"])
    ]
    decision = client.post(
        f"/api/approval-instances/{instance_id}/decisions",
        headers=hr_headers,
        json={"decision": "APPROVE", "comment": "Correct through the audited payroll workflow."},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "APPROVED"
    resolved = client.get(f"/api/comp-appeals/{appeal['id']}", headers=manager_headers)
    assert resolved.status_code == 200
    assert resolved.json()["status"] == AppealStatus.CORRECTION_REQUIRED.value
    assert resolved.json()["resolution"] == "Correct through the audited payroll workflow."

    # A final approval for an old notification never redirects to the newer
    # payroll round.  It creates an explicit settlement-required task instead
    # of changing attendance, payroll results, or the active batch state.
    work_item = db_session.scalars(
        select(CompAppealCorrectionWorkItem).where(
            CompAppealCorrectionWorkItem.appeal_id == appeal["id"]
        )
    ).one()
    assert work_item.source_batch_version == 1
    assert work_item.status is AppealCorrectionWorkStatus.HISTORICAL_SETTLEMENT_REQUIRED
    assert work_item.employee_id == employee.id
    assert batch.version == 2
    assert batch.status is BatchStatus.PENDING_STORE_CONFIRM
    assert (
        len(
            db_session.scalars(
                select(PayrollResult).where(PayrollResult.batch_id == batch.id)
            ).all()
        )
        == 1
    )
    assert (
        db_session.scalars(
            select(AdjustmentRecord).where(AdjustmentRecord.batch_id == batch.id)
        ).all()
        == []
    )

    correction_queue = client.get("/api/comp-appeal-corrections", headers=hr_headers)
    assert correction_queue.status_code == 200, correction_queue.text
    assert len(correction_queue.json()) == 1
    queue_item = correction_queue.json()[0]
    assert set(queue_item) == {
        "id",
        "appeal_id",
        "batch_id",
        "source_batch_version",
        "org_unit_id",
        "department",
        "status",
        "created_at",
    }
    assert queue_item["id"] == work_item.id
    assert queue_item["appeal_id"] == appeal["id"]
    assert queue_item["batch_id"] == batch.id
    assert queue_item["source_batch_version"] == 1
    assert queue_item["org_unit_id"] == store.id
    assert queue_item["department"] == "DINING"
    assert queue_item["status"] == "HISTORICAL_SETTLEMENT_REQUIRED"
    assert queue_item["created_at"]
    assert client.get("/api/comp-appeal-corrections", headers=manager_headers).status_code == 403

    deliveries = list(
        db_session.scalars(
            select(DingTalkDelivery).where(DingTalkDelivery.recipient_user_id == manager.id)
        ).all()
    )
    assert {row.kind for row in deliveries} == {
        DingTalkDeliveryKind.PAYROLL_REVIEW,
        DingTalkDeliveryKind.APPEAL_STATUS,
    }
    status_delivery = next(
        row for row in deliveries if row.kind is DingTalkDeliveryKind.APPEAL_STATUS
    )
    assert status_delivery.batch_version == 1
    audit_text = "\n".join(
        str(row.detail)
        for row in db_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        if row.detail is not None
    )
    assert "Sensitive Employee Name" not in audit_text
    assert "5432.10" not in audit_text
    assert "Please verify the attendance source." not in audit_text


def test_approved_current_appeal_creates_a_triage_work_item_without_mutating_payroll(
    client, db_session
):
    store, employee, batch = _seed_review_round(db_session)
    hr = _user(db_session, "hr-current", ["GROUP_HR"])
    manager = _user(
        db_session,
        "dining-manager-current",
        ["STORE_MANAGER"],
        review_scopes=[(store.id, Department.DINING)],
    )
    hr_headers = _token(client, hr.username)
    manager_headers = _token(client, manager.username)
    _appeal_flow(client, hr_headers)
    assert (
        client.post(
            f"/api/dingtalk/batches/{batch.id}/review-deliveries", headers=hr_headers
        ).status_code
        == 201
    )
    delivery = client.get("/api/dingtalk/deliveries", headers=manager_headers).json()[0]
    created = client.post(
        "/api/comp-appeals",
        headers=manager_headers,
        json={
            "delivery_id": delivery["id"],
            "employee_id": employee.id,
            "reason": "Please verify a source record.",
        },
    )
    assert created.status_code == 201, created.text
    appeal = created.json()
    result_before = db_session.scalars(
        select(PayrollResult).where(PayrollResult.batch_id == batch.id)
    ).one()

    decision = client.post(
        f"/api/approval-instances/{appeal['approval_instance_id']}/decisions",
        headers=hr_headers,
        json={"decision": "APPROVE", "comment": "Use the controlled correction workflow."},
    )
    assert decision.status_code == 200, decision.text

    task = db_session.scalars(
        select(CompAppealCorrectionWorkItem).where(
            CompAppealCorrectionWorkItem.appeal_id == appeal["id"]
        )
    ).one()
    assert task.status is AppealCorrectionWorkStatus.PENDING_TRIAGE
    assert task.source_batch_version == 1
    assert task.created_by == hr.id
    # Approval is an authorization and handoff only.  It cannot infer an
    # attendance patch from a free-text appeal or overwrite the final amount.
    assert batch.version == 1
    assert batch.status is BatchStatus.PENDING_STORE_CONFIRM
    result_after = db_session.scalars(
        select(PayrollResult).where(PayrollResult.batch_id == batch.id)
    ).one()
    assert result_after.id == result_before.id
    assert result_after.net == Decimal("5432.10")
    assert (
        db_session.scalars(
            select(AdjustmentRecord).where(AdjustmentRecord.batch_id == batch.id)
        ).all()
        == []
    )


def test_sandbox_routing_fails_closed_for_an_unassigned_department_and_retry_is_audited(
    client, db_session
):
    store, _employee, batch = _seed_review_round(db_session)
    hr = _user(db_session, "hr", ["GROUP_HR"])
    manager = _user(
        db_session,
        "dining-manager",
        ["STORE_MANAGER"],
        review_scopes=[(store.id, Department.DINING)],
    )
    db_session.add(
        BatchConfirmation(
            batch_id=batch.id,
            batch_version=batch.version,
            org_unit_id=store.id,
            department=Department.KITCHEN,
        )
    )
    db_session.commit()
    headers = _token(client, hr.username)

    staged = client.post(f"/api/dingtalk/batches/{batch.id}/review-deliveries", headers=headers)
    assert staged.status_code == 201, staged.text
    assert staged.json()["routed"] == 1
    assert staged.json()["configuration_failures"] == 1
    failed = db_session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.recipient_user_id.is_(None))
    ).one()
    assert failed.error_code == "MISSING_ELIGIBLE_RECIPIENT"
    assert failed.status.value == "FAILED"

    own = client.get("/api/dingtalk/deliveries", headers=_token(client, manager.username))
    assert len(own.json()) == 1
    delivery_id = own.json()[0]["id"]
    retried = client.post(f"/api/dingtalk/deliveries/{delivery_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "SANDBOXED"
    assert retried.json()["attempt_count"] == 2
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "dingtalk.delivery.retry")
    ).one()
    assert audit_row.detail == {"sandbox": True, "attempt_count": 2}


class _FakeDingTalkClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def send_action_card(self, **message: str) -> DingTalkSendResult:
        self.messages.append(message)
        return DingTalkSendResult(task_id=987654, request_id="provider-request")


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        dingtalk_mode="live",
        dingtalk_app_id="00000000-0000-0000-0000-000000000001",
        dingtalk_client_id="ding-test-client",
        dingtalk_client_secret="test-dingtalk-secret-value",
        dingtalk_agent_id=123,
        dingtalk_public_base_url="https://payroll.example.test",
    )


def test_live_delivery_requires_provider_userid_and_sends_ephemeral_action_card(db_session):
    store, employee, batch = _seed_review_round(db_session)
    manager = _user(
        db_session,
        "live-dining-manager",
        ["STORE_MANAGER"],
        review_scopes=[(store.id, Department.DINING)],
    )
    settings = _live_settings()

    missing = dingtalk_service.stage_review_deliveries(
        db_session, batch_id=batch.id, settings=settings
    )
    assert missing.configuration_failures == 1
    failed = db_session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.batch_id == batch.id)
    ).one()
    assert failed.status.value == "FAILED"
    assert failed.error_code == "MISSING_DINGTALK_USER_ID"

    # A different immutable batch round gives this configured recipient a new
    # idempotency key without promoting the earlier failed row implicitly.
    manager.dingtalk_user_id = "provider-live-manager"
    batch.version = 2
    db_session.add(
        PayrollResult(
            batch_id=batch.id,
            batch_version=2,
            employee_id=employee.id,
            version=2,
            org_unit_id=store.id,
            department=Department.DINING,
            actual_attendance_days=Decimal("22"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("6000.00"),
            deposit=Decimal("0"),
            net=Decimal("5800.00"),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("0"),
            rule_version="v4",
            input_snapshot={},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    db_session.add(
        BatchConfirmation(
            batch_id=batch.id,
            batch_version=2,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    db_session.flush()
    staged = dingtalk_service.stage_review_deliveries(
        db_session, batch_id=batch.id, settings=settings
    )
    assert staged.routed == 1
    assert len(staged.pending_delivery_ids) == 1

    fake_client = _FakeDingTalkClient()
    sent = dingtalk_service.dispatch_live_delivery(
        db_session,
        delivery_id=staged.pending_delivery_ids[0],
        settings=settings,
        client=fake_client,  # type: ignore[arg-type]
    )
    assert sent.status.value == "SENT"
    assert sent.provider_task_id == 987654
    assert sent.attempt_count == 1
    assert len(fake_client.messages) == 1
    message = fake_client.messages[0]
    assert message["recipient_user_id"] == "provider-live-manager"
    assert "Sensitive Employee Name" in message["markdown"]
    assert "6000.00" in message["markdown"]
    assert message["action_url"].startswith(
        "https://payroll.example.test/comp-appeals?delivery_id="
    )
    assert not hasattr(sent, "message_body")
