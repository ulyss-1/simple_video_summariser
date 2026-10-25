"""Hermetic one-video smoke of the Whisper path (issue #262, follow-up to #137).

Marked `integration` (needs Docker Desktop) and `image` (the first `up --build`
builds the images, taking minutes): deselected unless run with
`pytest -m image`. Run it alone with
`pytest tests/test_pipeline_whisper_smoke_docker.py -m image`.

Where #137 takes a video with manual captions (`ingest` -> `analyze`), this one
takes a video with none through `ingest` -> `transcribe` -> `analyze`, with the
real faster-whisper `tiny` model transcribing a committed synthetic speech clip
(tests/fixtures/smoke/speech.opus). The analysis comes from #137's stub Ollama.

The stack is `compose.yml` + `compose.smoke.yml` + `compose.whisper.yml` under a
unique `ytdigest-t262-<hex>` project. The one network step is the model seed: a
one-shot `whisper-seed` container on `egress` downloads `tiny` into the
project's own `whisper-models` volume after `up` and before the video is
submitted. From then on no service of the pipeline can leave `internal`, and
`HF_HUB_OFFLINE=1` makes a missing model fail at once.

`down -v --remove-orphans` runs in a `finally`; the project's containers and
volumes must all be gone afterwards.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from tests.compose_stack import (
    SmokeStack,
    _all_jobs,
    _http,
    _leftovers,
    _video_jobs,
    _wait_until_done,
)
from tests.test_pipeline_smoke_docker import (
    INTERNAL_ONLY,
    LEAKY_KEY,
    PINNED,
    PINNED_ON,
    SMOKE_OVERRIDE,
    TLDR,
)

pytestmark = [pytest.mark.integration, pytest.mark.image]

VIDEO_ID = "BZiu46G4ukc"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
WHISPER_OVERRIDE = "tests/fixtures/smoke/compose.whisper.yml"
FILES = ("compose.yml", SMOKE_OVERRIDE, WHISPER_OVERRIDE)
DEADLINE_SEC = 180.0
SEED_TIMEOUT_SEC = 600
WHISPER_PINNED = {
    "WHISPER_MODEL": "tiny",
    "WHISPER_COMPUTE": "int8",
    "WHISPER_THREADS": "2",
    "AUDIO_KEEP": "1",
    "HF_HUB_OFFLINE": "1",
}
MODEL_GLOB = "/models/hub/models--Systran--faster-whisper-tiny/snapshots/*/model.bin"


@dataclass
class Run:
    stack: SmokeStack
    base: str
    submit_status: int
    submit_body: dict[str, Any]
    video: dict[str, Any]
    jobs: list[dict[str, Any]]
    ollama_model: str
    containers_after_seed: dict[str, dict[str, object]]


def _seed_model(stack: SmokeStack) -> None:
    """Download `tiny` into the whisper-models volume; fail at once if it cannot."""
    hint = (
        "seeding the `tiny` Whisper model failed; this is the only step that "
        "needs huggingface.co"
    )
    try:
        seed = stack.compose("run", "--rm", "whisper-seed", timeout=SEED_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as err:
        pytest.fail(f"{hint} (timed out after {SEED_TIMEOUT_SEC} s)\n{err.stderr!r}")
    if seed.returncode != 0:
        tail = "\n".join((seed.stdout + seed.stderr).splitlines()[-20:])
        pytest.fail(f"{hint} (exit {seed.returncode})\n{tail}")
    found = stack.exec("transcriber", "sh", "-c", f"ls {MODEL_GLOB}")
    if found.returncode != 0:
        pytest.fail(
            "tiny Whisper model not found in the whisper-models volume "
            f"(looked for {MODEL_GLOB})\n{found.stdout}{found.stderr}"
        )


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    project = f"ytdigest-t262-{uuid.uuid4().hex[:8]}"
    stack = SmokeStack(project, tmp_path_factory.mktemp("whisper-smoke"), FILES)
    # Exported variables must not leak in: the override layers pin what matters.
    with pytest.MonkeyPatch.context() as env:
        env.setenv("ANTHROPIC_API_KEY", LEAKY_KEY)
        env.setenv("CHUNK_SEC", "1200")
        env.setenv("WHISPER_MODEL", "large-v3")
        env.setenv("AUDIO_KEEP", "0")
        try:
            result = stack.up()
            assert result.returncode == 0, result.stderr
            _seed_model(stack)
            after_seed = stack.containers()
            base = f"http://127.0.0.1:{stack.web_port()}"
            config = json.loads(stack.compose("config", "--format", "json").stdout)
            model = config["services"]["analyzer"]["environment"]["OLLAMA_MODEL"]

            started = time.monotonic()
            status, body = _http("POST", f"{base}/api/videos", {"url": URL})
            video = (
                _wait_until_done(
                    stack, base, started, video_id=VIDEO_ID, deadline_sec=DEADLINE_SEC
                )
                if status == 202
                else {}
            )
            yield Run(
                stack=stack,
                base=base,
                submit_status=status,
                submit_body=body,
                video=video,
                jobs=_all_jobs(base),
                ollama_model=model,
                containers_after_seed=after_seed,
            )
        finally:
            stack.compose("down", "-v", "--remove-orphans")
    containers, volumes = _leftovers(project)
    assert containers == "", f"containers left behind: {containers}"
    assert volumes == "", f"volumes left behind: {volumes}"


def _networks(container_id: str) -> set[str]:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return set(json.loads(out))


# --- shape, pinning and isolation ----------------------------------------------


def test_project_name_is_unique_and_apart_from_the_developers_stack(run: Run) -> None:
    assert run.stack.project.startswith("ytdigest-t262-")
    assert len(run.stack.project.removeprefix("ytdigest-t262-")) == 8


def test_layer_pins_whisper_values_and_keeps_the_smoke_pins(run: Run) -> None:
    config = json.loads(run.stack.compose("config", "--format", "json").stdout)[
        "services"
    ]
    env = config["transcriber"]["environment"]
    for key, value in WHISPER_PINNED.items():
        assert env[key] == value, key
    for service in PINNED_ON:
        for key, value in PINNED.items():
            assert config[service]["environment"][key] == value, (service, key)


def test_seed_left_no_container_and_nothing_on_egress_is_running(run: Run) -> None:
    assert "whisper-seed" not in run.containers_after_seed
    ids = run.stack.compose("ps", "-q").stdout.split()
    assert ids
    for container_id in ids:
        assert f"{run.stack.project}_egress" not in _networks(container_id)


def test_model_lives_in_the_projects_own_volume(run: Run) -> None:
    out = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={run.stack.project}",
            "--format",
            "{{.Name}}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split()
    assert f"{run.stack.project}_whisper-models" in out


def test_api_key_exported_in_the_pytest_process_does_not_reach_the_analyzer(
    run: Run,
) -> None:
    result = run.stack.exec("analyzer", "sh", "-c", 'test -z "$ANTHROPIC_API_KEY"')
    assert result.returncode == 0, "ANTHROPIC_API_KEY is set inside analyzer"


@pytest.mark.parametrize("service", INTERNAL_ONLY)
def test_pipeline_services_sit_only_on_the_internal_network(
    run: Run, service: str
) -> None:
    networks = _networks(run.stack.container_id(service))
    assert networks == {f"{run.stack.project}_internal"}


@pytest.mark.parametrize("host", ["huggingface.co", "www.youtube.com"])
def test_transcriber_runs_but_cannot_reach_the_public_network(
    run: Run, host: str
) -> None:
    control = run.stack.exec("transcriber", "python", "-c", "print('ok')")
    assert control.returncode == 0 and control.stdout.strip() == "ok", control.stderr
    blocked = run.stack.exec(
        "transcriber",
        "python",
        "-c",
        f"import socket; socket.create_connection(({host!r}, 443), timeout=5)",
    )
    assert blocked.returncode != 0
    assert "OSError" in blocked.stderr or "gaierror" in blocked.stderr, blocked.stderr


def test_the_fake_yt_dlp_served_exactly_metadata_and_audio_calls(run: Run) -> None:
    out = run.stack.exec("transcriber", "cat", "/tmp/fake-ytdlp.log")
    assert out.returncode == 0, out.stderr
    calls = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    assert len(calls) == 2, calls
    assert len([c for c in calls if "--dump-json" in c]) == 1
    audio = [c for c in calls if "-f" in c and c[c.index("-f") + 1] == "bestaudio"]
    assert len(audio) == 1
    for call in calls:
        assert call[-1] == URL
        assert "--write-subs" not in call and "--write-auto-subs" not in call


# --- the run -------------------------------------------------------------------


def test_submit_returns_202_created_with_a_job_id(run: Run) -> None:
    assert run.submit_status == 202, run.submit_body
    assert run.submit_body["created"] is True
    assert isinstance(run.submit_body["job_id"], int)


def test_jobs_are_one_done_ingest_transcribe_and_analyze(run: Run) -> None:
    jobs = _video_jobs(run.jobs, VIDEO_ID)
    assert sorted(j["kind"] for j in jobs) == ["analyze", "ingest", "transcribe"]
    for job in jobs:
        assert job["state"] == "done", job
        assert job["attempts"] == 1, job
        assert job["error_class"] is None, job
        assert job["priority"] == 10, job
    analyze = next(j for j in jobs if j["kind"] == "analyze")
    assert analyze["dedupe_key"] == "v1:ollama"


def test_transcript_was_made_by_whisper(run: Run) -> None:
    assert run.video["status"] == "done"
    transcript = run.video["transcript"]
    assert transcript["source"] == "whisper"
    assert transcript["segment_count"] >= 1
    assert isinstance(transcript["language"], str) and transcript["language"]


def test_analysis_comes_from_the_stub_and_records_the_whisper_source(run: Run) -> None:
    analysis = run.video["analysis"]
    assert analysis is not None
    assert analysis["transcript_source"] == "whisper"
    assert analysis["prompt_version"] == "v1"
    assert analysis["model"] == run.ollama_model
    assert analysis["tldr"] == TLDR


def test_audio_is_kept_and_no_temporary_file_is_left(run: Run) -> None:
    kept = run.stack.exec(
        "transcriber", "stat", "-c", "%s", "/data/audio/BZ/BZiu46G4ukc.opus"
    )
    assert kept.returncode == 0, kept.stderr
    assert int(kept.stdout.strip()) > 0
    for directory, pattern in (
        ("/data/audio", ".download-*"),
        ("/data/audio/BZ", ".tmp-*"),
    ):
        found = run.stack.exec("transcriber", "find", directory, "-name", pattern)
        assert found.returncode == 0, found.stderr
        assert found.stdout.strip() == "", found.stdout
