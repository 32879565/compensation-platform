"""Add unmatched historical stores to a dedicated organization branch.

Revision ID: q6f9a2c8d753
Revises: p5e8f3a1b742
Create Date: 2026-07-20 23:50:00.000000

This is a data-only migration.  Historical salary rows do not contain a
reliable current geographic region, so missing stores are deliberately placed
under a clearly labelled historical region instead of guessing their present
organization assignment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa

from alembic import op

revision: str = "q6f9a2c8d753"
down_revision: str | None = "p5e8f3a1b742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REGION_CODE = "HIST-REGION-PENDING"
_REGION_NAME = "历史门店（待归属）"
_STORE_CODE_PREFIX = "HIST-ST-"


def _store_code(store_name: str) -> str:
    digest = sha256(store_name.encode("utf-8")).hexdigest()[:24].upper()
    return f"{_STORE_CODE_PREFIX}{digest}"


def _unmatched_historical_store_names(bind: sa.Connection) -> list[str]:
    blank_count = bind.scalar(sa.text("""
            SELECT count(*)
            FROM salary_record
            WHERE source = 'HISTORICAL'
              AND org_unit_id IS NULL
              AND btrim(store_name) = ''
            """))
    if blank_count:
        raise RuntimeError(
            "Cannot create historical organization nodes for blank store names "
            f"({blank_count} rows)."
        )
    return list(bind.scalars(sa.text("""
                SELECT DISTINCT store_name
                FROM salary_record
                WHERE source = 'HISTORICAL'
                  AND org_unit_id IS NULL
                ORDER BY store_name
                """)))


def _assert_store_names_are_new(bind: sa.Connection, store_names: list[str]) -> None:
    if not store_names:
        return

    ids_by_name: dict[str, list[int]] = defaultdict(list)
    for org_id, name in bind.execute(sa.text("""
            SELECT id, name
            FROM org_unit
            WHERE type = 'STORE' AND is_deleted = false
            """)):
        if name in store_names:
            ids_by_name[name].append(org_id)

    existing_names = sorted(ids_by_name)
    if existing_names:
        raise RuntimeError(
            "Historical store backfill only creates missing organizations, but these "
            "unmatched salary store names already exist: " + ", ".join(existing_names)
        )


def _group_id(bind: sa.Connection) -> int:
    group_ids = list(bind.scalars(sa.text("""
                SELECT id
                FROM org_unit
                WHERE type = 'GROUP' AND is_deleted = false
                ORDER BY id
                """)))
    if len(group_ids) != 1:
        raise RuntimeError(
            "Historical store backfill requires exactly one active GROUP organization; "
            f"found {len(group_ids)}."
        )
    return group_ids[0]


def _historical_region_id(bind: sa.Connection, parent_id: int) -> int:
    existing = bind.execute(
        sa.text("""
            SELECT id, parent_id, type, is_deleted
            FROM org_unit
            WHERE code = :code
            """),
        {"code": _REGION_CODE},
    ).one_or_none()
    if existing is not None:
        if existing.parent_id != parent_id or existing.type != "REGION" or existing.is_deleted:
            raise RuntimeError(
                f"Organization code {_REGION_CODE} already exists with incompatible attributes."
            )
        return existing.id

    return bind.scalar(
        sa.text("""
            INSERT INTO org_unit (parent_id, type, name, code, city, status)
            VALUES (:parent_id, 'REGION', :name, :code, NULL, 'HISTORICAL')
            RETURNING id
            """),
        {"parent_id": parent_id, "name": _REGION_NAME, "code": _REGION_CODE},
    )


def _create_and_link_stores(
    bind: sa.Connection,
    *,
    region_id: int,
    store_names: list[str],
) -> None:
    existing_codes = {
        code: (org_id, parent_id, org_type, name, is_deleted)
        for org_id, parent_id, org_type, name, code, is_deleted in bind.execute(
            sa.text("SELECT id, parent_id, type, name, code, is_deleted FROM org_unit")
        )
    }
    children_by_name: dict[str, list[int]] = defaultdict(list)
    for org_id, name in bind.execute(
        sa.text("""
            SELECT id, name
            FROM org_unit
            WHERE parent_id = :region_id AND type = 'STORE' AND is_deleted = false
            """),
        {"region_id": region_id},
    ):
        children_by_name[name].append(org_id)

    duplicate_children = sorted(name for name, ids in children_by_name.items() if len(ids) > 1)
    if duplicate_children:
        raise RuntimeError(
            "Historical region contains duplicate active store names: "
            + ", ".join(duplicate_children)
        )

    for store_name in store_names:
        child_ids = children_by_name.get(store_name)
        if child_ids:
            store_id = child_ids[0]
        else:
            code = _store_code(store_name)
            code_owner = existing_codes.get(code)
            if code_owner is not None:
                owner_id, owner_parent, owner_type, owner_name, owner_deleted = code_owner
                if (
                    owner_parent != region_id
                    or owner_type != "STORE"
                    or owner_name != store_name
                    or owner_deleted
                ):
                    raise RuntimeError(
                        f"Generated historical store code collision for {store_name}: {code}."
                    )
                store_id = owner_id
            else:
                store_id = bind.scalar(
                    sa.text("""
                        INSERT INTO org_unit (parent_id, type, name, code, city, status)
                        VALUES (:parent_id, 'STORE', :name, :code, NULL, 'HISTORICAL')
                        RETURNING id
                        """),
                    {"parent_id": region_id, "name": store_name, "code": code},
                )
                existing_codes[code] = (
                    store_id,
                    region_id,
                    "STORE",
                    store_name,
                    False,
                )
                children_by_name[store_name] = [store_id]

        bind.execute(
            sa.text("""
                UPDATE salary_record
                SET org_unit_id = :store_id
                WHERE source = 'HISTORICAL'
                  AND org_unit_id IS NULL
                  AND store_name = :store_name
                """),
            {"store_id": store_id, "store_name": store_name},
        )


def upgrade() -> None:
    bind = op.get_bind()
    unmatched_names = _unmatched_historical_store_names(bind)
    if not unmatched_names:
        return

    # The preflight makes the downgrade exactly reversible: every link changed
    # by this revision points to an organization node created by this revision.
    _assert_store_names_are_new(bind, unmatched_names)

    region_id = _historical_region_id(bind, _group_id(bind))
    _create_and_link_stores(bind, region_id=region_id, store_names=unmatched_names)

    remaining = bind.scalar(sa.text("""
            SELECT count(*)
            FROM salary_record
            WHERE source = 'HISTORICAL' AND org_unit_id IS NULL
            """))
    if remaining:
        raise RuntimeError(f"Historical store backfill left {remaining} salary rows unmatched.")


def downgrade() -> None:
    bind = op.get_bind()
    region = bind.execute(
        sa.text("SELECT id FROM org_unit WHERE code = :code"),
        {"code": _REGION_CODE},
    ).one_or_none()
    if region is None:
        return

    unmanaged_children = bind.scalar(
        sa.text("""
            SELECT count(*)
            FROM org_unit
            WHERE parent_id = :region_id
              AND code NOT LIKE :generated_prefix
            """),
        {"region_id": region.id, "generated_prefix": f"{_STORE_CODE_PREFIX}%"},
    )
    if unmanaged_children:
        raise RuntimeError(
            "Refusing to downgrade historical stores because the generated region "
            f"contains {unmanaged_children} non-migration organization nodes."
        )

    generated_store_ids = list(
        bind.scalars(
            sa.text("""
                SELECT id
                FROM org_unit
                WHERE parent_id = :region_id
                  AND code LIKE :generated_prefix
                ORDER BY id
                """),
            {"region_id": region.id, "generated_prefix": f"{_STORE_CODE_PREFIX}%"},
        )
    )
    for store_id in generated_store_ids:
        bind.execute(
            sa.text("""
                UPDATE salary_record
                SET org_unit_id = NULL
                WHERE source = 'HISTORICAL' AND org_unit_id = :store_id
                """),
            {"store_id": store_id},
        )
        bind.execute(
            sa.text("DELETE FROM org_unit WHERE id = :store_id"),
            {"store_id": store_id},
        )

    bind.execute(
        sa.text("DELETE FROM org_unit WHERE id = :region_id"),
        {"region_id": region.id},
    )
