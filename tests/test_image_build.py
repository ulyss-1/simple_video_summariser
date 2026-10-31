"""The prebuilt-base seam of the image tests (#368): argument lists only, no Docker."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.image_build import (
    DEPS_VAR,
    WHISPER_DEPS_VAR,
    backend_build_args,
    deps_context,
    prebuilt_deps,
    whisper_build_args,
)

LOCAL = "ytdigest-t135-deps-abc:latest"
FAKE_DEPS = "ghcr.io/example/ytdigest-deps@sha256:" + "d" * 64
FAKE_WHISPER = "ghcr.io/example/ytdigest-whisper-deps@sha256:" + "e" * 64


@pytest.mark.parametrize("environ", [{}, {DEPS_VAR: ""}, {DEPS_VAR: "  "}])
def test_unset_or_empty_gives_todays_from_source_arguments(environ: dict[str, str]) -> None:
    assert prebuilt_deps(environ) == ""
    assert backend_build_args(environ) == []
    assert whisper_build_args(LOCAL, environ) == [
        "--build-context",
        f"ytdigest-deps=docker-image://{LOCAL}",
    ]
    assert whisper_build_args(LOCAL, {**environ, WHISPER_DEPS_VAR: ""}) == whisper_build_args(
        LOCAL, environ
    )


def test_a_prebuilt_deps_image_replaces_the_deps_stage_and_is_the_whisper_base() -> None:
    environ = {DEPS_VAR: FAKE_DEPS}
    assert backend_build_args(environ) == ["--build-context", f"deps=docker-image://{FAKE_DEPS}"]
    assert whisper_build_args(LOCAL, environ) == [
        "--build-context",
        f"ytdigest-deps=docker-image://{FAKE_DEPS}",
    ]


def test_a_prebuilt_whisper_deps_image_replaces_that_stage_only() -> None:
    environ = {WHISPER_DEPS_VAR: FAKE_WHISPER}
    assert backend_build_args(environ) == []
    assert whisper_build_args(LOCAL, environ) == [
        "--build-context",
        f"ytdigest-deps=docker-image://{LOCAL}",
        "--build-context",
        f"whisper-deps=docker-image://{FAKE_WHISPER}",
    ]


def test_a_from_source_whisper_build_never_takes_the_whisper_variable() -> None:
    # The bad-pin test builds whisper-deps itself: it uses deps_context only.
    assert deps_context(LOCAL, {WHISPER_DEPS_VAR: FAKE_WHISPER, DEPS_VAR: FAKE_DEPS}) == [
        "--build-context",
        f"ytdigest-deps=docker-image://{FAKE_DEPS}",
    ]


def test_the_layer_cache_test_ignores_the_variables() -> None:
    # It exists to prove the Dockerfiles' own caching, so it always builds from source.
    cache_test = (Path(__file__).parent / "test_whisper_cache_docker.py").read_text()
    assert "image_build" not in cache_test
    assert DEPS_VAR not in cache_test and WHISPER_DEPS_VAR not in cache_test


def test_the_variables_are_read_in_the_one_helper_module() -> None:
    root = Path(__file__).parent
    others = [
        p.name
        for p in root.rglob("*.py")
        if p.name not in {"image_build.py", "test_image_build.py"}
        and "os.environ" in p.read_text()
        and "YTDIGEST_" in p.read_text()
    ]
    assert others == []
    assert "os.environ" in (root / "image_build.py").read_text()
