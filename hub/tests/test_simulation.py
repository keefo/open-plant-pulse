from pathlib import Path
import json
import unittest

from open_plant_pulse_hub.ingestion import decode_simulation_datagram


class SimulationDecoderTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture_path = (
            Path(__file__).parents[2] / "protocol" / "fixtures" / "simulated-reading-v1.json"
        )
        self.datagram = fixture_path.read_bytes()

    def test_decodes_full_simulated_reading(self) -> None:
        reading = decode_simulation_datagram(self.datagram)

        self.assertEqual(reading.sensor_id, "simulated-plant-01")
        self.assertEqual(reading.sequence, 42)
        self.assertEqual(reading.soil_temperature_c, 21.8)
        self.assertEqual(reading.air_humidity_percent, 53.6)
        self.assertEqual(reading.soil_ph, 6.4)
        self.assertEqual(reading.nitrogen_mg_kg, 86)

    def test_rejects_unknown_schema(self) -> None:
        datagram = self.datagram.replace(
            b"open-plant-pulse.simulation.v1",
            b"open-plant-pulse.simulation.v9",
        )

        with self.assertRaisesRegex(ValueError, "schema"):
            decode_simulation_datagram(datagram)

    def test_rejects_observation_time_without_timezone(self) -> None:
        envelope = json.loads(self.datagram)
        envelope["reading"]["observed_at"] = "2026-09-01T12:00:00"

        with self.assertRaisesRegex(ValueError, "timezone"):
            decode_simulation_datagram(json.dumps(envelope).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()