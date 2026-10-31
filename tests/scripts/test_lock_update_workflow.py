"""The weekly lock report workflow (issue #266): a static check of its YAML text."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "lock-update-check.yml"
CRON = r"cron: ['\"](\S+) (\S+) (\S+) (\S+) (\S+)['\"]"


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_runs_weekly_and_by_hand_only() -> None:
    text = _text()

    assert re.search(r"^on:\s*$", text, re.MULTILINE)
    assert re.search(r"^\s+schedule:\s*\n\s+- " + CRON, text, re.MULTILINE)
    assert "workflow_dispatch:" in text
    assert "pull_request" not in text and "push:" not in text


def test_cron_is_one_day_a_week_and_off_the_other_scheduled_minutes() -> None:
    mine = re.search(CRON, _text())
    ytdlp = re.search(CRON, (WORKFLOWS / "ytdlp-update-check.yml").read_text())
    digest = re.search(CRON, (WORKFLOWS / "base-image-digest-check.yml").read_text())

    assert mine and ytdlp and digest
    assert re.fullmatch(r"[0-7]", mine.group(5)), "day of week must be a single day"
    assert mine.group(3) == mine.group(4) == "*"
    assert mine.group(1) not in {ytdlp.group(1), digest.group(1)}
    assert (ytdlp.group(1), digest.group(1)) == ("17", "41")


def test_workflow_is_read_only_secretless_and_time_boxed() -> None:
    text = _text()

    assert re.search(r"^permissions:\s*\n\s+contents: read\s*$", text, re.MULTILINE)
    assert "secrets." not in text
    assert "needs:" not in text
    limit = re.search(r"timeout-minutes: (\d+)", text)
    assert limit and int(limit.group(1)) <= 15


def test_every_action_is_a_sha_already_used_by_another_workflow() -> None:
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", _text(), re.MULTILINE)
    trusted: set[str] = set()
    for other in WORKFLOWS.glob("*.yml"):
        if other != WORKFLOW:
            trusted.update(re.findall(r"uses:\s*(\S+)", other.read_text()))

    assert [ref.split("@")[0] for ref, _ in uses] == ["actions/checkout", "actions/setup-python"]
    for ref, rest in uses:
        assert re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", ref), ref
        assert "#" in rest
        assert ref in trusted, f"{ref} is not already used in another workflow"
    assert uses[0][0].endswith("11bd71901bbe5b1630ceea73d27597364c9af683")
    assert uses[1][0].endswith("5fda3b95a4ea91299a34e894583c3862153e4b97")


def test_it_resolves_on_python_314_and_runs_exactly_the_two_reports() -> None:
    text = _text()
    runs = re.findall(r"^\s+run:\s*(.+)$", text, re.MULTILINE)

    assert 'python-version: "3.14"' in text
    assert runs == [
        "python scripts/lock_requirements.py --report-outdated",
        "python scripts/lock_requirements.py --report-vulnerabilities",
    ]
    second = text.split("--report-outdated", 1)[1]
    assert re.search(r"if: \$\{\{ !cancelled\(\) \}\}\s+run: python .*--report-vulnerabilities", second)
    assert "pip install" not in text
