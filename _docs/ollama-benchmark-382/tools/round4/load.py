"""Load frozen transcripts into the throwaway DB with the repo's own functions; write manifest + golden.txt."""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from common.config import get_settings
from common.db import connect
from common.models import Segment, VideoMeta
from common.repo.videos import upsert_video
from services.transcriber.persist import save_transcript_with_chunks
from adapters.youtube.subtitles import speaker_source
from adapters.summarize.ollama import estimate_tokens

B = Path(sys.argv[1])
ids = sys.argv[2:]
settings = get_settings()
conn = connect()
mpath = B / "manifest.json"
manifest = json.loads(mpath.read_text()) if mpath.exists() else []
have = {m["video_id"] for m in manifest}
ids = [v for v in ids if v not in have]
for vid in ids:
    mj = json.loads((B / "frozen" / f"{vid}.meta.json").read_text())
    m, info = mj["meta"], mj["transcript"]
    segs_raw = json.loads((B / "frozen" / f"{vid}.segments.json").read_text())
    segs = [Segment(**s) for s in segs_raw]
    for k in ("published_at", "release_at"):
        if m.get(k):
            m[k] = datetime.fromisoformat(m[k])
    m["manual_subtitle_langs"] = tuple(m["manual_subtitle_langs"])
    m["auto_caption_langs"] = tuple(m["auto_caption_langs"])
    meta = VideoMeta(**m)
    upsert_video(conn, meta, "adhoc")
    conn.commit()
    tid = save_transcript_with_chunks(
        conn, settings, vid,
        source=("whisper" if info["kind"] == "whisper" else f"youtube_{info['kind']}"), language=info["lang"],
        speaker_source=("none" if info["kind"] == "whisper" else speaker_source(segs)), segments=segs,
        engine_meta=(info.get("engine_meta") or {}) if info["kind"] == "whisper" else {"track": info["lang"], "kind": info["kind"]},
    )
    conn.commit()
    sha = {
        f: hashlib.sha256((B / "frozen" / f"{vid}.{f}.json").read_bytes()).hexdigest()
        for f in ("meta", "segments")
    }
    manifest.append({
        "video_id": vid, "title": meta.title, "channel_id": meta.channel_id,
        "duration_sec": meta.duration_sec, "transcript_source": ("whisper" if info["kind"] == "whisper" else f"youtube_{info['kind']}"),
        "lang": info["lang"], "segments": info["segments"], "chars": info["chars"],
        "est_tokens": info["est_tokens"], "sha256": sha, "transcript_id": tid,
    })
    print("loaded", vid, tid)
(B / "manifest.json").write_text(json.dumps(manifest, indent=1))
(B / "golden.txt").write_text("\n".join(m["video_id"] for m in manifest) + "\n")
conn.close()
