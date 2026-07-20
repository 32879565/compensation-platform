"""复核范围管理 API 的纯入参验证。"""

import pytest
from pydantic import ValidationError

from app.routers.users import ReviewScopeReplaceBody


def test_review_scope_replacement_rejects_duplicate_org_department_pairs() -> None:
    with pytest.raises(ValidationError):
        ReviewScopeReplaceBody(
            scopes=[
                {"org_unit_id": 10, "department": "DINING"},
                {"org_unit_id": 10, "department": "DINING"},
            ]
        )


def test_review_scope_replacement_accepts_distinct_departments_in_one_store() -> None:
    body = ReviewScopeReplaceBody(
        scopes=[
            {"org_unit_id": 10, "department": "DINING"},
            {"org_unit_id": 10, "department": "KITCHEN"},
        ]
    )

    assert len(body.scopes) == 2
