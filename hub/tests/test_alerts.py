"""A leak has to reach a phone, once, and quickly."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from open_plant_pulse_hub.application.alerts import Alerts
from open_plant_pulse_hub.application.store import ReadingStore
from open_plant_pulse_hub.application.zigbee.drivers import LeakDriver
from open_plant_pulse_hub.application.zigbee.network import Reading
from open_plant_pulse_hub.application.zigbee.service import ZigbeeService


class FakeNetwork:
    """Enough of a radio for the service to attach itself to."""

    def __init__(self) -> None:
        self.on_reading_handler = None
        self.sent: List[Any] = []

    def on_reading(self, handler) -> None:
        self.on_reading_handler = handler

    def on_joined(self, handler) -> None:
        pass

    def on_delivery(self, handler) -> None:
        pass

    def devices(self):
        return []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, title, message, *, urgent=False, tags="", block=False):
        self.sent.append({"title": title, "message": message, "urgent": urgent})


class RecordingSounder:
    def __init__(self) -> None:
        self.started: List[Dict[str, Any]] = []
        self.stopped: List[int] = []

    def start(self, rule_id, sound, every_seconds):
        self.started.append({"rule_id": rule_id, "sound": sound,
                             "every_seconds": every_seconds})

    def stop(self, rule_id):
        self.stopped.append(rule_id)


class LeakAlarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.network = FakeNetwork()
        self.alerts = RecordingAlerts()
        self.sounder = RecordingSounder()
        self.eui = "f044d3fffef8217f"
        self.store.seen_zigbee_device(self.eui, "leak")
        self.store.enroll_zigbee_device(self.eui, "leak")
        self.store.add_alert_rule(f"zigbee-{self.eui}", "wet", "true",
                                  sound="Submarine", repeat_seconds=20)
        self.service = ZigbeeService(self.store, self.network,
                                     alerts=self.alerts, sounder=self.sounder)

    def _say(self, **values) -> None:
        self.network.on_reading_handler(
            Reading(eui=self.eui, kind="leak", values=values))

    def test_water_reaches_the_phone_and_the_room(self) -> None:
        self._say(wet=True)
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn("Water detected", self.alerts.sent[0]["title"])
        self.assertTrue(self.alerts.sent[0]["urgent"])
        self.assertEqual(self.sounder.started[0]["sound"], "Submarine")
        self.assertEqual(self.sounder.started[0]["every_seconds"], 20)

    def test_it_is_sent_once_however_often_the_sensor_repeats(self) -> None:
        """These sensors say it again every time they check in, and a
        phone that buzzes every few minutes until somebody gets home is
        a phone that gets silenced."""
        for _ in range(5):
            self._say(wet=True)
        self.assertEqual(len(self.alerts.sent), 1)

    def test_drying_out_stops_the_sound(self) -> None:
        """Nobody should have to press stop for a leak that dried."""
        self._say(wet=True)
        self._say(wet=False)
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn(1, self.sounder.stopped)

    def test_a_dry_sensor_saying_so_first_is_not_an_event(self) -> None:
        self._say(wet=False)
        self.assertEqual(self.alerts.sent, [])

    def test_a_battery_report_is_not_an_alarm(self) -> None:
        self._say(batt_pct=100)
        self.assertEqual(self.alerts.sent, [])

    def test_the_message_names_the_sensor(self) -> None:
        self.store.seen_zigbee_device(self.eui, "leak")
        self.store.enroll_zigbee_device(self.eui, "Kitchen leak")
        self._say(wet=True)
        self.assertIn("leak", self.alerts.sent[0]["message"])


class AlertSendingTests(unittest.TestCase):
    def test_an_unconfigured_hub_sends_nothing_and_does_not_raise(self) -> None:
        alerts = Alerts(None)
        self.assertFalse(alerts.configured)
        alerts.send("Water detected", "somewhere", urgent=True)

    def test_an_address_can_be_changed_while_running(self) -> None:
        alerts = Alerts(None)
        alerts.set_url("https://ntfy.sh/somewhere")
        self.assertTrue(alerts.configured)

    def test_a_title_outside_ascii_does_not_break_the_header(self) -> None:
        """A header is latin-1 at best, and an alarm must not fail on
        the one character somebody put in a room name."""
        alerts = Alerts("http://127.0.0.1:1/never")
        alerts.send("Water detected 漏水", "body", block=True)


class LeakDecodingTests(unittest.TestCase):
    """The bytes a SONOFF SNZB-05P actually sends."""

    def test_a_zone_status_change_says_wet(self) -> None:
        # Cluster-specific, Zone Status Change Notification, ALARM1 set.
        frame = bytes([0x09, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00])
        values = LeakDriver.decode(1280, frame)
        self.assertEqual(values["wet"], True)

    def test_a_cleared_zone_says_dry(self) -> None:
        frame = bytes([0x09, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.assertEqual(LeakDriver.decode(1280, frame)["wet"], False)

    def test_a_low_battery_bit_is_carried(self) -> None:
        frame = bytes([0x09, 0x03, 0x00, 0x09, 0x00, 0x00, 0x00])
        values = LeakDriver.decode(1280, frame)
        self.assertEqual((values["wet"], values["battery_low"]), (True, True))


if __name__ == "__main__":
    unittest.main()


class ThresholdRuleTests(unittest.TestCase):
    """A rule on a number, on a device that is not a leak sensor.

    This is the whole point of the rules table: the hard-coded version
    could only ever alarm on the one thing written into the service.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.network = FakeNetwork()
        self.alerts = RecordingAlerts()
        self.sounder = RecordingSounder()
        self.eui = "a4c13815bb584ebb"
        self.store.seen_zigbee_device(self.eui, "thermometer")
        self.store.enroll_zigbee_device(self.eui, "Bedroom")
        self.service = ZigbeeService(self.store, self.network,
                                     alerts=self.alerts, sounder=self.sounder)

    def _rule(self, **fields) -> int:
        defaults = {"measurement": "air_temperature_c", "test": "below",
                    "threshold": 16.0, "sound": "Basso"}
        return self.store.add_alert_rule(
            f"zigbee-{self.eui}", **{**defaults, **fields})["rule_id"]

    def _say(self, **values) -> None:
        self.network.on_reading_handler(
            Reading(eui=self.eui, kind="thermometer", values=values))

    def test_a_cold_room_rings(self) -> None:
        self._rule()
        self._say(air_temperature_c=15.4)
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn("15.4", self.alerts.sent[0]["title"])
        self.assertEqual(self.sounder.started[0]["sound"], "Basso")

    def test_a_warm_room_says_nothing(self) -> None:
        self._rule()
        self._say(air_temperature_c=19.0)
        self.assertEqual(self.alerts.sent, [])

    def test_it_rings_once_while_the_room_stays_cold(self) -> None:
        self._rule()
        for degrees in (15.4, 15.1, 14.8, 15.0):
            self._say(air_temperature_c=degrees)
        self.assertEqual(len(self.alerts.sent), 1)

    def test_a_reading_resting_on_the_threshold_does_not_ring_twice(self) -> None:
        """16.0, 15.9, 16.0, 15.9 is a thermometer doing its job, not
        four separate emergencies."""
        self._rule()
        for degrees in (15.9, 16.1, 15.9, 16.1, 15.9):
            self._say(air_temperature_c=degrees)
        self.assertEqual(len(self.alerts.sent), 1)

    def test_above_and_below_are_different_rules(self) -> None:
        self._rule(test="below", threshold=16.0)
        self._rule(test="above", threshold=28.0)
        self._say(air_temperature_c=30.0)
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn("above", self.alerts.sent[0]["title"])

    def test_humidity_needs_no_new_code(self) -> None:
        self._rule(measurement="rh_pct", test="above", threshold=75.0)
        self._say(rh_pct=80.0)
        self.assertEqual(len(self.alerts.sent), 1)

    def test_a_battery_going_flat_can_be_alarmed_on(self) -> None:
        """An alarm device with a dead cell is the failure nobody notices."""
        self._rule(measurement="batt_pct", test="below", threshold=20.0)
        self._say(batt_pct=12)
        self.assertEqual(len(self.alerts.sent), 1)

    def test_a_disabled_rule_is_silent(self) -> None:
        rule_id = self._rule()
        self.store.update_alert_rule(rule_id, enabled=False)
        self._say(air_temperature_c=10.0)
        self.assertEqual(self.alerts.sent, [])

    def test_a_measurement_the_device_did_not_send_is_not_judged(self) -> None:
        self._rule()
        self._say(rh_pct=50)
        self.assertEqual(self.alerts.sent, [])

    def test_nonsense_in_a_reading_does_not_raise(self) -> None:
        self._rule()
        self._say(air_temperature_c=None)
        self._say(air_temperature_c="warm")
        self.assertEqual(self.alerts.sent, [])

    def test_a_rule_that_only_sounds_does_not_push(self) -> None:
        self._rule(push=False)
        self._say(air_temperature_c=10.0)
        self.assertEqual(self.alerts.sent, [])
        self.assertEqual(len(self.sounder.started), 1)

    def test_warming_up_again_stops_the_sound(self) -> None:
        rule_id = self._rule(repeat_seconds=30)
        self._say(air_temperature_c=15.0)
        self._say(air_temperature_c=18.0)
        self.assertIn(rule_id, self.sounder.stopped)


class RuleStoreTests(unittest.TestCase):
    """What can be saved, and what the migration carried over."""

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.store.seen_zigbee_device("aabbccddeeff0011", "leak")
        self.store.enroll_zigbee_device("aabbccddeeff0011", "leak")
        self.sensor = "zigbee-aabbccddeeff0011"

    def test_a_rule_that_does_nothing_is_refused(self) -> None:
        """Saving one would read as the feature not working."""
        with self.assertRaises(ValueError):
            self.store.add_alert_rule(self.sensor, "wet", "true",
                                      sound=None, push=False)

    def test_a_sound_this_computer_lacks_is_refused(self) -> None:
        """Otherwise the alarm fails silently, discovered at the worst
        possible time."""
        with self.assertRaises(ValueError):
            self.store.add_alert_rule(self.sensor, "wet", "true",
                                      sound="Klaxon")

    def test_above_without_a_number_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_alert_rule(self.sensor, "temp_c", "above",
                                      sound="Ping")

    def test_sounding_faster_than_the_floor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_alert_rule(self.sensor, "wet", "true",
                                      sound="Ping", repeat_seconds=1)

    def test_a_rule_on_an_unknown_sensor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_alert_rule("zigbee-nothing", "wet", "true",
                                      sound="Ping")

    def test_measurements_are_read_off_the_kept_reports(self) -> None:
        self.store.record_zigbee_report("aabbccddeeff0011",
                                        {"wet": False, "batt_pct": 90,
                                         "payload": "not a measurement"},
                                        kind="leak")
        seen = self.store.measurements_seen(self.sensor)
        self.assertEqual(seen, ["batt_pct", "wet"])


class SoundSpeedTests(unittest.TestCase):
    """A button that lags is a button people press twice.

    `afplay` spawns a process, opens CoreAudio and tears it down: 1.7
    seconds measured, for a sound lasting half of one. Played through
    AppKit inside the hub it was quick but came out broken up -- a
    threading HTTP server with a radio loop in it is not what AppKit
    expects to be asked from. So the sound lives in a helper process
    with a run loop of its own: about 30ms, and whole.
    """

    def setUp(self) -> None:
        here = Path(__file__).parents[1] / "src" / "open_plant_pulse_hub" / "application"
        self.alerts = (here / "alerts.py").read_text()
        self.helper = (here / "_sound_helper.py").read_text()

    def test_sounds_go_through_the_helper(self) -> None:
        self.assertIn("def _ask_helper", self.alerts)
        self.assertIn("_sound_helper.py", self.alerts)

    def test_afplay_remains_the_way_out(self) -> None:
        """A machine without PyObjC should still make a noise."""
        self.assertIn("afplay", self.alerts)

    def test_appkit_is_not_used_inside_the_hub(self) -> None:
        """This is what made the sound stutter."""
        self.assertNotIn("from AppKit", self.alerts)

    def test_the_helper_owns_a_run_loop(self) -> None:
        self.assertIn("NSRunLoop", self.helper)

    def test_the_helper_opens_the_audio_device_before_it_is_needed(self) -> None:
        """Opening it costs a third of a second on the first play,
        whenever that is. It should not be the first alarm."""
        self.assertIn("setVolume_(0.0)", self.helper)

    def test_a_sound_already_playing_is_rewound(self) -> None:
        """play() on something already sounding is ignored, which
        would swallow every repeat of an alarm."""
        self.assertIn("isPlaying()", self.helper)


class SoundHelperTests(unittest.TestCase):
    """The helper itself, started the way the hub starts it."""

    def test_it_comes_up_and_plays_on_request(self) -> None:
        import subprocess
        script = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub"
                  / "application" / "_sound_helper.py")
        helper = subprocess.Popen([sys.executable, str(script)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        try:
            self.assertEqual(helper.stdout.readline().strip(), "ready")
            helper.stdin.write("Tink\n")
            helper.stdin.flush()
            self.assertEqual(helper.stdout.readline().strip(), "ok Tink")
            # A sound this machine does not have is said so, not guessed at.
            helper.stdin.write("Klaxon\n")
            helper.stdin.flush()
            self.assertEqual(helper.stdout.readline().strip(), "no Klaxon")
        finally:
            helper.stdin.close()
            helper.wait(timeout=5)


class TestingARuleTests(unittest.TestCase):
    """Firing a rule on purpose, before the day it matters."""

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.network = FakeNetwork()
        self.alerts = RecordingAlerts()
        self.sounder = RecordingSounder()
        self.sounder.play = lambda sound: self.sounder.started.append(
            {"rule_id": None, "sound": sound, "every_seconds": 0}) or True
        self.eui = "f044d3fffef8217f"
        self.store.seen_zigbee_device(self.eui, "leak")
        self.store.enroll_zigbee_device(self.eui, "leak")
        self.rule = self.store.add_alert_rule(
            f"zigbee-{self.eui}", "wet", "true",
            sound="Submarine", repeat_seconds=20)["rule_id"]
        self.service = ZigbeeService(self.store, self.network,
                                     alerts=self.alerts, sounder=self.sounder)

    def test_a_test_sounds_once_whatever_the_repeat_says(self) -> None:
        """A test that had to be stopped would be a worse button than
        no button."""
        result = self.service.test_rule(self.rule)
        self.assertTrue(result["sounded"])
        self.assertEqual(self.sounder.started[0]["every_seconds"], 0)

    def test_a_test_says_so_in_the_message(self) -> None:
        self.service.test_rule(self.rule)
        self.assertTrue(self.alerts.sent[0]["title"].startswith("Test:"))

    def test_an_unknown_rule_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.test_rule(9999)

    def test_testing_does_not_arm_the_rule(self) -> None:
        """A test must not leave the hub thinking the sensor is wet,
        or the real alarm afterwards would be swallowed as a repeat."""
        self.service.test_rule(self.rule)
        self.alerts.sent.clear()
        self.network.on_reading_handler(
            Reading(eui=self.eui, kind="leak", values={"wet": True}))
        self.assertEqual(len(self.alerts.sent), 1)
