"""Recount TLDR sentences with the abbreviation-aware counter for every run of round 1 and round 2, and write
corrected copies of the packs (originals untouched, labels and mappings unchanged).
Outputs: round2/out/sentence_recount.json, round2/out/packs_corrected/{pass1,pass2,repeats}/ (round-1 packs + repeat pack),
round2/out/mech_corrected_r1.json, round2/out/mech_corrected_r2.json."""
import json
import re
import shutil
import sys
from pathlib import Path

R = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2")
B1 = R.parent
sys.path.insert(0, str(R / "tools"))
from sentences import count_sentences  # noqa: E402

rows = []
for root, name in ((B1, "r1"), (R, "r2")):
    fixed = json.loads((root / "out/mech.json").read_text())
    for x in fixed:
        if "tldr_sentences" not in x:
            continue
        p = root / "raw" / f"{x['label']}__{x['video_id']}__{x['model'].replace(':', '_')}.result.json"
        tldr = json.loads(p.read_text())["analysis"]["tldr"]
        new = count_sentences(tldr)
        rows.append({"round": name, "label": x["label"], "video": x["video_id"], "model": x["model"], "old": x["tldr_sentences"], "new": new,
                     "rule_ok_old": 4 <= x["tldr_sentences"] <= 6, "rule_ok_new": 4 <= new <= 6})
        x["tldr_sentences_old"] = x["tldr_sentences"]
        x["tldr_sentences"] = new
    (R / f"out/mech_corrected_{name}.json").write_text(json.dumps(fixed, indent=1))
(R / "out/sentence_recount.json").write_text(json.dumps(rows, indent=1))
for r in rows:
    flag = "  <-- changed" if r["old"] != r["new"] else ""
    print(r["round"], r["label"], r["video"], r["model"], r["old"], "->", r["new"], flag)

# corrected packs: rewrite only the 'TLDR sentences N' number inside each summary block
out = R / "out/packs_corrected"
for src in (B1 / "out/packs/pass1", B1 / "out/packs/pass2", R / "out/packs/repeats"):
    dst = out / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    changes = []
    for f in sorted(dst.glob("*summaries.md")):
        txt = f.read_text()
        blocks = re.split(r"(?m)^(?=## Summary )", txt)
        res = []
        for b in blocks:
            m = re.search(r"TLDR sentences (\d+) \(rule", b)
            t = re.search(r"TLDR:\n(.*?)\n\n", b, re.S)
            if m and t:
                new = count_sentences(t.group(1))
                if new != int(m.group(1)):
                    changes.append((f.name, b.split("\n")[0], int(m.group(1)), new))
                b = b.replace(m.group(0), f"TLDR sentences {new} (rule", 1)
            res.append(b)
        f.write_text("".join(res))
    (dst / "README.md").write_text((dst / "README.md").read_text() + "\n(Corrected copy: the FACTS line's 'TLDR sentences' count is now abbreviation-aware; 'Dr.' no longer counts as a sentence end. Nothing else changed.)\n")
    print(src.name, "changed FACTS:", changes)
