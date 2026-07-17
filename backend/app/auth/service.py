"""认证与授权服务：认证、令牌签发/刷新/吊销、权限与组织范围解析。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    UserRole,
)
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


def resolve_org_scope(session: Session, user_id: int) -> frozenset[int] | None:
    """返回用户可见 org_unit id 集合；None 表示不受限（全集团）。

    有效范围 = 用户 user_org_scope 根节点各自子树的闭包。
    """
    if _has_global_scope(session, user_id):
        return None
    roots = [
        oid
        for (oid,) in session.execute(
            select(UserOrgScope.org_unit_id).where(UserOrgScope.user_id == user_id)
        ).all()
    ]
    if not roots:
        # 无全局角色且无范围配置：可见范围为空（fail-closed，什么也看不到）
        return frozenset()
    return _subtree_closure(session, roots)


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
