"""A read-only server for firmware images, on the household network.

The management interface stays on loopback, where it has always been. A sensor
cannot fetch an image from there, so images are served by this, which answers one
shape of request and holds nothing private: the bytes it serves are the same ones
this repository builds, and a sensor asks for them by digest, so there is no path
to name and nothing to enumerate.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from typing import Any, Tuple
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)
FIRMWARE_PREFIX = "/firmware/"
FIRMWARE_SUFFIX = ".bin"


def create_firmware_server(
    library: Any, host: str = "0.0.0.0", port: int = 8081
) -> ThreadingHTTPServer:
    class FirmwareHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send(b'{"status":"ok"}', "application/json")
                return
            data = self._image(path)
            if data is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send(data, "application/octet-stream")

        def do_HEAD(self) -> None:
            # ESP-IDF's OTA client asks for the size before it commits to the
            # download, and a server that only answers GET makes it guess.
            path = urlparse(self.path).path
            data = self._image(path)
            if data is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

        def _image(self, path: str) -> bytes | None:
            if not path.startswith(FIRMWARE_PREFIX) or not path.endswith(FIRMWARE_SUFFIX):
                return None
            digest = path[len(FIRMWARE_PREFIX) : -len(FIRMWARE_SUFFIX)]
            try:
                return library.read(digest)
            except ValueError:
                # A digest that is not a digest names no image, which is a
                # missing file rather than an error worth reporting.
                return None

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("firmware %s - %s", self.address_string(), format % args)

    server = ThreadingHTTPServer((host, port), FirmwareHandler)
    server.daemon_threads = True
    return server


def firmware_server_address(server: ThreadingHTTPServer) -> Tuple[str, int]:
    host, port = server.server_address[:2]
    return str(host), int(port)
