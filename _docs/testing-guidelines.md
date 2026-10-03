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
  - video IDs/URLs before they reach yt-dlp: accept only 11 chars of
    `[A-Za-z0-9_-]`, reject anything else. A leading `-` is valid (e.g.
    `-wNyEUrxzFU`), so never pass a bare ID - always pass the full watch URL
    so yt-dlp cannot read the ID as an option
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

## Flaky or racy behaviour: find the cause, don't loop the test

Running a test 20 or 50 times "to prove it's not flaky" is not evidence and
is not an acceptance criterion. A loop only lowers the odds of seeing a
failure; it never says why it happened, and it costs minutes on every check.
Owner decision, 2026-10-03.

When a test fails intermittently, or a criterion is about a race:

1. **Find the root cause.** Reproduce it once, read the error, name the exact
   condition (for example "the first connect to the container's mapped port
   is refused for a moment after the container reports ready").
2. **Fix the cause or wait on the condition explicitly.** Wait for the
   observable condition with a bounded deadline (30-60 s for infrastructure,
   a few seconds for locks), then fail with a clear message that names what
   was unreachable or never happened. No retries that hide a real failure.
3. **Force the interleaving instead of hoping for it.** For a race, make the
   dangerous order happen every time: hold one transaction open, start the
   competing call, confirm it is blocked (`pg_stat_activity` /
   `pg_blocking_pids`, bounded deadline), then commit or roll back. A
   `threading.Barrier` alone only makes a race *likely*; it can be kept as a
   smoke test, but it is not the proof.
4. **Test the logic deterministically.** Inject the clock, sleep and probe so
   wait/retry logic runs in microseconds with fakes (see
   `tests/test_postgres_readiness.py`).

One run of the suite is the bar. If a test needs repetition to be trusted,
the test is wrong: rewrite it to force the condition. Do not write "passes N
runs in a row", `seq N` loops, `pytest-repeat` or rerun plugins into
acceptance criteria or QA steps. If an issue still contains one, treat it as
superseded by this section: verify the forced-interleaving test instead.
