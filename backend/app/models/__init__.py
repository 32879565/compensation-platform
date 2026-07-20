"""集中导入所有模型，确保 Base.metadata 完整（供 Alembic autogenerate 与 create_all）。"""

from app.models.audit import AuditLog
from app.models.auth import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserOrgScope,
    UserRole,
)
from app.models.employee import Employee, EmployeeStatus, EmploymentType
from app.models.grade import JobGrade, SalaryBand
from app.models.org import OrgType, OrgUnit
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
    "PayPeriod",
    "PeriodStatus",
    "User",
    "Role",
    "Permission",
    "UserRole",
    "RolePermission",
    "UserOrgScope",
    "RefreshToken",
    "AuditLog",
    "SalaryRecord",
    "SalarySource",
    "ImportBatch",
    "ImportStagingRow",
    "ImportStatus",
    "RowStatus",
]
