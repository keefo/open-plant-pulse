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
from open_plant_pulse_hub.application.weights import WeightKeeper, due
from open_plant_pulse_hub.application.zigbee import ZigbeeService
from open_plant_pulse_hub.application.zigbee.network import Reading
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

    def test_it_says_when_the_plant_will_want_water(self) -> None:
        self.store.record_plant_weight(self.plant_id, 3_000, "watered", at=ago(12))
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(8, 2_700), (4, 2_500), (0.2, 2_400)])

        forecast = self.store.watering_forecast(self.plant_id)

        self.assertTrue(forecast["ready"])
        self.assertEqual(forecast["missing"], [])
        self.assertEqual(forecast["grams_to_go"], 400)
        self.assertEqual(forecast["grams_per_day"], 50)
        self.assertEqual(forecast["rate_from"], "this cycle")
        self.assertAlmostEqual(forecast["days"], 7.9, places=1)
        # A range, not a day: four weighings on a kitchen scale do not know
        # the hour. Fastest it has drunk brings it forward, slowest pushes out.
        self.assertLess(forecast["days_soonest"], forecast["days"])
        self.assertGreater(forecast["days_latest"], forecast["days"])

    def test_it_names_what_is_missing_rather_than_guessing(self) -> None:
        # Nothing at all.
        self.assertEqual(
            [m["code"] for m in self.store.watering_forecast(self.plant_id)["missing"]],
            ["no_weighing", "no_thirsty_weight", "no_rate"])

        # Weighed, and weighed again, but never told what thirsty looks like.
        self.water_history([(4, 2_800), (0.2, 2_600)])
        gap = self.store.watering_forecast(self.plant_id)
        self.assertFalse(gap["ready"])
        self.assertEqual([m["code"] for m in gap["missing"]], ["no_thirsty_weight"])
        self.assertIn("Thirsty at", gap["missing"][0]["how"])

        # Told that, and it can answer.
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.assertTrue(self.store.watering_forecast(self.plant_id)["ready"])

    def test_one_weighing_is_a_weight_and_not_a_rate(self) -> None:
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_600, at=ago(0.2))

        forecast = self.store.watering_forecast(self.plant_id)
        self.assertEqual([m["code"] for m in forecast["missing"]], ["no_rate"])
        self.assertIsNone(forecast["days"])

    def test_a_pot_already_at_its_thirsty_weight_says_so(self) -> None:
        self.store.record_plant_weight(self.plant_id, 2_500, "thirsty", observed=False)
        self.water_history([(6, 2_900), (3, 2_600), (0.2, 2_450)])

        forecast = self.store.watering_forecast(self.plant_id)
        self.assertTrue(forecast["ready"])
        self.assertTrue(forecast["thirsty_now"])
        self.assertEqual(forecast["days"], 0.0)
        # Nothing is "minus fifty millilitres to go".
        self.assertEqual(forecast["grams_to_go"], 0)

    def test_a_span_across_a_repot_is_not_the_plant_drinking(self) -> None:
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.water_history([(10, 2_800), (7, 2_700), (4, 2_600)])
        self.store.record_plant_care(self.plant_id, "repotting", at=ago(2.5))
        # New soil: the pot weighs what it weighs now, and the difference
        # across the repot is compost rather than evapotranspiration.
        self.water_history([(2, 3_400), (0.2, 3_300)])

        spans = self.store.water_use(self.plant_id)["intervals"]

        self.assertTrue(all(span["grams_per_day"] < 100 for span in spans), spans)
        for span in spans:
            self.assertFalse(span["from"] < ago(2.5) < span["to"], span)

    def test_anchors_set_before_a_repot_are_flagged(self) -> None:
        self.store.record_plant_weight(self.plant_id, 2_800, "watered", at=ago(12))
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", at=ago(12),
                                       observed=False)
        self.water_history([(8, 2_600), (4, 2_400), (0.2, 2_300)])
        self.assertEqual(self.store.watering_forecast(self.plant_id)["cautions"], [])

        self.store.record_plant_care(self.plant_id, "repotting", at=ago(1))

        cautions = self.store.watering_forecast(self.plant_id)["cautions"]
        self.assertEqual([c["code"] for c in cautions], ["anchors_predate_repot"])
        self.assertIn("Full at and Thirsty at", cautions[0]["what"])
        # Still forecasts, because a figure with a caveat beats no figure.
        self.assertTrue(self.store.watering_forecast(self.plant_id)["ready"])

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

    def test_a_weighing_entered_wrong_can_be_struck(self) -> None:
        """A number typed wrong is not a measurement, and it drags the rest along."""
        self.store.record_plant_weight(self.plant_id, 2_000, "thirsty", observed=False)
        self.store.record_plant_weight(self.plant_id, 2_800, at=ago(4))
        slip = self.store.record_plant_weight(self.plant_id, 240, at=ago(2))
        self.store.record_plant_weight(self.plant_id, 2_600, at=ago(0.2))
        self.assertGreater(self.store.water_use(self.plant_id)["daily_grams"], 1_000)

        wrong = [w for w in self.store.plant_weights(self.plant_id) if w["grams"] == 240][0]
        self.store.delete_plant_weight(wrong["weight_id"])

        self.assertEqual(len(self.store.plant_weights(self.plant_id)), 3)
        self.assertLess(self.store.water_use(self.plant_id)["daily_grams"], 100)
        with self.assertRaises(ValueError):
            self.store.delete_plant_weight(wrong["weight_id"])
        self.assertIsNotNone(slip)

    def test_striking_a_weighing_leaves_the_watering_it_recorded(self) -> None:
        # Two different claims: that the pot weighed this, and that the
        # plant was watered. Taking back the first is not taking back the
        # second, which is in the care log for whoever wants it gone.
        self.store.record_plant_weight(self.plant_id, 2_800, "watered")
        weighing = self.store.plant_weights(self.plant_id)[0]
        self.assertEqual(len(self.store.care_log(10, plant_id=self.plant_id)), 1)

        self.store.delete_plant_weight(weighing["weight_id"])

        self.assertEqual(self.store.plant_weights(self.plant_id), [])
        self.assertEqual(len(self.store.care_log(10, plant_id=self.plant_id)), 1)

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

    def test_the_air_card_is_the_rooms_readings_and_the_plants_bands(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # One card, on both pages, rather than a second one for plants
        # without a probe: the readings are stored once and read by all.
        self.assertIn('class="climate-history" data-page="plant"', page)
        self.assertEqual(page.count('id="temperature-history-chart"'), 1)
        self.assertEqual(page.count('id="humidity-history-chart"'), 1)
        self.assertNotIn("room-air-panel", page)

        # The values come from whichever sensor is in the room; the green
        # band comes from the plant being looked at.
        self.assertIn("function airSource()", script)
        self.assertIn("profileId: selectedPlant?.profile_id", script)
        self.assertIn("profiles?.profiles?.[climateProfileId", script)

    def test_the_forecast_reaches_the_list_and_the_plants_page(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        # Short on the card, explained on the plant's page.
        self.assertIn("`Water in ${Math.round(days)}d`", script)
        self.assertIn('id="forecast-chart"', page)
        self.assertIn('id="forecast-missing-list"', page)
        self.assertIn("function renderForecastMissing(missing)", script)

        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.assertIsNone(self.call("GET", "/api/plants")["items"][0]["water_in_days"])
        self.assertFalse(self.call("GET", f"/api/plants/{plant}")["forecast"]["ready"])

    def test_the_section_order_is_one_order_for_every_plant(self) -> None:
        """Somebody who wants the forecast at the top wants it on all of them."""
        settings = self.call("GET", "/api/settings")
        self.assertEqual(settings["plant_section_order"][:2], ["readings", "air"])

        saved = self.call("PUT", "/api/settings/sections",
                          {"order": ["forecast", "weight"]})
        self.assertEqual(saved["plant_section_order"][:2], ["forecast", "weight"])
        # Sections the saved order has not heard of follow it, in the order
        # they were built, so a new one is visible rather than lost.
        self.assertEqual(sorted(saved["plant_section_order"]),
                         sorted(settings["plant_section_order"]))
        # It is the hub's, not the browser's, so it follows whoever set it.
        self.assertEqual(self.call("GET", "/api/settings")["plant_section_order"],
                         saved["plant_section_order"])

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", "/api/settings/sections", {"order": ["nonesuch"]})
        self.assertEqual(refused.exception.code, 400)

    def test_every_orderable_section_is_named(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")

        for name in ("readings", "air", "weight", "forecast", "water-use",
                     "journey", "photos", "care-log"):
            self.assertIn(f'data-section="{name}"', page)
        # Arranged as miniatures, because a page of full-height panels
        # cannot be dragged into order.
        self.assertIn("main.arranging [data-section]", self.call_text("/app.css"))
        self.assertIn("function beginSectionDrag(event)", script)
        # Dragging works with a finger, and so does the keyboard.
        self.assertIn("pointermove", script)
        self.assertIn("function nudgeSection(event)", script)
        # The tile is carried: lifted out of the column, following the
        # finger, with a dashed gap of its own size where it would land.
        # Left in the flow it shifts every tile below it as it moves, the
        # pointer lands somewhere else than it did a frame ago, and the
        # next move undoes the last.
        self.assertIn('moving.style.position = "fixed"', script)
        self.assertIn('gap.className = "section-gap"', script)
        self.assertIn("gap.style.height = `${box.height}px`", script)
        self.assertIn(".section-gap { ", self.call_text("/app.css"))
        # And it puts everything back: no stray gap, no inline styles left
        # on a section once the finger lifts.
        self.assertIn("gap.remove();", script)
        self.assertIn('moving.style[property] = "";', script)
        # And the landing place comes from how far down the pointer is, not
        # from what is underneath it: the tiles have gaps between them, and
        # a pointer in a gap is over nothing at all.
        self.assertNotIn("elementsFromPoint", script)

    def test_the_care_log_can_be_folded_and_filtered(self) -> None:
        """Fifty lighting lines should not bury the feeding somebody wants."""
        settings = self.call("GET", "/api/settings")
        self.assertEqual(settings["care_log_hidden_kinds"], [])

        hidden = self.call("PUT", "/api/settings/care-log", {"hidden": ["lighting"]})
        self.assertEqual(hidden["care_log_hidden_kinds"], ["lighting"])
        # One preference for every plant, kept by the hub rather than the
        # browser, so it holds from the phone to the desk.
        self.assertEqual(self.call("GET", "/api/settings")["care_log_hidden_kinds"],
                         ["lighting"])

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", "/api/settings/care-log", {"hidden": ["nonesuch"]})
        self.assertEqual(refused.exception.code, 400)

        page = self.call_text("/")
        script = self.call_text("/app.js")
        # A box per kind, and a fold. There is one care log now: it is the
        # plant's, and the sensor-era copy of it has gone.
        self.assertEqual(page.count("care-log-controls"), 1)
        self.assertEqual(page.count("care-log-toggle"), 1)
        self.assertIn("function visibleCareEvents(items)", script)
        # A box for every kind the hub knows, not only the kinds this plant
        # happens to have: the choice is for every plant, and somebody
        # setting it should not have to find a plant with one of each.
        for kind in ("watering", "watering_due", "fertilizing", "lighting",
                     "drainage_assessment", "observation", "note"):
            self.assertIn(f'{kind}: "', script)
        self.assertIn('const present = Object.keys(CARE_KIND_LABELS);', script)
        # Folding is where somebody is on a page, so that stays in the browser.
        self.assertIn("CARE_LOG_FOLDED_KEY", script)
        # An empty log and one emptied by the boxes are different things.
        self.assertIn("Nothing of the kinds being shown", script)

    def test_a_weighing_is_struck_over_http_and_listed_to_be_struck(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.call("POST", f"/api/plants/{plant}/weight", {"grams": 2_400})
        weighing = self.call("GET", f"/api/plants/{plant}")["weights"][0]

        self.call("DELETE", f"/api/weights/{weighing['weight_id']}")
        self.assertEqual(self.call("GET", f"/api/plants/{plant}")["weights"], [])

        with self.assertRaises(HTTPError) as refused:
            self.call("DELETE", f"/api/weights/{weighing['weight_id']}")
        self.assertEqual(refused.exception.code, 400)

        page = self.call_text("/")
        script = self.call_text("/app.js")
        self.assertIn('id="weighing-log"', page)
        self.assertIn("async function removeWeighing(weighing)", script)

    def test_a_repot_is_recorded_and_breaks_the_weighings(self) -> None:
        """Same plant, different thing on the scale."""
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.call("POST", f"/api/plants/{plant}/care", {"kind": "repotting"})

        log = self.call("GET", f"/api/plants/{plant}")["care_log"]
        self.assertEqual([e["kind"] for e in log], ["repotting"])
        self.assertEqual(log[0]["title"], "Repotted")
        # Once a day, like watering and feeding: a pot is repotted or it is not.
        self.call("POST", f"/api/plants/{plant}/care", {"kind": "repotting"})
        self.assertEqual(len(self.call("GET", f"/api/plants/{plant}")["care_log"]), 1)

        page = self.call_text("/")
        script = self.call_text("/app.js")
        self.assertIn('id="plant-repotted"', page)
        self.assertIn('repotting: "Repottings"', script)

    def test_a_card_marks_watering_feeding_and_weighing(self) -> None:
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.call("POST", f"/api/plants/{plant}/care", {"kind": "fertilizing"})

        # When it was last fed travels with the plant, like when it was
        # last watered: it is the other thing a plant is given, and the one
        # nobody remembers.
        self.assertIsNotNone(self.call("GET", "/api/plants")["items"][0]["last_fed_at"])

        script = self.call_text("/app.js")
        # Marks rather than words on a card, because the words are the same
        # on every card and the dates are what differ.
        self.assertIn("const CARE_MARKS = {", script)
        for word in ('word: "Watered"', 'word: "Fed"', 'word: "Weighed"'):
            self.assertIn(word, script)
        # Each mark keeps its word for a pointer and for a screen reader.
        self.assertIn('setAttr(wrapper, "title", `${mark.word} ${since}`)', script)
        self.assertIn('said.className = "visually-hidden"', script)

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
        self.assertIn('class="plant-photos" data-page="plant"', page)
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
        # Short enough for a card -- 22d, 7mo, 2y 3mo -- with the whole
        # phrase kept for the tooltip.
        self.assertIn("function timeAtHomeParts(since)", script)
        self.assertIn("`Home ${home.short}`", script)
        self.assertIn("At home ${plural}", script)

    def test_the_care_log_leaves_out_hidden_kinds_before_it_counts(self) -> None:
        """A plant watered daily must not bury its own history in waterings."""
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        path = f"/api/plants/{plant_id}/care"
        # One old note, then enough of a noisier kind to bury it in a window.
        self.call("POST", path, {"kind": "note", "note": "Bought",
                                 "at": "2026-08-30T19:00:00Z"})
        for day in range(1, 13):
            self.call("POST", path, {"kind": "watering",
                                     "at": f"2026-09-{day:02d}T19:00:00Z"})

        buried = self.call("GET", f"/api/care-log?plant_id={plant_id}&limit=10")["items"]
        self.assertNotIn("note", [e["kind"] for e in buried])

        self.call("PUT", "/api/settings/care-log", {"hidden": ["watering"]})
        found = self.call("GET", f"/api/care-log?plant_id={plant_id}&limit=10")["items"]
        # The window now holds what was asked for, and the note is in it.
        self.assertEqual([e["kind"] for e in found], ["note"])
        # The plant's own page agrees, because it filters the same way.
        self.assertEqual(
            [e["kind"] for e in self.call("GET", f"/api/plants/{plant_id}")["care_log"]],
            ["note"])

        # Load more continues from the oldest already shown, not from a page
        # number that would shift when something new is recorded.
        self.call("PUT", "/api/settings/care-log", {"hidden": []})
        first = self.call("GET", f"/api/care-log?plant_id={plant_id}&limit=5")["items"]
        self.assertEqual(len(first), 5)
        older = self.call(
            "GET",
            f"/api/care-log?plant_id={plant_id}&limit=5&before={first[-1]['detected_at']}",
        )["items"]
        self.assertTrue(older)
        self.assertLess(older[0]["detected_at"], first[-1]["detected_at"])

        # Asked by plant, only that plant answers.
        other = self.call("POST", "/api/plants", {"display_name": "Fern"})["plant_id"]
        self.call("POST", f"/api/plants/{other}/care", {"kind": "watering"})
        mine = self.call("GET", f"/api/care-log?plant_id={plant_id}&limit=200")["items"]
        self.assertEqual(len(mine), 13)

        page = self.call_text("/")
        self.assertIn('id="plant-care-log-more"', page)
        self.assertIn("async function loadMoreCareLog(listId)", self.call_text("/app.js"))

    def test_care_can_be_dated_to_the_day_it_happened_and_struck_out(self) -> None:
        """Care is done with wet hands and written down later, sometimes wrongly."""
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        path = f"/api/plants/{plant_id}/care"

        self.call("POST", path, {"kind": "note", "note": "Buds moved",
                                 "at": "2026-10-12T18:00:00Z"})
        log = self.call("GET", f"/api/plants/{plant_id}")["care_log"]
        self.assertEqual(len(log), 1)
        # Filed under the day it happened, not the day it was typed.
        self.assertEqual(log[0]["detected_at"], "2026-10-12T18:00:00Z")

        # Nothing has been watered tomorrow.
        with self.assertRaises(HTTPError) as refused:
            self.call("POST", path, {"kind": "note", "note": "Later",
                                     "at": "2099-01-01T12:00:00Z"})
        self.assertEqual(refused.exception.code, 400)

        # A backdated watering counts in the journey as of its own day.
        self.call("POST", path, {"kind": "watering", "at": "2026-10-09T18:00:00Z"})
        self.assertEqual(
            self.call("GET", f"/api/plants/{plant_id}")["journey"]["watering_count"], 1)

        # Struck out, and the journey counted again rather than nudged.
        watering = next(e for e in self.call("GET", f"/api/plants/{plant_id}")["care_log"]
                        if e["kind"] == "watering")
        self.call("DELETE", f"/api/care/{watering['event_id']}")
        after = self.call("GET", f"/api/plants/{plant_id}")
        self.assertEqual(after["journey"]["watering_count"], 0)
        self.assertEqual([e["kind"] for e in after["care_log"]], ["note"])

        with self.assertRaises(HTTPError) as missing:
            self.call("DELETE", "/api/care/nothing-by-that-name")
        self.assertEqual(missing.exception.code, 400)

    def test_a_plant_remembers_what_it_cost_and_where_it_came_from(self) -> None:
        """The at-home date says when; these two say where and how much."""
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})
        path = f"/api/plants/{plant['plant_id']}"
        self.assertIsNone(plant["purchase_price_cents"])
        self.assertIsNone(plant["purchase_place"])

        saved = self.call("PUT", path, {
            "display_name": "Spider", "purchase_place": "  Gardenworks  ",
            "purchase_price_cents": 1299,
        })
        # Whole cents, so a shelf of plants sums without drift.
        self.assertEqual(saved["purchase_price_cents"], 1299)
        # Typed with the stray spaces a person leaves behind.
        self.assertEqual(saved["purchase_place"], "Gardenworks")
        self.assertEqual(self.call("GET", path)["purchase_place"], "Gardenworks")

        # A cutting cost nothing, which is not the same as not knowing.
        free = self.call("PUT", path, {"display_name": "Spider",
                                       "purchase_price_cents": 0})
        self.assertEqual(free["purchase_price_cents"], 0)

        # Left out, it goes back to unknown rather than staying at zero.
        cleared = self.call("PUT", path, {"display_name": "Spider"})
        self.assertIsNone(cleared["purchase_price_cents"])
        self.assertIsNone(cleared["purchase_place"])

        with self.assertRaises(HTTPError) as refused:
            self.call("PUT", path, {"display_name": "Spider",
                                    "purchase_price_cents": -1})
        self.assertEqual(refused.exception.code, 400)

        page = self.call_text("/")
        self.assertIn('id="plant-config-purchase-place"', page)
        self.assertIn('id="plant-config-purchase-price"', page)
        # Cents on the wire, whole currency in the field.
        self.assertIn("Math.round(amount * 100)", self.call_text("/app.js"))

    def test_the_driest_weighing_becomes_the_floor_without_being_asked_for(self) -> None:
        """Thirsty is a decision about the plant; dry is what the scale said."""
        plant_id = self.call("POST", "/api/plants", {"display_name": "Golden"})["plant_id"]
        path = f"/api/plants/{plant_id}/weight"

        self.call("POST", path, {"grams": 2438, "kind": "watered", "observed": False})
        self.call("POST", path, {"grams": 1450, "kind": "thirsty", "observed": False})

        # Typed-in anchors are claims about states the pot is not in, so
        # until something is actually weighed there is no floor to stand on.
        plant = self.call("GET", f"/api/plants/{plant_id}")
        self.assertIsNone(plant["weight"]["dry"])
        self.assertIsNone(plant["weight"]["held_grams"])

        # One weighing is the lightest there has been, so it is the floor,
        # and a pot sitting on its own floor is as dry as it is known to get.
        weight = self.call("POST", path, {"grams": 1944, "kind": "reading"})
        self.assertEqual(weight["dry"]["grams"], 1944)
        self.assertEqual(weight["water_percent"], 0)
        self.assertEqual(weight["remaining_percent"], 50)

        # A lighter one takes the floor down with it; the pot is then known
        # to hold 1358 g, against the 988 g one watering interval spends.
        weight = self.call("POST", path, {"grams": 1080, "kind": "reading"})
        self.assertEqual(weight["dry"]["grams"], 1080)
        self.assertEqual(weight["held_grams"], 1358)
        self.assertEqual(weight["capacity_grams"], 988)

        # A heavier weighing afterwards leaves the floor where it was: the
        # pot has been that light, and that does not stop being true.
        weight = self.call("POST", path, {"grams": 1944, "kind": "reading"})
        self.assertEqual(weight["dry"]["grams"], 1080)
        # Half the watering interval is spent and the pot is still 64% wet,
        # which is the number a person means by "how wet is it".
        self.assertEqual(weight["water_percent"], 64)
        self.assertEqual(weight["remaining_percent"], 50)

        # At the thirsty weight the interval is spent and the pot is not.
        weight = self.call("POST", path, {"grams": 1450, "kind": "reading"})
        self.assertEqual(weight["remaining_percent"], 0)
        self.assertEqual(weight["water_percent"], 27)

        # Set deliberately, a dry weight outranks the lightest weighing:
        # somebody who has weighed the pot empty knows better than the log.
        weight = self.call("POST", path, {"grams": 1000, "kind": "dry", "observed": False})
        self.assertEqual(weight["dry"]["grams"], 1000)
        self.assertEqual(weight["held_grams"], 1438)

        with self.assertRaises(HTTPError) as refused:
            self.call("POST", path, {"grams": 1000, "kind": "damp"})
        self.assertEqual(refused.exception.code, 400)

        # Nothing to fill in: the floor is read off the weighings, and the
        # chart paints full-to-dry with thirsty marked between them.
        page = self.call_text("/")
        self.assertNotIn('id="plant-weight-dry"', page)
        script = self.call_text("/app.js")
        self.assertIn("trend-dry-label", script)
        self.assertIn("trend-reserve", script)

    def test_a_cover_can_follow_the_newest_photograph(self) -> None:
        """A plant photographed to watch it grow wants the latest on its card."""
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        first = self.upload_photo(plant_id, PNG, taken_at="2026-01-01T12:00:00Z")
        self.call("PUT", f"/api/plants/{plant_id}/cover", {"follows_latest": True})

        newer = self.upload_photo(plant_id, JPEG, taken_at="2026-06-01T12:00:00Z")
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         newer["photo_id"])

        # Newest by when it was taken, not by when it arrived: a picture
        # from years ago uploaded this morning is not the newest.
        self.upload_photo(plant_id, PNG + b"old", taken_at="2019-07-14T12:00:00Z")
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         newer["photo_id"])

        # Choosing one by hand is a decision, and stops the following.
        self.call("PUT", f"/api/plants/{plant_id}/cover", {"photo_id": first["photo_id"]})
        plant = self.call("GET", f"/api/plants/{plant_id}")
        self.assertEqual(plant["cover_photo_id"], first["photo_id"])
        self.assertFalse(plant["cover_follows_latest"])
        self.upload_photo(plant_id, JPEG + b"newest", taken_at="2026-10-01T12:00:00Z")
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         first["photo_id"])

    def test_the_rule_about_the_pictures_arrives_with_the_pictures(self) -> None:
        # Two requests meant the box ticked itself a round trip after the
        # photographs appeared, which is what somebody notices.
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.upload_photo(plant, PNG)
        self.call("PUT", f"/api/plants/{plant}/cover", {"follows_latest": True})

        photos = self.call("GET", f"/api/plants/{plant}/photos")

        self.assertEqual(len(photos["items"]), 1)
        self.assertTrue(photos["cover_follows_latest"])
        script = self.call_text("/app.js")
        self.assertIn("coverFollowsLatest = Boolean(payload.cover_follows_latest)", script)

    def test_the_cover_checkbox_is_on_the_photographs(self) -> None:
        page = self.call_text("/")
        script = self.call_text("/app.js")
        self.assertIn('id="plant-cover-follows"', page)
        self.assertIn("async function setCoverFollowsLatest(following)", script)
        # Per plant, not for the household: one plant is a record of growth
        # and the next has one good picture nothing will beat.
        plant = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        other = self.call("POST", "/api/plants", {"display_name": "Fern"})["plant_id"]
        self.call("PUT", f"/api/plants/{plant}/cover", {"follows_latest": True})
        self.assertTrue(self.call("GET", f"/api/plants/{plant}")["cover_follows_latest"])
        self.assertFalse(self.call("GET", f"/api/plants/{other}")["cover_follows_latest"])

    def test_following_the_newest_keeps_up_with_a_corrected_date(self) -> None:
        plant_id = self.call("POST", "/api/plants", {"display_name": "Spider"})["plant_id"]
        self.upload_photo(plant_id, PNG, taken_at="2026-01-01T12:00:00Z")
        undated = self.upload_photo(plant_id, JPEG, taken_at="2019-01-01T12:00:00Z")
        self.call("PUT", f"/api/plants/{plant_id}/cover", {"follows_latest": True})
        self.assertNotEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                            undated["photo_id"])

        # Told a later date, it becomes the newest and takes the cover.
        self.call("PUT", f"/api/photos/{undated['photo_id']}",
                  {"taken_at": "2026-09-01T12:00:00Z"})
        self.assertEqual(self.call("GET", f"/api/plants/{plant_id}")["cover_photo_id"],
                         undated["photo_id"])

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

    def test_the_page_is_well_formed(self) -> None:
        """Every tag closes.

        An unclosed element does not fail loudly: the browser's parser
        discards the rest of the document, so a page simply has nothing on
        it and the console says nothing. Twice while moving sections about,
        a stray cut took a closing tag from the element before and the page
        went blank in exactly that way.
        """
        import re
        from collections import Counter

        page = self.call_text("/")
        void = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "source", "track", "wbr", "!doctype"}
        opened = Counter(m.group(1).lower()
                         for m in re.finditer(r"<([a-zA-Z][\w-]*)[\s/>]", page))
        closed = Counter(m.group(1).lower()
                         for m in re.finditer(r"</([a-zA-Z][\w-]*)>", page))
        unbalanced = {
            tag: (opened[tag], closed[tag])
            for tag in set(opened) | set(closed)
            if tag not in void and opened[tag] != closed[tag]
        }
        self.assertEqual(unbalanced, {}, f"tags that do not pair: {unbalanced}")

    def test_every_element_the_scripts_reach_for_exists(self) -> None:
        """A getElementById for an element that has gone takes the page down.

        setText and setAttr are called on the result without checking it,
        which is right -- a missing element is a mistake, not a state to
        handle -- but the mistake throws inside the refresh, and a refresh
        that throws stops the whole page updating. Removing markup has
        twice left a write behind that did exactly that.
        """
        import re

        static = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static"
        present = set(re.findall(r'\bid="([^"]+)"', (static / "index.html").read_text()))
        missing = {
            f"{script.name}:{wanted}"
            for script in static.glob("*.js")
            for wanted in re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', script.read_text())
            if wanted not in present
        }
        self.assertEqual(missing, set())

    def test_every_class_the_markup_styles_with_exists(self) -> None:
        """A class with no rule behind it silently does nothing.

        class="row" was borrowed from the scale firmware's own page, where
        it is defined; here it is not, so three rows of buttons had no
        layout at all. .nav-button went the same way and rendered a button
        as plain underlined text. Neither failed, neither logged, and both
        needed somebody to look at the page to notice.

        Hook classes carry no styling on purpose, so they are named here
        rather than discovered.
        """
        import re

        static = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "static"
        css = (static / "app.css").read_text()
        defined = set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", css))

        # Classes that exist only for scripts to find, and for state the page
        # sets without styling it.
        hooks = {
            # State and tone the page sets without a rule of its own.
            "mute", "note", "bad", "ok", "active", "danger", "hidden",
            "primary", "switch", "spacer",
            # Handles the scripts find elements by. Verified: each appears in
            # a closest() or querySelector() call rather than for styling.
            "zigbee-command", "zigbee-manage", "zigbee-claim",
            "forget-sensor", "arrange-sections", "delete-room", "edit-room",
            "lighting-toggle", "scale-high", "scale-low",
        }

        # Classes already in the markup with no rule behind them when this
        # test was written. They are listed so the test guards against new
        # ones rather than being switched off by the old; each is either a
        # leftover from a removed rule or styling that never arrived, and
        # shortening this list is its own piece of work.
        known_gaps = {
            "battery-percent", "calendar-spacer", "configuration-delivery",
            "section-subheading", "settings-sensor-name",
            # An SVG rect whose fill is an inline url(#gradient) attribute,
            # so it is styled, just not by a rule. Its sibling trend-refill
            # does have one, which is why only this one shows up.
            "trend-reserve",
        }

        used = set()
        for match in re.finditer(r'class="([^"]*)"', (static / "index.html").read_text()):
            used.update(word for word in match.group(1).split() if "{" not in word)
        for script in static.glob("*.js"):
            text = script.read_text()
            for match in re.finditer(r'className = "([^"]*)"', text):
                used.update(match.group(1).split())
            for match in re.finditer(r'className = `([^`]*)`', text):
                used.update(w for w in match.group(1).split() if "$" not in w)
            # SVG elements take their class through setAttribute or through
            # an attributes object, never through className. A battery chart
            # drawn with class names that existed nowhere passed this test
            # because neither form was being read.
            for match in re.finditer(r'setAttribute\(\s*"class"\s*,\s*"([^"]*)"', text):
                used.update(match.group(1).split())
            for match in re.finditer(r'\bclass:\s*"([^"]*)"', text):
                used.update(match.group(1).split())

        missing = sorted(name for name in used - defined - hooks - known_gaps)
        self.assertEqual(missing, [], f"styled with classes that have no rule: {missing}")

    def test_a_sensor_is_configured_on_its_own_page_not_its_plant_s(self) -> None:
        """Administering a device and caring for a plant are different jobs.

        The two were folded together while every sensor had a plant. A
        scale has none, so a sensor's settings are its own page again.
        """
        script = self.call_text("/app.js")
        page = self.call_text("/")

        self.assertIn(
            'if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "sensor-config";',
            script)
        self.assertIn("function plantSensor()", script)
        # The device panels live on the sensor's page, not the plant's.
        for panel in ('id="sensor-settings-panel"', 'id="sensor-firmware-panel"',
                      'id="sensor-ph-panel"', 'id="raw-report-log"'):
            start = page.index(panel)
            opening = page.rindex("<", 0, start)
            self.assertIn('data-page="sensor-config"', page[opening:start + len(panel) + 200],
                          f"{panel} should be on the sensor's own page")

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
        # Full and dry are drawn as the band between them, with thirsty
        # marked where the comfortable green gives way to the reserve.
        self.assertIn('class: "trend-cycle"', script)
        self.assertIn("Full at ${showWeight(fullGrams)}", script)
        self.assertIn("Thirsty at ${showWeight(refill)}", script)
        self.assertIn("Driest seen ${showWeight(dryGrams)}", script)
        # And both can be typed in, which does not invent a watering.
        self.assertIn('id="plant-weight-full"', page)
        self.assertIn('id="plant-weight-thirsty"', page)
        self.assertIn("observed: false", script)

    def test_a_pot_is_weighed_in_grams_and_nothing_else(self) -> None:
        """One unit, said once per box, with nothing to choose.

        Every weight box used to carry a kg/g selector, remembered per
        browser, converting the typed number when it changed. The scale
        reports grams, the database stores grams and every anchor on the
        page is a whole number of them, so the choice bought nothing and
        cost three selects, two placeholders per field and a stored
        preference that could disagree with the box beside it.
        """
        page = self.call_text("/")
        script = self.call_text("/app.js")

        for gone in ("plant-weight-unit", "plant-weight-full-unit",
                     "plant-weight-thirsty-unit"):
            self.assertNotIn(f'id="{gone}"', page)
        self.assertNotIn('<option value="kg">kg</option>', page)
        # The unit is in the label instead, where it cannot be changed.
        for label in ("Weighs (g)", "Full at (g)", "Thirsty at (g)"):
            self.assertIn(label, page)
        # Whole grams, and a range that suits them.
        self.assertIn('id="plant-weight-input" type="number" step="1" min="0"'
                      ' max="500000"', page)
        # No converting left to do.
        self.assertNotIn("Math.round(typed * 1000)", script)
        self.assertNotIn("WEIGHT_UNIT_FIELDS", script)
        self.assertNotIn("function weightUnit(", script)
        self.assertNotIn("function gramsFrom(", script)
        # Grams on the wire, as before.
        self.assertIn("JSON.stringify({ grams, kind, at })", script)
        # The remembered choices are cleared rather than left in every
        # browser for ever, since nothing reads them now.
        self.assertIn("forgetWeightUnits", script)
        # Swept by prefix rather than by naming each box, because naming
        # them missed one that an even earlier box had left behind.
        self.assertIn('startsWith("open-plant-pulse.weight-unit")', script)

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
        # What a plant has and what its instrument has are different lists
        # now. The air and the chemistry belong to the plant, whichever
        # device measured them; the raw report log and the firmware belong
        # to the device and appear only on its own page.
        for absent in ("raw-report-log", "sensor-firmware"):
            self.assertNotIn(f'{absent}" data-page="plant"', page)
        self.assertIn('class="climate-history" data-page="plant"', page)

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


class ScaleScheduleTests(unittest.TestCase):
    """When a scale's weight is worth writing down."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.plant_id = self.store.create_plant("white bird")["plant_id"]

    @staticmethod
    def _wall(hour: int, minute: int = 0, day: int = 16) -> datetime:
        """A moment written the way a clock on the wall writes it."""
        return datetime(2026, 10, day, hour, minute).astimezone()

    def test_interval_and_hour_are_alternatives(self) -> None:
        now = self._wall(9)
        # An interval counts seconds since the last kept weight.
        self.assertTrue(due(now - timedelta(minutes=30), now, 1800, None))
        self.assertFalse(due(now - timedelta(minutes=5), now, 1800, None))
        # An hour means one a day, from that hour onwards.
        self.assertTrue(due(self._wall(9, day=15), now, None, 8))
        self.assertFalse(due(self._wall(8, 30), now, None, 8))
        self.assertFalse(due(self._wall(9, day=15), now, None, 18))
        # An hour wins: a scale keeps one schedule, not two.
        self.assertFalse(due(self._wall(8, 30), now, 60, 8))

    def test_the_hour_is_the_one_on_the_wall_not_in_greenwich(self) -> None:
        """Six in the morning means six here.

        Reading the hour off a UTC clock made a hub in Vancouver open the
        window at eleven the night before, and the day's weight landed in
        the small hours instead of at breakfast.
        """
        # The same instant, handed over in two different time zones, is the
        # same instant: whatever `due` reads the hour from, it must not be
        # the offset it happened to be given.
        moment = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
        elsewhere = moment.astimezone(timezone(timedelta(hours=5, minutes=30)))
        yesterday = moment - timedelta(days=1)
        for hour in range(0, 24):
            self.assertEqual(
                due(yesterday, moment, None, hour),
                due(yesterday, elsewhere, None, hour),
                f"hour {hour} decided differently for the same instant",
            )

        # And it is the local wall clock that decides. On a machine running
        # in UTC there is nothing to tell apart, so there is nothing to test.
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        if offset == timedelta(0):
            self.skipTest("this machine keeps UTC, so local and UTC agree")
        local_hour = self._wall(9).hour
        self.assertTrue(due(self._wall(9, day=15), self._wall(9), None, local_hour))
        # An hour that has not arrived locally yet keeps the window shut,
        # however the UTC clock reads.
        later = (local_hour + 2) % 24
        if later > local_hour:
            self.assertFalse(due(self._wall(9, day=15), self._wall(9), None, later))

    def test_the_hub_keeps_time_where_it_is_told_to(self) -> None:
        """One setting, and everything that needs an hour of the day asks it.

        Reading the machine's zone would have been enough for a hub on the
        kitchen computer and wrong for one on a server that keeps UTC, which
        is what servers do.
        """
        settings = self.store.hub_settings()
        self.assertIsNone(settings["timezone"], "nothing is chosen to begin with")
        self.assertTrue(settings["timezone_effective"], "but something is in force")

        saved = self.store.set_hub_timezone("Asia/Tokyo")
        self.assertEqual(saved["timezone"], "Asia/Tokyo")
        self.assertEqual(saved["timezone_effective"], "Asia/Tokyo")

        # Nine in the morning in Tokyo is midnight UTC, so the hour the hub
        # reasons about has to come from the setting and not the clock.
        tokyo_morning = datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)
        yesterday = tokyo_morning - timedelta(days=1)
        zone = self.store.hub_zone()
        self.assertTrue(due(yesterday, tokyo_morning, None, 6, zone=zone))
        # Six in the evening Tokyo time has not come round yet at 09:30.
        self.assertFalse(due(yesterday, tokyo_morning, None, 18, zone=zone))

        # And the day a care event is filed under moves with it: 00:30 UTC
        # is already the 16th in Tokyo and still the 15th in Vancouver.
        self.assertEqual(self.store._local_day("2026-10-16T00:30:00Z"), "2026-10-16")
        self.store.set_hub_timezone("America/Vancouver")
        self.assertEqual(self.store._local_day("2026-10-16T00:30:00Z"), "2026-10-15")

        # Handing it back is allowed, and nonsense is not.
        self.assertIsNone(self.store.set_hub_timezone(None)["timezone"])
        self.assertIsNone(self.store.set_hub_timezone("   ")["timezone"])
        with self.assertRaises(ValueError):
            self.store.set_hub_timezone("Mars/Olympus_Mons")

    def test_a_weight_that_is_the_pot_being_lifted_is_refused(self) -> None:
        keeper = WeightKeeper(self.store)
        self.assertEqual(keeper.keep(self.plant_id, 15800, "scale"), 15800)
        # Pot off the scale, and something else on it.
        self.assertIsNone(keeper.keep(self.plant_id, 120, "scale"))
        self.assertIsNone(keeper.keep(self.plant_id, 40000, "scale"))
        latest = self.store.plant_weight_status(self.plant_id)["latest"]
        self.assertEqual(latest["grams"], 15800)

    def test_the_schedule_round_trips(self) -> None:
        sensor_id = self.store.seen_zigbee_device("acebe6fffe0d7f18", "scale")["sensor_id"]
        # Read back the way the Zigbee service reads it, which is what
        # actually decides whether the schedule is honoured.
        self.store.set_scale_schedule(sensor_id, None, 8)
        self.assertEqual(self.store.zigbee_sensors()[0]["scale_keep_at_hour"], 8)
        # Choosing an interval clears the hour rather than running both.
        self.store.set_scale_schedule(sensor_id, 3600, None)
        row = self.store.zigbee_sensors()[0]
        self.assertIsNone(row["scale_keep_at_hour"])
        self.assertEqual(row["scale_keep_every_seconds"], 3600)

    def test_an_impossible_schedule_is_refused(self) -> None:
        sensor_id = self.store.seen_zigbee_device("acebe6fffe0d7f18", "scale")["sensor_id"]
        with self.assertRaises(ValueError):
            self.store.set_scale_schedule(sensor_id, None, 99)
        with self.assertRaises(ValueError):
            self.store.set_scale_schedule(sensor_id, 5, None)
        with self.assertRaises(ValueError):
            self.store.set_scale_schedule("no-such-sensor", None, 8)

    def test_the_plant_knows_which_scale_weighs_it(self) -> None:
        self.assertIsNone(self.store.plant_scale(self.plant_id))
        sensor_id = self.store.seen_zigbee_device("acebe6fffe0d7f18", "scale")["sensor_id"]
        # An unclaimed device is not yet this plant's scale.
        self.assertIsNone(self.store.plant_scale(self.plant_id))
        self.store.enroll_zigbee_device("acebe6fffe0d7f18", "White bird scale")
        self.store.attach_sensor(sensor_id, self.plant_id)
        scale = self.store.plant_scale(self.plant_id)
        self.assertIsNotNone(scale)
        self.assertEqual(scale["eui"], "acebe6fffe0d7f18")
        self.assertEqual(scale["display_name"], "White bird scale")


class ZigbeeWeightFilingTests(unittest.TestCase):
    """A Zigbee scale's reading reaches the plant it is attached to."""

    class _Network:
        def devices(self): return []
        def notes(self, eui): return {}
        def on_reading(self, handler): pass
        def on_joined(self, handler): pass
        def on_delivery(self, handler): pass

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.plant_id = self.store.create_plant("white bird")["plant_id"]
        self.service = ZigbeeService(self.store, self._Network())
        self.eui = "acebe6fffe0d7f18"
        self.store.seen_zigbee_device(self.eui, "scale")

    def _reading(self, grams: float) -> Reading:
        return Reading(eui=self.eui, kind="scale",
                       values={"grams": grams, "ok": True, "stable": True,
                               "saturated": False})

    def _latest(self):
        return self.store.plant_weight_status(self.plant_id).get("latest")

    def test_nothing_is_filed_until_the_device_is_claimed_and_attached(self) -> None:
        # A reading from a device nobody has claimed may not even be theirs.
        self.service._file(self._reading(15800))
        self.assertIsNone(self._latest())
        self.service.accept(self.eui)
        # Claimed, but pointed at nothing yet.
        self.service._file(self._reading(15800))
        self.assertIsNone(self._latest())

    def test_an_attached_scale_writes_the_plant_a_weight(self) -> None:
        self.service.accept(self.eui)
        self.service.attach(self.eui, self.plant_id)
        self.service._file(self._reading(15800))
        latest = self._latest()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["grams"], 15800)

    def test_the_same_judgement_applies_as_over_http(self) -> None:
        self.service.accept(self.eui)
        self.service.attach(self.eui, self.plant_id)
        self.service._file(self._reading(15800))
        # The pot lifted off, and something else put on.
        self.service._file(self._reading(120))
        self.service._file(self._reading(40000))
        self.assertEqual(self._latest()["grams"], 15800)

    def test_a_known_device_keeps_its_kind_across_a_restart(self) -> None:
        """Health counters are lost on restart; the device is not."""
        self.service.accept(self.eui)
        fresh = ZigbeeService(self.store, self._Network())   # nothing in memory
        fresh.attach(self.eui, self.plant_id)                # used to raise
        fresh._file(self._reading(15800))
        self.assertEqual(self._latest()["grams"], 15800)

    def test_every_report_is_kept_whether_or_not_anybody_wanted_it(self) -> None:
        """A report judged and dropped is a question that cannot be re-asked.

        Keeping only the last one, in memory, meant a weight wanted at six in
        the morning had to wait for the device to speak at six in the
        morning, and a hub restarted at midnight knew nothing until it did.
        """
        # Unclaimed, so nothing is filed against a plant -- and the report is
        # kept anyway, because an unclaimed device is exactly the one
        # somebody is still working out.
        self.service._reading(self._reading(15800))
        kept = self.store.zigbee_reports(eui=self.eui)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["values"]["grams"], 15800)
        self.assertEqual(kept[0]["kind"], "scale")
        self.assertIsNone(self._latest(), "unclaimed devices still write no weight")

        # Its own values under its own names, so the next device model does
        # not need a schema change.
        self.service._reading(Reading(eui=self.eui, kind="scale",
                                      values={"grams": 15790, "batt": 3.1}))
        newest = self.store.zigbee_reports(eui=self.eui, limit=1)[0]
        self.assertEqual(newest["values"]["batt"], 3.1)

        # Newest first, and one device's reports are its own.
        self.store.record_zigbee_report("other-device", {"grams": 1})
        mine = self.store.zigbee_reports(eui=self.eui)
        self.assertEqual(len(mine), 2)
        self.assertEqual(mine[0]["values"]["grams"], 15790)

    def test_a_scheduled_weight_can_use_what_the_device_last_said(self) -> None:
        """At six in the morning the question is not "has it just spoken"."""
        old = "2026-10-01T00:00:00Z"
        self.store.record_zigbee_report(self.eui, {"grams": 15999}, at=old)
        # Stale: a reading from last week is not this morning's weight.
        self.assertIsNone(self.store.latest_zigbee_report(self.eui, within_seconds=3600))
        # Without a freshness limit it is simply the newest there is.
        self.assertEqual(
            self.store.latest_zigbee_report(self.eui)["values"]["grams"], 15999)

        self.service._reading(self._reading(15800))
        fresh = self.store.latest_zigbee_report(self.eui, within_seconds=3600)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["values"]["grams"], 15800)

    def test_reports_are_kept_for_a_week_and_no_longer(self) -> None:
        inside = (datetime.now(timezone.utc) - timedelta(days=6)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        outside = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.store.record_zigbee_report(self.eui, {"grams": 1}, at=inside)
        self.store.record_zigbee_report(self.eui, {"grams": 2}, at=outside)
        self.assertEqual(len(self.store.zigbee_reports(eui=self.eui)), 2)

        dropped = self.store.prune_zigbee_reports()

        self.assertEqual(dropped, 1)
        left = self.store.zigbee_reports(eui=self.eui)
        self.assertEqual([r["values"]["grams"] for r in left], [1])


class ZigbeeLatenessTests(unittest.TestCase):
    """How long is too long depends on what the device said it would do."""

    # Mirrors zigbeeSilentAfter() in app.js. Kept here because the rule is
    # the thing worth pinning down, and getting it wrong is invisible: the
    # page simply says a dead sensor is fine, or a healthy one is missing.
    @staticmethod
    def silent_after(interval_s):
        expected = interval_s if interval_s and interval_s > 0 else 30
        return max(90, expected * 3)

    def test_a_fast_sensor_is_late_within_minutes(self) -> None:
        # Ten seconds apart: five minutes of silence is thirty missed reports.
        self.assertGreater(304, self.silent_after(10))
        # Twenty-five seconds is two missed reports, which happens.
        self.assertLess(25, self.silent_after(10))

    def test_a_slow_sensor_is_not_late_after_five_minutes(self) -> None:
        # Twice a day. Five minutes says nothing at all about its health,
        # and a flat fifteen minute rule called this one dead every time.
        self.assertLess(304, self.silent_after(43200))
        self.assertGreater(172800, self.silent_after(43200))

    def test_continuous_sampling_is_judged_by_the_floor(self) -> None:
        # Reports as fast as the link allows and states its interval as 0,
        # which must not be read as "due immediately".
        self.assertEqual(self.silent_after(0), 90)
        self.assertGreater(304, self.silent_after(0))


class ProbeOnlySectionTests(unittest.TestCase):
    """Sections that only a soil probe can fill say so in the markup."""

    def test_the_soil_sections_declare_the_sensor_they_need(self) -> None:
        """A plant that is weighed but not probed had four dead panels.

        Soil moisture, the health conclusion drawn from it, the drainage
        response and the chemistry all come from a probe. On the eight
        plants here with no probe they each sat saying "waiting for a
        reading" about a reading that was never coming.

        Declared on the section rather than tested for in script, which is
        how the sensor pages already decide what to draw.
        """
        static = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub"
                  / "static" / "index.html").read_text()
        for section in ("readings", "assessment", "drainage", "chemistry"):
            marker = f'data-section="{section}" data-sensor-kind="bthome"'
            self.assertIn(marker, static,
                          f"{section} should need a probe to be shown")

    def test_what_stands_without_a_probe_is_left_alone(self) -> None:
        """Weight, care, photos and the room's air need no probe.

        The air section is the room's sensor, not this plant's, and the
        rest are things a person records. Hiding those too would empty the
        page of every plant nobody has wired up.
        """
        static = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub"
                  / "static" / "index.html").read_text()
        import re
        for section in ("weight", "calendar", "photos", "care-log", "air",
                        "journey", "lighting"):
            match = re.search(rf'data-section="{section}"[^>]*>', static)
            self.assertIsNotNone(match, f"{section} is missing")
            self.assertNotIn("data-sensor-kind", match.group(0),
                             f"{section} should not need a sensor")
