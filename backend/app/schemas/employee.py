from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.core.crypto import mask_bank_account, mask_id_card
from app.models.employee import Department, Employee, EmployeeStatus, EmploymentType


class EmployeeCreate(BaseModel):
    emp_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    org_unit_id: int
    job_grade_id: int | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    department: Department = Department.OTHER
    position_title: str | None = Field(default=None, max_length=64)
    is_special_position: bool = False
    hire_date: date | None = None
    probation_end: date | None = None
    social_city: str | None = Field(default=None, max_length=32)
    id_card: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=64)


class EmployeeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    org_unit_id: int | None = None
    job_grade_id: int | None = None
    employment_type: EmploymentType | None = None
    department: Department | None = None
    position_title: str | None = Field(default=None, max_length=64)
    is_special_position: bool | None = None
    status: EmployeeStatus | None = None
    hire_date: date | None = None
    probation_end: date | None = None
    leave_date: date | None = None
    social_city: str | None = Field(default=None, max_length=32)
    id_card: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=64)


class EmployeeOut(BaseModel):
    id: int
    emp_no: str
    name: str
    org_unit_id: int
    job_grade_id: int | None
    employment_type: EmploymentType
    department: Department
    position_title: str | None
    is_special_position: bool
    status: EmployeeStatus
    hire_date: date | None
    probation_end: date | None
    leave_date: date | None
    social_city: str | None
    id_card: str | None
    bank_account: str | None

    @classmethod
    def from_employee(cls, emp: Employee, *, reveal_pii: bool) -> EmployeeOut:
        """构造响应；无 employee:pii 权限时证件信息脱敏（不变量7）。"""
        return cls(
            id=emp.id,
            emp_no=emp.emp_no,
            name=emp.name,
            org_unit_id=emp.org_unit_id,
            job_grade_id=emp.job_grade_id,
            employment_type=emp.employment_type,
            department=emp.department,
            position_title=emp.position_title,
            is_special_position=emp.is_special_position,
            status=emp.status,
            hire_date=emp.hire_date,
            probation_end=emp.probation_end,
            leave_date=emp.leave_date,
            social_city=emp.social_city,
            id_card=emp.id_card if reveal_pii else mask_id_card(emp.id_card),
            bank_account=(emp.bank_account if reveal_pii else mask_bank_account(emp.bank_account)),
        )


class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int
