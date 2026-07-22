from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from app.dingtalk import client as client_module
from app.dingtalk.client import DingTalkClient, DingTalkClientError


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


def test_token_is_cached_and_action_card_uses_fixed_provider_destinations(monkeypatch):
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            return _Response({"accessToken": "provider-token", "expireIn": 7200})
        return _Response({"errcode": 0, "task_id": 42, "request_id": "req-1"})

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(
        client_id="ding-client",
        client_secret="secret-that-never-appears-in-errors",
        agent_id=123,
        timeout_seconds=3,
    )

    first_token, _ttl = client.access_token()
    second_token, _ttl = client.access_token()
    result = client.send_action_card(
        recipient_user_id="manager-userid",
        title="2026-07 薪资复核",
        markdown="请复核本门店厅面薪资结果。",
        action_url="https://pay.example.test/comp-appeals/1",
    )

    assert first_token == second_token == "provider-token"
    assert len(requests) == 2
    assert requests[0][0].full_url == "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    assert requests[1][0].full_url.startswith(
        "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?"
    )
    payload = json.loads(parse_qs(requests[1][0].data.decode("utf-8"))["msg"][0])
    assert payload["action_card"]["single_title"] == "查看并申诉"
    assert result.task_id == 42
    assert result.request_id == "req-1"


def test_action_card_accepts_purpose_specific_button(monkeypatch):
    captured: dict[str, object] = {}

    def fake_perform(request):
        captured["message"] = json.loads(parse_qs(request.data.decode("utf-8"))["msg"][0])
        return {"errcode": 0, "task_id": 7}

    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))
    monkeypatch.setattr(client, "_perform", fake_perform)

    client.send_action_card(
        recipient_user_id="ding-1",
        title="组织同步待确认",
        markdown="发现 3 项待应用变更，1 项冲突。",
        action_url="https://pay.example.test/org",
        action_title="查看组织同步",
    )

    assert captured["message"]["action_card"]["single_title"] == "查看组织同步"


@pytest.mark.parametrize(
    "action_url",
    [
        "http://pay.example.test/org",
        "https:///org",
        "https://pay.example.test:invalid/org",
        "https://user:password@pay.example.test/org",
        "https://pay.example.test/org?next=https://attacker.example",
        "https://pay.example.test/org#details",
    ],
)
def test_action_card_rejects_unsafe_action_urls_before_provider_use(action_url):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)

    with pytest.raises(DingTalkClientError, match="action URL"):
        client.send_action_card(
            recipient_user_id="manager-userid",
            title="组织同步待确认",
            markdown="发现 3 项待应用变更，1 项冲突。",
            action_url=action_url,
        )


def test_provider_http_errors_are_sanitized(monkeypatch):
    def fake_urlopen(request, *, timeout):
        raise HTTPError(request.full_url, 401, "rejected secret=value", {}, None)

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = DingTalkClient(
        client_id="ding-client",
        client_secret="highly-sensitive-value",
        agent_id=123,
    )

    with pytest.raises(DingTalkClientError) as caught:
        client.check_connection()

    message = str(caught.value)
    assert "401" in message
    assert "highly-sensitive-value" not in message
    assert "secret=value" not in message


def test_notification_timeout_is_reported_as_an_unknown_send_outcome(monkeypatch):
    def timeout_after_send(_request, *, timeout):
        del timeout
        raise TimeoutError

    monkeypatch.setattr(client_module, "urlopen", timeout_after_send)
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))

    with pytest.raises(client_module.DingTalkSendOutcomeUnknown):
        client.send_action_card(
            recipient_user_id="manager-userid",
            title="2026-07 薪资复核",
            markdown="请复核本门店厅面薪资结果。",
            action_url="https://pay.example.test/comp-appeals/1",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": 42},
        {"errcode": "0", "task_id": 42},
        {"errcode": False, "task_id": 42},
        {"errcode": 0, "task_id": True},
        {"errcode": 0, "task_id": 0},
    ],
)
def test_malformed_notification_response_is_an_unknown_outcome(monkeypatch, payload):
    monkeypatch.setattr(client_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))

    with pytest.raises(client_module.DingTalkSendOutcomeUnknown):
        client.send_action_card(
            recipient_user_id="manager-userid",
            title="2026-07 薪资复核",
            markdown="请复核本门店厅面薪资结果。",
            action_url="https://pay.example.test/comp-appeals/1",
        )


def test_client_reads_only_configured_root_subtrees(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    requests: list[tuple[str, int]] = []

    def fake_post(url: str, body: dict[str, object]) -> dict[str, object]:
        department_id = body["dept_id"]
        assert isinstance(department_id, int)
        requests.append((url, department_id))
        if url == client_module._DEPARTMENT_LIST_URL:
            children = {
                100: [{"dept_id": 110, "parent_id": 100, "name": "广州区"}],
                200: [{"dept_id": 210, "parent_id": 200, "name": "深圳区"}],
                110: [{"dept_id": 111, "parent_id": 110, "name": "天河店"}],
                210: [],
                111: [],
            }[department_id]
            return {"errcode": 0, "result": children}
        assert url == client_module._DEPARTMENT_USER_LIST_URL
        return {"errcode": 0, "result": {"list": [], "has_more": False}}

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    snapshot = client.list_organization_snapshot(root_department_ids=(100, 200))

    assert {department.department_id for department in snapshot.departments} == {110, 111, 210}
    assert {department.parent_id for department in snapshot.departments} >= {100, 200}
    assert 100 not in {department.department_id for department in snapshot.departments}
    assert 200 not in {department.department_id for department in snapshot.departments}
    assert {department_id for _url, department_id in requests} == {100, 110, 111, 200, 210}
    assert 1 not in {department_id for _url, department_id in requests}


def test_scoped_client_rejects_global_root_returned_as_a_descendant(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    requested_department_ids: list[int] = []

    def fake_post(url: str, body: dict[str, object]) -> dict[str, object]:
        department_id = body["dept_id"]
        assert isinstance(department_id, int)
        requested_department_ids.append(department_id)
        assert url == client_module._DEPARTMENT_LIST_URL
        return {
            "errcode": 0,
            "result": [{"dept_id": 1, "parent_id": 100, "name": "伪造全局根"}],
        }

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    with pytest.raises(DingTalkClientError, match="directory response"):
        client.list_organization_snapshot(root_department_ids=(100,))

    assert requested_department_ids == [100]


def test_explicit_global_root_remains_a_valid_boundary(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)

    def fake_post(url: str, _body: dict[str, object]) -> dict[str, object]:
        if url == client_module._DEPARTMENT_LIST_URL:
            return {"errcode": 0, "result": []}
        return {"errcode": 0, "result": {"list": [], "has_more": False}}

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    assert client.list_organization_snapshot(root_department_ids=(1,)).departments == ()


@pytest.mark.parametrize("root_id", [1, 100])
def test_client_rejects_configured_root_returned_as_its_own_child(monkeypatch, root_id):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)

    def fake_post(url: str, _body: dict[str, object]) -> dict[str, object]:
        assert url == client_module._DEPARTMENT_LIST_URL
        return {
            "errcode": 0,
            "result": [{"dept_id": root_id, "parent_id": root_id, "name": "重复根"}],
        }

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    with pytest.raises(DingTalkClientError, match="duplicate organization department"):
        client.list_organization_snapshot(root_department_ids=(root_id,))


@pytest.mark.parametrize("root_department_ids", [(), (0,), (True,), (100, 100)])
def test_client_rejects_invalid_configured_roots(monkeypatch, root_department_ids):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    called = False

    def unexpected_post(_url: str, _body: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(client, "_post_legacy_json", unexpected_post)

    with pytest.raises(ValueError):
        client.list_organization_snapshot(root_department_ids=root_department_ids)

    assert called is False


def test_client_counts_configured_roots_toward_department_limit(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    called = False

    def unexpected_post(_url: str, _body: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(client, "_post_legacy_json", unexpected_post)
    roots = tuple(range(1, client_module._MAX_DIRECTORY_DEPARTMENTS + 2))

    with pytest.raises(DingTalkClientError, match="safety limit"):
        client.list_organization_snapshot(root_department_ids=roots)

    assert called is False


def test_safe_read_retries_temporary_failures_three_total_times(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    transport_calls = 0
    sleep_delays: list[float] = []

    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))
    monkeypatch.setattr(client_module.random, "uniform", lambda _low, _high: 1.0)
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)

    def temporary_failure(_request):
        nonlocal transport_calls
        transport_calls += 1
        raise client_module._DingTalkTemporaryError("temporary")

    monkeypatch.setattr(client, "_perform", temporary_failure)

    with pytest.raises(client_module._DingTalkTemporaryError):
        client._post_legacy_json(client_module._DEPARTMENT_LIST_URL, {"dept_id": 100})

    assert transport_calls == 3
    assert sleep_delays == [0.25, 0.5]


def test_safe_read_does_not_retry_credential_errors(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    sleep_delays: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)

    def credential_failure():
        raise DingTalkClientError("invalid credentials")

    monkeypatch.setattr(client, "access_token", credential_failure)

    with pytest.raises(DingTalkClientError, match="invalid credentials"):
        client._post_legacy_json(client_module._DEPARTMENT_LIST_URL, {"dept_id": 100})

    assert sleep_delays == []


def test_safe_read_does_not_retry_provider_business_errors(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    transport_calls = 0
    sleep_delays: list[float] = []
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)

    def business_failure(_request):
        nonlocal transport_calls
        transport_calls += 1
        return {"errcode": 40035}

    monkeypatch.setattr(client, "_perform", business_failure)

    with pytest.raises(DingTalkClientError, match="code 40035"):
        client._post_legacy_json(client_module._DEPARTMENT_LIST_URL, {"dept_id": 100})

    assert transport_calls == 1
    assert sleep_delays == []


@pytest.mark.parametrize(
    "payload",
    [
        {"result": []},
        {"errcode": False, "result": []},
        {"errcode": 0.0, "result": []},
        {"errcode": "0", "result": []},
    ],
    ids=["missing", "boolean", "float", "string"],
)
def test_safe_read_rejects_non_integer_zero_errcodes_without_retry(monkeypatch, payload):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    transport_calls = 0
    sleep_delays: list[float] = []
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)

    def invalid_errcode(_request):
        nonlocal transport_calls
        transport_calls += 1
        return payload

    monkeypatch.setattr(client, "_perform", invalid_errcode)

    with pytest.raises(DingTalkClientError, match="code -1"):
        client._post_legacy_json(client_module._DEPARTMENT_LIST_URL, {"dept_id": 100})

    assert transport_calls == 1
    assert sleep_delays == []


def test_safe_read_does_not_retry_invalid_response_structures(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    transport_calls = 0
    sleep_delays: list[float] = []
    monkeypatch.setattr(client, "access_token", lambda: ("provider-token", 3600))
    monkeypatch.setattr(client_module.time, "sleep", sleep_delays.append)

    def malformed_response(_request):
        nonlocal transport_calls
        transport_calls += 1
        return {"errcode": 0, "result": {}}

    monkeypatch.setattr(client, "_perform", malformed_response)

    with pytest.raises(DingTalkClientError, match="invalid directory response"):
        client.list_organization_snapshot(root_department_ids=(100,))

    assert transport_calls == 1
    assert sleep_delays == []


class _SequentialRecordingExecutor:
    def __init__(self) -> None:
        self.map_result_sizes: list[tuple[int, ...]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def map(self, function, values):
        results = tuple(function(value) for value in values)
        self.map_result_sizes.append(tuple(len(result) for result in results))
        return iter(results)


def test_department_user_pages_are_deduplicated_before_executor_handoff(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    executor = _SequentialRecordingExecutor()
    repeated_user = {
        "userid": "same-user",
        "name": "同一员工",
        "job_number": "E001",
        "title": "店长",
        "active": True,
    }

    def fake_post(url: str, body: dict[str, object]) -> dict[str, object]:
        if url == client_module._DEPARTMENT_LIST_URL:
            return {"errcode": 0, "result": []}
        cursor = body["cursor"]
        return {
            "errcode": 0,
            "result": {
                "list": [repeated_user] * client_module._DIRECTORY_PAGE_SIZE,
                "has_more": cursor == 0,
                **({"next_cursor": 1} if cursor == 0 else {}),
            },
        }

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)
    monkeypatch.setattr(
        client_module,
        "ThreadPoolExecutor",
        lambda **_kwargs: executor,
    )

    snapshot = client.list_organization_snapshot(root_department_ids=(100,))

    assert [user.user_id for user in snapshot.users] == ["same-user"]
    assert executor.map_result_sizes[-1] == (1,)


def test_department_user_page_over_provider_size_fails_closed(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)
    raw_user = {"userid": "same-user", "name": "员工", "active": True}

    def fake_post(url: str, _body: dict[str, object]) -> dict[str, object]:
        if url == client_module._DEPARTMENT_LIST_URL:
            return {"errcode": 0, "result": []}
        return {
            "errcode": 0,
            "result": {
                "list": [raw_user] * (client_module._DIRECTORY_PAGE_SIZE + 1),
                "has_more": False,
            },
        }

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    with pytest.raises(DingTalkClientError, match="invalid directory response"):
        client.list_organization_snapshot(root_department_ids=(100,))


def test_conflicting_duplicate_user_across_pages_fails_closed(monkeypatch):
    client = DingTalkClient(client_id="ding-client", client_secret="secret", agent_id=123)

    def fake_post(url: str, body: dict[str, object]) -> dict[str, object]:
        if url == client_module._DEPARTMENT_LIST_URL:
            return {"errcode": 0, "result": []}
        cursor = body["cursor"]
        return {
            "errcode": 0,
            "result": {
                "list": [
                    {
                        "userid": "same-user",
                        "name": "员工甲" if cursor == 0 else "员工乙",
                        "active": True,
                    }
                ],
                "has_more": cursor == 0,
                **({"next_cursor": 1} if cursor == 0 else {}),
            },
        }

    monkeypatch.setattr(client, "_post_legacy_json", fake_post)

    with pytest.raises(DingTalkClientError, match="inconsistent organization users"):
        client.list_organization_snapshot(root_department_ids=(100,))
