"""`pg-backup-status.sh` and `pg-backup-notify.sh` through their CLI (#150)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.ops.backup_support import (
    ACCOUNT_ID,
    BUCKET,
    HOUR,
    NOTIFY,
    NOW,
    STATUS,
    Rig,
    snap_id,
)

LIMIT = (24 + 6) * HOUR  # RPO 24 h + grace 6 h


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.mark.parametrize(
    ("age", "exit_code"),
    [
        (0, 0),
        (LIMIT - 1, 0),  # just inside
        (LIMIT, 0),  # exactly at the limit is still fresh
        (LIMIT + 1, 1),  # just outside
        (7 * 24 * HOUR, 1),
    ],
    ids=["new", "just-inside", "at-limit", "just-outside", "week-old"],
)
def test_freshness_boundary_is_rpo_plus_grace(rig: Rig, age: int, exit_code: int) -> None:
    rig.seed((snap_id(1), NOW - age))
    result = rig.run(STATUS, "--check-fresh")
    assert result.returncode == exit_code, result.stderr
    if exit_code:
        assert "STALE" in result.stderr
        assert snap_id(1) in result.stderr


def test_the_newest_of_several_snapshots_decides(rig: Rig) -> None:
    rig.seed((snap_id(1), NOW - 90 * HOUR), (snap_id(2), NOW - 2 * HOUR), (snap_id(3), NOW - 50 * HOUR))
    assert rig.run(STATUS, "--check-fresh").returncode == 0
    result = rig.run(STATUS, "--latest-id")
    assert result.stdout.strip() == snap_id(2)


def test_an_empty_repository_is_stale(rig: Rig) -> None:
    result = rig.run(STATUS, "--check-fresh")
    assert result.returncode == 1
    assert "no snapshot" in result.stderr
    assert rig.run(STATUS, "--latest-id").returncode == 1


def test_an_unreachable_repository_is_a_distinct_failure(rig: Rig) -> None:
    rig.set(fail={"snapshots": 1}, leak=True)
    result = rig.run(STATUS, "--check-fresh")
    assert result.returncode == 2
    assert "stage=repository" in result.stderr
    assert rig.leaked(result) == []


def test_the_window_follows_the_configured_rpo_and_grace(rig: Rig) -> None:
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\nRPO_HOURS=12\nGRACE_HOURS=1\nSTAGING_DIR={rig.staging}\n",
    )
    rig.seed((snap_id(1), NOW - 13 * HOUR))
    assert rig.run(STATUS, "--check-fresh").returncode == 0
    rig.set_now(NOW + 1)
    assert rig.run(STATUS, "--check-fresh").returncode == 1


def test_status_report_shows_schedule_result_snapshot_and_size_without_secrets(rig: Rig) -> None:
    rig.seed((snap_id(7), NOW - 5 * HOUR))
    result = rig.run(STATUS)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "ytdigest-pg-backup.timer" in out
    assert "enabled: enabled" in out
    assert "Result=success" in out
    assert f"id: {snap_id(7)}" in out
    assert "freshness: OK" in out
    assert "snapshots_total: 1" in out
    assert "raw_data_bytes: 12345" in out
    assert rig.leaked(result) == []


def test_status_report_marks_a_stale_snapshot(rig: Rig) -> None:
    rig.seed((snap_id(7), NOW - 40 * HOUR))
    assert "freshness: STALE" in rig.run(STATUS).stdout


def test_status_with_no_snapshot_says_none(rig: Rig) -> None:
    out = rig.run(STATUS).stdout
    assert "none" in out
    assert "snapshots_total: 0" in out


def test_status_rejects_an_unknown_option(rig: Rig) -> None:
    assert rig.run(STATUS, "--bogus").returncode == 2


# ---- notification hook ----------------------------------------------------------------------


def hook_script(rig: Rig, body: str = "", mode: int = 0o700) -> Path:
    hook = rig.root / "hook.sh"
    hook.write_text(f'#!/bin/bash\nprintf "%s|%s\\n" "$1" "$2" >> "{rig.root}/hook.out"\n{body}\n')
    hook.chmod(mode)
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\nNOTIFY_HOOK={hook}\nSTAGING_DIR={rig.staging}\n",
    )
    return hook


def test_the_hook_is_called_with_the_failed_unit_and_no_secret(rig: Rig) -> None:
    hook_script(rig)
    result = rig.run(NOTIFY, "ytdigest-pg-backup.service")
    assert result.returncode == 0, result.stderr
    kind, message = (rig.root / "hook.out").read_text().strip().split("|", 1)
    assert kind == "failure"
    assert "ytdigest-pg-backup.service" in message
    assert rig.leaked(subprocess.CompletedProcess([], 0, message, "")) == []


def test_without_a_hook_the_failure_is_only_logged(rig: Rig) -> None:
    result = rig.run(NOTIFY, "ytdigest-pg-backup.service")
    assert result.returncode == 0
    assert "no NOTIFY_HOOK configured" in result.stderr


def test_a_failing_hook_makes_the_notify_unit_fail(rig: Rig) -> None:
    hook_script(rig, body="exit 3")
    result = rig.run(NOTIFY, "ytdigest-pg-backup.service")
    assert result.returncode == 1
    assert "stage=notify" in result.stderr


def test_a_group_writable_hook_is_refused(rig: Rig) -> None:
    hook_script(rig, mode=0o770)
    result = rig.run(NOTIFY, "ytdigest-pg-backup.service")
    assert result.returncode == 1
    assert not (rig.root / "hook.out").exists()


def test_a_hostile_unit_name_never_reaches_the_hook(rig: Rig) -> None:
    hook_script(rig)
    rig.run(NOTIFY, "x; rm -rf /")
    assert "rm -rf" not in (rig.root / "hook.out").read_text()
