"""工资结果必须携带可复现的规则与输入快照。"""

from app.models.payroll_result import PayrollResult


def test_payroll_result_persists_rule_input_and_batch_revision_snapshots() -> None:
    columns = set(PayrollResult.__table__.columns.keys())

    assert {"rule_version", "input_snapshot", "batch_version", "warnings"} <= columns
