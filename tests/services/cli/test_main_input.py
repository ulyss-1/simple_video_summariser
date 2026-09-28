"""``main()`` before the pipeline starts: argument and connection handling (issue #31).

No database is needed: every case here ends before a job runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from common.config import Settings
from services.cli.main import Deps, main
from tests.services.analyzer.fakes import FakeSummarizer
from tests.services.cli.fakes import (
    MANUAL_ID,
    FakeAudio,
    FakeTranscriber,
)
from tests.services.transcriber.fakes import (
    FakeMetadata,
    FakeSubtitles,
    fixture_meta,
)

SECRET = "hunter2-do-not-print"


class Boom(Exception):
    pass


class ConnectSpy:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self) -> psycopg.Connection:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        raise AssertionError("the test must not reach a real connection")


def make_deps(connect: ConnectSpy, tmp_path: Path) -> tuple[Deps, FakeMetadata, FakeSubtitles, FakeAudio]:
    metadata = FakeMetadata(fixture_meta("manual_en", MANUAL_ID))
    subtitles = FakeSubtitles()
    audio = FakeAudio()
    deps = Deps(
        settings=Settings(
            DATABASE_URL=f"postgresql://user:{SECRET}@db.invalid/videos",  # type: ignore[arg-type]
            AUDIO_DIR=tmp_path,
        ),
        connect=connect,
        metadata=metadata,
        subtitles=subtitles,
        audio=audio,
        transcriber=FakeTranscriber(),
        summarizer=FakeSummarizer(name="ollama"),
    )
    return deps, metadata, subtitles, audio


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "   ",
        "abc",
        "dQw4w9WgXc!",
        "https://evil.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PLabc123",
        "https://www.youtube.com/@handle",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "x" * 3000,
        "https://youtu.be/dQw4w9WgXcQ\x1b[2J",
    ],
)
def test_invalid_input_exits_2_with_one_line_and_touches_nothing(
    ref: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy()
    deps, metadata, subtitles, audio = make_deps(connect, tmp_path)

    code = main(["run", ref], deps=deps)

    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert "\x1b" not in err
    assert connect.calls == 0
    assert metadata.calls == [] and subtitles.fetch_calls == [] and audio.calls == []


def test_a_bare_id_with_a_leading_dash_needs_double_dash_and_the_usage_message_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy()
    deps, *_ = make_deps(connect, tmp_path)

    code = main(["run", "-wNyEUrxzFU"], deps=deps)

    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    assert "--" in err
    assert "Traceback" not in err
    assert connect.calls == 0


def test_double_dash_lets_an_id_with_a_leading_dash_through_to_the_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy(psycopg.OperationalError("down"))
    deps, *_ = make_deps(connect, tmp_path)

    code = main(["run", "--", "-wNyEUrxzFU"], deps=deps)

    assert code == 1  # not 2: the reference was accepted
    assert connect.calls == 1


@pytest.mark.parametrize("argv", [[], ["nope"], ["run"], ["run", "a", "b"]])
def test_bad_command_lines_exit_2(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy()
    deps, *_ = make_deps(connect, tmp_path)

    assert main(argv, deps=deps) == 2

    assert connect.calls == 0
    assert "Traceback" not in capsys.readouterr().err


def test_help_exits_0_and_prints_usage_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps, *_ = make_deps(ConnectSpy(), tmp_path)

    assert main(["run", "--help"], deps=deps) == 0

    assert "ytdigest" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        psycopg.OperationalError(f"connection failed: password={SECRET}"),
        psycopg.ProgrammingError(f"invalid dsn postgresql://user:{SECRET}@db.invalid/videos"),
        ConnectionRefusedError(f"postgresql://user:{SECRET}@db.invalid"),
    ],
)
def test_an_unreachable_database_prints_one_line_without_the_dsn_or_password(
    failure: BaseException, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy(failure)
    deps, metadata, subtitles, audio = make_deps(connect, tmp_path)

    code = main(["run", MANUAL_ID], deps=deps)

    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert len(err.strip().splitlines()) == 1
    assert "connect" in err
    assert SECRET not in err
    assert "db.invalid" not in err
    assert "Traceback" not in err
    assert metadata.calls == [] and subtitles.fetch_calls == [] and audio.calls == []


def test_the_traceback_of_a_connection_failure_still_never_shows_the_password_with_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = ConnectSpy(psycopg.OperationalError(f"password={SECRET}"))
    deps, *_ = make_deps(connect, tmp_path)

    code = main(["run", "-v", MANUAL_ID], deps=deps)

    assert code == 1
    assert SECRET not in capsys.readouterr().err


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    from common.config import get_settings

    for name in ("DATABASE_URL", "SUMMARIZER", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_a_missing_database_url_is_one_line_naming_the_variable_and_nothing_runs(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", MANUAL_ID]) == 1

    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "DATABASE_URL" in err


def test_the_anthropic_summarizer_without_a_key_fails_at_setup_naming_the_variable(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("DATABASE_URL", f"postgresql://user:{SECRET}@db.invalid/videos")
    clean_env.setenv("SUMMARIZER", "anthropic")

    assert main(["run", MANUAL_ID]) == 1

    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert SECRET not in err
    assert "Traceback" not in err
