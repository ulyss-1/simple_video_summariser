# ytdigest — Architecture Plan

**Status:** design draft, pre-implementation
**Date:** 2026-09-13
**Owner:** solution architect (single operator)

---

## 1. Problem statement

Automatically produce high-quality, structured, queryable summaries of
talking-head / podcast / interview videos from YouTube, on a schedule, storing
all results durably so prior analyses can be retrieved and compared.

Two entry points:

1. **Ad-hoc** — user submits a video URL, expects it analyzed.
2. **Monitored** — a set of subscribed channels is polled periodically; newly
   published videos are analyzed automatically.

---

## 2. Requirements

### 2.1 Functional

| # | Requirement |
|---|---|
| F1 | Accept a YouTube URL or video ID and produce an analysis |
| F2 | Maintain a list of monitored channels; detect newly published videos |
| F3 | Persist transcripts and analyses; retrieve any prior analysis |
| F4 | Produce both a short TL;DR **and** a deep structured record |
| F5 | Support full-text search across the accumulated corpus |
| F6 | Allow re-analysis of stored transcripts without re-downloading |

### 2.2 Non-functional

| # | Requirement | Notes |
|---|---|---|
| N1 | **Quality over latency** | Explicitly stated. Analysis may lag publication by hours. |
| N2 | Low running cost | Budget-constrained. Prefer local compute where viable. |
| N3 | Runs on existing Ubuntu host | CPU-only, Intel iGPU, no CUDA |
| N4 | Expandable to more services later | Clean boundaries now, not necessarily distribution now |
| N5 | No dependency on a mechanism that could be revoked | See §4.3 |

### 2.3 Explicit non-goals (v1)

- Near-realtime processing
- Visual/multimodal analysis (slides, on-screen code) — not needed for
  talking-head content
- Multi-user / multi-tenant, authn/authz beyond a reverse proxy
- Horizontal scale beyond a single host
- Non-YouTube sources

### 2.4 Scale envelope

- 10–30 monitored channels
- Estimated 5–15 new videos/day
- Typical video 30–120 min; ~9–10k words/hour ≈ ~13k tokens/hour
- Corpus growth: ~3–5k videos/year

This envelope is small. It should discipline every design decision below —
several "correct at scale" choices are wrong here.

---

## 3. Constraints and their consequences

| Constraint | Consequence |
|---|---|
| CPU-only, no usable GPU | Local Whisper is viable but slow; local LLM is **not** viable for quality work (§4.4) |
| Latency-tolerant | Unlocks `large-v3` Whisper and map-reduce summarization — both trade time for quality |
| Budget-constrained | Favors subtitle-first ingestion; LLM spend is the only material recurring cost |
| Consumer Claude subscription may not be automated against | Scheduled summarization must use an API key or a local model (§4.3) |

---

## 4. Key design decisions

Each decision records the choice, the reasoning, and — importantly — the
**signal that should trigger revisiting it**.

### D1. Transcript-only pipeline (no vision)

**Decision:** Ingest text only. No keyframe extraction, no vision model.

**Rationale:** Target content is talking-head/podcast/interview. Essentially
all information is carried in speech. Vision would multiply cost and latency
for near-zero recall gain.

**Revisit if:** the channel mix shifts toward technical talks with slides,
demos, or on-screen code.

### D2. Subtitles first, Whisper as fallback

**Decision:** Try YouTube subtitles via `yt-dlp` before invoking Whisper.
Preference order: manual subs → Whisper → auto-generated subs.

**Rationale:** Most established podcast channels ship subtitles. Subtitles are
free and instant; Whisper on this CPU is the expensive path. This converts
transcription from a per-video cost into an occasional one.

**Caveat — and this is the significant one:** YouTube *auto-generated* captions
have no punctuation and no speaker labels. For interviews, "who said what" is
central to a useful summary, and auto-captions destroy it. Hence auto-captions
rank *below* Whisper in the preference order, and a `PREFER_WHISPER` switch
should exist to bypass them entirely.

**Open question:** actual subtitle coverage across the real channel list, and
what fraction are manual vs auto. This is a cheap measurement and should be
done before implementation — it determines whether Whisper throughput matters
at all. See §11.

### D3. Quality-first Whisper configuration

**Decision:** `large-v3`, `int8` quantization, beam size 5, VAD filter on.

**Rationale:** N1 says quality wins. `small` is noticeably worse on proper
nouns, technical terms, and names — exactly the tokens that make a summary
useful. Since nothing downstream is waiting, a slow transcription is free.

**Consequence:** a 2-hour episode may take multiple hours of CPU. This is
acceptable *only because* transcription is isolated in its own worker (§6.3).

**Revisit if:** measured throughput can't keep up with daily inflow — i.e.
transcription backlog grows monotonically over a week.

### D4. Speaker diarization — deferred, not dismissed

**Decision:** Not in v1. Revisit after first quality measurement.

**Rationale:** Speaker attribution materially improves interview summaries, and
claim attribution is a stated output requirement. But diarization
(`pyannote`-class) adds a heavy CPU dependency, and Whisper alone gives no
speaker labels. Manual subtitles sometimes carry them; often not.

**Interim mitigation:** the analysis schema carries a `speaker` field
throughout, populated as `"unknown"` when unavailable. Adding diarization later
becomes a backfill, not a schema migration.

**This is the most likely v2 quality upgrade.** Flagging it explicitly so it
isn't forgotten.

### D4b. Speaker attribution by LLM inference (resolves Q2)

**Decision:** Speakers are inferred by the LLM from context — episode title,
description, and conversational cues (turn-taking, direct address, name
mentions) — rather than from acoustic diarization.

**Rationale:** Attribution matters for interview content, but real diarization
(D4) adds a heavy CPU dependency to a host already strained by Whisper. LLM
inference costs nothing beyond the calls already being made and requires no new
dependency.

**Required implementation detail — speaker roster.** Under map-reduce (D9) each
chunk is a separate call, so a model left to infer speakers per chunk will
drift: "Host" in one chunk, a name in another, "Speaker 2" in a third. Instead:

1. A **roster pass** runs once per episode, deriving participant names and
   roles from title, description, and the opening minutes of the transcript.
2. The resulting roster is injected into every chunk prompt as a closed set.
   The model assigns from that set or emits `unknown` — it does not invent
   labels.

This makes attribution stable across chunks and makes the reduce step's
deduplication reliable.

**Known weakness, accepted:** inference degrades in fast back-and-forth
exchanges and in panels with three or more participants, and **errors are
silent** — a misattributed claim is indistinguishable from a correct one in the
stored output. Mitigations:

- Prompts instruct the model to emit `unknown` rather than guess when
  turn-taking is ambiguous. An honest `unknown` is more useful than a wrong
  name.
- `transcripts.speaker_source` records how attribution was derived
  (`subtitle_labels | llm_inferred | none`), so accuracy can be evaluated later
  and re-attributed in bulk without re-transcribing.
- The per-claim `confidence` field covers attribution confidence, not just
  claim salience.

**Revisit if:** spot-checking shows attribution errors are frequent enough to
mislead, or if panel-format content becomes common. The upgrade path to real
diarization remains a backfill, not a migration.

### D5. Cloud LLM for summarization, local as measured fallback

**Decision:** Summarization runs behind a `Summarizer` interface with two
implementations (local via Ollama, cloud via Anthropic API). Start by measuring
local; expect to land on cloud.

**Rationale:** This is the decision that most deserves scrutiny, because the
stated preference (run locally, save money) conflicts with the hardware reality
and the stated priority (quality).

- A 7–8B model on CPU will take minutes per chunk, and a 13k-token prompt
  prefill is the slow part. A 30B-class model is impractical.
- More importantly, small local models degrade specifically at *long-context
  claim attribution across multiple speakers* — pulling specific, attributable
  claims rather than generic "they discussed X" filler. That is precisely the
  output quality requirement.
- The cost avoided is small. At 5–15 videos/day and ~13k input tokens each,
  a cheap cloud model tier lands in the **~$5–15/month** range. Verify current
  pricing at <https://docs.claude.com/en/docs/about-claude/pricing>.

**Recommendation:** measure local on ~20 real videos, compare side by side with
cloud output, and decide on evidence. The architecture must make that
comparison trivial — hence D7 (every analysis run is retained).

**Content sensitivity note:** the source material is public YouTube video.
There is no confidentiality argument for local-only processing, so the usual
driver for self-hosting doesn't apply here. The decision reduces to cost vs
quality.

### D6. Anthropic access via API key only

**Decision:** Automated/scheduled summarization uses an **API key**. Never a
consumer Claude.ai / Claude Code subscription.

**Rationale:** Anthropic's consumer terms prohibit accessing the service
through automated or non-human means except via an API key. A cron-driven
daily pipeline is exactly that. Headless `claude -p` authenticates against a
subscription and technically runs, but building the scheduler around it —
including any "queue overnight when session limits are exhausted" strategy —
puts a revocable, terms-violating mechanism on the critical path of the whole
system. That fails N5.

Interactive use of Claude for *developing and iterating on the prompts* is
entirely fine. It is the unattended scheduled invocation that is the problem.

**Consequence:** the realistic cost floor is the API spend in D5, or accepting
local-model quality. There is no third option.

### D6b. Audio retained with TTL and a disk cap (resolves Q3)

**Decision:** Retain transcribed audio for a bounded window (default 30 days,
configurable to 90), subject to a hard disk cap with oldest-first eviction.

**Rationale:** The realistic re-processing window is early — revisiting Whisper
model choice or adding diarization (D4) happens in the first weeks, not years
later. A TTL covers that without unbounded growth. It also insures against a
source video being deleted, geo-blocked, or age-gated before re-processing.

**Store the Whisper-normalized audio, not the original.** Whisper consumes
16 kHz mono; the original `bestaudio` stream is stereo and high-bitrate. Storing
16 kHz mono Opus costs roughly 7–10 MB/hour against 100–200 MB/hour for the
original — a 10–20× reduction with no loss for any downstream use. Diarization
also operates on 16 kHz mono, so this does not foreclose D4.

**Sizing:** worst case (every video requires Whisper, 15/day, 2h average) is
~7 GB at 30 days or ~20 GB at 90 days with normalized audio, against ~200 GB
if originals were kept. The true figure depends on subtitle coverage (Q1) —
if coverage is high, Whisper runs rarely and this is negligible.

**Both limits are enforced, not just the TTL.** A TTL alone cannot prevent a
backfill burst from filling the volume, so a byte cap with oldest-first
eviction runs regardless of age. Eviction is safe because audio is derived
data: the transcript, which is the durable asset (D7), is already stored.

**Implementation notes:**
- Audio lives on a filesystem volume, never in Postgres. A `media` table (or a
  column on `transcripts`) records path, bytes, and expiry.
- The purge job is owned by the `planner` (§6.2), which already runs periodic
  maintenance.
- Eviction and TTL purge must never delete audio for a job currently in
  `running` state.

Config: `AUDIO_TTL_DAYS` (default 30), `AUDIO_MAX_GB` (default 20),
`AUDIO_KEEP` (`1` = retain per policy, `0` = delete immediately after
transcription).

### D7. Transcripts are durable; analyses are versioned and disposable

**Decision:** Store transcripts separately from analyses. Never delete an
analysis. Tag each with `model` and `prompt_version`.

**Rationale:** This is the single highest-leverage structural choice. Prompt
iteration — not pipeline work — is where most summary quality will come from.
Re-running a new prompt against cached transcripts costs one LLM call;
re-downloading and re-transcribing costs hours. Retaining every run also makes
the local-vs-cloud comparison in D5 a query rather than an experiment.

### D8. Structured JSON output, normalized into relational tables

**Decision:** LLM returns schema-validated JSON. Persist `tldr` on the analysis
row; `topics`, `claims`, `quotes` as child tables with timestamp offsets.

**Rationale:** Prose-only summaries can't be queried, diffed, ranked, or
aggregated. Timestamps let every extracted item link back into the video. This
also satisfies F4 (both TL;DR and deep record) with one artifact.

### D9. Map-reduce summarization, not one-shot

**Decision:** Chunk transcripts into ~15-minute windows with ~60s overlap,
analyze each, then reduce into an episode-level TL;DR. Deduplicate at seams.

**Rationale:** A 2-hour episode is ~25k tokens. One-shot extraction over that
length reliably loses the middle — recall degrades in the interior of long
contexts. Chunking costs more calls and more wall-clock, which N1 says we can
afford. Short videos stay single-chunk.

**Tunable:** smaller chunks → better recall, more calls, higher cost.

### D9b. Forward-only monitoring with explicit backfill (resolves Q6)

**Decision:** Registering a channel processes only videos published *after*
registration. Historical videos are processed only on an explicit, bounded
backfill request.

**Rationale:** Automatic full-catalog ingestion is the single largest cost
event the system can trigger — 20 channels × ~200 episodes is ~4,000 videos,
potentially months of CPU if subtitle coverage is poor, plus the largest LLM
bill the system will ever produce, all from one API call. Making it deliberate
and bounded keeps a routine action (adding a channel) cheap and predictable.

**Two discovery paths, which is the key implementation consequence:**

| Path | Mechanism | Coverage |
|---|---|---|
| Monitoring | Channel RSS feed | ~15 most recent videos only |
| Backfill | `yt-dlp` against the channel's uploads playlist | Full catalog |

RSS is deliberately limited and cannot serve backfill. These are separate code
paths, not one parameterized path.

**Priority separation is required, not optional.** Backfill jobs enqueue at
lower priority than forward jobs. Without this, a 200-episode backfill occupies
the transcriber for weeks and today's new episodes queue behind it — the
monitoring feature silently stops working. The `jobs.priority` column (§7)
exists for this; the claim query already orders by it.

**Interaction with D6b:** a large backfill is precisely the burst that triggers
disk-cap eviction. Expected and safe — audio is derived data — but it means
backfilled audio will often be evicted before its TTL expires.

**API shape:**

    POST /channels/{id}/backfill  {"limit": 25, "dry_run": true}

`dry_run` returns the count and estimated transcription load without
enqueueing. Given the cost asymmetry, backfill should report what it is about
to do before doing it.

### D9c. Semantic search deferred, schema prepared now (resolves Q5)

**Decision:** v1 ships keyword full-text search only. Semantic search over the
corpus is a planned v2 feature, and the v1 schema is shaped so it can be added
without reprocessing.

**Rationale:** At a few thousand videos/year, keyword search degrades
predictably — recall of *gist* outlasts recall of *phrasing*, so the query you
actually want ("the episode where someone argued X") stops matching. But
semantic search is not worth building before there is a corpus to search.

**What "schema-ready" actually requires — and it is not the vector column.**
The cost is persisting **stable chunk boundaries now**:

- If chunks exist only transiently inside the analyzer (§6.4), embeddings
  cannot be attached to them later without re-deriving them.
- Worse, any change to `CHUNK_SEC` between now and then silently invalidates
  the mapping between stored analyses and any newly computed chunks.

Therefore v1 persists a `transcript_chunks` table: `id`, `transcript_id`,
`seq`, `start_sec`, `end_sec`, `text`, `chunk_strategy`. The analyzer reads
chunks from this table rather than computing them inline, which also makes
map-reduce reproducible and debuggable — a useful property independently of
embeddings.

Adding semantic search later is then: enable `pgvector`, add an `embedding`
column (dimension fixed by the model chosen at that time), backfill, add an
index. No reprocessing, no re-transcription.

**Embedding compute is the one genuinely local-friendly workload.** Unlike
generative models (D5), embedding models are small and fast on CPU — a
sentence-transformer class model embeds a full corpus in reasonable time
without a GPU. When this lands, it should run locally and cost nothing.

**Still out of scope, explicitly:** cross-video topic clustering, entity
resolution, position-tracking over time, contradiction detection. These need
everything above plus substantial prompt and evaluation work. Not planned.

### D10. PostgreSQL as both datastore and job queue

**Decision:** One Postgres instance. Job queue implemented as a table using
`SELECT ... FOR UPDATE SKIP LOCKED`.

**Rationale:** `SKIP LOCKED` is correct under concurrent workers and gives
transactional enqueue — a job and the row it references commit together, so a
job can never point at data that didn't land. At 5–15 jobs/day, a dedicated
broker (Redis/RabbitMQ/NATS) adds operational surface, a second failure domain,
and backup complexity for zero throughput benefit.

Postgres also supplies full-text search (F5) via `tsvector` for free, avoiding
a separate search service.

**Revisit if:** job volume reaches thousands/day, or multiple hosts need to
share the queue.

### D11. Service boundaries follow resource contention, not fashion

**Decision:** Four processes — `api`, `planner`, `transcriber`, `analyzer` —
sharing one database.

**Rationale, stated plainly:** true microservices at this volume would be
over-engineering. Network hops, service discovery, and distributed failure
modes are real costs, and 5–15 videos/day does not pay for them.

What *does* justify separation here is one specific fact: **transcription is
CPU-bound for hours while analysis is a short call.** Splitting those two means
a long Whisper run never blocks analysis or the API, and the CPU-bound
component can be scaled independently. That is a real, measurable benefit.

The other two splits (`api`, `planner`) are cheap and give operational clarity
— the API stays responsive, and scheduling logic is isolated from processing
logic.

This satisfies N4 without paying distributed-systems costs: the boundaries are
clean and the queue is an interface, so extracting a service to another host
later is a deployment change, not a rewrite.

### D12. Frontend: React SPA against the JSON API

**Decision:** A single-page app built with React + Vite + TypeScript, consuming
the existing `api` service. Served as static files by nginx or by FastAPI
itself.

**Clarification of layers**, since these are often conflated:

- **Node.js / npm** are *build-time only* — the runtime and package manager
  used to run the bundler. The build output is plain HTML/CSS/JS.
- **React** is the UI framework the app is written in.
- **No Node process runs in production.** Nothing new to patch, no extra
  container, no additional long-lived attack surface. The deployed artifact is
  a directory of static files.

**Why a framework at all:** vanilla JS with ES modules would avoid npm entirely
and carry zero supply-chain surface. That is viable for a handful of read-only
views, but breaks down once components must stay synchronized — transcript
scrolling bound to a player position, live filtering across thousands of
claims. Those are the interactions that motivated choosing an SPA over
server-rendered HTMX in the first place, so the framework is justified by the
same reasoning.

**Why React was chosen over Svelte:**

| | React (chosen) | Svelte |
|---|---|---|
| Model | Runtime, virtual DOM | Compiler, no virtual DOM |
| Bundle size | Larger | Smaller |
| Boilerplate | Hooks, dependency arrays, re-render semantics | Lower |
| Ecosystem | Much larger; mature third-party components | Smaller |
| AI assistance | Stronger (more training data) | Good |

The deciding factors are the ecosystem and AI-assisted development support.
Both matter concretely here: the transcript view needs virtualization to render
thousands of segments, and the detail view needs a player integration — both
are solved problems in React's ecosystem.

**Accepted trade-off:** React carries more incidental complexity (re-render
semantics, effect dependencies) than Svelte, which is the recurring source of
subtle bugs for developers who don't work in it daily. Two mitigations worth
adopting from the start:

- Keep state management minimal. TanStack Query for server state covers almost
  everything this app does — it is a read-heavy view over a REST API, with very
  little genuine client state. Avoid Redux or similar; it would be
  unjustified here.
- Use TypeScript, and generate the API types from the FastAPI OpenAPI schema
  rather than hand-writing them. This keeps the frontend honest about the
  backend contract and catches drift at build time — worth more than usual
  given the backend and frontend are maintained by the same person at
  different times.

**Supply chain — the actual risk here.** The framework choice is not the
security-relevant decision; the transitive npm dependency tree is. Mitigations:
keep direct dependencies minimal, commit the lockfile, pin versions, run
`npm audit` in CI, and build in a container rather than on the host. A UI with
six direct dependencies has a very different risk profile from one with sixty.

**Complementary, not alternative:** Metabase pointed at Postgres remains worth
running alongside for ad-hoc analytical queries ("which channels discuss X
most"). It costs no code and covers exploration the SPA would take weeks to
match. The SPA is the *reading* surface; Metabase is the *querying* surface.

### D12b. Embedded player with click-to-seek (resolves Q8)

**Decision:** The video detail view embeds the YouTube IFrame Player. Clicking
any timestamp — on a claim, quote, topic, or transcript segment — seeks the
embedded player in place. Player position does **not** drive transcript
auto-scroll (one-directional, not bidirectional).

**Rationale:** Verifying a claim against the source is the main reason to
revisit a video, and a new-tab round trip makes that friction high enough that
it stops happening. One-directional seek captures most of that value for a
fraction of the effort: bidirectional sync additionally requires polling player
state, virtualized rendering of thousands of segments, and scroll-position
conflict handling.

**Scope consequence:** the transcript view still needs virtualization if full
transcripts are rendered (a 2-hour episode is thousands of segments), but this
can be deferred by paginating or collapsing the transcript by default.

**Honest note on D12:** click-to-seek alone does not require React — it is a
few lines against the IFrame API and would work in server-rendered HTML.
React's ecosystem advantage becomes material only if bidirectional sync or
large-list virtualization is later adopted. React remains the choice per D12,
but this decision does not by itself justify it.

**Security/privacy implications of the embed:**

- The iframe issues third-party requests to YouTube from the user's browser.
  Use `youtube-nocookie.com` (privacy-enhanced mode) to avoid cookie-based
  tracking on page load.
- Content-Security-Policy must permit `frame-src` for the YouTube origin. Keep
  the rest of the CSP strict — this should be the only third-party frame origin
  allowed.
- The embed means the UI is not fully functional offline or on an isolated
  network. Acceptable given the source content is YouTube-hosted anyway.

### D12c. UI-only in v1; email + PDF distribution planned for v2 (resolves Q4b)

**Decision:** v1 has no push mechanism — the React SPA is the only consumption
surface. Two distribution features are planned for v2:

1. **Per-video email on completion** — when an analysis finishes, email a PDF
   export of it to a configured set of mailboxes.
2. **Periodic digest** — a weekly summary covering analyses completed across
   all monitored channels, with per-channel and aggregate totals.

**One decision must be made in v1 to keep this cheap**, and it is not obvious:

**Rendering must not live only in React.** If the analysis view exists solely
as a client-side React component, producing a PDF later requires either running
a headless browser (a heavy, fragile dependency) or maintaining a second,
divergent template. Both are avoidable by keeping a **server-side render path
for a single analysis** from the start — a template in the `api` service that
renders an analysis to HTML. The SPA remains the interactive surface; the
server-side template becomes the source for PDF and email bodies.

This costs little in v1 (one template over data already assembled) and is the
difference between "add a PDF renderer" and "rebuild the view twice" in v2.

**Schema additions deferred but anticipated:**

    recipients        id, email, active, scope (all | channel_id)
    notifications     id, analysis_id, recipient_id, kind, sent_at, status

Recording them here so the v1 schema does not accidentally preclude per-channel
subscriptions.

**Operational notes for when this lands:**
- Outbound SMTP is a new egress surface and a new credential to manage on the
  host. Prefer a relay with a scoped API credential over storing a mailbox
  password.
- Email delivery must be retried independently of analysis: a failed send must
  never re-run the analysis. This is a distinct job kind (`notify`), not a step
  inside `analyze`.
- The digest is a `planner` responsibility (it already owns periodic work), not
  a new service.

### D13. Private now, designed for public later (resolves Q7)

**Decision:** No authentication in v1 — the service is reachable only from a
trusted network. But the API is designed so that adding auth later is additive,
not a rewrite.

**What is actually worth protecting.** The corpus is summaries of public YouTube
videos; there is nothing confidential in it. The asset at risk is the **write
surface**: anyone who can reach `POST /channels/{id}/backfill` can spend the LLM
budget and saturate the CPU for weeks. Read endpoints are low-stakes; write
endpoints are the ones that need protection the moment exposure changes.

**Design rules adopted now, at near-zero cost:**

| Rule | Reason |
|---|---|
| Bearer token in an `Authorization` header — never cookies | Cookies are what make CSRF protection necessary. Avoiding them keeps that entire class of work permanently out of scope. |
| API fully stateless; no server-side sessions | No session store to introduce later. Scales to N api replicas for free. |
| All requests pass through one auth dependency, a no-op in v1 | Enabling auth becomes one implementation change, not an audit of every endpoint. |
| Read and write endpoints separated in the route tree | Allows read-public / write-authenticated later without restructuring. |
| Never reflect user input into error messages; no stack traces to clients | Cheap now, required later. |
| CORS default-deny; allow only the frontend origin | Prevents accidental exposure when the SPA is served from a different origin. |
| Rate-limit hook present on write endpoints, disabled in v1 | Backfill and submit are the abuse-relevant endpoints. |

**Explicitly deferred:** user accounts, OIDC, refresh tokens, per-user data
scoping, audit logging. None of these are implied by the rules above, and adding
them remains a v2 decision rather than a v1 cost.

**Deployment posture in v1:** API bound to loopback, fronted by the existing
reverse proxy, which terminates TLS and controls what is reachable. The
application assumes it may be exposed and does not rely on network position for
correctness — it relies on it only for authorization, which is the part being
deferred.

---

## 5. High-level architecture

```
                    ┌──────────┐
  user ────────────▶│   api    │  submit URL, add channel, query results
                    └────┬─────┘
                         │ enqueue
                         ▼
                  ┌─────────────┐
                  │  jobs table │  Postgres, SKIP LOCKED
                  └──┬───────┬──┘
             claim   │       │   claim
                     ▼       ▼
          ┌───────────────┐ ┌──────────┐
          │  transcriber  │ │ analyzer │
          │  (CPU-bound)  │ │  (LLM)   │
          └───────┬───────┘ └────┬─────┘
                  │              │
                  ▼              ▼
             ┌──────────────────────┐
             │      PostgreSQL      │
             │ transcripts/analyses │
             └──────────────────────┘
                         ▲
                  ┌──────┴──────┐
                  │   planner   │  RSS poll, backfill, reap stale jobs
                  └─────────────┘
```

### Processing flow

```
POST /videos ──▶ job(ingest)
                   │
        transcriber: fetch metadata, check subtitle availability
                   ├── manual subs present ──▶ transcripts ──▶ job(analyze)
                   └── otherwise ───────────▶ job(transcribe)
                                                   │
                                        Whisper large-v3 (hours)
                                                   │
                                              transcripts ──▶ job(analyze)
                                                                  │
                                        analyzer: chunk → map → reduce
                                                                  │
                                      analyses + topics/claims/quotes
```

---

## 6. Services

### 6.1 `api`

Thin HTTP layer. Enqueues work and serves stored results; performs no
processing, so it stays responsive while long jobs run.

| Endpoint | Purpose |
|---|---|
| `POST /videos` | Submit URL/ID → enqueue `ingest` |
| `POST /channels` | Register a channel for monitoring (forward-only) |
| `POST /channels/{id}/backfill` | Enqueue N historical videos; supports `dry_run` |
| `GET /videos/{id}` | Latest analysis, or job status if pending |
| `GET /search?q=` | Full-text search across transcripts |
| `GET /healthz` | Queue depth by kind and state |

Bind to loopback; front with the existing reverse proxy. No auth in v1 beyond
that (single operator).

### 6.1a `web` (frontend)

React SPA (Vite, TypeScript), static build. Not a running service — a build
artifact served by nginx or mounted as static files by the `api` service.

Views:

| View | Purpose |
|---|---|
| Library | Browse videos by channel/date; analysis status |
| Video detail | TL;DR, topics, claims, quotes; embedded player, click-to-seek (D12b) |
| Transcript | Full transcript with timestamps; click to seek. Collapsed/paginated by default to defer virtualization |
| Search | Full-text across the corpus, with highlighted excerpts |
| Compare | Two analyses of the same video side by side (model or prompt_version) |
| Ops | Queue depth, dead-lettered jobs, recent failures |

The *Compare* view is what makes D5 and D7 operationally useful — judging local
vs cloud output, or prompt v1 vs v2, needs them next to each other rather than
in two database queries.

The *Ops* view exists because there is no other operator interface; without it,
a dead-lettered video is invisible until someone queries the `jobs` table.

### 6.2 `planner`

Owns *discovery and scheduling*; contains no processing logic.

- Poll each monitored channel's RSS feed hourly:
  `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`
  — no API key, no quota consumption. This is why the YouTube Data API is not
  used for discovery.
- Enqueue `ingest` jobs for unseen videos (idempotent via unique constraint).
- **Purge expired audio** per the TTL and disk cap (D6b), skipping anything
  referenced by a `running` job.
- **Reap stale jobs:** requeue anything `running` past its lease, so a killed
  worker doesn't strand a video forever.
- **Backfill:** when `PROMPT_VERSION` changes, enqueue `analyze` for every
  stored transcript lacking an analysis at that version. This is the mechanism
  that makes D7 operationally real.

### 6.3 `transcriber`

The only CPU-bound service; the one that gets scaled.

Handles two job kinds so the expensive path is explicit and separately
observable:

- `ingest` — resolve metadata, attempt subtitles, decide the path
- `transcribe` — download audio, run Whisper (may run for hours)

Resource-capped (e.g. ~3 CPUs) so it cannot starve the rest of the host.
Whisper model is lazy-loaded so other services never pay the memory cost.

### 6.4 `analyzer`

Consumes `analyze` jobs. Chunks, maps, reduces, persists. Backend selected by
config (`SUMMARIZER=ollama|anthropic`), making D5's comparison a one-variable
change.

---

## 7. Data model

```
channels     channel_id PK, title, last_polled, active
videos       video_id PK, channel_id FK, title, duration_sec, published_at
transcripts  id PK, video_id FK, source, language, segments JSONB, full_text,
             speaker_source, speaker_roster JSONB
             UNIQUE(video_id, source)
transcript_chunks  id PK, transcript_id FK, seq, start_sec, end_sec, text,
             chunk_strategy        ← stable boundaries; embedding column added in v2 (D9c)
             UNIQUE(transcript_id, seq, chunk_strategy)
             fts tsvector GENERATED  ← full-text search index
analyses     id PK, video_id FK, transcript_id FK, model, prompt_version,
             tldr, input_tokens, output_tokens, created_at
topics       analysis_id FK, title, summary, start_sec
claims       analysis_id FK, text, speaker, start_sec, confidence
quotes       analysis_id FK, text, speaker, start_sec
jobs         id PK, video_id, kind, state, payload, priority, attempts,
             last_error, run_after, locked_at, locked_by, finished_at
             UNIQUE(video_id, kind)
```

Notes:

- `transcripts.source` ∈ `youtube_manual | youtube_auto | whisper` — retrieval
  prefers manual → whisper → auto, per D2.
- `segments` as JSONB keeps timestamped structure without a row per caption
  line (a 2h video is thousands of segments).
- `analyses` is append-only. Multiple rows per video is the normal state.
- `jobs.UNIQUE(video_id, kind)` makes enqueue idempotent — replays are free.

---

## 8. Job lifecycle and failure handling

**States:** `pending → running → done`, or `→ dead` after exhausting retries.

| Concern | Approach |
|---|---|
| Claim | `FOR UPDATE SKIP LOCKED`, ordered by priority then `run_after` |
| Retry | Exponential backoff on `run_after`, max ~4 attempts |
| Poison jobs | Move to `dead` with `last_error` retained for inspection |
| Crashed worker | Lease expiry (~6h, accommodating long transcriptions); planner requeues |
| Graceful shutdown | SIGTERM finishes the current job, then exits — never abandon an in-flight transcription |
| Idempotency | Unique constraints on `(video_id, kind)` and `(video_id, source)` |

**Retry hazard to watch:** transcription retries are expensive (hours of CPU).
Failures should be classified — a `yt-dlp` failure (video removed, geo-blocked,
age-gated, requires cookies) should dead-letter fast rather than burn four
Whisper attempts. Worth designing explicitly during implementation.

---

## 9. Deployment

- Docker Compose on the existing Ubuntu host; four services plus Postgres.
- Postgres not published externally; API bound to loopback behind the existing
  reverse proxy.
- Separate images: only the transcriber carries the Whisper dependency, keeping
  the other images small.
- Whisper model cache on a named volume (avoids re-downloading multi-GB models).
- Scaling knob: `docker compose up -d --scale transcriber=N`.
- Containers run as non-root.
- **Frontend:** React/Vite built in a Node container at image-build time; only
  the static output is copied into the serving image. No Node.js in the running
  stack.
- Optional: Metabase container alongside, read-only DB role (D12).

**Interaction with existing host setup:** this stack binds only to loopback and
the compose bridge network. Confirm the compose bridge subnet doesn't collide
with anything already routed on the host before deploying.

---

## 10. Configuration surface

| Variable | Default | Purpose |
|---|---|---|
| `SUMMARIZER` | `ollama` | `ollama` \| `anthropic` |
| `OLLAMA_MODEL` | `qwen3:8b` | local model |
| `ANTHROPIC_API_KEY` | — | API key only (D6) |
| `PROMPT_VERSION` | `v1` | bump → triggers backfill re-analysis |
| `WHISPER_MODEL` | `large-v3` | quality-first (D3) |
| `WHISPER_COMPUTE` | `int8` | CPU quantization |
| `PREFER_WHISPER` | `0` | `1` = never use auto-captions (D2) |
| `CHUNK_SEC` | `900` | map-reduce window (D9) |
| `POLL_INTERVAL_SEC` | `3600` | RSS poll cadence |
| `TRANSCRIBER_CPUS` | `3.0` | protect the rest of the host |
| `AUDIO_KEEP` | `1` | retain audio per policy (D6b) |
| `AUDIO_TTL_DAYS` | `30` | audio retention window |
| `AUDIO_MAX_GB` | `20` | hard cap; oldest-first eviction |

---

## 11. Measurements to take *before* building

Two cheap experiments that could change the design materially. Both should run
before any service code is written.

1. **Subtitle coverage** across the real channel list — what fraction of recent
   videos have manual subs, auto-only, or none. If manual coverage is high,
   Whisper throughput is nearly irrelevant and D3's cost concern evaporates.
   If coverage is poor, transcription becomes the system's bottleneck and
   deserves more design attention.

2. **Whisper realtime factor** on this specific CPU, across `small` / `medium`
   / `large-v3` at `int8`. Produces the go/no-go for local transcription and
   sizes the backlog risk. RTF > ~0.5 means a 2h episode costs >1h of CPU.

A third, once a handful of transcripts exist: **local vs cloud summarizer
quality** on the same ~20 videos, judged on claim specificity and attribution
accuracy. This decides D5 on evidence.

---

## 12. Phasing

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 | Measurements (§11) | Coverage % and Whisper RTF known |
| 1 | Schema + queue + single-video path, CLI-driven | One URL → stored analysis |
| 2 | Worker split, retries, graceful shutdown | Survives kill -9 mid-job |
| 3 | Planner + RSS monitoring | Channels processed unattended for a week |
| 4 | API + search | Results queryable |
| 4b | React SPA (library, detail, transcript, search, compare, ops) | Usable without touching psql |
| 5 | Quality iteration: prompt versions, local vs cloud comparison | D5 decided on data |
| 6 (opt) | Diarization (D4), semantic search via pgvector (D9c) | — |
| 7 (v2) | Email + PDF export per analysis; weekly digest; recipients/notifications (D12c) | Digest delivered unattended for a month |

---

## 13. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Local LLM quality insufficient (D5) | Core output requirement unmet | Interface seam; measure early; accept ~$10/mo cloud |
| Whisper too slow for inflow (D3) | Growing backlog | Measure first; fall back to `medium`, or accept auto-captions |
| `yt-dlp` breakage from YouTube changes | Full ingestion outage | Pin + update regularly; treat as expected maintenance, not an incident |
| Bot-detection / cookie requirements on download | Ingestion failures | Classify errors; dead-letter fast; may need cookie file |
| Auto-caption quality degrades summaries | Silent quality loss | `PREFER_WHISPER`; record `source` on every transcript and compare |
| Misattributed speakers (D4b) | Silently wrong claims — worst failure mode in the system | Fixed roster; prompt prefers `unknown` over guessing; `speaker_source` recorded for bulk re-attribution; spot-check during Phase 5 |
| Long-context recall loss | Missed content in long episodes | Map-reduce (D9); tune `CHUNK_SEC` |
| Cost drift if video volume grows | Budget breach | Token counts stored per analysis; monitor |
| npm transitive dependency compromise | Supply-chain exposure on the build host | Minimal direct deps, committed lockfile, pinned versions, build in container, `npm audit` in CI |
| Frontend maintenance burden | UI rots, becomes unusable | Minimal state management, generated API types, few views (D12) |

---

## 14. Open questions

1. What is the actual channel list, and its subtitle coverage? (§11.1)
2. ~~Speaker attribution~~ — **resolved:** LLM inference from context with a
   fixed per-episode roster (D4b). Real diarization deferred to v2.
3. ~~Retention policy~~ — **resolved:** keep Whisper-normalized 16 kHz mono
   audio with a TTL plus a hard disk cap and oldest-first eviction (D6b).
   Remaining sub-question: TTL of 30 or 90 days — decide once Q1 gives real
   subtitle coverage and therefore real volume.
4. ~~Consumption surface~~ — **resolved:** React SPA in v1 (D12); email + PDF
   per-video and weekly digest planned for v2, with a server-side render path
   retained in v1 to make it cheap (D12c).
5. ~~Cross-video synthesis~~ — **resolved:** semantic search planned for v2;
   v1 persists stable chunk boundaries so embeddings can be backfilled without
   reprocessing (D9c). Topic clustering and synthesis remain out of scope.
6. ~~Backfill scope~~ — **resolved:** forward-only on registration, explicit
   bounded backfill on demand via a separate endpoint and discovery path
   (D9b).
7. ~~Access model~~ — **resolved:** private in v1, API designed so auth is
   additive later; bearer-token/stateless/no-cookie discipline adopted now
   (D13).
8. ~~Player integration~~ — **resolved:** embedded IFrame player with
   one-directional click-to-seek; bidirectional sync deferred (D12b).

---

## 15. Summary of what makes this design work

- **Transcripts are the asset.** Everything else is re-derivable. This makes
  prompt iteration — the main quality lever — nearly free.
- **The expensive path is isolated.** Transcription can grind for hours without
  affecting anything else, and is the only thing that needs scaling.
- **Every expensive decision is behind a seam.** Summarizer backend, transcript
  source, and queue implementation can each change without touching the others.
- **The frontend adds no production runtime.** An SPA is a build artifact, not
  a service; Node exists only at build time.
- **Complexity matches the volume.** One database, four processes, no broker,
  no search service. Boundaries are clean enough to distribute later if the
  envelope changes — but nothing is distributed speculatively today.
