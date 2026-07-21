from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
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


def _user(session, username: str, role_code: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == role_code)).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _legacy_record(
    session,
    *,
    period: str,
    emp_no: str,
    name: str,
    position: str,
    comprehensive: str,
    allowance: str | None = None,
) -> SalaryRecord:
    fields = {"职位": position, "综合薪资": comprehensive}
    if allowance is not None:
        fields["补贴"] = allowance
    record = SalaryRecord(
        period=period,
        emp_no=emp_no,
        name=name,
        store_name="Legacy store",
        source=SalarySource.HISTORICAL,
        fields=fields,
    )
    session.add(record)
    session.flush()
    return record


def test_legacy_catalog_preview_is_aggregate_private_and_does_not_invent_official_grades(
    client, db_session
):
    _user(db_session, "legacy-catalog-hr", "GROUP_HR")
    headers = _headers(client, "legacy-catalog-hr")
    _legacy_record(
        db_session,
        period="2026-04",
        emp_no="PRIVATE-001",
        name="Private legacy person one",
        position="服务员",
        comprehensive="4000.00",
        allowance="100.00",
    )
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="PRIVATE-002",
        name="Private legacy person two",
        position="服务员",
        comprehensive="4400.00",
        allowance="0.00",
    )
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="PRIVATE-003",
        name="Private legacy person three",
        position="厨工",
        comprehensive="5000.00",
    )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == {
        "record_count": 3,
        "period_from": "2026-04",
        "period_to": "2026-05",
    }
    serialized = response.text
    assert "PRIVATE-" not in serialized
    assert "Private legacy person" not in serialized

    components = {item["source_field"]: item for item in body["component_candidates"]}
    assert components["综合薪资"]["record_count"] == 3
    assert components["综合薪资"]["nonzero_count"] == 3
    assert components["综合薪资"]["classification"] == "NEEDS_HR_CONFIRMATION"
    assert components["补贴"]["record_count"] == 2
    assert components["补贴"]["nonzero_count"] == 1
    assert components["补贴"]["suggested_component_type"] == "ALLOWANCE"
    assert components["补贴"]["suggested_allowance_kind"] is None

    assert body["grade_source_status"] == "OFFICIAL_MASTER_NOT_PRESENT"
    waiter = next(item for item in body["grade_candidates"] if item["position"] == "服务员")
    assert waiter["record_count"] == 2
    assert waiter["salary_sample_count"] == 2
    assert waiter["observed_median"] == "4200.00"
    assert waiter["is_official_grade"] is False
    assert "band_min" not in waiter
    assert "band_mid" not in waiter
    assert "band_max" not in waiter


def test_legacy_catalog_preview_requires_global_import_permission(client, db_session):
    _user(db_session, "legacy-catalog-employee", "EMPLOYEE")

    response = client.get(
        "/api/legacy-catalog/preview",
        headers=_headers(client, "legacy-catalog-employee"),
    )

    assert response.status_code == 403


def test_confirmed_legacy_component_import_uses_real_source_count_and_audits_provenance(
    client, db_session
):
    hr = _user(db_session, "legacy-component-hr", "GROUP_HR")
    headers = _headers(client, hr.username)
    for month, amount in (("2026-04", "4000.00"), ("2026-05", "4200.00")):
        _legacy_record(
            db_session,
            period=month,
            emp_no=f"SOURCE-{month}",
            name=f"Source {month}",
            position="服务员",
            comprehensive=amount,
        )

    response = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json={
            "source_field": "综合薪资",
            "expected_record_count": 2,
            "confirmed_by_hr": True,
            "reason": "旧系统综合薪资字段经薪酬负责人核对",
            "component": {
                "code": "COMPREHENSIVE",
                "name": "综合薪资",
                "component_type": "COMPREHENSIVE",
                "taxable": True,
                "in_social_base": True,
                "in_housing_base": True,
                "prorate_by_attendance": False,
                "sort_order": 10,
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["code"] == "COMPREHENSIVE"
    assert response.json()["created_by"] == hr.id
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "legacy_catalog.component.apply")
    ).one()
    assert event.actor_user_id == hr.id
    assert event.detail["source_field"] == "综合薪资"
    assert event.detail["source_record_count"] == 2
    assert event.detail["period_from"] == "2026-04"
    assert event.detail["period_to"] == "2026-05"
    assert event.detail["reason"] == "旧系统综合薪资字段经薪酬负责人核对"


@pytest.mark.parametrize(
    ("payload_update", "expected_status"),
    [
        ({"confirmed_by_hr": False}, 422),
        ({"expected_record_count": 999}, 409),
        ({"source_field": "不存在的旧字段"}, 404),
    ],
)
def test_legacy_component_import_fails_closed_without_confirmation_or_matching_source(
    client, db_session, payload_update, expected_status
):
    _user(db_session, "legacy-component-guard-hr", "GROUP_HR")
    headers = _headers(client, "legacy-component-guard-hr")
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="SOURCE-GUARD",
        name="Source guard",
        position="服务员",
        comprehensive="4200.00",
    )
    payload = {
        "source_field": "综合薪资",
        "expected_record_count": 1,
        "confirmed_by_hr": True,
        "reason": "经人事确认",
        "component": {
            "code": "LEGACY_GUARD",
            "name": "旧系统字段",
            "component_type": "COMPREHENSIVE",
        },
    }
    payload.update(payload_update)

    response = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json=payload,
    )

    assert response.status_code == expected_status


def test_confirmed_grade_import_keeps_historical_observations_distinct_from_policy_band(
    client, db_session
):
    hr = _user(db_session, "legacy-grade-hr", "GROUP_HR")
    headers = _headers(client, hr.username)
    for index, salary in enumerate(("4000.00", "4200.00", "4400.00"), start=1):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"GRADE-SOURCE-{index}",
            name=f"Grade source {index}",
            position="服务员",
            comprehensive=salary,
        )

    response = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=headers,
        json={
            "source_position": "服务员",
            "expected_record_count": 3,
            "policy_confirmation": "HR_CONFIRMED",
            "reason": "人事确认服务员对应门店一职级，薪档采用现行政策",
            "grade": {"code": "STORE-P1", "name": "门店一职级", "rank": 10},
            "band": {
                "band_min": "3800.00",
                "band_mid": "4300.00",
                "band_max": "5000.00",
                "effective_from": "2026-07-01",
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["grade"]["code"] == "STORE-P1"
    assert response.json()["band"]["band_min"] == "3800.00"
    assert response.json()["observed_history"] == {
        "record_count": 3,
        "salary_sample_count": 3,
        "observed_median": "4200.00",
    }
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "legacy_catalog.grade.apply")
    ).one()
    assert event.detail["source_position"] == "服务员"
    assert event.detail["observed_median"] == "4200.00"
    assert event.detail["policy_band"] == {
        "band_min": "3800.00",
        "band_mid": "4300.00",
        "band_max": "5000.00",
        "effective_from": "2026-07-01",
    }


def test_grade_import_requires_explicit_policy_confirmation_and_current_source_count(
    client, db_session
):
    _user(db_session, "legacy-grade-guard-hr", "GROUP_HR")
    headers = _headers(client, "legacy-grade-guard-hr")
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="GRADE-GUARD",
        name="Grade guard",
        position="服务员",
        comprehensive="4200.00",
    )
    base_payload = {
        "source_position": "服务员",
        "expected_record_count": 1,
        "policy_confirmation": "HR_CONFIRMED",
        "reason": "人事确认",
        "grade": {"code": "STORE-P1", "name": "门店一职级", "rank": 10},
        "band": {
            "band_min": "3800.00",
            "band_mid": "4300.00",
            "band_max": "5000.00",
            "effective_from": "2026-07-01",
        },
    }

    missing_confirmation = {
        **base_payload,
        "policy_confirmation": "HISTORICAL_DATA_ONLY",
    }
    stale_source = {**base_payload, "expected_record_count": 2}

    assert (
        client.post(
            "/api/legacy-catalog/grades/apply",
            headers=headers,
            json=missing_confirmation,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/legacy-catalog/grades/apply",
            headers=headers,
            json=stale_source,
        ).status_code
        == 409
    )


def test_preview_ignores_new_system_payroll_results(client, db_session):
    _user(db_session, "legacy-source-filter-hr", "GROUP_HR")
    headers = _headers(client, "legacy-source-filter-hr")
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="LEGACY-ONLY",
        name="Legacy only",
        position="服务员",
        comprehensive="4200.00",
    )
    db_session.add(
        SalaryRecord(
            period="2026-06",
            emp_no="NEW-SYSTEM",
            name="New system",
            store_name="New store",
            source=SalarySource.PAYROLL_RUN,
            fields={"职位": "店长", "综合薪资": str(Decimal("99999.00"))},
        )
    )
    db_session.flush()

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["source"]["record_count"] == 1
    assert {item["position"] for item in response.json()["grade_candidates"]} == {"服务员"}
