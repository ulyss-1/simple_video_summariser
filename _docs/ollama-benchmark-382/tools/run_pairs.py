"""Phase 2 orchestrator: pairs by video (shortest -> longest), then repeats of video 1. Resumable.

Does nothing until GO exists. Starts a pair only if both runs are expected to end before SOFT (09:15).
Stop rules: 3 failures of a model => skip that model; a watchdog trip, an interruption, or the soft
deadline stops everything. All state is on disk (raw/*.result.json); rerun the script to resume.
"""
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

B = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382")
RAW = B / "raw"
MODELS = ["qwen3.5:4b", "gemma4:e4b"]
SEED = 382
SOFT = datetime(2026, 11, 3, 9, 15).timestamp()
HARD = datetime(2026, 11, 3, 9, 20).timestamp()
manifest = sorted(json.loads((B / "manifest.json").read_text()), key=lambda m: m["est_tokens"])
FIRST = "qovnfjVd8cs"  # video 1: repeats
REPEATS = 3
PRIOR_SEC_PER_TOKEN = {"qwen3.5:4b": 0.12, "gemma4:e4b": 0.135}
MARGIN = 1.25


def stem(label: str, vid: str, model: str) -> str:
    return f"{label}__{vid}__{model.replace(':', '_')}"


def result(label: str, vid: str, model: str):
    p = RAW / f"{stem(label, vid, model)}.result.json"
    return json.loads(p.read_text()) if p.exists() else None


def est_sec(model: str, est_tokens: int) -> float:
    ratios = [r["wall_sec"] / m["est_tokens"] for m in manifest for r in [result("main", m["video_id"], model)]
              if r and r["status"] == "ok"]
    per_tok = max([PRIOR_SEC_PER_TOKEN[model] * 0.5] + [sum(ratios) / len(ratios)] if ratios else [PRIOR_SEC_PER_TOKEN[model]])
    return (100 + per_tok * est_tokens) * MARGIN


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(B / "logs" / "run_pairs.log", "a") as f:
        f.write(line + "\n")


def run(label: str, vid: str, model: str, force: bool) -> str:
    log(f"START {label} {vid} {model}")
    cmd = [str(B / "tools/run.sh"), "python", str(B / "tools/run_one.py"), "--model", model, "--video", vid,
           "--label", label, "--raw", str(RAW), "--deadline-epoch", str(HARD), "--soft-epoch", str(SOFT)]
    if force:
        cmd.append("--force")
    with open(B / "logs" / f"{stem(label, vid, model)}.log", "w") as lf:
        subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    r = result(label, vid, model)
    status = r["status"] if r else "no_result"
    log(f"END   {label} {vid} {model} -> {status} ({r['wall_sec']:.0f}s)" if r else f"END {label} {vid} {model} -> no_result")
    return status


def sync_manifest() -> None:
    """Load newly frozen transcripts into the throwaway DB (between timed runs only)."""
    ids = sorted(f.name.split(".")[0] for f in (B / "frozen").glob("*.segments.json"))
    have = {m["video_id"] for m in json.loads((B / "manifest.json").read_text())}
    new = [i for i in ids if i not in have and (B / "frozen" / f"{i}.meta.json").exists()]
    if new:
        subprocess.run([str(B / "tools/run.sh"), "python", str(B / "tools/load.py"), str(B), *new],
                       stdout=open(B / "logs" / "load.log", "a"), stderr=subprocess.STDOUT)
        log(f"loaded new transcripts: {new}")


def acquisition_done() -> bool:
    return "ALLDONE" in (B / "logs" / "acquire.log").read_text()


def plan_for(vid: str):
    return random.Random(f"{SEED}:{vid}").sample(MODELS, 2)


def main() -> int:
    while not (B / "GO").exists():
        time.sleep(30)
    log("GO seen")
    failures = {m: 0 for m in MODELS}
    stopped = None

    def attempt(label, vid, model, force):
        nonlocal stopped
        r = result(label, vid, model)
        if r and r["status"] in ("ok", "failed", "timeout"):
            return True
        if failures[model] >= 3:
            log(f"SKIP {label} {vid} {model}: 3 failures stop rule")
            return True
        status = run(label, vid, model, force)
        if status in ("failed", "timeout"):
            failures[model] += 1
        if status in ("watchdog_tripped", "interrupted", "no_result"):
            stopped = f"{status} at {label} {vid} {model}"
            return False
        return True

    def pair(label, vid, tokens, order, force):
        """Run a pair if it fits before 09:15; False means stop everything."""
        todo = [m for m in order if not result(label, vid, m)]
        need = sum(est_sec(m, tokens) for m in todo if failures[m] < 3)
        if need and time.time() + need > SOFT:
            log(f"STOP: {label} {vid} needs ~{need:.0f}s and would pass 09:15")
            return False
        for m in order:
            if not attempt(label, vid, m, force):
                return False
        return True

    plan_log = {"seed": SEED, "main": {}, "repeats": {}}
    while not stopped:
        sync_manifest()
        manifest = sorted(json.loads((B / "manifest.json").read_text()), key=lambda m: m["est_tokens"])
        pending = [m for m in manifest if any(not result("main", m["video_id"], x) for x in MODELS)]
        if pending:
            m = pending[0]
            order = plan_for(m["video_id"])
            plan_log["main"][m["video_id"]] = order
            (B / "order_plan.json").write_text(json.dumps(plan_log, indent=1))
            if not pair("main", m["video_id"], m["est_tokens"], order, False):
                stopped = stopped or "soft deadline (pair would not finish)"
            continue
        # no main work pending: repeats of video 1 meanwhile, else wait for the fetcher
        reps = [(f"rep{i}", MODELS if i % 2 else MODELS[::-1]) for i in range(1, REPEATS + 1)]
        todo = [(l, o) for l, o in reps if any(not result(l, FIRST, x) for x in o)]
        t1 = next((x["est_tokens"] for x in manifest if x["video_id"] == FIRST), 3914)
        if todo:
            l, o = todo[0]
            plan_log["repeats"][l] = o
            (B / "order_plan.json").write_text(json.dumps(plan_log, indent=1))
            if not pair(l, FIRST, t1, o, True):
                stopped = stopped or "soft deadline (repeat would not finish)"
            continue
        if acquisition_done():
            break
        time.sleep(30)
        if time.time() > SOFT:
            stopped = "soft deadline"
    log(f"DONE stopped={stopped} failures={failures}")
    (B / "run_pairs.done").write_text(json.dumps({"stopped": stopped, "failures": failures}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
