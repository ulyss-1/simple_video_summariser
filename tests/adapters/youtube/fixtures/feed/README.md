# Channel RSS feed fixtures

Used by `tests/adapters/youtube/test_feed.py` (issue #26).

**These files are NOT recordings.** When they were written (2026-09-28) the
endpoint `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>` answered
HTTP 404 (an HTML "Error 404" page) for every channel tried (NASA, TED, HYBE
LABELS, LuxDalet, Karpathy, ...) from the dev machine, including on repeated
tries, so no real feed could be recorded. The files reproduce the structure of
a real feed: Atom root with the `yt` and `media` namespaces, feed-level
`link`/`id`/`yt:channelId`/`title`/`author`/`published`, then one `<entry>` per
video with `id`, `yt:videoId`, `yt:channelId`, `title`, `link`, `author`,
`published`, `updated` and `media:group`. Replace them with real recordings
(`curl -A "<user agent>" "<url>"`, fixture names kept) as soon as the endpoint
answers; the tests read only the fields the adapter maps.

| File | Case | Channel |
|---|---|---|
| `nasa_with_short.xml` | first entry is a Short (`/shorts/` link); a title with `&amp;`; entries newest first | `UCLA_DiR1FfKNvjuUpBHmylQ` (NASA). `VV_JW4iCni0` and its title are the real Short from `fixtures/metadata/short.json`; the other two entries are invented |
| `dash_and_underscore_ids.xml` | video ID starting with `-` (`-wNyEUrxzFU`) and one starting with `_`; a `-07:00` offset; Cyrillic/CJK/emoji title | `UCFhiqk0Hh-xJvGQLyTc-giw` (LuxDalet). All three entries are hand-edited: the `-` and `_` IDs, the `-07:00` offset and titles are made up; `BZiu46G4ukc` is real |
