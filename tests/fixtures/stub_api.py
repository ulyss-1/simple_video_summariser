"""Stand-in upstream for the frontend image tests (issue #57).

Stdlib only; runs in `python:3.14-slim` on port 8000 under the network alias
`api`. Every request is echoed back as JSON (method, raw path with query, body,
request headers). Special cases:

- `/videos/{id}/render` sends the #45 security headers a real render sends.
- `/status/404` and `/status/503` return a canned JSON error with that status.
- Nothing else sends any security header or Cache-Control.
Every response carries `X-Request-Id`, echoing the request's, so a test can see
that the API's own headers pass through nginx.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RENDER_CSP = "default-src 'none'; style-src 'unsafe-inline'"
RENDER = re.compile(r"^/videos/[^/?]+/render(\?.*)?$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        extra: list[tuple[str, str]] = []
        status = 200
        payload: dict[str, object]
        if self.path.startswith("/status/"):
            status = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
            payload = {"error": f"canned-{status}"}
        else:
            payload = {
                "method": self.command,
                "path": self.path,
                "body": body.decode("latin-1"),
                "headers": [[k, v] for k, v in self.headers.items()],
            }
            if RENDER.match(self.path):
                extra = [
                    ("Content-Security-Policy", RENDER_CSP),
                    ("X-Content-Type-Options", "nosniff"),
                    ("Referrer-Policy", "no-referrer"),
                ]
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", self.headers.get("X-Request-Id", "none"))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
