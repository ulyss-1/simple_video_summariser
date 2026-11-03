"""Download + normalise one video's audio with the repo's YouTubeAudio (runs in a throwaway ytdigest-backend container).
Usage: dl_audio.py VIDEO_ID OUT_DIR   -> OUT_DIR/<id[:2]>/<id>.opus and prints AudioRef as JSON."""
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "/w")
from adapters.youtube.audio import YouTubeAudio  # noqa: E402

ref = YouTubeAudio().fetch_normalized(sys.argv[1], Path(sys.argv[2]))
print(json.dumps(dataclasses.asdict(ref)))
