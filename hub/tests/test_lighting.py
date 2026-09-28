from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from open_plant_pulse_hub.application import AdvertisementIngestionService, ReadingStore
from open_plant_pulse_hub.ingestion.replay import AdvertisementReplay
from open_plant_pulse_hub.lighting.driver import FakeLight, LightDriver
from open_plant_pulse_hub.lighting.neewer_gl1c import (
    NeewerGL1C,
    beacon_mac,
    colour_frame,
    frame,
    handshake_frame,
    power_frame,
)
from open_plant_pulse_hub.lighting.schedule import in_window, validate_schedule
from open_plant_pulse_hub.lighting.service import DRIVERS, LightService
from open_plant_pulse_hub.lighting.wemo_switch import WemoSwitch, parse_binary_state
from open_plant_pulse_hub.web import create_server, server_address


FIXTURE_PATH = (
    Path(__file__).parents[2] / "protocol" / "fixtures" / "bthome-v3-replay.json"
)


def at(hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return datetime(2026, 9, 27, int(hours), int(minutes))


class Gl1cProtocolTests(unittest.TestCase):
    def test_frames_match_the_bytes_the_light_answers(self):
        self.assertEqual(frame(0x04).hex(" "), "80 04 00 84")
        self.assertEqual(frame(0x06, bytes([0x01])).hex(" "), "80 06 01 01 88")
        self.assertEqual(power_frame(True).hex(" "), "80 05 02 01 01 89")
        self.assertEqual(power_frame(False).hex(" "), "80 05 02 01 00 88")

    def test_the_grow_preset_is_brightness_then_cct_then_tint(self):
        # Verified against the physical light; nothing on the wire can catch a
        # reversed order, so the bytes are pinned here.
        self.assertEqual(colour_frame(100, 4200, 0).hex(" "), "80 05 04 02 64 2a 32 4b")

    def test_colour_values_are_clamped_to_what_the_light_takes(self):
        self.assertEqual(colour_frame(150, 9000, 80)[3:7], bytes([0x02, 100, 70, 100]))
        self.assertEqual(colour_frame(-5, 1000, -80)[3:7], bytes([0x02, 0, 29, 0]))

    def test_the_handshake_carries_our_address(self):
        packet = handshake_frame("192.168.0.231")
        self.assertEqual(packet[:6], bytes([0x80, 0x02, 0x10, 0x00, 0x00, 0x0D]))
        self.assertEqual(packet[6:-1], b"192.168.0.231")

    def test_the_beacon_mac_is_read_from_its_tlvs(self):
        # Built to the documented layout: tag, length, value records after the
        # header, the model string among them and the 6-byte MAC after it.
        body = bytes([0x80, 0x01, 0x0E, 0x01, 0x04]) + b"GL1C" + bytes([0x02, 0x06])
        body += bytes.fromhex("f36c5ecff69e")
        packet = body + bytes([sum(body) & 0xFF])
        self.assertEqual(beacon_mac(packet), "f3:6c:5e:cf:f6:9e")

    def test_packets_that_are_not_a_gl1c_beacon_yield_nothing(self):
        self.assertIsNone(beacon_mac(frame(0x03)))
        self.assertIsNone(beacon_mac(frame(0x01, bytes([0x02, 0x06]) + bytes(6))))

    def test_settings_are_checked_and_defaulted(self):
        self.assertEqual(
            NeewerGL1C.validate_config({"mac": "F3-6C-5E-CF-F6-9E"}),
            {"mac": "f3:6c:5e:cf:f6:9e", "brightness": 100, "cct": 4200, "tint": 0},
        )
        with self.assertRaises(ValueError):
            NeewerGL1C.validate_config({"cct": 2000})
        with self.assertRaises(ValueError):
            NeewerGL1C.validate_config({"colour": "red"})

    def test_an_offline_light_is_never_reported_on(self):
        light = NeewerGL1C({})
        self.assertEqual(light.state().power, None)
        self.assertFalse(light.state().online)
        self.assertEqual(light.set_power(True).outcome, "unreachable")


class FakeSwitch:
    """A Wemo that answers on one port, or on none."""

    def __init__(self, port=49152, power=False):
        self.port = port
        self.power = power
        self.calls = []
        self.set_replies_error = False

    def post(self, url, body, action):
        port = int(url.split(":")[2].split("/")[0])
        self.calls.append((port, action))
        if port != self.port:
            raise OSError("connection refused")
        if action == "SetBinaryState":
            wanted = b"<BinaryState>1</BinaryState>" in body
            already = wanted == self.power
            self.power = wanted
            state = "Error" if already else str(int(wanted))
            return f"<s:Envelope><BinaryState>{state}</BinaryState></s:Envelope>"
        return f"<s:Envelope><BinaryState>{int(self.power)}|0|0</BinaryState></s:Envelope>"


class WemoTests(unittest.TestCase):
    def test_binary_state_takes_the_field_before_any_bar(self):
        self.assertTrue(parse_binary_state("<BinaryState>1|1700000000|0</BinaryState>"))
        self.assertFalse(parse_binary_state("<BinaryState>0</BinaryState>"))
        self.assertIsNone(parse_binary_state("<BinaryState>Error</BinaryState>"))

    def test_the_ports_are_walked_and_the_answering_one_remembered(self):
        switch = FakeSwitch(port=49155)
        driver = WemoSwitch({"host": "192.168.0.208"}, post=switch.post)
        self.assertFalse(driver.refresh())
        self.assertEqual([port for port, _ in switch.calls], [49153, 49152, 49154, 49155])
        self.assertEqual(driver.config_updates(), {"last_port": 49155})
        switch.calls.clear()
        driver.refresh()
        self.assertEqual([port for port, _ in switch.calls], [49155])

    def test_a_set_is_confirmed_by_reading_back_not_by_its_reply(self):
        switch = FakeSwitch(power=True)
        driver = WemoSwitch({"host": "192.168.0.208", "last_port": 49152}, post=switch.post)
        # The switch answers Error to a set it is already in; that is still on.
        result = driver.set_power(True)
        self.assertEqual(result.outcome, "confirmed")
        self.assertEqual(switch.calls[-1], (49152, "GetBinaryState"))
        self.assertTrue(driver.state().power)

    def test_a_switch_that_never_answers_is_unreachable_after_two_walks(self):
        switch = FakeSwitch(port=1)
        driver = WemoSwitch({"host": "192.168.0.208"}, post=switch.post)
        self.assertEqual(driver.set_power(False).outcome, "unreachable")
        self.assertEqual(len(switch.calls), 8)
        self.assertFalse(driver.state().online)
        self.assertIsNone(driver.state().power)

    def test_the_address_is_required(self):
        with self.assertRaises(ValueError):
            WemoSwitch.validate_config({})
        with self.assertRaises(ValueError):
            WemoSwitch.validate_config({"host": "wemo.local"})


class WindowTests(unittest.TestCase):
    def test_on_is_lit_and_off_is_dark(self):
        self.assertTrue(in_window("08:00", "21:30", at("08:00")))
        self.assertTrue(in_window("08:00", "21:30", at("21:29")))
        self.assertFalse(in_window("08:00", "21:30", at("21:30")))
        self.assertFalse(in_window("08:00", "21:30", at("07:59")))

    def test_a_window_may_wrap_midnight(self):
        self.assertTrue(in_window("20:00", "06:00", at("23:00")))
        self.assertTrue(in_window("20:00", "06:00", at("05:59")))
        self.assertFalse(in_window("20:00", "06:00", at("06:00")))
        self.assertFalse(in_window("20:00", "06:00", at("12:00")))

    def test_an_empty_or_malformed_window_is_refused(self):
        with self.assertRaises(ValueError):
            validate_schedule({"enabled": True, "on": "08:00", "off": "08:00"})
        with self.assertRaises(ValueError):
            validate_schedule({"enabled": True, "on": "8:00", "off": "21:30"})
        with self.assertRaises(ValueError):
            validate_schedule({"enabled": "yes", "on": "08:00", "off": "21:30"})


def enroll(store, sensor_id, name):
    return store.manage_sensor(sensor_id, name, "", "strelitzia", None, None, 1800)


class LightStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_ids = [item["sensor_id"] for item in self.store.sensors("unclaimed")]
        enroll(self.store, self.sensor_ids[0], "Strelitzia")
        enroll(self.store, self.sensor_ids[1], "Monstera")

    def tearDown(self):
        self.store.close()

    def test_a_plant_starts_with_the_default_window_unsaved(self):
        lighting = self.store.plant_lighting(self.sensor_ids[0])
        self.assertEqual(lighting["light_ids"], [])
        self.assertEqual(lighting["schedule"], {"enabled": True, "on": "08:00", "off": "21:30"})
        self.assertFalse(lighting["schedule_saved"])

    def test_lights_and_schedule_are_saved_together(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        lighting = self.store.set_plant_lighting(
            self.sensor_ids[0], ["light-a"], {"enabled": True, "on": "07:00", "off": "19:00"}
        )
        self.assertEqual(lighting["light_ids"], ["light-a"])
        self.assertTrue(lighting["schedule_saved"])
        self.assertEqual(self.store.light("light-a")["plant"]["display_name"], "Strelitzia")

    def test_a_light_belongs_to_one_plant(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        schedule = {"enabled": True, "on": "07:00", "off": "19:00"}
        self.store.set_plant_lighting(self.sensor_ids[0], ["light-a"], schedule)
        with self.assertRaisesRegex(ValueError, "already lights Strelitzia"):
            self.store.set_plant_lighting(self.sensor_ids[1], ["light-a"], schedule)
        # Letting go of it frees it for another plant.
        self.store.set_plant_lighting(self.sensor_ids[0], [], schedule)
        self.store.set_plant_lighting(self.sensor_ids[1], ["light-a"], schedule)
        self.assertEqual(self.store.light("light-a")["plant"]["display_name"], "Monstera")

    def test_only_an_enrolled_plant_can_have_lights(self):
        with self.assertRaises(ValueError):
            self.store.plant_lighting(self.sensor_ids[2])

    def test_light_names_are_unique(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        with self.assertRaises(ValueError):
            self.store.create_light("light-b", "Neewer", "wemo-switch", {"host": "10.0.0.2"})

    def test_deleting_the_plant_lets_go_of_its_lights_and_schedule(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        self.store.set_plant_lighting(
            self.sensor_ids[0], ["light-a"], {"enabled": True, "on": "07:00", "off": "19:00"}
        )
        self.store.delete_sensor(self.sensor_ids[0])
        self.assertIsNone(self.store.light("light-a")["plant"])
        self.assertEqual(self.store.light_assignments(), {})

    def test_the_schedule_survives_replacing_the_sensor(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        self.store.set_plant_lighting(
            self.sensor_ids[0], ["light-a"], {"enabled": False, "on": "07:00", "off": "19:00"}
        )
        self.store.replace_sensor(self.sensor_ids[0], self.sensor_ids[2])
        lighting = self.store.plant_lighting(self.sensor_ids[2])
        self.assertEqual(lighting["light_ids"], ["light-a"])
        self.assertEqual(lighting["schedule"], {"enabled": False, "on": "07:00", "off": "19:00"})

    def test_learned_facts_are_merged_into_the_settings(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {"cct": 4200})
        self.store.remember_light_facts("light-a", {"last_ip": "192.168.0.118"})
        self.assertEqual(self.store.light("light-a")["config"], {"cct": 4200, "last_ip": "192.168.0.118"})

    def test_the_event_log_is_bounded(self):
        self.store.create_light("light-a", "Neewer", "neewer-gl1c", {})
        for _ in range(205):
            self.store.record_light_event("light-a", "schedule", True, "confirmed", None)
        self.assertEqual(len(self.store.light_events("light-a", 500)), 200)


class Clock:
    def __init__(self):
        self.monotonic = 1000.0
        self.wall = at("12:00")

    def __call__(self):
        return self.monotonic

    def now(self):
        return self.wall


class LightingCareLogTests(unittest.TestCase):
    """Switching a light is part of a plant's history, and the record of it."""

    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enroll(self.store, self.sensor_id, "Strelitzia")
        self.store.create_light("light-a", "Lamp A", "wemo-switch", {})
        self.store.create_light("light-b", "Lamp B", "wemo-switch", {})
        self.store.set_plant_lighting(
            self.sensor_id, ["light-a", "light-b"],
            {"enabled": True, "on": "08:00", "off": "21:30"},
        )

    def tearDown(self):
        self.store.close()

    def switch(self, light_id, at, wanted, outcome="confirmed", source="schedule"):
        """Write the event the hub writes, dated, so hours can be checked."""
        self.store._database.execute(
            """
            INSERT INTO care_events (event_id, sensor_id, kind, detected_at, title,
                                     summary, confidence, changes_json)
            VALUES (?, ?, 'lighting', ?, 'x', 'y', 'high', ?)
            """,
            (light_id + at, self.sensor_id, at,
             json.dumps({"light_id": light_id, "light": "Lamp", "wanted": wanted,
                         "source": source, "outcome": outcome})),
        )
        self.store._database.commit()

    def test_a_switched_light_appears_in_the_plants_care_log(self):
        self.store.record_light_event("light-a", "schedule", True, "confirmed", None)

        entry = self.store.care_log(10, self.sensor_id)[0]
        self.assertEqual(entry["kind"], "lighting")
        self.assertEqual(entry["title"], "Lamp A on")
        self.assertIn("daily schedule", entry["summary"])
        self.assertEqual(entry["confidence"], "high")
        self.assertEqual(entry["changes"]["light_id"], "light-a")
        self.assertTrue(entry["changes"]["wanted"])

    def test_a_light_nobody_could_reach_says_so(self):
        self.store.record_light_event("light-a", "schedule", True, "unreachable", "timed out")

        entry = self.store.care_log(10, self.sensor_id)[0]
        self.assertIn("could not be reached", entry["summary"])
        self.assertEqual(entry["confidence"], "low")

    def test_a_light_no_plant_owns_writes_nothing(self):
        self.store.create_light("light-loose", "Spare", "wemo-switch", {})
        before = len(self.store.care_log(50, self.sensor_id))

        self.store.record_light_event("light-loose", "manual", True, "confirmed", None)

        self.assertEqual(len(self.store.care_log(50, self.sensor_id)), before)

    def test_lifetime_hours_count_the_time_the_plant_was_lit(self):
        # Lamp A from 08:00 to 12:00 and Lamp B from 10:00 to 14:00 is six hours
        # of light, not eight: this is how long the plant was lit.
        self.switch("light-a", "2026-09-20T08:00:00Z", True)
        self.switch("light-b", "2026-09-20T10:00:00Z", True)
        self.switch("light-a", "2026-09-20T12:00:00Z", False)
        self.switch("light-b", "2026-09-20T14:00:00Z", False)

        self.assertEqual(self.store.lighting_hours(self.sensor_id), 6.0)
        self.assertEqual(self.store.plant_journey(self.sensor_id)["lighting_hours"], 6.0)

    def test_the_watchdog_holding_a_light_on_does_not_restart_the_clock(self):
        self.switch("light-a", "2026-09-21T08:00:00Z", True)
        self.switch("light-a", "2026-09-21T09:00:00Z", True, source="watchdog")
        self.switch("light-a", "2026-09-21T10:00:00Z", False)

        self.assertEqual(self.store.lighting_hours(self.sensor_id), 2.0)

    def test_an_instruction_the_light_never_answered_counts_for_nothing(self):
        # Being told to come on is not being on, and counting it would inflate
        # the one number somebody uses to judge whether a plant gets enough.
        self.switch("light-a", "2026-09-22T08:00:00Z", True, outcome="unreachable")
        self.switch("light-a", "2026-09-22T12:00:00Z", False, outcome="unreachable")

        self.assertEqual(self.store.lighting_hours(self.sensor_id), 0.0)

    def test_a_light_still_on_counts_up_to_now(self):
        started = datetime.now(timezone.utc) - timedelta(hours=3)
        self.switch("light-a", started.strftime("%Y-%m-%dT%H:%M:%SZ"), True)

        self.assertAlmostEqual(self.store.lighting_hours(self.sensor_id), 3.0, delta=0.2)

    def test_a_plant_that_has_never_had_a_light_reports_none(self):
        self.assertEqual(self.store.lighting_hours(self.sensor_id), 0.0)
        self.assertEqual(self.store.plant_journey(self.sensor_id)["lighting_hours"], 0.0)


class SchedulerTests(unittest.TestCase):
    """The rules gl1cd ran the lights by, now applied per plant."""

    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_ids = [item["sensor_id"] for item in self.store.sensors("unclaimed")]
        enroll(self.store, self.sensor_ids[0], "Strelitzia")
        enroll(self.store, self.sensor_ids[1], "Monstera")
        self.clock = Clock()
        self.service = self.make_service()
        self.light_id = self.service.add_light("Panel", "fake", {})["light_id"]
        self.schedule(True, "08:00", "21:30")

    def tearDown(self):
        self.service.stop()
        self.store.close()

    def make_service(self, watchdog=True):
        return LightService(
            self.store,
            {"fake": FakeLight},
            watchdog=watchdog,
            threaded=False,
            clock=self.clock,
            now=self.clock.now,
        )

    @property
    def light(self) -> FakeLight:
        return self.service._lights[self.light_id].driver

    def schedule(self, enabled, on, off, sensor=0, light_ids=None):
        self.store.set_plant_lighting(
            self.sensor_ids[sensor],
            [self.light_id] if light_ids is None else light_ids,
            {"enabled": enabled, "on": on, "off": off},
        )
        self.service.lighting_changed()

    def tick(self, hhmm=None):
        if hhmm:
            self.clock.wall = at(hhmm)
        self.service.evaluate(self.light_id)

    def test_starting_inside_the_window_turns_the_light_on(self):
        self.tick("12:00")
        self.assertEqual(self.light.commands, [True])
        self.assertEqual(self.store.light_events(self.light_id)[0]["source"], "schedule")

    def test_starting_outside_the_window_turns_the_light_off(self):
        self.tick("22:00")
        self.assertEqual(self.light.commands, [False])

    def test_a_light_already_right_is_left_alone(self):
        self.light.power = True
        self.tick("12:00")
        self.assertEqual(self.light.commands, [])

    def test_each_edge_is_acted_on_once(self):
        self.tick("21:00")
        self.tick("21:29")
        self.tick("21:30")
        self.tick("21:31")
        self.assertEqual(self.light.commands, [True, False])

    def test_drift_is_corrected_between_edges(self):
        self.tick("12:00")
        self.light.power = False  # someone pressed the light's own button
        self.tick("12:01")
        self.assertEqual(self.light.commands, [True, True])
        self.assertEqual(self.store.light_events(self.light_id)[0]["source"], "watchdog")

    def test_drift_is_left_alone_after_a_manual_command(self):
        self.tick("12:00")
        self.service.set_power(self.light_id, False)
        self.tick("12:30")
        self.assertEqual(self.light.commands, [True, False])
        self.clock.monotonic += 3601
        self.tick("13:31")
        self.assertEqual(self.light.commands, [True, False, True])

    def test_drift_is_left_alone_when_the_watchdog_is_off(self):
        self.service.stop()
        self.service = self.make_service(watchdog=False)
        self.service.start()
        self.tick("12:00")
        self.light.power = False
        self.tick("12:01")
        self.assertEqual(self.light.commands, [True])

    def test_an_edge_acts_even_during_a_manual_override(self):
        self.tick("12:00")
        self.service.set_power(self.light_id, True)
        self.tick("21:30")
        self.assertEqual(self.light.commands, [True, True, False])

    def test_a_disabled_schedule_leaves_the_light_alone(self):
        self.schedule(False, "08:00", "21:30")
        self.tick("12:00")
        self.assertEqual(self.light.commands, [])

    def test_an_unreachable_light_is_not_commanded_but_the_wait_is_shown(self):
        self.light.online = False
        self.tick("12:00")
        self.assertEqual(self.light.commands, [])
        self.assertIn("scheduled on is waiting", self.service.light(self.light_id)["alert"])
        self.light.online = True
        self.tick("12:01")
        self.assertEqual(self.light.commands, [True])
        self.assertIsNone(self.service.light(self.light_id)["alert"])

    def test_an_unconfirmed_edge_is_retried_next_tick(self):
        self.light.obeys = False
        self.tick("12:00")
        self.assertIn("was not confirmed", self.service.light(self.light_id)["alert"])
        self.light.obeys = True
        self.tick("12:00")
        self.assertEqual(self.light.commands, [True, True])
        self.assertIsNone(self.service.light(self.light_id)["alert"])

    def test_a_light_back_from_offline_drops_the_manual_override(self):
        self.tick("12:00")
        self.service.set_power(self.light_id, False)
        self.light.online = False
        self.tick("12:05")
        self.light.online = True
        self.tick("12:10")
        self.assertEqual(self.light.commands, [True, False, True])

    def test_a_light_without_a_plant_is_only_switched_by_hand(self):
        self.schedule(True, "08:00", "21:30", light_ids=[])
        self.tick("12:00")
        self.assertEqual(self.light.commands, [])
        self.service.set_power(self.light_id, True)
        self.assertEqual(self.light.commands, [True])

    def test_an_archived_plant_no_longer_drives_its_lights(self):
        self.store.set_sensor_archived(self.sensor_ids[0], True)
        self.tick("12:00")
        self.assertEqual(self.light.commands, [])

    def test_a_light_moved_to_another_plant_follows_that_plant(self):
        self.tick("12:00")
        self.schedule(True, "08:00", "21:30", light_ids=[])
        self.schedule(True, "13:00", "20:00", sensor=1)
        self.tick("12:30")
        self.assertEqual(self.light.commands, [True, False])

    def test_an_edited_schedule_applies_at_once(self):
        self.tick("12:00")
        self.schedule(True, "13:00", "20:00")
        self.tick("12:00")
        self.assertEqual(self.light.commands, [True, False])

    def test_learned_facts_are_kept(self):
        self.light.learn(last_seen="here")
        self.tick("12:00")
        self.assertEqual(self.store.light(self.light_id)["config"], {"last_seen": "here"})


class DriverRegistryTests(unittest.TestCase):
    def test_the_real_drivers_are_offered_and_the_simulated_one_is_not(self):
        self.assertEqual(set(DRIVERS), {"neewer-gl1c", "wemo-switch"})
        for driver in DRIVERS.values():
            self.assertTrue(issubclass(driver, LightDriver))


class LightApiTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadingStore()
        AdvertisementReplay(AdvertisementIngestionService(self.store)).replay(FIXTURE_PATH)
        self.sensor_id = self.store.sensors("unclaimed")[0]["sensor_id"]
        enroll(self.store, self.sensor_id, "Strelitzia")
        self.lights = LightService(self.store, {"fake": FakeLight, **DRIVERS}, threaded=False)
        self.lights.start()
        self.server = create_server(self.store, "127.0.0.1", 0, lights=self.lights)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server_address(self.server)
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.lights.stop()
        self.store.close()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return urlopen(request, timeout=5)

    def call(self, path, method="GET", payload=None):
        with self.request(path, method, payload) as response:
            return json.load(response) if response.status != 204 else None

    def refused(self, path, method, payload=None):
        with self.assertRaises(HTTPError) as raised:
            self.request(path, method, payload)
        raised.exception.close()
        return raised.exception.code

    def test_the_add_form_is_described_by_the_drivers(self):
        kinds = {item["kind"]: item for item in self.call("/api/light-drivers")["items"]}
        self.assertEqual(kinds["wemo-switch"]["fields"][0]["name"], "host")
        self.assertIn("colour", kinds["neewer-gl1c"]["capabilities"])

    def test_a_light_is_added_renamed_switched_and_removed(self):
        light = self.call("/api/lights", "POST", {"display_name": "Bench", "driver": "fake"})
        light_id = light["light_id"]
        self.assertEqual(light["state"], {"online": True, "power": None, "detail": None})

        renamed = self.call(f"/api/lights/{light_id}", "PUT", {"display_name": "Shelf", "config": {}})
        self.assertEqual(renamed["display_name"], "Shelf")

        switched = self.call(f"/api/lights/{light_id}/power", "POST", {"on": True})
        self.assertEqual(switched["result"]["outcome"], "confirmed")
        self.assertTrue(switched["state"]["power"])
        self.assertGreater(switched["override_seconds_left"], 3500)
        self.assertEqual(switched["events"][0]["source"], "manual")

        self.call(f"/api/lights/{light_id}", "DELETE")
        self.assertEqual(self.call("/api/lights")["items"], [])
        self.assertEqual(self.refused(f"/api/lights/{light_id}", "GET"), 404)

    def test_bad_settings_are_refused(self):
        self.assertEqual(
            self.refused("/api/lights", "POST", {"display_name": "Sansi", "driver": "wemo-switch", "config": {}}),
            400,
        )
        self.assertEqual(
            self.refused("/api/lights", "POST", {"display_name": "X", "driver": "lava-lamp"}), 400
        )

    def test_a_plant_picks_its_lights_and_schedule(self):
        light_id = self.call("/api/lights", "POST", {"display_name": "Bench", "driver": "fake"})["light_id"]
        path = f"/api/sensors/{quote(self.sensor_id)}/lighting"
        before = self.call(path)
        self.assertFalse(before["schedule_saved"])
        self.assertEqual([light["light_id"] for light in before["lights"]], [light_id])

        saved = self.call(
            path, "PUT", {"light_ids": [light_id], "schedule": {"enabled": True, "on": "07:30", "off": "19:00"}}
        )
        self.assertEqual(saved["light_ids"], [light_id])
        self.assertEqual(saved["lights"][0]["plant"]["sensor_id"], self.sensor_id)
        self.assertEqual(saved["lights"][0]["schedule"]["on"], "07:30")

        self.assertEqual(
            self.refused(path, "PUT", {"light_ids": [light_id], "schedule": {"enabled": True, "on": "07:30", "off": "07:30"}}),
            400,
        )

    def test_a_cross_origin_switch_is_refused(self):
        light_id = self.call("/api/lights", "POST", {"display_name": "Bench", "driver": "fake"})["light_id"]
        request = Request(
            f"{self.base_url}/api/lights/{light_id}/power",
            data=json.dumps({"on": True}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=5)
        raised.exception.close()
        self.assertEqual(raised.exception.code, 400)

    def test_the_plant_page_can_switch_its_lights(self):
        with self.request("/lights.js") as response:
            script = response.read()
        self.assertIn(b"lighting-toggle", script)
        # A light that cannot be reached is not offered a switch that would
        # appear to work.
        self.assertIn(b"!light.state.online", script)
        # One switch beside Adjust lighting for all of them, which only asks
        # the lights that are not already where it is going.
        with self.request("/sensors/" + quote(self.sensor_id)) as response:
            self.assertIn(b'id="plant-lighting-all"', response.read())
        self.assertIn(b"lightIsOn(light) !== on", script)

    def test_the_lights_tab_is_a_page_of_its_own(self):
        with self.request("/settings/lights") as response:
            self.assertIn(b'id="settings-lights-panel"', response.read())


if __name__ == "__main__":
    unittest.main()
