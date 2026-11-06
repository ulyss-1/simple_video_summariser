"""Backend container (network on): metadata (+ audio unless --no-audio) for one video, via the repo's adapters.
Usage: p1_fetch.py VIDEO_ID AUDIO_DIR OUT_JSON [--no-audio]. Prints one JSON line."""
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "/w")
from adapters.youtube.audio import YouTubeAudio  # noqa: E402
from adapters.youtube.metadata import YouTubeMetadata  # noqa: E402

vid, adir, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
meta = YouTubeMetadata().fetch(vid)
m = dataclasses.asdict(meta)
for k in ("published_at", "release_at"):
    if m[k] is not None:
        m[k] = m[k].isoformat()
m["manual_subtitle_langs"] = list(m["manual_subtitle_langs"])
m["auto_caption_langs"] = list(m["auto_caption_langs"])
res = {"video_id": vid, "meta": m}
if "--no-audio" not in sys.argv:
    res["audio"] = dataclasses.asdict(YouTubeAudio().fetch_normalized(vid, adir))
out.write_text(json.dumps(res))
print(json.dumps({"video_id": vid, "audio": res.get("audio")}))
