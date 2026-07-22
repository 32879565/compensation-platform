from __future__ import annotations

import json
from urllib.error import HTTPError

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
        action_url="https://pay.example.test/comp-appeals?delivery=1",
    )

    assert first_token == second_token == "provider-token"
    assert len(requests) == 2
    assert requests[0][0].full_url == "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    assert requests[1][0].full_url.startswith(
        "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?"
    )
    assert result.task_id == 42
    assert result.request_id == "req-1"


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
            action_url="https://pay.example.test/comp-appeals?delivery=1",
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
            action_url="https://pay.example.test/comp-appeals?delivery=1",
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
