"""Labelled extras after the main protocol: ctx16k (num_ctx 16384, both models, EfGtEDJyRvk then eEE3S-MvzEs),
then `rerun` of qwen on qovnfjVd8cs at 8192. Never mixed into main tables. Resumable (skips done results).
ctx16k: per-run cap 75 min, 3-failure rule not applied, watchdog stays at 1 GB.
"""
import json
import os
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path

B = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382")
RAW = B / "raw"
SOFT = datetime(2026, 11, 3, 9, 15).timestamp()
HARD = datetime(2026, 11, 3, 9, 20).timestamp()
MODELS = ["qwen3.5:4b", "gemma4:e4b"]
CAP16 = 75 * 60


def stem(label, vid, model):
    return f"{label}__{vid}__{model.replace(':', '_')}"


def done(label, vid, model):
    return (RAW / f"{stem(label, vid, model)}.result.json").exists()


def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(B / "logs" / "extra_runs.log", "a") as f:
        f.write(line + "\n")


def busy():
    return subprocess.run(["pgrep", "-f", "tools/run_one.py"], capture_output=True).returncode == 0


def run(label, vid, model, ctx, cap):
    log(f"START {label} {vid} {model} num_ctx={ctx} cap={cap}s")
    env = {**os.environ, "OLLAMA_NUM_CTX": str(ctx)}
    cmd = [str(B / "tools/run.sh"), "python", str(B / "tools/run_one.py"), "--model", model, "--video", vid,
           "--label", label, "--raw", str(RAW), "--deadline-epoch", str(HARD), "--soft-epoch", str(SOFT),
           "--cap-sec", str(cap), "--force"]
    with open(B / "logs" / f"{stem(label, vid, model)}.log", "w") as lf:
        subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
    p = RAW / f"{stem(label, vid, model)}.result.json"
    r = json.loads(p.read_text()) if p.exists() else None
    log(f"END   {label} {vid} {model} -> {r['status'] if r else 'no_result'}")
    return r["status"] if r else "no_result"


def main():
    while busy():  # let the running rep1 finish
        time.sleep(15)
    order_log = {}
    for vid, need_min in (("EfGtEDJyRvk", 0), ("eEE3S-MvzEs", 75)):
        order = random.Random(f"ctx16k:{vid}").sample(MODELS, 2)
        order_log[vid] = order
        (B / "order_plan_ctx16k.json").write_text(json.dumps(order_log))
        for m in order:
            if done("ctx16k", vid, m):
                continue
            if time.time() + max(need_min, 25) * 60 > SOFT and time.time() + 5 * 60 > SOFT:
                log("no time left")
                return
            if need_min and time.time() + need_min * 60 > SOFT:
                log(f"skip {vid}: fewer than {need_min} min before 09:15")
                break
            if run("ctx16k", vid, m, 16384, CAP16) in ("interrupted",):
                return
    if not done("rerun", "qovnfjVd8cs", "qwen3.5:4b") and time.time() + 25 * 60 < SOFT:
        run("rerun", "qovnfjVd8cs", "qwen3.5:4b", 8192, 45 * 60)
    log("extras DONE")


main()
