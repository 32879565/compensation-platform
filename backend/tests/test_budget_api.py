from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Permission, Role, RolePermission, User, UserOrgScope, UserRole
from app.models.org import OrgType, OrgUnit

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


def _org_tree(session):
    group = OrgUnit(code="BUDGET_GROUP", name="Group", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    west = OrgUnit(code="BUDGET_WEST", name="West", type=OrgType.REGION, parent_id=group.id)
    east = OrgUnit(code="BUDGET_EAST", name="East", type=OrgType.REGION, parent_id=group.id)
    session.add_all([west, east])
    session.flush()
    west_store = OrgUnit(
        code="BUDGET_WEST_STORE", name="West store", type=OrgType.STORE, parent_id=west.id
    )
    east_store = OrgUnit(
        code="BUDGET_EAST_STORE", name="East store", type=OrgType.STORE, parent_id=east.id
    )
    session.add_all([west_store, east_store])
    session.flush()
    return {
        "group": group,
        "west": west,
        "east": east,
        "west_store": west_store,
        "east_store": east_store,
    }


def _user(
    session, username: str, role_codes: list[str], scope_ids: list[int] | None = None
) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for role_code in role_codes:
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


def _scoped_budget_writer_role(session) -> None:
    seed_rbac(session)
    role = Role(code="SCOPED_BUDGET_WRITER", name="Scoped budget writer", is_global_scope=False)
    session.add(role)
    session.flush()
    for code in (Perm.BUDGET_READ, Perm.BUDGET_WRITE):
        permission_id = session.scalars(select(Permission.id).where(Permission.code == code)).one()
        session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    session.flush()


def _budget_payload(org_unit_id: int, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "org_unit_id": org_unit_id,
        "period": "2026-07-01",
        "headcount_budget": 12,
        "labor_cost_budget": "120000.00",
        "note": "July plan",
    }
    payload.update(changes)
    return payload


def test_budget_crud_requires_month_period_and_records_audit(client, db_session):
    orgs = _org_tree(db_session)
    admin = _user(db_session, "budget-admin", ["GROUP_HR"])
    headers = _token(client, admin.username)

    created = client.post(
        "/api/budgets", headers=headers, json=_budget_payload(orgs["west_store"].id)
    )
    assert created.status_code == 201, created.text
    budget_id = created.json()["id"]
    budget_version = created.json()["version"]
    assert created.json()["labor_cost_budget"] == "120000.00"

    duplicate = client.post(
        "/api/budgets", headers=headers, json=_budget_payload(orgs["west_store"].id)
    )
    assert duplicate.status_code == 409
    invalid_period = client.post(
        "/api/budgets",
        headers=headers,
        json=_budget_payload(orgs["east_store"].id, period="2026-07-02"),
    )
    assert invalid_period.status_code == 422
    non_store = client.post("/api/budgets", headers=headers, json=_budget_payload(orgs["west"].id))
    assert non_store.status_code == 422

    listed = client.get("/api/budgets", headers=headers, params={"period": "2026-07-01"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == budget_id

    updated = client.patch(
        f"/api/budgets/{budget_id}",
        headers=headers,
        json={"version": budget_version, "headcount_budget": 14, "note": "  revised plan  "},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["headcount_budget"] == 14
    assert updated.json()["note"] == "revised plan"
    assert updated.json()["version"] == budget_version + 1
    stale_update = client.patch(
        f"/api/budgets/{budget_id}",
        headers=headers,
        json={"version": budget_version, "headcount_budget": 9},
    )
    assert stale_update.status_code == 409

    deleted = client.delete(
        f"/api/budgets/{budget_id}", headers=headers, params={"version": updated.json()["version"]}
    )
    assert deleted.status_code == 204
    assert client.get("/api/budgets", headers=headers).json()["total"] == 0
    audit_rows = list(
        db_session.scalars(
            select(AuditLog).where(AuditLog.actor_user_id == admin.id).order_by(AuditLog.id)
        ).all()
    )
    actions = {row.action for row in audit_rows}
    assert {"budget.create", "budget.update", "budget.delete"} <= actions
    create_audit = next(row for row in audit_rows if row.action == "budget.create")
    update_audit = next(row for row in audit_rows if row.action == "budget.update")
    delete_audit = next(row for row in audit_rows if row.action == "budget.delete")
    assert create_audit.detail["before"] is None
    assert create_audit.detail["after"]["labor_cost_budget"] == "120000.00"
    assert update_audit.detail["before"]["headcount_budget"] == 12
    assert update_audit.detail["after"]["headcount_budget"] == 14
    assert delete_audit.detail["before"]["note"] == "revised plan"
    assert delete_audit.detail["after"] is None


def test_budget_scope_hides_and_blocks_unseen_organizations(client, db_session):
    orgs = _org_tree(db_session)
    _scoped_budget_writer_role(db_session)
    _user(db_session, "budget-admin", ["GROUP_HR"])
    _user(db_session, "west-writer", ["SCOPED_BUDGET_WRITER"], [orgs["west"].id])
    admin_headers = _token(client, "budget-admin")
    writer_headers = _token(client, "west-writer")
    west = client.post(
        "/api/budgets", headers=admin_headers, json=_budget_payload(orgs["west_store"].id)
    )
    east = client.post(
        "/api/budgets", headers=admin_headers, json=_budget_payload(orgs["east_store"].id)
    )
    assert west.status_code == east.status_code == 201

    listed = client.get("/api/budgets", headers=writer_headers)
    assert listed.status_code == 200
    assert [item["org_unit_id"] for item in listed.json()["items"]] == [orgs["west_store"].id]
    assert (
        client.patch(
            f"/api/budgets/{east.json()['id']}",
            headers=writer_headers,
            json={"version": east.json()["version"], "headcount_budget": 1},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/budgets",
            headers=writer_headers,
            json=_budget_payload(orgs["east_store"].id, period="2026-08-01"),
        ).status_code
        == 404
    )


def test_budget_endpoints_require_their_permissions(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "ordinary", ["EMPLOYEE"])
    headers = _token(client, "ordinary")

    assert client.get("/api/budgets", headers=headers).status_code == 403
    response = client.post(
        "/api/budgets", headers=headers, json=_budget_payload(orgs["west_store"].id)
    )
    assert response.status_code == 403
