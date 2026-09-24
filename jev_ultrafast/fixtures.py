"""Loopback-only static server for the local fixture page, owned by the worker.

It serves an explicit allowlist (the self-contained fixture page) on 127.0.0.1 at a random port. There is
no API, no directory listing and no other file; any other method, path or Host header is refused.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

STATIC = Path(__file__).with_name("static")
FILES = {"/fixture.html": ("fixture.html", "text/html; charset=utf-8")}
LOOPBACK = "127.0.0.1"


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # On Windows SO_REUSEADDR would let another process bind the same port.
    daemon_threads = True


class FixtureServer:
    def __init__(self):
        content = {path: ((STATIC / name).read_bytes(), mime) for path, (name, mime) in FILES.items()}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def respond(self, status, body=b"", mime="text/plain; charset=utf-8", head=False):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def serve(self, head):
                if self.headers.get("Host") != f"{LOOPBACK}:{server.port}":
                    return self.respond(403, b"Forbidden", head=head)
                entry = content.get(urlsplit(self.path).path)
                if entry is None:
                    return self.respond(404, b"Not found", head=head)
                self.respond(200, entry[0], entry[1], head=head)

            def do_GET(self):
                self.serve(head=False)

            def do_HEAD(self):
                self.serve(head=True)

            def log_message(self, *_args):
                pass  # stdout is the protocol channel; request logs are not needed.

        self.httpd = _Server((LOOPBACK, 0), Handler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://{LOOPBACK}:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="jev-fixtures", daemon=True)
        self.thread.start()

    def url(self, scenario):
        return f"{self.origin}/fixture.html?scenario={quote(scenario)}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
