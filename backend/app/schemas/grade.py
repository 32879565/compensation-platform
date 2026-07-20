from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class JobGradeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    rank: int = 0


class JobGradeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    rank: int | None = None


class JobGradeOut(BaseModel):
    id: int
    code: str
    name: str
    rank: int

    model_config = {"from_attributes": True}


class SalaryBandCreate(BaseModel):
    job_grade_id: int
    band_min: Decimal = Field(ge=0)
    band_mid: Decimal = Field(ge=0)
    band_max: Decimal = Field(ge=0)
    effective_from: date


class SalaryBandOut(BaseModel):
    id: int
    job_grade_id: int
    band_min: Decimal
    band_mid: Decimal
    band_max: Decimal
    effective_from: date

    model_config = {"from_attributes": True}
