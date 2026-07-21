from sqlalchemy import func, select

from app.auth.bootstrap import create_super_admin
from app.e2e.bootstrap import (
    E2E_EMPLOYEE_NO,
    E2E_PERIOD,
    require_disposable_seed_environment,
    seed_payroll_scenario,
)
from app.models.attendance import AttendanceRecord
from app.models.auth import Role, User, UserOrgScope, UserReviewScope, UserRole
from app.models.employee import Department, Employee
from app.models.payroll_policy import EmployeeTaxYtdOpening, PayrollPolicy


def test_e2e_seed_environment_is_fail_closed() -> None:
    for marker, allow_writes in (
        (None, None),
        ("marker", None),
        ("marker", "false"),
        (None, "true"),
    ):
        try:
            require_disposable_seed_environment(marker=marker, allow_writes=allow_writes)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("unsafe E2E seed environment was accepted")

    require_disposable_seed_environment(marker="disposable-marker", allow_writes="true")


def test_e2e_payroll_seed_is_complete_and_idempotent(db_session) -> None:
    create_super_admin(db_session, "e2e-admin", "StrongAdminPassword123!")

    first = seed_payroll_scenario(
        db_session,
        admin_username="e2e-admin",
        reviewer_username="e2e-reviewer",
        reviewer_password="StrongReviewerPassword123!",
    )
    second = seed_payroll_scenario(
        db_session,
        admin_username="e2e-admin",
        reviewer_username="e2e-reviewer",
        reviewer_password="StrongReviewerPassword123!",
    )

    assert second == first
    employee = db_session.scalars(select(Employee).where(Employee.emp_no == E2E_EMPLOYEE_NO)).one()
    reviewer = db_session.scalars(select(User).where(User.username == "e2e-reviewer")).one()
    role_codes = set(
        db_session.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == reviewer.id)
        ).all()
    )
    assert role_codes == {"EMPLOYEE", "STORE_MANAGER"}
    assert reviewer.employee_id == employee.id
    assert (
        db_session.scalar(
            select(func.count(UserOrgScope.id)).where(UserOrgScope.user_id == reviewer.id)
        )
        == 1
    )
    review_scope = db_session.scalars(
        select(UserReviewScope).where(UserReviewScope.user_id == reviewer.id)
    ).one()
    assert review_scope.department is Department.OTHER
    attendance = db_session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id,
            AttendanceRecord.period == E2E_PERIOD,
        )
    ).one()
    assert attendance.generated_expected_days == attendance.expected_days
    assert db_session.scalar(select(func.count(PayrollPolicy.id))) == 1
    assert db_session.scalar(select(func.count(EmployeeTaxYtdOpening.id))) == 1
