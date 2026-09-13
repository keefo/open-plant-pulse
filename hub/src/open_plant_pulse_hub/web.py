from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.domain.plant_profiles import public_plant_profiles


STATIC_DIR = Path(__file__).with_name("static")


def create_server(store: ReadingStore, host: str, port: int) -> ThreadingHTTPServer:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request = urlparse(self.path)
            path = request.path
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "has_reading": store.latest() is not None,
                        "started_at": started_at,
                    }
                )
            elif path == "/api/plant-profiles":
                self._send_json(public_plant_profiles())
            elif path == "/api/readings/latest":
                latest = store.latest()
                self._send_json(latest or {"reading": None}, HTTPStatus.OK if latest else HTTPStatus.NOT_FOUND)
            elif path == "/api/readings/history":
                self._send_json({"items": store.history()})
            elif path == "/api/care-log":
                self._send_json({"items": store.care_log()})
            elif path == "/api/watering-calendar":
                self._send_watering_calendar(request.query)
            elif path == "/api/pot-response":
                self._send_pot_response(request.query)
            elif path == "/api/plant-journey":
                self._send_plant_journey(request.query)
            elif path == "/":
                self._send_file("index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._send_file("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._send_file("app.js", "text/javascript; charset=utf-8")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/sensors/profile":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 4096:
                    raise ValueError("request body size is invalid")
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                result = store.set_sensor_profile(
                    str(payload.get("sensor_id", "")),
                    str(payload.get("profile_id", "")),
                )
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)

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

    return ThreadingHTTPServer((host, port), DashboardHandler)


def server_address(server: ThreadingHTTPServer) -> Tuple[str, int]:
    host, port = server.server_address[:2]
    return str(host), int(port)