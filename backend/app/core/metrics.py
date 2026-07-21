"""Low-cardinality, Prometheus-compatible request metrics.

This module deliberately records only route templates selected by the router.
It never uses a request path, query string, client address, user, or response
body as a metric label.
"""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass
from errno import ECONNABORTED, ECONNRESET, ENOTCONN, EPIPE
from math import isfinite
from threading import Lock
from time import perf_counter

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ALLOWED_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_METRICS_ROUTE = "/metrics"
_UNMATCHED_ROUTE = "/unmatched"
_CLOSED_CLIENT_SEND_ERRNOS = frozenset({ECONNABORTED, ECONNRESET, ENOTCONN, EPIPE})


@dataclass
class _Series:
    count: int = 0
    duration_seconds: float = 0.0


class RequestMetrics:
    """Keep process-local, aggregate request measurements safe for scraping."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._series: dict[tuple[str, str, int], _Series] = {}
        self._server_errors = 0
        self._failed_requests = 0

    def record(
        self,
        *,
        method: str,
        route_template: str,
        status_code: int | None,
        duration_seconds: float,
        request_failed: bool = False,
    ) -> None:
        """Record a request using a router-selected route template.

        ``request_failed`` captures ASGI execution failures and incomplete
        responses without changing the HTTP status that was actually sent. A
        request without a successfully sent response start has no status series.
        """
        with self._lock:
            if status_code is not None:
                normalized_status = _normalize_status(status_code)
                normalized_duration = _normalize_duration(duration_seconds)
                key = (_normalize_method(method), route_template, normalized_status)
                series = self._series.setdefault(key, _Series())
                series.count += 1
                series.duration_seconds += normalized_duration
                if 500 <= normalized_status <= 599:
                    self._server_errors += 1
            if request_failed:
                self._failed_requests += 1

    def render_prometheus(self) -> str:
        """Render a deterministic Prometheus text exposition snapshot."""
        snapshot, server_errors, failed_requests = self._snapshot()
        lines = [
            "# HELP compensation_http_requests_total "
            "Total HTTP requests by method, route template, and status.",
            "# TYPE compensation_http_requests_total counter",
        ]
        for method, route, status, count, _ in snapshot:
            lines.append(
                "compensation_http_requests_total"
                f"{{{_format_labels(method, route, status)}}} {count}"
            )

        lines.extend(
            [
                "# HELP compensation_http_request_duration_seconds "
                "Request duration by method, route template, and status.",
                "# TYPE compensation_http_request_duration_seconds summary",
            ]
        )
        for method, route, status, count, duration_seconds in snapshot:
            labels = _format_labels(method, route, status)
            lines.append(
                "compensation_http_request_duration_seconds_sum"
                f"{{{labels}}} {duration_seconds:.6f}"
            )
            lines.append(f"compensation_http_request_duration_seconds_count{{{labels}}} {count}")

        lines.extend(
            [
                "# HELP compensation_http_requests_5xx_total "
                "Total HTTP responses with a 5xx status.",
                "# TYPE compensation_http_requests_5xx_total counter",
                f"compensation_http_requests_5xx_total {server_errors}",
                "# HELP compensation_http_request_failures_total "
                "Total requests with a failed ASGI execution or incomplete response.",
                "# TYPE compensation_http_request_failures_total counter",
                f"compensation_http_request_failures_total {failed_requests}",
            ]
        )
        return "\n".join(lines) + "\n"

    def _snapshot(self) -> tuple[list[tuple[str, str, int, int, float]], int, int]:
        with self._lock:
            series = [
                (method, route, status, values.count, values.duration_seconds)
                for (method, route, status), values in self._series.items()
            ]
            return sorted(series), self._server_errors, self._failed_requests


class RequestMetricsMiddleware:
    """Capture aggregate request measurements without retaining request details."""

    def __init__(self, app: ASGIApp, metrics: RequestMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = perf_counter()
        status_code: int | None = None
        response_started = False
        response_body_complete = False
        response_complete = False
        response_has_trailers = False
        app_failed = False
        cancelled = False
        client_disconnected = False
        closed_client_send = False
        disconnect_exception = False

        async def receive_with_disconnect() -> Message:
            nonlocal client_disconnected

            message = await receive()
            if message["type"] == "http.disconnect":
                client_disconnected = True
            return message

        async def send_with_status(message: Message) -> None:
            nonlocal closed_client_send
            nonlocal response_body_complete
            nonlocal response_complete
            nonlocal response_has_trailers
            nonlocal response_started
            nonlocal status_code

            message_type = message["type"]
            try:
                await send(message)
            except OSError as exc:
                # Only a closed peer while sending is normal client lifecycle.
                # Other OSErrors must remain application failures.
                if _is_closed_client_send_error(exc):
                    closed_client_send = True
                raise

            if message_type == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_has_trailers = bool(message.get("trailers", False))
            elif message_type == "http.response.body" and not message.get("more_body", False):
                response_body_complete = True
                response_complete = not response_has_trailers
            elif (
                message_type == "http.response.trailers"
                and response_has_trailers
                and response_body_complete
                and not message.get("more_trailers", False)
            ):
                response_complete = True

        try:
            await self.app(scope, receive_with_disconnect, send_with_status)
        except CancelledError:
            # Cancellation is expected during normal shutdown and client lifecycle.
            cancelled = True
            raise
        except BaseException as exc:
            app_failed = True
            disconnect_exception = (
                isinstance(exc, ClientDisconnect) and (client_disconnected or closed_client_send)
            ) or (
                closed_client_send
                and isinstance(exc, OSError)
                and _is_closed_client_send_error(exc)
            )
            raise
        finally:
            route_template = normalized_route_template(scope)
            # Scrapes should not contribute to application traffic metrics.
            if route_template != _METRICS_ROUTE:
                self.metrics.record(
                    method=scope.get("method", "OTHER"),
                    route_template=route_template,
                    status_code=status_code,
                    duration_seconds=perf_counter() - started_at,
                    request_failed=_request_failed(
                        app_failed=app_failed,
                        cancelled=cancelled,
                        client_disconnected=client_disconnected,
                        closed_client_send=closed_client_send,
                        disconnect_exception=disconnect_exception,
                        response_complete=response_complete,
                        response_started=response_started,
                    ),
                )


def normalized_route_template(scope: Scope) -> str:
    """Return the matched static route template, never the raw request path."""
    route_path = getattr(scope.get("route"), "path", None)
    if not isinstance(route_path, str) or not route_path.startswith("/"):
        return _UNMATCHED_ROUTE
    return route_path


def _request_failed(
    *,
    app_failed: bool,
    cancelled: bool,
    client_disconnected: bool,
    closed_client_send: bool,
    disconnect_exception: bool,
    response_complete: bool,
    response_started: bool,
) -> bool:
    """Return whether this was a server-side execution or response failure."""
    if cancelled:
        return False
    if app_failed:
        return not disconnect_exception
    if client_disconnected or closed_client_send:
        return False
    return not response_started or not response_complete


def _is_closed_client_send_error(error: OSError) -> bool:
    """Recognize only OS errors that specifically mean the client closed its peer."""
    return error.errno is not None and error.errno in _CLOSED_CLIENT_SEND_ERRNOS


def _normalize_method(method: str) -> str:
    normalized_method = method.upper()
    return normalized_method if normalized_method in _ALLOWED_METHODS else "OTHER"


def _normalize_status(status_code: int) -> int:
    return status_code if 100 <= status_code <= 599 else 500


def _normalize_duration(duration_seconds: float) -> float:
    return duration_seconds if isfinite(duration_seconds) and duration_seconds >= 0 else 0.0


def _format_labels(method: str, route: str, status: int) -> str:
    return f'method="{method}",route="{_escape_label_value(route)}",status="{status}"'


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
