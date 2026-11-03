"""Guard against parametrize ids that embed a memory address (#194).

A parametrize value with no explicit ``id=`` is labelled by its ``repr``. For a
value that uses the default ``object.__repr__`` — ``<... at 0x7f..>`` — that
label contains a memory address, which differs between processes. ``pytest
--lf`` / ``--ff`` match tests by node id, so such a test cannot be re-run across
runs: shuffle (#72) can find an order-dependent failure that ``--lf`` then
cannot replay. Give those parameters an explicit ``id=``.

The check scans the whole collected suite for the default-repr address
signature `` at 0x<hex>``. A bare ``0x`` substring is not used: it collides
with legitimate hex-string inputs (``0x1``, ``0x10``) and URL-encoded test data
(``%20x``, ``%40x``), none of which embeds an address.
"""

import re

from tests.xdist_support import REPO_ROOT, run_pytest

# `object.__repr__` renders ``<... object at 0x<hex>>``; functions, lambdas and
# other default reprs share the `` at 0x<hex>`` tail. That tail is the address.
_ADDRESS_IN_ID = re.compile(r" at 0x[0-9A-Fa-f]+")


def test_no_collected_node_id_embeds_a_memory_address() -> None:
    """Keeps the real wait: only a real pytest collection of the whole suite shows the node ids."""
    # run_pytest drops the outer worker's PYTEST_XDIST_* variables (#384).
    result = run_pytest(REPO_ROOT, "--collect-only", "-q")
    # 0 = tests collected, 5 = none collected; anything else is a real error.
    assert result.returncode in (0, 5), result.stdout + result.stderr

    offenders = [line for line in result.stdout.splitlines() if _ADDRESS_IN_ID.search(line)]

    assert not offenders, (
        "parametrize ids embed a memory address (give them an explicit id=):\n"
        + "\n".join(offenders)
    )
