"""Inspect-only Docker API proxy for authoritative Compose worker-death evidence."""

from __future__ import annotations

import http.client
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_INSPECT_PATH = re.compile(r"/containers/[0-9a-f]{12}(?:[0-9a-f]{52})?/json")
_MAX_RESPONSE_BYTES = 1_048_576
_DOCKER_SOCKET = "/var/run/docker.sock"


def permitted_inspect_path(method: str, path: str) -> bool:
    """Return whether a request is the sole Docker operation this authority permits."""
    return method == "GET" and _INSPECT_PATH.fullmatch(path) is not None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(_DOCKER_SOCKET)


class _Handler(BaseHTTPRequestHandler):
    server_version = "kdive-worker-death-api"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        if not permitted_inspect_path("GET", self.path):
            self.send_error(403)
            return
        connection = _UnixHTTPConnection("localhost", timeout=3)
        try:
            connection.request("GET", self.path)
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        except OSError:
            self.send_error(502)
            return
        finally:
            connection.close()
        if len(body) > _MAX_RESPONSE_BYTES:
            self.send_error(502)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def do_DELETE(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def do_PUT(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log caller-controlled paths from this internal authority.
        return


def main() -> None:
    """Serve the private Compose authority until the container is stopped."""
    server = ThreadingHTTPServer(("0.0.0.0", 2375), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
