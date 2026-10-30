"""Layer 3: what a particular device's bytes mean.

The network layer moves opaque payloads on clusters. This is where a payload
becomes grams, and where "tare" becomes the byte that says so. A second kind
of sensor adds a driver here and touches nothing else.
"""
from __future__ import annotations

from itertools import count
import struct
from typing import Dict, Optional

from .network import SCALE_CLUSTER, SCALE_ENDPOINT, Reading

# The standard clusters this hub reads, by their numbers in the spec.
POWER_CLUSTER = 0x0001
ON_OFF_CLUSTER = 0x0006
TEMPERATURE_CLUSTER = 0x0402
HUMIDITY_CLUSTER = 0x0405

# ZCL sequence numbers, which only have to keep changing.
_SEQUENCE = count(1)



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

    # The report as the firmware packs it: one packed struct, little
    # endian, straight into the frame with no ZCL header, because the
    # cluster is ours and there is nobody else to agree with. The order
    # is scale_report_t in sensors/hx711-scale/main/zigbee.c and the two
    # must be changed together.
    REPORT = "<IffbBBBBBII"
    FLAG_OK = 1 << 0
    FLAG_STABLE = 1 << 1
    FLAG_SATURATED = 1 << 2
    FLAG_WIFI = 1 << 3

    @classmethod
    def decode(cls, cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        """A weight report, from our own firmware's own struct.

        The serial gateway did this unpacking inside itself, so when the
        dongle replaced it the scale went on transmitting every few
        seconds into a hub that dropped every frame -- reporting
        happily, recording nothing. It belongs here, where the other
        devices' decoding is, and not in a backend.
        """
        if int(cluster) != SCALE_CLUSTER:
            return None
        note = cls._note(payload)
        if note is not None:
            return note
        if len(payload) < struct.calcsize(cls.REPORT):
            return None
        (seq, grams, batt, batt_pct, flags, major, minor, patch,
         reset, uptime, interval) = struct.unpack(
            cls.REPORT, payload[:struct.calcsize(cls.REPORT)])
        return {
            "seq": seq,
            "grams": round(grams, 1),
            "batt": round(batt, 2),
            # -1 means no divider is wired, which battery_percent reads
            # as "nothing is measuring the cell" rather than as flat.
            "batt_pct": int(batt_pct),
            "ok": bool(flags & cls.FLAG_OK),
            "stable": bool(flags & cls.FLAG_STABLE),
            "saturated": bool(flags & cls.FLAG_SATURATED),
            "wifi": bool(flags & cls.FLAG_WIFI),
            "fw": f"{major}.{minor}.{patch}",
            "reset": int(reset),
            "uptime": int(uptime),
            "interval": int(interval),
        }

    @staticmethod
    def _note(payload: bytes) -> Optional[Dict[str, object]]:
        """A short text frame the scale sends about itself, as key=value."""
        try:
            text = payload.decode("ascii").strip("\x00").strip()
        except UnicodeDecodeError:
            return None
        if not text or not text.isprintable():
            return None
        found: Dict[str, object] = {}
        for part in text.split():
            key, sep, value = part.partition("=")
            if sep and value and key in ("host", "ip"):
                found[key] = value
        return found or None

    # ---- commands, as bytes for the network layer to carry ----

    @staticmethod
    def payload_for(command: str) -> bytes:
        """A word from the outside turned into bytes for this device.

        Our own firmware reads short text, so the word is the payload.
        A certified device would not understand a word of it, which is
        why this is a driver's job and not the web layer's.
        """
        return command.encode()

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


# ---- ZCL, the part every certified device has in common ----
#
# Our own firmware speaks a protocol we chose, so the scale needs none of
# this. Everything bought off a shelf answers in attributes, and writing
# that parser once is the difference between a new device being a table
# entry and being another decoder to get subtly wrong.

# The attribute types met so far. An unknown type is skipped rather than
# guessed at: reading two bytes where the device wrote four does not
# produce a wrong number, it produces a wrong frame.
_ZCL_TYPES: Dict[int, tuple] = {
    0x10: (1, False),   # boolean
    0x18: (1, False),   # 8-bit bitmap
    0x20: (1, False),   # uint8
    0x21: (2, False),   # uint16
    0x28: (1, True),    # int8
    0x29: (2, True),    # int16
    0x23: (4, False),   # uint32
    0x2B: (4, True),    # int32
}


def zcl_attributes(payload: bytes) -> Optional[Dict[int, int]]:
    """The attributes in a report or a read-back, by id.

    Two frames carry them and both matter: a device reports 0x0A when
    something changes, and answers 0x01 when the hub asks. Anything else
    -- a command, a default response, a manufacturer-specific frame --
    is not an attribute and returns None rather than nonsense.
    """
    if len(payload) < 3:
        return None
    frame_control = payload[0]
    if frame_control & 0x01:                      # cluster-specific, not ours
        return None
    body = payload[3:]
    if frame_control & 0x04:                      # a manufacturer code sits
        body = payload[5:]                        # between the control and the id
    command = payload[4] if frame_control & 0x04 else payload[2]
    if command not in (0x01, 0x0A):
        return None
    read_back = command == 0x01
    found: Dict[int, int] = {}
    while len(body) >= 3:
        attribute = body[0] | body[1] << 8
        if read_back:
            if body[2] != 0x00:                   # a status that is not success
                return found
            body = body[3:]
        else:
            body = body[2:]
        if not body:
            return found
        kind, body = body[0], body[1:]
        size = _ZCL_TYPES.get(kind)
        if size is None or len(body) < size[0]:
            return found
        width, signed = size
        found[attribute] = int.from_bytes(body[:width], "little", signed=signed)
        body = body[width:]
    return found


TUYA_CLUSTER = 0xEF00
IAS_ZONE_CLUSTER = 0x0500
# The ZCL general commands that are protocol, not data: an acknowledgement
# of a reporting setup, and a default response. Both are expected, neither
# carries a reading, and treating them as unreadable data made a working
# device look like a broken one in the log.
ZCL_HOUSEKEEPING = (0x07, 0x0B)


def zcl_command(payload: bytes) -> Optional[int]:
    """The command id of a ZCL frame, or None if it is not one."""
    if len(payload) < 3:
        return None
    return payload[4] if payload[0] & 0x04 else payload[2]


def tuya_datapoints(payload: bytes) -> Dict[int, int]:
    """The datapoints in a Tuya report, by number.

    Tuya devices declare the standard clusters and then say nothing on
    them, sending everything down a manufacturer cluster of their own in
    this shape instead. The numbers mean whatever the maker decided, so
    reading them is one job and knowing what they mean is another.
    """
    if len(payload) < 5 or payload[2] not in (0x01, 0x02, 0x06):
        return {}
    body = payload[5:]                        # past the ZCL header and sequence
    found: Dict[int, int] = {}
    while len(body) >= 4:
        datapoint, kind = body[0], body[1]
        length = int.from_bytes(body[2:4], "big")
        if len(body) < 4 + length or length == 0:
            break
        raw = body[4:4 + length]
        # Only the two shapes a measurement arrives in: a value and a
        # boolean or enum. A string is a name, not a reading.
        if kind in (0x02, 0x01, 0x04):
            found[datapoint] = int.from_bytes(raw, "big",
                                              signed=kind == 0x02 and length == 4)
        body = body[4 + length:]
    return found


class PlugDriver:
    """A mains switch that speaks standard ZCL rather than our own protocol.

    The scale is ours, so its cluster is manufacturer specific and its
    commands are short words we chose. A certified plug is not ours: it
    answers the On/Off cluster every Zigbee switch answers, so the bytes
    here are from the specification and not from us.

    It is about nothing. A plug does not measure a plant or a room -- it
    switches whatever is plugged into it, and until the hub is told what
    that is, saying it is "about" anything would be a guess.
    """

    kind = "plug"
    cluster = ON_OFF_CLUSTER
    endpoint = 1              # the first endpoint, where a plug's switch lives
    subject = None

    # Cluster-specific, client to server. The sequence number only has to
    # differ from the last one for a reply to be matched to its request.
    _FRAME_CONTROL = 0x01
    COMMANDS = {"off": 0x00, "on": 0x01, "toggle": 0x02}

    @classmethod
    def payload_for(cls, command: str) -> bytes:
        word = command.strip().lower()
        if word == "state":
            # Read Attributes (general command 0x00) of attribute 0x0000,
            # OnOff. Asking is the only way to know: a plug switched at its
            # own button changes without telling anyone it was not bound to.
            return bytes([0x00, next(_SEQUENCE) & 0xFF, 0x00, 0x00, 0x00])
        if word not in cls.COMMANDS:
            raise ValueError(f"a plug understands on, off, toggle or state, not {command!r}")
        return bytes([cls._FRAME_CONTROL, next(_SEQUENCE) & 0xFF, cls.COMMANDS[word]])

    @staticmethod
    def decode(cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        """Whether the switch is closed, from a report or an answer."""
        attributes = zcl_attributes(payload)
        if not attributes or 0x0000 not in attributes:
            return None
        return {"on": bool(attributes[0x0000])}


class ThermometerDriver:
    """Air temperature and humidity from a certified sensor.

    This was a placeholder, written before the hardware arrived to check
    the claim that a second kind of device needs no changes outside this
    file. The claim held, but its guesses did not: the placeholder
    expected our own firmware on a manufacturer cluster, and the device
    that turned up -- a SONOFF SNZB-02B -- speaks the standard
    measurement clusters like any thermometer would.

    It is about a room rather than a plant. A thermometer measures the
    air a shelf of plants sits in, and filing that against one of them
    would make the other plants' air a fact nobody recorded.
    """

    kind = "thermometer"
    # Temperature is where the hub writes to when it asks; the other two
    # clusters are read from and never written to.
    cluster = TEMPERATURE_CLUSTER
    endpoint = 1
    subject = "room"

    # What the hub reads, and what each raw number means. Both
    # measurements arrive as hundredths, and battery percentage arrives
    # in half-percent steps, which is the specification and not a bug.
    READINGS = {
        TEMPERATURE_CLUSTER: (0x0000, "temp_c", 0.01),
        HUMIDITY_CLUSTER: (0x0000, "rh_pct", 0.01),
    }

    # Which cluster each question goes to. A device with one cluster can
    # let the driver's own stand for all of them; a thermometer measures
    # two different things in two different places and has to say which.
    ASKS = {
        "state": TEMPERATURE_CLUSTER,
        "temperature": TEMPERATURE_CLUSTER,
        "humidity": HUMIDITY_CLUSTER,
        "battery": POWER_CLUSTER,
        # Everything a Tuya device is willing to say, which is the only
        # way to see its datapoints without waiting for it to volunteer
        # them. Harmless on a device that is not Tuya: it has no such
        # cluster and the frame goes nowhere.
        "datapoints": TUYA_CLUSTER,
    }

    @classmethod
    def cluster_for(cls, command: str) -> int:
        return cls.ASKS.get(command.strip().lower(), cls.cluster)

    @classmethod
    def payload_for(cls, command: str) -> bytes:
        """Asks for a measurement. There is nothing to tell this device."""
        word = command.strip().lower()
        if word not in cls.ASKS:
            raise ValueError(f"a thermometer only answers questions, not {command!r}")
        if word == "datapoints":
            # Tuya's own "tell me everything" command, which is
            # cluster-specific and carries no body.
            return bytes([0x01, next(_SEQUENCE) & 0xFF, 0x03])
        attribute = 0x0021 if word == "battery" else 0x0000
        return bytes([0x00, next(_SEQUENCE) & 0xFF, 0x00,
                      attribute & 0xFF, attribute >> 8])

    @classmethod
    def decode(cls, cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        attributes = zcl_attributes(payload)
        if not attributes:
            return None
        if cluster == POWER_CLUSTER:
            return _battery_from(attributes)
        reading = cls.READINGS.get(int(cluster))
        if reading is None:
            return None
        attribute, name, scale = reading
        if attribute not in attributes:
            return None
        return {name: round(attributes[attribute] * scale, 2)}


def _battery_from(attributes: Dict[int, int]) -> Optional[Dict[str, object]]:
    """Charge, in the two shapes the rest of the hub already reads.

    Named batt_pct and batt because that is what battery_percent and
    battery_volts above look for: a bought thermometer then gets the
    same battery history as our own boards, without either side knowing
    about the other.
    """
    values: Dict[str, object] = {}
    if 0x0021 in attributes:                 # BatteryPercentageRemaining, half-steps
        values["batt_pct"] = round(attributes[0x0021] / 2)
    if 0x0020 in attributes:                 # BatteryVoltage, tenths of a volt
        values["batt"] = round(attributes[0x0020] / 10, 1)
    return values or None


class LeakDriver:
    """A water leak sensor, which speaks the alarm protocol.

    IAS Zone is how Zigbee does alarms, and it is a conversation rather
    than a report: the device asks to be enrolled, the hub says yes and
    gives it a zone number, and only then does it send anything when it
    gets wet. Ignore the request and the sensor sits there for ever,
    perfectly healthy and silent, which is the worst way for a leak
    sensor to fail.

    It is about a room. Water on a floor is the room's business, not
    one pot's.
    """

    kind = "leak"
    cluster = IAS_ZONE_CLUSTER
    endpoint = 1
    subject = "room"

    # What the device is asking about when it asks to be enrolled.
    ZONE_WATER = 0x002A
    # Zone status bits, from the specification.
    ALARM1 = 1 << 0          # the thing this sensor is for
    TAMPER = 1 << 2
    BATTERY_LOW = 1 << 3

    @classmethod
    def payload_for(cls, command: str) -> bytes:
        word = command.strip().lower()
        if word == "enroll":
            # Zone Enroll Response: yes, and you are zone 1. Sent back
            # to the device, so client to server.
            return bytes([0x01, next(_SEQUENCE) & 0xFF, 0x00, 0x00, 0x01])
        if word == "state":
            # ZoneStatus, for asking rather than waiting.
            return bytes([0x00, next(_SEQUENCE) & 0xFF, 0x00, 0x02, 0x00])
        raise ValueError(f"a leak sensor understands enroll or state, not {command!r}")

    @classmethod
    def decode(cls, cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        if int(cluster) == POWER_CLUSTER:
            return _battery_from(zcl_attributes(payload) or {})
        if int(cluster) != IAS_ZONE_CLUSTER or len(payload) < 3:
            return None
        if payload[0] & 0x01:                       # cluster-specific
            command, body = payload[2], payload[3:]
            if command == 0x01 and len(body) >= 2:  # Zone Enroll Request
                return {"enrolling": True,
                        "zone_type": int.from_bytes(body[0:2], "little")}
            if command == 0x00 and len(body) >= 2:  # Zone Status Change
                return cls._status(int.from_bytes(body[0:2], "little"))
            return None
        attributes = zcl_attributes(payload)
        if attributes and 0x0002 in attributes:     # ZoneStatus, read back
            return cls._status(attributes[0x0002])
        return None

    @classmethod
    def _status(cls, status: int) -> Dict[str, object]:
        """The alarm bits, said in words the rest of the hub can file."""
        return {
            "wet": bool(status & cls.ALARM1),
            "tamper": bool(status & cls.TAMPER),
            "battery_low": bool(status & cls.BATTERY_LOW),
        }


class SoilProbeDriver:
    """A probe in a pot, reporting what the soil is doing.

    A Tuya device, so it declares the air temperature and humidity
    clusters and then says almost nothing on them -- its real readings
    come down the manufacturer cluster as numbered datapoints. The
    numbers below were established by asking the device for all of them
    and watching which moved: 5 tracked the temperature the standard
    cluster also answered, 109 rose as the soil took up water, and 15
    is Tuya's usual battery percentage.

    It is about a plant, not a room. Soil is one pot's business, and
    filing it against a room would say something about every other pot
    in there that nobody measured.
    """

    kind = "soil"
    cluster = TUYA_CLUSTER
    endpoint = 1
    subject = "plant"

    # datapoint -> what it is, and what to multiply the raw number by.
    DATAPOINTS = {
        5: ("soil_temperature_c", 0.1),
        109: ("moisture_percent", 1),
        15: ("batt_pct", 1),
    }

    @classmethod
    def payload_for(cls, command: str) -> bytes:
        """Asks for everything. There is nothing to tell a probe."""
        if command.strip().lower() not in ("state", "datapoints"):
            raise ValueError(f"a soil probe only answers questions, not {command!r}")
        return bytes([0x01, next(_SEQUENCE) & 0xFF, 0x03])

    @classmethod
    def decode(cls, cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        if int(cluster) == TEMPERATURE_CLUSTER:
            # It answers here too, and the same reading by either route
            # is the same reading: filed under the soil's name, because
            # that is where this probe is.
            attributes = zcl_attributes(payload)
            if attributes and 0x0000 in attributes:
                return {"soil_temperature_c": round(attributes[0x0000] * 0.01, 2)}
            return None
        values: Dict[str, object] = {}
        for point, raw in tuya_datapoints(payload).items():
            known = cls.DATAPOINTS.get(point)
            if known is None:
                continue
            name, scale = known
            values[name] = round(raw * scale, 2) if scale != 1 else raw
        return values or None


class TuyaDriver:
    """Whatever a Tuya device is saying, before anyone knows what it means.

    Tuya devices number their readings and keep the meanings to
    themselves, so the numbers are kept under their own names until a
    real driver claims them. Recording them unnamed is not laziness: it
    is the only way to watch a datapoint move while doing something to
    the device, which is how the meaning gets established at all.
    """

    kind = "tuya"
    cluster = TUYA_CLUSTER
    endpoint = 1
    subject = None

    @staticmethod
    def decode(cluster: int, payload: bytes) -> Optional[Dict[str, object]]:
        points = tuya_datapoints(payload)
        return {f"tuya_{point}": value for point, value in points.items()} or None


# Which driver owns a cluster, for reading frames the radio hands up raw.
# A driver may own several: a thermometer's temperature, humidity and
# battery arrive on three, and all three are that one device talking.
DRIVERS: Dict[str, type] = {
    ScaleDriver.kind: ScaleDriver,
    SoilProbeDriver.kind: SoilProbeDriver,
    LeakDriver.kind: LeakDriver,
    PlugDriver.kind: PlugDriver,
    ThermometerDriver.kind: ThermometerDriver,
}


_BY_CLUSTER: Dict[int, type] = {
    PlugDriver.cluster: PlugDriver,
    SCALE_CLUSTER: ScaleDriver,
    TEMPERATURE_CLUSTER: ThermometerDriver,
    HUMIDITY_CLUSTER: ThermometerDriver,
    POWER_CLUSTER: ThermometerDriver,
    TUYA_CLUSTER: TuyaDriver,
    IAS_ZONE_CLUSTER: LeakDriver,
}


def decode_frame(cluster: int, payload: bytes,
                 kind: Optional[str] = None) -> Optional[Dict[str, object]]:
    """What a raw frame means, or None when no driver claims it.

    The EZSP radio hands up every frame as bytes, including replies to
    commands the hub itself sent. Without this the hub filed them under
    the kind "frame", which is not a kind of device and overwrote the
    real one -- a plug that answered a question stopped being a plug.
    """
    # The device's own driver first, because a cluster does not always
    # mean the same thing: Tuya numbers its readings and each maker
    # chooses what the numbers are, so datapoint 109 is soil moisture
    # on this probe and could be anything on the next one.
    driver = DRIVERS.get(kind or "") or _BY_CLUSTER.get(int(cluster))
    decode = getattr(driver, "decode", None) if driver else None
    if decode is None:
        driver = _BY_CLUSTER.get(int(cluster))
        decode = getattr(driver, "decode", None) if driver else None
    if decode is None:
        return None
    try:
        return decode(int(cluster), payload)
    except Exception:
        return None


def subject_of(kind: str) -> Optional[str]:
    """What a device of this kind is about: "plant", "room", or nothing."""
    driver = DRIVERS.get(kind)
    return getattr(driver, "subject", None) if driver else None
