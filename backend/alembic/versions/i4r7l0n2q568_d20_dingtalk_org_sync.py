"""D20: direct DingTalk organization and reviewer synchronization.

Revision ID: i4r7l0n2q568
Revises: h3q6k9m1p457
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence

import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "i4r7l0n2q568"
down_revision: str | None = "h3q6k9m1p457"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


department_enum = postgresql.ENUM(
    "DINING", "KITCHEN", "OTHER", name="department", create_type=False
)
org_type_enum = postgresql.ENUM("GROUP", "REGION", "STORE", name="org_type", create_type=False)
dingtalk_delivery_status_enum = postgresql.ENUM(
    "PENDING", "SANDBOXED", "SENT", "FAILED", name="dingtalk_delivery_status", create_type=False
)


def _sync_enums(schema: str) -> tuple[postgresql.ENUM, ...]:
    """Build D20-local enums so temporary-schema migrations never reuse public types."""

    return (
        postgresql.ENUM(
            "PREVIEWED",
            "APPLIED",
            "STALE",
            name="dingtalk_org_sync_batch_status",
            schema=schema,
            create_type=False,
        ),
        postgresql.ENUM(
            "MANUAL",
            "SCHEDULED",
            name="dingtalk_org_sync_trigger",
            schema=schema,
            create_type=False,
        ),
        postgresql.ENUM(
            "REGION",
            "STORE",
            "REVIEWER",
            name="dingtalk_org_sync_item_kind",
            schema=schema,
            create_type=False,
        ),
        postgresql.ENUM(
            "LINK",
            "CREATE",
            "UPDATE",
            "ACTIVATE",
            "DEACTIVATE",
            "ASSIGN_SCOPE",
            "REMOVE_SCOPE",
            "NO_CHANGE",
            name="dingtalk_org_sync_action",
            schema=schema,
            create_type=False,
        ),
        postgresql.ENUM(
            "READY",
            "CONFLICT",
            "APPLIED",
            "IGNORED",
            name="dingtalk_org_sync_item_status",
            schema=schema,
            create_type=False,
        ),
    )


def _decrypt_legacy_pii_v1(token: str, *, key: str) -> str:
    """Decode the immutable Fernet format used before D20.

    Keep this implementation in the migration: replaying historical migrations
    must not silently change when the application's current crypto helpers do.
    """

    derived_key = hashlib.sha256(key.encode("utf-8")).digest()
    cipher = Fernet(base64.urlsafe_b64encode(derived_key))
    return cipher.decrypt(token.encode("ascii")).decode("utf-8")


def _blind_index_dingtalk_user_id_v1(value: str, *, key: str) -> str:
    """Return the immutable v1 domain-separated provider identity digest."""

    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("DingTalk user identifier is invalid")
    if not key:
        raise ValueError("blind-index key is required")
    derived_key = hashlib.sha256(
        b"compensation-platform:dingtalk-user-id:v1\0" + key.encode("utf-8")
    ).digest()
    return hmac.new(derived_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
    ]


def _reject_ambiguous_reviewer_scopes() -> None:
    duplicate_scope_count = op.get_bind().scalar(sa.text("""
            SELECT count(*)
            FROM (
                SELECT org_unit_id, department
                FROM user_review_scope
                GROUP BY org_unit_id, department
                HAVING count(*) > 1
            ) AS duplicate_scope
            """))
    if duplicate_scope_count:
        raise RuntimeError(
            "D20 cannot enforce one reviewer per store and department: "
            "duplicate reviewer scope rows exist; resolve them before upgrading."
        )


def _backfill_dingtalk_user_hashes() -> None:
    """Backfill legacy encrypted reviewer ids and reject duplicate bindings."""

    bind = op.get_bind()
    rows = bind.execute(sa.text("""
            SELECT app_user.id, app_user.employee_id, app_user.dingtalk_user_id,
                   employee.dingtalk_user_id_hash AS employee_hash
            FROM app_user
            LEFT JOIN employee ON employee.id = app_user.employee_id
            WHERE app_user.dingtalk_user_id IS NOT NULL
            ORDER BY app_user.id
            """)).mappings().all()
    if not rows:
        return
    employee_rows = bind.execute(sa.text("""
            SELECT id, dingtalk_user_id_hash
            FROM employee
            WHERE dingtalk_user_id_hash IS NOT NULL
               OR id IN (
                    SELECT employee_id
                    FROM app_user
                    WHERE dingtalk_user_id IS NOT NULL
                      AND employee_id IS NOT NULL
               )
            ORDER BY id
            FOR UPDATE
            """)).mappings().all()
    encryption_key = get_settings().encryption_key
    employee_hash_by_id = {row["id"]: row["dingtalk_user_id_hash"] for row in employee_rows}
    employee_owner_by_hash = {row["dingtalk_user_id_hash"]: row["id"] for row in employee_rows}
    user_owner_by_hash: dict[str, int] = {}
    planned_digest_by_employee: dict[int, str] = {}
    pending: list[tuple[int, int | None, str]] = []
    for row in rows:
        try:
            provider_user_id = _decrypt_legacy_pii_v1(row["dingtalk_user_id"], key=encryption_key)
            if provider_user_id is None:
                raise ValueError("empty provider identifier")
            digest = _blind_index_dingtalk_user_id_v1(
                provider_user_id,
                key=encryption_key,
            )
        except Exception as exc:
            raise RuntimeError("D20 cannot validate an existing DingTalk reviewer binding") from exc
        if digest in user_owner_by_hash:
            raise RuntimeError(
                "D20 found duplicate existing DingTalk reviewer bindings; resolve them first"
            )
        user_owner_by_hash[digest] = row["id"]
        employee_id = row["employee_id"]
        employee_hash = employee_hash_by_id.get(employee_id)
        if employee_hash is not None and employee_hash != digest:
            raise RuntimeError(
                "D20 found an inconsistent employee DingTalk identity; resolve it first"
            )
        existing_employee_owner = employee_owner_by_hash.get(digest)
        if existing_employee_owner is not None and existing_employee_owner != employee_id:
            raise RuntimeError(
                "D20 found a DingTalk reviewer identity owned by another employee; "
                "resolve it first"
            )
        if employee_id is not None:
            planned_digest = planned_digest_by_employee.get(employee_id)
            if planned_digest is not None and planned_digest != digest:
                raise RuntimeError(
                    "D20 found multiple DingTalk reviewer identities for one employee; "
                    "resolve them first"
                )
            planned_digest_by_employee[employee_id] = digest
        pending.append((row["id"], employee_id, digest))

    for user_id, employee_id, digest in pending:
        bind.execute(
            sa.text("UPDATE app_user SET dingtalk_user_id_hash = :digest WHERE id = :user_id"),
            {"digest": digest, "user_id": user_id},
        )
        if employee_id is not None:
            bind.execute(
                sa.text(
                    "UPDATE employee SET dingtalk_user_id_hash = :digest "
                    "WHERE id = :employee_id AND dingtalk_user_id_hash IS NULL"
                ),
                {"digest": digest, "employee_id": employee_id},
            )


def upgrade() -> None:
    _reject_ambiguous_reviewer_scopes()

    bind = op.get_bind()
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    if not isinstance(schema, str) or not schema:
        raise RuntimeError("D20 cannot determine the current PostgreSQL schema")
    sync_enums = _sync_enums(schema)
    (
        dingtalk_org_sync_batch_status_enum,
        dingtalk_org_sync_trigger_enum,
        dingtalk_org_sync_item_kind_enum,
        dingtalk_org_sync_action_enum,
        dingtalk_org_sync_item_status_enum,
    ) = sync_enums
    for enum_type in sync_enums:
        enum_type.create(bind, checkfirst=True)

    op.add_column("org_unit", sa.Column("dingtalk_dept_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_org_unit_dingtalk_dept_id_positive",
        "org_unit",
        "dingtalk_dept_id IS NULL OR dingtalk_dept_id > 0",
    )
    op.create_unique_constraint(
        "uq_org_unit_dingtalk_dept_id",
        "org_unit",
        ["dingtalk_dept_id"],
    )

    op.add_column(
        "app_user",
        sa.Column("dingtalk_user_id_hash", sa.String(length=64), nullable=True),
    )
    _backfill_dingtalk_user_hashes()
    op.create_unique_constraint(
        "uq_app_user_dingtalk_user_id_hash",
        "app_user",
        ["dingtalk_user_id_hash"],
    )
    op.create_unique_constraint(
        "uq_user_review_scope_org_department",
        "user_review_scope",
        ["org_unit_id", "department"],
    )

    op.create_table(
        "dingtalk_org_sync_batch",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            dingtalk_org_sync_batch_status_enum,
            server_default="PREVIEWED",
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("root_config_hash", sa.String(length=64), nullable=False),
        sa.Column("local_baseline_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "trigger",
            dingtalk_org_sync_trigger_enum,
            nullable=False,
            server_default="MANUAL",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "requested_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id"),
            nullable=True,
        ),
        sa.Column(
            "applied_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id"),
            nullable=True,
        ),
        sa.Column("remote_store_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("local_store_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ready_store_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("store_conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ready_reviewer_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reviewer_conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("remote_region_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("local_region_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ready_region_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("region_conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("warning_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "remote_store_count >= 0 AND local_store_count >= 0 "
            "AND ready_store_count >= 0 AND store_conflict_count >= 0 "
            "AND ready_reviewer_count >= 0 AND reviewer_conflict_count >= 0 "
            "AND remote_region_count >= 0 AND local_region_count >= 0 "
            "AND ready_region_count >= 0 AND region_conflict_count >= 0 "
            "AND warning_count >= 0",
            name="ck_dingtalk_org_sync_batch_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "(status = 'APPLIED' AND applied_by_user_id IS NOT NULL AND applied_at IS NOT NULL) "
            "OR (status <> 'APPLIED' AND applied_by_user_id IS NULL AND applied_at IS NULL)",
            name="ck_dingtalk_org_sync_batch_applied_audit",
        ),
    )
    op.create_index(
        "ix_dingtalk_org_sync_batch_public_id",
        "dingtalk_org_sync_batch",
        ["public_id"],
        unique=True,
    )
    for column in ("status", "expires_at", "requested_by_user_id", "applied_by_user_id"):
        op.create_index(
            f"ix_dingtalk_org_sync_batch_{column}",
            "dingtalk_org_sync_batch",
            [column],
        )
    op.create_index(
        "ix_dingtalk_org_sync_batch_status_applied_at_id",
        "dingtalk_org_sync_batch",
        ["status", "applied_at", "id"],
    )
    op.create_index(
        "ix_dingtalk_org_sync_batch_scheduled_reuse",
        "dingtalk_org_sync_batch",
        [
            "trigger",
            "status",
            "root_config_hash",
            "snapshot_hash",
            "local_baseline_hash",
            "expires_at",
            "id",
        ],
    )

    op.create_table(
        "dingtalk_org_sync_item",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "batch_id",
            sa.BigInteger(),
            sa.ForeignKey("dingtalk_org_sync_batch.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_key", sa.String(length=160), nullable=False),
        sa.Column(
            "kind",
            dingtalk_org_sync_item_kind_enum,
            nullable=False,
        ),
        sa.Column(
            "status",
            dingtalk_org_sync_item_status_enum,
            server_default="READY",
            nullable=False,
        ),
        sa.Column("action", dingtalk_org_sync_action_enum, nullable=False),
        sa.Column("remote_department_id", sa.BigInteger(), nullable=True),
        sa.Column("remote_department_name", sa.String(length=128), nullable=False),
        sa.Column("remote_department_path", sa.String(length=1024), nullable=False),
        sa.Column("remote_user_id_hash", sa.String(length=64), nullable=True),
        sa.Column("applied_identity_proof", sa.String(length=64), nullable=True),
        sa.Column(
            "proposed_org_unit_id",
            sa.BigInteger(),
            sa.ForeignKey("org_unit.id"),
            nullable=True,
        ),
        sa.Column(
            "proposed_parent_org_unit_id",
            sa.BigInteger(),
            sa.ForeignKey("org_unit.id"),
            nullable=True,
        ),
        sa.Column(
            "proposed_employee_id",
            sa.BigInteger(),
            sa.ForeignKey("employee.id"),
            nullable=True,
        ),
        sa.Column("proposed_org_type", org_type_enum, nullable=True),
        sa.Column("department", department_enum, nullable=True),
        sa.Column("match_method", sa.String(length=64), nullable=False),
        sa.Column("conflict_code", sa.String(length=64), nullable=True),
        sa.Column(
            "change_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("baseline_fingerprint", sa.String(length=64), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "remote_department_id IS NULL OR remote_department_id > 0",
            name="ck_dingtalk_org_sync_item_remote_department_positive",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "row_key",
            name="uq_dingtalk_org_sync_item_batch_row_key",
        ),
    )
    for column in (
        "batch_id",
        "kind",
        "status",
        "proposed_org_unit_id",
        "proposed_parent_org_unit_id",
        "proposed_employee_id",
    ):
        op.create_index(
            f"ix_dingtalk_org_sync_item_{column}",
            "dingtalk_org_sync_item",
            [column],
        )
    op.create_index(
        "ix_dingtalk_org_sync_item_batch_org_unit",
        "dingtalk_org_sync_item",
        ["batch_id", "proposed_org_unit_id"],
    )

    op.create_table(
        "dingtalk_org_sync_notification",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "batch_id",
            sa.BigInteger(),
            sa.ForeignKey("dingtalk_org_sync_batch.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            dingtalk_delivery_status_enum,
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_task_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_dingtalk_org_sync_notification_key",
        ),
    )
    for column in ("batch_id", "recipient_user_id", "status"):
        op.create_index(
            f"ix_dingtalk_org_sync_notification_{column}",
            "dingtalk_org_sync_notification",
            [column],
        )

    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES ('dingtalk_org:sync', '同步钉钉门店与负责人')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM "role" AS role
            CROSS JOIN permission
            WHERE role.code IN ('SUPER_ADMIN', 'GROUP_HR')
              AND permission.code = 'dingtalk_org:sync'
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    for column in ("status", "recipient_user_id", "batch_id"):
        op.drop_index(
            f"ix_dingtalk_org_sync_notification_{column}",
            table_name="dingtalk_org_sync_notification",
        )
    op.drop_table("dingtalk_org_sync_notification")

    op.drop_index(
        "ix_dingtalk_org_sync_item_batch_org_unit",
        table_name="dingtalk_org_sync_item",
    )
    for column in (
        "proposed_employee_id",
        "proposed_parent_org_unit_id",
        "proposed_org_unit_id",
        "status",
        "kind",
        "batch_id",
    ):
        op.drop_index(
            f"ix_dingtalk_org_sync_item_{column}",
            table_name="dingtalk_org_sync_item",
        )
    op.drop_table("dingtalk_org_sync_item")

    op.drop_index(
        "ix_dingtalk_org_sync_batch_scheduled_reuse",
        table_name="dingtalk_org_sync_batch",
    )
    op.drop_index(
        "ix_dingtalk_org_sync_batch_status_applied_at_id",
        table_name="dingtalk_org_sync_batch",
    )
    for column in ("applied_by_user_id", "requested_by_user_id", "expires_at", "status"):
        op.drop_index(
            f"ix_dingtalk_org_sync_batch_{column}",
            table_name="dingtalk_org_sync_batch",
        )
    op.drop_index(
        "ix_dingtalk_org_sync_batch_public_id",
        table_name="dingtalk_org_sync_batch",
    )
    op.drop_table("dingtalk_org_sync_batch")

    op.drop_constraint(
        "uq_user_review_scope_org_department",
        "user_review_scope",
        type_="unique",
    )
    op.drop_constraint(
        "uq_app_user_dingtalk_user_id_hash",
        "app_user",
        type_="unique",
    )
    op.drop_column("app_user", "dingtalk_user_id_hash")
    op.drop_constraint(
        "uq_org_unit_dingtalk_dept_id",
        "org_unit",
        type_="unique",
    )
    op.drop_constraint(
        "ck_org_unit_dingtalk_dept_id_positive",
        "org_unit",
        type_="check",
    )
    op.drop_column("org_unit", "dingtalk_dept_id")

    bind = op.get_bind()
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    if not isinstance(schema, str) or not schema:
        raise RuntimeError("D20 cannot determine the current PostgreSQL schema")
    for enum_type in reversed(_sync_enums(schema)):
        enum_type.drop(bind, checkfirst=True)

    # The permission may have been seeded by newer application code before
    # this migration ran.  Keep the harmless catalog row and grants rather
    # than deleting RBAC data whose ownership cannot be proven.
