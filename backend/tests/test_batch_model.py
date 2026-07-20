from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.comp import AllowanceKind, ComponentType, SalaryComponentDef
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_employee_department_defaults_other(db_session):
    store = OrgUnit(code="S", name="店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    db_session.add(emp)
    db_session.flush()
    db_session.refresh(emp)
    assert emp.department is Department.OTHER
    assert emp.is_special_position is False


def test_employee_department_kitchen(db_session):
    store = OrgUnit(code="S", name="店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    emp = Employee(
        emp_no="E1",
        name="厨师",
        org_unit_id=store.id,
        department=Department.KITCHEN,
        is_special_position=True,
        position_title="洗碗岗",
    )
    db_session.add(emp)
    db_session.flush()
    db_session.refresh(emp)
    assert emp.department is Department.KITCHEN
    assert emp.is_special_position is True


def test_component_new_types_and_allowance_kind(db_session):
    comp = SalaryComponentDef(
        code="COMP", name="综合薪资", component_type=ComponentType.COMPREHENSIVE
    )
    house = SalaryComponentDef(code="HOUSE", name="房补", component_type=ComponentType.HOUSING)
    fixed = SalaryComponentDef(
        code="ALLOW",
        name="固定补贴",
        component_type=ComponentType.ALLOWANCE,
        allowance_kind=AllowanceKind.FIXED,
    )
    db_session.add_all([comp, house, fixed])
    db_session.flush()
    db_session.refresh(fixed)
    assert fixed.allowance_kind is AllowanceKind.FIXED
    assert comp.component_type is ComponentType.COMPREHENSIVE


def test_attendance_worked_hours_and_rest_days(db_session):
    from decimal import Decimal

    from app.models.attendance import AttendanceRecord

    store = OrgUnit(code="S", name="店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    db_session.add(emp)
    db_session.flush()
    att = AttendanceRecord(
        employee_id=emp.id,
        period="2026-05",
        expected_days=Decimal("26"),
        actual_days=Decimal("0"),
        worked_hours=Decimal("189"),
        rest_days=Decimal("4"),
    )
    db_session.add(att)
    db_session.flush()
    db_session.refresh(att)
    assert att.worked_hours == Decimal("189.00")
    assert att.rest_days == Decimal("4.00")


def test_payroll_batch_status_and_unique_period(db_session):
    b = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 4, 26),
        attendance_end=date(2026, 5, 25),
    )
    db_session.add(b)
    db_session.flush()
    db_session.refresh(b)
    assert b.status is BatchStatus.DRAFT
    assert b.version == 1
    # 同月份唯一
    db_session.add(
        PayrollBatch(
            period="2026-05",
            attendance_start=date(2026, 4, 26),
            attendance_end=date(2026, 5, 25),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
