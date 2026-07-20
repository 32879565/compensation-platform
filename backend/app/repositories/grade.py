from app.models.grade import JobGrade, SalaryBand
from app.repositories.base import BaseRepository

# 职级/薪档为集团全局主数据，不做组织范围限制。


class JobGradeRepository(BaseRepository[JobGrade]):
    model = JobGrade

    def by_code(self, code: str) -> JobGrade | None:
        return self.session.scalars(self._base_query().where(JobGrade.code == code)).first()


class SalaryBandRepository(BaseRepository[SalaryBand]):
    model = SalaryBand
