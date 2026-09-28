import asyncio
import inspect
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.application.migrations import DATABASE_SCHEMA_VERSION, MIGRATIONS
from open_plant_pulse_hub.application.store import RECEIVE_DIAGNOSTIC_LIMIT
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.ingestion import ble
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.ble import BleakSubscriber, _detection_callback
from open_plant_pulse_hub.ingestion.bthome import BTHOME_SERVICE_UUID
from open_plant_pulse_hub.ingestion.device_configuration import (
    DRAIN_SOURCE_ADAPTER,
    DeviceConfigurationSynchronizer,
    DrainResult,
)
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay

FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)


SENSOR_ID = "sensor-aabbccddeeff"
PACKET1_1235 = "402e2c3ed3040000451001"
PACKET2_1235 = "4001600c401016003ed304000054080144010002000700"
PACKET1_1236 = "402e2c3ed4040000451001"
PACKET2_1236 = "403ed4040000"


def packet1_hex(report_id, humidity=0x2C):
    return f"402e{humidity:02x}3e{report_id.to_bytes(4, 'little').hex()}451001"


def packet2_hex(report_id):
    return f"403e{report_id.to_bytes(4, 'little').hex()}"


def advertisement(service_data_hex, received_at="2026-09-26T21:20:05Z", **overrides):
    fields = {
        "received_at": received_at,
        "local_name": SENSOR_ID,
        "observed_identifier": "replay-a",
        "rssi": -48,
        "service_data": bytes.fromhex(service_data_hex),
        "source_adapter": "replay",
    }
    fields.update(overrides)
    return Advertisement(**fields)


class AdvertisementReplayTests(unittest.TestCase):
    def test_replays_three_sensors_without_duplicates_or_stale_values(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(database_path=database_path)
            try:
                statuses = AdvertisementReplay(
                    AdvertisementIngestionService(store)
                ).replay(FIXTURE_PATH)
                history = store.history()
            finally:
                store.close()

            self.assertEqual(
                statuses,
                [event["expected_status"] for event in fixture["events"]],
            )
            self.assertEqual(len(history), 4)
            self.assertEqual(len({item["reading"]["sensor_id"] for item in history}), 3)
            joined = history[0]["reading"]
            self.assertEqual(joined["report_id"], 42)
            self.assertEqual(joined["observed_at"], "2026-09-13T12:00:00Z")
            self.assertEqual(joined["moisture_percent"], 32.0)
            self.assertEqual(joined["battery_percent"], 87)
            self.assertEqual(joined["battery_voltage_v"], 3.912)
            self.assertIs(joined["battery_charging"], False)
            self.assertEqual(joined["soil_ph"], 6.5)
            self.assertEqual(
                [joined["nitrogen_mg_kg"], joined["phosphorus_mg_kg"], joined["potassium_mg_kg"]],
                [12, 8, 30],
            )
            air_only = history[1]["reading"]
            self.assertIsNone(air_only["soil_temperature_c"])
            self.assertIsNone(air_only["moisture_percent"])
            self.assertIsNone(air_only["observed_at"])
            self.assertEqual(history[1]["received_at"], "2026-09-13T12:30:00Z")
            self.assertEqual(air_only["air_temperature_c"], 24.5)
            self.assertEqual(
                fixture["events"][3]["expected"]["soil_source_status"],
                "unavailable",
            )
            packet2_first = history[2]["reading"]
            self.assertEqual(packet2_first["report_id"], 7)
            self.assertEqual(packet2_first["battery_percent"], 64)
            self.assertIs(packet2_first["battery_charging"], True)
            self.assertIsNone(history[1]["reading"]["battery_charging"])
            self.assertIsNone(packet2_first["soil_ph"])

            with sqlite3.connect(database_path) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], 3)
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    database.execute(
                        "SELECT decode_status, COUNT(*) FROM advertisements "
                        "GROUP BY decode_status ORDER BY decode_status"
                    ).fetchall(),
                    [("accepted", 8), ("conflict", 1), ("duplicate", 1), ("rejected", 2)],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT moisture_percent FROM sensor_readings "
                        "WHERE sensor_id = ? AND report_id = 42",
                        (SENSOR_ID,),
                    ).fetchone()[0],
                    32.0,
                )

            reopened = ReadingStore(database_path=database_path)
            try:
                statuses = AdvertisementReplay(
                    AdvertisementIngestionService(reopened)
                ).replay(FIXTURE_PATH)
                self.assertEqual(
                    statuses,
                    ["duplicate"] * 9 + ["conflict", "rejected", "rejected"],
                )
                self.assertEqual(len(reopened.history()), 4)
            finally:
                reopened.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0],
                    4,
                )


    def test_bounds_rejected_advertisement_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(database_path=database_path)
            for index in range(RECEIVE_DIAGNOSTIC_LIMIT + 5):
                store.record_rejected_advertisement(
                    received_at=f"2026-09-13T12:{index // 60:02d}:{index % 60:02d}Z",
                    observed_identifier=f"rejected-{index}",
                    source_adapter="test",
                    rssi=None,
                    service_data=b"invalid",
                    error="invalid fixture",
                )
            store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM advertisements").fetchone()[0],
                    RECEIVE_DIAGNOSTIC_LIMIT,
                )


class ReportJoinTests(unittest.TestCase):
    """Packet 1 and packet 2 of one report, in either order."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.ingestion = AdvertisementIngestionService(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def assert_joined(self) -> None:
        latest = self.store.latest(SENSOR_ID)
        reading = latest["reading"]
        self.assertEqual(reading["report_id"], 1235)
        self.assertEqual(reading["air_temperature_c"], 27.2)
        self.assertEqual(reading["battery_percent"], 96)
        self.assertEqual(reading["battery_voltage_v"], 4.16)
        self.assertIs(reading["battery_charging"], False)
        self.assertEqual(reading["soil_ph"], 6.8)
        self.assertEqual(
            [reading["nitrogen_mg_kg"], reading["phosphorus_mg_kg"], reading["potassium_mg_kg"]],
            [1, 2, 7],
        )
        # The live cache that serves the unscoped latest must agree.
        self.assertEqual(self.store.latest()["reading"], reading)
        self.assertEqual(self.store.sensor(SENSOR_ID)["reading_count"], 1)

    def test_packet1_then_packet2_joins_one_reading(self) -> None:
        self.assertEqual(self.ingestion.ingest(advertisement(PACKET1_1235)), "accepted")
        # Stored, but not the latest until its second packet makes it whole.
        self.assertIsNone(self.store.latest(SENSOR_ID))
        self.assertEqual(len(self.store.history(SENSOR_ID)), 1)
        self.assertEqual(self.ingestion.ingest(advertisement(PACKET2_1235)), "accepted")
        self.assert_joined()

    def test_latest_stays_on_the_last_complete_report(self) -> None:
        """A report whose second packet is still coming must not blank the page."""
        self.ingestion.ingest(advertisement(PACKET1_1235))
        self.ingestion.ingest(advertisement(PACKET2_1235))
        self.ingestion.ingest(advertisement(PACKET1_1236, received_at="2026-09-26T21:20:10Z"))
        for latest in (self.store.latest(SENSOR_ID), self.store.latest()):
            self.assertEqual(latest["reading"]["report_id"], 1235)
            self.assertEqual(latest["reading"]["battery_percent"], 96)
            self.assertEqual(latest["reading"]["soil_ph"], 6.8)
        self.ingestion.ingest(advertisement(PACKET2_1236, received_at="2026-09-26T21:20:11Z"))
        self.assertEqual(self.store.latest(SENSOR_ID)["reading"]["report_id"], 1236)

    def test_latest_is_the_newest_report_not_the_last_stored(self) -> None:
        # The newest report arrives over the air; a drain then backfills the
        # two the air missed, which are stored after it.
        for service_data_hex in (packet1_hex(50), packet2_hex(50)):
            self.ingestion.ingest(advertisement(service_data_hex))
        for report_id in (48, 49):
            for service_data_hex in (packet1_hex(report_id, 0x30), packet2_hex(report_id)):
                self.assertEqual(
                    self.ingestion.ingest(
                        advertisement(
                            service_data_hex,
                            "2026-09-26T21:20:09Z",
                            source_adapter=DRAIN_SOURCE_ADAPTER,
                            rssi=None,
                        )
                    ),
                    "accepted",
                )
        for latest in (self.store.latest(SENSOR_ID), self.store.latest()):
            self.assertEqual(latest["reading"]["report_id"], 50)
            self.assertEqual(latest["reading"]["air_humidity_percent"], 44.0)
        self.assertEqual(
            [item["reading"]["report_id"] for item in self.store.history(SENSOR_ID)],
            [50, 48, 49],
        )

    def test_a_reading_without_a_report_id_does_not_outrank_a_report(self) -> None:
        self.ingestion.ingest(advertisement(PACKET1_1235))
        self.ingestion.ingest(advertisement(PACKET2_1235))
        self.store.add(
            SensorReading(
                sensor_id=SENSOR_ID,
                report_id=None,
                observed_at="2026-09-26T21:21:00Z",
                soil_temperature_c=20.0,
                moisture_percent=40.0,
                conductivity_us_cm=900,
            )
        )
        self.assertEqual(self.store.latest(SENSOR_ID)["reading"]["report_id"], 1235)

    def test_says_whether_a_report_is_acknowledged_and_whether_one_before_it_is_missing(
        self,
    ) -> None:
        for report_id in (10, 11, 12, 14):
            for service_data_hex in (packet1_hex(report_id), packet2_hex(report_id)):
                self.ingestion.ingest(advertisement(service_data_hex))
        # Nothing acknowledged yet: there is nowhere to count a gap from.
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 14), (False, False))
        self.store.mark_reports_acknowledged(SENSOR_ID, [10, 11])
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 11), (True, False))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 12), (False, False))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 13), (False, False))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 14), (False, True))
        # Half a report is not held: its packet 2 may be the one missed.
        self.ingestion.ingest(advertisement(packet1_hex(13)))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 14), (False, True))
        self.ingestion.ingest(advertisement(packet2_hex(13)))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 14), (False, False))
        self.assertEqual(self.store.report_delivery(SENSOR_ID, 16), (False, True))
        # A report may carry no battery or soil extras; complete is complete.
        self.assertIsNone(self.store.latest(SENSOR_ID)["reading"]["battery_percent"])

    def test_packet2_then_packet1_joins_one_reading(self) -> None:
        self.assertEqual(self.ingestion.ingest(advertisement(PACKET2_1235)), "accepted")
        # Held, but not a reading until packet 1 arrives.
        self.assertIsNone(self.store.latest(SENSOR_ID))
        self.assertEqual(self.store.history(SENSOR_ID), [])
        self.assertEqual(self.ingestion.ingest(advertisement(PACKET1_1235)), "accepted")
        self.assert_joined()

    def test_repeated_packets_are_duplicates_and_store_nothing(self) -> None:
        for service_data_hex in (PACKET1_1235, PACKET2_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(
            self.ingestion.ingest(advertisement(PACKET1_1235, "2026-09-26T21:20:09Z")),
            "duplicate",
        )
        self.assertEqual(
            self.ingestion.ingest(advertisement(PACKET2_1235, "2026-09-26T21:20:10Z")),
            "duplicate",
        )
        self.assert_joined()
        self.assertEqual(
            self.store.latest(SENSOR_ID)["received_at"], "2026-09-26T21:20:05Z"
        )
        self.assertEqual(
            [item["decode_status"] for item in self.store.raw_sensor_reports(SENSOR_ID)],
            ["duplicate", "duplicate", "accepted", "accepted"],
        )

    def test_same_report_with_different_content_is_a_logged_conflict(self) -> None:
        for service_data_hex in (PACKET1_1235, PACKET2_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        # Humidity 45 instead of 44, and battery 95 instead of 96.
        self.assertEqual(
            self.ingestion.ingest(advertisement("402e2d3ed3040000451001")),
            "conflict",
        )
        self.assertEqual(
            self.ingestion.ingest(
                advertisement("40015f0c401016003ed304000054080144010002000700")
            ),
            "conflict",
        )
        self.assert_joined()
        self.assertEqual(self.store.latest(SENSOR_ID)["reading"]["air_humidity_percent"], 44.0)
        conflicts = self.store.raw_sensor_reports(SENSOR_ID)[:2]
        self.assertEqual([item["decode_status"] for item in conflicts], ["conflict", "conflict"])
        self.assertEqual([item["report_id"] for item in conflicts], [1235, 1235])
        self.assertEqual(
            [item["packet_kind"] for item in conflicts], ["packet2", "packet1"]
        )

    def test_a_charging_report_joins_its_reading_as_charging(self) -> None:
        self.ingestion.ingest(advertisement(packet1_hex(1236)))
        self.assertEqual(
            self.ingestion.ingest(advertisement("40015b0c141016013ed4040000")), "accepted"
        )
        for latest in (self.store.latest(SENSOR_ID), self.store.latest()):
            reading = latest["reading"]
            self.assertEqual(reading["report_id"], 1236)
            self.assertEqual(reading["battery_percent"], 91)
            self.assertEqual(reading["battery_voltage_v"], 4.116)
            self.assertIs(reading["battery_charging"], True)
        with self.store._condition:
            stored = self.store._database.execute(
                "SELECT battery_charging FROM sensor_readings "
                "UNION ALL SELECT battery_charging FROM report_packet2"
            ).fetchall()
        self.assertEqual(stored, [(1,), (1,)])

    def test_a_packet2_differing_only_in_charging_is_a_conflict(self) -> None:
        for service_data_hex in (PACKET1_1235, PACKET2_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(
            self.ingestion.ingest(
                advertisement("4001600c401016013ed304000054080144010002000700")
            ),
            "conflict",
        )
        self.assert_joined()

    def test_a_conflicting_packet2_held_before_its_packet1_is_not_attached(self) -> None:
        self.ingestion.ingest(advertisement(PACKET2_1235))
        self.assertEqual(
            self.ingestion.ingest(
                advertisement("40015f0c401016003ed304000054080144010002000700")
            ),
            "conflict",
        )
        self.ingestion.ingest(advertisement(PACKET1_1235))
        self.assert_joined()

    def test_an_unknown_time_stays_unknown(self) -> None:
        self.ingestion.ingest(advertisement(PACKET1_1235, "2026-09-26T21:20:05Z"))
        self.ingestion.ingest(advertisement(PACKET2_1235, "2026-09-26T21:20:05Z"))
        latest = self.store.latest(SENSOR_ID)
        self.assertIsNone(latest["reading"]["observed_at"])
        self.assertEqual(latest["received_at"], "2026-09-26T21:20:05Z")
        with self.store._condition:
            stored = self.store._database.execute(
                "SELECT observed_at, received_at FROM sensor_readings"
            ).fetchall()
        self.assertEqual(stored, [(None, "2026-09-26T21:20:05Z")])
        # Placed by when it was received for ranges, without claiming that time.
        items = self.store.history_range(
            SENSOR_ID, "2026-09-26T21:00:00Z", "2026-09-26T22:00:00Z"
        )
        self.assertEqual([item["reading"]["report_id"] for item in items], [1235])
        self.assertIsNone(items[0]["reading"]["observed_at"])

    def test_the_sensor_timestamp_is_the_observation_time(self) -> None:
        self.ingestion.ingest(
            advertisement(
                "40022e092e2c2f643ed2040000451001500037b86a562900",
                "2026-09-26T21:25:00Z",
            )
        )
        self.ingestion.ingest(advertisement("403ed2040000", "2026-09-26T21:25:01Z"))
        latest = self.store.latest(SENSOR_ID)
        self.assertEqual(latest["reading"]["observed_at"], "2026-09-26T21:20:00Z")
        self.assertEqual(latest["received_at"], "2026-09-26T21:25:00Z")

    def test_a_nutrient_rise_arriving_after_its_reading_is_still_noticed(self) -> None:
        for service_data_hex in (
            "400298082f233e01000000568403",
            "403e010000005408013f500028006e00",
            # Conductivity up by 320, then nitrogen up by 18 once the
            # packet 2 joins the reading.
            "400298082f233e0200000056c404",
        ):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(self.store.care_log(sensor_id=SENSOR_ID), [])

        self.ingestion.ingest(advertisement("403e020000005408013f620028006e00"))

        events = self.store.care_log(sensor_id=SENSOR_ID)
        self.assertEqual([event["kind"] for event in events], ["fertilizing"])
        self.assertEqual(events[0]["changes"]["nitrogen_mg_kg"], 18.0)

    def test_a_report_is_acknowledgeable_only_once_complete_and_owned(self) -> None:
        self.ingestion.ingest(advertisement(PACKET2_1235))
        self.store.manage_sensor(SENSOR_ID, "Fern", "Office", "monstera", None, None, 60)
        self.assertFalse(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))
        self.ingestion.ingest(advertisement(PACKET1_1235))
        self.assertTrue(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))
        self.assertFalse(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1234))
        self.assertFalse(
            self.ingestion.report_is_acknowledgeable("sensor-001122334455", 1235)
        )

        # Packet 1 alone is a reading, but not a complete report.
        self.ingestion.ingest(advertisement(PACKET1_1236))
        self.assertFalse(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1236))
        self.ingestion.ingest(advertisement(PACKET2_1236))
        self.assertTrue(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1236))

    def test_an_unclaimed_sensor_cannot_be_acknowledged(self) -> None:
        # The acknowledgement travels over the bonded link, which only a sensor
        # this hub owns has.
        for service_data_hex in (PACKET1_1235, PACKET2_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(self.store.sensor(SENSOR_ID)["enrollment_status"], "unclaimed")
        self.assertFalse(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))

    def test_a_report_in_conflict_is_never_acknowledgeable(self) -> None:
        self.ingestion.ingest(advertisement("40"))
        self.store.manage_sensor(SENSOR_ID, "Fern", "Office", "monstera", None, None, 60)
        self.ingestion.ingest(advertisement(PACKET1_1235))
        # Packet 2 repeats identically, but the packet 1 the
        # sensor now holds under this ID is not the one stored.
        self.assertEqual(
            self.ingestion.ingest(advertisement("402e2d3ed3040000451001")), "conflict"
        )
        self.ingestion.ingest(advertisement(PACKET2_1235))
        self.assertFalse(self.ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))

class MigrationTests(unittest.TestCase):
    def test_migrates_version_1_rows_to_current_nullable_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                database.executescript(MIGRATIONS[1])
                database.execute("PRAGMA user_version=1")
                database.execute(
                    """
                    INSERT INTO sensor_readings (
                        sensor_id, sequence, observed_at, received_at,
                        soil_temperature_c, moisture_percent, conductivity_us_cm
                    ) VALUES ('legacy-1', 9, '2026-01-01T00:00:00Z',
                              '2026-01-01T00:00:01Z', 20.0, 40.0, 900)
                    """
                )

            store = ReadingStore(database_path=database_path)
            store.close()
            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(database.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], 1)
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0],
                    1,
                )
                columns = {
                    row[1]: row[3]
                    for row in database.execute("PRAGMA table_info(sensor_readings)")
                }
                self.assertEqual(columns["moisture_percent"], 0)
                self.assertIn("advertisement_id", columns)
                advertisement_columns = {
                    row[1] for row in database.execute("PRAGMA table_info(advertisements)")
                }
                self.assertIn("service_data", advertisement_columns)

    def test_migrates_minute_intervals_to_seconds_and_requires_protocol_2_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                for version in range(1, 8):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=7")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, first_seen_at, last_seen_at, transport,
                        contract_version, expected_interval_minutes,
                        device_config_revision, device_config_applied_revision
                    ) VALUES
                        ('sensor-confirmed', 'legacy', 'sensor-confirmed', 'enrolled',
                         'Confirmed', '2026-09-13T12:00:00Z', '2026-09-13T12:00:00Z',
                         'direct', 1, 60, 3, 3),
                        ('sensor-pending', 'legacy', 'sensor-pending', 'enrolled',
                         'Pending', '2026-09-13T12:00:00Z', '2026-09-13T12:00:00Z',
                         'direct', 1, 30, 4, 0)
                    """
                )

            store = ReadingStore(database_path=database_path)
            try:
                self.assertEqual(
                    store.sensor("sensor-confirmed")["expected_interval_seconds"],
                    3600,
                )
                self.assertEqual(
                    store.sensor("sensor-confirmed")["sensor_reporting_interval_seconds"],
                    3600,
                )
                self.assertEqual(store.sensor("sensor-confirmed")["device_config_status"], "pending")
                self.assertIsNone(
                    store.sensor("sensor-pending")["sensor_reporting_interval_seconds"]
                )
            finally:
                store.close()

    def test_migrates_opp_identity_without_changing_other_sensors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                for version in range(1, 5):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=4")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        first_seen_at, last_seen_at, transport, contract_version
                    ) VALUES ('opp:aabbccddeeff', 'device-local-name',
                              'opp:aabbccddeeff', 'unclaimed',
                              '2026-09-13T12:00:00Z', '2026-09-13T12:00:00Z',
                              'bthome', 2),
                             ('plant-01', 'legacy', 'plant-01',
                              'enrolled', '2026-09-13T12:00:00Z',
                              '2026-09-13T12:00:00Z', 'direct', 1)
                    """
                )
                database.execute(
                    """
                    INSERT INTO advertisements (
                        sensor_id, packet_id, received_at, transport, source_adapter,
                        observed_identifier, payload_sha256, decode_status
                    ) VALUES ('opp:aabbccddeeff', 1, '2026-09-13T12:00:00Z',
                              'bthome', 'test', 'test-device', 'hash', 'accepted')
                    """
                )
                advertisement_id = database.execute(
                    "SELECT advertisement_id FROM advertisements"
                ).fetchone()[0]
                database.execute(
                    """
                    INSERT INTO sensor_readings (
                        advertisement_id, sensor_id, sequence, observed_at, received_at,
                        air_temperature_c, air_humidity_percent, soil_source_status,
                        air_source_status, contract_version
                    ) VALUES (?, 'opp:aabbccddeeff', 1, '2026-09-13T12:00:00Z',
                              '2026-09-13T12:00:00Z', 24.2, 53.6, 'unavailable',
                              'available', 2)
                    """,
                    (advertisement_id,),
                )
                database.execute(
                    """
                    UPDATE sensors
                    SET replaced_by_sensor_id = 'opp:aabbccddeeff'
                    WHERE sensor_id = 'plant-01'
                    """
                )
                database.execute(
                    """
                    INSERT INTO care_events (
                        event_id, sensor_id, kind, detected_at, title, summary,
                        confidence, changes_json
                    ) VALUES ('event-1', 'opp:aabbccddeeff', 'watering',
                              '2026-09-13T12:00:00Z', 'Watered', 'Test', 'high', '{}')
                    """
                )

            store = ReadingStore(database_path=database_path)
            store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(
                    database.execute("SELECT sensor_id FROM sensors ORDER BY sensor_id").fetchall(),
                    [("plant-01",), ("sensor-aabbccddeeff",)],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT identity_value FROM sensors WHERE sensor_id = ?",
                        ("sensor-aabbccddeeff",),
                    ).fetchone()[0],
                    "sensor-aabbccddeeff",
                )
                self.assertEqual(
                    database.execute(
                        "SELECT replaced_by_sensor_id FROM sensors WHERE sensor_id = ?",
                        ("plant-01",),
                    ).fetchone()[0],
                    "sensor-aabbccddeeff",
                )
                for table in ("advertisements", "sensor_readings", "care_events"):
                    self.assertEqual(
                        database.execute(f"SELECT sensor_id FROM {table}").fetchone()[0],
                        "sensor-aabbccddeeff",
                    )


    def test_migrates_version_16_readings_to_contract_v3_without_losing_any(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA foreign_keys=ON")
                for version in range(1, 17):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=16")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, first_seen_at, last_seen_at, transport,
                        contract_version
                    ) VALUES (?, 'device-local-name', ?, 'enrolled', 'Fern',
                              '2026-09-13T12:00:00Z', '2026-09-13T12:30:00Z',
                              'bthome', 2)
                    """,
                    (SENSOR_ID, SENSOR_ID),
                )
                database.executemany(
                    """
                    INSERT INTO advertisements (
                        advertisement_id, sensor_id, packet_id, received_at, transport,
                        source_adapter, observed_identifier, rssi, contract_version,
                        payload_sha256, decode_status, decode_error, service_data
                    ) VALUES (?, ?, ?, ?, 'bthome', 'bleak', 'platform-a', -50, 2,
                              'hash', ?, ?, ?)
                    """,
                    [
                        (1, SENSOR_ID, 42, "2026-09-13T12:00:00Z", "accepted", None,
                         bytes.fromhex("40002a022e0903f014148a0c45f20056d204")),
                        (2, SENSOR_ID, 42, "2026-09-13T12:00:02Z", "duplicate", None,
                         bytes.fromhex("40002a022e0903f014148a0c45f20056d204")),
                        (3, SENSOR_ID, None, "2026-09-13T12:10:00Z", "rejected",
                         "truncated", bytes.fromhex("400009")),
                        (4, SENSOR_ID, 43, "2026-09-13T12:30:00Z", "accepted", None,
                         bytes.fromhex("40002b03d71145f500")),
                    ],
                )
                database.executemany(
                    """
                    INSERT INTO sensor_readings (
                        reading_id, advertisement_id, sensor_id, sequence, observed_at,
                        received_at, soil_temperature_c, moisture_percent,
                        conductivity_us_cm, air_temperature_c, air_humidity_percent,
                        soil_ph, nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg,
                        soil_source_status, air_source_status, contract_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)
                    """,
                    [
                        (1, 1, SENSOR_ID, 42, "2026-09-13T12:00:00Z",
                         "2026-09-13T12:00:00Z", 23.5, 32.1, 1234, 24.2, 53.6,
                         6.4, 86, 41, 124, "available", "available"),
                        (2, 4, SENSOR_ID, 43, "2026-09-13T12:30:00Z",
                         "2026-09-13T12:30:00Z", None, None, None, 24.5, 45.67,
                         None, None, None, None, "unavailable", "available"),
                    ],
                )
                database.execute(
                    """
                    INSERT INTO care_events (
                        event_id, sensor_id, kind, detected_at, title, summary,
                        confidence, changes_json
                    ) VALUES ('event-1', ?, 'watering', '2026-09-13T12:00:00Z',
                              'Watered', 'Test', 'high', '{}')
                    """,
                    (SENSOR_ID,),
                )

            store = ReadingStore(database_path=database_path)
            try:
                history = store.history(SENSOR_ID)
                self.assertEqual(len(history), 2)
                old = history[0]["reading"]
                self.assertIsNone(old["report_id"])
                self.assertEqual(old["observed_at"], "2026-09-13T12:00:00Z")
                self.assertEqual(old["moisture_percent"], 32.1)
                self.assertEqual(old["soil_ph"], 6.4)
                self.assertIsNone(old["battery_percent"])
                self.assertEqual(len(store.care_log(sensor_id=SENSOR_ID)), 1)
                reports = store.raw_sensor_reports(SENSOR_ID)
                self.assertEqual([item["advertisement_id"] for item in reports], [4, 3, 2, 1])
                self.assertEqual([item["report_id"] for item in reports], [None] * 4)
                self.assertEqual(reports[1]["decode_error"], "truncated")

                # The first contract-v3 report of the same sensor reuses no key:
                # old readings have no report ID to collide with.
                ingestion = AdvertisementIngestionService(store)
                self.assertEqual(
                    ingestion.ingest(
                        advertisement("402e2c3e2a000000451001", "2026-09-26T21:20:05Z")
                    ),
                    "accepted",
                )
                self.assertEqual(
                    ingestion.ingest(
                        advertisement("402e2d3e2a000000451001", "2026-09-26T21:20:06Z")
                    ),
                    "conflict",
                )
                self.assertEqual(len(store.history(SENSOR_ID)), 3)
                self.assertEqual(
                    ingestion.ingest(advertisement("403e2a000000", "2026-09-26T21:20:07Z")),
                    "accepted",
                )
                self.assertEqual(store.latest(SENSOR_ID)["reading"]["report_id"], 42)
            finally:
                store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    database.execute(
                        "SELECT reading_id, advertisement_id, report_id, legacy_packet_id, "
                        "observed_at, received_at FROM sensor_readings ORDER BY reading_id"
                    ).fetchall()[:2],
                    [
                        (1, 1, None, 42, "2026-09-13T12:00:00Z", "2026-09-13T12:00:00Z"),
                        (2, 4, None, 43, "2026-09-13T12:30:00Z", "2026-09-13T12:30:00Z"),
                    ],
                )
                reading_columns = {
                    row[1]: row[3] for row in database.execute("PRAGMA table_info(sensor_readings)")
                }
                self.assertEqual(reading_columns["observed_at"], 0)
                self.assertNotIn("sequence", reading_columns)
                for column in (
                    "report_id", "battery_percent", "battery_voltage_v", "soil_ph",
                    "nitrogen_mg_kg", "phosphorus_mg_kg", "potassium_mg_kg",
                ):
                    self.assertIn(column, reading_columns)
                advertisement_columns = {
                    row[1] for row in database.execute("PRAGMA table_info(advertisements)")
                }
                self.assertIn("report_id", advertisement_columns)
                self.assertIn("packet_kind", advertisement_columns)
                self.assertNotIn("packet_id", advertisement_columns)
                self.assertEqual(
                    database.execute(
                        "SELECT legacy_packet_id FROM advertisements "
                        "WHERE advertisement_id <= 4 ORDER BY advertisement_id"
                    ).fetchall(),
                    [(42,), (42,), (None,), (43,)],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT \"table\" FROM pragma_foreign_key_list('sensor_readings') "
                        "WHERE \"from\" = 'advertisement_id'"
                    ).fetchone()[0],
                    "advertisements",
                )
                self.assertEqual(
                    database.execute(
                        "SELECT name FROM sqlite_master WHERE name LIKE '%v17%'"
                    ).fetchall(),
                    [],
                )
                self.assertEqual(
                    {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index' "
                            "AND name NOT LIKE 'sqlite_autoindex%'"
                        )
                    }
                    >= {
                        "advertisements_by_sensor_time",
                        "advertisements_by_status_time",
                        "sensor_readings_by_sensor_time",
                        "sensor_readings_by_report",
                    },
                    True,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute(
                        "INSERT INTO sensor_readings (sensor_id, report_id, received_at) "
                        "VALUES (?, 42, '2026-09-26T21:21:00Z')",
                        (SENSOR_ID,),
                    )
                database.execute(
                    "INSERT INTO sensor_readings (sensor_id, report_id, received_at) "
                    "VALUES (?, NULL, '2026-09-26T21:21:00Z')",
                    (SENSOR_ID,),
                )


    def test_migrates_version_17_packet2_and_readings_to_durable_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA foreign_keys=ON")
                for version in range(1, 18):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=17")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, first_seen_at, last_seen_at, transport,
                        contract_version
                    ) VALUES (?, 'device-local-name', ?, 'enrolled', 'Fern',
                              '2026-09-26T21:00:00Z', '2026-09-26T21:20:00Z',
                              'bthome', 3)
                    """,
                    (SENSOR_ID, SENSOR_ID),
                )
                # Before version 20 packet 1 was logged as 'main' and packet 2 was
                # kept in report_supplements.
                database.execute(
                    """
                    INSERT INTO advertisements (
                        advertisement_id, sensor_id, report_id, packet_kind, received_at,
                        transport, source_adapter, observed_identifier, rssi,
                        contract_version, payload_sha256, decode_status, service_data
                    ) VALUES (1, ?, 1235, 'main', '2026-09-26T21:20:05Z', 'bthome',
                              'bleak', 'platform-a', -48, 3, 'hash', 'accepted', ?)
                    """,
                    (SENSOR_ID, bytes.fromhex(PACKET1_1235)),
                )
                database.execute(
                    """
                    INSERT INTO sensor_readings (
                        reading_id, advertisement_id, sensor_id, report_id, received_at,
                        air_temperature_c, air_humidity_percent, battery_percent,
                        battery_voltage_v, soil_source_status, air_source_status,
                        contract_version
                    ) VALUES (1, 1, ?, 1235, '2026-09-26T21:20:05Z', 27.2, 44.0,
                              96, 4.16, 'unavailable', 'available', 3)
                    """,
                    (SENSOR_ID,),
                )
                database.executemany(
                    """
                    INSERT INTO report_supplements (
                        sensor_id, report_id, received_at, battery_percent,
                        battery_voltage_v, soil_ph, nitrogen_mg_kg, phosphorus_mg_kg,
                        potassium_mg_kg, force_report
                    ) VALUES (?, ?, '2026-09-26T21:20:06Z', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (SENSOR_ID, 1235, 96, 4.16, 6.8, 1, 2, 7, 1),
                        # Held before its packet 1 arrived.
                        (SENSOR_ID, 1236, None, None, None, None, None, None, 0),
                    ],
                )

            store = ReadingStore(database_path=database_path)
            try:
                reading = store.latest(SENSOR_ID)["reading"]
                self.assertEqual(reading["report_id"], 1235)
                self.assertEqual(reading["battery_percent"], 96)
                ingestion = AdvertisementIngestionService(store)
                self.assertTrue(ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))
                # A packet 2 kept from before still judges repeats and joins
                # the packet 1 that completes its report.
                self.assertEqual(
                    ingestion.ingest(advertisement(PACKET2_1236)), "duplicate"
                )
                self.assertEqual(ingestion.ingest(advertisement(PACKET1_1236)), "accepted")
                self.assertTrue(ingestion.report_is_acknowledgeable(SENSOR_ID, 1236))
                store.mark_reports_acknowledged(SENSOR_ID, [1236])
            finally:
                store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                packet2_columns = [
                    row[1] for row in database.execute("PRAGMA table_info(report_packet2)")
                ]
                self.assertNotIn("force_report", packet2_columns)
                self.assertEqual(
                    database.execute(
                        "SELECT sensor_id, report_id, received_at, battery_percent, "
                        "battery_voltage_v, soil_ph, nitrogen_mg_kg, phosphorus_mg_kg, "
                        "potassium_mg_kg FROM report_packet2 ORDER BY report_id"
                    ).fetchall(),
                    [
                        (SENSOR_ID, 1235, "2026-09-26T21:20:06Z", 96, 4.16, 6.8, 1, 2, 7),
                        (SENSOR_ID, 1236, "2026-09-26T21:20:06Z",
                         None, None, None, None, None, None),
                    ],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT report_id, acknowledged_at IS NOT NULL "
                        "FROM sensor_readings ORDER BY report_id"
                    ).fetchall(),
                    [(1235, 0), (1236, 1)],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT name FROM sqlite_master WHERE name LIKE '%v18%'"
                    ).fetchall(),
                    [],
                )
                self.assertIsNotNone(
                    database.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'advertisements_conflicts_by_report'"
                    ).fetchone()
                )

    def test_migrates_version_18_battery_to_an_unknown_charging_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA foreign_keys=ON")
                for version in range(1, 19):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=18")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, first_seen_at, last_seen_at, transport,
                        contract_version
                    ) VALUES (?, 'device-local-name', ?, 'enrolled', 'Fern',
                              '2026-09-26T21:00:00Z', '2026-09-26T21:20:00Z',
                              'bthome', 3)
                    """,
                    (SENSOR_ID, SENSOR_ID),
                )
                # Before version 20 packet 1 was logged as 'main' and packet 2 was
                # kept in report_supplements.
                database.execute(
                    """
                    INSERT INTO advertisements (
                        advertisement_id, sensor_id, report_id, packet_kind, received_at,
                        transport, source_adapter, observed_identifier, rssi,
                        contract_version, payload_sha256, decode_status, service_data
                    ) VALUES (1, ?, 1235, 'main', '2026-09-26T21:20:05Z', 'bthome',
                              'bleak', 'platform-a', -48, 3, 'hash', 'accepted', ?)
                    """,
                    (SENSOR_ID, bytes.fromhex(PACKET1_1235)),
                )
                database.executemany(
                    """
                    INSERT INTO sensor_readings (
                        reading_id, advertisement_id, sensor_id, report_id, received_at,
                        air_temperature_c, air_humidity_percent, battery_percent,
                        battery_voltage_v, soil_ph, nitrogen_mg_kg, phosphorus_mg_kg,
                        potassium_mg_kg, soil_source_status, air_source_status,
                        contract_version, acknowledged_at
                    ) VALUES (?, ?, ?, ?, '2026-09-26T21:20:05Z', 27.2, 44.0, ?, ?,
                              ?, ?, ?, ?, 'unavailable', 'available', ?, ?)
                    """,
                    [
                        (1, 1, SENSOR_ID, 1235, 96, 4.16, 6.8, 1, 2, 7, 3,
                         "2026-09-26T21:20:30Z"),
                        # From before contract v3: no report, no battery.
                        (2, None, SENSOR_ID, None, None, None, None, None, None, None, 0,
                         None),
                    ],
                )
                database.executemany(
                    """
                    INSERT INTO report_supplements (
                        sensor_id, report_id, received_at, battery_percent,
                        battery_voltage_v, soil_ph, nitrogen_mg_kg, phosphorus_mg_kg,
                        potassium_mg_kg
                    ) VALUES (?, ?, '2026-09-26T21:20:06Z', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (SENSOR_ID, 1235, 96, 4.16, 6.8, 1, 2, 7),
                        # Held before its packet 1 arrived.
                        (SENSOR_ID, 1236, None, None, None, None, None, None),
                    ],
                )
                readings_before = database.execute(
                    "SELECT * FROM sensor_readings ORDER BY reading_id"
                ).fetchall()
                packet2_before = database.execute(
                    "SELECT * FROM report_supplements ORDER BY report_id"
                ).fetchall()

            store = ReadingStore(database_path=database_path)
            try:
                # What the sensor never said stays unknown, not "not charging".
                reading = store.latest(SENSOR_ID)["reading"]
                self.assertEqual(
                    (reading["report_id"], reading["battery_percent"],
                     reading["battery_voltage_v"], reading["battery_charging"]),
                    (1235, 96, 4.16, None),
                )
                self.assertEqual(
                    [item["reading"]["battery_charging"] for item in store.history(SENSOR_ID)],
                    [None, None],
                )
                ingestion = AdvertisementIngestionService(store)
                self.assertTrue(ingestion.report_is_acknowledgeable(SENSOR_ID, 1235))
                # A packet 2 kept from before still judges repeats and joins.
                self.assertEqual(
                    ingestion.ingest(advertisement(PACKET2_1236)), "duplicate"
                )
                self.assertEqual(ingestion.ingest(advertisement(PACKET1_1236)), "accepted")
                self.assertIsNone(store.latest(SENSOR_ID)["reading"]["battery_charging"])
                # New reports carry it.
                for service_data_hex in (packet1_hex(1237), "40015b0c141016013ed5040000"):
                    self.assertEqual(ingestion.ingest(advertisement(service_data_hex)), "accepted")
                self.assertIs(store.latest(SENSOR_ID)["reading"]["battery_charging"], True)
            finally:
                store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                # Every row that was there is still there, unchanged, with the
                # new column appended as null.
                self.assertEqual(
                    database.execute(
                        "SELECT * FROM sensor_readings WHERE reading_id <= 2 "
                        "ORDER BY reading_id"
                    ).fetchall(),
                    [row + (None,) for row in readings_before],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT * FROM report_packet2 WHERE report_id <= 1236 "
                        "ORDER BY report_id"
                    ).fetchall(),
                    [row + (None,) for row in packet2_before],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT report_id, battery_charging FROM report_packet2 "
                        "WHERE report_id = 1237"
                    ).fetchall(),
                    [(1237, 1)],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute(
                        "UPDATE sensor_readings SET battery_charging = 2 WHERE reading_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute(
                        "UPDATE report_packet2 SET battery_charging = 2 "
                        "WHERE report_id = 1235"
                    )

    def test_migrates_version_19_packet_names_to_packet1_and_packet2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA foreign_keys=ON")
                for version in range(1, 20):
                    database.executescript(MIGRATIONS[version])
                database.execute("PRAGMA user_version=19")
                database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, first_seen_at, last_seen_at, transport,
                        contract_version
                    ) VALUES (?, 'device-local-name', ?, 'enrolled', 'Fern',
                              '2026-09-26T21:00:00Z', '2026-09-26T21:20:00Z',
                              'bthome', 3)
                    """,
                    (SENSOR_ID, SENSOR_ID),
                )
                # Version 19 names: 'main', 'supplementary' and report_supplements.
                database.executemany(
                    """
                    INSERT INTO advertisements (
                        advertisement_id, sensor_id, report_id, packet_kind,
                        legacy_packet_id, received_at, transport, source_adapter,
                        observed_identifier, rssi, contract_version, payload_sha256,
                        decode_status, decode_error, service_data
                    ) VALUES (?, ?, ?, ?, ?, ?, 'bthome', 'bleak', 'platform-a', -48,
                              ?, 'hash', ?, ?, ?)
                    """,
                    [
                        (1, SENSOR_ID, 1235, "main", None, "2026-09-26T21:20:05Z", 3,
                         "accepted", None, bytes.fromhex(PACKET1_1235)),
                        (2, SENSOR_ID, 1235, "supplementary", None, "2026-09-26T21:20:06Z",
                         3, "accepted", None, bytes.fromhex(PACKET2_1235)),
                        (3, SENSOR_ID, None, "beacon", None, "2026-09-26T21:20:07Z", 3,
                         "accepted", None, bytes.fromhex("40")),
                        (4, SENSOR_ID, 1235, "main", None, "2026-09-26T21:20:08Z", 3,
                         "duplicate", None, bytes.fromhex(PACKET1_1235)),
                        (5, SENSOR_ID, 1235, "supplementary", None, "2026-09-26T21:20:09Z",
                         3, "conflict", None,
                         bytes.fromhex("40015f0c401016003ed304000054080144010002000700")),
                        (6, SENSOR_ID, None, None, None, "2026-09-26T21:20:10Z", None,
                         "rejected", "truncated", bytes.fromhex("400009")),
                        (7, SENSOR_ID, None, None, 42, "2026-09-13T12:00:00Z", 2,
                         "accepted", None, bytes.fromhex("40002a022e0903f014148a0c45f20056d204")),
                    ],
                )
                database.executemany(
                    """
                    INSERT INTO sensor_readings (
                        reading_id, advertisement_id, sensor_id, report_id,
                        legacy_packet_id, observed_at, received_at, air_temperature_c,
                        air_humidity_percent, battery_percent, battery_voltage_v, soil_ph,
                        nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg,
                        soil_source_status, air_source_status, contract_version,
                        acknowledged_at, battery_charging
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 27.2, 44.0, ?, ?, ?, ?, ?, ?,
                              ?, 'available', ?, ?, ?)
                    """,
                    [
                        (1, 1, SENSOR_ID, 1235, None, None, "2026-09-26T21:20:05Z",
                         96, 4.16, 6.8, 1, 2, 7, "unavailable", 3,
                         "2026-09-26T21:20:30Z", 0),
                        (2, 7, SENSOR_ID, None, 42, "2026-09-13T12:00:00Z",
                         "2026-09-13T12:00:00Z", None, None, None, None, None, None,
                         "available", 2, None, None),
                        # Stored directly, never from a packet.
                        (3, None, SENSOR_ID, None, None, "2026-09-12T12:00:00Z",
                         "2026-09-12T12:00:00Z", None, None, None, None, None, None,
                         "available", 0, None, None),
                    ],
                )
                database.executemany(
                    """
                    INSERT INTO report_supplements (
                        sensor_id, report_id, received_at, battery_percent,
                        battery_voltage_v, soil_ph, nitrogen_mg_kg, phosphorus_mg_kg,
                        potassium_mg_kg, battery_charging
                    ) VALUES (?, ?, '2026-09-26T21:20:06Z', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (SENSOR_ID, 1235, 96, 4.16, 6.8, 1, 2, 7, 0),
                        # Held before its packet 1 arrived.
                        (SENSOR_ID, 1236, None, None, None, None, None, None, None),
                    ],
                )
                advertisements_before = database.execute(
                    "SELECT * FROM advertisements ORDER BY advertisement_id"
                ).fetchall()
                readings_before = database.execute(
                    "SELECT * FROM sensor_readings ORDER BY reading_id"
                ).fetchall()
                packet2_before = database.execute(
                    "SELECT * FROM report_supplements ORDER BY report_id"
                ).fetchall()

            store = ReadingStore(database_path=database_path)
            try:
                self.assertEqual(
                    [item["packet_kind"] for item in store.raw_sensor_reports(SENSOR_ID)],
                    [None, None, "packet2", "packet1", "beacon", "packet2", "packet1"],
                )
                self.assertEqual(store.latest(SENSOR_ID)["reading"]["report_id"], 1235)
                # The held packet 2 still judges repeats and joins its packet 1.
                ingestion = AdvertisementIngestionService(store)
                self.assertEqual(ingestion.ingest(advertisement(PACKET2_1236)), "duplicate")
                self.assertEqual(ingestion.ingest(advertisement(PACKET1_1236)), "accepted")
                self.assertTrue(ingestion.report_is_acknowledgeable(SENSOR_ID, 1236))
            finally:
                store.close()

            renamed = {"main": "packet1", "supplementary": "packet2"}
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertGreaterEqual(DATABASE_SCHEMA_VERSION, 20)
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                # Every row is still there with its ID, only the packet kind renamed.
                self.assertEqual(
                    database.execute(
                        "SELECT * FROM advertisements WHERE advertisement_id <= 7 "
                        "ORDER BY advertisement_id"
                    ).fetchall(),
                    [
                        row[:3] + (renamed.get(row[3], row[3]),) + row[4:]
                        for row in advertisements_before
                    ],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT packet_kind, COUNT(*) FROM advertisements "
                        "WHERE advertisement_id <= 7 GROUP BY packet_kind ORDER BY packet_kind"
                    ).fetchall(),
                    [(None, 2), ("beacon", 1), ("packet1", 2), ("packet2", 2)],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT * FROM sensor_readings WHERE reading_id <= 3 "
                        "ORDER BY reading_id"
                    ).fetchall(),
                    readings_before,
                )
                self.assertEqual(
                    database.execute(
                        "SELECT * FROM report_packet2 ORDER BY report_id"
                    ).fetchall(),
                    packet2_before,
                )
                self.assertEqual(
                    database.execute(
                        "SELECT name FROM sqlite_master WHERE name LIKE '%v20%' "
                        "OR name LIKE '%supplement%'"
                    ).fetchall(),
                    [],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT \"table\" FROM pragma_foreign_key_list('sensor_readings') "
                        "WHERE \"from\" = 'advertisement_id'"
                    ).fetchone()[0],
                    "advertisements",
                )
                self.assertLessEqual(
                    {
                        "advertisements_by_sensor_time",
                        "advertisements_by_status_time",
                        "advertisements_conflicts_by_report",
                        "sensor_readings_by_sensor_time",
                        "sensor_readings_by_report",
                    },
                    {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        )
                    },
                )
                # The old names are refused from now on.
                for kind in ("main", "supplementary"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        database.execute(
                            "UPDATE advertisements SET packet_kind = ? "
                            "WHERE advertisement_id = 1",
                            (kind,),
                        )
                # A reading still holds on to the packet 1 it came from.
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute("DELETE FROM advertisements WHERE advertisement_id = 1")


class DetectionCallbackTests(unittest.TestCase):
    """Packets that arrive before the sensor's name must not be lost."""

    PACKET1 = bytes.fromhex("40022e092e2c2f643ed2040000451001500037b86a562900")
    PACKET2 = bytes.fromhex("4001600c401016003ed204000054080144010002000700")

    def _callback(self):
        queue = asyncio.Queue()
        return queue, _detection_callback(queue, {}, {})

    @staticmethod
    def _data(service_data=None, local_name=None):
        return SimpleNamespace(
            service_data={} if service_data is None else {BTHOME_SERVICE_UUID: service_data},
            local_name=local_name,
            rssi=-40,
        )

    @staticmethod
    def _drain(queue):
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    def test_both_packets_of_a_report_survive_waiting_for_the_name(self):
        queue, detected = self._callback()
        device = SimpleNamespace(address="ABCD")
        detected(device, self._data(self.PACKET1))
        detected(device, self._data(self.PACKET2))
        self.assertTrue(queue.empty())

        detected(device, self._data(local_name="sensor-aabbccddeeff"))

        items = self._drain(queue)
        self.assertEqual([item.service_data for item in items], [self.PACKET1, self.PACKET2])
        self.assertTrue(all(item.local_name == "sensor-aabbccddeeff" for item in items))
        self.assertTrue(all(item.connection_target is device for item in items))

    def test_a_name_arriving_with_data_releases_what_was_held_first(self):
        queue, detected = self._callback()
        device = SimpleNamespace(address="ABCD")
        detected(device, self._data(self.PACKET1))
        detected(device, self._data(self.PACKET2, local_name="sensor-aabbccddeeff"))
        self.assertEqual(
            [item.service_data for item in self._drain(queue)],
            [self.PACKET1, self.PACKET2],
        )

    def test_known_names_are_applied_without_waiting(self):
        queue, detected = self._callback()
        device = SimpleNamespace(address="ABCD")
        detected(device, self._data(local_name="sensor-aabbccddeeff"))
        detected(device, self._data(self.PACKET1))
        items = self._drain(queue)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].local_name, "sensor-aabbccddeeff")
        self.assertIs(items[0].connection_target, device)


class BleakSubscriberTests(unittest.TestCase):
    def test_recovers_after_scanner_failure_and_isolates_three_sensors(self) -> None:
        events = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["events"]
        attempts = []

        class FakeScanner:
            def __init__(self, callback, **kwargs):
                if len(inspect.signature(callback).parameters) != 2:
                    raise TypeError("callback must be callable with 2 parameters")
                self.callback = callback
                self.kwargs = kwargs

            async def __aenter__(self):
                attempts.append(self.kwargs)
                if len(attempts) == 1:
                    raise RuntimeError("adapter unavailable")
                for event in events:
                    data = SimpleNamespace(
                        local_name=event["local_name"],
                        rssi=event["rssi"],
                        service_data={
                            BTHOME_SERVICE_UUID: bytes.fromhex(event["service_data_hex"])
                        },
                    )
                    self.callback(SimpleNamespace(address=event["observed_identifier"]), data)
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        store = ReadingStore()
        subscriber = BleakSubscriber(
            AdvertisementIngestionService(store),
            scanner_factory=FakeScanner,
            initial_backoff=0.01,
            maximum_backoff=0.02,
        )
        subscriber.start()
        try:
            self.assertTrue(store.wait_for_reading(1.0))
            deadline = time.monotonic() + 1.0
            while len(store.history()) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(attempts), 2)
            self.assertEqual(attempts[-1], {})
            self.assertEqual(len(store.history()), 4)
            self.assertEqual(
                {item["reading"]["sensor_id"] for item in store.history()},
                {
                    "sensor-aabbccddeeff",
                    "sensor-001122334455",
                    "sensor-102030405060",
                },
            )
            self.assertEqual(subscriber.health()["status"], "scanning")
            deadline = time.monotonic() + 1.0
            while (
                subscriber.health()["last_receive_at"] is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertIsNotNone(subscriber.health()["last_receive_at"])
        finally:
            subscriber.close()
            store.close()


class FakeQueueSensor:
    """A sensor's side of the characteristic: its queue, drain mode and config.

    Pages hold up to eight of the oldest queued reports; a cumulative
    acknowledgement beyond what has been paged, or of zero, is refused.
    """

    def __init__(self, reports=()):
        self.queue = list(reports)
        self.connections = 0
        self.writes = []
        self.draining = False
        self.last_paged = 0
        self.configuration = b""
        self.ignore_acknowledgements = False

    def add(self, report_id, humidity=0x2C):
        self.queue.append(
            (report_id, bytes.fromhex(packet1_hex(report_id, humidity)),
             bytes.fromhex(packet2_hex(report_id)))
        )

    def page(self):
        records = self.queue[:8]
        if records:
            self.last_paged = max(self.last_paged, records[-1][0])
        return bytes((0x20, len(records))) + b"".join(
            report_id.to_bytes(4, "little")
            + bytes((len(packet1),)) + packet1
            + bytes((len(packet2),)) + packet2
            for report_id, packet1, packet2 in records
        )

    def factory(self, fail=False):
        sensor = self

        class Client:
            def __init__(self, target, timeout=None):
                self.target = target

            async def __aenter__(self):
                if fail:
                    raise RuntimeError("connection failed")
                sensor.connections += 1
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                sensor.draining = False
                return False

            async def write_gatt_char(self, uuid, payload, response):
                payload = bytes(payload)
                sensor.writes.append(payload)
                if payload == b"\x20":
                    sensor.draining = True
                elif payload == b"\x22":
                    sensor.draining = False
                elif payload[0] == 0x21 and len(payload) == 5:
                    report_id = int.from_bytes(payload[1:], "little")
                    if report_id == 0 or report_id > sensor.last_paged:
                        raise RuntimeError("acknowledgement refused")
                    if not sensor.ignore_acknowledgements:
                        sensor.queue = [r for r in sensor.queue if r[0] > report_id]
                else:
                    sensor.configuration = payload

            async def read_gatt_char(self, uuid):
                return sensor.page() if sensor.draining else sensor.configuration

        return Client


class DrainSubscriberTests(unittest.IsolatedAsyncioTestCase):
    """When the subscriber drains a sensor's queue."""

    def setUp(self) -> None:
        self.store = ReadingStore()
        self.events = []
        self.clock = [100.0]
        self.outcomes = []
        events, outcomes, store = self.events, self.outcomes, self.store

        class FakeSynchronizer:
            def owns(self, sensor_id):
                return DeviceConfigurationSynchronizer(store).owns(sensor_id)

            def report_delivery(self, sensor_id, report_id):
                return store.report_delivery(sensor_id, report_id)

            def pending_revision(self, sensor_id):
                desired = store.pending_device_configuration(sensor_id)
                return None if desired is None else desired["revision"]

            def has_release_pending(self, sensor_id):
                return store.release_is_pending(sensor_id)

            async def release(self, sensor_id, target):
                events.append(("release", sensor_id))
                store.mark_released(sensor_id)
                return "released"

            async def synchronize(self, sensor_id, target):
                events.append(("synchronize", sensor_id))
                return "applied"

            async def drain(self, sensor_id, observed_identifier, target, ingest, **work):
                events.append(("drain", sensor_id, observed_identifier, target, work))
                return DrainResult(outcomes.pop(0) if outcomes else "drained")

        self.subscriber = BleakSubscriber(
            AdvertisementIngestionService(self.store),
            configuration_synchronizer=FakeSynchronizer(),
        )

    def tearDown(self) -> None:
        self.store.close()

    def enrol(self) -> None:
        # Found by its beacon, then adopted, and its configuration delivered.
        AdvertisementIngestionService(self.store).ingest(advertisement("40"))
        self.store.manage_sensor(SENSOR_ID, "Fern", "Office", "monstera", None, None, 60)
        revision = self.store.pending_device_configuration(SENSOR_ID)["revision"]
        self.store.mark_device_configuration_applied(SENSOR_ID, revision, 60)

    async def hear(self, *service_data_hexes) -> None:
        await hear(self.subscriber, self.clock, service_data_hexes)

    def drains(self):
        return [event for event in self.events if event[0] == "drain"]

    async def test_the_first_report_after_start_is_drained_once_complete(self) -> None:
        self.enrol()
        # The first packet of a report waits for the second, so the connection
        # falls after the fresh report rather than across it.
        await self.hear(packet1_hex(10))
        self.assertEqual(self.drains(), [])
        await self.hear(packet2_hex(10))
        self.assertEqual(
            self.drains(),
            [("drain", SENSOR_ID, "platform-identifier", "connection-target",
              {"configure": False, "station_status": False, "firmware_update": False})],
        )

    async def test_a_fresh_report_with_nothing_missed_waits_for_the_interval(self) -> None:
        self.enrol()
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.store.mark_reports_acknowledged(SENSOR_ID, [10])
        for report_id in (11, 12, 13):
            self.clock[0] += 5.0
            await self.hear(packet1_hex(report_id), packet2_hex(report_id))
        self.assertEqual(len(self.drains()), 1)
        self.clock[0] = 100.0 + ble.DRAIN_INTERVAL_SECONDS
        await self.hear(packet1_hex(14), packet2_hex(14))
        self.assertEqual(len(self.drains()), 2)

    async def test_a_report_missed_over_the_air_is_drained_at_once(self) -> None:
        self.enrol()
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.store.mark_reports_acknowledged(SENSOR_ID, [10])
        self.clock[0] += 5.0
        await self.hear(packet1_hex(11), packet2_hex(11))
        self.assertEqual(len(self.drains()), 1)
        # Report 12 was never heard.
        self.clock[0] += 10.0
        await self.hear(packet1_hex(13), packet2_hex(13))
        self.assertEqual(len(self.drains()), 2)

    async def test_drains_are_spaced_even_when_they_fail(self) -> None:
        self.enrol()
        self.outcomes.extend(["failed", "failed"])
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.assertEqual(len(self.drains()), 1)
        # The report is complete, so its repeats may retry, but not at once.
        self.clock[0] += ble.DRAIN_SPACING_SECONDS - 0.5
        await self.hear(packet1_hex(10))
        self.assertEqual(len(self.drains()), 1)
        self.clock[0] += 0.5
        await self.hear(packet1_hex(10))
        self.assertEqual(len(self.drains()), 2)

    async def test_a_report_never_completed_over_the_air_drains_after_the_limit(self) -> None:
        self.enrol()
        await self.hear(packet1_hex(10))
        self.assertEqual(self.drains(), [])
        self.clock[0] += ble.DRAIN_DEFER_LIMIT_SECONDS - 0.5
        await self.hear(packet1_hex(10))
        self.assertEqual(self.drains(), [])
        self.clock[0] += 0.5
        await self.hear(packet1_hex(10))
        self.assertEqual(len(self.drains()), 1)

    async def test_a_drain_is_never_started_twice_for_one_sensor(self) -> None:
        self.enrol()
        self.subscriber._draining.add(SENSOR_ID)
        await self.hear(packet1_hex(10))
        self.assertEqual(self.drains(), [])

    async def test_a_drain_stopped_by_a_conflict_waits_for_the_interval(self) -> None:
        self.enrol()
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.store.mark_reports_acknowledged(SENSOR_ID, [10])
        self.outcomes.append("stopped")
        self.clock[0] += 5.0
        await self.hear(packet1_hex(13), packet2_hex(13))
        self.assertEqual(len(self.drains()), 2)
        # The gap is still there, but no drain could close it.
        self.clock[0] += 5.0
        await self.hear(packet1_hex(14), packet2_hex(14))
        self.assertEqual(len(self.drains()), 2)
        self.clock[0] += ble.DRAIN_INTERVAL_SECONDS
        await self.hear(packet1_hex(15), packet2_hex(15))
        self.assertEqual(len(self.drains()), 3)

    async def test_beacons_and_acknowledged_reports_never_drain(self) -> None:
        self.enrol()
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.store.mark_reports_acknowledged(SENSOR_ID, [10])
        self.clock[0] += ble.DRAIN_INTERVAL_SECONDS
        await self.hear("40", packet1_hex(10), packet2_hex(10))
        self.assertEqual(len(self.drains()), 1)

    async def test_an_unclaimed_sensor_is_never_drained(self) -> None:
        await self.hear(packet1_hex(10), packet2_hex(10))
        self.clock[0] += ble.DRAIN_INTERVAL_SECONDS
        await self.hear(packet1_hex(11), packet2_hex(11))
        self.assertEqual(self.drains(), [])

    async def test_configuration_rides_on_the_drain_connection(self) -> None:
        self.enrol()
        self.store.manage_sensor(SENSOR_ID, "Fern", "Study", "monstera", None, None, 60)
        # Held while the drain waits for the report to complete, then on its link.
        await self.hear(packet1_hex(10))
        self.assertEqual(self.events, [])
        await self.hear(packet2_hex(10))
        self.assertEqual(
            self.events,
            [("drain", SENSOR_ID, "platform-identifier", "connection-target",
              {"configure": True, "station_status": False, "firmware_update": False})],
        )

    async def test_a_pending_release_goes_before_any_drain(self) -> None:
        self.enrol()
        with self.store._condition, self.store._database:
            self.store._database.execute(
                "UPDATE sensors SET release_pending = 1 WHERE sensor_id = ?", (SENSOR_ID,)
            )
        await self.hear(packet1_hex(10))
        self.assertEqual(self.events, [("release", SENSOR_ID)])


async def hear(subscriber, clock_value, service_data_hexes) -> None:
    queue = asyncio.Queue()
    for service_data_hex in service_data_hexes:
        await queue.put(
            advertisement(
                service_data_hex,
                observed_identifier="platform-identifier",
                source_adapter="bleak",
                connection_target="connection-target",
            )
        )
    # The subscriber's clock only, so that time moves when the test says.
    clock = SimpleNamespace(monotonic=lambda: clock_value[0])
    with patch.object(ble, "time", clock):
        consumer = asyncio.create_task(subscriber._consume(queue))
        try:
            await asyncio.wait_for(queue.join(), timeout=5.0)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


class DrainEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The subscriber, the real synchronizer and a sensor with a backlog."""

    async def test_the_newest_report_stays_latest_while_older_ones_are_backfilled(
        self,
    ) -> None:
        store = ReadingStore()
        try:
            AdvertisementIngestionService(store).ingest(advertisement("40"))
            store.manage_sensor(SENSOR_ID, "Fern", "Office", "monstera", None, None, 60)
            sensor = FakeQueueSensor()
            for report_id in (48, 49):
                sensor.add(report_id, humidity=0x30)
            sensor.add(50)
            subscriber = BleakSubscriber(
                AdvertisementIngestionService(store),
                configuration_synchronizer=DeviceConfigurationSynchronizer(
                    store, client_factory=sensor.factory()
                ),
            )
            # Only report 50 is heard over the air, and hearing it drains.
            await hear(subscriber, [100.0], (packet1_hex(50), packet2_hex(50)))

            self.assertEqual(sensor.connections, 1)
            self.assertEqual(sensor.queue, [])
            # Configuration first, on the same link, then the drain.
            self.assertEqual(sensor.writes[0][0], 6)
            self.assertEqual(
                [payload.hex() for payload in sensor.writes[1:]],
                ["20", "2132000000", "22"],
            )
            self.assertEqual(store.latest(SENSOR_ID)["reading"]["report_id"], 50)
            self.assertEqual(
                sorted(item["reading"]["report_id"] for item in store.history(SENSOR_ID)),
                [48, 49, 50],
            )
            self.assertEqual(store.report_delivery(SENSOR_ID, 50), (True, False))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()