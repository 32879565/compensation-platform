"""RBAC 种子 + 首个超级管理员创建（fail-closed 下无默认账号，需一次性引导）。

用法：
    COMP_BOOTSTRAP_PASSWORD=... python -m app.auth.bootstrap --username admin
口令从环境变量读取，不回显、不入命令行历史。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.permissions import PERMISSION_CATALOG, ROLE_DEFINITIONS
from app.core.security import hash_password
from app.models.auth import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)


def seed_rbac(session: Session) -> None:
    """幂等写入权限点、角色、角色-权限映射。"""
    perm_by_code: dict[str, Permission] = {
        p.code: p for p in session.scalars(select(Permission)).all()
    }
    for code, name in PERMISSION_CATALOG.items():
        if code not in perm_by_code:
            perm = Permission(code=code, name=name)
            session.add(perm)
            perm_by_code[code] = perm
    session.flush()

    role_by_code: dict[str, Role] = {r.code: r for r in session.scalars(select(Role)).all()}
    for rd in ROLE_DEFINITIONS:
        role = role_by_code.get(rd.code)
        if role is None:
            role = Role(code=rd.code, name=rd.name, is_global_scope=rd.is_global_scope)
            session.add(role)
            session.flush()
            role_by_code[rd.code] = role
        else:
            role.name = rd.name
            role.is_global_scope = rd.is_global_scope

        existing = {
            pid
            for (pid,) in session.execute(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            ).all()
        }
        for pcode in dict.fromkeys(rd.permissions):  # 去重，防止定义里重复权限点
            perm = perm_by_code[pcode]
            if perm.id not in existing:
                session.add(RolePermission(role_id=role.id, permission_id=perm.id))
                existing.add(perm.id)
    session.flush()


def create_super_admin(session: Session, username: str, password: str) -> User:
    """创建（或复用）超级管理员并授予 SUPER_ADMIN 角色。"""
    if not password or len(password) < 12:
        raise ValueError("超级管理员口令至少 12 位")
    seed_rbac(session)
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
        session.flush()
    admin_role = session.scalars(select(Role).where(Role.code == "SUPER_ADMIN")).one()
    has_role = session.execute(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == admin_role.id)
    ).first()
    if not has_role:
        session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    session.flush()
    return user


def main() -> None:  # pragma: no cover - CLI 入口
    import argparse
    import os
    import sys

    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description="RBAC 种子 + 创建超级管理员")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()

    password = os.environ.get("COMP_BOOTSTRAP_PASSWORD", "")
    if not password:
        print("请通过环境变量 COMP_BOOTSTRAP_PASSWORD 提供口令（≥12 位）", file=sys.stderr)
        sys.exit(1)

    with SessionLocal() as session:
        create_super_admin(session, args.username, password)
        session.commit()
    print(f"已创建/更新超级管理员：{args.username}，并完成 RBAC 种子。")


if __name__ == "__main__":  # pragma: no cover
    main()
