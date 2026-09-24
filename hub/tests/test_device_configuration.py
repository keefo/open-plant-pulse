import unittest

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.ingestion.device_configuration import (
    DEVICE_CONFIG_CHARACTERISTIC_UUID,
    REPORT_ACK_CHARACTERISTIC_UUID,
    DeviceConfiguration,
    DeviceConfigurationSynchronizer,
    ReportAcknowledgement,
    decode_device_configuration,
    decode_report_acknowledgement,
    encode_device_configuration,
    encode_report_acknowledgement,
)


class DeviceConfigurationCodecTests(unittest.TestCase):
    def test_round_trips_shared_binary_fixture(self) -> None:
        config = DeviceConfiguration(42, 60, "Kitchen basil", "Kitchen")
        payload = encode_device_configuration(config)

        self.assertEqual(
            payload.hex(),
            "022a0000003c0000000d074b69746368656e20626173696c4b69746368656e",
        )
        self.assertEqual(decode_device_configuration(payload), config)

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

    def test_round_trips_report_acknowledgement(self) -> None:
        acknowledgement = ReportAcknowledgement(0x12345678, 42)
        payload = encode_report_acknowledgement(acknowledgement)

        self.assertEqual(payload.hex(), "01785634122a")
        self.assertEqual(decode_report_acknowledgement(payload), acknowledgement)
        with self.assertRaises(ValueError):
            decode_report_acknowledgement(b"\x02\x78\x56\x34\x12\x2a")
        with self.assertRaises(ValueError):
            encode_report_acknowledgement(ReportAcknowledgement(0, 42))


class DeviceConfigurationSynchronizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()
        self.sensor_id = "sensor-aabbccddeeff"
        self.store.add(
            SensorReading(
                sensor_id=self.sensor_id,
                sequence=1,
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

    async def test_acknowledges_exact_forced_report_after_ingestion(self) -> None:
        report_payload = encode_report_acknowledgement(ReportAcknowledgement(9, 77))
        writes = []

        class FakeClient:
            def __init__(self, identifier, timeout):
                self.identifier = identifier

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def write_gatt_char(self, uuid, payload, response):
                writes.append((uuid, bytes(payload), response))

            async def read_gatt_char(self, uuid):
                if not writes:
                    return report_payload
                return writes[-1][1]

        synchronizer = DeviceConfigurationSynchronizer(self.store, client_factory=FakeClient)
        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier", 77),
            "applied-and-acknowledged",
        )
        self.assertIn((REPORT_ACK_CHARACTERISTIC_UUID, report_payload, True), writes)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[1][0], DEVICE_CONFIG_CHARACTERISTIC_UUID)
        self.assertEqual(decode_device_configuration(writes[1][1]).plant_name, "Fern")

        with self.assertRaises(ValueError):
            decode_report_acknowledgement(report_payload[:-1])


if __name__ == "__main__":
    unittest.main()