"""Layer 3: what a particular device's bytes mean.

The network layer moves opaque payloads on clusters. This is where a payload
becomes grams, and where "tare" becomes the byte that says so. A second kind
of sensor adds a driver here and touches nothing else.
"""
from __future__ import annotations

from typing import Dict, Optional

from .network import SCALE_CLUSTER, SCALE_ENDPOINT, Reading



# Battery, for any device that has one.
#
# Not a method on the scale driver, which is where this started: every
# battery-powered device in this project reports charge the same way, and a
# thermometer's cell drains exactly like a scale's. Keeping it here means a
# new driver gets battery history by reporting the same two fields rather
# than by remembering to implement anything.

def battery_percent(reading: Reading) -> Optional[int]:
    """Charge left, or None when the firmware says it cannot tell.

    A device with no divider wired to measure its cell reports -1, which
    must not be read as "nearly flat".
    """
    percent = reading.values.get("batt_pct")
    if not isinstance(percent, (int, float)) or percent < 0:
        return None
    return int(percent)


def battery_volts(reading: Reading) -> Optional[float]:
    """The cell voltage, or None when nothing is measuring it.

    An unwired divider floats at a couple of hundred millivolts, which is a
    number and not a measurement.
    """
    if battery_percent(reading) is None:
        return None
    volts = reading.values.get("batt")
    return float(volts) if isinstance(volts, (int, float)) else None


class ScaleDriver:
    """The HX711 scale: weight out, short commands in."""

    kind = "scale"
    cluster = SCALE_CLUSTER
    endpoint = SCALE_ENDPOINT
    # What this device is about. A scale measures a particular plant's
    # weight, so it attaches to a plant; a room thermometer measures the air
    # in a room and attaches to that instead. Everything that differs
    # between them follows from this one word: which picker the page shows,
    # what the reading is filed against, how the list describes it. Nothing
    # asks "is this a scale?" to decide.
    subject = "plant"

    @staticmethod
    def weight_grams(reading: Reading) -> Optional[float]:
        """Grams, or None when the device says the reading is not good.

        A saturated reading usually means a load cell wire has come loose,
        and is worse than no reading at all because it looks like a number.
        """
        if reading.kind != ScaleDriver.kind:
            return None
        values = reading.values
        if not values.get("ok") or values.get("saturated"):
            return None
        grams = values.get("grams")
        return float(grams) if isinstance(grams, (int, float)) else None

    @staticmethod
    def wifi_on(reading: Reading) -> Optional[bool]:
        value = reading.values.get("wifi")
        return bool(value) if isinstance(value, bool) else None

    # ---- commands, as bytes for the network layer to carry ----

    @staticmethod
    def tare() -> bytes:
        return b"t"

    @staticmethod
    def set_wifi(on: bool) -> bytes:
        return b"w 1" if on else b"w 0"

    @staticmethod
    def set_interval(seconds: int) -> bytes:
        return f"i {int(seconds)}".encode()

    @staticmethod
    def set_report_floor(seconds: int) -> bytes:
        return f"z {int(seconds)}".encode()


class ThermometerDriver:
    """Not built yet, and here deliberately.

    The architecture is only worth anything if a second kind of device needs
    no changes outside this file. Writing the one that differs -- a device
    about a room rather than a plant -- is how that claim gets checked
    before the hardware arrives.
    """

    kind = "thermometer"
    cluster = 0xFC01
    endpoint = 11
    subject = "room"


DRIVERS: Dict[str, type] = {
    ScaleDriver.kind: ScaleDriver,
    ThermometerDriver.kind: ThermometerDriver,
}


def subject_of(kind: str) -> Optional[str]:
    """What a device of this kind is about: "plant", "room", or nothing."""
    driver = DRIVERS.get(kind)
    return getattr(driver, "subject", None) if driver else None
