"""Regression coverage for the trusted-proxy deployment contract."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

_TRUSTED_FRONTEND_IP = "172.30.0.10"


def _client_ip_app() -> ProxyHeadersMiddleware:
    app = FastAPI()

    @app.get("/")
    def client_ip(request: Request) -> dict[str, str]:
        assert request.client is not None
        return {"ip": request.client.host}

    return ProxyHeadersMiddleware(app, trusted_hosts=_TRUSTED_FRONTEND_IP)


def test_only_the_frontend_proxy_can_supply_a_forwarded_client_ip() -> None:
    app = _client_ip_app()
    forwarded = {"X-Forwarded-For": "203.0.113.7"}

    with TestClient(app, client=(_TRUSTED_FRONTEND_IP, 50000)) as trusted_client:
        assert trusted_client.get("/", headers=forwarded).json() == {"ip": "203.0.113.7"}

    with TestClient(app, client=("127.0.0.1", 50000)) as direct_client:
        assert direct_client.get("/", headers=forwarded).json() == {"ip": "127.0.0.1"}


def test_deployment_config_keeps_the_proxy_trust_boundary_aligned() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    dockerfile = (project_dir / "backend" / "Dockerfile").read_text(encoding="utf-8")
    compose = (project_dir / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (project_dir / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert f'"--forwarded-allow-ips", "{_TRUSTED_FRONTEND_IP}"' in dockerfile
    assert '"--forwarded-allow-ips", "*"' not in dockerfile
    assert f"ipv4_address: {_TRUSTED_FRONTEND_IP}" in compose
    assert "ipv4_address: 172.30.0.11" in compose
    assert 'profiles: ["production"]' in compose
    assert "set_real_ip_from 172.30.0.11;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx


def test_restore_script_recreates_schema_and_requires_a_checksum() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    restore = (project_dir / "deploy" / "restore.ps1").read_text(encoding="utf-8")
    backup = (project_dir / "deploy" / "backup.ps1").read_text(encoding="utf-8")

    assert "Backup checksum is required" in restore
    assert "[string]$EmergencyBackupPath" in restore
    assert "Get-FileHash -LiteralPath $resolvedBackup -Algorithm SHA256" in restore
    assert "pg_restore --list" in restore
    assert restore.index("pg_restore --list") < restore.index("backup.ps1")
    assert "DROP SCHEMA public CASCADE;" in restore
    assert "CREATE SCHEMA public;" in restore
    assert "pg_restore --exit-on-error --no-owner" in restore
    assert "ps --status running -q backend | Out-String" in restore
    assert "ps -q postgres | Out-String" in restore
    assert "printenv $Name" in restore
    assert "docker exec -i $postgresId psql" in restore
    assert "DROP SCHEMA public CASCADE;" in restore
    assert '"DROP SCHEMA public CASCADE; CREATE SCHEMA public;' not in restore
    assert "sh -c" not in restore
    assert "sh -c" not in backup
    assert "Set-Content -LiteralPath $checksumPath -Encoding ascii -NoNewline" in backup


def test_supply_chain_inputs_are_pinned_and_hash_verified() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    backend_dir = project_dir / "backend"
    dockerfile = (backend_dir / "Dockerfile").read_text(encoding="utf-8")
    frontend_dockerfile = (project_dir / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    compose = (project_dir / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (project_dir / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    build_lock = (backend_dir / "requirements-build.lock").read_text(encoding="utf-8")
    runtime_lock = (backend_dir / "requirements.lock").read_text(encoding="utf-8")
    dev_lock = (backend_dir / "requirements-dev.lock").read_text(encoding="utf-8")

    assert "requirements-build.lock" in dockerfile
    assert dockerfile.count("--require-hashes") == 2
    assert "--no-build-isolation" in dockerfile
    assert "@sha256:" in dockerfile
    assert "@sha256:" in frontend_dockerfile
    assert "postgres:16@sha256:" in compose
    assert "caddy:2.8-alpine@sha256:" in compose
    for lock in (build_lock, runtime_lock, dev_lock):
        assert "--hash=sha256:" in lock
    assert "setuptools==83.0.0" in build_lock

    action_refs = re.findall(r"uses: actions/[^@\s]+@([^\s#]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_ci_validates_the_production_compose_profile() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    workflow = (project_dir / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Validate deployment configuration" in workflow
    assert (
        "docker compose -f deploy/docker-compose.yml --profile production config --quiet"
        in workflow
    )
    for name in (
        "POSTGRES_PASSWORD",
        "COMP_SECRET_KEY",
        "COMP_ENCRYPTION_KEY",
        "COMP_PUBLIC_HOST",
        "COMP_ACME_EMAIL",
    ):
        assert name in workflow

    validation_step = workflow.split("- name: Validate deployment configuration", 1)[1].split(
        "- name: Build backend image", 1
    )[0]
    assert 'COMP_COOKIE_SECURE: "true"' in validation_step
    assert "Validate Caddyfile" in workflow
    assert "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" in workflow


def test_loopback_service_ports_can_be_overridden_for_an_isolated_e2e_stack() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    compose = (project_dir / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "${COMP_POSTGRES_PORT:-5432}:5432" in compose
    assert "${COMP_BACKEND_PORT:-8000}:8000" in compose
    assert "${COMP_FRONTEND_PORT:-8080}:80" in compose
