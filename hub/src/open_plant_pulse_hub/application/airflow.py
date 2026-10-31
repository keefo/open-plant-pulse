"""Holding one box at one humidity, with the fan it already has.

The room called `vzbot` is a 3D-printer enclosure used as a plant box.
Sealed, it runs to 86 % RH; the begonias in it want 55-75 %, and the
way down is the enclosure's exhaust fan, driven by hand today with
`SET_FAN_SPEED FAN=Exhaust_fan SPEED=0.1`. A hand on a fan is somebody
watching a number the hub already has.

Self-contained on purpose. One module, one settings row, one page in
Settings; nothing else in the hub learns about fans, and a room keeps
meaning a room.

Three things about this control problem shape the code more than the
arithmetic does, and each is explained where it happens:

  - the measurement arrives every few minutes, so the loop steps on a
    reading rather than on a timer
  - the derivative term is mostly noise at that sample rate, so it is
    off unless somebody turns it up
  - the fan does not start at 0.01, so there is a floor and the
    integral is held whenever the output is against a limit
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib import error, parse, request

LOG = logging.getLogger(__name__)

# Long enough for a printer on wifi, short enough that a loop waking
# once a minute is never two calls deep.
TIMEOUT_SECONDS = 6
# A print owns the fan. These are Klipper's words for "busy".
PRINTING_STATES = ("printing", "paused")


class KlipperFan:
    """The exhaust fan on a Klipper printer, over Moonraker.

    Two calls: ask what the fan and the printer are doing, and set a
    speed. The speed goes through the same gcode a person would type,
    because that is the interface the printer documents and the one
    the user already uses by hand.
    """

    def __init__(self, base_url: str, fan_name: str = "Exhaust_fan") -> None:
        self._base = (base_url or "").rstrip("/")
        self._fan = fan_name

    def _ask(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            with request.urlopen(f"{self._base}{path}",
                                 timeout=TIMEOUT_SECONDS) as answer:
                return json.loads(answer.read().decode("utf-8"))
        except (error.URLError, OSError, ValueError, json.JSONDecodeError):
            return None

    def state(self) -> Dict[str, Any]:
        """What the fan is doing and whether the printer is busy.

        `reachable` is false rather than raising: a printer that is off
        is an ordinary thing for a plant hub to find, and the page
        should say so rather than show an error.
        """
        query = parse.quote(f"fan_generic {self._fan}", safe="")
        answered = self._ask(f"/printer/objects/query?{query}&print_stats")
        if answered is None:
            return {"reachable": False, "speed": None, "printing": False,
                    "print_state": None}
        status = (answered.get("result") or {}).get("status") or {}
        fan = status.get(f"fan_generic {self._fan}") or {}
        prints = status.get("print_stats") or {}
        state = prints.get("state")
        return {
            "reachable": True,
            "speed": fan.get("speed"),
            "printing": state in PRINTING_STATES,
            "print_state": state,
        }

    def set_speed(self, speed: float) -> bool:
        """Set the fan, the way a person would type it."""
        speed = max(0.0, min(1.0, float(speed)))
        script = parse.quote(
            f"SET_FAN_SPEED FAN={self._fan} SPEED={speed:.3f}", safe="")
        try:
            post = request.Request(
                f"{self._base}/printer/gcode/script?script={script}",
                data=b"", method="POST")
            with request.urlopen(post, timeout=TIMEOUT_SECONDS) as answer:
                return 200 <= answer.status < 300
        except (error.URLError, OSError, ValueError):
            LOG.warning("could not set %s to %.3f", self._fan, speed)
            return False


class HumidityController:
    """A PID over humidity, stepped once per reading.

    The error is positive when the box is too humid, and the output is
    fan speed, so more error means more fan and the signs need no
    inverting anywhere.
    """

    def __init__(self) -> None:
        self._integral = 0.0
        self._last_rh: Optional[float] = None
        self._last_at: Optional[float] = None

    def reset(self) -> None:
        """Forget the past. Used when the loop stops controlling, so
        the integral does not carry over a gap it knows nothing about."""
        self._integral = 0.0
        self._last_rh = None
        self._last_at = None

    @property
    def integral(self) -> float:
        return self._integral

    def step(self, rh: float, at: float, settings: Dict[str, Any]) -> float:
        """One new reading in, one fan speed out.

        `at` is when the reading was taken, not when this ran: the gap
        between readings is the dt the integral needs, and a loop
        waking every minute must not integrate the same measurement
        sixty times an hour.
        """
        target = float(settings["target_rh"])
        error_now = rh - target
        minutes = 0.0
        if self._last_at is not None and at > self._last_at:
            minutes = (at - self._last_at) / 60.0
        # A gap longer than a few readings is not a dt, it is an
        # absence, and integrating across it would be inventing time.
        minutes = min(minutes, 15.0)

        proportional = float(settings["kp"]) * error_now

        derivative = 0.0
        if settings["kd"] and self._last_rh is not None and minutes > 0:
            # On the measurement, not the error: changing the target
            # should not kick the fan.
            derivative = -float(settings["kd"]) * (rh - self._last_rh) / minutes

        # The integral is added first, then held back if the result is
        # against a limit -- an exhaust fan cannot beat humid outside
        # air, and without this the integral would climb all evening
        # and take hours to unwind afterwards.
        trial = self._integral + float(settings["ki"]) * error_now * minutes
        output = proportional + trial + derivative
        highest = float(settings["max_speed"])
        if 0.0 < output < highest or (output <= 0 and trial > 0) or (
                output >= highest and trial < 0):
            self._integral = trial
        output = proportional + self._integral + derivative

        self._last_rh = rh
        self._last_at = at
        # How much air is wanted, not how this fan should deliver it.
        # The floor and the pulsing belong to the fan -- it cannot
        # turn below a quarter speed -- and keeping them out of here
        # means the arithmetic is about humidity and nothing else.
        #
        # Zero when nothing is wanted, which is at or under target
        # with the integral unwound. That is the hysteresis: the fan
        # runs while the box is over and stops when it is not.
        return max(0.0, min(highest, output))


class FanService:
    """Reads the sensor, decides, and tells the fan. Once a minute.

    Waking often and stepping rarely: the wake is for noticing that a
    print started or that the sensor has gone quiet, both of which
    want a quick hand off the fan. The control itself only moves when
    a reading it has not seen arrives.
    """

    # Ten seconds, because the fan is pulsed and a pulse cannot be
    # shorter than a tick. It is still only a wake: the control itself
    # moves when a reading arrives.
    TICK_SECONDS = 10

    def __init__(self, store: Any, fan_factory: Optional[Callable] = None) -> None:
        self._store = store
        self._make_fan = fan_factory or KlipperFan
        self._pid = HumidityController()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_at: Optional[float] = None
        # What the controller last asked for, held between readings,
        # and where we are in the pulse cycle that delivers it.
        self._demand = 0.0
        self._pulse_began: Optional[float] = None
        self._told: Optional[float] = None
        self._last: Dict[str, Any] = {"speed": None, "why": "not started",
                                      "rh": None, "at": None}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="fan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> Dict[str, Any]:
        """What it is doing and why, for the page to show."""
        with self._lock:
            return dict(self._last)

    def _run(self) -> None:
        while not self._stop.wait(self.TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                # A controller that dies quietly is worse than one
                # that misses a step: the fan would sit wherever it
                # was last told, for ever.
                LOG.exception("the fan loop stumbled")

    def tick(self) -> Dict[str, Any]:
        """One pass. Separated from the loop so a test can call it."""
        settings = self._store.fan_control()
        # Read whatever the mode is. This used to be read only on the
        # way to a decision, so with the control off the page said
        # "--" about a box that was being measured perfectly well.
        # What the box is doing and whether the hub is acting on it
        # are two different questions.
        reading = self._reading(settings)
        rh, at = reading if reading else (None, None)

        if not settings["enabled"] or settings["mode"] == "off":
            return self._note("off", None, settings, act=False, rh=rh, at=at)

        fan = self._make_fan(settings["moonraker_url"], settings["fan_name"])
        printer = fan.state()
        if not printer["reachable"]:
            return self._note("the printer cannot be reached", None, settings,
                              act=False, rh=rh, at=at)
        if printer["printing"]:
            # The print owns the fan; some of them set it themselves.
            self._pid.reset()
            return self._note(f"standing down, printer is {printer['print_state']}",
                              printer["speed"], settings, act=False, rh=rh, at=at)

        if settings["mode"] == "manual":
            self._pid.reset()
            speed = float(settings["manual_speed"])
            if printer["speed"] is None or abs(printer["speed"] - speed) > 0.001:
                fan.set_speed(speed)
            return self._note("set by hand", speed, settings, act=False,
                              rh=rh, at=at)

        if reading is None:
            self._pid.reset()
            self._demand = 0.0
            self._tell(fan, 0.0)
            return self._note("no humidity reading", 0.0, settings, act=False)
        old = time.time() - at
        if old > settings["stale_after_s"]:
            # Acting on an old number is guessing, and the fan goes
            # off rather than running on this morning's humidity.
            self._pid.reset()
            self._demand = 0.0
            self._tell(fan, 0.0)
            return self._note(f"the reading is {int(old / 60)} min old",
                              0.0, settings, act=False, rh=rh, at=at)

        if self._seen_at is not None and at <= self._seen_at:
            # Nothing new was measured, but the pulse still has to
            # be turned on and off between readings.
            speed = self._pulse(settings)
            self._tell(fan, speed)
            return self._note("holding", speed, settings, act=False, rh=rh, at=at)
        self._seen_at = at
        self._demand = self._pid.step(rh, at, settings)
        speed = self._pulse(settings)
        self._tell(fan, speed)
        return self._note(f"holding {settings['target_rh']:g}%", speed, settings,
                          act=True, rh=rh, at=at)

    def _pulse(self, settings: Dict[str, Any]) -> float:
        """Turn the demand into a speed this fan can actually turn at.

        The fan buzzes below about a quarter speed, and a quarter
        speed held for a minute takes ten points of humidity out of a
        box that is only ever two or three over. So a demand under the
        floor is delivered as the floor for part of a window and
        nothing for the rest -- the trick a slow PWM plays on a
        heater. A demand the fan can hold is simply held.
        """
        floor = float(settings["min_speed"])
        if self._demand <= 0:
            self._pulse_began = None
            return 0.0
        if self._demand >= floor:
            self._pulse_began = None
            return min(float(settings["max_speed"]), self._demand)
        window = max(20, int(settings["pulse_window_s"]))
        now = time.time()
        if self._pulse_began is None or now - self._pulse_began >= window:
            self._pulse_began = now
        # How much of the window the fan should be turning for, so the
        # average over it comes to what was asked.
        share = max(0.0, min(1.0, self._demand / floor))
        return floor if (now - self._pulse_began) < window * share else 0.0

    def _tell(self, fan: Any, speed: float) -> None:
        """Send a speed only when it is different from the last one.

        The loop wakes every ten seconds and the fan usually wants the
        same thing, so without this the printer would be asked to set
        a speed it is already at, six times a minute, for ever.
        """
        if self._told is not None and abs(self._told - speed) < 0.001:
            return
        if fan.set_speed(speed):
            self._told = speed

    def _reading(self, settings: Dict[str, Any]):
        """The newest humidity from the chosen sensor, and when.

        The newest humidity, not the newest reading: these
        thermometers send temperature and humidity in separate
        reports, so about half the latest rows have no humidity in
        them. Reading only the newest row had the controller deciding
        the sensor had stopped, and switching the fan off, every
        other minute.
        """
        sensor_id = settings.get("sensor_id")
        if not sensor_id:
            return None
        found = self._store.latest_humidity(sensor_id)
        if found is None:
            return None
        value, when = found
        try:
            taken = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            taken = time.time()
        return float(value), taken

    def _note(self, why: str, speed: Optional[float], settings: Dict[str, Any],
              act: bool, rh: Optional[float] = None,
              at: Optional[float] = None) -> Dict[str, Any]:
        state = {
            "why": why,
            "speed": speed,
            "rh": rh,
            "at": at,
            "target_rh": settings["target_rh"],
            "mode": settings["mode"],
            "integral": round(self._pid.integral, 4),
            "acted": act,
        }
        with self._lock:
            self._last = state
        # Written down so somebody can see afterwards whether this
        # worked. Only a change is a row; the store decides that.
        if speed is not None:
            try:
                self._store.record_fan(speed, rh, settings["mode"], why)
            except Exception:
                LOG.exception("could not write the fan down")
        return state
