from __future__ import annotations

import pytest
from sqlalchemy import select

from app.audit import service as audit
from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.org import OrgType, OrgUnit
from app.models.salary import SalaryRecord, SalarySource

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


def _user(session, username: str, roles: list[str]) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for role_code in roles:
        role = session.scalars(select(Role).where(Role.code == role_code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_auditor_can_filter_paginate_and_sees_masked_detail(client, db_session):
    auditor = _user(db_session, "auditor", ["AUDITOR"])
    _user(db_session, "ordinary", ["EMPLOYEE"])
    first = audit.record(
        db_session,
        action="employee.create",
        actor=(99, "hr-a"),
        target_type="employee",
        target_id=1,
        detail={
            "id_card": "raw-id",
            "attachment_url": "https://signed.example.test/evidence?token=secret",
            "nested": {"bank_account": "raw-bank"},
        },
    )
    second = audit.record(
        db_session,
        action="employee.create",
        actor=(98, "hr-b"),
        target_type="employee",
        target_id=2,
        detail={"safe": "value"},
    )
    db_session.commit()
    headers = _token(client, "auditor")

    response = client.get(
        "/api/audit-logs",
        headers=headers,
        params={"action": "employee.create", "actor_username": "hr-a", "page_size": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["items"] == [
        {
            "id": first.id,
            "ts": body["items"][0]["ts"],
            "actor_user_id": 99,
            "actor_username": "hr-a",
            "action": "employee.create",
            "result": "SUCCESS",
            "target_type": "employee",
            "target_id": 1,
            "detail": {
                "id_card": "***",
                "attachment_url": "***",
                "nested": {"bank_account": "***"},
            },
        }
    ]
    assert second.id != first.id
    # The view record is written after the result set is selected, so it does
    # not recurse into this page response.
    assert all(item["action"] != "audit.log.view" for item in body["items"])
    view = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "audit.log.view", AuditLog.actor_user_id == auditor.id)
        .order_by(AuditLog.id.desc())
    ).first()
    assert view is not None
    assert view.detail["returned"] == 1


def test_audit_log_requires_audit_read_permission(client, db_session):
    _user(db_session, "ordinary", ["EMPLOYEE"])

    assert client.get("/api/audit-logs", headers=_token(client, "ordinary")).status_code == 403


def test_salary_search_audits_query_shape_without_sensitive_filter_values(client, db_session):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    db_session.add(
        SalaryRecord(
            period="2026-05",
            emp_no="E001",
            name="张三",
            store_name="广州店",
            org_unit_id=store.id,
            source=SalarySource.HISTORICAL,
            fields={"net": "5000.00"},
        )
    )
    hr = _user(db_session, "hr", ["GROUP_HR"])

    response = client.get(
        "/api/salary-records",
        headers=_token(client, "hr"),
        params={"name": "张三", "emp_no": "E001", "period": "2026-05", "store": "广州店"},
    )

    assert response.status_code == 200, response.text
    entry = db_session.scalars(
        select(AuditLog).where(
            AuditLog.action == "salary.records.search", AuditLog.actor_user_id == hr.id
        )
    ).one()
    assert entry.detail == {
        "has_name_filter": True,
        "has_emp_no_filter": True,
        "has_period_filter": True,
        "has_store_filter": True,
        "page": 1,
        "page_size": 50,
        "returned": 1,
    }
    assert "张三" not in str(entry.detail)
    assert "E001" not in str(entry.detail)
