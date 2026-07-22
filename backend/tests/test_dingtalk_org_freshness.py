from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings
from app.core.security import hash_password
from app.dingtalk import service as dingtalk_service
from app.dingtalk.org_freshness import (
    DingTalkOrganizationFreshnessError,
    require_recent_organization_scopes,
)
from app.dingtalk.read_sync import (
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
)
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    DingTalkDelivery,
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
from app.models.payroll_result import BatchConfirmation


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        dingtalk_mode="live",
        dingtalk_app_id="00000000-0000-0000-0000-000000000001",
        dingtalk_corp_id="ding-test-corp",
        dingtalk_client_id="ding-test-client",
        dingtalk_client_secret="test-dingtalk-secret-value",
        dingtalk_agent_id=123,
        dingtalk_public_base_url="https://payroll.example.test",
        dingtalk_read_sync_enabled=True,
    )


def _seed_scope(db_session, *, suffix: str):
    seed_rbac(db_session)
    actor = User(
        username=f"freshness-actor-{suffix}",
        password_hash=hash_password("StrongPass123!"),
    )
    store = OrgUnit(
        code=f"FRESH-{suffix}",
        name=f"新鲜度门店{suffix}",
        type=OrgType.STORE,
    )
    db_session.add_all([actor, store])
    db_session.flush()
    store.dingtalk_dept_id = store.id + 100
    employee = Employee(
        emp_no=f"FRESH-EMP-{suffix}",
        name=f"负责人{suffix}",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    manager = User(
        username=f"freshness-manager-{suffix}",
        password_hash=hash_password("StrongPass123!"),
        employee_id=None,
    )
    db_session.add_all([employee, manager])
    db_session.flush()
    manager.employee_id = employee.id
    provider_user_id = f"provider-user-{suffix}"
    provider_hash = blind_index_dingtalk_user_id(
        provider_user_id,
        key=_live_settings().encryption_key,
    )
    employee.dingtalk_user_id_hash = provider_hash
    manager.dingtalk_user_id = provider_user_id
    manager.dingtalk_user_id_hash = provider_hash
    manager_role = db_session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    db_session.add_all(
        [
            UserRole(user_id=manager.id, role_id=manager_role.id),
            UserReviewScope(
                user_id=manager.id,
                org_unit_id=store.id,
                department=Department.DINING,
            ),
        ]
    )
    db_session.flush()
    return actor, store, employee, manager


def _add_applied_sync(
    db_session,
    *,
    actor: User,
    store: OrgUnit,
    employee: Employee,
    now: datetime,
    applied_at: datetime | None = None,
) -> DingTalkOrgSyncBatch:
    batch = DingTalkOrgSyncBatch(
        status=DingTalkOrgSyncBatchStatus.APPLIED,
        snapshot_hash="a" * 64,
        expires_at=now + timedelta(minutes=15),
        requested_by_user_id=actor.id,
        applied_by_user_id=actor.id,
        applied_at=applied_at or now,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add_all(
        [
            DingTalkOrgSyncItem(
                batch_id=batch.id,
                row_key=f"STORE:{store.id}",
                kind=DingTalkOrgSyncItemKind.STORE,
                status=DingTalkOrgSyncItemStatus.APPLIED,
                remote_department_id=store.id + 100,
                remote_department_name=store.name,
                remote_department_path=f"集团 / {store.name}",
                proposed_org_unit_id=store.id,
                match_method="LINK|STABLE_DEPARTMENT_ID",
                baseline_fingerprint="b" * 64,
            ),
            DingTalkOrgSyncItem(
                batch_id=batch.id,
                row_key=f"REVIEWER:{store.id}:DINING",
                kind=DingTalkOrgSyncItemKind.REVIEWER,
                status=DingTalkOrgSyncItemStatus.APPLIED,
                remote_department_id=store.id + 100,
                remote_department_name=store.name,
                remote_department_path=f"集团 / {store.name}",
                proposed_org_unit_id=store.id,
                proposed_employee_id=employee.id,
                department=Department.DINING,
                match_method="ASSIGN|JOB_NUMBER",
                applied_identity_proof=dingtalk_organization_identity_proof(
                    employee.dingtalk_user_id_hash or "",
                    key=_live_settings().encryption_key,
                    tenant_id=_live_settings().dingtalk_corp_id or "",
                    batch_public_id=batch.public_id,
                    snapshot_hash=batch.snapshot_hash,
                    remote_department_id=store.id + 100,
                    org_unit_id=store.id,
                    department=Department.DINING.value,
                    employee_id=employee.id,
                ),
                baseline_fingerprint="c" * 64,
            ),
        ]
    )
    db_session.flush()
    return batch


def _add_payroll_round(db_session, *, store: OrgUnit, suffix: str) -> PayrollBatch:
    batch = PayrollBatch(
        period=f"2025-{int(suffix[-2:]):02d}",
        attendance_start=date(2025, 1, 1),
        attendance_end=date(2025, 1, 31),
        status=BatchStatus.PENDING_STORE_CONFIRM,
        version=1,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        BatchConfirmation(
            batch_id=batch.id,
            batch_version=batch.version,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    db_session.flush()
    return batch


def _replace_reviewer(db_session, *, store: OrgUnit, suffix: str) -> tuple[Employee, User]:
    db_session.execute(
        delete(UserReviewScope).where(
            UserReviewScope.org_unit_id == store.id,
            UserReviewScope.department == Department.DINING,
        )
    )
    employee = Employee(
        emp_no=f"REPLACEMENT-EMP-{suffix}",
        name=f"调任负责人{suffix}",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    manager = User(
        username=f"replacement-manager-{suffix}",
        password_hash=hash_password("StrongPass123!"),
    )
    db_session.add_all([employee, manager])
    db_session.flush()
    manager.employee_id = employee.id
    provider_user_id = f"replacement-provider-{suffix}"
    provider_hash = blind_index_dingtalk_user_id(
        provider_user_id,
        key=_live_settings().encryption_key,
    )
    employee.dingtalk_user_id_hash = provider_hash
    manager.dingtalk_user_id = provider_user_id
    manager.dingtalk_user_id_hash = provider_hash
    manager_role = db_session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    db_session.add_all(
        [
            UserRole(user_id=manager.id, role_id=manager_role.id),
            UserReviewScope(
                user_id=manager.id,
                org_unit_id=store.id,
                department=Department.DINING,
            ),
        ]
    )
    db_session.flush()
    return employee, manager


def test_scope_freshness_binds_latest_sync_to_current_reviewer(db_session):
    now = datetime.now(UTC)
    actor, store, employee, _manager = _seed_scope(db_session, suffix="01")
    batch = _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
    )

    assert (
        require_recent_organization_scopes(
            db_session,
            {(store.id, Department.DINING)},
            encryption_key=_live_settings().encryption_key,
            tenant_id=_live_settings().dingtalk_corp_id or "",
            now=now,
        )
        == batch.id
    )
    with pytest.raises(DingTalkOrganizationFreshnessError):
        require_recent_organization_scopes(
            db_session,
            {(store.id, Department.KITCHEN)},
            encryption_key=_live_settings().encryption_key,
            tenant_id=_live_settings().dingtalk_corp_id or "",
            now=now,
        )

    _replace_reviewer(db_session, store=store, suffix="01")
    with pytest.raises(DingTalkOrganizationFreshnessError):
        require_recent_organization_scopes(
            db_session,
            {(store.id, Department.DINING)},
            encryption_key=_live_settings().encryption_key,
            tenant_id=_live_settings().dingtalk_corp_id or "",
            now=now,
        )


def test_scope_freshness_rejects_expired_batch(db_session):
    now = datetime.now(UTC)
    actor, store, employee, _manager = _seed_scope(db_session, suffix="02")
    _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
        applied_at=now - timedelta(minutes=6),
    )

    with pytest.raises(DingTalkOrganizationFreshnessError):
        require_recent_organization_scopes(
            db_session,
            {(store.id, Department.DINING)},
            encryption_key=_live_settings().encryption_key,
            tenant_id=_live_settings().dingtalk_corp_id or "",
            now=now,
        )


def test_scope_freshness_rejects_same_employee_rebound_to_new_identity(db_session):
    now = datetime.now(UTC)
    actor, store, employee, manager = _seed_scope(db_session, suffix="07")
    _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
    )
    replacement_provider_id = "unconfirmed-provider-user-07"
    replacement_hash = blind_index_dingtalk_user_id(
        replacement_provider_id,
        key=_live_settings().encryption_key,
    )
    manager.dingtalk_user_id = replacement_provider_id
    manager.dingtalk_user_id_hash = replacement_hash
    employee.dingtalk_user_id_hash = replacement_hash
    db_session.flush()

    with pytest.raises(DingTalkOrganizationFreshnessError):
        require_recent_organization_scopes(
            db_session,
            {(store.id, Department.DINING)},
            encryption_key=_live_settings().encryption_key,
            tenant_id=_live_settings().dingtalk_corp_id or "",
            now=now,
        )


def test_live_staging_requires_sync_before_writing_delivery(db_session):
    _actor, store, _employee, _manager = _seed_scope(db_session, suffix="03")
    payroll_batch = _add_payroll_round(db_session, store=store, suffix="03")

    with pytest.raises(dingtalk_service.DingTalkError):
        dingtalk_service.stage_review_deliveries(
            db_session,
            batch_id=payroll_batch.id,
            settings=_live_settings(),
        )

    assert db_session.scalars(select(DingTalkDelivery)).all() == []


def test_live_dispatch_rechecks_reviewer_after_staging(db_session):
    now = datetime.now(UTC)
    actor, store, employee, _manager = _seed_scope(db_session, suffix="04")
    _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
    )
    payroll_batch = _add_payroll_round(db_session, store=store, suffix="04")
    staged = dingtalk_service.stage_review_deliveries(
        db_session,
        batch_id=payroll_batch.id,
        settings=_live_settings(),
    )
    assert len(staged.pending_delivery_ids) == 1
    delivery_id = staged.pending_delivery_ids[0]
    db_session.commit()

    _replace_reviewer(db_session, store=store, suffix="04")
    db_session.commit()

    class ClientMustNotBeCalled:
        def send_action_card(self, **_kwargs):
            raise AssertionError("provider must not be called with a stale reviewer")

    delivery = dingtalk_service.dispatch_live_delivery(
        db_session,
        delivery_id=delivery_id,
        settings=_live_settings(),
        client=ClientMustNotBeCalled(),  # type: ignore[arg-type]
    )

    assert delivery.status == DingTalkDeliveryStatus.FAILED
    assert delivery.error_code == "ORGANIZATION_SYNC_STALE"
    assert delivery.attempt_count == 0


def test_live_dispatch_cancels_pre_send_marker_if_freshness_changes(db_session, monkeypatch):
    now = datetime.now(UTC)
    actor, store, employee, _manager = _seed_scope(db_session, suffix="05")
    _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
    )
    payroll_batch = _add_payroll_round(db_session, store=store, suffix="05")
    staged = dingtalk_service.stage_review_deliveries(
        db_session,
        batch_id=payroll_batch.id,
        settings=_live_settings(),
    )
    delivery_id = staged.pending_delivery_ids[0]
    db_session.commit()

    calls = 0

    def freshness_changes(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        raise DingTalkOrganizationFreshnessError("organization changed")

    monkeypatch.setattr(
        dingtalk_service,
        "require_recent_organization_scopes",
        freshness_changes,
    )
    monkeypatch.setattr(
        dingtalk_service,
        "_review_action_card",
        lambda *_args, **_kwargs: ("title", "body", "https://payroll.example.test/review"),
    )

    class ClientMustNotBeCalled:
        def send_action_card(self, **_kwargs):
            raise AssertionError("provider must not be called after freshness changes")

    delivery = dingtalk_service.dispatch_live_delivery(
        db_session,
        delivery_id=delivery_id,
        settings=_live_settings(),
        client=ClientMustNotBeCalled(),  # type: ignore[arg-type]
    )

    assert calls == 2
    assert delivery.status == DingTalkDeliveryStatus.FAILED
    assert delivery.error_code == "ORGANIZATION_SYNC_STALE"
    assert delivery.attempt_count == 0
    assert delivery.dispatched_at is None


def test_live_staging_allows_sent_idempotent_noop_after_sync_window(db_session):
    now = datetime.now(UTC)
    actor, store, employee, _manager = _seed_scope(db_session, suffix="06")
    sync_batch = _add_applied_sync(
        db_session,
        actor=actor,
        store=store,
        employee=employee,
        now=now,
    )
    payroll_batch = _add_payroll_round(db_session, store=store, suffix="06")
    staged = dingtalk_service.stage_review_deliveries(
        db_session,
        batch_id=payroll_batch.id,
        settings=_live_settings(),
    )
    delivery_id = staged.pending_delivery_ids[0]
    delivery = db_session.get(DingTalkDelivery, delivery_id)
    assert delivery is not None
    delivery.status = DingTalkDeliveryStatus.SENT
    delivery.attempt_count = 1
    delivery.dispatched_at = now
    delivery.provider_task_id = 12345
    sync_batch.applied_at = now - timedelta(minutes=10)
    db_session.commit()

    repeated = dingtalk_service.stage_review_deliveries(
        db_session,
        batch_id=payroll_batch.id,
        settings=_live_settings(),
    )

    assert repeated.routed == 0
    assert repeated.existing == 1
    assert repeated.pending_delivery_ids == ()
