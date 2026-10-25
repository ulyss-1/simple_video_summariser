# Add channel catalog and selected processing

## Intent

A user can add a YouTube channel, inspect its uploads, and select new videos to enter the existing ingest and transcription pipeline. The catalog shows useful metadata progressively without making the user wait for full metadata extraction across a large channel.

## User flow

The application exposes an Add channel control and menu. The user submits a supported channel reference. After registration, the application opens that channel's catalog and shows the latest 50 uploads by default, newest to oldest.

Range controls are labeled Show last 50, Show last 200, Show last 500, and Show all. Show all remains bounded by the existing 5,000-entry catalog safety cap. Changing the range preserves newest-to-oldest ordering and retains selections that remain in the loaded catalog.

Each row displays the video title, canonical YouTube link, description, and publication date. Titles and links appear as soon as the flat catalog returns. Publication dates and descriptions fill progressively through asynchronous metadata enrichment. Per-row loading and failure states do not block other rows.

Known videos remain visible with their current processing status and cannot be selected. New videos have accessible selection controls. The user confirms one channel-scoped bulk action to queue selected videos for processing.

## Architecture

Channel registration reuses `POST /channels`. A channel catalog API returns a newest-first flat listing with stable video IDs, titles, canonical links derived from validated IDs, known state, and current processing status where applicable.

Metadata enrichment is asynchronous and bounded. Discovery records or returns the flat listing immediately, while background work progressively obtains publication dates and descriptions. The read contract exposes enrichment state per row so the UI can poll or refetch without blocking the whole catalog.

A channel-scoped bulk endpoint accepts selected video IDs. The server validates syntax, channel membership, known state, and the configured range cap rather than trusting browser state. New videos reuse backfill semantics: `origin='backfill'`, low-priority ingest jobs, idempotent creation, and existing downstream subtitle/transcription policy. Known or concurrently added videos are reported deterministically and are not duplicated.

The API contract and generated TypeScript types are updated together. TanStack Query owns catalog and mutation server state; row selection remains route-local UI state.

## UI and accessibility

The catalog follows existing form, table, loading, alert, and confirmation patterns. The expensive bulk action requires confirmation and clearly states that processing may use existing subtitles or download audio and transcribe when needed.

Rows use alternating theme-compatible light and darker surfaces. Selected, known-disabled, loading, and failed states include text, controls, or icons so meaning never relies on color alone. Zebra striping remains stable across range changes and any pagination or virtualization.

Descriptions render as plain React text and may be visually truncated with an accessible expansion control. Dates use the project's UTC display convention. Native links open canonical YouTube watch URLs safely.

## Error handling

Channel validation and registration preserve existing fixed 404, 422, 502, and 503 behavior without echoing untrusted input. Catalog discovery failures show a retryable page-level error while retaining the registered channel. Metadata failures are isolated per row and retryable. Bulk submission reports a checkable result for every selected ID and never silently hides partial outcomes.

Double submission is prevented in the UI and remains idempotent on the server. If catalog membership changes between discovery and submission, the server response identifies rejected or already-known IDs.

## Testing

Backend tests cover bounded 50, 200, 500, and 5,000-entry ranges using injectable small limits; newest-first ordering; known status; progressive enrichment states; channel membership validation; idempotent bulk enqueue; concurrent additions; and partial metadata failures.

Frontend DOM tests cover channel registration, default and alternate range controls, progressive row updates, title/link/description/date rendering, zebra rows, known disabled rows, selection retention, confirmation, double-submit prevention, per-result outcomes, and fixed network/server errors. Tests do not call the network.

OpenAPI drift, generated client type tests, Python lint/types/tests, frontend lint/tests/build, and relevant integration tests remain green.

## Scope boundaries

This design does not force transcription when manual subtitles satisfy current policy, exceed the 5,000-entry safety cap, delete channels, process playlists, or add global selection state. It does not persist arbitrary source URLs; canonical links are derived from validated video IDs.
