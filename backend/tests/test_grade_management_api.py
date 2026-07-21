from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
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


@pytest.fixture
def hr_headers(client, db_session) -> dict[str, str]:
    seed_rbac(db_session)
    user = User(username="grade-hr", password_hash=hash_password("StrongPass123!"))
    db_session.add(user)
    db_session.flush()
    role = db_session.scalars(select(Role).where(Role.code == "GROUP_HR")).one()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.flush()

    response = client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def store_id(db_session) -> int:
    group = OrgUnit(code="GRADE-GROUP", name="Grade Group", type=OrgType.GROUP)
    db_session.add(group)
    db_session.flush()
    store = OrgUnit(
        code="GRADE-STORE",
        name="Grade Store",
        type=OrgType.STORE,
        parent_id=group.id,
    )
    db_session.add(store)
    db_session.flush()
    return store.id


def _create_grade(
    client,
    headers: dict[str, str],
    *,
    code: str,
    name: str | None = None,
    rank: int = 0,
) -> dict[str, Any]:
    response = client.post(
        "/api/grades",
        headers=headers,
        json={"code": code, "name": name or code, "rank": rank},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _band_payload(
    *,
    effective_from: str = "2026-01-01",
    band_min: str = "3000.00",
    band_mid: str = "4500.00",
    band_max: str = "6000.00",
) -> dict[str, str]:
    return {
        "band_min": band_min,
        "band_mid": band_mid,
        "band_max": band_max,
        "effective_from": effective_from,
    }


def _create_employee(
    client,
    headers: dict[str, str],
    *,
    emp_no: str,
    store_id: int,
    job_grade_id: int | None = None,
):
    payload: dict[str, Any] = {
        "emp_no": emp_no,
        "name": emp_no,
        "org_unit_id": store_id,
        "hire_date": "2026-01-01",
    }
    if job_grade_id is not None:
        payload["job_grade_id"] = job_grade_id
    return client.post("/api/employees", headers=headers, json=payload)


def _mixed_scope_grade_writer(session, *, username: str, org_unit_id: int) -> User:
    """Grant GRADE_WRITE locally while a different global role grants only audit read."""
    seed_rbac(session)
    global_role = Role(
        code=f"GLOBAL_OTHER_{username}"[:32],
        name=f"Unrelated global role {username}",
        is_global_scope=True,
    )
    scoped_role = Role(
        code=f"LOCAL_GRADE_{username}"[:32],
        name=f"Scoped grade writer {username}",
        is_global_scope=False,
    )
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add_all([global_role, scoped_role, user])
    session.flush()
    audit_read = session.scalars(select(Permission).where(Permission.code == Perm.AUDIT_READ)).one()
    grade_write = session.scalars(
        select(Permission).where(Permission.code == Perm.GRADE_WRITE)
    ).one()
    session.add_all(
        [
            RolePermission(role_id=global_role.id, permission_id=audit_read.id),
            RolePermission(role_id=scoped_role.id, permission_id=grade_write.id),
            UserRole(user_id=user.id, role_id=global_role.id),
            UserRole(user_id=user.id, role_id=scoped_role.id),
            UserOrgScope(user_id=user.id, org_unit_id=org_unit_id),
        ]
    )
    session.flush()
    return user


def test_grade_list_is_stably_sorted_and_supports_status_filters(client, hr_headers):
    _create_grade(client, hr_headers, code="P2", rank=2)
    inactive = _create_grade(client, hr_headers, code="P3-B", rank=3)
    _create_grade(client, hr_headers, code="P3-A", rank=3)
    _create_grade(client, hr_headers, code="P1", rank=1)

    deactivated = client.post(
        f"/api/grades/{inactive['id']}/deactivate",
        headers=hr_headers,
        json={"reason": "Role catalog consolidation"},
    )
    assert deactivated.status_code == 200

    default_response = client.get("/api/grades", headers=hr_headers)
    active_response = client.get("/api/grades?status=active", headers=hr_headers)
    inactive_response = client.get("/api/grades?status=inactive", headers=hr_headers)
    all_response = client.get("/api/grades?status=all", headers=hr_headers)

    assert default_response.status_code == 200
    assert active_response.status_code == 200
    assert inactive_response.status_code == 200
    assert all_response.status_code == 200
    assert [item["code"] for item in default_response.json()] == ["P3-A", "P2", "P1"]
    assert default_response.json() == active_response.json()
    assert [item["code"] for item in inactive_response.json()] == ["P3-B"]
    assert [item["code"] for item in all_response.json()] == [
        "P3-A",
        "P3-B",
        "P2",
        "P1",
    ]
    assert all(item["is_active"] for item in active_response.json())
    assert not inactive_response.json()[0]["is_active"]


def test_grade_catalog_writes_reject_permission_scope_mixing(
    client,
    db_session,
    hr_headers,
    store_id,
):
    """A global unrelated role must not globalize a locally granted grade write."""
    active = _create_grade(client, hr_headers, code="SCOPE-GRADE-A", rank=1)
    to_deactivate = _create_grade(client, hr_headers, code="SCOPE-GRADE-D", rank=2)
    to_restore = _create_grade(client, hr_headers, code="SCOPE-GRADE-R", rank=3)
    band_parent = _create_grade(client, hr_headers, code="SCOPE-GRADE-B", rank=4)
    deactivated = client.post(
        f"/api/grades/{to_restore['id']}/deactivate",
        headers=hr_headers,
        json={"reason": "Prepare inactive grade for scope regression"},
    )
    assert deactivated.status_code == 200, deactivated.text

    scoped_user = _mixed_scope_grade_writer(
        db_session,
        username="grade-mixed-scope",
        org_unit_id=store_id,
    )
    scoped_headers = _headers(client, scoped_user.username)
    responses = [
        client.post(
            "/api/grades",
            headers=scoped_headers,
            json={"code": "SCOPE-GRADE-C", "name": "Unauthorized create", "rank": 5},
        ),
        client.patch(
            f"/api/grades/{active['id']}",
            headers=scoped_headers,
            json={
                "expected_version": active["version"],
                "name": "Unauthorized update",
            },
        ),
        client.post(
            f"/api/grades/{to_deactivate['id']}/deactivate",
            headers=scoped_headers,
            json={"reason": "Unauthorized scoped deactivate"},
        ),
        client.post(
            f"/api/grades/{to_restore['id']}/restore",
            headers=scoped_headers,
            json={"reason": "Unauthorized scoped restore"},
        ),
        client.post(
            f"/api/grades/{band_parent['id']}/bands",
            headers=scoped_headers,
            json=_band_payload(),
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403, 403, 403], [
        response.text for response in responses
    ]


def test_grade_create_trims_text_and_rejects_blank_fields(client, hr_headers):
    created = client.post(
        "/api/grades",
        headers=hr_headers,
        json={"code": "  P5  ", "name": "  Senior Specialist  ", "rank": 5},
    )

    assert created.status_code == 201
    assert created.json()["code"] == "P5"
    assert created.json()["name"] == "Senior Specialist"
    assert created.json()["version"] == 1
    assert created.json()["is_active"] is True

    for payload in (
        {"code": "   ", "name": "Nonblank", "rank": 1},
        {"code": "P6", "name": "   ", "rank": 1},
    ):
        response = client.post("/api/grades", headers=hr_headers, json=payload)
        assert response.status_code == 422


def test_grade_patch_rejects_empty_or_null_changes_and_checks_expected_version(client, hr_headers):
    grade = _create_grade(client, hr_headers, code="P6", name="Original", rank=6)
    initial_version = grade.get("version", 1)

    for payload in (
        {},
        {"expected_version": initial_version},
        {"expected_version": initial_version, "name": None},
        {"expected_version": initial_version, "rank": None},
        {"name": "Missing version"},
    ):
        response = client.patch(f"/api/grades/{grade['id']}", headers=hr_headers, json=payload)
        assert response.status_code == 422

    updated = client.patch(
        f"/api/grades/{grade['id']}",
        headers=hr_headers,
        json={
            "expected_version": initial_version,
            "name": "  Principal Specialist  ",
            "rank": 7,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Principal Specialist"
    assert updated.json()["rank"] == 7
    assert updated.json()["version"] == initial_version + 1

    stale = client.patch(
        f"/api/grades/{grade['id']}",
        headers=hr_headers,
        json={"expected_version": initial_version, "name": "Stale overwrite"},
    )
    assert stale.status_code == 409

    no_op = client.patch(
        f"/api/grades/{grade['id']}",
        headers=hr_headers,
        json={
            "expected_version": updated.json()["version"],
            "name": updated.json()["name"],
        },
    )
    assert no_op.status_code == 422


def test_grade_deactivate_and_restore_require_reason_and_are_idempotent(client, hr_headers):
    grade = _create_grade(client, hr_headers, code="P7", rank=7)

    for payload in ({}, {"reason": "   "}):
        response = client.post(
            f"/api/grades/{grade['id']}/deactivate",
            headers=hr_headers,
            json=payload,
        )
        assert response.status_code == 422

    deactivated = client.post(
        f"/api/grades/{grade['id']}/deactivate",
        headers=hr_headers,
        json={"reason": "No longer used for new assignments"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert deactivated.json()["version"] == grade["version"] + 1

    deactivated_again = client.post(
        f"/api/grades/{grade['id']}/deactivate",
        headers=hr_headers,
        json={"reason": "Repeated request after a client timeout"},
    )
    assert deactivated_again.status_code == 200
    assert deactivated_again.json()["version"] == deactivated.json()["version"]

    blank_restore = client.post(
        f"/api/grades/{grade['id']}/restore",
        headers=hr_headers,
        json={"reason": "   "},
    )
    assert blank_restore.status_code == 422

    restored = client.post(
        f"/api/grades/{grade['id']}/restore",
        headers=hr_headers,
        json={"reason": "Approved for new assignments again"},
    )
    assert restored.status_code == 200
    assert restored.json()["is_active"] is True
    assert restored.json()["version"] == deactivated.json()["version"] + 1

    restored_again = client.post(
        f"/api/grades/{grade['id']}/restore",
        headers=hr_headers,
        json={"reason": "Repeated request after a client timeout"},
    )
    assert restored_again.status_code == 200
    assert restored_again.json()["version"] == restored.json()["version"]


def test_inactive_grade_blocks_new_assignments_and_bands_but_not_existing_employee_edits(
    client, hr_headers, store_id
):
    grade = _create_grade(client, hr_headers, code="P8", rank=8)
    assigned = _create_employee(
        client,
        hr_headers,
        emp_no="ASSIGNED-P8",
        store_id=store_id,
        job_grade_id=grade["id"],
    )
    assert assigned.status_code == 201
    ungraded = _create_employee(
        client,
        hr_headers,
        emp_no="UNASSIGNED",
        store_id=store_id,
    )
    assert ungraded.status_code == 201

    deactivated = client.post(
        f"/api/grades/{grade['id']}/deactivate",
        headers=hr_headers,
        json={"reason": "Retired from the assignment catalog"},
    )
    assert deactivated.status_code == 200

    new_assignment = _create_employee(
        client,
        hr_headers,
        emp_no="NEW-P8",
        store_id=store_id,
        job_grade_id=grade["id"],
    )
    assert new_assignment.status_code == 409

    reassignment = client.patch(
        f"/api/employees/{ungraded.json()['id']}",
        headers=hr_headers,
        json={"job_grade_id": grade["id"]},
    )
    assert reassignment.status_code == 409

    band = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json=_band_payload(),
    )
    assert band.status_code == 409

    unrelated_edit = client.patch(
        f"/api/employees/{assigned.json()['id']}",
        headers=hr_headers,
        json={"name": "Existing employee renamed"},
    )
    assert unrelated_edit.status_code == 200
    assert unrelated_edit.json()["name"] == "Existing employee renamed"
    assert unrelated_edit.json()["job_grade_id"] == grade["id"]


def test_salary_band_parent_must_exist(client, hr_headers):
    missing_id = 999_999
    listed = client.get(f"/api/grades/{missing_id}/bands", headers=hr_headers)
    created = client.post(
        f"/api/grades/{missing_id}/bands",
        headers=hr_headers,
        json=_band_payload(),
    )

    assert listed.status_code == 404
    assert created.status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("band_min", "-0.01"),
        ("band_mid", "-0.01"),
        ("band_max", "-0.01"),
        ("band_min", "3000.001"),
        ("band_mid", "4500.001"),
        ("band_max", "6000.001"),
    ],
)
def test_salary_band_rejects_negative_or_sub_cent_amounts(client, hr_headers, field, value):
    grade = _create_grade(client, hr_headers, code=f"AMOUNT-{field}-{value}")
    payload = _band_payload()
    payload[field] = value
    # Prove the 422 comes from amount validation, not a missing legacy body key.
    payload["job_grade_id"] = grade["id"]

    response = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json={**payload, "job_grade_id": grade["id"]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        _band_payload(band_min="4500.01", band_mid="4500.00"),
        _band_payload(band_mid="6000.01", band_max="6000.00"),
    ],
)
def test_salary_band_rejects_invalid_min_mid_max_order(client, hr_headers, payload):
    grade = _create_grade(
        client, hr_headers, code=f"ORDER-{payload['band_min']}-{payload['band_mid']}"
    )

    response = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json={**payload, "job_grade_id": grade["id"]},
    )

    assert response.status_code == 400


def test_salary_band_rejects_duplicate_date_and_mismatched_legacy_grade_id(client, hr_headers):
    grade = _create_grade(client, hr_headers, code="P9", rank=9)
    other_grade = _create_grade(client, hr_headers, code="P10", rank=10)

    first = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json=_band_payload(),
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json=_band_payload(band_min="3100.00", band_mid="4600.00", band_max="6100.00"),
    )
    assert duplicate.status_code == 409

    mismatched_legacy_id = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json={
            **_band_payload(effective_from="2026-02-01"),
            "job_grade_id": other_grade["id"],
        },
    )
    assert mismatched_legacy_id.status_code == 422

    matching_legacy_id = client.post(
        f"/api/grades/{grade['id']}/bands",
        headers=hr_headers,
        json={
            **_band_payload(effective_from="2026-02-01"),
            "job_grade_id": grade["id"],
        },
    )
    assert matching_legacy_id.status_code == 201


def test_salary_band_history_is_stable_and_exposes_effective_to(client, hr_headers):
    grade = _create_grade(client, hr_headers, code="P11", rank=11)
    for effective_from in ("2026-04-01", "2026-01-01", "2026-07-01"):
        response = client.post(
            f"/api/grades/{grade['id']}/bands",
            headers=hr_headers,
            json=_band_payload(effective_from=effective_from),
        )
        assert response.status_code == 201

    history = client.get(f"/api/grades/{grade['id']}/bands", headers=hr_headers)

    assert history.status_code == 200
    assert [item["effective_from"] for item in history.json()] == [
        "2026-07-01",
        "2026-04-01",
        "2026-01-01",
    ]
    assert [item["effective_to"] for item in history.json()] == [
        None,
        "2026-07-01",
        "2026-04-01",
    ]
