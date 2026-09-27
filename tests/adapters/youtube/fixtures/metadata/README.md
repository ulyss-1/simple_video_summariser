# yt-dlp `--dump-json` fixtures

Used by `tests/adapters/youtube/test_metadata.py` (issue #16).

Recorded on 2026-09-27 with yt-dlp 2026.08.19 (`python -m yt_dlp`, the pin in
`requirements.backend.txt`), always from the full watch URL:

```
python -m yt_dlp --dump-json --skip-download --no-playlist --no-warnings \
    "https://www.youtube.com/watch?v=<id>"
```

The upcoming premiere cannot be dumped with those flags alone: yt-dlp exits 1
with `ERROR: [youtube] WI4__7z_dJw: Premieres in 12 hours`. It was recorded with
`--ignore-no-formats-error` added, which is what the adapter does on its second
try.

| File | Case | Source |
|---|---|---|
| `manual_en.json` | normal video, manual English subtitles (64 manual languages) | https://www.youtube.com/watch?v=iG9CE55wbtY (Ken Robinson, "Do schools kill creativity?", TED) |
| `auto_only.json` | auto captions only, no manual subtitles | https://www.youtube.com/watch?v=zjkBMFhNj_g (Andrej Karpathy, "[1hr Talk] Intro to Large Language Models") |
| `no_captions.json` | no subtitles and no auto captions | https://www.youtube.com/watch?v=BZiu46G4ukc (LuxDalet, "Clouds Timelapse - 1 Hour No Audio") |
| `short.json` | a Short (`media_type: short`, 32 s) | https://www.youtube.com/watch?v=VV_JW4iCni0 (NASA, "NASA Moon Base Update (Aug. 4, 2026)") |
| `upcoming_premiere.json` | upcoming premiere: `live_status: is_upcoming`, no `duration`, `live_chat` listed under `subtitles` | https://www.youtube.com/watch?v=WI4__7z_dJw (HYBE LABELS, BOYNEXTDOOR 'ANIMAL' MV, premiering 2026-09-28) |
| `non_english_translated_en.json` | Ukrainian video (`language: uk`); auto captions are `uk-orig` plus translations, `en` among them (`lang=uk&tlang=en`) | https://www.youtube.com/watch?v=pnMM6MwpaTc (Кузьма Скрябін, "Неділя з Кварталом", 1+1) |

## Trimming

Each dump is otherwise as yt-dlp wrote it. Only these fields were cut:

- `formats`, `thumbnails`, `heatmap` are emptied to `[]`; `requested_formats`,
  `requested_subtitles` and `requested_downloads` are removed. The parser reads
  none of them, and format URLs embed the recording machine's IP address.
- `subtitles` and `automatic_captions` keep every language key, but each
  language's list of tracks is cut to one entry: the `vtt` track, as
  `{"ext", "name", "url"}`. The `url` keeps only the `v`, `kind`, `lang`,
  `tlang` and `fmt` query parameters of the `www.youtube.com/api/timedtext`
  URL (signatures and expiry dropped); a track served only through a
  `googlevideo.com` manifest has no `url`.

Nothing was added or reworded. To record a new case, run the command above,
apply the same cuts, and check the result has no `googlevideo.com`,
`signature` or `ip=` left in it.
