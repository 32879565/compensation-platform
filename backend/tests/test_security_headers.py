"""Static release checks for browser-facing security headers."""

from pathlib import Path


def test_frontend_proxy_and_tls_gateway_declare_a_restrictive_csp() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    nginx = (project_dir / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    caddy = (project_dir / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    for config in (nginx, caddy):
        assert "Content-Security-Policy" in config
        assert "default-src 'self'" in config
        assert "base-uri 'self'" in config
        assert "object-src 'none'" in config
        assert "frame-ancestors 'none'" in config
        assert "form-action 'self'" in config

    # HSTS is intentionally emitted only by the TLS-terminating gateway, not
    # by the internal HTTP-only frontend container.
    assert "Strict-Transport-Security" in caddy
