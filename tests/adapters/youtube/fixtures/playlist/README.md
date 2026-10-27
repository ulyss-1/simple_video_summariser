# Playlist fixtures

Used by `tests/adapters/youtube/test_playlist.py` (issue #342).

Each JSON file carries a `_fixture` key naming how it was made (the parser
ignores it). The files are yt-dlp `--flat-playlist --dump-single-json` output
(`--no-warnings`, no cookies, no credentials) trimmed to `id`, `title`,
`playlist_count` and, per entry, `id`, `title`, `duration`. Recorded on
2026-10-27 with yt-dlp 2026.10.21 (the version pinned in
`requirements.backend.txt`), against public playlists only: Andrej Karpathy's
course list and playlists yt-dlp's own extractor tests use.

| File | Origin |
|---|---|
| `normal_playlist.json` | **Real recording**: "Neural Networks: Zero to Hero", `PLAqhIrjkxbuWI23v9cThsA9GvCAUhRvKZ`, no `--playlist-end`. 10 entries, `playlist_count` 10 |
| `normal_playlist_end3.json` | **Real recording**, same playlist, with `--playlist-end 3`: 3 entries while `playlist_count` still says 10 |
| `private_and_deleted.json` | **Real recording, entries 0-69 kept** of a 162-entry playlist (`PLYwq8WOe86_xGmR7FrcJq8Sb7VW8K3Tt2`, a yt-dlp test URL), so `playlist_count` still says 162. Holds `[Private video]` and `[Deleted video]` entries: yt-dlp lists them with that title, a real 11-character ID and no duration or channel |
| `duplicate_entries.json` | **Real recording, trimmed** after the second occurrence of a repeated video (`MxMrnsxX9ZA`) in a 96-entry playlist (`PLwP_SiAcdui0KVebT0mU9Apz359a4ubsC`, a yt-dlp test URL "with deleted videos"); `playlist_count` still says 96 |
| `empty_playlist.json` | **SYNTHETIC, not a recording.** No public empty playlist could be found or made without an account; the shape (`entries: []`, `playlist_count: 0`) is a guess at what yt-dlp prints, like `catalog/empty_channel.json` |

Error stderr is real and lives in `tests/fixtures/ytdlp_errors/cases.toml`
(yt-dlp 2026.10.21, 2026-10-27):

- Well-formed but unused ID `PL0123456789ABCDEF`: "The playlist does not exist."
- yt-dlp's own private-playlist test URL `PLtPgu7CB4gbY9oDN3drwC3cMbJggS7dKl`:
  the same line, so a private playlist and a missing one are indistinguishable.
- `PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` (right shape, not an ID YouTube
  accepts): `HTTP Error 400`, which maps to a tool failure (502), not a
  missing playlist.

## ID bounds (confirmed)

Every recorded real ID is `PL` plus 32 characters (34 in all), inside the
12-64 range of `PLAYLIST_ID_PATTERN`; `PL0123456789ABCDEF` (18, the older
16-hex form) is accepted too, and YouTube answered it with a clean "does not
exist". No change to the issue's bounds was needed.
