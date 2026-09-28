"""Runs the hub's lights: one driver and one worker per light.

Each worker applies the rules gl1cd proved on the real lights, now to whichever
plant owns the light:

* act on every edge of the plant's window until the light confirms;
* between edges, correct drift, except for an hour after someone switched the
  light by hand;
* a light that comes back after being unreachable has most likely rebooted, so
  an earlier manual override no longer says what anyone wants.

Every light has its own worker, so a Wemo walking four hung ports for most of a
minute never delays another light.
"""
from datetime import datetime
import logging
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Type

from .driver import FakeLight, LightDriver, LightState, PowerResult
from .neewer_gl1c import NeewerGL1C
from .schedule import in_window
from .wemo_switch import WemoSwitch


LOG = logging.getLogger(__name__)
DRIVERS: Dict[str, Type[LightDriver]] = {
    NeewerGL1C.kind: NeewerGL1C,
    WemoSwitch.kind: WemoSwitch,
}
SIMULATED_DRIVERS: Dict[str, Type[LightDriver]] = {**DRIVERS, FakeLight.kind: FakeLight}
TICK_SECONDS = 10.0
MANUAL_OVERRIDE_SECONDS = 3600.0


class _Light:
    def __init__(self, light_id: str, driver: LightDriver) -> None:
        self.light_id = light_id
        self.driver = driver
        # One command at a time: the scheduler and a person must never
        # interleave on one device.
        self.command_lock = threading.Lock()
        # The wanted state last confirmed; None until the first edge is handled.
        self.last_wanted: Optional[bool] = None
        self.override_until = 0.0
        self.alert: Optional[str] = None
        self.was_online = False
        self.stopping = threading.Event()
        self.wake = threading.Event()


class LightService:
    def __init__(
        self,
        store: Any,
        drivers: Optional[Mapping[str, Type[LightDriver]]] = None,
        tick_seconds: float = TICK_SECONDS,
        override_seconds: float = MANUAL_OVERRIDE_SECONDS,
        watchdog: bool = True,
        threaded: bool = True,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._store = store
        self._drivers = dict(drivers if drivers is not None else DRIVERS)
        self._tick_seconds = tick_seconds
        self._override_seconds = override_seconds
        self._watchdog = watchdog
        self._threaded = threaded
        self._clock = clock
        self._now = now
        self._lights: Dict[str, _Light] = {}
        self._lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------
    def start(self) -> None:
        for light in self._store.lights():
            try:
                self._launch(light["light_id"], light["driver"], light["config"])
            except ValueError as error:
                LOG.warning("light %s not started: %s", light["display_name"], error)

    def stop(self) -> None:
        with self._lock:
            lights = list(self._lights.values())
            self._lights.clear()
        for light in lights:
            self._halt(light)

    # --- what the API asks ------------------------------------------------
    def driver_kinds(self) -> List[Dict[str, Any]]:
        return [driver.describe() for driver in self._drivers.values()]

    def lights(self) -> List[Dict[str, Any]]:
        assignments = self._store.light_assignments()
        return [self._present(light, assignments.get(light["light_id"])) for light in self._store.lights()]

    def light(self, light_id: str) -> Dict[str, Any]:
        light = self._store.light(light_id)
        if light is None:
            raise LookupError("light not found")
        return self._present(light, self._store.light_assignments().get(light_id))

    def add_light(self, display_name: str, driver: str, config: Any) -> Dict[str, Any]:
        driver_class = self._driver_class(driver)
        clean = driver_class.validate_config(config if config is not None else {})
        light_id = "light-" + secrets.token_hex(4)
        self._store.create_light(light_id, display_name, driver, clean)
        self._launch(light_id, driver, clean)
        return self.light(light_id)

    def update_light(self, light_id: str, display_name: str, config: Any) -> Dict[str, Any]:
        stored = self._store.light(light_id)
        if stored is None:
            raise LookupError("light not found")
        driver_class = self._driver_class(stored["driver"])
        clean = driver_class.validate_config(config if config is not None else {})
        # What the driver learned for itself (an address, a port) is not in the
        # form, and an edit of the settings must not throw it away.
        for name in driver_class.learned_fields:
            if name not in clean and name in stored["config"]:
                clean[name] = stored["config"][name]
        self._store.update_light(light_id, display_name, clean)
        if clean != stored["config"]:
            with self._lock:
                old = self._lights.pop(light_id, None)
            if old is not None:
                self._halt(old)
            self._launch(light_id, stored["driver"], clean)
        return self.light(light_id)

    def remove_light(self, light_id: str) -> None:
        """Forget a light. It is left in whatever state it is in."""
        if self._store.light(light_id) is None:
            raise LookupError("light not found")
        with self._lock:
            light = self._lights.pop(light_id, None)
        if light is not None:
            self._halt(light)
        self._store.delete_light(light_id)

    def set_power(self, light_id: str, on: bool) -> Dict[str, Any]:
        """Switch a light by hand, and leave it that way for a while."""
        light = self._running(light_id)
        light.override_until = self._clock() + self._override_seconds
        with light.command_lock:
            result = light.driver.set_power(on)
        self._record(light, "manual", on, result)
        if result.confirmed:
            light.alert = None
        self._keep_learned(light)
        return {**self.light(light_id), "result": {"outcome": result.outcome, "detail": result.detail}}

    def lighting_changed(self) -> None:
        """A schedule or a light's plant changed: apply it at once."""
        with self._lock:
            lights = list(self._lights.values())
        for light in lights:
            light.last_wanted = None
            light.wake.set()

    # --- the rules --------------------------------------------------------
    def evaluate(self, light_id: str) -> None:
        """One scheduler pass over one light."""
        with self._lock:
            light = self._lights.get(light_id)
        if light is None:
            return
        assignment = self._store.light_assignments().get(light_id)
        schedule = self._active_schedule(assignment)
        state = light.driver.state()
        if state.online and not light.was_online:
            # Back after being unreachable: most likely rebooted, so an earlier
            # manual choice no longer reflects what anyone wants.
            light.override_until = 0.0
        light.was_online = state.online
        if schedule is None:
            light.last_wanted = None
            light.alert = None
            return
        now = self._now()
        want = in_window(schedule["on"], schedule["off"], now)
        edge = want != light.last_wanted
        if not state.online:
            if edge:
                light.alert = (
                    f"{now:%H:%M} scheduled {'on' if want else 'off'} is waiting: "
                    f"{state.detail or 'the light is unreachable'}"
                )
            return
        if not edge:
            if not self._watchdog or self._clock() < light.override_until:
                return
            if state.power is None:
                return
        if state.power == want:
            light.last_wanted = want
            light.alert = None
            return
        with light.command_lock:
            result = light.driver.set_power(want)
        self._record(light, "schedule" if edge else "watchdog", want, result)
        if result.confirmed:
            light.last_wanted = want
            light.alert = None
        else:
            action = "scheduled" if edge else "watchdog"
            light.alert = (
                f"{now:%H:%M} {action} {'on' if want else 'off'} was not confirmed: "
                f"{result.detail or result.outcome}"
            )
        self._keep_learned(light)

    # --- internals --------------------------------------------------------
    def _driver_class(self, kind: str) -> Type[LightDriver]:
        driver_class = self._drivers.get(kind)
        if driver_class is None:
            raise ValueError(f"unknown light type {kind!r}")
        return driver_class

    def _launch(self, light_id: str, kind: str, config: Dict[str, Any]) -> None:
        driver = self._driver_class(kind)(config)
        light = _Light(light_id, driver)
        with self._lock:
            self._lights[light_id] = light
        driver.start()
        if self._threaded:
            threading.Thread(
                target=self._work, args=(light,), name=f"light-{light_id}", daemon=True
            ).start()

    def _halt(self, light: _Light) -> None:
        light.stopping.set()
        light.wake.set()
        try:
            light.driver.stop()
        except Exception:
            LOG.exception("stopping light %s failed", light.light_id)

    def _running(self, light_id: str) -> _Light:
        with self._lock:
            light = self._lights.get(light_id)
        if light is None:
            if self._store.light(light_id) is None:
                raise LookupError("light not found")
            raise ValueError("this light's driver is not running")
        return light

    def _work(self, light: _Light) -> None:
        while not light.stopping.is_set():
            try:
                self.evaluate(light.light_id)
                self._keep_learned(light)
            except Exception:
                # A worker that dies leaves a light unscheduled for good, so
                # nothing may end this loop but a stop.
                LOG.exception("light %s scheduler pass failed", light.light_id)
            light.wake.wait(self._tick_seconds)
            light.wake.clear()

    def _keep_learned(self, light: _Light) -> None:
        facts = light.driver.config_updates()
        if facts:
            self._store.remember_light_facts(light.light_id, facts)

    def _record(self, light: _Light, source: str, wanted: bool, result: PowerResult) -> None:
        log = LOG.info if result.confirmed else LOG.warning
        log(
            "light %s %s %s: %s%s",
            light.light_id,
            source,
            "on" if wanted else "off",
            result.outcome,
            f" ({result.detail})" if result.detail else "",
        )
        self._store.record_light_event(light.light_id, source, wanted, result.outcome, result.detail)

    @staticmethod
    def _active_schedule(assignment: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if assignment is None or assignment["archived"]:
            return None
        schedule = assignment["schedule"]
        if schedule is None or not schedule["enabled"]:
            return None
        return schedule

    def _present(self, stored: Dict[str, Any], assignment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            light = self._lights.get(stored["light_id"])
        driver_class = self._drivers.get(stored["driver"])
        if light is None:
            state = LightState(False, None, "this light's driver is not running")
            alert, override_left = None, 0
        else:
            try:
                state = light.driver.state()
            except Exception as error:  # a driver must never take the page down
                state = LightState(False, None, f"driver error: {error}")
            alert = light.alert
            override_left = max(0, int(light.override_until - self._clock()))
        schedule = self._active_schedule(assignment)
        return {
            **stored,
            "driver_label": driver_class.label if driver_class else stored["driver"],
            "capabilities": list(driver_class.capabilities) if driver_class else [],
            "state": {"online": state.online, "power": state.power, "detail": state.detail},
            "alert": alert,
            "override_seconds_left": override_left,
            "schedule": (
                {
                    **schedule,
                    "in_window": in_window(schedule["on"], schedule["off"], self._now()),
                }
                if schedule is not None
                else None
            ),
            "events": self._store.light_events(stored["light_id"], 5),
        }

