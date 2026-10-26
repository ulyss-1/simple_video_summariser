"""Blocking HTTP calls that a cancel request can abort (issue #302).

The summarizers use blocking ``urllib`` transports, so a call can sit in a
read for minutes (a 900 s Ollama generation). ``call_abortably`` runs the
blocking call on a helper thread while the calling thread waits in slices of
``poll_interval`` and asks ``cancelled()`` after each slice. On a cancel it
fires the call's ``CallHandle``: the transport registered a closer on it, which
shuts the open socket so the blocked read fails and the helper thread ends (a
cancelled Ollama generation then really stops on the host). ``Cancelled`` is
raised once the thread has ended.

A response that arrived in the slice in which the flag was set wins: the
result of a finished call is never discarded for a cancel.

Adapters learn about a cancel only through the ``cancelled`` callable.
"""

from __future__ import annotations

import http.client
import socket
import threading
import urllib.request
from collections.abc import Callable
from typing import Any, cast

from common.errors import Cancelled

#: How often a waiting call checks the cancel signal.
DEFAULT_POLL_INTERVAL_SEC = 0.5

#: How long a cancelled call waits for its aborted thread to end.
JOIN_TIMEOUT_SEC = 5.0


class CallHandle:
    """What a transport registers its closers on, for one call.

    ``abort()`` runs every registered closer once. A closer registered after
    the abort runs at once, so an abort that arrives before the connection
    exists still takes effect.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closers: list[Callable[[], None]] = []
        self._aborted = False

    @property
    def aborted(self) -> bool:
        return self._aborted

    def register(self, closer: Callable[[], None]) -> None:
        with self._lock:
            if not self._aborted:
                self._closers.append(closer)
                return
        closer()

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            closers, self._closers = self._closers, []
        for closer in closers:
            try:
                closer()
            except Exception:  # noqa: BLE001, S110 - one failing closer must not hide the others
                pass


def call_abortably[T](
    work: Callable[[CallHandle], T],
    *,
    cancelled: Callable[[], bool],
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
    join_timeout: float = JOIN_TIMEOUT_SEC,
) -> T:
    """Run ``work(handle)`` on a thread; abort it and raise ``Cancelled`` on a cancel."""
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")
    handle = CallHandle()
    done = threading.Event()
    outcome: list[tuple[bool, Any]] = []

    def run() -> None:
        try:
            outcome.append((True, work(handle)))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            outcome.append((False, exc))
        finally:
            done.set()

    thread = threading.Thread(target=run, name="abortable-call", daemon=True)
    thread.start()
    while not done.wait(poll_interval):
        if cancelled():
            if done.is_set():
                break  # finished in this very slice: the result wins
            handle.abort()
            thread.join(join_timeout)
            raise Cancelled()
    thread.join()
    ok, value = outcome[0]
    if ok:
        return cast(T, value)
    raise value


# --- urllib with a closable socket ---------------------------------------------


def _close_connection(conn: _TrackedMixin) -> None:
    conn.aborted = True
    conn.auto_open = 0
    sock = conn.tracked_sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes a read blocked in another thread
        except OSError:
            pass
    conn.close()


class _TrackedMixin:
    """A connection that remembers its socket: urllib drops ``conn.sock`` after the headers."""

    aborted = False
    auto_open: int
    tracked_sock: socket.socket | None = None
    sock: Any
    close: Callable[[], None]

    def connect(self) -> None:
        if self.aborted:
            raise OSError("the call was aborted")
        super().connect()  # type: ignore[misc]
        self.tracked_sock = self.sock
        if self.aborted:
            _close_connection(self)
            raise OSError("the call was aborted")


class _TrackedHTTPConnection(_TrackedMixin, http.client.HTTPConnection):
    pass


class _TrackedHTTPSConnection(_TrackedMixin, http.client.HTTPSConnection):
    pass


def _tracked_factory(
    cls: type[http.client.HTTPConnection], handle: CallHandle | None
) -> Callable[..., http.client.HTTPConnection]:
    def make(host: str, **kwargs: Any) -> http.client.HTTPConnection:
        conn = cls(host, **kwargs)
        if handle is not None:
            handle.register(lambda: _close_connection(cast(_TrackedMixin, conn)))
        return conn

    return make


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        handle = getattr(req, "abort_handle", None)
        return self.do_open(_tracked_factory(_TrackedHTTPConnection, handle), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        handle = getattr(req, "abort_handle", None)
        context = cast(Any, self)._context
        return self.do_open(_tracked_factory(_TrackedHTTPSConnection, handle), req, context=context)


def build_abortable_opener(*handlers: Any) -> urllib.request.OpenerDirector:
    """An opener whose connections close on ``CallHandle.abort()``."""
    return urllib.request.build_opener(_HTTPHandler, _HTTPSHandler, *handlers)


def attach(request: urllib.request.Request, handle: CallHandle | None) -> None:
    """Tie ``request``'s connection to ``handle`` (the tracked handlers read it)."""
    cast(Any, request).abort_handle = handle
