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
