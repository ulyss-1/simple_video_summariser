"""Fake ``python -m yt_dlp`` for the pipeline smoke test (issue #137).

Bind-mounted read-only into the ``transcriber`` container and put first on its
``PYTHONPATH``, so the adapter's ``sys.executable -m yt_dlp`` runs this instead
of the real tool. It replays two recorded fixtures and refuses everything else,
so a drifting adapter fails loudly instead of reaching YouTube.

Stdlib only, no environment variables: the paths below are the bind-mount
targets in ``compose.smoke.yml``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

URL = "https://www.youtube.com/watch?v=iG9CE55wbtY"
METADATA = Path("/smoke/fixtures/manual_en.json")
VTT = Path("/smoke/fixtures/youtube_manual_iG9CE55wbtY.en.vtt")
LOG = Path("/tmp/fake-ytdlp.log")


def _refuse(argv: list[str]) -> int:
    sys.stderr.write(f"ERROR: fake yt-dlp: unexpected call: {argv}\n")
    return 1


def _after(flags: list[str], name: str) -> str | None:
    index = flags.index(name) if name in flags else -1
    return flags[index + 1] if 0 <= index < len(flags) - 1 else None


def main(argv: list[str]) -> int:
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")

    if not argv or argv[-1] != URL:
        return _refuse(argv)
    flags = argv[:-1]

    if "--dump-json" in flags:
        if "--write-subs" in flags or "--write-auto-subs" in flags:
            return _refuse(argv)
        sys.stdout.write(METADATA.read_text(encoding="utf-8"))
        return 0

    target = _after(flags, "--paths")
    if (
        target is not None
        and "--skip-download" in flags
        and "--write-subs" in flags
        and "--write-auto-subs" not in flags
        and _after(flags, "--sub-langs") == "en"
        and _after(flags, "--sub-format") == "vtt"
    ):
        shutil.copyfile(VTT, Path(target) / "fake.en.vtt")
        return 0

    return _refuse(argv)


sys.exit(main(sys.argv[1:]))
