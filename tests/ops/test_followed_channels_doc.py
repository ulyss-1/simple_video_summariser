"""``_docs/followed-channels.md`` must match migration 0026's seed (#386).

Offline: reads repo files only. The check is a pure function over text that
returns a list of problems, shown failing on crafted bad documents first.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "_docs" / "followed-channels.md"
ROW = re.compile(r"^\|\s*(?P<title>[^|]+?)\s*\|\s*`(?P<id>UC[^`]*)`\s*\|", re.MULTILINE)


def _seed() -> tuple[tuple[str, str], ...]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("0026")
    assert revision is not None
    return revision.module.FOLLOWED_CHANNELS  # type: ignore[no-any-return]


def check(text: str, seed: tuple[tuple[str, str], ...]) -> list[str]:
    rows = [(m["id"], m["title"]) for m in ROW.finditer(text)]
    ids = [i for i, _ in rows]
    docs = dict(rows)
    want = dict(seed)
    problems = [f"duplicated id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    problems += [f"missing id {i}" for i in sorted(want.keys() - docs.keys())]
    problems += [f"extra id {i}" for i in sorted(docs.keys() - want.keys())]
    problems += [
        f"title differs for {i}: {docs[i]!r} != {want[i]!r}"
        for i in sorted(want.keys() & docs.keys())
        if want[i] != docs[i]
    ]
    return problems


def _table(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"| {t} | `{i}` | `@h` |" for i, t in rows)


A = ("UCaaaaaaaaaaaaaaaaaaaaaa", "A")
B = ("UCbbbbbbbbbbbbbbbbbbbbbb", "B")
C = ("UCcccccccccccccccccccccc", "C")


def test_check_flags_bad_documents() -> None:
    seed = (A, B)
    assert check(_table([A, B]), seed) == []
    assert check(_table([A]), seed) == [f"missing id {B[0]}"]
    assert check(_table([A, B, C]), seed) == [f"extra id {C[0]}"]
    assert f"duplicated id {A[0]}" in check(_table([A, B, A]), seed)
    assert len(check(_table([A, ("UCbbbbbbbbbbbbbbbbbbbbbc", "B")]), seed)) == 2
    assert check(_table([A, (B[0], "Bx")]), seed) == [
        f"title differs for {B[0]}: 'Bx' != 'B'"
    ]


def test_followed_channels_doc_matches_migration() -> None:
    assert check(DOC.read_text(encoding="utf-8"), _seed()) == []
