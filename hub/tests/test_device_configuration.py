import json
from pathlib import Path
import unittest

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.device_configuration import (
    DEVICE_CONFIG_CHARACTERISTIC_UUID,
    DRAIN_END,
    DRAIN_MAX_PAGES,
    DRAIN_REQUEST,
    DRAIN_SOURCE_ADAPTER,
    DeviceConfiguration,
    DeviceConfigurationSynchronizer,
    QueuedReport,
    decode_device_configuration,
    decode_queue_page,
    encode_cumulative_acknowledgement,
    encode_device_configuration,
)

from test_ble_ingestion import FakeQueueSensor, packet1_hex, packet2_hex

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



def record(report_id, packet1, packet2):
    return (
        report_id.to_bytes(4, "little")
        + bytes((len(packet1),)) + packet1
        + bytes((len(packet2),)) + packet2
    )


class BulkDrainCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["bulk_drain"]
        self.sensor_id = "sensor-aabbccddeeff"

    def test_decodes_the_fixture_page_into_its_reports_and_exact_packets(self) -> None:
        # Each record is exactly the two packets the sensor advertises for it.
        packets = {
            packet["name"]: bytes.fromhex(packet["service_data_hex"])
            for packet in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["packets"]
        }
        reports = decode_queue_page(bytes.fromhex(self.fixture["page_hex"]), self.sensor_id)
        self.assertEqual([report.report_id for report in reports], self.fixture["page_reports"])
        self.assertEqual(
            reports,
            [
                QueuedReport(1234, packets["packet1"], packets["packet2"]),
                QueuedReport(
                    1235,
                    packets["packet1_air_only_no_timestamp"],
                    packets["packet2_report_id_only"],
                ),
            ],
        )

    def test_decodes_the_empty_page(self) -> None:
        self.assertEqual(
            decode_queue_page(bytes.fromhex(self.fixture["empty_page_hex"]), self.sensor_id), []
        )

    def test_encodes_the_drain_tokens_of_the_fixture(self) -> None:
        self.assertEqual(DRAIN_REQUEST.hex(), self.fixture["drain_request_hex"])
        self.assertEqual(DRAIN_END.hex(), self.fixture["end_hex"])
        payload = encode_cumulative_acknowledgement(self.fixture["cumulative_ack_report_id"])
        self.assertEqual(payload.hex(), self.fixture["cumulative_ack_hex"])
        self.assertEqual(payload, bytes.fromhex("21d3040000"))
        self.assertEqual(encode_cumulative_acknowledgement(0xFFFFFFFF).hex(), "21ffffffff")
        for report_id in (0, -1, 0x100000000):
            with self.subTest(report_id=report_id):
                with self.assertRaises(ValueError):
                    encode_cumulative_acknowledgement(report_id)
        # Told apart from every other payload on the characteristic by its
        # first byte.
        configuration = encode_device_configuration(DeviceConfiguration(1, 60, "Fern", "Office"))
        for token in (DRAIN_REQUEST, DRAIN_END, payload):
            self.assertNotIn(token[0], (configuration[0], 3, 5, 7))

    def test_refuses_a_malformed_page_whole(self) -> None:
        page = bytes.fromhex(self.fixture["page_hex"])
        packet1_7, packet2_7 = bytes.fromhex(packet1_hex(7)), bytes.fromhex(packet2_hex(7))
        packet1_8, packet2_8 = bytes.fromhex(packet1_hex(8)), bytes.fromhex(packet2_hex(8))
        nine = b"".join(
            record(report_id, bytes.fromhex(packet1_hex(report_id)),
                   bytes.fromhex(packet2_hex(report_id)))
            for report_id in range(1, 10)
        )
        for description, payload in (
            ("nothing", b""),
            ("marker only", b"\x20"),
            ("wrong marker", b"\x21" + page[1:]),
            ("a configuration", encode_device_configuration(
                DeviceConfiguration(1, 60, "Fern", "Office"))),
            ("more than eight", bytes((0x20, 9)) + nine),
            ("count too high for the records", bytes((0x20, 3)) + page[2:]),
            ("truncated", page[:-1]),
            ("bytes after the last record", page + b"\x00"),
            ("count too low for the records", bytes((0x20, 1)) + page[2:]),
            ("empty packet", bytes((0x20, 1)) + record(7, b"", packet2_7)),
            ("record ID not the packets'", bytes((0x20, 1)) + record(8, packet1_7, packet2_7)),
            ("packets of two reports", bytes((0x20, 1)) + record(7, packet1_7, packet2_8)),
            ("two packet 1s", bytes((0x20, 1)) + record(7, packet1_7, packet1_7)),
            ("two packet 2s",
             bytes((0x20, 1)) + record(7, packet2_7, packet2_7)),
            ("swapped packets", bytes((0x20, 1)) + record(7, packet2_7, packet1_7)),
            ("a beacon", bytes((0x20, 1)) + record(7, packet1_7, b"\x40")),
            ("undecodable packet", bytes((0x20, 1)) + record(7, packet1_7, b"\x41\x3e\x07")),
            ("out of order", bytes((0x20, 2)) + record(8, packet1_8, packet2_8)
             + record(7, packet1_7, packet2_7)),
            ("too long", bytes((0x20, 0)) + b"\x00" * 511),
        ):
            with self.subTest(description):
                with self.assertRaises(ValueError):
                    decode_queue_page(payload, self.sensor_id)


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

    def fake_client(self, writes):
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
                return writes[-1][1]

        return FakeClient

    async def test_configuration_alone_writes_no_drain_token(self) -> None:
        writes = []
        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=self.fake_client(writes)
        )
        self.assertEqual(
            await synchronizer.synchronize(self.sensor_id, "platform-identifier"), "applied"
        )
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1][0], 6)


class BulkDrainTests(unittest.IsolatedAsyncioTestCase):
    """Draining a sensor's queue over one connection."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.sensor_id = "sensor-aabbccddeeff"
        self.ingestion = AdvertisementIngestionService(self.store)
        self.hear("40")
        self.store.manage_sensor(self.sensor_id, "Fern", "Office", "monstera", None, None, 60)
        revision = self.store.pending_device_configuration(self.sensor_id)["revision"]
        self.store.mark_device_configuration_applied(self.sensor_id, revision, 60)
        self.sensor = FakeQueueSensor()

    def tearDown(self) -> None:
        self.store.close()

    def hear(self, service_data_hex):
        return self.ingestion.ingest(
            Advertisement(
                received_at="2026-09-26T21:20:05Z",
                local_name=self.sensor_id,
                observed_identifier="platform-identifier",
                rssi=-48,
                service_data=bytes.fromhex(service_data_hex),
                source_adapter="bleak",
            )
        )

    async def drain(self, fail=False, **work):
        synchronizer = DeviceConfigurationSynchronizer(
            self.store, client_factory=self.sensor.factory(fail=fail)
        )
        return await synchronizer.drain(
            self.sensor_id, "platform-identifier", "connection-target",
            self.ingestion.ingest, **work
        )

    def rows(self, query, *parameters):
        with self.store._condition:
            return self.store._database.execute(query, parameters).fetchall()

    def acknowledged(self):
        return [
            row[0] for row in self.rows(
                "SELECT report_id FROM sensor_readings WHERE sensor_id = ? "
                "AND acknowledged_at IS NOT NULL ORDER BY report_id",
                self.sensor_id,
            )
        ]

    def writes(self):
        return [payload.hex() for payload in self.sensor.writes]

    async def test_drains_a_backlog_in_pages_over_one_connection(self) -> None:
        for report_id in range(101, 121):
            self.sensor.add(report_id)

        result = await self.drain()

        self.assertEqual(result.outcome, "drained")
        self.assertEqual((result.stored_packets, result.pages), (40, 4))
        self.assertEqual(result.acknowledged_report_id, 120)
        self.assertEqual(self.sensor.connections, 1)
        self.assertEqual(self.sensor.queue, [])
        self.assertEqual(
            self.writes(),
            ["20", encode_cumulative_acknowledgement(108).hex(),
             encode_cumulative_acknowledgement(116).hex(),
             encode_cumulative_acknowledgement(120).hex(), "22"],
        )
        self.assertEqual(
            [item["reading"]["report_id"] for item in self.store.history(self.sensor_id)],
            list(range(101, 121)),
        )
        self.assertEqual(self.acknowledged(), list(range(101, 121)))
        # Every packet is in the raw report log, marked as drained.
        self.assertEqual(
            self.rows(
                "SELECT packet_kind, decode_status, observed_identifier, rssi, COUNT(*) "
                "FROM advertisements WHERE source_adapter = ? GROUP BY packet_kind",
                DRAIN_SOURCE_ADAPTER,
            ),
            [("packet1", "accepted", "platform-identifier", None, 20),
             ("packet2", "accepted", "platform-identifier", None, 20)],
        )
        newest = self.store.raw_sensor_reports(self.sensor_id, 1)[0]
        self.assertEqual(
            (newest["report_id"], newest["packet_kind"], newest["source_adapter"]),
            (120, "packet2", DRAIN_SOURCE_ADAPTER),
        )

    async def test_reports_already_heard_over_the_air_are_acknowledged_too(self) -> None:
        for report_id in (1, 2, 3):
            self.sensor.add(report_id)
        for service_data_hex in (packet1_hex(2), packet2_hex(2), packet1_hex(3)):
            self.hear(service_data_hex)

        result = await self.drain()

        self.assertEqual(result.outcome, "drained")
        self.assertEqual(result.stored_packets, 3)
        self.assertEqual(self.writes(), ["20", "2103000000", "22"])
        self.assertEqual(self.acknowledged(), [1, 2, 3])

    async def test_drained_fixture_reports_keep_whether_the_battery_was_charging(self) -> None:
        packets = {
            packet["name"]: bytes.fromhex(packet["service_data_hex"])
            for packet in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["packets"]
        }
        self.sensor.queue = [
            (1234, packets["packet1"], packets["packet2"]),
            (1235, packets["packet1_air_only_no_timestamp"], packets["packet2_report_id_only"]),
            (1236, bytes.fromhex(packet1_hex(1236)), packets["packet2_charging"]),
        ]

        result = await self.drain()

        self.assertEqual(result.outcome, "drained")
        self.assertEqual(result.stored_packets, 6)
        self.assertEqual(self.acknowledged(), [1234, 1235, 1236])
        self.assertEqual(
            [
                (item["reading"]["report_id"], item["reading"]["battery_charging"])
                for item in self.store.history(self.sensor_id)
            ],
            [(1234, False), (1235, None), (1236, True)],
        )
        latest = self.store.latest(self.sensor_id)["reading"]
        self.assertEqual(
            (latest["battery_percent"], latest["battery_voltage_v"], latest["battery_charging"]),
            (91, 4.116, True),
        )
        self.assertEqual(
            self.rows(
                "SELECT report_id, battery_charging FROM report_packet2 "
                "WHERE sensor_id = ? ORDER BY report_id",
                self.sensor_id,
            ),
            [(1234, 0), (1235, None), (1236, 1)],
        )

    async def test_an_empty_queue_ends_at_once(self) -> None:
        result = await self.drain()
        self.assertEqual((result.outcome, result.pages), ("drained", 1))
        self.assertEqual(self.writes(), ["20", "22"])

    async def test_a_conflict_stops_the_acknowledgement_before_it(self) -> None:
        # Report 104 is stored with different content from what the sensor holds.
        self.hear(packet1_hex(104, humidity=0x2D))
        for report_id in range(101, 111):
            self.sensor.add(report_id)

        result = await self.drain()

        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(result.acknowledged_report_id, 103)
        self.assertEqual(self.writes(), ["20", "2167000000", "22"])
        self.assertEqual(self.acknowledged(), [101, 102, 103])
        self.assertEqual(
            [report[0] for report in self.sensor.queue], list(range(104, 111))
        )
        # The rest of the page is stored all the same, but kept on the sensor.
        stored = [item["reading"]["report_id"] for item in self.store.history(self.sensor_id)]
        self.assertEqual(stored, [104, 101, 102, 103, 105, 106, 107, 108])

    async def test_a_conflict_first_on_the_page_acknowledges_nothing(self) -> None:
        self.hear(packet1_hex(101, humidity=0x2D))
        for report_id in (101, 102):
            self.sensor.add(report_id)
        result = await self.drain()
        self.assertEqual(result.outcome, "stopped")
        self.assertIsNone(result.acknowledged_report_id)
        self.assertEqual(self.writes(), ["20", "22"])
        self.assertEqual(self.acknowledged(), [])

    async def test_a_page_with_a_mismatching_record_is_refused(self) -> None:
        self.sensor.add(7)
        self.sensor.queue.append(
            (9, bytes.fromhex(packet1_hex(8)), bytes.fromhex(packet2_hex(8)))
        )

        result = await self.drain()

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(self.writes(), ["20", "22"])
        self.assertEqual(self.store.history(self.sensor_id), [])
        self.assertEqual(len(self.sensor.queue), 2)

    async def test_stops_when_the_sensor_does_not_remove_what_it_was_told_to(self) -> None:
        for report_id in range(1, 12):
            self.sensor.add(report_id)
        self.sensor.ignore_acknowledgements = True
        result = await self.drain()
        self.assertEqual((result.outcome, result.pages), ("failed", 2))
        self.assertEqual(self.writes(), ["20", "2108000000", "22"])

    async def test_one_connection_reads_a_bounded_number_of_pages(self) -> None:
        for report_id in range(1, 201):
            self.sensor.add(report_id)
        result = await self.drain()
        self.assertEqual((result.outcome, result.pages), ("more", DRAIN_MAX_PAGES))
        self.assertEqual(result.acknowledged_report_id, DRAIN_MAX_PAGES * 8)
        self.assertEqual(self.writes()[-1], "22")
        self.assertEqual(len(self.sensor.queue), 200 - DRAIN_MAX_PAGES * 8)

    async def test_a_failed_connection_keeps_configuration_pending(self) -> None:
        self.store.manage_sensor(self.sensor_id, "Fern", "Study", "monstera", None, None, 60)
        self.sensor.add(1)
        result = await self.drain(fail=True, configure=True)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(self.store.sensor(self.sensor_id)["device_config_status"], "retrying")
        self.assertEqual(self.acknowledged(), [])

    async def test_configuration_goes_before_the_drain_on_the_same_link(self) -> None:
        self.store.manage_sensor(self.sensor_id, "Fern", "Study", "monstera", None, None, 60)
        self.sensor.add(1)

        result = await self.drain(configure=True)

        self.assertEqual(result.outcome, "drained")
        self.assertEqual(self.sensor.connections, 1)
        self.assertEqual(decode_device_configuration(self.sensor.writes[0]).room, "Study")
        self.assertEqual(self.writes()[1:], ["20", "2101000000", "22"])
        self.assertIsNone(self.store.pending_device_configuration(self.sensor_id))


if __name__ == "__main__":
    unittest.main()