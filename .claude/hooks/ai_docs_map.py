#!/usr/bin/env python3
"""Shared helpers for the ai_docs hooks.

Every doc in ai_docs/ declares the code it owns in its own YAML frontmatter.
There is no central manifest: the map is rebuilt from disk on each call, so a
doc added by an agent is wired into the nudge system with no extra step.

Freshness is never stored in a file — it is derived from git, by comparing when
a doc was last committed against how many commits have since touched the code it
owns. A hand-maintained "last reviewed" stamp is the first thing to go stale.

Glob rules for `owns:` entries — use `**` (never a bare `*`) to cross directory
boundaries, so the pattern means the same thing to Python's fnmatch and to git's
pathspec engine.
"""

import fnmatch
import os
import subprocess
from pathlib import Path

DOCS_DIR = "ai_docs"


def repo_root():
    """Project root, or None if it cannot be determined."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / DOCS_DIR).is_dir():
        return Path(env)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return Path(out.stdout.strip())
    except Exception:
        pass
    return Path(env) if env else None


def parse_frontmatter(text):
    """Minimal frontmatter reader: scalars and `- ` lists only.

    Hand-rolled because system Python has no PyYAML and a hook must never
    depend on an install step.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith((" ", "\t")) and line.strip().startswith("- "):
            if key:
                data.setdefault(key, [])
                if isinstance(data[key], list):
                    data[key].append(line.strip()[2:].strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        data[key] = value if value else []
    return data


def load_docs(root):
    """All ai_docs entries as {path, doc, summary, owns}. Never raises."""
    docs = []
    if root is None:
        return docs
    for md in sorted((root / DOCS_DIR).glob("*.md")):
        if md.name == "README.md":
            continue
        try:
            meta = parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        owns = meta.get("owns") or []
        if isinstance(owns, str):
            owns = [owns]
        docs.append(
            {
                "path": f"{DOCS_DIR}/{md.name}",
                "doc": meta.get("doc") or md.stem,
                "summary": meta.get("summary") or "",
                "owns": [o for o in owns if o],
            }
        )
    return docs


def owners_for(rel_path, docs):
    """Docs owning `rel_path` (repo-relative, forward slashes)."""
    rel_path = rel_path.replace(os.sep, "/").lstrip("./")
    return [d for d in docs if any(fnmatch.fnmatch(rel_path, p) for p in d["owns"])]


def _git(root, args):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=str(root), capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def commits_behind(root, doc):
    """Commits touching this doc's owned code since the doc last changed.

    Returns None when the answer is unknown or meaningless: no owned paths, doc
    never committed (a fresh stub), or git unavailable.
    """
    if root is None or not doc["owns"]:
        return None
    ts = _git(root, ["log", "-1", "--format=%ct", "--", doc["path"]])
    if not ts:
        return None
    pathspecs = [":(glob)" + p for p in doc["owns"]]
    count = _git(
        root, ["rev-list", "--count", f"--since=@{ts}", "HEAD", "--"] + pathspecs
    )
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def is_stub(root, doc):
    """True when the doc still carries the untouched-stub marker."""
    try:
        return "<!-- stub -->" in (root / doc["path"]).read_text(encoding="utf-8")
    except Exception:
        return False
