import json
from pathlib import Path
import unittest

from open_plant_pulse_hub.ingestion.bthome import (
    decode_service_data,
    has_force_report_event,
    sensor_id_from_local_name,
)


class BTHomeDecoderTests(unittest.TestCase):
    def test_decodes_forced_report_button_event_without_changing_measurements(self) -> None:
        payload = bytes.fromhex("400007037c153a0145f100")
        reading = decode_service_data(payload, "sensor-aabbccddeeff")

        self.assertTrue(has_force_report_event(payload))
        self.assertEqual(reading.sequence, 7)
        self.assertEqual(reading.air_temperature_c, 24.1)
        self.assertEqual(reading.air_humidity_percent, 55.0)
        self.assertFalse(has_force_report_event(bytes.fromhex("400007037c1545f100")))

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

    def test_decodes_version_2_full_and_partial_fixtures(self) -> None:
        fixture_path = (
            Path(__file__).parents[2]
            / "protocol"
            / "fixtures"
            / "bthome-v2-sensor-v2.json"
        )
        events = json.loads(fixture_path.read_text(encoding="utf-8"))["events"]

        for event in events:
            if "expected" not in event:
                continue
            sensor_id = sensor_id_from_local_name(event["local_name"])
            reading = decode_service_data(bytes.fromhex(event["service_data_hex"]), sensor_id)
            for field, expected in event["expected"].items():
                self.assertEqual(getattr(reading, field), expected, event["id"])

    def test_rejects_truncated_and_unsupported_version_2_objects(self) -> None:
        fixture_path = (
            Path(__file__).parents[2]
            / "protocol"
            / "fixtures"
            / "bthome-v2-sensor-v2.json"
        )
        events = json.loads(fixture_path.read_text(encoding="utf-8"))["events"]
        rejected = [event for event in events if event.get("expected_status") == "rejected"]

        for event in rejected:
            with self.assertRaises(ValueError, msg=event["id"]):
                decode_service_data(
                    bytes.fromhex(event["service_data_hex"]),
                    "sensor-abcdef123456",
                )

    def test_uses_sensor_name_and_canonicalizes_legacy_name_during_upgrade(self) -> None:
        self.assertEqual(
            sensor_id_from_local_name("sensor-aabbccddeeff"),
            "sensor-aabbccddeeff",
        )
        self.assertEqual(
            sensor_id_from_local_name("sensor-AABBCCDDEEFF"),
            "sensor-aabbccddeeff",
        )
        self.assertEqual(
            sensor_id_from_local_name("OPP-AABBCCDDEEFF"),
            "sensor-aabbccddeeff",
        )
        with self.assertRaisesRegex(ValueError, "sensor- followed by 12 lowercase hex digits"):
            sensor_id_from_local_name("sensor-not-a-device")


if __name__ == "__main__":
    unittest.main()