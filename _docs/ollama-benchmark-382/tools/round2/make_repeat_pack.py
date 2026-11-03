"""Blind pack for run-to-run quality variance: 7 summaries of bIvyxp-FAGU (5 successful round-2 repeats + 2 round-1 main runs).
Neutral labels S1..S7 (shuffled), mapping private (out/private/mapping_repeats.json). No scoring here."""
import json
import random
import sys
from pathlib import Path

R = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2")
B1 = R.parent
sys.path.insert(0, str(R / "tools"))
import make_packs as mp  # noqa: E402

VID = "bIvyxp-FAGU"
mech2 = {(x["label"], x["model"]): x for x in json.loads((R / "out/mech.json").read_text()) if x["video_id"] == VID}
mech1 = {(x["label"], x["model"]): x for x in json.loads((B1 / "out/mech.json").read_text()) if x["video_id"] == VID}
items = []
for root, mech in ((R, mech2), (B1, mech1)):
    for p in sorted((root / "raw").glob(f"*__{VID}__*.result.json")):
        r = json.loads(p.read_text())
        if r["status"] == "ok" and r.get("analysis") and (r["label"].startswith("r2-ctx16k-rep") or (root == B1 and r["label"] == "main")):
            items.append((r, mech[(r["label"], r["model"])], "round2" if root == R else "round1"))
assert len(items) == 7, len(items)
random.Random(38207).shuffle(items)
d = R / "out/packs/repeats"
d.mkdir(parents=True, exist_ok=True)
(d / "RUBRIC.md").write_text((R / "RUBRIC.md").read_text())
(d / "README.md").write_text(
    "# Scorer instructions (blind), run-to-run variance pack\n\n"
    "Seven summaries S1..S7 of ONE video, from unnamed systems and runs. Score each independently with RUBRIC.md "
    "(faithfulness, instruction following, coverage; integers 1 to 5). Do not guess where a summary comes from.\n\n"
    "- `transcript.txt`: the only source of truth (timestamps `[mm:ss]`).\n"
    "- `summaries.md`: S1..S7, each with a `FACTS (mechanical)` line (use it for instruction following), then TLDR, topics, claims, quotes (quotes are checked mechanically, do not score them).\n\n"
    "Procedure: read the transcript, write its 3 to 8 main points BEFORE reading any summary; then score S1..S7, listing unsupported statements for faithfulness. "
    "Return JSON only: {\"main_points\": [...], \"S1\": {\"faithfulness\": 4, \"instruction_following\": 5, \"coverage\": 3, \"unsupported\": [...]}, ...}\n")
(d / "transcript.txt").write_text(mp.transcript_lines(VID))
mapping, body = {}, [f"# Seven summaries of the same video ({VID})\n"]
for i, (r, m, rnd) in enumerate(items, start=1):
    tag = f"S{i}"
    mapping[tag] = {"model": r["model"], "label": r["label"], "round": rnd, "num_ctx": r["settings"]["OLLAMA_NUM_CTX"]}
    body.append(f"## Summary {tag}\n")
    body.append(
        f"FACTS (mechanical): TLDR sentences {m['tldr_sentences']} (rule: 4 to 6); paragraphs {m['tldr_paragraphs']}; "
        f"markdown present {'yes' if m['tldr_markdown'] else 'no'}; preamble present {'yes' if m['tldr_preamble'] else 'no'}; "
        f"JSON repairs needed {m['calls_needing_repair']}; speakers outside roster {m['speakers_coerced']}; "
        f"quotes verbatim in transcript {m['quotes_verbatim']} of {m['quotes_total']}\n")
    body.append(mp.render(r["analysis"]))
(d / "summaries.md").write_text("\n".join(body))
(R / "out/private").mkdir(parents=True, exist_ok=True)
(R / "out/private/mapping_repeats.json").write_text(json.dumps(mapping, indent=1))
print(d, list(mapping))
