"""The ESP32-C6 gateway behind the layer 2 interface.

This board runs the Zigbee stack itself and speaks JSON lines over USB. All
of that is confined to this file; nothing above it knows a serial port exists.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from ._serial import SerialLine, candidate_ports
from .network import Delivery, Device, Reading

LOG = logging.getLogger(__name__)

# How long to wait for a gateway to identify itself when probing a port.
PROBE_SECONDS = 1.5
# A gateway that has said nothing at all for this long is presumed gone,
# which is not the same as a network with nothing to report.
SILENCE_SECONDS = 180.0
RECONNECT_SECONDS = 5.0
# How often to ask the gateway whether it is still there. Without this,
# a healthy gateway with nothing to report goes quiet and then looks exactly
# like an unplugged one -- which is the confusion this whole distinction was
# supposed to prevent.
HEARTBEAT_SECONDS = 30.0


class SerialGateway:
    """Talks to the C6 gateway, and presents it as a ZigbeeNetwork."""

    def __init__(self, port: Optional[str] = None) -> None:
        self._wanted_port = port
        self._line: Optional[SerialLine] = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Set when the port has been handed back deliberately. Without it the
        # reconnect loop takes the port again within seconds, so flashing the
        # gateway or watching the link would mean stopping the whole hub.
        self._released = False
        self._devices: Dict[str, Device] = {}
        self._next_id = 1
        self._last_heard = 0.0
        self._pan: Optional[str] = None
        self._channel: Optional[int] = None
        # When the gateway says its join window shuts, and when it said
        # so. The remaining time is worked out from those rather than kept as
        # a countdown here, so a stale answer reads as closed, not as open.
        self._permit_s = 0
        self._permit_at = 0.0
        # The stack's own view of who is reachable. Distinct from the
        # gateway's address cache: this is the radio's answer, and it is only
        # ever read.
        self._neighbours: List[Dict[str, object]] = []
        # What devices have said about themselves, by EUI-64.
        self._notes: Dict[str, Dict[str, str]] = {}
        self._note_handlers: List[Any] = []
        self._restart_handlers: List[Any] = []
        self._uptime_s: Optional[int] = None
        self._reset_reason: Optional[str] = None
        self._on_reading: List[Callable[[Reading], None]] = []
        self._on_joined: List[Callable[[Device], None]] = []
        self._on_delivery: List[Callable[[Delivery], None]] = []

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, name="zigbee", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            if self._line:
                self._line.close()
                self._line = None

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Hands the serial port back, and stays off it until told otherwise."""
        self._released = True
        with self._lock:
            if self._line:
                LOG.info("releasing %s", self._line.path)
                self._line.close()
                self._line = None
        self._devices.clear()
        self._pan = None
        self._channel = None

    def reconnect(self) -> None:
        """Looks for a gateway again, forgetting any remembered port."""
        self._released = False

    @property
    def connected(self) -> bool:
        # Judged on having heard something recently, not on the file
        # descriptor being open. An unplugged board leaves a device node that
        # opens happily and then returns nothing forever.
        return self._line is not None and (time.time() - self._last_heard) < SILENCE_SECONDS

    @property
    def uptime_s(self) -> Optional[int]:
        """How long the gateway says it has been running."""
        return self._uptime_s

    @property
    def reset_reason(self) -> Optional[str]:
        return self._reset_reason

    @property
    def pan(self) -> Optional[str]:
        return self._pan

    @property
    def channel(self) -> Optional[int]:
        return self._channel

    @property
    def permit_seconds_left(self) -> int:
        if not self._permit_s:
            return 0
        left = self._permit_s - (time.time() - self._permit_at)
        return int(left) if left > 0 else 0

    # ---- the interface ----

    def devices(self) -> List[Device]:
        return sorted(self._devices.values(), key=lambda d: d.eui)

    def permit_join(self, seconds: int) -> None:
        self._send_json({"t": "permit", "seconds": int(seconds)})

    def send(self, eui: str, cluster: int, endpoint: int, payload: bytes) -> int:
        """One APS frame to one device, by EUI-64.

        The C6 carries the payload as text because that is what this gateway
        and the scale agreed on; a zigpy backend would put the same bytes on
        the same cluster. Callers deal in bytes either way.
        """
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        self._send_json({
            "t": "cmd",
            "id": request_id,
            "eui": eui,
            "cluster": cluster,
            "endpoint": endpoint,
            "cmd": payload.decode("ascii", "replace"),
        })
        return request_id

    def forget(self, eui: str) -> None:
        """Drops a device from the gateway's table.

        Distinct from asking it to leave the network: a device that is dead
        or out of range cannot be told anything, and pretending otherwise
        leaves it listed forever.
        """
        self._send_json({"t": "forget", "eui": eui})
        self._devices.pop(eui, None)

    def remove(self, eui: str) -> None:
        """Asks the device to leave, then drops it.

        Without the leave request a device that is still powered simply
        rejoins and reappears, which reads as a bug rather than a decision.
        """
        self._send_json({"t": "remove", "eui": eui})
        self._devices.pop(eui, None)

    def on_note(self, handler: Any) -> None:
        """Called when a device says something about itself."""
        self._note_handlers.append(handler)

    def notes(self, eui: str) -> Dict[str, str]:
        return dict(self._notes.get(eui, {}))

    def neighbours(self) -> List[Dict[str, object]]:
        return list(self._neighbours)

    def refresh(self) -> None:
        """Asks the gateway for its table, after a restart at either end."""
        self._send_json({"t": "list"})
        self._send_json({"t": "neighbours"})

    def on_reading(self, handler: Callable[[Reading], None]) -> None:
        self._on_reading.append(handler)

    def on_joined(self, handler: Callable[[Device], None]) -> None:
        self._on_joined.append(handler)

    def on_delivery(self, handler: Callable[[Delivery], None]) -> None:
        self._on_delivery.append(handler)

    # ---- the serial side ----

    def _send_json(self, message: Dict[str, object]) -> bool:
        with self._lock:
            if not self._line:
                LOG.warning("no gateway; dropped %s", message.get("t"))
                return False
            try:
                self._line.write((json.dumps(message) + "\n").encode())
                return True
            except OSError:
                LOG.exception("writing to the gateway failed")
                self._line.close()
                self._line = None
                return False

    def _open(self) -> bool:
        ports = [self._wanted_port] if self._wanted_port else candidate_ports()
        for path in ports:
            try:
                line = SerialLine(path)
            except OSError:
                continue
            # Ask what it is. The gateway answers a list request; anything
            # else on the bus stays quiet, which is how they are told apart.
            try:
                line.write(b'{"t":"list"}\n')
            except OSError:
                line.close()
                continue
            deadline = time.time() + PROBE_SECONDS
            while time.time() < deadline:
                raw = line.readline()
                if raw and raw.lstrip().startswith(b"{"):
                    with self._lock:
                        self._line = line
                    self._last_heard = time.time()
                    LOG.info("gateway found on %s", path)
                    self._consume(raw)
                    # The probe may well have caught a reading rather than
                    # the reply, so ask again now the port is ours.
                    self.refresh()
                    return True
            line.close()
        return False

    def _work(self) -> None:
        asked_at = 0.0
        while not self._stopping.is_set():
            if self._released:
                self._stopping.wait(RECONNECT_SECONDS)
                continue
            if self._line is None and not self._open():
                self._stopping.wait(RECONNECT_SECONDS)
                continue
            # Ask, rather than wait to be told. A network where nothing is
            # reporting is silent, and silence is not evidence of a missing
            # gateway: the reply to this is.
            now = time.time()
            if now - asked_at > HEARTBEAT_SECONDS:
                asked_at = now
                self.refresh()
            try:
                raw = self._line.readline() if self._line else None
            except OSError:
                raw = None
                with self._lock:
                    if self._line:
                        self._line.close()
                    self._line = None
                continue
            if raw:
                self._consume(raw)

    def _consume(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text.startswith("{"):
            return                      # the gateway's own log lines
        self._last_heard = time.time()
        try:
            message = json.loads(text)
        except ValueError:
            LOG.debug("unparsable line from the gateway: %.60s", text)
            return
        kind = message.get("t")
        if kind == "reading":
            self._handle_reading(message)
        elif kind == "joined":
            self._handle_joined(message)
        elif kind == "devices":
            self._handle_devices(message)
        elif kind == "info":
            # A short note from a device about itself. Parsed as key=value so
            # a second kind of device can say something else without this
            # layer learning what it means.
            eui = str(message.get("eui", ""))
            text = str(message.get("text", ""))
            if eui and "=" in text:
                key, _, value = text.partition("=")
                self._notes.setdefault(eui, {})[key.strip()] = value.strip()
                for handler in self._note_handlers:
                    try:
                        handler(eui, key.strip(), value.strip())
                    except Exception:
                        LOG.exception("a note handler failed")
        elif kind == "neighbours":
            rows = message.get("list") or []
            self._neighbours = [r for r in rows if isinstance(r, dict)]
        elif kind == "permit":
            self._permit_s = _int_or_none(message.get("seconds")) or 0
            self._permit_at = time.time()
        elif kind == "ack":
            self._fan_out(self._on_delivery, Delivery(
                request_id=int(message.get("id", -1)),
                status=int(message.get("status", 0)),
                note=str(message.get("note", "")),
            ))

    def _handle_reading(self, message: Dict[str, object]) -> None:
        eui = str(message.get("eui", ""))
        if not eui:
            return
        self._note_device(eui, message.get("short"))
        known = {"t", "eui", "short", "kind", "lqi", "rssi"}
        reading = Reading(
            eui=eui,
            kind=str(message.get("kind", "unknown")),
            values={k: v for k, v in message.items() if k not in known},
            lqi=_int_or_none(message.get("lqi")),
            rssi=_int_or_none(message.get("rssi")),
        )
        self._fan_out(self._on_reading, reading)

    def _handle_joined(self, message: Dict[str, object]) -> None:
        eui = str(message.get("eui", ""))
        if not eui:
            return
        device = self._note_device(eui, message.get("short"))
        self._fan_out(self._on_joined, device)

    # What the gateway's reset reason number means, as the ESP-IDF
    # enumeration orders them.
    RESET_REASONS = {
        0: "unknown", 1: "power on", 2: "external", 3: "software",
        4: "panic", 5: "interrupt watchdog", 6: "task watchdog",
        7: "other watchdog", 8: "deep sleep", 9: "brownout",
        10: "sdio", 11: "usb", 12: "jtag",
    }

    def _note_uptime(self, message: Dict[str, object]) -> None:
        """Spot a gateway that has restarted since we last looked.

        Its uptime going backwards is the only honest signal: the serial
        port looks identical either side of a reset, which is exactly how
        this went unnoticed while it was happening several times a day.
        """
        uptime = _int_or_none(message.get("uptime_s"))
        if uptime is None:
            return
        previous = self._uptime_s
        self._uptime_s = uptime
        self._reset_reason = self.RESET_REASONS.get(
            _int_or_none(message.get("reset")) or 0, "unknown")
        if previous is None or uptime >= previous:
            return
        LOG.warning("the gateway restarted (%s) after %s s up",
                    self._reset_reason, previous)
        for handler in self._restart_handlers:
            try:
                handler(self._reset_reason, previous)
            except Exception:
                LOG.exception("a gateway restart handler failed")

    def on_restart(self, handler: Any) -> None:
        """Called when the gateway is seen to have restarted."""
        self._restart_handlers.append(handler)

    def _handle_devices(self, message: Dict[str, object]) -> None:
        self._pan = _text_or_none(message.get("pan"))
        self._channel = _int_or_none(message.get("channel"))
        self._note_uptime(message)
        if "permit_s" in message:
            self._permit_s = _int_or_none(message.get("permit_s")) or 0
            self._permit_at = time.time()
        for row in message.get("list", []) or []:
            eui = str(row.get("eui", ""))
            if eui:
                self._devices[eui] = Device(
                    eui=eui,
                    short=_hex_or_none(row.get("short")),
                    last_seen_s=_int_or_none(row.get("last_seen_s")),
                )

    def _note_device(self, eui: str, short: object) -> Device:
        device = Device(eui=eui, short=_hex_or_none(short), last_seen_s=0)
        self._devices[eui] = device
        return device

    @staticmethod
    def _fan_out(handlers, item) -> None:
        for handler in handlers:
            try:
                handler(item)
            except Exception:
                LOG.exception("a zigbee handler raised")


def _int_or_none(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _text_or_none(value: object) -> Optional[str]:
    return str(value) if isinstance(value, str) else None


def _hex_or_none(value: object) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None
