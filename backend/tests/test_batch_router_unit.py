"""无需数据库的批次 API 入参验证测试。"""

from pydantic import ValidationError
import pytest

from app.models.payroll_result import DisputeStatus
from app.routers.batch import ResolveBody


def test_approved_dispute_requires_a_bounded_source_attendance_change() -> None:
    with pytest.raises(ValidationError):
        ResolveBody(
            decision=DisputeStatus.APPROVED,
            resolution="核实后应修正考勤",
        )

    with pytest.raises(ValidationError):
        ResolveBody(
            decision=DisputeStatus.APPROVED,
            resolution="核实后应修正考勤",
            attendance_changes={"rest_days": "32"},
        )

    body = ResolveBody(
        decision=DisputeStatus.APPROVED,
        resolution="核实后应修正考勤",
        attendance_changes={"rest_days": "2"},
    )

    assert body.attendance_changes is not None
    assert body.attendance_changes.rest_days == 2


def test_non_approval_resolution_cannot_smuggle_source_changes() -> None:
    with pytest.raises(ValidationError):
        ResolveBody(
            decision=DisputeStatus.REJECTED,
            resolution="核实无误",
            attendance_changes={"rest_days": "2"},
        )
