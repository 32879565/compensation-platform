from app.models.employee import Employee
from app.repositories.base import BaseRepository, Page


class EmployeeRepository(BaseRepository[Employee]):
    model = Employee

    def _apply_org_scope(self, stmt):
        # 员工按所属门店受限：受限主体只见其范围组织内的员工
        if self._org_scope is None:
            return stmt
        return stmt.where(Employee.org_unit_id.in_(self._org_scope))

    def by_emp_no(self, emp_no: str) -> Employee | None:
        return self.session.scalars(self._base_query().where(Employee.emp_no == emp_no)).first()

    def search(
        self,
        *,
        name: str | None = None,
        emp_no: str | None = None,
        org_unit_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page[Employee]:
        from sqlalchemy import func, select

        stmt = self._base_query()
        if name:
            stmt = stmt.where(Employee.name.ilike(f"%{name}%"))
        if emp_no:
            stmt = stmt.where(Employee.emp_no.ilike(f"{emp_no}%"))
        if org_unit_id is not None:
            stmt = stmt.where(Employee.org_unit_id == org_unit_id)

        page = max(1, page)
        page_size = min(500, max(1, page_size))
        total = self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        items = self.session.scalars(
            stmt.order_by(Employee.emp_no).limit(page_size).offset((page - 1) * page_size)
        ).all()
        return Page(items=items, total=total, page=page, page_size=page_size)
