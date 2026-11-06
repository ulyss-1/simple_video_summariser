"""Round-3 driver (#382): Phase 1 = Whisper transcripts of the rate-limited videos + export of the live S&P video,
Phase 2 = both models, label r3-ctx16k, pairs shortest to longest. Resumable (re-run it after a crash).

Env: R3_ROOT (default round3 dir), R3_P1_END / R3_HARD "YYYY-MM-DD HH:MM" (defaults 03:30 / 08:10 on 2026-11-04),
R3_DRY=1 (fake Ollama; whisper on R3_DRY_AUDIO instead of downloads), OLLAMA_HOST_OVERRIDE.
STOP file in R3_ROOT: stop cleanly (SIGTERM the current run, kill whisper).
"""
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

TOP = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round3")
R = Path(os.environ.get("R3_ROOT", str(TOP)))
TOOLS = TOP / "tools"
W = "/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round3/repo"
RAW, LOGS, FROZEN, AUDIO = R / "raw", R / "logs", R / "frozen", R / "audio"
WOUT = R / "whisper_out"
MODELS = ["qwen3.5:4b", "gemma4:e4b"]
LABEL = "r3-ctx16k"
P1_END = datetime.strptime(os.environ.get("R3_P1_END", "2026-11-04 03:45"), "%Y-%m-%d %H:%M").timestamp()
HARD = datetime.strptime(os.environ.get("R3_HARD", "2026-11-04 08:10"), "%Y-%m-%d %H:%M").timestamp()
NO_NEW = HARD - 5 * 60   # 08:05: nothing new starts unless it finishes by then
KILL_AT = HARD - 2 * 60  # 08:08: an in-flight run is killed and recorded as interrupted
CAP = 75 * 60
SEC_PER_TOKEN = 0.3      # gating estimate only (observed 0.19-0.25 s per estimated token per model at 16384)
DRY = os.environ.get("R3_DRY") == "1"
CLI_HOST = os.environ.get("R3_CLI_HOST", "172.17.0.1:11434")
OLLAMA_ENV = {**os.environ, "OLLAMA_HOST": CLI_HOST}
WHISPER = "ytdigest-t382c-whisper"
P1_VIDEOS = ["PxP7IoagbtA", "_Yt7NuD-nbk", "_m1XcP1addc", "hoYWNqh_u5I", "HKskNSEb5_c"]  # shortest first
P1_DUR = {"PxP7IoagbtA": 1046, "_Yt7NuD-nbk": 1235, "_m1XcP1addc": 1342, "hoYWNqh_u5I": 1470, "HKskNSEb5_c": 2608}
LIVE = "YjdfcHo_lE4"
current: dict = {"proc": None}


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(LOGS / "phase3.log", "a") as f:
        f.write(line + "\n")


def sh(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def stopped() -> bool:
    return (R / "STOP").exists()


def status(text: str) -> None:
    (R / "STATUS.md").write_text(f"# STATUS (round 3)\n\nUpdated {time.strftime('%F %T')} IST\n\n{text}\n")


# ----------------------------------------------------------------------------- Phase 1
def fetch(vid: str, with_audio: bool) -> dict | None:
    out = AUDIO / f"{vid}.fetch.json"
    if out.exists() and (not with_audio or "audio" in json.loads(out.read_text())):
        return json.loads(out.read_text())
    cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{W}:/w:ro", "-v", f"{TOOLS}:/tools:ro", "-v", f"{AUDIO}:/audio",
           "--entrypoint", "python", "ytdigest-backend:latest", "-I", "/tools/p1_fetch.py", vid, "/audio", f"/audio/{vid}.fetch.json"]
    if not with_audio:
        cmd.append("--no-audio")
    r = sh(cmd)
    if r.returncode != 0 or not out.exists():
        log(f"fetch {vid} failed once (rc={r.returncode}), retrying in 30 s")
        time.sleep(30)
        r = sh(cmd)
    (LOGS / f"fetch_{vid}.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr[-4000:])
    if r.returncode != 0 or not out.exists():
        log(f"FETCH FAILED {vid} rc={r.returncode}: {r.stderr.strip().splitlines()[-1][:300] if r.stderr.strip() else ''}")
        return None
    return json.loads(out.read_text())


def freeze(vid: str, fetch_json: Path, src: Path, kind: str) -> dict | None:
    r = sh([str(TOOLS / "run.sh"), "python", str(TOOLS / "freeze.py"), str(R), vid, str(fetch_json), str(src), kind])
    if r.returncode != 0:
        log(f"FREEZE FAILED {vid}: {r.stderr[-500:]}")
        return None
    info = json.loads(r.stdout.strip().splitlines()[-1])
    sha = {f: hashlib.sha256((FROZEN / f"{vid}.{f}.json").read_bytes()).hexdigest() for f in ("meta", "segments")}
    mp = R / "manifest_phase1.json"
    man = json.loads(mp.read_text()) if mp.exists() else []
    man = [m for m in man if m["video_id"] != vid]
    fj = json.loads(fetch_json.read_text())["meta"]
    man.append({"video_id": vid, "title": fj["title"], "channel_id": fj["channel_id"], "duration_sec": fj["duration_sec"],
                "transcript_source": "whisper", "sha256": sha, "frozen_at": time.strftime("%F %T"), **info})
    mp.write_text(json.dumps(man, indent=1))
    log(f"FROZEN {vid} {info}")
    return info


def export_live() -> None:
    if (FROZEN / f"{LIVE}.segments.json").exists():
        return
    base = os.environ.get("R3_LIVE_API", "http://127.0.0.1:8080") + f"/api/videos/{LIVE}"
    head = json.loads(urllib.request.urlopen(base, timeout=20).read())  # GET only
    segs, off, total = [], 0, None
    while total is None or off < total:
        doc = json.loads(urllib.request.urlopen(f"{base}/transcript?offset={off}&limit=200", timeout=30).read())
        total = doc["total_segments"]
        if not doc["segments"]:
            break
        segs += doc["segments"]
        off += len(doc["segments"])
    assert len(segs) == total, (len(segs), total)
    assert head["transcript"]["source"] == "whisper"
    src = WOUT / f"{LIVE}.live.json"
    src.write_text(json.dumps({"source": doc["source"], "language": doc["language"], "transcript_id": doc["transcript_id"],
                               "segments": segs}))
    fj = fetch(LIVE, False)
    if fj:
        freeze(LIVE, AUDIO / f"{LIVE}.fetch.json", src, "live_api")


def whisper_cmd(vid: str, a: dict, out: Path) -> list:
    return ["docker", "run", "-d", "--name", WHISPER, "--cpus", "3", "--memory", "6g", "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", "ytdigest_whisper-models:/models:ro", "-v", f"{AUDIO}:/audio:ro", "-v", f"{TOOLS}:/tools:ro",
            "-v", f"{WOUT}:/wout",
            "-e", "HOME=/tmp", "-e", "HF_HOME=/models", "-e", "HF_HUB_OFFLINE=1", "-e", "WHISPER_MODEL=large-v3",
            "-e", "WHISPER_COMPUTE=int8", "-e", "WHISPER_THREADS=3", "-e", "OMP_NUM_THREADS=4",
            "-e", "DATABASE_URL=postgresql://none:none@127.0.0.1:1/none", "-e", "PYTHONPATH=/app", "-e", "AUDIO_DIR=/audio",
            "-e", f"AUDIO_REL={a['rel_path']}", "-e", f"AUDIO_BYTES={a['bytes']}", "-e", f"AUDIO_SEC={a['duration_sec']}",
            "-e", f"OUT=/wout/{out.name}",
            "--entrypoint", "python", "ytdigest-whisper:latest", "/tools/p1_whisper.py"]


def run_whisper(vid: str, a: dict, deadline: float) -> Path | None:
    out = WOUT / f"{vid}.whisper.json"
    if out.exists():
        return out
    t0 = time.time()
    running = sh(["docker", "inspect", "-f", "{{.State.Running}}|{{.Config.Env}}", WHISPER])
    if running.returncode == 0 and running.stdout.startswith("true") and f"OUT=/wout/{out.name}" in running.stdout:
        st = sh(["docker", "inspect", "-f", "{{.State.StartedAt}}", WHISPER]).stdout.strip()
        try:
            t0 = datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        log(f"WHISPER ADOPTED running container for {vid} (driver restart)")
    else:
        sh(["docker", "rm", "-f", WHISPER])
        r = sh(whisper_cmd(vid, a, out))
        if r.returncode != 0:
            log(f"whisper start failed: {r.stderr[:300]}")
            return None
        log(f"WHISPER START {vid} audio={a['duration_sec']:.0f}s")
    while True:
        st = sh(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", WHISPER]).stdout.split()
        if st and st[0] == "false":
            break
        if stopped() or time.time() > deadline:
            log(f"WHISPER KILL {vid} ({'STOP' if stopped() else 'phase-1 deadline'}) after {time.time() - t0:.0f}s")
            sh(["docker", "rm", "-f", WHISPER])
            return None
        if mem_avail_kb() < 1024 * 1024:
            log(f"MemAvailable < 1 GB during whisper ({mem_avail_kb()} kB): stopping whisper (a result)")
            (LOGS / f"whisper_{vid}_watchdog.txt").write_text(f"tripped {time.strftime('%T')} avail={mem_avail_kb()}\n")
            sh(["docker", "rm", "-f", WHISPER])
            return None
        time.sleep(10)
    lg = sh(["docker", "logs", WHISPER])
    (LOGS / f"whisper_{vid}.log").write_text(lg.stdout + lg.stderr)
    sh(["docker", "rm", "-f", WHISPER])
    if not out.exists():
        log(f"WHISPER FAILED {vid}: {(lg.stdout + lg.stderr)[-400:]}")
        return None
    log(f"WHISPER DONE {vid} wall={time.time() - t0:.0f}s")
    return out


def phase1() -> None:
    export_live()
    audio: dict[str, dict] = {}
    blocked = 0
    for vid in P1_VIDEOS[:4]:
        if (FROZEN / f"{vid}.segments.json").exists():
            continue
        if stopped() or time.time() > P1_END:
            return
        fj = fetch(vid, True)
        if fj is None:
            blocked += 1
            log(f"ALERT audio/metadata fetch failed for {vid}")
            (R / "ALERT_FETCH_FAILED").write_text(f"{time.strftime('%F %T')} {vid}\n")
            if blocked >= 2 and not audio:
                log("ALERT audio looks blocked: two failures, no audio yet; skipping Phase 1")
                return
            continue
        audio[vid] = fj["audio"]
        time.sleep(15)
    rtf = None
    for vid in P1_VIDEOS:
        if stopped() or time.time() > P1_END:
            break
        if (FROZEN / f"{vid}.segments.json").exists():
            continue
        if vid == P1_VIDEOS[4]:
            if rtf is None or time.time() + P1_DUR[vid] * rtf * 1.25 > P1_END:
                log("HKskNSEb5_c skipped: no clear time left")
                break
            fetch(vid, True)
        fj = json.loads((AUDIO / f"{vid}.fetch.json").read_text()) if (AUDIO / f"{vid}.fetch.json").exists() else None
        if fj is None or "audio" not in fj:
            continue
        a = fj["audio"]
        est = a["duration_sec"] * (rtf or 0.8)
        if rtf is not None and time.time() + est > P1_END:
            log(f"no time for whisper {vid} (est {est:.0f}s)")
            continue
        t0 = time.time()
        out = run_whisper(vid, a, P1_END)
        if out is None:
            continue
        rtf = (time.time() - t0) / a["duration_sec"]
        log(f"observed wall/audio ratio {rtf:.2f}")
        freeze(vid, AUDIO / f"{vid}.fetch.json", out, "whisper_file")
        status(f"Phase 1: whisper done for {vid}; next ones follow. See logs/phase3.log")


# ----------------------------------------------------------------------------- Phase 2
def mem_avail_kb() -> int:
    return int([ln for ln in open("/proc/meminfo") if ln.startswith("MemAvailable")][0].split()[1])


def stem(vid: str, model: str) -> str:
    return f"{LABEL}__{vid}__{model.replace(':', '_')}"


def result(vid: str, model: str) -> dict | None:
    p = RAW / f"{stem(vid, model)}.result.json"
    return json.loads(p.read_text()) if p.exists() else None


def finished(vid: str, model: str) -> bool:
    r = result(vid, model)
    return bool(r) and r["status"] in ("ok", "failed", "timeout", "watchdog_tripped", "interrupted")


def run_cmd(vid: str, model: str, cap: float) -> subprocess.Popen:
    cmd = [str(TOOLS / "run.sh"), "python", str(TOOLS / "run_one.py"), "--model", model, "--video", vid,
           "--label", LABEL, "--raw", str(RAW), "--force", "--cap-sec", str(cap),
           "--deadline-epoch", str(KILL_AT), "--cli-host", CLI_HOST]
    if DRY:
        cmd.append("--no-unload")
    lf = open(LOGS / f"{stem(vid, model)}.log", "w")
    return subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env={**os.environ, "OLLAMA_NUM_CTX": "16384"})


def plain_run(vid: str, model: str) -> str:
    cap = min(CAP, KILL_AT - time.time())
    log(f"START {LABEL} {vid} {model} cap={cap:.0f}s")
    p = run_cmd(vid, model, cap)
    current["proc"] = p
    while p.poll() is None:
        if stopped():
            log("STOP file: SIGTERM to run")
            for q in sh(["pgrep", "-f", "tools/run_one.py"]).stdout.split():
                try:
                    os.kill(int(q), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                p.wait(timeout=120)
            except subprocess.TimeoutExpired:
                p.kill()
            break
        time.sleep(2)
    current["proc"] = None
    r = result(vid, model)
    st = r["status"] if r else "no_result"
    log(f"END   {LABEL} {vid} {model} -> {st}" + (f" wall={r['wall_sec']:.0f}s" if r else ""))
    return st


def manifest() -> list[dict]:
    p = R / "manifest.json"
    return sorted(json.loads(p.read_text()), key=lambda m: m["est_tokens"]) if p.exists() else []


def phase2() -> None:
    have = {m["video_id"] for m in manifest()}
    new = [p.name.split(".")[0] for p in FROZEN.glob("*.segments.json")
           if p.name.split(".")[0] not in have and (FROZEN / (p.name.split(".")[0] + ".meta.json")).exists()]
    if new:
        r = sh([str(TOOLS / "run.sh"), "python", str(TOOLS / "load.py"), str(R)] + sorted(new))
        log(f"loaded {sorted(new)}: {r.stdout[-300:]} {r.stderr[-300:]}")
    fails = {m: 0 for m in MODELS}
    for m in manifest():
        for x in MODELS:
            r = result(m["video_id"], x)
            if r and r["status"] != "ok" and finished(m["video_id"], x):
                fails[x] += 1
    plan = {}
    for m in manifest():
        vid, n = m["video_id"], m["est_tokens"]
        seed = f"{LABEL}:{vid}"
        order = random.Random(seed).sample(MODELS, 2)
        plan[vid] = {"seed": seed, "order": order, "est_tokens": n}
        (R / "order_plan_r3.json").write_text(json.dumps(plan, indent=1))
        for model in order:
            if finished(vid, model):
                continue
            if stopped():
                log("STOP seen")
                return
            if fails[model] >= 3:
                log(f"{model} skipped (3 failures)")
                continue
            est = min(n * SEC_PER_TOKEN, CAP)
            if time.time() + est > NO_NEW:
                log(f"no time for {vid} {model}: est {est:.0f}s, would end after {datetime.fromtimestamp(NO_NEW):%H:%M}")
                return
            if sh(["pgrep", "-f", "tools/run_one.py"]).stdout.strip() and not DRY:
                log("another run_one is running; abort")
                return
            if plain_run(vid, model) != "ok":
                fails[model] += 1
            status(f"Phase 2 in progress; last: {vid} {model}. See logs/phase3.log, raw/*.result.json")


def unload_models() -> None:
    try:
        ps = json.loads(urllib.request.urlopen(os.environ.get("OLLAMA_HOST_OVERRIDE", "http://172.17.0.1:11434") + "/api/ps", timeout=10).read())
        for m in ps.get("models", []):
            sh(["ollama", "stop", m["name"]], env=OLLAMA_ENV)
    except Exception as e:  # noqa: BLE001
        log(f"unload failed {type(e).__name__}")


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    for d in (RAW, FROZEN, AUDIO, WOUT):
        d.mkdir(exist_ok=True)
    if not DRY:
        names = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
        while "ytdigest-transcriber-1" in names:
            log("live transcriber still running: waiting")
            if stopped():
                return 0
            time.sleep(30)
            names = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
    side = None
    if not DRY:
        if sh(["bash", str(TOP / "db.sh")]).returncode != 0:
            log("db.sh failed")
        if not (R / "host_facts_before.txt").exists():
            sh(["bash", str(TOOLS / "host_facts.sh"), str(R / "host_facts_before.txt")])
        pidf = R / "sidecar.pid"
        if pidf.exists() and Path(f"/proc/{pidf.read_text().strip()}").exists():
            log("sidecar already running (driver restart): kept")
        else:
            side = subprocess.Popen([sys.executable, str(TOOLS / "sidecar.py")])
            pidf.write_text(str(side.pid))
    try:
        if not DRY or os.environ.get("R3_DRY_P1"):
            try:
                phase1()
            except Exception:  # noqa: BLE001
                import traceback
                log("PHASE 1 crashed: " + traceback.format_exc()[-1000:])
            sh(["docker", "rm", "-f", WHISPER])
        log(f"PHASE 1 over; frozen: {sorted(p.name.split('.')[0] for p in FROZEN.glob('*.segments.json'))}")
        (R / "PHASE1_DONE").write_text(time.strftime("%F %T"))
        if not stopped():
            try:
                phase2()
            except Exception:  # noqa: BLE001
                import traceback
                log("PHASE 2 crashed: " + traceback.format_exc()[-1000:])
    finally:
        log("driver finished")
        (R / "ORCH_DONE").write_text(time.strftime("%F %T"))
        sh(["docker", "rm", "-f", WHISPER])
        if not DRY:
            unload_models()
            sh(["bash", str(TOOLS / "host_facts.sh"), str(R / "host_facts_after.txt")])
        if side:
            side.terminate()
        status("Driver finished (see logs/phase3.log). Next: tools/run.sh python tools/mech.py; stats.py; make_packs.py; cleanup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
