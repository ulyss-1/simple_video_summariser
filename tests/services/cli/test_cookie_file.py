"""``default_deps`` and ``YTDLP_COOKIE_FILE`` (issue #63)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapters.youtube.ytdlp import CookieFileError
from common.config import get_settings
from services.cli.main import default_deps, main
from tests.services.transcriber.fakes import make_settings


def test_default_deps_refuses_a_set_but_missing_cookie_file(tmp_path: Path) -> None:
    settings = make_settings(YTDLP_COOKIE_FILE=tmp_path / "missing.txt", SUMMARIZER="ollama")
    with pytest.raises(CookieFileError, match="YTDLP_COOKIE_FILE"):
        default_deps(settings)


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_main_exits_2_on_an_unusable_cookie_file(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "missing.txt"
    if kind == "directory":
        target = tmp_path
    elif kind == "unreadable":
        target.write_text("x")
        target.chmod(0)
        if os.access(target, os.R_OK):
            pytest.skip("permission bits are not enforced (running as root)")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("SUMMARIZER", "ollama")
    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(target))
    get_settings.cache_clear()
    try:
        rc = main(["run", "dQw4w9WgXcQ"])
    finally:
        get_settings.cache_clear()

    assert rc == 2
    assert "YTDLP_COOKIE_FILE" in capsys.readouterr().err
