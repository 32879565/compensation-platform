"""Minimal, fixed-destination DingTalk enterprise-application client.

Only the official DingTalk hosts below are reachable through this client.  It
never logs credentials, access tokens, recipient identifiers, or response
bodies.  Access tokens are cached in memory with an early-refresh margin, as
required by DingTalk's current enterprise-app token contract.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.core.config import Settings, get_settings

# Fixed official endpoint; this value is not a credential.
_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"  # nosec B105
_WORK_NOTIFICATION_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"
_DEPARTMENT_LIST_URL = "https://oapi.dingtalk.com/topapi/v2/department/listsub"
_DEPARTMENT_USER_LIST_URL = "https://oapi.dingtalk.com/topapi/v2/user/list"
_ATTENDANCE_RESULT_URL = "https://oapi.dingtalk.com/attendance/list"
_MAX_RESPONSE_BYTES = 64 * 1024
_TOKEN_REFRESH_MARGIN_SECONDS = 120
_DIRECTORY_PAGE_SIZE = 100
_MAX_DIRECTORY_DEPARTMENTS = 2_000
_MAX_DIRECTORY_USERS = 50_000
_MAX_PROVIDER_PAGES = 1_000
_ATTENDANCE_USER_BATCH_SIZE = 50
_READ_MAX_WORKERS = 8
_READ_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0)
_ATTENDANCE_MAX_WINDOW = timedelta(days=7)


def _optional_provider_text(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 else None


class DingTalkClientError(Exception):
    """A deliberately sanitized DingTalk integration error."""


class DingTalkSendOutcomeUnknown(DingTalkClientError):
    """The provider may have accepted a notification before the response failed."""


class _DingTalkTemporaryError(DingTalkClientError):
    """Internal marker for provider/network failures that are safe to retry."""


@dataclass(frozen=True)
class DingTalkConnection:
    expires_in_seconds: int


@dataclass(frozen=True)
class DingTalkSendResult:
    task_id: int
    request_id: str | None


@dataclass(frozen=True)
class DingTalkDirectoryUser:
    """Minimal contact fields needed for identity matching.

    Provider identifiers stay inside the backend process and are never part of
    a public API response.
    """

    user_id: str
    name: str
    job_number: str | None
    active: bool


@dataclass(frozen=True)
class DingTalkAttendanceResult:
    """A normalized provider check-result row used only for aggregate preview."""

    user_id: str
    work_date: int | str | None
    check_type: str | None
    time_result: str | None
    location_result: str | None
    plan_check_time: int | str | None
    user_check_time: int | str | None
    record_id: int | str | None


class DingTalkClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        agent_id: int,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._agent_id = agent_id
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> DingTalkClient:
        client_id = settings.dingtalk_client_id
        client_secret = settings.dingtalk_client_secret
        agent_id = settings.dingtalk_agent_id
        if client_id is None or client_secret is None or agent_id is None:
            raise DingTalkClientError("DingTalk application credentials are not configured")
        return cls(
            client_id=client_id,
            client_secret=client_secret.get_secret_value(),
            agent_id=agent_id,
            timeout_seconds=settings.dingtalk_timeout_seconds,
        )

    def _read_json_response(self, response: Any) -> dict[str, Any]:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise DingTalkClientError("DingTalk returned an oversized response")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DingTalkClientError("DingTalk returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise DingTalkClientError("DingTalk returned an invalid response")
        return payload

    def _perform(self, request: Request) -> dict[str, Any]:
        try:
            # All callers supply one of this module's fixed official endpoints.
            with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310
                return self._read_json_response(response)
        except HTTPError as exc:
            # Do not include the URL, response body, or exception repr: legacy
            # DingTalk work-notification URLs carry the access token in a query
            # parameter mandated by the provider API.
            raise DingTalkClientError(f"DingTalk rejected the request (HTTP {exc.code})") from None
        except (TimeoutError, URLError, OSError):
            raise _DingTalkTemporaryError("DingTalk is temporarily unreachable") from None

    def _fetch_access_token(self) -> tuple[str, int]:
        body = json.dumps(
            {"appKey": self._client_id, "appSecret": self._client_secret},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._perform(
            Request(
                _TOKEN_URL,
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
        )
        token = payload.get("accessToken")
        expires_in = payload.get("expireIn")
        if not isinstance(token, str) or not token or not isinstance(expires_in, int):
            raise DingTalkClientError("DingTalk authentication returned an invalid response")
        if expires_in <= _TOKEN_REFRESH_MARGIN_SECONDS:
            raise DingTalkClientError("DingTalk authentication returned an invalid expiry")
        return token, expires_in

    def access_token(self, *, force_refresh: bool = False) -> tuple[str, int]:
        now = time.monotonic()
        with self._token_lock:
            if (
                not force_refresh
                and self._access_token is not None
                and now < self._access_token_expires_at
            ):
                return self._access_token, max(1, int(self._access_token_expires_at - now))
            token, expires_in = self._fetch_access_token()
            cached_for = expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + cached_for
            return token, cached_for

    def check_connection(self) -> DingTalkConnection:
        _token, expires_in = self.access_token(force_refresh=True)
        return DingTalkConnection(expires_in_seconds=expires_in)

    def _post_legacy_json(self, url: str, body: dict[str, object]) -> dict[str, Any]:
        """POST JSON to one of this module's fixed legacy OpenAPI destinations."""

        for attempt in range(len(_READ_RETRY_DELAYS_SECONDS) + 1):
            try:
                token, _expires_in = self.access_token()
                request_url = f"{url}?{urlencode({'access_token': token})}"
                payload = self._perform(
                    Request(
                        request_url,
                        data=json.dumps(
                            body,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                )
                errcode = payload.get("errcode")
                if errcode != 0:
                    safe_code = (
                        errcode
                        if isinstance(errcode, int) and not isinstance(errcode, bool)
                        else -1
                    )
                    raise DingTalkClientError(
                        f"DingTalk rejected the read request (code {safe_code})"
                    )
                return payload
            except _DingTalkTemporaryError:
                if attempt >= len(_READ_RETRY_DELAYS_SECONDS):
                    raise
                time.sleep(_READ_RETRY_DELAYS_SECONDS[attempt])
        raise DingTalkClientError("DingTalk is temporarily unreachable")

    @staticmethod
    def _require_list(value: object) -> list[object]:
        if not isinstance(value, list):
            raise DingTalkClientError("DingTalk returned an invalid directory response")
        return value

    def list_directory_users(self) -> tuple[DingTalkDirectoryUser, ...]:
        """Read every visible department and its direct members.

        DingTalk's department and member endpoints are direct-child/direct-member
        APIs, so both hierarchies are traversed explicitly with hard limits and
        bounded parallelism. Users appearing in several departments are
        deduplicated by provider ID.
        """

        def child_department_ids(department_id: int) -> tuple[int, ...]:
            payload = self._post_legacy_json(
                _DEPARTMENT_LIST_URL,
                {"dept_id": department_id},
            )
            children = self._require_list(payload.get("result"))
            child_ids: list[int] = []
            for raw_child in children:
                if not isinstance(raw_child, dict):
                    raise DingTalkClientError("DingTalk returned an invalid directory response")
                child_id = raw_child.get("dept_id")
                if not isinstance(child_id, int) or isinstance(child_id, bool) or child_id <= 0:
                    raise DingTalkClientError("DingTalk returned an invalid directory response")
                child_ids.append(child_id)
            return tuple(child_ids)

        page_lock = threading.Lock()
        page_count = 0

        def department_users(department_id: int) -> tuple[DingTalkDirectoryUser, ...]:
            nonlocal page_count
            page_cursor = 0
            seen_cursors: set[int] = set()
            users: list[DingTalkDirectoryUser] = []
            while True:
                if page_cursor in seen_cursors:
                    raise DingTalkClientError("DingTalk returned an invalid directory cursor")
                seen_cursors.add(page_cursor)
                with page_lock:
                    page_count += 1
                    if page_count > _MAX_PROVIDER_PAGES:
                        raise DingTalkClientError("DingTalk directory exceeds the safety limit")
                payload = self._post_legacy_json(
                    _DEPARTMENT_USER_LIST_URL,
                    {
                        "dept_id": department_id,
                        "cursor": page_cursor,
                        "size": _DIRECTORY_PAGE_SIZE,
                        "contain_access_limit": False,
                    },
                )
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise DingTalkClientError("DingTalk returned an invalid directory response")
                raw_users = self._require_list(result.get("list"))
                for raw_user in raw_users:
                    if not isinstance(raw_user, dict):
                        raise DingTalkClientError("DingTalk returned an invalid directory response")
                    user_id = raw_user.get("userid")
                    name = raw_user.get("name")
                    if not isinstance(user_id, str) or not user_id.strip():
                        raise DingTalkClientError("DingTalk returned an invalid directory response")
                    if not isinstance(name, str) or not name.strip():
                        raise DingTalkClientError("DingTalk returned an invalid directory response")
                    normalized_user_id = user_id.strip()
                    if len(normalized_user_id) > 256 or len(name.strip()) > 128:
                        raise DingTalkClientError("DingTalk returned an invalid directory response")
                    raw_job_number = raw_user.get("job_number")
                    job_number = (
                        raw_job_number.strip()
                        if isinstance(raw_job_number, str) and raw_job_number.strip()
                        else None
                    )
                    if job_number is not None and len(job_number) > 128:
                        raise DingTalkClientError("DingTalk returned an invalid directory response")
                    raw_active = raw_user.get("active", True)
                    active = raw_active if isinstance(raw_active, bool) else True
                    users.append(
                        DingTalkDirectoryUser(
                            user_id=normalized_user_id,
                            name=name.strip(),
                            job_number=job_number,
                            active=active,
                        )
                    )
                has_more = result.get("has_more", False)
                if not isinstance(has_more, bool):
                    raise DingTalkClientError("DingTalk returned an invalid directory response")
                if not has_more:
                    return tuple(users)
                next_cursor = result.get("next_cursor")
                if (
                    not isinstance(next_cursor, int)
                    or isinstance(next_cursor, bool)
                    or next_cursor < 0
                ):
                    raise DingTalkClientError("DingTalk returned an invalid directory cursor")
                page_cursor = next_cursor

        department_ids = [1]
        known_department_ids = {1}
        frontier = [1]
        users_by_id: dict[str, DingTalkDirectoryUser] = {}
        with ThreadPoolExecutor(
            max_workers=_READ_MAX_WORKERS,
            thread_name_prefix="dingtalk-directory",
        ) as executor:
            while frontier:
                child_groups = executor.map(child_department_ids, frontier)
                next_frontier: list[int] = []
                for child_ids in child_groups:
                    for child_id in child_ids:
                        if child_id in known_department_ids:
                            continue
                        known_department_ids.add(child_id)
                        department_ids.append(child_id)
                        next_frontier.append(child_id)
                        if len(department_ids) > _MAX_DIRECTORY_DEPARTMENTS:
                            raise DingTalkClientError("DingTalk directory exceeds the safety limit")
                frontier = next_frontier

            for users in executor.map(department_users, department_ids):
                for user in users:
                    users_by_id[user.user_id] = user
                    if len(users_by_id) > _MAX_DIRECTORY_USERS:
                        raise DingTalkClientError("DingTalk directory exceeds the safety limit")

        return tuple(users_by_id.values())

    def list_attendance_results(
        self,
        *,
        user_ids: list[str] | tuple[str, ...],
        start: datetime,
        end: datetime,
    ) -> tuple[DingTalkAttendanceResult, ...]:
        """Read check results in provider-sized batches without persisting raw punches."""

        if end < start:
            raise ValueError("attendance end must not precede start")
        if end - start > timedelta(days=31):
            raise ValueError("attendance range must not exceed 31 days")
        normalized_user_ids = list(
            dict.fromkeys(user_id.strip() for user_id in user_ids if user_id.strip())
        )
        if len(normalized_user_ids) > _MAX_DIRECTORY_USERS:
            raise ValueError("attendance user list exceeds the safety limit")
        if any(len(user_id) > 256 for user_id in normalized_user_ids):
            raise ValueError("attendance user identifier is invalid")
        if not normalized_user_ids:
            return ()

        windows: list[tuple[datetime, datetime]] = []
        window_start = start
        while window_start <= end:
            window_end = min(
                end,
                window_start + _ATTENDANCE_MAX_WINDOW - timedelta(seconds=1),
            )
            windows.append((window_start, window_end))
            window_start = window_end + timedelta(seconds=1)

        batches = [
            normalized_user_ids[batch_start : batch_start + _ATTENDANCE_USER_BATCH_SIZE]
            for batch_start in range(
                0,
                len(normalized_user_ids),
                _ATTENDANCE_USER_BATCH_SIZE,
            )
        ]
        tasks = [
            (batch, window_start, window_end)
            for window_start, window_end in windows
            for batch in batches
        ]

        def attendance_for_task(
            task: tuple[list[str], datetime, datetime],
        ) -> tuple[DingTalkAttendanceResult, ...]:
            batch, batch_window_start, batch_window_end = task
            task_records: list[DingTalkAttendanceResult] = []
            start_text = batch_window_start.strftime("%Y-%m-%d %H:%M:%S")
            end_text = batch_window_end.strftime("%Y-%m-%d %H:%M:%S")
            offset = 0
            page_count = 0
            while True:
                page_count += 1
                if page_count > _MAX_PROVIDER_PAGES:
                    raise DingTalkClientError("DingTalk attendance exceeds the safety limit")
                payload = self._post_legacy_json(
                    _ATTENDANCE_RESULT_URL,
                    {
                        "workDateFrom": start_text,
                        "workDateTo": end_text,
                        "userIdList": batch,
                        "offset": offset,
                        "limit": _ATTENDANCE_USER_BATCH_SIZE,
                    },
                )
                raw_records = payload.get("recordresult")
                if not isinstance(raw_records, list):
                    raise DingTalkClientError("DingTalk returned an invalid attendance response")
                for raw_record in raw_records:
                    if not isinstance(raw_record, dict):
                        raise DingTalkClientError(
                            "DingTalk returned an invalid attendance response"
                        )
                    user_id = raw_record.get("userId")
                    if not isinstance(user_id, str) or user_id not in batch:
                        raise DingTalkClientError(
                            "DingTalk returned an invalid attendance response"
                        )

                    task_records.append(
                        DingTalkAttendanceResult(
                            user_id=user_id,
                            work_date=raw_record.get("workDate"),
                            check_type=_optional_provider_text(raw_record.get("checkType")),
                            time_result=_optional_provider_text(raw_record.get("timeResult")),
                            location_result=_optional_provider_text(
                                raw_record.get("locationResult")
                            ),
                            plan_check_time=raw_record.get("planCheckTime"),
                            user_check_time=raw_record.get("userCheckTime"),
                            record_id=raw_record.get("recordId"),
                        )
                    )
                has_more = payload.get("hasMore", False)
                if not isinstance(has_more, bool):
                    raise DingTalkClientError("DingTalk returned an invalid attendance response")
                if not has_more:
                    return tuple(task_records)
                if not raw_records:
                    raise DingTalkClientError("DingTalk returned an invalid attendance cursor")
                offset += len(raw_records)

        records: list[DingTalkAttendanceResult] = []
        with ThreadPoolExecutor(
            max_workers=_READ_MAX_WORKERS,
            thread_name_prefix="dingtalk-attendance",
        ) as executor:
            for task_records in executor.map(attendance_for_task, tasks):
                records.extend(task_records)
        return tuple(records)

    def send_action_card(
        self,
        *,
        recipient_user_id: str,
        title: str,
        markdown: str,
        action_url: str,
    ) -> DingTalkSendResult:
        recipient = recipient_user_id.strip()
        if not recipient or len(recipient) > 256:
            raise DingTalkClientError("The DingTalk recipient identifier is invalid")
        if not title.strip() or len(title) > 128:
            raise DingTalkClientError("The DingTalk notification title is invalid")
        if not markdown.strip() or len(markdown) > 1000:
            raise DingTalkClientError("The DingTalk notification body is invalid")
        if not action_url.startswith("https://") or len(action_url) > 500:
            raise DingTalkClientError("The DingTalk notification action URL is invalid")

        token, _expires_in = self.access_token()
        message = {
            "msgtype": "action_card",
            "action_card": {
                "title": title,
                "markdown": markdown,
                "single_title": "查看并申诉",
                "single_url": action_url,
            },
        }
        serialized_message = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(serialized_message.encode("utf-8")) > 2048:
            raise DingTalkClientError("The DingTalk notification exceeds the provider limit")
        form = urlencode(
            {
                "agent_id": str(self._agent_id),
                "userid_list": recipient,
                "to_all_user": "false",
                "msg": serialized_message,
            }
        ).encode("utf-8")
        # DingTalk's work-notification API currently requires access_token as a
        # query parameter.  The URL is never logged or surfaced in exceptions.
        request_url = f"{_WORK_NOTIFICATION_URL}?{urlencode({'access_token': token})}"
        try:
            payload = self._perform(
                Request(
                    request_url,
                    data=form,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    },
                    method="POST",
                )
            )
        except DingTalkClientError:
            # Once the POST has started, a missing/invalid response cannot prove
            # that DingTalk rejected it.  Callers must reconcile instead of
            # blindly sending the same salary notification again.
            raise DingTalkSendOutcomeUnknown(
                "DingTalk notification outcome could not be confirmed"
            ) from None
        errcode = payload.get("errcode")
        if type(errcode) is not int:
            raise DingTalkSendOutcomeUnknown("DingTalk notification outcome could not be confirmed")
        if errcode != 0:
            raise DingTalkClientError(f"DingTalk rejected the notification (code {errcode})")
        task_id = payload.get("task_id")
        request_id = payload.get("request_id")
        if type(task_id) is not int or task_id <= 0:
            raise DingTalkSendOutcomeUnknown("DingTalk notification outcome could not be confirmed")
        return DingTalkSendResult(
            task_id=task_id,
            request_id=request_id if isinstance(request_id, str) else None,
        )


@lru_cache
def get_dingtalk_client() -> DingTalkClient:
    """Return the process-local client so its access-token cache is reused."""

    return DingTalkClient.from_settings(get_settings())
