"""无需数据库的批次 API 入参验证测试。"""

import pytest
from pydantic import ValidationError

from app.models.payroll_result import DisputeStatus
from app.routers.batch import BatchCreate, ResolveBody


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


def test_batch_create_rejects_invalid_month_and_reversed_attendance_dates() -> None:
    with pytest.raises(ValidationError):
        BatchCreate(
            period="2026-13",
            attendance_start="2026-05-01",
            attendance_end="2026-05-31",
        )

    with pytest.raises(ValidationError):
        BatchCreate(
            period="2026-05",
            attendance_start="2026-05-31",
            attendance_end="2026-05-01",
        )
