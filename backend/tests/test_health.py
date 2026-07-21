from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.main import app, create_app

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_api_prefix() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_api_health_returns_e2e_marker_only_when_explicitly_configured(monkeypatch) -> None:
    marker = "compensation-e2e-disposable-target"
    monkeypatch.setenv("COMP_E2E_TARGET_MARKER", marker)
    get_settings.cache_clear()
    try:
        configured_client = TestClient(create_app())
        response = configured_client.get("/api/health")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "e2e_target_marker": marker}
    assert configured_client.get("/health").json() == {"status": "ok"}


def test_readiness_checks_the_database(pg_engine, monkeypatch) -> None:
    import app.main as main_module

    monkeypatch.setattr(main_module, "engine", pg_engine)

    resp = client.get("/health/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readiness_hides_database_failure_details(monkeypatch) -> None:
    import app.main as main_module

    class UnavailableEngine:
        def connect(self):
            raise SQLAlchemyError("postgres://sensitive-host:5432/compensation")

    monkeypatch.setattr(main_module, "engine", UnavailableEngine())

    resp = client.get("/health/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "Database is unavailable."}


def test_docs_disabled_when_not_debug() -> None:
    # debug=False（默认）时 OpenAPI 文档必须不可达，防止生产接口清单泄露
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404
