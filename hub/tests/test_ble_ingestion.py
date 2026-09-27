import asyncio
import inspect
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.application.migrations import DATABASE_SCHEMA_VERSION, MIGRATIONS
from open_plant_pulse_hub.application.store import RECEIVE_DIAGNOSTIC_LIMIT
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.ble import BleakSubscriber, _detection_callback
from open_plant_pulse_hub.ingestion.bthome import BTHOME_SERVICE_UUID
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay

FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)


SENSOR_ID = "sensor-aabbccddeeff"
MAIN_1235 = "402e2c3ed3040000451001"
SUPPLEMENTARY_1235 = "4001600c40103ed304000054080144010002000700"
FORCED_SUPPLEMENTARY_1235 = "4001600c40103a013ed304000054080144010002000700"


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
            supplement_first = history[2]["reading"]
            self.assertEqual(supplement_first["report_id"], 7)
            self.assertEqual(supplement_first["battery_percent"], 64)
            self.assertIsNone(supplement_first["soil_ph"])

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
                    [("accepted", 6), ("conflict", 1), ("duplicate", 1), ("rejected", 2)],
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
                    ["duplicate"] * 7 + ["conflict", "rejected", "rejected"],
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
    """Main and supplementary packets of one report, in either order."""

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
        self.assertEqual(reading["soil_ph"], 6.8)
        self.assertEqual(
            [reading["nitrogen_mg_kg"], reading["phosphorus_mg_kg"], reading["potassium_mg_kg"]],
            [1, 2, 7],
        )
        # The live cache that serves the unscoped latest must agree.
        self.assertEqual(self.store.latest()["reading"], reading)
        self.assertEqual(self.store.sensor(SENSOR_ID)["reading_count"], 1)

    def test_main_then_supplementary_joins_one_reading(self) -> None:
        self.assertEqual(self.ingestion.ingest(advertisement(MAIN_1235)), "accepted")
        self.assertIsNone(self.store.latest(SENSOR_ID)["reading"]["battery_percent"])
        self.assertEqual(self.ingestion.ingest(advertisement(SUPPLEMENTARY_1235)), "accepted")
        self.assert_joined()

    def test_supplementary_then_main_joins_one_reading(self) -> None:
        self.assertEqual(self.ingestion.ingest(advertisement(SUPPLEMENTARY_1235)), "accepted")
        # Held, but not a reading until the main packet arrives.
        self.assertIsNone(self.store.latest(SENSOR_ID))
        self.assertEqual(self.store.history(SENSOR_ID), [])
        self.assertEqual(self.ingestion.ingest(advertisement(MAIN_1235)), "accepted")
        self.assert_joined()

    def test_repeated_packets_are_duplicates_and_store_nothing(self) -> None:
        for service_data_hex in (MAIN_1235, SUPPLEMENTARY_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(
            self.ingestion.ingest(advertisement(MAIN_1235, "2026-09-26T21:20:09Z")),
            "duplicate",
        )
        self.assertEqual(
            self.ingestion.ingest(advertisement(SUPPLEMENTARY_1235, "2026-09-26T21:20:10Z")),
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
        for service_data_hex in (MAIN_1235, SUPPLEMENTARY_1235):
            self.ingestion.ingest(advertisement(service_data_hex))
        # Humidity 45 instead of 44, and battery 95 instead of 96.
        self.assertEqual(
            self.ingestion.ingest(advertisement("402e2d3ed3040000451001")),
            "conflict",
        )
        self.assertEqual(
            self.ingestion.ingest(
                advertisement("40015f0c40103ed304000054080144010002000700")
            ),
            "conflict",
        )
        self.assert_joined()
        self.assertEqual(self.store.latest(SENSOR_ID)["reading"]["air_humidity_percent"], 44.0)
        conflicts = self.store.raw_sensor_reports(SENSOR_ID)[:2]
        self.assertEqual([item["decode_status"] for item in conflicts], ["conflict", "conflict"])
        self.assertEqual([item["report_id"] for item in conflicts], [1235, 1235])
        self.assertEqual(
            [item["packet_kind"] for item in conflicts], ["supplementary", "main"]
        )

    def test_a_conflicting_supplement_held_before_its_main_is_not_attached(self) -> None:
        self.ingestion.ingest(advertisement(SUPPLEMENTARY_1235))
        self.assertEqual(
            self.ingestion.ingest(
                advertisement("40015f0c40103ed304000054080144010002000700")
            ),
            "conflict",
        )
        self.ingestion.ingest(advertisement(MAIN_1235))
        self.assert_joined()

    def test_an_unknown_time_stays_unknown(self) -> None:
        self.ingestion.ingest(advertisement(MAIN_1235, "2026-09-26T21:20:05Z"))
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
        latest = self.store.latest(SENSOR_ID)
        self.assertEqual(latest["reading"]["observed_at"], "2026-09-26T21:20:00Z")
        self.assertEqual(latest["received_at"], "2026-09-26T21:25:00Z")

    def test_a_nutrient_rise_arriving_after_its_reading_is_still_noticed(self) -> None:
        for service_data_hex in (
            "400298082f233e01000000568403",
            "403e010000005408013f500028006e00",
            # Conductivity up by 320, then nitrogen up by 18 once the
            # supplement joins the reading.
            "400298082f233e0200000056c404",
        ):
            self.ingestion.ingest(advertisement(service_data_hex))
        self.assertEqual(self.store.care_log(sensor_id=SENSOR_ID), [])

        self.ingestion.ingest(advertisement("403e020000005408013f620028006e00"))

        events = self.store.care_log(sensor_id=SENSOR_ID)
        self.assertEqual([event["kind"] for event in events], ["fertilizing"])
        self.assertEqual(events[0]["changes"]["nitrogen_mg_kg"], 18.0)

    def test_a_forced_report_counts_only_once_both_packets_are_stored(self) -> None:
        self.ingestion.ingest(advertisement(FORCED_SUPPLEMENTARY_1235))
        self.assertFalse(self.ingestion.stored_forced_report(SENSOR_ID, 1235))
        self.ingestion.ingest(advertisement(MAIN_1235))
        self.assertTrue(self.ingestion.stored_forced_report(SENSOR_ID, 1235))
        self.assertFalse(self.ingestion.stored_forced_report(SENSOR_ID, 1234))
        self.assertFalse(self.ingestion.stored_forced_report("sensor-001122334455", 1235))

        self.ingestion.ingest(advertisement("402e2c3ed2040000451001"))
        self.ingestion.ingest(advertisement("4001600c40103ed204000054080144010002000700"))
        self.assertFalse(self.ingestion.stored_forced_report(SENSOR_ID, 1234))

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
                self.assertEqual(store.latest(SENSOR_ID)["reading"]["report_id"], 42)
            finally:
                store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 17)
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


class DetectionCallbackTests(unittest.TestCase):
    """Packets that arrive before the sensor's name must not be lost."""

    MAIN = bytes.fromhex("40022e092e2c2f643ed2040000451001500037b86a562900")
    SUPPLEMENTARY = bytes.fromhex("4001600c40103ed204000054080144010002000700")

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
        detected(device, self._data(self.MAIN))
        detected(device, self._data(self.SUPPLEMENTARY))
        self.assertTrue(queue.empty())

        detected(device, self._data(local_name="sensor-aabbccddeeff"))

        items = self._drain(queue)
        self.assertEqual([item.service_data for item in items], [self.MAIN, self.SUPPLEMENTARY])
        self.assertTrue(all(item.local_name == "sensor-aabbccddeeff" for item in items))
        self.assertTrue(all(item.connection_target is device for item in items))

    def test_a_name_arriving_with_data_releases_what_was_held_first(self):
        queue, detected = self._callback()
        device = SimpleNamespace(address="ABCD")
        detected(device, self._data(self.MAIN))
        detected(device, self._data(self.SUPPLEMENTARY, local_name="sensor-aabbccddeeff"))
        self.assertEqual(
            [item.service_data for item in self._drain(queue)],
            [self.MAIN, self.SUPPLEMENTARY],
        )

    def test_known_names_are_applied_without_waiting(self):
        queue, detected = self._callback()
        device = SimpleNamespace(address="ABCD")
        detected(device, self._data(local_name="sensor-aabbccddeeff"))
        detected(device, self._data(self.MAIN))
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


class ForcedReportSubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledges_one_exact_report_once_after_both_packets_are_stored(self) -> None:
        store = ReadingStore()
        synchronized = []

        class RecordingIngestion(AdvertisementIngestionService):
            def ingest(self, advertisement):
                status = super().ingest(advertisement)
                synchronized.append(("ingested", status))
                return status

        class FakeSynchronizer:
            def has_pending(self, sensor_id):
                return False

            async def synchronize(self, sensor_id, target, force_report_id=None):
                synchronized.append((sensor_id, target, force_report_id))
                return "acknowledged"

        subscriber = BleakSubscriber(
            RecordingIngestion(store), configuration_synchronizer=FakeSynchronizer()
        )
        queue = asyncio.Queue()
        # An ordinary report, then the forced one advertised as its two packets
        # alternate: supplementary first, so it is complete only on the main.
        for service_data_hex in (
            "402e2c3ed2040000451001",
            "4001600c40103ed204000054080144010002000700",
            FORCED_SUPPLEMENTARY_1235,
            MAIN_1235,
            FORCED_SUPPLEMENTARY_1235,
            MAIN_1235,
        ):
            await queue.put(
                advertisement(
                    service_data_hex,
                    observed_identifier="platform-identifier",
                    source_adapter="bleak",
                    connection_target="connection-target",
                )
            )
        consumer = asyncio.create_task(subscriber._consume(queue))
        try:
            await asyncio.wait_for(queue.join(), timeout=1.0)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            store.close()

        self.assertEqual(
            synchronized,
            [
                ("ingested", "accepted"),
                ("ingested", "accepted"),
                ("ingested", "accepted"),
                ("ingested", "accepted"),
                (SENSOR_ID, "connection-target", 1235),
                ("ingested", "duplicate"),
                ("ingested", "duplicate"),
            ],
        )


if __name__ == "__main__":
    unittest.main()