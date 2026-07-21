from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Permission, Role, RolePermission, User, UserOrgScope, UserRole
from app.models.employee import Employee
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


def _user(session, username: str, role_code: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == role_code)).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _mixed_scope_importer(
    session,
    *,
    username: str,
    scoped_permission: str,
) -> User:
    """Grant IMPORT_RUN globally but the catalog write only through a local role."""
    seed_rbac(session)
    store = OrgUnit(
        code=f"LEGACY_SCOPE_{username}",
        name=f"Legacy catalog scope {username}",
        type=OrgType.STORE,
    )
    importer_role = Role(
        code=f"GLOBAL_IMPORT_{username}"[:32],
        name=f"Global importer {username}",
        is_global_scope=True,
    )
    writer_role = Role(
        code=f"LOCAL_WRITE_{username}"[:32],
        name=f"Scoped catalog writer {username}",
        is_global_scope=False,
    )
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add_all([store, importer_role, writer_role, user])
    session.flush()
    import_run = session.scalars(select(Permission).where(Permission.code == Perm.IMPORT_RUN)).one()
    catalog_write = session.scalars(
        select(Permission).where(Permission.code == scoped_permission)
    ).one()
    session.add_all(
        [
            RolePermission(role_id=importer_role.id, permission_id=import_run.id),
            RolePermission(role_id=writer_role.id, permission_id=catalog_write.id),
            UserRole(user_id=user.id, role_id=importer_role.id),
            UserRole(user_id=user.id, role_id=writer_role.id),
            UserOrgScope(user_id=user.id, org_unit_id=store.id),
        ]
    )
    session.flush()
    return user


def _headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _source_snapshot_id(client, headers: dict[str, str]) -> str:
    response = client.get("/api/legacy-catalog/preview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["source"]["snapshot_id"]


def _legacy_record(
    session,
    *,
    period: str,
    emp_no: str,
    name: str,
    position: str,
    comprehensive: str,
    allowance: str | None = None,
    employee_id: int | None = None,
) -> SalaryRecord:
    fields = {"职位": position, "综合薪资": comprehensive}
    if allowance is not None:
        fields["补贴"] = allowance
    record = SalaryRecord(
        period=period,
        emp_no=emp_no,
        name=name,
        store_name="Legacy store",
        employee_id=employee_id,
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
    for index, salary in enumerate(("4100.00", "4200.00", "4300.00"), start=4):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"PRIVATE-00{index}",
            name=f"Private legacy person {index}",
            position="服务员",
            comprehensive=salary,
        )
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="PRIVATE-009",
        name="Private legacy person nine",
        position="厨工",
        comprehensive="5000.00",
    )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"]["record_count"] == 6
    assert body["source"]["period_from"] == "2026-04"
    assert body["source"]["period_to"] == "2026-05"
    assert len(body["source"]["snapshot_id"]) == 32
    serialized = response.text
    assert "PRIVATE-" not in serialized
    assert "Private legacy person" not in serialized

    components = {item["source_field"]: item for item in body["component_candidates"]}
    assert components["综合薪资"]["record_count"] == 6
    assert components["综合薪资"]["nonzero_count"] == 6
    assert components["综合薪资"]["classification"] == "NEEDS_HR_CONFIRMATION"
    assert components["补贴"]["record_count"] == 2
    assert components["补贴"]["nonzero_count"] == 1
    assert components["补贴"]["suggested_component_type"] == "ALLOWANCE"
    assert components["补贴"]["suggested_allowance_kind"] is None

    assert body["grade_source_status"] == "OFFICIAL_MASTER_NOT_PRESENT"
    waiter = next(item for item in body["grade_candidates"] if item["position"] == "服务员")
    assert waiter["record_count"] == 5
    assert waiter["contributor_count"] == 5
    assert waiter["salary_sample_count"] == 5
    assert waiter["observed_median"] == "4200.00"
    assert waiter["is_official_grade"] is False
    assert "band_min" not in waiter
    assert "band_mid" not in waiter
    assert "band_max" not in waiter


def test_legacy_grade_preview_omits_sub_threshold_positions_entirely(client, db_session):
    """Fewer than five samples must not disclose a position or its exact counts."""
    _user(db_session, "legacy-privacy-threshold-hr", "GROUP_HR")
    headers = _headers(client, "legacy-privacy-threshold-hr")
    for index in range(1, 5):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"RARE-{index}",
            name=f"Rare employee {index}",
            position="稀有单人岗位",
            comprehensive=f"{4000 + index * 100}.00",
        )
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="RARE-ZERO",
        name="Rare zero placeholder",
        position="稀有单人岗位",
        comprehensive="0.00",
    )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["grade_candidates"] == []
    assert "稀有单人岗位" not in response.text


@pytest.mark.parametrize("guessed_count", [1, 4, 999])
def test_suppressed_legacy_position_apply_is_indistinguishable_from_missing(
    client, db_session, guessed_count: int
):
    _user(db_session, f"legacy-hidden-{guessed_count}-hr", "GROUP_HR")
    headers = _headers(client, f"legacy-hidden-{guessed_count}-hr")
    for index in range(1, 5):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"HIDDEN-{guessed_count}-{index}",
            name=f"Hidden {index}",
            position="隐私岗位",
            comprehensive=f"{4000 + index * 100}.00",
        )
    snapshot_id = _source_snapshot_id(client, headers)

    response = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=headers,
        json={
            "source_position": "隐私岗位",
            "expected_record_count": guessed_count,
            "expected_source_snapshot_id": snapshot_id,
            "policy_confirmation": "HR_CONFIRMED",
            "reason": "Direct guesses must not reveal private evidence",
            "grade": {"code": f"PRIVATE-{guessed_count}", "name": "Private", "rank": 1},
            "band": {
                "band_min": "3800.00",
                "band_mid": "4300.00",
                "band_max": "5000.00",
                "effective_from": "2026-07-01",
            },
        },
    )

    assert response.status_code == 404, response.text


def test_legacy_grade_privacy_threshold_counts_distinct_people_not_monthly_rows(client, db_session):
    """One person's repeated monthly records must never satisfy k-anonymity."""
    _user(db_session, "legacy-distinct-person-hr", "GROUP_HR")
    headers = _headers(client, "legacy-distinct-person-hr")
    for month in range(1, 7):
        _legacy_record(
            db_session,
            period=f"2026-{month:02d}",
            emp_no="REPEATED-ONE",
            name="Repeated legacy person",
            position="仅一人历史岗位",
            comprehensive=f"{4000 + month * 100}.00",
        )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["grade_candidates"] == []
    assert "仅一人历史岗位" not in response.text


def test_legacy_grade_identity_prefers_employee_id_over_changed_names_and_numbers(
    client, db_session
):
    """Aliases for one reconciled employee remain one private contributor."""

    _user(db_session, "legacy-stable-identity-hr", "GROUP_HR")
    headers = _headers(client, "legacy-stable-identity-hr")
    store = OrgUnit(code="IDENTITY_STORE", name="Identity store", type=OrgType.STORE)
    employee = Employee(emp_no="CURRENT-IDENTITY", name="Current name", org_unit_id=0)
    db_session.add(store)
    db_session.flush()
    employee.org_unit_id = store.id
    db_session.add(employee)
    db_session.flush()
    for month in range(1, 7):
        _legacy_record(
            db_session,
            period=f"2026-{month:02d}",
            emp_no=f"OLD-NUMBER-{month}",
            name=f"Changed name {month}",
            position="别名岗位",
            comprehensive=f"{4000 + month * 100}.00",
            employee_id=employee.id,
        )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["grade_candidates"] == []
    assert "别名岗位" not in response.text


def test_legacy_grade_percentiles_weight_each_employee_once(client, db_session):
    """Many months from one person cannot dominate a position percentile."""

    _user(db_session, "legacy-equal-weight-hr", "GROUP_HR")
    headers = _headers(client, "legacy-equal-weight-hr")
    for month in range(1, 11):
        _legacy_record(
            db_session,
            period=f"2025-{month:02d}",
            emp_no="MANY-MONTHS",
            name="Many months",
            position="均衡岗位",
            comprehensive="10000.00",
        )
    for index, salary in enumerate(("4000", "4100", "4200", "4300"), start=1):
        _legacy_record(
            db_session,
            period="2026-01",
            emp_no=f"ONE-MONTH-{index}",
            name=f"One month {index}",
            position="均衡岗位",
            comprehensive=salary,
        )

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    candidate = response.json()["grade_candidates"][0]
    assert candidate["contributor_count"] == 5
    assert candidate["salary_sample_count"] == 14
    assert candidate["observed_median"] == "4200.00"


def test_legacy_component_preview_covers_confirmed_real_old_system_fields(client, db_session):
    """The reviewed 68,245-row legacy column names stay mapped to catalog semantics."""
    _user(db_session, "legacy-real-field-hr", "GROUP_HR")
    headers = _headers(client, "legacy-real-field-hr")
    expected_types = {
        "传菜岗": "POSITION",
        "冷饮岗": "POSITION",
        "服务岗": "POSITION",
        "外卖岗": "POSITION",
        "收银岗": "POSITION",
        "前厅领班岗": "POSITION",
        "迎宾岗": "POSITION",
        "前厅经理岗": "POSITION",
        "领班岗": "POSITION",
        "经理岗": "POSITION",
        "春节6天补贴": "ALLOWANCE",
        "留守休息补贴": "ALLOWANCE",
        "通岗补贴": "ALLOWANCE",
        "留守出勤补贴": "ALLOWANCE",
        "留守出勤补贴 (N列)": "ALLOWANCE",
        "留守未出勤补贴": "ALLOWANCE",
        "过年未上班补贴": "ALLOWANCE",
        "春节出勤补贴": "ALLOWANCE",
        "病假补贴": "ALLOWANCE",
        "留年激励": "PERFORMANCE",
        "宿舍长奖励": "PERFORMANCE",
        "伯乐奖": "PERFORMANCE",
        "返岗激励": "PERFORMANCE",
        "奖励": "PERFORMANCE",
        "禁烟奖励": "PERFORMANCE",
        "春节激励": "PERFORMANCE",
        "刀工/其余奖罚": "PERFORMANCE",
        "绩效": "PERFORMANCE",
        "考勤扣罚": "DEDUCTION",
        "绩效扣除": "DEDUCTION",
        "扣罚": "DEDUCTION",
        "餐具破損": "DEDUCTION",
        "餐具破损": "DEDUCTION",
        "留守扣除": "DEDUCTION",
        "考勤实际扣罚": "DEDUCTION",
    }
    derived_fields = {
        "应发工资",
        "实发工资",
        "合计工资",
        "扣除后应发",
        "个人奖金系数",
        "奖金比例",
        "总奖金",
        "法定补贴",
        "法定补贴 (Y列)",
        "法定补贴 (Z列)",
        "法定补贴 (AA列)",
        "法定补贴 (AB列)",
        "法定补贴 (AC列)",
        "法定补贴 (AD列)",
        "法定补贴 (AE列)",
        "法定补贴 (AF列)",
        "法定补贴 (AG列)",
        "法定补贴 (AH列)",
        "法定补贴 (AI列)",
    }
    non_amount_fields = {
        "加班工时",
        "出勤天数",
        "出勤天数 (AM列)",
        "应出勤",
        "休息天数",
        "留年休息天数",
        "总工时",
        "工时",
        "法定出勤",
    }
    db_session.add(
        SalaryRecord(
            period="2026-05",
            emp_no="REAL-FIELD-001",
            name="Real field evidence",
            store_name="Legacy store",
            source=SalarySource.HISTORICAL,
            fields={
                "职位": "服务员",
                "综合薪资": "4300.00",
                **dict.fromkeys(expected_types, "100.00"),
                **dict.fromkeys(derived_fields, "100.00"),
                **dict.fromkeys(non_amount_fields, "8.00"),
            },
        )
    )
    db_session.flush()

    response = client.get("/api/legacy-catalog/preview", headers=headers)

    assert response.status_code == 200, response.text
    candidates = {item["source_field"]: item for item in response.json()["component_candidates"]}
    for source_field, component_type in expected_types.items():
        assert candidates[source_field]["suggested_component_type"] == component_type
        assert candidates[source_field]["classification"] == "NEEDS_HR_CONFIRMATION"
        assert candidates[source_field]["importable"] is True

    for source_field in derived_fields:
        derived = candidates[source_field]
        assert derived["suggested_component_type"] is None
        assert derived["classification"] == "DERIVED_NOT_CATALOG_COMPONENT"
        assert derived["importable"] is False

    assert non_amount_fields.isdisjoint(candidates)


def test_legacy_catalog_preview_requires_global_import_permission(client, db_session):
    _user(db_session, "legacy-catalog-employee", "EMPLOYEE")

    response = client.get(
        "/api/legacy-catalog/preview",
        headers=_headers(client, "legacy-catalog-employee"),
    )

    assert response.status_code == 403


def test_legacy_component_apply_rejects_global_import_plus_scoped_structure_write(
    client,
    db_session,
):
    user = _mixed_scope_importer(
        db_session,
        username="legacy-component-scope",
        scoped_permission=Perm.STRUCTURE_WRITE,
    )
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="SCOPE-COMPONENT",
        name="Scoped component evidence",
        position="服务员",
        comprehensive="4200.00",
    )

    response = client.post(
        "/api/legacy-catalog/components/apply",
        headers=_headers(client, user.username),
        json={
            "source_field": "综合薪资",
            "expected_record_count": 1,
            "expected_source_snapshot_id": "0" * 32,
            "confirmed_by_hr": True,
            "reason": "Scope mixing must not authorize a global catalog mutation",
            "component": {
                "code": "SCOPED_LEGACY_COMPONENT",
                "name": "Scoped legacy component",
                "component_type": "COMPREHENSIVE",
            },
        },
    )

    assert response.status_code == 403, response.text


def test_legacy_grade_apply_rejects_global_import_plus_scoped_grade_write(client, db_session):
    user = _mixed_scope_importer(
        db_session,
        username="legacy-grade-scope",
        scoped_permission=Perm.GRADE_WRITE,
    )
    for index in range(1, 6):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"SCOPE-GRADE-{index}",
            name=f"Scoped grade evidence {index}",
            position="服务员",
            comprehensive=f"{4000 + index * 100}.00",
        )

    response = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=_headers(client, user.username),
        json={
            "source_position": "服务员",
            "expected_record_count": 5,
            "expected_source_snapshot_id": "0" * 32,
            "policy_confirmation": "HR_CONFIRMED",
            "reason": "Scope mixing must not authorize a global grade mutation",
            "grade": {"code": "SCOPED-P1", "name": "Scoped P1", "rank": 1},
            "band": {
                "band_min": "3800.00",
                "band_mid": "4300.00",
                "band_max": "5000.00",
                "effective_from": "2026-07-01",
            },
        },
    )

    assert response.status_code == 403, response.text


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
    snapshot_id = _source_snapshot_id(client, headers)

    response = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json={
            "source_field": "综合薪资",
            "expected_record_count": 2,
            "expected_source_snapshot_id": snapshot_id,
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
    assert event.detail["source_snapshot_id"] == snapshot_id
    assert event.detail["component"] == {
        "id": response.json()["id"],
        "code": "COMPREHENSIVE",
        "name": "综合薪资",
        "component_type": "COMPREHENSIVE",
        "taxable": True,
        "in_social_base": True,
        "in_housing_base": True,
        "prorate_by_attendance": False,
        "allowance_kind": None,
        "sort_order": 10,
    }


def test_legacy_component_source_field_cannot_be_reapplied_under_a_different_code(
    client,
    db_session,
):
    _user(db_session, "legacy-component-once-hr", "GROUP_HR")
    headers = _headers(client, "legacy-component-once-hr")
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="SOURCE-ONCE-COMPONENT",
        name="Source once component",
        position="服务员",
        comprehensive="4200.00",
    )
    snapshot_id = _source_snapshot_id(client, headers)

    def payload(code: str) -> dict:
        return {
            "source_field": "综合薪资",
            "expected_record_count": 1,
            "expected_source_snapshot_id": snapshot_id,
            "confirmed_by_hr": True,
            "reason": "The legacy source field has one immutable catalog assignment",
            "component": {
                "code": code,
                "name": code,
                "component_type": "COMPREHENSIVE",
            },
        }

    first = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json=payload("LEGACY_SOURCE_PRIMARY"),
    )
    duplicate_source = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json=payload("LEGACY_SOURCE_ALIAS"),
    )

    assert first.status_code == 201, first.text
    assert duplicate_source.status_code == 409, duplicate_source.text
    refreshed = client.get("/api/legacy-catalog/preview", headers=headers)
    candidate = next(
        item
        for item in refreshed.json()["component_candidates"]
        if item["source_field"] == "综合薪资"
    )
    assert candidate["applied"] is True
    assert candidate["importable"] is False
    assert candidate["applied_target_id"] == first.json()["id"]


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
    snapshot_id = _source_snapshot_id(client, headers)
    payload = {
        "source_field": "综合薪资",
        "expected_record_count": 1,
        "expected_source_snapshot_id": snapshot_id,
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


def test_legacy_apply_rejects_any_source_dataset_change_since_preview(client, db_session):
    _user(db_session, "legacy-source-snapshot-hr", "GROUP_HR")
    headers = _headers(client, "legacy-source-snapshot-hr")
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="SNAPSHOT-1",
        name="Snapshot one",
        position="服务员",
        comprehensive="4200.00",
    )
    stale_snapshot_id = _source_snapshot_id(client, headers)
    _legacy_record(
        db_session,
        period="2026-05",
        emp_no="SNAPSHOT-2",
        name="Snapshot two",
        position="厨工",
        comprehensive="5000.00",
    )

    response = client.post(
        "/api/legacy-catalog/components/apply",
        headers=headers,
        json={
            "source_field": "综合薪资",
            "expected_record_count": 1,
            "expected_source_snapshot_id": stale_snapshot_id,
            "confirmed_by_hr": True,
            "reason": "Must review the complete changed source again",
            "component": {
                "code": "STALE_SNAPSHOT",
                "name": "Stale snapshot",
                "component_type": "COMPREHENSIVE",
            },
        },
    )

    assert response.status_code == 409, response.text
    assert "refresh" in response.json()["detail"].lower()


def test_confirmed_grade_import_keeps_historical_observations_distinct_from_policy_band(
    client, db_session
):
    hr = _user(db_session, "legacy-grade-hr", "GROUP_HR")
    headers = _headers(client, hr.username)
    for index, salary in enumerate(
        ("4000.00", "4100.00", "4200.00", "4300.00", "4400.00"), start=1
    ):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"GRADE-SOURCE-{index}",
            name=f"Grade source {index}",
            position="服务员",
            comprehensive=salary,
        )
    snapshot_id = _source_snapshot_id(client, headers)

    response = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=headers,
        json={
            "source_position": "服务员",
            "expected_record_count": 5,
            "expected_source_snapshot_id": snapshot_id,
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
        "record_count": 5,
        "contributor_count": 5,
        "salary_sample_count": 5,
        "observed_median": "4200.00",
    }
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "legacy_catalog.grade.apply")
    ).one()
    assert event.detail["source_position"] == "服务员"
    assert event.detail["contributor_count"] == 5
    assert event.detail["observed_median"] == "4200.00"
    assert event.detail["source_snapshot_id"] == snapshot_id
    assert event.detail["period_from"] == "2026-05"
    assert event.detail["period_to"] == "2026-05"
    assert event.detail["grade"] == {
        "id": response.json()["grade"]["id"],
        "code": "STORE-P1",
        "name": "门店一职级",
        "rank": 10,
        "version": 1,
    }
    assert event.detail["policy_band"] == {
        "band_min": "3800.00",
        "band_mid": "4300.00",
        "band_max": "5000.00",
        "effective_from": "2026-07-01",
    }


def test_legacy_position_cannot_be_reapplied_under_a_different_grade_code(client, db_session):
    _user(db_session, "legacy-grade-once-hr", "GROUP_HR")
    headers = _headers(client, "legacy-grade-once-hr")
    for index in range(1, 6):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"SOURCE-ONCE-GRADE-{index}",
            name=f"Source once grade {index}",
            position="服务员",
            comprehensive=f"{4000 + index * 100}.00",
        )
    snapshot_id = _source_snapshot_id(client, headers)

    def payload(code: str, effective_from: str) -> dict:
        return {
            "source_position": "服务员",
            "expected_record_count": 5,
            "expected_source_snapshot_id": snapshot_id,
            "policy_confirmation": "HR_CONFIRMED",
            "reason": "The legacy position has one immutable grade assignment",
            "grade": {"code": code, "name": code, "rank": 1},
            "band": {
                "band_min": "3800.00",
                "band_mid": "4300.00",
                "band_max": "5000.00",
                "effective_from": effective_from,
            },
        }

    first = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=headers,
        json=payload("LEGACY-GRADE-PRIMARY", "2026-07-01"),
    )
    duplicate_source = client.post(
        "/api/legacy-catalog/grades/apply",
        headers=headers,
        json=payload("LEGACY-GRADE-ALIAS", "2026-08-01"),
    )

    assert first.status_code == 201, first.text
    assert duplicate_source.status_code == 409, duplicate_source.text
    refreshed = client.get("/api/legacy-catalog/preview", headers=headers)
    candidate = next(
        item for item in refreshed.json()["grade_candidates"] if item["position"] == "服务员"
    )
    assert candidate["applied"] is True
    assert candidate["applied_target_id"] == first.json()["grade"]["id"]


def test_grade_import_requires_explicit_policy_confirmation_and_current_source_count(
    client, db_session
):
    _user(db_session, "legacy-grade-guard-hr", "GROUP_HR")
    headers = _headers(client, "legacy-grade-guard-hr")
    for index in range(1, 6):
        _legacy_record(
            db_session,
            period="2026-05",
            emp_no=f"GRADE-GUARD-{index}",
            name=f"Grade guard {index}",
            position="服务员",
            comprehensive=f"{4000 + index * 100}.00",
        )
    snapshot_id = _source_snapshot_id(client, headers)
    base_payload = {
        "source_position": "服务员",
        "expected_record_count": 5,
        "expected_source_snapshot_id": snapshot_id,
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
    stale_source = {**base_payload, "expected_record_count": 6}

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
    assert response.json()["grade_candidates"] == []
    assert "店长" not in response.text
    assert "服务员" not in response.text
