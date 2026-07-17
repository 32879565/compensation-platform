from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Employee,
    EmploymentType,
    JobGrade,
    OrgType,
    OrgUnit,
    PayPeriod,
    PeriodStatus,
    SalaryBand,
)
from app.repositories.base import BaseRepository

pytestmark = pytest.mark.usefixtures("pg_engine")


def _store(session, code="S01001", name="测试门店", parent=None):
    unit = OrgUnit(code=code, name=name, type=OrgType.STORE, city="广州", parent_id=parent)
    session.add(unit)
    session.flush()
    return unit


def test_org_tree_self_reference(db_session):
    group = OrgUnit(code="G1", name="集团", type=OrgType.GROUP)
    db_session.add(group)
    db_session.flush()
    region = OrgUnit(code="R1", name="广州区域", type=OrgType.REGION, parent_id=group.id)
    db_session.add(region)
    db_session.flush()
    store = _store(db_session, parent=region.id)

    assert store.parent.parent.id == group.id
    assert region.children[0].id == store.id


def test_org_code_unique(db_session):
    _store(db_session, code="DUP")
    # 第二条同 code 直接 add（不经会 flush 的 _store），在 raises 块内触发唯一约束
    db_session.add(OrgUnit(code="DUP", name="另一家", type=OrgType.STORE, city="广州"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_employee_emp_no_unique(db_session):
    store = _store(db_session)
    db_session.add(Employee(emp_no="E001", name="张三", org_unit_id=store.id))
    db_session.flush()
    db_session.add(Employee(emp_no="E001", name="李四", org_unit_id=store.id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_employee_defaults(db_session):
    store = _store(db_session)
    emp = Employee(emp_no="E100", name="王五", org_unit_id=store.id)
    db_session.add(emp)
    db_session.flush()
    db_session.refresh(emp)
    assert emp.employment_type is EmploymentType.FULL_TIME
    assert emp.status.value == "ACTIVE"
    assert emp.is_deleted is False


def test_money_is_decimal_not_float(db_session):
    grade = JobGrade(code="P1", name="一级", rank=1)
    db_session.add(grade)
    db_session.flush()
    band = SalaryBand(
        job_grade_id=grade.id,
        band_min=Decimal("3000.00"),
        band_mid=Decimal("4500.50"),
        band_max=Decimal("6000.00"),
        effective_from=date(2026, 1, 1),
    )
    db_session.add(band)
    db_session.flush()
    db_session.refresh(band)
    # 不变量1：金额读回必须是 Decimal 且精度保留
    assert isinstance(band.band_mid, Decimal)
    assert band.band_mid == Decimal("4500.50")


def test_pay_period_unique_and_status(db_session):
    db_session.add(PayPeriod(year_month="2026-05"))
    db_session.flush()
    p = db_session.query(PayPeriod).filter_by(year_month="2026-05").one()
    assert p.status is PeriodStatus.OPEN
    db_session.add(PayPeriod(year_month="2026-05"))
    with pytest.raises(IntegrityError):
        db_session.flush()


class _StoreRepo(BaseRepository[OrgUnit]):
    model = OrgUnit


def test_repository_pagination_and_soft_delete(db_session):
    for i in range(5):
        _store(db_session, code=f"P{i:03d}", name=f"店{i}")
    repo = _StoreRepo(db_session)

    page = repo.list(page=1, page_size=2)
    assert page.total == 5
    assert len(page.items) == 2

    victim = repo.list(page=1, page_size=1).items[0]
    repo.soft_delete(victim)
    assert repo.get(victim.id) is None  # 软删后默认查询不可见
    assert repo.list(page=1, page_size=50).total == 4
