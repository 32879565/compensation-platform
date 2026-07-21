from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.core.crypto import mask_bank_account, mask_id_card
from app.models.employee import Department, Employee, EmployeeStatus, EmploymentType


def validate_employee_lifecycle_dates(
    *,
    hire_date: date | None,
    probation_end: date | None,
    leave_date: date | None,
) -> None:
    """Validate the chronology of one merged employee lifecycle state."""

    if hire_date is None:
        raise ValueError("hire_date cannot be cleared")
    if probation_end is not None and probation_end < hire_date:
        raise ValueError("probation_end cannot be earlier than hire_date")
    if leave_date is not None and leave_date < hire_date:
        raise ValueError("leave_date cannot be earlier than hire_date")


class EmployeeCreate(BaseModel):
    emp_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    org_unit_id: int
    job_grade_id: int | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    department: Department = Department.OTHER
    position_title: str | None = Field(default=None, max_length=64)
    is_special_position: bool = False
    # New master-data records must have a labor-relationship start date.  The
    # database remains nullable only so legacy imports can be repaired through
    # the audited HR workflow; payroll fail-closes those rows in the meantime.
    hire_date: date
    probation_end: date | None = None
    leave_date: date | None = None
    social_city: str | None = Field(default=None, max_length=32)
    id_card: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_lifecycle_dates(self) -> EmployeeCreate:
        validate_employee_lifecycle_dates(
            hire_date=self.hire_date,
            probation_end=self.probation_end,
            leave_date=self.leave_date,
        )
        return self


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

    @field_validator(
        "name",
        "org_unit_id",
        "employment_type",
        "department",
        "is_special_position",
        "status",
        "hire_date",
    )
    @classmethod
    def reject_null_required_fields(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be cleared")
        return value


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
    dingtalk_linked: bool

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
            dingtalk_linked=emp.dingtalk_user_id_hash is not None,
        )


class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int
