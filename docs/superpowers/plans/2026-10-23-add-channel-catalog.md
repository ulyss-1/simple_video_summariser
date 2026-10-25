# Add channel catalog and selected processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator register a YouTube channel, immediately browse a bounded newest-first flat catalog, progressively receive per-video metadata, and idempotently queue selected unknown videos through the existing backfill pipeline.

**Architecture:** Persist flat catalog snapshots separately from `videos`, because merely browsing an upload must not make it “known.” `POST /channels/{channel_id}/catalog/discover` performs the bounded flat listing and upsert, `GET /channels/{channel_id}/catalog` reads the persisted rows and joined processing state, and a bounded planner task enriches pending rows through the existing `MetadataSource`; a channel-scoped bulk write validates every requested ID against the persisted catalog before reusing backfill insertion and low-priority ingest semantics. The React route uses generated OpenAPI types and TanStack Query for registration, discovery, polling, retry, and bulk submission, while keeping selected IDs in route-local state.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, psycopg 3, PostgreSQL 18/Alembic, yt-dlp adapters, React 19.3, TypeScript 5.9, TanStack Query 5, React Router 8, Vitest/jsdom, Testing Library, plain CSS.

**Spec:** `docs/superpowers/specs/2026-10-23-add-channel-catalog-design.md`

## Global Constraints

- Work inside WSL Ubuntu 26.04 with Python 3.14 and Node 24; activate `.venv` before Python commands and run npm through a login shell.
- Do not add or move dependencies; runtime Python, Whisper-only, dev Python, and frontend dependencies remain in their existing dependency files.
- Preserve import direction `services/ -> adapters/ -> common/`; `common/` must not import adapters and adapters must not import services.
- Issue #242 stacks on issue #231: merge or cherry-pick #231 before implementation, use migration revision `0017` with `down_revision = "0016"`, and preserve #231's delete/tombstone behavior.
- Migrations remain standalone commands and never run on application startup.
- Tests requiring PostgreSQL are marked `integration`; all other tests run without Docker and no test calls the network.
- Treat video IDs as exactly 11 characters from `[A-Za-z0-9_-]`; a leading `-` is valid, and yt-dlp receives a full watch URL rather than a bare ID.
- Channel catalog limits are exactly `50`, `200`, `500`, and `5000`; “Show all” means `5000`, never an unbounded listing.
- A catalog browse does not insert into `videos`; only the confirmed bulk action creates `origin='backfill'` videos and `ingest` jobs at priority `-10`.
- Metadata enrichment is bounded, asynchronous, per-row, and failure-isolated; catalog discovery returns titles and canonical links without waiting for metadata.
- Canonical links are derived from validated IDs as `https://www.youtube.com/watch?v=<video_id>` and arbitrary source URLs are never persisted.
- Known videos stay visible, expose the existing five-state processing status, and cannot be selected or enqueued again.
- TanStack Query owns server state; selected video IDs remain local to the catalog route and no global state library is added.
- API-provided title and description values render only as React text children; never use `dangerouslySetInnerHTML`, `innerHTML`, or `insertAdjacentHTML`.
- Dates render in UTC as `YYYY-MM-DD`; external YouTube links use `target="_blank" rel="noopener noreferrer"`.
- Bulk processing requires an inline confirmation stating that existing subtitles may be used or audio may be downloaded and transcribed; no modal is introduced.
- Every user-visible loading, failed, selected, and known-disabled state has text or native-control semantics and never relies on colour alone.
- Use the existing design tokens and plain CSS in `web/index.css`; add no CSS framework, component library, icon set, font package, or hand memoisation.
- Keep fixed 404, 422, 502, and 503 response text free of submitted input and upstream yt-dlp details.
- Tests follow strict red/green TDD, use public interfaces and fakes, inject small bounds instead of creating thousands of entities, and do not use repeat-run loops.

## Review Focus

1. **A syntactically valid ID copied from another channel** must be rejected without creating a video or job — pinned by `test_bulk_rejects_an_id_not_in_this_channel_catalog` in Task 5.
2. **Metadata whose returned `channel_id` differs from the catalog channel** must fail only that row and must not leak the returned ID or metadata — pinned by `test_enrichment_isolates_a_wrong_channel_result_and_continues` in Task 3.
3. **A selection spanning a range change** must retain IDs still present and discard IDs no longer loaded — pinned by `retains only selected rows still present after changing range` in Task 8.
4. **Two concurrent bulk submissions for the same new ID** must produce one `videos` row and one active low-priority ingest job, with one `queued` and one `already_known` result — pinned by `test_concurrent_bulk_submission_queues_each_video_once` in Task 5.
5. **Hostile title/description text from YouTube** must render literally, create no element, and never enter an HTML sink — pinned by `renders hostile metadata as literal text` in Task 8.

---

## File Structure

### New files

- `migrations/versions/0017_channel_catalog.py` — create the persisted catalog-entry/enrichment queue table, constraints, and claim index.
- `common/repo/channel_catalog.py` — catalog snapshot upsert/read, joined known status, bounded enrichment claiming, success/failure/retry transitions.
- `common/channel_catalog.py` — pure discovery and bulk-processing application services over repository/queue ports.
- `services/planner/catalog_enrichment.py` — one bounded planner tick that claims catalog rows and invokes `MetadataSource` outside database transactions.
- `tests/migrations/test_channel_catalog.py` — migration upgrade/downgrade and database constraints.
- `tests/common/repo/test_channel_catalog.py` — repository integration contract.
- `tests/common/test_channel_catalog.py` — pure discovery and bulk unit tests with fakes.
- `tests/services/api/test_routes_channel_catalog.py` — HTTP validation, fixed errors, discovery/read/retry/bulk response contracts.
- `tests/services/api/test_routes_channel_catalog_db.py` — API/repository integration and forced concurrent bulk interleaving.
- `tests/services/planner/test_catalog_enrichment.py` — bounded enrichment, isolation, retry state, and metadata persistence.
- `tests/services/planner/test_catalog_enrichment_wiring.py` — production registry/settings/adapter wiring.
- `web/src/lib/channelCatalog.ts` — generated-schema aliases, range constants, runtime guards, UTC formatter, canonical-link helper.
- `web/src/lib/channelCatalog.test.ts` — pure range/guard/date/link tests.
- `web/src/hooks/useChannelCatalog.ts` — typed catalog queries and discovery/retry/bulk mutations.
- `web/src/routes/ChannelCatalog.tsx` — registration and catalog route, local selection, table, confirmation, and result announcements.
- `web/src/routes/ChannelCatalog.dom.test.tsx` — complete no-network DOM flow and accessibility/error coverage.
- `web/src/routes/ChannelCatalog.source.test.ts` — source-scan protection for text rendering and local-only state.

### Modified files

- `common/models.py` — add catalog row/status/enrichment dataclasses used across repository and services.
- `common/config.py` — add exact bounded enrichment settings.
- `common/repo/videos.py` — expose the existing status projection for catalog joins without duplicating status rules.
- `services/api/deps.py` — add the 300-second discovery catalog dependency while preserving the existing 90-second backfill dependency.
- `services/api/schemas.py` — declare catalog read response models.
- `services/api/routes_read.py` — add the persisted `GET /channels/{channel_id}/catalog` route.
- `services/api/routes_write.py` — add discovery, metadata retry, and bulk-processing request/response routes.
- `services/planner/main.py` — register and wire the bounded `catalog_enrichment` task and advisory lock.
- `_docs/architecture.md` — record the table, planner task, HTTP contracts, and frontend route after code contracts are fixed.
- `web/openapi.json` — regenerated FastAPI contract.
- `web/src/api/schema.d.ts` — regenerated TypeScript declarations.
- `web/src/api/client.types.test.ts` — compile-time catalog request/response assertions.
- `web/src/routes/index.tsx` — register `/channels/:channelId/catalog`.
- `web/src/routes/Shell.tsx` — expose the Add channel navigation control.
- `web/src/routes/routes.test.tsx` — route-table and trailing-slash coverage.
- `web/index.css` — catalog zebra, selected, failed, disabled, disclosure, and narrow-table rules using existing tokens.

## Exact Interfaces

```python
# common/models.py
CatalogEnrichmentState = Literal["pending", "running", "done", "failed"]
VideoProcessingStatus = Literal["done", "processing", "unavailable", "failed", "idle"]

@dataclass(frozen=True, slots=True)
class CatalogVideo:
    channel_id: str
    video_id: str
    position: int
    title: str | None
    description: str | None
    published_at: datetime | None
    enrichment_state: CatalogEnrichmentState
    enrichment_error: str | None
    known: bool
    processing_status: VideoProcessingStatus | None

@dataclass(frozen=True, slots=True)
class CatalogPage:
    channel_id: str
    limit: int
    total_count: int | None
    items: tuple[CatalogVideo, ...]

@dataclass(frozen=True, slots=True)
class CatalogClaim:
    channel_id: str
    video_id: str
    attempts: int
```

```python
# common/repo/channel_catalog.py
CATALOG_LIMITS: tuple[int, ...] = (50, 200, 500, 5000)

def replace_catalog_snapshot(
    conn: psycopg.Connection[Any], channel_id: str, listing: ChannelCatalog, *, limit: int
) -> None: ...

def get_catalog_page(
    conn: psycopg.Connection[Any], channel_id: str, *, limit: int
) -> CatalogPage | None: ...

def claim_enrichment(
    conn: psycopg.Connection[Any], *, batch_size: int
) -> list[CatalogClaim]: ...

def complete_enrichment(conn: psycopg.Connection[Any], meta: VideoMeta) -> bool: ...

def fail_enrichment(
    conn: psycopg.Connection[Any], channel_id: str, video_id: str, *, error_class: str
) -> bool: ...

def retry_enrichment(conn: psycopg.Connection[Any], channel_id: str, video_id: str) -> bool: ...
```

```python
# common/channel_catalog.py
@dataclass(frozen=True, slots=True)
class BulkCatalogResult:
    video_id: str
    outcome: Literal["queued", "already_known", "rejected"]
    job_id: int | None
    reason: Literal["not_in_catalog"] | None


def discover_catalog(
    conn: psycopg.Connection[Any], catalog: CatalogSource, channel_id: str, *, limit: int
) -> CatalogPage: ...

def process_catalog_selection(
    conn: psycopg.Connection[Any], queue: JobQueue, channel_id: str,
    video_ids: Sequence[str], *, max_selected: int = 5000
) -> tuple[BulkCatalogResult, ...]: ...
```

```http
POST /channels/{channel_id}/catalog/discover
{"limit": 50}
→ 200 CatalogPage; performs one bounded flat listing and schedules rows as pending by persistence

GET /channels/{channel_id}/catalog?limit=50
→ 200 CatalogPage; 404 for unregistered/no discovered snapshot; no subprocess and no write

POST /channels/{channel_id}/catalog/{video_id}/metadata/retry
→ 202 {"channel_id": "…", "video_id": "…", "enrichment_state": "pending"}

POST /channels/{channel_id}/catalog/process
{"video_ids": ["aaaaaaaaaaa", "bbbbbbbbbbb"]}
→ 202 {"channel_id": "…", "results": [{"video_id": "…", "outcome": "queued", "job_id": 123, "reason": null}]}
```

```ts
// web/src/hooks/useChannelCatalog.ts
export function useChannelCatalog(channelId: string, limit: CatalogLimit): UseQueryResult<CatalogPage>
export function useDiscoverCatalog(channelId: string): UseMutationResult<CatalogPage, CatalogError, CatalogLimit>
export function useRetryCatalogMetadata(channelId: string): UseMutationResult<MetadataRetry, CatalogError, string>
export function useProcessCatalogSelection(channelId: string): UseMutationResult<BulkCatalogResponse, CatalogError, readonly string[]>
```

### Task 1: Persist catalog snapshots without making uploads known

**Files:**
- Create: `migrations/versions/0017_channel_catalog.py`
- Create: `tests/migrations/test_channel_catalog.py`
- Create: `common/repo/channel_catalog.py`
- Create: `tests/common/repo/test_channel_catalog.py`
- Modify: `common/models.py:69-104`
- Modify: `common/repo/videos.py:356-487`

**Interfaces:**
- Consumes: `ChannelCatalog`, `CatalogEntry`, existing `videos`, `jobs`, and `analyses` tables.
- Produces: the dataclasses and repository signatures in **Exact Interfaces**, plus a reusable `_VIDEO_STATUS_JOINS`/`_VIDEO_STATUS_SQL` projection in `common/repo/videos.py` so catalog and library status cannot drift.

- [ ] **Step 1: Write migration tests that fail because revision 0017 does not exist**

```python
@pytest.mark.integration
def test_0017_catalog_table_enforces_state_and_identity(head_conn: psycopg.Connection[Any]) -> None:
    columns = {row[0] for row in head_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'channel_catalog_entries'"
    )}
    assert {"channel_id", "video_id", "position", "snapshot_limit", "total_count",
            "title", "description", "published_at", "enrichment_state",
            "enrichment_error", "attempts", "discovered_at", "updated_at"} <= columns
    with pytest.raises(psycopg.errors.CheckViolation):
        head_conn.execute(
            "INSERT INTO channel_catalog_entries "
            "(channel_id, video_id, position, snapshot_limit, enrichment_state) "
            "VALUES ('UCx', 'aaaaaaaaaaa', 0, 50, 'unknown')"
        )
```

Also pin downgrade/upgrade round-trip and the checks `position >= 0`, `snapshot_limit IN (50,200,500,5000)`, `attempts >= 0`, and `(enrichment_state = 'failed') = (enrichment_error IS NOT NULL)`.

- [ ] **Step 2: Run the migration test and verify the schema assertion fails**

Run: `. .venv/bin/activate && pytest tests/migrations/test_channel_catalog.py -v`

Expected: FAIL because `channel_catalog_entries` is absent or revision `0016` cannot be imported.

- [ ] **Step 3: Implement revision 0017**

```python
revision: str = "0017"
down_revision: str | None = "0016"

def upgrade() -> None:
    op.execute("""
        CREATE TABLE channel_catalog_entries (
            channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
            video_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position >= 0),
            snapshot_limit INTEGER NOT NULL CHECK (snapshot_limit IN (50, 200, 500, 5000)),
            total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0),
            title TEXT,
            description TEXT,
            published_at TIMESTAMPTZ,
            enrichment_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (enrichment_state IN ('pending', 'running', 'done', 'failed')),
            enrichment_error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (channel_id, video_id),
            CHECK ((enrichment_state = 'failed') = (enrichment_error IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX channel_catalog_claim_idx ON channel_catalog_entries (updated_at, channel_id, position) WHERE enrichment_state = 'pending'")
    op.execute("CREATE INDEX channel_catalog_snapshot_idx ON channel_catalog_entries (channel_id, position, video_id)")
```

`downgrade()` drops indexes through `DROP TABLE channel_catalog_entries`; do not modify the baseline migration.

- [ ] **Step 4: Run migration tests to green**

Run: `. .venv/bin/activate && pytest tests/migrations/test_channel_catalog.py -v`

Expected: PASS.

- [ ] **Step 5: Write repository tests for snapshot replacement, order, known status, and enrichment transitions**

```python
@pytest.mark.integration
def test_snapshot_is_newest_first_and_browsing_does_not_make_videos_known(conn):
    listing = ChannelCatalog(CHANNEL, (
        CatalogEntry("newest00001", "Newest", 10),
        CatalogEntry("older000001", "Older", 20),
    ), 77)
    replace_catalog_snapshot(conn, CHANNEL, listing, limit=50)
    page = get_catalog_page(conn, CHANNEL, limit=50)
    assert page is not None
    assert [(row.video_id, row.position, row.known) for row in page.items] == [
        ("newest00001", 0, False), ("older000001", 1, False)
    ]
    assert conn.execute("SELECT count(*) FROM videos").fetchone() == (0,)
```

Add tests proving: rediscovery updates position/title but preserves `done` metadata; expanding 50→200 keeps the same row identity; a shorter refresh removes stale positions from the active snapshot; known rows return all five existing status values; claims use `FOR UPDATE SKIP LOCKED`, increment attempts, and respect injected `batch_size=2`; completion stores cleaned title/description/date and marks `done`; failure stores only the exception class; retry changes only `failed`→`pending`; missing channel/snapshot returns `None`.

- [ ] **Step 6: Run repository tests and verify they fail on missing interfaces**

Run: `. .venv/bin/activate && pytest tests/common/repo/test_channel_catalog.py -v`

Expected: FAIL on assertions after test-local imports, not during collection; initially define the dataclasses/signatures with `raise NotImplementedError` if needed to preserve assertion-first failure.

- [ ] **Step 7: Implement models, shared status projection, and repository SQL**

Use one transaction for a snapshot. Upsert listed rows with `position = enumerate(entries)`, `snapshot_limit = limit`, and `total_count = listing.total_count`; preserve existing non-null description/date and a `done` state. Delete only rows for that channel whose `position >= len(entries)` or whose ID is absent from the new snapshot, so the table represents the latest loaded range. `get_catalog_page` must use `ORDER BY position ASC, video_id ASC`, derive canonical links later in schemas, and left join the exact library status projection.

Claim with:

```sql
WITH picked AS (
    SELECT channel_id, video_id
    FROM channel_catalog_entries
    WHERE enrichment_state = 'pending'
    ORDER BY updated_at, channel_id, position, video_id
    FOR UPDATE SKIP LOCKED
    LIMIT %(batch_size)s
)
UPDATE channel_catalog_entries AS c
SET enrichment_state = 'running', enrichment_error = NULL,
    attempts = attempts + 1, updated_at = now()
FROM picked
WHERE (c.channel_id, c.video_id) = (picked.channel_id, picked.video_id)
RETURNING c.channel_id, c.video_id, c.attempts
```

Never expose upstream error text; `fail_enrichment(..., error_class=type(exc).__name__)` stores the class name only.

- [ ] **Step 8: Run focused repository and migration tests**

Run: `. .venv/bin/activate && pytest tests/migrations/test_channel_catalog.py tests/common/repo/test_channel_catalog.py -v`

Expected: PASS.

- [ ] **Step 9: Commit the persistence slice**

```bash
git add migrations/versions/0017_channel_catalog.py common/models.py common/repo/channel_catalog.py common/repo/videos.py tests/migrations/test_channel_catalog.py tests/common/repo/test_channel_catalog.py
git commit -m "feat: persist channel catalog snapshots"
```

### Task 2: Discover flat catalogs through a bounded API

**Files:**
- Create: `common/channel_catalog.py`
- Create: `tests/common/test_channel_catalog.py`
- Create: `tests/services/api/test_routes_channel_catalog.py`
- Modify: `services/api/deps.py:104-119`
- Modify: `services/api/schemas.py:77-105`
- Modify: `services/api/routes_read.py:91-388`
- Modify: `services/api/routes_write.py:105-253,455-634`

**Interfaces:**
- Consumes: `CatalogSource.list_uploads(channel_id, *, limit)`, `channel_exists`, `replace_catalog_snapshot`, `get_catalog_page`.
- Produces: `discover_catalog(...)`, `CatalogItemOut`, `CatalogPageOut`, `CatalogDiscoverRequest`, `POST /channels/{channel_id}/catalog/discover`, and `GET /channels/{channel_id}/catalog?limit=`.

- [ ] **Step 1: Write failing pure discovery tests for allowed limits and immediate flat fields**

```python
@pytest.mark.parametrize("limit", [50, 200, 500, 5000])
def test_discover_catalog_accepts_only_named_ranges(conn, catalog, limit):
    page = discover_catalog(conn, catalog, CHANNEL, limit=limit)
    assert page.limit == limit
    assert [item.video_id for item in page.items] == ["newest00001", "older000001"]
    assert all(item.description is None for item in page.items)

@pytest.mark.parametrize("limit", [0, 49, 51, 4999, 5001, True])
def test_discover_catalog_rejects_other_limits_before_listing(conn, catalog, limit):
    with pytest.raises(ValueError, match="catalog limit"):
        discover_catalog(conn, catalog, CHANNEL, limit=limit)
    assert catalog.calls == []
```

Also test unregistered channels before adapter access and `PermanentSourceError(REMOVED)` as an empty list, matching architecture §8.3.1.

- [ ] **Step 2: Run pure discovery tests to red**

Run: `. .venv/bin/activate && pytest tests/common/test_channel_catalog.py -v -k discover`

Expected: FAIL because `discover_catalog` is absent.

- [ ] **Step 3: Implement minimal discovery service**

```python
def discover_catalog(conn, catalog, channel_id, *, limit):
    if isinstance(limit, bool) or limit not in CATALOG_LIMITS:
        raise ValueError("catalog limit must be one of 50, 200, 500, 5000")
    with conn.transaction():
        if not channel_exists(conn, channel_id):
            raise ChannelNotRegistered(channel_id)
    try:
        listing = catalog.list_uploads(channel_id, limit=limit)
    except PermanentSourceError:
        listing = ChannelCatalog(channel_id, (), None)
    with conn.transaction():
        replace_catalog_snapshot(conn, channel_id, listing, limit=limit)
        page = get_catalog_page(conn, channel_id, limit=limit)
    assert page is not None
    return page
```

No transaction remains open while yt-dlp runs.

- [ ] **Step 4: Write failing HTTP tests for read/discover contracts and fixed errors**

Pin exact bodies, including:

```python
def test_discovery_returns_flat_rows_before_metadata(api, catalog):
    response = api.post(f"/channels/{CHANNEL}/catalog/discover", json={"limit": 50})
    assert response.status_code == 200
    assert response.json()["items"][0] == {
        "video_id": "newest00001",
        "title": "Newest",
        "url": "https://www.youtube.com/watch?v=newest00001",
        "description": None,
        "published_at": None,
        "enrichment_state": "pending",
        "enrichment_error": None,
        "known": False,
        "processing_status": None,
    }
```

Add 422 tests for malformed/raw-encoded channel IDs and non-enum limits, asserting connection/catalog dependencies were untouched; 404 for unregistered/no snapshot; 502 for `ToolFailureError`; 503 for transient/rate-limit errors; no reflected marker; GET performs no adapter call; newest-first order survives serialization.

- [ ] **Step 5: Run HTTP tests to red**

Run: `. .venv/bin/activate && pytest tests/services/api/test_routes_channel_catalog.py -v`

Expected: FAIL because routes and schemas are absent.

- [ ] **Step 6: Implement schemas, dependencies, and routes**

```python
class CatalogItemOut(BaseModel):
    video_id: str
    title: str | None
    url: str
    description: str | None
    published_at: UtcDatetime | None
    enrichment_state: Literal["pending", "running", "done", "failed"]
    enrichment_error: str | None
    known: bool
    processing_status: VideoStatus | None

class CatalogPageOut(BaseModel):
    channel_id: str
    limit: Literal[50, 200, 500, 5000]
    total_count: int | None
    items: list[CatalogItemOut]
```

Build `url` with the validated ID, not stored text. Add `CATALOG_DISCOVERY_TIMEOUT_SEC = 300` and a distinct `get_catalog_discovery()` dependency; do not change `get_catalog()`'s 90-second synchronous-backfill timeout. The read route uses `get_read_conn`; discovery uses `get_conn` and maps errors to the existing fixed listing messages.

- [ ] **Step 7: Run discovery/API tests to green**

Run: `. .venv/bin/activate && pytest tests/common/test_channel_catalog.py tests/services/api/test_routes_channel_catalog.py -v -k 'discover or catalog'`

Expected: PASS.

- [ ] **Step 8: Commit the discovery slice**

```bash
git add common/channel_catalog.py tests/common/test_channel_catalog.py services/api/deps.py services/api/schemas.py services/api/routes_read.py services/api/routes_write.py tests/services/api/test_routes_channel_catalog.py
git commit -m "feat: expose bounded channel catalogs"
```

### Task 3: Enrich catalog metadata asynchronously and in bounded batches

**Files:**
- Create: `services/planner/catalog_enrichment.py`
- Create: `tests/services/planner/test_catalog_enrichment.py`
- Create: `tests/services/planner/test_catalog_enrichment_wiring.py`
- Modify: `common/config.py:124-139`
- Modify: `services/planner/main.py:3-24,122-136,263-372`

**Interfaces:**
- Consumes: `claim_enrichment`, `complete_enrichment`, `fail_enrichment`, `MetadataSource.fetch(video_id)`, and `Settings.YTDLP_COOKIE_FILE`.
- Produces: `CATALOG_ENRICH_INTERVAL_SEC: PositiveInt = 30`, `CATALOG_ENRICH_BATCH_SIZE: PositiveInt = 10`, `run_catalog_enrichment(conn, metadata, *, batch_size) -> int`, planner task name `catalog_enrichment`, lock key `(LOCK_NAMESPACE, 10)`.

- [ ] **Step 1: Write failing bounded/failure-isolation tests**

```python
def test_enrichment_isolates_a_wrong_channel_result_and_continues(conn):
    seed_pending(conn, CHANNEL, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    metadata = FakeMetadata({
        "aaaaaaaaaaa": meta("aaaaaaaaaaa", channel_id=OTHER_CHANNEL),
        "bbbbbbbbbbb": meta("bbbbbbbbbbb", channel_id=CHANNEL, description="ready"),
    })
    assert run_catalog_enrichment(conn, metadata, batch_size=2) == 1
    rows = states(conn)
    assert rows["aaaaaaaaaaa"] == ("failed", "ChannelMismatch")
    assert rows["bbbbbbbbbbb"] == ("done", None)
```

Add tests for: injected `batch_size=2` claims only two of three; metadata adapter exception stores class only and leaves other rows progressing; each network call runs with transaction status `IDLE`; return value counts completed rows; retryable failed rows are not automatically hot-looped; a planner restart can claim persisted `pending` rows.

- [ ] **Step 2: Run planner enrichment tests to red**

Run: `. .venv/bin/activate && pytest tests/services/planner/test_catalog_enrichment.py -v`

Expected: FAIL because the planner module is absent.

- [ ] **Step 3: Implement one bounded enrichment tick**

```python
def run_catalog_enrichment(conn, metadata, *, batch_size):
    with conn.transaction():
        claims = claim_enrichment(conn, batch_size=batch_size)
    completed = 0
    for claim in claims:
        try:
            meta = metadata.fetch(claim.video_id)
            if meta.channel_id != claim.channel_id:
                raise ChannelMismatch
        except Exception as exc:
            with conn.transaction():
                fail_enrichment(conn, claim.channel_id, claim.video_id,
                                error_class=type(exc).__name__)
        else:
            with conn.transaction():
                completed += int(complete_enrichment(conn, meta))
    return completed
```

Catch expected metadata/error-taxonomy exceptions plus `ChannelMismatch`; allow `KeyboardInterrupt`/`SystemExit` through. Log channel/video IDs and exception class only, never exception text.

- [ ] **Step 4: Write failing configuration and registry-wiring tests**

Assert defaults and strict positive validation, task order after `reconcile_channels` and before `channel_backfill`, lock-key uniqueness, configured batch size pass-through, and construction of `YouTubeMetadata(timeout=120, cookie_file=settings.YTDLP_COOKIE_FILE)` by the real registry closure.

- [ ] **Step 5: Run wiring tests to red**

Run: `. .venv/bin/activate && pytest tests/services/planner/test_catalog_enrichment_wiring.py tests/common/test_config.py tests/services/planner/test_main.py -v`

Expected: FAIL on absent settings/task.

- [ ] **Step 6: Wire settings, lock, adapter, and task**

Build the metadata adapter inside each task invocation, as channel backfill does. Register:

```python
Task(
    "catalog_enrichment",
    settings.CATALOG_ENRICH_INTERVAL_SEC,
    with_conn(lambda conn: run_catalog_enrichment(
        conn,
        YouTubeMetadata(cookie_file=settings.YTDLP_COOKIE_FILE),
        batch_size=settings.CATALOG_ENRICH_BATCH_SIZE,
    )),
)
```

- [ ] **Step 7: Run planner tests to green**

Run: `. .venv/bin/activate && pytest tests/services/planner/test_catalog_enrichment.py tests/services/planner/test_catalog_enrichment_wiring.py tests/services/planner/test_main.py tests/common/test_config.py -v`

Expected: PASS.

- [ ] **Step 8: Commit asynchronous enrichment**

```bash
git add common/config.py services/planner/catalog_enrichment.py services/planner/main.py tests/services/planner/test_catalog_enrichment.py tests/services/planner/test_catalog_enrichment_wiring.py tests/common/test_config.py tests/services/planner/test_main.py
git commit -m "feat: enrich catalog metadata asynchronously"
```

### Task 4: Expose metadata retry and progressive catalog reads

**Files:**
- Modify: `services/api/routes_write.py`
- Modify: `tests/services/api/test_routes_channel_catalog.py`
- Create: `tests/services/api/test_routes_channel_catalog_db.py`

**Interfaces:**
- Consumes: `retry_enrichment(conn, channel_id, video_id) -> bool` and `get_catalog_page`.
- Produces: `MetadataRetryOut` and `POST /channels/{channel_id}/catalog/{video_id}/metadata/retry`.

- [ ] **Step 1: Write failing route tests for failed-row retry and progressive reads**

```python
def test_retry_changes_only_a_failed_catalog_row_to_pending(client, conn):
    seed_catalog_row(conn, state="failed", error="ToolFailureError")
    response = client.post(f"/channels/{CHANNEL}/catalog/{VIDEO}/metadata/retry")
    assert response.status_code == 202
    assert response.json() == {
        "channel_id": CHANNEL, "video_id": VIDEO, "enrichment_state": "pending"
    }
```

Also prove: retry on non-member is fixed 404; retry on pending/running/done is fixed 409 and does not overwrite metadata; malformed channel/video path is 422 before DB access; one failed row coexists with another `done` row in GET output; subsequent GET changes pending→done without rediscovery.

- [ ] **Step 2: Run retry/progressive tests to red**

Run: `. .venv/bin/activate && pytest tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py -v -k 'retry or progressive'`

Expected: FAIL because the retry route is absent.

- [ ] **Step 3: Implement retry route with fixed outcomes**

Validate both raw path segments using the existing untrusted converter pattern before `get_conn`. In one transaction, distinguish missing row (404) from wrong state (409), and return only the canonical IDs/state. Do not invoke metadata in the request.

- [ ] **Step 4: Run retry/progressive tests to green**

Run: `. .venv/bin/activate && pytest tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py -v -k 'retry or progressive'`

Expected: PASS.

- [ ] **Step 5: Commit retry support**

```bash
git add services/api/routes_write.py tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py
git commit -m "feat: retry failed catalog metadata"
```

### Task 5: Validate and queue selected catalog videos idempotently

**Files:**
- Modify: `common/channel_catalog.py`
- Modify: `tests/common/test_channel_catalog.py`
- Modify: `services/api/routes_write.py`
- Modify: `tests/services/api/test_routes_channel_catalog.py`
- Modify: `tests/services/api/test_routes_channel_catalog_db.py`

**Interfaces:**
- Consumes: `insert_backfill_video`, `PostgresQueue.enqueue(kind, video_id, payload, priority)`, `PRIORITY_BACKFILL == -10`, and persisted channel membership.
- Produces: `process_catalog_selection(...)`, `CatalogProcessRequest`, `CatalogProcessItemOut`, `CatalogProcessResponse`, and `POST /channels/{channel_id}/catalog/process`.

- [ ] **Step 1: Write failing pure tests for order, membership, known rows, and exact queue semantics**

```python
def test_bulk_rejects_an_id_not_in_this_channel_catalog(conn, queue):
    seed_catalog(conn, CHANNEL, ["aaaaaaaaaaa"])
    results = process_catalog_selection(conn, queue, CHANNEL, ["bbbbbbbbbbb"])
    assert results == (BulkCatalogResult(
        "bbbbbbbbbbb", "rejected", None, "not_in_catalog"
    ),)
    assert video_ids(conn) == []
    assert queue.calls == []

def test_bulk_queues_unknown_rows_with_backfill_semantics(conn, queue):
    seed_catalog(conn, CHANNEL, ["aaaaaaaaaaa"])
    [result] = process_catalog_selection(conn, queue, CHANNEL, ["aaaaaaaaaaa"])
    assert result.outcome == "queued"
    assert stored_origin(conn, "aaaaaaaaaaa") == "backfill"
    assert queue.calls == [("ingest", "aaaaaaaaaaa", {"origin": "backfill"}, -10)]
```

Add tests preserving request order, rejecting duplicate IDs at validation, max-selected injection (`max_selected=2` with three IDs), and reporting pre-existing rows as `already_known` with no queue call.

- [ ] **Step 2: Run pure bulk tests to red**

Run: `. .venv/bin/activate && pytest tests/common/test_channel_catalog.py -v -k bulk`

Expected: FAIL because processing is absent.

- [ ] **Step 3: Implement per-ID transactional bulk processing**

Validate the whole input before writes: non-empty, unique, length `<= max_selected`, and every ID syntactically valid. For each ID in request order, open one short transaction, lock its catalog membership row `FOR SHARE`, return `rejected/not_in_catalog` if absent, then call `insert_backfill_video`. If insert loses a race, return `already_known`. If inserted, enqueue:

```python
job_id = queue.enqueue(
    "ingest", video_id,
    payload={"origin": "backfill"},
    priority=PRIORITY_BACKFILL,
)
```

The insert and enqueue share the transaction. If enqueue returns `None` after this transaction inserted the video, read the active/latest ingest job and return its ID; never return `queued` without a checkable job ID.

- [ ] **Step 4: Write failing HTTP and forced-concurrency tests**

```python
@pytest.mark.integration
def test_concurrent_bulk_submission_queues_each_video_once(head_dsn):
    # Hold the first transaction after membership validation, start the second,
    # verify it is blocked with pg_blocking_pids(), then release the first.
    first, second = force_bulk_interleaving(head_dsn, CHANNEL, VIDEO)
    assert sorted([first.outcome, second.outcome]) == ["already_known", "queued"]
    with psycopg.connect(head_dsn) as check:
        assert check.execute("SELECT count(*) FROM videos WHERE video_id=%s", (VIDEO,)).fetchone() == (1,)
        assert check.execute("SELECT count(*) FROM jobs WHERE video_id=%s AND kind='ingest'", (VIDEO,)).fetchone() == (1,)
```

HTTP tests pin 202 for mixed outcomes, exact one-result-per-requested-ID bodies, strict body shape, empty/duplicate/5001 lists as fixed 422 before DB access, malformed IDs without reflection, unregistered channel 404, and second submission as deterministic `already_known`.

- [ ] **Step 5: Run route/concurrency tests to red**

Run: `. .venv/bin/activate && pytest tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py -v -k 'process or bulk or concurrent'`

Expected: FAIL because the route is absent or idempotency assertions fail.

- [ ] **Step 6: Implement strict request/response models and route**

```python
class CatalogProcessRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    video_ids: list[str] = Field(min_length=1, max_length=5000)

    @field_validator("video_ids")
    @classmethod
    def unique_valid_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not is_video_id(v) for v in values):
            raise ValueError("video_ids must be unique canonical YouTube video IDs")
        return values
```

Return 202 even when every item is `already_known`/`rejected`, because the response is the asynchronous bulk command receipt; each item remains independently checkable.

- [ ] **Step 7: Run all backend catalog tests to green**

Run: `. .venv/bin/activate && pytest tests/common/test_channel_catalog.py tests/common/repo/test_channel_catalog.py tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py tests/services/planner/test_catalog_enrichment.py -v`

Expected: PASS.

- [ ] **Step 8: Commit selected processing**

```bash
git add common/channel_catalog.py tests/common/test_channel_catalog.py services/api/routes_write.py tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py
git commit -m "feat: queue selected catalog videos"
```

### Task 6: Freeze the API contract and generated TypeScript interfaces

**Files:**
- Modify: `_docs/architecture.md:282-337,404-439,843-894,972-1002,1742-1753`
- Modify: `web/openapi.json`
- Modify: `web/src/api/schema.d.ts`
- Modify: `web/src/api/client.types.test.ts`
- Modify: `tests/services/api/test_openapi_drift.py` only if the existing generic success-schema assertion exposes a real contract omission.

**Interfaces:**
- Consumes: all Task 2–5 FastAPI models/routes.
- Produces: generated `CatalogPageOut`, `CatalogItemOut`, `MetadataRetryOut`, `CatalogProcessRequest`, and `CatalogProcessResponse` TypeScript types and documented architecture contracts.

- [ ] **Step 1: Add compile-time API calls before regeneration**

```ts
const catalog = await api.get('/channels/{channel_id}/catalog', {
  params: { path: { channel_id: 'UCabc' }, query: { limit: 50 } },
})
expectTypeOf(catalog).toEqualTypeOf<components['schemas']['CatalogPageOut']>()
await api.post('/channels/{channel_id}/catalog/process', {
  params: { path: { channel_id: 'UCabc' } },
  body: { video_ids: ['aaaaaaaaaaa'] },
})
// @ts-expect-error catalog range is a closed enum
await api.get('/channels/{channel_id}/catalog', {
  params: { path: { channel_id: 'UCabc' }, query: { limit: 51 } },
})
```

- [ ] **Step 2: Run typecheck and OpenAPI drift to verify red**

Run: `. .venv/bin/activate && pytest tests/services/api/test_openapi_drift.py -v && bash -lc 'cd web && npm run typecheck'`

Expected: drift FAIL names the new/changed catalog paths; TypeScript FAIL says the paths do not exist.

- [ ] **Step 3: Regenerate OpenAPI and TypeScript declarations**

Run: `. .venv/bin/activate && python -m services.api.openapi_export && bash -lc 'cd web && npm run gen:api'`

Expected: `web/openapi.json` and `web/src/api/schema.d.ts` change; no hand editing of generated declarations.

- [ ] **Step 4: Update architecture with exact implemented contracts**

Add `channel_catalog_entries` DDL, the `catalog_enrichment` planner task/bounds, all four HTTP routes, route-local selection/TanStack ownership, and tests. Preserve the `REMOVED` ambiguity and state explicitly that discovery treats it as an empty listing.

- [ ] **Step 5: Run drift, generated-type check, and typecheck**

Run: `. .venv/bin/activate && pytest tests/services/api/test_openapi_drift.py tests/services/api/test_openapi_export.py -v && bash -lc 'cd web && npm run gen:api:check && npm run typecheck'`

Expected: PASS.

- [ ] **Step 6: Commit contract/docs together**

```bash
git add _docs/architecture.md web/openapi.json web/src/api/schema.d.ts web/src/api/client.types.test.ts tests/services/api/test_openapi_drift.py
git commit -m "docs: define channel catalog contracts"
```

### Task 7: Add typed frontend catalog state and mutations

**Files:**
- Create: `web/src/lib/channelCatalog.ts`
- Create: `web/src/lib/channelCatalog.test.ts`
- Create: `web/src/hooks/useChannelCatalog.ts`
- Modify: `web/src/api/index.ts` only if a public helper type must be exported.

**Interfaces:**
- Consumes: generated schemas and the generic `api` client.
- Produces: `CatalogLimit = 50 | 200 | 500 | 5000`, `CATALOG_RANGES`, `formatCatalogDate`, runtime guards, and the four hooks in **Exact Interfaces**.

- [ ] **Step 1: Write pure failing tests for fixed ranges, UTC dates, canonical links, and response guards**

```ts
it('maps the four labels to exact bounded limits', () => {
  expect(CATALOG_RANGES).toEqual([
    ['Show last 50', 50], ['Show last 200', 200],
    ['Show last 500', 500], ['Show all', 5000],
  ])
})

it('formats dates in UTC under the suite non-UTC timezone', () => {
  expect(formatCatalogDate('2026-09-01T02:00:00Z')).toBe('2026-09-01')
})
```

Guard tests reject missing result entries, invalid enrichment/outcome enums, and malformed arrays without displaying server text.

- [ ] **Step 2: Run pure frontend tests to red**

Run: `bash -lc 'cd web && npm test -- src/lib/channelCatalog.test.ts'`

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement the pure catalog module**

Alias generated types from `components['schemas']`, keep the range tuple `as const`, use string slicing after validating the ISO prefix rather than locale-sensitive `Intl`, and derive canonical URLs only from IDs accepted by the existing `videoId` helper.

- [ ] **Step 4: Write hook tests through a minimal DOM consumer**

In `ChannelCatalog.dom.test.tsx` initially mount a probe component that invokes each hook. Assert exact same-origin requests:

```text
POST /api/channels/{id}/catalog/discover {"limit":50}
GET  /api/channels/{id}/catalog?limit=50
POST /api/channels/{id}/catalog/{video}/metadata/retry (no body)
POST /api/channels/{id}/catalog/process {"video_ids":[...]}
```

Assert discovery seeds/invalidates `['channel-catalog', channelId, limit]`, GET polls every 5 seconds while any row is `pending|running`, stops when all rows are `done|failed`, retry invalidates the channel catalog, and bulk mutation has `retry: 0`.

- [ ] **Step 5: Run hook-focused DOM tests to red**

Run: `bash -lc 'cd web && npm test -- src/routes/ChannelCatalog.dom.test.tsx'`

Expected: FAIL because hooks are absent.

- [ ] **Step 6: Implement typed hooks**

Map every HTTP/network/parse failure to `CatalogError` carrying only kind/status, never server detail. Use query keys `['channel-catalog', channelId, limit]`; after discovery call `queryClient.setQueryData` for that exact key. Poll at `CATALOG_POLL_MS = 5000` only while a visible row is pending/running.

- [ ] **Step 7: Run pure and hook tests to green**

Run: `bash -lc 'cd web && npm test -- src/lib/channelCatalog.test.ts src/routes/ChannelCatalog.dom.test.tsx && npm run typecheck'`

Expected: PASS for the probe tests and typecheck.

- [ ] **Step 8: Commit frontend state layer**

```bash
git add web/src/lib/channelCatalog.ts web/src/lib/channelCatalog.test.ts web/src/hooks/useChannelCatalog.ts web/src/routes/ChannelCatalog.dom.test.tsx web/src/api/index.ts
git commit -m "feat: add channel catalog query hooks"
```

### Task 8: Deliver registration, range browsing, progressive rows, and stable selection UI

**Files:**
- Create: `web/src/routes/ChannelCatalog.tsx`
- Create: `web/src/routes/ChannelCatalog.source.test.ts`
- Modify: `web/src/routes/ChannelCatalog.dom.test.tsx`
- Modify: `web/src/routes/index.tsx:1-35`
- Modify: `web/src/routes/Shell.tsx:1-20`
- Modify: `web/src/routes/routes.test.tsx:25-45`

**Interfaces:**
- Consumes: `POST /channels`, Task 7 hooks, `CatalogPageOut`, React Router route param `channelId`.
- Produces: Add channel form at `/channels/add` (same component without a route ID), catalog at `/channels/:channelId/catalog`, and route-local `Set<string>` selection.

- [ ] **Step 1: Write failing registration/navigation DOM tests**

```ts
it('registers a supported channel reference then opens its default catalog', async () => {
  stubFetch(routeApi({ registration: channelRegistration, discovery: page(50) }))
  const { router } = await mount('/channels/add')
  fireEvent.change(screen.getByLabelText('YouTube channel'), { target: { value: '@example' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
  await settle()
  expect(router.state.location.pathname).toBe(`/channels/${UC}/catalog`)
  expect(lastDiscoveryBody()).toEqual({ limit: 50 })
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Channel catalog')
})
```

Cover fixed 404/422/502/503 registration messages with no reflected body, disabled double submit, visible label/no placeholder-only input, and Add channel nav link.

- [ ] **Step 2: Write failing catalog row/range/progressive-state DOM tests**

Pin: default 50 and all four labels; changing range invokes discovery with the exact limit; newest-first server order remains unchanged; title and safe canonical link appear immediately; pending says `Loading metadata…`; failed says `Metadata unavailable` plus Retry; retry disables while pending; description/date fill after fake-timer poll; known status text and disabled checkbox; UTC date; description disclosure uses `aria-expanded`; zebra classes derive from `position % 2`, not current DOM index.

Include Review Focus tests exactly:

```ts
it('retains only selected rows still present after changing range', async () => {
  // select A and B in 200; refresh 50 returns B and C
  // B remains checked, A is removed from local selection, C is unchecked
})

it('renders hostile metadata as literal text', async () => {
  const hostile = '<img src=x onerror=alert(1)>'
  await mountWithPage(pageWith({ title: hostile, description: hostile }))
  expect(screen.getAllByText(hostile)).toHaveLength(2)
  expect(document.querySelector('img')).toBeNull()
})
```

- [ ] **Step 3: Run route DOM tests to red**

Run: `bash -lc 'cd web && npm test -- src/routes/ChannelCatalog.dom.test.tsx src/routes/routes.test.tsx'`

Expected: FAIL because component/routes are absent.

- [ ] **Step 4: Implement route registration and semantic catalog table**

Register both routes:

```tsx
{ path: 'channels/add', element: <ChannelCatalog /> },
{ path: 'channels/:channelId/catalog', element: <ChannelCatalog /> },
```

The registration form calls `api.post('/channels', {body:{url}})`, navigates using the canonical response `channel_id`, and the catalog route immediately runs discovery at 50. Render a `<table>` with scoped headers: Select, Title, Description, Published, Status. New rows use `<input type="checkbox" aria-label={`Select ${title ?? video_id}`}>`; known rows use the same native control disabled and visible text `Already known · <status>`.

On every successful catalog response, reconcile local selection:

```ts
setSelected((current) => new Set(
  [...current].filter((id) => data.items.some((row) => row.video_id === id && !row.known)),
))
```

Use stable class names `catalog-row catalog-row--even|odd`, plus modifiers for selected/known/failed; visible text carries the state.

- [ ] **Step 5: Add source-scan tests and implementation safeguards**

```ts
for (const file of ['ChannelCatalog.tsx', '../hooks/useChannelCatalog.ts']) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  expect(source).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  expect(source).not.toMatch(/useMemo|useCallback/)
}
```

Also assert no global store import and all external links have both safe attributes.

- [ ] **Step 6: Run registration/catalog tests to green**

Run: `bash -lc 'cd web && npm test -- src/routes/ChannelCatalog.dom.test.tsx src/routes/ChannelCatalog.source.test.ts src/routes/routes.test.tsx && npm run typecheck'`

Expected: PASS.

- [ ] **Step 7: Commit the browse UI**

```bash
git add web/src/routes/ChannelCatalog.tsx web/src/routes/ChannelCatalog.dom.test.tsx web/src/routes/ChannelCatalog.source.test.ts web/src/routes/index.tsx web/src/routes/Shell.tsx web/src/routes/routes.test.tsx
git commit -m "feat: browse progressive channel catalogs"
```

### Task 9: Confirm processing, prevent double submission, and report every outcome

**Files:**
- Modify: `web/src/routes/ChannelCatalog.tsx`
- Modify: `web/src/routes/ChannelCatalog.dom.test.tsx`
- Modify: `web/index.css:10-46,296-341`

**Interfaces:**
- Consumes: `useProcessCatalogSelection(channelId)` and route-local selected IDs.
- Produces: inline confirmation, one bulk request, per-ID live outcomes, and accessible visual states.

- [ ] **Step 1: Write failing confirmation and result DOM tests**

```ts
it('requires one channel-scoped confirmation and reports every result', async () => {
  await mountSelected(['aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc'])
  fireEvent.click(screen.getByRole('button', { name: 'Process 3 selected videos' }))
  expect(screen.getByText(/may use existing subtitles or download audio and transcribe/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm processing' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm processing' }))
  await settle()
  expect(processPosts()).toHaveLength(1)
  expect(processBody()).toEqual({ video_ids: ['aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc'] })
  expect(screen.getByText('Queued: aaaaaaaaaaa')).toBeTruthy()
  expect(screen.getByText('Already known: bbbbbbbbbbb')).toBeTruthy()
  expect(screen.getByText('Rejected: ccccccccccc — not in this channel catalog')).toBeTruthy()
})
```

Also test deterministic catalog-order submission rather than click order, Cancel restores focus to the bulk button, Confirm/Cancel/checkboxes/range controls are disabled while pending, network/500 errors retain selection for retry, success clears queued/already-known/rejected IDs only after all results render, and `aria-live="polite"` announces the outcome summary.

- [ ] **Step 2: Run bulk UI tests to red**

Run: `bash -lc 'cd web && npm test -- src/routes/ChannelCatalog.dom.test.tsx -t "confirmation|result|double|network"'`

Expected: FAIL because confirmation/results are absent.

- [ ] **Step 3: Implement inline confirmation and synchronous double-click guard**

Follow the Ops retry pattern with `confirming`, `confirmRef`, `actionRef`, and `sentRef`. The copy is exactly:

```text
Process N selected videos from this channel? Processing may use existing subtitles or download audio and transcribe when needed.
```

`sentRef.current` changes before `mutate`, so two clicks in one render cannot submit twice. Build IDs by filtering `data.items` in catalog order against the selected set. Render one `<li>` per API result and a summary in an `aria-live="polite"` region; never collapse partial outcomes into one success message.

- [ ] **Step 4: Write failing CSS/state assertions**

Assert catalog rows have alternating `catalog-row--even`/`--odd` classes stable across range changes, selected rows include `catalog-row--selected` plus a checked control, failed rows include `catalog-row--failed` plus failure text, and known rows include `catalog-row--known` plus disabled control/status text.

- [ ] **Step 5: Add token-based catalog styling**

Use existing custom properties only. Add a darker compatible surface token derived from the approved palette, for example `--color-surface-alt: #f3f4f6`, then:

```css
.catalog-row--odd { background: var(--color-surface); }
.catalog-row--even { background: var(--color-surface-alt); }
.catalog-row--selected { box-shadow: inset 4px 0 0 var(--color-focus); }
.catalog-row--known { color: var(--color-muted); }
.catalog-row--failed { box-shadow: inset 4px 0 0 var(--color-danger); }
.catalog-description { max-width: 28rem; overflow-wrap: anywhere; }
```

Keep text/control cues, focus visibility, 44px controls, and `.table-scroll`; do not encode meaning through zebra/outline colour alone.

- [ ] **Step 6: Run all catalog frontend tests to green**

Run: `bash -lc 'cd web && npm test -- src/lib/channelCatalog.test.ts src/routes/ChannelCatalog.dom.test.tsx src/routes/ChannelCatalog.source.test.ts src/routes/routes.test.tsx && npm run lint && npm run build'`

Expected: PASS; Vite build regenerates but does not change already-current API types.

- [ ] **Step 7: Commit end-to-end bulk UI**

```bash
git add web/src/routes/ChannelCatalog.tsx web/src/routes/ChannelCatalog.dom.test.tsx web/index.css
git commit -m "feat: confirm selected catalog processing"
```

### Task 10: End-to-end regression and delivery verification

**Files:**
- Modify only files whose verification exposes a defect; do not broaden scope.

**Interfaces:**
- Consumes: every prior task’s public interface.
- Produces: one verified end-to-end delivery with no contract drift, lint/type errors, regressions, or unstaged generated changes.

- [ ] **Step 1: Run focused backend tests including real PostgreSQL integration**

Run: `. .venv/bin/activate && pytest tests/migrations/test_channel_catalog.py tests/common/test_channel_catalog.py tests/common/repo/test_channel_catalog.py tests/services/api/test_routes_channel_catalog.py tests/services/api/test_routes_channel_catalog_db.py tests/services/planner/test_catalog_enrichment.py tests/services/planner/test_catalog_enrichment_wiring.py -v`

Expected: PASS. Docker Desktop must be running for integration tests.

- [ ] **Step 2: Run the fast Python suite**

Run: `. .venv/bin/activate && pytest -m "not integration"`

Expected: PASS.

- [ ] **Step 3: Run the full Python suite**

Run: `. .venv/bin/activate && pytest`

Expected: PASS in one run; do not use repeat loops.

- [ ] **Step 4: Run required Python lint and types**

Run: `. .venv/bin/activate && ruff check . && mypy .`

Expected: PASS.

- [ ] **Step 5: Run complete frontend verification**

Run: `bash -lc 'cd web && npm run gen:api:check && npm test && npm run lint && npm run build'`

Expected: PASS with no network calls and no generated drift.

- [ ] **Step 6: Inspect the final diff and generated-file cleanliness**

Run: `git status --short && git diff --check && git diff --stat`

Expected: only intended issue #242 implementation files are changed; no secrets, `.env`, reports, research notes, or unrelated untracked directories are staged.

- [ ] **Step 7: Commit any verification-only fixes, if needed**

Stage each concrete file that was changed solely to correct a verification failure, then run:

```bash
git commit -m "fix: complete channel catalog verification"
```

Skip this commit when verification required no changes; never create an empty commit and never stage unrelated files.
