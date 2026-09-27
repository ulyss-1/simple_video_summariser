# ytdigest

ytdigest produces structured, queryable summaries of talking-head, podcast and
interview videos from YouTube. You can submit a single video URL, or subscribe
to channels so that new uploads are fetched, transcribed (from subtitles, or
with Whisper when there are none), summarized by an LLM and stored in Postgres,
where earlier analyses can be retrieved and compared. For setup, commands and
contribution rules see [AGENTS.md](AGENTS.md); the design is in
[`_docs/architecture.md`](_docs/architecture.md).
