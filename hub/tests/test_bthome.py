import json
from pathlib import Path
import unittest

from open_plant_pulse_hub.ingestion import decode_service_data


class BTHomeDecoderTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture_path = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v2.json"
        self.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_decodes_shared_sensor_fixture(self) -> None:
        reading = decode_service_data(bytes.fromhex(self.fixture["service_data_hex"]))

        self.assertEqual(
            reading.soil_temperature_c,
            self.fixture["expected"]["soil_temperature_c"],
        )
        self.assertEqual(reading.moisture_percent, self.fixture["expected"]["moisture_percent"])
        self.assertEqual(
            reading.conductivity_us_cm,
            self.fixture["expected"]["conductivity_us_cm"],
        )

    def test_rejects_wrong_object_order(self) -> None:
        payload = bytearray.fromhex(self.fixture["service_data_hex"])
        payload[4] = 0x03

        with self.assertRaisesRegex(ValueError, "object order"):
            decode_service_data(bytes(payload))


if __name__ == "__main__":
    unittest.main()