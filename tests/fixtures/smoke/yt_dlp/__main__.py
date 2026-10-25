"""Fake ``python -m yt_dlp`` for the pipeline smokes (issues #137, #262).

Bind-mounted read-only into the ``transcriber`` container and put first on its
``PYTHONPATH``, so the adapter's ``sys.executable -m yt_dlp`` runs this instead
of the real tool. It replays recorded fixtures and refuses everything else, so
a drifting adapter fails loudly instead of reaching YouTube.

One table, keyed by the full watch URL, says what each recorded video serves:
metadata for ``--dump-json``, plus either a VTT (manual captions, #137) or an
audio clip (no captions, so the Whisper path, #262). A fixture file that a
project does not mount is refused, not an error of the fake.

Stdlib only, no environment variables: the paths below are the bind-mount
targets in ``compose.smoke.yml`` and ``compose.whisper.yml``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

LOG = Path("/tmp/fake-ytdlp.log")
FIXTURES = Path("/smoke/fixtures")

VIDEOS: dict[str, dict[str, Path | None]] = {
    "https://www.youtube.com/watch?v=iG9CE55wbtY": {
        "metadata": FIXTURES / "manual_en.json",
        "vtt": FIXTURES / "youtube_manual_iG9CE55wbtY.en.vtt",
        "audio": None,
    },
    "https://www.youtube.com/watch?v=BZiu46G4ukc": {
        "metadata": FIXTURES / "no_captions.json",
        "vtt": None,
        "audio": FIXTURES / "speech.opus",
    },
}


def _refuse(argv: list[str]) -> int:
    sys.stderr.write(f"ERROR: fake yt-dlp: unexpected call: {argv}\n")
    return 1


def _after(flags: list[str], name: str) -> str | None:
    index = flags.index(name) if name in flags else -1
    return flags[index + 1] if 0 <= index < len(flags) - 1 else None


def _mounted(path: Path | None) -> Path | None:
    return path if path is not None and path.is_file() else None


def main(argv: list[str]) -> int:
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")

    video = VIDEOS.get(argv[-1]) if argv else None
    if video is None:
        return _refuse(argv)
    flags = argv[:-1]

    if "--dump-json" in flags:
        metadata = _mounted(video["metadata"])
        if metadata is None or "--write-subs" in flags or "--write-auto-subs" in flags:
            return _refuse(argv)
        sys.stdout.write(metadata.read_text(encoding="utf-8"))
        return 0

    target = _after(flags, "--paths")
    vtt = _mounted(video["vtt"])
    if (
        target is not None
        and vtt is not None
        and "--skip-download" in flags
        and "--write-subs" in flags
        and "--write-auto-subs" not in flags
        and _after(flags, "--sub-langs") == "en"
        and _after(flags, "--sub-format") == "vtt"
    ):
        shutil.copyfile(vtt, Path(target) / "fake.en.vtt")
        return 0

    audio = _mounted(video["audio"])
    out = _after(flags, "-o")
    if (
        out is not None
        and audio is not None
        and _after(flags, "-f") == "bestaudio"
        and "--write-subs" not in flags
        and "--write-auto-subs" not in flags
    ):
        shutil.copyfile(audio, out)
        return 0

    return _refuse(argv)


sys.exit(main(sys.argv[1:]))
