"""Hermetic one-video smoke run through the real compose stack (issue #137).

Marked `integration` (needs Docker Desktop) and `image` (the first `up --build`
builds the images, taking minutes): deselected unless run with
`pytest -m image`, which is what CI's `images` job does. Run it alone with
`pytest tests/test_pipeline_smoke_docker.py -m image`.

It brings up `compose.yml` plus `tests/fixtures/smoke/compose.smoke.yml` under a
unique `ytdigest-t137-<hex>` project (the developer's `ytdigest` project, its
volumes, `compose.override.yml` and `.env` are never touched), submits one video
through `web`'s `/api/videos`, and waits for `status: "done"`. The path is
`ingest` (recorded manual captions) -> `analyze`; no `transcribe` job exists.

Nothing touches the public network or a real LLM:

- yt-dlp is a fake `yt_dlp` package (tests/fixtures/smoke/yt_dlp), bind-mounted
  into `transcriber`, that replays the recorded metadata and VTT fixtures and
  logs every call to /tmp/fake-ytdlp.log;
- the summarizer is stub Ollama (tests/fixtures/smoke/fake_ollama.py), answered
  through the real Ollama adapter (`SUMMARIZER=ollama`);
- `transcriber`, `analyzer`, `planner` and the stub sit only on the `internal`
  network, which has no route out. No API key is used: `ANTHROPIC_API_KEY` is
  empty in `analyzer` even if the shell exports one.

Waits go through `up --wait --wait-timeout`, then one poll loop with a 120 s
deadline. `down -v --remove-orphans` runs in a `finally`.

Shared resources: the three `ytdigest-*` image tags. The criterion names host
port 8080, but that port is shared with #58's test and any running stack (and on
some machines is forwarded to the internet), so the override publishes `web` on
`127.0.0.1` with a port Docker picks, and the test asks Compose which one.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from tests.compose_stack import REPO_ROOT, Stack

pytestmark = [pytest.mark.integration, pytest.mark.image]

VIDEO_ID = "iG9CE55wbtY"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SMOKE_OVERRIDE = "tests/fixtures/smoke/compose.smoke.yml"
METADATA = REPO_ROOT / "tests/adapters/youtube/fixtures/metadata/manual_en.json"
DEADLINE_SEC = 120.0
POLL_SEC = 1.0
TLDR = "SMOKE-TLDR: creativity matters as much as literacy"
LEAKY_KEY = "sk-should-not-leak"
PINNED = {
    "SUMMARIZER": "ollama",
    "PROMPT_VERSION": "v1",
    "CHUNK_SEC": "900",
    "OVERLAP_SEC": "60",
}
PINNED_ON = ("api", "planner", "analyzer", "transcriber")
INTERNAL_ONLY = ("transcriber", "analyzer", "planner", "fake-ollama")


class SmokeStack(Stack):
    """#58's `Stack` with the smoke override merged on every call."""

    def compose(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        files: tuple[str, ...] = ("compose.yml", SMOKE_OVERRIDE),
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        return super().compose(*args, env=env, files=files, timeout=timeout)

    def logs_tail(self, service: str, lines: int = 50) -> str:
        return self.compose(
            "logs", "--no-log-prefix", "--tail", str(lines), service, timeout=120
        ).stdout

    def web_port(self) -> int:
        out = self.compose("port", "web", "8080", timeout=60).stdout.strip()
        assert out.startswith("127.0.0.1:"), f"web is not on loopback: {out!r}"
        return int(out.rpartition(":")[2])


@dataclass
class Run:
    stack: SmokeStack
    base: str
    submit_status: int
    submit_body: dict[str, Any]
    video: dict[str, Any]
    jobs: list[dict[str, Any]]
    resubmit_status: int
    resubmit_body: dict[str, Any]
    jobs_after_resubmit: list[dict[str, Any]]
    ollama_model: str


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        with err:
            raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode("utf-8", "replace")


def _video_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [j for j in jobs if j["video_id"] == VIDEO_ID]


def _all_jobs(base: str) -> list[dict[str, Any]]:
    status, doc = _http("GET", f"{base}/api/ops/jobs?limit=100")
    assert status == 200, doc
    items: list[dict[str, Any]] = doc["items"]
    return items


def _diagnostics(stack: SmokeStack, base: str, last_status: object) -> str:
    try:
        rows = [
            f"  {j['kind']} state={j['state']} attempts={j['attempts']} "
            f"error_class={j['error_class']} last_error={j['last_error']}"
            for j in _all_jobs(base)
        ]
    except Exception as err:  # noqa: BLE001 - diagnostics must not hide the failure
        rows = [f"  (jobs unavailable: {err!r})"]
    return (
        f"last observed status: {last_status!r}\n"
        "jobs:\n" + "\n".join(rows) + "\n"
        f"--- transcriber (last 50 lines) ---\n{stack.logs_tail('transcriber')}\n"
        f"--- analyzer (last 50 lines) ---\n{stack.logs_tail('analyzer')}"
    )


def _wait_until_done(stack: SmokeStack, base: str, started: float) -> dict[str, Any]:
    """Poll once a second until `done`; fail fast on `failed`, `unavailable` or an error."""
    last: object = None
    while True:
        status, doc = _http("GET", f"{base}/api/videos/{VIDEO_ID}")
        if status == 200:
            last = doc["status"]
            if last == "done":
                assert isinstance(doc, dict)
                return doc
            if last in ("failed", "unavailable"):
                pytest.fail(
                    f"video ended as {last!r}\n{_diagnostics(stack, base, last)}"
                )
            errors = [
                j for j in _video_jobs(_all_jobs(base)) if j["last_error"] is not None
            ]
            if errors:
                pytest.fail(
                    f"a job reported last_error (status {last!r})\n"
                    f"{_diagnostics(stack, base, last)}"
                )
        elif status != 404:
            pytest.fail(
                f"GET video returned {status}: {doc}\n{_diagnostics(stack, base, last)}"
            )
        if time.monotonic() - started > DEADLINE_SEC:
            pytest.fail(
                f"timeout: video not done within {DEADLINE_SEC:.0f} s\n"
                f"{_diagnostics(stack, base, last)}"
            )
        time.sleep(POLL_SEC)


def _leftovers(project: str) -> tuple[str, str]:
    label = f"label=com.docker.compose.project={project}"

    def lines(*cmd: str) -> str:
        return subprocess.run(
            ["docker", *cmd], capture_output=True, text=True, timeout=120, check=True
        ).stdout.strip()

    return (
        lines("ps", "-a", "--filter", label, "-q"),
        lines("volume", "ls", "--filter", label, "-q"),
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    project = f"ytdigest-t137-{uuid.uuid4().hex[:8]}"
    stack = SmokeStack(project, tmp_path_factory.mktemp("smoke"))
    # Exported variables must not leak in: the override pins what matters.
    with pytest.MonkeyPatch.context() as env:
        env.setenv("ANTHROPIC_API_KEY", LEAKY_KEY)
        env.setenv("CHUNK_SEC", "1200")
        try:
            result = stack.up()
            assert result.returncode == 0, result.stderr
            base = f"http://127.0.0.1:{stack.web_port()}"
            config = json.loads(stack.compose("config", "--format", "json").stdout)
            model = config["services"]["analyzer"]["environment"]["OLLAMA_MODEL"]

            started = time.monotonic()
            status, body = _http("POST", f"{base}/api/videos", {"url": URL})
            video = _wait_until_done(stack, base, started) if status == 202 else {}
            jobs = _all_jobs(base)
            again_status, again_body = _http("POST", f"{base}/api/videos", {"url": URL})
            yield Run(
                stack=stack,
                base=base,
                submit_status=status,
                submit_body=body,
                video=video,
                jobs=jobs,
                resubmit_status=again_status,
                resubmit_body=again_body,
                jobs_after_resubmit=_all_jobs(base),
                ollama_model=model,
            )
        finally:
            stack.compose("down", "-v", "--remove-orphans")
    containers, volumes = _leftovers(project)
    assert containers == "", f"containers left behind: {containers}"
    assert volumes == "", f"volumes left behind: {volumes}"


# --- shape and isolation ------------------------------------------------------


def test_project_name_and_files_never_include_the_developers_overlay(run: Run) -> None:
    assert run.stack.project.startswith("ytdigest-t137-")
    assert len(run.stack.project.removeprefix("ytdigest-t137-")) == 8


def test_override_pins_values_so_an_exported_shell_variable_cannot_change_them(
    run: Run,
) -> None:
    config = json.loads(run.stack.compose("config", "--format", "json").stdout)[
        "services"
    ]
    for service in PINNED_ON:
        env = config[service]["environment"]
        for key, value in PINNED.items():
            assert env[key] == value, (service, key)
    analyzer = config["analyzer"]["environment"]
    assert analyzer["OLLAMA_HOST"] == "http://fake-ollama:11434"
    assert analyzer["ANTHROPIC_API_KEY"] == ""


def test_api_key_exported_in_the_pytest_process_does_not_reach_the_analyzer(
    run: Run,
) -> None:
    result = run.stack.exec("analyzer", "sh", "-c", 'test -z "$ANTHROPIC_API_KEY"')
    assert result.returncode == 0, "ANTHROPIC_API_KEY is set inside analyzer"


@pytest.mark.parametrize("service", INTERNAL_ONLY)
def test_pipeline_services_sit_only_on_the_internal_network(
    run: Run, service: str
) -> None:
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{json .NetworkSettings.Networks}}",
            run.stack.container_id(service),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    assert set(json.loads(out)) == {f"{run.stack.project}_internal"}


def test_transcriber_runs_but_cannot_reach_youtube(run: Run) -> None:
    control = run.stack.exec("transcriber", "python", "-c", "print('ok')")
    assert control.returncode == 0 and control.stdout.strip() == "ok", control.stderr
    blocked = run.stack.exec(
        "transcriber",
        "python",
        "-c",
        "import socket; socket.create_connection(('www.youtube.com', 443), timeout=5)",
    )
    assert blocked.returncode != 0
    assert "OSError" in blocked.stderr or "gaierror" in blocked.stderr, blocked.stderr


def test_the_fake_yt_dlp_served_exactly_the_manual_caption_calls(run: Run) -> None:
    out = run.stack.exec("transcriber", "cat", "/tmp/fake-ytdlp.log")
    assert out.returncode == 0, out.stderr
    calls = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    dumps = [c for c in calls if "--dump-json" in c]
    subs = [c for c in calls if "--write-subs" in c]
    assert len(dumps) == 1 and len(subs) == 1
    assert len(calls) == 2, calls
    for call in (dumps[0], subs[0]):
        assert call[-1] == URL
    assert "en" == subs[0][subs[0].index("--sub-langs") + 1]
    assert not any("--write-auto-subs" in c for c in calls)


def test_analyzer_startup_probe_reached_the_stub(run: Run) -> None:
    assert "analyzer.ollama_ready" in run.stack.logs_tail("analyzer", 1000)


# --- the run ------------------------------------------------------------------


def test_submit_returns_202_created_with_a_job_id(run: Run) -> None:
    assert run.submit_status == 202, run.submit_body
    assert run.submit_body["created"] is True
    assert isinstance(run.submit_body["job_id"], int)


def test_video_is_done_with_the_recorded_manual_transcript(run: Run) -> None:
    video = run.video
    metadata = json.loads(METADATA.read_text(encoding="utf-8"))
    assert video["status"] == "done"
    assert video["origin"] == "adhoc"
    assert video["title"] == metadata["title"]
    transcript = video["transcript"]
    assert transcript["source"] == "youtube_manual"
    assert transcript["language"] == "en"
    assert transcript["segment_count"] > 0


def test_analysis_comes_from_the_stub_through_the_real_adapter(run: Run) -> None:
    analysis = run.video["analysis"]
    assert analysis is not None
    assert analysis["prompt_version"] == "v1"
    assert analysis["transcript_source"] == "youtube_manual"
    assert analysis["model"] == run.ollama_model
    assert analysis["tldr"] == TLDR


def test_stub_claims_keep_roster_speakers_and_coerce_invented_ones(run: Run) -> None:
    speakers = {c["speaker"] for c in run.video["analysis"]["claims"]}
    assert "Ken Robinson" in speakers
    assert "unknown" in speakers
    assert "Not In Roster" not in speakers


def test_jobs_are_one_done_ingest_and_one_done_analyze_and_no_transcribe(
    run: Run,
) -> None:
    jobs = _video_jobs(run.jobs)
    assert sorted(j["kind"] for j in jobs) == ["analyze", "ingest"]
    for job in jobs:
        assert job["state"] == "done", job
        assert job["attempts"] == 1, job
        assert job["error_class"] is None, job
        assert job["priority"] == 10, job
    analyze = next(j for j in jobs if j["kind"] == "analyze")
    assert analyze["dedupe_key"] == "v1:ollama"


def test_resubmitting_a_done_video_returns_200_with_the_same_job_and_no_new_job(
    run: Run,
) -> None:
    assert run.resubmit_status == 200, run.resubmit_body
    assert run.resubmit_body["created"] is False
    assert run.resubmit_body["job_id"] == run.submit_body["job_id"]
    assert len(_video_jobs(run.jobs_after_resubmit)) == 2
    assert len(run.jobs_after_resubmit) == 2
