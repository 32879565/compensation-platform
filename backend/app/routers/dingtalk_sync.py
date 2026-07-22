"""DingTalk contact, attendance, and confirmed organization synchronization."""

from __future__ import annotations

import calendar
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal, cast

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal, get_session
from app.dingtalk.client import (
    DingTalkClient,
    DingTalkClientError,
    DingTalkOrganizationSnapshot,
    get_dingtalk_client,
)
from app.dingtalk.org_sync import (
    DingTalkOrganizationSyncError,
    OrganizationPreview,
    apply_organization_sync,
    get_applied_organization_sync_result,
    get_latest_organization_preview,
    preview_organization_sync,
    take_organization_sync_lock,
)
from app.dingtalk.read_sync import (
    AttendancePreviewRow,
    DirectoryMatchResult,
    LocalEmployeeIdentity,
    aggregate_attendance_results,
    match_directory_users,
)
from app.models.dingtalk import (
    DingTalkAttendanceSnapshot,
    DingTalkAttendanceSync,
    DingTalkAttendanceSyncStatus,
)
from app.models.employee import Employee, EmployeeStatus

router = APIRouter(prefix="/api/dingtalk/sync", tags=["dingtalk"])
_PREVIEW_ROW_LIMIT = 200
_logger = get_logger("app.dingtalk_sync")
AttendanceRefreshRunner = Callable[[str, tuple[int, str]], None]
_ORGANIZATION_READ_LOCK = threading.Lock()
_ORG_SYNC_HTTP_STATUS = {
    "BATCH_NOT_FOUND": 404,
    "ORG_PREVIEW_NOT_FOUND": 404,
    "ORG_PROVIDER_UNAVAILABLE": 502,
    "ORG_ROOT_CONFIG_INVALID": 409,
    "ORG_ROOT_NOT_FOUND": 409,
}
_ORG_SYNC_HTTP_DETAIL = {
    "BATCH_NOT_FOUND": "未找到组织同步预览",
    "ORG_PREVIEW_NOT_FOUND": "未找到组织同步预览",
    "ORG_PROVIDER_UNAVAILABLE": "钉钉组织服务暂不可用",
    "ORG_ROOT_CONFIG_INVALID": "钉钉组织同步根配置无效",
    "ORG_ROOT_NOT_FOUND": "钉钉组织同步根节点不存在",
}


def _read_organization_snapshot(
    client: DingTalkClient,
    root_department_ids: tuple[int, ...],
) -> DingTalkOrganizationSnapshot:
    if not _ORGANIZATION_READ_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="A DingTalk organization read is already in progress",
        )
    try:
        return client.list_organization_snapshot(root_department_ids=root_department_ids)
    finally:
        _ORGANIZATION_READ_LOCK.release()


def _require_global_sync_manager(
    principal: Principal = Depends(require_permission(Perm.NOTIFICATION_MANAGE)),
    session: Session = Depends(get_session),
) -> Principal:
    if resolve_permission_org_scope(session, principal, Perm.NOTIFICATION_MANAGE) is not None:
        raise HTTPException(status_code=403, detail="DingTalk sync requires group scope")
    return principal


def _require_global_permission(session: Session, principal: Principal, permission: str) -> None:
    if not principal.has_permission(permission):
        raise HTTPException(status_code=403, detail="Additional DingTalk sync access is required")
    if resolve_permission_org_scope(session, principal, permission) is not None:
        raise HTTPException(status_code=403, detail="DingTalk sync requires group scope")


def _require_directory_reader(
    principal: Principal = Depends(_require_global_sync_manager),
    session: Session = Depends(get_session),
) -> Principal:
    _require_global_permission(session, principal, Perm.EMPLOYEE_READ)
    return principal


def _require_directory_writer(
    principal: Principal = Depends(_require_global_sync_manager),
    session: Session = Depends(get_session),
) -> Principal:
    _require_global_permission(session, principal, Perm.EMPLOYEE_WRITE)
    return principal


def _require_attendance_reader(
    principal: Principal = Depends(_require_global_sync_manager),
    session: Session = Depends(get_session),
) -> Principal:
    _require_global_permission(session, principal, Perm.EMPLOYEE_READ)
    _require_global_permission(session, principal, Perm.ATTENDANCE_READ)
    return principal


def _require_organization_sync_manager(
    principal: Principal = Depends(_require_global_sync_manager),
    session: Session = Depends(get_session),
) -> Principal:
    _require_global_permission(session, principal, Perm.DINGTALK_ORG_SYNC)
    return principal


def _require_read_sync_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.dingtalk_read_sync_enabled:
        raise HTTPException(status_code=409, detail="DingTalk read sync is not enabled")
    if not settings.dingtalk_credentials_configured:
        raise HTTPException(status_code=409, detail="DingTalk credentials are incomplete")
    return settings


class DirectoryMatchItemOut(BaseModel):
    employee_id: int
    emp_no: str
    local_name: str
    dingtalk_name: str
    dingtalk_job_number: str | None
    match_method: Literal["STABLE_ID", "JOB_NUMBER", "UNIQUE_NAME"]


class DirectoryPreviewOut(BaseModel):
    total_remote_users: int
    matched: int
    stable_id_matches: int
    job_number_matches: int
    unique_name_matches: int
    ambiguous: int
    unmatched: int
    truncated: bool
    items: list[DirectoryMatchItemOut]


class DirectoryApplyOut(BaseModel):
    matched: int
    linked: int
    unchanged: int
    ambiguous: int
    unmatched: int


class OrganizationNodeItemOut(BaseModel):
    id: int
    kind: Literal["REGION", "STORE"]
    remote_department_id: int | None
    remote_department_name: str
    remote_department_path: str
    action: Literal["LINK", "CREATE", "ACTIVATE", "UPDATE", "DEACTIVATE", "NO_CHANGE"]
    change_fields: list[Literal["name", "parent_id", "dingtalk_dept_id"]]
    match_method: str
    proposed_org_unit_id: int | None
    proposed_org_unit_name: str | None
    proposed_parent_org_unit_id: int | None
    proposed_parent_org_unit_name: str | None
    status: Literal["READY", "CONFLICT", "APPLIED", "IGNORED"]
    conflict_code: str | None


class OrganizationReviewerItemOut(BaseModel):
    id: int
    remote_department_id: int | None
    remote_department_name: str
    remote_department_path: str
    department: Literal["DINING", "KITCHEN"]
    action: Literal["ASSIGN", "REMOVE", "CONFLICT"]
    match_method: str
    dingtalk_name: str | None
    current_reviewer_name: str | None
    proposed_employee_id: int | None
    proposed_employee_name: str | None
    status: Literal["READY", "CONFLICT", "APPLIED", "IGNORED"]
    conflict_code: str | None


class OrganizationPreviewOut(BaseModel):
    batch_id: str
    trigger: Literal["MANUAL", "SCHEDULED"]
    created_at: datetime
    last_checked_at: datetime
    expires_at: datetime
    remote_regions: int
    local_regions: int
    ready_regions: int
    region_conflicts: int
    remote_stores: int
    local_stores: int
    ready_stores: int
    store_conflicts: int
    ready_reviewers: int
    reviewer_conflicts: int
    warnings: int
    region_items: list[OrganizationNodeItemOut]
    store_items: list[OrganizationNodeItemOut]
    reviewer_items: list[OrganizationReviewerItemOut]


class OrganizationApplyOut(BaseModel):
    applied_regions: int
    applied_stores: int
    applied_reviewers: int
    unresolved: int
    already_applied: bool


class AttendancePreviewRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class AttendancePreviewOut(BaseModel):
    period: str
    matched_employees: int
    employees_with_records: int
    total_records: int
    ambiguous_directory_users: int
    unmatched_directory_users: int
    items: list[AttendancePreviewRow]


class AttendanceSnapshotOut(BaseModel):
    period: str
    status: Literal["NOT_STARTED", "QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    matched_employees: int
    employees_with_records: int
    total_records: int
    ambiguous_directory_users: int
    unmatched_directory_users: int
    source_start: datetime | None
    source_end: datetime | None
    started_at: datetime | None
    refreshed_at: datetime | None
    error_code: str | None
    items: list[AttendancePreviewRow]


def _period_bounds(
    period: str,
    *,
    today: date | None = None,
) -> tuple[datetime, datetime]:
    year, month = (int(part) for part in period.split("-"))
    current_date = today or datetime.now(UTC).date()
    if (year, month) > (current_date.year, current_date.month):
        raise ValueError("attendance period cannot be in the future")
    last_day = calendar.monthrange(year, month)[1]
    end_day = (
        min(last_day, current_date.day)
        if (year, month) == (current_date.year, current_date.month)
        else last_day
    )
    return (
        datetime(year, month, 1, 0, 0, 0),
        datetime(year, month, end_day, 23, 59, 59),
    )


def _active_employees(session: Session, *, for_update: bool = False) -> list[Employee]:
    statement = (
        select(Employee)
        .where(
            Employee.is_deleted.is_(False),
            Employee.status == EmployeeStatus.ACTIVE,
        )
        .order_by(Employee.id)
    )
    if for_update:
        statement = statement.with_for_update()
    return list(session.scalars(statement).all())


def _match_directory(
    session: Session,
    client: DingTalkClient,
    settings: Settings,
) -> tuple[list[Employee], DirectoryMatchResult, int]:
    employees = _active_employees(session)
    try:
        remote_users = client.list_directory_users()
    except DingTalkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    local_identities = [
        LocalEmployeeIdentity(
            employee_id=employee.id,
            emp_no=employee.emp_no,
            name=employee.name,
            dingtalk_user_id_hash=employee.dingtalk_user_id_hash,
        )
        for employee in employees
    ]
    result = match_directory_users(
        local_identities,
        remote_users,
        encryption_key=settings.encryption_key,
    )
    return employees, result, len(remote_users)


def _directory_preview(
    result: DirectoryMatchResult, total_remote_users: int
) -> DirectoryPreviewOut:
    method_counts = Counter(match.method for match in result.matches)
    visible_matches = result.matches[:_PREVIEW_ROW_LIMIT]
    return DirectoryPreviewOut(
        total_remote_users=total_remote_users,
        matched=len(result.matches),
        stable_id_matches=method_counts["STABLE_ID"],
        job_number_matches=method_counts["JOB_NUMBER"],
        unique_name_matches=method_counts["UNIQUE_NAME"],
        ambiguous=result.ambiguous_remote_users,
        unmatched=result.unmatched_remote_users,
        truncated=len(result.matches) > len(visible_matches),
        items=[
            DirectoryMatchItemOut(
                employee_id=match.employee_id,
                emp_no=match.emp_no,
                local_name=match.local_name,
                dingtalk_name=match.remote_name,
                dingtalk_job_number=match.remote_job_number,
                match_method=match.method,
            )
            for match in visible_matches
        ],
    )


def _organization_preview_response(preview: OrganizationPreview) -> OrganizationPreviewOut:
    """Map only the explicitly approved preview fields across the HTTP boundary."""

    return OrganizationPreviewOut(
        batch_id=preview.batch_id,
        trigger=cast(Literal["MANUAL", "SCHEDULED"], preview.trigger.value),
        created_at=preview.created_at,
        last_checked_at=preview.last_checked_at,
        expires_at=preview.expires_at,
        remote_regions=preview.remote_regions,
        local_regions=preview.local_regions,
        ready_regions=preview.ready_regions,
        region_conflicts=preview.region_conflicts,
        remote_stores=preview.remote_stores,
        local_stores=preview.local_stores,
        ready_stores=preview.ready_stores,
        store_conflicts=preview.store_conflicts,
        ready_reviewers=preview.ready_reviewers,
        reviewer_conflicts=preview.reviewer_conflicts,
        warnings=preview.warnings,
        region_items=[
            OrganizationNodeItemOut(
                id=item.id,
                kind=cast(Literal["REGION", "STORE"], item.kind.value),
                remote_department_id=item.remote_department_id,
                remote_department_name=item.remote_department_name,
                remote_department_path=item.remote_department_path,
                action=cast(
                    Literal["LINK", "CREATE", "ACTIVATE", "UPDATE", "DEACTIVATE", "NO_CHANGE"],
                    item.action.value,
                ),
                change_fields=cast(
                    list[Literal["name", "parent_id", "dingtalk_dept_id"]],
                    list(item.change_fields),
                ),
                match_method=item.match_method,
                proposed_org_unit_id=item.proposed_org_unit_id,
                proposed_org_unit_name=item.proposed_org_unit_name,
                proposed_parent_org_unit_id=item.proposed_parent_org_unit_id,
                proposed_parent_org_unit_name=item.proposed_parent_org_unit_name,
                status=cast(Literal["READY", "CONFLICT", "APPLIED", "IGNORED"], item.status.value),
                conflict_code=item.conflict_code,
            )
            for item in preview.region_items
        ],
        store_items=[
            OrganizationNodeItemOut(
                id=item.id,
                kind=cast(Literal["REGION", "STORE"], item.kind.value),
                remote_department_id=item.remote_department_id,
                remote_department_name=item.remote_department_name,
                remote_department_path=item.remote_department_path,
                action=cast(
                    Literal["LINK", "CREATE", "ACTIVATE", "UPDATE", "DEACTIVATE", "NO_CHANGE"],
                    item.action.value,
                ),
                change_fields=cast(
                    list[Literal["name", "parent_id", "dingtalk_dept_id"]],
                    list(item.change_fields),
                ),
                match_method=item.match_method,
                proposed_org_unit_id=item.proposed_org_unit_id,
                proposed_org_unit_name=item.proposed_org_unit_name,
                proposed_parent_org_unit_id=item.proposed_parent_org_unit_id,
                proposed_parent_org_unit_name=item.proposed_parent_org_unit_name,
                status=cast(Literal["READY", "CONFLICT", "APPLIED", "IGNORED"], item.status.value),
                conflict_code=item.conflict_code,
            )
            for item in preview.store_items
        ],
        reviewer_items=[
            OrganizationReviewerItemOut(
                id=item.id,
                remote_department_id=item.remote_department_id,
                remote_department_name=item.remote_department_name,
                remote_department_path=item.remote_department_path,
                department=cast(Literal["DINING", "KITCHEN"], item.department.value),
                action=cast(Literal["ASSIGN", "REMOVE", "CONFLICT"], item.action),
                match_method=item.match_method,
                dingtalk_name=item.dingtalk_name,
                current_reviewer_name=item.current_reviewer_name,
                proposed_employee_id=item.proposed_employee_id,
                proposed_employee_name=item.proposed_employee_name,
                status=cast(Literal["READY", "CONFLICT", "APPLIED", "IGNORED"], item.status.value),
                conflict_code=item.conflict_code,
            )
            for item in preview.reviewer_items
        ],
    )


def _organization_sync_http_error(exc: DingTalkOrganizationSyncError) -> HTTPException:
    _logger.warning(
        "DingTalk organization sync failed",
        extra={"context": {"code": exc.code, "error_type": type(exc).__name__}},
    )
    return HTTPException(
        status_code=_ORG_SYNC_HTTP_STATUS.get(exc.code, 409),
        detail=_ORG_SYNC_HTTP_DETAIL.get(exc.code, "组织同步状态冲突，请重新预览"),
    )


def _organization_provider_http_error(exc: DingTalkClientError) -> HTTPException:
    code = "ORG_PROVIDER_UNAVAILABLE"
    _logger.warning(
        "DingTalk organization provider read failed",
        extra={"context": {"code": code, "error_type": type(exc).__name__}},
    )
    return HTTPException(
        status_code=_ORG_SYNC_HTTP_STATUS[code],
        detail=_ORG_SYNC_HTTP_DETAIL[code],
    )


def _attendance_snapshot_response(session: Session, period: str) -> AttendanceSnapshotOut:
    sync = session.scalars(
        select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == period)
    ).one_or_none()
    if sync is None:
        return AttendanceSnapshotOut(
            period=period,
            status="NOT_STARTED",
            matched_employees=0,
            employees_with_records=0,
            total_records=0,
            ambiguous_directory_users=0,
            unmatched_directory_users=0,
            source_start=None,
            source_end=None,
            started_at=None,
            refreshed_at=None,
            error_code=None,
            items=[],
        )

    rows = session.execute(
        select(DingTalkAttendanceSnapshot, Employee)
        .join(Employee, Employee.id == DingTalkAttendanceSnapshot.employee_id)
        .where(DingTalkAttendanceSnapshot.period == period)
        .order_by(Employee.emp_no, Employee.id)
    ).all()
    return AttendanceSnapshotOut(
        period=period,
        status=sync.status.value,
        matched_employees=sync.matched_employees,
        employees_with_records=sync.employees_with_records,
        total_records=sync.total_records,
        ambiguous_directory_users=sync.ambiguous_directory_users,
        unmatched_directory_users=sync.unmatched_directory_users,
        source_start=sync.source_start,
        source_end=sync.source_end,
        started_at=sync.started_at,
        refreshed_at=sync.refreshed_at,
        error_code=sync.error_code,
        items=[
            AttendancePreviewRow(
                employee_id=employee.id,
                emp_no=employee.emp_no,
                name=employee.name,
                record_count=snapshot.record_count,
                normal_count=snapshot.normal_count,
                late_count=snapshot.late_count,
                early_count=snapshot.early_count,
                absent_count=snapshot.absent_count,
                not_signed_count=snapshot.not_signed_count,
                other_count=snapshot.other_count,
            )
            for snapshot, employee in rows
        ],
    )


def _mark_attendance_refresh_failed(
    period: str,
    actor: tuple[int, str],
    error_code: str,
) -> None:
    with SessionLocal() as session:
        sync = session.scalars(
            select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == period)
        ).one_or_none()
        if sync is None:
            return
        sync.status = DingTalkAttendanceSyncStatus.FAILED
        sync.error_code = error_code
        audit.record(
            session,
            action="dingtalk.attendance.refresh.failed",
            result="FAILURE",
            actor=actor,
            target_type="dingtalk_attendance",
            target_id=sync.id,
            detail={"period": period, "error_code": error_code},
        )
        session.commit()


def _run_attendance_refresh(period: str, actor: tuple[int, str]) -> None:
    """Fetch provider data off-request and atomically replace the safe aggregate cache."""

    try:
        settings = get_settings()
        if not settings.dingtalk_read_sync_enabled or not settings.dingtalk_credentials_configured:
            raise DingTalkClientError("DingTalk read sync is not configured")

        with SessionLocal() as session:
            sync = session.scalars(
                select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == period)
            ).one_or_none()
            if sync is None:
                return
            sync.status = DingTalkAttendanceSyncStatus.RUNNING
            sync.started_at = datetime.now(UTC)
            sync.error_code = None
            session.commit()

            local_identities = [
                LocalEmployeeIdentity(
                    employee_id=employee.id,
                    emp_no=employee.emp_no,
                    name=employee.name,
                    dingtalk_user_id_hash=employee.dingtalk_user_id_hash,
                )
                for employee in _active_employees(session)
            ]
            # Do not keep a database transaction open during the long provider read.
            session.rollback()

        client = DingTalkClient.from_settings(settings)
        remote_users = client.list_directory_users()
        match_result = match_directory_users(
            local_identities,
            remote_users,
            encryption_key=settings.encryption_key,
        )
        employee_by_user_id = {
            match.user_id: (match.employee_id, match.emp_no, match.local_name)
            for match in match_result.matches
        }
        start, end = _period_bounds(period)
        records = client.list_attendance_results(
            user_ids=list(employee_by_user_id),
            start=start,
            end=end,
        )
        items = aggregate_attendance_results(
            records,
            employee_by_user_id=employee_by_user_id,
        )
        refreshed_at = datetime.now(UTC)

        with SessionLocal() as session:
            sync = session.scalars(
                select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == period)
            ).one_or_none()
            if sync is None:
                return
            session.execute(
                delete(DingTalkAttendanceSnapshot).where(
                    DingTalkAttendanceSnapshot.period == period
                )
            )
            if items:
                session.execute(
                    insert(DingTalkAttendanceSnapshot),
                    [
                        {
                            "sync_id": sync.id,
                            "employee_id": item.employee_id,
                            "period": period,
                            "record_count": item.record_count,
                            "normal_count": item.normal_count,
                            "late_count": item.late_count,
                            "early_count": item.early_count,
                            "absent_count": item.absent_count,
                            "not_signed_count": item.not_signed_count,
                            "other_count": item.other_count,
                            "refreshed_at": refreshed_at,
                            "created_by": actor[0],
                        }
                        for item in items
                    ],
                )
            sync.status = DingTalkAttendanceSyncStatus.COMPLETED
            sync.matched_employees = len(match_result.matches)
            sync.employees_with_records = len(items)
            sync.total_records = sum(item.record_count for item in items)
            sync.ambiguous_directory_users = match_result.ambiguous_remote_users
            sync.unmatched_directory_users = match_result.unmatched_remote_users
            sync.source_start = start.replace(tzinfo=UTC)
            sync.source_end = end.replace(tzinfo=UTC)
            sync.refreshed_at = refreshed_at
            sync.error_code = None
            audit.record(
                session,
                action="dingtalk.attendance.refresh.completed",
                actor=actor,
                target_type="dingtalk_attendance",
                target_id=sync.id,
                detail={
                    "period": period,
                    "matched_employees": sync.matched_employees,
                    "employees_with_records": sync.employees_with_records,
                    "records": sync.total_records,
                },
            )
            session.commit()
    except DingTalkClientError as exc:
        _logger.warning(
            "DingTalk rejected the attendance refresh",
            extra={"context": {"period": period, "provider_error": str(exc)}},
        )
        _mark_attendance_refresh_failed(period, actor, "PROVIDER_READ_FAILED")
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        _logger.error(
            "DingTalk attendance refresh failed",
            extra={"context": {"period": period, "error_type": type(exc).__name__}},
        )
        _mark_attendance_refresh_failed(period, actor, "INTERNAL_REFRESH_FAILED")


def get_attendance_refresh_runner() -> AttendanceRefreshRunner:
    return _run_attendance_refresh


@router.post("/organization/preview", response_model=OrganizationPreviewOut)
def preview_dingtalk_organization(
    response: Response,
    principal: Principal = Depends(_require_organization_sync_manager),
    settings: Settings = Depends(_require_read_sync_enabled),
    client: DingTalkClient = Depends(get_dingtalk_client),
    session: Session = Depends(get_session),
) -> OrganizationPreviewOut:
    response.headers["Cache-Control"] = "no-store"
    try:
        # Authentication has completed; release that read transaction before
        # the potentially long provider traversal, then open a short staging
        # transaction only after the snapshot is complete.
        session.rollback()
        snapshot = _read_organization_snapshot(
            client,
            tuple(
                remote_id
                for remote_id, _local_anchor_code in settings.dingtalk_org_root_mapping_pairs
            ),
        )
        preview = preview_organization_sync(
            session,
            snapshot,
            encryption_key=settings.encryption_key,
            actor=(principal.user_id, principal.username),
            root_mappings=settings.dingtalk_org_root_mapping_pairs,
            dining_manager_titles=settings.dingtalk_dining_manager_title_set,
            kitchen_manager_titles=settings.dingtalk_kitchen_manager_title_set,
        )
    except DingTalkClientError as exc:
        raise _organization_provider_http_error(exc) from None
    except DingTalkOrganizationSyncError as exc:
        raise _organization_sync_http_error(exc) from None
    return _organization_preview_response(preview)


@router.get("/organization/latest", response_model=OrganizationPreviewOut)
def get_latest_dingtalk_organization(
    response: Response,
    _principal: Principal = Depends(_require_organization_sync_manager),
    session: Session = Depends(get_session),
) -> OrganizationPreviewOut:
    response.headers["Cache-Control"] = "no-store"
    preview = get_latest_organization_preview(session)
    if preview is None:
        raise _organization_sync_http_error(
            DingTalkOrganizationSyncError("ORG_PREVIEW_NOT_FOUND", "No organization preview")
        )
    return _organization_preview_response(preview)


@router.post(
    "/organization/{batch_id}/apply",
    response_model=OrganizationApplyOut,
)
def apply_dingtalk_organization(
    response: Response,
    batch_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
    principal: Principal = Depends(_require_organization_sync_manager),
    settings: Settings = Depends(_require_read_sync_enabled),
    client: DingTalkClient = Depends(get_dingtalk_client),
    session: Session = Depends(get_session),
) -> OrganizationApplyOut:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = get_applied_organization_sync_result(session, batch_id)
        if result is not None:
            session.rollback()
            return OrganizationApplyOut(
                applied_regions=result.applied_regions,
                applied_stores=result.applied_stores,
                applied_reviewers=result.applied_reviewers,
                unresolved=result.unresolved,
                already_applied=result.already_applied,
            )
        # Re-read DingTalk at confirmation time so a manager transfer made
        # after preview cannot be applied from a stale staged identity.
        session.rollback()
        snapshot = _read_organization_snapshot(
            client,
            tuple(
                remote_id
                for remote_id, _local_anchor_code in settings.dingtalk_org_root_mapping_pairs
            ),
        )
        result = apply_organization_sync(
            session,
            batch_id,
            fresh_snapshot=snapshot,
            encryption_key=settings.encryption_key,
            tenant_id=settings.dingtalk_corp_id or "",
            actor=(principal.user_id, principal.username),
            root_mappings=settings.dingtalk_org_root_mapping_pairs,
        )
    except DingTalkClientError as exc:
        raise _organization_provider_http_error(exc) from None
    except DingTalkOrganizationSyncError as exc:
        raise _organization_sync_http_error(exc) from None
    return OrganizationApplyOut(
        applied_regions=result.applied_regions,
        applied_stores=result.applied_stores,
        applied_reviewers=result.applied_reviewers,
        unresolved=result.unresolved,
        already_applied=result.already_applied,
    )


@router.post("/employees/preview", response_model=DirectoryPreviewOut)
def preview_employee_directory(
    principal: Principal = Depends(_require_directory_reader),
    settings: Settings = Depends(_require_read_sync_enabled),
    client: DingTalkClient = Depends(get_dingtalk_client),
    session: Session = Depends(get_session),
) -> DirectoryPreviewOut:
    _employees, result, remote_count = _match_directory(session, client, settings)
    response = _directory_preview(result, remote_count)
    audit.record(
        session,
        action="dingtalk.directory.preview",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_directory",
        detail={
            "remote": response.total_remote_users,
            "matched": response.matched,
            "ambiguous": response.ambiguous,
            "unmatched": response.unmatched,
        },
    )
    session.commit()
    return response


@router.post("/employees/apply", response_model=DirectoryApplyOut)
def apply_employee_directory_matches(
    principal: Principal = Depends(_require_directory_writer),
    settings: Settings = Depends(_require_read_sync_enabled),
    client: DingTalkClient = Depends(get_dingtalk_client),
    session: Session = Depends(get_session),
) -> DirectoryApplyOut:
    # Never keep a stale employee snapshot across the provider call.  The
    # organization apply and manual identity endpoint use the same advisory
    # lock, so reload and match only after the remote read is complete.
    session.rollback()
    try:
        remote_users = client.list_directory_users()
    except DingTalkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    take_organization_sync_lock(session)
    employees = _active_employees(session, for_update=True)
    result = match_directory_users(
        [
            LocalEmployeeIdentity(
                employee_id=employee.id,
                emp_no=employee.emp_no,
                name=employee.name,
                dingtalk_user_id_hash=employee.dingtalk_user_id_hash,
            )
            for employee in employees
        ],
        remote_users,
        encryption_key=settings.encryption_key,
    )
    employee_by_id = {employee.id: employee for employee in employees}
    linked = 0
    unchanged = 0
    for match in result.matches:
        employee = employee_by_id[match.employee_id]
        if employee.dingtalk_user_id_hash == match.user_id_hash:
            unchanged += 1
            continue
        employee.dingtalk_user_id_hash = match.user_id_hash
        linked += 1
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="DingTalk identities changed during confirmation; preview again",
        ) from None
    audit.record(
        session,
        action="dingtalk.directory.apply",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_directory",
        detail={
            "matched": len(result.matches),
            "linked": linked,
            "unchanged": unchanged,
            "ambiguous": result.ambiguous_remote_users,
            "unmatched": result.unmatched_remote_users,
        },
    )
    session.commit()
    return DirectoryApplyOut(
        matched=len(result.matches),
        linked=linked,
        unchanged=unchanged,
        ambiguous=result.ambiguous_remote_users,
        unmatched=result.unmatched_remote_users,
    )


@router.get("/attendance/snapshot", response_model=AttendanceSnapshotOut)
def get_attendance_snapshot(
    period: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    _principal: Principal = Depends(_require_attendance_reader),
    session: Session = Depends(get_session),
) -> AttendanceSnapshotOut:
    return _attendance_snapshot_response(session, period)


@router.post(
    "/attendance/refresh",
    response_model=AttendanceSnapshotOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_attendance_refresh(
    body: AttendancePreviewRequest,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(_require_attendance_reader),
    _settings: Settings = Depends(_require_read_sync_enabled),
    runner: AttendanceRefreshRunner = Depends(get_attendance_refresh_runner),
    session: Session = Depends(get_session),
) -> AttendanceSnapshotOut:
    try:
        _period_bounds(body.period)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Attendance period cannot be in the future",
        ) from None
    sync = session.scalars(
        select(DingTalkAttendanceSync)
        .where(DingTalkAttendanceSync.period == body.period)
        .with_for_update()
    ).one_or_none()
    if sync is not None and sync.status in {
        DingTalkAttendanceSyncStatus.QUEUED,
        DingTalkAttendanceSyncStatus.RUNNING,
    }:
        return _attendance_snapshot_response(session, body.period)

    if sync is None:
        sync = DingTalkAttendanceSync(
            period=body.period,
            status=DingTalkAttendanceSyncStatus.QUEUED,
            requested_by_user_id=principal.user_id,
        )
        session.add(sync)
        session.flush()
    else:
        sync.status = DingTalkAttendanceSyncStatus.QUEUED
        sync.requested_by_user_id = principal.user_id
        sync.started_at = None
        sync.error_code = None

    audit.record(
        session,
        action="dingtalk.attendance.refresh.queued",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_attendance",
        target_id=sync.id,
        detail={"period": body.period},
    )
    session.commit()
    response = _attendance_snapshot_response(session, body.period)
    background_tasks.add_task(
        runner,
        body.period,
        (principal.user_id, principal.username),
    )
    return response


@router.post("/attendance/preview", response_model=AttendancePreviewOut)
def preview_attendance(
    body: AttendancePreviewRequest,
    principal: Principal = Depends(_require_attendance_reader),
    settings: Settings = Depends(_require_read_sync_enabled),
    client: DingTalkClient = Depends(get_dingtalk_client),
    session: Session = Depends(get_session),
) -> AttendancePreviewOut:
    try:
        start, end = _period_bounds(body.period)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Attendance period cannot be in the future",
        ) from None
    _employees, result, _remote_count = _match_directory(session, client, settings)
    employee_by_user_id = {
        match.user_id: (match.employee_id, match.emp_no, match.local_name)
        for match in result.matches
    }
    try:
        records = client.list_attendance_results(
            user_ids=list(employee_by_user_id),
            start=start,
            end=end,
        )
    except DingTalkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    items = aggregate_attendance_results(
        records,
        employee_by_user_id=employee_by_user_id,
    )
    response = AttendancePreviewOut(
        period=body.period,
        matched_employees=len(result.matches),
        employees_with_records=len(items),
        total_records=sum(item.record_count for item in items),
        ambiguous_directory_users=result.ambiguous_remote_users,
        unmatched_directory_users=result.unmatched_remote_users,
        items=list(items),
    )
    audit.record(
        session,
        action="dingtalk.attendance.preview",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_attendance",
        detail={
            "period": body.period,
            "matched_employees": response.matched_employees,
            "employees_with_records": response.employees_with_records,
            "records": response.total_records,
        },
    )
    session.commit()
    return response
