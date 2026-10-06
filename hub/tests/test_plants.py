"""Plants the hub holds without a sensor.

Most households have more plants than sensors. These cover the plant that
nothing is watching: it is created by hand, cared for by hand, and still has a
history and a journey of its own.
"""

from datetime import datetime, timedelta, timezone
import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.web import create_server, server_address


def observe(store: ReadingStore, sensor_id: str = "sensor-one") -> None:
    store.record_beacon(
        sensor_id=sensor_id,
        received_at="2026-10-05T00:00:00Z",
        observed_identifier="test",
        source_adapter="test",
        rssi=-40,
        service_data=b"\x40\x00\x01",
        contract_version=3,
    )


class ManualPlantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.room = self.store.create_room("Study", "east", "bright", None)

    def tearDown(self) -> None:
        self.store.close()

    def test_a_plant_can_be_taken_on_with_no_sensor_in_sight(self) -> None:
        plant = self.store.create_plant("Fiddle fig", self.room["room_id"], 10)

        self.assertIsNone(plant["sensor_id"])
        self.assertEqual(plant["room"], "Study")
        self.assertEqual(plant["expected_watering_days"], 10)
        self.assertIsNotNone(plant["created_at"])
        self.assertEqual([p["plant_id"] for p in self.store.plants()], [plant["plant_id"]])

    def test_its_care_is_recorded_against_the_plant_itself(self) -> None:
        plant = self.store.create_plant("Fiddle fig")

        self.store.record_plant_care(plant["plant_id"], "watering")
        self.store.record_plant_care(plant["plant_id"], "fertilizing")
        journey = self.store.record_plant_care(
            plant["plant_id"], "note", note="Repotted into a 30 cm pot."
        )

        self.assertEqual(journey["watering_count"], 1)
        self.assertEqual(journey["fertilizing_count"], 1)
        log = self.store.care_log(10, plant_id=plant["plant_id"])
        self.assertEqual(
            sorted(entry["kind"] for entry in log), ["fertilizing", "note", "watering"]
        )
        # Nothing noticed any of it, because nothing was watching.
        self.assertTrue(all(entry["sensor_id"] in (None, "") for entry in log))

    def test_watering_is_once_a_day_and_a_note_is_not(self) -> None:
        plant = self.store.create_plant("Fiddle fig")

        for _ in range(3):
            self.store.record_plant_care(plant["plant_id"], "watering")
        self.store.record_plant_care(plant["plant_id"], "note", note="Leaf drop")
        self.store.record_plant_care(plant["plant_id"], "note", note="Moved it")

        self.assertEqual(self.store.plant_journey_of(plant["plant_id"])["watering_count"], 1)
        notes = [
            entry
            for entry in self.store.care_log(10, plant_id=plant["plant_id"])
            if entry["kind"] == "note"
        ]
        self.assertEqual(len(notes), 2)

    def test_days_are_counted_from_the_day_it_was_taken_on(self) -> None:
        # A plant with no readings is still being looked after, so its days
        # cannot run from a first reading that never comes.
        plant = self.store.create_plant("Fiddle fig")

        journey = self.store.plant_journey_of(plant["plant_id"])

        self.assertEqual(journey["started_at"], plant["created_at"])
        self.assertEqual(journey["monitored_days"], 1)

    def test_a_plant_with_a_sensor_is_let_go_of_through_the_sensor(self) -> None:
        observe(self.store)
        self.store.manage_sensor("sensor-one", "Fern", "Office", "strelitzia", None, None, 1800)
        plant_id = self.store.sensor("sensor-one")["plant_id"]

        with self.assertRaisesRegex(ValueError, "forget the sensor instead"):
            self.store.delete_plant(plant_id)

    def test_letting_a_manual_plant_go_takes_its_history_with_it(self) -> None:
        plant = self.store.create_plant("Fiddle fig")
        self.store.record_plant_care(plant["plant_id"], "watering")

        self.store.delete_plant(plant["plant_id"])

        self.assertEqual(self.store.plants(), [])
        self.assertEqual(self.store.care_log(10, plant_id=plant["plant_id"]), [])

    def test_renaming_a_plant_renames_what_watches_it(self) -> None:
        observe(self.store)
        self.store.manage_sensor("sensor-one", "Fern", "Office", "strelitzia", None, None, 1800)
        plant_id = self.store.sensor("sensor-one")["plant_id"]

        self.store.update_plant(plant_id, "Bird of paradise", self.room["room_id"], None)

        # Two pages showing one plant must not disagree about its name.
        self.assertEqual(self.store.sensor("sensor-one")["display_name"], "Bird of paradise")

    def test_it_refuses_what_it_cannot_record(self) -> None:
        plant = self.store.create_plant("Fiddle fig")
        for description, call in (
            ("no name", lambda: self.store.create_plant("  ")),
            ("unknown room", lambda: self.store.create_plant("Ivy", 404)),
            ("silly cadence", lambda: self.store.create_plant("Ivy", None, 0)),
            ("unknown kind", lambda: self.store.record_plant_care(plant["plant_id"], "singing")),
            ("empty note", lambda: self.store.record_plant_care(plant["plant_id"], "note")),
            ("unknown plant", lambda: self.store.record_plant_care(404, "watering")),
        ):
            with self.subTest(description), self.assertRaises(ValueError):
                call()


def ago(days: float) -> str:
    """A timestamp that many days back, for a cycle that has to span time."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlantWeightTests(unittest.TestCase):
    """For a pot, mass is the measure of water with nothing assumed about soil."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.plant_id = self.store.create_plant("Bird of paradise")["plant_id"]

    def tearDown(self) -> None:
        self.store.close()

    def test_a_pot_with_one_weighing_knows_what_it_weighs_and_nothing_else(self) -> None:
        self.store.record_plant_weight(self.plant_id, 17_400)

        status = self.store.plant_weight_status(self.plant_id)

        self.assertEqual(status["latest"]["grams"], 17_400)
        # Everything else needs a second number to mean anything.
        for absent in ("watered", "thirsty", "used_grams", "capacity_grams",
                       "remaining_percent", "daily_grams", "days_left"):
            self.assertIsNone(status[absent], absent)

    def test_the_ceiling_turns_later_weighings_into_water_used(self) -> None:
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(6))
        self.store.record_plant_weight(self.plant_id, 16_700, at=ago(0))

        status = self.store.plant_weight_status(self.plant_id)

        # Grams of water gone, which is millilitres.
        self.assertEqual(status["used_grams"], 700)
        self.assertEqual(status["daily_grams"], 117)

    def test_a_floor_as_well_gives_what_is_left_and_how_long(self) -> None:
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(10))
        self.store.record_plant_weight(self.plant_id, 16_200, "thirsty", at=ago(9))
        self.store.record_plant_weight(self.plant_id, 16_800, at=ago(0))

        status = self.store.plant_weight_status(self.plant_id)

        self.assertEqual(status["capacity_grams"], 1_200)
        self.assertEqual(status["remaining_percent"], 50)
        self.assertIsNotNone(status["days_left"])

    def test_two_weighings_an_hour_apart_say_nothing_about_a_day(self) -> None:
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(0.05))
        self.store.record_plant_weight(self.plant_id, 17_380, at=ago(0))

        # That difference is the scale, not the plant.
        self.assertIsNone(self.store.plant_weight_status(self.plant_id)["daily_grams"])

    def test_the_rate_is_measured_since_the_watering_not_across_it(self) -> None:
        # A rate measured across a watering would be a rate of filling.
        self.store.record_plant_weight(self.plant_id, 16_300, at=ago(12))
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(10))
        self.store.record_plant_weight(self.plant_id, 16_900, at=ago(0))

        self.assertEqual(self.store.plant_weight_status(self.plant_id)["daily_grams"], 50)

    def test_recording_the_watered_weight_records_the_watering(self) -> None:
        # That weighing is taken because the plant has just been watered, and
        # saying so twice invites the two to disagree.
        self.store.record_plant_weight(self.plant_id, 17_400, "watered")

        log = self.store.care_log(10, plant_id=self.plant_id)
        self.assertEqual([entry["kind"] for entry in log], ["watering"])
        self.assertEqual(
            self.store.plant_journey_of(self.plant_id)["watering_count"], 1
        )

    def test_it_refuses_a_weight_no_pot_has(self) -> None:
        for description, grams in (("nothing", 0), ("half a tonne", 600_000)):
            with self.subTest(description), self.assertRaises(ValueError):
                self.store.record_plant_weight(self.plant_id, grams)
        with self.assertRaises(ValueError):
            self.store.record_plant_weight(self.plant_id, 1_000, "floating")
        with self.assertRaises(ValueError):
            self.store.record_plant_weight(404, 1_000)


class PlantApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
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

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2.0) as response:
            return None if response.status == 204 else json.load(response)

    def test_a_plant_is_made_cared_for_and_read_back_over_http(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Fiddle fig"})
        self.call("POST", f"/api/plants/{plant['plant_id']}/care", {"kind": "watering"})

        read = self.call("GET", f"/api/plants/{plant['plant_id']}")

        self.assertEqual(read["display_name"], "Fiddle fig")
        self.assertIsNone(read["sensor_id"])
        # One request carries everything the page draws.
        self.assertEqual(read["journey"]["watering_count"], 1)
        self.assertEqual([entry["kind"] for entry in read["care_log"]], ["watering"])
        self.assertIsNotNone(read["last_watered_at"])

    def test_a_plant_can_be_renamed_and_let_go_of(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Fiddle fig"})
        path = f"/api/plants/{plant['plant_id']}"

        renamed = self.call("PUT", path, {"display_name": "Fiddle-leaf fig"})
        self.assertEqual(renamed["display_name"], "Fiddle-leaf fig")

        self.call("DELETE", path)
        self.assertEqual(self.call("GET", "/api/plants")["items"], [])

    def test_the_page_is_served_for_a_plant_address(self) -> None:
        with urlopen(self.base_url + "/plants/1", timeout=2.0) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"<title>Open Plant Pulse</title>", response.read())

    def test_a_pot_is_weighed_in_grams_whatever_the_keypad_offered(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Bird of paradise"})
        path = f"/api/plants/{plant['plant_id']}"

        self.call("POST", path + "/weight", {"grams": 17_400, "kind": "watered"})
        read = self.call("GET", path)

        self.assertEqual(read["weight"]["watered"]["grams"], 17_400)
        self.assertEqual(read["weights"][0]["grams"], 17_400)
        # The ceiling is taken after watering, so the watering is on record.
        self.assertEqual(read["journey"]["watering_count"], 1)

    def test_a_pot_can_be_weighed_in_either_unit(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # A 17 kg pot in grams is five digits to get wrong; a seedling in
        # kilograms is 0.42 of one. The scale's owner chooses.
        self.assertIn('id="plant-weight-unit"', page)
        self.assertIn('<option value="g">g</option>', page)
        self.assertIn('Math.round(typed * 1000)', script)
        # Grams on the wire whichever was typed.
        self.assertIn("JSON.stringify({ grams, kind })", script)
        # And the choice outlives the page, per browser.
        self.assertIn("WEIGHT_UNIT_KEY", script)

    def test_the_page_carries_only_what_a_plant_without_a_sensor_has(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # Its own page, its own sections.
        self.assertIn('data-page="plant"', page)
        self.assertIn('id="plant-weight-title"', page)
        self.assertIn('id="plant-care-log"', page)
        self.assertIn('if (path.startsWith("/plants/")) return "plant";', script)
        self.assertIn('id="plant-weight-input"', page)
        # Nothing on it claims a measurement the plant does not have.
        for absent in ("climate-history", "chemistry-grid", "raw-report-log", "sensor-firmware"):
            section = page[page.index('data-page="plant"'):]
            self.assertNotIn(f'{absent}" data-page="plant"', section)

    def test_a_plant_with_no_sensor_is_offered_on_the_household_page(self) -> None:
        script = self.call_text("/app.js")

        self.assertIn('plant.sensor_id === null', script)
        self.assertIn("/plants/${plant.plant_id}", script)
        # A plant is not faulty for having no sensor, and the page must not
        # tell somebody to enrol one above a list of their own plants.
        self.assertIn('"manual"', script)
        self.assertIn("No plants yet. Add one", script)

    def call_text(self, path: str) -> str:
        with urlopen(self.base_url + path, timeout=2.0) as response:
            return response.read().decode("utf-8")

    def test_an_unknown_plant_is_a_404_and_a_bad_one_a_400(self) -> None:
        for path, code in (("/api/plants/404", 404), ("/api/plants/fig", 400)):
            with self.subTest(path), self.assertRaises(HTTPError) as raised:
                self.call("GET", path)
            self.assertEqual(raised.exception.code, code)
            raised.exception.close()


if __name__ == "__main__":
    unittest.main()
