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
from urllib.parse import quote
from urllib.request import Request, urlopen

from pathlib import Path
from tempfile import TemporaryDirectory

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.application.store import vapour_pressure_deficit
from open_plant_pulse_hub.application.photos import PhotoLibrary
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


# The smallest things the hub will recognise as pictures.
PNG = bytes.fromhex("89504e470d0a1a0a") + b"a png, as far as anyone here cares"
JPEG = b"\xff\xd8\xff" + b"a jpeg, as far as anyone here cares"


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

    def test_correcting_an_anchor_is_not_a_second_watering(self) -> None:
        # Saying the full pot weighs 17.4 kg is a calibration, not an event.
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 16_200, "thirsty")

        self.assertEqual(self.store.care_log(10, plant_id=self.plant_id), [])
        status = self.store.plant_weight_status(self.plant_id)
        self.assertEqual(status["watered"]["grams"], 17_400)
        self.assertEqual(status["thirsty"]["grams"], 16_200)
        self.assertEqual(status["capacity_grams"], 1_200)

    def test_a_typed_anchor_is_not_what_the_pot_weighs_now(self) -> None:
        # Saying "it weighs 16.2 kg dry" is a claim about a state the pot is
        # not in; it must not become the latest weighing.
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(5))
        self.store.record_plant_weight(self.plant_id, 16_900, at=ago(1))
        self.store.record_plant_weight(self.plant_id, 16_200, "thirsty", observed=False)

        status = self.store.plant_weight_status(self.plant_id)
        self.assertEqual(status["latest"]["grams"], 16_900)
        self.assertEqual(status["thirsty"]["grams"], 16_200)
        self.assertEqual(status["used_grams"], 500)
        # And the claim is no part of how fast the pot is drying.
        self.assertEqual(status["daily_grams"], 125)

    def test_a_pot_with_only_typed_anchors_has_no_weight_yet(self) -> None:
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 16_200, "thirsty", observed=False)

        status = self.store.plant_weight_status(self.plant_id)
        self.assertIsNone(status["latest"])
        self.assertIsNone(status["used_grams"])
        self.assertEqual(status["capacity_grams"], 1_200)
        self.assertEqual(self.store.care_log(10, plant_id=self.plant_id), [])

    def test_the_newest_anchor_is_the_one_that_counts(self) -> None:
        # A repot or a season's growth gives a new full weight; the old one
        # stays in the history rather than being overwritten.
        self.store.record_plant_weight(self.plant_id, 17_400, "watered", at=ago(30),
                                       observed=False)
        self.store.record_plant_weight(self.plant_id, 19_000, "watered", at=ago(1),
                                       observed=False)

        self.assertEqual(
            self.store.plant_weight_status(self.plant_id)["watered"]["grams"], 19_000
        )
        self.assertEqual(len(self.store.plant_weights(self.plant_id)), 2)

    def test_water_use_is_measured_between_weighings(self) -> None:
        """A pot on a scale is a weighing lysimeter; this is what it reads."""
        self.store.record_plant_weight(self.plant_id, 2_800, "watered", at=ago(14))
        self.store.record_plant_weight(self.plant_id, 2_650, at=ago(11))
        self.store.record_plant_weight(self.plant_id, 2_500, at=ago(8))
        # Minutes apart: scale noise, not a day's drinking.
        self.store.record_plant_weight(self.plant_id, 2_495, at=ago(8 - 0.01))
        self.store.record_plant_weight(self.plant_id, 2_300, at=ago(4))
        # A watering, so the span that holds it cannot be measured at all.
        self.store.record_plant_weight(self.plant_id, 2_900, "watered", at=ago(3))
        self.store.record_plant_weight(self.plant_id, 2_700, at=ago(0.5))

        use = self.store.water_use(self.plant_id, days=30)
        spans = [(i["days"], i["grams"], i["grams_per_day"]) for i in use["intervals"]]

        self.assertEqual(spans, [(3.0, 150, 50), (3.0, 150, 50), (3.99, 195, 49),
                                 (2.5, 200, 80)])
        # The pair minutes apart is one weighing, not a span: the later
        # figure stands and the span starts from it.
        self.assertNotIn(0.0, [span[0] for span in spans])
        # Nothing claims to know about the days between the watering and the
        # weighing after it.
        self.assertEqual(use["measured_days"], 12.5)
        self.assertEqual(use["used_grams"], 695)
        self.assertEqual(use["daily_grams"], 56)
        self.assertEqual((use["lowest_daily_grams"], use["highest_daily_grams"]), (49, 80))

    def test_a_pot_reweighed_minutes_later_is_not_drinking(self) -> None:
        """2762 grams and then 2055 twelve minutes later is a correction."""
        self.store.record_plant_weight(self.plant_id, 2_762, "watered", at=ago(2))
        self.store.record_plant_weight(self.plant_id, 2_055, at=ago(2 - 0.008))
        self.store.record_plant_weight(self.plant_id, 1_932, at=ago(1))
        self.store.record_plant_weight(self.plant_id, 1_850, at=ago(0.02))

        rates = [i["grams_per_day"] for i in
                 self.store.water_use(self.plant_id)["intervals"]]

        # Not 700 grams of water before lunch.
        self.assertTrue(all(rate < 200 for rate in rates), rates)
        self.assertEqual(len(rates), 2)

    def water_history(self, pattern):
        for days, grams in pattern:
            self.store.record_plant_weight(self.plant_id, grams, at=ago(days))

    def test_a_plant_that_stops_drinking_with_a_full_pot_is_worth_saying(self) -> None:
        # The one case where the obvious response makes things worse: water
        # the plant is not taking is not water the plant is short of.
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(20, 2_900), (16, 2_800), (12, 2_700), (8, 2_600),
                            (4, 2_590), (0.2, 2_585)])

        findings = self.store.water_use_findings(self.plant_id)
        self.assertEqual([f["code"] for f in findings], ["drinking_less_while_full"])
        self.assertIn("before adding more", findings[0]["summary"])

        # And it is in the care log, as a reading rather than an event.
        logged = [e for e in self.store.care_log(10, plant_id=self.plant_id)
                  if e["kind"] == "observation"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["changes"]["source"], "water-use")
        self.assertEqual(logged[0]["confidence"], "derived")
        # Saying it twice on one day adds nothing.
        self.store.record_water_use_findings(self.plant_id)
        self.assertEqual(
            len([e for e in self.store.care_log(10, plant_id=self.plant_id)
                 if e["kind"] == "observation"]), 1)

    def test_the_same_slowdown_with_an_empty_pot_means_the_opposite(self) -> None:
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(20, 2_900), (16, 2_750), (12, 2_600), (8, 2_450),
                            (4, 2_120), (0.2, 2_100)])

        findings = self.store.water_use_findings(self.plant_id)
        self.assertEqual([f["code"] for f in findings], ["drinking_less_and_low"])

    def test_steady_drinking_is_not_worth_a_word(self) -> None:
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(20, 2_900), (16, 2_800), (12, 2_700), (8, 2_600),
                            (4, 2_500), (0.05, 2_401)])

        self.assertEqual(self.store.water_use_findings(self.plant_id), [])
        self.assertEqual([e for e in self.store.care_log(10, plant_id=self.plant_id)
                          if e["kind"] == "observation"], [])

    def test_a_weighing_written_down_late_says_nothing_about_today(self) -> None:
        # Filling in last week should not have the hub announcing findings
        # against a history it is halfway through being given.
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(20, 2_900), (16, 2_800), (12, 2_700), (8, 2_600),
                            (4, 2_590), (0.2, 2_585)])
        before = len([e for e in self.store.care_log(20, plant_id=self.plant_id)
                      if e["kind"] == "observation"])

        self.store.record_plant_weight(self.plant_id, 2_850, at=ago(17))

        self.assertEqual(
            len([e for e in self.store.care_log(20, plant_id=self.plant_id)
                 if e["kind"] == "observation"]), before)

    def air_in_the_room(self, room_id, early, late, switch_days=4, span_days=21):
        """A sensor in the room, reporting the air hourly."""
        self.store._database.execute(
            "INSERT INTO sensors (sensor_id, identity_kind, identity_value,"
            " enrollment_status, display_name, room_id, transport, contract_version,"
            " first_seen_at, last_seen_at) VALUES"
            " ('sensor-air','mac','aa','enrolled','Air',?,'ble',1,?,?)",
            (room_id, ago(60), ago(0)))
        for hour in range(span_days * 24):
            days = span_days - hour / 24
            celsius, humidity = early if days > switch_days else late
            self.store._database.execute(
                "INSERT INTO sensor_readings (sensor_id, observed_at, received_at,"
                " air_temperature_c, air_humidity_percent) VALUES (?,?,?,?,?)",
                ("sensor-air", ago(days), ago(days), celsius, humidity))
        self.store._database.commit()

    def test_the_drying_power_of_a_room_is_worked_out_from_its_air(self) -> None:
        # Tetens, against the textbook figures.
        for celsius, humidity, expected in [(21, 50, 1.24), (25, 40, 1.90), (18, 65, 0.72)]:
            self.assertAlmostEqual(
                vapour_pressure_deficit(celsius, humidity), expected, places=2)
        # Saturated air pulls nothing, whatever its temperature.
        self.assertEqual(vapour_pressure_deficit(30, 100), 0.0)

    def test_a_change_the_room_explains_is_reported_as_the_room(self) -> None:
        """The air belongs to the room, so a sensor in it speaks for this pot."""
        room = self.store.create_room("Living", "south", "bright")["room_id"]
        self.store.update_plant(self.plant_id, "Spider", room_id=room)
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        # Cool and damp, then warm and dry.
        self.air_in_the_room(room, (18, 80), (26, 30))
        self.water_history([(20, 2_900), (16, 2_850), (12, 2_800), (8, 2_750),
                            (4, 2_700), (0.2, 2_500)])

        findings = self.store.water_use_findings(self.plant_id)
        self.assertEqual([f["code"] for f in findings], ["the_air_changed"])
        # Four times the water, and nearly all of it the air.
        self.assertGreater(findings[0]["evidence"]["share_of_usual"], 3)
        self.assertLess(abs(findings[0]["evidence"]["share_against_air"] - 1), 0.35)

    def test_a_change_the_room_does_not_explain_is_still_the_plant(self) -> None:
        room = self.store.create_room("Living", "south", "bright")["room_id"]
        self.store.update_plant(self.plant_id, "Spider", room_id=room)
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        # The room did turn damper, but nowhere near enough to account for
        # a plant that all but stopped drinking.
        self.air_in_the_room(room, (26, 30), (19, 78))
        self.water_history([(20, 2_900), (16, 2_800), (12, 2_700), (8, 2_600),
                            (4, 2_590), (0.2, 2_585)])

        findings = self.store.water_use_findings(self.plant_id)
        self.assertEqual([f["code"] for f in findings], ["drinking_less_while_full"])
        self.assertLess(findings[0]["evidence"]["share_against_air"], 0.5)

    def test_the_room_tells_every_plant_in_it_what_the_air_is_doing(self) -> None:
        """Air belongs to the room, not to the pot the probe is in."""
        room = self.store.create_room("Living", "south", "bright")["room_id"]
        self.store.update_plant(self.plant_id, "Spider", room_id=room)
        elsewhere = self.store.create_room("Bedroom", "north", "low")["room_id"]
        other = self.store.create_plant("Fern", room_id=elsewhere)["plant_id"]
        roomless = self.store.create_plant("Orchid")["plant_id"]
        self.air_in_the_room(room, (21, 50), (21, 50), span_days=1)

        air = self.store.room_air(room)
        self.assertEqual(air["sensor_name"], "Air")
        self.assertEqual(air["air_temperature_c"], 21.0)
        self.assertAlmostEqual(air["drying_kpa"], 1.24, places=2)

        # A plant with no hardware of its own, in that room, is told it.
        self.assertEqual(
            self.store.room_air(self.store.plant(self.plant_id)["room_id"])["sensor_id"],
            "sensor-air")
        # And nothing is borrowed across rooms, or invented for a plant in none.
        self.assertIsNone(self.store.room_air(elsewhere))
        self.assertIsNone(self.store.room_air(self.store.plant(roomless)["room_id"]))
        self.assertIsNone(self.store.plant(other)["room_id"] and
                          self.store.room_air(self.store.plant(other)["room_id"]))

    def test_a_plant_in_a_room_with_no_sensor_keeps_its_raw_rate(self) -> None:
        # Nothing is borrowed from another room's air.
        self.store.record_plant_weight(self.plant_id, 2_800, at=ago(8))
        self.store.record_plant_weight(self.plant_id, 2_400, at=ago(4))
        use = self.store.water_use(self.plant_id)
        self.assertFalse(use["drying_known"])
        self.assertIsNone(use["intervals"][0]["drying_kpa"])
        self.assertIsNone(use["intervals"][0]["grams_per_day_per_kpa"])

    def test_water_use_says_nothing_before_there_is_anything_to_say(self) -> None:
        empty = self.store.water_use(self.plant_id)
        self.assertEqual(empty["intervals"], [])
        self.assertIsNone(empty["daily_grams"])
        self.assertEqual(empty["measured_days"], 0)

        # One weighing is a weight, not a rate.
        self.store.record_plant_weight(self.plant_id, 2_400, at=ago(2))
        self.assertEqual(self.store.water_use(self.plant_id)["intervals"], [])

        # A pot that gained weight with no watering recorded is not drinking
        # backwards; it is something the scale cannot explain, so it is left out.
        self.store.record_plant_weight(self.plant_id, 2_600, at=ago(1))
        self.assertEqual(self.store.water_use(self.plant_id)["intervals"], [])

    def test_a_weighing_can_be_written_down_late(self) -> None:
        # The number was on a scrap of paper from Tuesday.
        self.store.record_plant_weight(self.plant_id, 2_400, at=ago(9))
        self.store.record_plant_weight(self.plant_id, 2_100, at=ago(1))

        weighings = self.store.plant_weights(self.plant_id)
        self.assertEqual([row["grams"] for row in weighings], [2_100, 2_400])
        # Dated where it belongs, so it is the older of the two.
        self.assertLess(weighings[1]["at"], weighings[0]["at"])

    def test_it_refuses_a_weighing_that_has_not_happened(self) -> None:
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(ValueError):
            self.store.record_plant_weight(self.plant_id, 2_400, at=tomorrow)
        with self.assertRaises(ValueError):
            self.store.record_plant_weight(self.plant_id, 2_400, at="last Tuesday")
        # A moment with no time zone is not a moment.
        with self.assertRaises(ValueError):
            self.store.record_plant_weight(self.plant_id, 2_400, at="2026-10-01T12:00:00")
        self.assertEqual(self.store.plant_weights(self.plant_id), [])

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
        self.directory = TemporaryDirectory()
        self.photo_directory = Path(self.directory.name)
        self.server = create_server(
            self.store, "127.0.0.1", 0,
            photos=PhotoLibrary(self.store, self.photo_directory),
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.store.close()
        self.directory.cleanup()

    def upload_photo(self, plant_id, body, taken_at=None):
        query = f"?taken_at={quote(taken_at)}" if taken_at else ""
        request = Request(
            f"{self.base_url}/api/plants/{plant_id}/photos{query}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urlopen(request, timeout=2.0) as response:
            return json.load(response)

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
        for address in ("/plants/1", "/plants/1/config"):
            with urlopen(self.base_url + address, timeout=2.0) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"<title>Open Plant Pulse</title>", response.read())

    def test_a_plant_is_told_where_it_lives_and_how_often_it_drinks(self) -> None:
        room = self.call("POST", "/api/rooms", {"name": "Study", "aspect": "north", "light": "low"})
        plant = self.call("POST", "/api/plants", {"display_name": "Fern"})
        path = f"/api/plants/{plant['plant_id']}"

        saved = self.call("PUT", path, {
            "display_name": "Boston fern",
            "room_id": room["room_id"],
            "expected_watering_days": 4,
        })

        self.assertEqual(saved["display_name"], "Boston fern")
        self.assertEqual(saved["room"], "Study")
        self.assertEqual(saved["expected_watering_days"], 4)
        # And a plant may be in no room at all, which is not the same as the
        # question never having been asked.
        homeless = self.call("PUT", path, {"display_name": "Boston fern", "room_id": None})
        self.assertIsNone(homeless["room_id"])

    def test_a_plant_is_told_what_kind_of_plant_it_is(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Golden pothos"})
        path = f"/api/plants/{plant['plant_id']}"
        self.assertIsNone(plant["profile_id"])

        saved = self.call("PUT", path, {"display_name": "Golden pothos", "profile_id": "pothos"})
        self.assertEqual(saved["profile_id"], "pothos")

        # A profile that does not exist is refused rather than stored.
        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", path, {"display_name": "Golden pothos", "profile_id": "nonesuch"})
        self.assertEqual(refused.exception.code, 400)
        self.assertEqual(self.call("GET", path)["profile_id"], "pothos")

        # And a plant may be no particular kind of plant.
        self.assertIsNone(
            self.call("PUT", path, {"display_name": "Golden pothos"})["profile_id"]
        )

    def test_a_plant_is_told_which_variety_it_is(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Pothos"})
        path = f"/api/plants/{plant['plant_id']}"

        saved = self.call("PUT", path, {
            "display_name": "Pothos", "profile_id": "pothos", "variety": "marble_queen",
        })
        self.assertEqual(saved["variety"], "marble_queen")

        # A variety belongs to its profile and nowhere else.
        for refused in (
            {"profile_id": "pothos", "variety": "nonesuch"},
            {"profile_id": "spider_plant", "variety": "marble_queen"},
            {"variety": "marble_queen"},
        ):
            with self.assertRaises(HTTPError) as error:
                self.call("PUT", path, {"display_name": "Pothos", **refused})
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.call("GET", path)["variety"], "marble_queen")

        # Changing the profile drops a variety the new one does not know.
        moved = self.call("PUT", path, {"display_name": "Pothos", "profile_id": "spider_plant"})
        self.assertIsNone(moved["variety"])

    def test_every_profile_offers_its_varieties(self) -> None:
        catalog = self.call("GET", "/api/plant-profiles")["profiles"]

        pothos = {kind["id"] for kind in catalog["pothos"]["varieties"]}
        self.assertIn("golden", pothos)
        self.assertIn("marble_queen", pothos)
        # The same species, so one profile with two varieties rather than two
        # profiles that would have to be kept in step.
        self.assertEqual(catalog["pothos"]["scientific_name"], "Epipremnum aureum")
        # Every variety is named and identified; a note is optional.
        for profile in catalog.values():
            for kind in profile.get("varieties", []):
                self.assertEqual({"id", "name", "note"} >= set(kind), True)
                self.assertTrue(kind["id"] and kind["name"])

    def test_a_plant_keeps_photographs_and_one_of_them_is_the_cover(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})
        plant_id = plant["plant_id"]
        self.assertIsNone(plant["cover_photo_id"])

        first = self.upload_photo(plant_id, PNG)
        second = self.upload_photo(plant_id, JPEG)
        # The first photograph is the cover, since a plant with one picture
        # and no cover is a question nobody wants asked.
        self.assertTrue(first["cover"])
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         first["photo_id"])

        self.call("PUT", f"/api/plants/{plant_id}/cover", {"photo_id": second["photo_id"]})
        listed = self.call("GET", f"/api/plants/{plant_id}/photos")["items"]
        self.assertEqual([photo["cover"] for photo in listed], [True, False])

        # The bytes come back as what they are, not as what was declared.
        with urlopen(self.base_url + f"/api/photos/{first['photo_id']}", timeout=2.0) as served:
            self.assertEqual(served.headers["Content-Type"], "image/png")
            self.assertEqual(served.read(), PNG)

        # Removing the cover falls back to what is left rather than to nothing.
        self.call("DELETE", f"/api/photos/{second['photo_id']}")
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         first["photo_id"])
        self.call("DELETE", f"/api/photos/{first['photo_id']}")
        self.assertIsNone(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"])
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}/photos")["items"], [])

    def test_a_photograph_is_asked_for_by_its_contents(self) -> None:
        # SQLite hands a deleted row's id to the next one, so an id alone is
        # not a stable name for a picture the browser keeps for a year.
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        first = self.upload_photo(plant_id, PNG)
        self.call("DELETE", f"/api/photos/{first['photo_id']}")
        second = self.upload_photo(plant_id, JPEG)

        self.assertEqual(second["photo_id"], first["photo_id"])
        self.assertNotEqual(second["digest"], first["digest"])
        # Which is why the page asks with the digest, and the plant carries
        # its cover's so a card can do the same.
        script = self.call_text("/app.js")
        self.assertIn("function photoUrl(photoId, digest)", script)
        self.assertIn("photoUrl(photo.photo_id, photo.digest)", script)
        self.assertIn("setFleetCardCover(card,", script)
        self.assertEqual(
            self.call("GET", f"/api/plants/{plant_id}")["cover_photo_digest"],
            second["digest"],
        )
        # And the query string is ignored by the hub itself.
        with urlopen(self.base_url + f"/api/photos/{second['photo_id']}?v=whatever",
                     timeout=2.0) as served:
            self.assertEqual(served.read(), JPEG)

    def test_a_sensor_can_join_a_plant_the_hub_already_knows(self) -> None:
        """A plant kept by hand for a season is the same plant with a probe in it."""
        room = self.store.create_room("Living", "south", "bright")["room_id"]
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})
        plant_id = plant["plant_id"]
        self.call("PUT", f"/api/plants/{plant_id}", {
            "display_name": "Spider", "room_id": room, "profile_id": "spider_plant",
            "variety": "vittatum", "acquired_at": "2019-07-14T12:00:00Z",
        })
        self.call("POST", f"/api/plants/{plant_id}/weight", {"grams": 2_400, "kind": "watered"})
        self.upload_photo(plant_id, PNG)
        self.store._database.execute(
            "INSERT INTO sensors (sensor_id, identity_kind, identity_value,"
            " enrollment_status, display_name, transport, contract_version,"
            " first_seen_at, last_seen_at) VALUES"
            " ('sensor-new','mac','bb','unclaimed','','ble',1,?,?)", (ago(1), ago(0)))
        self.store._database.commit()

        self.call("PUT", "/api/sensors/sensor-new", {
            "expected_interval_seconds": 1800, "room_id": room, "plant_id": plant_id,
        })

        # One plant, not two, and everything about it still there.
        self.assertEqual(len(self.call("GET", "/api/plants")["items"]), 1)
        joined = self.call("GET", f"/api/plants/{plant_id}")
        self.assertEqual(joined["sensor_id"], "sensor-new")
        self.assertEqual(joined["variety"], "vittatum")
        self.assertEqual(joined["acquired_at"], "2019-07-14T12:00:00Z")
        self.assertEqual(joined["weight"]["watered"]["grams"], 2_400)
        self.assertEqual(len(joined["photos"] if "photos" in joined else
                             self.call("GET", f"/api/plants/{plant_id}/photos")["items"]), 1)

        # And a sensor already on one plant is not quietly moved to another.
        spare = self.call("POST", "/api/plants", {"display_name": "Fern"})["plant_id"]
        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", "/api/sensors/sensor-new", {
                "expected_interval_seconds": 1800, "plant_id": spare})
        self.assertEqual(refused.exception.code, 400)

    def test_a_sensor_moves_from_one_plant_to_another(self) -> None:
        """Adopting a probe and aiming it are separate decisions."""
        first = self.call("POST", "/api/plants", {"display_name": "Plant A"})["plant_id"]
        second = self.call("POST", "/api/plants", {"display_name": "Plant B"})["plant_id"]
        self.store._database.execute(
            "INSERT INTO sensors (sensor_id, identity_kind, identity_value,"
            " enrollment_status, display_name, transport, contract_version,"
            " first_seen_at, last_seen_at, plant_id) VALUES"
            " ('probe','mac','cc','enrolled','Probe','ble',1,?,?,?)",
            (ago(9), ago(0), first))
        self.store._database.commit()
        self.store.record_plant_care(first, "watering", at=ago(5))
        self.call("POST", f"/api/plants/{first}/weight", {"grams": 2_400})

        self.call("PUT", "/api/sensors/probe/plant", {"plant_id": second})

        self.assertIsNone(self.call("GET", f"/api/plants/{first}")["sensor_id"])
        self.assertEqual(self.call("GET", f"/api/plants/{second}")["sensor_id"], "probe")
        # What happened to the first plant happened to it: moving the probe
        # does not move the watering it detected.
        kept = self.call("GET", f"/api/plants/{first}")
        self.assertEqual(len(kept["care_log"]), 1)
        self.assertEqual(kept["weight"]["latest"]["grams"], 2_400)

        # And off entirely, without being released or set up again.
        self.call("PUT", "/api/sensors/probe/plant", {"plant_id": None})
        self.assertIsNone(self.call("GET", f"/api/plants/{second}")["sensor_id"])

    def test_only_an_adopted_sensor_can_be_pointed_at_a_plant(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Plant"})["plant_id"]
        self.store._database.execute(
            "INSERT INTO sensors (sensor_id, identity_kind, identity_value,"
            " enrollment_status, display_name, transport, contract_version,"
            " first_seen_at, last_seen_at) VALUES"
            " ('stranger','mac','dd','unclaimed','','ble',1,?,?)", (ago(1), ago(0)))
        self.store._database.commit()

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", "/api/sensors/stranger/plant", {"plant_id": plant})
        self.assertEqual(refused.exception.code, 400)

        page = self.call_text("/")
        script = self.call_text("/app.js")
        self.assertIn('id="plant-sensor-attach"', page)
        self.assertIn('id="plant-sensor-detach"', page)
        self.assertIn("function renderFreeSensors()", script)

    def test_having_no_sensor_is_not_worth_a_badge(self) -> None:
        script = self.call_text("/app.js")
        page = self.call_text("/")

        self.assertNotIn('"No sensor"', script)
        # The sensor's own setup offers the plants nothing is watching.
        self.assertIn('id="onboarding-plant"', page)
        self.assertIn("async function renderPlantChoices()", self.call_text("/onboarding.js"))

    def test_a_plant_with_a_sensor_has_photographs_too(self) -> None:
        # The photographs are the plant's, not the instrument's, so the
        # sensored page shows the same section.
        page = self.call_text("/")
        script = self.call_text("/app.js")
        self.assertIn('class="plant-photos" data-page="plant detail"', page)
        self.assertIn("selectedSensor?.plant_id", script)

    def test_a_photograph_keeps_the_day_it_was_taken(self) -> None:
        # A record meant to outlast the year it started in cannot file a
        # picture from three summers ago under the day it was uploaded.
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        old_photo = self.upload_photo(plant_id, PNG, taken_at="2019-07-14T17:32:05Z")
        self.assertEqual(old_photo["taken_at"], "2019-07-14T17:32:05Z")
        self.assertNotEqual(old_photo["taken_at"][:4], old_photo["added_at"][:4])

        # Newest first by when it was taken, so the uploading order does not
        # decide the order the plant grew in.
        recent = self.upload_photo(plant_id, JPEG, taken_at="2026-05-01T09:00:00Z")
        listed = self.call("GET", f"/api/plants/{plant_id}/photos")["items"]
        self.assertEqual([p["photo_id"] for p in listed],
                         [recent["photo_id"], old_photo["photo_id"]])

    def test_a_date_can_be_set_on_a_photograph_that_had_none(self) -> None:
        # Pictures taken before any of this existed, or stripped of their
        # metadata on the way here, have only somebody's memory to go on.
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        photo = self.upload_photo(plant_id, PNG)
        self.assertIsNone(photo["taken_at"])

        dated = self.call("PUT", f"/api/photos/{photo['photo_id']}",
                          {"taken_at": "2019-07-14T12:00:00Z"})
        self.assertEqual(dated["taken_at"], "2019-07-14T12:00:00Z")

        # And unsaid, for a date given in error.
        self.assertIsNone(
            self.call("PUT", f"/api/photos/{photo['photo_id']}", {"taken_at": None})["taken_at"]
        )

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", f"/api/photos/{photo['photo_id']}", {"taken_at": "2099-01-01T12:00:00Z"})
        self.assertEqual(refused.exception.code, 400)

        page = self.call_text("/")
        script = self.call_text("/app.js")
        # A date to type on the way in, and the caption is the control that
        # corrects it afterwards.
        self.assertIn('id="plant-photo-date"', page)
        self.assertIn("function chosenPhotoDate()", script)
        self.assertIn("async function setPhotoDate(photoId, date)", script)

    def test_it_keeps_no_date_rather_than_a_wrong_one(self) -> None:
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        # A camera with a flat clock, and one running ahead.
        for nonsense in ("1970-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "whenever"):
            photo = self.upload_photo(plant_id, PNG + nonsense.encode(), taken_at=nonsense)
            self.assertIsNone(photo["taken_at"], nonsense)

    def test_a_plant_says_how_long_it_has_lived_here(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})
        path = f"/api/plants/{plant['plant_id']}"
        self.assertIsNone(plant["acquired_at"])

        saved = self.call("PUT", path, {
            "display_name": "Spider", "acquired_at": "2019-07-14T12:00:00Z",
        })
        self.assertEqual(saved["acquired_at"], "2019-07-14T12:00:00Z")

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", path, {"display_name": "Spider",
                                    "acquired_at": "2099-01-01T12:00:00Z"})
        self.assertEqual(refused.exception.code, 400)

        script = self.call_text("/app.js")
        self.assertIn("function timeAtHome(since)", script)
        self.assertIn("at home", script)

    def test_it_refuses_what_is_not_a_picture(self) -> None:
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        for body in (b"not a picture", b"\x00\x00\x00\x18ftypheic" + b"x" * 40):
            with self.assertRaises(HTTPError) as refused:
                self.upload_photo(plant_id, body)
            self.assertEqual(refused.exception.code, 400)
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}/photos")["items"], [])

    def test_the_same_photograph_under_two_plants_is_one_file(self) -> None:
        first = self.call("POST", "/api/plants", {"display_name": "One"})["plant_id"]
        second = self.call("POST", "/api/plants", {"display_name": "Two"})["plant_id"]

        kept = self.upload_photo(first, PNG)
        shared = self.upload_photo(second, PNG)
        self.assertEqual(kept["digest"], shared["digest"])
        self.assertEqual(len(list(self.photo_directory.glob("*"))), 1)

        # And the file stays while anything still refers to it.
        self.call("DELETE", f"/api/photos/{shared['photo_id']}")
        self.assertEqual(len(list(self.photo_directory.glob("*"))), 1)
        with urlopen(self.base_url + f"/api/photos/{kept['photo_id']}", timeout=2.0) as served:
            self.assertEqual(served.read(), PNG)
        self.call("DELETE", f"/api/photos/{kept['photo_id']}")
        self.assertEqual(list(self.photo_directory.glob("*")), [])

    def test_a_plant_with_no_sensor_still_has_lights(self) -> None:
        # The lights were always the plant's; only the address was the
        # sensor's, so a plant without one reaches them directly.
        plant = self.call("POST", "/api/plants", {"display_name": "Calathea"})
        path = f"/api/plants/{plant['plant_id']}/lighting"

        default = self.call("GET", path)
        self.assertEqual(default["plant_id"], plant["plant_id"])
        self.assertFalse(default["schedule_saved"])

        saved = self.call("PUT", path, {
            "light_ids": [],
            "schedule": {"enabled": True, "on": "07:00", "off": "19:30"},
        })
        self.assertTrue(saved["schedule_saved"])
        self.assertEqual(saved["schedule"]["off"], "19:30")
        self.assertEqual(self.call("GET", path)["schedule"]["on"], "07:00")

    def test_the_configuration_page_asks_the_plant_its_own_questions(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        self.assertIn('data-page="plant-config"', page)
        self.assertIn('path.endsWith("/config")) return "plant-config"', script)

        # Every plant-level setting lives on this page and only here. The
        # same question asked in two places is how a plant with a sensor came
        # to have a profile, a variety and an at-home date it could not
        # reach: each was added to the page one kind of plant uses.
        plant_fields = (
            "plant-config-display-name", "plant-config-room", "plant-config-profile",
            "plant-config-variety", "plant-config-acquired", "plant-config-watering-days",
            "lighting-enabled", "lighting-on", "lighting-off",
        )
        panel = page[page.index('id="plant-config-title"'):page.index('id="sensor-settings-panel"')]
        for field in plant_fields:
            self.assertEqual(page.count(f'id="{field}"'), 1, f"{field} is asked twice")
            self.assertIn(f'id="{field}"', panel, f"{field} is not on the plant's page")

        # And the sensor's section asks nothing about the plant: saving the
        # device must not be able to rewrite what the plant is.
        sensor_panel = page[page.index('id="sensor-settings-panel"'):]
        for gone in ("detail-setting-name", "detail-setting-room", "detail-setting-profile"):
            self.assertNotIn(gone, page, f"{gone} still asks the plant's question")
        self.assertIn("detail-setting-reporting-interval", sensor_panel)
        self.assertNotIn('display_name: document.getElementById("detail-setting-name")', script)

    def test_a_plant_with_a_sensor_is_configured_in_the_same_place(self) -> None:
        script = self.call_text("/app.js")

        # The sensor's own address leads to its plant's configuration, and
        # the sensor's settings are a section of it rather than a page.
        self.assertIn(
            'if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "plant-config";',
            script)
        self.assertIn("function plantSensor()", script)
        # A plant with no sensor gets no empty sensor sections.
        self.assertIn('"sensor-settings-panel", "sensor-firmware-panel", "raw-report-log"', script)

    def test_a_pot_is_weighed_in_grams_whatever_the_keypad_offered(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Bird of paradise"})
        path = f"/api/plants/{plant['plant_id']}"

        self.call("POST", path + "/weight", {"grams": 17_400, "kind": "watered"})
        read = self.call("GET", path)

        self.assertEqual(read["weight"]["watered"]["grams"], 17_400)
        self.assertEqual(read["weights"][0]["grams"], 17_400)
        # The ceiling is taken after watering, so the watering is on record.
        self.assertEqual(read["journey"]["watering_count"], 1)

    def test_a_weighing_carries_the_day_it_was_taken(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        self.assertIn('id="plant-weight-date" type="date"', page)
        # Today unless somebody says otherwise, and never a day ahead.
        self.assertIn("function resetWeighingDate()", script)
        self.assertIn("if (chosen > today) return null;", script)
        self.assertIn("JSON.stringify({ grams, kind, at })", script)

    def test_the_pot_has_a_month_of_its_own_history(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # The same question the probe's chart answers, by another instrument.
        self.assertIn('id="plant-weight-chart"', page)
        self.assertIn("Pot weight · last 30 days", page)
        self.assertIn("plantWeightTrendMs", script)
        # Full and dry are drawn as the band between them.
        self.assertIn('class: "trend-cycle"', script)
        self.assertIn("Full at ${showWeight(weight.watered.grams)}", script)
        self.assertIn("Thirsty at ${showWeight(weight.thirsty.grams)}", script)
        # And both can be typed in, which does not invent a watering.
        self.assertIn('id="plant-weight-full"', page)
        self.assertIn('id="plant-weight-thirsty"', page)
        self.assertIn("observed: false", script)

    def test_a_pot_can_be_weighed_in_either_unit(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # A 17 kg pot in grams is five digits to get wrong; a seedling in
        # kilograms is 0.42 of one. The scale's owner chooses.
        # Every box says which unit it is read in, including the anchors: a
        # number typed into an unlabelled box is a guess about what was meant.
        for selector in ("plant-weight-unit", "plant-weight-full-unit", "plant-weight-thirsty-unit"):
            self.assertIn(f'id="{selector}"', page)
        self.assertEqual(page.count('<option value="g">g</option>'), 3)
        self.assertIn("Math.round(typed * 1000)", script)
        # Grams on the wire whichever was typed.
        self.assertIn("JSON.stringify({ grams, kind, at })", script)
        # And each choice outlives the page, per browser.
        self.assertIn("WEIGHT_UNIT_FIELDS", script)
        self.assertIn("open-plant-pulse.weight-unit.full", script)

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
        page = self.call_text("/")

        # One grid, one kind of tile: a plant with a sensor and a plant
        # without are the same kind of thing and sit side by side.
        self.assertIn("function renderHousehold()", script)
        self.assertNotIn('plant-grid"', page)
        self.assertIn("sensorsByPlant.get(plant.plant_id)", script)
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
