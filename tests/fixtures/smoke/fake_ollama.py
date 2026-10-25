"""Stub Ollama server for the pipeline smoke test (issue #137). Stdlib only.

Usage: ``python fake_ollama.py <model> [port]``. The model name comes from the
command line (compose's ``OLLAMA_MODEL``), never from the environment.

Answers only what the real adapter asks for: ``GET /api/tags`` and
``POST /api/chat`` for the roster, a chunk analysis and the reduce step, told
apart by the request's ``format``. Anything else is a 400.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TLDR = "SMOKE-TLDR: creativity matters as much as literacy"
ROSTER = {"speakers": [{"name": "Ken Robinson", "role": "host"}]}
CHUNK = {
    "topics": [
        {
            "title": "Creativity in schools",
            "summary": "Schools undervalue it.",
            "start_sec": 10,
        }
    ],
    "claims": [
        {
            "text": "Creativity is as important as literacy.",
            "speaker": "Ken Robinson",
            "start_sec": 20,
            "confidence": "high",
        },
        {
            "text": "A claim by someone the roster never named.",
            "speaker": "Not In Roster",
            "start_sec": 40,
            "confidence": "medium",
        },
    ],
    "quotes": [],
}


def chat_content(request: dict[str, Any]) -> str | None:
    """The reply text for a chat request, or ``None`` if it is not a known call."""
    fmt = request.get("format")
    if fmt is None:
        return TLDR
    properties = fmt.get("properties") if isinstance(fmt, dict) else None
    if not isinstance(properties, dict):
        return None
    if "speakers" in properties:
        return json.dumps(ROSTER)
    if "topics" in properties:
        return json.dumps(CHUNK)
    return None


class Handler(BaseHTTPRequestHandler):
    model = ""

    def _send(self, status: int, doc: dict[str, Any]) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": self.model, "model": self.model}]})
        else:
            self._send(400, {"error": f"unexpected GET {self.path}"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            request = None
        content = None
        if self.path == "/api/chat" and isinstance(request, dict):
            content = chat_content(request)
        if content is None:
            self._send(400, {"error": f"unexpected POST {self.path}"})
            return
        self._send(
            200,
            {
                "model": self.model,
                "message": {"role": "assistant", "content": content},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 40,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"fake-ollama: {self.command} {self.path}\n")


def main(argv: list[str]) -> None:
    Handler.model = argv[0]
    port = int(argv[1]) if len(argv) > 1 else 11434
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
