import asyncio
import errno
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from app.core.metrics import RequestMetrics, RequestMetricsMiddleware, render_org_sync_metrics
from app.main import create_app
from app.models.audit import AuditLog
from app.models.dingtalk import DingTalkOrgSyncBatch


async def _receive() -> Message:
    return {"type": "http.disconnect"}


def _metrics_scope(
    route_template: str = "/exports/{export_id}", asgi_spec_version: str = "2.4"
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": asgi_spec_version},
        "method": "GET",
        "route": SimpleNamespace(path=route_template),
    }


def _metric_line(metric: str, rendered: str) -> str:
    return next(line for line in rendered.splitlines() if line.startswith(metric))


def _metric_value(metric: str, rendered: str) -> float:
    return float(_metric_line(metric, rendered).split()[-1])


def _org_batch(
    *,
    public_id: str,
    checked_at: datetime,
    created_at: datetime,
    counts: tuple[int, int, int, int, int, int],
) -> DingTalkOrgSyncBatch:
    (
        ready_regions,
        ready_stores,
        ready_reviewers,
        region_conflicts,
        store_conflicts,
        reviewer_conflicts,
    ) = counts
    return DingTalkOrgSyncBatch(
        public_id=public_id,
        snapshot_hash=public_id[0] * 64,
        root_config_hash=public_id[1] * 64,
        local_baseline_hash=public_id[2] * 64,
        expires_at=checked_at + timedelta(hours=1),
        created_at=created_at,
        updated_at=created_at,
        last_checked_at=checked_at,
        ready_region_count=ready_regions,
        ready_store_count=ready_stores,
        ready_reviewer_count=ready_reviewers,
        region_conflict_count=region_conflicts,
        store_conflict_count=store_conflicts,
        reviewer_conflict_count=reviewer_conflicts,
    )


@pytest.mark.usefixtures("pg_engine")
def test_org_metrics_use_audit_max_and_latest_checked_batch(db_session) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            AuditLog(
                action="dingtalk.organization.schedule.succeeded",
                result="SUCCESS",
                ts=now - timedelta(seconds=10),
            ),
            # Inserted later but older: max(ts), not row id, must win.
            AuditLog(
                action="dingtalk.organization.schedule.succeeded",
                result="SUCCESS",
                ts=now - timedelta(seconds=30),
            ),
            AuditLog(
                action="dingtalk.organization.schedule.failed",
                result="FAILURE",
                ts=now - timedelta(seconds=20),
            ),
            _org_batch(
                public_id="abc" + "0" * 29,
                checked_at=now - timedelta(minutes=5),
                created_at=now,
                counts=(90, 90, 90, 90, 90, 90),
            ),
            _org_batch(
                public_id="def" + "1" * 29,
                checked_at=now - timedelta(minutes=1),
                created_at=now - timedelta(days=1),
                counts=(1, 2, 3, 4, 5, 6),
            ),
            # Tied last_checked_at: highest id must be selected.
            _org_batch(
                public_id="ghi" + "2" * 29,
                checked_at=now - timedelta(minutes=1),
                created_at=now - timedelta(days=2),
                counts=(7, 8, 9, 10, 11, 12),
            ),
        ]
    )
    db_session.commit()

    rendered = render_org_sync_metrics(db_session, now=now)

    assert (
        _metric_value("compensation_dingtalk_org_sync_last_success_timestamp_seconds", rendered)
        == (now - timedelta(seconds=10)).timestamp()
    )
    assert (
        _metric_value("compensation_dingtalk_org_sync_last_failure_timestamp_seconds", rendered)
        == (now - timedelta(seconds=20)).timestamp()
    )
    assert _metric_value("compensation_dingtalk_org_sync_ready_changes", rendered) == 24
    assert _metric_value("compensation_dingtalk_org_sync_conflicts", rendered) == 33
    assert _metric_value("compensation_dingtalk_org_sync_stale_seconds", rendered) == 10
    assert "{" not in rendered
    assert "abc" not in rendered
    assert "ghi" not in rendered


@pytest.mark.usefixtures("pg_engine")
def test_org_metrics_empty_database_is_numeric_and_never_run_is_stale(db_session) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    rendered = render_org_sync_metrics(db_session, now=now)

    assert (
        _metric_value("compensation_dingtalk_org_sync_last_success_timestamp_seconds", rendered)
        == 0
    )
    assert (
        _metric_value("compensation_dingtalk_org_sync_last_failure_timestamp_seconds", rendered)
        == 0
    )
    assert _metric_value("compensation_dingtalk_org_sync_ready_changes", rendered) == 0
    assert _metric_value("compensation_dingtalk_org_sync_conflicts", rendered) == 0
    assert (
        _metric_value("compensation_dingtalk_org_sync_stale_seconds", rendered) == now.timestamp()
    )


@pytest.mark.usefixtures("pg_engine")
def test_org_metrics_clamp_future_success_staleness_to_zero(db_session) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    db_session.add(
        AuditLog(
            action="dingtalk.organization.schedule.succeeded",
            result="SUCCESS",
            ts=now + timedelta(minutes=1),
        )
    )
    db_session.commit()

    rendered = render_org_sync_metrics(db_session, now=now)

    assert _metric_value("compensation_dingtalk_org_sync_stale_seconds", rendered) == 0


def test_metrics_endpoint_omits_org_series_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    def unavailable_session() -> object:
        raise SQLAlchemyError("private database topology")

    monkeypatch.setattr(main_module, "SessionLocal", unavailable_session)
    app = create_app()

    @app.get("/metrics-db-probe")
    def metrics_db_probe() -> Response:
        return Response(status_code=200)

    client = TestClient(app)
    assert client.get("/metrics-db-probe").status_code == 200
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "compensation_http_requests_total" in response.text
    assert 'route="/metrics-db-probe"' in response.text
    assert "compensation_dingtalk_org_sync_" not in response.text
    assert "private database topology" not in response.text


def test_metrics_endpoint_discards_org_series_when_session_exit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    class ExitFailureSession:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            raise SQLAlchemyError("private close failure")

    monkeypatch.setattr(main_module, "SessionLocal", ExitFailureSession)
    monkeypatch.setattr(
        main_module,
        "render_org_sync_metrics",
        lambda *args, **kwargs: "compensation_dingtalk_org_sync_ready_changes 42\n",
    )
    app = create_app()

    @app.get("/metrics-exit-probe")
    def metrics_exit_probe() -> Response:
        return Response(status_code=200)

    client = TestClient(app)
    assert client.get("/metrics-exit-probe").status_code == 200
    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'route="/metrics-exit-probe"' in response.text
    assert "compensation_dingtalk_org_sync_" not in response.text
    assert "private close failure" not in response.text


def test_metrics_render_deterministic_aggregate_series() -> None:
    metrics = RequestMetrics()
    metrics.record(
        method="GET",
        route_template="/api/employees/{employee_id}",
        status_code=200,
        duration_seconds=0.125,
    )
    metrics.record(
        method="GET",
        route_template="/api/employees/{employee_id}",
        status_code=200,
        duration_seconds=0.375,
    )
    metrics.record(
        method="POST",
        route_template="/api/payroll",
        status_code=503,
        duration_seconds=0.25,
    )

    employee_labels = 'method="GET",route="/api/employees/{employee_id}",status="200"'
    payroll_labels = 'method="POST",route="/api/payroll",status="503"'
    expected_lines = [
        "# HELP compensation_http_requests_total "
        "Total HTTP requests by method, route template, and status.",
        "# TYPE compensation_http_requests_total counter",
        f"compensation_http_requests_total{{{employee_labels}}} 2",
        f"compensation_http_requests_total{{{payroll_labels}}} 1",
        "# HELP compensation_http_request_duration_seconds "
        "Request duration by method, route template, and status.",
        "# TYPE compensation_http_request_duration_seconds summary",
        f"compensation_http_request_duration_seconds_sum{{{employee_labels}}} 0.500000",
        f"compensation_http_request_duration_seconds_count{{{employee_labels}}} 2",
        f"compensation_http_request_duration_seconds_sum{{{payroll_labels}}} 0.250000",
        f"compensation_http_request_duration_seconds_count{{{payroll_labels}}} 1",
        "# HELP compensation_http_requests_5xx_total Total HTTP responses with a 5xx status.",
        "# TYPE compensation_http_requests_5xx_total counter",
        "compensation_http_requests_5xx_total 1",
        "# HELP compensation_http_request_failures_total "
        "Total requests with a failed ASGI execution or incomplete response.",
        "# TYPE compensation_http_request_failures_total counter",
        "compensation_http_request_failures_total 0",
    ]

    assert metrics.render_prometheus() == "\n".join(expected_lines) + "\n"


def test_metrics_endpoint_uses_templates_and_excludes_raw_request_values() -> None:
    app = create_app()

    @app.get("/metrics-probe/{record_id}")
    def metrics_probe(record_id: str) -> Response:
        return Response(status_code=503)

    client = TestClient(app)
    private_value = "salary-9988?employee=alice@example.test"
    assert client.get(f"/metrics-probe/{private_value}").status_code == 503
    assert client.get(f"/not-found/private-{private_value}").status_code == 404

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'route="/metrics-probe/{record_id}"' in response.text
    assert 'route="/unmatched"' in response.text
    assert "compensation_http_requests_5xx_total 1" in response.text
    assert "salary-9988" not in response.text
    assert "alice@example.test" not in response.text
    assert client.get("/api/metrics").status_code == 404


def test_metrics_marks_broken_stream_after_success_headers_as_failed() -> None:
    metrics = RequestMetrics()
    messages: list[Message] = []

    async def broken_chunks():
        yield b"partial"
        raise RuntimeError("stream transport interrupted")

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(broken_chunks(), media_type="text/plain")
        await response(scope, receive, send)

    async def capture_send(message: Message) -> None:
        messages.append(message)

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(RuntimeError, match="stream transport interrupted"):
        asyncio.run(middleware(_metrics_scope(), _receive, capture_send))

    rendered = metrics.render_prometheus()
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    assert messages[-1]["type"] == "http.response.body"
    assert messages[-1]["more_body"] is True
    assert (
        'compensation_http_requests_total{method="GET",route="/exports/{export_id}",status="200"} 1'
        in rendered
    )
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 1" in rendered


def test_metrics_marks_background_failure_after_complete_response_as_failed() -> None:
    metrics = RequestMetrics()

    async def complete_chunks():
        yield b"complete"

    async def fail_background_task() -> None:
        raise RuntimeError("background export cleanup failed")

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(
            complete_chunks(),
            media_type="text/plain",
            background=BackgroundTask(fail_background_task),
        )
        await response(scope, receive, send)

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(RuntimeError, match="background export cleanup failed"):
        asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    rendered = metrics.render_prometheus()
    assert (
        'compensation_http_requests_total{method="GET",route="/exports/{export_id}",status="200"} 1'
        in rendered
    )
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 1" in rendered


def test_metrics_marks_returned_partial_asgi_response_as_failed() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    rendered = metrics.render_prometheus()
    assert "compensation_http_request_failures_total 1" in rendered


def test_metrics_excludes_streaming_client_disconnect_from_failure_counter() -> None:
    metrics = RequestMetrics()
    messages: list[Message] = []
    partial_body_sent = asyncio.Event()
    stream_cancelled = asyncio.Event()

    async def chunks():
        try:
            yield b"partial"
            await asyncio.Event().wait()
        finally:
            stream_cancelled.set()

    async def receive_disconnect_after_partial_body() -> Message:
        await partial_body_sent.wait()
        return {"type": "http.disconnect"}

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(chunks(), media_type="text/plain")
        await response(scope, receive, send)

    async def capture_send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            partial_body_sent.set()

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(
        middleware(
            _metrics_scope(asgi_spec_version="2.3"),
            receive_disconnect_after_partial_body,
            capture_send,
        )
    )

    rendered = metrics.render_prometheus()
    assert messages[0]["type"] == "http.response.start"
    assert messages[-1]["type"] == "http.response.body"
    assert messages[-1]["more_body"] is True
    assert stream_cancelled.is_set()
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 0" in rendered


@pytest.mark.parametrize(
    "closed_errno",
    [errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ENOTCONN],
)
def test_metrics_excludes_closed_client_send_from_failure_counter(closed_errno: int) -> None:
    metrics = RequestMetrics()
    messages: list[Message] = []

    async def chunks():
        yield b"partial"

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(chunks(), media_type="text/plain")
        await response(scope, receive, send)

    async def closed_client_send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body":
            raise OSError(closed_errno, "client closed stream")

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(ClientDisconnect):
        asyncio.run(middleware(_metrics_scope(), _receive, closed_client_send))

    rendered = metrics.render_prometheus()
    assert messages[0]["type"] == "http.response.start"
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 0" in rendered


@pytest.mark.parametrize("error_type", [OSError, BrokenPipeError])
def test_metrics_records_non_disconnect_send_oserror_as_failure(error_type: type[OSError]) -> None:
    metrics = RequestMetrics()

    async def chunks():
        yield b"partial"

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(chunks(), media_type="text/plain")
        await response(scope, receive, send)

    async def failing_send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise error_type(errno.EIO, "storage write failed")

    middleware = RequestMetricsMiddleware(application, metrics)

    # Starlette wraps every OSError from StreamingResponse as ClientDisconnect.
    with pytest.raises(ClientDisconnect):
        asyncio.run(middleware(_metrics_scope(), _receive, failing_send))

    rendered = metrics.render_prometheus()
    assert (
        'compensation_http_requests_total{method="GET",route="/exports/{export_id}",status="200"} 1'
        in rendered
    )
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 1" in rendered


def test_metrics_omits_http_series_for_closed_client_before_response_start() -> None:
    metrics = RequestMetrics()

    async def chunks():
        yield b"partial"

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        response = StreamingResponse(chunks(), media_type="text/plain")
        await response(scope, receive, send)

    async def closed_client_send(message: Message) -> None:
        if message["type"] == "http.response.start":
            raise OSError(errno.EPIPE, "client closed before headers")

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(ClientDisconnect):
        asyncio.run(middleware(_metrics_scope(), _receive, closed_client_send))

    rendered = metrics.render_prometheus()
    assert "compensation_http_requests_total{" not in rendered
    assert "compensation_http_request_duration_seconds_sum{" not in rendered
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 0" in rendered


def test_metrics_omits_http_series_for_disconnect_before_response_start() -> None:
    metrics = RequestMetrics()

    async def receive_disconnect() -> Message:
        return {"type": "http.disconnect"}

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(middleware(_metrics_scope(), receive_disconnect, discard_send))

    rendered = metrics.render_prometheus()
    assert "compensation_http_requests_total{" not in rendered
    assert "compensation_http_request_duration_seconds_sum{" not in rendered
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 0" in rendered


def test_metrics_records_pre_response_server_failure_without_http_series() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("application failed before response")

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(RuntimeError, match="application failed before response"):
        asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    rendered = metrics.render_prometheus()
    assert "compensation_http_requests_total{" not in rendered
    assert "compensation_http_request_duration_seconds_sum{" not in rendered
    assert "compensation_http_requests_5xx_total 0" in rendered
    assert "compensation_http_request_failures_total 1" in rendered


def test_metrics_waits_for_final_response_trailer() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"complete", "more_body": False})
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": True})
        await send({"type": "http.response.trailers", "headers": []})

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    assert "compensation_http_request_failures_total 0" in metrics.render_prometheus()


def test_metrics_marks_missing_final_response_trailer_as_failed() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"complete", "more_body": False})
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": True})

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    assert "compensation_http_request_failures_total 1" in metrics.render_prometheus()


def test_metrics_keeps_real_application_error_after_client_disconnect() -> None:
    metrics = RequestMetrics()

    async def receive_disconnect() -> Message:
        return {"type": "http.disconnect"}

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        raise RuntimeError("background work failed after client disconnected")

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(RuntimeError, match="background work failed"):
        asyncio.run(middleware(_metrics_scope(), receive_disconnect, discard_send))

    assert "compensation_http_request_failures_total 1" in metrics.render_prometheus()


def test_metrics_excludes_metrics_scrape_from_failure_counter() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)
    asyncio.run(middleware(_metrics_scope("/metrics"), _receive, discard_send))

    rendered = metrics.render_prometheus()
    assert 'route="/metrics"' not in rendered
    assert "compensation_http_request_failures_total 0" in rendered


def test_metrics_excludes_normal_cancellation_from_failure_counter() -> None:
    metrics = RequestMetrics()

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError()

    async def discard_send(message: Message) -> None:
        del message

    middleware = RequestMetricsMiddleware(application, metrics)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(_metrics_scope(), _receive, discard_send))

    rendered = metrics.render_prometheus()
    assert _metric_line("compensation_http_request_failures_total", rendered) == (
        "compensation_http_request_failures_total 0"
    )
