from configparser import ConfigParser

from app.db.alembic_url import escape_alembic_config_value


def test_percent_encoded_database_url_survives_alembic_config_interpolation():
    database_url = "postgresql+psycopg://user:p%2Dword@postgres:5432/database"
    parser = ConfigParser()
    parser.add_section("alembic")

    parser.set("alembic", "sqlalchemy.url", escape_alembic_config_value(database_url))

    assert parser.get("alembic", "sqlalchemy.url") == database_url
