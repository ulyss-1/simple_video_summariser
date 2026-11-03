"""One (model, video) benchmark run through the real analyzer handler (issue #382 scratch driver).

Usage: run_one.py --model M --video ID [--label main|rep1..] [--force] [--deadline-epoch T] [--raw DIR]
Environment (see env.sh): DATABASE_URL, OLLAMA_HOST (URL), OLLAMA_NUM_CTX, ... exactly as the live analyzer.
Writes DIR/<label>__<video>__<model>.{calls.jsonl,samples.jsonl,result.json}; result.json is the
completeness marker and is written last (atomic rename).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import structlog

from adapters.summarize.ollama import (
    HttpResponse,
    OllamaSummarizer,
    estimate_tokens,
    urllib_transport,
)
from adapters.summarize.prompt_loader import load_prompt_set
from common.chunking import chunk_strategy
from common.config import get_settings
from common.db import connect
from common.models import ChunkAnalysis
from common.repo.analyses import analysis_run_for
from common.worker import JobContext
from services.analyzer.handler import make_analyze_handler
from services.eval.runner import apply_overrides, evaluate_video, synthetic_job

MIN_AVAILABLE_KB = 1024 * 1024  # 1 GB watchdog
RUN_CAP_SEC = 45 * 60
KIND = {"derive_roster": "roster", "analyze_chunk": "chunk", "reduce": "reduce"}


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            out[k] = int(v.split()[0])
    return out


def runner_rss_kb() -> int:
    try:
        ps = subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return -1
    best = 0
    for line in ps.splitlines():
        if "llama-server" in line or ("ollama" in line and "runner" in line):
            best = max(best, int(line.split()[0]))
    return best


def api_get(host: str, path: str) -> Any:
    with urllib.request.urlopen(host.rstrip("/") + path, timeout=10) as r:
        return json.loads(r.read())


def unload_all(host: str, cli_host: str) -> dict[str, Any]:
    """Unload every resident model with the ollama CLI; return /api/ps afterwards."""
    ps: Any = api_get(host, "/api/ps")
    for m in ps.get("models", []):
        subprocess.run(
            ["ollama", "stop", m["name"]],
            env={**os.environ, "OLLAMA_HOST": cli_host},
            capture_output=True,
            timeout=60,
        )
    end = time.time() + 60
    while time.time() < end:
        ps = api_get(host, "/api/ps")
        if not ps.get("models"):
            break
        time.sleep(1)
    return ps  # type: ignore[no-any-return]


class Rec(OllamaSummarizer):
    """Notes the call kind for the transport wrapper and the per-chunk parser counters."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.cur: tuple[str, bool] = ("?", False)
        self.chunk_stats: list[dict[str, Any]] = []

    def _chat(self, method: str, messages: Any, format: Any, is_repair: bool) -> str:
        self.cur = (method, is_repair)
        return super()._chat(method, messages, format, is_repair)

    def analyze_chunk(self, chunk: Any, roster: Any, meta: Any) -> ChunkAnalysis:
        result = super().analyze_chunk(chunk, roster, meta)
        self.chunk_stats.append(
            {
                "seq": chunk.seq,
                "items_dropped": result.items_dropped,
                "start_sec_clamped": result.start_sec_clamped,
                "speaker_coercions": result.speaker_coercions,
                "topics": len(result.topics),
                "claims": len(result.claims),
                "quotes": len(result.quotes),
            }
        )
        return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--label", default="main")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--deadline-epoch", type=float, default=None)
    ap.add_argument("--cap-sec", type=float, default=RUN_CAP_SEC)
    ap.add_argument("--soft-epoch", type=float, default=None)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--cli-host", default="172.17.0.1:11434")
    ap.add_argument("--no-unload", action="store_true", help="dry-run only")
    a = ap.parse_args()

    raw = Path(a.raw)
    raw.mkdir(parents=True, exist_ok=True)
    stem = f"{a.label}__{a.video}__{a.model.replace(':', '_')}"
    calls_path, samples_path, result_path = (raw / f"{stem}.{x}" for x in ("calls.jsonl", "samples.jsonl", "result.json"))
    for p in (calls_path, samples_path, result_path):
        p.unlink(missing_ok=True)

    base = get_settings()
    settings = apply_overrides(base, summarizer="ollama", model=a.model, prompt_version="v1")
    host = settings.OLLAMA_HOST

    state: dict[str, Any] = {"status": "running", "stop": False, "min_avail_kb": 10**12, "peak_runner_kb": 0,
                             "peak_used_kb": 0, "peak_swap_kb": 0, "n_samples": 0, "reason": None}
    lock = threading.Lock()
    t_start = time.time()
    mono0 = time.monotonic()
    summ_ref: dict[str, Rec] = {}

    pre: dict[str, Any] = {"started_epoch": t_start, "loadavg": open("/proc/loadavg").read().strip(),
                           "meminfo_kb": {k: v for k, v in meminfo().items() if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}}
    try:
        pre["docker_stats"] = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                                             capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
    except Exception as e:  # noqa: BLE001
        pre["docker_stats"] = f"error {type(e).__name__}"
    if not a.no_unload:
        pre["ps_after_unload"] = unload_all(host, a.cli_host)
    else:
        pre["ps_after_unload"] = api_get(host, "/api/ps")
    if pre["ps_after_unload"].get("models") and not a.no_unload:
        print("model still resident, abort", file=sys.stderr)
        return 5

    def finish(status: str, reason: str | None, outcome: str | None, hard: bool) -> None:
        """Write result.json (atomic) and, when hard, unload and exit the process."""
        with lock:
            if state.get("finished"):
                return
            state["finished"] = True
        calls = []
        if calls_path.exists():
            calls = [json.loads(line) for line in calls_path.read_text().splitlines() if line.strip()]
        s = summ_ref.get("s")
        doc: dict[str, Any] = {
            "model": a.model, "video_id": a.video, "label": a.label, "status": status, "reason": reason,
            "outcome_text": outcome, "wall_sec": time.monotonic() - mono0, "started_epoch": t_start,
            "ended_epoch": time.time(), "pre": pre, "n_calls": len(calls),
            "mem": {k: state[k] for k in ("min_avail_kb", "peak_used_kb", "peak_swap_kb", "peak_runner_kb", "n_samples")},
            "chunk_stats": s.chunk_stats if s else [],
            "settings": {k: getattr(settings, k) for k in ("OLLAMA_MODEL", "OLLAMA_HOST", "OLLAMA_NUM_CTX",
                         "OLLAMA_OUTPUT_RESERVE_TOKENS", "OLLAMA_TIMEOUT_SEC", "CHUNK_SEC", "OVERLAP_SEC", "PROMPT_VERSION")},
            "analysis": state.get("analysis"),
        }
        tmp = result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        os.replace(tmp, result_path)
        if hard:
            try:
                unload_all(host, a.cli_host)
            finally:
                os._exit(4)

    def watcher() -> None:
        i = 0
        with open(samples_path, "a") as sf:
            while not state["stop"]:
                m = meminfo()
                avail = m["MemAvailable"]
                with lock:
                    state["min_avail_kb"] = min(state["min_avail_kb"], avail)
                if avail < MIN_AVAILABLE_KB:
                    finish("watchdog_tripped", f"MemAvailable {avail} kB at {time.strftime('%H:%M:%S')}", None, True)
                now = time.time()
                limit = min(t_start + a.cap_sec, a.deadline_epoch or 1e18)
                if now > limit:
                    cap = now > t_start + a.cap_sec - 1
                    finish("timeout" if cap else "interrupted",
                           f"{a.cap_sec:.0f} s run cap" if cap else "owner deadline", None, True)
                if i % 2 == 0:
                    rss = runner_rss_kb()
                    used = m["MemTotal"] - avail
                    swap = m["SwapTotal"] - m["SwapFree"]
                    with lock:
                        state["peak_used_kb"] = max(state["peak_used_kb"], used)
                        state["peak_swap_kb"] = max(state["peak_swap_kb"], swap)
                        state["peak_runner_kb"] = max(state["peak_runner_kb"], rss)
                        state["n_samples"] += 1
                    sf.write(json.dumps({"t": round(now - t_start, 1), "avail_kb": avail, "used_kb": used,
                                         "swap_kb": swap, "runner_rss_kb": rss,
                                         "load1": open("/proc/loadavg").read().split()[0]}) + "\n")
                    sf.flush()
                i += 1
                time.sleep(1)

    threading.Thread(target=watcher, daemon=True).start()

    counter = {"n": 0}

    def transport(url: str, body: bytes, timeout: float) -> HttpResponse:
        s = summ_ref["s"]
        method, is_repair = s.cur
        if a.soft_epoch and time.time() > a.soft_epoch:
            state["soft_hit"] = True  # 09:15 rule: no new call starts after the soft deadline
            raise OSError("soft deadline reached, no new call started")
        counter["n"] += 1
        rec: dict[str, Any] = {
            "video_id": a.video, "model": a.model, "label": a.label, "call_index": counter["n"],
            "kind": "repair" if is_repair else KIND.get(method, method), "method": method,
            "request_chars": len(body.decode("utf-8")), "request_est_tokens": estimate_tokens(body.decode("utf-8")),
            "t_start_epoch": time.time(),
        }
        t0 = time.monotonic()
        try:
            resp = urllib_transport(url, body, timeout)
        except BaseException as exc:
            rec.update(error=type(exc).__name__, wall_sec=time.monotonic() - t0, status=None)
            with open(calls_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            raise
        rec["wall_sec"] = time.monotonic() - t0
        rec["status"] = resp.status
        try:
            doc = json.loads(resp.body)
            for k in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
                      "load_duration", "total_duration", "done_reason"):
                rec[k] = doc.get(k)
            rec["response_keys"] = sorted(doc.keys())
            msg = doc.get("message") or {}
            rec["response_chars"] = len(msg.get("content") or "")
            if doc.get("error"):
                rec["error_field"] = str(doc["error"])[:200]
        except Exception:  # noqa: BLE001
            rec["error"] = "undecodable"
        with open(calls_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return resp

    prompts = load_prompt_set(settings.PROMPT_VERSION)
    summarizer = Rec(
        host=settings.OLLAMA_HOST, model=settings.OLLAMA_MODEL, num_ctx=settings.OLLAMA_NUM_CTX,
        output_reserve_tokens=settings.OLLAMA_OUTPUT_RESERVE_TOKENS, timeout_sec=settings.OLLAMA_TIMEOUT_SEC,
        prompts=prompts, transport=transport,
    )
    summ_ref["s"] = summarizer
    handler = make_analyze_handler(connect=connect, summarizer=summarizer, settings=settings)

    conn = connect()
    mono0 = time.monotonic()  # wall time excludes the pre-run unload and docker stats
    if a.force:
        # repeat run: call the same handler with a forced job (evaluate_video would skip a done pair)
        from services.eval.runner import VideoOutcome, describe_failure

        try:
            handler(
                dataclasses.replace(synthetic_job(a.video, settings, summarizer), payload={"force": True}),
                JobContext(structlog.get_logger("eval")),
            )
            outcome = VideoOutcome("analyzed", "analyzed (forced repeat)")
        except Exception as exc:  # noqa: BLE001
            outcome = VideoOutcome("failed", f"failed: {describe_failure(exc)}")
    else:
        outcome = evaluate_video(conn, handler, settings, summarizer, a.video, dry_run=False)
    status = "ok" if outcome.status in ("analyzed",) else outcome.status
    if outcome.status == "skipped":
        status = "skipped"
    if status == "failed" and state.get("soft_hit"):
        status = "interrupted"
    if status == "ok":
        run = analysis_run_for(conn, a.video, settings.OLLAMA_MODEL, settings.PROMPT_VERSION)
        conn.rollback()
        if run is not None:
            an = dataclasses.asdict(run.analysis)
            an["transcript_source"] = run.transcript_source
            state["analysis"] = an
            if a.force and run.analysis.id is not None:
                conn.execute("DELETE FROM analyses WHERE id = %s", (run.analysis.id,))
                conn.commit()
    conn.close()
    state["stop"] = True
    time.sleep(0.1)
    finish(status, None, outcome.text, False)
    print(f"{stem}: {status}: {outcome.text}", flush=True)
    return 0 if status in ("ok", "skipped") else 1


if __name__ == "__main__":
    structlog.configure()
    sys.exit(main())
