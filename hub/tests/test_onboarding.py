import json
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import ReadingStore
from fake_sensors import place_sensors
from open_plant_pulse_hub.web import create_server, server_address

FIXTURE_PATH = Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"


def enrolled_sensor(store, sensor_id, display_name="white bird"):
    return store.manage_sensor(sensor_id, display_name, "living", "monstera", None, None, 1800)


class OnboardingStateTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        place_sensors(self.store)
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

    def test_forgetting_leaves_a_release_to_deliver(self):
        """The sensor may be asleep when somebody clicks, so the intent persists."""
        enrolled_sensor(self.store, self.sensor_id)
        self.assertFalse(self.store.release_is_pending(self.sensor_id))

        self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")

        self.assertTrue(self.store.release_is_pending(self.sensor_id))
        self.store.mark_released(self.sensor_id)
        self.assertFalse(self.store.release_is_pending(self.sensor_id))

    def test_claiming_a_sensor_clears_any_stale_release(self):
        enrolled_sensor(self.store, self.sensor_id)
        self.store.set_sensor_onboarding_state(self.sensor_id, "onboarding")
        self.store.set_sensor_onboarding_state(self.sensor_id, "onboarded")
        self.assertFalse(self.store.release_is_pending(self.sensor_id))

    def test_rejects_an_unknown_onboarding_state(self):
        with self.assertRaises(ValueError):
            self.store.set_sensor_onboarding_state(self.sensor_id, "paired")


class RoomTests(unittest.TestCase):
    """Rooms are chosen, not typed, and carry what shapes a plant's needs."""

    def setUp(self):
        self.store = ReadingStore()
        place_sensors(self.store)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]

    def tearDown(self):
        self.store.close()

    def test_a_room_records_what_shapes_a_plant(self):
        room = self.store.create_room("Study", "north", "low", "Small windows")
        self.assertEqual(room["aspect"], "north")
        self.assertEqual(room["light"], "low")
        self.assertEqual(room["notes"], "Small windows")
        self.assertEqual(room["sensor_count"], 0)

    def test_two_rooms_cannot_share_a_name(self):
        self.store.create_room("Study")
        with self.assertRaises(ValueError):
            self.store.create_room("Study")

    def test_every_compass_point_and_the_two_non_directions_are_offered(self):
        for aspect in (
            "north", "north_east", "east", "south_east",
            "south", "south_west", "west", "north_west",
            "several", "none", "unknown",
        ):
            room = self.store.create_room("Room " + aspect, aspect)
            self.assertEqual(room["aspect"], aspect)

    def test_an_invalid_aspect_or_light_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.create_room("Study", aspect="up")
        with self.assertRaises(ValueError):
            self.store.create_room("Study", light="dazzling")

    def test_a_sensor_joins_a_room_by_identity(self):
        room = self.store.create_room("Study")
        sensor = self.store.manage_sensor(
            self.sensor_id, "fern", "", "monstera", None, None, 1800, room_id=room["room_id"]
        )
        self.assertEqual(sensor["room"], "Study")
        self.assertEqual(sensor["room_id"], room["room_id"])
        self.assertEqual(self.store.rooms()[0]["sensor_count"], 1)

    def test_renaming_a_room_renames_it_for_its_sensors(self):
        room = self.store.create_room("Study")
        self.store.manage_sensor(
            self.sensor_id, "fern", "", "monstera", None, None, 1800, room_id=room["room_id"]
        )
        self.store.update_room(room["room_id"], "Back study", "east", "medium", None)
        self.assertEqual(self.store.sensor(self.sensor_id)["room"], "Back study")

    def test_a_room_holding_sensors_is_not_deleted(self):
        room = self.store.create_room("Study")
        self.store.manage_sensor(
            self.sensor_id, "fern", "", "monstera", None, None, 1800, room_id=room["room_id"]
        )
        with self.assertRaises(ValueError):
            self.store.delete_room(room["room_id"])
        self.assertEqual(len(self.store.rooms()), 1)

    def test_an_empty_room_is_deleted(self):
        room = self.store.create_room("Spare")
        self.store.delete_room(room["room_id"])
        self.assertEqual(self.store.rooms(), [])

    def test_an_unknown_room_is_refused_at_enrolment(self):
        with self.assertRaises(ValueError):
            self.store.manage_sensor(
                self.sensor_id, "fern", "", "monstera", None, None, 1800, room_id=999
            )




class HouseholdNetworkTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        place_sensors(self.store)
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
        place_sensors(self.store)
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






class ReachabilityTests(unittest.TestCase):
    """Being reachable is separate from reporting, on the sensor side."""

    def setUp(self):
        self.source = (
            Path(__file__).parents[2] / "sensors/soil-probe/firmware/main/app_main.c"
        ).read_text()

    def test_a_powered_sensor_stays_reachable(self):
        self.assertIn("usb_serial_jtag_is_connected()", self.source)
        self.assertIn("REACHABLE_INTERVAL_MS", self.source)

    def test_a_battery_sensor_stays_reachable_while_being_worked_on(self):
        self.assertIn("RESPONSIVE_WINDOW_MS", self.source)
        self.assertIn("opp_bthome_last_connection_ms()", self.source)

    def test_reachability_repeats_the_last_payload_rather_than_inventing_one(self):
        # Re-sending the same report ID is what stops a sensor that is reachable
        # every few seconds from filling the database with rows saying nothing.
        # An unclaimed sensor repeats its latest report; an owned one repeats
        # the newest report in its delivery queue until the hub drains it.
        self.assertIn("advertise_reachable_window", self.source)
        self.assertIn("last_unqueued", self.source)
        self.assertIn("opp_delivery_newest", self.source)




class OnboardingWebTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        place_sensors(self.store)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enrolled_sensor(self.store, self.sensor_id)
        self.server = create_server(self.store, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
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
        for path in ("/settings", "/settings/wifi"):
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

    def test_rooms_round_trip_over_the_api(self):
        """Rooms are made, renamed and removed through the API.

        The household may already have rooms: naming one while adopting a
        sensor makes it, because a sensor's room is now the room and not a
        label beside it. So this counts the one it creates rather than
        assuming it is alone.
        """
        with self.request("/api/rooms") as response:
            already = [r["name"] for r in json.load(response)["items"]]
        with self.request(
            "/api/rooms",
            method="POST",
            payload={"name": "Study", "aspect": "north", "light": "low", "notes": "Small windows"},
        ) as response:
            created = json.load(response)
        self.assertEqual(created["name"], "Study")
        with self.request("/api/rooms") as response:
            self.assertEqual(sorted(r["name"] for r in json.load(response)["items"]),
                             sorted(already + ["Study"]))
        with self.request(
            "/api/rooms/%d" % created["room_id"],
            method="PUT",
            payload={"name": "Back study", "aspect": "east", "light": "medium"},
        ) as response:
            self.assertEqual(json.load(response)["name"], "Back study")
        self.request("/api/rooms/%d" % created["room_id"], method="DELETE").close()
        with self.request("/api/rooms") as response:
            self.assertEqual(sorted(r["name"] for r in json.load(response)["items"]),
                             sorted(already))

    def test_resetting_a_sensor_returns_it_to_onboarding(self):
        with self.request(
            self.sensor_path("/onboarding"),
            method="POST",
            payload={"onboarding_state": "onboarding"},
        ) as response:
            sensor = json.load(response)
        self.assertEqual(sensor["onboarding_state"], "onboarding")


class GuidedFlowTests(unittest.TestCase):
    """Walk the whole journey a customer takes, against replayed advertisements."""

    def setUp(self):
        self.store = ReadingStore()
        place_sensors(self.store)
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
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                             daemon=True)
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







    def test_the_scan_pulse_respects_reduced_motion(self):
        self.assertIn(b"prefers-reduced-motion", self.get("/app.css"))





    def test_the_aspect_choices_cover_the_compass(self):
        page = self.get("/")
        for value in (b'"north_east"', b'"south_west"', b'"several"', b'"none"'):
            self.assertIn(value, page)
        # Half the compass is not enough to judge how much light a plant gets.
        self.assertIn(b"North-east", page)
        self.assertIn(b"No windows", page)

    def test_the_rooms_tab_is_reachable_by_its_own_address(self):
        script = self.get("/app.js")
        self.assertIn(b'const SETTINGS_TABS = ', script)
        self.assertIn(b'"rooms"', script)
        self.assertIn(b'href="/settings/rooms"', self.get("/"))






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
