from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.bootstrap import seed_rbac
from app.dingtalk.service import stage_review_deliveries
from app.importing.header_rules import MONEY_FIELDS
from app.importing.parser import SalaryRow
from app.importing.publish import ImportPublishError, publish_import_for_review
from app.importing.service import confirm_import, stage_import
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import DingTalkDelivery, DingTalkDeliveryStatus
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import BatchConfirmation, PayrollResult

pytestmark = pytest.mark.usefixtures("pg_engine")


def _employee(session, store, *, emp_no: str, name: str) -> Employee:
    # The legacy employee catalogue currently contains OTHER for every employee.
    # A final-payroll import must therefore carry an explicit review department.
    employee = Employee(
        emp_no=emp_no,
        name=name,
        org_unit_id=store.id,
        department=Department.OTHER,
    )
    session.add(employee)
    session.flush()
    return employee


def _salary_row(
    *,
    emp_no: str,
    name: str,
    store_name: str,
    department: str,
    gross: str | None,
    net: str | None,
) -> SalaryRow:
    fields = {
        "复核部门": department,
        "实际计薪出勤天数": "21.5",
        "出勤工资": "5000.05",
        "加班工资": "200.10",
        "法定节假日工资": "300.15",
        "固定补贴": "100.20",
        "浮动补贴": "80.30",
        "房补": "400",
        "押金": "600",
    }
    if gross is not None:
        fields["应发工资"] = gross
    if net is not None:
        fields["实发工资"] = net
    money = {
        field: Decimal(value)
        for field, value in fields.items()
        if field in MONEY_FIELDS
    }
    return SalaryRow(
        period="2026-05",
        emp_no=emp_no,
        name=name,
        store_name=store_name,
        fields=fields,
        money=money,
    )


def _confirmed_import(session, store, rows: list[SalaryRow]):
    batch = stage_import(
        session,
        filename="2026-05-final-payroll.xlsx",
        period="2026-05",
        rows=rows,
    )
    assert batch.error_rows == 0
    assert confirm_import(session, batch) == len(rows)
    return batch


def _reviewer(session, *, username: str, store_id: int, department: Department) -> User:
    seed_rbac(session)
    role = session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    user = User(username=username, password_hash="not-used-by-this-test")
    session.add(user)
    session.flush()
    session.add_all(
        [
            UserRole(user_id=user.id, role_id=role.id),
            UserReviewScope(
                user_id=user.id,
                org_unit_id=store_id,
                department=department,
            ),
        ]
    )
    session.flush()
    return user


def test_confirmed_excel_projects_exact_results_and_routes_each_department(db_session):
    store = OrgUnit(code="PUB-S1", name="发布门店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    dining = _employee(db_session, store, emp_no="PUB-E1", name="厅面员工")
    kitchen = _employee(db_session, store, emp_no="PUB-E2", name="厨房员工")
    dining_manager = _reviewer(
        db_session,
        username="publish-dining-manager",
        store_id=store.id,
        department=Department.DINING,
    )
    kitchen_manager = _reviewer(
        db_session,
        username="publish-kitchen-manager",
        store_id=store.id,
        department=Department.KITCHEN,
    )
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=dining.emp_no,
                name=dining.name,
                store_name=store.name,
                department="厅面",
                gross="6080.80",
                net="5480.80",
            ),
            _salary_row(
                emp_no=kitchen.emp_no,
                name=kitchen.name,
                store_name=store.name,
                department="厨房",
                gross="6380.90",
                net="5780.90",
            ),
        ],
    )

    published = publish_import_for_review(db_session, imported)

    assert published.import_batch_id == imported.id
    assert published.employees == 2
    assert published.scopes == 2
    assert published.already_published is False
    payroll_batch = db_session.get(PayrollBatch, published.payroll_batch_id)
    assert payroll_batch is not None
    assert payroll_batch.period == "2026-05"
    assert payroll_batch.status == BatchStatus.PENDING_STORE_CONFIRM
    assert payroll_batch.attendance_start.isoformat() == "2026-05-01"
    assert payroll_batch.attendance_end.isoformat() == "2026-05-31"

    results = list(
        db_session.scalars(
            select(PayrollResult)
            .where(PayrollResult.batch_id == payroll_batch.id)
            .order_by(PayrollResult.employee_id)
        ).all()
    )
    assert len(results) == 2
    by_employee = {result.employee_id: result for result in results}
    assert by_employee[dining.id].department == Department.DINING
    assert by_employee[dining.id].gross == Decimal("6080.80")
    assert by_employee[dining.id].net == Decimal("5480.80")
    assert by_employee[dining.id].actual_attendance_days == Decimal("21.50")
    assert by_employee[dining.id].source_import_batch_id == imported.id
    assert by_employee[kitchen.id].department == Department.KITCHEN
    assert by_employee[kitchen.id].gross == Decimal("6380.90")
    assert by_employee[kitchen.id].net == Decimal("5780.90")
    assert all(result.rule_version == "IMPORT-v1" for result in results)
    assert all(
        result.warnings == ["此结果来自人事确认的 Excel 导入，未经过系统核算引擎"]
        for result in results
    )
    dining_lines = {line["code"]: line for line in by_employee[dining.id].lines}
    assert dining_lines["ATTEND_WAGE"]["amount"] == "5000.05"
    assert dining_lines["HOLIDAY"]["amount"] == "300.15"
    assert dining_lines["FIXED_ALLOWANCE"]["amount"] == "100.20"

    confirmations = list(
        db_session.scalars(
            select(BatchConfirmation).where(BatchConfirmation.batch_id == payroll_batch.id)
        ).all()
    )
    assert {(row.org_unit_id, row.department) for row in confirmations} == {
        (store.id, Department.DINING),
        (store.id, Department.KITCHEN),
    }

    staged = stage_review_deliveries(db_session, batch_id=payroll_batch.id)
    assert staged.routed == 2
    assert staged.configuration_failures == 0
    deliveries = list(
        db_session.scalars(
            select(DingTalkDelivery).where(DingTalkDelivery.batch_id == payroll_batch.id)
        ).all()
    )
    assert {(delivery.department, delivery.recipient_user_id) for delivery in deliveries} == {
        (Department.DINING, dining_manager.id),
        (Department.KITCHEN, kitchen_manager.id),
    }
    assert all(delivery.status == DingTalkDeliveryStatus.SANDBOXED for delivery in deliveries)


def test_publish_is_idempotent_for_the_same_import_and_delivery_round(db_session):
    store = OrgUnit(code="IDEMP-S1", name="幂等门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="IDEMP-E1", name="员工")
    _reviewer(
        db_session,
        username="idempotent-manager",
        store_id=store.id,
        department=Department.DINING,
    )
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厅面",
                gross="5000",
                net="4400",
            )
        ],
    )

    first = publish_import_for_review(db_session, imported)
    first_delivery = stage_review_deliveries(db_session, batch_id=first.payroll_batch_id)
    second = publish_import_for_review(db_session, imported)
    second_delivery = stage_review_deliveries(db_session, batch_id=second.payroll_batch_id)

    assert first.already_published is False
    assert second.already_published is True
    assert second.payroll_batch_id == first.payroll_batch_id
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 1
    assert db_session.scalar(select(func.count()).select_from(BatchConfirmation)) == 1
    assert db_session.scalar(select(func.count()).select_from(DingTalkDelivery)) == 1
    assert first_delivery.routed == 1
    assert second_delivery.existing == 1


@pytest.mark.parametrize(
    ("department", "gross", "net", "expected"),
    [
        ("其他", "5000", "4400", "复核部门"),
        ("厅面", None, "4400", "应发工资"),
        ("厨房", "5000", None, "实发工资"),
    ],
)
def test_publish_blocks_ambiguous_or_incomplete_final_payroll(
    db_session, department, gross, net, expected
):
    store = OrgUnit(code=f"BAD-{department}-{gross}-{net}", name="校验门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="BAD-E1", name="待校验员工")
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department=department,
                gross=gross,
                net=net,
            )
        ],
    )

    with pytest.raises(ImportPublishError, match=expected):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0


def test_publish_rejects_unconfirmed_import(db_session):
    store = OrgUnit(code="UNCONF-S1", name="未确认门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="UNCONF-E1", name="员工")
    imported = stage_import(
        db_session,
        filename="unconfirmed.xlsx",
        period="2026-05",
        rows=[
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厅面",
                gross="5000",
                net="4400",
            )
        ],
    )

    with pytest.raises(ImportPublishError, match="确认"):
        publish_import_for_review(db_session, imported)


def test_publish_does_not_mix_with_an_existing_review_round(db_session):
    store = OrgUnit(code="MIX-S1", name="冲突门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="MIX-E1", name="员工")
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厅面",
                gross="5000",
                net="4400",
            )
        ],
    )
    existing = PayrollBatch(
        period="2026-05",
        attendance_start="2026-05-01",
        attendance_end="2026-05-31",
        status=BatchStatus.PENDING_STORE_CONFIRM,
    )
    db_session.add(existing)
    db_session.flush()

    with pytest.raises(ImportPublishError, match="草稿"):
        publish_import_for_review(db_session, imported)

