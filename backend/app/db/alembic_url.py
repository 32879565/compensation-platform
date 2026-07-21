"""Small adapter between SQLAlchemy URLs and ConfigParser-backed Alembic config."""


def escape_alembic_config_value(value: str) -> str:
    """Escape percent signs so ConfigParser returns the original URL."""

    return value.replace("%", "%%")
