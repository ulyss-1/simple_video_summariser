"""The one place image tests read the prebuilt-base seam (#368).

CI may point two environment variables at prebuilt dependency images (digest
references on ghcr.io, found by scripts/deps_image_tag.py):

- ``YTDIGEST_DEPS_IMAGE`` replaces the ``deps`` stage of ops/Dockerfile.backend
  (named build context ``deps``) and is the ``ytdigest-deps`` base of the
  whisper image;
- ``YTDIGEST_WHISPER_DEPS_IMAGE`` replaces the ``whisper-deps`` stage of
  ops/Dockerfile.whisper (named build context ``whisper-deps``).

Unset or empty means "build from source", which is today's behaviour. The
layer-cache tests (tests/test_whisper_cache_docker.py) exist to prove the
Dockerfiles' own caching and never import this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEPS_VAR = "YTDIGEST_DEPS_IMAGE"
WHISPER_DEPS_VAR = "YTDIGEST_WHISPER_DEPS_IMAGE"


def _ref(var: str, environ: Mapping[str, str] | None) -> str:
    source = environ if environ is not None else os.environ  # noqa: TID251 - the seam's one read
    return source.get(var, "").strip()


def prebuilt_deps(environ: Mapping[str, str] | None = None) -> str:
    """The prebuilt ``deps`` image reference, or "" to build it from source."""
    return _ref(DEPS_VAR, environ)


def backend_build_args(environ: Mapping[str, str] | None = None) -> list[str]:
    """Extra `docker build` arguments for ops/Dockerfile.backend (default target)."""
    ref = prebuilt_deps(environ)
    return ["--build-context", f"deps=docker-image://{ref}"] if ref else []


def deps_context(local_deps_tag: str, environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``ytdigest-deps`` build context of ops/Dockerfile.whisper.

    The prebuilt image when set, else the locally built ``local_deps_tag``. It is
    all a from-source build of ``whisper-deps`` needs.
    """
    ref = prebuilt_deps(environ) or local_deps_tag
    return ["--build-context", f"ytdigest-deps=docker-image://{ref}"]


def whisper_build_args(
    local_deps_tag: str, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Extra `docker build` arguments for ops/Dockerfile.whisper (default target)."""
    args = deps_context(local_deps_tag, environ)
    ref = _ref(WHISPER_DEPS_VAR, environ)
    if ref:
        args += ["--build-context", f"whisper-deps=docker-image://{ref}"]
    return args


def compose_override(environ: Mapping[str, str] | None = None) -> str:
    """A compose override file body that applies the seam to the stack build, or "".

    compose.yml names no registry (#368), so the compose-stack tests would build
    the deps and whisper layers from source even when CI has prebuilt bases.
    This adds the same named build contexts as ``backend_build_args`` and
    ``whisper_build_args`` for ``deps``, ``migrate`` (the backend image) and
    ``transcriber`` (the whisper image). Unset variables give "" (no file).
    """
    deps = prebuilt_deps(environ)
    whisper = _ref(WHISPER_DEPS_VAR, environ)
    if not deps and not whisper:
        return ""
    lines = ["services:"]
    if deps:
        for service in ("deps", "migrate"):
            lines += [
                f"  {service}:",
                "    build:",
                "      additional_contexts:",
                f"        deps: docker-image://{deps}",
            ]
    if deps or whisper:
        lines += ["  transcriber:", "    build:", "      additional_contexts:"]
        if deps:
            lines.append(f"        ytdigest-deps: docker-image://{deps}")
        if whisper:
            lines.append(f"        whisper-deps: docker-image://{whisper}")
    return "\n".join(lines) + "\n"
