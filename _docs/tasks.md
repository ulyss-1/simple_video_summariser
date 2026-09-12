# ytdigest — Task Backlog

**System in one paragraph:** ytdigest ingests YouTube podcast/interview videos,
obtains a transcript (YouTube subtitles when available, local speech-to-text
otherwise), sends the transcript to an LLM in ~15-minute chunks, and stores a
short summary plus structured topics, claims, and quotes in PostgreSQL. A
scheduler polls subscribed channels for new videos. A React SPA is the reading
interface. Everything runs in Docker Compose on one Linux host.

**How to use this backlog.** Tasks are ordered so that dependencies generally
come earlier, but each is written to be picked up without reading the other
documents. Where a task needs a table name, interface shape, or config key, it
is stated inline. Tasks 2–9 are foundational and should be done in order;
after that, adapters (10–22), services (23–31), API (32–40), frontend (41–48),
and deployment/ops (49–56) can largely proceed in parallel.

**Conventions assumed throughout:** Python 3.14, PostgreSQL 18, FastAPI,
psycopg3, Alembic, pytest. Frontend is React 19 + Vite 8 + TypeScript. Package
layout is `common/` (shared library), `adapters/` (outbound integrations),
`services/` (runnable entrypoints), `web/` (frontend), `migrations/`, `ops/`.

---

# Phase 0 — Measurement

These three answer open questions that could change later design choices. They
are throwaway scripts, not production code, and should be done first.

---
## 1. Project skeleton with a passing test
Goal: An empty but runnable project with tooling wired up and one test that passes.
Description: Create the repository structure (`common/`, `adapters/`, `services/`, `migrations/`, `ops/`, `tests/`), a `pyproject.toml` targeting Python 3.14, and a virtualenv setup documented in the README. Add pytest, ruff, and mypy as dev dependencies with minimal configuration, plus a single trivial test (e.g. asserting a version constant) that passes via `pytest`. The exit criterion is that a fresh clone can run `pip install -e ".[dev]" && pytest && ruff check .` successfully.
---

---
## 2. Subtitle coverage measurement script
Goal: Determine what fraction of target channels' videos already have usable subtitles.
Description: Write a standalone script that takes a list of YouTube channel IDs, fetches each channel's recent videos from its RSS feed at `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`, and for each video runs `yt-dlp --dump-json` to check whether English subtitles exist and whether they are manually authored or auto-generated. Output a per-channel and total summary of manual / auto / none. This number decides how often the expensive speech-to-text path will actually run.
---

---
## 3. Transcription engine bake-off harness
Goal: Measure speed and accuracy of three speech-to-text options on this specific CPU.
Description: Write a script that downloads ~5 minutes of audio from a given YouTube video, normalizes it to 16 kHz mono, and transcribes it with each of `faster-whisper large-v3` (int8), `faster-whisper turbo`, and a Parakeet CPU implementation. Report for each: real-time factor (processing seconds ÷ audio seconds), peak memory, and the raw text for manual accuracy comparison. Pay particular attention to proper nouns and technical terms, which are what make a summary useful.
---

---
## 4. Verify ctranslate2 wheel availability for Python 3.14
Goal: Confirm the speech-to-text dependency installs on the target Python version.
Description: `ctranslate2` (used by `faster-whisper`) ships binary wheels with no source distribution, and has historically lagged new CPython releases — meaning a missing wheel is a hard install failure. Check whether a `cp314` wheel exists via `pip download ctranslate2 --only-binary=:all: --python-version 3.14 --no-deps`. Document the result; if absent, note that the speech-to-text container will need to be built on Python 3.13 independently of the other services.
---

# Foundation

---
## 5. Configuration module
Goal: One typed, validated source of configuration for all services.
Description: Implement `common/config.py` using pydantic-settings, exposing a single settings object read from environment variables. Cover at minimum: `DATABASE_URL`, `CHUNK_SEC` (default 900), `OVERLAP_SEC` (default 60), `PROMPT_VERSION` (default `v1`), `SUMMARIZER` (`ollama`|`anthropic`), `WHISPER_MODEL`, `AUDIO_TTL_DAYS` (30), `AUDIO_MAX_GB` (20), `POLL_INTERVAL_SEC` (3600), `HEARTBEAT_SEC` (60), and `LOG_LEVEL`. Services must import this rather than reading `os.environ` directly; add tests for defaults and for required-value failures.
---

---
## 6. Structured logging setup
Goal: Machine-readable logs with consistent correlation fields.
Description: Implement `common/logging.py` configuring JSON-formatted structured logging (structlog or stdlib equivalent), with a helper to bind contextual fields that then appear on every subsequent log line. The fields that matter downstream are `job_id`, `video_id`, `kind`, and `attempt`. Include a test asserting that bound context appears in emitted records.
---

---
## 7. Database bootstrap and migration tooling
Goal: A running PostgreSQL instance and a working migration workflow.
Description: Add a minimal `compose.yml` with a `postgres:18-alpine` service (named volume, healthcheck, no published port), and initialize Alembic against it with a `DATABASE_URL` from the config module. Create one trivial migration to prove `alembic upgrade head` and `downgrade` both work. Migrations must be runnable as a standalone command, never automatically on application startup, because concurrent service replicas racing the same migration can corrupt the schema.
---

---
## 8. Core schema migration
Goal: Tables for channels, videos, transcripts, chunks, and analyses.
Description: Write an Alembic migration creating: `channels` (channel_id PK, title, active, monitor_from, last_polled), `videos` (video_id PK, channel_id FK, title, duration_sec, published_at, description, origin, unavailable), `transcripts` (id, video_id FK, source, language, speaker_source, segments JSONB, full_text, engine_meta, UNIQUE(video_id, source)), `transcript_chunks` (id, transcript_id FK, seq, start_sec, end_sec, text, chunk_strategy, UNIQUE(transcript_id, chunk_strategy, seq)), `analyses` (id, video_id FK, transcript_id FK, chunk_strategy, model, prompt_version, tldr, speaker_roster JSONB, input_tokens, output_tokens, created_at), and child tables `topics`, `claims`, `quotes` each referencing `analyses(id)` ON DELETE CASCADE. Add a generated `STORED` tsvector column on `transcripts.full_text` with a GIN index — it must be STORED, since GIN cannot index a virtual generated column.
---

---
## 9. Jobs table migration
Goal: The queue table, with correct uniqueness semantics.
Description: Write a migration creating `jobs` with columns: id, video_id, kind, dedupe_key (default `'default'`), state (`pending|running|done|dead`), priority, payload JSONB, attempts, last_error, error_class, run_after, locked_by, locked_at, heartbeat_at, finished_at, created_at. Add a **partial** unique index on `(video_id, kind, dedupe_key) WHERE state IN ('pending','running')` — scoping it to active states is essential, because a full unique constraint would permanently block re-running an analysis after the first one completes. Add supporting partial indexes for claiming (`kind, priority DESC, run_after` where pending) and reaping (`heartbeat_at` where running).
---

---
## 10. Media table migration
Goal: Track retained audio files and their expiry.
Description: Write a migration creating `media` (id, video_id FK, path, bytes, format, created_at, expires_at, UNIQUE(video_id, format)), with indexes on `expires_at` and `created_at`. Audio files live on a filesystem volume and are never stored in the database; this table records where they are and when they may be deleted. The `created_at` index supports oldest-first eviction when a disk cap is exceeded.
---

---
## 11. Job queue implementation
Goal: A correct, concurrency-safe queue backed by PostgreSQL.
Description: Implement `common/queue.py` with a `JobQueue` interface and a PostgreSQL adapter. `enqueue(kind, video_id, dedupe_key, payload, priority, run_after)` inserts idempotently; `claim(kinds)` is a context manager that atomically selects one pending job using `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)` ordered by `priority DESC, run_after`, marks it done on clean exit, and reschedules with exponential backoff on exception. Also provide `heartbeat(job_id)` and a `reap_stale()` that returns jobs to pending when their heartbeat is older than a threshold.
---

---
## 12. Job queue tests
Goal: Prove the queue behaves correctly under concurrency and failure.
Description: Write integration tests against a real PostgreSQL instance (testcontainers or a disposable compose service) covering: two concurrent workers never claim the same job; enqueueing a duplicate while one is pending is a no-op; enqueueing the same kind again *after* completion succeeds; a raised exception reschedules with increased attempts and a future `run_after`; exceeding max attempts moves the job to `dead`; and `reap_stale` returns a job whose heartbeat has gone stale without touching one that is still beating.
---

---
## 13. Worker runtime
Goal: A reusable claim-loop that handlers plug into.
Description: Implement `common/worker.py` providing a `Worker` class that takes a name, a list of job kinds, and a handler function, then loops: claim a job, run the handler, sleep when idle. It must send a heartbeat every 60 seconds during long-running handlers (some jobs legitimately run for hours), touch a `/tmp/heartbeat` file each iteration for container liveness checks, and handle SIGTERM by finishing the current job before exiting rather than abandoning it mid-flight.
---

---
## 14. Repository layer
Goal: Typed data access so services never write raw SQL.
Description: Implement `common/repo/` with one module per aggregate (channels, videos, transcripts, analyses), exposing functions like `upsert_video`, `save_transcript`, `get_best_transcript`, `save_analysis`, `latest_analysis`, and `search`. Include a SQL helper that ranks transcript sources in preference order — manually authored subtitles first, then machine transcription, then auto-generated captions last — so this ordering is defined in exactly one place rather than reimplemented by each caller.
---

---
## 15. Error taxonomy
Goal: Classify failures so retries are proportionate to cause.
Description: Implement `common/errors.py` defining error classes and a `classify(exception) -> str` function mapping real failure signatures to categories: `PERMANENT_SOURCE` (video removed, private, geo-blocked, age-gated — never retry), `TRANSIENT_NETWORK`, `RATE_LIMITED`, `TOOL_FAILURE` (downloader extractor broken), `LLM_INVALID_OUTPUT`, `LLM_UNAVAILABLE`, `RESOURCE` (disk full, OOM — retrying makes it worse), and `BUG`. This matters most for speech-to-text jobs, where a blind retry costs hours of CPU; a removed video must fail fast rather than burn four attempts.
---

# Adapters

---
## 16. YouTube metadata adapter
Goal: Fetch structured metadata for a video ID.
Description: Implement `adapters/youtube/metadata.py` wrapping `yt-dlp --dump-json` to return a typed object with video_id, channel_id, title, duration_sec, upload date, description, and the lists of available manual and automatic subtitle languages. Map failures through the error classifier so that an unavailable video is distinguishable from a network blip. Tests should use recorded JSON fixtures rather than live network calls.
---

---
## 17. WebVTT parser
Goal: Turn subtitle files into timestamped segments.
Description: Implement a pure function that parses WebVTT content into a list of `(start_sec, end_sec, text)` segments, stripping inline markup tags. YouTube auto-generated captions use a rolling display style that emits each phrase repeatedly across consecutive cues, so consecutive duplicate text must be collapsed. Test against fixture files covering manual subtitles, auto-generated rolling captions, and malformed input.
---

---
## 18. Subtitle download adapter
Goal: Retrieve subtitles for a video when they exist.
Description: Implement `adapters/youtube/subtitles.py` that uses `yt-dlp` to download English subtitles in VTT format to a temp directory, parses them via the VTT parser, and returns segments plus whether the source was manual or auto-generated. Return an empty result rather than raising when no subtitles are published. Manual subtitles sometimes carry speaker labels; preserve them if present.
---

---
## 19. Audio acquisition and normalization
Goal: Produce a compact audio file suitable for speech-to-text.
Description: Implement `adapters/youtube/audio.py` that downloads the best audio stream with `yt-dlp` and converts it via ffmpeg to **16 kHz mono Opus**, returning the path and byte size. Speech-to-text models consume 16 kHz mono anyway, and storing that instead of the original stream costs roughly 7–10 MB/hour rather than 100–200 MB/hour — a 10–20× reduction with no downstream loss.
---

---
## 20. Speech-to-text adapter
Goal: Transcribe an audio file into timestamped segments.
Description: Implement a `Transcriber` interface with a `faster-whisper` implementation running on CPU with int8 quantization, VAD filtering, and a configurable model size. Return segments plus engine metadata (model name, compute type, and the measured real-time factor) so transcription performance can be tracked over time rather than benchmarked once. Load the model lazily so that services which never transcribe do not pay the memory cost.
---

---
## 21. Transcript chunker
Goal: Split a transcript into stable, overlapping analysis windows.
Description: Implement a pure function that divides transcript segments into fixed time windows (default 900 s) each extended backwards by an overlap (default 60 s) to preserve context across boundaries, returning chunks with sequence number and start/end times. Label each chunk set with a strategy string encoding its parameters (e.g. `time:900:60`) so that changing the window size produces a *new* chunk set rather than silently invalidating stored ones. Transcripts shorter than one window produce a single chunk. Property-test that chunks cover the whole transcript and overlaps stay bounded.
---

---
## 22. LLM output schema and validation
Goal: Guarantee the database only ever receives well-formed analysis data.
Description: Define pydantic models for the two LLM response shapes: a speaker roster (`{"speakers": [{"name", "role"}]}`) and a per-chunk analysis (`{"topics": [...], "claims": [...], "quotes": [...]}` with text, speaker, start_sec, and confidence fields). Implement a parser that strips markdown code fences, extracts the JSON object, and validates it. Critically, enforce in code that every `speaker` value is either a name from the supplied roster or the literal `"unknown"` — a prompt alone cannot guarantee a closed set, and a silently invented speaker name is worse than no attribution.
---

---
## 23. Prompt templates v1
Goal: Versioned prompt files, not inline strings.
Description: Create `adapters/summarize/prompts/v1/` containing three prompts as files: a roster prompt (derive participant names and roles from title, description, and the opening transcript), a chunk-analysis prompt (extract topics, specific attributable claims, and verbatim quotes with timestamps), and a reduce prompt (merge chunk-level summaries into one coherent 4–6 sentence episode summary). The prompt directory is selected by a `PROMPT_VERSION` config value. Instruct the model to emit `"unknown"` rather than guess when speaker turn-taking is ambiguous — an honest unknown is more useful than a confident wrong name.
---

---
## 24. Ollama summarizer adapter
Goal: Run analysis against a locally hosted model.
Description: Implement a `Summarizer` with three methods — `derive_roster`, `analyze_chunk`, and `reduce` — calling a local Ollama server's `/api/chat` endpoint with JSON response format and a configurable model and context size. Return token counts from the response so local and cloud runs can be compared on equal terms. Ollama runs on the host rather than in the compose stack, so the host address must be configurable.
---

---
## 25. Anthropic summarizer adapter
Goal: Run analysis against the Claude API, cheaply.
Description: Implement the same three-method `Summarizer` interface against the Anthropic Messages API using an API key from configuration. Because analysis is queued background work with no waiting user, default to the **Batch API**, which costs 50% less than synchronous calls, and keep a synchronous path for user-triggered re-analysis. Mark the system prompt and speaker roster as cacheable, since they repeat across every chunk of an episode.
---

---
## 26. Channel RSS feed adapter
Goal: Discover newly published videos without API quota.
Description: Implement `adapters/youtube/feed.py` fetching and parsing `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`, returning video ID, channel ID, title, and published timestamp for each entry. This feed is free and needs no API key, which is why it is used instead of the YouTube Data API. Note that it returns only roughly the 15 most recent videos — it cannot serve historical catalog retrieval.
---

---
## 27. Channel catalog adapter
Goal: Enumerate a channel's full back catalog on demand.
Description: Implement `adapters/youtube/catalog.py` using `yt-dlp` against a channel's uploads playlist in flat mode to list historical video IDs, supporting a limit and returning a count without fetching full metadata for each. This is a separate code path from the RSS feed, not a parameterization of it, because the feed is capped at recent entries. It exists to support an explicit, bounded backfill request rather than automatic full-history ingestion.
---

# Services and pipeline

---
## 28. Ingest job handler
Goal: Resolve a video's metadata and choose its transcript path.
Description: Implement the handler for `ingest` jobs: fetch metadata, upsert the video row, then decide how to get a transcript. If manually authored subtitles exist, download and store them and enqueue an `analyze` job. Otherwise enqueue a `transcribe` job — auto-generated captions are deliberately *not* preferred over machine transcription, because they lack punctuation and speaker labels, both of which materially degrade interview summaries. Use auto-captions only as a last resort when transcription has already failed permanently, and record which source was used.
---

---
## 29. Transcribe job handler
Goal: Produce a transcript from audio and hand off to analysis.
Description: Implement the handler for `transcribe` jobs: acquire normalized audio, register a `media` row with an expiry timestamp, run speech-to-text with periodic heartbeats (this can run for hours), persist the transcript with engine metadata, generate and store its chunks, then enqueue an `analyze` job. Respect a config flag that deletes the audio immediately instead of retaining it. Limit retries to two attempts rather than the usual four, since each retry costs hours of CPU.
---

---
## 30. Analyze job handler
Goal: Turn a stored transcript into a stored analysis.
Description: Implement the handler for `analyze` jobs: select the best available transcript, load its chunks for the configured strategy (generating them if the strategy has changed), derive a speaker roster once per episode, analyze each chunk with that roster injected as a closed set, then reduce the chunk summaries into one episode summary. Deduplicate claims and quotes across overlapping chunk boundaries by normalized text. Persist the analysis and all child rows in a single transaction, recording model name, prompt version, and token counts.
---

---
## 31. CLI single-video pipeline
Goal: Run the whole pipeline for one URL from the command line.
Description: Build a CLI entrypoint that accepts a YouTube URL or video ID and runs ingest → transcript → chunk → analyze inline, printing progress and the resulting summary. This is the primary development and debugging tool before the API and workers exist, and remains useful afterwards for reproducing a single video's behaviour in isolation. It should share the same handler code as the workers, not duplicate it.
---

---
## 32. Transcriber service entrypoint
Goal: A long-running worker for ingest and transcription jobs.
Description: Wire the ingest and transcribe handlers into the worker runtime as a runnable module, claiming both `ingest` and `transcribe` job kinds. This is the only CPU-bound service and the one that gets scaled horizontally, so it must hold no in-process state beyond the model cache. Verify it survives SIGTERM mid-job and that a `kill -9` leaves a job that the reaper later recovers.
---

---
## 33. Analyzer service entrypoint
Goal: A long-running worker for analysis jobs.
Description: Wire the analyze handler into the worker runtime as a runnable module claiming the `analyze` kind, with the summarizer backend selected by configuration so switching between local and cloud models is a single environment variable. Keep it separate from the transcriber service so a multi-hour transcription never blocks analysis of other videos.
---

---
## 34. Planner: channel polling
Goal: Automatically discover and queue newly published videos.
Description: Implement a periodic task that iterates active channels, fetches each channel's RSS feed, inserts any unseen videos, and enqueues `ingest` jobs for them. Only videos published after the channel's `monitor_from` timestamp are queued, so registering a channel does not trigger ingestion of its entire history. Record the poll time and any error per channel so a persistently failing feed is visible.
---

---
## 35. Planner: stale job reaper
Goal: Recover jobs orphaned by crashed workers.
Description: Implement a periodic task that returns `running` jobs to `pending` when their heartbeat is older than a threshold (default 5 minutes). The check must be based on heartbeat staleness rather than total runtime, because some jobs legitimately run for many hours — a fixed timeout would either kill healthy work or leave dead jobs stranded. Log how many jobs were recovered.
---

---
## 36. Planner: audio retention enforcement
Goal: Keep retained audio bounded by both age and disk usage.
Description: Implement a periodic task that deletes audio files past their expiry timestamp, and independently evicts oldest-first whenever total retained bytes exceed a configured cap. Both limits are needed: a large backfill can land far more audio inside the retention window than the window anticipated, so a date-based rule alone cannot protect the volume. Never delete audio referenced by a job currently in `running` state.
---

---
## 37. Planner: re-analysis sweep
Goal: Re-run analysis across stored transcripts when prompts change.
Description: Implement a periodic task that finds transcripts with no analysis at the current prompt version and enqueues `analyze` jobs for them at low priority. This is what makes prompt iteration cheap — a new prompt version costs one LLM call per video instead of re-downloading and re-transcribing everything. Low priority ensures a sweep across the whole corpus never starves newly published videos.
---

---
## 38. Planner service entrypoint
Goal: One scheduled process running all periodic maintenance.
Description: Combine channel polling, job reaping, audio retention, and the re-analysis sweep into a single runnable service with a configurable interval. This service must run at exactly one replica — its tasks are not safe to run concurrently — so document that constraint clearly and consider wrapping each task in a PostgreSQL advisory lock as defence in depth.
---

# HTTP API

---
## 39. API skeleton
Goal: A running HTTP service with health reporting.
Description: Create a FastAPI application with a `GET /healthz` endpoint reporting liveness plus queue depth grouped by job kind and state. Split routes into separate read, write, and ops modules from the start, and route every request through a single auth dependency that is a no-op placeholder for now — this makes adding authentication later one implementation change rather than an audit of every endpoint. The service must perform no processing itself so it stays responsive while long jobs run.
---

---
## 40. Video submission endpoints
Goal: Let a user queue a specific video or register a channel.
Description: Implement `POST /videos` accepting a YouTube URL or bare video ID (parsing both `youtube.com/watch?v=` and `youtu.be/` forms), creating the video row and enqueueing an `ingest` job at elevated priority since a human is waiting. Implement `POST /channels` registering a channel for monitoring, recording the registration timestamp as the point from which new videos are collected. Both should be idempotent on repeat submission.
---

---
## 41. Channel backfill endpoint
Goal: Deliberately ingest a bounded slice of a channel's history.
Description: Implement `POST /channels/{id}/backfill` accepting a limit and a `dry_run` flag, enumerating historical videos via the channel catalog adapter and enqueueing `ingest` jobs at low priority. With `dry_run` set, return the count and an estimate of the transcription workload without enqueueing anything. This endpoint is the single most expensive operation in the system — a large channel could mean weeks of CPU — so it must report what it is about to do before doing it.
---

---
## 42. Read endpoints
Goal: Expose stored analyses and transcripts over HTTP.
Description: Implement `GET /videos` (paginated, filterable by channel, date, and processing state), `GET /videos/{id}` (latest analysis with its topics, claims, and quotes, or current job status if still processing), `GET /videos/{id}/analyses` (all runs for that video, so different models or prompt versions can be compared), and `GET /videos/{id}/transcript` (paginated segments). Pagination on the transcript endpoint matters because a two-hour episode is thousands of segments.
---

---
## 43. Full-text search endpoint
Goal: Find videos by what was said in them.
Description: Implement `GET /search?q=` querying the stored tsvector index over transcript text using `websearch_to_tsquery`, returning matching videos ranked by relevance with highlighted excerpts via `ts_headline`. Support a result limit and include enough video metadata for the caller to render a result list without a second request.
---

---
## 44. Ops endpoints
Goal: Make queue state and failures inspectable without a database client.
Description: Implement `GET /ops/jobs` listing jobs filterable by state, kind, and error class, and `POST /ops/jobs/{id}/retry` returning a dead job to pending with its attempt counter reset. Without these, a permanently failed video is invisible until someone manually queries the database. Include the stored error message and classification in the listing.
---

---
## 45. Server-side analysis renderer
Goal: Render one analysis as standalone HTML on the server.
Description: Implement `GET /videos/{id}/render` returning a complete HTML page for a single analysis using a server-side template. This exists because a planned later feature emails a PDF export of each analysis, and if the only renderer lives in the client-side React app, producing a PDF would require either running a headless browser or maintaining a second divergent template. One template now avoids building the view twice later.
---

---
## 46. Generated API client types
Goal: Keep the frontend honest about the backend contract.
Description: Add a build step that generates TypeScript types from the API's OpenAPI schema into the frontend source tree, with a checked-in command to regenerate them. Hand-written API types drift silently; generated ones turn a backend contract change into a frontend build failure. Document the regeneration step in the README since backend and frontend may be edited weeks apart.
---

# Frontend

---
## 47. Frontend scaffold
Goal: A React app that builds to static files.
Description: Scaffold a Vite + React + TypeScript application under `web/`, configured to output a static bundle and to proxy `/api` to the backend during development. Enable the React Compiler so memoization is handled automatically rather than by hand. Add TanStack Query for server state and deliberately no global state library — this is a read-heavy view over a REST API with very little genuine client state.
---

---
## 48. Timestamp and player components
Goal: One reusable click-to-seek primitive.
Description: Build an embedded YouTube player component using the IFrame Player API via the `youtube-nocookie.com` origin, exposing a seek function through React context, plus a `Timestamp` component that renders a clickable time and seeks the player. Every timestamp in the app — on claims, quotes, topics, and transcript segments — must render through this single component so seek behaviour exists in exactly one place.
---

---
## 49. Library view
Goal: Browse everything the system has processed.
Description: Build the landing view listing videos with title, channel, publication date, and processing state, filterable by channel and date and paginated against the list endpoint. Videos still being processed should show their current stage rather than appearing broken or missing. This is the default route of the application.
---

---
## 50. Video detail view
Goal: Read one analysis alongside the source video.
Description: Build the detail view rendering the summary, topics, claims, and quotes for a video, with the embedded player above and every timestamp clickable to seek. Claims should display their attributed speaker and confidence, with unattributed ones clearly marked rather than hidden. This is the view a reader spends most of their time in, so prioritize legibility over density.
---

---
## 51. Transcript view
Goal: Read the full transcript with seek-on-click.
Description: Build a view rendering transcript segments with timestamps, each clickable to seek the player. Page or collapse the transcript by default rather than rendering all of it — a two-hour episode is thousands of segments, and deferring virtualization keeps this task small. Speaker labels should be shown where the transcript carries them.
---

---
## 52. Search view
Goal: Find past episodes by content.
Description: Build a search view querying the full-text endpoint and rendering results with highlighted excerpts, video title, channel, and date, linking through to each video's detail view. Keep the interaction simple — a query box and a result list — since this is keyword search rather than a faceted browse.
---

---
## 53. Compare view
Goal: Put two analyses of the same video side by side.
Description: Build a view that loads all analyses for a video and renders two of them alongside each other, selectable by model and prompt version, aligning summaries and claim lists for visual comparison. This is the tool for deciding whether a local model is good enough versus a cloud one, and whether a new prompt version is actually better — judgements that are impractical from two separate database queries.
---

---
## 54. Ops view
Goal: See queue health and failures in the UI.
Description: Build a view showing queue depth by job kind and state, a list of failed jobs with their error classification and message, and a retry action. There is no other operator interface for this system, so without it a permanently failed video stays invisible. Keep it plain — this is a diagnostic screen, not a dashboard.
---

# Deployment and operations

---
## 55. Backend container image
Goal: One image serving all Python services.
Description: Write a multi-stage Dockerfile installing dependencies into a virtualenv in a builder stage and copying only that plus application code into a slim runtime stage, running as a non-root user. The API, scheduler, analyzer, and migration runner all use this single image and differ only by their command, since they share every dependency. Copy the requirements file and install *before* copying application code, so editing a source file does not invalidate the dependency layer.
---

---
## 56. Speech-to-text container image
Goal: Isolate the heavy transcription dependencies.
Description: Write a Dockerfile deriving from the backend image and adding only the speech-to-text dependencies, which add roughly 2 GB that no other service needs. Install with `--only-binary=:all:` and fail loudly if no wheel is available, because the underlying library ships no source distribution and a missing wheel would otherwise surface as a confusing resolver error. Set the OpenMP thread count explicitly — the inference library otherwise detects the host's core count and ignores the container's CPU limit.
---

---
## 57. Frontend container image and web server config
Goal: Serve the built SPA with correct headers.
Description: Write a Dockerfile with a Node build stage producing the static bundle and an nginx runtime stage containing only those files — no Node or npm packages in the running image. Write the nginx config with an SPA fallback (`try_files $uri /index.html`, so deep links work), long cache headers on fingerprinted assets, a reverse proxy from `/api` to the backend, and a Content-Security-Policy permitting the YouTube embed origin and nothing else third-party. Use `npm ci` rather than `npm install` so the lockfile is authoritative.
---

---
## 58. Compose stack
Goal: Bring the whole system up with one command.
Description: Write the full compose file wiring database, one-shot migration runner, API, scheduler, analyzer, transcriber, and web server, with the migration container gating all others via `service_completed_successfully`. Put the database and workers on an internal network with no egress path, publish only the web server and only on loopback, and cap the transcriber's CPU and memory so it cannot starve the host. Add health checks: HTTP for the API, heartbeat-file freshness for the workers.
---

---
## 59. Development compose overlay
Goal: Fast iteration without rebuilding images.
Description: Write a compose override that bind-mounts source directories into the backend containers with auto-reload enabled, runs the frontend dev server with hot module reload instead of nginx, publishes the database port for direct client access, and selects a small speech-to-text model so local runs finish in seconds rather than hours. Production deployment must specify the base compose file explicitly so the override is never applied by accident.
---

---
## 60. Metrics endpoint
Goal: Expose the handful of numbers that matter.
Description: Add a metrics endpoint exposing: queue depth by kind and state, job duration by kind, measured transcription real-time factor, LLM token counts and cumulative cost, retained audio bytes, and a counter of how often an LLM returned a speaker name outside the supplied roster. The last one is a quality canary — a rising count means speaker attribution is drifting, which produces confidently wrong claims rather than obvious errors.
---

---
## 61. CI pipeline
Goal: Catch regressions before they reach the host.
Description: Set up CI running lint, type checks, and the Python test suite against a real PostgreSQL service container, plus the frontend build and `npm audit`. Build all three container images to verify the Dockerfiles still work, particularly the speech-to-text image where a missing binary wheel is a hard failure. Fail the build on lockfile drift so pinned dependencies stay pinned.
---

---
## 62. Quality evaluation harness
Goal: Compare summarizer outputs on a fixed set of real episodes.
Description: Assemble a golden set of roughly twenty already-transcribed episodes and a script that runs analysis over all of them with a given model and prompt version, storing results for side-by-side review. Judge on claim specificity (a specific attributable statement versus generic "they discussed X") and on attribution accuracy by spot-checking speakers against the audio. Automating a quality *score* is out of scope — the goal is to make human comparison fast, not to replace it.
---
