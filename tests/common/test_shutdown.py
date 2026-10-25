"""Unit tests for ``common.shutdown.SignalLatch`` (issue #108, hardened by #198).

The latch's whole job is to not lose a signal that lands during startup, so the
ordering inside ``start()`` — install the handlers *first*, then adopt the early
flag — is load-bearing. These tests pin it directly, next to the subprocess
early-signal tests of the three entrypoints.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator

import pytest

from common.shutdown import SignalLatch


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> Iterator[None]:
    # Belt and suspenders: whatever a test leaves installed, put the process's
    # own SIGTERM/SIGINT dispositions back so no other test inherits a handler.
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_start_installs_handlers_before_reading_the_early_flag() -> None:
    # A signal delivered while early_requested runs is caught by the latch's own
    # handler, which start() installs FIRST, so the latch ends up requested even
    # though early_requested returned False. Adopting the early flag before
    # installing the handlers would leave the default SIGTERM disposition in
    # place and this signal would terminate the process (exit 143) instead of
    # being latched. #198.
    def early_requested() -> bool:
        os.kill(os.getpid(), signal.SIGTERM)
        return False

    latch = SignalLatch.start(early_requested)
    try:
        assert latch.requested is True
    finally:
        latch.restore()


def test_start_adopts_an_early_flag_that_is_already_set() -> None:
    # The ordinary early-signal path: no signal during start(), but the early
    # handler saw one before main() took over, and start() adopts it.
    latch = SignalLatch.start(lambda: True)
    try:
        assert latch.requested is True
    finally:
        latch.restore()


def test_start_without_an_early_signal_is_not_requested() -> None:
    latch = SignalLatch.start(lambda: False)
    try:
        assert latch.requested is False
    finally:
        latch.restore()
