"""请求级上下文（客户端 IP、actor），供审计在深层调用无需层层透传。"""

from __future__ import annotations

from contextvars import ContextVar

_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)
_actor: ContextVar[tuple[int, str] | None] = ContextVar("actor", default=None)


def set_client_ip(ip: str | None) -> None:
    _client_ip.set(ip)


def get_client_ip() -> str | None:
    return _client_ip.get()


def set_actor(user_id: int, username: str) -> None:
    _actor.set((user_id, username))


def get_actor() -> tuple[int, str] | None:
    return _actor.get()
