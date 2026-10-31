"""Holding the plant box at a humidity with the printer's exhaust fan."""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from open_plant_pulse_hub.application.airflow import (
    FanService, HumidityController, KlipperFan)
from open_plant_pulse_hub.application.store import ReadingStore


class FakeFan:
    """A printer that answers, with whatever state a test wants.

    The service builds a fan on each tick, so a test watching across
    ticks hands the same one back through `factory`.
    """

    def __init__(self, url: str = "", name: str = "") -> None:
        self.sent: List[float] = []
        self.reachable = True
        self.printing = False
        self.print_state = "complete"
        self.speed = 0.0

    def state(self) -> Dict[str, Any]:
        return {"reachable": self.reachable, "speed": self.speed,
                "printing": self.printing, "print_state": self.print_state}

    def set_speed(self, speed: float) -> bool:
        self.sent.append(round(speed, 4))
        self.speed = speed
        return True

    def factory(self):
        return lambda *args: self


def gains(**over) -> Dict[str, Any]:
    base = {"target_rh": 65.0, "kp": 0.05, "ki": 0.002, "kd": 0.0,
            "min_speed": 0.05, "max_speed": 1.0}
    base.update(over)
    return base


class ControllerTests(unittest.TestCase):
    """The arithmetic, with no printer and no store in the way."""

    def setUp(self) -> None:
        self.pid = HumidityController()

    def test_a_dry_box_does_not_run_the_fan(self) -> None:
        self.assertEqual(self.pid.step(55.0, 0.0, gains()), 0.0)

    def test_a_humid_box_runs_it(self) -> None:
        # Ten points over: 0.05 * 10 = 0.5 before any integral.
        self.assertAlmostEqual(self.pid.step(75.0, 0.0, gains()), 0.5, places=3)

    def test_the_integral_builds_while_it_stays_humid(self) -> None:
        first = self.pid.step(70.0, 0.0, gains())
        later = self.pid.step(70.0, 600.0, gains())      # ten minutes on
        self.assertGreater(later, first)

    def test_dt_comes_from_the_readings_not_the_clock(self) -> None:
        """A loop waking every minute must not integrate the same
        measurement sixty times an hour."""
        quick = HumidityController()
        quick.step(70.0, 0.0, gains())
        quick.step(70.0, 60.0, gains())
        slow = HumidityController()
        slow.step(70.0, 0.0, gains())
        slow.step(70.0, 600.0, gains())
        self.assertGreater(slow.integral, quick.integral)

    def test_a_long_gap_is_not_integrated_as_time(self) -> None:
        """An absence is not a dt."""
        self.pid.step(70.0, 0.0, gains())
        self.pid.step(70.0, 86_400.0, gains())           # a day later
        self.assertLess(self.pid.integral, 0.002 * 5 * 16)

    def test_the_integral_does_not_wind_up_against_the_ceiling(self) -> None:
        """An exhaust fan cannot beat humid outside air, and an
        integral that climbs all evening takes hours to unwind."""
        for minute in range(0, 600, 10):
            self.pid.step(95.0, minute * 60.0, gains())
        wound = self.pid.integral
        for minute in range(600, 1200, 10):
            self.pid.step(95.0, minute * 60.0, gains())
        self.assertAlmostEqual(self.pid.integral, wound, places=6)

    def test_it_comes_back_down_promptly_once_the_box_dries(self) -> None:
        for minute in range(0, 600, 10):
            self.pid.step(95.0, minute * 60.0, gains())
        speed = 1.0
        for minute in range(600, 900, 10):
            speed = self.pid.step(50.0, minute * 60.0, gains())
        self.assertEqual(speed, 0.0)

    def test_it_asks_for_what_is_wanted_not_what_the_fan_can_do(self) -> None:
        """The floor and the pulsing belong to the fan. This says how
        much air is wanted and nothing about how to deliver it."""
        self.assertAlmostEqual(self.pid.step(65.5, 0.0, gains(min_speed=0.25)),
                               0.025, places=3)

    def test_nothing_wanted_stops_it(self) -> None:
        """At or under target, which is the only thing that stops it."""
        self.assertEqual(self.pid.step(64.0, 0.0, gains(min_speed=0.25)), 0.0)

    def test_it_runs_on_until_the_box_is_back_to_target(self) -> None:
        """Hysteresis without a second setting: it runs while the box
        is over and stops when it is not."""
        self.pid.step(72.0, 0.0, gains(min_speed=0.25))
        over = self.pid.step(66.0, 600.0, gains(min_speed=0.25))
        under = self.pid.step(63.0, 1200.0, gains(min_speed=0.25))
        self.assertGreater(over, 0.0)
        self.assertEqual(under, 0.0)

    def test_it_never_exceeds_the_ceiling(self) -> None:
        self.assertLessEqual(self.pid.step(100.0, 0.0, gains(max_speed=0.6)), 0.6)

    def test_the_derivative_is_off_unless_asked_for(self) -> None:
        """Two readings minutes apart, each rounded to a tenth, are
        mostly noise."""
        self.pid.step(70.0, 0.0, gains())
        without = self.pid.step(72.0, 600.0, gains())
        other = HumidityController()
        other.step(70.0, 0.0, gains(kd=0.5))
        with_d = other.step(72.0, 600.0, gains(kd=0.5))
        self.assertNotAlmostEqual(without, with_d, places=3)

    def test_the_derivative_is_taken_on_the_reading_not_the_error(self) -> None:
        """Moving the target should not kick the fan."""
        steady = HumidityController()
        steady.step(70.0, 0.0, gains(kd=0.5))
        same = steady.step(70.0, 600.0, gains(kd=0.5, target_rh=60.0))
        other = HumidityController()
        other.step(70.0, 0.0, gains(kd=0.5))
        moved = other.step(70.0, 600.0, gains(kd=0.5, target_rh=60.0))
        self.assertAlmostEqual(same, moved, places=6)


class ServiceTests(unittest.TestCase):
    """What the loop does with a printer and a sensor in front of it."""

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.sensor = "zigbee-a4c1381ed574ffff"
        self.store.seen_zigbee_device("a4c1381ed574ffff", "thermometer")
        self.store.enroll_zigbee_device("a4c1381ed574ffff", "vzbot_thermometer")
        self.store.set_fan_control(enabled=True, mode="auto",
                                   sensor_id=self.sensor, target_rh=65)
        self.fan = FakeFan()
        self.service = FanService(self.store, fan_factory=self.fan.factory())

    def _say(self, rh: float, minutes_ago: float = 0.0) -> None:
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.record_air_reading(self.sensor, 25.0, rh, observed_at=at)

    def test_a_humid_box_gets_the_fan(self) -> None:
        self._say(80.0)
        state = self.service.tick()
        self.assertGreater(self.fan.sent[-1], 0)
        self.assertTrue(state["acted"])

    def test_it_stands_down_while_the_printer_prints(self) -> None:
        """The print owns that fan; some of them set it themselves."""
        self._say(80.0)
        self.fan.printing = True
        self.fan.print_state = "printing"
        state = self.service.tick()
        self.assertEqual(self.fan.sent, [])
        self.assertIn("printing", state["why"])

    def test_an_old_reading_turns_the_fan_off(self) -> None:
        """Acting on this morning's humidity is guessing."""
        self._say(80.0, minutes_ago=90)
        state = self.service.tick()
        self.assertEqual(self.fan.sent[-1], 0.0)
        self.assertIn("old", state["why"])

    def test_an_unreachable_printer_is_said_plainly(self) -> None:
        self._say(80.0)
        self.fan.reachable = False
        state = self.service.tick()
        self.assertIn("cannot be reached", state["why"])

    def test_nothing_happens_while_it_is_off(self) -> None:
        self.store.set_fan_control(mode="off")
        self._say(90.0)
        self.service.tick()
        self.assertEqual(self.fan.sent, [])

    def test_manual_sets_the_speed_and_leaves_it(self) -> None:
        self.store.set_fan_control(mode="manual", manual_speed=0.3)
        self._say(90.0)
        self.service.tick()
        self.assertEqual(self.fan.sent, [0.3])

    def test_the_same_reading_twice_is_not_two_decisions(self) -> None:
        """The loop wakes every half minute; the box is measured every
        few. Stepping on the clock would integrate one measurement
        over and over."""
        self._say(80.0)
        self.service.tick()
        sent = len(self.fan.sent)
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.fan.sent), sent)

    def test_it_says_what_it_is_doing(self) -> None:
        self._say(80.0)
        self.service.tick()
        state = self.service.state()
        self.assertEqual(state["rh"], 80.0)
        self.assertEqual(state["target_rh"], 65.0)


class FanAddressTests(unittest.TestCase):
    def test_the_gcode_is_the_one_a_person_would_type(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "open_plant_pulse_hub"
                  / "application" / "airflow.py").read_text()
        self.assertIn("SET_FAN_SPEED FAN={self._fan} SPEED={speed:.3f}", source)


if __name__ == "__main__":
    unittest.main()


class ShowingTheBoxTests(unittest.TestCase):
    """What the box is doing, and whether the hub is acting on it,
    are two different questions.

    The humidity was read only on the way to a decision, so with the
    control switched off the page said "--" about a box that was being
    measured perfectly well.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.sensor = "zigbee-a4c1381ed574ffff"
        self.store.seen_zigbee_device("a4c1381ed574ffff", "thermometer")
        self.store.enroll_zigbee_device("a4c1381ed574ffff", "vzbot_thermometer")
        self.store.set_fan_control(sensor_id=self.sensor)
        self.fan = FakeFan()
        self.service = FanService(self.store, fan_factory=self.fan.factory())
        self.store.record_air_reading(self.sensor, 25.0, 58.0)

    def test_the_humidity_shows_while_the_control_is_off(self) -> None:
        self.store.set_fan_control(mode="off", enabled=False)
        self.assertEqual(self.service.tick()["rh"], 58.0)

    def test_it_shows_while_set_by_hand(self) -> None:
        self.store.set_fan_control(mode="manual", enabled=True, manual_speed=0.2)
        self.assertEqual(self.service.tick()["rh"], 58.0)

    def test_it_shows_while_the_printer_is_printing(self) -> None:
        self.store.set_fan_control(mode="auto", enabled=True)
        self.fan.printing = True
        self.fan.print_state = "printing"
        self.assertEqual(self.service.tick()["rh"], 58.0)

    def test_switching_off_does_not_touch_the_fan(self) -> None:
        """Off means the hub keeps its hands off, not that it sets
        the fan to nothing."""
        self.store.set_fan_control(mode="off", enabled=False)
        self.service.tick()
        self.assertEqual(self.fan.sent, [])


class AlternatingReportTests(unittest.TestCase):
    """These thermometers send temperature and humidity separately.

    About half the newest rows have no humidity in them, so a
    controller reading only the newest row decided the sensor had
    stopped and switched the fan off every other minute.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.sensor = "zigbee-a4c1381ed574ffff"
        self.store.seen_zigbee_device("a4c1381ed574ffff", "thermometer")
        self.store.enroll_zigbee_device("a4c1381ed574ffff", "vzbot_thermometer")
        self.store.set_fan_control(enabled=True, mode="auto",
                                   sensor_id=self.sensor, target_rh=65)
        self.fan = FakeFan()
        self.service = FanService(self.store, fan_factory=self.fan.factory())

    def test_a_temperature_only_row_does_not_blind_it(self) -> None:
        self.store.record_air_reading(self.sensor, None, 80.0)
        self.store.record_air_reading(self.sensor, 26.0, None)
        state = self.service.tick()
        self.assertEqual(state["rh"], 80.0)
        self.assertNotIn("no humidity", state["why"])

    def test_a_sensor_that_never_reported_humidity_is_said_plainly(self) -> None:
        self.store.record_air_reading(self.sensor, 26.0, None)
        self.assertIn("no humidity", self.service.tick()["why"])


class PulsingTests(unittest.TestCase):
    """A fan whose slowest speed is bigger than the dose wanted.

    Measured on the real box: a minute at 14% took 5.5 points out,
    and it is only ever 2 or 3 points over target. A quarter speed --
    the slowest this fan turns without buzzing -- held for a whole
    minute would take out ten. No gain fixes that: the answer is to
    run the fan at its floor for part of a window and leave it off
    for the rest.
    """

    def setUp(self) -> None:
        self.store = ReadingStore(database_path=":memory:")
        self.sensor = "zigbee-a4c1381ed574ffff"
        self.store.seen_zigbee_device("a4c1381ed574ffff", "thermometer")
        self.store.enroll_zigbee_device("a4c1381ed574ffff", "vzbot_thermometer")
        self.store.set_fan_control(enabled=True, mode="auto", sensor_id=self.sensor,
                                   target_rh=65, min_speed=0.25, pulse_window_s=120)
        self.fan = FakeFan()
        self.service = FanService(self.store, fan_factory=self.fan.factory())

    def _say(self, rh: float) -> None:
        self.store.record_air_reading(self.sensor, 25.0, rh)

    def test_a_small_demand_pulses_rather_than_running_on(self) -> None:
        self._say(67.0)                      # two points over: asks 0.10
        self.service.tick()
        # It starts at the floor, which is the only speed it can turn.
        self.assertEqual(self.fan.sent[-1], 0.25)
        # Two points over at Kp 0.05 is 0.1, which is 40% of a 0.25
        # floor, so the fan should be off for most of the window.
        self.service._pulse_began -= 60      # 60s into a 120s window
        self.assertEqual(self.service._pulse(self.store.fan_control()), 0.0)

    def test_a_demand_the_fan_can_hold_is_held(self) -> None:
        self._say(85.0)                      # twenty over: asks 1.0
        self.service.tick()
        self.assertGreaterEqual(self.fan.sent[-1], 0.25)
        self.assertIsNone(self.service._pulse_began)

    def test_the_average_over_a_window_is_what_was_asked(self) -> None:
        """That is the whole point of pulsing."""
        self.service._demand = 0.10
        settings = self.store.fan_control()
        self.service._pulse(settings)
        on = 0
        for second in range(0, 120, 5):
            self.service._pulse_began = time.time() - second
            if self.service._pulse(settings) > 0:
                on += 5
        average = 0.25 * on / 120
        self.assertAlmostEqual(average, 0.10, delta=0.02)

    def test_nothing_wanted_stops_the_pulse(self) -> None:
        self.service._demand = 0.0
        self.assertEqual(self.service._pulse(self.store.fan_control()), 0.0)
        self.assertIsNone(self.service._pulse_began)

    def test_the_printer_is_not_asked_for_a_speed_it_already_has(self) -> None:
        """The loop wakes every ten seconds; without this the printer
        would be told the same thing six times a minute for ever."""
        self._say(85.0)
        self.service.tick()
        sent = len(self.fan.sent)
        for _ in range(5):
            self.service.tick()
        self.assertEqual(len(self.fan.sent), sent)
