#!/usr/bin/env bash
# Decides which CI jobs a change needs. Reads changed file paths (one per line)
# on stdin and prints `python=`, `frontend=`, `images=` and `bakeoff=` as
# true/false. For the first three, anything not listed as skippable runs the
# job, so an unknown path is safe. `bakeoff=` is the one exception, by owner
# decision (#316): it is true only for the bake-off set below, so an unknown
# path gives false. The push-to-master run, which sets everything true, is the
# safety net for that.
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

# The bake-off set (#76, #316): the only paths that run the `bakeoff` job, and
# the paths the `images` job ignores. A shared image-test helper that a
# bake-off test imports must be added here (tests/test_ci_changes.py checks).
BAKEOFF='^(ops/Dockerfile\.bakeoff(\.dockerignore)?$|compose\.bakeoff\.yml$|scripts/phase0/|tests/test_bakeoff_image(_docker)?\.py$)'

any_outside() { # any changed file that matches none of the given patterns
  local pattern
  pattern=$(IFS='|'; echo "$*")
  [ -n "$files" ] && printf '%s\n' "$files" | grep -vP "$pattern" >/dev/null
}

if [ -z "$files" ]; then
  echo "python=true"; echo "frontend=true"; echo "images=true"; echo "bakeoff=true"; exit 0
fi

# CI itself changed: run everything.
if printf '%s\n' "$files" | grep -qE '^(\.github/workflows/ci\.yml|scripts/ci_changes\.sh)$'; then
  echo "python=true"; echo "frontend=true"; echo "images=true"; echo "bakeoff=true"; exit 0
fi

any_outside "$DOCS" "$WEBSRC" && echo "python=true" || echo "python=false"
printf '%s\n' "$files" | grep -qE '^web/' && echo "frontend=true" || echo "frontend=false"
any_outside "$DOCS" "$WEBSRC" "$UNIT_TESTS" "$BAKEOFF" && echo "images=true" || echo "images=false"
printf '%s\n' "$files" | grep -qP "$BAKEOFF" && echo "bakeoff=true" || echo "bakeoff=false"
