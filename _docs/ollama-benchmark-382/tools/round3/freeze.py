"""Freeze one video into frozen/<id>.{meta,segments}.json (same format as earlier rounds).
Usage (via run.sh): freeze.py ROOT VIDEO_ID FETCH_JSON SEGMENTS_SRC KIND   (KIND: whisper_file | live_api)"""
import dataclasses
import json
import sys
from pathlib import Path

from adapters.summarize.ollama import estimate_tokens
from common.models import Segment

R, vid, fetch_json, src, kind = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
fetch = json.loads(fetch_json.read_text())
doc = json.loads(src.read_text())
if kind == "live_api":
    segs = [Segment(start=s["start"], end=s["end"], text=s["text"], speaker=s["speaker"]) for s in doc["segments"]]
    eng = {"exported_from": "live API GET /api/videos/<id>/transcript (read-only)", "source": doc["source"]}
else:
    segs = [Segment(**s) for s in doc["segments"]]
    eng = doc["engine_meta"]
text = "\n".join(s.text for s in segs)
info = {"kind": "whisper", "lang": doc["language"], "speaker_source": "none", "segments": len(segs),
        "chars": len(text), "est_tokens": estimate_tokens(text), "engine_meta": eng}
(R / "frozen" / f"{vid}.meta.json").write_text(json.dumps({"meta": fetch["meta"], "transcript": info}, indent=1))
(R / "frozen" / f"{vid}.segments.json").write_text(json.dumps([dataclasses.asdict(s) for s in segs]))
print(json.dumps({"video_id": vid, **{k: v for k, v in info.items() if k != "engine_meta"}}))
