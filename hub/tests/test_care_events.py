from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.domain.care_events import CareEvent, CareEventDetector
from open_plant_pulse_hub.domain.models import SensorReading


BASE_READING = SensorReading(
    sensor_id="plant-01",
    report_id=1,
    observed_at="2026-09-01T12:00:00Z",
    soil_temperature_c=22.0,
    moisture_percent=35.0,
    conductivity_us_cm=900,
    air_temperature_c=24.0,
    air_humidity_percent=50.0,
    soil_ph=6.3,
    nitrogen_mg_kg=80,
    phosphorus_mg_kg=40,
    potassium_mg_kg=110,
)


class CareEventDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = CareEventDetector()
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.detector.detect(BASE_READING, self.now)

    def test_detects_sudden_watering_rise(self) -> None:
        reading = replace(BASE_READING, report_id=2, moisture_percent=70.0)

        events = self.detector.detect(reading, self.now + timedelta(minutes=1))

        self.assertEqual([event.kind for event in events], ["watering"])
        self.assertEqual(events[0].changes["moisture_percent"], 35.0)
        self.assertIn("35.0% to 70.0%", events[0].summary)
        self.assertEqual(
            self.detector.detect(
                replace(BASE_READING, report_id=3, moisture_percent=90.0),
                self.now + timedelta(minutes=2),
            ),
            [],
        )

    def test_assesses_drainage_after_stable_post_watering_window(self) -> None:
        self.detector.detect(
            replace(BASE_READING, report_id=2, moisture_percent=75.0),
            self.now + timedelta(minutes=1),
        )
        self.assertEqual(
            self.detector.detect(
                replace(BASE_READING, report_id=3, moisture_percent=62.0),
                self.now + timedelta(minutes=9),
            ),
            [],
        )
        self.detector.detect(
            replace(BASE_READING, report_id=4, moisture_percent=60.0),
            self.now + timedelta(minutes=10),
        )
        # Steady, but not yet for the whole settle window.
        self.assertEqual(
            self.detector.detect(
                replace(BASE_READING, report_id=5, moisture_percent=60.0),
                self.now + timedelta(minutes=29),
            ),
            [],
        )

        events = self.detector.detect(
            replace(BASE_READING, report_id=6, moisture_percent=60.0),
            self.now + timedelta(minutes=30),
        )

        self.assertEqual([event.kind for event in events], ["drainage_assessment"])
        self.assertEqual(events[0].title, "Pot response: balanced")
        self.assertEqual(events[0].changes["peak_moisture_percent"], 75.0)
        self.assertEqual(events[0].changes["settled_moisture_percent"], 60.0)
        self.assertEqual(events[0].changes["retained_fraction"], 0.625)
        self.assertEqual(events[0].changes["settle_minutes"], 9.0)
        self.assertEqual(events[0].changes["response_class"], "balanced")

    def test_frequent_reports_do_not_settle_a_pot_still_draining(self) -> None:
        # The first sensor's watering on 2026-09-27: reports every 30 seconds,
        # moisture falling about a point a minute. Any three reports in a row
        # sit within 1.5 points, but the pot is not settled.
        self.detector.detect(
            replace(BASE_READING, report_id=2, moisture_percent=100.0),
            self.now + timedelta(minutes=1),
        )
        events = []
        moisture = 60.0
        for step in range(1, 60):
            moisture -= 0.5
            events += self.detector.detect(
                replace(BASE_READING, report_id=2 + step, moisture_percent=moisture),
                self.now + timedelta(minutes=1, seconds=30 * step),
            )
        self.assertNotIn("drainage_assessment", [event.kind for event in events])

    def test_ignores_normal_drift(self) -> None:
        self.assertEqual(
            self.detector.detect(replace(BASE_READING, report_id=2, moisture_percent=39.0), self.now),
            [],
        )

    def test_detects_new_watering_after_full_dry_back_during_cooldown(self) -> None:
        self.detector.detect(
            replace(BASE_READING, report_id=2, moisture_percent=70.0),
            self.now + timedelta(minutes=1),
        )
        self.detector.detect(
            replace(BASE_READING, report_id=3, moisture_percent=39.0),
            self.now + timedelta(minutes=2),
        )

        events = self.detector.detect(
            replace(BASE_READING, report_id=4, moisture_percent=70.0),
            self.now + timedelta(minutes=3),
        )

        self.assertEqual([event.kind for event in events], ["watering"])
        self.assertEqual(
            self.detector.detect(replace(BASE_READING, report_id=1, moisture_percent=70.0), self.now),
            [],
        )

    def test_detects_possible_fertilizing_from_correlated_rises(self) -> None:
        reading = replace(
            BASE_READING,
            report_id=2,
            conductivity_us_cm=1220,
            nitrogen_mg_kg=98,
        )

        events = self.detector.detect(reading, self.now + timedelta(minutes=1))

        self.assertEqual([event.kind for event in events], ["fertilizing"])
        self.assertEqual(events[0].confidence, "medium")
        self.assertEqual(events[0].changes["conductivity_us_cm"], 320.0)

    def test_an_older_report_arriving_late_changes_nothing(self) -> None:
        self.detector.detect(
            replace(BASE_READING, report_id=5, moisture_percent=36.0),
            self.now + timedelta(minutes=1),
        )
        self.assertEqual(
            self.detector.detect(
                replace(BASE_READING, report_id=4, moisture_percent=70.0),
                self.now + timedelta(minutes=2),
            ),
            [],
        )
        # The late report did not become the baseline, so the rise is still seen.
        events = self.detector.detect(
            replace(BASE_READING, report_id=6, moisture_percent=70.0),
            self.now + timedelta(minutes=3),
        )
        self.assertEqual([event.kind for event in events], ["watering"])
        self.assertEqual(events[0].changes["moisture_percent"], 34.0)

    def test_detects_fertilizing_when_nutrients_arrive_after_their_reading(self) -> None:
        without_nutrients = replace(
            BASE_READING,
            report_id=2,
            conductivity_us_cm=1220,
            soil_ph=None,
            nitrogen_mg_kg=None,
            phosphorus_mg_kg=None,
            potassium_mg_kg=None,
        )
        self.assertEqual(
            self.detector.detect(without_nutrients, self.now + timedelta(minutes=1)), []
        )
        self.assertEqual(
            self.detector.amend(
                replace(without_nutrients, report_id=3, nitrogen_mg_kg=98),
                self.now + timedelta(minutes=1),
            ),
            [],
        )

        events = self.detector.amend(
            replace(without_nutrients, nitrogen_mg_kg=98, phosphorus_mg_kg=40, potassium_mg_kg=110),
            self.now + timedelta(minutes=1),
        )

        self.assertEqual([event.kind for event in events], ["fertilizing"])
        self.assertEqual(events[0].changes["nitrogen_mg_kg"], 18.0)

    def test_detects_profile_refill_crossing_once_until_moisture_recovers(self) -> None:
        self.detector.detect(
            replace(BASE_READING, report_id=2, moisture_percent=44.0),
            self.now + timedelta(seconds=30),
            refill_below=40.0,
        )
        first = self.detector.detect(
            replace(BASE_READING, report_id=3, moisture_percent=39.5),
            self.now + timedelta(minutes=1),
            refill_below=40.0,
        )
        repeated = self.detector.detect(
            replace(BASE_READING, report_id=4, moisture_percent=40.5),
            self.now + timedelta(minutes=2),
            refill_below=40.0,
        )
        self.detector.detect(
            replace(BASE_READING, report_id=5, moisture_percent=44.0),
            self.now + timedelta(minutes=3),
            refill_below=40.0,
        )
        rearmed = self.detector.detect(
            replace(BASE_READING, report_id=6, moisture_percent=39.0),
            self.now + timedelta(minutes=4),
            refill_below=40.0,
        )

        self.assertEqual([event.kind for event in first], ["watering_due"])
        self.assertEqual(first[0].changes["refill_below_percent"], 40.0)
        self.assertEqual(repeated, [])
        self.assertEqual([event.kind for event in rearmed], ["watering_due"])


if __name__ == "__main__":
    unittest.main()

class ManualCareTests(unittest.TestCase):
    """Care a person gave the plant, which no probe could have seen."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.store.record_beacon(
            sensor_id="plant-01",
            received_at="2026-09-28T00:00:00Z",
            observed_identifier="test",
            source_adapter="test",
            rssi=-40,
            service_data=b"\x40\x00\x01",
            contract_version=3,
        )
        self.store.manage_sensor("plant-01", "Fern", "Office", "monstera", 40, 1500, 1800)

    def tearDown(self) -> None:
        self.store.close()

    def test_recording_feeding_writes_one_entry_and_counts_it(self) -> None:
        journey = self.store.record_manual_care("plant-01", "fertilizing")

        entry = self.store.care_log(10, "plant-01")[0]
        self.assertEqual(entry["kind"], "fertilizing")
        self.assertEqual(entry["title"], "Fertilized")
        self.assertEqual(entry["changes"]["source"], "manual")
        # Not a guess from a reading, so not something to confirm.
        self.assertEqual(entry["confidence"], "recorded")
        self.assertEqual(journey["fertilizing_count"], 1)

    def test_a_plant_is_fed_once_a_day_however_often_the_button_is_pressed(self) -> None:
        for _ in range(5):
            journey = self.store.record_manual_care("plant-01", "fertilizing")

        self.assertEqual(len(self.store.care_log(20, "plant-01")), 1)
        self.assertEqual(journey["fertilizing_count"], 1)
        # And the figure kept still agrees with counting the log again.
        self.assertEqual(self.store.rebuild_journey("plant-01"), journey)

    def test_another_day_is_another_feeding(self) -> None:
        self.store.record_manual_care("plant-01", "fertilizing")
        self.store.record_manual_care("plant-01", "fertilizing", at="2026-09-27T09:00:00Z")

        self.assertEqual(len(self.store.care_log(20, "plant-01")), 2)
        self.assertEqual(self.store.plant_journey("plant-01")["fertilizing_count"], 2)

    def test_a_probe_noticing_the_same_day_is_the_same_feeding(self) -> None:
        # A rise the probe reads and the hand that caused it are one feeding.
        self.store.record_manual_care("plant-01", "fertilizing", at="2026-09-28T09:00:00Z")
        self.store._save_event(
            CareEvent(
                event_id="detected-1",
                sensor_id="plant-01",
                kind="fertilizing",
                detected_at="2026-09-28T17:00:00Z",
                title="Likely fertilization",
                summary="Conductivity and nutrients rose together.",
                confidence="high",
                changes={"conductivity_us_cm": 400},
            )
        )

        entries = [e for e in self.store.care_log(20, "plant-01") if e["kind"] == "fertilizing"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.store.plant_journey("plant-01")["fertilizing_count"], 1)
        self.assertEqual(self.store.rebuild_journey("plant-01")["fertilizing_count"], 1)

    def test_a_fed_day_is_marked_on_the_year(self) -> None:
        self.store.record_manual_care("plant-01", "fertilizing", at="2026-09-20T17:00:00Z")

        # The calendar is keyed on the plant, not the sensor watching it.
        days = self.store.watering_calendar(
            self.store.sensor("plant-01")["plant_id"],
            "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"
        )
        fed = [day for day in days if day["fertilizing_count"]]

        self.assertEqual(len(fed), 1)
        # Marked on the day somebody would say they fed it, which is the day the
        # entry is named after rather than the UTC day it was stored on.
        self.assertEqual(fed[0]["date"], self.store._local_day("2026-09-20T17:00:00Z"))
        # Feeding is marked alongside whatever the day already said about water.
        self.assertEqual(fed[0]["watering_count"], 0)
        self.assertEqual(fed[0]["drying_level"], 0)

    def test_it_refuses_what_it_cannot_record(self) -> None:
        with self.assertRaises(ValueError):
            self.store.record_manual_care("plant-01", "levitating")
        with self.assertRaises(ValueError):
            self.store.record_manual_care("sensor-unknown", "fertilizing")


class PlantAsSubjectTests(unittest.TestCase):
    """A care event happened to a plant; a sensor is how the hub found out."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.store.record_beacon(
            sensor_id="sensor-one",
            received_at="2026-10-05T00:00:00Z",
            observed_identifier="test",
            source_adapter="test",
            rssi=-40,
            service_data=b"\x40\x00\x01",
            contract_version=3,
        )

    def tearDown(self) -> None:
        self.store.close()

    def enroll(self, sensor_id: str = "sensor-one", name: str = "Fern") -> int:
        self.store.manage_sensor(sensor_id, name, "Office", "strelitzia", None, None, 1800)
        return self.store.sensor(sensor_id)["plant_id"]

    def saved_event(self, sensor_id: str, kind: str, detected_at: str) -> None:
        self.store._save_event(
            CareEvent(
                event_id=f"{kind}-{detected_at}",
                sensor_id=sensor_id,
                kind=kind,
                detected_at=detected_at,
                title="t",
                summary="s",
                confidence="high",
                changes={},
            )
        )

    def test_an_event_carries_the_plant_and_the_sensor_that_noticed(self) -> None:
        plant_id = self.enroll()
        self.saved_event("sensor-one", "watering", "2026-10-05T09:00:00Z")

        row = self.store._database.execute(
            "SELECT plant_id, sensor_id FROM care_events WHERE kind = 'watering'"
        ).fetchone()

        self.assertEqual(row, (plant_id, "sensor-one"))

    def test_enrolling_adopts_what_the_sensor_noticed_beforehand(self) -> None:
        # Detected while the sensor sat in the inbox, before it had a plant.
        self.saved_event("sensor-one", "watering", "2026-10-04T09:00:00Z")
        self.assertIsNone(
            self.store._database.execute(
                "SELECT plant_id FROM care_events WHERE kind = 'watering'"
            ).fetchone()[0]
        )

        plant_id = self.enroll()

        # The plant did not begin when somebody named it.
        self.assertEqual(
            self.store._database.execute(
                "SELECT plant_id FROM care_events WHERE kind = 'watering'"
            ).fetchone()[0],
            plant_id,
        )
        self.assertEqual(self.store.plant_journey("sensor-one")["watering_count"], 1)

    def test_a_sensor_nobody_has_claimed_has_no_journey_and_that_is_not_an_error(self) -> None:
        self.saved_event("sensor-one", "watering", "2026-10-04T09:00:00Z")

        journey = self.store.plant_journey("sensor-one")

        self.assertEqual(journey["watering_count"], 0)
        self.assertIsNone(journey["started_at"])
        # Its own events are still readable, by the sensor, until a plant has them.
        self.assertEqual(len(self.store.care_log(10, "sensor-one")), 1)

    def test_the_care_log_asked_by_sensor_answers_for_the_plant(self) -> None:
        plant_id = self.enroll()
        self.saved_event("sensor-one", "watering", "2026-10-05T09:00:00Z")
        # Recorded against the plant with no sensor involved at all.
        self.store.record_manual_care("sensor-one", "fertilizing", at="2026-10-05T10:00:00Z")

        by_sensor = self.store.care_log(10, "sensor-one")
        by_plant = self.store.care_log(10, plant_id=plant_id)

        self.assertEqual([e["event_id"] for e in by_sensor], [e["event_id"] for e in by_plant])
        self.assertEqual(len(by_sensor), 2)

    def test_the_journey_can_be_asked_for_by_the_plant_itself(self) -> None:
        plant_id = self.enroll()
        self.saved_event("sensor-one", "watering", "2026-10-05T09:00:00Z")

        self.assertEqual(
            self.store.plant_journey_of(plant_id), self.store.plant_journey("sensor-one")
        )
        self.assertEqual(
            self.store.rebuild_journey_of(plant_id), self.store.plant_journey_of(plant_id)
        )


class CalendarWithoutASensorTests(unittest.TestCase):
    """A plant's care history does not depend on anything watching it."""

    def setUp(self) -> None:
        self.store = ReadingStore()

    def test_a_plant_with_no_sensor_still_has_a_year(self) -> None:
        """Nine of ten plants here have no probe, and all nine were blank.

        The calendar was keyed on a sensor id and derived the plant from
        it, so a plant nobody had wired up showed an empty year for ever --
        although watering and feeding are typed in by hand and stored
        against the plant.
        """
        plant_id = self.store.create_plant("orchid")["plant_id"]
        self.store.record_plant_care(plant_id, "watering", at="2026-05-02T09:00:00Z")
        self.store.record_plant_care(plant_id, "watering", at="2026-05-20T09:00:00Z")
        self.store.record_plant_care(plant_id, "fertilizing", at="2026-05-20T09:05:00Z")

        days = self.store.watering_calendar(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

        watered = [day for day in days if day["watering_count"]]
        fed = [day for day in days if day["fertilizing_count"]]
        self.assertEqual(len(watered), 2)
        self.assertEqual(len(fed), 1)
        # No probe, so no soil shading -- the measured part is the only part
        # that goes missing.
        self.assertTrue(all(day.get("final_moisture_percent") is None
                            for day in days))

    def test_the_watering_rhythm_is_the_plants_too(self) -> None:
        plant_id = self.store.create_plant("jesmin")["plant_id"]
        for day in ("2026-05-01", "2026-05-11", "2026-05-21"):
            self.store.record_plant_care(plant_id, "watering", at=f"{day}T09:00:00Z")
        summary = self.store.watering_interval_summary(plant_id)
        self.assertEqual(summary["interval_count"], 2)
        self.assertEqual(summary["typical_days"], 10.0)

    def test_a_weighed_plant_gets_its_calendar_shaded_from_the_pot(self) -> None:
        """A scale measures wetness from the other side of the same thing.

        Nearly all of what a pot gains and loses is water, so a plant that
        is weighed has the information to shade its year even with no probe
        in it. Most plants here are weighed and unprobed, and their years
        were blank beside the one watched plant's.
        """
        plant_id = self.store.create_plant("marble queen small")["plant_id"]
        for at, grams, kind in [
            ("2026-05-01T09:00:00Z", 506, "watered"),
            ("2026-05-04T09:00:00Z", 470, "reading"),
            ("2026-05-08T09:00:00Z", 430, "reading"),
            ("2026-05-14T09:00:00Z", 380, "dry"),
        ]:
            self.store.record_plant_weight(plant_id, grams, kind=kind, at=at)

        wetness = self.store.plant_wetness_from_weight(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

        # Full on the day it was watered, empty at its driest, falling between.
        self.assertEqual(wetness["2026-05-01"], 100.0)
        self.assertEqual(wetness["2026-05-14"], 0.0)
        self.assertGreater(wetness["2026-05-04"], wetness["2026-05-08"])

        days = self.store.watering_calendar(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
        shaded = {d["date"] for d in days
                  if d.get("final_moisture_percent") is not None}
        # Every day of the drying run, weighed or not: the four
        # weighings and the days between them, which are a straight
        # line down from one to the next.
        self.assertTrue({"2026-05-01", "2026-05-04", "2026-05-08",
                         "2026-05-14"} <= shaded)
        self.assertIn("2026-05-06", shaded)
        self.assertEqual(len(shaded), 14)
        self.assertEqual(self.store.plant_wetness_source(plant_id), "weight")

    def test_a_probe_is_not_overridden_by_the_scale(self) -> None:
        """One scale of wetness per chart, and the probe's wins.

        A row of squares meaning soil for part of the year and pot weight
        for the rest is a chart that changes meaning halfway along.
        """
        plant_id = self.store.create_plant("white bird")["plant_id"]
        self.store.record_plant_weight(plant_id, 15900, kind="watered",
                                       at="2026-05-01T09:00:00Z")
        self.store.record_plant_weight(plant_id, 15000, kind="dry",
                                       at="2026-05-10T09:00:00Z")
        self.assertEqual(self.store.plant_wetness_source(plant_id), "weight")

    def test_a_day_between_two_weighings_is_filled_in(self) -> None:
        """A pot dries steadily, so the day nobody weighed is not a mystery.

        Weighing is a daily habit that gets missed, and every miss left a
        blank square in the middle of a drying run that is otherwise a
        smooth slope either side of it.
        """
        plant_id = self.store.create_plant("marble queen small")["plant_id"]
        for at, grams, kind in [
            ("2026-05-01T09:00:00Z", 500, "watered"),
            ("2026-05-02T09:00:00Z", 478, "reading"),
            ("2026-05-04T09:00:00Z", 435, "reading"),   # the 3rd is missing
            ("2026-05-05T09:00:00Z", 400, "dry"),
        ]:
            self.store.record_plant_weight(plant_id, grams, kind=kind, at=at)

        wetness = self.store.plant_wetness_from_weight(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

        self.assertIn("2026-05-03", wetness)
        # Halfway between its neighbours, because that is what a straight
        # line between two weighings says.
        self.assertAlmostEqual(
            wetness["2026-05-03"],
            (wetness["2026-05-02"] + wetness["2026-05-04"]) / 2, places=0)

    def test_a_gap_a_pot_was_watered_across_is_left_alone(self) -> None:
        """Nothing here knows which day of the gap the watering happened.

        Filling it would draw a gentle rise over several days, showing a
        slow soaking that never happened instead of one sudden refill.
        """
        plant_id = self.store.create_plant("orchid")["plant_id"]
        self.store.record_plant_weight(plant_id, 380, kind="dry",
                                       at="2026-05-01T09:00:00Z")
        self.store.record_plant_weight(plant_id, 500, kind="watered",
                                       at="2026-05-06T09:00:00Z")

        wetness = self.store.plant_wetness_from_weight(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

        self.assertEqual(sorted(wetness), ["2026-05-01", "2026-05-06"])

    def test_a_long_silence_is_not_a_drying_curve(self) -> None:
        """Two weighings three months apart say nothing about April."""
        plant_id = self.store.create_plant("air plant")["plant_id"]
        self.store.record_plant_weight(plant_id, 500, kind="watered",
                                       at="2026-03-01T09:00:00Z")
        self.store.record_plant_weight(plant_id, 300, kind="dry",
                                       at="2026-06-01T09:00:00Z")

        wetness = self.store.plant_wetness_from_weight(
            plant_id, "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

        self.assertEqual(sorted(wetness), ["2026-03-01", "2026-06-01"])
