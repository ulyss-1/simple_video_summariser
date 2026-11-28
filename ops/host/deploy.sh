#!/usr/bin/env bash
# Production release deploy (#380). Runbook: _docs/deploy-host.md,
# "Production releases".
#
#   deploy.sh <tag> [--allow-schema-behind]  deploy, or roll back to, a release tag
#   deploy.sh --latest                       the newest v* tag on origin/release (timer)
#   deploy.sh --status                       print the state file
#   deploy.sh --compose <args...>            `docker compose` on prod with the deployed tag
#
# Order: preflight, fetch and verify the tag, build all images, a backup when
# the tag brings a new Alembic revision, `run --rm migrate`, `up -d --wait`,
# a bounded health check. Compose always gets `-p ytdigest -f compose.yml`
# (plus the overlays listed in .env's COMPOSE_FILE), never `down`.
#
# Everything runs inside main(): `git checkout` rewrites this very file while
# it runs, and bash reads a script lazily, so the whole body must be parsed
# before the first command executes.

WORK=""

main() {
  set -Eeuo pipefail
  umask 077

  local PROG="ytdigest-deploy"
  local PROJECT="ytdigest"
  local REMOTE="origin"
  local TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'
  local HERE REPO
  HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  REPO="$(cd -- "$HERE/../.." && pwd)"
  local STATE_DIR="${YTDIGEST_DEPLOY_STATE_DIR:-/var/lib/ytdigest-deploy}"
  local STATE="$STATE_DIR/state.json"
  local LOG="$STATE_DIR/deploy.log"
  local HEALTH_URL="${YTDIGEST_DEPLOY_HEALTH_URL:-http://127.0.0.1:8080/api/healthz}"
  local HEALTH_TIMEOUT="${YTDIGEST_DEPLOY_HEALTH_TIMEOUT_SEC:-180}"
  local HEALTH_INTERVAL="${YTDIGEST_DEPLOY_HEALTH_INTERVAL_SEC:-3}"
  local WAIT_TIMEOUT="${YTDIGEST_DEPLOY_WAIT_TIMEOUT_SEC:-600}"

  local MODE="deploy" TAG="" ALLOW_BEHIND=0 LOG_OK=0
  local -a COMPOSE_ARGS=() FARGS=()
  local STAGE="preflight" PHASE="pre" PREV_REF="" SHA="" HEAD_REV="" DB_REV=""
  local SKIP_MIGRATE=0 SNAPSHOT="" STARTED=""
  local -A ATTEMPT=() GOOD=()

  # ---- output -------------------------------------------------------------
  now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
  log() {
    local line
    line="ts=$(now_iso) prog=$PROG $*"
    printf '%s\n' "$line"
    if ((LOG_OK)); then printf '%s\n' "$line" >>"$LOG"; fi
  }
  # refuse REASON: one line, non-zero, nothing changed.
  refuse() {
    trap - ERR
    printf '%s: refused: %s\n' "$PROG" "$1" >&2
    if ((LOG_OK)); then printf 'ts=%s prog=%s event=refused reason="%s"\n' "$(now_iso)" "$PROG" "$1" >>"$LOG"; fi
    exit 1
  }
  usage() {
    printf 'usage: %s <vX.Y.Z> [--allow-schema-behind] | --latest | --status | --compose <args...>\n' "$0" >&2
    exit 2
  }

  # ---- state file (written only by this script, one object per line) ------
  state_get() { # section key
    [[ -f $STATE ]] || return 0
    sed -n "s/^  \"$1\": {.*\"$2\": \"\\([^\"]*\\)\".*/\\1/p" -- "$STATE"
  }
  load_state() {
    local k
    for k in tag sha deployed_at alembic_head result stage; do
      ATTEMPT[$k]="$(state_get last_attempt "$k")"
      GOOD[$k]="$(state_get last_good "$k")"
    done
  }
  obj() { # name of an associative array -> one JSON object, or null
    local -n o="$1"
    if [[ -z ${o[tag]:-} ]]; then printf 'null'; return; fi
    printf '{"tag": "%s", "sha": "%s", "deployed_at": "%s", "alembic_head": "%s", "result": "%s", "stage": "%s"}' \
      "${o[tag]}" "${o[sha]}" "${o[deployed_at]}" "${o[alembic_head]}" "${o[result]}" "${o[stage]}"
  }
  write_state() {
    local tmp="$STATE.tmp"
    {
      printf '{\n'
      printf '  "last_attempt": %s,\n' "$(obj ATTEMPT)"
      printf '  "last_good": %s\n' "$(obj GOOD)"
      printf '}\n'
    } >"$tmp"
    chmod 600 -- "$tmp"
    mv -f -- "$tmp" "$STATE"
  }
  record() { # result stage
    ATTEMPT=([tag]="$TAG" [sha]="$SHA" [deployed_at]="$STARTED" [alembic_head]="$HEAD_REV" [result]="$1" [stage]="$2")
    if [[ $1 == ok ]]; then
      local k
      for k in "${!ATTEMPT[@]}"; do GOOD[$k]="${ATTEMPT[$k]}"; done
    fi
    write_state
  }

  # ---- failures after the attempt started ----------------------------------
  fail() { # stage message
    trap - ERR
    STAGE="$1"
    printf '%s: FAILED stage=%s: %s\n' "$PROG" "$1" "$2" >&2
    if [[ $PHASE == pre ]]; then
      if [[ -n $PREV_REF ]]; then git -C "$REPO" -c advice.detachedHead=false checkout --quiet "$PREV_REF" 2>/dev/null || true; fi
      printf '%s: nothing was changed: the running containers are untouched\n' "$PROG" >&2
    else
      local prev="${GOOD[tag]:-}" cmd
      if [[ -n $prev && $prev != "$TAG" ]]; then
        cmd="ops/host/deploy.sh $prev"
        if [[ $SKIP_MIGRATE == 0 && -n $DB_REV && $DB_REV != "$HEAD_REV" ]]; then cmd+=" --allow-schema-behind"; fi
        printf '%s: previous tag: %s\n' "$PROG" "$prev" >&2
        printf '%s: rollback: %s\n' "$PROG" "$cmd" >&2
      else
        printf '%s: previous tag: none recorded; fix forward or restore (_docs/backup-restore.md)\n' "$PROG" >&2
      fi
      if [[ -n $SNAPSHOT ]]; then printf '%s: pre-deploy backup snapshot: %s\n' "$PROG" "$SNAPSHOT" >&2; fi
    fi
    if [[ -n $STARTED ]]; then record failed "$1"; fi
    log "event=finish tag=$TAG outcome=failed stage=$1"
    exit 1
  }
  on_err() { fail "$STAGE" "unexpected error at line $1"; }

  # ---- .env, compose files and the dev-overlay guards ---------------------
  env_get() { # KEY -> value of the last KEY= line of .env, quotes stripped
    local v
    v="$(grep -E "^[[:space:]]*$1=" -- "$REPO/.env" | tail -n 1 | sed -E "s/^[[:space:]]*$1=//; s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"']$//")" || true
    printf '%s' "$v"
  }
  compose_files() {
    local cf name entry
    if [[ ${COMPOSE_FILE:-} == *compose.override.yml* ]]; then
      refuse "COMPOSE_FILE in the environment names compose.override.yml (the dev overlay)"
    fi
    [[ -f $REPO/.env ]] || refuse "no .env in $REPO (copy .env.example, mode 600; see the runbook)"
    name="$(env_get COMPOSE_PROJECT_NAME)"
    if [[ -n $name && $name != "$PROJECT" ]]; then
      refuse ".env sets COMPOSE_PROJECT_NAME=$name; production is $PROJECT"
    fi
    cf="$(env_get COMPOSE_FILE)"
    [[ -n $cf ]] || refuse ".env sets no COMPOSE_FILE, so a bare docker compose here would merge compose.override.yml; set COMPOSE_FILE=compose.yml"
    local -a entries
    IFS=':' read -r -a entries <<<"$cf"
    [[ ${entries[0]} == compose.yml ]] || refuse "COMPOSE_FILE in .env must start with compose.yml (got $cf)"
    FARGS=()
    for entry in "${entries[@]}"; do
      case "$entry" in
        compose.yml | compose.anthropic.yml | compose.tunnel.yml) FARGS+=(-f "$entry") ;;
        *compose.override.yml) refuse "COMPOSE_FILE in .env merges compose.override.yml (the dev overlay)" ;;
        *) refuse "COMPOSE_FILE in .env lists an unsupported file: $entry" ;;
      esac
    done
    if [[ -n $(env_get CLOUDFLARED_TUNNEL_ID) && $cf != *compose.tunnel.yml* ]]; then
      refuse "CLOUDFLARED_TUNNEL_ID is set but COMPOSE_FILE does not list compose.tunnel.yml"
    fi
    if [[ -n $(env_get ANTHROPIC_API_KEY_FILE) && $cf != *compose.anthropic.yml* ]]; then
      refuse "ANTHROPIC_API_KEY_FILE is set but COMPOSE_FILE does not list compose.anthropic.yml"
    fi
    if [[ $cf == *compose.tunnel.yml* ]] && command -v systemctl >/dev/null 2>&1 &&
      systemctl is-active --quiet cloudflared.service 2>/dev/null; then
      refuse "host cloudflared.service is active; two connectors on one tunnel split traffic (stop and disable it first)"
    fi
    # Nothing from the caller's shell may retarget the stack.
    unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES \
      COMPOSE_ENV_FILES COMPOSE_PROJECT_DIRECTORY DOCKER_HOST DOCKER_CONTEXT IMAGE_TAG
  }
  dc() { IMAGE_TAG="$TAG" docker compose -p "$PROJECT" "${FARGS[@]}" "$@"; }

  check_dev_containers() {
    local ids line name files mounts envs
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ||
      refuse "docker is not reachable"
    [[ -n $ids ]] || return 0
    # shellcheck disable=SC2086 # ids is a list of container IDs
    while IFS='|' read -r name files mounts envs; do
      [[ -n $name ]] || continue
      if [[ $files == *compose.override.yml* ]]; then
        refuse "container ${name#/} of project $PROJECT was made with compose.override.yml (dev overlay)"
      fi
      if [[ ,$mounts, =~ ,(/app|/home/node/app)(/|,) ]]; then
        refuse "container ${name#/} of project $PROJECT has a dev-overlay source bind mount"
      fi
      if [[ ,$envs =~ ,WHISPER_MODEL=tiny ]]; then
        refuse "container ${name#/} of project $PROJECT runs WHISPER_MODEL=tiny (dev overlay)"
      fi
    done < <(docker inspect --format '{{.Name}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{range .Mounts}}{{if eq .Type "bind"}}{{.Destination}},{{end}}{{end}}|{{range .Config.Env}}{{.}},{{end}}' $ids)
  }

  check_state_dir() {
    [[ -d $STATE_DIR && ! -L $STATE_DIR ]] ||
      refuse "state directory $STATE_DIR is missing (sudo install -d -m 700 -o \"\$USER\" $STATE_DIR)"
    [[ "$(stat -c %u -- "$STATE_DIR")" == "$(id -u)" ]] || refuse "state directory $STATE_DIR is not owned by the running account"
    local mode
    mode="$(stat -c %a -- "$STATE_DIR")"
    (((8#$mode & 8#077) == 0)) || refuse "state directory $STATE_DIR has mode $mode, looser than 700"
    LOG_OK=1
  }

  # ---- git ------------------------------------------------------------------
  g() { git -C "$REPO" "$@"; }
  fetch_release() {
    g fetch --quiet --no-tags "$REMOTE" "+refs/heads/release:refs/remotes/$REMOTE/release" 2>/dev/null ||
      refuse "cannot fetch the release branch from $REMOTE"
  }
  is_release_tag() { # tag -> 0 when its commit is an ancestor of origin/release
    local c
    c="$(g rev-parse --verify --quiet "refs/tags/$1^{commit}")" || return 1
    g merge-base --is-ancestor "$c" "refs/remotes/$REMOTE/release"
  }
  newest_release_tag() {
    local t
    g fetch --quiet --tags "$REMOTE" 2>/dev/null || refuse "cannot fetch tags from $REMOTE (a moved tag is refused, never forced)"
    while IFS= read -r t; do
      [[ $t =~ $TAG_RE ]] || continue
      if is_release_tag "$t"; then printf '%s' "$t"; return 0; fi
    done < <(g tag --list 'v*' | sort -V -r)
    return 0
  }

  # ---- Alembic ----------------------------------------------------------------
  tag_revisions() { # sha -> "rev down" lines
    local line key val
    local -a revs=() downs=()
    while IFS= read -r line; do
      key="${line%%[:= ]*}"
      if [[ $line =~ \"([A-Za-z0-9_]+)\" ]]; then val="${BASH_REMATCH[1]}"; else val=""; fi
      if [[ $key == revision ]]; then revs+=("$val"); else downs+=("$val"); fi
    done < <(g grep -h -E '^(down_)?revision(:[^=]*)?[[:space:]]*=' "$1" -- migrations/versions/ || true)
    printf '%s\n' "${revs[@]}" >"$WORK/revs"
    printf '%s\n' "${downs[@]}" >"$WORK/downs"
  }
  db_revision() {
    local running has
    running="$(dc ps --status running -q db)" || refuse "docker compose ps failed"
    if [[ -z $running ]]; then
      if docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1; then
        refuse "db is not running but volume ${PROJECT}_pgdata exists; start it first: docker compose -p $PROJECT -f compose.yml up -d db"
      fi
      DB_REV=""
      return 0
    fi
    has="$(dc exec -T db psql -U ytdigest -d ytdigest -tAc "SELECT to_regclass('public.alembic_version') IS NOT NULL")" ||
      refuse "cannot read the database revision"
    if [[ ${has//[[:space:]]/} == t ]]; then
      DB_REV="$(dc exec -T db psql -U ytdigest -d ytdigest -tAc "SELECT version_num FROM alembic_version")" ||
        refuse "cannot read the database revision"
      DB_REV="${DB_REV//[[:space:]]/}"
    else
      DB_REV=""
    fi
  }

  # ---- arguments --------------------------------------------------------------
  (($#)) || usage
  while (($#)); do
    case "$1" in
      --allow-schema-behind) ALLOW_BEHIND=1 ;;
      --latest) MODE="latest" ;;
      --status) MODE="status" ;;
      --compose) MODE="compose"; shift; COMPOSE_ARGS=("$@"); break ;;
      -h | --help) usage ;;
      -*) refuse "unknown option $1" ;;
      *)
        [[ -z $TAG ]] || usage
        TAG="$1"
        ;;
    esac
    shift
  done
  if [[ $MODE == deploy ]]; then
    [[ -n $TAG ]] || usage
    [[ $TAG =~ $TAG_RE ]] || refuse "tag '$TAG' does not match ^v[0-9]+.[0-9]+.[0-9]+\$"
  elif [[ -n $TAG ]]; then
    usage
  fi

  if [[ $MODE == status ]]; then
    if [[ -f $STATE ]]; then cat -- "$STATE"; else printf 'no deploy recorded in %s\n' "$STATE"; fi
    return 0
  fi

  cd -- "$REPO"   # compose files and .env are relative to the checkout
  check_state_dir
  load_state

  if [[ $MODE == compose ]]; then
    local a
    for a in "${COMPOSE_ARGS[@]}"; do
      case "$a" in -v | --volumes | --volumes=*) refuse "never remove production volumes" ;; esac
    done
    TAG="${GOOD[tag]:-}"
    [[ -n $TAG ]] || refuse "no successful deploy recorded, so there is no tag to use"
    compose_files
    dc "${COMPOSE_ARGS[@]}"
    return
  fi

  # One deploy at a time. A timer firing during a running deploy does nothing.
  exec 9>>"$STATE_DIR/deploy.lock"
  if ! flock -n 9; then
    if [[ $MODE == latest ]]; then
      log "event=skip reason=locked detail=\"another deploy is running; nothing done\""
      return 0
    fi
    refuse "another deploy is running (lock $STATE_DIR/deploy.lock)"
  fi

  if [[ $MODE == latest ]]; then
    fetch_release
    TAG="$(newest_release_tag)"
    if [[ -z $TAG ]]; then
      log "event=skip reason=no-release-tag"
      return 0
    fi
    if [[ ${ATTEMPT[tag]:-} == "$TAG" && ${ATTEMPT[result]:-} != ok ]]; then
      log "event=skip tag=$TAG reason=last-attempt-failed detail=\"run ops/host/deploy.sh $TAG by hand\""
      return 0
    fi
  fi

  # Re-running the deployed tag: a no-op that writes one log line.
  if [[ ${ATTEMPT[tag]:-} == "$TAG" && ${ATTEMPT[result]:-} == ok &&
    "$(g rev-parse --verify --quiet HEAD || true)" == "${ATTEMPT[sha]:-}" ]]; then
    log "event=noop tag=$TAG detail=\"already deployed\""
    return 0
  fi

  # ---- preflight ----------------------------------------------------------
  STAGE="preflight"
  local tool
  for tool in git docker curl flock; do
    command -v "$tool" >/dev/null 2>&1 || refuse "$tool is not installed"
  done
  [[ -z "$(g status --porcelain)" ]] || refuse "the checkout $REPO has uncommitted or untracked changes"
  compose_files
  check_dev_containers

  # ---- fetch and verify the tag --------------------------------------------
  STAGE="fetch"
  log "event=start tag=$TAG"
  [[ $MODE == latest ]] || fetch_release
  if [[ $MODE == deploy ]]; then
    g fetch --quiet --no-tags "$REMOTE" "refs/tags/$TAG:refs/tags/$TAG" 2>/dev/null ||
      refuse "cannot fetch tag $TAG from $REMOTE (missing, or it moved: a moved tag is never forced)"
  fi
  SHA="$(g rev-parse --verify --quiet "refs/tags/$TAG^{commit}")" || refuse "tag $TAG not found"
  g merge-base --is-ancestor "$SHA" "refs/remotes/$REMOTE/release" ||
    refuse "tag $TAG is not an ancestor of $REMOTE/release"

  WORK="$(mktemp -d)"
  trap 'rm -rf -- "$WORK"' EXIT
  tag_revisions "$SHA"
  HEAD_REV="$(grep -vxF -f "$WORK/downs" "$WORK/revs" || true)"
  [[ -n $HEAD_REV && $HEAD_REV != *$'\n'* ]] ||
    refuse "tag $TAG does not have exactly one Alembic head"
  db_revision
  if [[ -n $DB_REV && $DB_REV != "$HEAD_REV" ]] && ! grep -qxF -- "$DB_REV" "$WORK/revs"; then
    if ((ALLOW_BEHIND == 0)); then
      refuse "the database is at Alembic revision $DB_REV, newer than $TAG's head $HEAD_REV; pass --allow-schema-behind to run old code on the newer schema"
    fi
    SKIP_MIGRATE=1
    log "event=schema-behind db=$DB_REV head=$HEAD_REV detail=\"--allow-schema-behind: migrate is skipped\""
  fi

  # ---- from here on an attempt is recorded ----------------------------------
  STARTED="$(now_iso)"
  trap 'on_err $LINENO' ERR
  record running build
  PREV_REF="$(g symbolic-ref --quiet --short HEAD || g rev-parse HEAD)"
  g -c advice.detachedHead=false checkout --quiet --detach "$SHA" || fail checkout "cannot check out $TAG"

  STAGE="build"
  log "event=stage stage=build tag=$TAG"
  dc build || fail build "docker compose build failed"

  STAGE="backup"
  if ((SKIP_MIGRATE == 0)) && [[ -n $DB_REV && $DB_REV != "$HEAD_REV" ]]; then
    log "event=stage stage=backup db=$DB_REV head=$HEAD_REV"
    "$REPO/ops/host/pg-backup.sh" >"$WORK/backup.out" 2>&1 || {
      tail -n 5 -- "$WORK/backup.out" >&2
      fail backup "pre-deploy backup failed; nothing was migrated"
    }
    SNAPSHOT="$(grep -oE 'snapshot=[0-9a-f]+' -- "$WORK/backup.out" | tail -n 1 | cut -d= -f2 || true)"
    [[ -n $SNAPSHOT ]] || fail backup "pre-deploy backup printed no snapshot id"
    log "event=backup snapshot=$SNAPSHOT"
  fi

  PHASE="post"
  if ((SKIP_MIGRATE == 0)); then
    STAGE="migrate"
    log "event=stage stage=migrate"
    dc run --rm migrate || fail migrate "alembic upgrade head failed"
  fi

  STAGE="up"
  log "event=stage stage=up"
  if ((SKIP_MIGRATE == 1)); then
    local -a services=()
    local s
    while IFS= read -r s; do
      [[ -n $s && $s != migrate && $s != deps ]] && services+=("$s")
    done < <(dc config --services)
    dc up -d --wait --wait-timeout "$WAIT_TIMEOUT" --no-build --no-deps "${services[@]}" ||
      fail up "docker compose up --wait failed"
  else
    dc up -d --wait --wait-timeout "$WAIT_TIMEOUT" --no-build || fail up "docker compose up --wait failed"
  fi

  STAGE="health"
  local deadline
  deadline=$(($(date +%s) + HEALTH_TIMEOUT))
  until curl -fsS --max-time 5 -o /dev/null -- "$HEALTH_URL"; do
    if (($(date +%s) >= deadline)); then
      fail health "$HEALTH_URL not healthy within ${HEALTH_TIMEOUT}s"
    fi
    sleep "$HEALTH_INTERVAL"
  done

  trap - ERR
  record ok done
  log "event=finish tag=$TAG sha=$SHA outcome=ok alembic_head=$HEAD_REV${SNAPSHOT:+ snapshot=$SNAPSHOT}"
}

main "$@"; exit $?
