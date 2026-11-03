"""Runs inside the throwaway Whisper container: transcribe a real speech file again and again.

Uses the live transcriber's own FasterWhisperTranscriber with `cancelled` set, so the work runs in a child
process exactly as in the live worker (model loaded per job, like the live stack). Stays busy until the
container is stopped. One JSON line per iteration to stdout (docker logs).
Env: AUDIO_DIR (mount), AUDIO_REL, AUDIO_BYTES, AUDIO_SEC, WHISPER_MODEL, WHISPER_COMPUTE, WHISPER_THREADS.
"""
import json
import os
import time

from adapters.transcription.faster_whisper import FasterWhisperTranscriber
from common.models import AudioRef

ref = AudioRef(rel_path=os.environ["AUDIO_REL"], bytes=int(os.environ["AUDIO_BYTES"]),
               duration_sec=float(os.environ["AUDIO_SEC"]))
t = FasterWhisperTranscriber(model=os.environ["WHISPER_MODEL"], compute_type=os.environ["WHISPER_COMPUTE"],
                             threads=int(os.environ["WHISPER_THREADS"]))
i = 0
while True:
    i += 1
    t0 = time.time()
    try:
        r = t.transcribe(ref, language="en", cancelled=lambda: False)
        rec = {"iter": i, "start": t0, "wall": round(time.time() - t0, 1), "segments": len(r.segments),
               "meta": {k: r.engine_meta.get(k) for k in ("load_sec", "transcribe_sec", "rtf", "threads")}}
    except Exception as exc:  # noqa: BLE001
        rec = {"iter": i, "start": t0, "wall": round(time.time() - t0, 1), "error": f"{type(exc).__name__}: {exc}"[:300]}
        time.sleep(5)
    print(json.dumps(rec), flush=True)
