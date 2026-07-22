"""用户级薪资复核范围管理。"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope, revoke_all_for_user
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.dingtalk.org_freshness import invalidate_applied_reviewer_proofs
from app.dingtalk.org_sync import take_organization_sync_lock
from app.dingtalk.read_sync import blind_index_dingtalk_user_id
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit

router = APIRouter(prefix="/api/users", tags=["users"])


def _require_global_user_manager(
    principal: Principal = Depends(require_permission(Perm.USER_MANAGE)),
    session: Session = Depends(get_session),
) -> Principal:
    """Account administration is global by design, never a store-scoped grant."""
    if resolve_permission_org_scope(session, principal, Perm.USER_MANAGE) is not None:
        raise HTTPException(
            status_code=403, detail="user management requires global organization scope"
        )
    return principal


class ReviewScopeBody(BaseModel):
    org_unit_id: int = Field(gt=0)
    department: Department


class ReviewScopeReplaceBody(BaseModel):
    scopes: list[ReviewScopeBody]

    @model_validator(mode="after")
    def no_duplicate_scopes(self) -> ReviewScopeReplaceBody:
        pairs = {(scope.org_unit_id, scope.department) for scope in self.scopes}
        if len(pairs) != len(self.scopes):
            raise ValueError("复核范围中不能重复同一门店和部门")
        return self


class ReviewScopeOut(BaseModel):
    org_unit_id: int
    department: Department

    model_config = {"from_attributes": True}


class UserSummaryOut(BaseModel):
    id: int
    username: str
    status: str
    employee_id: int | None
    dingtalk_recipient_configured: bool
    login_enabled: bool
    roles: list[str]
    review_scopes: list[ReviewScopeOut]


@router.get("", response_model=list[UserSummaryOut])
def list_users(
    _principal: Principal = Depends(_require_global_user_manager),
    session: Session = Depends(get_session),
) -> list[UserSummaryOut]:
    """List non-sensitive account metadata needed to assign review scopes.

    User management is global-admin-only under the seeded RBAC model.  Roles
    and explicit reviewer assignments are fetched in bounded set queries, not
    by lazily loading relationships for every account.
    """
    users = list(
        session.scalars(
            select(User).where(User.is_deleted.is_(False)).order_by(User.username, User.id)
        ).all()
    )
    if not users:
        return []
    user_ids = [user.id for user in users]
    roles_by_user: dict[int, list[str]] = defaultdict(list)
    for user_id, role_code in session.execute(
        select(UserRole.user_id, Role.code)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
        .order_by(UserRole.user_id, Role.code)
    ).all():
        roles_by_user[user_id].append(role_code)
    scopes_by_user: dict[int, list[ReviewScopeOut]] = defaultdict(list)
    for user_id, org_unit_id, department in session.execute(
        select(
            UserReviewScope.user_id,
            UserReviewScope.org_unit_id,
            UserReviewScope.department,
        )
        .where(UserReviewScope.user_id.in_(user_ids))
        .order_by(
            UserReviewScope.user_id,
            UserReviewScope.org_unit_id,
            UserReviewScope.department,
        )
    ).all():
        scopes_by_user[user_id].append(
            ReviewScopeOut(org_unit_id=org_unit_id, department=department)
        )
    return [
        UserSummaryOut(
            id=user.id,
            username=user.username,
            status=user.status,
            employee_id=user.employee_id,
            dingtalk_recipient_configured=user.dingtalk_user_id is not None,
            login_enabled=user.login_enabled,
            roles=roles_by_user[user.id],
            review_scopes=scopes_by_user[user.id],
        )
        for user in users
    ]


def _target_user_or_404(session: Session, user_id: int, *, for_update: bool = False) -> User:
    statement = select(User).where(User.id == user_id)
    if for_update:
        statement = statement.with_for_update()
    user = session.scalars(statement).first()
    if user is None or user.is_deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _ensure_active_store_scopes(session: Session, scopes: list[ReviewScopeBody]) -> None:
    org_ids = {scope.org_unit_id for scope in scopes}
    if not org_ids:
        return
    stores = set(
        session.scalars(
            select(OrgUnit.id)
            .where(
                OrgUnit.id.in_(org_ids),
                OrgUnit.is_deleted.is_(False),
                OrgUnit.type == OrgType.STORE,
            )
            .with_for_update()
        ).all()
    )
    if stores != org_ids:
        raise HTTPException(status_code=422, detail="复核范围必须是有效的门店组织")


@router.get("/{user_id}/review-scopes", response_model=list[ReviewScopeOut])
def list_review_scopes(
    user_id: int,
    _principal: Principal = Depends(_require_global_user_manager),
    session: Session = Depends(get_session),
) -> list[UserReviewScope]:
    _target_user_or_404(session, user_id)
    return list(
        session.scalars(
            select(UserReviewScope)
            .where(UserReviewScope.user_id == user_id)
            .order_by(UserReviewScope.org_unit_id, UserReviewScope.department)
        ).all()
    )


@router.put("/{user_id}/review-scopes", response_model=list[ReviewScopeOut])
def replace_review_scopes(
    user_id: int,
    body: ReviewScopeReplaceBody,
    principal: Principal = Depends(_require_global_user_manager),
    session: Session = Depends(get_session),
) -> list[UserReviewScope]:
    # Serializing replacements on the durable user row prevents two writers
    # from each deleting an empty/old scope set and accidentally committing the
    # union of their requested scopes.
    take_organization_sync_lock(session)
    _target_user_or_404(session, user_id, for_update=True)
    _ensure_active_store_scopes(session, body.scopes)
    before_rows = list(
        session.scalars(
            select(UserReviewScope)
            .where(UserReviewScope.user_id == user_id)
            .order_by(UserReviewScope.org_unit_id, UserReviewScope.department)
        ).all()
    )
    before = [
        {"org_unit_id": row.org_unit_id, "department": row.department.value} for row in before_rows
    ]
    session.execute(delete(UserReviewScope).where(UserReviewScope.user_id == user_id))
    rows = [
        UserReviewScope(
            user_id=user_id,
            org_unit_id=scope.org_unit_id,
            department=scope.department,
        )
        for scope in body.scopes
    ]
    session.add_all(rows)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="同一门店和部门只能配置一位负责人",
        ) from None
    before_scopes = {(row.org_unit_id, row.department) for row in before_rows}
    after_scopes = {(row.org_unit_id, row.department) for row in rows}
    invalidated_proof_count = 0
    if before_scopes != after_scopes:
        invalidated_proof_count = invalidate_applied_reviewer_proofs(
            session,
            scopes=before_scopes | after_scopes,
        )
    audit.record(
        session,
        action="user.review_scope.replace",
        actor=(principal.user_id, principal.username),
        target_type="app_user",
        target_id=user_id,
        detail={
            "before": before,
            "after": [
                {"org_unit_id": row.org_unit_id, "department": row.department.value} for row in rows
            ],
            "invalidated_sync_proof_count": invalidated_proof_count,
        },
    )
    session.commit()
    return rows


class DingTalkRecipientBody(BaseModel):
    dingtalk_user_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def normalize_recipient(self) -> DingTalkRecipientBody:
        if self.dingtalk_user_id is not None:
            normalized = self.dingtalk_user_id.strip()
            if not normalized:
                raise ValueError("钉钉 userid 不能为空；如需清除请提交 null")
            self.dingtalk_user_id = normalized
        return self


class DingTalkRecipientOut(BaseModel):
    configured: bool


class UserLoginEnabledBody(BaseModel):
    model_config = {"extra": "forbid"}

    login_enabled: bool


class UserLoginEnabledOut(BaseModel):
    login_enabled: bool


@router.put("/{user_id}/login-enabled", response_model=UserLoginEnabledOut)
def replace_login_enabled(
    user_id: int,
    body: UserLoginEnabledBody,
    principal: Principal = Depends(_require_global_user_manager),
    session: Session = Depends(get_session),
) -> UserLoginEnabledOut:
    """Make a reviewer DingTalk-only without disabling notification routing."""

    if user_id == principal.user_id and not body.login_enabled:
        raise HTTPException(status_code=409, detail="You cannot disable your own login.")
    user = _target_user_or_404(session, user_id, for_update=True)
    before = user.login_enabled
    user.login_enabled = body.login_enabled
    if not body.login_enabled:
        revoke_all_for_user(session, user.id)
    audit.record(
        session,
        action="user.login_enabled.replace",
        actor=(principal.user_id, principal.username),
        target_type="app_user",
        target_id=user.id,
        detail={"before": before, "after": body.login_enabled},
    )
    session.commit()
    return UserLoginEnabledOut(login_enabled=user.login_enabled)


@router.put("/{user_id}/dingtalk-recipient", response_model=DingTalkRecipientOut)
def replace_dingtalk_recipient(
    user_id: int,
    body: DingTalkRecipientBody,
    principal: Principal = Depends(_require_global_user_manager),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> DingTalkRecipientOut:
    """Replace an encrypted provider userid without ever reading it back via API."""

    # Take the same advisory lock as organization confirmation before any row
    # lock.  Then inspect only non-reversible identity digests/booleans for
    # other accounts; do not bulk-decrypt provider userids merely to compare.
    take_organization_sync_lock(session)
    user = _target_user_or_404(session, user_id, for_update=True)
    before_configured = user.dingtalk_user_id is not None
    old_hash = user.dingtalk_user_id_hash
    identity_changed = body.dingtalk_user_id != user.dingtalk_user_id
    if identity_changed:
        has_review_scope = session.scalar(
            select(UserReviewScope.id).where(UserReviewScope.user_id == user.id).limit(1)
        )
        if has_review_scope is not None:
            raise HTTPException(
                status_code=409,
                detail="已配置工资复核范围的负责人只能通过钉钉组织同步换绑",
            )
    new_hash = (
        blind_index_dingtalk_user_id(
            body.dingtalk_user_id,
            key=settings.encryption_key,
        )
        if body.dingtalk_user_id is not None
        else None
    )
    employee_bindings = session.execute(
        select(Employee.id, Employee.dingtalk_user_id_hash)
        .where((Employee.dingtalk_user_id_hash.is_not(None)) | (Employee.id == user.employee_id))
        .order_by(Employee.id)
        .with_for_update()
    ).all()
    if new_hash is not None and any(
        employee_hash == new_hash and employee_id != user.employee_id
        for employee_id, employee_hash in employee_bindings
    ):
        raise HTTPException(status_code=409, detail="该钉钉账号已绑定其他员工")
    if (
        new_hash is not None
        and user.employee_id is None
        and any(employee_hash == new_hash for _employee_id, employee_hash in employee_bindings)
    ):
        raise HTTPException(
            status_code=409, detail="该钉钉账号已绑定员工，不能用于未关联员工的账号"
        )
    account_bindings = session.execute(
        select(
            User.id,
            User.employee_id,
            User.dingtalk_user_id_hash,
            User.dingtalk_user_id.is_not(None).label("provider_id_configured"),
        )
        .where(User.id != user.id)
        .order_by(User.id)
        .with_for_update()
    ).all()
    configured_accounts = [
        account
        for account in account_bindings
        if account.provider_id_configured or account.dingtalk_user_id_hash is not None
    ]
    if body.dingtalk_user_id is not None:
        if any(
            account.provider_id_configured and account.dingtalk_user_id_hash is None
            for account in configured_accounts
        ):
            raise HTTPException(
                status_code=409,
                detail="存在未完成迁移的钉钉账号绑定，请先修复身份索引",
            )
        if any(account.dingtalk_user_id_hash == new_hash for account in configured_accounts):
            raise HTTPException(status_code=409, detail="该钉钉账号已绑定其他用户")
    if user.employee_id is not None:
        employee = session.scalars(
            select(Employee).where(Employee.id == user.employee_id).with_for_update()
        ).one_or_none()
        if employee is None or employee.is_deleted:
            raise HTTPException(status_code=409, detail="关联员工不存在")
        sibling_accounts = [
            account for account in configured_accounts if account.employee_id == user.employee_id
        ]
        if identity_changed and sibling_accounts:
            raise HTTPException(
                status_code=409,
                detail="同一员工存在另一个已配置钉钉身份的账号，请先清理账号绑定",
            )
        if identity_changed:
            if employee.dingtalk_user_id_hash not in {
                None,
                old_hash,
                new_hash,
            }:
                raise HTTPException(
                    status_code=409,
                    detail="员工钉钉身份与账号不一致，请通过组织同步修正",
                )
            employee.dingtalk_user_id_hash = new_hash
    user.dingtalk_user_id = body.dingtalk_user_id
    user.dingtalk_user_id_hash = new_hash
    invalidated_proof_count = 0
    if identity_changed and user.employee_id is not None:
        invalidated_proof_count = invalidate_applied_reviewer_proofs(
            session,
            employee_ids={user.employee_id},
        )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="该钉钉账号已绑定其他用户") from None
    audit.record(
        session,
        action="user.dingtalk_recipient.replace",
        actor=(principal.user_id, principal.username),
        target_type="app_user",
        target_id=user.id,
        detail={
            "before_configured": before_configured,
            "after_configured": user.dingtalk_user_id is not None,
            "invalidated_sync_proof_count": invalidated_proof_count,
        },
    )
    session.commit()
    return DingTalkRecipientOut(configured=user.dingtalk_user_id is not None)
