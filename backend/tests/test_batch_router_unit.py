"""无需数据库的批次 API 入参验证测试。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.payroll_result import DisputeStatus
from app.routers.approval import SalaryAdjustmentCreate
from app.routers.attendance import AttendanceBody
from app.routers.batch import BatchCreate, DisputeBody, ResolveBody, SupplementBody, UnlockBody
from app.routers.comp import InitialStructureComponentBody, SetComponentBody
from app.routers.holiday import HolidayWorkBody
from app.routers.payroll_adjustment import MonthlyAdjustmentBody


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
        attachment_url="https://evidence.example/approved-change.pdf",
    )

    assert body.attendance_changes is not None
    assert body.attendance_changes.rest_days == 2

    with pytest.raises(ValidationError):
        ResolveBody(
            decision=DisputeStatus.APPROVED,
            resolution="有源数据变更但没有证明附件",
            attendance_changes={"rest_days": "2"},
        )

    with pytest.raises(ValidationError):
        ResolveBody(
            decision=DisputeStatus.APPROVED,
            resolution="核实后应修正考勤",
            attendance_changes={"rest_days": "1.234"},
        )


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

    with pytest.raises(ValidationError):
        BatchCreate(
            period="2026-05",
            attendance_start="2026-04-01",
            attendance_end="2026-04-30",
        )

    with pytest.raises(ValidationError):
        BatchCreate(
            period="2026-05",
            attendance_start="2026-04-26",
            attendance_end="2026-05-25",
        )

    with pytest.raises(ValidationError):
        BatchCreate(
            period="2026-05",
            attendance_start="2026-05-01",
            attendance_end="2026-05-25",
        )


def test_audited_attendance_values_and_reasons_are_canonical() -> None:
    with pytest.raises(ValidationError):
        AttendanceBody(expected_days="22", actual_days="22", worked_hours="1.234")

    body = AttendanceBody(
        expected_days="22",
        actual_days="22",
        correction_reason="  correction evidence reviewed  ",
        expected_days_adjust_reason="   ",
    )
    assert body.correction_reason == "correction evidence reviewed"
    assert body.expected_days_adjust_reason is None

    with pytest.raises(ValidationError):
        UnlockBody(reason="   ")

    with pytest.raises(ValidationError):
        DisputeBody(employee_id=1, salary_item="ATTEND_WAGE", opinion="   ")


@pytest.mark.parametrize(
    "factory",
    [
        lambda url: AttendanceBody(expected_days="22", actual_days="22", attachment_url=url),
        lambda url: ResolveBody(
            decision=DisputeStatus.REJECTED,
            resolution="核实无误",
            attachment_url=url,
        ),
        lambda url: SupplementBody(note="补充证明", attachment_url=url),
        lambda url: HolidayWorkBody(worked=True, evidence_url=url),
        lambda url: MonthlyAdjustmentBody(
            amount="1",
            reason="已审批补发",
            attachment_url=url,
            taxable=True,
            in_social_base=True,
            in_housing_base=True,
        ),
        lambda url: SetComponentBody(
            amount="1", effective_from=date(2026, 5, 1), attachment_url=url
        ),
        lambda url: InitialStructureComponentBody(component_id=1, amount="1", attachment_url=url),
        lambda url: SalaryAdjustmentCreate(
            employee_id=1,
            component_id=1,
            amount="1",
            effective_from=date(2026, 5, 1),
            reason="已审批调整",
            attachment_url=url,
        ),
    ],
    ids=[
        "attendance",
        "dispute",
        "dispute-supplement",
        "holiday",
        "monthly-adjustment",
        "salary-component",
        "initial-structure",
        "approval",
    ],
)
def test_audit_evidence_urls_require_credential_free_https(factory) -> None:
    with pytest.raises(ValidationError):
        factory("javascript:alert(document.domain)")

    with pytest.raises(ValidationError):
        factory("data:text/html,<script>alert(1)</script>")

    with pytest.raises(ValidationError):
        factory("http://files.example.test/evidence.pdf")

    with pytest.raises(ValidationError):
        factory("https://trusted.example@evil.example/evidence.pdf")

    model = factory("  https://files.example.test/evidence.pdf  ")
    field = "evidence_url" if isinstance(model, HolidayWorkBody) else "attachment_url"
    assert getattr(model, field) == "https://files.example.test/evidence.pdf"
