"""Rejected R2 credentials fail fast, against REAL restic and a local 401/403 backend (#412)."""

from __future__ import annotations

import http.server
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ops.backup_support import BACKUP, BUCKET, Rig

pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="needs the real restic")

PROBE_SECONDS = 3  # injected via AUTH_PROBE_TIMEOUT; production default is 45
BOUND_SECONDS = 20.0  # generous ceiling for 3 s + process start; restic alone retries for minutes


def _server(status: int) -> Iterator[int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def respond(self) -> None:
            body = b'<?xml version="1.0"?><Error><Code>InvalidAccessKeyId</Code></Error>'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = respond

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def rejecting_port() -> Iterator[int]:
    # restic retries a 401 for minutes (the observed hang)
    yield from _server(401)


@pytest.fixture
def forbidden_port() -> Iterator[int]:
    # restic treats a 403 as permanent and fails by itself in well under a second
    yield from _server(403)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    (r.bin / "restic").unlink()  # the real /usr/bin/restic is found instead
    return r


@pytest.mark.parametrize("args", [("--check",), ("--init",), ()], ids=["check", "init", "run"])
def test_rejected_credentials_fail_in_seconds_naming_the_stage_and_leaking_nothing(
    rig: Rig, rejecting_port: int, args: tuple[str, ...]
) -> None:
    env = {
        "YTDIGEST_TEST_REPOSITORY": f"s3:http://127.0.0.1:{rejecting_port}/{BUCKET}",
        "AUTH_PROBE_TIMEOUT": str(PROBE_SECONDS),
    }
    started = time.monotonic()
    result = rig.run(BACKUP, *args, extra_env=env)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr
    assert elapsed < BOUND_SECONDS
    assert "FAILED stage=auth:" in result.stderr
    assert "credentials rejected" in result.stderr
    assert rig.leaked(result) == []
    assert f"127.0.0.1:{rejecting_port}" not in result.stdout + result.stderr
    assert not any("pg_dump" in c["args"] for c in rig.calls("docker"))


@pytest.mark.parametrize("args", [("--check",), ("--init",), ()], ids=["check", "init", "run"])
def test_a_403_fails_immediately_naming_the_stage_and_leaking_nothing(
    rig: Rig, forbidden_port: int, args: tuple[str, ...]
) -> None:
    env = {"YTDIGEST_TEST_REPOSITORY": f"s3:http://127.0.0.1:{forbidden_port}/{BUCKET}"}
    started = time.monotonic()
    result = rig.run(BACKUP, *args, extra_env=env)
    assert time.monotonic() - started < BOUND_SECONDS
    assert result.returncode == 1, result.stderr
    assert "FAILED stage=" in result.stderr
    assert "access denied" in result.stderr
    assert rig.leaked(result) == []
    assert f"127.0.0.1:{forbidden_port}" not in result.stdout + result.stderr
