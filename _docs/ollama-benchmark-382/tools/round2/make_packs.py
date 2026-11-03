"""Blind scoring packs: two passes, X/Y randomised per video and pass, mapping kept apart.

make_packs.py [--passes 2] [--seed 3820]  -> out/packs/pass<N>/..., out/private/mapping_pass<N>.json
Packs hold the rubric, the transcript text and two summaries labelled X / Y. No model name, timing,
token count or mechanical-check number appears in a pack (the mechanical facts for instruction
following are given as 'facts' that carry no model information).
"""
import argparse
import json
import random
import re
from pathlib import Path

B = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2")
RAW = B / "raw"
MODELS = ["qwen3.5:4b", "gemma4:e4b"]
_redact = re.compile(r"qwen[\w.:-]*|gemma[\w.:-]*|ollama", re.I)


def transcript_lines(vid: str) -> str:
    segs = json.loads((B / "frozen" / f"{vid}.segments.json").read_text())
    lines, cur, t0 = [], [], None
    for s in segs:
        if t0 is None:
            t0 = s["start"]
        cur.append(s["text"].strip())
        if s["end"] - t0 >= 30:
            lines.append(f"[{int(t0) // 60:02d}:{int(t0) % 60:02d}] " + " ".join(cur))
            cur, t0 = [], None
    if cur:
        lines.append(f"[{int(t0) // 60:02d}:{int(t0) % 60:02d}] " + " ".join(cur))
    return "\n".join(lines) + "\n"


def render(a: dict) -> str:
    out = ["TLDR:", a["tldr"].strip(), "", "TOPICS:"]
    for t in a["topics"]:
        out.append(f"- {t['title']}" + (f": {t['summary']}" if t.get("summary") else ""))
    out += ["", "CLAIMS:"]
    for c in a["claims"]:
        out.append(f"- [{c.get('speaker', 'unknown')}] {c['text']}")
    out += ["", "QUOTES (checked mechanically, do not score):"]
    for q in a["quotes"]:
        out.append(f"- [{q.get('speaker', 'unknown')}] {q['text']}")
    return _redact.sub("[redacted]", "\n".join(out)) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--seed", type=int, default=3820)
    args = ap.parse_args()
    manifest = {m["video_id"]: m for m in json.loads((B / "manifest.json").read_text())}
    rubric = (B / "RUBRIC.md").read_text()
    mech = json.loads((B / "out" / "mech.json").read_text())  # run mech.py first

    def has_output(label, vid, model):
        f = RAW / f"{label}__{vid}__{model.replace(':', '_')}.result.json"
        return f.exists() and json.loads(f.read_text()).get("analysis") is not None

    units = []  # (video, setting): main for every video where any model produced output, ctx16k likewise
    for label in ("r2-ctx16k", "r2-ctx16k-conc", "r2-ctx16k-rep1", "r2-ctx16k-rep2", "r2-ctx16k-rep3"):
        for vid in sorted(manifest, key=lambda v: manifest[v]["est_tokens"]):
            if any(has_output(label, vid, m) for m in MODELS):
                units.append((label, vid))
    for p in range(1, args.passes + 1):
        rng = random.Random(args.seed + p)
        d = B / "out" / "packs" / f"pass{p}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "RUBRIC.md").write_text(rubric)
        (d / "README.md").write_text((B / "SCORER_README.md").read_text())
        mapping = {"seed": args.seed + p, "units": {}}
        for i, (label, vid) in enumerate(units, start=1):
            m = manifest[vid]
            tag = f"U{i:02d}"
            xy = MODELS[:]
            rng.shuffle(xy)
            mapping["units"][tag] = {"video_id": vid, "setting": label, "X": xy[0], "Y": xy[1]}
            (d / f"{tag}.transcript.txt").write_text(transcript_lines(vid))
            body = [f"# {tag}  (video title: {m['title']})\n"]
            for lab, model in zip("XY", xy, strict=True):
                f = RAW / f"{label}__{vid}__{model.replace(':', '_')}.result.json"
                r = json.loads(f.read_text()) if f.exists() else None
                body.append(f"## Summary {lab}\n")
                if r and r.get("analysis"):
                    mrow = next((x for x in mech if x["video_id"] == vid and x["model"] == model
                                 and x["label"] == label), None)
                    if mrow:
                        body.append(
                            f"FACTS (mechanical): TLDR sentences {mrow['tldr_sentences']} (rule: 4 to 6); paragraphs "
                            f"{mrow['tldr_paragraphs']}; markdown present {'yes' if mrow['tldr_markdown'] else 'no'}; "
                            f"preamble present {'yes' if mrow['tldr_preamble'] else 'no'}; JSON repairs needed "
                            f"{mrow['calls_needing_repair']}; speakers outside roster {mrow['speakers_coerced']}; "
                            f"quotes verbatim in transcript {mrow['quotes_verbatim']} of {mrow['quotes_total']}\n"
                        )
                    body.append(render(r["analysis"]))
                else:
                    body.append("[NO OUTPUT: the summarising job failed or did not finish for this video]\n")
            (d / f"{tag}.summaries.md").write_text("\n".join(body))
        pdir = B / "out" / "private"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"mapping_pass{p}.json").write_text(json.dumps(mapping, indent=1))
        print("pack", d, len(units), "units")


if __name__ == "__main__":
    main()
