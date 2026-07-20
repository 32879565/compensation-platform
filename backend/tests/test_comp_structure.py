from datetime import date
from decimal import Decimal

import pytest

from app.comp.service import (
    BandStatus,
    StructureError,
    compa_ratio,
    current_structure,
    set_component_amount,
    structure_total,
)
from app.models.comp import ComponentType, SalaryComponentDef
from app.models.employee import Employee
from app.models.grade import JobGrade, SalaryBand
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def emp_id(db_session):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    db_session.add(emp)
    db_session.flush()
    return emp.id


def _component(session, code, ctype=ComponentType.BASE):
    c = SalaryComponentDef(code=code, name=code, component_type=ctype)
    session.add(c)
    session.flush()
    return c


def test_component_taxable_and_base_flags(db_session):
    c = SalaryComponentDef(
        code="BASE", name="基本", component_type=ComponentType.BASE, in_social_base=True
    )
    db_session.add(c)
    db_session.flush()
    db_session.refresh(c)
    assert c.taxable is True
    assert c.in_social_base is True
    assert c.in_housing_base is False


def test_effective_dating_creates_new_record_closes_old(db_session, emp_id):
    base = _component(db_session, "BASE")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    # 2026-06 调薪
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("6000"),
        effective_from=date(2026, 6, 1),
    )
    # 调薪前查 5 月 → 旧值 5000
    may = current_structure(db_session, emp_id, date(2026, 5, 1))
    assert len(may) == 1 and may[0].amount == Decimal("5000")
    # 调薪后查 7 月 → 新值 6000
    jul = current_structure(db_session, emp_id, date(2026, 7, 1))
    assert len(jul) == 1 and jul[0].amount == Decimal("6000")
    # 历史未被覆盖：共 2 条记录
    hist = current_structure(db_session, emp_id, date(2026, 6, 1))
    assert hist[0].amount == Decimal("6000")


def test_same_day_correction_updates_amount(db_session, emp_id):
    base = _component(db_session, "BASE")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    # 同一生效日再设 → 修正金额，不新增
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5500"),
        effective_from=date(2026, 1, 1),
    )
    recs = current_structure(db_session, emp_id, date(2026, 1, 1))
    assert len(recs) == 1
    assert recs[0].amount == Decimal("5500")


def test_backdated_effective_rejected(db_session, emp_id):
    base = _component(db_session, "BASE")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 6, 1),
    )
    with pytest.raises(StructureError):
        set_component_amount(
            db_session,
            employee_id=emp_id,
            component_id=base.id,
            amount=Decimal("4000"),
            effective_from=date(2026, 3, 1),  # 早于当前生效日
        )


def test_structure_total_excludes_deductions(db_session, emp_id):
    base = _component(db_session, "BASE", ComponentType.BASE)
    allow = _component(db_session, "ALLOW", ComponentType.ALLOWANCE)
    deduct = _component(db_session, "DED", ComponentType.DEDUCTION)
    d = date(2026, 1, 1)
    for c, amt in ((base, "5000"), (allow, "1000"), (deduct, "300")):
        set_component_amount(
            db_session,
            employee_id=emp_id,
            component_id=c.id,
            amount=Decimal(amt),
            effective_from=d,
        )
    # 5000 + 1000，扣款 300 不计入
    assert structure_total(db_session, emp_id, d) == Decimal("6000")


def _grade_with_band(db_session, mn, md, mx):
    grade = JobGrade(code="P3", name="三级", rank=3)
    db_session.add(grade)
    db_session.flush()
    db_session.add(
        SalaryBand(
            job_grade_id=grade.id,
            band_min=Decimal(mn),
            band_mid=Decimal(md),
            band_max=Decimal(mx),
            effective_from=date(2026, 1, 1),
        )
    )
    db_session.flush()
    return grade


def test_compa_ratio_in_band(db_session, emp_id):
    base = _component(db_session, "BASE")
    grade = _grade_with_band(db_session, "4000", "5000", "6000")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    r = compa_ratio(db_session, employee_id=emp_id, job_grade_id=grade.id, on_date=date(2026, 1, 1))
    assert r.band_status is BandStatus.IN_BAND
    assert r.compa_ratio == Decimal("1")


def test_compa_ratio_over_band(db_session, emp_id):
    base = _component(db_session, "BASE")
    grade = _grade_with_band(db_session, "4000", "5000", "6000")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("7000"),
        effective_from=date(2026, 1, 1),
    )
    r = compa_ratio(db_session, employee_id=emp_id, job_grade_id=grade.id, on_date=date(2026, 1, 1))
    assert r.band_status is BandStatus.OVER


def test_compa_ratio_no_band(db_session, emp_id):
    base = _component(db_session, "BASE")
    set_component_amount(
        db_session,
        employee_id=emp_id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    r = compa_ratio(db_session, employee_id=emp_id, job_grade_id=None, on_date=date(2026, 1, 1))
    assert r.band_status is BandStatus.NO_BAND
    assert r.compa_ratio is None
