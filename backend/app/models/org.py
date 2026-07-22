import enum

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin


class OrgType(enum.StrEnum):
    GROUP = "GROUP"  # 集团
    REGION = "REGION"  # 区域
    STORE = "STORE"  # 门店


class OrgUnit(Base, TimestampMixin, SoftDeleteMixin):
    """组织单元：集团/区域/门店 的自引用层级树。门店的 city 决定社保口径（S12）。"""

    __tablename__ = "org_unit"
    __table_args__ = (
        CheckConstraint(
            "dingtalk_dept_id IS NULL OR dingtalk_dept_id > 0",
            name="ck_org_unit_dingtalk_dept_id_positive",
        ),
        UniqueConstraint("dingtalk_dept_id", name="uq_org_unit_dingtalk_dept_id"),
    )

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    type: Mapped[OrgType] = mapped_column(Enum(OrgType, name="org_type"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    dingtalk_dept_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )

    parent: Mapped["OrgUnit | None"] = relationship(
        "OrgUnit", remote_side="OrgUnit.id", back_populates="children"
    )
    children: Mapped[list["OrgUnit"]] = relationship(
        "OrgUnit", back_populates="parent", cascade="all"
    )
