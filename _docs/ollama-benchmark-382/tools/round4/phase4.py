"""Round-4 driver (#382): 6 new videos, both models, label r4-ctx16k. Reuses phase3.py's building blocks.

Per video, shortest first: transcript = captions (cheap route, retried once) else Whisper large-v3 (container,
never overlapping an LLM run), then the pair (random model order, seed r4-ctx16k:<video>). Resumable.
HARD PAUSE 08:50 IST 2026-11-05: no new step unless predicted to end by 08:45; in-flight run killed at 08:48.
STOP file in round4/: clean stop. Env: R4_HARD "YYYY-MM-DD HH:MM", OLLAMA_HOST_OVERRIDE.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase3 as p  # noqa: E402

p.HARD = datetime.strptime(os.environ.get("R4_HARD", "2026-11-05 08:50"), "%Y-%m-%d %H:%M").timestamp()
p.NO_NEW = p.HARD - 5 * 60
p.KILL_AT = p.HARD - 2 * 60
p.P1_END = p.NO_NEW
p.SEC_PER_TOKEN = 0.227  # max observed gemma4:e4b wall per est token in round 4 (0.126-0.227)
R, TOOLS, FROZEN, AUDIO, WOUT, LOGS = p.R, p.TOOLS, p.FROZEN, p.AUDIO, p.WOUT, p.LOGS
W = p.W
# (video_id, channel, duration_sec); order = shortest first
VIDEOS = [("k1xJbNAGmFg", "BWB", 1084), ("zVc-S0Od8_0", "BWB", 1117), ("iM3YsKfRh6E", "Dr Berg", 1359),
          ("hoYWNqh_u5I", "Sven Carlin", 1470), ("SMFV5A8aq0c", "Joseph Carlson", 1550), ("eeawNrRi-cc", "After Hours", 1516)]  # substitute for HKskNSEb5_c (43 min, does not fit), owner-approved 2026-11-05
WHISPER_RATIO = [2.54]  # wall/audio; round 3 (3 threads): 2.4-2.6; round 4 uses 6 cpus/threads, updated from the first run
_orig_cmd = p.whisper_cmd


def _cmd6(vid, a, out):
    c = _orig_cmd(vid, a, out)
    return [x.replace("WHISPER_THREADS=3", "WHISPER_THREADS=6").replace("OMP_NUM_THREADS=4", "OMP_NUM_THREADS=6") for x in c[:0]] + \
        ["6" if (i > 0 and c[i - 1] == "--cpus") else x.replace("WHISPER_THREADS=3", "WHISPER_THREADS=6").replace("OMP_NUM_THREADS=4", "OMP_NUM_THREADS=6")
         for i, x in enumerate(c)]


p.whisper_cmd = _cmd6
TOK_PER_SEC = 6.2     # est tokens per audio second (observed 4.4-6.3)


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(LOGS / "phase4.log", "a") as f:
        f.write(line + "\n")


def finished(vid: str, model: str) -> bool:
    """An 'interrupted' run (pause/STOP) is NOT finished: it is redone on resume."""
    r = p.result(vid, model)
    return bool(r) and r["status"] in ("ok", "failed", "timeout", "watchdog_tripped")


p.log = log
p.finished = finished


CAPTIONS_BLOCKED = [False]


def caption_acquire(vid: str) -> bool:
    if (FROZEN / f"{vid}.segments.json").exists():
        return True
    if CAPTIONS_BLOCKED[0]:
        log(f"captions skipped for {vid}: the subtitle endpoint is still rate-limited (429)")
        return False
    for attempt in (1, 2):
        cmd = ["docker", "run", "--rm", "--name", f"ytdigest-t382d-acq-{vid.replace('_', '').replace('-', '')}",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{W}:/w:ro", "-v", f"{TOOLS}:/tools:ro",
               "-v", f"{FROZEN}:/out", "--entrypoint", "python", "ytdigest-backend:latest", "-I", "/tools/acquire.py", vid, "/out"]
        r = p.sh(cmd)
        (LOGS / f"captions_{vid}.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr[-3000:])
        if r.returncode == 0 and (FROZEN / f"{vid}.segments.json").exists():
            log(f"CAPTIONS OK {vid}: {r.stdout.strip()[:200]}")
            record(vid, "captions")
            return True
        log(f"captions failed {vid} attempt {attempt}: {(r.stderr.strip().splitlines() or [''])[-1][:200]}")
        if attempt == 1:
            time.sleep(60)
    CAPTIONS_BLOCKED[0] = True
    return False


def record(vid: str, how: str) -> None:
    mp = R / "manifest_phase1.json"
    man = [m for m in (json.loads(mp.read_text()) if mp.exists() else []) if m["video_id"] != vid]
    sha = {f: hashlib.sha256((FROZEN / f"{vid}.{f}.json").read_bytes()).hexdigest() for f in ("meta", "segments")}
    mj = json.loads((FROZEN / f"{vid}.meta.json").read_text())
    man.append({"video_id": vid, "title": mj["meta"]["title"], "channel_id": mj["meta"]["channel_id"],
                "duration_sec": mj["meta"]["duration_sec"], "transcript_source": how, "sha256": sha,
                "frozen_at": time.strftime("%F %T"), "kind": mj["transcript"]["kind"]})
    mp.write_text(json.dumps(man, indent=1))


def whisper_for(vid: str) -> bool:
    fj = p.fetch(vid, True)
    if fj is None or "audio" not in fj:
        return False
    p.unload_models()
    t0 = time.time()
    out = p.run_whisper(vid, fj["audio"], p.NO_NEW)
    if out is None:
        return False
    WHISPER_RATIO[0] = (time.time() - t0) / fj["audio"]["duration_sec"] * 1.05
    log(f"whisper ratio now {WHISPER_RATIO[0]:.2f}")
    if p.freeze(vid, AUDIO / f"{vid}.fetch.json", out, "whisper_file") is None:
        return False
    record(vid, "whisper")
    return True


def load_new() -> None:
    have = {m["video_id"] for m in p.manifest()}
    new = [x for x, _, _ in VIDEOS if x not in have and (FROZEN / f"{x}.segments.json").exists()]
    if new:
        r = p.sh([str(TOOLS / "run.sh"), "python", str(TOOLS / "load.py"), str(R)] + new)
        log(f"loaded {new}: {r.stdout[-200:]} {r.stderr[-300:]}")


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    names = p.sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
    while "ytdigest-transcriber-1" in names and not p.stopped():
        log("live transcriber running: waiting")
        time.sleep(30)
        names = p.sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
    p.sh(["bash", str(TOP_DB)])
    if not (R / "host_facts_before.txt").exists():
        p.sh(["bash", str(TOOLS / "host_facts.sh"), str(R / "host_facts_before.txt")])
    pidf = R / "sidecar.pid"
    side = None
    if pidf.exists() and Path(f"/proc/{pidf.read_text().strip()}").exists():
        log("sidecar already running: kept")
    else:
        side = subprocess.Popen([sys.executable, str(TOOLS / "sidecar.py")])
        pidf.write_text(str(side.pid))
    fails = {m: 0 for m in p.MODELS}
    for vid_, _, _ in VIDEOS:  # resume: failures of finished runs count towards the stop rule
        for m_ in p.MODELS:
            r_ = p.result(vid_, m_)
            if r_ and r_["status"] != "ok" and finished(vid_, m_):
                fails[m_] += 1
    log(f"failure counts at (re)start: {fails}")
    try:
        # 1. captions for everything first (cheap; each failure goes to the Whisper path later)
        for vid, _, _ in VIDEOS:
            if p.stopped():
                break
            if not (FROZEN / f"{vid}.segments.json").exists():
                caption_acquire(vid)
                time.sleep(20)
        load_new()
        p.status("Captions step done; per-video loop running (see logs/phase4.log)")
        # 2. per video, shortest first: transcript (whisper if needed), then the pair
        for vid, chan, dur in VIDEOS:
            if p.stopped():
                break
            n = int(dur * TOK_PER_SEC)
            if (FROZEN / f"{vid}.segments.json").exists():
                n = next((m["est_tokens"] for m in p.manifest() if m["video_id"] == vid), n)
                pair = 2 * n * p.SEC_PER_TOKEN
                need_w = 0.0
            else:
                need_w = dur * WHISPER_RATIO[0]
                pair = 2 * n * p.SEC_PER_TOKEN
            todo_pair = [m for m in p.MODELS if not p.finished(vid, m)]
            if todo_pair and time.time() + need_w + pair * len(todo_pair) / 2 > p.NO_NEW:
                log(f"SKIP {vid} ({chan}, {dur}s): needs about {(need_w + pair) / 60:.0f} min, would end after {datetime.fromtimestamp(p.NO_NEW):%H:%M}")
                continue
            if need_w:
                log(f"WHISPER path for {vid} (no captions)")
                if not whisper_for(vid):
                    log(f"no transcript for {vid}")
                    continue
                load_new()
            elif vid not in {m['video_id'] for m in p.manifest()}:
                load_new()
            n = next(m["est_tokens"] for m in p.manifest() if m["video_id"] == vid)
            order = random.Random(f"r4-ctx16k:{vid}").sample(p.MODELS, 2)
            plan_f = R / "order_plan_r4.json"
            plan = json.loads(plan_f.read_text()) if plan_f.exists() else {}
            plan[vid] = {"seed": f"r4-ctx16k:{vid}", "order": order, "est_tokens": n, "channel": chan}
            plan_f.write_text(json.dumps(plan, indent=1))
            for model in order:
                if p.finished(vid, model) or p.stopped():
                    continue
                if fails[model] >= 3:
                    log(f"{model} skipped (3 failures)")
                    continue
                if time.time() + min(n * p.SEC_PER_TOKEN, p.CAP) > p.NO_NEW:
                    log(f"no time for {vid} {model}")
                    continue
                if p.plain_run(vid, model) != "ok":
                    fails[model] += 1
                p.status(f"Last: {vid} {model}. See logs/phase4.log, raw/")
    finally:
        log("driver finished (resumable: bash start.sh)")
        (R / "ORCH_DONE").write_text(time.strftime("%F %T"))
        p.sh(["docker", "rm", "-f", p.WHISPER])
        p.unload_models()
        p.sh(["bash", str(TOOLS / "host_facts.sh"), str(R / "host_facts_after.txt")])
        pid = (R / "sidecar.pid")
        if side:
            side.terminate()
        p.status("Driver finished or paused (see logs/phase4.log). Resume: bash start.sh")
    return 0


TOP_DB = p.TOP / "db.sh"
if __name__ == "__main__":
    sys.exit(main())
