"""`ops/host/pg-backup.sh` through its CLI with stubbed docker, restic, clock and df (#150)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
from pathlib import Path

import pytest

from tests.ops.backup_support import (
    ACCOUNT_ID,
    BACKUP,
    BUCKET,
    HOST,
    NOW,
    SECRETS,
    Rig,
    snap_id,
)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def stage_of(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stderr.splitlines():
        if "FAILED stage=" in line:
            return line.split("FAILED stage=")[1].split(":")[0]
    return ""


def compose_calls(rig: Rig) -> list[list[str]]:
    return [c["args"] for c in rig.calls("docker") if c["args"][:1] == ["compose"]]


# ---- success ------------------------------------------------------------------


def test_success_publishes_one_verified_snapshot_and_leaves_no_plaintext(rig: Rig) -> None:
    result = rig.run(BACKUP)
    assert result.returncode == 0, result.stderr

    # dump is validated before anything leaves the host; retention comes last
    assert rig.restic_subcommands() == ["backup", "check", "forget"]
    calls = rig.calls()
    list_at = next(i for i, c in enumerate(calls) if c["tool"] == "docker" and "--list" in c["args"])
    upload_at = next(i for i, c in enumerate(calls) if c["tool"] == "restic" and c["args"][0] == "backup")
    assert list_at < upload_at

    stored = sorted(p.name for p in next((rig.stub / "repo").iterdir()).iterdir())
    assert stored == ["manifest.txt", "ytdigest.dump"]  # no .partial ever published
    manifest = next((rig.stub / "repo").iterdir()) / "manifest.txt"
    text = manifest.read_text()
    assert "alembic 0007_example" in text
    assert "table videos 12" in text
    assert "# dbsize 1048576" in text

    assert not (rig.staging / "work").exists()
    assert "event=start" in result.stdout
    assert "event=finish outcome=ok" in result.stdout
    assert f"snapshot={snap_id_from_state(rig)}" in result.stdout
    assert "bytes=" in result.stdout
    assert rig.leaked(result) == []


def snap_id_from_state(rig: Rig) -> str:
    state = json.loads((rig.stub / "restic_state.json").read_text())
    return str(state["snapshots"][-1]["id"])


def test_only_the_production_stack_is_addressed_and_never_modified(rig: Rig) -> None:
    result = rig.run(BACKUP, extra_env={"COMPOSE_PROJECT_NAME": "something-else"})
    assert result.returncode == 0, result.stderr
    calls = compose_calls(rig)
    assert calls
    for args in calls:
        assert args[1:5] == ["-p", "ytdigest", "-f", "compose.yml"], args
        assert args[5] == "exec", args  # never up/down/restart/run/migrate
    assert {c["compose_env"] for c in rig.calls("docker")} == {None}
    dump = next(a for a in calls if "pg_dump" in a)
    assert "-Fc" in dump
    assert any(a.startswith("--snapshot=") for a in dump)


def test_restic_gets_r2_settings_from_files_and_no_secret_in_any_argument(rig: Rig) -> None:
    assert rig.run(BACKUP).returncode == 0
    env = rig.calls("restic")[-1]["env"]
    assert env["region"] == "auto"
    assert env["has_key"] and env["has_secret"] and env["password_file_set"]
    assert env["repo"] == f"s3:https://{ACCOUNT_ID}.eu.r2.cloudflarestorage.com/{BUCKET}"
    for call in rig.calls():
        joined = " ".join(call["args"])
        assert [s for s in SECRETS if s in joined] == []


@pytest.mark.parametrize(
    ("conf", "host"),
    [
        ("R2_JURISDICTION=default\n", f"{ACCOUNT_ID}.r2.cloudflarestorage.com"),
        ("R2_JURISDICTION=eu\n", f"{ACCOUNT_ID}.eu.r2.cloudflarestorage.com"),
        ("R2_JURISDICTION=fedramp\n", f"{ACCOUNT_ID}.fedramp.r2.cloudflarestorage.com"),
        ("R2_ENDPOINT_HOST=s3.example.test\n", "s3.example.test"),
    ],
    ids=["default", "eu", "fedramp", "explicit-host"],
)
def test_endpoint_is_derived_from_config_never_hard_coded(rig: Rig, conf: str, host: str) -> None:
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\n{conf}STAGING_DIR={rig.staging}\n",
    )
    assert rig.run(BACKUP).returncode == 0
    assert rig.calls("restic")[-1]["env"]["repo"] == f"s3:https://{host}/{BUCKET}"


def _conf(rig: Rig, extra: str = "") -> None:
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\n{extra}STAGING_DIR={rig.staging}\n",
    )


def test_retention_runs_after_check_and_uses_the_configured_policy(rig: Rig) -> None:
    _conf(rig, "RETENTION_DAYS=10\nKEEP_LAST_FLOOR=5\n")
    assert rig.run(BACKUP).returncode == 0
    (forget,) = rig.restic_calls("forget")
    assert "--prune" in forget
    assert forget[forget.index("--keep-within") + 1] == "10d"
    assert forget[forget.index("--keep-last") + 1] == "5"
    assert "--keep-daily" not in forget and "--keep-weekly" not in forget
    assert forget[forget.index("--group-by") + 1] == "host,tags"
    assert forget[forget.index("--tag") + 1] == "ytdigest-pg"
    assert rig.restic_subcommands() == ["backup", "check", "forget"]


def test_default_policy_is_14_days_with_a_floor_of_3(rig: Rig) -> None:
    assert rig.run(BACKUP).returncode == 0
    (forget,) = rig.restic_calls("forget")
    assert forget[forget.index("--keep-within") + 1] == "14d"
    assert forget[forget.index("--keep-last") + 1] == "3"


def _surviving(rig: Rig) -> set[str]:
    state = json.loads((rig.stub / "restic_state.json").read_text())
    return {s["id"] for s in state["snapshots"]}


DAY = 86400


def test_a_snapshot_one_second_inside_14_days_is_kept_and_exactly_14_days_is_pruned(rig: Rig) -> None:
    # Real restic's keep-within is strict: a snapshot must be NEWER than 14 d (counted
    # back from the newest snapshot, the one just taken) to be kept; the real-restic
    # test in test_pg_backup_restore_docker.py proves that boundary. Floor 1 so it
    # does not hide the age rule.
    _conf(rig, "KEEP_LAST_FLOOR=1\n")
    rig.seed((snap_id(1), NOW - 14 * DAY), (snap_id(2), NOW - 14 * DAY + 1), (snap_id(3), NOW - 13 * DAY))
    assert rig.run(BACKUP).returncode == 0
    kept = _surviving(rig)
    assert snap_id(2) in kept and snap_id(3) in kept  # just inside, and well inside
    assert snap_id(1) not in kept  # exactly 14 days is outside


def test_the_keep_last_floor_survives_when_every_old_snapshot_is_past_14_days(rig: Rig) -> None:
    # A failed night (not simulated here) after a long outage: the only snapshots
    # are old. The new one is newest, so keep-within is relative to it; the floor
    # still keeps the newest 3 whatever their age.
    rig.seed((snap_id(1), NOW - 90 * DAY), (snap_id(2), NOW - 60 * DAY), (snap_id(3), NOW - 30 * DAY))
    assert rig.run(BACKUP).returncode == 0
    kept = _surviving(rig)
    assert snap_id(1) not in kept  # 4 snapshots exist, the floor keeps the newest 3
    assert {snap_id(2), snap_id(3)} <= kept and len(kept) == 3


def test_a_failed_backup_prunes_nothing_even_with_old_snapshots(rig: Rig) -> None:
    rig.seed((snap_id(1), NOW - 90 * DAY), (snap_id(2), NOW - 60 * DAY))
    rig.set(fail={"backup": 1})
    assert rig.run(BACKUP).returncode != 0
    assert rig.restic_calls("forget") == []
    assert _surviving(rig) == {snap_id(1), snap_id(2)}


@pytest.mark.parametrize(
    ("name", "value", "ok"),
    [
        ("RETENTION_DAYS", "0", False),  # would keep nothing
        ("RETENTION_DAYS", "1", True),
        ("RETENTION_DAYS", "3650", True),
        ("RETENTION_DAYS", "3651", False),
        ("RETENTION_DAYS", "-1", False),
        ("KEEP_LAST_FLOOR", "0", False),  # no floor is not allowed
        ("KEEP_LAST_FLOOR", "1", True),
        ("KEEP_LAST_FLOOR", "1001", False),
        ("RPO_HOURS", "0", False),
        ("RPO_HOURS", "1", True),
        ("GRACE_HOURS", "0", True),
        ("RETENTION_DAYS", "x", False),
    ],
    ids=lambda v: str(v),
)
def test_retention_and_rpo_bounds_are_enforced(rig: Rig, name: str, value: str, ok: bool) -> None:
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\n{name}={value}\nSTAGING_DIR={rig.staging}\n",
    )
    result = rig.run(BACKUP)
    if ok:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert stage_of(result) == "config"
        assert rig.calls("docker") == []


@pytest.mark.parametrize(("day", "deep"), [(1, True), (2, False), (31, False)], ids=["first", "second", "last"])
def test_deeper_data_check_runs_only_on_the_first_utc_day_of_the_month(rig: Rig, day: int, deep: bool) -> None:
    # 2027-01-<day> 12:00 UTC
    epoch = 1_798_761_600 + (day - 1) * 86400 + 12 * 3600
    rig.set_now(epoch)
    assert rig.run(BACKUP).returncode == 0
    (check,) = rig.restic_calls("check")
    assert any(a.startswith("--read-data-subset") for a in check) is deep


# ---- failures: every stage names itself, keeps older snapshots, cleans up --------------


@pytest.mark.parametrize(
    ("scenario", "stage", "reached_upload"),
    [
        ({"ready_rc": 1}, "db", False),
        ({"psql_rc": 1}, "manifest", False),
        ({"manifest_lines": []}, "manifest", False),
        ({"manifest_lines": ["table t 1"]}, "manifest", False),
        ({"dump_rc": 1}, "dump", False),
        ({"dump_empty": True}, "dump", False),
        ({"list_rc": 1}, "validate", False),
        ({"docker_info_rc": 1}, "docker", False),
        ({"fail": {"backup": 1}}, "upload", True),
        ({"fail": {"check": 1}}, "verify", True),
        ({"fail": {"forget": 1}}, "retention", True),
        ({"version": "0.17.3"}, "prereq", False),
    ],
    ids=[
        "db-not-ready",
        "psql-fails",
        "empty-manifest",
        "no-alembic",
        "dump-fails",
        "empty-dump",
        "unreadable-dump",
        "docker-down",
        "upload-fails",
        "check-fails",
        "retention-fails",
        "restic-too-old",
    ],
)
def test_each_failure_names_its_stage_and_never_publishes_a_bad_snapshot(
    rig: Rig, scenario: dict[str, object], stage: str, reached_upload: bool
) -> None:
    rig.seed((snap_id(99), NOW - 3600))
    rig.set(**scenario)
    result = rig.run(BACKUP)
    assert result.returncode != 0
    assert stage_of(result) == stage, result.stderr
    if stage != "prereq":  # prereq fails before the run is logged as started
        assert f"outcome=failed stage={stage}" in result.stdout
    assert not (rig.staging / "work").exists()
    assert rig.leaked(result) == []
    subs = rig.restic_subcommands()
    assert ("backup" in subs) is reached_upload
    # an older known-good snapshot is never removed by a failed run
    state_file = rig.stub / "restic_state.json"
    if state_file.exists():
        ids = [s["id"] for s in json.loads(state_file.read_text())["snapshots"]]
        assert snap_id(99) in ids
    # retention (prune) only ever follows a successful check
    prunes = [a for a in rig.restic_calls("forget") if "--prune" in a]
    if stage not in ("retention",):
        assert prunes == []


def test_failed_verification_withdraws_only_the_new_snapshot(rig: Rig) -> None:
    rig.seed((snap_id(99), NOW - 86400))
    rig.set(fail={"check": 1})
    result = rig.run(BACKUP)
    assert stage_of(result) == "verify"
    (forget,) = rig.restic_calls("forget")
    assert "--prune" not in forget
    assert snap_id(99) not in forget
    ids = [s["id"] for s in json.loads((rig.stub / "restic_state.json").read_text())["snapshots"]]
    assert ids == [snap_id(99)]


def test_not_enough_local_space_stops_before_the_dump(rig: Rig) -> None:
    rig.set(dbsize=200 * 1024 * 1024)
    (rig.stub / "df_avail").write_text(str(200 * 1024 + 65536 - 1))  # one KB short
    result = rig.run(BACKUP)
    assert stage_of(result) == "disk"
    assert not any("pg_dump" in c["args"] for c in rig.calls("docker"))
    assert rig.restic_subcommands() == []


def test_exactly_enough_local_space_is_accepted(rig: Rig) -> None:
    rig.set(dbsize=200 * 1024 * 1024)
    (rig.stub / "df_avail").write_text(str(200 * 1024 + 65536))
    assert rig.run(BACKUP).returncode == 0


def test_staging_inside_docker_data_root_is_refused(rig: Rig) -> None:
    rig.set(docker_root=str(rig.staging.parent))
    result = rig.run(BACKUP)
    assert stage_of(result) == "staging"
    assert rig.restic_subcommands() == []


def test_staging_inside_the_checkout_is_refused(rig: Rig) -> None:
    inside = HOST.parent.parent / ".pytest-staging-never-created"
    rig.write_file(
        "backup.conf",
        f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\nSTAGING_DIR={inside}\n",
    )
    result = rig.run(BACKUP)
    assert result.returncode != 0
    assert not inside.exists()


def test_restic_failure_output_is_redacted(rig: Rig) -> None:
    rig.set(fail={"backup": 1}, leak=True, leak_host="evil.example.test")
    result = rig.run(BACKUP)
    assert stage_of(result) == "upload"
    assert rig.leaked(result) == []
    assert "https://" not in result.stdout + result.stderr
    assert "user:pw" not in result.stdout + result.stderr
    assert ACCOUNT_ID not in result.stdout + result.stderr


# ---- credentials and config ---------------------------------------------------------------


def test_missing_credentials_fail_before_docker_is_touched(rig: Rig) -> None:
    (rig.cfg / "r2.env").unlink()
    result = rig.run(BACKUP)
    assert stage_of(result) == "credentials"
    assert rig.calls() == []


def _mutate(rig: Rig, case: str) -> None:
    cfg = rig.cfg
    if case == "empty-password":
        (cfg / "restic.password").write_text("")
    elif case == "short-password":
        (cfg / "restic.password").write_text("short")
    elif case == "empty-r2":
        (cfg / "r2.env").write_text("")
    elif case == "loose-r2":
        (cfg / "r2.env").chmod(0o644)
    elif case == "group-readable-password":
        (cfg / "restic.password").chmod(0o640)
    elif case == "loose-conf":
        (cfg / "backup.conf").chmod(0o664)
    elif case == "loose-dir":
        cfg.chmod(0o755)
    elif case == "symlinked-password":
        real = cfg.parent / "real.password"
        real.write_text("x" * 30)
        real.chmod(0o600)
        (cfg / "restic.password").unlink()
        (cfg / "restic.password").symlink_to(real)
    elif case == "missing-secret-key":
        (cfg / "r2.env").write_text("AWS_ACCESS_KEY_ID=abc\n")
    elif case == "unknown-key":
        (cfg / "r2.env").write_text("AWS_ACCESS_KEY_ID=a\nAWS_SECRET_ACCESS_KEY=b\nOTHER=c\n")
    elif case == "quoted-value":
        (cfg / "r2.env").write_text('AWS_ACCESS_KEY_ID="a b"\nAWS_SECRET_ACCESS_KEY=b\n')
    elif case == "bad-account":
        (cfg / "backup.conf").write_text(f"R2_ACCOUNT_ID=nothex\nR2_BUCKET={BUCKET}\nSTAGING_DIR={rig.staging}\n")
    elif case == "bad-bucket":
        (cfg / "backup.conf").write_text(f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET=Bad_Bucket\nSTAGING_DIR={rig.staging}\n")
    elif case == "bad-jurisdiction":
        (cfg / "backup.conf").write_text(
            f"R2_ACCOUNT_ID={ACCOUNT_ID}\nR2_BUCKET={BUCKET}\nR2_JURISDICTION=moon\nSTAGING_DIR={rig.staging}\n"
        )
    elif case == "url-endpoint":
        (cfg / "backup.conf").write_text(
            f"R2_BUCKET={BUCKET}\nR2_ENDPOINT_HOST=https://u:p@x.test\nSTAGING_DIR={rig.staging}\n"
        )
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "empty-password",
        "short-password",
        "empty-r2",
        "loose-r2",
        "group-readable-password",
        "loose-conf",
        "loose-dir",
        "symlinked-password",
        "missing-secret-key",
        "unknown-key",
        "quoted-value",
        "bad-account",
        "bad-bucket",
        "bad-jurisdiction",
        "url-endpoint",
    ],
)
def test_bad_credential_or_config_files_are_refused_without_echoing_values(rig: Rig, case: str) -> None:
    _mutate(rig, case)
    result = rig.run(BACKUP)
    assert result.returncode != 0
    assert stage_of(result) in ("config", "credentials")
    assert rig.calls("docker") == [] and rig.restic_subcommands() == []
    assert rig.leaked(result) == []
    assert "u:p@" not in result.stdout + result.stderr


def test_read_only_credential_files_are_accepted(rig: Rig) -> None:
    (rig.cfg / "r2.env").chmod(0o400)
    (rig.cfg / "restic.password").chmod(0o400)
    assert rig.run(BACKUP).returncode == 0


def test_config_directory_inside_the_checkout_is_refused(rig: Rig) -> None:
    result = rig.run(BACKUP, extra_env={"YTDIGEST_BACKUP_CONFIG_DIR": str(HOST)})
    assert stage_of(result) == "config"


# ---- locking, interruption, retry ---------------------------------------------------------


def test_a_second_run_while_one_holds_the_lock_fails_at_the_lock_stage(rig: Rig) -> None:
    with (rig.staging / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = rig.run(BACKUP)
    assert stage_of(result) == "lock"
    assert not any("pg_dump" in c["args"] for c in rig.calls("docker"))


def test_leftovers_of_an_interrupted_run_do_not_block_the_next_run(rig: Rig) -> None:
    leftover = rig.staging / "work"
    leftover.mkdir()
    (leftover / "ytdigest.dump.partial").write_text("half a dump")
    assert rig.run(BACKUP).returncode == 0
    assert not leftover.exists()


def test_sigterm_during_the_dump_removes_plaintext_and_publishes_nothing(rig: Rig) -> None:
    rig.set(dump_block=True)
    proc = rig.popen(BACKUP)
    try:
        rig.wait_for("dump_started")
        assert (rig.staging / "work").is_dir()
        os.killpg(proc.pid, signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    assert proc.returncode == 143
    assert not (rig.staging / "work").exists()
    assert "backup" not in rig.restic_subcommands()
    assert "outcome=failed" in out
    assert [s for s in SECRETS if s in out + err] == []


# ---- --check and --init ---------------------------------------------------------------------


def test_check_mode_validates_without_dumping_or_uploading(rig: Rig) -> None:
    result = rig.run(BACKUP, "--check")
    assert result.returncode == 0, result.stderr
    assert rig.restic_subcommands() == ["cat"]
    assert not any("pg_dump" in c["args"] for c in rig.calls("docker"))


def test_check_mode_reports_an_unreachable_repository(rig: Rig) -> None:
    rig.set(fail={"cat": 1})
    result = rig.run(BACKUP, "--check")
    assert stage_of(result) == "repository"


def test_init_creates_a_missing_repository(rig: Rig) -> None:
    rig.set(repo_exists=False)
    result = rig.run(BACKUP, "--init")
    assert result.returncode == 0, result.stderr
    assert rig.restic_subcommands() == ["cat", "init"]


def test_init_refuses_an_existing_repository(rig: Rig) -> None:
    result = rig.run(BACKUP, "--init")
    assert stage_of(result) == "init"
    assert "init" not in rig.restic_subcommands()


def test_unknown_argument_is_a_usage_error(rig: Rig) -> None:
    assert rig.run(BACKUP, "--restore").returncode == 2


# ---- static guarantees -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["pg-backup.sh", "pg-backup-lib.sh", "pg-backup-status.sh", "pg-restore-drill.sh"])
def test_scripts_never_read_dotenv_or_the_postgres_password(name: str) -> None:
    text = (HOST / name).read_text()
    assert "POSTGRES_PASSWORD" not in text
    assert "set -x" not in text
    assert re.search(r"(?<![A-Za-z0-9])\.env\b", text) is None  # r2.env is fine, .env is not


def test_scripts_use_strict_mode_and_a_restrictive_umask() -> None:
    assert "set -Eeuo pipefail" in BACKUP.read_text()
    assert "umask 077" in (HOST / "pg-backup-lib.sh").read_text()


def test_a_custom_endpoint_host_is_masked_in_restic_errors(rig: Rig) -> None:
    _conf(rig, "R2_ENDPOINT_HOST=s3.example.test\n")
    rig.set(fail={"backup": 1}, leak=True, leak_host="s3.example.test")
    result = rig.run(BACKUP)
    assert stage_of(result) == "upload"
    assert "s3.example.test" not in result.stdout + result.stderr
