#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit): name the ai_docs page that owns the edited file.

Fires at the moment of the edit, so the reminder is specific — which doc, and
how far behind it is — rather than a generic "remember the docs" that a model
skims past.

Deduplicated per session: each doc is mentioned at most once, and not at all if
the doc has already been edited this session. Without that, a 40-file task would
re-inject the same paragraph 40 times.

Fails silent by construction. A hook that errors is worse than a missing nudge.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def marker_dir(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:96]
    base = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-ai-docs" / safe
    base.mkdir(parents=True, exist_ok=True)
    return base


def main():
    payload = json.load(sys.stdin)
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return

    import ai_docs_map as m

    root = m.repo_root()
    if root is None:
        return

    try:
        rel = str(Path(file_path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return
    rel = rel.replace(os.sep, "/")

    markers = marker_dir(payload.get("session_id"))

    # Editing a doc satisfies it for the rest of the session.
    if rel.startswith(f"{m.DOCS_DIR}/"):
        (markers / Path(rel).name).write_text("done", encoding="utf-8")
        return

    docs = m.load_docs(root)
    pending = []
    for doc in m.owners_for(rel, docs):
        marker = markers / Path(doc["path"]).name
        if marker.exists():
            continue
        marker.write_text("nudged", encoding="utf-8")
        pending.append(doc)

    if not pending:
        return

    lines = []
    for doc in pending:
        behind = m.commits_behind(root, doc)
        if m.is_stub(root, doc):
            state = "still an empty stub"
        elif behind:
            state = f"{behind} commit{'s' if behind != 1 else ''} behind the code it documents"
        else:
            state = "currently up to date"
        lines.append(f"- `{doc['path']}` ({state})")

    context = (
        "Documentation ownership — you just edited `%s`.\n%s\n"
        "Before reporting this task complete, update the doc(s) above so they describe "
        "how the code behaves now. Record current behaviour, not a changelog of what "
        "changed. If the edit genuinely does not affect what a doc says, leave it alone "
        "— this is a pointer, not an order." % (rel, "\n".join(lines))
    )

    json.dump(
        {
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
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
