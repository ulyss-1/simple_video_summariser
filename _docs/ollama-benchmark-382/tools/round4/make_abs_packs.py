"""Single-summary (absolute 1-5) blind packs for round 4: out/packs/abs1, abs2; mapping in out/private/ (not in the packs)."""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_packs as mp  # noqa: E402

B, RAW = mp.B, mp.RAW
man = {m["video_id"]: m for m in json.loads((B / "manifest.json").read_text())}
mech = json.loads((B / "out/mech.json").read_text())
items = []
for f in sorted(RAW.glob("r4-ctx16k__*.result.json")):
    r = json.loads(f.read_text())
    if r["status"] == "ok" and r.get("analysis"):
        items.append(r)
note = ("ABSOLUTE SCORING (no pair): score this single summary against the transcript with the rubric, each criterion 1-5 "
        "(5 = excellent, 1 = unusable). There is no second summary to compare with; do not guess where it came from.\n\n")
for p_ in (1, 2):
    rng = random.Random(3820 + 40 + p_)
    order = items[:]
    rng.shuffle(order)
    d = B / "out/packs" / f"abs{p_}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "RUBRIC.md").write_text((B / "RUBRIC.md").read_text())
    (d / "README.md").write_text(note + (B / "SCORER_README.md").read_text())
    mapping = {"seed": 3860 + p_, "units": {}}
    for i, r in enumerate(order, start=1):
        vid, model = r["video_id"], r["model"]
        tag = f"A{i:02d}"
        mapping["units"][tag] = {"video_id": vid, "model": model, "label": r["label"]}
        (d / f"{tag}.transcript.txt").write_text(mp.transcript_lines(vid))
        mrow = next(x for x in mech if x["video_id"] == vid and x["model"] == model and x["label"] == r["label"])
        body = [f"# {tag}  (video title: {man[vid]['title']})\n", "## Summary\n",
                f"FACTS (mechanical): TLDR sentences {mrow['tldr_sentences']} (rule: 4 to 6); paragraphs {mrow['tldr_paragraphs']}; "
                f"markdown present {'yes' if mrow['tldr_markdown'] else 'no'}; preamble present {'yes' if mrow['tldr_preamble'] else 'no'}; "
                f"JSON repairs needed {mrow['calls_needing_repair']}; speakers outside roster {mrow['speakers_coerced']}; "
                f"quotes verbatim in transcript {mrow['quotes_verbatim']} of {mrow['quotes_total']}\n", mp.render(r["analysis"])]
        (d / f"{tag}.summary.md").write_text("\n".join(body))
    (B / "out/private").mkdir(parents=True, exist_ok=True)
    (B / "out/private" / f"mapping_abs{p_}.json").write_text(json.dumps(mapping, indent=1))
    print("pack", d, len(order), "units")
