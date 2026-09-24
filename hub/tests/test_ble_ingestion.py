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
from open_plant_pulse_hub.ingestion.ble import BleakSubscriber
from open_plant_pulse_hub.ingestion.bthome import BTHOME_SERVICE_UUID
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay

FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v2-sensor-v2.json"
)


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
                [
                    "accepted",
                    "duplicate",
                    "accepted",
                    "accepted",
                    "accepted",
                    "rejected",
                    "rejected",
                ],
            )
            self.assertEqual(len(history), 4)
            self.assertEqual(len({item["reading"]["sensor_id"] for item in history}), 3)
            air_only = history[1]["reading"]
            self.assertIsNone(air_only["soil_temperature_c"])
            self.assertIsNone(air_only["moisture_percent"])
            self.assertEqual(air_only["air_temperature_c"], 24.5)
            self.assertEqual(
                fixture["events"][2]["expected"]["soil_source_status"],
                "unavailable",
            )

            with sqlite3.connect(database_path) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], 3)
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    database.execute(
                        "SELECT decode_status, COUNT(*) FROM advertisements GROUP BY decode_status"
                    ).fetchall(),
                    [("accepted", 4), ("duplicate", 1), ("rejected", 2)],
                )

            reopened = ReadingStore(database_path=database_path)
            try:
                statuses = AdvertisementReplay(
                    AdvertisementIngestionService(reopened)
                ).replay(FIXTURE_PATH)
                self.assertEqual(
                    statuses,
                    [
                        "duplicate",
                        "duplicate",
                        "duplicate",
                        "duplicate",
                        "duplicate",
                        "rejected",
                        "rejected",
                    ],
                )
                self.assertEqual(len(reopened.history()), 4)
            finally:
                reopened.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0],
                    4,
                )

    def test_accepts_reused_packet_id_after_an_intervening_sample(self) -> None:
        events = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["events"]
        store = ReadingStore()
        ingestion = AdvertisementIngestionService(store)

        def advertisement(event, received_at):
            return Advertisement(
                received_at=received_at,
                local_name=event["local_name"],
                observed_identifier=event["observed_identifier"],
                rssi=event["rssi"],
                service_data=bytes.fromhex(event["service_data_hex"]),
                source_adapter="replay",
            )

        try:
            self.assertEqual(
                ingestion.ingest(advertisement(events[0], "2026-09-13T12:00:00Z")),
                "accepted",
            )
            self.assertEqual(
                ingestion.ingest(advertisement(events[2], "2026-09-13T12:30:00Z")),
                "accepted",
            )
            self.assertEqual(
                ingestion.ingest(advertisement(events[0], "2026-09-13T13:00:00Z")),
                "accepted",
            )
        finally:
            store.close()

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
                         'simulation', 1, 60, 3, 3),
                        ('sensor-pending', 'legacy', 'sensor-pending', 'enrolled',
                         'Pending', '2026-09-13T12:00:00Z', '2026-09-13T12:00:00Z',
                         'simulation', 1, 30, 4, 0)
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

    def test_migrates_opp_identity_without_changing_simulator_identity(self) -> None:
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
                             ('simulated-plant-01', 'legacy', 'simulated-plant-01',
                              'enrolled', '2026-09-13T12:00:00Z',
                              '2026-09-13T12:00:00Z', 'simulation', 1)
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
                    WHERE sensor_id = 'simulated-plant-01'
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
                    [("sensor-aabbccddeeff",), ("simulated-plant-01",)],
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
                        ("simulated-plant-01",),
                    ).fetchone()[0],
                    "sensor-aabbccddeeff",
                )
                for table in ("advertisements", "sensor_readings", "care_events"):
                    self.assertEqual(
                        database.execute(f"SELECT sensor_id FROM {table}").fetchone()[0],
                        "sensor-aabbccddeeff",
                    )


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
    async def test_acknowledges_one_exact_packet_once_after_durable_ingestion(self) -> None:
        ingested = []
        synchronized = []

        class FakeIngestion:
            def ingest(self, advertisement):
                ingested.append(advertisement.service_data)
                return "accepted" if len(ingested) == 1 else "duplicate"

        class FakeSynchronizer:
            def has_pending(self, sensor_id):
                return False

            async def synchronize(self, sensor_id, target, packet_id=None):
                synchronized.append((sensor_id, target, packet_id, len(ingested)))
                return "acknowledged"

        subscriber = BleakSubscriber(
            FakeIngestion(), configuration_synchronizer=FakeSynchronizer()
        )
        queue = asyncio.Queue()
        forced_payload = bytes.fromhex("40004d037c153a0145f100")
        advertisement = Advertisement(
            received_at="2026-09-13T12:00:00Z",
            local_name="sensor-aabbccddeeff",
            observed_identifier="platform-identifier",
            rssi=-50,
            service_data=forced_payload,
            source_adapter="bleak",
            connection_target="connection-target",
        )
        await queue.put(advertisement)
        await queue.put(advertisement)
        consumer = asyncio.create_task(subscriber._consume(queue))
        try:
            await asyncio.wait_for(queue.join(), timeout=1.0)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        self.assertEqual(len(ingested), 2)
        self.assertEqual(
            synchronized,
            [("sensor-aabbccddeeff", "connection-target", 77, 1)],
        )


if __name__ == "__main__":
    unittest.main()