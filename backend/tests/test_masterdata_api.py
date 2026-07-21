import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserOrgScope, UserRole
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _org_tree(session):
    group = OrgUnit(code="G", name="集团", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    gz = OrgUnit(code="R_GZ", name="广州", type=OrgType.REGION, parent_id=group.id)
    sz = OrgUnit(code="R_SZ", name="深圳", type=OrgType.REGION, parent_id=group.id)
    session.add_all([gz, sz])
    session.flush()
    gz_store = OrgUnit(
        code="S_GZ1", name="广州店", type=OrgType.STORE, parent_id=gz.id, city="广州"
    )
    sz_store = OrgUnit(
        code="S_SZ1", name="深圳店", type=OrgType.STORE, parent_id=sz.id, city="深圳"
    )
    session.add_all([gz_store, sz_store])
    session.flush()
    return {"group": group, "gz": gz, "sz": sz, "gz_store": gz_store, "sz_store": sz_store}


def _user(session, username, role_codes, scope_ids=(), password="StrongPass123!"):
    seed_rbac(session)
    u = User(username=username, password_hash=hash_password(password))
    session.add(u)
    session.flush()
    for code in role_codes:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=u.id, role_id=role.id))
    for oid in scope_ids:
        session.add(UserOrgScope(user_id=u.id, org_unit_id=oid))
    session.flush()
    return u


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client, username, password="StrongPass123!"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_hr_can_create_and_list_employee_with_pii(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "E001",
            "name": "张三",
            "org_unit_id": orgs["gz_store"].id,
            "hire_date": "2026-01-01",
            "social_city": "广州",
            "id_card": "440101199001011234",
        },
    )
    assert r.status_code == 201
    # GROUP_HR 有 employee:pii，返回全量证件
    assert r.json()["id_card"] == "440101199001011234"

    lst = client.get("/api/employees", headers=h)
    assert lst.status_code == 200
    assert lst.json()["total"] == 1


def test_employee_create_requires_hire_date(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")

    response = client.post(
        "/api/employees",
        headers=headers,
        json={
            "emp_no": "NO-HIRE-DATE",
            "name": "Missing lifecycle date",
            "org_unit_id": orgs["gz_store"].id,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("invalid_dates", "field"),
    [
        ({"probation_end": "2025-12-31"}, "probation_end"),
        ({"leave_date": "2025-12-31"}, "leave_date"),
    ],
)
def test_employee_create_rejects_lifecycle_dates_before_hire_date(
    client, db_session, invalid_dates, field
):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")

    response = client.post(
        "/api/employees",
        headers=headers,
        json={
            "emp_no": f"INVALID-{field}",
            "name": "Invalid lifecycle",
            "org_unit_id": orgs["gz_store"].id,
            "hire_date": "2026-01-01",
            **invalid_dates,
        },
    )

    assert response.status_code == 422
    assert field in str(response.json()["detail"])


@pytest.mark.parametrize(
    "patch",
    [
        {"probation_end": "2025-12-31"},
        {"leave_date": "2025-12-31"},
        {"hire_date": "2026-07-01"},
    ],
)
def test_employee_update_rejects_invalid_merged_lifecycle_dates(client, db_session, patch):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no=f"INVALID-MERGED-{next(iter(patch))}",
        probation_end="2026-06-30",
        leave_date="2026-12-31",
    ).json()["id"]

    response = client.patch(f"/api/employees/{employee_id}", headers=headers, json=patch)

    assert response.status_code == 422


def test_employee_update_can_clear_optional_lifecycle_dates(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="CLEAR-OPTIONAL-DATES",
        probation_end="2026-06-30",
        leave_date="2026-12-31",
    ).json()["id"]

    response = client.patch(
        f"/api/employees/{employee_id}",
        headers=headers,
        json={"probation_end": None, "leave_date": None},
    )

    assert response.status_code == 200
    assert response.json()["probation_end"] is None
    assert response.json()["leave_date"] is None


def test_known_special_position_is_classified_automatically(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")

    response = client.post(
        "/api/employees",
        headers=headers,
        json={
            "emp_no": "SPECIAL-ROLE",
            "name": "Approved-day employee",
            "org_unit_id": orgs["gz_store"].id,
            "hire_date": "2026-01-01",
            "position_title": "储备厨师长",
            "is_special_position": False,
        },
    )

    assert response.status_code == 201
    assert response.json()["is_special_position"] is True


def test_employee_update_cannot_clear_hire_date(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="KEEP-HIRE-DATE",
    ).json()["id"]

    response = client.patch(
        f"/api/employees/{employee_id}", headers=headers, json={"hire_date": None}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "org_unit_id",
        "employment_type",
        "department",
        "is_special_position",
        "status",
    ],
)
def test_employee_update_rejects_null_for_required_master_fields(client, db_session, field):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no=f"KEEP-{field}",
    ).json()["id"]

    response = client.patch(f"/api/employees/{employee_id}", headers=headers, json={field: None})

    assert response.status_code == 422


def test_employee_update_cannot_opt_a_named_special_position_out(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="SPECIAL-UPDATE",
    ).json()["id"]

    response = client.patch(
        f"/api/employees/{employee_id}",
        headers=headers,
        json={"position_title": "寒假工", "is_special_position": False},
    )

    assert response.status_code == 200
    assert response.json()["is_special_position"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"position_title": "普通岗位"},
        {"probation_end": "2026-06-30"},
    ],
    ids=["position-title", "probation-end"],
)
def test_employee_payroll_input_update_runs_history_guard(client, db_session, monkeypatch, payload):
    """Title and probation changes can alter payroll and must share its history lock."""
    from app.routers import employee as employee_router

    orgs = _org_tree(db_session)
    _user(db_session, "hr-position-lock", ["GROUP_HR"])
    headers = _token(client, "hr-position-lock")
    employee_id = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="POSITION-LOCK",
        position_title="洗碗岗位",
    ).json()["id"]

    def _locked(*args, **kwargs):
        raise HTTPException(status_code=409, detail="payroll history is locked")

    monkeypatch.setattr(employee_router, "_ensure_employee_history_mutable", _locked)

    response = client.patch(
        f"/api/employees/{employee_id}",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]


def test_emp_no_conflict_returns_409(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    payload = {
        "emp_no": "E001",
        "name": "张三",
        "org_unit_id": orgs["gz_store"].id,
        "hire_date": "2026-01-01",
    }
    assert client.post("/api/employees", headers=h, json=payload).status_code == 201
    assert client.post("/api/employees", headers=h, json=payload).status_code == 409


def test_employee_create_with_unknown_job_grade_returns_404(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")

    response = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="UNKNOWN-GRADE-CREATE",
        job_grade_id=999_999,
    )

    assert response.status_code == 404
    assert "grade" in response.json()["detail"].lower()


def test_employee_update_with_unknown_job_grade_returns_404(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    employee = _create_emp(
        client,
        headers,
        orgs["gz_store"].id,
        emp_no="UNKNOWN-GRADE-UPDATE",
    ).json()

    response = client.patch(
        f"/api/employees/{employee['id']}",
        headers=headers,
        json={"job_grade_id": 999_999, "expected_version": employee["version"]},
    )

    assert response.status_code == 404
    assert "grade" in response.json()["detail"].lower()


def test_employee_must_belong_to_a_store(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")

    r = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "GROUP-EMP",
            "name": "Invalid scope",
            "org_unit_id": orgs["gz"].id,
            "hire_date": "2026-01-01",
        },
    )
    assert r.status_code == 422


def test_store_manager_pii_is_masked(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    _user(db_session, "mgr", ["STORE_MANAGER"], scope_ids=[orgs["gz_store"].id])
    hr = _token(client, "hr")
    client.post(
        "/api/employees",
        headers=hr,
        json={
            "emp_no": "E001",
            "name": "张三",
            "org_unit_id": orgs["gz_store"].id,
            "hire_date": "2026-01-01",
            "id_card": "440101199001011234",
        },
    )
    mgr = _token(client, "mgr")
    lst = client.get("/api/employees", headers=mgr)
    assert lst.status_code == 200
    # 店长无 employee:pii，证件必须脱敏
    assert lst.json()["items"][0]["id_card"] == f"440{'*' * 13}34"


def test_region_manager_cannot_see_other_region_employee(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    # 广州区域经理，范围只含广州区域
    _user(db_session, "gzmgr", ["REGION_MANAGER"], scope_ids=[orgs["gz"].id])
    hr = _token(client, "hr")
    # 在深圳店建一名员工
    client.post(
        "/api/employees",
        headers=hr,
        json={
            "emp_no": "SZ1",
            "name": "李四",
            "org_unit_id": orgs["sz_store"].id,
            "hire_date": "2026-01-01",
        },
    )
    gz = _token(client, "gzmgr")
    lst = client.get("/api/employees", headers=gz)
    assert lst.status_code == 200
    assert lst.json()["total"] == 0  # 深圳员工对广州经理不可见


def _make_scoped_writer_role(session):
    """自定义非全局角色：可读写员工，但受组织范围约束（用于测范围拦截）。"""
    from app.auth.permissions import Perm
    from app.models.auth import Permission, Role, RolePermission

    seed_rbac(session)
    role = Role(code="SCOPED_WRITER", name="范围写手", is_global_scope=False)
    session.add(role)
    session.flush()
    for pcode in (Perm.EMPLOYEE_READ, Perm.EMPLOYEE_WRITE):
        pid = session.scalars(select(Permission.id).where(Permission.code == pcode)).one()
        session.add(RolePermission(role_id=role.id, permission_id=pid))
    session.flush()


def _make_global_employee_operator_and_scoped_pii_roles(session):
    """Create the mixed global-read/write plus local-PII regression fixture."""
    from app.auth.permissions import Perm
    from app.models.auth import Permission, Role, RolePermission

    seed_rbac(session)
    global_operator = Role(
        code="GLOBAL_EMPLOYEE_OPERATOR",
        name="Global employee operator",
        is_global_scope=True,
    )
    scoped_pii = Role(code="SCOPED_EMPLOYEE_PII", name="Scoped employee PII", is_global_scope=False)
    session.add_all([global_operator, scoped_pii])
    session.flush()
    permission_ids = {
        code: session.scalars(select(Permission.id).where(Permission.code == code)).one()
        for code in (Perm.EMPLOYEE_READ, Perm.EMPLOYEE_WRITE, Perm.EMPLOYEE_PII)
    }
    session.add_all(
        [
            RolePermission(
                role_id=global_operator.id,
                permission_id=permission_ids[Perm.EMPLOYEE_READ],
            ),
            RolePermission(
                role_id=global_operator.id,
                permission_id=permission_ids[Perm.EMPLOYEE_WRITE],
            ),
            RolePermission(role_id=scoped_pii.id, permission_id=permission_ids[Perm.EMPLOYEE_PII]),
        ]
    )
    session.flush()


def test_scoped_pii_does_not_unmask_global_employee_reads_or_write_responses(client, db_session):
    orgs = _org_tree(db_session)
    _make_global_employee_operator_and_scoped_pii_roles(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    mixed = _user(
        db_session,
        "mixed-pii",
        ["GLOBAL_EMPLOYEE_OPERATOR", "SCOPED_EMPLOYEE_PII"],
        scope_ids=[orgs["gz"].id],
    )
    hr = _token(client, "hr")
    gz = _create_emp(
        client,
        hr,
        orgs["gz_store"].id,
        emp_no="GZ-PII",
        id_card="440101199001011234",
        bank_account="6222021234567890",
    ).json()
    sz = _create_emp(
        client,
        hr,
        orgs["sz_store"].id,
        emp_no="SZ-PII",
        id_card="440102199001011234",
        bank_account="6222029876543210",
    ).json()
    headers = _token(client, mixed.username)

    listed = client.get("/api/employees", headers=headers)
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[gz["id"]]["id_card"] == "440101199001011234"
    assert by_id[sz["id"]]["id_card"] != "440102199001011234"

    # Global employee:read does make the record reachable, but local PII does
    # not reveal the remote record through a direct fetch either.
    assert client.get(f"/api/employees/{sz['id']}", headers=headers).json()["id_card"] != (
        "440102199001011234"
    )

    created = _create_emp(
        client,
        headers,
        orgs["sz_store"].id,
        emp_no="SZ-WRITE-PII",
        id_card="440103199001011234",
    )
    assert created.status_code == 403
    assert "PII" in created.json()["detail"]
    updated = client.patch(f"/api/employees/{sz['id']}", headers=headers, json={"name": "Updated"})
    assert updated.status_code == 200
    assert updated.json()["id_card"] != "440102199001011234"
    pii_update = client.patch(
        f"/api/employees/{sz['id']}",
        headers=headers,
        json={"bank_account": "6222020000000000"},
    )
    assert pii_update.status_code == 403


def test_scoped_writer_cannot_create_in_unseen_org(client, db_session):
    orgs = _org_tree(db_session)
    _make_scoped_writer_role(db_session)
    _user(db_session, "gzw", ["SCOPED_WRITER"], scope_ids=[orgs["gz"].id])
    h = _token(client, "gzw")
    # 在可见的广州店建人 → 成功
    ok = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "GZ1",
            "name": "赵六",
            "org_unit_id": orgs["gz_store"].id,
            "hire_date": "2026-01-01",
        },
    )
    assert ok.status_code == 201
    # 试图在不可见的深圳店建人 → 404（组织范围拦截，防越权写）
    blocked = client.post(
        "/api/employees",
        headers=h,
        json={
            "emp_no": "SZ2",
            "name": "王五",
            "org_unit_id": orgs["sz_store"].id,
            "hire_date": "2026-01-01",
        },
    )
    assert blocked.status_code == 404


def test_global_role_not_restricted_by_scope(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    hr = _token(client, "hr")
    r = client.post(
        "/api/employees",
        headers=hr,
        json={
            "emp_no": "SZ3",
            "name": "王五",
            "org_unit_id": orgs["sz_store"].id,
            "hire_date": "2026-01-01",
        },
    )
    assert r.status_code == 201  # 全局角色不受组织范围限制


def test_employee_read_requires_permission(client, db_session):
    _org_tree(db_session)
    _user(db_session, "emp", ["EMPLOYEE"])  # 只有 payslip:read:self
    h = _token(client, "emp")
    assert client.get("/api/employees", headers=h).status_code == 403


def test_org_tree_scoped_to_region(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "gzmgr", ["REGION_MANAGER"], scope_ids=[orgs["gz"].id])
    h = _token(client, "gzmgr")
    tree = client.get("/api/org/tree", headers=h)
    assert tree.status_code == 200
    codes = {n["code"] for n in tree.json()}
    # 可见森林根是广州区域；深圳不可见
    assert "R_GZ" in codes
    assert "R_SZ" not in codes


def test_org_update_parent_cycle_rejected(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    # 把广州区域的父设成它自己的下级门店 → 成环，应 400
    r = client.patch(
        f"/api/org/{orgs['gz'].id}", headers=h, json={"parent_id": orgs["gz_store"].id}
    )
    assert r.status_code == 400


def test_grade_crud(client, db_session):
    _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.post("/api/grades", headers=h, json={"code": "P3", "name": "三级", "rank": 3})
    assert r.status_code == 201
    gid = r.json()["id"]
    band = client.post(
        f"/api/grades/{gid}/bands",
        headers=h,
        json={
            "job_grade_id": gid,
            "band_min": "3000.00",
            "band_mid": "4500.00",
            "band_max": "6000.00",
            "effective_from": "2026-01-01",
        },
    )
    assert band.status_code == 201
    assert client.get(f"/api/grades/{gid}/bands", headers=h).json()[0]["band_mid"] == "4500.00"


def test_band_invalid_range_rejected(client, db_session):
    _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    gid = client.post("/api/grades", headers=h, json={"code": "P4", "name": "四级"}).json()["id"]
    r = client.post(
        f"/api/grades/{gid}/bands",
        headers=h,
        json={
            "job_grade_id": gid,
            "band_min": "6000.00",
            "band_mid": "4500.00",
            "band_max": "3000.00",
            "effective_from": "2026-01-01",
        },
    )
    assert r.status_code == 400


def _create_emp(client, headers, org_id, emp_no="E001", **extra):
    return client.post(
        "/api/employees",
        headers=headers,
        json={
            "emp_no": emp_no,
            "name": "张三",
            "org_unit_id": org_id,
            "hire_date": "2026-01-01",
            **extra,
        },
    )


def test_get_employee_by_id_and_scope_hidden(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    _user(db_session, "gzmgr", ["REGION_MANAGER"], scope_ids=[orgs["gz"].id])
    hr = _token(client, "hr")
    eid = _create_emp(client, hr, orgs["sz_store"].id, emp_no="SZ9").json()["id"]
    # 全局 HR 可取到
    assert client.get(f"/api/employees/{eid}", headers=hr).status_code == 200
    # 广州区域经理取深圳员工 → 404（越权不可达）
    gz = _token(client, "gzmgr")
    assert client.get(f"/api/employees/{eid}", headers=gz).status_code == 404


def test_update_employee_changes_field(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    hr = _token(client, "hr")
    eid = _create_emp(client, hr, orgs["gz_store"].id).json()["id"]
    r = client.patch(f"/api/employees/{eid}", headers=hr, json={"name": "李四"})
    assert r.status_code == 200
    assert r.json()["name"] == "李四"
    assert r.json()["version"] == 2


def test_update_employee_transfer_to_unseen_org_blocked(client, db_session):
    orgs = _org_tree(db_session)
    _make_scoped_writer_role(db_session)
    _user(db_session, "gzw", ["SCOPED_WRITER"], scope_ids=[orgs["gz"].id])
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "gzw")
    eid = _create_emp(client, h, orgs["gz_store"].id, emp_no="GZ5").json()["id"]
    # 试图把员工转到不可见的深圳店 → 404
    r = client.patch(f"/api/employees/{eid}", headers=h, json={"org_unit_id": orgs["sz_store"].id})
    assert r.status_code == 404

    global_headers = _token(client, "hr")
    transferred = client.patch(
        f"/api/employees/{eid}",
        headers=global_headers,
        json={"org_unit_id": orgs["sz_store"].id},
    )
    assert transferred.status_code == 200
    stale_scoped_edit = client.patch(
        f"/api/employees/{eid}", headers=h, json={"name": "No longer visible"}
    )
    assert stale_scoped_edit.status_code == 404


def test_delete_employee_soft(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    hr = _token(client, "hr")
    eid = _create_emp(client, hr, orgs["gz_store"].id).json()["id"]
    assert client.delete(f"/api/employees/{eid}", headers=hr).status_code == 204
    # 软删后列表不再出现
    assert client.get("/api/employees", headers=hr).json()["total"] == 0


def test_org_create_update_delete(client, db_session):
    orgs = _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    hr = _token(client, "hr")
    created = client.post(
        "/api/org",
        headers=hr,
        json={
            "code": "NEW",
            "name": "新店",
            "type": "STORE",
            "parent_id": orgs["gz"].id,
            "city": "广州",
        },
    )
    assert created.status_code == 201
    nid = created.json()["id"]
    upd = client.patch(f"/api/org/{nid}", headers=hr, json={"name": "新店改名"})
    assert upd.status_code == 200
    assert upd.json()["name"] == "新店改名"
    assert client.delete(f"/api/org/{nid}", headers=hr).status_code == 204


def test_org_duplicate_code_409(client, db_session):
    _org_tree(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    hr = _token(client, "hr")
    body = {"code": "DUPE", "name": "A", "type": "STORE"}
    assert client.post("/api/org", headers=hr, json=body).status_code == 201
    assert client.post("/api/org", headers=hr, json=body).status_code == 409
