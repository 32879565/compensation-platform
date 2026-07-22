# DingTalk Organization Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete production-grade DingTalk-authoritative region, store, and payroll-reviewer synchronization with HR-confirmed atomic application and daily change discovery.

**Architecture:** Extend the existing two-phase preview/apply engine instead of replacing it. Add a pure hierarchy classifier, explicit persisted actions, region-aware planning, a separate organization-notification delivery model, and a one-shot scheduled job; keep the current advisory locks, snapshot proofs, encrypted identities, and live salary-access guard.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 16, Alembic, pytest, React 18, TypeScript, TanStack Query, Ant Design, Vitest, Playwright, Docker Compose.

**Design Spec:** `docs/superpowers/specs/2026-07-22-dingtalk-organization-sync-design.md`

## Global Constraints

- DingTalk is authoritative for region/store hierarchy and store reviewer routing; the application never writes organization changes back to DingTalk.
- Read only the configured DingTalk root subtrees. Production root configuration uses positive remote IDs mapped to immutable local non-store organization codes.
- A preview may persist staging rows but must not modify `OrgUnit`, employee identity bindings, user roles, review scopes, or sessions.
- Any structure or identity conflict blocks the whole batch. Apply is one PostgreSQL transaction and never partially succeeds.
- First organization binding uses exact normalized relative path and node type; subsequent binding uses only `dingtalk_dept_id`.
- First reviewer binding uses exact `Employee.emp_no`; subsequent binding uses encrypted DingTalk identity plus its domain-separated blind index. Never fall back to name or phone.
- Missing remote organizations are deactivated, never deleted; all payroll, identity, and audit history remains queryable under existing permissions.
- Scheduled execution creates or reuses a preview and notifies HR; it never applies formal changes.
- Preview confirmation expires after 15 minutes and re-reads DingTalk before applying.
- Remote user IDs remain encrypted or irreversibly hashed at rest and never appear in API responses, logs, audit detail, or notifications.
- Salary disclosure remains fail-closed behind the existing organization-freshness and live-manager checks.
- Preserve all pre-existing uncommitted work. Stage and commit only files named by the current task.
- Every task follows TDD: failing focused test, minimal implementation, focused pass, then a narrow commit.

---

## File Structure

### Backend files to create

- `backend/app/dingtalk/org_structure.py` — pure root validation, tree traversal, relative paths, and REGION/STORE/INTERNAL classification.
- `backend/app/dingtalk/org_notifications.py` — stage and dispatch non-payroll organization-sync notifications.
- `backend/app/dingtalk/org_sync_job.py` — one-shot scheduled preview CLI with a non-blocking advisory lock.
- `backend/tests/test_dingtalk_org_structure.py` — pure classifier tests.
- `backend/tests/test_dingtalk_org_notifications.py` — recipient selection, idempotency, payload privacy, and dispatch tests.
- `backend/tests/test_dingtalk_org_sync_job.py` — scheduler lock, idempotency, provider failure, and exit-code tests.

### Backend files to modify

- `backend/app/core/config.py` — parse and fail-close root mappings; expose organization-sync timing metadata.
- `backend/app/dingtalk/client.py` — accept a purpose-specific action-card button label while preserving payroll defaults.
- `backend/app/models/dingtalk.py` — explicit sync action/trigger enums, region counts, change fields, and organization-notification delivery.
- `backend/app/models/__init__.py` — export the new enums and delivery model.
- `backend/alembic/versions/i4r7l0n2q568_d20_dingtalk_org_sync.py` — update the unpublished D20 schema and downgrade order.
- `backend/app/dingtalk/org_sync.py` — stage and atomically apply region/store/reviewer actions using the classifier.
- `backend/app/dingtalk/org_freshness.py` — accept applied region-aware store items without weakening reviewer proof checks.
- `backend/app/routers/dingtalk_sync.py` — root-ID configuration, latest-preview endpoint, region response items, and stable errors.
- `backend/app/core/metrics.py` and `backend/app/main.py` — append low-cardinality database-backed organization-sync metrics.
- `backend/tests/test_config_failclosed.py` — mapping validation and production fail-closed tests.
- `backend/tests/test_dingtalk_client.py` — action-card label compatibility.
- `backend/tests/test_dingtalk_org_sync_models.py` — persistence contracts.
- `backend/tests/test_dingtalk_org_sync_migration.py` and `backend/tests/test_dingtalk_org_sync_alembic.py` — D20 upgrade/downgrade and real PostgreSQL checks.
- `backend/tests/test_dingtalk_org_sync_api.py` — hierarchy preview/apply, exact reviewer matching, conflicts, and latest-preview API.
- `backend/tests/test_dingtalk_org_freshness.py` — authorization proofs after region-aware application.
- `backend/tests/test_metrics.py` — operational metric rendering.

### Frontend and operations files to modify

- `frontend/src/api/dingtalk.ts` and `frontend/src/api/dingtalk.test.ts` — region-aware types, latest-preview GET, and strict response projection.
- `frontend/src/pages/OrgTreePage.tsx` and `frontend/src/pages/OrgTreePage.test.tsx` — latest status, region/store/reviewer sections, conflict/expiry blocking, and applied-result counts.
- `frontend/e2e/compensation-workflows.spec.ts` — sandbox organization preview and conflict-blocked confirmation journey.
- `deploy/.env.example` and `deploy/docker-compose.yml` — root mappings, timezone, and a profile-gated one-shot job service.
- `docs/operations.md` — root mapping, daily scheduling, first-run UAT, metrics, recovery, and troubleshooting.

---

### Task 1: Lock the Configuration Contract and Restore Static Quality

**Files:**
- Modify: `backend/app/core/config.py:60-275`
- Modify: `backend/tests/test_config_failclosed.py`
- Modify: `deploy/.env.example:30-48`
- Modify: `deploy/docker-compose.yml:35-72`
- Format only: the six files currently reported by Ruff

**Interfaces:**
- Produces: `Settings.dingtalk_org_root_mapping_pairs -> tuple[tuple[int, str], ...]`
- Produces: `Settings.dingtalk_org_sync_timezone: str`
- Preserves: `Settings.dingtalk_store_root_name_set` for sandbox compatibility only

- [ ] **Step 1: Add failing root-mapping configuration tests**

```python
def _set_dingtalk_read_env(monkeypatch) -> None:
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_ID", "ding-client")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_SECRET", "c" * 48)
    monkeypatch.setenv("COMP_DINGTALK_AGENT_ID", "123")
    monkeypatch.setenv("COMP_DINGTALK_CORP_ID", "corp-1")
    monkeypatch.setenv("COMP_DINGTALK_READ_SYNC_ENABLED", "true")


def test_live_read_sync_requires_stable_root_mappings(monkeypatch):
    _set_dingtalk_read_env(monkeypatch)
    monkeypatch.setenv("COMP_DINGTALK_MODE", "live")
    monkeypatch.setenv("COMP_DINGTALK_PUBLIC_BASE_URL", "https://pay.example.test")
    monkeypatch.setenv("COMP_DINGTALK_ORG_ROOT_MAPPINGS", "")

    with pytest.raises(ValidationError, match="root mappings"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "value",
    ["abc:GROUP", "0:GROUP", "1:", "1:GROUP,1:OTHER"],
)
def test_dingtalk_root_mappings_reject_invalid_or_duplicate_values(monkeypatch, value):
    _set_dingtalk_read_env(monkeypatch)
    monkeypatch.setenv("COMP_DINGTALK_ORG_ROOT_MAPPINGS", value)

    with pytest.raises(ValidationError, match="root mappings"):
        Settings(_env_file=None)


def test_dingtalk_root_mappings_are_canonical(monkeypatch):
    _set_dingtalk_read_env(monkeypatch)
    monkeypatch.setenv("COMP_DINGTALK_ORG_ROOT_MAPPINGS", " 100 : GROUP-GZ , 200:GROUP-SZ ")

    settings = Settings(_env_file=None)

    assert settings.dingtalk_org_root_mappings == "100:GROUP-GZ,200:GROUP-SZ"
    assert settings.dingtalk_org_root_mapping_pairs == ((100, "GROUP-GZ"), (200, "GROUP-SZ"))
```

- [ ] **Step 2: Run the configuration tests and verify they fail**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_config_failclosed.py -q --no-cov`

Expected: FAIL because `dingtalk_org_root_mappings` and `dingtalk_org_root_mapping_pairs` do not exist.

- [ ] **Step 3: Implement canonical root mapping and timezone validation**

Add these fields and methods to `Settings`:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

dingtalk_org_root_mappings: str = ""
dingtalk_org_sync_timezone: str = "Asia/Shanghai"

@field_validator("dingtalk_org_root_mappings")
@classmethod
def validate_dingtalk_org_root_mappings(cls, value: str) -> str:
    pairs: list[tuple[int, str]] = []
    for raw_pair in value.split(","):
        if not raw_pair.strip():
            continue
        raw_id, separator, raw_code = raw_pair.partition(":")
        code = raw_code.strip()
        if separator != ":" or not raw_id.strip().isdigit() or not code:
            raise ValueError("root mappings must use <positive-id>:<local-code>")
        remote_id = int(raw_id.strip())
        if remote_id <= 0 or len(code) > 64 or ":" in code or "," in code:
            raise ValueError("root mappings contain an invalid id or local code")
        pairs.append((remote_id, code))
    if len(pairs) > 20:
        raise ValueError("root mappings must contain at most 20 roots")
    if len({remote_id for remote_id, _ in pairs}) != len(pairs):
        raise ValueError("root mappings contain a duplicate remote root")
    return ",".join(f"{remote_id}:{code}" for remote_id, code in pairs)

@field_validator("dingtalk_org_sync_timezone")
@classmethod
def validate_dingtalk_org_sync_timezone(cls, value: str) -> str:
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("must be a valid IANA timezone") from exc
    return normalized

@property
def dingtalk_org_root_mapping_pairs(self) -> tuple[tuple[int, str], ...]:
    if not self.dingtalk_org_root_mappings:
        return ()
    return tuple(
        (int(raw_id), code)
        for pair in self.dingtalk_org_root_mappings.split(",")
        for raw_id, code in [pair.split(":", 1)]
    )
```

In `validate_dingtalk_configuration`, add:

```python
if (
    self.dingtalk_mode is DingTalkMode.LIVE
    and self.dingtalk_read_sync_enabled
    and not self.dingtalk_org_root_mapping_pairs
):
    raise ValueError("DingTalk live read sync requires root mappings")
```

- [ ] **Step 4: Expose the settings in deployment samples**

Add to both backend environment locations:

```yaml
COMP_DINGTALK_ORG_ROOT_MAPPINGS: ${COMP_DINGTALK_ORG_ROOT_MAPPINGS:-}
COMP_DINGTALK_ORG_SYNC_TIMEZONE: ${COMP_DINGTALK_ORG_SYNC_TIMEZONE:-Asia/Shanghai}
```

Add to `.env.example`:

```dotenv
# 正式环境使用钉钉根部门ID映射到现有本地非门店组织代码；多个映射用逗号分隔。
COMP_DINGTALK_ORG_ROOT_MAPPINGS=
COMP_DINGTALK_ORG_SYNC_TIMEZONE=Asia/Shanghai
```

- [ ] **Step 5: Apply only mechanical Ruff fixes to the current six violations**

Run: `cd backend; .\.venv\Scripts\ruff.exe check app/dingtalk/client.py app/dingtalk/org_sync.py app/dingtalk/service.py app/routers/users.py tests/test_dingtalk_manager_review.py tests/test_users_api.py --fix`

Inspect: `git diff -- backend/app/dingtalk/client.py backend/app/dingtalk/org_sync.py backend/app/dingtalk/service.py backend/app/routers/users.py backend/tests/test_dingtalk_manager_review.py backend/tests/test_users_api.py`

Expected: only import ordering and line wrapping; no behavior changes.

- [ ] **Step 6: Run focused and static checks**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_config_failclosed.py -q --no-cov; .\.venv\Scripts\ruff.exe check app tests; .\.venv\Scripts\python.exe -m mypy app`

Expected: all commands PASS.

- [ ] **Step 7: Commit configuration and baseline cleanup**

```powershell
git add backend/app/core/config.py backend/tests/test_config_failclosed.py deploy/.env.example deploy/docker-compose.yml backend/app/dingtalk/client.py backend/app/dingtalk/org_sync.py backend/app/dingtalk/service.py backend/app/routers/users.py backend/tests/test_dingtalk_manager_review.py backend/tests/test_users_api.py
git commit -m "chore: validate DingTalk organization roots"
```

### Task 2: Extend the D20 Persistence Contract

**Files:**
- Modify: `backend/app/models/dingtalk.py:33-350`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/alembic/versions/i4r7l0n2q568_d20_dingtalk_org_sync.py`
- Modify: `backend/tests/test_dingtalk_org_sync_models.py`
- Modify: `backend/tests/test_dingtalk_org_sync_migration.py`
- Modify: `backend/tests/test_dingtalk_org_sync_alembic.py`

**Interfaces:**
- Produces: `DingTalkOrgSyncAction`, `DingTalkOrgSyncTrigger`, `DingTalkOrgSyncNotification`
- Extends: `DingTalkOrgSyncItemKind.REGION`
- Adds: `DingTalkOrgSyncItem.action`, `change_fields`, `proposed_org_type`
- Adds batch counts: remote/local/ready/conflict regions and `warning_count`

- [ ] **Step 1: Write failing model-contract tests**

```python
def test_org_sync_models_cover_regions_actions_and_scheduled_notifications() -> None:
    assert {kind.value for kind in DingTalkOrgSyncItemKind} == {
        "REGION", "STORE", "REVIEWER"
    }
    assert {action.value for action in DingTalkOrgSyncAction} == {
        "LINK", "CREATE", "UPDATE", "ACTIVATE", "DEACTIVATE",
        "ASSIGN_SCOPE", "REMOVE_SCOPE", "NO_CHANGE",
    }
    assert {trigger.value for trigger in DingTalkOrgSyncTrigger} == {"MANUAL", "SCHEDULED"}
    assert DingTalkOrgSyncBatch.__table__.c.requested_by_user_id.nullable is True
    assert DingTalkOrgSyncItem.__table__.c.action.nullable is False
    assert DingTalkOrgSyncItem.__table__.c.change_fields.nullable is False
    assert ("idempotency_key",) in _unique_column_sets(DingTalkOrgSyncNotification.__table__)
```

- [ ] **Step 2: Run the model test and verify it fails**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_models.py -q --no-cov`

Expected: import/attribute failures for the new enums and model.

- [ ] **Step 3: Add enums and mapped columns**

Use these exact enum values:

```python
class DingTalkOrgSyncTrigger(enum.StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


class DingTalkOrgSyncAction(enum.StrEnum):
    LINK = "LINK"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    ACTIVATE = "ACTIVATE"
    DEACTIVATE = "DEACTIVATE"
    ASSIGN_SCOPE = "ASSIGN_SCOPE"
    REMOVE_SCOPE = "REMOVE_SCOPE"
    NO_CHANGE = "NO_CHANGE"


class DingTalkOrgSyncItemKind(enum.StrEnum):
    REGION = "REGION"
    STORE = "STORE"
    REVIEWER = "REVIEWER"
```

Add `trigger`, `root_config_hash`, `last_checked_at`, four region counts, and `warning_count` to `DingTalkOrgSyncBatch`. Make `requested_by_user_id` nullable for scheduled system previews. Add this notification model without changing payroll delivery nullability:

```python
class DingTalkOrgSyncNotification(Base, TimestampMixin):
    __tablename__ = "dingtalk_org_sync_notification"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_dingtalk_org_sync_notification_key"),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("dingtalk_org_sync_batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True
    )
    status: Mapped[DingTalkDeliveryStatus] = mapped_column(
        Enum(DingTalkDeliveryStatus, name="dingtalk_delivery_status"), nullable=False,
        default=DingTalkDeliveryStatus.PENDING, index=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
```

On `DingTalkOrgSyncItem`, add `action` as the new enum, `change_fields` as non-null JSON with default `list`, and nullable `proposed_org_type` using the existing `org_type` enum.

- [ ] **Step 4: Update D20 upgrade and downgrade in dependency order**

The migration must create enums before tables, add `REGION` to `dingtalk_org_sync_item_kind`, create the action/trigger enums, add the new batch/item columns and checks, then create `dingtalk_org_sync_notification`. Downgrade must drop the notification table before batches and drop the new enums last.

Use server defaults only for upgrade-safe non-null creation:

```python
sa.Column("trigger", sa.Enum("MANUAL", "SCHEDULED", name="dingtalk_org_sync_trigger"),
          nullable=False, server_default="MANUAL")
sa.Column("action", sa.Enum(
    "LINK", "CREATE", "UPDATE", "ACTIVATE", "DEACTIVATE",
    "ASSIGN_SCOPE", "REMOVE_SCOPE", "NO_CHANGE",
    name="dingtalk_org_sync_action"), nullable=False)
sa.Column("change_fields", postgresql.JSONB(astext_type=sa.Text()),
          nullable=False, server_default=sa.text("'[]'::jsonb"))
```

- [ ] **Step 5: Run fake-op and real PostgreSQL migration tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_models.py tests\test_dingtalk_org_sync_migration.py tests\test_dingtalk_org_sync_alembic.py -q --no-cov`

Expected: all PASS, including fresh upgrade and rollback-on-backfill-conflict.

- [ ] **Step 6: Commit the persistence contract**

```powershell
git add backend/app/models/dingtalk.py backend/app/models/__init__.py backend/alembic/versions/i4r7l0n2q568_d20_dingtalk_org_sync.py backend/tests/test_dingtalk_org_sync_models.py backend/tests/test_dingtalk_org_sync_migration.py backend/tests/test_dingtalk_org_sync_alembic.py
git commit -m "feat: persist region-aware DingTalk sync"
```

### Task 3: Build the Pure Hierarchy Classifier

**Files:**
- Create: `backend/app/dingtalk/org_structure.py`
- Create: `backend/tests/test_dingtalk_org_structure.py`
- Modify: `backend/app/dingtalk/client.py:412-612`
- Modify: `backend/tests/test_dingtalk_client.py`

**Interfaces:**
- Consumes: `DingTalkOrganizationSnapshot`, configured `(remote_root_id, local_anchor_code)` pairs, existing remote bindings, exact existing store paths
- Produces: `classify_organization(...) -> ClassifiedOrganization`
- Produces immutable `ClassifiedNode` records with `kind`, `relative_path`, `root_id`, `depth`, and original department

- [ ] **Step 1: Write classifier tests for the approved rules**

```python
def test_classifies_region_store_and_internal_departments() -> None:
    snapshot = DingTalkOrganizationSnapshot(
        departments=(
            DingTalkDepartment(100, 1, "运营中心"),
            DingTalkDepartment(110, 100, "广州一区"),
            DingTalkDepartment(120, 110, "天河店"),
            DingTalkDepartment(121, 120, "厅面"),
        ),
        users=(),
    )

    result = classify_organization(
        snapshot,
        root_ids=frozenset({100}),
        bound_types={},
        exact_store_paths=frozenset(),
    )

    assert [(node.department.department_id, node.kind) for node in result.regions] == [
        (110, OrgType.REGION)
    ]
    assert [node.department.department_id for node in result.stores] == [120]
    assert result.internal_department_ids == frozenset({121})


def test_nested_stores_fail_closed() -> None:
    snapshot = DingTalkOrganizationSnapshot(
        departments=(
            DingTalkDepartment(120, 100, "天河店"),
            DingTalkDepartment(121, 120, "二楼店"),
        ),
        users=(),
    )
    with pytest.raises(OrganizationStructureError):
        classify_organization(
            snapshot,
            root_ids=frozenset({100}),
            bound_types={},
            exact_store_paths=frozenset(),
        )


def test_client_reads_only_configured_root_subtrees(fake_dingtalk_transport) -> None:
    client = fake_dingtalk_transport.client
    snapshot = client.list_organization_snapshot(root_department_ids=(100, 200))
    assert {department.parent_id for department in snapshot.departments} >= {100, 200}
    assert 1 not in fake_dingtalk_transport.requested_department_ids
```

- [ ] **Step 2: Run the classifier test and verify it fails**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_structure.py -q --no-cov`

Expected: FAIL because `org_structure` does not exist.

- [ ] **Step 3: Implement focused immutable types and normalization**

```python
@dataclass(frozen=True)
class ClassifiedNode:
    department: DingTalkDepartment
    kind: OrgType
    root_id: int
    relative_path: tuple[str, ...]
    depth: int


@dataclass(frozen=True)
class ClassifiedOrganization:
    regions: tuple[ClassifiedNode, ...]
    stores: tuple[ClassifiedNode, ...]
    internal_department_ids: frozenset[int]
    warning_department_ids: frozenset[int]


class OrganizationStructureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_org_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
```

- [ ] **Step 4: Implement deterministic traversal and classification**

`classify_organization` must first build `by_id` and `children_by_parent`, reject duplicate IDs, missing parents inside a selected subtree, cycles, depth over 32, and overlapping configured roots. It then marks a node as STORE in this order: existing remote STORE binding, exact normalized store path, or normalized name ending in `店`. Store ancestors below the configured root become REGION; descendants of a STORE become internal IDs; a STORE below another STORE raises `ORG_NODE_CLASSIFICATION_CONFLICT`.

Use this public signature:

```python
def classify_organization(
    snapshot: DingTalkOrganizationSnapshot,
    *,
    root_ids: frozenset[int],
    bound_types: dict[int, OrgType],
    exact_store_paths: frozenset[tuple[int, tuple[str, ...]]],
) -> ClassifiedOrganization:
    by_id: dict[int, DingTalkDepartment] = {}
    for department in snapshot.departments:
        if department.department_id in by_id:
            raise OrganizationStructureError(
                "ORG_SNAPSHOT_INVALID", "duplicate DingTalk department"
            )
        by_id[department.department_id] = department

    def relative_path(department: DingTalkDepartment) -> tuple[int, tuple[str, ...]] | None:
        names = [normalize_org_name(department.name)]
        seen = {department.department_id}
        current = department
        for _depth in range(32):
            parent_id = current.parent_id
            if parent_id in root_ids:
                return parent_id, tuple(reversed(names))
            parent = by_id.get(parent_id) if parent_id is not None else None
            if parent is None:
                return None
            if parent.department_id in seen:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "DingTalk department cycle"
                )
            if parent.department_id in root_ids:
                return parent.department_id, tuple(reversed(names))
            seen.add(parent.department_id)
            names.append(normalize_org_name(parent.name))
            current = parent
        raise OrganizationStructureError(
            "ORG_SNAPSHOT_INVALID", "DingTalk department path exceeds 32 levels"
        )

    paths = {
        department.department_id: path
        for department in snapshot.departments
        if (path := relative_path(department)) is not None
    }
    store_ids: set[int] = set()
    for department_id, (root_id, path) in paths.items():
        bound_type = bound_types.get(department_id)
        if bound_type == OrgType.STORE:
            store_ids.add(department_id)
        elif bound_type == OrgType.REGION:
            continue
        elif (root_id, path) in exact_store_paths or path[-1].endswith("店"):
            store_ids.add(department_id)

    region_ids: set[int] = set()
    internal_ids: set[int] = set()
    for department_id, (root_id, path) in paths.items():
        ancestors: list[int] = []
        current = by_id[department_id]
        while current.parent_id not in {None, root_id}:
            parent = by_id.get(current.parent_id)
            if parent is None:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "orphan DingTalk department"
                )
            ancestors.append(parent.department_id)
            current = parent
        store_ancestors = store_ids.intersection(ancestors)
        if department_id in store_ids and store_ancestors:
            raise OrganizationStructureError(
                "ORG_NODE_CLASSIFICATION_CONFLICT", "a store contains another store"
            )
        if store_ancestors:
            internal_ids.add(department_id)
        if department_id in store_ids:
            region_ids.update(ancestor for ancestor in ancestors if ancestor not in store_ids)

    def node(department_id: int, kind: OrgType) -> ClassifiedNode:
        root_id, path = paths[department_id]
        return ClassifiedNode(
            department=by_id[department_id], kind=kind, root_id=root_id,
            relative_path=path, depth=len(path),
        )

    selected_ids = region_ids | store_ids | internal_ids
    warnings = frozenset(set(paths) - selected_ids)
    return ClassifiedOrganization(
        regions=tuple(
            sorted((node(value, OrgType.REGION) for value in region_ids),
                   key=lambda value: (value.depth, value.department.department_id))
        ),
        stores=tuple(node(value, OrgType.STORE) for value in sorted(store_ids)),
        internal_department_ids=frozenset(internal_ids),
        warning_department_ids=warnings,
    )
```

Return regions sorted by `(depth, department_id)`, stores by `department_id`, and every ID set as `frozenset` so snapshot hashing is deterministic.

- [ ] **Step 5: Scope provider traversal to the configured roots**

Change the client signature to:

```python
def list_organization_snapshot(
    self,
    *,
    root_department_ids: tuple[int, ...] | None = None,
) -> DingTalkOrganizationSnapshot:
```

Validate unique positive roots. Initialize both `department_ids` and `frontier` from those roots; use `(1,)` only when the argument is `None`. Root nodes are synchronization boundaries and do not need synthetic `DingTalkDepartment` rows. Continue reading direct members of each root and every discovered descendant, preserving all existing page, department, user, and worker limits.

Change safe read retry to three total attempts with jitter:

```python
_READ_RETRY_DELAYS_SECONDS = (0.25, 0.5)

# inside _post_legacy_json after a retryable failure
time.sleep(_READ_RETRY_DELAYS_SECONDS[attempt] * random.uniform(0.8, 1.2))
```

Extend `test_dingtalk_client.py` to patch `random.uniform` to `1.0` and assert exactly three transport calls for repeated temporary failures. Invalid credentials, provider business errors, and invalid response structures remain non-retryable.

- [ ] **Step 6: Run classifier and scoped-client tests and static checks**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_structure.py tests\test_dingtalk_client.py -q --no-cov; .\.venv\Scripts\ruff.exe check app/dingtalk/org_structure.py app/dingtalk/client.py tests/test_dingtalk_org_structure.py tests/test_dingtalk_client.py; .\.venv\Scripts\python.exe -m mypy app/dingtalk/org_structure.py app/dingtalk/client.py`

Expected: all PASS.

- [ ] **Step 7: Commit the classifier and scoped provider read**

```powershell
git add backend/app/dingtalk/org_structure.py backend/tests/test_dingtalk_org_structure.py backend/app/dingtalk/client.py backend/tests/test_dingtalk_client.py
git commit -m "feat: classify DingTalk organization hierarchy"
```

### Task 4: Stage Region and Store Changes With Explicit Actions

**Files:**
- Modify: `backend/app/dingtalk/org_sync.py:72-1145`
- Modify: `backend/tests/test_dingtalk_org_sync_api.py`

**Interfaces:**
- Consumes: `classify_organization`, `Settings.dingtalk_org_root_mapping_pairs`
- Changes signature: `preview_organization_sync(..., root_mappings, trigger, actor)`
- Produces: `OrganizationPreview.region_items`, region counts, warning count, root config hash

- [ ] **Step 1: Add failing preview tests for full path matching and authority actions**

Add test cases that use existing database/API helpers to assert:

```python
assert response.json()["region_items"][0] == {
    "id": response.json()["region_items"][0]["id"],
    "kind": "REGION",
    "action": "CREATE",
    "change_fields": [],
    "remote_department_id": 110,
    "remote_department_name": "广州一区",
    "remote_department_path": "广州一区",
    "match_method": "NO_LOCAL_PATH_MATCH",
    "proposed_org_unit_id": None,
    "proposed_org_unit_name": "广州一区",
    "proposed_parent_org_unit_id": anchor.id,
    "proposed_parent_org_unit_name": anchor.name,
    "status": "READY",
    "conflict_code": None,
}
assert response.json()["store_items"][0]["action"] == "DEACTIVATE"
assert response.json()["store_conflicts"] == 0
```

Also prove two same-name stores under different exact region paths do not match each other, and two candidates at the same path produce `ORG_PATH_AMBIGUOUS` and a nonzero conflict count.

- [ ] **Step 2: Run the new preview cases and verify they fail**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py -q --no-cov -k "region or path or deactivate"`

Expected: FAIL because the response has no region items and missing stores are conflicts.

- [ ] **Step 3: Replace action encoding with typed staged fields**

Remove `_encode_method` and `_decode_method`. Construct every item with:

```python
DingTalkOrgSyncItem(
    row_key=row_key,
    kind=kind,
    action=action,
    change_fields=list(change_fields),
    status=status,
    remote_department_id=remote_id,
    remote_department_name=remote_name,
    remote_department_path=" / ".join(relative_path),
    proposed_org_unit_id=local_id,
    proposed_parent_org_unit_id=parent_id,
    proposed_org_type=proposed_type,
    match_method=match_method,
    conflict_code=conflict_code,
    baseline_fingerprint=baseline,
)
```

Use `DingTalkOrgSyncAction.UPDATE` with `change_fields=["name"]`, `["parent_id"]`, or both. Convert a local-only active node in the configured authority scope to a READY `DEACTIVATE`, not a conflict.

- [ ] **Step 4: Resolve configured anchors and exact relative paths**

At preview start, resolve every local anchor code uniquely and validate it is active, not deleted, and not STORE. Hash the sorted `(root_id, anchor.code)` pairs into `root_config_hash`. Build local relative paths only while walking from an organization to its configured anchor; leaving the anchor tree is `ORG_PATH_AMBIGUOUS`.

The new signature is:

```python
def preview_organization_sync(
    session: Session,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    encryption_key: str,
    actor: tuple[int, str] | None,
    root_mappings: tuple[tuple[int, str], ...],
    trigger: DingTalkOrgSyncTrigger = DingTalkOrgSyncTrigger.MANUAL,
    now: datetime | None = None,
    dining_manager_titles: frozenset[str] = frozenset({"店长"}),
    kitchen_manager_titles: frozenset[str] = frozenset({"厨房经理"}),
) -> OrganizationPreview:
```

- [ ] **Step 5: Extend preview DTOs and aggregate counts internally**

Rename `StorePreviewItem` to `OrganizationNodePreviewItem` with `kind: OrgType`, `action: DingTalkOrgSyncAction`, and `change_fields: tuple[str, ...]`. Extend `OrganizationPreview` with `trigger`, `created_at`, `last_checked_at`, `remote_regions`, `local_regions`, `ready_regions`, `region_conflicts`, `warnings`, and `region_items`.

After computing the root hash, remote snapshot hash, and complete local baseline hash, scheduled previews must query for an unexpired PREVIEWED batch with all three hashes. If one exists, update only `last_checked_at` and return its projected preview; do not insert new items. Manual previews always create a new batch and mark older PREVIEWED batches for the same root hash STALE.

- [ ] **Step 6: Run all organization preview/API tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_structure.py tests\test_dingtalk_org_sync_api.py -q --no-cov`

Expected: all preview tests PASS; apply-focused tests may still fail until Task 6 and should be selected out only if their old action encoding is the failure.

- [ ] **Step 7: Commit hierarchy preview support**

```powershell
git add backend/app/dingtalk/org_sync.py backend/tests/test_dingtalk_org_sync_api.py
git commit -m "feat: preview DingTalk region and store changes"
```

### Task 5: Enforce Exact Reviewer Identity Matching

**Files:**
- Modify: `backend/app/dingtalk/org_sync.py:775-1050`
- Modify: `backend/tests/test_dingtalk_org_sync_api.py`

**Interfaces:**
- Produces: `_match_reviewer_identity(...) -> tuple[Employee | None, str, str | None]`
- Preserves: stable blind-index match for already-bound employees
- Removes: `UNIQUE_NAME` reviewer acceptance and all name fallback

- [ ] **Step 1: Add failing exact-identity tests**

```python
def test_reviewer_sync_never_uses_unique_name(client, db_session):
    # Local and DingTalk names match, but DingTalk has no job number and no stable binding.
    response = client.post("/api/dingtalk/sync/organization/preview")
    item = next(row for row in response.json()["reviewer_items"] if row["department"] == "DINING")
    assert item["status"] == "CONFLICT"
    assert item["conflict_code"] == "ORG_EMPLOYEE_MATCH_FAILED"


def test_reviewer_sync_uses_unique_exact_job_number(client, db_session):
    response = client.post("/api/dingtalk/sync/organization/preview")
    item = next(row for row in response.json()["reviewer_items"] if row["department"] == "DINING")
    assert item["status"] == "READY"
    assert item["action"] == "ASSIGN_SCOPE"
    assert item["match_method"] == "JOB_NUMBER"
```

- [ ] **Step 2: Run the new reviewer tests and verify the name case fails**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py -q --no-cov -k "reviewer_sync_never or reviewer_sync_uses"`

Expected: FAIL because the existing directory matcher still produces a unique-name candidate.

- [ ] **Step 3: Implement the exact matcher**

```python
def _match_reviewer_identity(
    remote_user: DingTalkOrganizationUser,
    *,
    employees: tuple[Employee, ...],
    encryption_key: str,
) -> tuple[Employee | None, str, str | None]:
    provider_hash = blind_index_dingtalk_user_id(remote_user.user_id, key=encryption_key)
    stable = [employee for employee in employees if employee.dingtalk_user_id_hash == provider_hash]
    if len(stable) == 1:
        return stable[0], "STABLE_ID", None
    if len(stable) > 1:
        return None, "STABLE_ID", "ORG_IDENTITY_CONFLICT"
    job_number = (remote_user.job_number or "").strip()
    if not job_number:
        return None, "JOB_NUMBER", "ORG_EMPLOYEE_MATCH_FAILED"
    matches = [employee for employee in employees if employee.emp_no.strip() == job_number]
    if len(matches) == 1:
        return matches[0], "JOB_NUMBER", None
    return (
        None,
        "JOB_NUMBER",
        "ORG_EMPLOYEE_MATCH_FAILED" if not matches else "ORG_IDENTITY_CONFLICT",
    )
```

Use only active, non-deleted employees for new job-number matches. Retain the existing ownership checks that reject a provider identity already bound to another user or employee.

- [ ] **Step 4: Make missing managers a safe scope-removal warning**

When zero active title candidates exist for a store/department, stage READY `REMOVE_SCOPE`, set `warning_count += 1`, and clear every existing scope during apply. When more than one candidate exists, stage conflict `ORG_MANAGER_AMBIGUOUS` and block the whole batch.

- [ ] **Step 5: Run reviewer and privacy tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py tests\test_dingtalk_read_sync.py tests\test_dingtalk_read_sync_api.py -q --no-cov`

Expected: all PASS and no response contains remote user IDs.

- [ ] **Step 6: Commit strict reviewer matching**

```powershell
git add backend/app/dingtalk/org_sync.py backend/tests/test_dingtalk_org_sync_api.py
git commit -m "feat: require exact DingTalk reviewer identities"
```

### Task 6: Apply Every Organization Change Atomically

**Files:**
- Modify: `backend/app/dingtalk/org_sync.py:1148-1625`
- Modify: `backend/app/dingtalk/org_freshness.py`
- Modify: `backend/tests/test_dingtalk_org_sync_api.py`
- Modify: `backend/tests/test_dingtalk_org_freshness.py`

**Interfaces:**
- Consumes: explicit staged action, `change_fields`, region/store dependency graph
- Produces: `OrganizationApplyResult(applied_regions, applied_stores, applied_reviewers, already_applied)`
- Guarantees: all conflicts block before writes; parents create first; deactivation is child-first

- [ ] **Step 1: Add failing atomic-apply tests**

Test all of the following with the existing PostgreSQL fixture:

```python
assert apply_response.status_code == 409  # any REGION, STORE, or REVIEWER conflict
assert db_session.get(OrgUnit, original.id).name == original_name
assert db_session.query(UserReviewScope).count() == original_scope_count
```

Add a successful case containing CREATE REGION → CREATE STORE → ASSIGN_SCOPE, and a failure injected after region creation that proves the region, store, identity binding, role, scope, session revocation, and audit summary all roll back.

- [ ] **Step 2: Run atomic tests and verify they fail**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py -q --no-cov -k "atomic or region_apply or any_conflict"`

Expected: FAIL because current apply blocks only reviewer conflicts and has no region dependency ordering.

- [ ] **Step 3: Block any conflict before formal writes**

Replace reviewer-only conflict detection with:

```python
conflicts = [item for item in items if item.status == DingTalkOrgSyncItemStatus.CONFLICT]
if conflicts:
    session.rollback()
    raise DingTalkOrganizationSyncError(
        "ORG_PREVIEW_HAS_CONFLICTS",
        "Organization conflicts must be resolved before confirmation",
    )
```

Also compare `root_config_hash`, remote snapshot hash, and every local baseline after acquiring table locks. Mark the batch STALE with stable codes on mismatch.

- [ ] **Step 4: Apply parent-first creates and updates**

Build a staged remote-ID-to-local-ID map. Process READY REGION items by remote-path depth, then STORE items, then REVIEWER items. For a CREATE item, resolve a parent created earlier in the same transaction through its parent remote ID; generate codes as `DINGTALK-R-{id}` and `DINGTALK-S-{id}` and fail on any collision rather than adding suffixes.

For UPDATE, modify only fields listed in `change_fields`. For ACTIVATE, set status ACTIVE and update listed fields. For DEACTIVATE, set status HISTORICAL and never clear `dingtalk_dept_id`.

- [ ] **Step 5: Revoke scopes, proofs, and sessions before deactivation**

For `REMOVE_SCOPE`, deleted/deactivated stores, and reassigned reviewers, delete the affected `UserReviewScope`, call `invalidate_applied_reviewer_proofs`, and call `revoke_all_for_user` for every displaced account. Deactivate nodes in descending path depth after scopes are removed.

- [ ] **Step 6: Update freshness proof selection**

Keep `require_recent_organization_scopes` strict: it must still find exactly one APPLIED STORE item with the current `dingtalk_dept_id` and exactly one reviewer proof. REGION items may coexist in the batch but never satisfy store coverage.

- [ ] **Step 7: Run all organization, freshness, and concurrency tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py tests\test_dingtalk_org_freshness.py tests\test_dingtalk_manager_review.py tests\test_users_api.py -q --no-cov`

Expected: all PASS.

- [ ] **Step 8: Commit atomic hierarchy application**

```powershell
git add backend/app/dingtalk/org_sync.py backend/app/dingtalk/org_freshness.py backend/tests/test_dingtalk_org_sync_api.py backend/tests/test_dingtalk_org_freshness.py
git commit -m "feat: apply DingTalk organization atomically"
```

### Task 7: Expose Region-Aware Preview and Latest Status APIs

**Files:**
- Modify: `backend/app/routers/dingtalk_sync.py:167-215,602-693`
- Modify: `backend/tests/test_dingtalk_org_sync_api.py`

**Interfaces:**
- Produces: `GET /api/dingtalk/sync/organization/latest`
- Extends: POST preview and apply output with region counts and items
- Preserves: `POST /api/dingtalk/sync/organization/{batch_id}/apply`

- [ ] **Step 1: Add failing API contract tests**

```python
latest = client.get("/api/dingtalk/sync/organization/latest")
assert latest.status_code == 200
assert latest.headers["cache-control"] == "no-store"
assert latest.json()["trigger"] in {"MANUAL", "SCHEDULED"}
assert latest.json()["region_items"][0]["kind"] == "REGION"
assert "remote_user_id_hash" not in latest.text

denied = scoped_hr_client.get("/api/dingtalk/sync/organization/latest")
assert denied.status_code == 403
```

- [ ] **Step 2: Run the latest-endpoint tests and verify 404/failure**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py -q --no-cov -k latest`

Expected: FAIL because the route does not exist.

- [ ] **Step 3: Define strict Pydantic output models**

Create `OrganizationNodeItemOut` with `kind: Literal["REGION", "STORE"]`, typed action, `change_fields: list[Literal["name", "parent_id"]]`, and the existing safe display fields. Extend `OrganizationPreviewOut` and `OrganizationApplyOut` with region counts. Do not expose hashes, encrypted IDs, baseline fingerprints, or provider IDs.

- [ ] **Step 4: Implement latest-preview reading without provider access**

Add `get_latest_organization_preview(session) -> OrganizationPreview | None` to `org_sync.py`, selecting the newest PREVIEWED/APPLIED/STALE batch plus items and projecting through the same safe DTO builder. The GET route requires `_require_organization_sync_manager`, sets `Cache-Control: no-store`, and returns 404 with `ORG_PREVIEW_NOT_FOUND` when no batch exists.

- [ ] **Step 5: Map stable errors consistently**

Use one mapping:

```python
_ORG_SYNC_HTTP_STATUS = {
    "BATCH_NOT_FOUND": 404,
    "ORG_PREVIEW_NOT_FOUND": 404,
    "ORG_PROVIDER_UNAVAILABLE": 502,
    "ORG_ROOT_CONFIG_INVALID": 409,
    "ORG_ROOT_NOT_FOUND": 409,
}
```

All other sync-domain errors return 409. User detail is Chinese and stable; logs contain code and exception class, never provider response bodies.

- [ ] **Step 6: Run API and router checks**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_api.py -q --no-cov; .\.venv\Scripts\ruff.exe check app/routers/dingtalk_sync.py app/dingtalk/org_sync.py`

Expected: all PASS.

- [ ] **Step 7: Commit the API contract**

```powershell
git add backend/app/routers/dingtalk_sync.py backend/app/dingtalk/org_sync.py backend/tests/test_dingtalk_org_sync_api.py
git commit -m "feat: expose DingTalk organization sync status"
```

### Task 8: Add Idempotent HR Notifications

**Files:**
- Create: `backend/app/dingtalk/org_notifications.py`
- Create: `backend/tests/test_dingtalk_org_notifications.py`
- Modify: `backend/app/dingtalk/client.py:743-816`
- Modify: `backend/tests/test_dingtalk_client.py`

**Interfaces:**
- Produces: `stage_org_sync_notifications(session, batch, settings) -> tuple[int, ...]`
- Produces: `dispatch_org_sync_notification(session, notification_id, settings, client) -> DingTalkOrgSyncNotification`
- Extends: `DingTalkClient.send_action_card(..., action_title="查看并申诉")`

- [ ] **Step 1: Add failing client and notification tests**

```python
def test_org_sync_notification_selects_only_global_hr_and_hides_pii(db_session, live_settings):
    ids = stage_org_sync_notifications(db_session, batch=batch, settings=live_settings)
    rows = db_session.scalars(
        select(DingTalkOrgSyncNotification).where(DingTalkOrgSyncNotification.id.in_(ids))
    ).all()
    assert {row.recipient_user_id for row in rows} == {global_hr.id}
    assert all(row.idempotency_key == f"org-sync:{batch.public_id}:user:{row.recipient_user_id}" for row in rows)


def test_action_card_accepts_purpose_specific_button(fake_transport):
    client.send_action_card(
        recipient_user_id="ding-1", title="组织同步待确认",
        markdown="发现 3 项变更、1 项冲突。", action_url="https://pay.example.test/org",
        action_title="查看组织同步",
    )
    assert fake_transport.message["action_card"]["single_title"] == "查看组织同步"
```

- [ ] **Step 2: Run the notification tests and verify they fail**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_client.py tests\test_dingtalk_org_notifications.py -q --no-cov`

Expected: FAIL because the module and action-title argument do not exist.

- [ ] **Step 3: Extend the client without changing payroll behavior**

```python
def send_action_card(
    self,
    *,
    recipient_user_id: str,
    title: str,
    markdown: str,
    action_url: str,
    action_title: str = "查看并申诉",
) -> DingTalkSendResult:
    if not action_title.strip() or len(action_title) > 32:
        raise DingTalkClientError("The DingTalk notification action title is invalid")
    message = {
        "msgtype": "action_card",
        "action_card": {
            "title": title.strip(),
            "markdown": markdown.strip(),
            "single_title": action_title.strip(),
            "single_url": action_url,
        },
    }
```

Retain the existing recipient, title, markdown, URL, serialized-size, access-token, response, and provider-task validation around this message construction. Existing payroll tests must still observe `查看并申诉`.

- [ ] **Step 4: Stage recipients using effective global permission**

Select active, non-deleted, login-enabled users with encrypted DingTalk IDs. For each candidate, call `load_global_permissions(session, user.id)` and include only users holding both `Perm.DINGTALK_ORG_SYNC` and `Perm.NOTIFICATION_MANAGE`. Insert with the deterministic idempotency key and return existing IDs on repeat.

- [ ] **Step 5: Dispatch privacy-safe summaries**

Decrypt the recipient only at send time. Build exactly this content from counts, never item names:

```python
title = "钉钉组织同步待确认"
markdown = (
    f"发现 {batch.ready_region_count + batch.ready_store_count + batch.ready_reviewer_count} "
    f"项待应用变更，{batch.region_conflict_count + batch.store_conflict_count + batch.reviewer_conflict_count} "
    "项冲突。请由集团 HR 进入薪酬平台核对。"
)
action_url = f"{str(settings.dingtalk_public_base_url).rstrip('/')}/org"
```

Use PENDING/SANDBOXED/SENT/FAILED, increment attempts once per confirmed attempt, save only stable errors, and treat `DingTalkSendOutcomeUnknown` as FAILED without blind automatic resend.

In sandbox mode, stage the row directly as SANDBOXED with no client call. In live mode, require `dingtalk_public_base_url`; a missing URL marks the row FAILED with `PUBLIC_BASE_URL_MISSING` and does not expose configuration values.

- [ ] **Step 6: Run notification and existing delivery tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_client.py tests\test_dingtalk_org_notifications.py tests\test_dingtalk_api.py -q --no-cov`

Expected: all PASS.

- [ ] **Step 7: Commit notification delivery**

```powershell
git add backend/app/dingtalk/org_notifications.py backend/tests/test_dingtalk_org_notifications.py backend/app/dingtalk/client.py backend/tests/test_dingtalk_client.py
git commit -m "feat: notify HR of DingTalk organization changes"
```

### Task 9: Add the One-Shot Scheduled Preview and Metrics

**Files:**
- Create: `backend/app/dingtalk/org_sync_job.py`
- Create: `backend/tests/test_dingtalk_org_sync_job.py`
- Modify: `backend/app/core/metrics.py`
- Modify: `backend/app/main.py:53-63`
- Modify: `backend/tests/test_metrics.py`

**Interfaces:**
- Produces CLI: `python -m app.dingtalk.org_sync_job`
- Produces: `run_scheduled_org_sync(session, settings, client, now=None) -> int`
- Produces Prometheus gauges for last success, last failure, ready changes, and conflicts

- [ ] **Step 1: Add failing scheduler tests**

```python
def test_scheduled_job_creates_one_preview_and_is_idempotent(db_session, settings, fake_client):
    first = run_scheduled_org_sync(db_session, settings=settings, client=fake_client)
    second = run_scheduled_org_sync(db_session, settings=settings, client=fake_client)
    assert first == 0
    assert second == 0
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 1


def test_scheduled_job_returns_failure_without_formal_writes(db_session, settings, failing_client):
    assert run_scheduled_org_sync(db_session, settings=settings, client=failing_client) == 1
    assert db_session.scalar(select(func.count()).select_from(OrgUnit)) == original_org_count
```

- [ ] **Step 2: Run scheduler tests and verify the module is missing**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_job.py -q --no-cov`

Expected: FAIL on import.

- [ ] **Step 3: Implement a non-blocking advisory lock and one-shot main**

```python
_SCHEDULE_LOCK = "compensation-platform:dingtalk-org-sync-schedule:v1"

def run_scheduled_org_sync(
    session: Session,
    *,
    settings: Settings,
    client: DingTalkClient,
    now: datetime | None = None,
) -> int:
    try:
        acquired = session.scalar(
            select(func.pg_try_advisory_xact_lock(func.hashtext(_SCHEDULE_LOCK)))
        )
        if session.get_bind().dialect.name == "postgresql" and not acquired:
            session.rollback()
            return 0
        snapshot = client.list_organization_snapshot(
            root_department_ids=tuple(
                remote_id for remote_id, _ in settings.dingtalk_org_root_mapping_pairs
            )
        )
        preview = preview_organization_sync(
            session, snapshot, encryption_key=settings.encryption_key, actor=None,
            root_mappings=settings.dingtalk_org_root_mapping_pairs,
            trigger=DingTalkOrgSyncTrigger.SCHEDULED, now=now,
            dining_manager_titles=settings.dingtalk_dining_manager_title_set,
            kitchen_manager_titles=settings.dingtalk_kitchen_manager_title_set,
        )
        batch = session.scalars(
            select(DingTalkOrgSyncBatch).where(
                DingTalkOrgSyncBatch.public_id == preview.batch_id
            )
        ).one()
        change_count = preview.ready_regions + preview.ready_stores + preview.ready_reviewers
        conflict_count = (
            preview.region_conflicts + preview.store_conflicts + preview.reviewer_conflicts
        )
        if change_count or conflict_count:
            notification_ids = stage_org_sync_notifications(
                session, batch=batch, settings=settings
            )
            session.commit()
            for notification_id in notification_ids:
                dispatch_org_sync_notification(
                    session, notification_id=notification_id, settings=settings, client=client
                )
        audit.record(
            session, action="dingtalk.organization.schedule.succeeded", actor=None,
            target_type="dingtalk_org_sync_batch", target_id=batch.id,
            detail={"changes": change_count, "conflicts": conflict_count},
        )
        session.commit()
        return 0
    except (DingTalkClientError, DingTalkOrganizationSyncError) as exc:
        session.rollback()
        audit.record(
            session, action="dingtalk.organization.schedule.failed", result="FAILURE",
            actor=None, detail={"error_code": getattr(exc, "code", "ORG_PROVIDER_UNAVAILABLE"),
                                "error_type": type(exc).__name__},
        )
        session.commit()
        return 1


def main() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        return run_scheduled_org_sync(
            session, settings=settings, client=get_dingtalk_client()
        )


if __name__ == "__main__":
    raise SystemExit(main())
```

Before the provider call, release any authentication/read transaction. Reuse an identical still-pending scheduled preview by root hash, remote snapshot hash, and local baseline hash; never create duplicate notification idempotency keys. Record `dingtalk.organization.schedule.succeeded` or `dingtalk.organization.schedule.failed` through the append-only audit service; failures store only the stable error code and exception class.

- [ ] **Step 4: Add database-backed low-cardinality metrics**

Add `render_org_sync_metrics(session, now)` that derives success/failure timestamps from the two scheduler audit actions and change/conflict gauges from the latest batch. Emit only numeric gauges with no user, root, department, or error labels:

```text
compensation_dingtalk_org_sync_last_success_timestamp_seconds
compensation_dingtalk_org_sync_last_failure_timestamp_seconds
compensation_dingtalk_org_sync_ready_changes
compensation_dingtalk_org_sync_conflicts
compensation_dingtalk_org_sync_stale_seconds
```

In `/metrics`, open `SessionLocal`, append the organization metrics to `request_metrics.render_prometheus()`, and on database error emit no organization series while preserving request metrics.

- [ ] **Step 5: Run scheduler and metric tests**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests\test_dingtalk_org_sync_job.py tests\test_metrics.py -q --no-cov`

Expected: all PASS; metric output contains no organization names, user IDs, or provider IDs.

- [ ] **Step 6: Commit scheduling and metrics**

```powershell
git add backend/app/dingtalk/org_sync_job.py backend/tests/test_dingtalk_org_sync_job.py backend/app/core/metrics.py backend/app/main.py backend/tests/test_metrics.py
git commit -m "feat: schedule DingTalk organization previews"
```

### Task 10: Upgrade the Organization Sync UI

**Files:**
- Modify: `frontend/src/api/dingtalk.ts:79-140,231-268,300-326`
- Modify: `frontend/src/api/dingtalk.test.ts`
- Modify: `frontend/src/pages/OrgTreePage.tsx`
- Modify: `frontend/src/pages/OrgTreePage.test.tsx`

**Interfaces:**
- Consumes safe backend `OrganizationPreviewOut`
- Produces: `fetchLatestDingTalkOrganization()`
- Displays region/store/reviewer sections and blocks apply on any conflict or expiry

- [ ] **Step 1: Add failing API projection tests**

```typescript
client.get.mockResolvedValueOnce({ data: preview })
const latest = await fetchLatestDingTalkOrganization()
expect(client.get).toHaveBeenCalledWith('/api/dingtalk/sync/organization/latest')
expect(latest.region_items[0]?.kind).toBe('REGION')
expect(latest.region_items[0]).not.toHaveProperty('baseline_fingerprint')
```

Update the apply expectation to preserve `/api/dingtalk/sync/organization/${batchId}/apply`.

- [ ] **Step 2: Run API tests and verify the latest function is missing**

Run: `cd frontend; npm test -- --run src/api/dingtalk.test.ts`

Expected: FAIL on missing export/types.

- [ ] **Step 3: Define strict frontend types and projections**

```typescript
export type DingTalkOrganizationAction =
  | 'LINK' | 'CREATE' | 'UPDATE' | 'ACTIVATE' | 'DEACTIVATE'
  | 'ASSIGN_SCOPE' | 'REMOVE_SCOPE' | 'NO_CHANGE'
export type DingTalkOrganizationNodeKind = 'REGION' | 'STORE'
export type DingTalkOrganizationTrigger = 'MANUAL' | 'SCHEDULED'

export interface DingTalkOrganizationNodeItem {
  id: number
  kind: DingTalkOrganizationNodeKind
  action: DingTalkOrganizationAction
  change_fields: Array<'name' | 'parent_id'>
  remote_department_id: number | null
  remote_department_name: string
  remote_department_path: string
  match_method: string
  proposed_org_unit_id: number | null
  proposed_org_unit_name: string | null
  proposed_parent_org_unit_id: number | null
  proposed_parent_org_unit_name: string | null
  status: DingTalkOrganizationSyncItemStatus
  conflict_code: string | null
}
```

Project every response field explicitly; never return the raw Axios object or spread provider payloads.

- [ ] **Step 4: Load the latest batch without opening the modal**

Add a permission-gated TanStack query with key `['dingtalkOrganizationLatest', queryScope]`. Show last check, trigger, expiry, change count, conflict count, and a “查看/刷新预览” button. Treat 404 as no previous preview, not a page error.

- [ ] **Step 5: Render three sections and strict apply state**

Create a reusable `OrganizationChangesSection` for REGION and STORE. Show tags from action plus `change_fields`, group reviewers into ASSIGN_SCOPE/REMOVE_SCOPE/conflict, and compute:

```typescript
const totalConflicts =
  preview.region_conflicts + preview.store_conflicts + preview.reviewer_conflicts
const totalReady = preview.ready_regions + preview.ready_stores + preview.ready_reviewers
const expired = Date.parse(preview.expires_at) <= Date.now()
const canApply = totalReady > 0 && totalConflicts === 0 && !expired
```

After apply, refresh both the organization tree and latest-preview query. If tree refresh fails, retain the “应用已成功” warning and never auto-resubmit.

- [ ] **Step 6: Add page tests for every blocking state**

Cover scheduled latest status, region creation/move, store deactivation, reviewer removal, any-category conflict, expiry, successful apply, and post-apply tree-refresh failure. Use fake timers for expiry rather than depending on wall-clock time.

- [ ] **Step 7: Run frontend unit/static/build checks**

Run: `cd frontend; npm test -- --run src/api/dingtalk.test.ts src/pages/OrgTreePage.test.tsx; npm run typecheck; npm run lint -- --quiet; npm run build`

Expected: all PASS.

- [ ] **Step 8: Commit the UI**

```powershell
git add frontend/src/api/dingtalk.ts frontend/src/api/dingtalk.test.ts frontend/src/pages/OrgTreePage.tsx frontend/src/pages/OrgTreePage.test.tsx
git commit -m "feat: review full DingTalk organization changes"
```

### Task 11: Deployment, Runbook, E2E, and Final Verification

**Files:**
- Modify: `deploy/docker-compose.yml`
- Modify: `deploy/.env.example`
- Modify: `docs/operations.md`
- Modify: `frontend/e2e/compensation-workflows.spec.ts`

**Interfaces:**
- Produces profile-gated one-shot service `dingtalk-org-sync-job`
- Documents an external daily scheduler at 09:00 `Asia/Shanghai`
- Produces one browser-level organization-sync acceptance journey

- [ ] **Step 1: Add a failing Compose/config validation assertion**

Extend the existing deployment tests or add a focused test that parses `docker compose config` and asserts the job uses the backend image, has no published ports, depends on healthy Postgres, and runs:

```yaml
command: ["python", "-m", "app.dingtalk.org_sync_job"]
restart: "no"
profiles: ["org-sync-job"]
```

- [ ] **Step 2: Add the one-shot Compose service**

Reuse the backend build and exact environment keys. Do not embed a loop or cron daemon. Document invoking it from the host scheduler:

```powershell
docker compose -f deploy/docker-compose.yml --profile org-sync-job run --rm dingtalk-org-sync-job
```

The production scheduler owns 09:00 `Asia/Shanghai`; overlapping invocations remain safe because the job uses the advisory lock.

- [ ] **Step 3: Add the browser acceptance journey**

In the isolated E2E stack, route/mock the sandbox preview endpoints and verify: latest scheduled summary appears, region/store/reviewer tables render, any conflict disables confirmation, a refreshed conflict-free batch applies once, and the organization tree refreshes. The E2E fixture must contain no real DingTalk credentials or identifiers.

- [ ] **Step 4: Update the operations runbook**

Document exact root mapping syntax, anchor prerequisites, manager-title configuration, first manual UAT, database backup, conflict codes, job command, 26-hour stale alert, notification failures, provider outages, compensating sync, and disaster recovery. State explicitly that D20 downgrade is forbidden after production data exists unless restoring the pre-D20 database backup.

- [ ] **Step 5: Run the complete backend gate**

Run:

```powershell
cd backend
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\black.exe --check app tests
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe -m pytest
```

Expected: all PASS; total coverage remains at least 80%; payroll calculation coverage remains at least 95%.

- [ ] **Step 6: Run the complete frontend gate**

Run:

```powershell
cd frontend
npm run lint
npm run typecheck
npm test -- --run
npm run build
npm run test:e2e -- compensation-workflows.spec.ts
```

Expected: all PASS and the production bundle builds.

- [ ] **Step 7: Validate migrations, Compose, and repository hygiene**

Run:

```powershell
cd backend
.\.venv\Scripts\alembic.exe heads
cd ..
docker compose -f deploy/docker-compose.yml config --quiet
git diff --check
git status --short
```

Expected: exactly one Alembic head (`i4r7l0n2q568`), valid Compose, no whitespace errors, and only intentional files modified.

- [ ] **Step 8: Perform a final security/quality review**

Review the complete diff for raw DingTalk IDs, credentials, PII in logs/API/notifications, weak name fallback, partial-apply paths, unlocked authorization changes, and unbounded provider traversal. Resolve every finding and rerun the affected focused and full gates.

- [ ] **Step 9: Commit operations and acceptance coverage**

```powershell
git add deploy/docker-compose.yml deploy/.env.example docs/operations.md frontend/e2e/compensation-workflows.spec.ts
git commit -m "docs: operate DingTalk organization synchronization"
```

---

## Completion Evidence

Before declaring the feature complete, capture in the final handoff:

- All commit hashes from Tasks 1-11.
- Backend test totals and coverage.
- Frontend unit-test and E2E totals.
- Ruff, Black, Mypy, ESLint, TypeScript, build, Alembic-head, and Compose results.
- The final list of supported actions and stable error codes.
- Confirmation that no real DingTalk credentials, provider IDs, or payroll data entered tests, logs, notifications, commits, or build artifacts.
- Any external UAT/production prerequisites that still require HR, DingTalk administrators, or infrastructure operators.
