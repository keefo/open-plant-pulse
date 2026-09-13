import json
from dataclasses import replace
from pathlib import Path
import socket
import sqlite3
import tempfile
from threading import Thread
import unittest
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.ingestion import decode_simulation_datagram
from open_plant_pulse_hub.ingestion.udp import SimulationUdpReceiver
from open_plant_pulse_hub.web import create_server, server_address


FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "simulated-reading-v1.json"


class UdpReceiverTests(unittest.TestCase):
    def test_receives_simulated_datagram(self) -> None:
        store = ReadingStore()
        receiver = SimulationUdpReceiver(store, port=0)
        receiver.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(FIXTURE_PATH.read_bytes(), receiver.address)

            self.assertTrue(store.wait_for_reading(timeout=1.0))
            latest = store.latest()
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest["reading"]["sensor_id"], "simulated-plant-01")
            self.assertEqual(latest["reading"]["potassium_mg_kg"], 124)
        finally:
            receiver.close()


class CareLogTests(unittest.TestCase):
    def test_builds_lifetime_plant_journey(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T00:00:00Z", 40.0),
            (2, "2027-01-01T01:00:00Z", 34.0),
            (3, "2027-01-02T00:00:00Z", 55.0),
            (4, "2027-01-03T00:00:00Z", 40.0),
            (5, "2027-01-03T01:00:00Z", 34.0),
            (6, "2027-01-04T02:00:00Z", 55.0),
        ]
        for sequence, observed_at, moisture in samples:
            store.add(replace(reading, sequence=sequence, observed_at=observed_at, moisture_percent=moisture))

        self.assertEqual(
            store.plant_journey(reading.sensor_id),
            {
                "started_at": "2027-01-01T00:00:00Z",
                "monitored_days": 4,
                "watering_count": 2,
                "fertilizing_count": 0,
                "missed_watering_count": 1,
            },
        )

    def test_queries_drainage_history_independently_for_sensor(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T00:00:00Z", 30.0),
            (2, "2027-01-01T00:02:00Z", 50.0),
            (3, "2027-01-01T00:12:00Z", 45.0),
            (4, "2027-01-01T00:13:00Z", 45.0),
            (5, "2027-01-01T00:14:00Z", 45.0),
        ]
        for sequence, observed_at, moisture in samples:
            store.add(replace(reading, sequence=sequence, observed_at=observed_at, moisture_percent=moisture))

        assessments = store.drainage_assessments(reading.sensor_id)

        self.assertEqual(len(assessments), 1)
        self.assertEqual(assessments[0]["kind"], "drainage_assessment")
        self.assertEqual(store.drainage_assessments("another-sensor"), [])

    def test_calculates_median_interval_between_distinct_watering_days(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T08:00:00Z", 30.0),
            (2, "2027-01-02T08:00:00Z", 50.0),
            (3, "2027-01-11T08:00:00Z", 30.0),
            (4, "2027-01-12T08:00:00Z", 50.0),
            (5, "2027-01-31T08:00:00Z", 30.0),
            (6, "2027-02-01T08:00:00Z", 50.0),
        ]
        for sequence, observed_at, moisture in samples:
            store.add(replace(reading, sequence=sequence, observed_at=observed_at, moisture_percent=moisture))

        self.assertEqual(
            store.watering_interval_summary(reading.sensor_id),
            {"typical_days": 15.0, "interval_count": 2},
        )

    def test_persists_detected_watering_event(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(database_path=database_path)
            store.add(
                replace(
                    reading,
                    sequence=1,
                    observed_at="2030-01-01T12:00:00Z",
                    moisture_percent=35.0,
                )
            )
            store.add(
                replace(
                    reading,
                    sequence=2,
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
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store = ReadingStore()
        store.set_sensor_profile(reading.sensor_id, "monstera")
        store.add(replace(reading, sequence=1, moisture_percent=44.0))
        store.add(replace(reading, sequence=2, moisture_percent=39.0))

        events = store.care_log()

        self.assertEqual(events[0]["kind"], "watering_due")
        self.assertEqual(events[0]["changes"]["refill_below_percent"], 40.0)

    def test_builds_watering_and_escalating_drying_calendar(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store = ReadingStore()
        samples = [
            (1, "2027-01-01T00:00:00Z", 40.0),
            (2, "2027-01-01T01:00:00Z", 34.0),
            (7, "2027-01-01T02:00:00Z", 36.0),
            (3, "2027-01-02T02:00:00Z", 33.0),
            (4, "2027-01-03T02:00:00Z", 30.0),
            (5, "2027-01-04T02:00:00Z", 25.0),
            (6, "2027-01-05T00:00:00Z", 50.0),
        ]
        for sequence, observed_at, moisture in samples:
            store.add(replace(reading, sequence=sequence, observed_at=observed_at, moisture_percent=moisture))

        activity = store.watering_calendar(
            reading.sensor_id,
            "2027-01-01T00:00:00Z",
            "2027-01-06T00:00:00Z",
        )

        self.assertEqual(
            activity,
            [
                {"date": "2027-01-01", "watering_count": 0, "drying_level": 0, "final_moisture_percent": 36.0},
                {"date": "2027-01-02", "watering_count": 0, "drying_level": 1, "final_moisture_percent": 33.0},
                {"date": "2027-01-03", "watering_count": 0, "drying_level": 2, "final_moisture_percent": 30.0},
                {"date": "2027-01-04", "watering_count": 0, "drying_level": 3, "final_moisture_percent": 25.0},
                {"date": "2027-01-05", "watering_count": 1, "drying_level": 0, "final_moisture_percent": 50.0},
            ],
        )


class ReadingPersistenceTests(unittest.TestCase):
    def test_retains_all_readings_and_restores_bounded_live_history(self) -> None:
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "hub.sqlite3")
            store = ReadingStore(history_size=2, database_path=database_path)
            for sequence in range(1, 4):
                store.add(replace(reading, sequence=sequence, moisture_percent=40.0 + sequence))
            store.add(replace(reading, sequence=3, moisture_percent=99.0))
            store.close()

            with sqlite3.connect(database_path) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0], 3)
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(database.execute("PRAGMA journal_mode").fetchone()[0], "wal")

            reopened = ReadingStore(history_size=2, database_path=database_path)
            try:
                self.assertEqual(
                    [item["reading"]["sequence"] for item in reopened.history()],
                    [2, 3],
                )
                self.assertEqual(reopened.latest()["reading"]["moisture_percent"], 43.0)
            finally:
                reopened.close()


class WebApiTests(unittest.TestCase):
    def test_serves_sensor_scoped_plant_journey(self) -> None:
        store = ReadingStore()
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store.add(reading)
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
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
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_sensor_scoped_pot_response_history(self) -> None:
        server = create_server(ReadingStore(), "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(
                f"http://{host}:{port}/api/pot-response?sensor_id=simulated-plant-01",
                timeout=1.0,
            ) as response:
                payload = json.load(response)

            self.assertEqual(payload, {"items": []})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_sensor_watering_calendar_range(self) -> None:
        store = ReadingStore()
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store.add(replace(reading, sequence=1, observed_at="2027-03-01T10:00:00Z", moisture_percent=30.0))
        store.add(replace(reading, sequence=2, observed_at="2027-03-01T10:10:00Z", moisture_percent=60.0))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            query = urlencode(
                {
                    "sensor_id": reading.sensor_id,
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
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            request = Request(
                f"http://{host}:{port}/api/sensors/profile",
                data=json.dumps(
                    {"sensor_id": "simulated-plant-01", "profile_id": "monstera"}
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
        server = create_server(ReadingStore(), "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(f"http://{host}:{port}/api/health", timeout=1.0) as response:
                payload = json.load(response)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["started_at"].endswith("Z"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_serves_latest_reading(self) -> None:
        store = ReadingStore()
        store.add(decode_simulation_datagram(FIXTURE_PATH.read_bytes()))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
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
        reading = decode_simulation_datagram(FIXTURE_PATH.read_bytes())
        store.add(replace(reading, sequence=1, moisture_percent=35.0))
        store.add(replace(reading, sequence=2, moisture_percent=70.0))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
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
        thread = Thread(target=server.serve_forever, daemon=True)
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
            self.assertEqual(payload["profiles"]["strelitzia"]["watering"]["refill_below"], 35)
            self.assertEqual(payload["profiles"]["strelitzia"]["watering"]["post_water_target"], [55, 70])
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