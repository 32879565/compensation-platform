from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


def _trim_nonblank(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class JobGradeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    rank: int = 0

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return _trim_nonblank(value, field_name="code")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _trim_nonblank(value, field_name="name")


class JobGradeUpdate(BaseModel):
    expected_version: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    rank: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_updated_name(cls, value: object) -> object:
        if value is None:
            raise ValueError("name cannot be null")
        if isinstance(value, str):
            return _trim_nonblank(value, field_name="name")
        return value

    @field_validator("rank", mode="before")
    @classmethod
    def reject_null_rank(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    @model_validator(mode="after")
    def includes_change(self) -> JobGradeUpdate:
        if self.model_fields_set == {"expected_version"}:
            raise ValueError("at least one grade field must be changed")
        return self


class JobGradeLifecycle(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int | None = Field(default=None, gt=0)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return _trim_nonblank(value, field_name="reason")


class JobGradeOut(BaseModel):
    id: int
    code: str
    name: str
    rank: int
    version: int
    is_active: bool
    deactivated_at: datetime | None

    model_config = {"from_attributes": True}


class SalaryBandCreate(BaseModel):
    # Kept optional for backwards compatibility.  The path remains canonical;
    # when supplied, the body identifier must agree with it.
    job_grade_id: int | None = Field(default=None, gt=0)
    band_min: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    band_mid: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    band_max: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    effective_from: date


class SalaryBandOut(BaseModel):
    id: int
    job_grade_id: int
    band_min: Decimal
    band_mid: Decimal
    band_max: Decimal
    effective_from: date
    effective_to: date | None

    model_config = {"from_attributes": True}
