"""Round-2 Phase 2 orchestrator (#382 remaining steps), resumable, checkpointing every run to raw/.

Steps (priority order): 1 concurrent memory (Whisper beside a summary run), 2 repeats of bIvyxp-FAGU,
3 pairs of the previously rate-limited videos (shortest to longest). All with OLLAMA_NUM_CTX=16384.
Waits for GO. STOP file: SIGTERM the current run, stop. Hard end 23:30 IST (no run starts after 23:15).
Env for dry runs: R2_ROOT (default round2 dir), R2_HARD ("YYYY-MM-DD HH:MM"), R2_DRY=1 (fake Ollama, no whisper).
"""
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

R = Path(os.environ.get("R2_ROOT", "/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2"))
RAW = R / "raw"
LOGS = R / "logs"
MODELS = ["qwen3.5:4b", "gemma4:e4b"]
HARD = datetime.strptime(os.environ.get("R2_HARD", "2026-11-03 23:30"), "%Y-%m-%d %H:%M").timestamp()
NO_NEW = HARD - 15 * 60  # 23:15: no new run after this
END_MARGIN = 2 * 60
CAP = 75 * 60
DRY = os.environ.get("R2_DRY") == "1"
CONC_VIDEO = "EfGtEDJyRvk"
REP_VIDEO = "bIvyxp-FAGU"
SEC_PER_TOKEN = 0.25  # gating estimate only (round 1: 0.18 to 0.25 s per estimated token at 16k)
CLI_HOST = os.environ.get("R2_CLI_HOST", "172.17.0.1:11434")
WHISPER = "ytdigest-t382b-whisper"
OLLAMA_ENV = {**os.environ, "OLLAMA_HOST": CLI_HOST}
current: dict = {"proc": None}


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(LOGS / "phase2.log", "a") as f:
        f.write(line + "\n")


def stem(label: str, vid: str, model: str) -> str:
    return f"{label}__{vid}__{model.replace(':', '_')}"


def result(label: str, vid: str, model: str) -> dict | None:
    p = RAW / f"{stem(label, vid, model)}.result.json"
    return json.loads(p.read_text()) if p.exists() else None


def finished(label: str, vid: str, model: str) -> bool:
    r = result(label, vid, model)
    return bool(r) and r["status"] in ("ok", "failed", "timeout", "watchdog_tripped")


def stopped() -> bool:
    return (R / "STOP").exists()


def time_left() -> float:
    return HARD - END_MARGIN - time.time()


def can_start(est_sec: float) -> bool:
    return (not stopped()) and time.time() < NO_NEW and time_left() > min(est_sec, CAP)


def manifest() -> list[dict]:
    return sorted(json.loads((R / "manifest.json").read_text()), key=lambda m: m["est_tokens"])


def est_tokens(vid: str) -> int:
    return next(m["est_tokens"] for m in manifest() if m["video_id"] == vid)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def run_cmd(label: str, vid: str, model: str, cap: float) -> subprocess.Popen:
    cmd = [str(R / "tools/run.sh"), "python", str(R / "tools/run_one.py"), "--model", model, "--video", vid,
           "--label", label, "--raw", str(RAW), "--force", "--cap-sec", str(cap),
           "--deadline-epoch", str(HARD - 60), "--cli-host", CLI_HOST]
    if DRY:
        cmd.append("--no-unload")
    lf = open(LOGS / f"{stem(label, vid, model)}.log", "w")
    return subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env={**os.environ, "OLLAMA_NUM_CTX": "16384"})


def wait_run(p: subprocess.Popen) -> None:
    current["proc"] = p
    while p.poll() is None:
        if stopped():
            log("STOP file: SIGTERM to run")
            # run_one.py's SIGTERM handler writes result.json (interrupted), unloads models, exits
            for q in sh(["pgrep", "-f", "tools/run_one.py"]).stdout.split():
                try:
                    os.kill(int(q), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            p.wait(timeout=120)
            break
        time.sleep(2)
    current["proc"] = None


def plain_run(label: str, vid: str, model: str) -> str:
    cap = min(CAP, time_left())
    log(f"START {label} {vid} {model} cap={cap:.0f}s")
    wait_run(run_cmd(label, vid, model, cap))
    r = result(label, vid, model)
    st = r["status"] if r else "no_result"
    log(f"END   {label} {vid} {model} -> {st} wall={r['wall_sec']:.0f}s" if r else f"END   {label} {vid} {model} -> no_result")
    return st


# ---------------------------------------------------------------- step 1: concurrent
def mem_avail_kb() -> int:
    return int([line for line in open("/proc/meminfo") if line.startswith("MemAvailable")][0].split()[1])


def oom_kill_count() -> int:
    for line in open("/proc/vmstat"):
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return -1


def unload_models() -> None:
    import urllib.request
    try:
        ps = json.loads(urllib.request.urlopen(os.environ.get("OLLAMA_HOST_OVERRIDE", "http://172.17.0.1:11434") + "/api/ps", timeout=10).read())
    except Exception as e:  # noqa: BLE001
        log(f"unload_models: /api/ps failed {type(e).__name__}")
        return
    for m in ps.get("models", []):
        sh(["ollama", "stop", m["name"]], env=OLLAMA_ENV)
    time.sleep(3)


def whisper_alive() -> bool:
    return sh(["docker", "inspect", "-f", "{{.State.Running}}", WHISPER]).stdout.strip() == "true"


def whisper_cpu() -> float:
    out = sh(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", WHISPER]).stdout.strip().rstrip("%")
    try:
        return float(out)
    except ValueError:
        return 0.0


def start_whisper() -> bool:
    a = json.loads((R / "audio" / "audio.json").read_text())
    sh(["docker", "rm", "-f", WHISPER])
    img = "ytdigest-backend:latest" if DRY else "ytdigest-whisper:latest"
    cmd = ["docker", "run", "-d", "--name", WHISPER, "--cpus", "3", "--memory", "6g", "--network", "none",
           "-v", "ytdigest_whisper-models:/models:ro", "-v", f"{R}/audio:/audio:ro", "-v", f"{R}/tools:/tools:ro",
           "-e", "HF_HOME=/models", "-e", "HF_HUB_OFFLINE=1", "-e", "WHISPER_MODEL=large-v3", "-e", "WHISPER_COMPUTE=int8",
           "-e", "WHISPER_THREADS=3", "-e", "DATABASE_URL=postgresql://none:none@127.0.0.1:1/none", "-e", "OMP_NUM_THREADS=4", "-e", "AUDIO_DIR=/audio", "-e", "PYTHONPATH=/app",
           "-e", f"AUDIO_REL={a['rel_path']}", "-e", f"AUDIO_BYTES={a['bytes']}", "-e", f"AUDIO_SEC={a['duration_sec']}",
           "--entrypoint", "python", img, "/tools/whisper_loop.py"]
    if DRY:
        cmd = ["docker", "run", "-d", "--name", WHISPER, "--network", "none", "--entrypoint", "sleep", img, "infinity"]
    r = sh(cmd)
    if r.returncode != 0:
        log(f"whisper start failed: {r.stderr[:300]}")
        return False
    busy = 0
    deadline = time.time() + 8 * 60
    while time.time() < deadline and busy < 3:
        time.sleep(5)
        if not whisper_alive():
            log("whisper container died during warm-up: " + sh(["docker", "logs", "--tail", "20", WHISPER]).stdout[-600:])
            return False
        if mem_avail_kb() < 1024 * 1024:
            log(f"MemAvailable < 1 GB during whisper warm-up ({mem_avail_kb()} kB): stopping whisper")
            sh(["docker", "stop", "-t", "5", WHISPER])
            return False
        busy = busy + 1 if (whisper_cpu() > 150 or DRY) else 0
    log(f"whisper busy (cpu>150% x3): {busy >= 3}")
    return busy >= 3


def supervise_whisper(stop: threading.Event, events: list, mw_out: Path) -> None:
    while not stop.wait(10):
        if mw_out.exists() and "TRIPPED" in mw_out.read_text():
            continue  # the watchdog stopped it on purpose: never restart after a trip
        if not whisper_alive():
            state = sh(["docker", "inspect", "-f", "{{json .State}}", WHISPER]).stdout.strip()
            events.append({"t": time.strftime("%T"), "event": "whisper_not_running_restart", "state_before": state})
            log(f"whisper not running ({state[:200]}): docker start")
            sh(["docker", "start", WHISPER])


def conc_run(model: str) -> str:
    label = "r2-ctx16k-conc"
    n = est_tokens(CONC_VIDEO)
    if not can_start(n * SEC_PER_TOKEN * 1.2):
        log(f"no time for {label} {model}")
        return "skipped_time"
    unload_models()
    if mem_avail_kb() < 2 * 1024 * 1024:
        log(f"MemAvailable only {mem_avail_kb()} kB before the concurrent run; waiting up to 3 min")
        t_end = time.time() + 180
        while time.time() < t_end and mem_avail_kb() < 2 * 1024 * 1024:
            time.sleep(5)
    oom0 = oom_kill_count()
    if not start_whisper():
        log("whisper warm-up failed; aborting concurrent run for " + model)
        lg = sh(["docker", "logs", WHISPER])
        (LOGS / f"whisper_{model.replace(':', '_')}_failed_{int(time.time())}.log").write_text(
            lg.stdout + lg.stderr + "\nSTATE " + sh(["docker", "inspect", "-f", "{{json .State}}", WHISPER]).stdout)
        sh(["docker", "rm", "-f", WHISPER])
        return "whisper_failed"
    cap = min(CAP, time_left())
    log(f"START {label} {CONC_VIDEO} {model} cap={cap:.0f}s (whisper busy)")
    mw_out = LOGS / f"mem_concurrent_{model.replace(':', '_')}.jsonl"
    stopf = R / "MEMWATCH_STOP"
    stopf.unlink(missing_ok=True)
    p = run_cmd(label, CONC_VIDEO, model, cap)
    mw = subprocess.Popen([sys.executable, str(R / "tools/mem_watch.py"), "--out", str(mw_out), "--whisper", WHISPER,
                           "--pid", str(p.pid), "--stop-file", str(stopf)], env=OLLAMA_ENV)
    ev: list = []
    stop = threading.Event()
    th = threading.Thread(target=supervise_whisper, args=(stop, ev, mw_out), daemon=True)
    th.start()
    wait_run(p)
    stop.set()
    stopf.touch()
    try:
        mw.wait(timeout=30)
    except subprocess.TimeoutExpired:
        mw.kill()
    tripped = "TRIPPED" in mw_out.read_text() if mw_out.exists() else False
    unload_models()
    rp = RAW / f"{stem(label, CONC_VIDEO, model)}.result.json"
    if tripped and rp.exists():
        rr = json.loads(rp.read_text())
        if rr["status"] != "watchdog_tripped":
            rr["status_before_rewrite"] = rr["status"]
            rr["status"] = "watchdog_tripped"
            rr["reason"] = [x for x in mw_out.read_text().splitlines() if x.startswith("TRIPPED")][0]
            rp.write_text(json.dumps(rr, indent=1, default=str))
    whisper_log = sh(["docker", "logs", WHISPER]).stdout
    (LOGS / f"whisper_{model.replace(':', '_')}.log").write_text(whisper_log)
    insp = sh(["docker", "inspect", "-f", "{{json .State}}", WHISPER]).stdout.strip()
    sh(["docker", "rm", "-f", WHISPER])
    iters = [json.loads(x) for x in whisper_log.splitlines() if x.startswith("{")]
    note = {"model": model, "mem_watch_tripped": tripped, "whisper_state_at_end": insp, "whisper_events": ev,
            "whisper_iterations": len(iters), "whisper_iteration_errors": sum(1 for i in iters if "error" in i),
            "kernel_oom_kill_counter_before": oom0, "kernel_oom_kill_counter_after": oom_kill_count(),
            "dmesg_oom": "dmesg not permitted for this user; /proc/vmstat oom_kill counter used"}
    (RAW / f"{stem(label, CONC_VIDEO, model)}.conc_note.json").write_text(json.dumps(note, indent=1))
    r = result(label, CONC_VIDEO, model)
    st = r["status"] if r else "no_result"
    log(f"END   {label} {model} -> {st} tripped={tripped} whisper_iters={len(iters)} notes={note['dmesg_oom'][:80]}")
    # let memory recover before anything else
    t_end = time.time() + 180
    while time.time() < t_end and mem_avail_kb() <= 3 * 1024 * 1024:
        time.sleep(5)
    if mem_avail_kb() <= 3 * 1024 * 1024:
        log(f"MemAvailable did not recover ({mem_avail_kb()} kB): halting step 1")
        return "mem_not_recovered"
    return st


def step1() -> None:
    order = random.Random("r2-conc").sample(MODELS, 2)
    (R / "order_plan_r2.json").write_text(json.dumps({"step1": order}))
    log(f"STEP 1 order {order}")
    for m in order:
        if finished("r2-ctx16k-conc", CONC_VIDEO, m) or stopped():
            continue
        try:
            if conc_run(m) in ("whisper_failed", "mem_not_recovered"):
                log("STEP 1 halted (see above); continuing with step 2 and 3")
                return
        except Exception:  # noqa: BLE001
            import traceback
            log("STEP 1 crashed, recorded, continuing: " + traceback.format_exc()[-800:])
            sh(["docker", "rm", "-f", WHISPER])
            return


# ---------------------------------------------------------------- step 2: repeats
def step2() -> None:
    runs = [(m, n) for m in MODELS for n in (1, 2, 3)]
    random.Random("r2-rep").shuffle(runs)
    plan = (json.loads((R / "order_plan_r2.json").read_text()) if (R / "order_plan_r2.json").exists() else {})
    plan["step2"] = runs
    (R / "order_plan_r2.json").write_text(json.dumps(plan))
    log(f"STEP 2 order {runs}")
    fails = {m: 0 for m in MODELS}
    for m, n in runs:  # failures of earlier (already finished) runs count towards the stop rule
        r = result(f"r2-ctx16k-rep{n}", REP_VIDEO, m)
        if r and r["status"] != "ok" and finished(f"r2-ctx16k-rep{n}", REP_VIDEO, m):
            fails[m] += 1
    for m, n in runs:
        label = f"r2-ctx16k-rep{n}"
        if finished(label, REP_VIDEO, m):
            continue
        if fails[m] >= 3:
            log(f"STEP 2: {m} skipped (3 failures)")
            continue
        if not can_start(est_tokens(REP_VIDEO) * SEC_PER_TOKEN * 1.2):
            log("STEP 2: no time for the next run")
            return
        if plain_run(label, REP_VIDEO, m) != "ok":
            fails[m] += 1


# ---------------------------------------------------------------- step 3: pairs
def sync_new_transcripts() -> None:
    have = {m["video_id"] for m in manifest()}
    new = [p.name.split(".")[0] for p in (R / "frozen").glob("*.segments.json") if p.name.split(".")[0] not in have
           and (R / "frozen" / (p.name.split(".")[0] + ".meta.json")).exists()]
    if new:
        log(f"loading new transcripts {new}")
        r = sh([str(R / "tools/run.sh"), "python", str(R / "tools/load.py"), str(R)] + new)
        log(r.stdout[-300:] + r.stderr[-300:])


def step3(ids: list[str], must: list[str]) -> None:
    fails = {m: 0 for m in MODELS}
    while not stopped() and time.time() < NO_NEW:
        sync_new_transcripts()
        todo = [m for m in manifest() if m["video_id"] in ids
                and not all(finished("r2-ctx16k", m["video_id"], x) for x in MODELS)]
        if not todo:
            if all(v in {m["video_id"] for m in manifest()} for v in must):
                return
            if sh(["pgrep", "-f", "acquire_patient.sh"]).returncode != 0:
                log("STEP 3: acquisition script not running and nothing left to run")
                return
            log("STEP 3: waiting for acquisition")
            time.sleep(120)
            continue
        v = todo[0]
        vid, n = v["video_id"], v["est_tokens"]
        order = random.Random(f"r2-ctx16k:{vid}").sample(MODELS, 2)
        plan = (json.loads((R / "order_plan_r2.json").read_text()) if (R / "order_plan_r2.json").exists() else {})
        plan.setdefault("step3", {})[vid] = order
        (R / "order_plan_r2.json").write_text(json.dumps(plan))
        if time_left() < 2 * n * SEC_PER_TOKEN:
            log(f"STEP 3: no time for the pair {vid} ({n} tokens)")
            return
        for m in order:
            if finished("r2-ctx16k", vid, m):
                continue
            if fails[m] >= 3:
                log(f"STEP 3: {m} skipped (3 failures)")
                continue
            if not can_start(n * SEC_PER_TOKEN * 1.2):
                log(f"STEP 3: no time for {vid} {m}")
                return
            if plain_run("r2-ctx16k", vid, m) != "ok":
                fails[m] += 1
            if stopped():
                return


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    while not (R / "GO").exists():
        if stopped():
            return 0
        time.sleep(10)
    while not DRY:
        names = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
        if "ytdigest-transcriber-1" not in names and "ytdigest-analyzer-1" not in names:
            break
        log("GO exists but live transcriber/analyzer still running: waiting")
        if stopped():
            return 0
        time.sleep(30)
    log(f"GO seen; hard end {datetime.fromtimestamp(HARD)}")
    if not DRY:
        sh(["bash", str(R / "tools/host_facts.sh"), str(R / "host_facts_before.txt")])
        g = sh(["git", "-C", "/home/hamlet/proj/simple_video_summariser/.claude/worktrees/agent-aaf2b01791212b49d", "diff", "--stat",
                "fa92b8c", "HEAD", "--", "adapters/summarize", "services/analyzer", "services/eval", "common/chunking"])
        (R / "code_diff_since_round1.txt").write_text(g.stdout or "no change in adapters/summarize, services/analyzer, services/eval, common/chunking since fa92b8c\n")
        log("code diff since round-1 commit fa92b8c: " + ((g.stdout.strip().splitlines() or ["none"])[-1]))
    side = None
    if not DRY:
        side = subprocess.Popen([sys.executable, str(R / "tools/sidecar.py")])
        (R / "sidecar.pid").write_text(str(side.pid))
    try:
        import traceback
        if not DRY or os.environ.get("R2_DRY_STEP1"):
            try:
                step1()
            except Exception:  # noqa: BLE001
                log("STEP 1 crashed: " + traceback.format_exc()[-800:])
        try:
            step2()
        except Exception:  # noqa: BLE001
            log("STEP 2 crashed: " + traceback.format_exc()[-800:])
        main5 = ["PxP7IoagbtA", "_Yt7NuD-nbk", "_m1XcP1addc", "hoYWNqh_u5I", "HKskNSEb5_c"]
        if DRY:
            step3([m["video_id"] for m in manifest()], [])
        else:
            step3(main5 + ["7Oi1dqcI9pc", "lyxcHOp6LhM"], main5)
    finally:
        log("orchestrator finished")
        (R / "ORCH_DONE").write_text(time.strftime("%F %T"))
        (R / "ACQ_STOP").touch()
        sh(["docker", "rm", "-f", WHISPER])
        if not DRY:
            sh(["bash", str(R / "tools/host_facts.sh"), str(R / "host_facts_after.txt")])
        if side:
            side.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
