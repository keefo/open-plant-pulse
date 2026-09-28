from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import socket
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.domain.plant_profiles import public_plant_profiles


STATIC_DIR = Path(__file__).with_name("static")
# Top-level paths the browser application owns. Each one, and anything beneath it,
# is served the same shell so a reload or a pasted link lands on the right view.
APPLICATION_PAGES = ("/sensors", "/settings", "/onboarding")
# A firmware image for this sensor is about 1.3 MB; the ceiling is a slot's worth
# with room to spare, so an accidental upload of something enormous is refused
# before it is read rather than after.
MAX_FIRMWARE_UPLOAD_BYTES = 4 * 1024 * 1024


class DashboardServer(ThreadingHTTPServer):
    """The management interface's HTTP server.

    An IPv6 host is served over IPv6, and "::" over IPv4 as well, so a name that
    resolves to either family reaches it. Unless the network is allowed in, it
    answers only the computer it runs on: macOS lets an ordinary user take port
    80 only on every address at once, so the limit is kept per request instead
    of by binding to loopback.
    """

    def __init__(self, address: Tuple[str, int], handler: Any, allow_network: bool) -> None:
        self.allow_network = allow_network
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, handler)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6 and self.server_address[0] == "::":
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _plain_address(text: str) -> Any:
    address = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_this_computer(peer: str, local: str) -> bool:
    """Whether a connection came from the computer that accepted it.

    A browser on this computer may connect through loopback or through one of
    the computer's own network addresses, depending on how the name resolved;
    either way both ends of the connection carry the same address.
    """
    peer_address = _plain_address(peer)
    return peer_address.is_loopback or peer_address == _plain_address(local)


def create_server(
    store: ReadingStore,
    host: str,
    port: int,
    scanner_health: Optional[Callable[[], Dict[str, Optional[str]]]] = None,
    firmware: Optional[Any] = None,
    allow_network: bool = False,
) -> ThreadingHTTPServer:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    class DashboardHandler(BaseHTTPRequestHandler):
        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            if self.server.allow_network or is_this_computer(
                self.client_address[0], self.connection.getsockname()[0]
            ):
                return True
            self.send_error(HTTPStatus.FORBIDDEN, "This hub answers only the computer it runs on")
            return False

        def do_GET(self) -> None:
            request = urlparse(self.path)
            path = request.path
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "has_reading": store.latest() is not None,
                        "started_at": started_at,
                        "database": store.database_health(),
                        "scanner": (
                            scanner_health()
                            if scanner_health
                            else {"status": "disabled"}
                        ),
                    }
                )
            elif path == "/api/plant-profiles":
                self._send_json(public_plant_profiles())
            elif path == "/api/settings":
                self._send_json(store.hub_settings())
            elif path == "/api/settings/wifi":
                self._send_json(store.hub_wifi_settings())
            elif path == "/api/rooms":
                self._send_json({"items": store.rooms()})
            elif path == "/api/firmware":
                self._send_json(
                    {"items": firmware.images() if firmware is not None else [],
                     "storage": firmware is not None}
                )
            elif path == "/api/sensors":
                self._send_sensors(request.query)
            elif path.startswith("/api/sensors/"):
                sensor_id = unquote(path[len("/api/sensors/") :])
                sensor = store.sensor(sensor_id)
                self._send_json(
                    sensor or {"error": "sensor not found"},
                    HTTPStatus.OK if sensor else HTTPStatus.NOT_FOUND,
                )
            elif path == "/api/readings/latest":
                sensor_id = parse_qs(request.query).get("sensor_id", [None])[0]
                latest = store.latest(sensor_id)
                self._send_json(
                    latest or {"reading": None},
                    HTTPStatus.OK if latest else HTTPStatus.NOT_FOUND,
                )
            elif path == "/api/readings/history":
                self._send_reading_history(request.query)
            elif path == "/api/care-log":
                sensor_id = parse_qs(request.query).get("sensor_id", [None])[0]
                self._send_json({"items": store.care_log(sensor_id=sensor_id)})
            elif path == "/api/raw-reports":
                self._send_raw_reports(request.query)
            elif path == "/api/watering-calendar":
                self._send_watering_calendar(request.query)
            elif path == "/api/pot-response":
                self._send_pot_response(request.query)
            elif path == "/api/plant-journey":
                self._send_plant_journey(request.query)
            elif path == "/" or any(
                path == page or path.startswith(page + "/") for page in APPLICATION_PAGES
            ):
                self._send_file("index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._send_file("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._send_file("app.js", "text/javascript; charset=utf-8")
            elif path == "/onboarding.js":
                self._send_file("onboarding.js", "text/javascript; charset=utf-8")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if (
                path
                not in ("/api/sensors/profile", "/api/settings", "/api/settings/wifi")
                and not path.startswith("/api/sensors/")
                and not path.startswith("/api/rooms/")
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self._require_same_origin()
                payload = self._read_json()
                if path == "/api/sensors/profile":
                    result = store.set_sensor_profile(
                        str(payload.get("sensor_id", "")),
                        str(payload.get("profile_id", "")),
                    )
                elif path == "/api/settings":
                    result = store.set_reporting_interval(
                        int(payload.get("reporting_interval_minutes", 0))
                    )
                elif path.startswith("/api/rooms/"):
                    result = store.update_room(
                        int(path[len("/api/rooms/") :]),
                        str(payload.get("name", "")),
                        str(payload.get("aspect", "unknown")),
                        str(payload.get("light", "unknown")),
                        self._optional_str(payload.get("notes")),
                    )
                elif path == "/api/settings/wifi":
                    wifi_ssid = payload.get("wifi_ssid")
                    if wifi_ssid is not None and not isinstance(wifi_ssid, str):
                        raise ValueError("wifi_ssid must be a string")
                    result = store.set_hub_wifi_network(wifi_ssid)
                elif path.endswith("/name"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/name")])
                    result = store.rename_sensor(sensor_id, str(payload.get("display_name", "")))
                else:
                    sensor_id = unquote(path[len("/api/sensors/") :])
                    result = store.manage_sensor(
                        sensor_id,
                        str(payload.get("display_name", "")),
                        str(payload.get("room", "")),
                        str(payload.get("profile_id", "")),
                        self._optional_float(payload.get("moisture_low_percent")),
                        self._optional_int(payload.get("conductivity_high_us_cm")),
                        int(payload.get("expected_interval_seconds", 1800)),
                        self._optional_int(payload.get("room_id")),
                    )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/rooms/"):
                try:
                    self._require_same_origin()
                    store.delete_room(int(path[len("/api/rooms/") :]))
                except ValueError as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/api/firmware/"):
                try:
                    self._require_same_origin()
                    if firmware is None:
                        raise ValueError("this hub is not storing firmware")
                    firmware.delete(unquote(path[len("/api/firmware/") :]))
                except ValueError as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not path.startswith("/api/sensors/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self._require_same_origin()
                sensor_id = unquote(path[len("/api/sensors/") :])
                store.delete_sensor(sensor_id)
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                self._require_same_origin()
                # An image is megabytes of binary, so this one route reads its
                # own body; everything else here is a small JSON object.
                payload = {} if path == "/api/firmware" else self._read_json()
                if path.startswith("/api/sensors/") and path.endswith("/archive"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/archive")])
                    archived = payload.get("archived")
                    if not isinstance(archived, bool):
                        raise ValueError("archived must be a boolean")
                    result = store.set_sensor_archived(sensor_id, archived)
                elif path.startswith("/api/sensors/") and path.endswith("/replace"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/replace")])
                    merge_history = payload.get("merge_history", False)
                    if not isinstance(merge_history, bool):
                        raise ValueError("merge_history must be a boolean")
                    result = store.replace_sensor(
                        sensor_id,
                        str(payload.get("replacement_sensor_id", "")),
                        merge_history,
                    )
                elif path == "/api/rooms":
                    result = store.create_room(
                        str(payload.get("name", "")),
                        str(payload.get("aspect", "unknown")),
                        str(payload.get("light", "unknown")),
                        self._optional_str(payload.get("notes")),
                    )
                elif path.startswith("/api/sensors/") and path.endswith("/wifi"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/wifi")])
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled must be a boolean")
                    result = store.set_sensor_wifi_enabled(sensor_id, enabled)
                elif path.startswith("/api/sensors/") and path.endswith("/wifi-result"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/wifi-result")])
                    result = store.record_sensor_wifi_result(
                        sensor_id,
                        str(payload.get("wifi_state", "")),
                        self._optional_str(payload.get("wifi_failure")),
                        self._optional_str(payload.get("wifi_address")),
                    )
                elif path == "/api/firmware":
                    result = self._store_firmware()
                    if result is None:
                        return
                elif path.startswith("/api/sensors/") and path.endswith("/firmware"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/firmware")])
                    digest = self._optional_str(payload.get("digest"))
                    result = (
                        store.request_firmware_update(sensor_id, digest)
                        if digest
                        else store.cancel_firmware_update(sensor_id)
                    )
                elif path.startswith("/api/sensors/") and path.endswith("/onboarding"):
                    sensor_id = unquote(path[len("/api/sensors/") : -len("/onboarding")])
                    result = store.set_sensor_onboarding_state(
                        sensor_id,
                        str(payload.get("onboarding_state", "")),
                    )
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)

        def _send_sensors(self, query_string: str) -> None:
            status = parse_qs(query_string).get("status", [None])[0]
            try:
                self._send_json({"items": store.sensors(status)})
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        def _send_reading_history(self, query_string: str) -> None:
            query = parse_qs(query_string)
            sensor_id = query.get("sensor_id", [None])[0]
            start_value = query.get("start", [None])[0]
            end_value = query.get("end", [None])[0]
            if start_value is None and end_value is None:
                self._send_json({"items": store.history(sensor_id)})
                return
            try:
                if not sensor_id or start_value is None or end_value is None:
                    raise ValueError("sensor_id, start, and end are required for ranged history")
                start_at = self._query_timestamp(start_value)
                end_at = self._query_timestamp(end_value)
                start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                if end <= start:
                    raise ValueError("history end must be after start")
                if end - start > timedelta(days=31):
                    raise ValueError("history range cannot exceed 31 days")
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": store.history_range(sensor_id, start_at, end_at)})

        def _send_raw_reports(self, query_string: str) -> None:
            sensor_id = parse_qs(query_string).get("sensor_id", [""])[0]
            if not sensor_id:
                self._send_json({"error": "sensor_id is required"}, HTTPStatus.BAD_REQUEST)
                return
            if store.sensor(sensor_id) is None:
                self._send_json({"error": "sensor not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"items": store.raw_sensor_reports(sensor_id)})

        def _store_firmware(self) -> Optional[Dict[str, Any]]:
            """Take an uploaded image, or say why it is not one.

            The version is read out of the image rather than taken from the
            request, so what the hub offers is what the sensor will report.
            """
            if firmware is None:
                raise ValueError("this hub is not storing firmware")
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_FIRMWARE_UPLOAD_BYTES:
                raise ValueError("firmware upload size is invalid")
            return firmware.add(self.rfile.read(content_length))

        def _read_json(self) -> Dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 4096:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _require_same_origin(self) -> None:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ValueError("cross-origin state changes are not allowed")

        @staticmethod
        def _optional_float(value: Any) -> Optional[float]:
            return None if value in (None, "") else float(value)

        @staticmethod
        def _optional_int(value: Any) -> Optional[int]:
            return None if value in (None, "") else int(value)

        @staticmethod
        def _optional_str(value: Any) -> Optional[str]:
            if value in (None, ""):
                return None
            if not isinstance(value, str):
                raise ValueError("expected a string")
            return value

        def _send_watering_calendar(self, query_string: str) -> None:
            try:
                query = parse_qs(query_string)
                sensor_id = query.get("sensor_id", [""])[0]
                start_at = self._query_timestamp(query.get("start", [""])[0])
                end_at = self._query_timestamp(query.get("end", [""])[0])
                if not sensor_id or end_at <= start_at:
                    raise ValueError("sensor_id and an increasing date range are required")
                start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                if end - start > timedelta(days=371):
                    raise ValueError("watering calendar range cannot exceed 371 days")
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                {
                    "items": store.watering_calendar(sensor_id, start_at, end_at),
                    "watering_interval": store.watering_interval_summary(sensor_id),
                }
            )

        def _send_pot_response(self, query_string: str) -> None:
            sensor_id = parse_qs(query_string).get("sensor_id", [""])[0]
            if not sensor_id:
                self._send_json({"error": "sensor_id is required"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": store.drainage_assessments(sensor_id)})

        def _send_plant_journey(self, query_string: str) -> None:
            sensor_id = parse_qs(query_string).get("sensor_id", [""])[0]
            if not sensor_id:
                self._send_json({"error": "sensor_id is required"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(store.plant_journey(sensor_id))

        @staticmethod
        def _query_timestamp(value: str) -> str:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("calendar dates must be ISO 8601 timestamps") from error
            if parsed.tzinfo is None:
                raise ValueError("calendar dates must include a timezone")
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        def _send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, filename: str, content_type: str) -> None:
            body = (STATIC_DIR / filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: Any) -> None:
            return

    return DashboardServer((host, port), DashboardHandler, allow_network)


def server_address(server: ThreadingHTTPServer) -> Tuple[str, int]:
    host, port = server.server_address[:2]
    return str(host), int(port)