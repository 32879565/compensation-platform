"""Shared, exact DingTalk organization rules used at sync and access time."""

from __future__ import annotations

import unicodedata

from app.models.employee import Department


def normalize_manager_title(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def manager_department_for_title(
    title: str | None,
    *,
    dining_titles: frozenset[str],
    kitchen_titles: frozenset[str],
) -> Department | None:
    normalized = normalize_manager_title(title)
    normalized_dining = {normalize_manager_title(candidate) for candidate in dining_titles}
    normalized_kitchen = {normalize_manager_title(candidate) for candidate in kitchen_titles}
    if normalized in normalized_dining:
        return Department.DINING
    if normalized in normalized_kitchen:
        return Department.KITCHEN
    return None
