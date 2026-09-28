# Channel catalog fixtures

Used by `tests/adapters/youtube/test_catalog.py` (issue #27).

Each JSON file carries a `_fixture` key naming how it was made; the parser
ignores it. The files are yt-dlp `--flat-playlist --dump-single-json` output
trimmed to `id`, `playlist_count` and, per entry, `id`, `title`, `duration`
(everything else, thumbnails included, was removed).

| File | Origin |
|---|---|
| `small_channel_full.json` | **Real recording**, yt-dlp 2026.08.19, 2026-09-28: Andrej Karpathy's uploads playlist `UUXUPKJO5MZQN11PqgIvyuvQ`, no `--playlist-end`. 17 entries, `playlist_count` 17 |
| `small_channel_end5.json` | **Real recording**, same channel, date and version, with `--playlist-end 5`: 5 entries while `playlist_count` still says 17 |
| `empty_channel.json` | **NOT a recording. Hand-made.** No empty-but-existing uploads playlist could be found to record. The shape (`entries: []`, `playlist_count: 0`) is a guess; replace it with a real recording if one turns up |

Error stderr for a missing channel is real and lives in
`tests/fixtures/ytdlp_errors/cases.toml`. For a channel with no uploads yt-dlp
2026.08.19 says "The playlist does not exist" (verified live on YouTube's
"Sports" channel), so it is indistinguishable from a missing channel and maps to
`PermanentSourceError(REMOVED)`. A terminated channel could not be provoked from
here, so no terminated case is recorded.
