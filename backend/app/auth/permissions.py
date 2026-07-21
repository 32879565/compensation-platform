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
    ATTENDANCE_SCHEDULE_READ = "attendance_schedule:read"
    ATTENDANCE_SCHEDULE_WRITE = "attendance_schedule:write"
    ATTENDANCE_EXPECTED_DAYS_ADJUST = "attendance:expected_days:adjust"
    HOLIDAY_CALENDAR_READ = "holiday_calendar:read"
    HOLIDAY_CALENDAR_WRITE = "holiday_calendar:write"
    POLICY_READ = "policy:read"
    POLICY_WRITE = "policy:write"
    PAYROLL_READ = "payroll:read"
    PAYROLL_RUN = "payroll:run"
    PAYROLL_APPROVE = "payroll:approve"
    PAYROLL_CORRECT = "payroll:correct"
    PAYROLL_REVIEW = "payroll:review"  # 门店负责人复核确认/提异议
    ADJUSTMENT_READ = "adjustment:read"
    ADJUSTMENT_CREATE = "adjustment:create"
    ADJUSTMENT_APPROVE = "adjustment:approve"
    APPROVAL_FLOW_MANAGE = "approval_flow:manage"
    BUDGET_READ = "budget:read"
    BUDGET_WRITE = "budget:write"
    DASHBOARD_READ = "dashboard:read"
    EXPORT_DATA = "export:data"
    NOTIFICATION_MANAGE = "notification:manage"
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
    Perm.ATTENDANCE_SCHEDULE_READ: "查看应出勤规则",
    Perm.ATTENDANCE_SCHEDULE_WRITE: "维护应出勤规则",
    Perm.ATTENDANCE_EXPECTED_DAYS_ADJUST: "调整应出勤天数",
    Perm.HOLIDAY_CALENDAR_READ: "查看法定节假日日历",
    Perm.HOLIDAY_CALENDAR_WRITE: "维护法定节假日日历",
    Perm.POLICY_READ: "查看社保、公积金与个税政策",
    Perm.POLICY_WRITE: "维护社保、公积金与个税政策",
    Perm.PAYROLL_READ: "查看核算",
    Perm.PAYROLL_RUN: "执行核算",
    Perm.PAYROLL_APPROVE: "复核核算",
    Perm.PAYROLL_CORRECT: "解锁后更正工资源数据",
    Perm.PAYROLL_REVIEW: "门店复核确认/提异议",
    Perm.ADJUSTMENT_READ: "查看调薪",
    Perm.ADJUSTMENT_CREATE: "发起调薪",
    Perm.ADJUSTMENT_APPROVE: "审批调薪",
    Perm.APPROVAL_FLOW_MANAGE: "维护审批流程",
    Perm.BUDGET_READ: "查看预算",
    Perm.BUDGET_WRITE: "维护预算",
    Perm.DASHBOARD_READ: "查看看板",
    Perm.EXPORT_DATA: "导出数据",
    Perm.NOTIFICATION_MANAGE: "管理薪酬通知",
    Perm.IMPORT_RUN: "导入薪资数据",
    Perm.SALARY_READ: "查询薪资记录",
    Perm.AUDIT_READ: "查看审计日志",
    Perm.USER_MANAGE: "用户与权限管理",
    Perm.PAYSLIP_READ_SELF: "查看本人工资条",
}

_ALL_READ = [p for p in PERMISSION_CATALOG if p.endswith(":read")]
_SUPER_ADMIN_PERMISSIONS = tuple(
    permission for permission in PERMISSION_CATALOG if permission != Perm.PAYROLL_REVIEW
)


@dataclass(frozen=True)
class RoleDef:
    code: str
    name: str
    is_global_scope: bool
    permissions: tuple[str, ...]


ROLE_DEFINITIONS: tuple[RoleDef, ...] = (
    # Payroll review is never global: the authoritative workflow requires an
    # explicit store-and-department reviewer assignment before confirmation or
    # a dispute can be raised.  Super administrators retain every operational
    # permission except that scoped reviewer action.
    RoleDef("SUPER_ADMIN", "超级管理员", True, _SUPER_ADMIN_PERMISSIONS),
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
            Perm.ATTENDANCE_SCHEDULE_READ,
            Perm.ATTENDANCE_SCHEDULE_WRITE,
            Perm.ATTENDANCE_EXPECTED_DAYS_ADJUST,
            Perm.HOLIDAY_CALENDAR_READ,
            Perm.HOLIDAY_CALENDAR_WRITE,
            Perm.POLICY_READ,
            Perm.POLICY_WRITE,
            Perm.PAYROLL_READ,
            Perm.PAYROLL_RUN,
            Perm.PAYROLL_APPROVE,
            Perm.PAYROLL_CORRECT,
            Perm.ADJUSTMENT_READ,
            Perm.ADJUSTMENT_CREATE,
            Perm.ADJUSTMENT_APPROVE,
            Perm.APPROVAL_FLOW_MANAGE,
            Perm.BUDGET_READ,
            Perm.BUDGET_WRITE,
            Perm.DASHBOARD_READ,
            Perm.EXPORT_DATA,
            Perm.NOTIFICATION_MANAGE,
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
            Perm.PAYROLL_REVIEW,
            Perm.ADJUSTMENT_READ,
            Perm.ADJUSTMENT_CREATE,
            Perm.ADJUSTMENT_APPROVE,
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
            Perm.PAYROLL_READ,
            Perm.PAYROLL_REVIEW,
            Perm.ADJUSTMENT_CREATE,
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
            Perm.POLICY_READ,
            Perm.DASHBOARD_READ,
            Perm.EXPORT_DATA,
            Perm.SALARY_READ,
        ),
    ),
    # AUDIT_READ 本身以 :read 结尾，已包含在 _ALL_READ 中
    RoleDef("AUDITOR", "审计", True, tuple(_ALL_READ)),
    RoleDef("EMPLOYEE", "员工", False, (Perm.PAYSLIP_READ_SELF,)),
)
