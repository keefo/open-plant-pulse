from datetime import datetime, timedelta, timezone
import unittest

from simulator.sensor import build_reading


class SensorSimulatorTests(unittest.TestCase):
    def test_accelerates_timestamp_and_plant_cycle_together(self) -> None:
        started_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

        envelope = build_reading(1, 2.0, started_at, 60.0)

        reading = envelope["reading"]
        self.assertEqual(reading["observed_at"], "2026-09-01T12:02:00Z")
        self.assertEqual(reading["moisture_percent"], 50.76)
        self.assertEqual(reading["soil_ph"], 5.3)

        completed_cycle = build_reading(2, 45.0, started_at, 60.0)
        self.assertEqual(completed_cycle["reading"]["observed_at"], "2026-09-01T12:45:00Z")
        self.assertEqual(completed_cycle["reading"]["moisture_percent"], 52.0)

    def test_time_scale_is_configurable(self) -> None:
        started_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

        envelope = build_reading(1, 45.0, started_at, 1.0)

        self.assertEqual(
            datetime.fromisoformat(envelope["reading"]["observed_at"].replace("Z", "+00:00")),
            started_at + timedelta(seconds=45),
        )
        self.assertEqual(envelope["reading"]["moisture_percent"], 51.53)


if __name__ == "__main__":
    unittest.main()