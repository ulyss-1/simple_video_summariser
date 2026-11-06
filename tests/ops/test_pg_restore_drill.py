"""`pg-restore-drill.sh` through its CLI with stubbed docker and restic (#150)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.ops.backup_support import (
    BACKUP,
    DRILL,
    MANIFEST_LINES,
    SECRETS,
    Rig,
    snap_id,
)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    rig = Rig(tmp_path)
    assert rig.run(BACKUP).returncode == 0  # a real snapshot in the stub repository
    rig.set(restored_lines=MANIFEST_LINES)
    return rig


def newest(rig: Rig) -> str:
    import json

    state = json.loads((rig.stub / "restic_state.json").read_text())
    return str(state["snapshots"][-1]["id"])


def drill_calls(rig: Rig) -> list[dict[str, Any]]:
    # Only what happened after the backup that built the fixture.
    return [c for c in rig.calls("docker") if str(c.get("project", "")).startswith("ytdigest-drill-")]


def stage_of(result: object) -> str:
    stderr = getattr(result, "stderr", "")
    for line in stderr.splitlines():
        if "FAILED stage=" in line:
            return str(line.split("FAILED stage=")[1].split(":")[0])
    return ""


def verbs(rig: Rig) -> list[str]:
    out = []
    for c in drill_calls(rig):
        args = c["args"]
        assert isinstance(args, list)
        out.append("exec" if "exec" in args else next(a for a in args if a in ("up", "down")))
    return out


def test_a_good_snapshot_is_restored_verified_and_cleaned_up(rig: Rig) -> None:
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert result.returncode == 0, result.stderr
    assert "event=verified" in result.stdout
    assert "outcome=verified" in result.stdout
    assert "tables=3" in result.stdout
    calls = drill_calls(rig)
    projects = {str(c["project"]) for c in calls}
    assert len(projects) == 1
    (project,) = projects
    assert project.startswith("ytdigest-drill-") and project != "ytdigest"
    seq = verbs(rig)
    assert seq[0] == "up" and seq[-1] == "down"
    up = next(c for c in calls if "up" in c["args"])
    assert "--wait" in up["args"] and "--wait-timeout" in up["args"]  # bounded readiness
    down = next(c for c in calls if "down" in c["args"])
    assert "-v" in down["args"]
    restore = next(c for c in calls if "--exit-on-error" in c["args"])
    assert "--no-owner" in restore["args"]
    assert not list(rig.staging.glob("drill.*"))  # plaintext gone
    assert rig.leaked(result) == []


def test_empty_tables_are_valid_not_a_failure(rig: Rig) -> None:
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert "table transcripts 0" in "\n".join(MANIFEST_LINES)
    assert result.returncode == 0


def test_the_encrypted_snapshot_is_retained(rig: Rig) -> None:
    before = len(rig.restic_calls("forget"))
    rig.run(DRILL, "--snapshot", newest(rig))
    assert len(rig.restic_calls("forget")) == before  # the drill never forgets or prunes
    assert (rig.stub / "repo" / newest(rig)).exists()


def test_latest_is_not_accepted_it_must_be_resolved_first(rig: Rig) -> None:
    result = rig.run(DRILL, "--snapshot", "latest")
    assert result.returncode != 0
    assert "--latest-id" in result.stderr
    assert drill_calls(rig) == []


def test_a_snapshot_id_is_required(rig: Rig) -> None:
    assert rig.run(DRILL).returncode != 0


@pytest.mark.parametrize("bad", ["../../etc", "ABCDEF12", "xyz", "1234567", "a" * 65, "abc; rm -rf /"])
def test_a_malformed_snapshot_id_is_rejected_before_anything_runs(rig: Rig, bad: str) -> None:
    result = rig.run(DRILL, "--snapshot", bad)
    assert stage_of(result) == "arguments"
    assert drill_calls(rig) == []


@pytest.mark.parametrize(
    "project",
    ["ytdigest", "ytdigest-prod", "other", "ytdigest-drill-", "ytdigest-drill-A", "ytdigest_drill_x", "ytdigest-drill-x y"],
)
def test_the_production_project_and_any_non_drill_name_is_refused(rig: Rig, project: str) -> None:
    before = len(rig.calls())
    result = rig.run(DRILL, "--snapshot", newest(rig), "--project", project)
    assert result.returncode != 0
    assert stage_of(result) == "target"
    assert len(rig.calls()) == before  # no docker, no restic: refused first
    assert not any("down" in c["args"] for c in rig.calls("docker"))


def test_ambient_compose_variables_cannot_retarget_the_drill(rig: Rig) -> None:
    result = rig.run(DRILL, "--snapshot", newest(rig), extra_env={"COMPOSE_PROJECT_NAME": "ytdigest"})
    assert result.returncode == 0, result.stderr
    assert {c["compose_env"] for c in rig.calls("docker")} == {None}
    assert {c["project"] for c in drill_calls(rig)} != {"ytdigest"}


@pytest.mark.parametrize("leftover", ["leftover_containers", "leftover_volumes"])
def test_a_target_that_already_exists_is_refused_and_left_alone(rig: Rig, leftover: str) -> None:
    rig.set(**{leftover: "abc123"})
    result = rig.run(DRILL, "--snapshot", newest(rig), "--project", "ytdigest-drill-taken")
    assert stage_of(result) == "target"
    assert not any("down" in c["args"] or "up" in c["args"] for c in rig.calls("docker"))


def test_a_target_database_that_already_has_data_is_refused_and_removed(rig: Rig) -> None:
    rig.set(target_objects=4)
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert stage_of(result) == "target"
    assert not any("--exit-on-error" in c["args"] for c in rig.calls("docker"))
    assert verbs(rig)[-1] == "down"


@pytest.mark.parametrize(
    ("scenario", "stage"),
    [
        ({"up_rc": 1}, "start"),
        ({"list_rc": 1}, "validate"),
        ({"restore_rc": 1}, "restore"),
        ({"ro_rc": 1}, "verify"),
        ({"psql_rc": 1}, "verify"),
    ],
    ids=["not-healthy", "unreadable-dump", "restore-error", "invalid-index", "cannot-query"],
)
def test_a_failure_exits_non_zero_names_its_stage_and_still_cleans_up(
    rig: Rig, scenario: dict[str, object], stage: str
) -> None:
    rig.set(**scenario)
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert result.returncode != 0
    assert stage_of(result) == stage, result.stderr
    assert "outcome=verified" not in result.stdout
    assert verbs(rig)[-1] == "down"
    assert not list(rig.staging.glob("drill.*"))
    assert (rig.stub / "repo" / newest(rig)).exists()  # the snapshot is kept
    assert rig.leaked(result) == []


@pytest.mark.parametrize(
    ("restored", "needle"),
    [
        (["alembic 0006_old", *MANIFEST_LINES[1:]], "-alembic 0007_example"),
        ([*MANIFEST_LINES[:2], "table videos 11", *MANIFEST_LINES[3:]], "-table videos 12"),
        ([line for line in MANIFEST_LINES if not line.startswith("table channels")], "-table channels 3"),
        ([*MANIFEST_LINES, "table surprise 1"], "+table surprise 1"),
        ([line.replace("indexes 9", "indexes 8") for line in MANIFEST_LINES], "-indexes 9"),
        ([line.replace("constraints 14", "constraints 13") for line in MANIFEST_LINES], "-constraints 14"),
    ],
    ids=["alembic", "row-count", "missing-table", "extra-table", "index-count", "constraint-count"],
)
def test_any_difference_from_the_backup_time_manifest_fails_verification(
    rig: Rig, restored: list[str], needle: str
) -> None:
    rig.set(restored_lines=restored)
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert stage_of(result) == "verify"
    assert needle in result.stderr
    assert verbs(rig)[-1] == "down"


def test_a_restored_database_without_alembic_version_fails(rig: Rig) -> None:
    rig.set(restored_lines=["table videos 12"])
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert stage_of(result) == "verify"


def test_an_unknown_snapshot_fails_before_any_container_is_started(rig: Rig) -> None:
    result = rig.run(DRILL, "--snapshot", snap_id(12345))
    assert stage_of(result) == "fetch"
    assert drill_calls(rig) == []


def test_a_restic_failure_does_not_leak_secrets(rig: Rig) -> None:
    rig.set(fail={"restore": 1}, leak=True)
    result = rig.run(DRILL, "--snapshot", newest(rig))
    assert stage_of(result) == "fetch"
    assert [s for s in SECRETS if s in result.stdout + result.stderr] == []


def test_the_drill_never_names_the_production_project_in_any_docker_call(rig: Rig) -> None:
    rig.run(DRILL, "--snapshot", newest(rig))
    after_backup = [c for c in rig.calls("docker") if "pg_dump" not in c["args"]]
    drill_only = [c for c in after_backup if c.get("project") and c["project"] != "ytdigest"]
    assert drill_only
    for c in drill_only:
        args = c["args"]
        assert isinstance(args, list)
        assert "compose.yml" not in args  # never the production compose file


# ---- --fetch-only: disaster-recovery step 1, no Docker -------------------------------------


def test_fetch_only_decrypts_into_a_new_staging_directory_and_touches_no_container(rig: Rig) -> None:
    before = len(rig.calls("docker"))
    target = rig.staging / "dr"
    result = rig.run(DRILL, "--snapshot", newest(rig), "--fetch-only", str(target))
    assert result.returncode == 0, result.stderr
    dump = Path(result.stdout.strip().splitlines()[0])
    assert dump.name == "ytdigest.dump" and dump.is_file()
    assert (target.stat().st_mode & 0o777) == 0o700
    assert "outcome=fetched" in result.stdout
    assert len(rig.calls("docker")) == before


@pytest.mark.parametrize("where", ["outside", "existing", "staging-root"])
def test_fetch_only_refuses_unsafe_targets(rig: Rig, where: str) -> None:
    if where == "outside":
        target = rig.root / "elsewhere"
    elif where == "staging-root":
        target = rig.staging
    else:
        target = rig.staging / "taken"
        target.mkdir()
    result = rig.run(DRILL, "--snapshot", newest(rig), "--fetch-only", str(target))
    assert stage_of(result) == "fetch"
