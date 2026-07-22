from __future__ import annotations

import json
import threading
from datetime import datetime
from urllib.error import URLError

import pytest

from app.dingtalk import client as client_module
from app.dingtalk.client import (
    DingTalkClient,
    DingTalkClientError,
    DingTalkDepartment,
    DingTalkDirectoryUser,
    DingTalkOrganizationAccess,
    DingTalkOrganizationSnapshot,
    DingTalkOrganizationUser,
)
from app.dingtalk.read_sync import (
    LocalEmployeeIdentity,
    aggregate_attendance_results,
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
    match_directory_users,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


def _local(
    employee_id: int,
    emp_no: str,
    name: str,
    *,
    linked_hash: str | None = None,
) -> LocalEmployeeIdentity:
    return LocalEmployeeIdentity(
        employee_id=employee_id,
        emp_no=emp_no,
        name=name,
        dingtalk_user_id_hash=linked_hash,
    )


def test_organization_snapshot_models_preserve_provider_identity_and_membership():
    department = DingTalkDepartment(department_id=2, parent_id=1, name="Store A")
    user = DingTalkOrganizationUser(
        user_id="u-manager",
        name="Manager",
        job_number="M001",
        title="Store Manager",
        active=True,
        department_ids=(2, 3),
    )

    snapshot = DingTalkOrganizationSnapshot(departments=(department,), users=(user,))

    assert snapshot.departments == (department,)
    assert snapshot.users == (user,)


def test_organization_snapshot_recurses_paginates_and_aggregates_memberships(monkeypatch):
    def fake_urlopen(request, *, timeout):
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})

        body = json.loads(request.data.decode("utf-8"))
        if "/department/listsub" in request.full_url:
            children_by_parent = {
                1: [
                    {"dept_id": 2, "parent_id": 1, "name": "Store A"},
                    {"dept_id": 3, "parent_id": 1, "name": "Store B"},
                ],
                2: [{"dept_id": 4, "parent_id": 2, "name": "Kitchen"}],
                3: [],
                4: [],
            }
            return _Response({"errcode": 0, "result": children_by_parent[body["dept_id"]]})

        assert "/user/list" in request.full_url
        department_id = body["dept_id"]
        cursor = body["cursor"]
        if department_id == 2 and cursor == 0:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "has_more": True,
                        "next_cursor": 100,
                        "list": [
                            {
                                "userid": "u-manager",
                                "name": "Manager",
                                "job_number": "M001",
                                "title": "Store Manager",
                                "active": True,
                            }
                        ],
                    },
                }
            )
        if department_id == 2 and cursor == 100:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "has_more": False,
                        "list": [
                            {
                                "userid": "u-assistant",
                                "name": "Assistant",
                                "job_number": None,
                                "title": "Assistant Manager",
                                "active": True,
                            }
                        ],
                    },
                }
            )
        if department_id == 3:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "has_more": False,
                        "list": [
                            {
                                "userid": "u-manager",
                                "name": "Manager",
                                "job_number": "M001",
                                "title": "Store Manager",
                                "active": True,
                            }
                        ],
                    },
                }
            )
        if department_id == 4:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "has_more": False,
                        "list": [
                            {
                                "userid": "u-chef",
                                "name": "Chef",
                                "job_number": "K001",
                                "title": "Kitchen Manager",
                                "active": True,
                            }
                        ],
                    },
                }
            )
        return _Response({"errcode": 0, "result": {"has_more": False, "list": []}})

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    snapshot = client.list_organization_snapshot()

    assert snapshot.departments == (
        DingTalkDepartment(department_id=2, parent_id=1, name="Store A"),
        DingTalkDepartment(department_id=3, parent_id=1, name="Store B"),
        DingTalkDepartment(department_id=4, parent_id=2, name="Kitchen"),
    )
    users_by_id = {user.user_id: user for user in snapshot.users}
    assert users_by_id["u-manager"] == DingTalkOrganizationUser(
        user_id="u-manager",
        name="Manager",
        job_number="M001",
        title="Store Manager",
        active=True,
        department_ids=(2, 3),
    )
    assert users_by_id["u-assistant"].department_ids == (2,)
    assert users_by_id["u-chef"].department_ids == (4,)


def test_organization_snapshot_rejects_department_seen_under_two_parents(monkeypatch):
    user_reads = 0

    def fake_urlopen(request, *, timeout):
        nonlocal user_reads
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})

        body = json.loads(request.data.decode("utf-8"))
        if "/department/listsub" in request.full_url:
            children_by_parent = {
                1: [
                    {"dept_id": 2, "parent_id": 1, "name": "Parent A"},
                    {"dept_id": 3, "parent_id": 1, "name": "Parent B"},
                ],
                2: [{"dept_id": 4, "parent_id": 2, "name": "Shared child"}],
                3: [{"dept_id": 4, "parent_id": 3, "name": "Shared child"}],
            }
            return _Response({"errcode": 0, "result": children_by_parent[body["dept_id"]]})

        user_reads += 1
        return _Response({"errcode": 0, "result": {"has_more": False, "list": []}})

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    with pytest.raises(DingTalkClientError, match="duplicate organization department"):
        client.list_organization_snapshot()

    assert user_reads == 0


def _install_single_organization_user_response(monkeypatch, raw_user: dict[str, object]):
    def fake_urlopen(request, *, timeout):
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})
        if "/department/listsub" in request.full_url:
            return _Response({"errcode": 0, "result": []})
        return _Response(
            {
                "errcode": 0,
                "result": {
                    "has_more": False,
                    "list": [raw_user],
                },
            }
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)


def test_organization_snapshot_rejects_user_without_active(monkeypatch):
    _install_single_organization_user_response(
        monkeypatch,
        {"userid": "u-manager", "name": "Manager"},
    )
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    with pytest.raises(DingTalkClientError, match="invalid directory response"):
        client.list_organization_snapshot()


def test_organization_snapshot_accepts_case_insensitive_false_active(monkeypatch):
    _install_single_organization_user_response(
        monkeypatch,
        {"userid": "u-manager", "name": "Manager", "active": "FaLsE"},
    )
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    snapshot = client.list_organization_snapshot()

    assert snapshot.users[0].active is False


def test_organization_snapshot_rejects_invalid_active_string(monkeypatch):
    _install_single_organization_user_response(
        monkeypatch,
        {"userid": "u-manager", "name": "Manager", "active": "unknown"},
    )
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    with pytest.raises(DingTalkClientError, match="invalid directory response"):
        client.list_organization_snapshot()


def test_organization_access_reads_current_user_and_complete_parent_paths(monkeypatch):
    requests: list[tuple[str, dict[str, object]]] = []

    def fake_urlopen(request, *, timeout):
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})
        body = json.loads(request.data.decode("utf-8"))
        requests.append((request.full_url, body))
        if "/user/get" in request.full_url:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "userid": "u-manager",
                        "name": "Manager",
                        "job_number": "M001",
                        "title": "Store Manager",
                        "active": True,
                        "dept_id_list": [31, 41],
                    },
                }
            )
        assert "/department/listparentbyuser" in request.full_url
        return _Response(
            {
                "errcode": 0,
                "result": {
                    "parent_list": [
                        {"parent_dept_id_list": [31, 20, 1]},
                        {"parent_dept_id_list": [41, 40, 1]},
                    ]
                },
            }
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    access = client.get_organization_access(" u-manager ")

    assert access == DingTalkOrganizationAccess(
        user=DingTalkOrganizationUser(
            user_id="u-manager",
            name="Manager",
            job_number="M001",
            title="Store Manager",
            active=True,
            department_ids=(31, 41),
        ),
        parent_department_paths=((31, 20, 1), (41, 40, 1)),
    )
    assert [body for _url, body in requests] == [
        {"userid": "u-manager", "language": "zh_CN"},
        {"userid": "u-manager"},
    ]


def test_organization_access_rejects_inconsistent_membership_reads(monkeypatch):
    def fake_urlopen(request, *, timeout):
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})
        if "/user/get" in request.full_url:
            return _Response(
                {
                    "errcode": 0,
                    "result": {
                        "userid": "u-manager",
                        "name": "Manager",
                        "title": "Store Manager",
                        "active": True,
                        "dept_id_list": [31],
                    },
                }
            )
        return _Response(
            {
                "errcode": 0,
                "result": {"parent_list": [{"parent_dept_id_list": [99, 20, 1]}]},
            }
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    with pytest.raises(DingTalkClientError, match="inconsistent organization access"):
        client.get_organization_access("u-manager")


def test_organization_identity_proof_binds_tenant_batch_scope_and_employee():
    user_hash = blind_index_dingtalk_user_id(
        "u-manager",
        key="test-encryption-key-only-for-tests",
    )
    arguments = {
        "key": "test-encryption-key-only-for-tests",
        "tenant_id": "ding-corp-a",
        "batch_public_id": "a" * 32,
        "snapshot_hash": "b" * 64,
        "remote_department_id": 700,
        "org_unit_id": 70,
        "department": "DINING",
        "employee_id": 7,
    }

    proof = dingtalk_organization_identity_proof(user_hash, **arguments)

    for field, changed in (
        ("tenant_id", "ding-corp-b"),
        ("batch_public_id", "c" * 32),
        ("snapshot_hash", "d" * 64),
        ("remote_department_id", 701),
        ("org_unit_id", 71),
        ("department", "KITCHEN"),
        ("employee_id", 8),
    ):
        mutated = {**arguments, field: changed}
        assert dingtalk_organization_identity_proof(user_hash, **mutated) != proof


def test_directory_matching_prefers_stable_binding_then_job_number_then_unique_name():
    key = "test-encryption-key-only-for-tests"
    existing_hash = blind_index_dingtalk_user_id("provider-existing", key=key)
    locals_ = [
        _local(1, "E001", "旧姓名", linked_hash=existing_hash),
        _local(2, "E002", "王芳"),
        _local(3, "LOCAL-3", "李雷"),
    ]
    remotes = [
        DingTalkDirectoryUser(
            user_id="provider-existing", name="新姓名", job_number="OTHER", active=True
        ),
        DingTalkDirectoryUser(
            user_id="provider-job", name="另一姓名", job_number="e002", active=True
        ),
        DingTalkDirectoryUser(user_id="provider-name", name=" 李雷 ", job_number=None, active=True),
    ]

    result = match_directory_users(locals_, remotes, encryption_key=key)

    assert [(match.employee_id, match.method) for match in result.matches] == [
        (1, "STABLE_ID"),
        (2, "JOB_NUMBER"),
        (3, "UNIQUE_NAME"),
    ]
    assert result.ambiguous_remote_users == 0
    assert result.unmatched_remote_users == 0


def test_duplicate_names_or_duplicate_job_numbers_are_never_automatically_bound():
    locals_ = [
        _local(1, "E001", "张伟"),
        _local(2, "E002", "张伟"),
        _local(3, "E003", "唯一姓名"),
    ]
    remotes = [
        DingTalkDirectoryUser("remote-1", "张伟", None, True),
        DingTalkDirectoryUser("remote-2", "张伟", None, True),
        DingTalkDirectoryUser("remote-3", "唯一姓名", "E003", True),
        DingTalkDirectoryUser("remote-4", "另一个人", "E003", True),
    ]

    result = match_directory_users(
        locals_,
        remotes,
        encryption_key="test-encryption-key-only-for-tests",
    )

    assert result.matches == ()
    assert result.ambiguous_remote_users == 4
    assert result.unmatched_remote_users == 0


def test_directory_client_recurses_departments_paginates_and_deduplicates_users(monkeypatch):
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})

        body = json.loads(request.data.decode("utf-8"))
        if "/department/listsub" in request.full_url:
            department_id = body["dept_id"]
            children = [{"dept_id": 2, "name": "门店"}] if department_id == 1 else []
            return _Response({"errcode": 0, "errmsg": "ok", "result": children})

        assert "/user/list" in request.full_url
        if body["dept_id"] == 1:
            return _Response(
                {
                    "errcode": 0,
                    "errmsg": "ok",
                    "result": {
                        "has_more": True,
                        "next_cursor": 100,
                        "list": [
                            {
                                "userid": "u-root",
                                "name": "根员工",
                                "job_number": "E001",
                                "active": True,
                            }
                        ],
                    },
                }
            )
        if body["dept_id"] == 1 and body["cursor"] == 100:
            raise AssertionError("unreachable branch")
        return _Response(
            {
                "errcode": 0,
                "errmsg": "ok",
                "result": {
                    "has_more": False,
                    "list": [
                        {
                            "userid": "u-child",
                            "name": "门店员工",
                            "job_number": None,
                            "active": True,
                        },
                        {
                            "userid": "u-root",
                            "name": "根员工",
                            "job_number": "E001",
                            "active": True,
                        },
                    ],
                },
            }
        )

    # Return the second root page before the child department page.
    root_page_calls = 0

    def paginated_urlopen(request, *, timeout):
        nonlocal root_page_calls
        if "/user/list" in request.full_url:
            body = json.loads(request.data.decode("utf-8"))
            if body["dept_id"] == 1:
                root_page_calls += 1
                if body["cursor"] == 100:
                    requests.append((request, timeout))
                    return _Response(
                        {
                            "errcode": 0,
                            "errmsg": "ok",
                            "result": {
                                "has_more": False,
                                "list": [
                                    {
                                        "userid": "u-root-2",
                                        "name": "第二页",
                                        "job_number": "E002",
                                        "active": True,
                                    }
                                ],
                            },
                        }
                    )
        return fake_urlopen(request, timeout=timeout)

    monkeypatch.setattr(client_module, "urlopen", paginated_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    users = client.list_directory_users()

    assert {user.user_id for user in users} == {"u-root", "u-root-2", "u-child"}
    assert root_page_calls == 2
    provider_urls = [request.full_url for request, _timeout in requests[1:]]
    assert provider_urls
    assert all(url.startswith("https://oapi.dingtalk.com/") for url in provider_urls)


def test_directory_client_reads_sibling_departments_in_parallel(monkeypatch):
    sibling_barrier = threading.Barrier(2, timeout=1)

    def fake_urlopen(request, *, timeout):
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})

        body = json.loads(request.data.decode("utf-8"))
        if "/department/listsub" in request.full_url:
            department_id = body["dept_id"]
            if department_id == 1:
                return _Response(
                    {
                        "errcode": 0,
                        "result": [
                            {"dept_id": 2, "name": "Sibling A"},
                            {"dept_id": 3, "name": "Sibling B"},
                        ],
                    }
                )
            try:
                sibling_barrier.wait()
            except threading.BrokenBarrierError as exc:
                raise AssertionError("sibling departments were fetched serially") from exc
            return _Response({"errcode": 0, "result": []})

        return _Response(
            {
                "errcode": 0,
                "result": {"has_more": False, "list": []},
            }
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    assert client.list_directory_users() == ()


def test_directory_client_retries_transient_read_failures(monkeypatch):
    department_attempts = 0
    sleep_delays: list[float] = []

    def fake_urlopen(request, *, timeout):
        nonlocal department_attempts
        if request.full_url == client_module._TOKEN_URL:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})

        if "/department/listsub" in request.full_url:
            department_attempts += 1
            if department_attempts == 1:
                raise URLError("temporary test failure")
            return _Response({"errcode": 0, "result": []})

        return _Response(
            {
                "errcode": 0,
                "result": {"has_more": False, "list": []},
            }
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    assert client.list_directory_users() == ()
    assert department_attempts == 2
    assert sleep_delays


def test_attendance_client_batches_users_and_summary_never_exposes_provider_ids(monkeypatch):
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        if len(requests) == 1:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})
        body = json.loads(request.data.decode("utf-8"))
        records = [
            {
                "userId": user_id,
                "workDate": 1782864000000,
                "checkType": "OnDuty",
                "timeResult": "Late" if user_id == "u-1" else "Normal",
                "locationResult": "Normal",
                "recordId": index + 1,
            }
            for index, user_id in enumerate(body["userIdList"])
        ]
        return _Response({"errcode": 0, "errmsg": "ok", "recordresult": records, "hasMore": False})

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)
    user_ids = [f"u-{index}" for index in range(1, 52)]

    records = client.list_attendance_results(
        user_ids=user_ids,
        start=datetime(2026, 7, 1, 0, 0, 0),
        end=datetime(2026, 7, 31, 23, 59, 59),
    )
    summary = aggregate_attendance_results(
        records,
        employee_by_user_id={"u-1": (101, "E001", "张一")},
    )

    attendance_requests = requests[1:]
    attendance_bodies = [json.loads(request.data) for request in attendance_requests]
    assert sorted(len(body["userIdList"]) for body in attendance_bodies) == [1] * 5 + [50] * 5
    assert sorted(
        (body["workDateFrom"], body["workDateTo"]) for body in attendance_bodies
    ) == sorted(
        [
            ("2026-07-01 00:00:00", "2026-07-07 23:59:59"),
            ("2026-07-08 00:00:00", "2026-07-14 23:59:59"),
            ("2026-07-15 00:00:00", "2026-07-21 23:59:59"),
            ("2026-07-22 00:00:00", "2026-07-28 23:59:59"),
            ("2026-07-29 00:00:00", "2026-07-31 23:59:59"),
        ]
        * 2
    )
    assert all(
        request.full_url.startswith("https://oapi.dingtalk.com/attendance/list?")
        for request in attendance_requests
    )
    assert summary[0].employee_id == 101
    assert summary[0].late_count == 5
    assert "u-1" not in summary[0].model_dump_json()


def test_attendance_client_rejects_ranges_longer_than_one_month_without_a_request(monkeypatch):
    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(client_module, "urlopen", unexpected_urlopen)
    client = DingTalkClient(client_id="ding-client", client_secret="secret-value", agent_id=123)

    with pytest.raises(ValueError, match="31 days"):
        client.list_attendance_results(
            user_ids=["u-1"],
            start=datetime(2026, 6, 1),
            end=datetime(2026, 7, 31),
        )
