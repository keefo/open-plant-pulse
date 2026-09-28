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

        days = self.store.watering_calendar(
            "plant-01", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"
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
