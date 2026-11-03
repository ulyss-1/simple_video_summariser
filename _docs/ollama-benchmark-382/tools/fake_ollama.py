"""Fake Ollama for dry-runs: canned valid outputs, Ollama-shaped timing fields. No model, no CPU."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
loaded: set[str] = set()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, doc):
        b = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/ps":
            self._send({"models": [{"name": m} for m in loaded]})
        else:
            self._send({"version": "fake"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        loaded.add(req["model"])
        fmt = req.get("format")
        if isinstance(fmt, dict) and "speakers" in json.dumps(fmt):
            out = json.dumps({"speakers": []})
        elif isinstance(fmt, dict):
            out = json.dumps({
                "topics": [{"title": "Topic A", "summary": "About A", "start_sec": 5}],
                "claims": [{"text": "A claim.", "speaker": "unknown", "start_sec": 10, "confidence": "high"}],
                "quotes": [{"text": "a verbatim quote that is made up", "speaker": "unknown", "start_sec": 12}],
            })
        else:
            out = "One. Two. Three. Four."
        self._send({
            "model": req["model"], "message": {"role": "assistant", "content": out}, "done": True,
            "done_reason": "stop", "total_duration": 3_000_000_000, "load_duration": 1_000_000_000,
            "prompt_eval_count": 1000, "prompt_eval_duration": 1_000_000_000,
            "eval_count": 100, "eval_duration": 1_000_000_000,
        })


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
