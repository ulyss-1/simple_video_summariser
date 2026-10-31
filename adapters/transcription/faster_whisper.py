"""faster-whisper implementation of the ``Transcriber`` port (issue #20).

CPU only: int8 compute, beam size 5, VAD filtering (D3, architecture.md 7.2).
Importing this module never imports ``faster_whisper`` - only the first
``transcribe()`` call does, lazily, so an image without
``requirements.whisper.txt`` installed can still import it (task #56 builds
the transcriber image separately). Constructing a transcriber never loads
the model either; the first ``transcribe()`` call loads it once, and later
calls on the same instance reuse it.
"""

from __future__ import annotations

import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from importlib import metadata
from time import perf_counter
from typing import Any

import structlog

from common.config import get_settings
from common.errors import (
    BugError,
    Cancelled,
    ResourceError,
    ToolFailureError,
    TransientNetworkError,
)
from common.models import AudioRef, Segment, TranscriptResult
from common.safe_url import redact

_log = structlog.get_logger(__name__)

#: Decoding options fixed by D3 - not configurable per the issue's AC.
_BEAM_SIZE = 5
_VAD_FILTER = True

_REQUIREMENTS_FILE = "requirements.whisper.txt"

#: The module the parent runs as the child (issue #324).
CHILD_MODULE = "adapters.transcription.faster_whisper_child"
DEFAULT_POLL_INTERVAL_SEC = 0.5
DEFAULT_REAP_TIMEOUT_SEC = 5.0
DEFAULT_PARENT_WATCH_SEC = 2.0
#: How much of the child's stderr is kept for error messages.
STDERR_TAIL_BYTES = 2048

#: Child -> parent message types (JSON lines on the reply pipe).
MSG_PROGRESS = "progress"
MSG_DONE = "done"
MSG_ERROR = "error"
#: ``error`` kinds, each mapped to the class the in-process path raises.
KIND_TOOL = "tool"
KIND_NETWORK = "network"
KIND_MEMORY = "memory"
KIND_UNEXPECTED = "unexpected"

#: The codes Whisper accepts (faster-whisper rejects anything else, including
#: region-tagged ones like ``en-US``).
_WHISPER_LANGUAGES = frozenset(
    [
        "af",
        "am",
        "ar",
        "as",
        "az",
        "ba",
        "be",
        "bg",
        "bn",
        "bo",
        "br",
        "bs",
        "ca",
        "cs",
        "cy",
        "da",
        "de",
        "el",
        "en",
        "es",
        "et",
        "eu",
        "fa",
        "fi",
        "fo",
        "fr",
        "gl",
        "gu",
        "ha",
        "haw",
        "he",
        "hi",
        "hr",
        "ht",
        "hu",
        "hy",
        "id",
        "is",
        "it",
        "ja",
        "jw",
        "ka",
        "kk",
        "km",
        "kn",
        "ko",
        "la",
        "lb",
        "ln",
        "lo",
        "lt",
        "lv",
        "mg",
        "mi",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "mt",
        "my",
        "ne",
        "nl",
        "nn",
        "no",
        "oc",
        "pa",
        "pl",
        "ps",
        "pt",
        "ro",
        "ru",
        "sa",
        "sd",
        "si",
        "sk",
        "sl",
        "sn",
        "so",
        "sq",
        "sr",
        "su",
        "sv",
        "sw",
        "ta",
        "te",
        "tg",
        "th",
        "tk",
        "tl",
        "tr",
        "tt",
        "uk",
        "ur",
        "uz",
        "vi",
        "yi",
        "yo",
        "zh",
        "yue",
    ]
)

#: Old ISO 639-1 codes YouTube still emits, and Javanese, which Whisper spells "jw".
_LANGUAGE_ALIASES = {"iw": "he", "in": "id", "ji": "yi", "jv": "jw"}


def whisper_language(code: str | None) -> str | None:
    """Reduce a BCP-47 tag (``en-US``, ``pt_BR``, ``zh-Hans``) to a Whisper code.

    ``None`` for an empty or unknown code, which makes Whisper detect the
    language instead of failing the job.
    """
    if code is None:
        return None
    primary = code.strip().lower().replace("_", "-").split("-", 1)[0]
    primary = _LANGUAGE_ALIASES.get(primary, primary)
    return primary if primary in _WHISPER_LANGUAGES else None


class FasterWhisperTranscriber:
    """``Transcriber`` backed by faster-whisper on CPU (architecture.md 3, 7.2)."""

    def __init__(
        self,
        model: str | None = None,
        compute_type: str | None = None,
        threads: int | None = None,
        *,
        child_command: Sequence[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
        reap_timeout: float = DEFAULT_REAP_TIMEOUT_SEC,
        parent_watch_sec: float = DEFAULT_PARENT_WATCH_SEC,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")
        # Any explicit arg is used as-is; only a missing one reads settings,
        # so importing/constructing this class never forces get_settings()
        # to validate the environment (common/config.py's own contract).
        if model is None or compute_type is None or threads is None:
            settings = get_settings()
            model = settings.WHISPER_MODEL if model is None else model
            compute_type = (
                settings.WHISPER_COMPUTE if compute_type is None else compute_type
            )
            threads = settings.WHISPER_THREADS if threads is None else threads

        self._model_name = model
        self._compute_type = compute_type
        self._threads = threads
        self._loaded_model: Any = None
        self._engine_version: str = "unknown"
        self._child_command = (
            list(child_command)
            if child_command is not None
            else [sys.executable, "-m", CHILD_MODULE]
        )
        self._poll_interval = poll_interval
        self._reap_timeout = reap_timeout
        self._parent_watch_sec = parent_watch_sec
        self._child_slot = threading.Lock()  # at most one child at a time

    def transcribe(
        self,
        audio: AudioRef,
        *,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> TranscriptResult:
        """Speech-to-text for ``audio``.

        Without ``cancelled`` the work runs in this process and the model is
        cached on the instance (the CLI path). With it the work runs in a child
        process that is killed when ``cancelled()`` turns true (``Cancelled``
        is raised, issue #324); the model then lives only in the child.
        ``on_progress`` is called on the calling thread in both modes.
        """
        if cancelled is None:
            return self._transcribe_in_process(audio, language, on_progress)
        return self._transcribe_in_child(audio, language, on_progress, cancelled)

    def _transcribe_in_child(
        self,
        audio: AudioRef,
        language: str | None,
        on_progress: Callable[[float], None] | None,
        cancelled: Callable[[], bool],
    ) -> TranscriptResult:
        if not self._child_slot.acquire(blocking=False):
            raise BugError("a faster-whisper child is already running in this process")
        try:
            request = {
                "model": self._model_name,
                "compute_type": self._compute_type,
                "threads": self._threads,
                "audio": {
                    "rel_path": audio.rel_path,
                    "bytes": audio.bytes,
                    "duration_sec": audio.duration_sec,
                },
                "language": language,
                "parent_watch_sec": self._parent_watch_sec,
            }
            return _ChildRun(
                self._child_command,
                request,
                on_progress=on_progress,
                cancelled=cancelled,
                poll_interval=self._poll_interval,
                reap_timeout=self._reap_timeout,
            ).run()
        finally:
            self._child_slot.release()

    def _transcribe_in_process(
        self,
        audio: AudioRef,
        language: str | None,
        on_progress: Callable[[float], None] | None,
    ) -> TranscriptResult:
        model, engine_version, load_sec = self._ensure_model()
        language = whisper_language(language)

        audio_path = get_settings().AUDIO_DIR / audio.rel_path

        start = perf_counter()
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=_BEAM_SIZE,
                vad_filter=_VAD_FILTER,
            )
        except MemoryError:
            raise
        except Exception as exc:
            raise ToolFailureError(
                f"faster-whisper failed to transcribe {audio_path}: {exc}"
            ) from exc

        segments = list(_collect_segments(raw_segments, audio_path, on_progress))
        transcribe_sec = perf_counter() - start

        resolved_language = language if language is not None else info.language
        audio_sec = audio.duration_sec
        rtf = (transcribe_sec / audio_sec) if audio_sec else None

        engine_meta: dict[str, Any] = {
            "engine": "faster-whisper",
            "engine_version": engine_version,
            "model": self._model_name,
            "compute_type": self._compute_type,
            "beam_size": _BEAM_SIZE,
            "vad": _VAD_FILTER,
            "threads": self._threads,
            "audio_sec": audio_sec,
            "load_sec": load_sec,
            "transcribe_sec": transcribe_sec,
            "rtf": rtf,
        }

        return TranscriptResult(
            segments=tuple(segments),
            language=resolved_language,
            engine_meta=engine_meta,
        )

    def check_available(self) -> None:
        """Raise ``ToolFailureError`` if faster-whisper is not installed; loads nothing."""
        _import_faster_whisper()

    def _ensure_model(self) -> tuple[Any, str, float]:
        """Load the model on first use; reuse it after.

        Returns ``(model, engine_version, load_sec)``. ``load_sec`` is 0.0
        on every call after the first, since no loading happened.
        """
        if self._loaded_model is not None:
            return self._loaded_model, self._engine_version, 0.0

        faster_whisper = _import_faster_whisper()

        start = perf_counter()
        try:
            model = faster_whisper.WhisperModel(
                self._model_name,
                device="cpu",
                compute_type=self._compute_type,
                cpu_threads=self._threads,
            )
        except MemoryError:
            raise
        except OSError as exc:
            # Model download (huggingface_hub) failing offline surfaces as a
            # connection/socket error here; anything else at load time (a bad
            # model name, a corrupt cache entry) is left unclassified.
            raise TransientNetworkError(
                f"failed to load faster-whisper model {self._model_name!r}: {exc}"
            ) from exc
        load_sec = perf_counter() - start

        self._loaded_model = model
        self._engine_version = _engine_version(faster_whisper)
        return self._loaded_model, self._engine_version, load_sec


def _import_faster_whisper() -> Any:
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ToolFailureError(
            f"faster-whisper is not installed; install {_REQUIREMENTS_FILE}"
        ) from exc
    return faster_whisper


def _collect_segments(
    raw_segments: Iterator[Any],
    audio_path: object,
    on_progress: Callable[[float], None] | None,
) -> Iterator[Segment]:
    """Turn faster-whisper's segment objects into ``Segment``s.

    ``on_progress`` is called after each decoded segment, outside of any
    error handling here, so an exception it raises (e.g. ``Cancelled``)
    stops iteration and propagates unchanged rather than being reclassified
    as a tool failure.
    """
    iterator = iter(raw_segments)
    while True:
        try:
            raw = next(iterator)
        except StopIteration:
            return
        except MemoryError:
            raise
        except Exception as exc:
            raise ToolFailureError(
                f"faster-whisper failed to transcribe {audio_path}: {exc}"
            ) from exc

        text = raw.text.strip()
        if text:
            yield Segment(start=raw.start, end=raw.end, text=text)
        if on_progress is not None:
            on_progress(raw.end)


def _engine_version(module: Any) -> str:
    version = getattr(module, "__version__", None)
    if version:
        return str(version)
    try:
        return metadata.version("faster-whisper")
    except metadata.PackageNotFoundError:
        return "unknown"


# -- the child process (issue #324) -------------------------------------------


def _result_from_done(message: dict[str, Any]) -> TranscriptResult:
    try:
        segments = tuple(
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]))
            for s in message["segments"]
        )
        language = message["language"]
        engine_meta = message["engine_meta"]
        if not isinstance(language, str) or not isinstance(engine_meta, dict):
            raise TypeError("language or engine_meta has the wrong type")
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolFailureError(f"faster-whisper child sent a bad 'done' message: {exc!r}") from exc
    return TranscriptResult(segments=segments, language=language, engine_meta=engine_meta)


class _ChildRun:
    """One child process: spawn, relay its messages, and never leave it running.

    Everything the child sends comes through a dedicated pipe (``reply_fd`` in
    the request), so library output on its stdout/stderr cannot corrupt the
    protocol. Two daemon threads only move bytes (the reply pipe into a queue,
    stderr into a bounded tail); every decision, ``on_progress`` and
    ``cancelled`` included, runs on the calling thread.
    """

    def __init__(
        self,
        command: Sequence[str],
        request: dict[str, Any],
        *,
        on_progress: Callable[[float], None] | None,
        cancelled: Callable[[], bool],
        poll_interval: float,
        reap_timeout: float,
    ) -> None:
        self._command = list(command)
        self._request = request
        self._on_progress = on_progress
        self._cancelled = cancelled
        self._poll_interval = poll_interval
        self._reap_timeout = reap_timeout
        self._messages: queue.Queue[bytes | None] = queue.Queue()
        self._stop_reader = threading.Event()
        self._stderr_tail = bytearray()
        self._tail_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def run(self) -> TranscriptResult:
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=(write_fd,),
                start_new_session=True,  # never fork: the parent has threads and DB connections
            )
        except OSError as exc:
            os.close(read_fd)
            raise ToolFailureError(f"could not start the faster-whisper child: {exc}") from exc
        finally:
            os.close(write_fd)  # pass_fds keeps the same number in the child
        try:
            self._start_threads(proc, read_fd)
            self._send_request(proc, write_fd)
            return self._relay(proc)
        finally:
            self._finish(proc, read_fd)

    # -- plumbing ------------------------------------------------------------

    def _send_request(self, proc: subprocess.Popen[bytes], reply_fd: int) -> None:
        assert proc.stdin is not None
        line = json.dumps({**self._request, "reply_fd": reply_fd}) + "\n"
        try:
            proc.stdin.write(line.encode())
            proc.stdin.flush()
        except OSError:
            pass  # the child is already gone; the relay reports how it ended

    def _start_threads(self, proc: subprocess.Popen[bytes], read_fd: int) -> None:
        for target, args in ((self._read_replies, (read_fd,)), (self._drain_stderr, (proc,))):
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _read_replies(self, read_fd: int) -> None:
        buffer = b""
        try:
            while True:
                ready, _, _ = select.select([read_fd], [], [], 0.05)
                if not ready:
                    if self._stop_reader.is_set():
                        break
                    continue
                data = os.read(read_fd, 65536)
                if not data:
                    break
                buffer += data
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    self._messages.put(line)
        except (OSError, ValueError):
            pass
        if buffer:
            self._messages.put(buffer)  # an unterminated last line is malformed
        self._messages.put(None)

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stderr
        assert stream is not None
        try:
            while chunk := os.read(stream.fileno(), 4096):
                with self._tail_lock:
                    self._stderr_tail += chunk
                    del self._stderr_tail[:-STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            pass

    def _tail(self) -> str:
        with self._tail_lock:
            text = bytes(self._stderr_tail).decode("utf-8", "replace").strip()
        return redact(text)

    # -- the protocol --------------------------------------------------------

    def _relay(self, proc: subprocess.Popen[bytes]) -> TranscriptResult:
        result: TranscriptResult | None = None
        exit_deadline: float | None = None
        while True:
            try:
                raw = self._messages.get(timeout=self._poll_interval)
            except queue.Empty:
                if result is None and self._cancelled():
                    late = self._take_done()
                    if late is not None:
                        return late  # finished in the same slice as the cancel
                    raise Cancelled() from None
                if proc.poll() is not None:
                    self._stop_reader.set()  # collect what is left, then see EOF
                elif result is not None:
                    exit_deadline = exit_deadline or time.monotonic() + self._reap_timeout
                    if time.monotonic() >= exit_deadline:
                        _log.warning("faster-whisper child sent 'done' but did not exit")
                        return result
                continue
            if raw is None:
                break
            message = self._parse(raw)
            if result is not None:
                raise ToolFailureError("faster-whisper child sent a message after 'done'")
            kind = message.get("type")
            if kind == MSG_PROGRESS:
                self._progress(message)
            elif kind == MSG_DONE:
                result = _result_from_done(message)
            elif kind == MSG_ERROR:
                raise self._error_from(message)
            else:
                raise ToolFailureError(f"faster-whisper child sent an unknown message: {kind!r}")
        if result is not None:
            return result
        raise self._death(proc)

    def _take_done(self) -> TranscriptResult | None:
        """A complete ``done`` the child sent before now, if any.

        The reader is told to stop and joined first: it exits only once the pipe
        is empty, so whatever the child had written is in the queue by then.
        """
        self._stop_reader.set()
        self._threads[0].join(timeout=1.0)
        while True:
            try:
                raw = self._messages.get_nowait()
            except queue.Empty:
                return None
            if raw is None:
                return None
            message = self._parse(raw)
            if message.get("type") == MSG_DONE:
                return _result_from_done(message)

    @staticmethod
    def _parse(raw: bytes) -> dict[str, Any]:
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise ToolFailureError(
                f"faster-whisper child sent a malformed line: {raw[:80]!r}"
            ) from exc
        if not isinstance(message, dict):
            raise ToolFailureError(f"faster-whisper child sent a non-object line: {raw[:80]!r}")
        return message

    def _progress(self, message: dict[str, Any]) -> None:
        done_sec = message.get("done_sec")
        if isinstance(done_sec, bool) or not isinstance(done_sec, (int, float)):
            raise ToolFailureError("faster-whisper child sent a 'progress' without done_sec")
        if self._on_progress is not None:
            self._on_progress(float(done_sec))
        if self._cancelled():
            raise Cancelled()

    @staticmethod
    def _error_from(message: dict[str, Any]) -> Exception:
        kind = message.get("kind")
        text = message.get("message")
        if not isinstance(text, str):
            return ToolFailureError("faster-whisper child sent an 'error' without a message")
        text = redact(text)
        if kind == KIND_NETWORK:
            return TransientNetworkError(text)
        if kind == KIND_MEMORY:
            return ResourceError(text)
        if kind == KIND_TOOL:
            return ToolFailureError(text)
        if kind == KIND_UNEXPECTED:
            return BugError(text)
        return ToolFailureError(f"faster-whisper child sent an unknown error kind {kind!r}: {text}")

    def _death(self, proc: subprocess.Popen[bytes]) -> Exception:
        """The child ended without a final message, and not by our kill."""
        try:
            code = proc.wait(timeout=self._reap_timeout)
        except subprocess.TimeoutExpired:
            return ToolFailureError("faster-whisper child closed its channel but did not exit")
        tail = self._tail()
        detail = f"; stderr: {tail}" if tail else ""
        if code == -signal.SIGKILL:
            return ResourceError(
                "faster-whisper child was killed (SIGKILL), probably out of memory" + detail
            )
        if code < 0:
            name = signal.Signals(-code).name
            return ToolFailureError(f"faster-whisper child died of {name}{detail}")
        if code == 0:
            return ToolFailureError(f"faster-whisper child exited 0 without a result{detail}")
        return ToolFailureError(f"faster-whisper child exited with status {code}{detail}")

    # -- cleanup -------------------------------------------------------------

    def _finish(self, proc: subprocess.Popen[bytes], read_fd: int) -> None:
        """Kill the child's whole group, reap it, stop the helper threads."""
        try:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=self._reap_timeout)
            except subprocess.TimeoutExpired:
                _log.warning("faster-whisper child did not die in time; giving up its pipes")
                for pipe in (proc.stdin, proc.stderr):
                    if pipe is not None:
                        pipe.close()
                proc.wait()
        finally:
            self._stop_reader.set()
            for thread in self._threads:
                thread.join(timeout=self._reap_timeout)
            for pipe in (proc.stdin, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                os.close(read_fd)
            except OSError:
                pass
