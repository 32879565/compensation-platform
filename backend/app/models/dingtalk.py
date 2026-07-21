"""Sandbox-first DingTalk delivery and tightly scoped compensation appeals.

The application never persists a payroll message body.  A delivery records the
authorized recipient and operational outcome only; payroll facts are resolved
from the immutable batch snapshot only when a configured live transport is
introduced.  This keeps sandbox/retry operations useful without creating a
second, less protected copy of salary data.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.employee import Department


class DingTalkDeliveryKind(enum.StrEnum):
    PAYROLL_REVIEW = "PAYROLL_REVIEW"
    APPEAL_STATUS = "APPEAL_STATUS"


class DingTalkDeliveryStatus(enum.StrEnum):
    PENDING = "PENDING"
    SANDBOXED = "SANDBOXED"
    SENT = "SENT"
    FAILED = "FAILED"


class DingTalkAttendanceSyncStatus(enum.StrEnum):
    """Lifecycle of a cached, read-only DingTalk attendance refresh."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AppealStatus(enum.StrEnum):
    """The business outcome of a manager's appeal.

    A correction-required outcome deliberately does not mutate payroll data:
    the existing S13 source-correction workflow remains the only path that can
    recalculate a batch.  This preserves versioned payroll history and avoids
    pretending that a generic approval decision itself is an accounting entry.
    """

    PENDING = "PENDING"
    UPHELD = "UPHELD"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"


class AppealCorrectionWorkStatus(enum.StrEnum):
    """The next controlled step after an appeal was approved for correction.

    A work item is deliberately not a payroll mutation.  The correction
    operator must use the existing source-data correction and rerun workflow;
    this queue preserves the immutable review round and makes an approved
    appeal impossible to silently disappear between approval and that work.
    """

    PENDING_TRIAGE = "PENDING_TRIAGE"
    PENDING_REOPEN = "PENDING_REOPEN"
    HISTORICAL_SETTLEMENT_REQUIRED = "HISTORICAL_SETTLEMENT_REQUIRED"


class DingTalkDelivery(Base, TimestampMixin):
    """One idempotent, audited notification attempt without payroll payload data."""

    __tablename__ = "dingtalk_delivery"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_dingtalk_delivery_idempotency"),)

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )
    recipient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), nullable=True, index=True
    )
    kind: Mapped[DingTalkDeliveryKind] = mapped_column(
        Enum(DingTalkDeliveryKind, name="dingtalk_delivery_kind"), nullable=False, index=True
    )
    status: Mapped[DingTalkDeliveryStatus] = mapped_column(
        Enum(DingTalkDeliveryStatus, name="dingtalk_delivery_status"),
        nullable=False,
        default=DingTalkDeliveryStatus.PENDING,
        index=True,
    )
    # Stable internal error codes only (for example MISSING_RECIPIENT).  Do
    # not store a remote response body because it can echo sensitive content.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Provider task id is operational metadata only; request ids, access
    # tokens, message bodies, and recipient provider ids are never persisted.
    provider_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)


class DingTalkAttendanceSync(Base, TimestampMixin):
    """One refresh state per payroll period, without raw provider punch data."""

    __tablename__ = "dingtalk_attendance_sync"
    __table_args__ = (
        CheckConstraint(
            "matched_employees >= 0 AND employees_with_records >= 0 "
            "AND total_records >= 0 AND ambiguous_directory_users >= 0 "
            "AND unmatched_directory_users >= 0",
            name="ck_dingtalk_attendance_sync_nonnegative_counts",
        ),
        UniqueConstraint("period", name="uq_dingtalk_attendance_sync_period"),
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    status: Mapped[DingTalkAttendanceSyncStatus] = mapped_column(
        Enum(DingTalkAttendanceSyncStatus, name="dingtalk_attendance_sync_status"),
        nullable=False,
        default=DingTalkAttendanceSyncStatus.QUEUED,
        index=True,
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), nullable=True, index=True
    )
    matched_employees: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    employees_with_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous_directory_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unmatched_directory_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DingTalkAttendanceSnapshot(Base, TimestampMixin):
    """Per-employee status counts; raw provider IDs and punch times are excluded."""

    __tablename__ = "dingtalk_attendance_snapshot"
    __table_args__ = (
        CheckConstraint(
            "record_count >= 0 AND normal_count >= 0 AND late_count >= 0 "
            "AND early_count >= 0 AND absent_count >= 0 "
            "AND not_signed_count >= 0 AND other_count >= 0",
            name="ck_dingtalk_attendance_snapshot_nonnegative_counts",
        ),
        UniqueConstraint(
            "employee_id",
            "period",
            name="uq_dingtalk_attendance_snapshot_employee_period",
        ),
    )

    sync_id: Mapped[int] = mapped_column(
        ForeignKey("dingtalk_attendance_sync.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    early_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    absent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_signed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    other_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompAppeal(Base, TimestampMixin):
    """A manager appeal tied to one delivered (store, department) notification."""

    __tablename__ = "comp_appeal"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_comp_appeal_dedupe"),)

    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("dingtalk_delivery.id"),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id"), nullable=True, index=True
    )
    requester_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[AppealStatus] = mapped_column(
        Enum(AppealStatus, name="appeal_status"),
        nullable=False,
        default=AppealStatus.PENDING,
        index=True,
    )
    resolution: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    approval_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_instance.id"), nullable=True, unique=True, index=True
    )


class CompAppealCorrectionWorkItem(Base, TimestampMixin):
    """An auditable, fail-closed handoff into the payroll correction workflow.

    ``source_batch_version`` is never updated.  In particular, a decision
    about an old review notification cannot be redirected at a newer payroll
    round.  The work item contains no payroll message, free-text appeal reason,
    or amount; those remain in the protected appeal/adjustment records.
    """

    __tablename__ = "comp_appeal_correction_work_item"
    __table_args__ = (UniqueConstraint("appeal_id", name="uq_appeal_correction_work_item"),)

    appeal_id: Mapped[int] = mapped_column(ForeignKey("comp_appeal.id"), nullable=False, index=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    source_batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id"), nullable=True, index=True
    )
    status: Mapped[AppealCorrectionWorkStatus] = mapped_column(
        Enum(AppealCorrectionWorkStatus, name="appeal_correction_work_status"),
        nullable=False,
        default=AppealCorrectionWorkStatus.PENDING_TRIAGE,
        index=True,
    )
