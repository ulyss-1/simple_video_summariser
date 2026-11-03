"""Mechanical checks per (model, video) main run, from the driver's checkpoints (no LLM).

mech.py -> writes out/mech.json (list of per-run dicts) and prints a per-model summary.
"""
import json
import re
import sys
from pathlib import Path

B = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382")
RAW = B / "raw"
MODELS = ["qwen3.5:4b", "gemma4:e4b"]

_ws = re.compile(r"\s+")
from sentences import count_sentences  # abbreviation-aware (the old regex counted 'Dr.' as a sentence end)
_md = re.compile(r"(^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s))|\*\*|__|`", re.M)
_pre = re.compile(r"^\s*(here(?:'s| is| are)|sure|certainly|of course|okay|ok[,.]|tl;?dr\s*[:\-]|summary\s*[:\-]|in summary)", re.I)


def norm(s: str) -> str:
    return _ws.sub(" ", s).strip()


def transcript_text(vid: str) -> str:
    segs = json.loads((B / "frozen" / f"{vid}.segments.json").read_text())
    return norm(" ".join(s["text"] for s in segs))


def mech(r: dict) -> dict:
    out = {"video_id": r["video_id"], "model": r["model"], "label": r["label"], "status": r["status"],
           "outcome": r.get("outcome_text")}
    a = r.get("analysis")
    cs = r.get("chunk_stats") or []
    out["items_dropped"] = sum(c.get("items_dropped", 0) for c in cs)
    out["start_sec_clamped"] = sum(c.get("start_sec_clamped", 0) for c in cs)
    calls = [json.loads(line) for line in (RAW / f"{r['label']}__{r['video_id']}__{r['model'].replace(':', '_')}.calls.jsonl").read_text().splitlines() if line.strip()] if (RAW / f"{r['label']}__{r['video_id']}__{r['model'].replace(':', '_')}.calls.jsonl").exists() else []
    out["calls_by_kind"] = {k: sum(1 for c in calls if c["kind"] == k) for k in ("roster", "chunk", "reduce", "repair")}
    out["calls_needing_repair"] = out["calls_by_kind"]["repair"]
    out["truncated_calls"] = sum(1 for c in calls if c.get("done_reason") == "length")
    out["adapter_rejections"] = out["calls_by_kind"]["repair"]  # each repair follows one parser rejection
    if not a:
        return out
    tldr = a["tldr"]
    out["tldr_chars"] = len(tldr)
    out["tldr_sentences"] = count_sentences(tldr)
    out["tldr_paragraphs"] = len([p for p in re.split(r"\n\s*\n", tldr.strip()) if p.strip()])
    out["tldr_markdown"] = bool(_md.search(tldr))
    out["tldr_preamble"] = bool(_pre.search(tldr))
    tx = transcript_text(r["video_id"])
    quotes = [norm(q["text"]) for q in a["quotes"]]
    verb = [q for q in quotes if q and q in tx]
    out["quotes_total"] = len(quotes)
    out["quotes_verbatim"] = len(verb)
    out["quote_verbatim_rate"] = (len(verb) / len(quotes)) if quotes else None
    out["speakers_coerced"] = a.get("speakers_coerced")
    out["topics"] = len(a["topics"])
    out["claims"] = len(a["claims"])
    out["quotes"] = len(a["quotes"])
    out["roster_size"] = len((a.get("speaker_roster") or {}).get("speakers", []))
    return out


def main() -> None:
    rows = []
    for p in sorted(RAW.glob("*.result.json")):
        rows.append(mech(json.loads(p.read_text())))
    (B / "out").mkdir(exist_ok=True)
    (B / "out" / "mech.json").write_text(json.dumps(rows, indent=1))
    for m in MODELS:
        rs = [r for r in rows if r["model"] == m and r["label"] == "main"]
        ok = [r for r in rs if "tldr_sentences" in r]
        print(m, "runs", len(rs), "with output", len(ok))
        for r in ok:
            print("  ", r["video_id"], {k: r[k] for k in ("tldr_sentences", "tldr_paragraphs", "tldr_markdown",
                  "tldr_preamble", "quote_verbatim_rate", "speakers_coerced", "items_dropped", "calls_needing_repair")})


if __name__ == "__main__":
    sys.exit(main())
