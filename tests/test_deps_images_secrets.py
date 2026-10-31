"""Nothing secret can enter a prebuilt dependency image, from the files alone (#368).

Static. tests/test_deps_image_secret_docker.py builds a stage and scans its layers.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = (ROOT / "ops" / "Dockerfile.backend", ROOT / "ops" / "Dockerfile.whisper")
NO_GHCR = (
    "compose.yml",
    "compose.override.yml",
    "ops/host/preflight.sh",
    "_docs/deploy-host.md",
)


def test_dockerignore_still_excludes_secrets_history_tests_and_docs() -> None:
    lines = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env*", "**/.env*", ".git", "tests/", "_docs/"} <= lines


def test_no_dockerfile_declares_an_arg_or_env_named_like_a_secret() -> None:
    secret = re.compile(r"TOKEN|PASSWORD|SECRET|KEY", re.IGNORECASE)
    for dockerfile in DOCKERFILES:
        for line in dockerfile.read_text().splitlines():
            match = re.match(r"\s*(ARG|ENV)\s+(.*)", line)
            if match:
                names = re.findall(r"([A-Za-z_]\w*)=", match.group(2))
                names += re.findall(r"^([A-Za-z_]\w*)\s*$", match.group(2))
                assert not any(secret.search(n) for n in names), f"{dockerfile.name}: {line}"


def test_production_inputs_never_mention_ghcr() -> None:
    # Production builds from source and the deploy host needs no registry access.
    for name in NO_GHCR:
        path = ROOT / name
        assert path.exists(), name
        assert "ghcr.io" not in path.read_text(), name


def test_no_dockerfile_instruction_reads_ghcr() -> None:
    # A comment may explain the opt-in seam; no instruction may name the registry.
    for dockerfile in DOCKERFILES:
        for line in dockerfile.read_text().splitlines():
            if not line.lstrip().startswith("#"):
                assert "ghcr.io" not in line, f"{dockerfile.name}: {line}"
