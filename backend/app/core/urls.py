"""Validation helpers for user-supplied evidence links."""

from __future__ import annotations

from urllib.parse import urlsplit


def require_http_url(value: str) -> str:
    """Return a normalized credential-free HTTPS URL or raise ``ValueError``.

    Evidence links are rendered as browser links. Restricting transport and
    embedded credentials at the write boundary blocks common stored-link and
    misleading-host attacks across every API that records audit evidence.
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError("evidence URL must not be blank")
    if "\\" in normalized or any(character.isspace() for character in normalized):
        raise ValueError("evidence URL must be an absolute HTTPS URL without credentials")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("evidence URL must be an absolute HTTPS URL without credentials")
    return normalized


def optional_http_url(value: object) -> object:
    """Pydantic ``mode='before'`` helper for nullable evidence URL fields."""

    if not isinstance(value, str):
        return value
    if not value.strip():
        return None
    return require_http_url(value)
