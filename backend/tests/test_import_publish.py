from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings
from app.dingtalk import service as dingtalk_service
from app.dingtalk.service import stage_review_deliveries
from app.importing import publish as publish_module
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
from app.models.salary import SalaryRecord

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture(autouse=True)
def _isolate_publish_tests_from_org_sync(monkeypatch):
    """Organization freshness integration is covered in its dedicated test module."""

    monkeypatch.setattr(
        dingtalk_service,
        "require_recent_organization_scopes",
        lambda _session, _scopes, **_kwargs: 1,
    )


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
    money = {field: Decimal(value) for field, value in fields.items() if field in MONEY_FIELDS}
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


def _live_dingtalk_settings() -> Settings:
    return Settings(
        _env_file=None,
        dingtalk_mode="live",
        dingtalk_corp_id="ding-test-corp",
        dingtalk_client_id="ding-test-client",
        dingtalk_client_secret="test-dingtalk-secret-value",
        dingtalk_agent_id=123,
        dingtalk_public_base_url="https://payroll.example.test",
        dingtalk_read_sync_enabled=True,
    )


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


def test_publish_projects_results_and_review_scopes_only_for_selected_stores(db_session):
    first_store = OrgUnit(code="SELECT-S1", name="选择一店", type=OrgType.STORE, city="广州")
    second_store = OrgUnit(code="SELECT-S2", name="选择二店", type=OrgType.STORE, city="广州")
    db_session.add_all([first_store, second_store])
    db_session.flush()
    first_employee = _employee(db_session, first_store, emp_no="SELECT-E1", name="一店员工")
    second_employee = _employee(db_session, second_store, emp_no="SELECT-E2", name="二店员工")
    first_manager = _reviewer(
        db_session,
        username="selected-first-manager",
        store_id=first_store.id,
        department=Department.DINING,
    )
    imported = _confirmed_import(
        db_session,
        first_store,
        [
            _salary_row(
                emp_no=first_employee.emp_no,
                name=first_employee.name,
                store_name=first_store.name,
                department="厅面",
                gross="5000",
                net="4400",
            ),
            _salary_row(
                emp_no=second_employee.emp_no,
                name=second_employee.name,
                store_name=second_store.name,
                department="厅面",
                gross="5100",
                net="4500",
            ),
        ],
    )
    published = publish_import_for_review(db_session, imported, store_ids={first_store.id})

    assert published.employees == 1
    assert published.stores == 1
    results = list(db_session.scalars(select(PayrollResult)).all())
    assert [(row.employee_id, row.org_unit_id) for row in results] == [
        (first_employee.id, first_store.id)
    ]
    confirmations = list(db_session.scalars(select(BatchConfirmation)).all())
    assert [(row.org_unit_id, row.department) for row in confirmations] == [
        (first_store.id, Department.DINING)
    ]

    staged = stage_review_deliveries(
        db_session,
        batch_id=published.payroll_batch_id,
        org_unit_ids={first_store.id},
    )
    assert staged.routed == 1
    assert staged.scopes == 1
    deliveries = list(db_session.scalars(select(DingTalkDelivery)).all())
    assert [(row.org_unit_id, row.recipient_user_id) for row in deliveries] == [
        (first_store.id, first_manager.id)
    ]


def test_publish_rejects_empty_or_foreign_store_selection_without_writes(db_session):
    store = OrgUnit(code="SELECT-VALID", name="有效选择门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="SELECT-VALID-E1", name="员工")
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
    with pytest.raises(ImportPublishError, match="至少选择一家门店"):
        publish_import_for_review(db_session, imported, store_ids=set())
    with pytest.raises(ImportPublishError, match="不属于该导入批次"):
        publish_import_for_review(db_session, imported, store_ids={store.id + 1000})

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0
    assert db_session.scalar(select(func.count()).select_from(BatchConfirmation)) == 0
    assert db_session.scalar(select(func.count()).select_from(DingTalkDelivery)) == 0


def test_import_publish_targets_report_store_counts_and_departments(db_session):
    first_store = OrgUnit(code="TARGET-S1", name="目标一店", type=OrgType.STORE)
    second_store = OrgUnit(code="TARGET-S2", name="目标二店", type=OrgType.STORE)
    db_session.add_all([first_store, second_store])
    db_session.flush()
    first_employee = _employee(db_session, first_store, emp_no="TARGET-E1", name="一店员工")
    second_employee = _employee(db_session, second_store, emp_no="TARGET-E2", name="二店员工")
    _reviewer(
        db_session,
        username="target-first-manager",
        store_id=first_store.id,
        department=Department.DINING,
    )
    imported = _confirmed_import(
        db_session,
        first_store,
        [
            _salary_row(
                emp_no=first_employee.emp_no,
                name=first_employee.name,
                store_name=first_store.name,
                department="厅面",
                gross="5000",
                net="4400",
            ),
            _salary_row(
                emp_no=second_employee.emp_no,
                name=second_employee.name,
                store_name=second_store.name,
                department="厨房",
                gross="5100",
                net="4500",
            ),
        ],
    )

    targets = publish_module.list_import_publish_targets(db_session, imported)
    assert [(row.store_id, row.store_name, row.employee_count) for row in targets] == [
        (first_store.id, first_store.name, 1),
        (second_store.id, second_store.name, 1),
    ]
    assert targets[0].departments == (Department.DINING,)
    assert targets[1].departments == (Department.KITCHEN,)
    assert all(target.locked is False for target in targets)


def test_import_publish_targets_restore_the_persisted_locked_store_range(db_session):
    first_store = OrgUnit(code="TARGET-LOCK-S1", name="锁定目标一店", type=OrgType.STORE)
    second_store = OrgUnit(code="TARGET-LOCK-S2", name="锁定目标二店", type=OrgType.STORE)
    db_session.add_all([first_store, second_store])
    db_session.flush()
    first_employee = _employee(
        db_session,
        first_store,
        emp_no="TARGET-LOCK-E1",
        name="一店员工",
    )
    second_employee = _employee(
        db_session,
        second_store,
        emp_no="TARGET-LOCK-E2",
        name="二店员工",
    )
    imported = _confirmed_import(
        db_session,
        first_store,
        [
            _salary_row(
                emp_no=first_employee.emp_no,
                name=first_employee.name,
                store_name=first_store.name,
                department="厅面",
                gross="5000",
                net="4400",
            ),
            _salary_row(
                emp_no=second_employee.emp_no,
                name=second_employee.name,
                store_name=second_store.name,
                department="厨房",
                gross="5100",
                net="4500",
            ),
        ],
    )

    publish_import_for_review(db_session, imported, store_ids={first_store.id})

    targets = publish_module.list_import_publish_targets(db_session, imported)
    assert [(target.store_id, target.locked) for target in targets] == [(first_store.id, True)]


def test_publish_rejects_workbook_department_that_conflicts_with_employee_master(db_session):
    store = OrgUnit(code="DEPT-MISMATCH-S1", name="部门冲突门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="DEPT-MISMATCH-E1", name="厅面员工")
    employee.department = Department.DINING
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厨房",
                gross="5000",
                net="4400",
            )
        ],
    )

    with pytest.raises(ImportPublishError, match="员工主数据部门"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0
    assert db_session.scalar(select(func.count()).select_from(BatchConfirmation)) == 0


def test_publish_rejects_confirmed_salary_after_employee_moves_store(db_session):
    source_store = OrgUnit(code="ORG-SOURCE-S1", name="原门店", type=OrgType.STORE)
    current_store = OrgUnit(code="ORG-CURRENT-S2", name="现门店", type=OrgType.STORE)
    db_session.add_all([source_store, current_store])
    db_session.flush()
    employee = _employee(db_session, source_store, emp_no="ORG-MOVE-E1", name="调店员工")
    imported = _confirmed_import(
        db_session,
        source_store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=source_store.name,
                department="厅面",
                gross="5000",
                net="4400",
            )
        ],
    )
    employee.org_unit_id = current_store.id
    db_session.flush()

    with pytest.raises(ImportPublishError, match="当前所属门店.*已确认工资记录"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0
    assert db_session.scalar(select(func.count()).select_from(BatchConfirmation)) == 0


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("status", "INACTIVE", id="inactive"),
        pytest.param("type", OrgType.REGION, id="not-a-store"),
        pytest.param("is_deleted", True, id="soft-deleted"),
    ],
)
def test_publish_rejects_employee_whose_current_org_is_not_an_active_store(
    db_session, attribute, value
):
    store = OrgUnit(
        code=f"ORG-INVALID-{attribute}",
        name="失效门店",
        type=OrgType.STORE,
    )
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no=f"ORG-INVALID-{attribute}-E1", name="员工")
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
    setattr(store, attribute, value)
    db_session.flush()

    with pytest.raises(ImportPublishError, match="有效营业门店"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0
    assert db_session.scalar(select(func.count()).select_from(BatchConfirmation)) == 0


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


def test_publish_retry_rejects_a_different_store_selection(db_session):
    first_store = OrgUnit(code="LOCKED-S1", name="锁定一店", type=OrgType.STORE)
    second_store = OrgUnit(code="LOCKED-S2", name="锁定二店", type=OrgType.STORE)
    db_session.add_all([first_store, second_store])
    db_session.flush()
    first_employee = _employee(db_session, first_store, emp_no="LOCKED-E1", name="一店员工")
    second_employee = _employee(db_session, second_store, emp_no="LOCKED-E2", name="二店员工")
    imported = _confirmed_import(
        db_session,
        first_store,
        [
            _salary_row(
                emp_no=first_employee.emp_no,
                name=first_employee.name,
                store_name=first_store.name,
                department="厅面",
                gross="5000",
                net="4400",
            ),
            _salary_row(
                emp_no=second_employee.emp_no,
                name=second_employee.name,
                store_name=second_store.name,
                department="厨房",
                gross="5100",
                net="4500",
            ),
        ],
    )

    first = publish_import_for_review(db_session, imported, store_ids={first_store.id})
    same = publish_import_for_review(db_session, imported, store_ids={first_store.id})
    with pytest.raises(ImportPublishError, match="已经按其他门店范围发布"):
        publish_import_for_review(db_session, imported, store_ids={second_store.id})

    assert same.already_published is True
    assert same.payroll_batch_id == first.payroll_batch_id
    results = list(db_session.scalars(select(PayrollResult)).all())
    assert {(row.employee_id, row.org_unit_id) for row in results} == {
        (first_employee.id, first_store.id)
    }


def test_publish_retry_rejects_a_missing_department_confirmation(db_session):
    store = OrgUnit(code="SCOPE-INTEGRITY-S1", name="范围完整门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    dining_employee = _employee(
        db_session,
        store,
        emp_no="SCOPE-INTEGRITY-E1",
        name="厅面员工",
    )
    kitchen_employee = _employee(
        db_session,
        store,
        emp_no="SCOPE-INTEGRITY-E2",
        name="厨房员工",
    )
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=dining_employee.emp_no,
                name=dining_employee.name,
                store_name=store.name,
                department="厅面",
                gross="5000",
                net="4400",
            ),
            _salary_row(
                emp_no=kitchen_employee.emp_no,
                name=kitchen_employee.name,
                store_name=store.name,
                department="厨房",
                gross="5100",
                net="4500",
            ),
        ],
    )
    publish_import_for_review(db_session, imported, store_ids={store.id})
    missing = db_session.scalar(
        select(BatchConfirmation).where(
            BatchConfirmation.org_unit_id == store.id,
            BatchConfirmation.department == Department.KITCHEN,
        )
    )
    assert missing is not None
    db_session.delete(missing)
    db_session.flush()

    with pytest.raises(ImportPublishError, match="复核范围不完整"):
        publish_import_for_review(db_session, imported, store_ids={store.id})


def test_excel_delivery_retry_requeues_the_failed_row_after_routing_is_fixed(db_session):
    store = OrgUnit(code="RETRY-S1", name="重试门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="RETRY-E1", name="重试员工")
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
    published = publish_import_for_review(db_session, imported)
    settings = _live_dingtalk_settings()

    first = stage_review_deliveries(
        db_session, batch_id=published.payroll_batch_id, settings=settings
    )
    failed = db_session.scalars(select(DingTalkDelivery)).one()
    assert first.configuration_failures == 1
    assert failed.status is DingTalkDeliveryStatus.FAILED
    assert failed.error_code == "MISSING_ELIGIBLE_RECIPIENT"

    still_missing = stage_review_deliveries(
        db_session, batch_id=published.payroll_batch_id, settings=settings
    )
    assert still_missing.configuration_failures == 1
    assert still_missing.existing == 1
    assert db_session.scalar(select(func.count()).select_from(DingTalkDelivery)) == 1

    manager = _reviewer(
        db_session,
        username="retry-manager",
        store_id=store.id,
        department=Department.DINING,
    )
    manager.dingtalk_user_id = "provider-retry-manager"
    db_session.flush()

    recovered = stage_review_deliveries(
        db_session, batch_id=published.payroll_batch_id, settings=settings
    )
    assert recovered.routed == 1
    assert recovered.configuration_failures == 0
    assert recovered.existing == 1
    assert recovered.pending_delivery_ids == (failed.id,)
    assert db_session.scalar(select(func.count()).select_from(DingTalkDelivery)) == 1
    db_session.refresh(failed)
    assert failed.recipient_user_id == manager.id
    assert failed.status is DingTalkDeliveryStatus.PENDING
    assert failed.error_code is None

    staged_again = stage_review_deliveries(
        db_session, batch_id=published.payroll_batch_id, settings=settings
    )
    assert staged_again.existing == 1
    assert staged_again.pending_delivery_ids == (failed.id,)
    assert db_session.scalar(select(func.count()).select_from(DingTalkDelivery)) == 1


@pytest.mark.parametrize(
    ("department", "gross", "net", "expected"),
    [
        ("其他", "5000", "4400", "复核部门"),
        ("厅面", None, "4400", "应发工资"),
        ("厨房", "5000", None, "实发工资"),
        ("厅面", "-1", "0", "不可以为负数"),
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


def test_publish_blocks_attendance_days_above_the_calendar_month(db_session):
    store = OrgUnit(code="DAYS-S1", name="天数校验门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="DAYS-E1", name="员工")
    row = _salary_row(
        emp_no=employee.emp_no,
        name=employee.name,
        store_name=store.name,
        department="厅面",
        gross="5000",
        net="4400",
    )
    row.fields["实际计薪出勤天数"] = "32"
    row.money["实际计薪出勤天数"] = Decimal("32")
    imported = _confirmed_import(db_session, store, [row])

    with pytest.raises(ImportPublishError, match="不可以超过当月天数"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0


@pytest.mark.parametrize("oversized", ["1e100", "1000000000000"])
def test_publish_rejects_amounts_outside_numeric_14_2(db_session, oversized):
    store = OrgUnit(code=f"AMOUNT-{oversized}", name="金额范围门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="AMOUNT-E1", name="金额员工")
    imported = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厅面",
                gross=oversized,
                net="4400",
            )
        ],
    )

    with pytest.raises(ImportPublishError, match="金额范围"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(PayrollResult)) == 0


def test_publish_rechecks_the_current_import_field_allowlist(db_session):
    store = OrgUnit(code="PUBLISH-FIELD-S1", name="发布字段门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="PUBLISH-FIELD-E1", name="发布字段员工")
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
    record = db_session.scalars(
        select(SalaryRecord).where(SalaryRecord.import_batch_id == imported.id)
    ).one()
    record.fields = {**record.fields, "身份证号": "440101199001011234"}
    db_session.flush()

    with pytest.raises(ImportPublishError, match="模板未支持.*身份证号"):
        publish_import_for_review(db_session, imported)

    assert db_session.scalar(select(func.count()).select_from(PayrollBatch)) == 0


def test_reopened_import_preserves_all_locked_identity_snapshots(db_session):
    store = OrgUnit(code="IDENTITY-S1", name="身份快照门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = _employee(db_session, store, emp_no="IDENTITY-E1", name="原姓名")
    employee.id_card = "440101199001011234"
    employee.bank_account = "6222000000000000"
    employee.social_city = "广州"
    first_import = _confirmed_import(
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
    first = publish_import_for_review(db_session, first_import)
    payroll_batch = db_session.get(PayrollBatch, first.payroll_batch_id)
    assert payroll_batch is not None

    employee.emp_no = "IDENTITY-E1-RENAMED"
    employee.name = "新姓名"
    employee.id_card = "440101199901019999"
    employee.bank_account = "6222999999999999"
    employee.social_city = "深圳"
    payroll_batch.version = 2
    payroll_batch.status = BatchStatus.DRAFT
    db_session.flush()
    second_import = _confirmed_import(
        db_session,
        store,
        [
            _salary_row(
                emp_no=employee.emp_no,
                name=employee.name,
                store_name=store.name,
                department="厅面",
                gross="5100",
                net="4500",
            )
        ],
    )

    second = publish_import_for_review(db_session, second_import)
    result = db_session.scalars(
        select(PayrollResult).where(
            PayrollResult.batch_id == second.payroll_batch_id,
            PayrollResult.batch_version == 2,
        )
    ).one()

    assert result.emp_no_snapshot == "IDENTITY-E1"
    assert result.employee_name_snapshot == "原姓名"
    assert result.id_card_snapshot == "440101199001011234"
    assert result.bank_account_snapshot == "6222000000000000"
    assert result.social_city_snapshot == "广州"


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
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.PENDING_STORE_CONFIRM,
    )
    db_session.add(existing)
    db_session.flush()

    with pytest.raises(ImportPublishError, match="草稿"):
        publish_import_for_review(db_session, imported)
