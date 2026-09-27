from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from open_plant_pulse_hub.domain.care_events import CareEventDetector
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
                replace(BASE_READING, report_id=3, moisture_percent=60.8),
                self.now + timedelta(minutes=9),
            ),
            [],
        )
        self.detector.detect(
            replace(BASE_READING, report_id=4, moisture_percent=60.2),
            self.now + timedelta(minutes=10),
        )

        events = self.detector.detect(
            replace(BASE_READING, report_id=5, moisture_percent=60.0),
            self.now + timedelta(minutes=11),
        )

        self.assertEqual([event.kind for event in events], ["drainage_assessment"])
        self.assertEqual(events[0].title, "Pot response: balanced")
        self.assertEqual(events[0].changes["peak_moisture_percent"], 75.0)
        self.assertEqual(events[0].changes["settled_moisture_percent"], 60.33)
        self.assertEqual(events[0].changes["retained_fraction"], 0.633)
        self.assertEqual(events[0].changes["settle_minutes"], 10.0)
        self.assertEqual(events[0].changes["response_class"], "balanced")

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