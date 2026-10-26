# ytdigest

ytdigest produces structured, queryable summaries of talking-head, podcast and
interview videos from YouTube. You can submit a single video URL, or subscribe
to channels so that new uploads are fetched, transcribed (from subtitles, or
with Whisper when there are none), summarized by an LLM and stored in Postgres,
where earlier analyses can be retrieved and compared. For setup, commands and
contribution rules see [AGENTS.md](AGENTS.md); the design is in
[`_docs/architecture.md`](_docs/architecture.md).

## Regenerating API types

The frontend's API types are generated from the backend's OpenAPI schema and
never written by hand (architecture.md 9, D12).

**When to run it:** after any change to a route, a request or response model,
or a status code.

**The commands:** activate `.venv`, export the schema, then generate the types.
`npm` needs a login shell (`bash -lc`) because Node comes from nvm
(AGENTS.md, Environment).

```sh
. .venv/bin/activate
python -m services.api.openapi_export && (cd web && bash -lc 'npm run gen:api')
```

**What to commit:** `web/openapi.json` and `web/src/api/schema.d.ts` go in the
same commit as the backend change.

**What failures mean:**

- The drift test (`tests/services/api/test_openapi_drift.py`) failing means
  `web/openapi.json` no longer matches the backend. Its message lists the paths
  added, removed or changed. Run the commands above.
- `npm run gen:api:check` failing means `web/src/api/schema.d.ts` no longer
  matches `web/openapi.json`. Run `npm run gen:api`.
- `npm run build` regenerates `schema.d.ts` first, then type-checks, so a
  changed response shape surfaces as a TypeScript error where the frontend uses
  it.

## CI

`.github/workflows/ci.yml` runs on every pull request, every push to `master`
and on demand. It has three jobs that run in parallel, so a red run says which
area broke. Every step is the same command as in
[AGENTS.md](AGENTS.md) -> Commands, so a red step is reproduced locally by
running that command in WSL (use `bash -lc` for the `npm` ones).

- `python`: installs `ffmpeg` (AGENTS.md -> Setup; the audio tests run the real
  binaries), then `ruff check .`, `mypy .`, `pytest -m "not integration"`, then
  `pytest -m "integration and not image"` (testcontainers starts Postgres on
  the runner's Docker). The fast step includes the OpenAPI drift test and
  `tests/test_requirements_pinned.py`, which fails on any requirement in
  `requirements.backend.txt`, `requirements.whisper.txt` or the `dev` extra
  that is not an exact `name==version` pin, and `tests/test_requirements_lock.py`,
  which fails when a `requirements.*.lock` no longer matches its input. The job
  installs from the backend and dev locks with `--require-hashes`, then the
  project itself, then fails if an installed package differs from a lock or is
  in neither.
- `frontend`, in `web/`: `npm ci`, `npm run --if-present gen:api:check`,
  `npm run --if-present lint`, `npm test`, `npm run build`,
  `npm audit --audit-level=high`.
- `images`: `pytest -m "image and not bakeoff"`, which builds the backend
  image, then the whisper image (`FROM` the backend tag) and the frontend
  image, and runs their runtime checks.
- `bakeoff`: `pytest -m "image and bakeoff"`, the bake-off image tests (#76).
  It runs only when a bake-off file changes (the path set is in
  `scripts/ci_changes.sh`) and always on a push to `master`; on other PRs it
  shows as Skipped (#316).

A further gated job `e2e` runs the Playwright browser suite once
`web/package.json` has a `test:e2e` script (#177); until then it passes in a
few seconds without installing anything.

Making these checks required for merging is a repository setting (branch
protection), not a file in this repo.
