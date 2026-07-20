from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.org import OrgUnit
from app.repositories.org import OrgUnitRepository
from app.schemas.org import OrgTreeNode, OrgUnitCreate, OrgUnitOut, OrgUnitUpdate

router = APIRouter(prefix="/api/org", tags=["org"])


def _repo(session: Session, principal: Principal) -> OrgUnitRepository:
    return OrgUnitRepository(session, org_scope=principal_scope(principal))


@router.get("/tree", response_model=list[OrgTreeNode])
def get_tree(
    principal: Principal = Depends(require_permission(Perm.ORG_READ)),
    session: Session = Depends(get_session),
) -> list[OrgTreeNode]:
    units = _repo(session, principal).all_visible()

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
    return _repo(session, principal).all_visible()


@router.post("", response_model=OrgUnitOut, status_code=status.HTTP_201_CREATED)
def create_unit(
    body: OrgUnitCreate,
    principal: Principal = Depends(require_permission(Perm.ORG_WRITE)),
    session: Session = Depends(get_session),
) -> OrgUnit:
    repo = _repo(session, principal)
    if body.parent_id is not None and repo.get(body.parent_id) is None:
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
    repo = _repo(session, principal)
    unit = repo.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="组织不存在或不可见")
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data and data["parent_id"] is not None:
        new_parent = data["parent_id"]
        if new_parent == unit_id or new_parent in repo.descendant_ids(unit_id):
            raise HTTPException(status_code=400, detail="不能把上级设为自身或其下级（成环）")
        if repo.get(new_parent) is None:
            raise HTTPException(status_code=404, detail="上级组织不存在或不可见")
    for field, value in data.items():
        setattr(unit, field, value)
    session.flush()
    audit.record(
        session,
        action="org.update",
        actor=(principal.user_id, principal.username),
        target_type="org_unit",
        target_id=unit.id,
        detail={"changed": sorted(data.keys())},
    )
    session.commit()
    return unit


@router.delete("/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unit(
    unit_id: int,
    principal: Principal = Depends(require_permission(Perm.ORG_WRITE)),
    session: Session = Depends(get_session),
) -> None:
    repo = _repo(session, principal)
    unit = repo.get(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="组织不存在或不可见")
    repo.soft_delete(unit)
    audit.record(
        session,
        action="org.delete",
        actor=(principal.user_id, principal.username),
        target_type="org_unit",
        target_id=unit_id,
    )
    session.commit()
