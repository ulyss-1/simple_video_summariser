"""Real smoke test for the faster-whisper adapter (issue #20).

Deselected by default (``tests/adapters/transcription/conftest.py``): run
with ``pytest -m whisper`` after ``pip install -r requirements.whisper.txt``
(AGENTS.md -> Setup keeps faster-whisper off the host, so this is meant to be
run in a throwaway venv, not the repo's own ``.venv``).

Uses the real ``tiny`` model on a locally generated clip - no network access
beyond the one-time model download that faster-whisper does on first use.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from adapters.transcription.faster_whisper import FasterWhisperTranscriber
from common.config import get_settings
from common.models import AudioRef

pytestmark = pytest.mark.whisper

_DURATION_SEC = 4.0


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _generate_clip(dest: Path) -> float:
    """A short 16 kHz mono WAV clip: real speech via espeak-ng if present."""
    if shutil.which("espeak-ng"):
        raw = dest.with_suffix(".raw.wav")
        subprocess.run(
            [
                "espeak-ng",
                "-s",
                "140",
                "-w",
                str(raw),
                "the quick brown fox jumps over the lazy dog",
            ],
            check=True,
            capture_output=True,
        )
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", str(dest)],
                check=True,
                capture_output=True,
            )
        finally:
            raw.unlink(missing_ok=True)
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={_DURATION_SEC}",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
    return _probe_duration(dest)


def test_the_tiny_model_transcribes_a_generated_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()

    rel_path = "sm/smoke.wav"
    clip_path = tmp_path / rel_path
    clip_path.parent.mkdir(parents=True)
    duration_sec = _generate_clip(clip_path)

    transcriber = FasterWhisperTranscriber(model="tiny", compute_type="int8", threads=1)
    audio = AudioRef(
        rel_path=rel_path, bytes=clip_path.stat().st_size, duration_sec=duration_sec
    )

    progress: list[float] = []
    result = transcriber.transcribe(audio, on_progress=progress.append)

    assert result.engine_meta["engine"] == "faster-whisper"
    assert result.engine_meta["model"] == "tiny"
    assert result.engine_meta["beam_size"] == 5
    assert result.engine_meta["vad"] is True
    assert result.engine_meta["audio_sec"] == pytest.approx(duration_sec, abs=0.2)
    assert result.engine_meta["rtf"] is not None
    assert result.engine_meta["load_sec"] > 0

    if shutil.which("espeak-ng"):
        joined = " ".join(s.text.lower() for s in result.segments)
        assert "fox" in joined or "dog" in joined

    get_settings.cache_clear()
