"""Generic, auditable approval records and salary-adjustment business documents."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin
from app.models.grade import MONEY


class ApprovalBusinessType(enum.StrEnum):
    """Business documents supported by the reusable approval state machine."""

    SALARY_ADJUSTMENT = "SALARY_ADJUSTMENT"
    COMP_APPEAL = "COMP_APPEAL"  # Reserved for the DingTalk appeal integration.


class ApprovalInstanceStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ApprovalActionType(enum.StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"


class SalaryAdjustmentStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ApprovalFlow(Base, TimestampMixin, SoftDeleteMixin):
    """A routable flow selected by business type, organization and amount."""

    __tablename__ = "approval_flow"
    __table_args__ = (
        CheckConstraint(
            "min_amount IS NULL OR min_amount >= 0", name="ck_approval_flow_min_amount"
        ),
        CheckConstraint(
            "max_amount IS NULL OR max_amount >= 0", name="ck_approval_flow_max_amount"
        ),
        CheckConstraint(
            "min_amount IS NULL OR max_amount IS NULL OR max_amount >= min_amount",
            name="ck_approval_flow_amount_range",
        ),
        Index(
            "ix_approval_flow_routing",
            "business_type",
            "org_unit_id",
            "is_active",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    business_type: Mapped[ApprovalBusinessType] = mapped_column(
        Enum(ApprovalBusinessType, name="approval_business_type"), nullable=False, index=True
    )
    # A null root is the group-wide fallback.  A non-null root applies to that
    # organization subtree and wins over a group-wide candidate.
    org_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    min_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    max_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class ApprovalStep(Base, TimestampMixin):
    __tablename__ = "approval_step"
    __table_args__ = (
        UniqueConstraint("flow_id", "step_order", name="uq_approval_step_flow_order"),
    )

    flow_id: Mapped[int] = mapped_column(ForeignKey("approval_flow.id"), nullable=False, index=True)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Role codes are durable flow configuration.  Resolution still checks the
    # actor's permission and organization scope at action time.
    role_code: Mapped[str] = mapped_column(String(32), nullable=False)


class ApprovalInstance(Base, TimestampMixin):
    """One active/historical execution of a configured flow for a document."""

    __tablename__ = "approval_instance"
    __table_args__ = (
        UniqueConstraint("business_type", "business_id", name="uq_approval_instance_business"),
        Index("ix_approval_instance_todo", "status", "current_step_order", "org_unit_id"),
    )

    flow_id: Mapped[int] = mapped_column(ForeignKey("approval_flow.id"), nullable=False, index=True)
    business_type: Mapped[ApprovalBusinessType] = mapped_column(
        Enum(ApprovalBusinessType, name="approval_business_type"), nullable=False, index=True
    )
    # A generic business reference keeps the approval engine reusable.  The
    # owning business service validates its concrete document before it creates
    # an instance.
    business_id: Mapped[int] = mapped_column(nullable=False)
    requester_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[ApprovalInstanceStatus] = mapped_column(
        Enum(ApprovalInstanceStatus, name="approval_instance_status"),
        nullable=False,
        default=ApprovalInstanceStatus.PENDING,
        server_default=ApprovalInstanceStatus.PENDING.value,
        index=True,
    )
    current_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flow_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalAction(Base, TimestampMixin):
    __tablename__ = "approval_action"
    __table_args__ = (
        UniqueConstraint("instance_id", "step_order", name="uq_approval_action_step"),
        Index("ix_approval_action_instance", "instance_id", "created_at"),
    )

    instance_id: Mapped[int] = mapped_column(ForeignKey("approval_instance.id"), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[ApprovalActionType] = mapped_column(
        Enum(ApprovalActionType, name="approval_action_type"), nullable=False
    )
    actor_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    comment: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class SalaryAdjustment(Base, TimestampMixin):
    """A proposed employee-component amount that only becomes effective on approval."""

    __tablename__ = "salary_adjustment"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_salary_adjustment_amount"),
        Index("ix_salary_adjustment_org_status", "org_unit_id", "status"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("salary_component_def.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    attachment_url: Mapped[str] = mapped_column(String(512), nullable=False)
    requester_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    status: Mapped[SalaryAdjustmentStatus] = mapped_column(
        Enum(SalaryAdjustmentStatus, name="salary_adjustment_status"),
        nullable=False,
        default=SalaryAdjustmentStatus.DRAFT,
        server_default=SalaryAdjustmentStatus.DRAFT.value,
        index=True,
    )
    before_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    approval_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_instance.id"), nullable=True, unique=True, index=True
    )
    applied_structure_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee_salary_structure.id"), nullable=True, unique=True
    )
