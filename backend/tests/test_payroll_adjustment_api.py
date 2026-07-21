from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Permission, Role, RolePermission, User, UserOrgScope, UserRole
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_adjustment import (
    MonthlyPayrollAdjustment,
    MonthlyPayrollAdjustmentRevision,
    PayrollAdjustmentType,
)
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import AdjustmentRecord, PayrollResult
from app.payroll.batch_service import _input_snapshot
from app.payroll.service import build_input, build_inputs

pytestmark = pytest.mark.usefixtures("pg_engine")


def _user(
    session,
    username: str,
    roles: list[str],
    *,
    org_scope_id: int | None = None,
) -> User:
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


def _scoped_payroll_correct_user(session, username: str, org_scope_id: int) -> User:
    seed_rbac(session)
    role = Role(
        code=f"PCR_{username.upper()}",
        name=f"Scoped payroll correction {username}",
        is_global_scope=False,
    )
    session.add(role)
    session.flush()
    permission = session.scalars(
        select(Permission).where(Permission.code == "payroll:correct")
    ).one()
    session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    session.flush()
    return _user(session, username, [role.code], org_scope_id=org_scope_id)


def _world(session) -> dict[str, object]:
    group = OrgUnit(code="G_ADJ", name="Group", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    region_a = OrgUnit(code="R_ADJ_A", name="Region A", type=OrgType.REGION, parent_id=group.id)
    region_b = OrgUnit(code="R_ADJ_B", name="Region B", type=OrgType.REGION, parent_id=group.id)
    session.add_all([region_a, region_b])
    session.flush()
    store_a = OrgUnit(code="S_ADJ_A", name="Store A", type=OrgType.STORE, parent_id=region_a.id)
    store_b = OrgUnit(code="S_ADJ_B", name="Store B", type=OrgType.STORE, parent_id=region_b.id)
    session.add_all([store_a, store_b])
    session.flush()
    employee_a = Employee(
        emp_no="E_ADJ_A",
        name="Employee A",
        org_unit_id=store_a.id,
        hire_date=date(2025, 1, 1),
    )
    employee_b = Employee(
        emp_no="E_ADJ_B",
        name="Employee B",
        org_unit_id=store_b.id,
        hire_date=date(2025, 1, 1),
    )
    session.add_all([employee_a, employee_b])
    session.flush()
    return {
        "region_a": region_a,
        "store_a": store_a,
        "store_b": store_b,
        "employee_a": employee_a,
        "employee_b": employee_b,
    }


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
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _put_adjustment(
    client,
    headers: dict[str, str],
    employee_id: int,
    adjustment_type: str,
    *,
    amount: str = "125.50",
    reason: str = "Approved prior-period correction",
    attachment_url: str = "https://files.example/evidence.pdf",
    taxable: bool = False,
    in_social_base: bool = False,
    in_housing_base: bool = False,
):
    return client.put(
        f"/api/payroll-adjustments/{employee_id}/2026-05/{adjustment_type}",
        headers=headers,
        json={
            "amount": amount,
            "reason": reason,
            "attachment_url": attachment_url,
            "taxable": taxable,
            "in_social_base": in_social_base,
            "in_housing_base": in_housing_base,
        },
    )


def test_upsert_preserves_natural_row_and_immutable_revision_provenance(client, db_session):
    world = _world(db_session)
    creator = _user(db_session, "adjustment-creator", ["GROUP_HR"])
    updater = _user(db_session, "adjustment-updater", ["GROUP_HR"])
    creator_headers = _token(client, creator.username)

    created = _put_adjustment(
        client,
        creator_headers,
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
    )

    assert created.status_code == 200, created.text
    first = created.json()
    assert first["employee_id"] == world["employee_a"].id
    assert first["period"] == "2026-05"
    assert first["adjustment_type"] == "PREV_MAKEUP"
    assert first["amount"] == "125.50"
    assert first["taxable"] is False
    assert first["in_social_base"] is False
    assert first["in_housing_base"] is False
    assert first["created_by"] == creator.id
    assert first["updated_by"] == creator.id
    assert first["created_at"]

    updated = _put_adjustment(
        client,
        _token(client, updater.username),
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
        amount="175.25",
        reason="Corrected approved amount",
        attachment_url="https://files.example/revised.pdf",
    )

    assert updated.status_code == 200, updated.text
    second = updated.json()
    assert second["id"] == first["id"]
    assert second["amount"] == "175.25"
    assert second["created_by"] == first["created_by"]
    assert second["updated_by"] == updater.id
    assert second["created_at"] == first["created_at"]
    rows = list(
        db_session.scalars(
            select(MonthlyPayrollAdjustment).where(
                MonthlyPayrollAdjustment.employee_id == world["employee_a"].id,
                MonthlyPayrollAdjustment.period == "2026-05",
                MonthlyPayrollAdjustment.adjustment_type == PayrollAdjustmentType.PREV_MAKEUP,
            )
        ).all()
    )
    assert len(rows) == 1
    history = client.get(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_MAKEUP/history",
        headers=creator_headers,
    )
    assert history.status_code == 200, history.text
    revisions = history.json()
    assert [revision["revision"] for revision in revisions] == [1, 2]
    assert [revision["attachment_url"] for revision in revisions] == [
        "https://files.example/evidence.pdf",
        "https://files.example/revised.pdf",
    ]
    assert [revision["changed_by"] for revision in revisions] == [creator.id, updater.id]
    assert [revision["amount"] for revision in revisions] == ["125.50", "175.25"]
    listed = client.get(
        "/api/payroll-adjustments?period=2026-05",
        headers=creator_headers,
    )
    assert listed.status_code == 200, listed.text
    view_actions = set(
        db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.actor_user_id == creator.id,
                AuditLog.action.in_(("payroll_adjustment.view", "payroll_adjustment.history.view")),
            )
        ).all()
    )
    assert view_actions == {
        "payroll_adjustment.view",
        "payroll_adjustment.history.view",
    }
    stored_revisions = list(
        db_session.scalars(
            select(MonthlyPayrollAdjustmentRevision)
            .where(MonthlyPayrollAdjustmentRevision.adjustment_id == first["id"])
            .order_by(MonthlyPayrollAdjustmentRevision.revision)
        ).all()
    )
    assert len(stored_revisions) == 2


def test_adjustment_history_uses_revision_org_snapshot_after_employee_transfer(client, db_session):
    world = _world(db_session)
    creator = _user(db_session, "history-global-hr", ["GROUP_HR"])
    created = _put_adjustment(
        client,
        _token(client, creator.username),
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_DEDUCT.value,
    )
    assert created.status_code == 200, created.text
    old_region_reader = _scoped_payroll_correct_user(
        db_session,
        "history-old-region",
        world["region_a"].id,
    )
    new_region_reader = _scoped_payroll_correct_user(
        db_session,
        "history-new-region",
        world["store_b"].id,
    )
    world["employee_a"].org_unit_id = world["store_b"].id
    db_session.flush()

    visible = client.get(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_DEDUCT/history",
        headers=_token(client, old_region_reader.username),
    )
    hidden = client.get(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_DEDUCT/history",
        headers=_token(client, new_region_reader.username),
    )

    assert visible.status_code == 200, visible.text
    assert len(visible.json()) == 1
    assert visible.json()[0]["org_unit_id"] == world["store_a"].id
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == []


def test_revision_collision_returns_conflict_without_overwriting_history(
    client,
    db_session,
    monkeypatch,
):
    world = _world(db_session)
    creator = _user(db_session, "collision-creator", ["GROUP_HR"])
    updater = _user(db_session, "collision-updater", ["GROUP_HR"])
    created = _put_adjustment(
        client,
        _token(client, creator.username),
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
        attachment_url="https://files.example/original-collision.pdf",
    )
    assert created.status_code == 200, created.text
    updater_headers = _token(client, updater.username)
    original_flush = db_session.flush

    def collide_when_revision_is_pending(objects=None):
        if any(
            isinstance(candidate, MonthlyPayrollAdjustmentRevision) for candidate in db_session.new
        ):
            raise IntegrityError("insert revision", {}, Exception("unique violation"))
        return original_flush(objects)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(db_session, "flush", collide_when_revision_is_pending)
        response = _put_adjustment(
            client,
            updater_headers,
            world["employee_a"].id,
            PayrollAdjustmentType.PREV_MAKEUP.value,
            amount="200.00",
            attachment_url="https://files.example/colliding.pdf",
        )

    assert response.status_code == 409, response.text
    history = client.get(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_MAKEUP/history",
        headers=updater_headers,
    )
    assert history.status_code == 200, history.text
    assert [entry["attachment_url"] for entry in history.json()] == [
        "https://files.example/original-collision.pdf"
    ]


def test_monthly_adjustment_source_requires_payroll_correction_permission(client, db_session):
    world = _world(db_session)
    manager = _user(
        db_session,
        "regional-adjustments",
        ["REGION_MANAGER"],
        org_scope_id=world["region_a"].id,
    )
    db_session.add(
        MonthlyPayrollAdjustment(
            employee_id=world["employee_b"].id,
            org_unit_id=world["store_b"].id,
            period="2026-05",
            adjustment_type=PayrollAdjustmentType.PREV_DEDUCT,
            amount=Decimal("90"),
            reason="Other region correction",
            attachment_url="https://files.example/other.pdf",
            created_by=manager.id,
            updated_by=manager.id,
        )
    )
    db_session.flush()
    headers = _token(client, manager.username)
    own = _put_adjustment(
        client,
        headers,
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
    )
    listed = client.get(
        "/api/payroll-adjustments?period=2026-05",
        headers=headers,
    )

    assert own.status_code == 403
    assert listed.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {
            "amount": "0",
            "reason": "Approved correction",
            "attachment_url": "https://files.example/evidence.pdf",
        },
        {
            "amount": "-1",
            "reason": "Approved correction",
            "attachment_url": "https://files.example/evidence.pdf",
        },
        {
            "amount": "1",
            "reason": "   ",
            "attachment_url": "https://files.example/evidence.pdf",
        },
        {"amount": "1", "reason": "Approved correction", "attachment_url": "   "},
    ],
    ids=["zero", "negative", "blank-reason", "blank-evidence"],
)
def test_adjustment_upsert_requires_positive_amount_reason_and_evidence(client, db_session, body):
    world = _world(db_session)
    actor = _user(db_session, "validation-hr", ["GROUP_HR"])
    headers = _token(client, actor.username)

    response = client.put(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_MAKEUP",
        headers=headers,
        json=body,
    )

    assert response.status_code == 422


def test_adjustment_upsert_requires_explicit_policy_classification(client, db_session):
    world = _world(db_session)
    actor = _user(db_session, "classification-hr", ["GROUP_HR"])

    response = client.put(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-05/PREV_MAKEUP",
        headers=_token(client, actor.username),
        json={
            "amount": "1",
            "reason": "Approved correction",
            "attachment_url": "https://files.example/evidence.pdf",
        },
    )

    assert response.status_code == 422


def test_adjustment_endpoints_reject_non_calendar_month(client, db_session):
    world = _world(db_session)
    actor = _user(db_session, "period-validation-hr", ["GROUP_HR"])
    headers = _token(client, actor.username)

    listed = client.get(
        "/api/payroll-adjustments?period=2026-13",
        headers=headers,
    )
    upserted = client.put(
        f"/api/payroll-adjustments/{world['employee_a'].id}/2026-13/PREV_MAKEUP",
        headers=headers,
        json={
            "amount": "1",
            "reason": "Approved correction",
            "attachment_url": "https://files.example/evidence.pdf",
        },
    )

    assert listed.status_code == 422
    assert upserted.status_code == 422


@pytest.mark.parametrize("batch_status", [BatchStatus.CALCULATING, BatchStatus.LOCKED])
def test_adjustment_upsert_rejects_started_and_locked_batches(client, db_session, batch_status):
    world = _world(db_session)
    actor = _user(db_session, f"locked-{batch_status.value.lower()}", ["GROUP_HR"])
    db_session.add(
        PayrollBatch(
            period="2026-05",
            attendance_start=date(2026, 5, 1),
            attendance_end=date(2026, 5, 31),
            status=batch_status,
        )
    )
    db_session.flush()
    headers = _token(client, actor.username)

    response = _put_adjustment(
        client,
        headers,
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
    )

    assert response.status_code == 409
    assert db_session.scalars(select(MonthlyPayrollAdjustment)).first() is None


def test_reopened_draft_adjustment_requires_payroll_correction_permission(client, db_session):
    world = _world(db_session)
    manager = _user(
        db_session,
        "reopened-manager",
        ["REGION_MANAGER"],
        org_scope_id=world["region_a"].id,
    )
    db_session.add(
        PayrollBatch(
            period="2026-05",
            attendance_start=date(2026, 5, 1),
            attendance_end=date(2026, 5, 31),
            status=BatchStatus.DRAFT,
            version=2,
        )
    )
    db_session.flush()
    headers = _token(client, manager.username)

    response = _put_adjustment(
        client,
        headers,
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_DEDUCT.value,
    )

    assert response.status_code == 403


def test_reopened_adjustment_records_batch_version_and_pending_rerun(client, db_session):
    world = _world(db_session)
    actor = _user(db_session, "correction-hr", ["GROUP_HR"])
    employee = world["employee_a"]
    batch = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.DRAFT,
        version=2,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add_all(
        [
            PayrollResult(
                batch_id=batch.id,
                batch_version=1,
                employee_id=employee.id,
                version=1,
                org_unit_id=world["store_a"].id,
                department=Department.OTHER,
                actual_attendance_days=Decimal("22"),
                statutory_holiday_days=Decimal("0"),
                statutory_holiday_worked_days=Decimal("0"),
                gross=Decimal("1000"),
                deposit=Decimal("0"),
                net=Decimal("1000"),
                carry_forward=Decimal("0"),
                deferred_deductions=Decimal("0"),
                deferred_deposit=Decimal("0"),
                rule_version="v4",
                input_snapshot={},
                lines=[],
                exceptions=[],
                warnings=[],
                has_error=False,
            ),
            MonthlyPayrollAdjustment(
                employee_id=employee.id,
                org_unit_id=world["store_a"].id,
                period="2026-05",
                adjustment_type=PayrollAdjustmentType.PREV_MAKEUP,
                amount=Decimal("100"),
                reason="Original approved makeup",
                attachment_url="https://files.example/original.pdf",
                created_by=actor.id,
                updated_by=actor.id,
            ),
        ]
    )
    db_session.flush()

    response = _put_adjustment(
        client,
        _token(client, actor.username),
        employee.id,
        PayrollAdjustmentType.PREV_MAKEUP.value,
        amount="125.50",
        reason="Corrected after payroll review",
        attachment_url="https://files.example/corrected.pdf",
    )

    assert response.status_code == 200, response.text
    adjustment = db_session.scalars(select(AdjustmentRecord)).one()
    assert adjustment.batch_id == batch.id
    assert adjustment.batch_version == 2
    assert adjustment.employee_id == employee.id
    assert adjustment.item == "PREV_MAKEUP_SOURCE"
    assert adjustment.before_value["amount"] == "100.00"
    assert adjustment.after_value["amount"] == "125.50"
    assert adjustment.reason == "Corrected after payroll review"
    assert adjustment.attachment_url == "https://files.example/corrected.pdf"
    assert adjustment.recompute_result == {
        "status": "PENDING_RERUN",
        "batch_version": 2,
    }


def test_reopened_adjustment_rejects_employee_outside_original_cohort(client, db_session):
    world = _world(db_session)
    actor = _user(db_session, "future-correction-hr", ["GROUP_HR"])
    db_session.add(
        PayrollBatch(
            period="2026-05",
            attendance_start=date(2026, 5, 1),
            attendance_end=date(2026, 5, 31),
            status=BatchStatus.DRAFT,
            version=2,
        )
    )
    db_session.flush()

    response = _put_adjustment(
        client,
        _token(client, actor.username),
        world["employee_a"].id,
        PayrollAdjustmentType.PREV_DEDUCT.value,
    )

    assert response.status_code == 409
    assert db_session.scalars(select(MonthlyPayrollAdjustment)).first() is None
    assert db_session.scalars(select(AdjustmentRecord)).first() is None


def test_build_input_variants_aggregate_monthly_adjustments_into_snapshot(db_session):
    world = _world(db_session)
    actor = _user(db_session, "input-hr", ["GROUP_HR"])
    employee = world["employee_a"]
    db_session.add_all(
        [
            MonthlyPayrollAdjustment(
                employee_id=employee.id,
                org_unit_id=world["store_a"].id,
                period="2026-05",
                adjustment_type=PayrollAdjustmentType.PREV_MAKEUP,
                amount=Decimal("123.45"),
                reason="Prior month underpayment",
                attachment_url="https://files.example/makeup.pdf",
                created_by=actor.id,
                updated_by=actor.id,
            ),
            MonthlyPayrollAdjustment(
                employee_id=employee.id,
                org_unit_id=world["store_a"].id,
                period="2026-05",
                adjustment_type=PayrollAdjustmentType.PREV_DEDUCT,
                amount=Decimal("50.10"),
                reason="Prior month overpayment",
                attachment_url="https://files.example/deduct.pdf",
                created_by=actor.id,
                updated_by=actor.id,
            ),
        ]
    )
    db_session.flush()

    single, _missing = build_input(db_session, employee, "2026-05")
    bulk, _bulk_missing = build_inputs(db_session, [employee], "2026-05")[employee.id]
    snapshot = _input_snapshot(single, [])

    assert single.prev_makeup == Decimal("123.45")
    assert single.prev_deduct == Decimal("50.10")
    assert bulk.prev_makeup == Decimal("123.45")
    assert bulk.prev_deduct == Decimal("50.10")
    assert snapshot["prev_makeup"] == "123.45"
    assert snapshot["prev_deduct"] == "50.10"
