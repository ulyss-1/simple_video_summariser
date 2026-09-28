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
| `empty_channel.json` | **SYNTHETIC, not a recording.** Kept only to test the parser on the JSON-with-zero-entries shape (`entries: []`, `playlist_count: 0`). On 2026-09-28 every real channel with no uploads that was tried (YouTube's "Sports", "Music", "Gaming", "Movies", "News", "Live", "360 Video", "Fashion" and "Learning" channels) gave "The playlist does not exist", never an empty JSON, so this shape may be unreachable in practice. The shape is a guess |

Error stderr is real and lives in `tests/fixtures/ytdlp_errors/cases.toml`
(yt-dlp 2026.08.19, 2026-09-28):

- Made-up channel ID `UCaaaaaaaaaaaaaaaaaaaaaa`: "The playlist does not exist."
- Terminated channel `UCx7T6qYK4VaP2-OhorrFS3Q` ("phaumann", taken from yt-dlp
  issue #9367), uploads playlist: the same line, so a terminated channel maps to
  `PermanentSourceError(REMOVED)` by the same pattern. Its channel URL says "This
  channel was removed because it violated our Community Guidelines." instead, but
  the catalog never requests that URL, so it is not a case.
- Real channel without uploads (`UCEgdi0XIXXZ-qJOFPf4JSKw`, "Sports"): the same
  line again, so it is indistinguishable from a missing channel.
