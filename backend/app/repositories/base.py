from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import Base, SoftDeleteMixin


@dataclass(frozen=True)
class Page[ModelT: Base]:
    items: Sequence[ModelT]
    total: int
    page: int
    page_size: int


class BaseRepository[ModelT: Base]:
    """通用仓储：分页、软删过滤，并预留组织范围过滤钩子（S3/S5 注入）。

    不变量6：所有查询在此层统一施加组织范围过滤。基类默认放行；
    需要 RBAC 范围约束的仓储覆写 _apply_org_scope。
    """

    model: type[ModelT]

    def __init__(self, session: Session, org_scope: frozenset[int] | None = None) -> None:
        """org_scope=None 表示不受限（集团级）；否则为可见 org_unit id 集合。

        与 Principal 语义一致：None=不受限，空集=什么都看不到（fail-closed）。
        """
        self.session = session
        self._org_scope = org_scope

    def _base_query(self):
        stmt = select(self.model)
        if issubclass(self.model, SoftDeleteMixin):
            stmt = stmt.where(self.model.is_deleted.is_(False))
        return self._apply_org_scope(stmt)

    def _apply_org_scope(self, stmt):
        # 默认不施加范围限制；带组织维度的子类覆写此方法。
        return stmt

    def get(self, obj_id: int) -> ModelT | None:
        return self.session.scalars(self._base_query().where(self.model.id == obj_id)).first()

    def list(self, page: int = 1, page_size: int = 50) -> Page[ModelT]:
        page = max(1, page)
        page_size = min(500, max(1, page_size))
        base = self._base_query()
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        items = self.session.scalars(base.limit(page_size).offset((page - 1) * page_size)).all()
        return Page(items=items, total=total, page=page, page_size=page_size)

    def add(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        self.session.flush()
        return obj

    def soft_delete(self, obj: ModelT) -> None:
        if isinstance(obj, SoftDeleteMixin):
            obj.is_deleted = True
            self.session.flush()
        else:  # pragma: no cover - 无软删列的模型不应走此路径
            raise TypeError(f"{self.model.__name__} 不支持软删除")
