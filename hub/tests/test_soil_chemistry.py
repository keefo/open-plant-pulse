from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Thread
import unittest
from urllib.request import urlopen

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.application.store import CHEMISTRY_LOOKBACK
from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.domain.soil_chemistry import (
    MIN_MOISTURE_PERCENT,
    bulk_permittivity,
    estimate_pore_water_ec,
    nutrient_level,
    temperature_compensated_ec,
    topp_water_content,
)
from open_plant_pulse_hub.web import create_server, server_address


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
STRELITZIA_EC_IDEAL = [500, 1500]


def at(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def soil_reading(report_id, observed, conductivity, moisture, temperature=20.5):
    return SensorReading(
        sensor_id="plant-01",
        report_id=report_id,
        observed_at=at(observed),
        soil_temperature_c=temperature,
        moisture_percent=moisture,
        conductivity_us_cm=conductivity,
        air_temperature_c=22.0,
        air_humidity_percent=50.0,
        soil_ph=6.4,
        nitrogen_mg_kg=20,
        phosphorus_mg_kg=10,
        potassium_mg_kg=30,
    )


class PoreWaterModelTests(unittest.TestCase):
    """The model against readings this probe actually gave."""

    def assert_estimate_near(self, conductivity, moisture, temperature, expected):
        estimate = estimate_pore_water_ec(conductivity, moisture, temperature)
        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate.pore_water_ec_us_cm, expected, delta=expected * 0.05)

    def test_tap_water_in_a_glass(self) -> None:
        self.assert_estimate_near(41, 100, 23.8, 44)

    def test_just_after_watering(self) -> None:
        self.assert_estimate_near(133, 100.0, 19.9, 157)

    def test_settling_after_watering(self) -> None:
        self.assert_estimate_near(102, 53.2, 20.5, 226)

    def test_dry_soil_has_no_estimate(self) -> None:
        self.assertIsNone(estimate_pore_water_ec(0, 17.0, 21.7))
        # Conductivity alone does not rescue soil below the cutoff.
        self.assertIsNone(estimate_pore_water_ec(80, MIN_MOISTURE_PERCENT - 0.1, 21.7))
        self.assertIsNotNone(estimate_pore_water_ec(80, MIN_MOISTURE_PERCENT, 21.7))

    def test_missing_measurements_have_no_estimate(self) -> None:
        self.assertIsNone(estimate_pore_water_ec(None, 50.0, 20.0))
        self.assertIsNone(estimate_pore_water_ec(100, None, 20.0))
        self.assertIsNone(estimate_pore_water_ec(100, 50.0, None))
        self.assertIsNone(estimate_pore_water_ec(0, 50.0, 20.0))

    def test_compensates_conductivity_to_25_c(self) -> None:
        self.assertAlmostEqual(temperature_compensated_ec(100, 25.0), 100.0)
        self.assertAlmostEqual(temperature_compensated_ec(100, 15.0), 125.0)
        self.assertAlmostEqual(temperature_compensated_ec(100, 30.0), 100 / 1.1)
        # A cold reading of the same soil estimates the same as a warm one
        # once compensated, apart from water permittivity's own drift.
        cold = estimate_pore_water_ec(80, 50.0, 15.0)
        warm = estimate_pore_water_ec(100, 50.0, 25.0)
        self.assertAlmostEqual(cold.ec25_us_cm, warm.ec25_us_cm)

    def test_inverts_topp(self) -> None:
        for moisture in (0.2, 0.35, 0.532, 0.8):
            self.assertAlmostEqual(topp_water_content(bulk_permittivity(moisture)), moisture, places=6)
        self.assertEqual(bulk_permittivity(1.0), 80.0)

    def test_places_estimate_against_profile_range(self) -> None:
        self.assertEqual(nutrient_level(226, STRELITZIA_EC_IDEAL), "low")
        self.assertEqual(nutrient_level(499.9, STRELITZIA_EC_IDEAL), "low")
        self.assertEqual(nutrient_level(500, STRELITZIA_EC_IDEAL), "ok")
        self.assertEqual(nutrient_level(1500, STRELITZIA_EC_IDEAL), "ok")
        self.assertEqual(nutrient_level(1500.1, STRELITZIA_EC_IDEAL), "high")
        self.assertIsNone(nutrient_level(None, STRELITZIA_EC_IDEAL))
        self.assertIsNone(nutrient_level(800, None))


class ChemistrySelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore()

    def tearDown(self) -> None:
        self.store.close()

    def chemistry(self):
        return self.store.latest("plant-01", now=NOW)["chemistry"]

    def test_current_when_latest_reading_yields_an_estimate(self) -> None:
        self.store.add(soil_reading(1, NOW - timedelta(minutes=5), 102, 53.2))

        chemistry = self.chemistry()

        self.assertEqual(chemistry["status"], "current")
        self.assertFalse(chemistry["too_dry"])
        self.assertEqual(chemistry["pore_water_ec_us_cm"], 226)
        self.assertEqual(chemistry["ec25_us_cm"], 112.1)
        self.assertEqual(chemistry["nutrient_level"], "low")
        self.assertEqual(chemistry["age_seconds"], 300)
        self.assertEqual(chemistry["basis"]["report_id"], 1)
        self.assertFalse(chemistry["basis"]["post_watering"])

    def test_falls_back_to_most_recent_valid_reading_when_too_dry(self) -> None:
        self.store.add(soil_reading(1, NOW - timedelta(days=5), 300, 45.0))
        self.store.add(soil_reading(2, NOW - timedelta(days=2), 102, 53.2))
        self.store.add(soil_reading(3, NOW - timedelta(hours=1), 0, 17.0, 21.7))

        chemistry = self.chemistry()

        self.assertEqual(chemistry["status"], "last_valid")
        self.assertTrue(chemistry["too_dry"])
        self.assertEqual(chemistry["basis"]["report_id"], 2)
        self.assertEqual(chemistry["basis"]["conductivity_us_cm"], 102)
        self.assertEqual(chemistry["pore_water_ec_us_cm"], 226)
        self.assertEqual(chemistry["age_seconds"], 2 * 86400)
        self.assertFalse(chemistry["basis"]["post_watering"])

    def test_prefers_a_reading_taken_after_a_detected_watering(self) -> None:
        watered_at = NOW - timedelta(days=3)
        self.store.add(soil_reading(1, watered_at - timedelta(minutes=5), 60, 30.0))
        self.store.add(soil_reading(2, watered_at, 200, 70.0))
        self.store.add(soil_reading(3, watered_at + timedelta(minutes=20), 170, 66.0))
        self.store.add(soil_reading(4, watered_at + timedelta(minutes=40), 150, 60.0))
        self.store.add(soil_reading(5, watered_at + timedelta(minutes=70), 140, 58.0))
        # Newer and valid, but a day into the dry-back rather than after water.
        self.store.add(soil_reading(6, NOW - timedelta(days=1), 40, 25.0))
        self.store.add(soil_reading(7, NOW - timedelta(minutes=10), 0, 17.0))
        self.assertEqual(
            [event["kind"] for event in self.store.care_log(sensor_id="plant-01")].count("watering"), 1
        )

        chemistry = self.chemistry()

        self.assertEqual(chemistry["status"], "last_valid")
        self.assertEqual(chemistry["basis"]["report_id"], 4)
        self.assertTrue(chemistry["basis"]["post_watering"])
        self.assertEqual(
            chemistry["pore_water_ec_us_cm"],
            round(estimate_pore_water_ec(150, 60.0, 20.5).pore_water_ec_us_cm),
        )

    def test_marks_a_current_post_watering_reading(self) -> None:
        watered_at = NOW - timedelta(minutes=45)
        self.store.add(soil_reading(1, watered_at - timedelta(minutes=5), 60, 30.0))
        self.store.add(soil_reading(2, watered_at, 200, 70.0))
        self.store.add(soil_reading(3, NOW, 150, 60.0))

        chemistry = self.chemistry()

        self.assertEqual(chemistry["status"], "current")
        self.assertTrue(chemistry["basis"]["post_watering"])

    def test_nothing_valid_within_the_lookback_is_none(self) -> None:
        self.store.add(
            soil_reading(1, NOW - CHEMISTRY_LOOKBACK - timedelta(hours=1), 102, 53.2)
        )
        self.store.add(soil_reading(2, NOW - timedelta(hours=1), 0, 17.0))

        chemistry = self.chemistry()

        self.assertEqual(
            chemistry,
            {
                "status": "none",
                "too_dry": True,
                "pore_water_ec_us_cm": None,
                "ec25_us_cm": None,
                "nutrient_level": None,
                "basis": None,
                "age_seconds": None,
            },
        )

    def test_a_reading_just_inside_the_lookback_still_counts(self) -> None:
        self.store.add(
            soil_reading(1, NOW - CHEMISTRY_LOOKBACK + timedelta(hours=1), 102, 53.2)
        )
        self.store.add(soil_reading(2, NOW - timedelta(hours=1), 0, 17.0))

        self.assertEqual(self.chemistry()["status"], "last_valid")

    def test_places_reading_without_sensor_time_by_receipt(self) -> None:
        # A reading without sensor time is received now in real time.
        now = datetime.now(timezone.utc)
        self.store.add(replace(soil_reading(1, now, 102, 53.2), observed_at=None))
        self.store.add(soil_reading(2, now + timedelta(seconds=1), 0, 17.0))

        chemistry = self.store.latest("plant-01", now=now + timedelta(minutes=1))["chemistry"]

        self.assertEqual(chemistry["status"], "last_valid")
        self.assertIsNone(chemistry["basis"]["observed_at"])
        self.assertEqual(chemistry["basis"]["report_id"], 1)
        self.assertLessEqual(chemistry["age_seconds"], 61)

    def test_judges_high_nutrient_level_against_selected_profile(self) -> None:
        self.store.add(soil_reading(1, NOW, 900, 50.0))
        self.store.set_sensor_profile("plant-01", "monstera")

        chemistry = self.chemistry()

        self.assertGreater(chemistry["pore_water_ec_us_cm"], 1500)
        self.assertEqual(chemistry["nutrient_level"], "high")


class ChemistryApiTests(unittest.TestCase):
    def test_latest_reading_payloads_carry_chemistry(self) -> None:
        store = ReadingStore()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store.add(soil_reading(1, now - timedelta(days=2), 102, 53.2))
        store.add(soil_reading(2, now, 0, 17.0, 21.7))
        server = create_server(store, "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server_address(server)
            with urlopen(
                f"http://{host}:{port}/api/readings/latest?sensor_id=plant-01", timeout=1.0
            ) as response:
                latest = json.load(response)
            with urlopen(f"http://{host}:{port}/api/sensors/plant-01", timeout=1.0) as response:
                sensor = json.load(response)

            self.assertEqual(latest["reading"]["conductivity_us_cm"], 0)
            self.assertEqual(latest["reading"]["moisture_percent"], 17.0)
            chemistry = latest["chemistry"]
            self.assertEqual(
                set(chemistry),
                {
                    "status", "too_dry", "pore_water_ec_us_cm", "ec25_us_cm",
                    "nutrient_level", "basis", "age_seconds",
                },
            )
            self.assertEqual(chemistry["status"], "last_valid")
            self.assertTrue(chemistry["too_dry"])
            self.assertEqual(chemistry["pore_water_ec_us_cm"], 226)
            self.assertEqual(chemistry["ec25_us_cm"], 112.1)
            self.assertEqual(chemistry["nutrient_level"], "low")
            self.assertEqual(
                chemistry["basis"],
                {
                    "report_id": 1,
                    "observed_at": at(now - timedelta(days=2)),
                    "received_at": chemistry["basis"]["received_at"],
                    "conductivity_us_cm": 102,
                    "moisture_percent": 53.2,
                    "soil_temperature_c": 20.5,
                    "post_watering": False,
                },
            )
            self.assertAlmostEqual(chemistry["age_seconds"], 2 * 86400, delta=5)
            self.assertEqual(sensor["latest"]["chemistry"]["status"], "last_valid")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)
            store.close()


if __name__ == "__main__":
    unittest.main()
