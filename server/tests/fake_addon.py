"""A canned stand-in for the NT8Bridge AddOn: a stdlib http.server on a free port, with
nt8_mcp.app.BASE_URL pointed at it. No live NT8, no network.

    from fake_addon import FakeAddon

    def test_health():
        with FakeAddon() as fake:
            fake.json("/health", {"ok": True})
            assert nt8.nt_health()["ok"] is True

Registering:

    fake.json(path, body, method="GET", status=200)      constant response
    fake.register(path, method, handler)                 handler(req) -> body
                                                         or (status, body)

`path` never includes the query string. `req` is a Request with .method, .path,
.query (dict of lists, from urllib.parse.parse_qs), .body (str) and .n (how many
times this route has been called, starting at 1) — that counter is how a test scripts
a queued -> running -> done progression without module globals.

An unregistered path answers 404 {"error": "not found"}.
"""

import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from nt8_mcp import app


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    body: str = ""
    n: int = 1


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeAddon:
    def __init__(self):
        self.routes: dict[tuple[str, str], callable] = {}
        self.calls: dict[tuple[str, str], int] = {}
        self.port = _free_port()
        self._saved_base_url = None
        self._httpd = None
        self._thread = None

    # -- registration --------------------------------------------------------

    def register(self, path: str, method: str, handler):
        """handler(req: Request) -> body | (status, body). Replaces any earlier registration."""
        self.routes[(method.upper(), path)] = handler
        self.calls[(method.upper(), path)] = 0
        return self

    def json(self, path: str, body, method: str = "GET", status: int = 200):
        """Answer `path` with a constant body."""
        return self.register(path, method, lambda req: (status, body))

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _dispatch(self, method: str):
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                key = (method, split.path)
                handler = fake.routes.get(key)
                if handler is None:
                    return self._send(404, {"error": "not found"})
                fake.calls[key] += 1
                req = Request(method=method, path=split.path, query=parse_qs(split.query),
                              body=body, n=fake.calls[key])
                result = handler(req)
                status, payload = result if isinstance(result, tuple) else (200, result)
                self._send(status, payload)

            def _send(self, status: int, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def log_message(self, *args):  # quiet
                pass

        self._httpd = HTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._saved_base_url = app.BASE_URL
        app.BASE_URL = f"http://127.0.0.1:{self.port}"
        return self

    def stop(self):
        if self._saved_base_url is not None:
            app.BASE_URL = self._saved_base_url
            self._saved_base_url = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
