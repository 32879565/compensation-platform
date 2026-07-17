from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_api_prefix() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_docs_disabled_when_not_debug() -> None:
    # debug=False（默认）时 OpenAPI 文档必须不可达，防止生产接口清单泄露
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404
