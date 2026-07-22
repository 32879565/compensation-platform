from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.dingtalk.org_freshness import (
    invalidate_reviewer_authorization,
    lock_reviewer_authorization_users,
    reviewer_authorization_user_ids,
)
from app.dingtalk.org_sync import take_organization_sync_lock
from app.models.employee import Department
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import PayrollBatch
from app.models.payroll_result import BatchConfirmation, PayrollResult
from app.payroll.guards import lock_payroll_input_mutation
from app.repositories.org import OrgUnitRepository
from app.schemas.org import OrgTreeNode, OrgUnitCreate, OrgUnitOut, OrgUnitUpdate

router = APIRouter(prefix="/api/org", tags=["org"])


def _lock_org_units(session: Session, org_unit_ids: set[int]) -> dict[int, OrgUnit]:
    if not org_unit_ids:
        return {}
    rows = session.scalars(
        select(OrgUnit)
        .where(OrgUnit.id.in_(org_unit_ids))
        .order_by(OrgUnit.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.id: row for row in rows}


def _repo(session: Session, principal: Principal, permission: str) -> OrgUnitRepository:
    return OrgUnitRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, permission),
    )


def _ensure_org_not_in_active_payroll(session: Session, org_unit_id: int) -> None:
    """Keep an active review scope reachable until its batch is settled.

    Review-scope resolution deliberately hides soft-deleted stores.  Payroll
    results remain eligible for a future correction-round rerun even after
    lock, so deleting their store would either hide a required review action or
    make that rerun impossible.  Stores with any payroll history are retained
    as master-data records instead of being soft-deleted.
    """
    lock_payroll_input_mutation(session)
    session.scalars(select(PayrollBatch.id).with_for_update()).all()
    blocked = session.scalar(
        select(PayrollBatch.id)
        .outerjoin(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .outerjoin(BatchConfirmation, BatchConfirmation.batch_id == PayrollBatch.id)
        .where(
            or_(
                PayrollResult.org_unit_id == org_unit_id,
                BatchConfirmation.org_unit_id == org_unit_id,
            ),
        )
        .limit(1)
    )
    if blocked is not None:
        raise HTTPException(
            status_code=409,
            detail="参与未结算薪资批次的门店不能删除；请先完成或更正该批次",
        )


@router.get("/tree", response_model=list[OrgTreeNode])
def get_tree(
    principal: Principal = Depends(require_permission(Perm.ORG_READ)),
    session: Session = Depends(get_session),
) -> list[OrgTreeNode]:
    units = _repo(session, principal, Perm.ORG_READ).all_visible()

    def _node(u: OrgUnit) -> OrgTreeNode:
        # 只取标量字段构造，绝不经 ORM children 关系（会绕过组织范围拉全量后代）
        return OrgTreeNode(
            id=u.id,
            code=u.code,
            name=u.name,
            type=u.type,
            parent_id=u.parent_id,
            city=u.city,
            status=u.status,
        )

    nodes = {u.id: _node(u) for u in units}
    roots: list[OrgTreeNode] = []
    for u in units:
        node = nodes[u.id]
        parent = nodes.get(u.parent_id) if u.parent_id is not None else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)  # 父不在可见集合内 → 作为可见森林的根
    return roots


@router.get("", response_model=list[OrgUnitOut])
def list_units(
    principal: Principal = Depends(require_permission(Perm.ORG_READ)),
    session: Session = Depends(get_session),
) -> list[OrgUnit]:
    return _repo(session, principal, Perm.ORG_READ).all_visible()


@router.post("", response_model=OrgUnitOut, status_code=status.HTTP_201_CREATED)
def create_unit(
    body: OrgUnitCreate,
    principal: Principal = Depends(require_permission(Perm.ORG_WRITE)),
    session: Session = Depends(get_session),
) -> OrgUnit:
    take_organization_sync_lock(session)
    repo = _repo(session, principal, Perm.ORG_WRITE)
    if body.parent_id is not None and repo.get(body.parent_id) is None:
        raise HTTPException(status_code=404, detail="上级组织不存在或不可见")
    if body.parent_id is not None:
        locked_parent = _lock_org_units(session, {body.parent_id}).get(body.parent_id)
        if locked_parent is None or locked_parent.is_deleted:
            raise HTTPException(status_code=404, detail="上级组织不存在或不可见")
    unit = OrgUnit(
        code=body.code,
        name=body.name,
        type=body.type,
        parent_id=body.parent_id,
        city=body.city,
    )
    try:
        repo.add(unit)
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="组织编码已存在") from None
    audit.record(
        session,
        action="org.create",
        actor=(principal.user_id, principal.username),
        target_type="org_unit",
        target_id=unit.id,
        detail={"code": unit.code, "type": unit.type.value},
    )
    session.commit()
    return unit


@router.patch("/{unit_id}", response_model=OrgUnitOut)
def update_unit(
    unit_id: int,
    body: OrgUnitUpdate,
    principal: Principal = Depends(require_permission(Perm.ORG_WRITE)),
    session: Session = Depends(get_session),
) -> OrgUnit:
    take_organization_sync_lock(session)
    repo = _repo(session, principal, Perm.ORG_WRITE)
    unit = repo.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="组织不存在或不可见")
    data = body.model_dump(exclude_unset=True)
    routing_changed = any(
        field in data and data[field] != getattr(unit, field)
        for field in ("name", "parent_id", "status")
    )
    if "parent_id" in data and data["parent_id"] is not None:
        new_parent = data["parent_id"]
        if new_parent == unit_id or new_parent in repo.descendant_ids(unit_id):
            raise HTTPException(status_code=400, detail="不能把上级设为自身或其下级（成环）")
        if repo.get(new_parent) is None:
            raise HTTPException(status_code=404, detail="上级组织不存在或不可见")
    invalidated_proof_count = 0
    revoked_session_user_count = 0
    subtree_ids = repo.descendant_ids(unit.id)
    store_ids: set[int] = set()
    affected_scopes: set[tuple[int, Department]] = set()
    if routing_changed:
        store_ids = set(
            session.scalars(
                select(OrgUnit.id).where(
                    OrgUnit.id.in_(subtree_ids),
                    OrgUnit.type == OrgType.STORE,
                    OrgUnit.is_deleted.is_(False),
                )
            ).all()
        )
        affected_scopes = {
            (store_id, department)
            for store_id in store_ids
            for department in (Department.DINING, Department.KITCHEN)
        }
    affected_user_ids = reviewer_authorization_user_ids(
        session,
        scopes=affected_scopes,
    )
    lock_reviewer_authorization_users(session, affected_user_ids)
    locked_ids = set(subtree_ids)
    if "parent_id" in data and data["parent_id"] is not None:
        locked_ids.add(data["parent_id"])
    locked_units = _lock_org_units(session, locked_ids)
    unit = locked_units.get(unit_id)  # type: ignore[assignment]
    if unit is None or unit.is_deleted:
        raise HTTPException(status_code=409, detail="组织已被其他操作修改，请刷新后重试")
    if (
        "parent_id" in data
        and data["parent_id"] is not None
        and (data["parent_id"] not in locked_units or locked_units[data["parent_id"]].is_deleted)
    ):
        raise HTTPException(status_code=409, detail="上级组织已被其他操作修改，请刷新后重试")
    if routing_changed:
        invalidation = invalidate_reviewer_authorization(
            session,
            scopes=affected_scopes,
            locked_user_ids=affected_user_ids,
        )
        invalidated_proof_count = invalidation.invalidated_proof_count
        revoked_session_user_count = invalidation.revoked_user_count
    for field, value in data.items():
        setattr(unit, field, value)
    session.flush()
    audit.record(
        session,
        action="org.update",
        actor=(principal.user_id, principal.username),
        target_type="org_unit",
        target_id=unit.id,
        detail={
            "changed": sorted(data.keys()),
            "invalidated_sync_proof_count": invalidated_proof_count,
            "revoked_session_user_count": revoked_session_user_count,
        },
    )
    session.commit()
    return unit


@router.delete("/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unit(
    unit_id: int,
    principal: Principal = Depends(require_permission(Perm.ORG_WRITE)),
    session: Session = Depends(get_session),
) -> None:
    take_organization_sync_lock(session)
    repo = _repo(session, principal, Perm.ORG_WRITE)
    unit = repo.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="组织不存在或不可见")
    _ensure_org_not_in_active_payroll(session, unit.id)
    subtree_ids = repo.descendant_ids(unit.id)
    store_ids = set(
        session.scalars(
            select(OrgUnit.id).where(
                OrgUnit.id.in_(subtree_ids),
                OrgUnit.type == OrgType.STORE,
                OrgUnit.is_deleted.is_(False),
            )
        ).all()
    )
    affected_scopes = {
        (store_id, department)
        for store_id in store_ids
        for department in (Department.DINING, Department.KITCHEN)
    }
    affected_user_ids = reviewer_authorization_user_ids(
        session,
        scopes=affected_scopes,
    )
    lock_reviewer_authorization_users(session, affected_user_ids)
    locked_units = _lock_org_units(session, subtree_ids)
    unit = locked_units.get(unit_id)  # type: ignore[assignment]
    if unit is None or unit.is_deleted:
        raise HTTPException(status_code=409, detail="组织已被其他操作修改，请刷新后重试")
    invalidation = invalidate_reviewer_authorization(
        session,
        scopes=affected_scopes,
        locked_user_ids=affected_user_ids,
    )
    repo.soft_delete(unit)
    audit.record(
        session,
        action="org.delete",
        actor=(principal.user_id, principal.username),
        target_type="org_unit",
        target_id=unit_id,
        detail={
            "invalidated_sync_proof_count": invalidation.invalidated_proof_count,
            "revoked_session_user_count": invalidation.revoked_user_count,
        },
    )
    session.commit()
