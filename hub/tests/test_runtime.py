import json
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import time
import unittest
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.application.migrations import DATABASE_SCHEMA_VERSION
from open_plant_pulse_hub.application.zigbee.drivers import ScaleDriver, decode_frame
from open_plant_pulse_hub.application.zigbee.network import Device
from open_plant_pulse_hub.application.zigbee.network import Reading
from open_plant_pulse_hub.application.zigbee.service import (ZigbeeService,
                                                             _kind_from_descriptor)
from open_plant_pulse_hub.web import create_server, server_address


def sample_reading():
    """A complete reading, as a physical sensor would produce one.

    Written out in place rather than loaded from a fixture: these tests are
    about the store, the dashboard and care events, so the reading is theirs to
    vary and does not depend on any particular transport.
    """
    return SensorReading(
        sensor_id="plant-01",
        report_id=42,
        observed_at="2026-09-01T12:00:00Z",
        soil_temperature_c=21.8,
        moisture_percent=38.4,
        conductivity_us_cm=1180,
        air_temperature_c=24.2,
        air_humidity_percent=53.6,
        soil_ph=6.4,
        nitrogen_mg_kg=86,
        phosphorus_mg_kg=41,
        potassium_mg_kg=124,
    )


class CareLogTests(unittest.TestCase):
    def test_builds_lifetime_plant_journey(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        # Around the default profile's refill marker (18 %, bird of paradise).
        samples = [
            (1, "2027-01-01T00:00:00Z", 24.0),
            (2, "2027-01-01T01:00:00Z", 17.0),
            (3, "2027-01-02T00:00:00Z", 40.0),
            (4, "2027-01-03T00:00:00Z", 24.0),
            (5, "2027-01-03T01:00:00Z", 17.0),
            (6, "2027-01-04T02:00:00Z", 40.0),
        ]
        for report_id, observed_at, moisture in samples:
            store.add(replace(reading, report_id=report_id, observed_at=observed_at, moisture_percent=moisture))
        store.manage_sensor(reading.sensor_id, "Fern", "Office",
                            "strelitzia", None, None, 1800)
        # The journey belongs to the plant, so naming one is what gives this
        # sensor's history somewhere to be counted — and what adopts the
        # events detected before anybody had named it.
        store.manage_sensor(reading.sensor_id, "Fern", "Office", "strelitzia", None, None, 1800)

        expected = {
            "started_at": "2027-01-01T00:00:00Z",
            "monitored_days": 4,
            "watering_count": 2,
            "fertilizing_count": 0,
            "missed_watering_count": 1,
            "lighting_hours": 0,
        }
        self.assertEqual(store.plant_journey(reading.sensor_id), expected)
        # The figures are kept as they happen rather than counted on every poll,
        # so what was kept has to agree with counting the history again.
        self.assertEqual(store.rebuild_journey(reading.sensor_id), expected)

    def test_queries_drainage_history_independently_for_sensor(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T00:00:00Z", 30.0),
            (2, "2027-01-01T00:02:00Z", 50.0),
            (3, "2027-01-01T00:12:00Z", 45.0),
            (4, "2027-01-01T00:22:00Z", 45.0),
            (5, "2027-01-01T00:32:00Z", 45.0),
        ]
        for report_id, observed_at, moisture in samples:
            store.add(replace(reading, report_id=report_id, observed_at=observed_at, moisture_percent=moisture))
        store.manage_sensor(reading.sensor_id, "Fern", "Office",
                            "strelitzia", None, None, 1800)

        assessments = store.drainage_assessments(reading.sensor_id)

        self.assertEqual(len(assessments), 1)
        self.assertEqual(assessments[0]["kind"], "drainage_assessment")
        self.assertEqual(store.drainage_assessments("another-sensor"), [])

    def test_calculates_median_interval_between_distinct_watering_days(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T08:00:00Z", 30.0),
            (2, "2027-01-02T08:00:00Z", 50.0),
            (3, "2027-01-11T08:00:00Z", 30.0),
            (4, "2027-01-12T08:00:00Z", 50.0),
            (5, "2027-01-31T08:00:00Z", 30.0),
            (6, "2027-02-01T08:00:00Z", 50.0),
        ]
        for report_id, observed_at, moisture in samples:
            store.add(replace(reading, report_id=report_id, observed_at=observed_at, moisture_percent=moisture))
        store.manage_sensor(reading.sensor_id, "Fern", "Office",
                            "strelitzia", None, None, 1800)

        self.assertEqual(
            store.watering_interval_summary(
                store.sensor(reading.sensor_id)["plant_id"]),
            {"typical_days": 15.0, "interval_count": 2},
        )

    def test_persists_detected_watering_event(self) -> None:
        reading = sample_reading()
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(database_path=database_path)
            store.add(
                replace(
                    reading,
                    report_id=1,
                    observed_at="2030-01-01T12:00:00Z",
                    moisture_percent=35.0,
                )
            )
            store.add(
                replace(
                    reading,
                    report_id=2,
                    observed_at="2030-01-01T12:02:00Z",
                    moisture_percent=70.0,
                )
            )
            store.close()

            reopened = ReadingStore(database_path=database_path)
            try:
                events = reopened.care_log()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], "watering")
                self.assertEqual(events[0]["changes"]["moisture_percent"], 35.0)
                self.assertEqual(events[0]["detected_at"], "2030-01-01T12:02:00Z")
            finally:
                reopened.close()

    def test_uses_selected_profile_refill_marker(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        store.set_sensor_profile(reading.sensor_id, "monstera")
        store.add(replace(reading, report_id=1, moisture_percent=44.0))
        store.add(replace(reading, report_id=2, moisture_percent=39.0))

        events = store.care_log()

        self.assertEqual(events[0]["kind"], "watering_due")
        self.assertEqual(events[0]["changes"]["refill_below_percent"], 40.0)

    def test_an_evening_watering_lands_on_the_evening(self) -> None:
        """West of Greenwich, an evening is already tomorrow in UTC.

        Watering was filed on the UTC day while feeding was filed on the
        local one, so doing both at seven in the evening in Vancouver put
        them on different squares -- the watering on tomorrow, which had
        not happened yet.
        """
        store = ReadingStore()
        store.set_hub_timezone("America/Vancouver")
        plant_id = store.create_plant("Orchid")["plant_id"]
        # 19:09 on the 22nd in Vancouver; 02:09 on the 23rd in UTC.
        evening = "2026-10-23T02:09:21Z"
        store.record_plant_care(plant_id, "watering", evening)
        store.record_plant_care(plant_id, "fertilizing", evening)

        days = {
            day["date"]: day for day in store.watering_calendar(
                plant_id, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
        }
        self.assertEqual(days["2026-10-22"]["watering_count"], 1)
        self.assertEqual(days["2026-10-22"]["fertilizing_count"], 1)
        self.assertEqual(days.get("2026-10-23", {}).get("watering_count", 0), 0)

    def test_builds_watering_and_escalating_drying_calendar(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        # The days below are UTC, and which square an event lands on is read
        # off the hub's zone -- so the zone is stated rather than inherited
        # from whichever machine runs the tests. Without it, midnight UTC is
        # the previous afternoon in Vancouver and the watering moves a day.
        store.set_hub_timezone("UTC")
        # Around the default profile's refill marker (18 %, bird of paradise).
        samples = [
            (1, "2027-01-01T00:00:00Z", 24.0),
            (2, "2027-01-01T01:00:00Z", 17.0),
            (3, "2027-01-01T02:00:00Z", 19.0),
            (4, "2027-01-02T02:00:00Z", 16.0),
            (5, "2027-01-03T02:00:00Z", 13.0),
            (6, "2027-01-04T02:00:00Z", 8.0),
            (7, "2027-01-05T00:00:00Z", 40.0),
        ]
        for report_id, observed_at, moisture in samples:
            store.add(replace(reading, report_id=report_id, observed_at=observed_at, moisture_percent=moisture))
        store.manage_sensor(reading.sensor_id, "Fern", "Office",
                            "strelitzia", None, None, 1800)

        activity = store.watering_calendar(
            store.sensor(reading.sensor_id)["plant_id"],
            "2027-01-01T00:00:00Z",
            "2027-01-06T00:00:00Z",
        )

        self.assertEqual(
            activity,
            [
                {"date": "2027-01-01", "watering_count": 0, "fertilizing_count": 0,
                 "drying_level": 0, "final_moisture_percent": 19.0},
                {"date": "2027-01-02", "watering_count": 0, "fertilizing_count": 0,
                 "drying_level": 1, "final_moisture_percent": 16.0},
                {"date": "2027-01-03", "watering_count": 0, "fertilizing_count": 0,
                 "drying_level": 2, "final_moisture_percent": 13.0},
                {"date": "2027-01-04", "watering_count": 0, "fertilizing_count": 0,
                 "drying_level": 3, "final_moisture_percent": 8.0},
                {"date": "2027-01-05", "watering_count": 1, "fertilizing_count": 0,
                 "drying_level": 0, "final_moisture_percent": 40.0},
            ],
        )

    def test_builds_watering_calendar_promptly_with_large_history(self) -> None:
        reading = sample_reading()
        store = ReadingStore()
        try:
            store.add(reading)
            store.manage_sensor(reading.sensor_id, "Fern", "Office",
                                "strelitzia", None, None, 1800)
            rows = []
            for report_id in range(1, 40321):
                day_index = (report_id - 1) // 120
                sample_index = (report_id - 1) % 120
                rows.append(
                    (
                        reading.sensor_id,
                        report_id,
                        f"2026-{(day_index // 28) + 1:02d}-{(day_index % 28) + 1:02d}"
                        f"T{sample_index // 60:02d}:{sample_index % 60:02d}:00Z",
                        "2026-09-01T12:00:00Z",
                        float(report_id % 101),
                    )
                )
            with store._condition, store._database:
                store._database.executemany(
                    """
                    INSERT OR IGNORE INTO sensor_readings (
                        sensor_id, report_id, observed_at, received_at,
                        moisture_percent, soil_source_status, air_source_status,
                        contract_version
                    ) VALUES (?, ?, ?, ?, ?, 'available', 'available', 1)
                    """,
                    rows,
                )

            started = time.monotonic()
            activity = store.watering_calendar(
                store.sensor(reading.sensor_id)["plant_id"],
                "2026-01-01T00:00:00Z",
                "2027-01-01T00:00:00Z",
            )

            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(len(activity), 336)
        finally:
            store.close()


class ReadingPersistenceTests(unittest.TestCase):
    def test_retains_all_readings_and_restores_bounded_live_history(self) -> None:
        reading = sample_reading()
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(history_size=2, database_path=database_path)
            store.set_reporting_interval(18)
            for report_id in range(1, 4):
                store.add(replace(reading, report_id=report_id, moisture_percent=40.0 + report_id))
            store.add(replace(reading, report_id=3, moisture_percent=99.0))
            store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0], 3)
                self.assertEqual(
                    database.execute("PRAGMA user_version").fetchone()[0],
                    DATABASE_SCHEMA_VERSION,
                )
                self.assertEqual(database.execute("PRAGMA journal_mode").fetchone()[0], "wal")

            reopened = ReadingStore(history_size=2, database_path=database_path)
            try:
                self.assertEqual(
                    [item["reading"]["report_id"] for item in reopened.history()],
                    [2, 3],
                )
                self.assertEqual(reopened.latest()["reading"]["moisture_percent"], 43.0)
                self.assertEqual(
                    reopened.hub_settings()["reporting_interval_minutes"], 18
                )
            finally:
                reopened.close()

    def test_queries_and_downsamples_an_inclusive_history_range(self) -> None:
        reading = sample_reading()
        store = ReadingStore(history_size=2)
        try:
            for hour in range(12):
                store.add(
                    replace(
                        reading,
                        report_id=hour + 1,
                        observed_at=f"2027-01-01T{hour:02d}:00:00Z",
                        air_temperature_c=20.0 + hour,
                    )
                )

            items = store.history_range(
                reading.sensor_id,
                "2027-01-01T01:00:00Z",
                "2027-01-01T10:00:00.000Z",
                max_points=4,
            )

            self.assertLessEqual(len(items), 4)
            self.assertEqual(items[0]["reading"]["report_id"], 2)
            self.assertEqual(items[-1]["reading"]["report_id"], 11)
        finally:
            store.close()


class WebApiTests(unittest.TestCase):
    def test_serves_bounded_sensor_climate_history_range(self) -> None:
        store = ReadingStore()
        reading = sample_reading()
        for report_id, observed_at in enumerate(
            [
                "2027-01-01T00:00:00Z",
                "2027-01-02T00:00:00Z",
                "2027-01-03T00:00:00Z",
            ],
            start=1,
        ):
            store.add(replace(reading, report_id=report_id, observed_at=observed_at))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            query = urlencode(
                {
                    "sensor_id": reading.sensor_id,
                    "start": "2027-01-02T00:00:00Z",
                    "end": "2027-01-03T00:00:00Z",
                }
            )
            with urlopen(f"http://{host}:{port}/api/readings/history?{query}", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(
                [item["reading"]["report_id"] for item in payload["items"]],
                [2, 3],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)
            store.close()

    def test_serves_sensor_scoped_plant_journey(self) -> None:
        store = ReadingStore()
        reading = sample_reading()
        store.add(reading)
        store.manage_sensor(reading.sensor_id, "Fern", "Office", "strelitzia", None, None, 1800)
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(
                f"http://{host}:{port}/api/plant-journey?sensor_id={reading.sensor_id}",
                timeout=1.0,
            ) as response:
                payload = json.load(response)

            self.assertEqual(
                payload,
                {
                    "started_at": reading.observed_at,
                    "monitored_days": 1,
                    "watering_count": 0,
                    "fertilizing_count": 0,
                    "missed_watering_count": 0,
                    "lighting_hours": 0,
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_sensor_scoped_pot_response_history(self) -> None:
        server = create_server(ReadingStore(), "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(
                f"http://{host}:{port}/api/pot-response?sensor_id=plant-01",
                timeout=1.0,
            ) as response:
                payload = json.load(response)

            self.assertEqual(payload, {"items": []})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_a_plants_watering_calendar_range(self) -> None:
        store = ReadingStore()
        reading = sample_reading()
        store.add(replace(reading, report_id=1, observed_at="2027-03-01T10:00:00Z", moisture_percent=30.0))
        store.add(replace(reading, report_id=2, observed_at="2027-03-01T10:10:00Z", moisture_percent=60.0))
        store.manage_sensor(reading.sensor_id, "Fern", "Office",
                            "strelitzia", None, None, 1800)
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            query = urlencode(
                {
                    "plant_id": store.sensor(reading.sensor_id)["plant_id"],
                    "start": "2027-01-01T00:00:00Z",
                    "end": "2028-01-01T00:00:00Z",
                }
            )
            with urlopen(f"http://{host}:{port}/api/watering-calendar?{query}", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(
                payload["items"],
                [
                    {
                        "date": "2027-03-01",
                        "watering_count": 1,
                        "fertilizing_count": 0,
                        "drying_level": 0,
                        "final_moisture_percent": 60.0,
                    }
                ],
            )
            self.assertEqual(
                payload["watering_interval"],
                {"typical_days": None, "interval_count": 0},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_accepts_valid_sensor_profile_selection(self) -> None:
        server = create_server(ReadingStore(), "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            request = Request(
                f"http://{host}:{port}/api/sensors/profile",
                data=json.dumps(
                    {"sensor_id": "plant-01", "profile_id": "monstera"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            with urlopen(request, timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(payload["profile_id"], "monstera")
            self.assertEqual(payload["refill_below"], 40.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_health_exposes_service_clock_origin(self) -> None:
        store = ReadingStore()

        def scanner_health():
            return {
                "status": "unavailable",
                "last_error": "adapter unavailable",
                "last_receive_at": None,
            }

        server = create_server(store, "127.0.0.1", 0, scanner_health)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(f"http://{host}:{port}/api/health", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["started_at"].endswith("Z"))
            self.assertEqual(
                payload["database"],
                {"status": "ok", "schema_version": DATABASE_SCHEMA_VERSION},
            )
            self.assertEqual(payload["scanner"]["status"], "unavailable")
            self.assertIsNone(payload["scanner"]["last_receive_at"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)
            store.close()

    def test_serves_latest_reading(self) -> None:
        store = ReadingStore()
        store.add(sample_reading())
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(f"http://{host}:{port}/api/readings/latest", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["reading"]["soil_ph"], 6.4)
            self.assertIn("received_at", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_detected_care_events(self) -> None:
        store = ReadingStore()
        reading = sample_reading()
        store.add(replace(reading, report_id=1, moisture_percent=35.0))
        store.add(replace(reading, report_id=2, moisture_percent=70.0))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(f"http://{host}:{port}/api/care-log", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["items"][0]["kind"], "watering")
            self.assertEqual(payload["items"][0]["confidence"], "high")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_plant_chemistry_profiles(self) -> None:
        server = create_server(ReadingStore(), "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(f"http://{host}:{port}/api/plant-profiles", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(payload["default_profile"], "strelitzia")
            self.assertEqual(
                payload["profiles"]["strelitzia"]["chemistry"]["soil_ph"]["ideal"],
                [5.5, 7.0],
            )
            self.assertEqual(
                payload["profiles"]["strelitzia"]["climate"]["air_temperature_c"]["ideal"],
                [18, 29],
            )
            self.assertEqual(
                payload["profiles"]["strelitzia"]["climate"]["air_humidity_percent"]["ideal"],
                [40, 60],
            )
            self.assertEqual(
                payload["profiles"]["strelitzia"]["root_zone"]["moisture_percent"]["scale"],
                [0, 100],
            )
            self.assertEqual(payload["profiles"]["strelitzia"]["watering"]["refill_below"], 18)
            self.assertEqual(payload["profiles"]["strelitzia"]["watering"]["post_water_target"], [35, 55])
            self.assertEqual(
                [level["status"] for level in payload["assessment_policy"]["levels"]],
                ["Thriving", "Watch", "Stressed", "Critical"],
            )
            self.assertIn("not fertilization instructions", payload["guidance"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()

class ZigbeeKindFromDescriptorTests(unittest.TestCase):
    """What a device is, decided before it has said anything.

    A plug sends nothing until asked and a thermometer sleeps, so
    waiting for a reading left both as "unknown" with no controls.
    """

    def test_a_thermometer_measures_the_air(self) -> None:
        device = Device(eui="a", clusters=(0x0000, 0x0001, 0x0402, 0x0405))
        self.assertEqual(_kind_from_descriptor(device), "thermometer")

    def test_our_scale_is_not_a_thermometer(self) -> None:
        """It registers as an HA temperature sensor to pass commissioning.

        Temperature alone must not be enough, or every scale this
        project builds arrives claiming to measure the room.
        """
        self.assertIsNone(_kind_from_descriptor(
            Device(eui="a", clusters=(0x0000, 0x0003, 0x0402))))

    def test_a_scale_is_known_by_its_own_cluster(self) -> None:
        device = Device(eui="a", clusters=(0x0000, 0x0402, 0xFC00))
        self.assertEqual(_kind_from_descriptor(device), "scale")

    def test_a_plug_switches(self) -> None:
        device = Device(eui="a", clusters=(0x0000, 0x0006, 0x0702))
        self.assertEqual(_kind_from_descriptor(device), "plug")

    def test_a_device_with_nothing_to_offer_stays_unknown(self) -> None:
        """Better unnamed than named after controls it does not have."""
        self.assertIsNone(_kind_from_descriptor(Device(eui="a", clusters=(0x0000,))))


class _SilentRadio:
    """A radio that hears nothing, so the service can be asked directly."""

    def devices(self):
        return []

    def on_reading(self, handler):
        return None

    def on_joined(self, handler):
        return None

    def on_delivery(self, handler):
        return None


class ZigbeeLastValuesTests(unittest.TestCase):
    """What a device last said, across frames that each say part of it."""

    def test_a_later_frame_does_not_erase_an_earlier_measurement(self) -> None:
        """Temperature and humidity arrive separately, minutes apart.

        Replacing the values wholesale meant the page showed one or the
        other and never both, flipping between them as reports landed.
        """
        service = ZigbeeService(store=None, network=_SilentRadio())
        service._reading(Reading(eui="a", kind="thermometer", values={"temp_c": 24.4}))
        service._reading(Reading(eui="a", kind="thermometer", values={"rh_pct": 52.1}))
        self.assertEqual(service._health["a"].last_values,
                         {"temp_c": 24.4, "rh_pct": 52.1})

    def test_a_fresh_value_replaces_the_one_it_updates(self) -> None:
        service = ZigbeeService(store=None, network=_SilentRadio())
        service._reading(Reading(eui="a", kind="thermometer", values={"temp_c": 24.4}))
        service._reading(Reading(eui="a", kind="thermometer", values={"temp_c": 19.0}))
        self.assertEqual(service._health["a"].last_values, {"temp_c": 19.0})





class ZigbeeSoilProbeTests(unittest.TestCase):
    """A Zigbee soil probe files readings like any other probe.

    Into the same table the Bluetooth probes use, so the plant pages,
    the moisture curve and the watering figures need to learn nothing
    about Zigbee to show it.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.eui = "a4c138aee9841e5c"
        sensor = self.store.seen_zigbee_device(self.eui, "soil")
        self.sensor_id = sensor["sensor_id"]
        self.store.enroll_zigbee_device(self.eui, "Pot probe")
        self.service = ZigbeeService(self.store, _SilentRadio())

    def test_a_tuya_frame_becomes_a_soil_reading(self) -> None:
        # Datapoint 5 is the temperature in tenths, 109 the moisture.
        for frame in ("095502005505020004000000e1",      # 225 -> 22.5 C
                      "09560200566d02000400000046"):   # dp 109 -> 70 %
            self.service._reading(Reading(
                eui=self.eui, kind="frame",
                values={"cluster": 0xEF00, "endpoint": 1,
                        "payload": bytes.fromhex(frame)}))
        rows = [row["reading"] for row in self.store.history(self.sensor_id)]
        soil = [r for r in rows if r["soil_temperature_c"] is not None]
        moist = [r for r in rows if r["moisture_percent"] is not None]
        self.assertEqual(soil[0]["soil_temperature_c"], 22.5)
        self.assertEqual(moist[0]["moisture_percent"], 70)

    def test_the_probe_reports_no_air(self) -> None:
        """It is in a pot. A missing air number must read as absent."""
        self.service._reading(Reading(
            eui=self.eui, kind="frame",
            values={"cluster": 0xEF00, "endpoint": 1,
                    "payload": bytes.fromhex("095502005505020004000000e1")}))
        row = self.store.history(self.sensor_id)[0]["reading"]
        self.assertIsNone(row["air_temperature_c"])
        self.assertEqual(row["air_source_status"], "unavailable")


class ZigbeeScaleFrameTests(unittest.TestCase):
    """The scale's own frames, which the dongle hands up raw.

    The serial gateway unpacked these inside itself, so swapping the
    radio left the scale transmitting every few seconds into a hub
    that dropped every frame.
    """

    # A real frame, captured from the bench scale.
    FRAME = bytes.fromhex("c0d4000000ac5d461904864064030204000350b1090005000000")

    def test_a_report_becomes_a_weight(self) -> None:
        values = decode_frame(0xFC00, self.FRAME, kind="scale")
        self.assertEqual(values["grams"], 14187.0)
        self.assertEqual(values["fw"], "2.4.0")
        self.assertEqual(values["interval"], 5)
        self.assertTrue(values["ok"])
        self.assertFalse(values["saturated"])

    def test_the_flags_are_read_apart(self) -> None:
        """Steady and saturated are different facts about one reading."""
        values = decode_frame(0xFC00, self.FRAME, kind="scale")
        self.assertTrue(values["stable"])
        self.assertFalse(values["wifi"])

    def test_a_text_note_is_never_unpacked_as_a_weighing(self) -> None:
        """Length cannot tell them apart; being text can.

        The note was the shorter of the two until the scale began
        announcing its address as well as its name. At thirty-two bytes
        it passed the twenty-six byte test and every announcement was
        unpacked as a weighing: the chart drew a sawtooth between plus
        and minus 2e25 grams, because the letters of "host" read as a
        float are a number like any other.
        """
        long_note = b"host=white-bird ip=192.168.0.172"
        self.assertGreater(len(long_note), 26)
        self.assertEqual(decode_frame(0xFC00, long_note, kind="scale"),
                         {"host": "white-bird", "ip": "192.168.0.172"})

    def test_a_weighing_is_still_a_weighing(self) -> None:
        values = decode_frame(0xFC00, self.FRAME, kind="scale")
        self.assertEqual(values["grams"], 14187.0)

    def test_a_short_frame_is_not_guessed_at(self) -> None:
        self.assertIsNone(decode_frame(0xFC00, self.FRAME[:10], kind="scale"))

    def test_the_weight_reaches_the_driver_that_files_it(self) -> None:
        reading = Reading(eui="a", kind="scale",
                          values=decode_frame(0xFC00, self.FRAME, kind="scale"))
        self.assertEqual(ScaleDriver.weight_grams(reading), 14187.0)


class DeleteRoomTests(unittest.TestCase):
    """A room holding something says so, rather than crashing the request.

    Only sensors were counted, and plants.room_id is a foreign key too,
    so deleting a room with a plant in it raised IntegrityError out of
    the request thread: the connection died mid-reply and the page said
    "Failed to fetch" with nothing to explain it.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.room = self.store.create_room("spare")["room_id"]

    def test_a_room_holding_a_plant_is_kept_and_says_why(self) -> None:
        self.store.create_plant("orchid", room_id=self.room)
        with self.assertRaises(ValueError) as caught:
            self.store.delete_room(self.room)
        self.assertIn("1 plant", str(caught.exception))
        self.assertIn(self.room, [r["room_id"] for r in self.store.rooms()])

    def test_an_empty_room_goes(self) -> None:
        self.store.delete_room(self.room)
        self.assertNotIn(self.room, [r["room_id"] for r in self.store.rooms()])

    def test_a_room_emptied_of_its_plant_goes(self) -> None:
        plant = self.store.create_plant("orchid", room_id=self.room)["plant_id"]
        self.store.update_plant(plant, "orchid", room_id=None)
        self.store.delete_room(self.room)
        self.assertNotIn(self.room, [r["room_id"] for r in self.store.rooms()])


class RoomCountsTests(unittest.TestCase):
    """A room's card shows what is in it, because that is what blocks a delete."""

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.room = self.store.create_room("spare")["room_id"]

    def test_rooms_count_their_plants(self) -> None:
        self.store.create_plant("orchid", room_id=self.room)
        self.store.create_plant("fern", room_id=self.room)
        room = [r for r in self.store.rooms() if r["room_id"] == self.room][0]
        self.assertEqual(room["plant_count"], 2)
        self.assertEqual(room["sensor_count"], 0)

    def test_an_empty_room_counts_nothing(self) -> None:
        room = [r for r in self.store.rooms() if r["room_id"] == self.room][0]
        self.assertEqual(room["plant_count"], 0)


class RoomDetailTests(unittest.TestCase):
    """A room's page says what is in it, not just how much."""

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.room = self.store.create_room("spare")["room_id"]

    def test_a_room_names_its_plants(self) -> None:
        self.store.create_plant("orchid", room_id=self.room)
        detail = self.store.room_detail(self.room)
        self.assertEqual([p["display_name"] for p in detail["plants"]], ["orchid"])
        self.assertEqual(detail["sensors"], [])

    def test_an_unknown_room_is_none_rather_than_an_error(self) -> None:
        self.assertIsNone(self.store.room_detail(9999))

    def test_what_it_lists_is_what_blocks_the_delete(self) -> None:
        """The page shows the obstacle beside the button, so the two
        must be counting the same rows."""
        self.store.create_plant("orchid", room_id=self.room)
        detail = self.store.room_detail(self.room)
        with self.assertRaises(ValueError) as caught:
            self.store.delete_room(self.room)
        self.assertEqual(len(detail["plants"]), detail["plant_count"])
        self.assertIn(f"{detail['plant_count']} plant", str(caught.exception))
