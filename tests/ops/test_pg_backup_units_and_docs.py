"""Static checks of the backup systemd units, config example, drill compose file
and the runbook links (#150). Reads repo files; one test runs the installer into
a temp directory with a stubbed systemctl."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.ops.backup_support import ACCOUNT_ID, BUCKET, ROOT, Rig, snap_id

HOST = ROOT / "ops" / "host"
UNITS = HOST / "systemd"
RUNBOOK = ROOT / "_docs" / "backup-restore.md"
DEPLOY_DOC = ROOT / "_docs" / "deploy-host.md"


def unit(name: str) -> str:
    return (UNITS / name).read_text()


def test_the_backup_timer_is_nightly_and_persistent() -> None:
    timer = unit("ytdigest-pg-backup.timer")
    assert "Persistent=true" in timer
    assert re.search(r"^OnCalendar=\*-\*-\* 02:30:00 UTC$", timer, re.MULTILINE)
    assert "Unit=ytdigest-pg-backup.service" in timer


def test_the_freshness_timer_is_persistent_and_more_frequent_than_the_rpo() -> None:
    timer = unit("ytdigest-pg-backup-fresh.timer")
    assert "Persistent=true" in timer
    assert "OnCalendar=*-*-* 00/6:50:00 UTC" in timer


@pytest.mark.parametrize(
    "name",
    ["ytdigest-pg-backup.service", "ytdigest-pg-backup-fresh.service", "ytdigest-pg-backup-notify@.service"],
)
def test_services_run_as_the_named_account_with_no_embedded_secrets(name: str) -> None:
    text = unit(name)
    assert "Type=oneshot" in text
    assert "User=@USER@" in text
    assert "UMask=0077" in text
    assert "NoNewPrivileges=yes" in text
    assert re.search(r"^TimeoutStartSec=", text, re.MULTILINE)
    assert "Environment" not in text  # no EnvironmentFile or Environment= lines at all
    assert not re.search(r"AWS_|PASSWORD|SECRET|TOKEN|r2\.cloudflarestorage", text)


def test_failures_of_both_services_run_the_notification_unit() -> None:
    for name in ("ytdigest-pg-backup.service", "ytdigest-pg-backup-fresh.service"):
        assert "OnFailure=ytdigest-pg-backup-notify@%n.service" in unit(name)


def test_the_backup_service_runs_only_the_backup_script() -> None:
    text = unit("ytdigest-pg-backup.service")
    assert "ExecStart=@REPO@/ops/host/pg-backup.sh\n" in text
    assert text.count("ExecStart=") == 1


def test_no_unit_or_script_restores_automatically() -> None:
    for path in UNITS.iterdir():
        assert "pg-restore-drill" not in path.read_text(), path.name
    for script in ("pg-backup.sh", "pg-backup-status.sh", "pg-backup-notify.sh"):
        assert "pg_restore --exit-on-error" not in (HOST / script).read_text()


def test_the_drill_compose_image_is_the_production_db_image() -> None:
    compose = (ROOT / "compose.yml").read_text()
    drill = (HOST / "pg-restore-drill.compose.yml").read_text()
    prod = re.search(r"^  db:\n    image: (\S+)", compose, re.MULTILINE)
    mine = re.search(r"^    image: (\S+)", drill, re.MULTILINE)
    assert prod and mine
    assert prod.group(1) == mine.group(1)


def test_the_drill_stack_has_no_network_no_published_port_and_its_own_volume() -> None:
    drill = (HOST / "pg-restore-drill.compose.yml").read_text()
    assert "network_mode: none" in drill
    assert "ports:" not in drill
    volume_lines = [ln.strip() for ln in drill.splitlines() if ln.strip().startswith("- ") and ":/" in ln]
    assert volume_lines == ["- drillpgdata:/var/lib/postgresql"]  # a project-scoped volume, never pgdata


def test_the_config_example_parses_and_has_no_secret_keys() -> None:
    text = (HOST / "backup.conf.example").read_text()
    assert not re.search(r"^(AWS_|RESTIC_)", text, re.MULTILINE)
    keys = {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}
    assert {"R2_ACCOUNT_ID", "R2_BUCKET", "R2_JURISDICTION", "RPO_HOURS", "GRACE_HOURS", "KEEP_DAILY", "KEEP_WEEKLY"} <= keys


def test_the_shipped_config_example_is_accepted_by_the_scripts(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    example = (HOST / "backup.conf.example").read_text().replace("STAGING_DIR=/var/lib/ytdigest-backup", f"STAGING_DIR={rig.staging}")
    rig.write_file("backup.conf", example)
    rig.seed((snap_id(1), 1_800_000_000 - 100))
    result = rig.run(HOST / "pg-backup-status.sh", "--check-fresh")
    assert result.returncode == 0, result.stderr
    repos = {c["env"]["repo"] for c in rig.calls("restic")}
    assert repos == {f"s3:https://{'0' * 32}.eu.r2.cloudflarestorage.com/ytdigest-pg-backup"}


def test_installer_fills_in_user_and_checkout_and_enables_nothing(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    (bindir / "systemctl").write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    (bindir / "systemctl").chmod(0o755)
    (bindir / "install").write_text(  # keep -o (chown) away from the test: no root needed
        '#!/bin/bash\nargs=(); skip=0\nfor a in "$@"; do\n'
        '  if ((skip)); then skip=0; continue; fi\n'
        '  if [[ $a == -o ]]; then skip=1; continue; fi\n'
        '  args+=("$a")\ndone\nexec /usr/bin/install "${args[@]}"\n'
    )
    (bindir / "install").chmod(0o755)
    dest = tmp_path / "systemd"
    staging = tmp_path / "state"
    result = subprocess.run(
        ["bash", str(HOST / "install-pg-backup-units.sh"), "--user", "deploy"],
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "YTDIGEST_SYSTEMD_DIR": str(dest),
            "YTDIGEST_STAGING_DIR": str(staging),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in dest.iterdir())
    assert names == sorted(p.name for p in UNITS.iterdir())
    service = (dest / "ytdigest-pg-backup.service").read_text()
    assert "User=deploy" in service
    assert f"ExecStart={ROOT}/ops/host/pg-backup.sh" in service
    assert "@USER@" not in service and "@REPO@" not in service
    assert (staging.stat().st_mode & 0o777) == 0o700
    calls = log.read_text()
    assert "daemon-reload" in calls
    assert "enable" not in calls and "start" not in calls


def test_installer_rejects_a_bad_account_name(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(HOST / "install-pg-backup-units.sh"), "--user", "x; rm -rf /"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2


# ---- the runbook -------------------------------------------------------------------------------


def test_deploy_host_links_the_runbook_and_repeats_the_down_v_ban() -> None:
    text = DEPLOY_DOC.read_text()
    assert "backup-restore.md" in text
    assert "down -v` deletes `pgdata`" in text


RUNBOOK_MUST_MENTION = (
    "Steps you must do",
    "dash.cloudflare.com",
    "card",
    "irreversible",
    "Specify jurisdiction",
    "eu.r2.cloudflarestorage.com",
    "Object Read & Write",
    "Object Read only",
    "Specify bucket",
    "/etc/ytdigest-backup",
    "mode 700",
    "chmod 600",
    "openssl rand",
    "password manager",
    "offline copy",
    "restic version",
    "0.18.1",
    "pg-backup.sh --init",
    "pg-backup.sh --check",
    "systemctl enable --now ytdigest-pg-backup.timer",
    "pg-restore-drill.sh --snapshot",
    "pg-backup-status.sh --check-fresh",
    "NOTIFY_HOOK",
    "Evidence",
    "key",
    "disk",
    "lock",
    "Interrupted",
    "Disaster recovery",
    "In-place replacement",
    "bucket lock",
    "Not proven",
    "10 GB",
    "1.3%",
)


@pytest.mark.parametrize("needle", RUNBOOK_MUST_MENTION)
def test_runbook_covers(needle: str) -> None:
    assert needle.lower() in RUNBOOK.read_text().lower()


def test_runbook_steps_are_numbered_in_order_without_gaps() -> None:
    text = RUNBOOK.read_text()
    section = text.split("## 0. Steps you must do (owner)")[1].split("\n## 1. ")[0]
    numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", section, re.MULTILINE)]
    assert numbers == list(range(1, 19))


def test_runbook_never_tells_the_operator_to_down_v_production() -> None:
    for line in RUNBOOK.read_text().splitlines():
        if "down -v" in line:
            assert re.search(r"never|Nothing|only|not", line, re.IGNORECASE), line
    assert not re.search(r"compose -f compose\.yml down", RUNBOOK.read_text())


def test_runbook_secret_commands_never_put_values_on_a_command_line() -> None:
    text = RUNBOOK.read_text()
    assert "read -rsp" in text
    assert not re.search(r"AWS_SECRET_ACCESS_KEY=[A-Za-z0-9]{8,}", text)
    assert ACCOUNT_ID not in text and BUCKET not in text
    assert "RESTIC_PASSWORD=" not in text
