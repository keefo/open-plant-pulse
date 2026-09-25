import json
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay
from open_plant_pulse_hub.web import create_server, server_address


FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v2-sensor-v2.json"
)


class SensorManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)

    def tearDown(self) -> None:
        self.store.close()

    def test_enrolls_three_sensors_and_keeps_latest_readings_scoped(self) -> None:
        sensor_ids = [item["sensor_id"] for item in self.store.sensors("unclaimed")]
        self.assertEqual(len(sensor_ids), 3)

        for index, sensor_id in enumerate(sensor_ids, start=1):
            sensor = self.store.manage_sensor(
                sensor_id,
                f"Plant {index}",
                "Living room",
                "strelitzia",
                32.0,
                1800,
                1800,
            )
            self.assertEqual(sensor["enrollment_status"], "enrolled")
            self.assertEqual(sensor["latest"]["reading"]["sensor_id"], sensor_id)

        fleet = self.store.sensors("enrolled")
        self.assertEqual(len(fleet), 3)
        self.assertEqual({item["display_name"] for item in fleet}, {"Plant 1", "Plant 2", "Plant 3"})

    def test_sensor_history_does_not_compete_for_global_live_cache(self) -> None:
        bounded_store = ReadingStore(history_size=1)
        try:
            AdvertisementReplay(AdvertisementIngestionService(bounded_store)).replay(FIXTURE_PATH)
            cached_sensor_id = bounded_store.history()[0]["reading"]["sensor_id"]
            sensor_id = next(
                item["sensor_id"]
                for item in bounded_store.sensors("unclaimed")
                if item["sensor_id"] != cached_sensor_id
            )

            self.assertIsNotNone(bounded_store.latest(sensor_id))
            self.assertTrue(bounded_store.history(sensor_id))
        finally:
            bounded_store.close()

    def test_raw_report_log_is_sensor_scoped_newest_first_and_bounded(self) -> None:
        sensor_id = "sensor-aabbccddeeff"

        reports = self.store.raw_sensor_reports(sensor_id)

        self.assertEqual([item["packet_id"] for item in reports], [43, 42, 42])
        self.assertEqual(
            [item["decode_status"] for item in reports],
            ["accepted", "duplicate", "accepted"],
        )
        self.assertEqual(reports[0]["service_data_hex"], "40002b03d71145f500")
        self.assertEqual(len(self.store.raw_sensor_reports(sensor_id, limit=2)), 2)
        self.assertEqual(len(self.store.raw_sensor_reports("sensor-001122334455")), 1)
        self.assertEqual(self.store.raw_sensor_reports("missing"), [])

        ingestion = AdvertisementIngestionService(self.store)
        self.assertEqual(
            ingestion.ingest(
                Advertisement(
                    received_at="2026-09-13T12:45:00Z",
                    local_name=sensor_id,
                    observed_identifier="replay-a",
                    rssi=-52,
                    service_data=bytes.fromhex("40000902"),
                    source_adapter="test",
                )
            ),
            "rejected",
        )
        rejected = self.store.raw_sensor_reports(sensor_id)[0]
        self.assertEqual(rejected["decode_status"], "rejected")
        self.assertEqual(rejected["service_data_hex"], "40000902")
        self.assertIn("truncated", rejected["decode_error"])

        fixture_event = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["events"][0]
        for packet_id in range(55):
            service_data = bytearray.fromhex(fixture_event["service_data_hex"])
            service_data[2] = packet_id
            self.assertEqual(
                ingestion.ingest(
                    Advertisement(
                        received_at=f"2026-09-13T13:00:{packet_id:02d}Z",
                        local_name=sensor_id,
                        observed_identifier="replay-a",
                        rssi=-48,
                        service_data=bytes(service_data),
                        source_adapter="test",
                    )
                ),
                "accepted",
            )
        bounded_reports = self.store.raw_sensor_reports(sensor_id)
        self.assertEqual(len(bounded_reports), 50)
        self.assertEqual(bounded_reports[0]["packet_id"], 54)
        self.assertEqual(bounded_reports[-1]["packet_id"], 5)

    def test_archives_restores_and_replaces_sensor_with_optional_history_merge(self) -> None:
        source, replacement = [item["sensor_id"] for item in self.store.sensors("unclaimed")[:2]]
        self.store.manage_sensor(source, "Fern", "Office", "monstera", 40, 1500, 1200)

        archived = self.store.set_sensor_archived(source, True)
        self.assertEqual(archived["enrollment_status"], "archived")
        restored = self.store.set_sensor_archived(source, False)
        self.assertEqual(restored["enrollment_status"], "enrolled")

        source_count = len(self.store.history(source))
        replacement_count = len(self.store.history(replacement))
        moved = self.store.replace_sensor(source, replacement, merge_history=True)
        self.assertEqual(moved["display_name"], "Fern")
        self.assertEqual(len(self.store.history(replacement)), source_count + replacement_count)
        self.assertEqual(self.store.sensor(source)["replaced_by_sensor_id"], replacement)

    def test_sensor_specific_reporting_intervals_rename_and_delete_sensor_data(self) -> None:
        sensor_id, other_sensor_id = [
            sensor["sensor_id"] for sensor in self.store.sensors("unclaimed")[:2]
        ]
        self.store.manage_sensor(sensor_id, "Fern", "Office", "monstera", 40, 1500, 1200)
        self.store.manage_sensor(
            other_sensor_id, "Palm", "Kitchen", "strelitzia", 40, 1500, 3600
        )

        self.assertEqual(self.store.sensor(sensor_id)["expected_interval_seconds"], 1200)
        self.assertEqual(self.store.sensor(other_sensor_id)["expected_interval_seconds"], 3600)
        self.assertEqual(self.store.sensor(sensor_id)["device_config_status"], "pending")
        revision = self.store.sensor(sensor_id)["device_config_revision"]
        self.store.manage_sensor(sensor_id, "Fern", "Office", "strelitzia", 40, 1500, 1200)
        self.assertEqual(self.store.sensor(sensor_id)["device_config_revision"], revision)
        self.store.manage_sensor(sensor_id, "Fern", "Office", "strelitzia", 40, 1500, 1800)
        self.assertEqual(self.store.sensor(sensor_id)["device_config_revision"], revision + 1)
        self.assertEqual(
            self.store.set_reporting_interval(12),
            {"reporting_interval_minutes": 12},
        )
        self.assertEqual(self.store.sensor(sensor_id)["expected_interval_seconds"], 1800)
        self.assertEqual(self.store.sensor(other_sensor_id)["expected_interval_seconds"], 3600)
        self.assertEqual(self.store.rename_sensor(sensor_id, "Desk fern")["display_name"], "Desk fern")

        self.store.delete_sensor(sensor_id)

        self.assertIsNone(self.store.sensor(sensor_id))
        self.assertEqual(self.store.history(sensor_id), [])


class SensorManagementWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.store.close()

    def request(self, path: str, method: str = "GET", payload=None, origin=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if origin is not None:
            headers["Origin"] = origin
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        return urlopen(request, timeout=1.0)

    def test_browser_routes_list_enroll_and_scope_sensor_history(self) -> None:
        with self.request("/api/sensors?" + urlencode({"status": "unclaimed"})) as response:
            inbox = json.load(response)["items"]
        sensor_id = inbox[0]["sensor_id"]
        encoded_id = quote(sensor_id, safe="")

        with self.request(
            f"/api/sensors/{encoded_id}",
            "PUT",
            {
                "display_name": "Kitchen basil",
                "room": "Kitchen",
                "profile_id": "strelitzia",
                "expected_interval_seconds": 1800,
            },
        ) as response:
            enrolled = json.load(response)
        self.assertEqual(enrolled["display_name"], "Kitchen basil")

        with self.request(f"/sensors/{encoded_id}") as response:
            self.assertIn(b"Open Plant Pulse", response.read())
        with self.request("/api/readings/history?" + urlencode({"sensor_id": sensor_id})) as response:
            history = json.load(response)["items"]
        self.assertTrue(history)
        self.assertEqual({item["reading"]["sensor_id"] for item in history}, {sensor_id})

    def test_rejects_cross_origin_management_request(self) -> None:
        sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        with self.assertRaises(HTTPError) as raised:
            self.request(
                f"/api/sensors/{quote(sensor_id, safe='')}",
                "PUT",
                {
                    "display_name": "Unsafe",
                    "room": "",
                    "profile_id": "strelitzia",
                    "expected_interval_seconds": 1800,
                },
                origin="http://example.invalid",
            )
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_detail_page_contains_configuration_and_supports_management(self) -> None:
        sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        encoded_id = quote(sensor_id, safe="")
        self.store.manage_sensor(sensor_id, "Fern", "Office", "monstera", 40, 1500, 1200)

        with self.request(f"/sensors/{encoded_id}") as response:
            detail_page = response.read()
        self.assertIn(b"Configuration", detail_page)
        self.assertIn(b"Reporting interval", detail_page)
        self.assertIn(b'id="detail-setting-reporting-interval"', detail_page)
        self.assertNotIn(b"Global reporting interval", detail_page)
        self.assertNotIn(b"Moisture alert below", detail_page)
        self.assertNotIn(b"Conductivity alert above", detail_page)
        self.assertIn(b"SHT45 \xc2\xb7 LIVE HISTORY", detail_page)
        self.assertIn(b'data-history-range="live" aria-pressed="true">Live', detail_page)
        self.assertIn(b'data-history-range="30d"', detail_page)
        self.assertIn(b"Relative humidity", detail_page)
        self.assertIn(b"Raw sensor reports", detail_page)
        self.assertIn(b"Latest 50 \xc2\xb7 newest first", detail_page)
        # Replace, archive and delete were removed from this page: three
        # destructive-looking controls with no guidance on which to use, and
        # releasing a sensor now covers what people actually do. The store still
        # supports them, so bringing any back is a UI decision.
        self.assertNotIn(b"Delete sensor and history", detail_page)
        self.assertNotIn(b"Archive sensor", detail_page)
        self.assertNotIn(b"Replacement sensor", detail_page)
        with self.assertRaises(HTTPError) as raised:
            self.request("/manage")
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

        with self.request(
            f"/api/sensors/{encoded_id}",
            "PUT",
            {
                "display_name": "Window fern",
                "room": "Sunroom",
                "profile_id": "monstera",
                "expected_interval_seconds": 3600,
            },
        ) as response:
            updated = json.load(response)
        self.assertEqual(updated["display_name"], "Window fern")
        self.assertEqual(updated["room"], "Sunroom")
        self.assertEqual(updated["expected_interval_seconds"], 3600)
        self.assertEqual(updated["hub_reporting_interval_seconds"], 3600)
        self.assertIsNone(updated["sensor_reporting_interval_seconds"])
        self.assertEqual(updated["device_config_status"], "pending")

        with self.request(
            f"/api/sensors/{encoded_id}",
            "PUT",
            {
                "display_name": "Window fern",
                "room": "Sunroom",
                "profile_id": "monstera",
                "expected_interval_seconds": 5,
            },
        ) as response:
            updated = json.load(response)
        self.assertEqual(updated["expected_interval_seconds"], 5)
        self.assertEqual(updated["hub_reporting_interval_seconds"], 5)
        self.assertEqual(self.store.sensor(sensor_id)["expected_interval_seconds"], 5)

        with self.request(f"/api/sensors/{encoded_id}", "DELETE") as response:
            self.assertEqual(response.status, 204)

        self.assertIsNone(self.store.sensor(sensor_id))

    def test_raw_report_log_is_collapsed_last_and_remembers_its_state(self) -> None:
        with self.request("/sensors/sensor-aabbccddeeff") as response:
            detail_page = response.read().decode("utf-8")
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()

        # Debug output belongs after the controls somebody came to the page for,
        # and closed until asked for: it is the longest section on the page.
        self.assertLess(
            detail_page.index('id="sensor-settings-title"'),
            detail_page.index('id="raw-report-title"'),
        )
        self.assertIn('<details class="raw-report-log" id="raw-report-log"', detail_page)
        self.assertNotIn('<details class="raw-report-log" id="raw-report-log" open', detail_page)
        self.assertIn("<summary>", detail_page)
        # Opened or closed, it stays that way across the reloads that watching
        # reports arrive involves.
        self.assertIn('log.open = window.localStorage.getItem(RAW_REPORTS_OPEN_KEY) === "true";', app)
        self.assertIn('window.localStorage.setItem(RAW_REPORTS_OPEN_KEY, String(log.open));', app)
        self.assertIn("trackRawReportsDisclosure();", app)

    def test_raw_report_endpoint_scopes_reports_and_rejects_unknown_sensor(self) -> None:
        sensor_id = "sensor-aabbccddeeff"
        with self.request("/api/raw-reports?" + urlencode({"sensor_id": sensor_id})) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual([item["packet_id"] for item in payload["items"]], [43, 42, 42])
        self.assertEqual(payload["items"][0]["service_data_hex"], "40002b03d71145f500")

        with self.assertRaises(HTTPError) as raised:
            self.request("/api/raw-reports?" + urlencode({"sensor_id": "missing"}))
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_dashboard_polling_does_not_overlap_or_repeat_calendar_requests(self) -> None:
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()

        self.assertNotIn('setInterval(refresh, 1000)', app)
        self.assertIn('window.setTimeout(poll, 1000)', app)
        self.assertIn('fetch(`/api/raw-reports${query}`)', app)
        self.assertIn("function renderRawReports(items)", app)
        self.assertIn('if (wateringCalendarRequestKey === requestKey) return;', app)
        self.assertIn('wateringCalendarRequestKey = requestKey;', app)
        self.assertIn('latestReading.observed_at,', app)
        self.assertIn('profileId', app)
        self.assertIn('if (historyRequestKey === requestKey) return;', app)
        self.assertIn('refreshClimateHistory()', app)
        self.assertIn('svgElement("polyline", { class: "history-line"', app)
        self.assertIn('"history-point latest-point"', app)
        self.assertIn('"live": { label: "Live (up to 24 hours)", milliseconds: 24 * 60 * 60 * 1000, fitSamples: true }', app)
        self.assertIn('let selectedHistoryRange = "live";', app)
        self.assertIn('chartStartTime = firstSampleTime;', app)
        self.assertIn('chartEndTime = lastSampleTime;', app)
        self.assertIn('(timestamp - startTime) / (endTime - startTime)', app)
        self.assertNotIn('const plotStartTime = points.length > 1', app)
        self.assertIn('chart.toggleAttribute("hidden", points.length === 0);', app)
        self.assertNotIn('chart.hidden = points.length === 0;', app)

    def test_dashboard_polling_updates_fleet_cards_without_replacing_grid(self) -> None:
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()
        render_fleet = app[app.index("function renderFleet()") : app.index("function renderInbox()")]

        self.assertNotIn("grid.replaceChildren()", render_fleet)
        self.assertIn('grid.querySelectorAll(".fleet-card")', render_fleet)
        self.assertIn("updateFleetCard(card, sensor);", render_fleet)
        self.assertIn("if (cardAtIndex !== card)", render_fleet)

    def test_dashboard_uses_profile_alert_thresholds_without_sensor_threshold_inputs(self) -> None:
        static_dir = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static"
        page = (static_dir / "index.html").read_text()
        app = (static_dir / "app.js").read_text()

        self.assertNotIn("setting-moisture", page)
        self.assertNotIn("setting-conductivity", page)
        self.assertNotIn("setting-moisture", app)
        self.assertNotIn("setting-conductivity", app)
        self.assertIn("const moistureLow = profile?.watering?.refill_below;", app)
        self.assertIn("const conductivityHigh = profile?.root_zone?.conductivity_us_cm?.ideal?.[1];", app)
        self.assertIn('id="setting-reporting-interval"', page)
        self.assertIn('id="detail-setting-reporting-interval"', page)
        self.assertIn("Hub reporting interval", page)
        self.assertIn("Sensor reporting interval", page)
        self.assertIn('id="sensor-reporting-interval"', page)
        self.assertIn("readonly", page)
        self.assertIn('id="device-config-status"', page)
        self.assertIn("sent when the sensor next reports", page)
        for label in (
            "Every second",
            "Every 3 seconds",
            "Every 5 seconds",
            "Every 10 seconds",
            "Every 30 seconds",
            "Every minute",
        ):
            self.assertEqual(page.count(label), 2)
        self.assertIn("selectedSensor.sensor_reporting_interval_seconds", app)
        self.assertIn("requestSequence !== fleetRequestSequence", app)
        self.assertIn("sensorSettingsDraftSensorId === selectedSensor.sensor_id", app)
        self.assertIn("sensorSettingsDraftSensorId = selectedSensorId", app)
        self.assertIn("fleetRequestSequence += 1", app)
        self.assertIn("selectedSensor = payload", app)
        self.assertNotIn("It does not change sensor firmware", page)
        self.assertNotIn('id="reporting-settings-form"', page)
        self.assertNotIn('fetch("/api/settings")', app)


if __name__ == "__main__":
    unittest.main()