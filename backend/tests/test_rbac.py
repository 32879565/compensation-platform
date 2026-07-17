import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth.bootstrap import create_super_admin, seed_rbac
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import (
    AuthError,
    Principal,
    RefreshReuseError,
    authenticate,
    build_principal,
    issue_refresh_token,
    resolve_org_scope,
    revoke_all_for_user,
    rotate_refresh_token,
)
from app.core.security import hash_password
from app.models.auth import Role, User, UserOrgScope, UserRole
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _user(session, username, role_codes=(), scope_ids=(), password="StrongPass123!"):
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


def _org_tree(session):
    group = OrgUnit(code="G", name="集团", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    r1 = OrgUnit(code="R1", name="广州", type=OrgType.REGION, parent_id=group.id)
    r2 = OrgUnit(code="R2", name="深圳", type=OrgType.REGION, parent_id=group.id)
    session.add_all([r1, r2])
    session.flush()
    s1 = OrgUnit(code="S1", name="店1", type=OrgType.STORE, parent_id=r1.id)
    s2 = OrgUnit(code="S2", name="店2", type=OrgType.STORE, parent_id=r2.id)
    session.add_all([s1, s2])
    session.flush()
    return group, r1, r2, s1, s2


def test_global_role_unrestricted_scope(db_session):
    u = _user(db_session, "hr", ["GROUP_HR"])
    assert resolve_org_scope(db_session, u.id) is None


def test_region_scope_is_subtree_closure(db_session):
    _group, r1, _r2, s1, s2 = _org_tree(db_session)
    u = _user(db_session, "rm", ["REGION_MANAGER"], scope_ids=[r1.id])
    scope = resolve_org_scope(db_session, u.id)
    assert scope is not None
    assert r1.id in scope and s1.id in scope  # 本区域及其门店可见
    assert s2.id not in scope  # 他区域门店不可见


def test_non_global_without_scope_is_empty(db_session):
    # 非全局角色且未配置范围：可见集合为空（fail-closed）
    u = _user(db_session, "rm", ["REGION_MANAGER"], scope_ids=[])
    assert resolve_org_scope(db_session, u.id) == frozenset()


def test_soft_deleted_scope_root_excluded(db_session):
    # 范围根被软删后，其整棵子树都不应可见
    _group, r1, _r2, _s1, _s2 = _org_tree(db_session)
    u = _user(db_session, "rm", ["REGION_MANAGER"], scope_ids=[r1.id])
    r1.is_deleted = True
    db_session.flush()
    assert resolve_org_scope(db_session, u.id) == frozenset()


def test_principal_scope_helpers():
    # 不受限主体
    p_all = Principal(1, "a", frozenset(), None)
    assert p_all.is_unrestricted() is True
    with pytest.raises(ValueError):
        p_all.visible_org_ids()
    # 受限主体
    p_scoped = Principal(2, "b", frozenset(), frozenset({5, 6}))
    assert p_scoped.is_unrestricted() is False
    assert p_scoped.visible_org_ids() == frozenset({5, 6})


def test_build_principal_permissions(db_session):
    u = _user(db_session, "store", ["STORE_MANAGER"])
    p = build_principal(db_session, u)
    assert p.has_permission(Perm.ATTENDANCE_WRITE)
    assert not p.has_permission(Perm.PAYROLL_RUN)


def test_require_permission_allows_and_denies():
    p = Principal(1, "u", frozenset({Perm.EMPLOYEE_READ}), None)
    assert require_permission(Perm.EMPLOYEE_READ)(p) is p
    with pytest.raises(HTTPException) as ei:
        require_permission(Perm.PAYROLL_RUN)(p)
    assert ei.value.status_code == 403


def test_authenticate_rejects_disabled_user(db_session):
    u = _user(db_session, "x", ["EMPLOYEE"])
    u.status = "DISABLED"
    db_session.flush()
    with pytest.raises(AuthError):
        authenticate(db_session, "x", "StrongPass123!")


def test_refresh_rotation_invalidates_old(db_session):
    u = _user(db_session, "x", ["EMPLOYEE"])
    raw = issue_refresh_token(db_session, u.id)
    _uid, _new = rotate_refresh_token(db_session, raw)
    # 旧 token 已被吊销，再次使用应失败
    with pytest.raises(AuthError):
        rotate_refresh_token(db_session, raw)


def test_refresh_reuse_detection_revokes_all_sessions(db_session):
    # 重放已吊销 token 视为盗用：连坐吊销该用户全部会话（含轮换出的新 token）
    u = _user(db_session, "x", ["EMPLOYEE"])
    raw1 = issue_refresh_token(db_session, u.id)
    _uid, raw2 = rotate_refresh_token(db_session, raw1)  # raw1→raw2，raw1 被吊销
    with pytest.raises(RefreshReuseError):
        rotate_refresh_token(db_session, raw1)  # 重放 raw1，触发连坐吊销
    # 连坐后，合法轮换出来的 raw2 也应失效
    with pytest.raises(AuthError):
        rotate_refresh_token(db_session, raw2)


def test_revoke_all_invalidates_refresh(db_session):
    u = _user(db_session, "x", ["EMPLOYEE"])
    raw = issue_refresh_token(db_session, u.id)
    revoke_all_for_user(db_session, u.id)
    with pytest.raises(AuthError):
        rotate_refresh_token(db_session, raw)


def test_create_super_admin_is_idempotent_and_global(db_session):
    a1 = create_super_admin(db_session, "root", "SuperStrongPass123")
    a2 = create_super_admin(db_session, "root", "SuperStrongPass123")
    assert a1.id == a2.id  # 幂等，不重复建号
    assert resolve_org_scope(db_session, a1.id) is None  # 超管全局范围
    principal = build_principal(db_session, a1)
    assert principal.has_permission(Perm.USER_MANAGE)  # 拥有全部权限


def test_create_super_admin_rejects_weak_password(db_session):
    with pytest.raises(ValueError):
        create_super_admin(db_session, "root", "short")
