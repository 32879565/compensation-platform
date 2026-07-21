"""Privacy-minimizing matching and attendance-preview helpers for DingTalk."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from app.dingtalk.client import DingTalkAttendanceResult, DingTalkDirectoryUser

MatchMethod = Literal["STABLE_ID", "JOB_NUMBER", "UNIQUE_NAME"]


@dataclass(frozen=True)
class LocalEmployeeIdentity:
    employee_id: int
    emp_no: str
    name: str
    dingtalk_user_id_hash: str | None = None


@dataclass(frozen=True)
class DirectoryMatch:
    employee_id: int
    emp_no: str
    local_name: str
    remote_name: str
    remote_job_number: str | None
    method: MatchMethod
    # Internal-only values. API response models deliberately omit both fields.
    user_id: str
    user_id_hash: str


@dataclass(frozen=True)
class DirectoryMatchResult:
    matches: tuple[DirectoryMatch, ...]
    ambiguous_remote_users: int
    unmatched_remote_users: int


class AttendancePreviewRow(BaseModel):
    employee_id: int
    emp_no: str
    name: str
    record_count: int
    normal_count: int
    late_count: int
    early_count: int
    absent_count: int
    not_signed_count: int
    other_count: int


def _normalized_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def blind_index_dingtalk_user_id(value: str, *, key: str) -> str:
    """Return a domain-separated keyed digest suitable for equality matching."""

    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("DingTalk user identifier is invalid")
    if not key:
        raise ValueError("blind-index key is required")
    derived_key = hashlib.sha256(
        b"compensation-platform:dingtalk-user-id:v1\0" + key.encode("utf-8")
    ).digest()
    return hmac.new(derived_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def match_directory_users(
    local_employees: list[LocalEmployeeIdentity] | tuple[LocalEmployeeIdentity, ...],
    remote_users: list[DingTalkDirectoryUser] | tuple[DingTalkDirectoryUser, ...],
    *,
    encryption_key: str,
) -> DirectoryMatchResult:
    """Match deterministically and leave every collision for human review."""

    locals_by_id = {employee.employee_id: employee for employee in local_employees}
    remote_hashes = [
        blind_index_dingtalk_user_id(user.user_id, key=encryption_key) for user in remote_users
    ]
    linked_local_by_hash: dict[str, list[LocalEmployeeIdentity]] = defaultdict(list)
    for employee in local_employees:
        if employee.dingtalk_user_id_hash:
            linked_local_by_hash[employee.dingtalk_user_id_hash].append(employee)

    matched_local_ids: set[int] = set()
    matched_remote_indexes: set[int] = set()
    ambiguous_remote_indexes: set[int] = set()
    matches: list[DirectoryMatch] = []

    def add_match(remote_index: int, employee: LocalEmployeeIdentity, method: MatchMethod) -> None:
        user = remote_users[remote_index]
        matches.append(
            DirectoryMatch(
                employee_id=employee.employee_id,
                emp_no=employee.emp_no,
                local_name=employee.name,
                remote_name=user.name,
                remote_job_number=user.job_number,
                method=method,
                user_id=user.user_id,
                user_id_hash=remote_hashes[remote_index],
            )
        )
        matched_local_ids.add(employee.employee_id)
        matched_remote_indexes.add(remote_index)

    # Existing stable links always win, including after a name or job-number change.
    remote_hash_counts = Counter(remote_hashes)
    for remote_index, user_hash in enumerate(remote_hashes):
        candidates = linked_local_by_hash.get(user_hash, [])
        if len(candidates) == 1 and remote_hash_counts[user_hash] == 1:
            add_match(remote_index, candidates[0], "STABLE_ID")
        elif candidates:
            ambiguous_remote_indexes.add(remote_index)

    # A provider job number is considered safe only when it is unique on both sides.
    local_by_emp_no: dict[str, list[LocalEmployeeIdentity]] = defaultdict(list)
    for employee in local_employees:
        if employee.employee_id not in matched_local_ids and employee.dingtalk_user_id_hash is None:
            local_by_emp_no[_normalized_identity(employee.emp_no)].append(employee)
    remote_by_job_number: dict[str, list[int]] = defaultdict(list)
    for remote_index, user in enumerate(remote_users):
        if remote_index in matched_remote_indexes or not user.job_number:
            continue
        remote_by_job_number[_normalized_identity(user.job_number)].append(remote_index)
    for job_number, remote_indexes in remote_by_job_number.items():
        candidates = local_by_emp_no.get(job_number, [])
        if len(remote_indexes) == 1 and len(candidates) == 1:
            add_match(remote_indexes[0], candidates[0], "JOB_NUMBER")
        elif candidates:
            ambiguous_remote_indexes.update(remote_indexes)

    # User-requested name fallback: exact normalized names, unique on both sides only.
    local_by_name: dict[str, list[LocalEmployeeIdentity]] = defaultdict(list)
    for employee in locals_by_id.values():
        if employee.employee_id not in matched_local_ids and employee.dingtalk_user_id_hash is None:
            local_by_name[_normalized_identity(employee.name)].append(employee)
    remote_by_name: dict[str, list[int]] = defaultdict(list)
    for remote_index, user in enumerate(remote_users):
        if remote_index in matched_remote_indexes or remote_index in ambiguous_remote_indexes:
            continue
        remote_by_name[_normalized_identity(user.name)].append(remote_index)
    for name, remote_indexes in remote_by_name.items():
        candidates = local_by_name.get(name, [])
        if len(remote_indexes) == 1 and len(candidates) == 1:
            add_match(remote_indexes[0], candidates[0], "UNIQUE_NAME")
        elif candidates:
            ambiguous_remote_indexes.update(remote_indexes)

    unmatched_remote_users = (
        len(remote_users) - len(matched_remote_indexes) - len(ambiguous_remote_indexes)
    )
    return DirectoryMatchResult(
        matches=tuple(sorted(matches, key=lambda match: match.employee_id)),
        ambiguous_remote_users=len(ambiguous_remote_indexes),
        unmatched_remote_users=unmatched_remote_users,
    )


def aggregate_attendance_results(
    records: list[DingTalkAttendanceResult] | tuple[DingTalkAttendanceResult, ...],
    *,
    employee_by_user_id: dict[str, tuple[int, str, str]],
) -> tuple[AttendancePreviewRow, ...]:
    """Aggregate status counts while dropping provider IDs and raw timestamps."""

    counts: dict[int, dict[str, int | str]] = {}
    for record in records:
        identity = employee_by_user_id.get(record.user_id)
        if identity is None:
            continue
        employee_id, emp_no, name = identity
        row = counts.setdefault(
            employee_id,
            {
                "emp_no": emp_no,
                "name": name,
                "record_count": 0,
                "normal_count": 0,
                "late_count": 0,
                "early_count": 0,
                "absent_count": 0,
                "not_signed_count": 0,
                "other_count": 0,
            },
        )
        row["record_count"] = int(row["record_count"]) + 1
        time_result = (record.time_result or "").casefold()
        bucket = {
            "normal": "normal_count",
            "late": "late_count",
            "seriouslate": "late_count",
            "early": "early_count",
            "absenteeism": "absent_count",
            "notsigned": "not_signed_count",
        }.get(time_result, "other_count")
        row[bucket] = int(row[bucket]) + 1

    return tuple(
        AttendancePreviewRow(
            employee_id=employee_id,
            emp_no=str(row["emp_no"]),
            name=str(row["name"]),
            record_count=int(row["record_count"]),
            normal_count=int(row["normal_count"]),
            late_count=int(row["late_count"]),
            early_count=int(row["early_count"]),
            absent_count=int(row["absent_count"]),
            not_signed_count=int(row["not_signed_count"]),
            other_count=int(row["other_count"]),
        )
        for employee_id, row in sorted(counts.items())
    )
