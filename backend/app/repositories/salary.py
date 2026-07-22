from sqlalchemy import exists, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.models.employee import Department
from app.models.payroll_result import PayrollResult
from app.models.salary import ImportBatch, SalaryRecord, SalarySource
from app.repositories.base import BaseRepository, Page


class SalaryRecordRepository(BaseRepository[SalaryRecord]):
    model = SalaryRecord

    def __init__(
        self,
        session: Session,
        *,
        org_scope: frozenset[int] | None,
        import_review_scope: frozenset[tuple[int, Department]] | None,
    ) -> None:
        super().__init__(session, org_scope=org_scope)
        self._import_review_scope = import_review_scope

    def _apply_org_scope(self, stmt):
        # 按门店受限；未匹配到组织（org_unit_id 为空）的历史记录仅不受限主体可见
        if self._org_scope is not None:
            stmt = stmt.where(SalaryRecord.org_unit_id.in_(self._org_scope))

        # Confirmed imports contain every store before HR chooses which scopes
        # to publish.  Local salary readers must therefore prove both an exact
        # store/department review assignment and a PayrollResult created by
        # that immutable publish round.  ``None`` is reserved for a role that
        # grants salary:read globally; an empty set fails closed for imports.
        if self._import_review_scope is None:
            return stmt
        if not self._import_review_scope:
            return stmt.where(SalaryRecord.source != SalarySource.IMPORT)

        review_pairs = sorted(
            self._import_review_scope,
            key=lambda scope: (scope[0], scope[1].value),
        )
        published_in_review_scope = exists(
            select(PayrollResult.id)
            .join(ImportBatch, ImportBatch.id == PayrollResult.source_import_batch_id)
            .where(
                ImportBatch.id == SalaryRecord.import_batch_id,
                PayrollResult.batch_id == ImportBatch.published_batch_id,
                PayrollResult.batch_version == ImportBatch.published_batch_version,
                PayrollResult.employee_id == SalaryRecord.employee_id,
                PayrollResult.org_unit_id == SalaryRecord.org_unit_id,
                tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(review_pairs),
            )
            .correlate(SalaryRecord)
        )
        return stmt.where(
            or_(
                SalaryRecord.source != SalarySource.IMPORT,
                published_in_review_scope,
            )
        )

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
