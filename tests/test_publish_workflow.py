"""The publish workflow for the prebuilt dependency images (#368): static checks.

Reads the YAML text with regexes (no parser is a dependency), in the style of
tests/test_ci_workflow.py. Nothing here calls GitHub or Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish-deps-images.yml"
CI = WORKFLOWS / "ci.yml"
APPROVED_DOCKER_ACTIONS = {
    "docker/login-action",
    "docker/setup-buildx-action",
    "docker/build-push-action",
}
BUILD_STEPS = ("Build and push ytdigest-deps", "Build and push ytdigest-whisper-deps")
MOVE = "Move latest and write the summary"


def _text() -> str:
    return PUBLISH.read_text()


def _steps() -> list[str]:
    body = _text().split("    steps:\n", 1)[1]
    return re.split(r"(?m)^      - ", body)[1:]


def _step(name: str) -> str:
    return next(s for s in _steps() if s.startswith(f"name: {name}"))


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_triggers_are_master_push_on_the_hash_inputs_a_monthly_schedule_and_dispatch() -> None:
    text = _text()
    on = text.split("\npermissions:", 1)[0]
    assert "pull_request" not in text and "pull_request_target" not in text
    assert "branches: [master]" in on
    paths = re.search(r"paths:\n((?:      - .*\n)+)", on)
    assert paths
    assert set(re.findall(r"""["']([^"']+)["']""", paths.group(1))) == {
        "requirements.backend.lock",
        "requirements.whisper.lock",
        "ops/Dockerfile.backend",
        "ops/Dockerfile.whisper",
        "scripts/deps_image_tag.py",
        ".github/workflows/publish-deps-images.yml",
    }
    cron = re.search(r"""cron: ["'](\S+) (\S+) (\S+) (\S+) (\S+)["']""", on)
    assert cron
    minute, hour, day, month, weekday = cron.groups()
    # Monthly, on the 1st: when the YYYY-MM stamp rolls over.
    assert (day, month, weekday) == ("1", "*", "*")
    others = {
        m.group(1)
        for f in WORKFLOWS.glob("*.yml")
        if f != PUBLISH
        for m in re.finditer(r"""cron: ["'](\d+) """, f.read_text())
    }
    assert minute not in others and hour.isdigit()
    assert "workflow_dispatch:" in on


def test_publishing_job_is_restricted_to_this_repository_and_master() -> None:
    assert (
        "if: github.repository == 'ulyss-1/simple_video_summariser' "
        "&& github.ref == 'refs/heads/master'"
    ) in _text()
    assert len(re.findall(r"(?m)^  [\w-]+:$", _text().split("\njobs:\n", 1)[1])) == 1


def test_permissions_are_read_only_except_packages_write_on_the_one_job() -> None:
    text = _text()
    assert re.search(r"(?m)^permissions:\n  contents: read\n", text)
    assert text.count("packages: write") == 1
    job = text.split("\njobs:\n", 1)[1]
    assert re.search(r"(?m)^    permissions:\n      contents: read\n      packages: write\n", job)
    assert "packages: write" not in CI.read_text()
    assert "timeout-minutes:" in job


def test_only_the_job_token_is_used_and_only_to_log_in_and_to_prune() -> None:
    assert re.findall(r"secrets\.(\w+)", _text()) == ["GITHUB_TOKEN", "GITHUB_TOKEN"]
    login = _step("Log in to ghcr.io")
    assert "docker login ghcr.io" in login and "--password-stdin" in login
    assert "secrets.GITHUB_TOKEN" in login
    for step in _steps():
        if step.startswith(("name: Log in", "name: Delete old versions")):
            continue
        assert "secrets." not in step and "GITHUB_TOKEN" not in step, step[:40]


def test_builds_use_the_repo_root_and_pass_no_build_arguments_secrets_or_ssh() -> None:
    builds = [s for s in _steps() if "buildx build" in _code(s)]
    assert len(builds) == 2
    for step in builds:
        code = _code(step)
        assert "--push" in code and "--provenance=false" in code
        assert re.search(r'--tag "\$ref:\$TAG" \.\n', code), "context must be the repo root"
        for flag in ("--build-arg", "--secret", "--ssh", "--cache-from", "--cache-to"):
            assert flag not in code
        assert "org.opencontainers.image.source=$SOURCE_URL" in code
        assert "deps-inputs-hash=$INPUTS_HASH" in code
    assert "--target deps" in _step(BUILD_STEPS[0])
    whisper = _step(BUILD_STEPS[1])
    assert "--target whisper-deps" in whisper
    assert "ytdigest-deps=docker-image://$REGISTRY/ytdigest-deps@$DEPS_DIGEST" in whisper


def test_an_existing_hash_tag_is_never_rebuilt_and_latest_is_moved_afterwards() -> None:
    for name in BUILD_STEPS:
        step = _step(name)
        assert step.index("imagetools inspect") < step.index("buildx build")
        assert "skipped=true" in step
    names = [s.split("\n", 1)[0] for s in _steps()]
    assert names.index(f"name: {MOVE}") > names.index(f"name: {BUILD_STEPS[1]}")
    move = _step(MOVE)
    assert "imagetools create --tag" in move and "GITHUB_STEP_SUMMARY" in move


def test_publishes_serially_and_never_cancels_a_running_publish() -> None:
    assert re.search(
        r"(?m)^concurrency:\n  group: publish-deps-images\n  cancel-in-progress: false$", _text()
    )


def test_cleanup_runs_last_for_both_packages() -> None:
    step = _step("Delete old versions")
    assert "prune --package ytdigest-deps" in step
    assert "prune --package ytdigest-whisper-deps" in step
    assert _steps()[-1] == step


def test_actions_are_pinned_by_sha_first_party_or_one_of_the_three_approved() -> None:
    uses = re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", _text())
    ci_uses = set(re.findall(r"uses:\s*(\S+)", CI.read_text()))
    assert uses
    for ref, rest in uses:
        name = ref.split("@")[0]
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not SHA-pinned"
        assert re.search(r"# v\d", rest), f"{ref} lacks a version comment"
        assert name.startswith("actions/") or name in APPROVED_DOCKER_ACTIONS, name
        if name.startswith("actions/"):
            assert ref in ci_uses, f"{ref} is not the SHA ci.yml uses"


def test_no_workflow_uses_pull_request_target_and_ci_never_pushes() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        assert "pull_request_target" not in workflow.read_text(), workflow.name
    assert "docker push" not in CI.read_text() and "--push" not in CI.read_text()


def test_the_workflow_runs_the_shared_script_for_tags_and_cleanup() -> None:
    assert "python scripts/deps_image_tag.py tags" in _step("Compute the tags")
    assert "gh api" not in _text(), "the deletion lives in the script, where it is tested"
