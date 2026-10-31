# Deploy-host runbook

How to prepare the Ubuntu server that runs the ytdigest stack
(`compose.yml`, architecture §11) and the #3 transcription bake-off. Follow the
sections in order, then run `ops/host/preflight.sh` to confirm the host is
ready, then do the first deploy, then run the smoke check (§12).

> **This host, specifically.** The target is an Ubuntu 26.10 box (Intel Core
> i7-4712MQ, 4C/8T; 16 GB RAM). The architecture names Ubuntu 26.04 LTS
> (§16.2) for Python 3.14 parity; 26.10 is fine for the Docker path because the
> services run in `python:3.14-slim` containers, so host parity holds at the
> container, not the interpreter build. This same box is also slated to run a
> k3s cluster (issue #210, "Platform rollout"). Nothing here conflicts with
> that — Docker Engine and k3s coexist — but if k3s is installed first, re-run
> `ops/host/preflight.sh` to confirm Docker's `disk`, `cpu` and `memory`
> headroom still clears the floors with k3s also resident.

Commands that change the system are marked **(sudo)**. The rest are read-only
or run as the deploy account.

---

## 1. Prerequisites and version floors

- **OS:** Ubuntu 26.10 (or 26.04 LTS), 64-bit.
- **Docker Engine ≥ 25.0** — the worker healthcheck's `start_interval` in
  `compose.yml` (#58) needs Engine 25.0+.
- **Docker Compose plugin (v2) ≥ 2.24.0** — `deploy.resources.limits` honoured
  outside Swarm (§11.5), `build.additional_contexts` with `service:`,
  `start_interval`, and `up --wait --wait-timeout` all require 2.24.0+. If a
  later Compose feature used by `compose.yml` turns out to need a higher
  version, raise this floor and cite the Compose release note.
- **Hardware floors** (checked by the preflight): ≥ 4 CPUs (so
  `TRANSCRIBER_CPUS=3.0` is accepted — Docker rejects a CPU quota above the
  host count), ≥ 12 GB RAM, ≥ 40 GB free on Docker's data root.

Legacy `docker-compose` v1 (the Python script, `docker-compose` with a hyphen)
is **not** used anywhere; install only the v2 plugin (`docker compose`, a
space).

---

## 2. Install Docker Engine and the Compose v2 plugin

From Docker's official apt repository for Ubuntu. **(sudo)** throughout.

```bash
# 2.1 base tools and keyring
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# 2.2 apt source. VERSION_CODENAME comes from /etc/os-release.
. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 2.3 install engine, CLI, containerd, buildx and the compose v2 plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

> **New Ubuntu release gotcha.** If `sudo apt-get update` reports no Release
> file for `${VERSION_CODENAME}` (Docker's repo lags a brand-new Ubuntu like
> 26.10), pin the previous LTS codename Docker does publish — set
> `VERSION_CODENAME` by hand in step 2.2 to that codename — and note it here.
> Do not build Docker from source.

Enable the daemon at boot, so `restart: unless-stopped` services return after a
reboot. **(sudo)**

```bash
sudo systemctl enable --now docker.service containerd.service
systemctl is-enabled docker        # must print: enabled
docker version --format '{{.Server.Version}}'   # must be >= 25.0
docker compose version --short                   # must be >= 2.24.0
```

**Rootless Docker is not used.** `host-gateway` (how the analyzer reaches the
host's Ollama) and the fixed loopback publish for `web` behave differently under
rootless, and the stack is written for rootful Engine. Do not install rootless
Docker here.

---

## 3. Storage

Docker's images, volumes and build cache must have room. The preflight's `disk`
check wants ≥ 40 GB free on the filesystem holding Docker's data root.

```bash
docker info --format '{{.DockerRootDir}}'    # usually /var/lib/docker
df -h "$(docker info --format '{{.DockerRootDir}}')"
```

If the root filesystem is too small, move the data root **before pulling any
image**. **(sudo)**

```bash
sudo systemctl stop docker
sudo mkdir -p /etc/docker
# edit /etc/docker/daemon.json to add: { "data-root": "/srv/docker" }
sudo rsync -aP /var/lib/docker/ /srv/docker/    # only if moving an existing root
sudo systemctl start docker
docker info --format '{{.DockerRootDir}}'       # confirm the new path
```

**Sizing** (adds up to the preflight's 40 GB default):

| Item | Reserved size |
|---|---:|
| Images: backend, whisper (+~2 GB over backend), web, bake-off (#76) | 8 GB |
| `whisper-models` volume (~3 GB per large model; ~3 GB for the #3 bake-off set) | 3 GB |
| `audio` volume (`AUDIO_MAX_GB`, default 20) | 20 GB |
| `pgdata` volume | 4 GB |
| build cache | 5 GB |
| **Total** | **40 GB** |

Reclaim space with `docker builder prune` and `docker image prune`.
**`docker compose down -v` deletes `pgdata`** — never run it on the server.

---

## 4. Access

- The **`docker` group is root-equivalent** (a member can start a container
  that mounts `/`). Add only named deploy accounts.

  ```bash
  sudo usermod -aG docker <deploy-user>     # (sudo)
  getent group docker                       # list members; audit this
  ```

- Clone the repo over a **read-only deploy key** (or HTTPS with a read-only
  token) into one fixed directory owned by the deploy account. Nobody edits the
  server checkout by hand; updates are `git pull` of `master` plus a rebuild.

---

## 5. Get the repo onto the host

```bash
sudo install -d -o <deploy-user> -g <deploy-user> /opt/ytdigest     # (sudo)
sudo -u <deploy-user> git clone git@github.com:ulyss-1/simple_video_summariser.git /opt/ytdigest
cd /opt/ytdigest
```

All later commands run from `/opt/ytdigest` as the deploy account.

---

## 6. `.env` and secrets

The server's `.env` lives in the checkout root, owned by the deploy account,
mode `600`.

```bash
cp .env.example .env
chmod 600 .env
```

Then edit `.env`:

- **`COMPOSE_FILE=compose.yml`** — add this line. It stops a bare
  `docker compose` on the host from merging the dev overlay
  `compose.override.yml` (which carries source bind mounts and
  `WHISPER_MODEL=tiny`; #59). `.env.example` must **not** set `COMPOSE_FILE`,
  or it would break the dev overlay on developer machines.
- **`POSTGRES_PASSWORD`** — a strong value using only `[A-Za-z0-9_-]` (it is
  inserted unencoded into `DATABASE_URL`; #58). Generate one with:

  ```bash
  tr -dc 'A-Za-z0-9_-' < /dev/urandom | head -c 40; echo
  ```

- **Anthropic, legacy environment mode** — only if running
  `SUMMARIZER=anthropic`: put `ANTHROPIC_API_KEY` in `.env` (mode 600), never in
  a shell profile, shell history, or on a command line. Leave
  `ANTHROPIC_API_KEY_FILE` empty and deploy with the base command below.
- **Ollama mode** — leave both Anthropic variables empty and use the base
  command. No secret file is required.
- **Anthropic, recommended file mode** — create the file outside the checkout
  as the deploy account, without putting the key in a command argument or shell
  history:

  ```bash
  install -d -m 700 "$HOME/.config/ytdigest"
  umask 077
  read -rsp 'Anthropic API key: ' ANTHROPIC_KEY; printf '\n'
  printf '%s' "$ANTHROPIC_KEY" > "$HOME/.config/ytdigest/anthropic_api_key"
  unset ANTHROPIC_KEY
  chmod 600 "$HOME/.config/ytdigest/anthropic_api_key"
  ```

  The deploy account must own the file. Remove the legacy
  `ANTHROPIC_API_KEY` line from `.env`, then set only the non-secret path:

  ```dotenv
  ANTHROPIC_API_KEY_FILE=/home/<deploy-user>/.config/ytdigest/anthropic_api_key
  ```

  A missing variable makes Compose configuration fail; a missing file makes
  deployment fail. Do not create a dummy or empty fallback.

`.env` is gitignored; never commit it.

---

## 7. Networking and egress

Only the reverse proxy's ports are open inbound (the proxy itself is #141);
`web` stays on `127.0.0.1:8080` (§11.4, D13). Outbound HTTPS the host and
containers need:

| Destination | For |
|---|---|
| `www.youtube.com`, `*.googlevideo.com` | video metadata and audio (yt-dlp) |
| `huggingface.co` + its CDN | Whisper models |
| `pypi.org`, `files.pythonhosted.org` | image builds |
| `deb.debian.org` | image builds (Debian base) |
| `registry.npmjs.org` | web image build |
| `registry-1.docker.io` | base images |
| `api.anthropic.com` | only when `SUMMARIZER=anthropic` |

---

## 8. Ollama

The analyzer reaches the host's Ollama at `http://host.docker.internal:11434`
(`extra_hosts: host.docker.internal:host-gateway`). On Linux, Ollama defaults
to `127.0.0.1:11434`, which containers **cannot** reach through host-gateway.
Bind it to an address containers can reach.

**Default choice (used here): bind to the Docker bridge gateway**
(`docker0`, `172.17.0.1`). It is not exposed on any external interface, and
needs no firewall rule. Install the committed drop-in. **(sudo)**

```bash
sudo install -D -m 644 ops/host/ollama.override.conf \
  /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Trade-off: Ollama is no longer on `127.0.0.1`, so host-local clients must point
at the bridge address, and Ollama must start after `docker.service` (the
drop-in sets `After=docker.service`). Pull the model:

```bash
OLLAMA_HOST=172.17.0.1:11434 ollama pull "${OLLAMA_MODEL:-qwen3.5:4b}"
```

**Alternative: bind to `0.0.0.0` with firewall rules** that allow 11434 only
from Docker's default bridge address pool and deny it everywhere else. This
also keeps Ollama available to host-local clients but requires maintained
firewall policy. Set `OLLAMA_HOST=0.0.0.0:11434`, then apply these rules.
**(sudo)**

```bash
sudo ufw allow in on docker0 from 172.17.0.0/16 to any port 11434 proto tcp
sudo ufw deny in to any port 11434 proto tcp
sudo ufw status numbered
```

If Docker uses additional bridge pools, add one equally scoped allow rule per
pool before the deny rule. An active `ufw` default-deny also blocks
container-to-host traffic unless those Docker bridge ranges are allowed. Never
use a broad `ufw allow 11434/tcp`; the bridge-gateway default above avoids this
firewall maintenance.

---

## 9. UID/GID for host-mounted output

All project images run as `app`, UID/GID `10001` (`ops/Dockerfile.backend`,
#55). Named volumes need no host action — Docker sets their ownership. Only a
**host directory bind-mounted for output** (e.g. #76's bake-off report dir)
needs creating with that owner. **(sudo)**

```bash
sudo install -d -o 10001 -g 10001 /srv/ytdigest-bakeoff
```

To read that output as the deploy account without running containers as root,
add the deploy account to group `10001`, or `chmod g+r` the files.

---

## 10. Run the preflight

From the checkout root, as the deploy account:

```bash
ops/host/preflight.sh
```

It prints one `PASS | WARN | FAIL <check>: <detail>` line per requirement and
exits 0 when nothing FAILs (WARNs allowed), 1 when any check FAILs, 2 on a
usage error. It is read-only: it installs nothing, edits no config, never runs
`sudo`, and never prints a secret. Fix every FAIL before deploying; review each
WARN. `ops/host/preflight.sh --help` lists all checks and their threshold
variables.

The container checks run in the backend's pinned Python base image, with
`--pull=never`. On a fresh host that image is absent: the preflight FAILs
`ollama-from-container` and `egress` and prints the exact `docker pull`
command. Run it, then run the preflight again.

---

## 11. First deploy

Migrations run as a standalone one-shot, never automatically on startup
(AGENTS.md; concurrent replicas racing a migration can corrupt the schema).

Ollama or legacy Anthropic environment mode:

```bash
docker compose -f compose.yml run --rm migrate
# The production path never merges the dev overlay.
docker compose -f compose.yml up -d --build
```

Anthropic file mode instead uses both files for migration and startup:

```bash
docker compose -f compose.yml -f compose.anthropic.yml run --rm migrate
docker compose -f compose.yml -f compose.anthropic.yml up -d --build
```

Before and after an Anthropic file-mode deployment, verify that neither the
resolved model nor the analyzer container environment carries the key. These
commands print names and paths only; inspect the output and do not paste the
key into either command:

```bash
docker compose -f compose.yml -f compose.anthropic.yml config --format json \
  | grep -E 'ANTHROPIC_API_KEY|anthropic_api_key'
docker inspect ytdigest-analyzer-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep ANTHROPIC
```

The resolved config must contain only `ANTHROPIC_API_KEY_FILE` and the secret
path/source, never `ANTHROPIC_API_KEY`. The inspect output must contain only
`ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_api_key`. Container names can
differ; obtain the analyzer name with `docker compose ps analyzer`.

Sanity checks:

```bash
# no dev overlay leaked in: no bind mounts, no tiny model
docker compose config | grep -E 'type: bind|WHISPER_MODEL:\s*"?tiny"?' || echo "clean"

docker compose ps          # services healthy
```

`web` runs nginx as a non-root user (`nginx/nginx-unprivileged:1.30-alpine`)
on container port 8080 and is published on `127.0.0.1:8080` only
(`127.0.0.1:8080:8080`); put it behind the host reverse
proxy (#141) for TLS. Updates are `git pull` of `master`, then
`docker compose run --rm migrate` (if there is a new revision) and
`docker compose -f compose.yml up -d --build`. A red "base image digest check"
run means bump the stale base-image digest (nginx, node, python or postgres)
per architecture.md §11.3; a red "stable line superseded" or "line withdrawn
upstream" annotation means move the nginx line as described in the same
paragraph. Then follow the usual update steps. A `postgres`
digest bump recreates `db` on the next `docker compose -f compose.yml up -d
--build`: a short database restart on the same major version, with the data
staying in the `pgdata` volume. After an update, re-run §12.

---

## 12. Smoke check

A manual operator step. It sends one real video through the freshly deployed
stack over the public network (real YouTube through yt-dlp) and the host
Ollama (§8). With the default `SUMMARIZER=ollama` it costs no API money. Pytest
and CI never run it: `tests/test_pipeline_smoke_docker.py` (#137) is the
automated, hermetic counterpart, with a fake `yt_dlp`, a stub Ollama and no
egress. This step proves what #137 cannot: the real extractor, the analyzer
reaching the host Ollama through `host-gateway`, and a real model producing a
valid analysis.

The video is `iG9CE55wbtY` (TED, "Do schools kill creativity?", 1203 s), the
same one #137 replays. It has manual English captions, so the path is
`ingest` then `analyze` with no `transcribe` job.

**Preconditions**

- §11 is done and `docker compose ps` shows the services healthy.
- The Ollama model was pulled (§8).
- Commands run on the deploy host against `127.0.0.1:8080` (the `web` port), never
  through the public tunnel hostname, which Cloudflare Access fronts.

API calls used below (nginx strips `/api`): `POST /api/videos`,
`GET /api/videos/{video_id}`, `DELETE /api/videos/{video_id}` and
`GET /api/ops/jobs`.

### Guard: no spend

Show the analyzer's summarizer and model before submitting anything:

```bash
docker compose exec analyzer printenv SUMMARIZER OLLAMA_MODEL
```

**Stop** unless the first line is `ollama`. Any other value can spend money on
a real key. Note the second line: the pass check compares it with
`analysis.model`.

### Clean start

Delete any earlier copy. It prints only the HTTP status code:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  http://127.0.0.1:8080/api/videos/iG9CE55wbtY
```

- `204` or `404`: continue.
- `409`: a job is still running. Wait for it, or check `GET /api/ops/jobs`
  (see "When it fails"), then repeat the delete.

Why: a re-submit of an existing video answers `200` with `"created": false`
and nothing new runs, so it proves nothing. Delete removes the video's jobs;
the submit below clears the deletion tombstone and starts one new `ingest`.

### Submit

```bash
curl -s -w '\n%{http_code}\n' -X POST http://127.0.0.1:8080/api/videos \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=iG9CE55wbtY"}'
```

Expected: `202` with `"created": true` and a `job_id`. A `200` with
`"created": false` means the clean start was skipped: go back to it. A `422`
means the URL was mistyped. Always send the full watch URL, never a bare ID (an
ID can start with `-`).

### Wait

One loop, polling every 30 s with a deadline of 70 minutes. The deadline is
`INGEST_MAX_RUNTIME_SEC` + `ANALYZE_MAX_RUNTIME_SEC`, 600 + 3600 s by default.
It stops early on `done`, `failed` or `unavailable`, and at the deadline it
prints the last status it saw instead of waiting forever:

```bash
deadline=$((SECONDS + 4200))
while :; do
  status=$(curl -s http://127.0.0.1:8080/api/videos/iG9CE55wbtY |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])') || status=unknown
  status=${status:-unknown}
  echo "$(date +%T) status=$status"
  case "$status" in done|failed|unavailable) break ;; esac
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "deadline reached, last status: $status"
    break
  fi
  sleep 30
done
```

A 4B model on this CPU is slow (architecture.md §16.7), so most of the wait is
the `analyze` job.

### Pass criteria

Read them from `GET /api/videos/iG9CE55wbtY` and `GET /api/ops/jobs`:

- `status` is `done`
- `transcript.source` is `youtube_manual` and `transcript.language` is `en`
- `analysis.model` equals the `OLLAMA_MODEL` printed by the guard
- `analysis.tldr` is non-empty
- the video's jobs are exactly one `ingest` and one `analyze`, both
  `state: done`, with **no** `transcribe` job

```bash
curl -s http://127.0.0.1:8080/api/videos/iG9CE55wbtY | python3 -c '
import json, sys
v = json.load(sys.stdin)
t, a = v.get("transcript") or {}, v.get("analysis") or {}
print("status", v["status"])
print("transcript", t.get("source"), t.get("language"))
print("model", a.get("model"), "| tldr non-empty:", bool(a.get("tldr")))'
```

The jobs come from the listing in "When it fails". The analysis text is a real
model's output, so it differs from #137's stub; only the structure above is
compared.

A `done` with the wrong structure is not a pass. If `transcript.source` is not
`youtube_manual`, or a `transcribe` job exists, real caption detection no
longer matches the recorded fixture. Report it as an issue.

### When it fails

List this video's jobs. `GET /api/ops/jobs` has no video filter, newest first,
so the script filters by `video_id` and follows `next_before_id` (at most five
pages of 200, because on a live server planner polls and backfills also
enqueue jobs):

```bash
python3 - <<'PY'
import json, urllib.request
base = "http://127.0.0.1:8080/api/ops/jobs?limit=200"
url = base
for _ in range(5):
    with urllib.request.urlopen(url) as r:
        page = json.load(r)
    for j in page["items"]:
        if j["video_id"] == "iG9CE55wbtY":
            print(j["kind"], j["state"], j["attempts"], j["error_class"], j["last_error"])
    if page["next_before_id"] is None:
        break
    url = base + "&before_id=" + str(page["next_before_id"])
PY
```

Each line is `kind state attempts error_class last_error`. Then read the worker
logs:

```bash
docker compose logs --tail 200 transcriber analyzer
```

| `error_class` | Meaning and where to look |
|---|---|
| `TRANSIENT_NETWORK` | Egress is blocked or flaky: §7. |
| `RATE_LIMITED` | YouTube throttling or its bot check (`HTTP Error 429`, "Sign in to confirm you're not a bot"). The remedy is the optional cookie file (#63); see the comment in `compose.yml`'s `transcriber` service. |
| `PERMANENT_SOURCE` | The video itself; `status: unavailable` with `removed`, `private`, `geoblocked` or `agegated`. Replace the ID in this section, in a reviewed change, with another public video that has manual English captions. |
| `LLM_UNAVAILABLE` | Ollama binding and model: §8. |
| `LLM_INVALID_OUTPUT` | The model's reply failed validation. It is retried; report a `dead` analyze job as an issue. |

A log line `No supported JavaScript runtime` is a real fault since #134: the
image ships Deno and `yt-dlp-ejs`, so it means the image is not the expected
one (check `docker compose exec transcriber yt-dlp -v`, which lists
`JS runtimes: deno-...`) and formats may be silently missing.

### Cleanup

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  http://127.0.0.1:8080/api/videos/iG9CE55wbtY
```

Expect `204`. You may keep the video instead: the next run's clean start
deletes it anyway.

---

## Public access (Cloudflare Tunnel)

Optional. The tunnel is a **host systemd service** (`cloudflared.service`),
not a compose service: `web` stays on `127.0.0.1:8080` and `cloudflared`
dials out to Cloudflare and forwards to it. Nothing listens inbound. Do not
also run a second connector for the same tunnel in a container.

The service runs `cloudflared --no-autoupdate --config /etc/cloudflared/config.yml
tunnel run`. The config holds no secrets of its own; the credentials JSON
sits next to it and is root-only:

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json

ingress:
  - hostname: dev-ytdigest.devopsdojo.click
    service: http://127.0.0.1:8080
  - service: http_status:404       # required catch-all
```

One-time setup, on a machine where `cloudflared tunnel login` was done for
the zone:

```bash
cloudflared tunnel create ytdigest
cloudflared tunnel route dns <tunnel-id> dev-ytdigest.devopsdojo.click
# route by ID: right after `create`, the name can briefly be "not found"
sudo cp ~/.cloudflared/<tunnel-id>.json /etc/cloudflared/   # then write config.yml above
sudo chmod 600 /etc/cloudflared/<tunnel-id>.json
sudo systemctl enable --now cloudflared
```

Check: `systemctl status cloudflared`, `cloudflared tunnel list` (the tunnel
shows connections), then `curl -I https://dev-ytdigest.devopsdojo.click/`.

The app has no login of its own, so **put a Cloudflare Access policy in front
before sharing the hostname**. In the Zero Trust dashboard (not in this repo):
Access -> Applications -> Add -> Self-hosted, domain
`dev-ytdigest.devopsdojo.click`, policy "Allow" for your email with the
one-time PIN login method. Until that exists the URL is open to anyone, and
`POST /api/videos` spends transcription and LLM time.

Revoke: `sudo systemctl disable --now cloudflared`, then
`cloudflared tunnel delete ytdigest` and remove the DNS record.
