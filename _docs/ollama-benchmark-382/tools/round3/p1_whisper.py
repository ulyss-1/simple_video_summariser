"""Whisper container (--network none): transcribe one local audio file with the repo's FasterWhisperTranscriber
(child-process mode, as the live worker). Env: AUDIO_REL, AUDIO_BYTES, AUDIO_SEC, OUT, WHISPER_*."""
import json
import os
import time

from adapters.transcription.faster_whisper import FasterWhisperTranscriber
from common.models import AudioRef

ref = AudioRef(rel_path=os.environ["AUDIO_REL"], bytes=int(os.environ["AUDIO_BYTES"]),
               duration_sec=float(os.environ["AUDIO_SEC"]))
t = FasterWhisperTranscriber(model=os.environ["WHISPER_MODEL"], compute_type=os.environ["WHISPER_COMPUTE"],
                             threads=int(os.environ["WHISPER_THREADS"]))
t0 = time.time()
last = [0.0]


def prog(done: float) -> None:
    if time.time() - last[0] > 60:
        last[0] = time.time()
        print(json.dumps({"progress_sec": done, "elapsed": round(time.time() - t0)}), flush=True)


r = t.transcribe(ref, language="en", on_progress=prog, cancelled=lambda: False)
doc = {"language": r.language, "engine_meta": r.engine_meta, "wall_sec": round(time.time() - t0, 1),
       "segments": [{"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in r.segments]}
tmp = os.environ["OUT"] + ".tmp"
open(tmp, "w").write(json.dumps(doc))
os.replace(tmp, os.environ["OUT"])
print(json.dumps({"done": True, "segments": len(r.segments), "wall": doc["wall_sec"],
                  "meta": {k: r.engine_meta.get(k) for k in ("load_sec", "transcribe_sec", "rtf", "threads", "model", "compute_type")}}),
      flush=True)
