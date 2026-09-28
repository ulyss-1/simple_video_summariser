"""compose.yml / compose.override.yml structure (issue #7, architecture.md 11.4).

No Docker is invoked here — these are plain-text checks of the checked-in
YAML (PyYAML isn't an approved dependency, so a small line-based scanner
stands in for a real parser; good enough for the flat, hand-written files
this repo commits). The whole file runs under `pytest -m "not integration"`
with Docker stopped. `test_compose_docker.py` covers the behaviour that
genuinely needs the `docker compose` CLI.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_YML = REPO_ROOT / "compose.yml"
COMPOSE_OVERRIDE_YML = REPO_ROOT / "compose.override.yml"


def _service_block(compose_text: str, service: str) -> str:
    """The indented body of one top-level service.

    Line-based, not a real YAML parser: walks from the `  <service>:` header
    until a line that isn't blank and isn't indented at least 4 spaces
    (i.e. the next service, or a top-level key like `volumes:`).
    """
    lines = compose_text.splitlines()
    header = f"  {service}:"
    start = lines.index(header)
    end = start + 1
    while end < len(lines) and (lines[end] == "" or lines[end].startswith("    ")):
        end += 1
    return "\n".join(lines[start:end])


def _top_level_section(compose_text: str, key: str) -> str:
    """The body of a top-level section (e.g. `services:`), up to the next
    top-level key or end of file."""
    lines = compose_text.splitlines()
    start = lines.index(f"{key}:") + 1
    end = start
    while end < len(lines) and (lines[end] == "" or lines[end].startswith(" ")):
        end += 1
    return "\n".join(lines[start:end])


def test_compose_yml_defines_only_the_db_and_migrate_services() -> None:
    services_text = _top_level_section(COMPOSE_YML.read_text(), "services")
    service_headers = [
        line
        for line in services_text.splitlines()
        if line.startswith("  ")
        and not line.startswith("    ")
        and not line.lstrip().startswith("#")
    ]
    assert set(service_headers) == {"  db:", "  migrate:"}
    assert len(service_headers) == 2


def test_db_service_uses_postgres_18_alpine_at_least_18_6() -> None:
    block = _service_block(COMPOSE_YML.read_text(), "db")
    # >=18.6 per architecture.md 16.1: pinned to the exact tag, not `:18`.
    assert "image: postgres:18-alpine" in block


def test_db_service_has_a_named_pgdata_volume() -> None:
    block = _service_block(COMPOSE_YML.read_text(), "db")
    # postgres:18's image declares VOLUME /var/lib/postgresql (not .../data,
    # as older images and architecture.md's literal example do) — mounting
    # at .../data makes the entrypoint refuse to start (task #7 finding).
    assert "pgdata:/var/lib/postgresql" in block
    assert "pgdata:/var/lib/postgresql/data" not in block
    volumes_text = _top_level_section(COMPOSE_YML.read_text(), "volumes")
    assert "pgdata:" in volumes_text


def test_db_service_has_a_pg_isready_healthcheck() -> None:
    block = _service_block(COMPOSE_YML.read_text(), "db")
    assert "pg_isready" in block


def test_db_service_requires_postgres_password() -> None:
    block = _service_block(COMPOSE_YML.read_text(), "db")
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}" in block


def test_compose_yml_db_service_publishes_no_ports() -> None:
    block = _service_block(COMPOSE_YML.read_text(), "db")
    assert "ports:" not in block


def test_compose_override_publishes_db_on_loopback_only() -> None:
    block = _service_block(COMPOSE_OVERRIDE_YML.read_text(), "db")
    assert '"127.0.0.1:5432:5432"' in block


def test_compose_yml_db_service_is_only_on_the_internal_network() -> None:
    # architecture.md 11.4: no route off the compose bridge except through
    # explicitly attached networks. Production never adds a second one.
    block = _service_block(COMPOSE_YML.read_text(), "db")
    assert "networks: [internal]" in block


def test_compose_override_attaches_db_to_a_second_non_internal_network() -> None:
    # `internal: true` (compose.yml, 11.4) means Docker never forwards a
    # published port for that container, on any machine, regardless of the
    # port mapping (root cause of #7's QA FAIL). The dev-only fix: attach
    # `db` to an additional, ordinary bridge network here so the
    # `127.0.0.1:5432` publish above has a non-internal network to forward
    # through. `internal` must stay listed too - this adds a network, it
    # doesn't move `db` off the production one.
    override_text = COMPOSE_OVERRIDE_YML.read_text()
    block = _service_block(override_text, "db")
    assert "networks: [internal, devhost]" in block

    # The override's own top-level `networks:` section defines only the new
    # network, and it must not be `internal: true` itself.
    networks_text = _top_level_section(override_text, "networks")
    assert "devhost:" in networks_text
    assert "internal: true" not in networks_text


def test_env_example_is_committed_with_a_postgres_password_placeholder() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    assert "POSTGRES_PASSWORD=" in text


def test_dotenv_itself_is_gitignored() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines


def test_alembic_ini_script_location_is_migrations() -> None:
    text = (REPO_ROOT / "alembic.ini").read_text()
    assert "script_location = migrations" in text


def test_alembic_ini_carries_no_database_url_or_credentials() -> None:
    text = (REPO_ROOT / "alembic.ini").read_text()
    assert "sqlalchemy.url" not in text
    assert "://" not in text


def test_alembic_ini_names_revisions_sequentially_via_file_template() -> None:
    text = (REPO_ROOT / "alembic.ini").read_text()
    assert "file_template = %%(rev)s_%%(slug)s" in text
