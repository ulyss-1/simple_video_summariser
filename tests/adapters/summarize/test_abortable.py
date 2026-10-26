"""Aborting a blocking HTTP call on a cancel (issue #302).

Fake transports block on an ``Event`` that only the abort hook sets, so each
test is a forced interleaving. The real-socket tests use a loopback server
that never answers and prove the connection is closed from the host's side.
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

import pytest

from adapters.summarize import anthropic, ollama
from adapters.summarize.abortable import CallHandle, call_abortably
from common.errors import Cancelled

DEADLINE_SEC = 5.0
POLL = 0.001


class BlockingCall:
    """A request that blocks until the abort hook (``handle``) releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        self.released_by_abort = False

    def __call__(self, handle: CallHandle) -> str:
        self.thread = threading.current_thread()
        handle.register(self.release.set)
        self.started.set()
        self.released_by_abort = self.release.wait(DEADLINE_SEC)
        raise OSError("connection closed")

    def cancel_once_started(self) -> bool:
        assert self.started.wait(DEADLINE_SEC), "the request never started"
        return True


def test_a_cancel_fires_the_abort_hook_and_ends_the_request_thread() -> None:
    call = BlockingCall()

    with pytest.raises(Cancelled):
        call_abortably(call, cancelled=call.cancel_once_started, poll_interval=POLL)

    assert call.released_by_abort, "the abort hook never ran"
    assert call.thread is not None and not call.thread.is_alive()


def test_a_result_that_arrived_in_the_slice_the_flag_was_set_is_used() -> None:
    finished = threading.Event()
    holder: list[threading.Thread] = []

    def work(_handle: CallHandle) -> str:
        holder.append(threading.current_thread())
        finished.set()
        return "answer"

    def cancelled() -> bool:
        # Called only after a slice ended without a result: wait until the request
        # thread has ended, so its result is certainly there when the flag is read.
        assert finished.wait(DEADLINE_SEC), "the request never ran"
        holder[0].join(DEADLINE_SEC)
        return True

    assert call_abortably(work, cancelled=cancelled, poll_interval=POLL) == "answer"


def test_an_error_of_the_request_propagates_to_the_caller() -> None:
    def work(_handle: CallHandle) -> str:
        raise OSError("refused")

    with pytest.raises(OSError, match="refused"):
        call_abortably(work, cancelled=lambda: False, poll_interval=POLL)


def test_a_handle_registered_after_the_abort_runs_the_closer_at_once() -> None:
    handle = CallHandle()
    handle.abort()
    closed: list[int] = []
    handle.register(lambda: closed.append(1))
    assert closed == [1]


def test_a_failing_closer_does_not_hide_the_others() -> None:
    handle = CallHandle()
    closed: list[int] = []

    def bad() -> None:
        raise OSError("already closed")

    handle.register(bad)
    handle.register(lambda: closed.append(1))
    handle.abort()
    assert closed == [1]


def test_a_non_positive_poll_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        call_abortably(lambda _h: 1, cancelled=lambda: False, poll_interval=0)


# --- the real transports against a socket that never answers ---------------


class Silent:
    """A loopback server that reads the request, then waits for the client to hang up."""

    def __init__(self) -> None:
        self.received = threading.Event()
        self.client_closed = threading.Event()
        silent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                silent.received.set()
                self.connection.settimeout(DEADLINE_SEC)
                try:
                    if self.connection.recv(1) == b"":
                        silent.client_closed.set()
                except OSError:
                    silent.client_closed.set()

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def silent() -> Iterator[Silent]:
    with Silent() as server:
        yield server


def _cancel_once_received(server: Silent) -> Callable[[], bool]:
    def cancelled() -> bool:
        return server.received.is_set()

    return cancelled


def test_the_ollama_transport_closes_the_socket_on_an_abort(silent: Silent) -> None:
    with pytest.raises(Cancelled):
        call_abortably(
            lambda handle: ollama.urllib_transport(silent.url, b"{}", 60, handle=handle),
            cancelled=_cancel_once_received(silent),
            poll_interval=POLL,
        )
    assert silent.client_closed.wait(DEADLINE_SEC), "the server never saw the client hang up"


def test_the_anthropic_transport_closes_the_socket_on_an_abort(silent: Silent) -> None:
    with pytest.raises(Cancelled):
        call_abortably(
            lambda handle: anthropic.urllib_transport(
                "POST", silent.url, {}, b"{}", 60, handle=handle
            ),
            cancelled=_cancel_once_received(silent),
            poll_interval=POLL,
        )
    assert silent.client_closed.wait(DEADLINE_SEC), "the server never saw the client hang up"


def test_a_handle_aborted_before_connecting_refuses_the_connection(silent: Silent) -> None:
    handle = CallHandle()
    handle.abort()
    with pytest.raises((OSError, http.client.HTTPException)):
        ollama.urllib_transport(silent.url, b"{}", 60, handle=handle)
    assert not silent.received.is_set()
