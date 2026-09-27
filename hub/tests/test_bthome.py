import json
from pathlib import Path
import unittest

from open_plant_pulse_hub.domain import ReportSupplement, SensorReading
from open_plant_pulse_hub.ingestion.bthome import (
    decode_service_data,
    is_beacon,
    sensor_id_from_local_name,
)

FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3.json"
REPLAY_FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)
SENSOR_ID = "sensor-aabbccddeeff"
MAIN_HEX = "40022e092e2c2f643ed2040000451001500037b86a562900"
SUPPLEMENTARY_HEX = "4001600c401016003ed204000054080144010002000700"


def decode(service_data_hex: str):
    return decode_service_data(bytes.fromhex(service_data_hex), SENSOR_ID)


class BTHomeFixtureTests(unittest.TestCase):
    """The shared fixture the sensor's encoder tests also check against."""

    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_fixture_is_contract_version_3(self) -> None:
        self.assertEqual(self.fixture["contract_version"], 3)
        self.assertEqual(
            {packet["expected"]["kind"] for packet in self.fixture["packets"]},
            {"main", "supplementary", "beacon"},
        )

    def test_decodes_every_fixture_packet_exactly(self) -> None:
        kinds = {"main": SensorReading, "supplementary": ReportSupplement}
        for packet in self.fixture["packets"]:
            with self.subTest(packet["name"]):
                service_data = bytes.fromhex(packet["service_data_hex"])
                expected = dict(packet["expected"])
                kind = expected.pop("kind")
                if kind == "beacon":
                    self.assertTrue(is_beacon(service_data))
                    with self.assertRaises(ValueError):
                        decode_service_data(service_data, SENSOR_ID)
                    continue
                self.assertFalse(is_beacon(service_data))
                decoded = decode_service_data(service_data, SENSOR_ID)
                self.assertIsInstance(decoded, kinds[kind])
                self.assertEqual(decoded.sensor_id, SENSOR_ID)
                self.assertEqual(decoded.contract_version, 3)
                for field, value in expected.items():
                    self.assertEqual(getattr(decoded, field), value, field)

    def test_every_supplementary_packet_says_whether_it_is_charging(self) -> None:
        charging = {
            packet["name"]: packet["expected"]["battery_charging"]
            for packet in self.fixture["packets"]
            if packet["expected"]["kind"] == "supplementary"
        }
        self.assertEqual(
            charging,
            {
                "supplementary": False,
                "supplementary_report_id_only": None,
                "supplementary_charging": True,
            },
        )

    def test_decodes_every_replay_packet_as_its_expectation_says(self) -> None:
        replay = json.loads(REPLAY_FIXTURE_PATH.read_text(encoding="utf-8"))
        kinds = {"main": SensorReading, "supplementary": ReportSupplement}
        checked = 0
        for event in replay["events"]:
            if "expected" not in event:
                continue
            with self.subTest(event["id"]):
                expected = dict(event["expected"])
                decoded = decode_service_data(
                    bytes.fromhex(event["service_data_hex"]), expected.pop("sensor_id")
                )
                self.assertIsInstance(decoded, kinds[expected.pop("kind")])
                for field, value in expected.items():
                    self.assertEqual(getattr(decoded, field), value, field)
                checked += 1
        self.assertEqual(checked, 8)


class BTHomeDecoderTests(unittest.TestCase):
    def test_a_report_id_alone_is_a_supplementary_packet(self) -> None:
        # Every report sends one, so that the hub knows when a report is complete
        # even when there is nothing else to put in it.
        supplement = decode("403ed3040000")
        self.assertIsInstance(supplement, ReportSupplement)
        self.assertEqual(supplement.report_id, 1235)
        self.assertIsNone(supplement.battery_percent)
        self.assertIsNone(supplement.battery_charging)
        self.assertIsNone(supplement.soil_ph)

    def test_a_timestamp_is_optional_and_never_invented(self) -> None:
        reading = decode("402e2c3ed3040000451001")
        self.assertIsNone(reading.observed_at)
        self.assertEqual(reading.report_id, 1235)

    def test_decodes_supplementary_packets_with_one_group_each(self) -> None:
        battery_only = decode("4001600c401016013e01000000")
        self.assertEqual(battery_only.battery_percent, 96)
        self.assertEqual(battery_only.battery_voltage_v, 4.16)
        self.assertIs(battery_only.battery_charging, True)
        self.assertIsNone(battery_only.soil_ph)
        extras_only = decode("403e0100000054080100ffff00000100")
        self.assertEqual(extras_only.soil_ph, 0.0)
        self.assertEqual(extras_only.nitrogen_mg_kg, 65535)
        self.assertEqual(extras_only.phosphorus_mg_kg, 0)
        self.assertEqual(extras_only.potassium_mg_kg, 1)
        self.assertIsNone(extras_only.battery_charging)

    def test_rejects_malformed_packets(self) -> None:
        cases = {
            "empty": "",
            "encrypted device info": "41022e092e2c2f643ed2040000451001500037b86a562900",
            "no report ID": "40022e092e2c2f64451001500037b86a562900",
            "report ID zero": "40022e092e2c2f643e00000000451001500037b86a562900",
            "truncated object": MAIN_HEX[:-2],
            "truncated report ID": "402e2c3ed304",
            "unknown object": "40022e092e2c2f643ed2040000451001500037b86a5629005a01",
            "descending order": "402f64022e092e2c3ed2040000451001500037b86a562900",
            "repeated object": "402e2c2e2c3ed3040000451001",
            "partial soil group": "40022e092e2c3ed2040000451001",
            "partial air group": "40022e092e2c2f643ed2040000562900",
            "timestamp only": "403ed204000050003bb86a",
            "moisture above 100": "40022e092e2c2f653ed2040000451001500037b86a562900",
            "humidity above 100": "402e653ed3040000451001",
            "zero timestamp": "402e2c3ed304000045100150" + "00000000",
            "mixed main and supplementary": "4001600c401016002e2c3ed3040000451001",
            "charging in a main packet": "40022e0916002e2c2f643ed2040000451001",
            "battery without voltage": "40016016003ed3040000",
            "voltage without battery": "400c401016003ed3040000",
            "battery above 100": "4001650c401016003ed3040000",
            # Charging belongs to the battery group: all three or none.
            "battery without charging": "4001600c40103ed3040000",
            "battery without charging, with extras": "4001600c40103ed304000054080144010002000700",
            "charging alone": "4016003ed3040000",
            "charging without voltage": "40016016013ed3040000",
            "charging without level": "400c401016013ed3040000",
            "charging value 2": "4001600c401016023ed3040000",
            "charging value 255": "4001600c401016ff3ed3040000",
            "charging after the report ID": "4001600c40103ed30400001600",
            "truncated charging": "4001600c401016",
            # Forced reports are ordinary reports now; the button event that
            # marked one is no longer part of the contract.
            "button event": "403a013ed3040000",
            "button event with battery": "4001600c401016003a013ed304000054080144010002000700",
            "main and report ID with a button event": "402e2c3a013ed3040000451001",
            "extras with the wrong length": "403ed30400005407014401000200",
            "extras of an unknown layout": "403ed304000054080244010002000700",
            "pH above 14": "403ed30400005408018d010002000700",
            "truncated extras": "403ed3040000540801440100020007",
            "extras missing their length": "403ed304000054",
            "old three-byte beacon": "400007",
            "old contract v2 packet": "40002a022e0903f014148a0c45f20056d204",
        }
        for name, service_data_hex in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    decode(service_data_hex)

    def test_only_the_single_device_info_byte_is_a_beacon(self) -> None:
        self.assertTrue(is_beacon(b"\x40"))
        self.assertFalse(is_beacon(b""))
        self.assertFalse(is_beacon(b"\x41"))
        self.assertFalse(is_beacon(bytes.fromhex("400007")))
        self.assertFalse(is_beacon(bytes.fromhex(MAIN_HEX)))

    def test_accepts_only_the_lowercase_sensor_name(self) -> None:
        self.assertEqual(
            sensor_id_from_local_name("sensor-aabbccddeeff"),
            "sensor-aabbccddeeff",
        )
        for name in ("sensor-AABBCCDDEEFF", "OPP-AABBCCDDEEFF", "sensor-not-a-device", None):
            with self.subTest(name):
                with self.assertRaisesRegex(
                    ValueError, "sensor- followed by 12 lowercase hex digits"
                ):
                    sensor_id_from_local_name(name)


if __name__ == "__main__":
    unittest.main()
