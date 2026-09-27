# Progress

Status of the groomed backlog (GitHub issues #1–#20), worked through per
`_docs/process.md`. An issue is closed only after a QA PASS comment.

## Checkpoint 1 — 2026-09-27, 5 of 20 closed

| Issue | Title | Commits | QA |
|---|---|---|---|
| #1 | Project skeleton | 4de9e43 | PASS (2nd run, after `python3.14-venv` was installed) |
| #2 | Subtitle coverage script | 6cf2d4a, b5aab6b | PASS |
| #4 | faster-whisper wheels on 3.14 | a722cd7 | PASS |
| #5 | Configuration module | ebd0856 | PASS |
| #15 | Error taxonomy | f8afd58 | PASS |

Dependencies added (owner-approved): `yt-dlp==2026.8.19`,
`pydantic-settings==2.15.0`, `structlog==26.1.0` (with #6, in QA).

### Phase 0 findings

- **O7 resolved (#4):** faster-whisper 1.2.1 and its whole tree (ctranslate2
  4.8.2, av 18.1.0, tokenizers 0.23.2, onnxruntime 1.30.0) install from wheels
  on `python:3.14-slim`; no 3.13 transcriber image needed. The single
  `--platform manylinux_2_28_x86_64` download check gives a false negative;
  see architecture §16.2.
- **Subtitle coverage (#2),** 9 owner-chosen channels, 15 RSS entries each:
  manual 6 videos (1.95 h), auto-only 64 (48.72 h), none 0, error 2
  (transient), 63 Shorts excluded. Speech-to-text load is ~0 by default and
  ~48.7 h per this window with `PREFER_WHISPER=1`. Input for #64.

### In flight

- #6 structured logging — committed 2dd0695, in QA
- #17 WebVTT parser — QA FAIL (leading-zero numeric entity raises
  `ValueError`; whitespace-only input returns `[]`), fix in progress
- #16 metadata adapter, #7 database bootstrap — in implementation

### Follow-ups filed (not groomed yet)

- #69 map yt-dlp "rate-limited by YouTube" to `RATE_LIMITED`
- #70 config secrets leak via `ValidationError.errors()`; TID251 rule untested

### Blocked or owner-dependent

- #3 bake-off must run on the deploy host with real clips
- #20 speech-to-text adapter: unblocked by #4; also check #64 first
