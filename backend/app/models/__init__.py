"""集中导入所有模型，确保 Base.metadata 完整（供 Alembic autogenerate 与 create_all）。"""

from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule, PerformanceRecord
from app.models.audit import AuditLog
from app.models.auth import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserOrgScope,
    UserReviewScope,
    UserRole,
)
from app.models.budget import LaborBudget
from app.models.comp import (
    AllowanceKind,
    ComponentType,
    EmployeeSalaryStructure,
    SalaryComponentDef,
)
from app.models.employee import Department, Employee, EmployeeStatus, EmploymentType
from app.models.grade import JobGrade, SalaryBand
from app.models.holiday import HolidayCalendarPeriod, HolidayWorkRecord, StatutoryHolidayDate
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import EmployeeTaxDeduction, PayrollPolicy
from app.models.payroll_result import (
    AdjustmentRecord,
    BatchConfirmation,
    CompDispute,
    ConfirmStatus,
    DisputeStatus,
    PayrollResult,
)
from app.models.period import PayPeriod, PeriodStatus
from app.models.salary import (
    ImportBatch,
    ImportStagingRow,
    ImportStatus,
    RowStatus,
    SalaryRecord,
    SalarySource,
)

__all__ = [
    "OrgUnit",
    "OrgType",
    "Employee",
    "EmploymentType",
    "EmployeeStatus",
    "JobGrade",
    "SalaryBand",
    "HolidayCalendarPeriod",
    "StatutoryHolidayDate",
    "HolidayWorkRecord",
    "PayPeriod",
    "PeriodStatus",
    "User",
    "Role",
    "Permission",
    "UserRole",
    "RolePermission",
    "UserOrgScope",
    "UserReviewScope",
    "RefreshToken",
    "AuditLog",
    "LaborBudget",
    "SalaryRecord",
    "SalarySource",
    "ImportBatch",
    "ImportStagingRow",
    "ImportStatus",
    "RowStatus",
    "SalaryComponentDef",
    "EmployeeSalaryStructure",
    "ComponentType",
    "AttendanceRecord",
    "ExpectedAttendanceRule",
    "PerformanceRecord",
    "Department",
    "AllowanceKind",
    "PayrollBatch",
    "BatchStatus",
    "PayrollPolicy",
    "EmployeeTaxDeduction",
    "PayrollResult",
    "BatchConfirmation",
    "CompDispute",
    "AdjustmentRecord",
    "ConfirmStatus",
    "DisputeStatus",
]
