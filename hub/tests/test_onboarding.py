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
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
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


class OnboardingBeaconTests(unittest.TestCase):
    """A sensor with nothing to measure must still be findable."""

    def setUp(self):
        self.store = ReadingStore()
        self.ingestion = AdvertisementIngestionService(self.store)

    def tearDown(self):
        self.store.close()

    def beacon(self, packet_id=1, received_at=None):
        from datetime import datetime, timezone

        from open_plant_pulse_hub.ingestion.advertisement import Advertisement

        if received_at is None:
            received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return Advertisement(
            received_at=received_at,
            local_name="sensor-aabbccddeeff",
            observed_identifier="aa:bb:cc:dd:ee:ff",
            rssi=-48,
            service_data=bytes([0x40, 0x00, packet_id]),
            source_adapter="test",
        )

    def test_a_beacon_puts_the_sensor_in_the_inbox(self):
        self.assertEqual(self.ingestion.ingest(self.beacon()), "accepted")
        unclaimed = self.store.sensors("unclaimed")
        self.assertEqual([s["sensor_id"] for s in unclaimed], ["sensor-aabbccddeeff"])
        self.assertEqual(unclaimed[0]["onboarding_state"], "onboarding")
        self.assertEqual(unclaimed[0]["latest_rssi"], -48)

    def test_a_beacon_stores_no_reading(self):
        self.ingestion.ingest(self.beacon())
        sensor = self.store.sensor("sensor-aabbccddeeff")
        self.assertEqual(sensor["reading_count"], 0)
        self.assertIsNone(sensor["latest"])

    def test_a_repeated_beacon_is_a_duplicate(self):
        self.assertEqual(self.ingestion.ingest(self.beacon()), "accepted")
        self.assertEqual(self.ingestion.ingest(self.beacon()), "duplicate")

    def test_a_beacon_keeps_the_sensor_offerable_despite_old_readings(self):
        """Age since the last reading must not hide a sensor that is beaconing now."""
        self.ingestion.ingest(self.beacon())
        sensor = self.store.sensor("sensor-aabbccddeeff")
        self.assertLess(sensor["seen_age_seconds"], 300)

    def test_adopting_a_sensor_always_re_delivers_its_configuration(self):
        """The delivery is what opens the connection pairing needs."""
        self.ingestion.ingest(self.beacon())
        first = enrolled_sensor(self.store, "sensor-aabbccddeeff", "bare board")
        self.store.mark_device_configuration_applied(
            "sensor-aabbccddeeff", first["device_config_revision"], 1800
        )
        self.assertEqual(self.store.sensor("sensor-aabbccddeeff")["device_config_status"], "applied")

        self.store.set_sensor_onboarding_state("sensor-aabbccddeeff", "onboarding")
        again = enrolled_sensor(self.store, "sensor-aabbccddeeff", "bare board")

        self.assertGreater(again["device_config_revision"], first["device_config_revision"])
        self.assertEqual(again["device_config_status"], "pending")

    def test_a_beacon_sensor_can_be_enrolled(self):
        self.ingestion.ingest(self.beacon())
        sensor = enrolled_sensor(self.store, "sensor-aabbccddeeff", "bare board")
        self.assertEqual(sensor["enrollment_status"], "enrolled")
        self.assertEqual(sensor["onboarding_state"], "onboarded")

    def test_a_partial_measurement_is_still_rejected(self):
        from open_plant_pulse_hub.ingestion.advertisement import Advertisement

        partial = Advertisement(
            received_at="2026-09-24T18:00:00Z",
            local_name="sensor-aabbccddeeff",
            observed_identifier="aa:bb:cc:dd:ee:ff",
            rssi=-48,
            # Air temperature without humidity: a broken reading, not a beacon.
            service_data=bytes([0x40, 0x00, 1, 0x45, 0x10, 0x09]),
            source_adapter="test",
        )
        self.assertEqual(self.ingestion.ingest(partial), "rejected")


class ConfigurationRetryRateTests(unittest.TestCase):
    """Each attempt asks the operating system to pair, so it must be rare."""

    def test_the_retry_floor_is_far_slower_than_the_beacon_cadence(self):
        from open_plant_pulse_hub.ingestion.ble import CONFIGURATION_RETRY_SECONDS

        # An unclaimed sensor beacons every three seconds; without a floor every
        # one of those produced a pairing dialog.
        self.assertGreaterEqual(CONFIGURATION_RETRY_SECONDS, 10.0)

    def test_retries_are_not_keyed_on_the_advertisement_payload(self):
        source = (
            Path(__file__).parents[1]
            / "src/open_plant_pulse_hub/ingestion/ble.py"
        ).read_text()
        # Every beacon carries a new packet ID, so comparing payloads never
        # matches and the rate limit would never apply.
        self.assertNotIn(
            "self._last_configuration_attempt.get(sensor_id)\n                            != advertisement.service_data",
            source,
        )
        self.assertIn("CONFIGURATION_RETRY_SECONDS", source)


class ReleaseProtocolTests(unittest.TestCase):
    def test_the_release_payload_is_distinct_from_every_other(self):
        from open_plant_pulse_hub.ingestion.device_configuration import (
            DEVICE_RELEASE_PAYLOAD,
            decode_device_configuration,
        )

        self.assertEqual(DEVICE_RELEASE_PAYLOAD, bytes((5, 0x5A)))
        # Two bytes, and a version nothing else uses, so it cannot be mistaken
        # for a configuration or an acknowledgement.
        with self.assertRaises(ValueError):
            decode_device_configuration(DEVICE_RELEASE_PAYLOAD)

    def test_the_station_status_suffix_is_separated_from_the_configuration(self):
        from open_plant_pulse_hub.ingestion.device_configuration import (
            DeviceConfiguration,
            encode_device_configuration,
            split_station_status,
        )

        config = encode_device_configuration(
            DeviceConfiguration(revision=3, reporting_interval_seconds=1800,
                                plant_name="fern", room="Study")
        )
        suffix = bytes((0xA1, 1, 192, 168, 0, 111))
        body, station = split_station_status(config + suffix)
        self.assertEqual(body, config)
        self.assertEqual(station, (True, "192.168.0.111"))

        # A hub reading a sensor that sends no suffix still gets its config.
        body, station = split_station_status(config)
        self.assertEqual(body, config)
        self.assertIsNone(station)


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

    def test_rooms_round_trip_over_the_api(self):
        with self.request(
            "/api/rooms",
            method="POST",
            payload={"name": "Study", "aspect": "north", "light": "low", "notes": "Small windows"},
        ) as response:
            created = json.load(response)
        self.assertEqual(created["name"], "Study")
        with self.request("/api/rooms") as response:
            self.assertEqual([r["name"] for r in json.load(response)["items"]], ["Study"])
        with self.request(
            "/api/rooms/%d" % created["room_id"],
            method="PUT",
            payload={"name": "Back study", "aspect": "east", "light": "medium"},
        ) as response:
            self.assertEqual(json.load(response)["name"], "Back study")
        self.request("/api/rooms/%d" % created["room_id"], method="DELETE").close()
        with self.request("/api/rooms") as response:
            self.assertEqual(json.load(response)["items"], [])

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

    def test_only_pairable_sensors_are_offered(self):
        script = self.get("/onboarding.js")
        # A UDP simulator has no radio, and a sensor last heard days ago is not
        # in the room; offering either fails at pairing with nothing to show why.
        self.assertIn(b'sensor.transport === "bthome"', script)
        self.assertIn(b"ONBOARDING_CANDIDATE_MAX_AGE_SECONDS", script)
        # Freshness is when the device was last heard, not when it last measured:
        # a sensor beaconing for adoption has no measurement to be fresh about.
        self.assertIn(b"sensor.seen_age_seconds", script)
        # Switching a sensor off must remove it while somebody is still looking,
        # so the window is a few missed beacons rather than minutes.
        self.assertIn(b"ONBOARDING_CANDIDATE_MAX_AGE_SECONDS = 10", script)

    def test_the_candidate_list_updates_as_beacons_arrive(self):
        script = self.get("/onboarding.js")
        self.assertIn(b"describeLastHeard", script)
        self.assertIn(b"heard just now", script)

    def test_step_one_reports_the_real_scanner_state(self):
        page = self.get("/")
        script = self.get("/onboarding.js")
        self.assertIn(b'id="onboarding-scan-state"', page)
        self.assertIn(b'class="scan-pulse"', page)
        # It must be able to say the scanner is NOT running; an indicator that
        # always animates would hide exactly the fault it exists to surface.
        self.assertIn(b"is not running", script)
        self.assertIn(b'status === "scanning"', script)

    def test_the_scan_pulse_respects_reduced_motion(self):
        self.assertIn(b"prefers-reduced-motion", self.get("/app.css"))

    def test_the_last_step_keeps_working_after_it_is_reached(self):
        script = self.get("/onboarding.js")
        # The console has to join and a reading has to arrive, both slower than
        # a person reads the page, so the summary re-reads the live sensor.
        self.assertIn(b"fleetSensors.find", script)
        self.assertIn(b"Waiting for the sensor to report", script)
        self.assertIn(b"Web console reachable", script)

    def test_a_reading_must_arrive_after_setup_to_count_as_the_first(self):
        script = self.get("/onboarding.js")
        # An older reading from a previous life of the same sensor must not be
        # reported as the one this setup just produced.
        self.assertIn(b"onboardingFinishedAt", script)
        self.assertIn(b"Date.parse(latest.received_at) >= onboardingFinishedAt", script)

    def test_reporting_in_completes_setup_even_without_a_probe(self):
        script = self.get("/onboarding.js")
        # Setup proves the sensor reached the hub. A sensor announcing itself
        # with nothing to measure has done that; a missing probe is a note, not
        # a failed setup.
        self.assertIn(b"no measurements yet", script)
        self.assertIn(b'label: "Reporting"', script)
        self.assertIn(b"Nothing heard from the sensor", script)
        self.assertNotIn(b'label: "No probe"', script)

    def test_the_finished_step_no_longer_promises_a_pairing_code(self):
        page = self.get("/")
        self.assertNotIn(b"You will need its pairing code again", page)
        self.assertIn(b"refuses every other hub", page)

    def test_the_aspect_choices_cover_the_compass(self):
        page = self.get("/")
        for value in (b'"north_east"', b'"south_west"', b'"several"', b'"none"'):
            self.assertIn(value, page)
        # Half the compass is not enough to judge how much light a plant gets.
        self.assertIn(b"North-east", page)
        self.assertIn(b"No windows", page)

    def test_the_rooms_tab_is_reachable_by_its_own_address(self):
        self.assertIn(b'"/settings/rooms"', self.get("/app.js"))

    def test_the_room_is_picked_rather_than_typed(self):
        page = self.get("/")
        script = self.get("/onboarding.js")
        self.assertIn(b'<select id="onboarding-room"', page)
        self.assertIn(b'id="settings-rooms-panel"', page)
        self.assertIn(b"renderRoomChoices", script)
        self.assertIn(b"room_id:", script)

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
