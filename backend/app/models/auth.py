from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.db.base import Base, SoftDeleteMixin, TimestampMixin
from app.models.employee import Department


class User(Base, TimestampMixin, SoftDeleteMixin):
    """登录账号。password_hash 为 Argon2 摘要（不变量8：无明文）。

    employee_id 关联员工（员工自助场景），可空（如纯管理账号）。
    """

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("dingtalk_user_id_hash", name="uq_app_user_dingtalk_user_id_hash"),
    )

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id"), nullable=True, index=True
    )
    # DingTalk's provider userid is a stable external identifier and therefore
    # treated as PII.  API responses expose only a configured/not-configured
    # capability bit; the value is decrypted only immediately before sending.
    dingtalk_user_id: Mapped[str | None] = mapped_column(EncryptedString(512), nullable=True)
    # Keyed one-way digest used for equality matching and uniqueness checks.
    # The encrypted provider identifier above is still required only at the
    # outbound API boundary and is never used as a lookup key.
    dingtalk_user_id_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    # DingTalk-only reviewers remain active notification recipients while all
    # password and refresh-token paths into the HR console reject them.
    login_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


class Role(Base, TimestampMixin):
    """角色。is_global_scope=True 表示不受组织范围限制（集团级：HR/财务/审计/超管）。"""

    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_global_scope: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class Permission(Base, TimestampMixin):
    """细粒度权限点，如 employee:read / payroll:run。"""

    __tablename__ = "permission"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class UserRole(Base):
    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"), nullable=False, index=True)


class RolePermission(Base):
    __tablename__ = "role_permission"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),)

    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"), nullable=False, index=True)
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permission.id"), nullable=False, index=True
    )


class UserOrgScope(Base):
    """用户的组织范围根节点；有效范围 = 这些根的子树闭包（is_global_scope 角色则不受限）。"""

    __tablename__ = "user_org_scope"
    __table_args__ = (UniqueConstraint("user_id", "org_unit_id", name="uq_user_org_scope"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)


class UserReviewScope(Base):
    """An explicit reviewer assignment for one organization-unit and department pair.

    Reviewer access is intentionally distinct from the broader ``user_org_scope`` tree.
    A non-global reviewer can access only rows explicitly assigned here.
    """

    __tablename__ = "user_review_scope"
    __table_args__ = (
        UniqueConstraint("user_id", "org_unit_id", "department", name="uq_user_review_scope"),
        UniqueConstraint(
            "org_unit_id",
            "department",
            name="uq_user_review_scope_org_department",
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )


class RefreshToken(Base):
    """refresh token 的 sha256 摘要 + 生命周期。支持服务端吊销（不变量：会话可失效）。"""

    __tablename__ = "refresh_token"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoginThrottleBucket(Base):
    """An opaque, short-lived, shared login-rate-limit bucket.

    The key is a domain-separated HMAC digest rather than a raw client IP or
    username.  It is deliberately ephemeral operational state, not an audit
    trail; ``expires_at`` is indexed so request-time cleanup remains bounded.
    """

    __tablename__ = "login_throttle_bucket"
    __table_args__ = (
        CheckConstraint("failure_count > 0", name="ck_login_throttle_bucket_failure_count"),
        UniqueConstraint("scope", "key_digest", name="uq_login_throttle_bucket_scope_digest"),
        Index("ix_login_throttle_bucket_expires_at", "expires_at"),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
