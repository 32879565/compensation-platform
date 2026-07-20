"""把客户端真实 IP 放入请求上下文，供审计使用。

uvicorn --proxy-headers 已把 request.client.host 还原为真实客户端 IP
（Dockerfile CMD 已配置），此处直接取用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.audit.context import set_client_ip


class ClientIpMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        set_client_ip(request.client.host if request.client else None)
        return await call_next(request)
