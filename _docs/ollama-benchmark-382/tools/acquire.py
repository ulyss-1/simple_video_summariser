"""Acquire + freeze one video (runs in a throwaway ytdigest-backend container)."""
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "/w")
from adapters.summarize.ollama import estimate_tokens  # noqa: E402
from adapters.youtube.metadata import YouTubeMetadata  # noqa: E402
from adapters.youtube.subtitles import YouTubeSubtitles, speaker_source  # noqa: E402

vid, out = sys.argv[1], Path(sys.argv[2])
meta = YouTubeMetadata().fetch(vid)
subs = YouTubeSubtitles()
av = subs.available(meta)
if av.manual:
    kind, lang = "manual", av.manual
elif av.auto:
    kind, lang = "auto", av.auto
else:
    print(json.dumps({"video_id": vid, "error": "no captions"}))
    sys.exit(3)
segs = subs.fetch(vid, lang, kind)
m = dataclasses.asdict(meta)
for k in ("published_at", "release_at"):
    if m[k] is not None:
        m[k] = m[k].isoformat()
m["manual_subtitle_langs"] = list(m["manual_subtitle_langs"])
m["auto_caption_langs"] = list(m["auto_caption_langs"])
text = "\n".join(s.text for s in segs)
info = {
    "kind": kind,
    "lang": lang,
    "speaker_source": speaker_source(segs),
    "segments": len(segs),
    "chars": len(text),
    "est_tokens": estimate_tokens(text),
}
(out / f"{vid}.meta.json").write_text(json.dumps({"meta": m, "transcript": info}, indent=1))
(out / f"{vid}.segments.json").write_text(json.dumps([dataclasses.asdict(s) for s in segs]))
print(json.dumps({"video_id": vid, **info}))
