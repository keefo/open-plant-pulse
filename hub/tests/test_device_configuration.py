import json
from pathlib import Path
import unittest

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.device_configuration import (
    DEVICE_CONFIG_CHARACTERISTIC_UUID,
    REPORT_ACK_CHARACTERISTIC_UUID,
    DeviceConfiguration,
    DeviceConfigurationSynchronizer,
    decode_device_configuration,
    encode_device_configuration,
    encode_report_acknowledgement,
)

FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3.json"


class DeviceConfigurationCodecTests(unittest.TestCase):
    def test_round_trips_shared_binary_fixture(self) -> None:
        # Version 6 adds the console flag to the fixed header, so the byte after
        # the two text lengths is the switch and every offset moved by one.
        config = DeviceConfiguration(42, 60, "Kitchen basil", "Kitchen", console_enabled=True)
        payload = encode_device_configuration(config)

        self.assertEqual(
            payload.hex(),
            "062a0000003c0000000d07014b69746368656e20626173696c4b69746368656e",
        )
        self.assertEqual(decode_device_configuration(payload), config)

        switched_off = DeviceConfiguration(42, 60, "Kitchen basil", "Kitchen")
        self.assertEqual(encode_device_configuration(switched_off)[11], 0)
        self.assertFalse(decode_device_configuration(encode_device_configuration(switched_off)).console_enabled)

        for interval_seconds in (1, 3, 5, 10, 30, 60):
            with self.subTest(interval_seconds=interval_seconds):
                requested = DeviceConfiguration(42, interval_seconds, "Fern", "Office")
                self.assertEqual(
                    decode_device_configuration(encode_device_configuration(requested)),
                    requested,
                )

    def test_rejects_invalid_interval_text_and_payload(self) -> None:
        for config in (
            DeviceConfiguration(1, 0, "Fern", "Office"),
            DeviceConfiguration(1, 86401, "Fern", "Office"),
            DeviceConfiguration(1, 30, "", "Office"),
            DeviceConfiguration(1, 30, "x" * 81, "Office"),
            DeviceConfiguration(1, 30, "Fern\n", "Office"),
        ):
            with self.assertRaises(ValueError):
                encode_device_configuration(config)
        with self.assertRaises(ValueError):
            decode_device_configuration(b"\x01" + b"\0" * 10)

    def test_encodes_the_version_3_report_acknowledgement_fixture(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["acknowledgement_v3"]
        payload = encode_report_acknowledgement(fixture["report_id"])

        self.assertEqual(payload.hex(), fixture["token_hex"])
        self.assertEqual(payload, bytes.fromhex("03d2040000"))
        self.assertEqual(encode_report_acknowledgement(0xFFFFFFFF).hex(), "03ffffffff")
        # Told apart from every other payload on the characteristic by its
        # version and length.
        self.assertNotEqual(payload[0], encode_device_configuration(
            DeviceConfiguration(1, 60, "Fern", "Office")
        )[0])
        for report_id in (0, -1, 0x100000000):
            with self.subTest(report_id=report_id):
                with self.assertRaises(ValueError):
                    encode_report_acknowledgement(report_id)


class DeviceConfigurationSynchronizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.sensor_id = "sensor-aabbccddeeff"
        self.store.add(
            SensorReading(
                sensor_id=self.sensor_id,
                report_id=1,
                observed_at="2026-09-13T12:00:00Z",
                soil_temperature_c=20.0,
                moisture_percent=40.0,
                conductivity_us_cm=900,
            )
        )
        self.store.manage_sensor(
            self.sensor_id, "Fern", "Office", "monstera", None, None, 60
        )
        self.assertIsNone(self.store.sensor(self.sensor_id)["sensor_reporting_interval_seconds"])

    def tearDown(self) -> None:
        self.store.close()

    async def test_writes_reads_back_and_marks_revision_applied(self) -> None:
        clients = []

        class FakeClient:
            def __init__(self, identifier, timeout):
                self.identifier = identifier
                self.timeout = timeout
                self.payload = b""
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def write_gatt_char(self, uuid, payload, response):
                self.uuid = uuid
                self.payload = bytes(payload)
                self.response = response

            async def read_gatt_char(self, uuid):
                self.read_uuid = uuid
                return self.payload

        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=FakeClient, timeout=2.0
        )
        self.assertTrue(synchronizer.has_pending(self.sensor_id))

        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier"),
            "applied",
        )

        self.assertFalse(synchronizer.has_pending(self.sensor_id))
        sensor = self.store.sensor(self.sensor_id)
        self.assertEqual(sensor["device_config_status"], "applied")
        self.assertEqual(sensor["hub_reporting_interval_seconds"], 60)
        self.assertEqual(sensor["sensor_reporting_interval_seconds"], 60)
        self.assertEqual(clients[0].uuid, DEVICE_CONFIG_CHARACTERISTIC_UUID)
        self.assertEqual(decode_device_configuration(clients[0].payload).plant_name, "Fern")

        updated = self.store.manage_sensor(
            self.sensor_id, "Fern", "Office", "monstera", None, None, 30
        )
        self.assertEqual(updated["device_config_status"], "pending")
        self.assertEqual(updated["hub_reporting_interval_seconds"], 30)
        self.assertEqual(updated["sensor_reporting_interval_seconds"], 60)

        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier"),
            "applied",
        )
        synchronized = self.store.sensor(self.sensor_id)
        self.assertEqual(synchronized["sensor_reporting_interval_seconds"], 30)

    async def test_keeps_revision_pending_after_transport_failure(self) -> None:
        class FailingClient:
            def __init__(self, identifier, timeout):
                pass

            async def __aenter__(self):
                raise RuntimeError("connection failed")

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=FailingClient
        )

        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier"),
            "failed",
        )
        self.assertTrue(synchronizer.has_pending(self.sensor_id))
        self.assertEqual(self.store.sensor(self.sensor_id)["device_config_status"], "retrying")

    def fake_client(self, writes, fail_on_write=False):
        class FakeClient:
            def __init__(self, identifier, timeout):
                self.identifier = identifier

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def write_gatt_char(self, uuid, payload, response):
                if fail_on_write:
                    raise RuntimeError("write failed")
                writes.append((uuid, bytes(payload), response))

            async def read_gatt_char(self, uuid):
                return writes[-1][1]

        return FakeClient

    def complete_report(self, report_id):
        ingestion = AdvertisementIngestionService(self.store)
        report = report_id.to_bytes(4, "little").hex()
        for service_data_hex in (f"402e2c3e{report}451001", f"403e{report}"):
            ingestion.ingest(
                Advertisement(
                    received_at="2026-09-26T21:20:05Z",
                    local_name=self.sensor_id,
                    observed_identifier="platform-identifier",
                    rssi=-48,
                    service_data=bytes.fromhex(service_data_hex),
                    source_adapter="test",
                )
            )

    def acknowledged_at(self, report_id):
        with self.store._condition:
            return self.store._database.execute(
                "SELECT acknowledged_at FROM sensor_readings "
                "WHERE sensor_id = ? AND report_id = ?",
                (self.sensor_id, report_id),
            ).fetchone()[0]

    async def test_writes_the_acknowledgement_without_reading_first_then_the_configuration(
        self,
    ) -> None:
        self.complete_report(1235)
        writes = []
        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=self.fake_client(writes)
        )
        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier", 1235),
            "applied-and-acknowledged",
        )
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            writes[0], (REPORT_ACK_CHARACTERISTIC_UUID, bytes.fromhex("03d3040000"), True)
        )
        self.assertEqual(writes[1][0], DEVICE_CONFIG_CHARACTERISTIC_UUID)
        self.assertEqual(decode_device_configuration(writes[1][1]).plant_name, "Fern")
        self.assertIsNotNone(self.acknowledged_at(1235))

    async def test_acknowledges_alone_when_no_configuration_is_waiting(self) -> None:
        self.complete_report(1235)
        writes = []
        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=self.fake_client(writes)
        )
        await synchronizer.synchronize(self.sensor_id, "platform-identifier")
        writes.clear()

        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier", 1235),
            "acknowledged",
        )
        self.assertEqual(
            writes, [(REPORT_ACK_CHARACTERISTIC_UUID, bytes.fromhex("03d3040000"), True)]
        )

    async def test_a_failed_acknowledgement_is_not_recorded(self) -> None:
        self.complete_report(1235)
        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=self.fake_client([], fail_on_write=True)
        )
        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier", 1235),
            "failed",
        )
        self.assertIsNone(self.acknowledged_at(1235))


if __name__ == "__main__":
    unittest.main()