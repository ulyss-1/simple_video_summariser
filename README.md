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
