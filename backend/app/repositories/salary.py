from sqlalchemy import func, select

from app.models.salary import SalaryRecord
from app.repositories.base import BaseRepository, Page


class SalaryRecordRepository(BaseRepository[SalaryRecord]):
    model = SalaryRecord

    def _apply_org_scope(self, stmt):
        # 按门店受限；未匹配到组织（org_unit_id 为空）的历史记录仅不受限主体可见
        if self._org_scope is None:
            return stmt
        return stmt.where(SalaryRecord.org_unit_id.in_(self._org_scope))

    def search(
        self,
        *,
        name: str | None = None,
        emp_no: str | None = None,
        period: str | None = None,
        store: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page[SalaryRecord]:
        stmt = self._base_query()
        if name:
            stmt = stmt.where(SalaryRecord.name.ilike(f"%{name}%"))
        if emp_no:
            stmt = stmt.where(SalaryRecord.emp_no.ilike(f"{emp_no}%"))
        if period:
            stmt = stmt.where(SalaryRecord.period == period)
        if store:
            stmt = stmt.where(SalaryRecord.store_name.ilike(f"%{store}%"))

        page = max(1, page)
        page_size = min(500, max(1, page_size))
        total = self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        items = self.session.scalars(
            stmt.order_by(SalaryRecord.period.desc(), SalaryRecord.store_name, SalaryRecord.name)
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return Page(items=items, total=total, page=page, page_size=page_size)
