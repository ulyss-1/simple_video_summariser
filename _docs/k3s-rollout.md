# k3s rollout plan (issue #210)

Status: PLAN ONLY. Nothing in this document has been run. It changes no code,
commits no manifests, and the snippets below are the plan's reference, not
files in the repo. Written 2026-11-07. Relates to #210, #380, #408, #150, #407,
#393, #410.

How to read the command marks:

- Unmarked commands were checked against the official page cited in section
  12 (fetched 2026-11-07).
- **UNVERIFIED** means the command is standard practice but the official page
  could not be fetched or did not show it. Dry-run it before relying on it.
- **(sudo)** changes the system. Everything else is read-only or runs as the
  deploy account.

---

## 0. Read this first: the host, and the honest recommendation

### 0.1 The host is not what AGENTS.md and #380 say it is

The brief for this plan described a Windows laptop running Ubuntu inside WSL2
with Docker Desktop. Read-only probes on 2026-11-07 show otherwise:

| Probe                                 | Observed                                                                                                                                                                                                                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/proc/version`                       | stock Ubuntu `7.0.0-38-generic`, not a `microsoft-standard-WSL2` kernel                                                                                                                                                     |
| `/sys/class/dmi/id/product_name`      | `HP ProBook 640 G1` (bare metal), CPU i7-4712MQ, 8 threads                                                                                                                                                                  |
| `/etc/wsl.conf`, `/run/WSL`, `/mnt/c` | none exist                                                                                                                                                                                                                  |
| `/` filesystem                        | LVM (`ubuntu--vg-ubuntu--lv`), 455 GB, 88 GB free                                                                                                                                                                           |
| Docker                                | `docker.service` in the distro (`dockerd -H fd:// --containerd=/run/containerd/containerd.sock`), `docker info` says OS "Ubuntu 26.04.1 LTS", root `/var/lib/docker`, cgroup v2 with the systemd driver. Not Docker Desktop |
| Network                               | real NIC `wlo1` (Wi-Fi) `192.168.9.x`, `docker0` `172.17.0.1`, Tailscale and ZeroTier interfaces                                                                                                                            |
| Firewall                              | `ufw` is active                                                                                                                                                                                                             |
| Memory                                | 14 GiB total, about 2.3 GiB available, 4 GiB swap file plus 7.3 GiB zram that is nearly full; one Python process holds about 4.9 GB RSS                                                                                     |
| Other Docker projects on the daemon   | `ytdigest`, `ytdigest-dev`, plus unrelated `helix-*` and `monitoring_stack` volumes                                                                                                                                         |

`_docs/deploy-host.md` (the "This host, specifically" box) already describes
this machine as an i7-4712MQ Ubuntu box, so it agrees with the probes.
`AGENTS.md` ("All work happens inside WSL ... Docker Desktop") and #380 ("WSL
Ubuntu, Docker Desktop") are stale for this machine, or describe a second
machine (the owner's Windows dev laptop). **Owner decision 0 (section 10) is to
confirm which machine is production.** This plan is written for the machine
observed. Section 2.4 gives the WSL2 verdict for the other case.

### 0.2 Recommendation

1. **Do not move production from Compose to k3s on this laptop now.** The
   benefit of k3s in #210 comes from GitOps, a separate always-on control
   plane and a GPU worker. A single-node k3s on this machine gives none of
   that, and costs memory the machine does not have: 2.3 GiB is available while
   everything runs, and the Whisper container alone is allowed 6 GiB. k3s
   (server, containerd, CoreDNS, metrics-server, local-path provisioner) is
   roughly 0.7 to 1.2 GiB resident at idle (engineering estimate, not
   measured; measure in phase 1).
2. **If the goal is k3s, build it on a dedicated node first** (the "main node"
   of #210: always on, wired, not a dev workstation, 16 GB or more), rehearse
   the whole runbook there against a restic restore of production data, and
   only then cut over. Compose stays the production path until that
   rehearsal passes. This is the honest "move to a real Linux host first"
   answer: this machine already is real Linux, but it is also the developer's
   daily laptop (browser, editor, `ytdigest-dev`, `helix-*` and monitoring
   stacks share it), on Wi-Fi, and it sleeps with the lid.
3. **If the owner still wants k3s here,** it is workable with these
   conditions: Docker stays installed (other projects need it, so coexistence
   is permanent, not a stepping stone), `ytdigest-dev` and the unrelated
   stacks are stopped while k3s production runs, the lid-close and suspend
   behaviour is fixed (2.2), and the transcriber limit is revisited (4.6).
   Sections 3 to 9 are written so they work for either host. Where they differ,
   the text says so.
4. **WSL2 inside Windows is not recommended** for production k3s (2.4).

| Option                                                                 | Pros                                                           | Cons                                                                                         |
| ---------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| A. Keep Compose here (status quo, #380)                                | Works today, backups and smoke check exist, no new memory cost | No GitOps; #210 not delivered                                                                |
| B. Single-node k3s on this laptop                                      | No new hardware, can rehearse here                             | Memory pressure, Wi-Fi, suspend, shares a daemon with other projects, dev and prod mixed     |
| C. k3s on a dedicated node, laptop later as a worker (the #210 target) | Matches #210; separates dev from prod; room for ArgoCD         | Needs a machine; the laptop worker needs the join steps and a node taint (out of scope here) |
| D. k3s in WSL2 on Windows                                              | Reuses a Windows PC                                            | Sleep, reboot and idle-shutdown take the cluster down; NAT networking; see 2.4               |

Recommended order: A now, C as the target, B only as a time-boxed rehearsal.

---

## 1. Goals, non-goals and current-state inventory

### 1.1 Goals

- A runnable, reversible path from the Compose stack to k3s with a
  fresh verified backup before and a documented way back.
- A k3s install that does not break Docker or the other projects.
- Public access through a Cloudflare Tunnel with a Cloudflare Access email
  allow-list, with no inbound port opened on the host.
- Migrations still never run automatically on startup (AGENTS.md).

### 1.2 Non-goals

- ArgoCD, GitLab/Forgejo CI, Istio and the GPU worker join (#210 items 4 to
  7). They come after the app runs on k3s.
- Helm charts. The reference is plain manifests plus Kustomize (4.2).
- High availability. This is one node.
- Changing application code. Where the plan needs code (a backup script that
  works without `docker compose`, an immutable image tag), it is listed as a
  proposed follow-up in section 10.3, not done here.

### 1.3 Inventory of the Compose stack (`compose.yml`, project `ytdigest`)

| Service       | Image / build                                | Command                                                    | State and ports                                                   | Notes                                                                                               |
| ------------- | -------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `db`          | `postgres:18-alpine@sha256:10a5c69a...`      | image default                                              | volume `pgdata` mounted at `/var/lib/postgresql` (not `.../data`) | healthcheck `pg_isready -U ytdigest -d ytdigest`; network `internal` (no egress)                    |
| `migrate`     | builds `ytdigest-backend:latest`             | `alembic upgrade head`                                     | none                                                              | one-shot, `restart: "no"`; the others wait for it                                                   |
| `api`         | `ytdigest-backend:latest`                    | `uvicorn services.api.main:app --host 0.0.0.0 --port 8000` | none                                                              | healthcheck `curl -fsS http://localhost:8000/healthz`                                               |
| `web`         | `ytdigest-web:latest` (nginx, user 101)      | image default                                              | listens on 8080; published `127.0.0.1:8080`                       | nginx proxies `/api` to `api:8000` using Docker's resolver `127.0.0.11` (`ops/nginx.conf`, see 4.5) |
| `planner`     | `ytdigest-backend:latest`                    | `python -m services.planner.main`                          | volume `audio` at `/data/audio`                                   | exactly one replica (hard constraint); heartbeat health; stop grace 30 s                            |
| `analyzer`    | `ytdigest-backend:latest`                    | `python -m services.analyzer.main`                         | none                                                              | reaches host Ollama at `http://host.docker.internal:11434`; the only service that gets API keys     |
| `transcriber` | builds `ytdigest-whisper:latest` (on `deps`) | `python -m services.transcriber.main`                      | volumes `audio` at `/data/audio`, `whisper-models` at `/models`   | limit 3.0 CPUs, 6 GiB; `WHISPER_THREADS=3`, `large-v3`, `int8`                                      |

Networks: `internal` (no route out: db, migrate), `edge` (api, web),
`egress` (planner, analyzer, transcriber). In Kubernetes the first two
become NetworkPolicy work (4.8); the third is just the default.

Sizes on 2026-11-07 (read-only: `docker system df -v`, journal): `ytdigest_pgdata`
69.5 MB, `ytdigest_audio` 32 MB, `ytdigest_whisper-models` 3.09 GB, last
backup dump 438 KB. The database is tiny, so dump and restore take seconds.
The whisper models are the only large item to copy (or re-download from
Hugging Face).

Environment variables (from `compose.yml` and `.env.example`):

- Shared by every backend service: `DATABASE_URL`, `SUMMARIZER`,
  `ANTHROPIC_BATCH`, `OLLAMA_MODEL`, `ANTHROPIC_MODEL`, `PROMPT_VERSION`,
  `CHUNK_SEC`, `OVERLAP_SEC`, `LOG_FORMAT=json`.
- Planner: `POLL_INTERVAL_SEC`, `RECONCILE_INTERVAL_SEC`, `AUDIO_TTL_DAYS`,
  `AUDIO_MAX_GB`, `AUDIO_KEEP`, `SKIP_SHORTS`, `SWEEP_MAX_JOBS_ANTHROPIC`,
  `SWEEP_MAX_USD_ANTHROPIC`.
- Analyzer: `OLLAMA_HOST`, `OLLAMA_NUM_CTX`, `OLLAMA_OUTPUT_RESERVE_TOKENS`,
  `OLLAMA_TIMEOUT_SEC`, `ANALYZE_MAX_RUNTIME_SEC`, and the secrets
  `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
  `CEREBRAS_API_KEY`, `PUTER_AUTH_TOKEN`, `FREE_LLM_LOCAL_FALLBACK`.
- Transcriber: `WHISPER_MODEL`, `WHISPER_COMPUTE`, `WHISPER_THREADS`,
  `PREFER_WHISPER`, `AUTO_CAPTION_FALLBACK`, `SKIP_SHORTS`,
  `MAX_ATTEMPTS_TRANSCRIBE`, `AUDIO_KEEP`, `AUDIO_MAX_GB`, optional
  `YTDLP_COOKIE_FILE` (a credential file, mount read-only).

Secrets: `POSTGRES_PASSWORD` (also inside `DATABASE_URL`), the optional LLM
API keys, the optional yt-dlp cookie file, the restic repository password and
R2 key pair in `/etc/ytdigest-backup/` (not read or touched by this plan), and
the Cloudflare tunnel token (section 7).

Host-side things that are not containers:

- Ollama is a systemd service on the host. Its drop-in
  (`ops/host/ollama.override.conf`) binds it to `172.17.0.1:11434`, which is
  the `docker0` address, with `After=docker.service`. On this host it also
  listens on `127.0.0.1:11434` (probe: `ss -ltn`).
- Backups: systemd timers `ytdigest-pg-backup.timer` (daily) and
  `ytdigest-pg-backup-fresh.timer` run `pg-backup.sh`, which calls
  `docker compose -p ytdigest -f compose.yml` against the `db` service, dumps
  with `pg_dump -Fc`, uploads with restic to Cloudflare R2 and keeps
  `--keep-within 14d --keep-last 3` (#150, `_docs/backup-restore.md`).
  Failure notification is off by decision (#407).
- A `cloudflared` binary (2026.10.7) is installed and a locally-managed
  tunnel config exists under the deploy user's home, but `cloudflared.service`
  is inactive. So nothing is publicly exposed today (#408 is open).
- Resource shape (#393): the local Ollama model and Whisper must never run at
  once; a Postgres advisory lock per database enforces it. Different databases
  do not exclude each other (#410).

---

## 2. Prerequisites and decisions

### 2.1 What the host must satisfy

- Ubuntu 26.04, systemd, cgroup v2 (already true here:
  `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`).
- 2 cores and 2 GB RAM is the documented k3s server minimum (docs.k3s.io
  requirements). That is a floor for k3s alone. The application adds the
  limits in 4.6; the transcriber's 6 GiB limit is the issue on a 14 GB host.
- Free disk: the root filesystem has 88 GB free; k3s data lives under
  `/var/lib/rancher/k3s` and the local-path volumes under it too
  (default path `/var/lib/rancher/k3s/storage`, UNVERIFIED). Budget at least
  15 GB (images 5 to 8 GB, models 3 GB, Postgres, audio).
- Kernel 7.0 and k3s: no incident was found searching on 2026-11-07, but
  I could not confirm a k3s statement of support for Ubuntu 26.04 either.
  The test is the verification block in 3.3; treat the first start as the
  compatibility test.
- iptables on this host is `1.8.11 (nf_tables)`. The k3s known-issues page
  warns only about 1.8.0 to 1.8.4 and 1.6.1-and-older in nftables mode, so
  1.8.11 is outside the warned range. K3s ships its own iptables 1.8.8
  (`--prefer-bundled-bin` uses it); not needed here unless pod networking
  fails.
- Do not use the Docker snap with k3s (known-issues page). Docker here is the
  apt `dockerd`, which is fine.

### 2.2 Laptop-specific hazards to decide on

- Suspend and lid close stop the cluster and every backup timer. Production
  needs `systemctl mask sleep.target suspend.target hibernate.target
hybrid-sleep.target` **(sudo)** and `HandleLidSwitch=ignore` in
  `/etc/systemd/logind.conf.d/` **(sudo)**. This is a general fact about
  systemd, not from the k3s docs; test it.
- Wi-Fi: the node IP is `192.168.9.x` from DHCP. k3s records the node IP at
  start, and a change breaks the kubeconfig's server address only if you used
  the LAN IP. Use `https://127.0.0.1:6443` everywhere (3.4). Prefer a wired
  link or a DHCP reservation.
- Battery: a failing battery on an always-on 2014 laptop is a risk of its own.

### 2.3 Decisions that shape the commands

| Decision           | Recommendation here                                                                                                                                                                                                                                                                                                                                                              |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| k3s version        | Pin. On 2026-11-07 the `stable` and `latest` channels were both `v1.37.2+k3s1` (`https://update.k3s.io/v1-release/channels`). The installed `kubectl` snap is `v1.35.10`, which is outside kubectl's one-minor skew against 1.37; use `k3s kubectl` or upgrade the snap. Pin `v1.37.2+k3s1`, or `v1.35.10+k3s1` to match the existing client if the owner prefers the older line |
| Datastore          | Default embedded SQLite, single server. No etcd, no HA.                                                                                                                                                                                                                                                                                                                          |
| Ingress controller | Disable Traefik: Cloudflare Tunnel talks to the `web` Service directly, nothing listens on 80/443                                                                                                                                                                                                                                                                                |
| ServiceLB          | Disable: no `LoadBalancer` Services are used. It would otherwise bind host ports for any LoadBalancer Service                                                                                                                                                                                                                                                                    |
| Container runtime  | Default k3s containerd; images imported (5.2). Alternative `--docker` in 5.4, not the default                                                                                                                                                                                                                                                                                    |
| Storage            | Built-in `local-path` StorageClass (RWO, node-local)                                                                                                                                                                                                                                                                                                                             |
| Manifests          | Plain YAML + Kustomize in a new `deploy/k3s/` directory, applied by hand with `kubectl apply -k`                                                                                                                                                                                                                                                                                 |
| Secrets            | Created from local files with `kubectl create secret generic ... --from-file`, never committed                                                                                                                                                                                                                                                                                   |

### 2.4 WSL2 verdict (applies only if production turns out to be a WSL2 distro)

Everything in this subsection is reasoning from Microsoft's WSL configuration
page (learn.microsoft.com, page dated 2026-10-16, fetched 2026-11-07) and the
k3s docs, not a tested result. The k3s requirements page does not mention WSL
at all.

| Question                   | Finding                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| systemd                    | Supported: `[boot] systemd=true` in `/etc/wsl.conf`, then `wsl.exe --shutdown`. k3s installs as a systemd unit, so this is a prerequisite.                                                                                                                                                                                                                                                                             |
| cgroup v2                  | Default in WSL2 (`[automount] cgroups` default `v2`). Good for k3s.                                                                                                                                                                                                                                                                                                                                                    |
| Kernel modules             | Default modules loaded are `tun`, `ip_tables`, `br_netfilter` (`loadDefaultKernelModules`). k3s flannel VXLAN also needs `vxlan` and netfilter pieces, which the Microsoft kernel may or may not have built in; UNVERIFIED. A custom kernel is possible (`kernel=` in `.wslconfig`) but is an upkeep burden.                                                                                                           |
| Networking                 | Default NAT. The WSL VM's IP changes at each start; mirrored mode exists on Windows 11 22H2 and later. Ports reach Windows through `localhostForwarding`. Inbound LAN access needs Windows firewall and port proxy work. A Cloudflare Tunnel is outbound-only, so tunnel traffic is not affected.                                                                                                                      |
| Idle shutdown              | `vmIdleTimeout` default 60 s (Windows 11) and `instanceIdleTimeout` default 15 s: the VM stops when nothing keeps it busy. A running systemd service by itself may or may not count; to be safe a Windows scheduled task must hold a `wsl` session open. UNVERIFIED which process types count as activity.                                                                                                             |
| Windows sleep or reboot    | The VM stops. WSL does not start at Windows boot, only when something starts it. A Windows Task Scheduler entry at boot running `wsl.exe -d <distro>` is the usual workaround; cluster and backups are then down for the whole Windows reboot (Windows Update reboots are not under your control on Home editions).                                                                                                    |
| Memory                     | `.wslconfig` `memory` defaults to 50% of Windows RAM; Windows keeps the rest.                                                                                                                                                                                                                                                                                                                                          |
| Docker Desktop coexistence | Docker Desktop's daemon and k3s' containerd are separate runtimes, so images built with `docker` are not visible to k3s: import or use a registry (section 5). Docker Desktop has its own optional Kubernetes; do not enable it beside k3s. Do not install `dockerd` in the distro (AGENTS.md). Ollama on the distro would need to bind an address pods can reach; the WSL host's address for pods differs from Linux. |
| Disk                       | The distro lives in a VHDX, which grows and is not shrunk automatically (`sparseVhd` experimental).                                                                                                                                                                                                                                                                                                                    |

Verdict: **WSL2 can probably run k3s, but it cannot run it as an unattended
production service.** The VM lifecycle is tied to the Windows session, there is
no boot-time start without a Windows-side task, Windows reboots take
everything down, and several kernel-module facts are unverified. If the
production host really is WSL2, the answer is: move to a real Linux host first
(section 0.2, option C), and keep Compose in WSL meanwhile. Do not follow
section 3 inside WSL2 without a throwaway rehearsal.

---

## 3. Install k3s on Ubuntu 26.04

Run as the deploy account. Phases are cumulative; every step has a check and
the last has a rollback.

### 3.1 Preflight (read-only)

```bash
. /etc/os-release && echo "$PRETTY_NAME"        # Ubuntu 26.04.x LTS
uname -r                                         # record it
stat -fc %T /sys/fs/cgroup                       # must print cgroup2fs
free -h; swapon --show                           # record available memory
df -h /var/lib                                   # >= 15 GB free
ss -ltnp | grep -E ':(6443|10250|80|443)\b' || echo "ports free"
systemctl is-active ufw                          # active/inactive, no sudo needed
docker info --format '{{.OperatingSystem}} {{.CgroupVersion}}'
ops/host/preflight.sh                            # existing read-only preflight
```

Expected: ports 6443 and 10250 free. If memory available is under about
4 GiB with the Compose stack running, stop `ytdigest-dev` and the unrelated
stacks first (`docker compose -p <name> stop`), or do not continue (section
0.2).

### 3.2 Open the firewall for pods, not the world (ufw is active)

The k3s requirements page recommends turning ufw off, and says that if you
keep it on you must allow 6443/tcp and allow traffic from the pod CIDR
`10.42.0.0/16` and the service CIDR `10.43.0.0/16`. A single node needs 6443
only from the pods themselves. Recommended: keep ufw on and add rules for the
two CIDRs only. **(sudo)**

```bash
sudo ufw allow from 10.42.0.0/16 to any comment 'k3s pods'
sudo ufw allow from 10.43.0.0/16 to any comment 'k3s services'
sudo ufw status numbered
```

These two rules also let pods reach host services, including Ollama (4.7).
Do not open 6443, 10250 or UDP 8472 to the LAN; nothing outside needs them.
Docker's own published ports bypass ufw; that is existing behaviour and not
changed by k3s.

### 3.3 Install, pinned, with Traefik and ServiceLB off

Write the config file first so the flags are not lost on upgrade:

```bash
sudo install -d -m 755 /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<'EOF'
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
EOF
```

`/etc/rancher/k3s/config.yaml` is the documented default config path, and CLI
flags map to YAML keys with repeatable flags as lists (docs.k3s.io
configuration). `--disable=traefik` and `--disable=servicelb` are documented
flags (docs.k3s.io networking-services; packaged-components says `--disable`
can be repeated or comma-separated). The `disable:` list form is derived from
the documented mapping rule and is UNVERIFIED by example: after install, check
`kubectl get pods -A` shows no `traefik` or `svclb-*` pods, and if it does,
reinstall with `INSTALL_K3S_EXEC="--disable=traefik --disable=servicelb"`.

Download the installer, read it, then run it pinned (the quick-start command
is `curl -sfL https://get.k3s.io | sh -`; splitting it is the safer form of
the same thing):

```bash
curl -sfL https://get.k3s.io -o /tmp/k3s-install.sh
less /tmp/k3s-install.sh                          # read before running
sudo INSTALL_K3S_VERSION='v1.37.2+k3s1' sh /tmp/k3s-install.sh
```

`INSTALL_K3S_VERSION` with the `vX.Y.Z+k3s1` form is documented on the manual
upgrade page. Keep the install env for upgrades (9.2). Check:

```bash
systemctl is-active k3s                           # active
sudo k3s kubectl get nodes -o wide                # Ready (can take about 30-60 s)
sudo k3s kubectl get pods -A                      # coredns, metrics-server,
                                                  # local-path-provisioner Running;
                                                  # no traefik, no svclb-*
sudo k3s kubectl get storageclass                 # local-path (default)
ss -ltn | grep -E ':(6443|10250)\b'               # note: listen on all interfaces
```

The `systemctl` unit name `k3s` is the installer's default for a server; the
quick-start page does not state it (UNVERIFIED by page; the check above proves
it).

### 3.4 Kubeconfig handling

The installer writes `/etc/rancher/k3s/k3s.yaml` (documented). Do not make it
world-readable. Give the deploy user a private copy:

```bash
mkdir -p ~/.kube && chmod 700 ~/.kube
sudo install -m 600 -o "$USER" -g "$USER" /etc/rancher/k3s/k3s.yaml ~/.kube/config
export KUBECONFIG=~/.kube/config
```

The file's `server:` is `https://127.0.0.1:6443`; leave it. The `kubectl`
found on this host is the snap at `/snap/bin/kubectl`, version 1.35.10, so
the plain `kubectl` commands in this plan assume the skew is acceptable; if in
doubt run the same commands as `sudo k3s kubectl ...` (k3s bundles a matching
kubectl). Never copy this kubeconfig to another machine without changing the
server address, and never commit it.

### 3.5 Prove it works before touching the app

```bash
kubectl create namespace smoke
kubectl -n smoke run t --image=busybox:1.37 --restart=Never --command -- sleep 600   # image tag: pick a current one; UNVERIFIED tag
kubectl -n smoke wait --for=condition=Ready pod/t --timeout=120s
kubectl -n smoke exec t -- nslookup kubernetes.default.svc.cluster.local
kubectl -n smoke exec t -- wget -qO- -T 5 https://registry-1.docker.io/v2/ | head -c 80   # egress works
kubectl -n smoke exec t -- wget -qO- -T 5 http://172.17.0.1:11434/api/tags | head -c 80   # Ollama reachable (4.7)
kubectl delete namespace smoke
docker ps --format '{{.Names}}' | head -3        # Docker still works
curl -fsS http://127.0.0.1:8080/api/healthz      # the Compose prod stack still answers
```

If pod egress or DNS fails, the usual suspect is a firewall or iptables
conflict between Docker's rules (Docker sets a default-drop FORWARD policy)
and flannel's. Docker and k3s coexisting is not covered in the k3s docs I
fetched (the known-issues page says nothing on FORWARD). Check `sudo iptables
-S FORWARD` and `sudo ufw status`, and see section 10 risk R3.

### 3.6 Rollback of the install

The k3s installer creates `k3s-uninstall.sh` and `k3s-killall.sh` (the
quick-start page says the former exists; its path is the installer's default
`/usr/local/bin`, UNVERIFIED).

```bash
sudo /usr/local/bin/k3s-uninstall.sh      # removes k3s, containerd state, and /var/lib/rancher/k3s
sudo ufw delete allow from 10.42.0.0/16   # use `ufw status numbered` and delete by number
sudo ufw delete allow from 10.43.0.0/16
rm -f ~/.kube/config
ip -br link | grep -E 'cni0|flannel'      # both gone
```

Uninstalling deletes the local-path volumes, so only do it before data is
loaded, or after you have restored or confirmed Compose still holds the data
(section 6.12). The Compose stack, Docker and the `ytdigest_*` volumes are
never touched by the install or uninstall.

---

## 4. Cluster layout

Everything in one namespace, `ytdigest`. Snippets below are reference shapes,
trimmed for reading. They are not committed. The real files would live in
`deploy/k3s/` (follow-up F2) with `kustomization.yaml`.

### 4.1 Files

```
deploy/k3s/
  namespace.yaml
  configmap.yaml          # non-secret env (the compose x-backend-env and worker vars)
  postgres.yaml           # StatefulSet, Service "db", PVC template
  migrate-job.yaml        # NOT in kustomization.yaml; applied by hand (4.4)
  api.yaml  web.yaml  planner.yaml  analyzer.yaml  transcriber.yaml
  cloudflared.yaml        # section 7
  kustomization.yaml      # lists all but migrate-job.yaml
```

Secrets are not in the directory. They are created once, from files kept
outside the checkout (4.3).

### 4.2 Why Kustomize, applied by hand

The Compose file is about 240 lines; a chart adds templating nobody needs for
one environment. `kubectl apply -k deploy/k3s` is built into `kubectl`. ArgoCD
later consumes the same directory unchanged. This is a recommendation, not a
verified requirement.

### 4.3 Config and secrets

ConfigMap `ytdigest-config` holds the same values as `.env` for the non-secret
keys (`SUMMARIZER`, `OLLAMA_MODEL`, `CHUNK_SEC`, `LOG_FORMAT`, ...). Do not
put `DATABASE_URL` there.

Secrets, created from files on the host (use `umask 077`; the files stay
outside the repo, for example in `~/ytdigest-k3s-secrets/`):

```bash
umask 077
install -d ~/ytdigest-k3s-secrets
# 1. database: both values come from the existing production .env password
printf '%s' "$PW" > ~/ytdigest-k3s-secrets/POSTGRES_PASSWORD
printf 'postgresql://ytdigest:%s@db:5432/ytdigest' "$PW" > ~/ytdigest-k3s-secrets/DATABASE_URL
kubectl -n ytdigest create secret generic ytdigest-db \
  --from-file=POSTGRES_PASSWORD=$HOME/ytdigest-k3s-secrets/POSTGRES_PASSWORD \
  --from-file=DATABASE_URL=$HOME/ytdigest-k3s-secrets/DATABASE_URL
# 2. LLM keys (analyzer only), only the ones in use. Same --from-file pattern:
kubectl -n ytdigest create secret generic ytdigest-llm \
  --from-file=ANTHROPIC_API_KEY=$HOME/ytdigest-k3s-secrets/ANTHROPIC_API_KEY
# 3. optional yt-dlp cookies (transcriber only), mounted read-only as a file
kubectl -n ytdigest create secret generic ytdigest-ytdlp-cookies \
  --from-file=cookies.txt=$HOME/ytdigest-k3s-secrets/cookies.txt
```

`kubectl create secret generic ... --from-literal` is shown on the Kubernetes
"distribute credentials" page; `--from-file=key=path` is the standard form but
that page's excerpt did not show it (UNVERIFIED by page). Never use
`--from-literal` with a real value: it lands in shell history and in `ps`.
`DATABASE_URL` needs the password URL-encoded if it has special characters,
exactly as with Compose. `.env.example` also lists `ANTHROPIC_API_KEY_FILE`:
if the services honour it, prefer mounting the secret as a file and setting
that variable over an environment variable (Kubernetes docs: environment
variables leak more easily through logs and `describe`). Check the setting in
`common/config.py` before choosing.

Secrets are base64, not encrypted. Anyone who can run `kubectl` on the node
can read them. That is the same exposure as `.env` today. Encryption at rest
(`secrets-encryption` in k3s) is an optional hardening, not verified here.

### 4.4 Migrate is a Job, applied by hand

Never an `initContainer`: those run on every pod start, and migrations must
never run automatically (AGENTS.md). Keep `migrate-job.yaml` out of
`kustomization.yaml`, so `apply -k` cannot run it by accident. A Job's pod
template is immutable, so each run is a delete-then-apply.

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: migrate, namespace: ytdigest }
spec:
  backoffLimit: 0 # fail loudly, do not retry a half-migration
  ttlSecondsAfterFinished: 86400 # auto-clean after a day (see note)
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: ytdigest-backend:v0.0.0 # immutable tag, never :latest (#380)
          imagePullPolicy: Never
          command: ["alembic", "upgrade", "head"]
          envFrom: [{ configMapRef: { name: ytdigest-config } }]
          env:
            - name: DATABASE_URL
              valueFrom:
                { secretKeyRef: { name: ytdigest-db, key: DATABASE_URL } }
```

Run it:

```bash
kubectl -n ytdigest delete job migrate --ignore-not-found
kubectl apply -f deploy/k3s/migrate-job.yaml
kubectl -n ytdigest wait --for=condition=complete job/migrate --timeout=300s   # UNVERIFIED by page
kubectl -n ytdigest logs job/migrate
```

`backoffLimit` and the Job shape are from the Kubernetes Job page. The Job page
excerpt I could fetch did not include `ttlSecondsAfterFinished` or
`kubectl wait`; both are standard but UNVERIFIED by page.

The workers do not wait for `migrate` as Compose does (`depends_on:
service_completed_successfully`). There is no such Kubernetes primitive. The
runbook order (6.9) guarantees it: migrate first, workers after.

### 4.5 Workloads

Common rules:

- `strategy: {type: Recreate}` on every Deployment. The default
  `RollingUpdate` starts the new pod before stopping the old, which briefly
  runs two planners (violates "exactly one planner") and, for the transcriber,
  two 6 GiB pods on a 14 GB node.
- `replicas: 1` on planner, analyzer and transcriber. `api` and `web` may be 1
  (no HA on one node anyway).
- `terminationGracePeriodSeconds: 30` on the three workers (Compose
  `stop_grace_period: 30s`; must stay above `WORKER_SHUTDOWN_GRACE_SEC`,
  default 20). Kubernetes' default is also 30 (pod-lifecycle page).
- Image names are immutable tags such as `ytdigest-backend:v0.0.0`. Compose
  uses `:latest` today; #380 (IMAGE_TAG) is the intended remedy and does not
  exist yet. Until it does, tag by hand at build time (5.1).
- `securityContext.fsGroup: 10001` on pods that write the shared `audio` and
  `whisper-models` volumes (image user `app` is uid 10001). Whether
  local-path honours `fsGroup` is UNVERIFIED; the check is in 6.8.

Postgres (`StatefulSet`, 1 replica):

- Image `postgres:18-alpine@sha256:10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5`
  (the same digest as `compose.yml`).
- `volumeClaimTemplates` with `storageClassName: local-path`, `10Gi`, mounted
  at **`/var/lib/postgresql`** (not `/data`, not `.../data`: the compose.yml
  comment explains the postgres:18 layout change).
- `env`: `POSTGRES_USER=ytdigest`, `POSTGRES_DB=ytdigest`,
  `POSTGRES_PASSWORD` from the secret.
- `readinessProbe` and `livenessProbe`:
  `exec: [pg_isready, -U, ytdigest, -d, ytdigest]`, `periodSeconds: 10`,
  `timeoutSeconds: 5`, `failureThreshold: 5`; a `startupProbe` with the same
  command and `failureThreshold: 30` replaces compose's `start_period: 30s`.
- Headless-or-ClusterIP `Service` named `db` on 5432, so
  `postgresql://...@db:5432/...` works unchanged.
- Do NOT expose it outside the cluster. The dev override's host port 5432 is
  dev-only and has no equivalent here; use `kubectl port-forward` for admin.
- A single StatefulSet replica gets `Recreate` semantics already (it stops the
  old pod before the new). Never scale it above 1.

Other workloads:

| Workload      | Image                    | Command                                                    | Probe                                                                                       | Requests / limits (estimate, measure first)                                             |
| ------------- | ------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `api`         | `ytdigest-backend:<tag>` | `uvicorn services.api.main:app --host 0.0.0.0 --port 8000` | `httpGet /healthz :8000` readiness and liveness (replaces `curl -fsS`); startup allows 20 s | 100m / 256Mi req; 1 CPU / 768Mi limit                                                   |
| `web`         | `ytdigest-web:<tag>`     | image default (nginx on 8080)                              | `httpGet / :8080`                                                                           | 25m / 32Mi req; 250m / 128Mi limit                                                      |
| `planner`     | `ytdigest-backend:<tag>` | `python -m services.planner.main`                          | exec heartbeat test below                                                                   | 100m / 192Mi req; 500m / 512Mi limit; PVC `audio`                                       |
| `analyzer`    | `ytdigest-backend:<tag>` | `python -m services.analyzer.main`                         | exec heartbeat test                                                                         | 100m / 256Mi req; 1 CPU / 768Mi limit; `ytdigest-llm`                                   |
| `transcriber` | `ytdigest-whisper:<tag>` | `python -m services.transcriber.main`                      | exec heartbeat test                                                                         | 1 CPU / 3Gi req; **3 CPU / 6Gi limit** (compose values); PVCs `audio`, `whisper-models` |

The Compose worker health command, with Compose's `$$` doubling removed:

```yaml
livenessProbe:
  exec:
    command:
      [
        "sh",
        "-c",
        "test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 180",
      ]
  initialDelaySeconds: 60
  periodSeconds: 60
  failureThreshold: 5
```

Important difference: Compose only marks the container unhealthy and does
nothing. A Kubernetes `livenessProbe` **kills and restarts** the container
(pod-lifecycle page), which would abort a long transcription (a `transcribe`
starts over). So use a generous `failureThreshold` (5 here, 5 minutes) and do
not add a `readinessProbe` to the workers (nothing consumes their readiness).
The heartbeat is touched by the worker loop, per `compose.yml`.

Memory limits and the 6 GiB cap: with limits, the kernel OOM-kills the
container at its limit, the same as Compose's `memory: 6G`. Scheduling uses
requests, so requests below limits let the node overcommit. On a 14 GB node
with k3s (about 1 GiB), Postgres, api, web, planner, analyzer
(about 2.5 GiB in total with their limits) and a 6 GiB Whisper, the sum of
limits is about 9.5 to 10 GiB before Ollama (which runs on the host and its
model has its own 4 to 7 GiB, hence #393). That is the same pressure as today
and no better. Mitigation is #393 (already in code, via the advisory lock) and
not running the dev stack.

`web` and the nginx resolver: `ops/nginx.conf` uses `resolver 127.0.0.11`
(Docker's embedded DNS) and `set $api_upstream api:8000`. In Kubernetes that
resolver does not exist; the pod's DNS is CoreDNS at `10.43.0.10` (cluster
default, UNVERIFIED), so **nginx will fail to resolve `api` at request time**
(lazy DNS via a variable needs a working `resolver`). This is a real blocker
and needs a code change: take the resolver from the container's
`/etc/resolv.conf` at start (a one-line template in the image entrypoint) or
set a Kubernetes-specific nginx config. Tracked as follow-up F3. Do not
proceed past phase 2 without it. Dry-run: `kubectl exec` into `web` and
`wget -qO- http://127.0.0.1:8080/api/healthz`.

### 4.6 Analyzer versus transcriber exclusivity (#393)

The lock is a Postgres session advisory lock in the same database
(`LOCK_NAMESPACE_LLM`, key 1): the analyzer takes it exclusive around a local
Ollama call, the transcriber shared around Whisper. Nothing changes in
Kubernetes as long as **both workloads point at the same database**. What
Kubernetes adds:

- Never run a second copy of the stack (for example a rehearsal namespace)
  against the same Ollama and CPU at the same time, since the locks are per
  database (#410).
- `Recreate` also covers the rollout case: a rolling transcriber would
  hold a shared lock on the old pod while the new one starts.
- Do not add a `ResourceQuota` that makes pods unschedulable together; the
  jobs are deferred by the lock, not by the scheduler.

### 4.7 Ollama on the host, from pods

Compose does `extra_hosts: host.docker.internal:host-gateway` and
`OLLAMA_HOST=http://host.docker.internal:11434`. Ollama is bound to
`172.17.0.1:11434` (docker0) and `127.0.0.1:11434`.

Options for pods:

1. **Recommended while Docker stays installed:** keep Ollama's bind. In the
   analyzer pod, add a hostAliases entry (documented on the Kubernetes
   "customize hosts file" page) so the existing URL keeps working:

   ```yaml
   spec:
     hostAliases:
       - ip: "172.17.0.1"
         hostnames: ["host.docker.internal"]
   ```

   Whether a pod on the flannel network can reach `172.17.0.1:11434` (an
   address of the node's `docker0` bridge) is the thing to test, in 3.5 and
   6.7: the packet goes from the pod to the node's own address. It needs
   the ufw rule from 3.2. UNVERIFIED until tested. If it fails, the fix is
   to bind Ollama to an address k3s owns.

2. **Fallback:** a second Ollama bind. Ollama binds one address, so choose
   the `cni0` bridge address (flannel's per-node gateway, normally
   `10.42.0.1`, UNVERIFIED) by changing `OLLAMA_HOST` in the drop-in, with
   `After=k3s.service`. That breaks Compose's access to Ollama, so it is a
   cut-over step, not a parallel-run step.
3. **Not recommended:** `OLLAMA_HOST=0.0.0.0` (exposes Ollama on Wi-Fi) or
   `hostNetwork: true` on the analyzer (loses pod isolation for one hostname).

Coexistence is permanent here: the `ollama.service` drop-in has
`After=docker.service`, and other projects need Docker. So option 1 stays
valid.

### 4.8 Network policy (optional, later)

Compose's `internal` network keeps `db` off the internet. K3s includes a
network policy controller (docs.k3s.io: embedded kube-router netpol; I did
not fetch the page, UNVERIFIED). A default-deny plus allow-list policy is an
improvement but not a cut-over condition; add it after the first week.

---

## 5. Image build and distribution

### 5.1 Build with Docker, tag immutably

Use the existing Dockerfiles (`ops/Dockerfile.backend`, `.whisper`,
`.frontend`). Pick a release tag and build from a clean checkout of that tag
(#380 slice A is the intended way; until it lands, build by hand):

```bash
TAG=v0.0.0                      # whatever the release tag is; never :latest
git -C /opt/ytdigest checkout --detach "$TAG"       # a clean clone, not the dev checkout
docker build -f ops/Dockerfile.backend --target deps --provenance=false -t ytdigest-deps:"$TAG" .
docker build -f ops/Dockerfile.backend -t ytdigest-backend:"$TAG" .
docker build -f ops/Dockerfile.whisper --build-context ytdigest-deps=docker-image://ytdigest-deps:"$TAG" \
  -t ytdigest-whisper:"$TAG" .                      # UNVERIFIED: mirrors compose additional_contexts
docker build -f ops/Dockerfile.frontend -t ytdigest-web:"$TAG" .
```

The `--build-context` form is shown in AGENTS.md for the deps image and in the
`compose.yml` comment; the whisper command above is adapted from them and must
be dry-run (the compose comment warns the key is the image name without the
tag, and the Dockerfile's `FROM ytdigest-deps` has no tag). Simplest and
verified: `docker compose -f compose.yml build` with `IMAGE_TAG` support once
F1 lands. Until then retag after a compose build: `docker tag
ytdigest-backend:latest ytdigest-backend:$TAG`, with the risk that the
checkout was not clean.

### 5.2 Import into k3s containerd (recommended: no registry to run)

K3s has its own containerd, separate from Docker's. The documented method is a
tarball in `/var/lib/rancher/k3s/agent/images/`, which k3s loads (the page
says it can be added before or after k3s starts; imports while running exist
in releases since January 2025):

```bash
docker save -o /tmp/ytdigest-images-$TAG.tar \
  ytdigest-backend:$TAG ytdigest-whisper:$TAG ytdigest-web:$TAG
sudo cp /tmp/ytdigest-images-$TAG.tar /var/lib/rancher/k3s/agent/images/
sudo k3s ctr images list | grep ytdigest        # documented verify command
rm /tmp/ytdigest-images-$TAG.tar
```

Direct import with `sudo k3s ctr images import /tmp/...tar` is standard
containerd usage; the docs do not show it (UNVERIFIED by page; a community
write-up uses `docker save ... | sudo k3s ctr images import -`).

Rules that follow from the docs:

- Pods must use `imagePullPolicy: Never` (or `IfNotPresent` with a non-
  `latest` tag). "Pre-importing images onto the node is essential if you
  configure `imagePullPolicy` as `Never`" (docs.k3s.io add-ons/import-images).
  A `latest` tag defaults to `Always` and would try a registry.
- The base images `postgres:18-alpine@sha256:...` and `cloudflared` can be
  pulled by k3s normally (default `IfNotPresent`), since the node has egress.
- Gotcha to watch: Docker 29 here uses the containerd image store
  (`Storage Driver: overlayfs`). `docker save` output from it can carry
  attestation or multi-platform manifests that containerd refuses to import
  ("content digest not found" is the typical symptom, UNVERIFIED). The
  compose file builds `deps` with `provenance: false` for this reason. If the
  import fails, build with `--provenance=false --sbom=false` and
  `--platform linux/amd64`, or use 5.3.
- An import is local to the node. Adding the laptop as a worker later needs a
  registry (5.3) or an import on that node too.

### 5.3 Alternative: a local registry

Run `registry:2` in the cluster or as a host container and configure
`/etc/rancher/k3s/registries.yaml` (docs.k3s.io private-registry page, not
fetched, UNVERIFIED). Worth it only when the second node or ArgoCD arrives.
Not needed now.

### 5.4 Alternative: `k3s --docker`

K3s 1.24+ includes `cri-dockerd`, and `curl -sfL https://get.k3s.io | sh -s -
--docker` makes k3s use the Docker daemon as its runtime (docs.k3s.io
advanced). Pros: images built with `docker build` are visible to pods with no
import. Cons: it ties the cluster to the same daemon the other projects use
(a `docker system prune` or `down -v` on the shared daemon can hit k3s
containers and images), CRI plumbing is less common (more unknowns on
kernel 7.0), and moving to the dedicated node later changes the runtime. Not
recommended as the default. Choose it only if import proves painful.

---

## 6. Migration runbook: Compose to k3s on the same host

Window: plan 60 to 90 minutes of work, with the real downtime (api and web
down, workers frozen) about 10 to 15 minutes. The database is under 100 MB and
the dump is under 1 MB, so the data steps take seconds; the time is in image
import, pod start, verification and DNS/tunnel checks. Estimates, not
measured.

Variant for a new dedicated node: replace 6.3 to 6.5 with a restic restore of
the fresh snapshot onto the new node (`ops/host/pg-restore-drill.sh
--fetch-only`, `_docs/backup-restore.md` section 6a), run 6.7 onwards there,
and treat the old host as the rollback target.

Throughout, `PROD=ytdigest` is the Compose project, and the volume names
(`ytdigest_pgdata`, `ytdigest_audio`, `ytdigest_whisper-models`) are the ones
seen on this host.

### 6.0 Gate

Do not start unless all hold:

- Phases 0 to 3 of section 11 are done (k3s installed and proven with the
  smoke pod, images imported, secrets created, manifests rehearsed on a
  scratch namespace with dummy data).
- A backup path that works for k3s exists and has produced one verified
  snapshot (section 8). Until F4 is done there is no such path, which blocks
  cut-over.
- Owner decisions 0 to 5 in section 10 are made.

### 6.1 Preflight (read-only)

```bash
cd /opt/ytdigest                              # the prod checkout (#380), or your repo path
export COMPOSE_FILE=compose.yml
docker compose -p ytdigest ps                 # all healthy
curl -fsS http://127.0.0.1:8080/api/healthz
curl -fsS http://127.0.0.1:8080/api/ops/jobs | head -c 400     # note pending/running counts
ops/host/pg-backup-status.sh                  # last good snapshot age
systemctl list-timers 'ytdigest-pg-backup*'
free -h                                       # available memory
git -C /opt/ytdigest describe --tags          # the release being migrated
```

### 6.2 Fresh verified backup and restore-drill proof

```bash
sudo systemctl start ytdigest-pg-backup.service       # (sudo) or run ops/host/pg-backup.sh
journalctl -u ytdigest-pg-backup.service -n 20 --no-pager | grep -E 'finish|snapshot'
ops/host/pg-backup-status.sh --latest-id               # record the snapshot id
ops/host/pg-restore-drill.sh                           # restores to a disposable stack and verifies
                                                       # against the manifest; must end PASS
```

Record the snapshot id, the alembic revision and the table row counts the
drill prints. These are the numbers 6.6 must match.

### 6.3 Freeze

Stop every writer so no job is in flight. Waiting for the running job to end
is cleaner than interrupting it:

```bash
curl -fsS http://127.0.0.1:8080/api/ops/jobs | head -c 600       # running > 0 ?
docker compose -p ytdigest stop planner                  # no new work gets created
# let running jobs finish: poll until none running (bounded, up to 30 min)
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8080/api/ops/jobs | grep -q '"running": *0' && break
  sleep 30
done
docker compose -p ytdigest stop transcriber analyzer api web
docker compose -p ytdigest ps                            # only db is up
```

The `"running": 0` match depends on the real shape of `/api/ops/jobs`;
adapt after looking at one response (UNVERIFIED). A SIGTERM on a worker is
allowed: each has a 30 s drain, and a `transcribe` job interrupted restarts
from scratch on the new stack (the job stays in the DB).

**Disable the backup timers now** (finding: they back up the Compose `db`
only; if `db` stays up for rollback they would keep reporting fresh,
successful backups of a frozen database while production writes elsewhere):

```bash
sudo systemctl stop ytdigest-pg-backup.timer ytdigest-pg-backup-fresh.timer    # (sudo)
sudo systemctl disable ytdigest-pg-backup.timer ytdigest-pg-backup-fresh.timer # (sudo)
```

### 6.4 Final dump from the Compose database

```bash
mkdir -m 700 -p ~/migrate-$(date +%F) && cd ~/migrate-$(date +%F)
docker compose -p ytdigest -f /opt/ytdigest/compose.yml exec -T db \
  pg_dump -U ytdigest -d ytdigest -Fc > ytdigest.dump
docker compose -p ytdigest -f /opt/ytdigest/compose.yml exec -T db \
  pg_restore --list < ytdigest.dump | head -5          # sanity: a readable archive
ls -l ytdigest.dump ; sha256sum ytdigest.dump > ytdigest.dump.sha256
```

`pg_dump -Fc` and the `pg_restore --list` readback are the same method
`pg-backup.sh` uses. The dump contains all data in clear: it lives in a 700
directory, and is deleted in 6.12.

Capture the manifest from the live (frozen) database. Use the query set from
`manifest_sql` in `ops/host/pg-backup-lib.sh` as `psql -At`:

```bash
source /opt/ytdigest/ops/host/pg-backup-lib.sh 2>/dev/null   # UNVERIFIED: the lib may need env set
manifest_sql | docker compose -p ytdigest -f /opt/ytdigest/compose.yml exec -T db \
  psql -U ytdigest -d ytdigest -At > manifest.before.txt
```

If sourcing the library is awkward, record by hand the alembic revision
(`SELECT version_num FROM alembic_version;`) and the exact row count of every
table (`SELECT relname, n_live_tup` is an estimate; use `count(*)` per table).

### 6.5 Bring up k3s Postgres and restore

```bash
kubectl apply -f deploy/k3s/namespace.yaml
# the ytdigest-db secret from 4.3 must already exist
kubectl apply -f deploy/k3s/postgres.yaml
kubectl -n ytdigest rollout status statefulset/db --timeout=180s
kubectl -n ytdigest exec db-0 -- pg_isready -U ytdigest -d ytdigest
```

Restore into the empty, freshly initialised database. `-j` is unnecessary at
this size; `--exit-on-error` makes a failure obvious; `--no-owner` is not
needed because the owner role is `ytdigest` in both.

```bash
kubectl -n ytdigest exec -i db-0 -- \
  pg_restore -U ytdigest -d ytdigest --exit-on-error --no-privileges < ~/migrate-*/ytdigest.dump
```

The k3s Postgres image is the same major version (18) as the dump's server.
That is required: restoring a newer dump into an older major fails.

### 6.6 Verify against the manifest

```bash
kubectl -n ytdigest exec db-0 -- psql -U ytdigest -d ytdigest -At \
  -c 'SELECT version_num FROM alembic_version;'                    # = recorded revision
# same manifest query set as 6.4:
manifest_sql | kubectl -n ytdigest exec -i db-0 -- psql -U ytdigest -d ytdigest -At > manifest.after.txt
diff <(LC_ALL=C sort manifest.before.txt) <(LC_ALL=C sort manifest.after.txt) && echo MANIFEST MATCH
```

Stop here if the diff is not empty. Repair or roll back (6.12); do not continue.

### 6.7 Copy the volumes that matter

`audio`: 32 MB of in-flight downloaded audio. Copy it; losing it makes a
transcribe job re-download. `whisper-models`: 3 GB, re-downloadable from
Hugging Face but slow; copy it (egress allow-list in `deploy-host.md` 7).

Use a throwaway loader pod that mounts the PVCs created by the manifests:

```bash
kubectl apply -f deploy/k3s/pvcs.yaml        # audio, whisper-models (RWO, local-path)
kubectl -n ytdigest run loader --image=busybox:1.37 --restart=Never --overrides='{...mount audio+models PVCs...}' -- sleep 3600   # UNVERIFIED shape; write a loader.yaml
kubectl -n ytdigest wait --for=condition=Ready pod/loader --timeout=60s
docker run --rm -v ytdigest_audio:/m:ro busybox:1.37 tar -C /m -cf - . \
  | kubectl -n ytdigest exec -i loader -- tar -C /audio -xf -
docker run --rm -v ytdigest_whisper-models:/m:ro busybox:1.37 tar -C /m -cf - . \
  | kubectl -n ytdigest exec -i loader -- tar -C /models -xf -
kubectl -n ytdigest exec loader -- sh -c 'chown -R 10001:10001 /audio /models; du -sh /audio /models'
kubectl -n ytdigest delete pod loader
```

`docker run ... -v <named volume>:/m:ro` only reads the Compose volume; it does
not modify it. Note that pod `loader` and later workloads share RWO local-path
volumes: that works on a single node (RWO is per node, not per pod), UNVERIFIED
for concurrent mounts by planner and transcriber; the first smoke run in 6.9
proves it.

### 6.8 Check pre-conditions for the workers

```bash
kubectl -n ytdigest exec loader -- ls -ln /audio    # run before deleting loader; expect uid 10001
sudo k3s ctr images list | grep ytdigest             # three images imported (5.2)
kubectl -n ytdigest get secret ytdigest-db ytdigest-llm
```

### 6.9 Apply the application, migrate only if needed

```bash
kubectl apply -k deploy/k3s       # api, web, workers; excludes migrate-job.yaml
```

Do this only after reading the alembic revision from 6.6. If the image's
`alembic heads` equals the restored revision, **skip migrate**; if the new
release carries a newer revision, run the Job (4.4) now, with workers scaled
to 0:

```bash
kubectl -n ytdigest scale deploy/planner deploy/analyzer deploy/transcriber deploy/api --replicas=0
kubectl -n ytdigest exec deploy/api -- alembic heads 2>/dev/null || true   # or run the image once
# migrate if (and only if) the head is ahead of the DB:
kubectl -n ytdigest delete job migrate --ignore-not-found
kubectl apply -f deploy/k3s/migrate-job.yaml
kubectl -n ytdigest wait --for=condition=complete job/migrate --timeout=300s
kubectl -n ytdigest scale deploy/api --replicas=1
kubectl -n ytdigest scale deploy/planner deploy/analyzer deploy/transcriber --replicas=1   # order: api, then planner, analyzer, transcriber
kubectl -n ytdigest get pods -w
```

Never move the database across a migration and the code across a release
together without a backup in between; the pre-migration backup is the one from
6.2 plus the dump in 6.4.

### 6.10 Smoke check (section 12 of `_docs/deploy-host.md`)

The runbook's smoke check uses `127.0.0.1:8080`. That port is held by Compose
`web`, which is now stopped, but to avoid any confusion forward `web` to a
different port:

```bash
kubectl -n ytdigest port-forward svc/web 18080:8080 &       # leave it running
curl -fsS http://127.0.0.1:18080/api/healthz
```

Then follow `deploy-host.md` section 12 ("Guard: no spend", submit
`iG9CE55wbtY`, wait for `done`, pass criteria, cleanup) with `127.0.0.1:18080`
substituted for `127.0.0.1:8080`. It proves the extractor, the analyzer
reaching host Ollama (4.7) and a valid analysis. If the analyzer cannot
reach Ollama, fix it here (4.7 option 2), before cut-over.

Check also: `kubectl -n ytdigest logs deploy/planner --tail=20`, `... exec
deploy/transcriber -- ls /models`, and that the planner is the only one
(`kubectl -n ytdigest get pods -l app=planner`).

### 6.11 Cut-over

Two kinds of traffic exist: local (`127.0.0.1:8080`, not used by anyone but
the operator) and public (Cloudflare Tunnel; today there is none, #408 is
open). So the cut-over is the tunnel (section 7) plus the backups:

1. Create the k3s tunnel and Access policy (section 7) and test it.
2. Install the k3s backup path (section 8) and take one snapshot from the
   k3s database; run the restore drill against it. Only then:
3. Leave the Compose `db` stopped or removed (so a stale Compose-targeted
   backup would fail loudly instead of passing, see 6.3):
   `docker compose -p ytdigest stop db`.
4. Re-enable the _new_ timer or CronJob, not the old units.

### 6.12 Rollback and parallel run

Parallel run in the strict sense, both stacks processing jobs, is **not
possible and not allowed**: the host cannot hold two sets of workers (the
Whisper limit alone is 6 GiB), and #393's lock is per database (#410), so two
stacks would run Ollama and Whisper at once. The substitute is "cold standby":
the Compose stack and its volumes stay exactly as they were, stopped.

Untouched assets: `ytdigest_pgdata`, `ytdigest_audio`,
`ytdigest_whisper-models`, the images `ytdigest-*:latest`, the checkout, and
`.env`. Never run `docker compose down -v` or `docker volume rm` on them until
the k3s stack has run cleanly for an agreed period (suggest 14 days, equal to
the restic `--keep-within 14d`).

Rollback to Compose, before any new data matters:

```bash
kubectl -n ytdigest scale deploy --all --replicas=0        # stop k3s writers
kubectl -n ytdigest scale statefulset/db --replicas=0
# the Compose volumes still hold the pre-migration data
docker compose -p ytdigest -f /opt/ytdigest/compose.yml up -d --wait
curl -fsS http://127.0.0.1:8080/api/healthz
sudo systemctl enable --now ytdigest-pg-backup.timer ytdigest-pg-backup-fresh.timer   # (sudo)
```

Rollback after the k3s database has accepted new rows: the Compose database
is stale. Either accept losing the k3s-period writes (a few jobs, they can be
re-enqueued), or dump the k3s database and restore it into Compose's `db` with
the same `pg_dump -Fc` / `pg_restore` commands in the other direction (drop
and recreate the `ytdigest` database first; keep the Compose volume copy:
`docker run --rm -v ytdigest_pgdata:/v -v $PWD:/b busybox tar -C /v -czf
/b/pgdata-before.tgz .` before overwriting). Also remove or stop the tunnel
connector first, so there is never a connector pointing at a stopped origin
(a connector with no healthy origin returns 502s publicly).

When the grace period is over: delete the migrate directory (`shred -u` the
dump and `rm -rf ~/migrate-*`), then decide separately whether to remove the
Compose volumes.

---

## 7. Cloudflare Tunnel into k3s

For the compose-era tunnel (opt-in `compose.tunnel.yml`), see #426 and `_docs/deploy-host.md` "Public access".

Sources: developers.cloudflare.com Kubernetes deployment guide, remote tunnel
creation guide, Access policies (fetched 2026-11-07).

### 7.1 Which kind of tunnel

- **Remotely-managed, token-based** (recommended): created in the Zero Trust
  dashboard, the connector gets one token, routes (public hostnames) live in
  Cloudflare, and the Kubernetes guide uses exactly this: the token in a
  Secret, `TUNNEL_TOKEN` in the pod. No credentials JSON and no `config.yml`
  in the cluster.
- Locally-managed (credentials JSON + `config.yml`, as `deploy-host.md` does
  for the host service): works, but needs a ConfigMap, a Secret for the JSON
  and `cloudflared tunnel route dns`. More files, no advantage here.

Create a **new** tunnel for k3s. Do not reuse the existing locally-managed
tunnel: two connectors on one tunnel load-balance across both origins (#408
requires exactly one connector). Retire the old tunnel (and its dev hostname)
when the owner decides in #408.

### 7.2 What the owner clicks in Cloudflare (once)

1. Zero Trust dashboard, **Networking > Tunnels > Create a tunnel**. Choose
   the cloudflared type. Name it `ytdigest-k3s`. Select **Create Tunnel**.
2. On the connector page choose **Docker** as the environment and copy **only
   the token** (the long string starting `eyJ`), not the whole command.
   Do not paste it into chat, an issue or a commit.
3. Do not click Continue yet; first do 7.3 and 7.4 so the connector is up.

### 7.3 Put the token in a Secret from a file

```bash
umask 077
read -rs -p 'paste tunnel token: ' T; echo
printf '%s' "$T" > ~/ytdigest-k3s-secrets/tunnel-token; unset T
kubectl -n ytdigest create secret generic tunnel-token \
  --from-file=token=$HOME/ytdigest-k3s-secrets/tunnel-token
kubectl -n ytdigest get secret tunnel-token        # exists, no value printed
shred -u ~/ytdigest-k3s-secrets/tunnel-token
```

`read -rs` keeps the token out of the shell history and the process list.
The Cloudflare guide creates a Secret named `tunnel-token` with key `token`;
the same names are used here.

### 7.4 The cloudflared Deployment

Fields from the Cloudflare Kubernetes guide: image `cloudflare/cloudflared`,
env `TUNNEL_TOKEN` from the Secret key `token`, command
`cloudflared tunnel --no-autoupdate --loglevel info --output json --metrics
0.0.0.0:2000 run`, liveness `httpGet /ready :2000` (initialDelay 10, period
10, failureThreshold 1; `/ready` returns 200 only with an active Cloudflare
connection), pod label `pod: cloudflared`, replicas 2. The manifest below is
rebuilt from those fields (the guide's YAML could not be quoted verbatim by the
fetch tool, so it is a reconstruction, UNVERIFIED as a whole file):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: cloudflared, namespace: ytdigest }
spec:
  replicas: 2
  selector: { matchLabels: { pod: cloudflared } }
  strategy:
    { type: RollingUpdate, rollingUpdate: { maxUnavailable: 1, maxSurge: 0 } }
  template:
    metadata: { labels: { pod: cloudflared } }
    spec:
      containers:
        - name: cloudflared
          image: cloudflare/cloudflared:2026.10.7@sha256:<resolve and fill in> # pin version and digest, never :latest
          command:
            [
              cloudflared,
              tunnel,
              --no-autoupdate,
              --loglevel,
              info,
              --output,
              json,
              --metrics,
              "0.0.0.0:2000",
              run,
            ]
          env:
            - name: TUNNEL_TOKEN
              valueFrom: { secretKeyRef: { name: tunnel-token, key: token } }
          livenessProbe:
            httpGet: { path: /ready, port: 2000 }
            initialDelaySeconds: 10
            periodSeconds: 10
            failureThreshold: 3 # guide uses 1; 3 avoids restarts on a blip
          resources:
            requests: { cpu: 25m, memory: 48Mi }
            limits: { cpu: 250m, memory: 128Mi }
```

Resolve the digest (the tag `2026.10.7` matches the version of the local
binary and is UNVERIFIED as an image tag): `docker buildx imagetools inspect
cloudflare/cloudflared:2026.10.7` (UNVERIFIED), copy the `Digest:` line. The
repo convention pins digests (`compose.yml`, Dockerfiles).

Replicas: 2 is the Cloudflare guide's value and protects against a pod
restart; on one node it gives no node-level HA. 1 is acceptable. Both are the
same tunnel, which is the supported multi-replica form, and not the
two-connectors problem from 7.1.

```bash
kubectl apply -f deploy/k3s/cloudflared.yaml
kubectl -n ytdigest rollout status deploy/cloudflared --timeout=120s
kubectl -n ytdigest logs deploy/cloudflared --tail=20          # "Registered tunnel connection"
```

The dashboard then shows the tunnel as Healthy; select Continue.

### 7.5 Route a hostname to the in-cluster `web` Service

In the tunnel, **Routes > Add route > Published application**: subdomain and
domain of the owner's choice (a new prod hostname, not the old
`dev-ytdigest...`, #380 decision 6), **Service URL**
`http://web.ytdigest.svc.cluster.local:8080` (or `http://web:8080`, same
namespace). The Cloudflare guide uses the Service name the same way
(`http://httpbin-service`). Do not point at `api:8000`: `web` (nginx) is the
public entrance and it already hides `/api/metrics` (#408).

The DNS CNAME to `<tunnel-id>.cfargotunnel.com` is created by the dashboard
route (the Cloudflare page I fetched did not state this; it is the
documented behaviour for published applications, UNVERIFIED by page). The zone
must be on Cloudflare.

### 7.6 Access application with an email allow-list (before sharing the URL)

The app has no login of its own, and `POST /api/videos` spends compute and
money. **Do this before step 7.5 is reachable by anyone**, or create the Access
app first and the route after:

1. Zero Trust > **Access > Applications > Add an application > Self-hosted**.
   Application domain: the exact hostname from 7.5.
2. Policy: action **Allow**; include **Emails**, listing exact addresses.
   Per the Access docs "Access is deny by default", so no deny rule is needed.
   No Everyone, no domain-wide, no Bypass policy (#408).
3. Login method: One-time PIN is fine. The docs warn that One-time PIN alone
   lets anyone with a valid email request a PIN; the Emails include rule is
   what limits it. Add a **Require > Login method = One-time PIN** rule only if
   other login methods are configured.

### 7.7 Verify

```bash
curl -sI https://<prod-host>/ | head -5            # 302 to *.cloudflareaccess.com, not app content
curl -sI https://<prod-host>/api/healthz | head -5 # also redirected / challenged
# in a browser: a listed email gets the SPA; an unlisted one is denied
kubectl -n ytdigest get pods -l pod=cloudflared    # Running, no restarts
ss -ltn | grep -vE '127.0.0.1|::1|tailscale|:22\b' # compare with the pre-install list: no new public listener
```

### 7.8 No public ports

cloudflared only dials out. No `Ingress`, no `LoadBalancer`, no `NodePort`,
no hostPort. Traefik and ServiceLB are off. The only listeners k3s adds are
6443, 10250 and 8472/udp, which ufw keeps closed to the LAN (3.2) and which
the tunnel does not use.

### 7.9 Revoke or rotate

Remove the route, then the Access app, then `kubectl -n ytdigest delete
deploy cloudflared`, then delete the tunnel in the dashboard. To rotate the
token, use the dashboard's token refresh (UNVERIFIED location), replace the
Secret with the new file, then `kubectl -n ytdigest rollout restart
deploy/cloudflared`.

---

## 8. Backups and restore on k3s

State of play: `pg-backup.sh` and the restore drill talk to `docker compose -p
ytdigest ... exec db`. They will not see a database in k3s. That means **a
successful cut-over without changing the backup path leaves production with no
fresh backup**, and (6.3) a stale one that still looks fresh if Compose `db`
stays up.

Options:

| Option                                                                                                        | Work                                                                                                                               | Verdict                   |
| ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| A. Keep host timers; switch `dc exec db` to `kubectl exec db-0` behind a flag (`YTDIGEST_BACKUP_RUNTIME=k8s`) | Small script change plus tests; same credentials dir, restic, manifest, drill; `kubectl` needs the kubeconfig for the timer's user | **Recommended first**     |
| B. CronJob in the cluster running restic                                                                      | Needs an image holding `pg_dump` 18 and `restic`, the R2 secrets as a Secret, and a PVC for staging; the script logic ported       | Later, with #407 alerting |
| C. Both                                                                                                       | Redundant                                                                                                                          | No                        |

Requirements for either option:

- Same restic repository, same tag and host values (`BACKUP_TAG`,
  `BACKUP_HOST` in the existing scripts), so `forget --group-by host,tags`
  keeps applying one retention policy across the move. A different `--host`
  would start a second group and the old snapshots would never be pruned.
- Same manifest content (alembic revision, row counts) so the restore drill
  keeps working unchanged.
- R2 credentials stay in the root-owned directory on the host for option A.
  For option B they would be a Kubernetes Secret created from the same files
  (`--from-file`), which widens who can read them (anyone with cluster
  access); that is a real trade-off.
- One restore drill from a snapshot produced by the k3s database before the
  old Compose `db` is removed.
- #407 (alerting) is parked as phase 3 "after the k3s environment". Do not
  build alerting twice: port it once the backup path is stable on k3s. A
  dead-man's-switch pings the base URL on success and `/fail` on failure
  (#407).

The scripts are not touched by this plan. Follow-up F4.

---

## 9. Observability and day-2

### 9.1 Logs and status

```bash
kubectl -n ytdigest get pods,pvc
kubectl -n ytdigest logs deploy/analyzer --tail=100        # JSON logs (LOG_FORMAT=json)
kubectl -n ytdigest logs deploy/transcriber -f
kubectl -n ytdigest describe pod <name> | sed -n '/Events/,$p'
kubectl top nodes; kubectl top pods -n ytdigest            # metrics-server ships with k3s
journalctl -u k3s -n 100 --no-pager
```

Container logs sit under `/var/lib/rancher/k3s/agent/containerd/` and
`/var/log/pods`; k3s/kubelet rotate by default thresholds (UNVERIFIED by
page; check disk after a week). The Compose `max-size 10m, max-file 3` has no
manifest equivalent.

### 9.2 Upgrading k3s

Re-run the installer with the same env and arguments and a new
`INSTALL_K3S_VERSION` (manual upgrade page). The script regenerates the unit,
so anything you omit is lost: that is why 3.3 keeps the flags in
`/etc/rancher/k3s/config.yaml`. The script does not drain or cordon the node;
stop the workers first (`kubectl -n ytdigest scale deploy --all --replicas=0`)
so no job is in flight, upgrade, then scale back. Take a backup before.
Minor-version jumps one at a time (1.35 to 1.36 to 1.37), reading the k3s
release notes each time (standard Kubernetes practice, UNVERIFIED for k3s
docs).

### 9.3 Application upgrades

Build and import new immutable-tag images (5), `kubectl set image` or edit the
Kustomize tag, run the `migrate` Job by hand if the release has a new Alembic
revision (after a backup), then `kubectl apply -k`. Rollback is the previous
tag; code cannot go back across a migration (forward-only, #380).

### 9.4 Certificates and tokens

K3s server certificates are valid for a year by default and are renewed at
restart when within 90 days of expiry (k3s certificate docs; not fetched,
UNVERIFIED). Rotate the Cloudflare token as in 7.9. Rotate the Postgres
password by changing the Secret and the role together; the restic and R2
credentials follow `backup-restore.md`.

### 9.5 Resource pressure

Watch `kubectl top nodes`, `free -h`, zram usage, and the Ops view for
`jobs_deferred{reason="whisper_busy"}` (#393). If the node is under pressure:
stop the dev stack, lower the transcriber limit and `WHISPER_THREADS`
together (compose.yml note), or move the transcriber to a bigger node later
(#210 item 6). Kubelet eviction can kill pods under memory pressure; the 6 GiB
transcriber is the first candidate, so give it a `priorityClass` below
Postgres (or accept it).

---

## 10. Risks and owner decisions

### 10.1 Decisions (with recommendation)

| #   | Decision                                                                                                                           | Recommendation                                                                                 |
| --- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| D0  | Confirm which machine is production: the bare-metal Ubuntu ProBook observed, or a Windows+WSL2 laptop (AGENTS.md and #380 say WSL) | Confirm; then fix the stale AGENTS.md/#380 text in a separate change                           |
| D1  | Where k3s runs: this laptop, a dedicated node, or later both                                                                       | Dedicated node (C); this laptop only as a time-boxed rehearsal                                 |
| D2  | When: now, or after F1 to F4                                                                                                       | After F1 (immutable tags), F3 (nginx resolver) and F4 (backup path)                            |
| D3  | Dev stack and unrelated projects stopped while k3s production runs                                                                 | Yes                                                                                            |
| D4  | Laptop never suspends (lid, sleep targets)                                                                                         | Yes, or not production                                                                         |
| D5  | Firewall: keep ufw and add only the pod/service CIDRs, versus turning ufw off (k3s docs)                                           | Keep ufw with the two CIDR rules                                                               |
| D6  | k3s version pin                                                                                                                    | `v1.37.2+k3s1`; upgrade deliberately                                                           |
| D7  | Image distribution: tarball import, local registry, or `--docker`                                                                  | Tarball import now; registry when a second node joins                                          |
| D8  | Tunnel: new remotely-managed tunnel, hostname, Access mode (email), 1 or 2 replicas                                                | New tunnel, new hostname, email allow-list, 2 replicas; decide the old `dev-` hostname in #408 |
| D9  | Backup path: A (host timers via `kubectl exec`) now, B (CronJob) with #407                                                         | A first                                                                                        |
| D10 | Cold-standby grace period before deleting Compose volumes                                                                          | 14 days                                                                                        |

### 10.2 Risks

| Id  | Risk                                                                                             | Mitigation                                                                   |
| --- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| R1  | Memory: 2.3 GiB available on a 14 GB host, Whisper limit 6 GiB, k3s adds about 1 GiB             | Stop dev stacks, measure in phase 1, prefer a bigger/dedicated node          |
| R2  | Laptop on Wi-Fi, suspend, battery, shared with development                                       | D1, D4                                                                       |
| R3  | Docker and k3s on one kernel: FORWARD policy, iptables chains, pod egress or DNS fail            | 3.5 checks; `--prefer-bundled-bin`; rehearsal; rollback 3.6                  |
| R4  | nginx `resolver 127.0.0.11` does not exist in Kubernetes, so `/api` fails                        | F3 before cut-over                                                           |
| R5  | Liveness probe restarts kill a running transcription                                             | Generous failureThreshold, no readiness on workers                           |
| R6  | Pod to host Ollama (172.17.0.1) unreachable                                                      | Test in 3.5/6.10; fallback 4.7 option 2                                      |
| R7  | Backups silently stale or absent after cut-over                                                  | 6.3 disables timers; F4 before cut-over; drill from k3s snapshot             |
| R8  | Two stacks processing at once break the #393 exclusion (per-database)                            | No parallel run (6.12)                                                       |
| R9  | `docker save` to containerd import fails on attestations/multi-platform manifests                | 5.2 flags, registry fallback                                                 |
| R10 | local-path: node-local, no snapshots, deleted with k3s uninstall; `fsGroup` ownership UNVERIFIED | Backups (section 8); never uninstall without a restic snapshot; check in 6.7 |
| R11 | Kernel 7.0 plus current k3s: no evidence either way                                              | Phase 1 first-start check is the test; rollback 3.6                          |
| R12 | API server (6443) and kubelet (10250) listen on all interfaces of a roaming laptop               | ufw default-deny incoming (active here); nothing in 3.2 opens them           |
| R13 | Secrets are base64 in a SQLite datastore readable by root                                        | Same exposure as `.env`; optional secrets-encryption later                   |
| R14 | `kubectl` skew: snap client is 1.35 against server 1.37                                          | Use `k3s kubectl` or upgrade the snap                                        |

### 10.3 Proposed follow-up issues (for the PM to file; not filed here)

- F1: immutable per-release image tags in `compose.yml` and builds (#380 slice
  A has the acceptance criteria; this is the dependency).
- F2: `deploy/k3s/` Kustomize base from section 4, plus a CI `kubectl
kustomize` + kubeconform validation that needs no cluster.
- F3: web image: take nginx's resolver from the container instead of the
  hard-coded `127.0.0.11`.
- F4: backup scripts and restore drill that work against a k3s Postgres
  (option A in section 8), with tests.
- F5: Ollama bind for pods (drop-in variant) once the pod-reachability test
  in 6.10 is run.

---

## 11. Phased checklist and effort

| Phase | Work                                                                                                                          | Effort (one person)                    | Exit check                                                             |
| ----- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------- |
| 0     | Owner decisions D0 to D10; confirm the host; (if WSL) stop here and pick another host                                         | 1 hour                                 | decisions recorded on #210                                             |
| 1     | Sections 3.1 to 3.5 on the chosen node: install, smoke pod, Ollama reachability, uninstall rehearsal; measure k3s idle memory | half a day                             | smoke pod passes; compose still healthy; memory number recorded        |
| 2     | Code follow-ups F1, F3, F4 (engineering tasks via the normal process)                                                         | 2 to 4 days (engineering)              | merged, tests green                                                    |
| 3     | Write `deploy/k3s/` (F2), dry-run on a scratch namespace against a restic restore of production data; full smoke check        | 1 to 2 days                            | smoke check passes in k3s with restored data                           |
| 4     | Cloudflare: new tunnel, Access app, `cloudflared` Deployment, verification (section 7) against the scratch stack              | 2 hours (plus Cloudflare account time) | anonymous requests get the Access challenge                            |
| 5     | Cut-over: section 6, in a quiet window                                                                                        | 1.5 hours, downtime 10 to 15 minutes   | manifest match, smoke check, tunnel works, k3s backup snapshot + drill |
| 6     | Cold standby for 14 days, then retire Compose volumes; port #407 alerting; later ArgoCD (#210 item 4)                         | 14 days elapsed, minutes of work       | no rollback needed; compose volumes removed by the owner               |

If phase 0 picks this laptop for production, add the laptop work from 2.2 to
phase 1. If it picks WSL2, there is no phase 1: pick another host first.

---

## 12. Sources (all fetched 2026-11-07)

- k3s quick start: https://docs.k3s.io/quick-start (install command, kubeconfig
  path `/etc/rancher/k3s/k3s.yaml`, `k3s-uninstall.sh`, node token path)
- k3s requirements: https://docs.k3s.io/installation/requirements (2 cores /
  2 GB server minimum, ports 6443, 8472/udp, 10250, ufw and firewalld advice,
  pod CIDR 10.42.0.0/16, service CIDR 10.43.0.0/16, "WSL" not mentioned)
- k3s configuration: https://docs.k3s.io/installation/configuration
  (`/etc/rancher/k3s/config.yaml`, key mapping, `write-kubeconfig-mode`)
- k3s networking services: https://docs.k3s.io/networking/networking-services
  (`--disable=traefik`, `--disable=servicelb`)
- k3s packaged components: https://docs.k3s.io/installation/packaged-components
  (`--disable` repeatable or comma-separated)
- k3s image import: https://docs.k3s.io/add-ons/import-images (tarball
  directory, `k3s ctr images list`, `imagePullPolicy: Never`)
- k3s air-gap: https://docs.k3s.io/installation/airgap
- k3s manual upgrade: https://docs.k3s.io/upgrades/manual
  (`INSTALL_K3S_VERSION`, `INSTALL_K3S_CHANNEL`, no drain, `+k3s1` format)
- k3s advanced (`--docker`, cri-dockerd): https://docs.k3s.io/advanced
- k3s known issues: https://docs.k3s.io/known-issues (Docker snap, iptables
  versions, `--prefer-bundled-bin`)
- k3s release channels: https://update.k3s.io/v1-release/channels
- WSL configuration: https://learn.microsoft.com/en-us/windows/wsl/wsl-config
  (page date 2026-10-16)
- Cloudflare Kubernetes deployment guide:
  https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/deployment-guides/kubernetes/
- Cloudflare remote tunnel creation:
  https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/
- Cloudflare Access policies:
  https://developers.cloudflare.com/cloudflare-one/policies/access/
- Kubernetes Job: https://kubernetes.io/docs/concepts/workloads/controllers/job/
- Kubernetes pod lifecycle and probes:
  https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- Kubernetes secrets how-to:
  https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/
- Kubernetes hostAliases:
  https://kubernetes.io/docs/tasks/network/customize-hosts-file-for-pods/

Could not be verified from a primary page (and so are marked UNVERIFIED where
used): the local-path provisioner path and RWO semantics beyond "RWO only",
`k3s ctr images import`, `ttlSecondsAfterFinished` and `kubectl wait` text,
`kubectl create secret --from-file=key=path` text, the `disable:` list in
`config.yaml`, uninstall script path, the `cloudflared` image tag and digest,
k3s embedded network policy and certificate rotation behaviour, the
`/ready`-style manifest as one file, Cloudflare's automatic CNAME on a
published route, and everything about WSL2 kernel modules and idle behaviour
beyond the Microsoft page.
