"""认证与授权服务：认证、令牌签发/刷新/吊销、权限与组织范围解析。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.permissions import Perm
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.models.auth import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserOrgScope,
    UserReviewScope,
    UserRole,
)
from app.models.employee import Department
from app.models.org import OrgUnit


@dataclass(frozen=True)
class Principal:
    """一次请求的鉴权主体：用户 + 权限集 + 组织范围。"""

    user_id: int
    username: str
    permissions: frozenset[str]
    # None 表示不受组织范围限制（集团级角色）；否则为可见 org_unit id 集合
    org_scope: frozenset[int] | None

    def has_permission(self, perm: str) -> bool:
        return perm in self.permissions

    def is_unrestricted(self) -> bool:
        """是否不受组织范围限制（集团级角色）。"""
        return self.org_scope is None

    def visible_org_ids(self) -> frozenset[int]:
        """受限主体可见的 org_unit id 集合；对不受限主体调用属逻辑错误。

        显式区分「不受限」与「受限集合」，避免下游用 falsy 判断把二者混淆
        （None 与 空集都为 falsy，直接 `if org_scope:` 会造成 fail-open）。
        """
        if self.org_scope is None:
            raise ValueError("不受限主体没有有界的组织范围集合，请先判断 is_unrestricted()")
        return self.org_scope


class AuthError(Exception):
    """认证失败（凭据错误/账号禁用等）。"""


def get_user_by_username(session: Session, username: str) -> User | None:
    return session.scalars(
        select(User).where(User.username == username, User.is_deleted.is_(False))
    ).first()


def authenticate(session: Session, username: str, password: str) -> User:
    """校验用户名+口令。失败一律抛 AuthError（不泄露是用户不存在还是口令错）。"""
    user = get_user_by_username(session, username)
    # 用户不存在时也做一次哈希校验以抵消时序差异（防用户名枚举）
    stored_hash = user.password_hash if user else _DUMMY_HASH
    ok = verify_password(stored_hash, password)
    if not user or not ok:
        raise AuthError("用户名或密码错误")
    if user.status != "ACTIVE":
        raise AuthError("账号已禁用")
    return user


def load_permissions(session: Session, user_id: int) -> frozenset[str]:
    rows = session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
    ).all()
    return frozenset(code for (code,) in rows)


def _has_global_scope(session: Session, user_id: int) -> bool:
    return (
        session.execute(
            select(Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, Role.is_global_scope.is_(True))
            .limit(1)
        ).first()
        is not None
    )


def _has_global_permission_role(session: Session, user_id: int, permission: str) -> bool:
    """Return whether a global role grants this *specific* permission.

    ``Principal.org_scope`` is intentionally the union of every assigned role.
    It therefore cannot safely decide payroll-review visibility: a user with a
    global Finance role plus a local Store Manager role would otherwise inherit
    unrestricted store-review access from Finance even though Finance does not
    grant ``payroll:review``.
    """
    return (
        session.execute(
            select(Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                UserRole.user_id == user_id,
                Role.is_global_scope.is_(True),
                Permission.code == permission,
            )
            .limit(1)
        ).first()
        is not None
    )


def resolve_org_scope(session: Session, user_id: int) -> frozenset[int] | None:
    """返回用户可见 org_unit id 集合；None 表示不受限（全集团）。

    有效范围 = 用户 user_org_scope 根节点各自子树的闭包。
    """
    if _has_global_scope(session, user_id):
        return None
    return _assigned_org_scope(session, user_id)


def _assigned_org_scope(session: Session, user_id: int) -> frozenset[int]:
    """Resolve explicit user-org assignments without considering global roles.

    This is intentionally separate from :func:`resolve_org_scope`.  A user
    can hold a global role that grants one permission (for example
    ``audit:read``) and a scoped role that grants another (for example
    ``export:data``).  The latter must remain scoped rather than inheriting
    unrestricted visibility merely because both roles belong to the same user.
    """
    roots = [
        oid
        for (oid,) in session.execute(
            select(UserOrgScope.org_unit_id).where(UserOrgScope.user_id == user_id)
        ).all()
    ]
    if not roots:
        # No explicit scope assignment: fail closed even when an unrelated
        # global role is present on the same account.
        return frozenset()
    return _subtree_closure(session, roots)


def resolve_permission_org_scope(
    session: Session, principal: Principal, permission: str
) -> frozenset[int] | None:
    """Resolve scope for one authorization decision, fail-closed by default.

    ``None`` is returned only when a *global role that itself grants
    ``permission`` exists.  Otherwise explicit ``user_org_scope`` assignments
    determine the visible subtree, including for users who also hold unrelated
    global roles.  This prevents permission/scope mixing from widening a
    local export, dashboard, or write permission to the whole group.
    """
    if _has_global_permission_role(session, principal.user_id, permission):
        return None
    return _assigned_org_scope(session, principal.user_id)


def permission_org_scope_allows(
    session: Session,
    principal: Principal,
    permission: str,
    org_unit_id: int | None,
) -> bool:
    """Check one permission and its own organization grant as one decision.

    A principal can receive different permissions from unrelated global and
    scoped roles.  Checking ``has_permission`` and then reusing another
    permission's scope would let those grants combine into authority that no
    single role provides.  Legacy snapshots with no organization are visible
    only to a global grant for this exact permission.
    """
    if not principal.has_permission(permission):
        return False
    scope = resolve_permission_org_scope(session, principal, permission)
    return scope is None or (org_unit_id is not None and org_unit_id in scope)


def resolve_review_scope(
    session: Session, principal: Principal
) -> frozenset[tuple[int, Department]]:
    """Return the payroll-review scopes assigned to ``principal``.

    Payroll review is deliberately never global, even for a principal with a
    global organization role.  The specification requires every reviewer to
    see and act only within explicit Store/Department assignments; final HR
    approval is authorized separately by ``payroll:approve``.
    """
    rows = session.execute(
        select(UserReviewScope.org_unit_id, UserReviewScope.department)
        .join(OrgUnit, OrgUnit.id == UserReviewScope.org_unit_id)
        .where(
            UserReviewScope.user_id == principal.user_id,
            OrgUnit.is_deleted.is_(False),
        )
    ).all()
    return frozenset((org_unit_id, department) for org_unit_id, department in rows)


def resolve_payroll_read_scope(
    session: Session, principal: Principal
) -> frozenset[tuple[int, Department]] | None:
    """Return the payroll-result visibility scope for a principal.

    Global Finance/Auditor payroll-read roles are intentionally global readers.
    Store/region readers, on the other hand, are constrained to explicit review
    assignments.  This is distinct from ``resolve_review_scope`` because global
    payroll read must not grant global *review/confirmation* authority.
    """
    if _has_global_permission_role(session, principal.user_id, Perm.PAYROLL_READ):
        return None
    return resolve_review_scope(session, principal)


def _subtree_closure(session: Session, roots: list[int]) -> frozenset[int]:
    """BFS 展开子树（组织规模约百量级，一次性载入 (id,parent) 即可）。"""
    edges = session.execute(
        select(OrgUnit.id, OrgUnit.parent_id).where(OrgUnit.is_deleted.is_(False))
    ).all()
    live_ids: set[int] = {oid for oid, _pid in edges}
    children: dict[int, list[int]] = {}
    for oid, pid in edges:
        if pid is not None:
            children.setdefault(pid, []).append(oid)
    seen: set[int] = set()
    # 仅从存活的根出发；软删的范围根不纳入可见集合
    queue: deque[int] = deque(r for r in roots if r in live_ids)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(children.get(node, []))
    return frozenset(seen)


def build_principal(session: Session, user: User) -> Principal:
    return Principal(
        user_id=user.id,
        username=user.username,
        permissions=load_permissions(session, user.id),
        org_scope=resolve_org_scope(session, user.id),
    )


def issue_refresh_token(session: Session, user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    raw, digest = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=digest,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
            revoked_at=None,
            created_at=now,
        )
    )
    session.flush()
    return raw


class RefreshReuseError(AuthError):
    """重放已吊销的 refresh token（疑似盗用）；调用方应持久化随之触发的会话吊销。"""


def rotate_refresh_token(session: Session, raw_token: str) -> tuple[int, str]:
    """校验并轮换 refresh token：吊销旧的、签发新的。返回 (user_id, 新原始 token)。

    - with_for_update 行锁保证「检查-吊销」原子，杜绝并发双重轮换。
    - 重放已吊销 token 视为盗用信号：吊销该用户全部会话并抛 RefreshReuseError
      （RFC 9700 refresh token 重用检测）。
    """
    digest = hash_refresh_token(raw_token)
    rec = session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == digest).with_for_update()
    ).first()
    now = datetime.now(UTC)
    if rec is None:
        raise AuthError("refresh token 无效")
    if rec.revoked_at is not None:
        # 已被轮换/吊销的 token 再次出现 = 强泄露信号，连坐吊销该用户所有会话
        revoke_all_for_user(session, rec.user_id)
        raise RefreshReuseError("refresh token 已失效")
    if _expired(rec.expires_at, now):
        raise AuthError("refresh token 已过期")
    rec.revoked_at = now
    session.flush()
    new_raw = issue_refresh_token(session, rec.user_id)
    return rec.user_id, new_raw


def revoke_refresh_token(session: Session, raw_token: str) -> None:
    digest = hash_refresh_token(raw_token)
    rec = session.scalars(select(RefreshToken).where(RefreshToken.token_hash == digest)).first()
    if rec is not None and rec.revoked_at is None:
        rec.revoked_at = datetime.now(UTC)
        session.flush()


def revoke_all_for_user(session: Session, user_id: int) -> None:
    now = datetime.now(UTC)
    for rec in session.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    ):
        rec.revoked_at = now
    session.flush()


def access_token_for(user_id: int) -> str:
    return create_access_token(user_id)


def _expired(expires_at: datetime, now: datetime) -> bool:
    # 库中可能取回 naive datetime（取决于驱动），统一按 UTC 处理
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


# 用于用户不存在时的常量时间校验，抵消时序差异（一个合法的 Argon2 摘要）
from app.core.security import hash_password as _hash_password  # noqa: E402

_DUMMY_HASH = _hash_password("dummy-password-for-timing-safety")
