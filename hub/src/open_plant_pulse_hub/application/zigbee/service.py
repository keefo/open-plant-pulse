"""The hub's glue: readings from the network into whatever the store wants.

Deliberately thin. Deciding which weights are worth keeping already lives in
ScaleService, and duplicating that judgement here would be two answers to one
question. This turns Zigbee events into the same shape the HTTP path
produces, and tracks enough per device to tell a healthy link from a quiet one.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..weights import WeightKeeper
from .drivers import (DRIVERS, HUMIDITY_CLUSTER, IAS_ZONE_CLUSTER, ON_OFF_CLUSTER,
                      TEMPERATURE_CLUSTER,
                      TUYA_CLUSTER, ZCL_HOUSEKEEPING, tuya_datapoints, zcl_command,
                      ScaleDriver,
                      battery_percent, battery_volts, decode_frame, subject_of)
from .network import SCALE_CLUSTER, Delivery, Device, Reading

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
    QUIET_SECONDS = 600

    def __init__(self, store: Any, network: Any,
                 alerts: Optional[Any] = None,
                 sounder: Optional[Any] = None) -> None:
        self._store = store
        self._network = network
        # Somewhere to send an alarm, if the hub has been given one.
        self._alerts = alerts
        # Somewhere to make a noise in the house, if there is one.
        self._sounder = sounder
        # Which rules are currently inside their condition, so one
        # fires on entering rather than on every report that follows.
        # Empty at startup on purpose: a hub that comes up to find a
        # sensor already wet should say so.
        self._firing: Dict[int, bool] = {}
        # When each last fired, so a reading resting on a threshold
        # cannot ring every time it twitches by a tenth.
        self._fired_at: Dict[int, float] = {}
        self._health: Dict[str, DeviceHealth] = {}
        self._pending: Dict[int, str] = {}
        self._lock = threading.Lock()
        # Devices already written to the store, so one that talks constantly
        # is not written on every reading.
        # What has been written down for each device, so a write is
        # skipped only when it would say the same thing again. Keyed
        # by eui alone, a device first recorded as unknown could never
        # be corrected once the hub worked out what it was.
        self._recorded: Dict[str, str] = {}
        # Device and cluster pairs already reported as unreadable.
        self._unread: set = set()
        # Alarm devices already told yes, so one request gets one answer.
        self._enrolled: set = set()
        # What the kept reports said, read once per device.
        self._remembered_values: Dict[str, Dict[str, Any]] = {}
        # When the kept reports were last weeded. Starts at zero so the first
        # report after a restart clears anything a stopped hub outlived.
        self._pruned_at: float = 0.0
        # The same judgement the HTTP poller uses. A reading is a number;
        # whether it is this plant's weight is a question with rules, and
        # there is one set of them.
        self._keeper = WeightKeeper(store) if store else None
        if hasattr(network, "reporting_for"):
            network.reporting_for = self._reporting_for
        network.on_reading(self._reading)
        network.on_joined(self._joined)
        # Not every backend has notes; a dongle under zigpy would not.
        if hasattr(network, "on_note"):
            network.on_note(self._note)
        if hasattr(network, "on_restart"):
            network.on_restart(self._gateway_restarted)
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
            "uptime_s": getattr(self._network, "uptime_s", None),
            "reset_reason": getattr(self._network, "reset_reason", None),
            # Its restarts, which matter because each one used to cost
            # the device table and with it every reading that followed.
            "restarts": self._gateway_restarts(),
        }

    def _gateway_restarts(self) -> List[Dict[str, Any]]:
        if not self._store:
            return []
        try:
            return self._store.gateway_restarts()
        except Exception:
            LOG.exception("could not read the gateway restarts")
            return []

    def _reconcile(self, devices: List[Device]) -> None:
        """Takes the kind of every device the radio already knows.

        Join events only fire on a join, so a hub that restarts finds its
        devices already there and hears nothing about them. Without this a
        plug enrolled before the hub could identify it would stay
        "unknown", with no controls, until somebody paired it again.

        Done while listing rather than at startup because the radio
        connects on its own thread and has nothing to say for the first
        few seconds. Writing is skipped once the stored kind agrees, so
        this costs a dictionary lookup on every poll but a write once.
        """
        for device in devices:
            kind = _kind_from_descriptor(device)
            if kind:
                self._record(device.eui, kind)
            if kind == "leak":
                self._enrol(device.eui)

    def devices(self) -> List[Dict[str, Any]]:
        """Every device the gateway knows, joined to what the hub knows.

        The gateway supplies what the radio can see; the store supplies what
        the device is for -- its name, the plant or room it belongs to,
        whether it has been let in at all. Neither half is the whole device.
        """
        radio = self._network.devices()
        self._reconcile(radio)
        known = {d.eui: d for d in radio}
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
            # A device that was removed and is no longer on the radio has
            # gone, and should look it. Removing one used to archive its
            # row and leave it on the panel for ever, neither waiting nor
            # accepted, which reads as the remove button having failed.
            # One still on the radio stays listed however it is filed:
            # archived and present means it came back, which is worth
            # seeing rather than hiding.
            if (sensor.get("enrollment_status") == "archived"
                    and device is None and h is None):
                continue
            kind = h.kind if h and h.kind != "unknown" else _kind_of(sensor)
            status = sensor.get("enrollment_status", "unclaimed")
            out.append({
                "eui": eui,
                "sensor_id": sensor.get("sensor_id"),
                "name": sensor.get("display_name"),
                "waiting": status != "enrolled" and (device is not None or h is not None),
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
                # What the device said it was when it joined. The hub has
                # no driver for most of these and does not need one to say
                # whose they are: a list of addresses is unreadable, and
                # "SONOFF S61SZBTPB" is the whole answer to "which is it".
                "manufacturer": device.manufacturer if device else None,
                "model": device.model if device else None,
                "device_type": device.device_type if device else None,
                "clusters": list(device.clusters) if device else [],
                "power_source": device.power_source if device else None,
                "role": device.role if device else None,
                # The device's own build, not the hub's idea of one. A
                # sensor of ours reports it in every reading; a bought
                # device says it once, during the interview.
                "firmware": device.firmware if device else None,
                # What this device has been asked to report, so the page
                # can show the choice rather than the hub's default.
                "report_max_seconds": sensor.get("zigbee_report_max_seconds"),
                "report_sensitivity": sensor.get("zigbee_report_sensitivity"),
                "kind": kind,
                "readings": h.readings if h else 0,
                "joins": h.joins if h else 0,
                "commands": h.commands if h else 0,
                "failed_commands": h.failed_commands if h else 0,
                "quiet_for_s": (round(h.seconds_quiet())
                                if h and h.seconds_quiet() is not None
                                else self._remembered_age(eui)),
                "lqi": h.last_lqi if h else None,
                "rssi": h.last_rssi if h else None,
                "last": {**self._remembered(eui),
                         **(h.last_values if h else {})},
                "notes": _notes_for(sensor),
            })
        return out

    def _remembered_age(self, eui: str) -> Optional[int]:
        """How long ago the newest kept report arrived, in seconds."""
        if not self._store:
            return None
        try:
            report = self._store.latest_zigbee_report(eui)
        except Exception:
            return None
        at = (report or {}).get("at")
        if not at:
            return None
        try:
            when = datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None
        return max(0, round((datetime.now(timezone.utc) - when).total_seconds()))

    def _remembered(self, eui: str) -> Dict[str, Any]:
        """The last thing this device said, from the kept reports.

        Read once per device and held, because this is asked on every
        poll and the answer only matters until the device has said
        each thing again for itself -- at which point the live values
        are laid over the top anyway.
        """
        if not self._store:
            return {}
        with self._lock:
            held = self._remembered_values.get(eui)
        if held is not None:
            return held
        try:
            reports = self._store.zigbee_reports(eui=eui, limit=12)
        except Exception:
            LOG.exception("could not read the last reports from %s", eui)
            return {}
        values: Dict[str, Any] = {}
        # Newest first from the store, so applied in reverse the most
        # recent value of each measurement is the one that stands.
        for report in reversed(reports):
            values.update(report.get("values") or {})
        with self._lock:
            self._remembered_values[eui] = values
        return values

    def set_reporting(self, eui: str, max_seconds: Optional[int],
                      sensitivity: Optional[str]) -> Dict[str, Any]:
        """Choose how often this device speaks, and tell it so.

        Written down first and sent second, because the setting is the
        user's answer and the device is merely the thing that has to be
        told: a sleeping sensor that cannot be reached now is asked
        again the moment it wakes, and the choice is not lost in the
        meantime.
        """
        if not self._store:
            raise LookupError("no store")
        row = self._row_for(eui)
        if not row:
            raise LookupError("device not found")
        self._store.set_zigbee_reporting(row["sensor_id"], max_seconds, sensitivity)
        asked = bool(getattr(self._network, "reconfigure", lambda _: False)(eui))
        return {"eui": eui, "max_seconds": max_seconds,
                "sensitivity": sensitivity, "asked_now": asked}

    def _reporting_for(self, eui: str) -> tuple:
        """What this device has been asked for, for the radio to apply."""
        default = (getattr(self._network, "DEFAULT_MAX_SECONDS", 1800),
                   getattr(self._network, "DEFAULT_SENSITIVITY", "normal"))
        row = self._row_for(eui) if self._store else None
        if not row:
            return default
        return (row.get("zigbee_report_max_seconds") or default[0],
                row.get("zigbee_report_sensitivity") or default[1])

    def reported(self, eui: str) -> Dict[str, Any]:
        """The last thing one device said, and how long ago.

        Narrower than devices(), which joins the whole store, because the
        caller is a light driver asking about one plug several times a
        minute and has no use for names, rooms or enrolment.
        """
        with self._lock:
            health = self._health.get(eui)
        present = any(device.eui == eui for device in self._network.devices())
        return {
            "present": present,
            "values": dict(health.last_values) if health else {},
            "quiet_for_s": health.seconds_quiet() if health else None,
        }

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

    def _gateway_restarted(self, reason: Optional[str], ran_for_s: Optional[int]) -> None:
        """Write the gateway's restart down where it will still be tomorrow."""
        if not self._store:
            return
        try:
            self._store.record_gateway_restart(reason, ran_for_s)
        except Exception:
            LOG.exception("could not record the gateway restart")

    def _note(self, eui: str, key: str, value: str) -> None:
        if key == "host":
            self.remember_host(eui, value)

    def _enrol(self, eui: str) -> None:
        """Says yes to a device asking to be enrolled as an alarm zone."""
        if eui in self._enrolled:
            return
        self._enrolled.add(eui)
        try:
            self.command(eui, "enroll")
            LOG.info("zigbee: %s asked to be enrolled as an alarm zone; said yes", eui)
        except Exception:
            self._enrolled.discard(eui)
            LOG.exception("could not enrol %s", eui)

    def remember_address(self, eui: str, address: str) -> None:
        """Write down the address a device says it answers on.

        Kept beside the name because only one of them always works: a
        .local name needs mDNS running between the hub and the device,
        and when it is not the device page offers a link that times out
        while the device is perfectly reachable by address.
        """
        if not self._store or not address:
            return
        row = self._row_for(eui)
        if not row:
            return
        try:
            self._store.remember_zigbee_address(row["sensor_id"], address)
        except Exception:
            LOG.exception("could not remember the address of %s", eui)

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
            self.command(eui, f"i {int(row.get('scale_interval_seconds') or 300)}",
                         kind=ScaleDriver.kind)
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

    def _note_unread(self, eui: str, cluster: Optional[int], payload: bytes) -> None:
        """Says once that a device sent something the hub cannot read.

        Frames with no driver are dropped, which is right -- a hub
        cannot act on what it does not understand -- but dropping them
        in silence meant a thermometer that never sent its humidity
        looked exactly like one whose humidity the hub was throwing
        away. Said once per device and cluster, so a device reporting
        every minute does not fill the log with the same sentence.
        """
        payload = bytes(payload)
        # Protocol, not data. An acknowledgement of a reporting setup is
        # the device saying yes, and reporting it as unreadable made a
        # device that had just agreed to report look broken.
        if zcl_command(payload) in ZCL_HOUSEKEEPING:
            return
        # A Tuya device sends everything down a cluster of its own, so
        # its datapoints are named individually: which number carries
        # which measurement is the maker's choice, and the only way to
        # learn it is to see them.
        points = tuya_datapoints(payload) if cluster == TUYA_CLUSTER else {}
        keys = ([(eui, cluster, point) for point in points]
                or [(eui, cluster, None)])
        with self._lock:
            fresh = [key for key in keys if key not in self._unread]
            self._unread.update(fresh)
        if not fresh:
            return
        if points:
            LOG.info("zigbee: %s sent Tuya datapoints no driver reads: %s",
                     eui, ", ".join(f"{point}={value}" for point, value in points.items()))
            return
        LOG.info("zigbee: %s sent a frame on cluster 0x%04x that no driver "
                 "reads: %s", eui, cluster or 0, payload.hex())

    def _file_air(self, reading: Reading) -> None:
        """A thermometer's measurement, filed where the rooms can see it.

        The raw Zigbee log is a record of what a device said, not a
        source the rest of the hub reads. Until this existed a
        thermometer could be assigned to a room and still leave that
        room reading "no air readings yet", because every page asks
        sensor_readings and nothing was putting anything there.

        Each measurement arrives in its own frame, so a row is written
        with whichever of the two this frame carried. They are separate
        readings because they were separately measured; pretending they
        arrived together would mean inventing a timestamp for one.
        """
        values = reading.values
        temperature = values.get("temp_c")
        humidity = values.get("rh_pct")
        if temperature is None and humidity is None:
            return
        row = self._row_for(reading.eui)
        if not row or row.get("enrollment_status") != "enrolled":
            return
        try:
            self._store.record_air_reading(
                row["sensor_id"],
                float(temperature) if temperature is not None else None,
                float(humidity) if humidity is not None else None,
            )
        except Exception:
            LOG.exception("could not file the air reading from %s", reading.eui)

    def _file_soil(self, reading: Reading) -> None:
        """A probe's measurement, filed where the plant pages read it.

        Into the same table the Bluetooth probes use, so a Zigbee probe
        arrives on a plant page already understood -- the moisture
        curve, the watering figures and the journey all read soil
        readings and none of them need to learn a second place to look.
        """
        values = reading.values
        temperature = values.get("soil_temperature_c")
        moisture = values.get("moisture_percent")
        if temperature is None and moisture is None:
            return
        row = self._row_for(reading.eui)
        if not row or row.get("enrollment_status") != "enrolled":
            return
        try:
            self._store.record_soil_reading(
                row["sensor_id"],
                float(temperature) if temperature is not None else None,
                float(moisture) if moisture is not None else None,
            )
        except Exception:
            LOG.exception("could not file the soil reading from %s", reading.eui)

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
        if not self._store:
            return
        self._file_air(reading)
        self._file_soil(reading)
        if not self._keeper:
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
        if not self._store or self._recorded.get(eui) == kind:
            return
        try:
            self._store.seen_zigbee_device(eui, kind)
            self._recorded[eui] = kind
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
            self._recorded.pop(eui, None)

    def command(self, eui: str, command: str, kind: Optional[str] = None) -> int:
        """Sends a device a word, in whatever bytes that device speaks.

        The word arrives from the outside the same for every device; what
        it becomes on the wire does not. Our scale reads short text on a
        cluster of our own, a certified plug reads ZCL on the standard
        On/Off cluster, and the web layer should not have to know which.
        """
        driver = DRIVERS.get(kind or self._kind_for(eui), ScaleDriver)
        payload = driver.payload_for(command)
        # Where the question goes. Most devices answer everything on one
        # cluster; a thermometer's temperature and humidity live on two,
        # so the driver is asked rather than assumed.
        cluster = getattr(driver, "cluster_for", lambda _: driver.cluster)(command)
        request_id = self._network.send(eui, cluster, driver.endpoint, payload)
        with self._lock:
            self._pending[request_id] = eui
            health = self._health.setdefault(eui, DeviceHealth(eui=eui))
            health.commands += 1
        return request_id

    # ---- events ----

    def _reading(self, reading: Reading) -> None:
        # The radio hands up frames it cannot interpret, including the
        # replies to the hub's own commands. A driver turns those into a
        # reading of a real kind; anything no driver claims is counted as
        # contact with the device and otherwise let go. It must never be
        # filed as the kind "frame", which is not a kind of device and
        # would replace the one the device actually is.
        if reading.kind == "frame":
            # The kind first, because what a frame means can depend on
            # the device: Tuya numbers its readings and each maker picks
            # what the numbers mean.
            kind = self._kind_for(reading.eui)
            values = decode_frame(reading.values.get("cluster"),
                                  reading.values.get("payload") or b"",
                                  kind=kind)
            if values is None:
                self._note_unread(reading.eui,
                                  reading.values.get("cluster"),
                                  reading.values.get("payload") or b"")
            if values is None or kind == "unknown":
                with self._lock:
                    health = self._health.setdefault(
                        reading.eui, DeviceHealth(eui=reading.eui))
                    health.last_reading = time.time()
                    health.last_lqi = reading.lqi
                    health.last_rssi = reading.rssi
                return
            reading = Reading(eui=reading.eui, kind=kind, values=values,
                              lqi=reading.lqi, rssi=reading.rssi)
        with self._lock:
            health = self._health.setdefault(reading.eui, DeviceHealth(eui=reading.eui))
            health.readings += 1
            health.last_reading = time.time()
            health.last_values = {**health.last_values, **reading.values}
            health.last_lqi = reading.lqi
            health.last_rssi = reading.rssi
            health.kind = reading.kind
        self._record(reading.eui, reading.kind)
        # Written down before it is judged, and whatever the device is for.
        # A report nobody has a use for today is the one that answers the
        # question asked next week, and a device nobody has claimed yet is
        # exactly the one somebody is trying to work out.
        # An alarm device asks to be let into the network before it
        # will report anything. Unanswered it sits there healthy and
        # silent, which is the worst way for a leak sensor to fail.
        if reading.values.get("enrolling"):
            self._enrol(reading.eui)
        said = reading.values.get("host")
        if isinstance(said, str) and said:
            self.remember_host(reading.eui, said)
        address = reading.values.get("ip")
        if isinstance(address, str) and address:
            self.remember_address(reading.eui, address)
        self._raise_alarm(reading)
        self._keep_report(reading)
        self._file(reading)
        self._note_battery(reading)
        LOG.debug("%s: %s", reading.eui, reading.values)

    def test_rule(self, rule_id: int) -> Dict[str, Any]:
        """Fire a rule on purpose, so it can be heard before it matters.

        Sounds once whatever the rule's repeat says: a test that had
        to be stopped would be a worse button than no button. The push
        is waited for, unlike a real alarm, because somebody is
        watching and wants to be told.
        """
        if not self._store:
            raise ValueError("no store")
        rule = next((r for r in self._store.alert_rules()
                     if r["rule_id"] == rule_id), None)
        if rule is None:
            raise ValueError("unknown rule")
        eui = rule["sensor_id"][len("zigbee-"):]
        # The threshold itself stands in for a reading: it is the
        # number the rule is about, and a made-up one would read as a
        # measurement that happened.
        example = True if rule["test"] == "true" else rule["threshold"]
        said = self._phrase(rule, example)
        sounded = False
        if self._sounder and rule["sound"]:
            sounded = bool(self._sounder.play(rule["sound"]))
        pushed = None
        if self._alerts and rule["push"]:
            pushed = self._alerts.send(
                f"Test: {said}", f"{self._describe(eui)}: {said.lower()}.",
                tags="test_tube", block=True)
        return {"sounded": sounded, "pushed": pushed,
                "sound": rule["sound"], "push": rule["push"]}

    def _raise_alarm(self, reading: Reading) -> None:
        """Check what this device just said against the rules on it.

        Every rule, every measurement, no device kind anywhere: a rule
        on air_temperature_c works on whatever reports it. This
        replaced a hard-coded leak check, which could only ever alarm
        on the one thing somebody had written into the service.

        First thing after the reading is understood, before it is
        written down: filing touches the database, and an alarm should
        leave ahead of everything else.
        """
        if not self._store:
            return
        sensor_id = f"zigbee-{reading.eui}"
        try:
            rules = self._store.alert_rules(sensor_id)
        except Exception:
            LOG.exception("could not read the alert rules for %s", sensor_id)
            return
        for rule in rules:
            if not rule["enabled"]:
                continue
            if rule["measurement"] not in reading.values:
                continue
            self._apply_rule(rule, reading.values[rule["measurement"]], reading.eui)

    @staticmethod
    def _rule_met(rule: Dict[str, Any], value: Any) -> Optional[bool]:
        """Whether the reading is inside the rule, or None if it cannot say."""
        if rule["test"] == "true":
            return bool(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if rule["threshold"] is None:
            return None
        return (number > rule["threshold"] if rule["test"] == "above"
                else number < rule["threshold"])

    def _apply_rule(self, rule: Dict[str, Any], value: Any, eui: str) -> None:
        """Fire on entering the condition, and stop on leaving it.

        On the change rather than the state. A leak sensor repeats
        itself every time it checks in and a thermometer reports every
        few minutes, so a rule that fired on the state would fire for
        hours.

        A value resting on the threshold would still ring each time it
        twitched by a tenth, so a rule that has just fired stays quiet
        for QUIET_SECONDS however often it re-enters.
        """
        met = self._rule_met(rule, value)
        if met is None:
            return
        rule_id = int(rule["rule_id"])
        now = time.time()
        with self._lock:
            before = self._firing.get(rule_id)
            if met and not before:
                recent = self._fired_at.get(rule_id, 0.0)
                if now - recent < self.QUIET_SECONDS:
                    # Inside the condition, but it said so a moment ago.
                    self._firing[rule_id] = True
                    return
                self._fired_at[rule_id] = now
            self._firing[rule_id] = bool(met)
        if met and not before:
            self._sound_and_tell(rule, value, eui)
        elif before and not met:
            # Out of the condition: whatever was ringing has no reason
            # to carry on, and nobody has to press stop.
            if self._sounder:
                self._sounder.stop(rule_id)

    def _sound_and_tell(self, rule: Dict[str, Any], value: Any, eui: str) -> None:
        where = self._describe(eui)
        said = self._phrase(rule, value)
        if self._sounder and rule["sound"]:
            self._sounder.start(rule_id=int(rule["rule_id"]),
                                sound=rule["sound"],
                                every_seconds=int(rule["repeat_seconds"]))
        if self._alerts and rule["push"]:
            self._alerts.send(said, f"{where}: {said.lower()}.",
                              urgent=bool(rule["repeat_seconds"]),
                              tags="rotating_light")

    @staticmethod
    def _phrase(rule: Dict[str, Any], value: Any) -> str:
        """What the rule has to say, in words fit for a lock screen."""
        measurement = str(rule["measurement"]).replace("_", " ")
        if rule["test"] == "true":
            return "Water detected" if rule["measurement"] == "wet" \
                else f"{measurement} reported"
        direction = "above" if rule["test"] == "above" else "below"
        return f"{measurement} {value} is {direction} {rule['threshold']:g}"

    def _describe(self, eui: str) -> str:
        """What to call a device in a message read on a lock screen.

        Its name and its room if the hub knows them, because "leak" on
        its own does not say which floor to look at.
        """
        row = self._row_for(eui)
        if not row:
            return f"A sensor ({eui})"
        name = row.get("display_name") or eui
        room = row.get("room")
        return f"{name} in {room}" if room else str(name)

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
        # What a device says it is, believed over what it has sent, because
        # a plug sends nothing until it is asked. Kind used to come only
        # from readings, so a switch sat as "unknown" -- with no controls
        # and nothing to press -- until something it never does happened.
        self._record(device.eui, _kind_from_descriptor(device) or self._kind_for(device.eui))
        LOG.info("zigbee device joined: %s", device.eui)

    def _delivery(self, delivery: Delivery) -> None:
        with self._lock:
            eui = self._pending.pop(delivery.request_id, None)
            if eui and not delivery.delivered:
                self._health.setdefault(eui, DeviceHealth(eui=eui)).failed_commands += 1
        if not delivery.delivered:
            LOG.warning("command %d to %s not delivered: %s (status 0x%02x)",
                        delivery.request_id, eui or "?", delivery.note, delivery.status)


# What a device can do, in the order the question is worth asking. The
# first match wins, so the most telling cluster comes first.
#
# Clusters and not device types: this project's own scale and a SONOFF
# thermometer both declare type 0x0302, and classifying by that label
# alone would have made every new scale a thermometer. What a device
# can do is the harder fact.
_KIND_BY_CLUSTER = (
    ((SCALE_CLUSTER,), "scale"),         # ours, and nobody else's
    # Temperature *and* humidity, not temperature alone. This project's
    # own scale registers itself as a Home Automation temperature sensor
    # so that commissioning will accept it -- see the comment in
    # sensors/hx711-scale/main/zigbee.c -- and so declares the
    # temperature cluster without being a thermometer in any sense. A
    # device that measures the air a plant sits in reports its humidity
    # too, and that is the pair this hub has a panel for.
    ((TEMPERATURE_CLUSTER, HUMIDITY_CLUSTER), "thermometer"),
    ((IAS_ZONE_CLUSTER,), "leak"),    # the alarm cluster, whatever it alarms about
    ((ON_OFF_CLUSTER,), "plug"),
)


# Devices whose own description cannot be trusted, by what they say
# they are. A Tuya device declares the standard clusters and then sends
# nothing on them, so the clusters below would call this probe an air
# thermometer -- which is how it ended up on a rooms page reporting the
# temperature of a plant pot. The model string is the only honest thing
# such a device says about itself.
#
# This is what the wider Zigbee world calls a quirk, and it grows one
# line at a time: a device that needs an entry is a device somebody has
# in their hands and can watch.
_KIND_BY_MODEL = {
    "AY-303Z": "soil",       # AOYAN soil probe: Tuya datapoints, not clusters
}


def _kind_from_descriptor(device: Device) -> Optional[str]:
    """The kind a device declares, before it has reported anything.

    A plug sends nothing until it is asked and a thermometer sleeps
    between readings, so waiting for a reading to find out what either
    one is meant leaving both as "unknown", with no controls and nothing
    to press, for as long as they behaved normally.
    """
    named = _KIND_BY_MODEL.get((device.model or "").strip())
    if named:
        return named
    clusters = set(device.clusters or ())
    for required, kind in _KIND_BY_CLUSTER:
        if clusters.issuperset(required):
            return kind
    return None


def _notes_for(sensor: Dict[str, Any]) -> Dict[str, str]:
    """What this device has told the hub about itself, in words."""
    notes = {}
    host = sensor.get("zigbee_host")
    if host:
        notes["host"] = host
    address = sensor.get("wifi_address")
    if address:
        notes["ip"] = address
    return notes


def _kind_of(sensor: Dict[str, Any]) -> str:
    """The device kind, recovered from how the sensor row records transport."""
    transport = str(sensor.get("transport", ""))
    return transport[len("zigbee-"):] if transport.startswith("zigbee-") else "unknown"
