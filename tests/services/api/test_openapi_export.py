"""The OpenAPI exporter (issue #46): offline, deterministic, safe to write."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.api import openapi_export

REPO = Path(__file__).resolve().parents[3]
FORBIDDEN_PREFIXES = ("adapters", "yt_dlp", "faster_whisper")


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}  # noqa: TID251
    env.update(extra)
    return env


def _run(*args: str, cwd: Path = REPO, **env: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "services.api.openapi_export", *args],
        cwd=cwd,
        env=_clean_env(PYTHONPATH=str(REPO), **env),
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_export_runs_without_database_url_and_imports_no_adapters() -> None:
    probe = (
        "import json, sys\n"
        "from services.api import openapi_export\n"
        "openapi_export.render(openapi_export.build_schema())\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {FORBIDDEN_PREFIXES!r})\n"
        "print(json.dumps(bad))\n"
    )
    env = _clean_env(PYTHONPATH=str(REPO))
    assert "DATABASE_URL" not in env
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_cli_to_stdout_works_with_database_url_unset() -> None:
    result = _run("--out", "-")
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(b"}\n")
    assert json.loads(result.stdout)["openapi"].startswith("3.")


def test_output_is_byte_identical_across_cwd_hashseed_locale_and_tz(tmp_path: Path) -> None:
    first = _run("--out", "-", PYTHONHASHSEED="1", LC_ALL="C", TZ="UTC")
    second = _run(
        "--out", "-", cwd=tmp_path, PYTHONHASHSEED="12345", LC_ALL="C.UTF-8", TZ="Asia/Tokyo"
    )
    assert first.returncode == 0 == second.returncode, (first.stderr, second.stderr)
    assert first.stdout == second.stdout


def test_format_is_sorted_indented_utf8_lf_with_one_trailing_newline() -> None:
    schema = openapi_export.build_schema()
    text = openapi_export.render(schema)
    assert text == json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert "\r" not in text


def test_schema_has_no_host_or_build_specific_fields() -> None:
    schema = openapi_export.build_schema()
    assert "servers" not in schema
    assert schema["info"]["version"] == "0.1.0"
    text = openapi_export.render(schema).lower()
    for needle in ("localhost", "127.0.0.1", "timestamp", "git_sha"):
        assert needle not in text


def test_paths_are_unprefixed_and_exclude_docs() -> None:
    paths = openapi_export.build_schema()["paths"]
    assert "/healthz" in paths
    for path in paths:
        assert not path.startswith("/api"), path
    assert not {"/docs", "/redoc", "/openapi.json"} & set(paths)


def test_out_path_writes_file_and_exits_zero(tmp_path: Path) -> None:
    target = tmp_path / "schema.json"
    assert openapi_export.main(["--out", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == openapi_export.render(
        openapi_export.build_schema()
    )
    assert [p.name for p in tmp_path.iterdir()] == ["schema.json"]


def test_out_dash_writes_to_stdout(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert openapi_export.main(["--out", "-"]) == 0
    out = capsysbinary.readouterr().out
    assert out == openapi_export.render(openapi_export.build_schema()).encode("utf-8")


def test_default_target_is_the_committed_web_openapi_json() -> None:
    assert openapi_export.DEFAULT_OUT == REPO / "web" / "openapi.json"


def test_missing_directory_fails_with_one_line_error_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "nope" / "deeper" / "schema.json"
    assert openapi_export.main(["--out", str(target)]) != 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert str(target) in err
    assert list(tmp_path.iterdir()) == []


def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "schema.json"
    target.write_text("old\n", encoding="utf-8")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    assert openapi_export.main(["--out", str(target)]) != 0
    assert target.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["schema.json"]
