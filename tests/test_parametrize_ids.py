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
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# `object.__repr__` renders ``<... object at 0x<hex>>``; functions, lambdas and
# other default reprs share the `` at 0x<hex>`` tail. That tail is the address.
_ADDRESS_IN_ID = re.compile(r" at 0x[0-9A-Fa-f]+")


def test_no_collected_node_id_embeds_a_memory_address() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    # 0 = tests collected, 5 = none collected; anything else is a real error.
    assert result.returncode in (0, 5), result.stdout + result.stderr

    offenders = [line for line in result.stdout.splitlines() if _ADDRESS_IN_ID.search(line)]

    assert not offenders, (
        "parametrize ids embed a memory address (give them an explicit id=):\n"
        + "\n".join(offenders)
    )
