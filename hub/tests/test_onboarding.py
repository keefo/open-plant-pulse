import json
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay
from open_plant_pulse_hub.web import create_server, server_address

FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v2-sensor-v2.json"


def enrolled_sensor(store, sensor_id, display_name="white bird"):
    return store.manage_sensor(sensor_id, display_name, "living", "monstera", None, None, 1800)


class OnboardingStateTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]

    def tearDown(self):
        self.store.close()

    def test_an_observed_sensor_starts_in_onboarding(self):
        sensor = self.store.sensor(self.sensor_id)
        self.assertEqual(sensor["onboarding_state"], "onboarding")
        self.assertEqual(sensor["enrollment_status"], "unclaimed")
        self.assertFalse(sensor["wifi_enabled"])
        self.assertEqual(sensor["wifi_state"], "off")

    def test_enrolling_moves_the_sensor_to_onboarded(self):
        sensor = enrolled_sensor(self.store, self.sensor_id)
        self.assertEqual(sensor["onboarding_state"], "onboarded")

    def test_returning_to_onboarding_clears_everything_derived_from_the_bond(self):
        enrolled_sensor(self.store, self.sensor_id)
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        self.store.set_sensor_wifi_enabled(self.sensor_id, True)
        self.store.record_sensor_wifi_result(self.sensor_id, "joined", None, "192.168.0.111")

        sensor = self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")

        self.assertEqual(sensor["onboarding_state"], "onboarding")
        self.assertFalse(sensor["wifi_enabled"])
        self.assertEqual(sensor["wifi_state"], "off")
        self.assertIsNone(sensor["wifi_address"])
        self.assertIsNone(sensor["wifi_failure"])

    def test_forgetting_returns_the_sensor_to_the_inbox(self):
        enrolled_sensor(self.store, self.sensor_id)
        self.assertEqual(self.store.sensor(self.sensor_id)["enrollment_status"], "enrolled")

        sensor = self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")

        self.assertEqual(sensor["enrollment_status"], "unclaimed")
        self.assertEqual(sensor["onboarding_state"], "onboarding")
        enrolled_ids = [item["sensor_id"] for item in self.store.sensors("enrolled")]
        unclaimed_ids = [item["sensor_id"] for item in self.store.sensors("unclaimed")]
        self.assertNotIn(self.sensor_id, enrolled_ids)
        self.assertIn(self.sensor_id, unclaimed_ids)

    def test_forgetting_keeps_the_readings(self):
        enrolled_sensor(self.store, self.sensor_id)
        before = self.store.sensor(self.sensor_id)["reading_count"]
        self.assertGreater(before, 0)

        self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")

        self.assertEqual(self.store.sensor(self.sensor_id)["reading_count"], before)

    def test_rejects_an_unknown_onboarding_state(self):
        with self.assertRaises(ValueError):
            self.store.set_sensor_onboarding_state(self.sensor_id, "paired")


class HouseholdNetworkTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enrolled_sensor(self.store, self.sensor_id)

    def tearDown(self):
        self.store.close()

    def test_no_network_is_configured_initially(self):
        self.assertEqual(self.store.hub_wifi_settings(), {"wifi_ssid": None})

    def test_the_network_name_is_stored_without_a_password(self):
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        self.assertEqual(self.store.hub_wifi_settings(), {"wifi_ssid": "BEYONDCOW-2.4G"})
        columns = {
            row[1] for row in self.store._database.execute("PRAGMA table_info(hub_settings)")
        }
        self.assertNotIn("wifi_password", columns)

    def test_a_console_cannot_be_switched_on_without_a_network(self):
        with self.assertRaises(ValueError):
            self.store.set_sensor_wifi_enabled(self.sensor_id, True)

    def test_switching_a_console_on_is_pending_until_the_sensor_answers(self):
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        sensor = self.store.set_sensor_wifi_enabled(self.sensor_id, True)
        self.assertTrue(sensor["wifi_enabled"])
        self.assertEqual(sensor["wifi_state"], "pending")
        self.assertIsNone(sensor["wifi_address"])

    def test_forgetting_the_network_switches_every_console_off(self):
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        self.store.set_sensor_wifi_enabled(self.sensor_id, True)
        self.store.record_sensor_wifi_result(self.sensor_id, "joined", None, "192.168.0.111")

        self.store.set_hub_wifi_network(None)

        sensor = self.store.sensor(self.sensor_id)
        self.assertIsNone(self.store.hub_wifi_settings()["wifi_ssid"])
        self.assertFalse(sensor["wifi_enabled"])
        self.assertEqual(sensor["wifi_state"], "off")
        self.assertIsNone(sensor["wifi_address"])

    def test_an_unenrolled_sensor_cannot_use_the_network(self):
        other = self.store.sensors("unclaimed")[0]["sensor_id"]
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        with self.assertRaises(ValueError):
            self.store.set_sensor_wifi_enabled(other, True)


class WifiResultTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enrolled_sensor(self.store, self.sensor_id)
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        self.store.set_sensor_wifi_enabled(self.sensor_id, True)

    def tearDown(self):
        self.store.close()

    def test_a_join_records_the_address(self):
        sensor = self.store.record_sensor_wifi_result(
            self.sensor_id, "joined", None, "192.168.0.111"
        )
        self.assertEqual(sensor["wifi_state"], "joined")
        self.assertEqual(sensor["wifi_address"], "192.168.0.111")
        self.assertIsNone(sensor["wifi_failure"])

    def test_every_documented_failure_reason_is_accepted(self):
        for failure in (
            "wrong_password",
            "network_not_found",
            "association_timeout",
            "no_address",
            "unsupported_band",
        ):
            sensor = self.store.record_sensor_wifi_result(self.sensor_id, "failed", failure)
            self.assertEqual(sensor["wifi_failure"], failure)

    def test_a_failure_must_say_why(self):
        with self.assertRaises(ValueError):
            self.store.record_sensor_wifi_result(self.sensor_id, "failed")

    def test_an_unknown_failure_reason_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.record_sensor_wifi_result(self.sensor_id, "failed", "gremlins")

    def test_only_a_joined_sensor_carries_an_address(self):
        with self.assertRaises(ValueError):
            self.store.record_sensor_wifi_result(
                self.sensor_id, "failed", "wrong_password", "192.168.0.111"
            )


class OnboardingWebTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enrolled_sensor(self.store, self.sensor_id)
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.store.close()

    def request(self, path, method="GET", payload=None, origin=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if origin is not None:
            headers["Origin"] = origin
        return urlopen(
            Request(self.base_url + path, data=data, headers=headers, method=method),
            timeout=1.0,
        )

    def sensor_path(self, suffix):
        return f"/api/sensors/{quote(self.sensor_id, safe='')}{suffix}"

    def test_settings_and_onboarding_pages_are_served(self):
        for path in ("/settings", "/settings/wifi", "/onboarding"):
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"<title>", response.read())

    def test_an_unknown_page_is_still_not_found(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/nowhere")
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_the_household_network_round_trips(self):
        with self.request(
            "/api/settings/wifi", method="PUT", payload={"wifi_ssid": "BEYONDCOW-2.4G"}
        ) as response:
            self.assertEqual(json.load(response), {"wifi_ssid": "BEYONDCOW-2.4G"})
        with self.request("/api/settings/wifi") as response:
            self.assertEqual(json.load(response), {"wifi_ssid": "BEYONDCOW-2.4G"})

    def test_switching_a_console_on_reports_pending(self):
        self.request(
            "/api/settings/wifi", method="PUT", payload={"wifi_ssid": "BEYONDCOW-2.4G"}
        ).close()
        with self.request(
            self.sensor_path("/wifi"), method="POST", payload={"enabled": True}
        ) as response:
            sensor = json.load(response)
        self.assertTrue(sensor["wifi_enabled"])
        self.assertEqual(sensor["wifi_state"], "pending")

    def test_a_reported_failure_is_visible_on_the_sensor(self):
        self.request(
            "/api/settings/wifi", method="PUT", payload={"wifi_ssid": "BEYONDCOW-2.4G"}
        ).close()
        self.request(self.sensor_path("/wifi"), method="POST", payload={"enabled": True}).close()
        with self.request(
            self.sensor_path("/wifi-result"),
            method="POST",
            payload={"wifi_state": "failed", "wifi_failure": "wrong_password"},
        ) as response:
            sensor = json.load(response)
        self.assertEqual(sensor["wifi_state"], "failed")
        self.assertEqual(sensor["wifi_failure"], "wrong_password")

    def test_switching_a_console_on_without_a_network_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            self.request(self.sensor_path("/wifi"), method="POST", payload={"enabled": True})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_enabled_must_be_a_boolean(self):
        with self.assertRaises(HTTPError) as raised:
            self.request(self.sensor_path("/wifi"), method="POST", payload={"enabled": "yes"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_a_cross_origin_network_change_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            self.request(
                "/api/settings/wifi",
                method="PUT",
                payload={"wifi_ssid": "elsewhere"},
                origin="http://evil.example",
            )
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_resetting_a_sensor_returns_it_to_onboarding(self):
        with self.request(
            self.sensor_path("/onboarding"),
            method="POST",
            payload={"onboarding_state": "onboarding"},
        ) as response:
            sensor = json.load(response)
        self.assertEqual(sensor["onboarding_state"], "onboarding")


class GuidedFlowTests(unittest.TestCase):
    """Walk the whole journey a customer takes, against a simulated sensor."""

    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]

    def tearDown(self):
        self.store.close()

    def test_a_new_sensor_can_be_onboarded_without_a_network(self):
        candidate = self.store.sensor(self.sensor_id)
        self.assertEqual(candidate["onboarding_state"], "onboarding")

        sensor = enrolled_sensor(self.store, self.sensor_id)

        self.assertEqual(sensor["onboarding_state"], "onboarded")
        self.assertFalse(sensor["wifi_enabled"])
        self.assertIsNotNone(sensor["latest"])

    def test_the_console_can_be_switched_on_during_onboarding(self):
        self.store.set_hub_wifi_network("BEYONDCOW-2.4G")
        enrolled_sensor(self.store, self.sensor_id)

        self.store.set_sensor_wifi_enabled(self.sensor_id, True)
        sensor = self.store.record_sensor_wifi_result(
            self.sensor_id, "joined", None, "192.168.0.111"
        )

        self.assertEqual(sensor["wifi_state"], "joined")
        self.assertEqual(sensor["wifi_address"], "192.168.0.111")

    def test_a_reset_sensor_can_be_onboarded_again(self):
        enrolled_sensor(self.store, self.sensor_id)
        self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")

        sensor = enrolled_sensor(self.store, self.sensor_id, "second life")

        self.assertEqual(sensor["onboarding_state"], "onboarded")
        self.assertEqual(sensor["display_name"], "second life")


class InterfaceTests(unittest.TestCase):
    """The served page and scripts carry the pieces the flow depends on."""

    def setUp(self):
        self.store = ReadingStore()
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)
        self.store.close()

    def get(self, path):
        with urlopen(self.base_url + path, timeout=1.0) as response:
            return response.read()

    def test_the_onboarding_script_is_served(self):
        self.assertIn(b"function renderOnboarding()", self.get("/onboarding.js"))

    def test_the_page_carries_both_new_sections(self):
        page = self.get("/")
        self.assertIn(b'data-page="settings"', page)
        self.assertIn(b'data-page="onboarding"', page)
        self.assertIn(b'src="/onboarding.js"', page)

    def test_a_forgotten_sensor_is_not_called_onboarding(self):
        script = self.get("/onboarding.js")
        self.assertIn(b'"Not paired"', script)
        self.assertNotIn(b'"Onboarding"', script)

    def test_forgetting_a_sensor_asks_first(self):
        self.assertIn(b"window.confirm", self.get("/onboarding.js"))

    def test_the_navigation_calls_the_fleet_plants(self):
        self.assertIn(b">Plants</a>", self.get("/"))

    def test_every_failure_reason_has_wording(self):
        script = self.get("/onboarding.js")
        for failure in (
            b"wrong_password",
            b"network_not_found",
            b"association_timeout",
            b"no_address",
            b"unsupported_band",
        ):
            self.assertIn(failure, script)

    def test_the_password_field_is_never_sent_to_the_hub(self):
        script = self.get("/onboarding.js")
        self.assertIn(b'getElementById("wifi-ssid")', script)
        self.assertNotIn(b'wifi_password', script)


if __name__ == "__main__":
    unittest.main()
