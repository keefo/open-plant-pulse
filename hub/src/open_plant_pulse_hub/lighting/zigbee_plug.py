"""A light switched at the socket by a Zigbee smart plug.

The same idea as the Wemo switch next door, over a different radio. The
plug is a device the hub already knows: it was paired, identified and
enrolled through the Zigbee pages, and this driver only borrows it.

So the plug is named by its EUI-64 rather than configured here. Nothing
about the radio, the channel or the network lives in a light's settings,
and removing the light leaves the plug exactly where it was.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, List

from .driver import (
    CONFIRMED,
    UNCONFIRMED,
    UNREACHABLE,
    ConfigField,
    LightDriver,
    LightState,
    PowerResult,
)


LOG = logging.getLogger(__name__)
# How long to wait for the plug to say what it did. A command and its
# answer are two frames over a mesh; a second is generous and a schedule
# that blocks for longer than that is a schedule nobody is watching.
CONFIRM_SECONDS = 2.5
CONFIRM_POLL_SECONDS = 0.15
# A plug that has not been heard from in this long is not vouched for.
# It reports when asked, and the service asks on every tick, so silence
# past a few minutes means the mesh and not a quiet device.
STALE_SECONDS = 600.0


# The hub's one Zigbee service, lent to this driver.
#
# A driver is built from its settings and nothing else, which is right
# for a device reached by address but leaves no way to reach a radio.
# Injected once at startup rather than threaded through every driver's
# constructor: one kind of light needs this and the other three do not.
_ZIGBEE: Any = None


def use_zigbee(service: Any) -> None:
    """Lends the Zigbee service to plug-driven lights."""
    global _ZIGBEE
    _ZIGBEE = service


def available_plugs() -> List[Dict[str, str]]:
    """The enrolled plugs, for the form to offer as choices.

    Only enrolled ones: a device that has merely joined the radio has
    not been accepted by anybody, and offering it here would be a second
    way to adopt a device, in a form about something else.
    """
    if _ZIGBEE is None:
        return []
    try:
        devices = _ZIGBEE.devices()
    except Exception:
        LOG.exception("could not list the Zigbee plugs")
        return []
    plugs = []
    for device in devices:
        if device.get("kind") != "plug" or not device.get("accepted"):
            continue
        made = " ".join(x for x in (device.get("manufacturer"), device.get("model")) if x)
        plugs.append({"value": device["eui"],
                      "label": device.get("name") or made or device["eui"]})
    return sorted(plugs, key=lambda plug: plug["label"].lower())


class ZigbeePlugLight(LightDriver):
    """A light whose power is whatever the plug it is in is doing."""

    kind = "zigbee-plug"
    label = "Light on a Zigbee smart plug"
    capabilities = ("power",)
    config_fields = (
        ConfigField(
            "eui",
            "Smart plug",
            "zigbee-plug",
            required=True,
            help="Pair the plug on the Zigbee page first; it appears here once it is added.",
        ),
    )

    def __init__(self, config: Dict[str, Any],
                 zigbee: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(config)
        self._zigbee = zigbee
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def _radio(self) -> Any:
        """The service given to this driver, or the hub's own."""
        return self._zigbee if self._zigbee is not None else _ZIGBEE

    @property
    def _eui(self) -> str:
        return str(self.config.get("eui", ""))

    def _reported(self) -> Dict[str, Any]:
        radio = self._radio
        if radio is None:
            return {}
        try:
            return radio.reported(self._eui) or {}
        except Exception:
            LOG.exception("could not read the state of plug %s", self._eui)
            return {}

    def state(self) -> LightState:
        """What the plug last said, and never a guess.

        A plug that has not answered is reported as online with no power
        rather than as off: "off" is a fact about a relay and this is the
        absence of one.
        """
        if self._radio is None:
            return LightState(False, None, "the hub has no Zigbee radio")
        reported = self._reported()
        if not reported.get("present"):
            return LightState(False, None, "the plug is not on the Zigbee network")
        quiet = reported.get("quiet_for_s")
        if quiet is not None and quiet > STALE_SECONDS:
            return LightState(False, None, "the plug has stopped answering")
        power = reported.get("values", {}).get("on")
        if not isinstance(power, bool):
            return LightState(True, None, "the plug has not said whether it is on")
        return LightState(True, power)

    def set_power(self, on: bool) -> PowerResult:
        """Switches the socket, then asks the plug what it did.

        The answer is never assumed from the command: a frame the radio
        accepted is not a relay that moved, and on a mesh the difference
        is a light that silently did not come on this morning.
        """
        radio = self._radio
        if radio is None:
            return PowerResult(UNREACHABLE, "the hub has no Zigbee radio")
        with self._lock:
            try:
                radio.command(self._eui, "on" if on else "off", kind="plug")
            except Exception as error:
                return PowerResult(UNREACHABLE, str(error))
            deadline = self._clock() + CONFIRM_SECONDS
            while self._clock() < deadline:
                time.sleep(CONFIRM_POLL_SECONDS)
                try:
                    radio.command(self._eui, "state", kind="plug")
                except Exception:
                    break
                time.sleep(CONFIRM_POLL_SECONDS * 2)
                power = self._reported().get("values", {}).get("on")
                if power == on:
                    return PowerResult(CONFIRMED)
        power = self._reported().get("values", {}).get("on")
        if power is None:
            return PowerResult(UNCONFIRMED, "the plug did not say what it did")
        return PowerResult(UNCONFIRMED,
                           f"the plug reads {'on' if power else 'off'}")
