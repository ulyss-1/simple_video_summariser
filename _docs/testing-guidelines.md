# Testing guidelines

Per-layer approach is in architecture.md §13 — follow it. These rules apply on top.

## Workflow

- Write the test first, from the issue's acceptance criteria. Run it and see it
  fail on an assertion (not an ImportError), then implement until it passes.
- Exempt: Phase 0 measurement scripts (tasks 2–4).

## What to cover

- Boundaries: empty input, exactly-at-limit, one-past-limit (e.g. transcript
  shorter than / equal to one chunk window, attempts == max, last page).
- Invariants over many inputs: use Hypothesis property tests (chunker, VTT parser).
- Untrusted input, at trust boundaries only:
  - video IDs/URLs before they reach yt-dlp (reject leading `-`, non-ID chars)
  - API query/path params (search `q`, pagination, backfill limit)
  - LLM output: invented speakers, malformed JSON, code fences, prompt text
    echoed from the transcript
- Failure paths: every error class in `common/errors.py` maps to the right retry
  behaviour; a raising handler releases its job.
- Resource hygiene: temp files and audio removed, models loaded lazily, list
  endpoints bounded. (Memory growth is watched via metrics, not unit tests.)

## Rules

- No network: recorded yt-dlp fixtures, `FakeSummarizer`; never a real API key.
- Needs Postgres → `@pytest.mark.integration`; everything else runs under
  `pytest -m "not integration"`.
- Deterministic: no sleep, inject the clock, no test-order dependence.
- Test via public interfaces; prefer fakes over mocking internals.
- `tests/` mirrors the package layout; name tests after the behaviour.
