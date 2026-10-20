"""The hub's glue: readings from the network into whatever the store wants.

Deliberately thin. Deciding which weights are worth keeping already lives in
ScaleService, and duplicating that judgement here would be two answers to one
question. This turns Zigbee events into the same shape the HTTP path
produces, and tracks enough per device to tell a healthy link from a quiet one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..weights import WeightKeeper
from .drivers import DRIVERS, ScaleDriver, battery_percent, battery_volts, subject_of
from .network import Delivery, Device, Reading

LOG = logging.getLogger(__name__)

# How long to wait for a device to answer a request for a reading.
# A sleepy end device collects its commands on its own schedule,
# which has measured at about three seconds.
FRESH_READING_SECONDS = 20.0
# How often the kept reports are weeded. Hourly is far more often than a
# seven-day window needs, and it means a hub left running for months never
# accumulates a backlog to clear.
PRUNE_SECONDS = 3600.0


@dataclass
class DeviceHealth:
    """What this device has actually done, as opposed to what it claims.

    A scale once reported itself joined with 1584 sends and no failures while
    every frame went nowhere: its counter recorded transmit requests, not
    arrivals. Liveness here is judged only on what reached the hub.
    """

    eui: str
    readings: int = 0
    joins: int = 0
    commands: int = 0
    failed_commands: int = 0
    first_seen: float = field(default_factory=time.time)
    last_reading: Optional[float] = None
    last_values: Dict[str, Any] = field(default_factory=dict)
    last_lqi: Optional[int] = None
    last_rssi: Optional[int] = None
    kind: str = "unknown"

    def seconds_quiet(self) -> Optional[float]:
        return None if self.last_reading is None else time.time() - self.last_reading


class ZigbeeService:
    def __init__(self, store: Any, network: Any) -> None:
        self._store = store
        self._network = network
        self._health: Dict[str, DeviceHealth] = {}
        self._pending: Dict[int, str] = {}
        self._lock = threading.Lock()
        # Devices already written to the store, so one that talks constantly
        # is not written on every reading.
        self._recorded: set = set()
        # When the kept reports were last weeded. Starts at zero so the first
        # report after a restart clears anything a stopped hub outlived.
        self._pruned_at: float = 0.0
        # The same judgement the HTTP poller uses. A reading is a number;
        # whether it is this plant's weight is a question with rules, and
        # there is one set of them.
        self._keeper = WeightKeeper(store) if store else None
        network.on_reading(self._reading)
        network.on_joined(self._joined)
        # Not every backend has notes; a dongle under zigpy would not.
        if hasattr(network, "on_note"):
            network.on_note(self._note)
        network.on_delivery(self._delivery)

    def start(self) -> None:
        self._network.start()

    def stop(self) -> None:
        self._network.stop()

    # ---- what the control panel needs ----

    @property
    def connected(self) -> bool:
        return bool(getattr(self._network, "connected", False))

    def gateway(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "pan": getattr(self._network, "pan", None),
            "channel": getattr(self._network, "channel", None),
            "devices": len(self._health),
            "permit_s": getattr(self._network, "permit_seconds_left", 0),
            "released": bool(getattr(self._network, "released", False)),
            "neighbours": getattr(self._network, "neighbours", list)(),
        }

    def devices(self) -> List[Dict[str, Any]]:
        """Every device the gateway knows, joined to what the hub knows.

        The gateway supplies what the radio can see; the store supplies what
        the device is for -- its name, the plant or room it belongs to,
        whether it has been let in at all. Neither half is the whole device.
        """
        known = {d.eui: d for d in self._network.devices()}
        with self._lock:
            health = dict(self._health)
        sensors: Dict[str, Dict[str, Any]] = {}
        if self._store:
            try:
                sensors = {
                    str(row.get("identity_value", "")): row
                    for row in self._store.zigbee_sensors()
                }
            except Exception:
                LOG.exception("could not read the zigbee sensors")

        out: List[Dict[str, Any]] = []
        for eui in sorted(set(known) | set(health) | set(sensors)):
            device: Optional[Device] = known.get(eui)
            h = health.get(eui)
            sensor = sensors.get(eui, {})
            kind = h.kind if h and h.kind != "unknown" else _kind_of(sensor)
            status = sensor.get("enrollment_status", "unclaimed")
            out.append({
                "eui": eui,
                "sensor_id": sensor.get("sensor_id"),
                "name": sensor.get("display_name"),
                "waiting": status == "unclaimed" and not sensor.get("archived"),
                "accepted": status == "enrolled",
                # What a device of this kind is about, so the page knows
                # whether to offer a plant or a room, without asking what
                # kind of device it is.
                "subject": subject_of(kind),
                "plant_id": sensor.get("plant_id"),
                "plant": sensor.get("plant_name"),
                "room": sensor.get("room"),
                "room_id": sensor.get("room_id"),
                "short": f"0x{device.short:04x}" if device and device.short is not None else None,
                "kind": kind,
                "readings": h.readings if h else 0,
                "joins": h.joins if h else 0,
                "commands": h.commands if h else 0,
                "failed_commands": h.failed_commands if h else 0,
                "quiet_for_s": round(h.seconds_quiet()) if h and h.seconds_quiet() is not None else None,
                "lqi": h.last_lqi if h else None,
                "rssi": h.last_rssi if h else None,
                "last": h.last_values if h else {},
                "notes": self._network.notes(eui) if hasattr(self._network, "notes") else {},
            })
        return out

    def permit_join(self, seconds: int) -> None:
        self._network.permit_join(seconds)

    def accept(self, eui: str) -> None:
        """Lets a waiting device in, which is simply enrolling a sensor."""
        if not self._store:
            return
        kind = self._kind_for(eui)
        self._store.seen_zigbee_device(eui, kind)
        # Named after what it is, so the list is readable before anybody
        # renames it. A device with no name at all reads as a fault.
        self._store.enroll_zigbee_device(eui, kind.capitalize() if kind != "unknown" else "Zigbee device")

    def ignore(self, eui: str) -> None:
        """Turns a waiting device away by archiving its row.

        Archived rather than deleted: the device still has a place on the
        radio and will announce itself again, and a row that remembers the
        answer is better than one that keeps asking the question.
        """
        if not self._store:
            return
        sensor = self._store.seen_zigbee_device(eui, self._kind_for(eui))
        self._store.set_sensor_archived(sensor["sensor_id"], True)

    def attach(self, eui: str, target_id: Optional[int]) -> None:
        """Point a device at what it is about.

        Which of the two this is depends on the device kind, not on the
        caller: a scale is about a plant, a thermometer about a room. The
        driver already says which, so nothing here has to know.
        """
        if not self._store:
            return
        sensor = self._store.seen_zigbee_device(eui, self._kind_for(eui))
        subject = subject_of(self._kind_for(eui))
        if subject == "plant":
            self._store.attach_sensor(sensor["sensor_id"], target_id)
        elif subject == "room":
            self._store.set_sensor_room(sensor["sensor_id"], target_id)
        else:
            raise ValueError("this kind of device is not about anything")

    def rename(self, eui: str, name: str) -> None:
        if not self._store:
            return
        self._store.seen_zigbee_device(eui, self._kind_for(eui))
        self._store.enroll_zigbee_device(eui, name)

    def _kind_for(self, eui: str) -> str:
        """What kind of device this is, from memory or from the store.

        Health counters start empty on every hub restart, so asking them
        alone meant a device the hub has known for months was "unknown"
        until it next reported -- and attaching it to a plant failed with
        "this kind of device is not about anything". The row outlives the
        process; the counters do not.
        """
        with self._lock:
            health = self._health.get(eui)
        if health and health.kind and health.kind != "unknown":
            return health.kind
        if self._store:
            try:
                row = next((s for s in self._store.zigbee_sensors()
                            if str(s.get("identity_value", "")) == eui), None)
            except Exception:
                LOG.exception("could not look up the kind of %s", eui)
                row = None
            transport = str((row or {}).get("transport", ""))
            if transport.startswith("zigbee-"):
                return transport.split("-", 1)[1]
        return "unknown"

    def _note(self, eui: str, key: str, value: str) -> None:
        if key == "host":
            self.remember_host(eui, value)

    def remember_host(self, eui: str, host: str) -> None:
        """Write down the name a device says it answers to."""
        if not self._store or not host:
            return
        row = self._row_for(eui)
        if not row:
            return
        try:
            self._store.remember_zigbee_host(row["sensor_id"], host)
        except Exception:
            LOG.exception("could not remember the host of %s", eui)

    def host_of(self, eui: str) -> Optional[str]:
        """What it answers to, from this session or from an earlier one."""
        try:
            live = (self._network.notes(eui) or {}).get("host")
        except Exception:
            live = None
        if live:
            return str(live)
        row = self._row_for(eui)
        return (row or {}).get("zigbee_host")

    def weigh_now(self, eui: str, plant_id: int) -> Dict[str, Any]:
        """Take a reading now and write it down, on request.

        Asks the device for a fresh one rather than filing whatever last
        arrived: on a five minute interval the newest reading can be most
        of five minutes old, and "record it now" meaning "record what it
        said a while ago" is the sort of small lie that makes a log hard
        to trust later. The firmware answers any command with an immediate
        report, so a harmless one is enough to provoke it.
        """
        if not self._store or not self._keeper:
            raise ValueError("no store")
        row = self._row_for(eui)
        if not row:
            raise ValueError("the hub does not know that device")

        before = None
        with self._lock:
            health = self._health.get(eui)
            if health:
                before = dict(health.last_values).get("seq")

        interval = row.get("scale_keep_every_seconds")
        try:
            self.command(eui, f"i {int(row.get('scale_interval_seconds') or 300)}".encode())
        except Exception:
            LOG.exception("could not ask %s for a reading", eui)

        # Its answer, or the one already in hand if nothing new arrives.
        deadline = time.time() + FRESH_READING_SECONDS
        values: Dict[str, Any] = {}
        while time.time() < deadline:
            with self._lock:
                health = self._health.get(eui)
                values = dict(health.last_values) if health else {}
            if values and values.get("seq") != before:
                break
            time.sleep(0.25)

        grams = values.get("grams")
        if grams is None:
            raise ValueError("the scale has not reported a weight")
        if values.get("ok") is False or values.get("saturated"):
            raise ValueError("the scale says that reading is no good")

        kept = self._keeper.keep(
            int(plant_id), int(round(float(grams))),
            source_name=row.get("display_name") or row["sensor_id"],
            every_seconds=interval,
            at_hour=row.get("scale_keep_at_hour"),
            asked_for=True,
        )
        return {
            "grams": float(grams),
            "recorded": kept is not None,
            "fresh": values.get("seq") != before,
        }

    def _note_battery(self, reading: Reading) -> None:
        """Keep the charge, for any device that reports one.

        Not scale-specific: every battery device drains, and how fast is
        the question a chart answers. The store keeps one row an hour, so
        calling this on every reading is cheap however often they arrive.
        """
        if not self._store:
            return
        percent = battery_percent(reading)
        if percent is None:
            return
        row = self._row_for(reading.eui)
        if not row or row.get("enrollment_status") != "enrolled":
            return
        try:
            self._store.record_battery(row["sensor_id"], percent,
                                       battery_volts(reading))
        except Exception:
            LOG.exception("could not record the battery of %s", reading.eui)

    def _row_for(self, eui: str) -> Optional[Dict[str, Any]]:
        """This device's sensor row, or None if the hub has no record."""
        if not self._store:
            return None
        try:
            return next((s for s in self._store.zigbee_sensors()
                         if str(s.get("identity_value", "")) == eui), None)
        except Exception:
            LOG.exception("could not look up %s", eui)
            return None

    def _file(self, reading: Reading) -> None:
        """Write the reading against whatever the device is attached to.

        Only for a device that has been let in and pointed at something: a
        reading from a device nobody has claimed is a reading from a device
        that might not be theirs.
        """
        if not self._keeper or not self._store:
            return
        grams = ScaleDriver.weight_grams(reading)
        if grams is None:
            return
        row = self._row_for(reading.eui)
        if not row or row.get("enrollment_status") != "enrolled":
            return
        plant_id = row.get("plant_id")
        if not plant_id:
            return
        self._keeper.keep(
            int(plant_id), int(round(grams)),
            source_name=row.get("display_name") or row["sensor_id"],
            every_seconds=row.get("scale_keep_every_seconds"),
            at_hour=row.get("scale_keep_at_hour"),
        )

    def _record(self, eui: str, kind: str) -> None:
        """Puts a device in the store the first time it is heard.

        It arrives unclaimed: being on the radio is not the same as being in
        use, and somebody still has to say yes.
        """
        if not self._store or eui in self._recorded:
            return
        try:
            self._store.seen_zigbee_device(eui, kind)
            self._recorded.add(eui)
        except Exception:
            LOG.exception("could not record the zigbee device %s", eui)

    def release(self) -> None:
        self._network.release()
        with self._lock:
            self._health.clear()

    def reconnect(self) -> None:
        self._network.reconnect()

    def remove(self, eui: str) -> None:
        """Gets rid of a device, whether or not it is listening.

        One verb, not two. Asking it to leave is attempted first so a device
        that is still powered does not simply rejoin and reappear; dropping
        it here happens either way, because a device that is dead or out of
        range cannot be told anything and the user should not have to know
        which case they are in.
        """
        self._network.remove(eui)
        if self._store:
            try:
                sensor = self._store.seen_zigbee_device(eui, self._kind_for(eui))
                self._store.set_sensor_archived(sensor["sensor_id"], True)
            except Exception:
                LOG.exception("could not archive %s", eui)
        with self._lock:
            self._health.pop(eui, None)
            self._recorded.discard(eui)

    def command(self, eui: str, payload: bytes, kind: str = ScaleDriver.kind) -> int:
        driver = DRIVERS.get(kind, ScaleDriver)
        request_id = self._network.send(eui, driver.cluster, driver.endpoint, payload)
        with self._lock:
            self._pending[request_id] = eui
            health = self._health.setdefault(eui, DeviceHealth(eui=eui))
            health.commands += 1
        return request_id

    # ---- events ----

    def _reading(self, reading: Reading) -> None:
        with self._lock:
            health = self._health.setdefault(reading.eui, DeviceHealth(eui=reading.eui))
            health.readings += 1
            health.last_reading = time.time()
            health.last_values = dict(reading.values)
            health.last_lqi = reading.lqi
            health.last_rssi = reading.rssi
            health.kind = reading.kind
        self._record(reading.eui, reading.kind)
        # Written down before it is judged, and whatever the device is for.
        # A report nobody has a use for today is the one that answers the
        # question asked next week, and a device nobody has claimed yet is
        # exactly the one somebody is trying to work out.
        self._keep_report(reading)
        self._file(reading)
        self._note_battery(reading)
        LOG.debug("%s: %s", reading.eui, reading.values)

    def _keep_report(self, reading: Reading) -> None:
        """Store the raw report, and now and then drop the stale ones."""
        if not self._store:
            return
        try:
            self._store.record_zigbee_report(
                reading.eui, dict(reading.values), kind=reading.kind,
                lqi=reading.lqi, rssi=reading.rssi,
            )
        except Exception:
            # A report that could not be filed is not worth dropping the
            # reading over: the live path still works without the history.
            LOG.exception("could not keep the report from %s", reading.eui)
            return
        # Pruning on a timer would need a timer. Doing it on arrival, rarely,
        # costs one DELETE an hour against an indexed column.
        now = time.time()
        if now - self._pruned_at < PRUNE_SECONDS:
            return
        self._pruned_at = now
        try:
            dropped = self._store.prune_zigbee_reports()
            if dropped:
                LOG.info("dropped %d zigbee report(s) older than the window", dropped)
        except Exception:
            LOG.exception("could not prune the zigbee reports")

    def _joined(self, device: Device) -> None:
        with self._lock:
            health = self._health.setdefault(device.eui, DeviceHealth(eui=device.eui))
            health.joins += 1
        self._record(device.eui, self._kind_for(device.eui))
        LOG.info("zigbee device joined: %s", device.eui)

    def _delivery(self, delivery: Delivery) -> None:
        with self._lock:
            eui = self._pending.pop(delivery.request_id, None)
            if eui and not delivery.delivered:
                self._health.setdefault(eui, DeviceHealth(eui=eui)).failed_commands += 1
        if not delivery.delivered:
            LOG.warning("command %d to %s not delivered: %s (status 0x%02x)",
                        delivery.request_id, eui or "?", delivery.note, delivery.status)


def _kind_of(sensor: Dict[str, Any]) -> str:
    """The device kind, recovered from how the sensor row records transport."""
    transport = str(sensor.get("transport", ""))
    return transport[len("zigbee-"):] if transport.startswith("zigbee-") else "unknown"
