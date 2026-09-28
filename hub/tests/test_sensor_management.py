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
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
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

        self.assertEqual([item["report_id"] for item in reports], [42, 43, 43, 42, 42, 42])
        self.assertEqual(
            [item["packet_kind"] for item in reports],
            ["packet1", "packet2", "packet1", "packet1", "packet2", "packet1"],
        )
        self.assertEqual(
            [item["decode_status"] for item in reports],
            ["conflict", "accepted", "accepted", "duplicate", "accepted", "accepted"],
        )
        self.assertEqual(
            reports[0]["service_data_hex"],
            "40022e092e362f213e2a00000045f200504090a66a56d204",
        )
        self.assertIn("already stored with different content", reports[0]["decode_error"])
        self.assertEqual(len(self.store.raw_sensor_reports(sensor_id, limit=2)), 2)
        self.assertEqual(len(self.store.raw_sensor_reports("sensor-001122334455")), 2)
        self.assertEqual(self.store.raw_sensor_reports("missing"), [])

        ingestion = AdvertisementIngestionService(self.store)
        self.assertEqual(
            ingestion.ingest(
                Advertisement(
                    received_at="2026-09-13T12:45:00Z",
                    local_name=sensor_id,
                    observed_identifier="replay-a",
                    rssi=-52,
                    service_data=bytes.fromhex("40022e"),
                    source_adapter="test",
                )
            ),
            "rejected",
        )
        rejected = self.store.raw_sensor_reports(sensor_id)[0]
        self.assertEqual(rejected["decode_status"], "rejected")
        self.assertEqual(rejected["service_data_hex"], "40022e")
        self.assertIsNone(rejected["report_id"])
        self.assertIn("truncated", rejected["decode_error"])

        fixture_event = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["events"][0]
        for index in range(55):
            service_data = bytearray.fromhex(fixture_event["service_data_hex"])
            report_id_at = service_data.index(0x3E) + 1
            service_data[report_id_at : report_id_at + 4] = (100 + index).to_bytes(4, "little")
            self.assertEqual(
                ingestion.ingest(
                    Advertisement(
                        received_at=f"2026-09-13T13:00:{index:02d}Z",
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
        self.assertEqual(bounded_reports[0]["report_id"], 154)
        self.assertEqual(bounded_reports[-1]["report_id"], 105)

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
        # Configuring a sensor and watching a plant are separate pages served by
        # one document, so what tells them apart is the page each section is on.
        self.assertIn(b'<section class="climate-history" data-page="detail"', detail_page)
        self.assertIn(b'<section class="sensor-settings" data-page="config"', detail_page)
        self.assertIn(b'id="raw-report-log" data-page="config"', detail_page)
        self.assertIn(b'id="detail-config-link"', detail_page)
        self.assertIn(b'id="config-back-link"', detail_page)
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
                "expected_interval_seconds": 30,
            },
        ) as response:
            updated = json.load(response)
        self.assertEqual(updated["expected_interval_seconds"], 30)
        self.assertEqual(updated["hub_reporting_interval_seconds"], 30)
        self.assertEqual(self.store.sensor(sensor_id)["expected_interval_seconds"], 30)

        with self.request(f"/api/sensors/{encoded_id}", "DELETE") as response:
            self.assertEqual(response.status, 204)

        self.assertIsNone(self.store.sensor(sensor_id))

    def test_configuration_page_is_routed_and_reachable_from_both_lists(self) -> None:
        sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        static = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static"
        app = (static / "app.js").read_text()
        onboarding = (static / "onboarding.js").read_text()

        # The hub serves the application for the new route as for any other page.
        with self.request(f"/sensors/{quote(sensor_id, safe='')}/settings") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'data-page="config"', response.read())

        self.assertIn(
            'if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "config";',
            app,
        )
        # The sensor is still named by the path, with or without the suffix.
        self.assertIn(r"/^\/sensors\/([^/]+)(?:\/settings)?$/", app)
        # Reachable from the plant page, and from the row in hub settings.
        self.assertIn('sensorPath(selectedSensorId, "/settings")', app)
        self.assertIn('"/sensors/" + encodeURIComponent(sensor.sensor_id) + "/settings"', onboarding)
        # Polling follows the page: no charts to feed here, and no report log there.
        self.assertIn('if (page === "config") {', app)

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

    def test_latest_reading_carries_the_joined_packet2(self) -> None:
        query = urlencode({"sensor_id": "sensor-001122334455"})
        with self.request("/api/readings/latest?" + query) as response:
            reading = json.load(response)["reading"]

        self.assertEqual(reading["report_id"], 7)
        self.assertEqual(reading["observed_at"], "2026-09-13T12:00:03Z")
        self.assertEqual(reading["battery_percent"], 64)
        self.assertEqual(reading["battery_voltage_v"], 3.701)
        self.assertIs(reading["battery_charging"], True)
        self.assertIsNone(reading["soil_ph"])

        # Charging travels with the battery everywhere a reading is served:
        # false and unknown are different answers, and both survive JSON.
        with self.request("/api/sensors/sensor-001122334455") as response:
            self.assertIs(json.load(response)["latest"]["reading"]["battery_charging"], True)
        query = urlencode({"sensor_id": "sensor-aabbccddeeff"})
        with self.request("/api/readings/history?" + query) as response:
            history = json.load(response)["items"]
        self.assertEqual(
            [(item["reading"]["report_id"], item["reading"]["battery_charging"]) for item in history],
            [(42, False), (43, None)],
        )

        page = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "index.html").read_text()
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()
        self.assertIn('id="battery"', page)
        self.assertIn("<th>Report</th>", page)
        self.assertIn("reading.battery_percent", app)
        self.assertIn("reading.battery_voltage_v", app)
        self.assertIn("item.report_id", app)
        self.assertNotIn("packet_id", app)

    def test_raw_report_endpoint_scopes_reports_and_rejects_unknown_sensor(self) -> None:
        sensor_id = "sensor-aabbccddeeff"
        with self.request("/api/raw-reports?" + urlencode({"sensor_id": sensor_id})) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [item["report_id"] for item in payload["items"]], [42, 43, 43, 42, 42, 42]
        )
        self.assertEqual(payload["items"][1]["service_data_hex"], "403e2b000000")
        self.assertEqual(payload["items"][2]["service_data_hex"], "402e2e3e2b00000045f500")

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
        self.assertIn('latestReadingAt,', app)
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
        # SVG elements have no hidden property, so charts are hidden by attribute.
        self.assertIn('setHidden(chart, points.length === 0);', app)
        self.assertIn('element.toggleAttribute("hidden", Boolean(hidden))', app)
        self.assertNotIn('chart.hidden = points.length === 0;', app)

    def test_air_conditions_are_judged_on_their_charts(self) -> None:
        page = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "index.html").read_text()
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()
        # The air history charts carry the plant's ideal range and say where the
        # latest value sits, so there are no separate air cards.
        self.assertNotIn('data-metric="air_temperature_c"', page)
        self.assertNotIn('data-metric="air_humidity_percent"', page)
        self.assertIn('id="temperature-history-range"', page)
        self.assertIn('id="humidity-history-range"', page)
        self.assertIn('class: "ideal-band"', app)
        self.assertIn("profile?.climate.air_temperature_c.ideal", app)
        self.assertIn("profile?.climate.air_humidity_percent.ideal", app)

    def test_dashboard_polling_leaves_unchanged_elements_alone(self) -> None:
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()
        # A write that would put back the value already shown is skipped.
        self.assertIn('if (element.textContent !== value) element.textContent = value;', app)
        self.assertIn('if (element.dataset.renderKey === key) return false;', app)
        # Lists and charts are rebuilt only when what they are drawn from changes.
        for renderer, guard in [
            ("function renderCareLog(", "if (!renderKeyChanged(list, items)) return;"),
            ("function renderInbox(", "if (!renderKeyChanged(list, unclaimedSensors.map("),
            ("function renderRawReports(", "if (!renderKeyChanged(rows, items)) return;"),
            ("function renderBattery(", "if (!renderKeyChanged(element, [known, percent, charging, voltage])) return;"),
            ("function renderHistoryChart(", "if (!renderKeyChanged(chart, [points, unit, startTime, endTime, selectedHistoryRange, ideal])) return;"),
            ("function renderMoistureTrend(", "if (!renderKeyChanged(chart, [moistureTrendVersion,"),
        ]:
            body = app[app.index(renderer):]
            body = body[: body.index("\nfunction ", 1)]
            self.assertIn(guard, body, renderer)
            self.assertLess(body.index(guard), body.index("replaceChildren("), renderer)

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
        self.assertIn('id="journey-light-hours"', page)
        self.assertIn("Hours of light given", page)
        self.assertIn('id="detail-setting-reporting-interval"', page)
        self.assertIn("Hub reporting interval", page)
        self.assertIn("Sensor reporting interval", page)
        self.assertIn('id="sensor-reporting-interval"', page)
        self.assertIn("readonly", page)
        self.assertIn('id="device-config-status"', page)
        self.assertIn("sent when the sensor next reports", page)
        for label in ("Every 30 seconds", "Every minute", "Every 30 minutes"):
            self.assertEqual(page.count(label), 2)
        # Thirty seconds is the floor. Anything shorter spends the whole cycle
        # advertising and arrives no fresher, because a report carries what the
        # sensor last sampled on its own schedule.
        for label in ("Every second", "Every 3 seconds", "Every 5 seconds",
                      "Every 10 seconds"):
            self.assertNotIn(label, page)
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