from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _compose(name: str = "docker-compose.yml") -> tuple[str, dict]:
    raw = (ROOT / name).read_text()
    return raw, yaml.safe_load(raw)


def test_production_data_services_are_private_and_authenticated():
    raw, compose = _compose()
    services = compose["services"]

    assert "ports" not in services["db"]
    assert "ports" not in services["redis"]
    assert "--requirepass" in " ".join(services["redis"]["command"])
    assert "redis://:${SONDER_REDIS_PASSWORD:?" in raw
    assert "SONDER_REDIS_PASSWORD:?" in raw


def test_request_services_receive_no_maintenance_credential():
    _, compose = _compose()
    for service_name in ("api", "bot"):
        environment = compose["services"][service_name]["environment"]
        assert environment["SONDER_MAINTENANCE_DATABASE_URL"] == ""
        assert environment["SONDER_MAINT_DB_PASSWORD"] == ""

    for module in (ROOT / "sonder/api").glob("*.py"):
        assert "maintenance_session" not in module.read_text(), module
    assert "maintenance_session" not in (ROOT / "sonder/bot/telegram.py").read_text()


def test_compose_has_no_literal_database_or_redis_password():
    for name in ("docker-compose.yml", "docker-compose.test.yml"):
        raw, _ = _compose(name)
        for line in raw.splitlines():
            if "PASSWORD:" in line and not line.lstrip().startswith("#"):
                assert "${" in line or line.rstrip().endswith(': ""'), line


def test_role_migrations_contain_no_login_password():
    for migration in (ROOT / "migrations").glob("*.sql"):
        statements = "\n".join(
            line
            for line in migration.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        assert "LOGIN PASSWORD" not in statements.upper()
