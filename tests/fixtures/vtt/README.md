# WebVTT fixtures

Used by `tests/adapters/youtube/test_vtt.py` (issue #17).

## Real YouTube files

Captured with yt-dlp 2026.08.19 (`python -m yt_dlp`) on 2026-09-27, then
trimmed to the header plus every cue starting before 00:02:00. Nothing else
was edited: line endings, cue settings and inline markup are as YouTube
served them.

| File | Kind | Source |
|---|---|---|
| `youtube_manual_iG9CE55wbtY.en.vtt` | manual English subtitles | https://www.youtube.com/watch?v=iG9CE55wbtY (Ken Robinson, "Do schools kill creativity?", TED) |
| `youtube_auto_zjkBMFhNj_g.en.vtt` | English auto captions (the video has no manual subtitles) | https://www.youtube.com/watch?v=zjkBMFhNj_g (Andrej Karpathy, "[1hr Talk] Intro to Large Language Models") |

Commands:

```
python -m yt_dlp --skip-download --write-subs --sub-langs en --sub-format vtt \
    "https://www.youtube.com/watch?v=iG9CE55wbtY"
python -m yt_dlp --skip-download --write-auto-subs --sub-langs en --sub-format vtt \
    "https://www.youtube.com/watch?v=zjkBMFhNj_g"
```

## Hand-written malformed cases

- `malformed_timestamps.vtt` - bad arrow, non-numeric time, seconds out of
  range and `end < start`, between two good cues
- `truncated_in_timing.vtt` - file cut inside a timing line
- `truncated_in_text.vtt` - file cut inside a cue's text
- `header_only.vtt` - header block with no cues
- `not_webvtt.srt` - an SRT file, which must be rejected
