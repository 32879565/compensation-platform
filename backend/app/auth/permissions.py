"""权限点常量与角色定义（种子数据）。

权限命名：<资源>:<动作>，如 employee:read。self 后缀表示仅限本人（员工自助）。
"""

from __future__ import annotations

from dataclasses import dataclass


class Perm:
    ORG_READ = "org:read"
    ORG_WRITE = "org:write"
    EMPLOYEE_READ = "employee:read"
    EMPLOYEE_WRITE = "employee:write"
    # 查看未脱敏 PII（身份证/银行卡全量）。刻意不用 :read 后缀，避免被 AUDITOR 的
    # _ALL_READ 自动纳入——审计只应看脱敏值。
    EMPLOYEE_PII = "employee:pii"
    GRADE_READ = "grade:read"
    GRADE_WRITE = "grade:write"
    STRUCTURE_READ = "salary_structure:read"
    STRUCTURE_WRITE = "salary_structure:write"
    ATTENDANCE_READ = "attendance:read"
    ATTENDANCE_WRITE = "attendance:write"
    PAYROLL_READ = "payroll:read"
    PAYROLL_RUN = "payroll:run"
    PAYROLL_APPROVE = "payroll:approve"
    ADJUSTMENT_READ = "adjustment:read"
    ADJUSTMENT_CREATE = "adjustment:create"
    ADJUSTMENT_APPROVE = "adjustment:approve"
    BUDGET_READ = "budget:read"
    BUDGET_WRITE = "budget:write"
    DASHBOARD_READ = "dashboard:read"
    EXPORT_DATA = "export:data"
    IMPORT_RUN = "import:run"
    SALARY_READ = "salary:read"
    AUDIT_READ = "audit:read"
    USER_MANAGE = "user:manage"
    PAYSLIP_READ_SELF = "payslip:read:self"


PERMISSION_CATALOG: dict[str, str] = {
    Perm.ORG_READ: "查看组织",
    Perm.ORG_WRITE: "维护组织",
    Perm.EMPLOYEE_READ: "查看员工",
    Perm.EMPLOYEE_WRITE: "维护员工",
    Perm.EMPLOYEE_PII: "查看员工完整证件信息",
    Perm.GRADE_READ: "查看职级薪档",
    Perm.GRADE_WRITE: "维护职级薪档",
    Perm.STRUCTURE_READ: "查看薪资结构",
    Perm.STRUCTURE_WRITE: "维护薪资结构",
    Perm.ATTENDANCE_READ: "查看考勤",
    Perm.ATTENDANCE_WRITE: "录入考勤",
    Perm.PAYROLL_READ: "查看核算",
    Perm.PAYROLL_RUN: "执行核算",
    Perm.PAYROLL_APPROVE: "复核核算",
    Perm.ADJUSTMENT_READ: "查看调薪",
    Perm.ADJUSTMENT_CREATE: "发起调薪",
    Perm.ADJUSTMENT_APPROVE: "审批调薪",
    Perm.BUDGET_READ: "查看预算",
    Perm.BUDGET_WRITE: "维护预算",
    Perm.DASHBOARD_READ: "查看看板",
    Perm.EXPORT_DATA: "导出数据",
    Perm.IMPORT_RUN: "导入薪资数据",
    Perm.SALARY_READ: "查询薪资记录",
    Perm.AUDIT_READ: "查看审计日志",
    Perm.USER_MANAGE: "用户与权限管理",
    Perm.PAYSLIP_READ_SELF: "查看本人工资条",
}

_ALL_READ = [p for p in PERMISSION_CATALOG if p.endswith(":read")]


@dataclass(frozen=True)
class RoleDef:
    code: str
    name: str
    is_global_scope: bool
    permissions: tuple[str, ...]


ROLE_DEFINITIONS: tuple[RoleDef, ...] = (
    RoleDef("SUPER_ADMIN", "超级管理员", True, tuple(PERMISSION_CATALOG)),
    RoleDef(
        "GROUP_HR",
        "集团HR",
        True,
        (
            Perm.ORG_READ,
            Perm.ORG_WRITE,
            Perm.EMPLOYEE_READ,
            Perm.EMPLOYEE_WRITE,
            Perm.EMPLOYEE_PII,
            Perm.GRADE_READ,
            Perm.GRADE_WRITE,
            Perm.STRUCTURE_READ,
            Perm.STRUCTURE_WRITE,
            Perm.ATTENDANCE_READ,
            Perm.ATTENDANCE_WRITE,
            Perm.PAYROLL_READ,
            Perm.ADJUSTMENT_READ,
            Perm.ADJUSTMENT_CREATE,
            Perm.ADJUSTMENT_APPROVE,
            Perm.BUDGET_READ,
            Perm.BUDGET_WRITE,
            Perm.DASHBOARD_READ,
            Perm.EXPORT_DATA,
            Perm.IMPORT_RUN,
            Perm.SALARY_READ,
        ),
    ),
    RoleDef(
        "REGION_MANAGER",
        "区域HR经理",
        False,
        (
            Perm.ORG_READ,
            Perm.EMPLOYEE_READ,
            Perm.GRADE_READ,
            Perm.STRUCTURE_READ,
            Perm.ATTENDANCE_READ,
            Perm.ATTENDANCE_WRITE,
            Perm.PAYROLL_READ,
            Perm.ADJUSTMENT_READ,
            Perm.ADJUSTMENT_CREATE,
            Perm.BUDGET_READ,
            Perm.DASHBOARD_READ,
            Perm.SALARY_READ,
        ),
    ),
    RoleDef(
        "STORE_MANAGER",
        "店长",
        False,
        (
            Perm.EMPLOYEE_READ,
            Perm.ATTENDANCE_READ,
            Perm.ATTENDANCE_WRITE,
            Perm.DASHBOARD_READ,
            Perm.SALARY_READ,
        ),
    ),
    RoleDef(
        "FINANCE",
        "财务",
        True,
        (
            Perm.PAYROLL_READ,
            Perm.PAYROLL_RUN,
            Perm.PAYROLL_APPROVE,
            Perm.DASHBOARD_READ,
            Perm.EXPORT_DATA,
            Perm.SALARY_READ,
        ),
    ),
    # AUDIT_READ 本身以 :read 结尾，已包含在 _ALL_READ 中
    RoleDef("AUDITOR", "审计", True, tuple(_ALL_READ)),
    RoleDef("EMPLOYEE", "员工", False, (Perm.PAYSLIP_READ_SELF,)),
)
