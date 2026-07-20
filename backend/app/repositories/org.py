from collections import deque

from sqlalchemy import select

from app.models.org import OrgUnit
from app.repositories.base import BaseRepository


class OrgUnitRepository(BaseRepository[OrgUnit]):
    model = OrgUnit

    def _apply_org_scope(self, stmt):
        # 组织按 id 受限：受限主体只见其范围内的 org_unit
        if self._org_scope is None:
            return stmt
        return stmt.where(OrgUnit.id.in_(self._org_scope))

    def all_visible(self) -> list[OrgUnit]:
        return list(self.session.scalars(self._base_query()).all())

    def by_code(self, code: str) -> OrgUnit | None:
        return self.session.scalars(self._base_query().where(OrgUnit.code == code)).first()

    def descendant_ids(self, root_id: int) -> set[int]:
        """root 的全部后代 id（含自身）。用于防止把父节点设成自己的后代（成环）。"""
        edges = self.session.execute(
            select(OrgUnit.id, OrgUnit.parent_id).where(OrgUnit.is_deleted.is_(False))
        ).all()
        children: dict[int, list[int]] = {}
        for oid, pid in edges:
            if pid is not None:
                children.setdefault(pid, []).append(oid)
        seen: set[int] = set()
        queue: deque[int] = deque([root_id])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(children.get(node, []))
        return seen
