# Channel RSS feed fixtures

Used by `tests/adapters/youtube/test_feed.py` (issue #26).

## Recorded from the real endpoint

Recorded on 2026-09-28 from the dev machine, unmodified (byte for byte):

    curl -A "Mozilla/5.0" "https://www.youtube.com/feeds/videos.xml?channel_id=<ID>"

| File | Source URL | Notes |
|---|---|---|
| `nasa.xml` | `https://www.youtube.com/feeds/videos.xml?channel_id=UCLA_DiR1FfKNvjuUpBHmylQ` | NASA, 15 entries; video IDs starting with `_` and `-`; typographic apostrophe in titles |
| `the_verge.xml` | `https://www.youtube.com/feeds/videos.xml?channel_id=UCddiUEpeqJcYeBxX1IVBKvQ` | The Verge, 15 entries, most are Shorts (`/shorts/` link, not filtered); a title with `&quot;` |

What the real data shows, and the adapter relies on: the feed-level
`yt:channelId` has NO `UC` prefix, but every entry's `yt:channelId` has it;
all `published` timestamps use the `+00:00` offset. The endpoint also answers
404 intermittently for valid channels (hence `FeedNotFoundError`).

The feed for other channels tried (MrBeast, PewDiePie, Kurzgesagt, Marques
Brownlee) also used `+00:00` only, so no real feed with another offset was
found.

## Synthetic (not recordings)

Cases real data cannot give are built in memory in `test_feed.py`
(`entry_xml`/`feed_xml`): a `-07:00`/`+02:30` offset, missing or unparseable
`published`, wrong or bare entry channel ID, malformed XML, DOCTYPE/ENTITY,
oversize body, invalid video IDs, duplicates. These are all synthetic.
