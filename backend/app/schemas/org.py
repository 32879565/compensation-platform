from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.org import OrgType


class OrgUnitCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    type: OrgType
    parent_id: int | None = None
    city: str | None = Field(default=None, max_length=32)


class OrgUnitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    city: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=16)


class OrgUnitOut(BaseModel):
    id: int
    code: str
    name: str
    type: OrgType
    parent_id: int | None
    city: str | None
    status: str

    model_config = {"from_attributes": True}


class OrgTreeNode(OrgUnitOut):
    children: list[OrgTreeNode] = Field(default_factory=list)


OrgTreeNode.model_rebuild()
