#!/usr/bin/env bash
# Decides which CI jobs a change needs. Reads changed file paths (one per line)
# on stdin and prints `python=`, `frontend=` and `images=` as true/false.
# Anything not listed as skippable runs the job, so an unknown path is safe.
set -euo pipefail

files=$(cat)

# Never affects any job: documentation no test reads.
DOCS='^(docs/|_docs/(team/|process\.md$|plan\.md$|progress\.md$|task-template\.md$|tasks\.md$|design-system\.md$))'
# Frontend sources that python tests do not read. The python tests read
# web/openapi.json, web/package*.json, the web config files and
# web/src/api/schema.d.ts, so those are not listed here.
WEBSRC='^web/src/(?!api/schema\.d\.ts$)'
# Tests that only exercise python code, not the Docker images.
UNIT_TESTS='^tests/(?!.*(docker|image|compose_stack|fixtures/smoke))'

any_outside() { # any changed file that matches none of the given patterns
  local pattern
  pattern=$(IFS='|'; echo "$*")
  [ -n "$files" ] && printf '%s\n' "$files" | grep -vP "$pattern" >/dev/null
}

if [ -z "$files" ]; then
  echo "python=true"; echo "frontend=true"; echo "images=true"; exit 0
fi

# CI itself changed: run everything.
if printf '%s\n' "$files" | grep -qE '^(\.github/workflows/ci\.yml|scripts/ci_changes\.sh)$'; then
  echo "python=true"; echo "frontend=true"; echo "images=true"; exit 0
fi

any_outside "$DOCS" "$WEBSRC" && echo "python=true" || echo "python=false"
printf '%s\n' "$files" | grep -qE '^web/' && echo "frontend=true" || echo "frontend=false"
any_outside "$DOCS" "$WEBSRC" "$UNIT_TESTS" && echo "images=true" || echo "images=false"
