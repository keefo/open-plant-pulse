import json
import re
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import ReadingStore
from fake_sensors import place_sensors
from open_plant_pulse_hub.web import create_server, server_address


FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)


class SensorManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        place_sensors(self.store)

    def tearDown(self) -> None:
        self.store.close()


    def test_a_sensor_takes_the_room_of_the_plant_it_is_given(self) -> None:
        """A scale adopted on its own had no room, and no way to get one.

        The room was only ever set when adopting a sensor created a plant
        to go with it. It matters: a probe's air reading is shared with
        every plant in its room.
        """
        room_id = self.store.create_room("Conservatory")["room_id"]
        plant_id = self.store.create_plant("Fern")["plant_id"]
        self.store.update_plant(plant_id, "Fern", room_id=room_id)
        sensor_id = self.store.adopt_scale("http://scale.local", "scale")["sensor_id"]
        self.assertIsNone(self.store.sensor(sensor_id)["room_id"])

        attached = self.store.attach_sensor(sensor_id, plant_id)

        self.assertEqual(attached["room_id"], room_id)
        self.assertEqual(attached["room"], "Conservatory")
        # Taking it off the plant does not move it out of the room: it is
        # still sitting where it was sitting.
        detached = self.store.attach_sensor(sensor_id, None)
        self.assertEqual(detached["room_id"], room_id)
        self.assertIsNone(detached["plant_id"])

    def test_the_keep_interval_offers_everything_the_store_will_take(self) -> None:
        """A watering is watched in seconds; a big pot is watched in days."""
        sensor_id = self.store.adopt_scale("http://scale.local", "scale")["sensor_id"]

        # The ends of the range the store allows, which the menu was not
        # offering: half a minute to watch a pot drain, a week for one that
        # is only watered about that often.
        self.assertEqual(
            self.store.set_scale_keep(sensor_id, 30)["scale_keep_every_seconds"], 30)
        self.assertEqual(
            self.store.set_scale_keep(sensor_id, 604800)["scale_keep_every_seconds"], 604800)

        for refused in (29, 604801):
            with self.assertRaises(ValueError):
                self.store.set_scale_keep(sensor_id, refused)

        # Every choice the menu offers is one the store will accept, so no
        # option can be picked only to be refused on save.
        page = (
            Path(__file__).resolve().parents[1]
            / "src/open_plant_pulse_hub/static/index.html"
        ).read_text(encoding="utf-8")
        menu = page.split('<select id="scale-keep-every">', 1)[1].split("</select>", 1)[0]
        seconds = [int(value) for value in re.findall(r'<option value="(\d+)"', menu)]
        self.assertTrue(seconds, "the menu should offer intervals")
        self.assertEqual(seconds, sorted(seconds), "shortest first, as a person reads it")
        self.assertEqual(seconds[0], 30)
        self.assertEqual(seconds[-1], 604800)
        for value in seconds:
            self.assertEqual(
                self.store.set_scale_keep(sensor_id, value)["scale_keep_every_seconds"], value)



    def test_a_reference_needs_a_reading_and_must_be_a_ph(self) -> None:
        sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        with self.assertRaises(ValueError):
            self.store.set_soil_ph_calibration(sensor_id, 15.0)
        with self.assertRaises(ValueError):
            self.store.set_soil_ph_calibration("sensor-nobody", 7.0)

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
            self.store.set_reporting_interval(12)["reporting_interval_minutes"], 12
        )
        self.assertEqual(self.store.sensor(sensor_id)["expected_interval_seconds"], 1800)
        self.assertEqual(self.store.sensor(other_sensor_id)["expected_interval_seconds"], 3600)
        self.assertEqual(self.store.rename_sensor(sensor_id, "Desk fern")["display_name"], "Desk fern")

        self.store.delete_sensor(sensor_id)

        self.assertIsNone(self.store.sensor(sensor_id))
        self.assertEqual(self.store.history(sensor_id), [])



def _reporting_interval_markup(page: str) -> str:
    """Just the selects that choose a reporting interval."""
    kept = []
    for chunk in page.split("<select")[1:]:
        head, _, body = chunk.partition(">")
        if "reporting-interval" in head:
            kept.append(body.split("</select>")[0])
    assert kept, "no reporting-interval select on the page at all"
    return "\n".join(kept)


class SensorManagementWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        place_sensors(self.store)
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
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


    def test_configuration_page_is_routed_and_reachable_from_both_lists(self) -> None:
        sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        static = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static"
        app = (static / "app.js").read_text()
        onboarding = (static / "onboarding.js").read_text()

        # The hub serves the application for the route as for any other page.
        with self.request(f"/sensors/{quote(sensor_id, safe='')}/settings") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'data-page="sensor-config"', response.read())

        # A sensor's settings are a section of its plant's configuration, so
        # the sensor's own address leads to the plant's page.
        self.assertIn(
            'if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "sensor-config";',
            app,
        )
        # The sensor is still named by the path, with or without the suffix.
        self.assertIn(r"/^\/sensors\/([^/]+)(?:\/settings)?$/", app)
        # Reachable from the plant page.
        self.assertIn("`/plants/${plantId}/config`", app)
        # Polling follows the page: no charts to feed here, and no report log there.
        self.assertIn('if (page === "sensor-config") {', app)




    def test_dashboard_polling_does_not_overlap_or_repeat_calendar_requests(self) -> None:
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()

        self.assertNotIn('setInterval(refresh, 1000)', app)
        self.assertIn('window.setTimeout(poll, 1000)', app)
        self.assertIn('if (wateringCalendarRequestKey === requestKey) return;', app)
        self.assertIn('wateringCalendarRequestKey = requestKey;', app)
        self.assertIn('profileId', app)
        self.assertIn('if (historyRequestKey === requestKey) return;', app)
        self.assertIn('refreshClimateHistory()', app)
        self.assertIn('svgElement("polyline", {', app)
        self.assertIn('class: `history-line series-${entry.colour}`,', app)
        self.assertIn('"history-point latest-point"', app)
        self.assertIn('"live": { label: "Live (up to 24 hours)", milliseconds: 24 * 60 * 60 * 1000, fitSamples: true }', app)
        self.assertIn('let selectedHistoryRange = "live";', app)
        self.assertIn('chartStartTime = firstSampleTime;', app)
        # The right hand edge is now, not the last reading: a chart that
        # ends at the newest sample cannot show a sensor going quiet.
        self.assertNotIn('chartEndTime = lastSampleTime;', app)
        self.assertIn('const endTime = Math.max(newest, Date.now());', app)
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
            ("function renderHistoryChart(", "if (!renderKeyChanged(chart, [shown.map("),
            ("function renderMoistureTrend(", "if (!renderKeyChanged(chart, [moistureTrendVersion,"),
        ]:
            body = app[app.index(renderer):]
            body = body[: body.index("\nfunction ", 1)]
            self.assertIn(guard, body, renderer)
            self.assertLess(body.index(guard), body.index("replaceChildren("), renderer)

    def test_dashboard_polling_updates_cards_without_replacing_grid(self) -> None:
        app = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static" / "app.js").read_text()
        render = app[app.index("function renderHousehold()") : app.index("function tileKey(")]

        # The page refreshes every second; rebuilding the grid would reload
        # every cover photograph and lose whatever the pointer was over.
        self.assertNotIn("grid.replaceChildren()", render)
        self.assertIn('grid.querySelectorAll(".fleet-card[data-key]")', render)
        self.assertIn("updateHouseholdCard(card, tile.plant, tile.sensor);", render)
        self.assertIn("if (cardAtIndex !== card)", render)
        # And a cover is left alone unless it is a different photograph.
        cover = app[app.index("function setFleetCardCover("):]
        self.assertIn('(existing.getAttribute("src") || "") === wanted) return;',
                      cover[: cover.index("\n}")])


    def test_the_time_zone_is_chosen_on_the_settings_page(self) -> None:
        """A hub that reasons about hours has to be told which hours."""
        before = json.load(self.request("/api/settings"))
        self.assertIsNone(before["timezone"])
        self.assertTrue(before["timezone_effective"])

        # The menu comes from this machine's tzdata, so it cannot go stale.
        offered = json.load(self.request("/api/settings/timezones"))
        self.assertIn("America/Vancouver", offered["items"])
        self.assertIn("Asia/Tokyo", offered["items"])

        saved = json.load(self.request(
            "/api/settings/timezone", "PUT", {"timezone": "Asia/Tokyo"},
            origin=self.base_url))
        self.assertEqual(saved["timezone"], "Asia/Tokyo")
        self.assertEqual(json.load(self.request("/api/settings"))["timezone"], "Asia/Tokyo")

        with self.assertRaises(HTTPError) as refused:
            self.request("/api/settings/timezone", "PUT",
                         {"timezone": "Mars/Olympus_Mons"}, origin=self.base_url)
        self.assertEqual(refused.exception.code, 400)
        self.assertEqual(json.load(self.request("/api/settings"))["timezone"], "Asia/Tokyo",
                         "a refused zone leaves the saved one alone")

        # Handed back to the machine.
        self.assertIsNone(json.load(self.request(
            "/api/settings/timezone", "PUT", {"timezone": None},
            origin=self.base_url))["timezone"])

        page = self.request("/").read().decode("utf-8")
        self.assertIn('id="hub-timezone"', page)
        self.assertIn('href="/settings/time"', page)
        app = self.request("/app.js").read().decode("utf-8")
        # Filled once: six hundred options rebuilt on a poll would be six
        # hundred nodes replaced under somebody choosing from them.
        self.assertIn("hubTimezoneFilled", app)
        self.assertIn('dataset.edited !== "true"', app)


if __name__ == "__main__":
    unittest.main()