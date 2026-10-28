#!/usr/bin/env python3
"""SessionStart hook: put the ai_docs index and its freshness into context at turn 0.

The index is what makes the docs discoverable without an agent having to guess
that ai_docs/ exists. The freshness column is the part CLAUDE.md cannot carry,
because it is computed from git at session start rather than written down.

Fails silent by construction.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    import ai_docs_map as m

    root = m.repo_root()
    if root is None:
        return
    docs = m.load_docs(root)
    if not docs:
        return

    rows = []
    for doc in docs:
        if m.is_stub(root, doc):
            state = "EMPTY STUB"
        else:
            behind = m.commits_behind(root, doc)
            state = f"{behind} commits behind" if behind else "current"
        summary = doc["summary"] or ""
        rows.append(f"- `{doc['path']}` [{state}] — {summary}")

    context = (
        "ai_docs/ — the reference docs for this repo. Read the one that covers an area "
        "before working in it, and update it when your change makes it wrong.\n"
        + "\n".join(rows)
        + "\n`ai_docs/README.md` has the full contract. A doc marked EMPTY STUB has no "
        "content yet: if your task takes you into that area, write it rather than "
        "leaving the stub."
    )

    json.dump(
        {
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
