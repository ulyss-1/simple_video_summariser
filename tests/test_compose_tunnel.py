"""compose.tunnel.yml, the opt-in Cloudflare Tunnel overlay (issue #426).

Network-free text checks of the checked-in files; the rendered model is
covered in test_compose_tunnel_docker.py.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TUNNEL = (REPO_ROOT / "compose.tunnel.yml").read_text()
BASE = (REPO_ROOT / "compose.yml").read_text()
ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text()
VARS = ("PUBLIC_HOSTNAME", "CLOUDFLARED_TUNNEL_ID", "CLOUDFLARED_CREDENTIALS_FILE")


def _block(text: str, header: str) -> str:
    lines = text.splitlines()
    start = lines.index(header)
    end = start + 1
    while end < len(lines) and (lines[end] == "" or lines[end].startswith("  ") or lines[end].startswith("#")):
        end += 1
    return "\n".join(lines[start:end])


def test_the_base_compose_file_never_mentions_the_tunnel() -> None:
    for word in ("cloudflared", "tunnel", *VARS):
        assert word.lower() not in BASE.lower()


def test_overlay_defines_exactly_cloudflared_and_a_web_network_addition() -> None:
    services = _block(TUNNEL, "services:")
    assert re.findall(r"^  (\w+):$", services, re.MULTILINE) == ["cloudflared", "web"]
    web = services.split("  web:")[1]
    assert "networks: [edge, tunnel]" in web
    assert "ports" not in web


def test_cloudflared_is_one_pinned_replica_without_ports() -> None:
    block = _block(TUNNEL, "services:").split("  web:")[0]
    assert re.search(r"image: cloudflare/cloudflared:\d{4}\.\d+\.\d+@sha256:[0-9a-f]{64}\n", block)
    assert "deploy: {replicas: 1}" in block
    assert "networks: [tunnel]" in block
    assert "ports:" not in block
    assert "build:" not in block
    assert "tunnel --no-autoupdate" in block
    assert "source: cloudflared_credentials" in block


def test_ingress_has_the_hostname_rule_first_and_the_catch_all_last() -> None:
    config = _block(TUNNEL, "configs:")
    rules = re.findall(r"^        - (.+)$", config, re.MULTILINE)
    assert rules[0].startswith("hostname: ${PUBLIC_HOSTNAME:?")
    assert rules[-1] == "service: http_status:404"
    assert "service: http://web:8080" in config
    assert "tunnel: ${CLOUDFLARED_TUNNEL_ID:?" in config
    assert "credentials-file: /run/secrets/cloudflared_credentials" in config


def test_credentials_are_a_file_secret_never_an_environment_value() -> None:
    assert "file: ${CLOUDFLARED_CREDENTIALS_FILE:?" in _block(TUNNEL, "secrets:")
    code = "\n".join(line for line in TUNNEL.splitlines() if not line.lstrip().startswith("#"))
    assert "environment:" not in code
    assert "TUNNEL_TOKEN" not in TUNNEL


def test_no_real_hostname_or_identifier_in_the_overlay_or_env_example() -> None:
    for text in (TUNNEL, ENV_EXAMPLE):
        assert "devopsdojo" not in text
        assert "dev-ytdigest" not in text
        assert not re.search(r"ytdigest\.[a-z]+\.", text)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
        assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}", text)


def test_env_example_lists_the_three_variables_empty() -> None:
    for name in VARS:
        assert f"\n{name}=\n" in ENV_EXAMPLE


def test_digest_check_workflow_scans_the_overlay_and_parses_its_pin() -> None:
    workflow = (REPO_ROOT / ".github/workflows/base-image-digest-check.yml").read_text()
    assert " compose.tunnel.yml " in workflow
    spec = importlib.util.spec_from_file_location(
        "base_image_digest_check", REPO_ROOT / "scripts" / "base_image_digest_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    refs = module.pinned_compose_references(TUNNEL)
    assert [r.written for r in refs] == ["cloudflare/cloudflared"]
