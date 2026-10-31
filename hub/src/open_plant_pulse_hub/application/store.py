from collections import deque
from dataclasses import asdict, replace, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
from threading import Condition
from uuid import uuid4
from zoneinfo import ZoneInfo, available_timezones
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from open_plant_pulse_hub.domain import ReportPacket2, SensorReading

LOG = logging.getLogger(__name__)
from open_plant_pulse_hub.domain.care_events import CareEvent, CareEventDetector
from open_plant_pulse_hub.domain.plant_profiles import load_plant_profiles
from open_plant_pulse_hub.domain.soil_chemistry import (
    MIN_MOISTURE_PERCENT,
    PoreWaterEstimate,
    estimate_pore_water_ec,
    is_too_dry,
    nutrient_level,
)
from open_plant_pulse_hub.lighting.schedule import DEFAULT_SCHEDULE, validate_schedule

from .migrations import migrate_database


RECEIVE_DIAGNOSTIC_LIMIT = 1000
# Enough of a light's recent history to see what it did overnight.
LIGHT_EVENT_LIMIT = 200
# A light, and the plant it belongs to if any, in the order _light_payload reads.
LIGHT_QUERY = """
    SELECT lights.light_id, lights.display_name, lights.driver, lights.config,
           lights.created_at, plants.plant_id, plants.display_name, plants.archived,
           (SELECT sensors.sensor_id FROM sensors
            WHERE sensors.plant_id = plants.plant_id
              AND sensors.enrollment_status = 'enrolled'
            ORDER BY sensors.last_seen_at DESC LIMIT 1)
    FROM lights
    /* One row per light, not one per plant under it. A light may serve
       several -- a panel over a bench -- and joining the pairs listed the
       same lamp once for each, which read as duplicate hardware on the
       settings page. The plant named here is the headline one, the first
       living plant it serves; every plant under it is in
       light_assignments, which is what the scheduler reads. */
    LEFT JOIN plants ON plants.plant_id = (
        SELECT pair.plant_id FROM plant_lights AS pair
        JOIN plants AS candidate ON candidate.plant_id = pair.plant_id
        WHERE pair.light_id = lights.light_id
        ORDER BY candidate.archived, pair.plant_id
        LIMIT 1
    )
"""
# Everything a stored reading is rebuilt from, in the order _deserialize_row
# expects.
READING_COLUMNS = """
    sensor_id, report_id, observed_at, soil_temperature_c,
    moisture_percent, conductivity_us_cm, air_temperature_c,
    air_humidity_percent, soil_ph, nitrogen_mg_kg,
    phosphorus_mg_kg, potassium_mg_kg, received_at,
    soil_source_status, air_source_status, contract_version,
    battery_percent, battery_voltage_v, battery_charging
"""
# What packet 1 says about its report. The same report ID with the same
# values is the sensor repeating itself; with different values it is a conflict.
PACKET1_CONTENT_COLUMNS = """
    observed_at, soil_temperature_c, moisture_percent, conductivity_us_cm,
    air_temperature_c, air_humidity_percent, soil_source_status, air_source_status
"""
# The same for packet 2. These are also the reading's columns, in
# this order.
PACKET2_CONTENT_COLUMNS = """
    battery_percent, battery_voltage_v, soil_ph, nitrogen_mg_kg,
    phosphorus_mg_kg, potassium_mg_kg, battery_charging
"""
RAW_REPORT_LOG_LIMIT = 50
# A reading a pore-water estimate rests on: the serialized reading, when it was
# received, and the estimate it yields.
ChemistryBasis = Tuple[Dict[str, Any], str, PoreWaterEstimate]
# How far back a pore-water estimate may come from while the soil is too dry
# for a current one. Older than this it no longer describes the pot.
CHEMISTRY_LOOKBACK = timedelta(days=30)
# A reading this long after a detected watering has settled from the pour but
# not yet dried back, so it is the most comparable basis for an estimate.
POST_WATERING_BASIS_START = timedelta(minutes=30)
POST_WATERING_BASIS_END = timedelta(minutes=60)
DEVICE_CONFIG_TEXT_MAX_BYTES = 80
MIN_REPORTING_INTERVAL_MINUTES = 5
MAX_REPORTING_INTERVAL_MINUTES = 1440
# The shortest interval anybody can ask for. Below this a sensor spends its
# whole cycle advertising, reports pile up faster than the hub drains them, and
# nothing is any fresher: a report carries measurements sampled on the sensor's
# own schedule, not on this one.
MIN_REPORTING_INTERVAL_SECONDS = 30
MAX_REPORTING_INTERVAL_SECONDS = 86400
ONBOARDING_STATES = ("onboarding", "onboarded")
WIFI_STATES = ("off", "pending", "joined", "failed")
# How far an over-the-air update has got. Progress belongs to the sensor, which
# is the only one doing any of it; the hub records what it is told and, at the
# end, what it can see for itself — the version the sensor comes back reporting.
# Longer than any update takes. A download of a full image over Wi-Fi is
# seconds, and a restart is seconds more; five minutes is a sensor that is not
# coming back, which is the answer somebody needs rather than a spinner.
FIRMWARE_UPDATE_TIMEOUT_SECONDS = 300
FIRMWARE_UPDATE_STATES = (
    "idle",
    "pending",
    "commanded",
    "downloading",
    "installing",
    "rebooting",
    "succeeded",
    "failed",
)
# Every reason a sensor can give for failing to join, so the browser never has to
# show "it did not work" without saying what went wrong.
# Eight compass points, plus the two answers that are not a direction at all.
# Aspect drives how much light a plant gets, and "north-east" and "north" are
# not the same morning.
ROOM_ASPECTS = (
    "unknown",
    "none",
    "several",
    "north",
    "north_east",
    "east",
    "south_east",
    "south",
    "south_west",
    "west",
    "north_west",
)
ROOM_LIGHT_LEVELS = ("unknown", "low", "medium", "bright")
ROOM_NOTES_MAX_BYTES = 500
WIFI_FAILURES = (
    "wrong_password",
    "network_not_found",
    "association_timeout",
    "no_address",
    "unsupported_band",
)


# Who asked for the switch, in the words somebody reading a care log needs.
LIGHTING_SOURCE_TEXT = {
    "schedule": "by the plant's daily schedule",
    "watchdog": "to match the plant's schedule",
    "manual": "by hand",
}


def _lighting_summary(source: str, wanted: bool, outcome: str) -> str:
    reason = LIGHTING_SOURCE_TEXT.get(source, f"by {source}")
    state = "on" if wanted else "off"
    if outcome == "confirmed":
        return f"Switched {state} {reason}, and the light confirmed it."
    if outcome == "unreachable":
        return f"Asked to switch {state} {reason}, but the light could not be reached."
    return f"Asked to switch {state} {reason}; the light did not confirm."


# What a journey says about a plant nothing is known about yet.
EMPTY_JOURNEY = {
    "started_at": None,
    "monitored_days": 0,
    "watering_count": 0,
    "fertilizing_count": 0,
    "missed_watering_count": 0,
    "lighting_hours": 0,
}
# What a weighing was for. The ceiling is the pot once drainage has stopped,
# the floor is the pot when the plant wanted water, and everything else is
# simply where it is now.
def vapour_pressure_deficit(celsius: float, humidity_percent: float) -> float:
    """How hard the air is pulling water out of things, in kilopascals.

    Saturation vapour pressure by the Tetens equation, times the share of it
    the air is short of. Warm dry air pulls hard; cool damp air barely at
    all, and the same plant will drink two or three times as much on the
    first kind of day as on the second without anything having changed about
    the plant.

    This is the deficit of the air. The deficit at the leaf, which is what
    the stomata actually answer to, needs the leaf's own temperature and a
    houseplant hub has no way to measure that.
    """
    saturated = 0.6108 * math.exp(17.27 * celsius / (celsius + 237.3))
    return max(0.0, saturated * (1.0 - max(0.0, min(100.0, humidity_percent)) / 100.0))


# The sections of a plant's page, in the order they were built to appear.
# A saved order is repaired against this list, so it is the one place a new
# section has to be named.
# Every kind of thing the care log holds, and what to call it. A kind the
# hub does not know cannot be hidden, which is what keeps a typo in a
# request from quietly emptying somebody's log.
CARE_KINDS = {
    "watering": "Waterings",
    "watering_due": "Overdue reminders",
    "fertilizing": "Feedings",
    "lighting": "Lights on and off",
    "repotting": "Repottings",
    "drainage_assessment": "Drainage readings",
    "observation": "Readings from the weighings",
    "note": "Notes",
}

# Every section of a plant's page, in the order the page builds them. A
# section missing from here cannot be saved in a reordering at all: the
# order is validated against this tuple, so an unlisted name is refused and
# the whole arrangement is rejected with it.
def _minutes_of_day(text: Any) -> Optional[int]:
    """"HH:MM" as minutes past midnight, or None if it is not a time."""
    try:
        hours, _, minutes = str(text).partition(":")
        value = int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 1440 else None


PLANT_SECTIONS = (
    "readings", "air", "soil", "assessment", "chemistry", "light",
    "calendar", "weight", "journey", "photos", "care-log",
)

# "full" is the stated ceiling, set by hand; "watered" is an event. They
# were one kind once, so every watering redefined how much the pot holds.
WEIGHT_KINDS = ("reading", "watered", "thirsty", "dry", "full")
# How long a Zigbee device's own reports are kept. Long enough to answer a
# question about last week, short enough that nobody has to think about it:
# a device reporting every half hour leaves under four hundred rows.
# Kept for ever unless somebody says otherwise. The reports are a few
# dozen bytes each and are the only record of what a device actually
# said; a week's window quietly threw away the history worth charting.
ZIGBEE_REPORT_DAYS: Optional[int] = None
# How small a change is worth a frame. The words are the hub's; what
# each means in degrees or per cent is the driver's business, because
# only it knows what the numbers on the wire are measuring.
ZIGBEE_SENSITIVITIES = ("fine", "normal", "coarse")
# Below this a daily rate is guesswork: two weighings an hour apart say more
# about the scale than about the plant.
MIN_WEIGHT_SPAN = timedelta(hours=12)
# Two weighings minutes apart differ mostly by what the scale felt like
# saying; a rate drawn from them would be noise with a decimal point.
MIN_WATER_USE_SPAN = timedelta(hours=8)
# Care a person records themselves, because no probe could have seen it.
MANUAL_CARE_KINDS = {
    "watering": "Watered",
    "fertilizing": "Fertilized",
    "repotting": "Repotted",
    "note": "Note",
}
# Care that happens at most once in a day, whoever noticed it. A rise the probe
# reads and the hand that caused it are one feeding, not two, so they collapse
# into a single entry: the first to arrive is the one that is kept.
ONE_PER_DAY_KINDS = ("watering", "fertilizing", "repotting")
# How long after a watering was called for it is counted as missed.
MISSED_WATERING_GRACE = timedelta(hours=24)
# The longest believable stretch of light. Nobody leaves a grow light on over a
# whole night, so a stretch that appears to run longer than a day is a switching
# off that was never recorded, and the hours since are not hours of light.
MAX_LIGHTING_STRETCH = timedelta(hours=24)


def _empty_calendar_day(day: str, moisture: Optional[float] = None) -> Dict[str, Any]:
    return {
        "date": day,
        "watering_count": 0,
        "fertilizing_count": 0,
        "drying_level": 0,
        "final_moisture_percent": moisture,
    }


def _machine_zone_name() -> str:
    """What this computer calls its own zone, for showing rather than using.

    There is no portable way to ask the system for its IANA name, so the
    abbreviation it offers is the honest answer when a name is not to be had.
    """
    local = datetime.now().astimezone()
    return str(local.tzname() or local.strftime("UTC%z") or "UTC")


def hub_timezone_names() -> List[str]:
    """Every zone that can be chosen, for a menu to be built from."""
    return sorted(available_timezones())


# A sentinel for a timestamp that could not be read, so a bad row is
# skipped rather than silently counted as the current moment.
_UNPARSED = datetime(1970, 1, 1, tzinfo=timezone.utc)

def _timestamp(moment: datetime) -> str:
    # The form every stored time takes, so that text comparison orders them.
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _bound(value: str, *, after: bool = False) -> str:
    """An ISO timestamp as the exact shape the rows are stored in.

    Comparing timestamps as text is only sound while they all have the
    same precision. They do not: observed_at is written to the second,
    received_at carries microseconds, and a browser sends milliseconds,
    so "10:00:00Z" and "10:00:00.000Z" are the same instant and compare
    the other way round -- '.' sorts before 'Z', so the row would fall
    outside its own range. Every bound is therefore reshaped to the
    second before it is compared.

    A range is inclusive at both ends, so an upper bound carrying a
    fraction is rounded up: 10:00:00.500 must still admit 10:00:00.
    """
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return value
    moment = moment.astimezone(timezone.utc)
    if after and moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    return _timestamp(moment)



def _charging_column(charging: Optional[bool]) -> Optional[int]:
    # SQLite has no boolean; charging is stored as 0 or 1, null when not said.
    return None if charging is None else int(charging)


def _charging_value(column: Optional[int]) -> Optional[bool]:
    return None if column is None else bool(column)


def _seconds_between(first: Optional[str], last: Optional[str]) -> int:
    """How long a span of timestamps covers, or 0 if it cannot be read."""
    if not first or not last:
        return 0
    try:
        began = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ")
        ended = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return 0
    return max(0, int((ended - began).total_seconds()))


class ReadingStore:
    def __init__(self, history_size: int = 180, database_path: Optional[str] = None) -> None:
        self._condition = Condition()
        self._history_size = history_size
        self._history: Deque[Tuple[SensorReading, str]] = deque(maxlen=history_size)
        self._event_detector = CareEventDetector()
        catalog = load_plant_profiles()
        self._catalog = catalog
        default_profile = catalog["profiles"][catalog["default_profile"]]
        self._default_refill_below = float(default_profile["watering"]["refill_below"])
        self._sensor_refill_below: Dict[str, float] = {}
        # Resolved once rather than per care event; cleared when it changes.
        self._zone_cache: Any = None
        if database_path and database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._database = sqlite3.connect(database_path or ":memory:", check_same_thread=False)
        self._database.execute("PRAGMA foreign_keys=ON")
        if database_path and database_path != ":memory:":
            self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute("PRAGMA synchronous=NORMAL")
        migrate_database(self._database)
        for sensor_id, profile_id in self._database.execute(
            "SELECT sensor_id, profile_id FROM sensors WHERE profile_id IS NOT NULL"
        ):
            profile = catalog["profiles"].get(profile_id)
            if profile is not None:
                self._sensor_refill_below[sensor_id] = float(
                    profile["watering"]["refill_below"]
                )
        self._restore_history(history_size)

    def add(self, reading: SensorReading) -> None:
        """Store a reading that arrived without advertisement metadata.

        Real sensors reach the hub over BLE and go through add_advertisement().
        This is the plain entry point: a reading and nothing else, dated by when
        the sensor says it observed it rather than when a radio saw it.
        """
        received_datetime = datetime.now(timezone.utc)
        received_at = received_datetime.isoformat().replace("+00:00", "Z")
        observed_datetime = self._observation_datetime(reading.observed_at, received_datetime)
        with self._condition:
            self._ensure_sensor(
                reading.sensor_id,
                reading.contract_version,
                received_at,
                transport="direct",
                identity_kind="legacy",
            )
            if not self._save_reading(reading, received_at):
                return
            self._history.append((reading, received_at))
            refill_below = self._sensor_refill_below.get(
                reading.sensor_id,
                self._default_refill_below,
            )
            for event in self._event_detector.detect(reading, observed_datetime, refill_below):
                self._save_event(event)
            self._condition.notify_all()

    def add_advertisement(
        self,
        reading: SensorReading,
        received_at: str,
        observed_identifier: str,
        source_adapter: str,
        rssi: Optional[int],
        service_data: bytes,
    ) -> str:
        """Persist one decoded packet 1 and, for a new report, its reading.

        The report key is the sensor and its report ID. A report already stored
        with the same measurements is the sensor advertising it again and stores
        nothing; one stored with different measurements is a conflict, which is
        logged and never overwrites what is there. A packet 2 that arrived
        first is joined onto the reading as it is stored.
        """
        received_datetime = self._observation_datetime(received_at, datetime.now(timezone.utc))
        with self._condition:
            with self._database:
                self._ensure_sensor(
                    reading.sensor_id,
                    reading.contract_version,
                    received_at,
                    transport="bthome",
                    identity_kind="device-local-name",
                    rssi=rssi,
                    commit=False,
                )
                stored = self._database.execute(
                    f"""
                    SELECT {PACKET1_CONTENT_COLUMNS}
                    FROM sensor_readings
                    WHERE sensor_id = ? AND report_id = ?
                    """,
                    (reading.sensor_id, reading.report_id),
                ).fetchone()
                status = self._report_status(stored, self._packet1_content(reading))
                advertisement_id = self._log_advertisement(
                    reading.sensor_id,
                    reading.report_id,
                    "packet1",
                    status,
                    received_at,
                    observed_identifier,
                    source_adapter,
                    rssi,
                    reading.contract_version,
                    service_data,
                )
                if status == "accepted":
                    packet2 = self._database.execute(
                        f"""
                        SELECT {PACKET2_CONTENT_COLUMNS}
                        FROM report_packet2
                        WHERE sensor_id = ? AND report_id = ?
                        """,
                        (reading.sensor_id, reading.report_id),
                    ).fetchone()
                    if packet2 is not None:
                        reading = self._with_packet2(reading, packet2)
                    self._insert_reading(reading, received_at, advertisement_id)
                self._trim_receive_diagnostics()
            if status != "accepted":
                return status

            self._history.append((reading, received_at))
            refill_below = self._sensor_refill_below.get(
                reading.sensor_id,
                self._default_refill_below,
            )
            observed_datetime = self._observation_datetime(reading.observed_at, received_datetime)
            for event in self._event_detector.detect(reading, observed_datetime, refill_below):
                self._save_event(event)
            self._condition.notify_all()
            return status

    def add_packet2(
        self,
        packet2: ReportPacket2,
        received_at: str,
        observed_identifier: str,
        source_adapter: str,
        rssi: Optional[int],
        service_data: bytes,
    ) -> str:
        """Persist one decoded packet 2 and attach it to its reading.

        Packet 2 is kept whether or not packet 1 has arrived; a
        reading stored later picks it up. Duplicates and conflicts are judged
        against the stored packet 2 exactly as packet 1 is against the
        stored reading.
        """
        content = self._packet2_content(packet2)
        attached: Optional[Tuple[SensorReading, str]] = None
        with self._condition:
            with self._database:
                self._ensure_sensor(
                    packet2.sensor_id,
                    packet2.contract_version,
                    received_at,
                    transport="bthome",
                    identity_kind="device-local-name",
                    rssi=rssi,
                    commit=False,
                )
                stored = self._database.execute(
                    f"""
                    SELECT {PACKET2_CONTENT_COLUMNS}
                    FROM report_packet2
                    WHERE sensor_id = ? AND report_id = ?
                    """,
                    (packet2.sensor_id, packet2.report_id),
                ).fetchone()
                status = self._report_status(stored, content)
                self._log_advertisement(
                    packet2.sensor_id,
                    packet2.report_id,
                    "packet2",
                    status,
                    received_at,
                    observed_identifier,
                    source_adapter,
                    rssi,
                    packet2.contract_version,
                    service_data,
                )
                if status == "accepted":
                    self._database.execute(
                        f"""
                        INSERT INTO report_packet2 (
                            sensor_id, report_id, received_at, {PACKET2_CONTENT_COLUMNS}
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (packet2.sensor_id, packet2.report_id, received_at, *content),
                    )
                    self._database.execute(
                        """
                        UPDATE sensor_readings
                        SET battery_percent = ?, battery_voltage_v = ?, soil_ph = ?,
                            nitrogen_mg_kg = ?, phosphorus_mg_kg = ?, potassium_mg_kg = ?,
                            battery_charging = ?
                        WHERE sensor_id = ? AND report_id = ?
                        """,
                        (*content, packet2.sensor_id, packet2.report_id),
                    )
                    row = self._database.execute(
                        f"""
                        SELECT {READING_COLUMNS}
                        FROM sensor_readings
                        WHERE sensor_id = ? AND report_id = ?
                        """,
                        (packet2.sensor_id, packet2.report_id),
                    ).fetchone()
                    if row is not None:
                        item = self._deserialize_row(row)
                        attached = (item["reading"], item["received_at"])
                self._trim_receive_diagnostics()
            if attached is None:
                return status

            reading, reading_received_at = attached
            self._history = deque(
                (
                    (reading, cached_received_at)
                    if cached.sensor_id == reading.sensor_id
                    and cached.report_id == reading.report_id
                    else (cached, cached_received_at)
                    for cached, cached_received_at in self._history
                ),
                maxlen=self._history_size,
            )
            # Nutrients travel in packet 2, so a rise in them can only be
            # seen once it has joined its reading.
            observed_datetime = self._observation_datetime(
                reading.observed_at,
                self._observation_datetime(reading_received_at, datetime.now(timezone.utc)),
            )
            for event in self._event_detector.amend(reading, observed_datetime):
                self._save_event(event)
            self._condition.notify_all()
            return status

    def report_is_acknowledgeable(self, sensor_id: str, report_id: int) -> bool:
        """Return whether the sensor may be told this report is safely stored.

        That is when both of its packets are stored and nothing conflicting has
        been heard for it, and only for a sensor this hub owns: the
        acknowledgement travels over the bonded link, which an unclaimed sensor
        does not have. An acknowledged report is removed from the sensor's queue
        for good, so any doubt about what was stored means no.
        """
        with self._condition:
            row = self._database.execute(
                """
                SELECT 1
                FROM sensor_readings
                JOIN report_packet2
                  ON report_packet2.sensor_id = sensor_readings.sensor_id
                 AND report_packet2.report_id = sensor_readings.report_id
                JOIN sensors ON sensors.sensor_id = sensor_readings.sensor_id
                WHERE sensor_readings.sensor_id = ?
                  AND sensor_readings.report_id = ?
                  AND sensors.enrollment_status = 'enrolled'
                  AND NOT EXISTS (
                      SELECT 1 FROM advertisements
                      WHERE advertisements.sensor_id = sensor_readings.sensor_id
                        AND advertisements.report_id = sensor_readings.report_id
                        AND advertisements.decode_status = 'conflict'
                  )
                """,
                (sensor_id, report_id),
            ).fetchone()
        return row is not None

    def mark_reports_acknowledged(self, sensor_id: str, report_ids: Iterable[int]) -> None:
        """Record that a cumulative acknowledgement told the sensor these are stored.

        Kept as the latest acknowledgement: a report acknowledged again was
        still queued, so the latest is the one the sensor acted on.
        """
        acknowledged_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._condition, self._database:
            self._database.executemany(
                """
                UPDATE sensor_readings SET acknowledged_at = ?
                WHERE sensor_id = ? AND report_id = ?
                """,
                [(acknowledged_at, sensor_id, report_id) for report_id in report_ids],
            )

    def report_delivery(self, sensor_id: str, report_id: int) -> Tuple[bool, bool]:
        """Return whether this report is acknowledged, and whether an earlier one
        is missing.

        Missing means a report ID between the last acknowledged report and this
        one that the hub does not hold complete: something the sensor still has
        queued was not heard over the air. With nothing acknowledged yet there is
        no starting point, so nothing counts as missing.
        """
        with self._condition:
            acknowledged = self._database.execute(
                """
                SELECT 1 FROM sensor_readings
                WHERE sensor_id = ? AND report_id = ? AND acknowledged_at IS NOT NULL
                """,
                (sensor_id, report_id),
            ).fetchone()
            if acknowledged is not None:
                return True, False
            last = self._database.execute(
                """
                SELECT MAX(report_id) FROM sensor_readings
                WHERE sensor_id = ? AND acknowledged_at IS NOT NULL AND report_id < ?
                """,
                (sensor_id, report_id),
            ).fetchone()[0]
            if last is None:
                return False, False
            held = self._database.execute(
                """
                SELECT COUNT(*)
                FROM sensor_readings
                JOIN report_packet2
                  ON report_packet2.sensor_id = sensor_readings.sensor_id
                 AND report_packet2.report_id = sensor_readings.report_id
                WHERE sensor_readings.sensor_id = ?
                  AND sensor_readings.report_id > ? AND sensor_readings.report_id < ?
                """,
                (sensor_id, last, report_id),
            ).fetchone()[0]
        return False, held < report_id - last - 1

    def record_beacon(
        self,
        sensor_id: str,
        received_at: str,
        observed_identifier: str,
        source_adapter: str,
        rssi: Optional[int],
        service_data: bytes,
        contract_version: int,
    ) -> str:
        """Record that a sensor announced itself, without a reading.

        A beacon says a sensor exists and is in range, which is all a sensor
        with no report to send can honestly say. It creates the inbox entry so
        the sensor can be onboarded, and stores no measurement, so history never
        contains rows that only look like readings.
        """
        with self._condition, self._database:
            self._ensure_sensor(
                sensor_id,
                contract_version,
                received_at,
                transport="bthome",
                identity_kind="device-local-name",
                rssi=rssi,
                commit=False,
            )
            # A beacon carries nothing to tell one from the next, so each one is
            # simply presence, recorded as heard.
            self._log_advertisement(
                sensor_id,
                None,
                "beacon",
                "accepted",
                received_at,
                observed_identifier,
                source_adapter,
                rssi,
                contract_version,
                service_data,
            )
            self._trim_receive_diagnostics()
        return "accepted"

    def record_rejected_advertisement(
        self,
        received_at: str,
        observed_identifier: str,
        source_adapter: str,
        rssi: Optional[int],
        service_data: bytes,
        error: str,
        sensor_id: Optional[str] = None,
    ) -> None:
        with self._condition, self._database:
            known_sensor_id = None
            if sensor_id:
                known_sensor_id = self._database.execute(
                    "SELECT sensor_id FROM sensors WHERE sensor_id = ?", (sensor_id,)
                ).fetchone()
            self._database.execute(
                """
                INSERT INTO advertisements (
                    sensor_id, received_at, transport, source_adapter, observed_identifier,
                    rssi, payload_sha256, decode_status, decode_error, service_data
                ) VALUES (?, ?, 'bthome', ?, ?, ?, ?, 'rejected', ?, ?)
                """,
                (
                    known_sensor_id[0] if known_sensor_id else None,
                    received_at,
                    source_adapter[:64],
                    observed_identifier[:240],
                    rssi,
                    hashlib.sha256(service_data).hexdigest(),
                    error[:240],
                    service_data,
                ),
            )
            self._trim_receive_diagnostics()

    def raw_sensor_reports(
        self, sensor_id: str, limit: int = RAW_REPORT_LOG_LIMIT
    ) -> List[Dict[str, Any]]:
        """Return recent raw BTHome reports for one sensor, newest first."""
        safe_limit = max(1, min(limit, 200))
        with self._condition:
            rows = self._database.execute(
                """
                SELECT advertisement_id, received_at, report_id, packet_kind, decode_status,
                       rssi, source_adapter, observed_identifier, contract_version,
                       service_data, payload_sha256, decode_error
                FROM advertisements
                WHERE sensor_id = ?
                ORDER BY advertisement_id DESC
                LIMIT ?
                """,
                (sensor_id, safe_limit),
            ).fetchall()
        return [
            {
                "advertisement_id": row[0],
                "received_at": row[1],
                "report_id": row[2],
                "packet_kind": row[3],
                "decode_status": row[4],
                "rssi": row[5],
                "source_adapter": row[6],
                "observed_identifier": row[7],
                "contract_version": row[8],
                "service_data_hex": bytes(row[9]).hex() if row[9] is not None else None,
                "payload_sha256": row[10],
                "decode_error": row[11],
            }
            for row in rows
        ]

    def database_health(self) -> Dict[str, Any]:
        with self._condition:
            try:
                version = int(self._database.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.Error as error:
                return {"status": "error", "error": str(error)[:240]}
        return {"status": "ok", "schema_version": version}

    def sensors(self, enrollment_status: Optional[str] = None) -> List[Dict[str, Any]]:
        if enrollment_status not in (None, "unclaimed", "enrolled", "archived"):
            raise ValueError("invalid enrollment status")
        with self._condition:
            query = """
                SELECT sensor_id, identity_kind, identity_value, enrollment_status,
                       display_name,
                       (SELECT rooms.name FROM rooms
                        WHERE rooms.room_id = sensors.room_id) AS room,
                       plant_id, profile_id, first_seen_at,
                       last_seen_at, latest_rssi, transport, contract_version,
                       moisture_low_percent, conductivity_high_us_cm,
                       expected_interval_seconds, replaced_by_sensor_id,
                       device_config_revision, device_config_applied_revision,
                       device_config_attempted_at, device_config_error,
                       sensor_reporting_interval_seconds, onboarding_state,
                       wifi_enabled, wifi_state, wifi_failure, wifi_address, room_id,
                       firmware_version, station_checked_at, firmware_update_digest,
                       firmware_update_version, firmware_update_state,
                       firmware_update_id, firmware_update_percent,
                       firmware_update_error, firmware_update_started_at,
                       soil_ph_offset, soil_ph_reference, soil_ph_reference_raw,
                       soil_ph_reference_at, soil_ph_reference_ec,
                       scale_address, scale_keep_every_seconds,
                       scale_grams, scale_status, scale_stable, scale_read_at, scale_error,
                       scale_interval_seconds, scale_firmware, scale_build, scale_seq,
                       scale_battery_volts, scale_battery_percent,
                       scale_keep_at_hour, zigbee_host,
                       (SELECT plants.display_name FROM plants
                        WHERE plants.plant_id = sensors.plant_id) AS plant_name,
                       zigbee_report_max_seconds, zigbee_report_sensitivity
                FROM sensors
            """
            parameters: Tuple[Any, ...] = ()
            if enrollment_status is not None:
                query += " WHERE enrollment_status = ?"
                parameters = (enrollment_status,)
            query += " ORDER BY last_seen_at DESC, sensor_id"
            return [
                self._decorate_sensor(self._sensor_payload(row))
                for row in self._database.execute(query, parameters)
            ]

    def sensor(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT sensor_id, identity_kind, identity_value, enrollment_status,
                       display_name,
                       (SELECT rooms.name FROM rooms
                        WHERE rooms.room_id = sensors.room_id) AS room,
                       plant_id, profile_id, first_seen_at,
                       last_seen_at, latest_rssi, transport, contract_version,
                       moisture_low_percent, conductivity_high_us_cm,
                       expected_interval_seconds, replaced_by_sensor_id,
                       device_config_revision, device_config_applied_revision,
                       device_config_attempted_at, device_config_error,
                       sensor_reporting_interval_seconds, onboarding_state,
                       wifi_enabled, wifi_state, wifi_failure, wifi_address, room_id,
                       firmware_version, station_checked_at, firmware_update_digest,
                       firmware_update_version, firmware_update_state,
                       firmware_update_id, firmware_update_percent,
                       firmware_update_error, firmware_update_started_at,
                       soil_ph_offset, soil_ph_reference, soil_ph_reference_raw,
                       soil_ph_reference_at, soil_ph_reference_ec,
                       scale_address, scale_keep_every_seconds,
                       scale_grams, scale_status, scale_stable, scale_read_at, scale_error,
                       scale_interval_seconds, scale_firmware, scale_build, scale_seq,
                       scale_battery_volts, scale_battery_percent,
                       scale_keep_at_hour, zigbee_host
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            return self._decorate_sensor(self._sensor_payload(row)) if row is not None else None

    def hub_settings(self) -> Dict[str, Any]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT reporting_interval_minutes, plant_section_order,
                       care_log_hidden_kinds, timezone, zigbee_report_keep_days,
                       ntfy_url
                FROM hub_settings WHERE singleton_id = 1
                """
            ).fetchone()
        assert row is not None
        chosen = row[3]
        return {
            "reporting_interval_minutes": int(row[0]),
            "plant_section_order": self._section_order(row[1]),
            "care_log_hidden_kinds": self._hidden_kinds(row[2]),
            # What was chosen, which may be nothing, and what that works out
            # to. A page showing only the first cannot say what "6 am" means
            # on a hub nobody has configured.
            "timezone": chosen,
            "timezone_effective": chosen or _machine_zone_name(),
            # None means keep every report there has ever been.
            "zigbee_report_keep_days": int(row[4]) if row[4] is not None else None,
            # Where an alarm goes. Empty rather than None because the
            # field that shows it is a text box, and "None" is a word
            # somebody would have to delete before typing.
            "ntfy_url": row[5] or "",
        }

    # The sounds macOS ships. A rule naming anything else would be a
    # rule that silently does nothing, so the name is checked here
    # rather than discovered when the alarm fails to sound.
    ALERT_SOUNDS = ("Basso", "Blow", "Bottle", "Frog", "Funk", "Glass",
                    "Hero", "Morse", "Ping", "Pop", "Purr", "Sosumi",
                    "Submarine", "Tink")
    ALERT_TESTS = ("above", "below", "true")
    # Sounding more often than this is a fire alarm, not a plant hub.
    MIN_REPEAT_SECONDS = 5

    def measurements_seen(self, sensor_id: str, limit: int = 400) -> List[str]:
        """What this device has actually reported, for a rule to pick from.

        Read off the kept reports rather than a list somebody
        maintains: a curated list would mean every new kind of sensor
        arrives unable to raise an alarm until the hub is edited,
        which is the mistake the charts made before they were keyed to
        the record instead of the device kind.
        """
        eui = sensor_id[len("zigbee-"):] if sensor_id.startswith("zigbee-") else sensor_id
        with self._condition:
            rows = self._database.execute(
                """
                SELECT values_json FROM zigbee_reports
                WHERE eui = ? ORDER BY report_id DESC LIMIT ?
                """,
                (eui, limit),
            ).fetchall()
        seen = set()
        for (raw,) in rows:
            try:
                values = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for key, value in (values or {}).items():
                # Only what a rule could test. A payload or a cluster
                # number is not a measurement of anything.
                if isinstance(value, bool) or isinstance(value, (int, float)):
                    seen.add(key)
        return sorted(seen)

    def alert_rules(self, sensor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every alert rule, or one device's.

        The service reads the whole list on every reading, so this
        stays a plain select: a rule is six small columns and there
        will never be many.
        """
        where = "WHERE sensor_id = ?" if sensor_id else ""
        values = (sensor_id,) if sensor_id else ()
        with self._condition:
            rows = self._database.execute(
                f"""
                SELECT rule_id, sensor_id, measurement, test, threshold,
                       sound, repeat_seconds, push, enabled
                FROM alert_rules {where}
                ORDER BY measurement, rule_id
                """,
                values,
            ).fetchall()
        return [
            {"rule_id": row[0], "sensor_id": row[1], "measurement": row[2],
             "test": row[3], "threshold": row[4], "sound": row[5],
             "repeat_seconds": int(row[6]), "push": bool(row[7]),
             "enabled": bool(row[8])}
            for row in rows
        ]

    def _validate_rule(self, measurement: str, test: str,
                       threshold: Optional[float], sound: Optional[str],
                       repeat_seconds: int, push: bool) -> None:
        if not (measurement or "").strip():
            raise ValueError("a rule needs a measurement")
        if test not in self.ALERT_TESTS:
            raise ValueError(f"a rule tests {', '.join(self.ALERT_TESTS)}")
        if test != "true" and threshold is None:
            raise ValueError("above and below need a number to compare with")
        if sound and sound not in self.ALERT_SOUNDS:
            raise ValueError("that is not a sound this computer has")
        if repeat_seconds and repeat_seconds < self.MIN_REPEAT_SECONDS:
            raise ValueError(
                f"repeat at most every {self.MIN_REPEAT_SECONDS} seconds")
        # A rule that neither sounds nor pushes is a rule that does
        # nothing, and saving one would read as a feature not working.
        if not sound and not push:
            raise ValueError("a rule has to sound, or tell the phone, or both")

    def add_alert_rule(self, sensor_id: str, measurement: str, test: str,
                       threshold: Optional[float] = None,
                       sound: Optional[str] = None, repeat_seconds: int = 0,
                       push: bool = True) -> Dict[str, Any]:
        self._validate_rule(measurement, test, threshold, sound,
                            repeat_seconds, push)
        with self._condition, self._database:
            known = self._database.execute(
                "SELECT 1 FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
            if known is None:
                raise ValueError("unknown sensor")
            cursor = self._database.execute(
                """
                INSERT INTO alert_rules
                    (sensor_id, measurement, test, threshold, sound,
                     repeat_seconds, push)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sensor_id, measurement.strip(), test,
                 None if test == "true" else float(threshold),
                 sound or None, int(repeat_seconds), 1 if push else 0),
            )
        return next(r for r in self.alert_rules(sensor_id)
                    if r["rule_id"] == cursor.lastrowid)

    def update_alert_rule(self, rule_id: int, **fields: Any) -> Dict[str, Any]:
        """Change a rule, leaving out what was not mentioned."""
        current = next((r for r in self.alert_rules()
                        if r["rule_id"] == rule_id), None)
        if current is None:
            raise ValueError("unknown rule")
        merged = {**current, **{k: v for k, v in fields.items() if k in current}}
        self._validate_rule(merged["measurement"], merged["test"],
                            merged["threshold"], merged["sound"],
                            int(merged["repeat_seconds"]), merged["push"])
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE alert_rules
                SET measurement = ?, test = ?, threshold = ?, sound = ?,
                    repeat_seconds = ?, push = ?, enabled = ?
                WHERE rule_id = ?
                """,
                (merged["measurement"], merged["test"],
                 None if merged["test"] == "true" else merged["threshold"],
                 merged["sound"] or None, int(merged["repeat_seconds"]),
                 1 if merged["push"] else 0, 1 if merged["enabled"] else 0,
                 rule_id),
            )
        return next(r for r in self.alert_rules() if r["rule_id"] == rule_id)

    def delete_alert_rule(self, rule_id: int) -> None:
        with self._condition, self._database:
            gone = self._database.execute(
                "DELETE FROM alert_rules WHERE rule_id = ?", (rule_id,)
            ).rowcount
        if gone == 0:
            raise ValueError("unknown rule")

    FAN_MODES = ("off", "manual", "auto")
    FAN_FIELDS = ("enabled", "mode", "moonraker_url", "fan_name", "sensor_id",
                  "target_rh", "kp", "ki", "kd", "manual_speed",
                  "min_speed", "max_speed", "stale_after_s", "pulse_window_s")

    def latest_humidity(self, sensor_id: str) -> Optional[Tuple[float, str]]:
        """The newest humidity this sensor reported, and when.

        Not the newest row: these thermometers send temperature and
        humidity in separate reports, so about half the latest rows
        have no humidity in them at all. A controller reading only the
        newest row concluded the sensor had stopped and switched the
        fan off every other minute.
        """
        with self._condition:
            row = self._database.execute(
                """
                SELECT air_humidity_percent, observed_at FROM sensor_readings
                WHERE sensor_id = ? AND air_humidity_percent IS NOT NULL
                ORDER BY reading_id DESC LIMIT 1
                """,
                (sensor_id,),
            ).fetchone()
        return (float(row[0]), row[1]) if row else None

    def record_fan(self, speed: float, rh: Optional[float], mode: str,
                   why: str) -> None:
        """Write down what the fan was told, if it is news.

        Only when the speed changes: the loop wakes twice a minute and
        the fan moves a few times an hour, so a row per tick would be
        thousands a day all saying the same thing. Two rows with a gap
        between them mean the fan held in between, which is what a
        step chart draws.
        """
        with self._condition, self._database:
            last = self._database.execute(
                "SELECT speed FROM fan_log ORDER BY entry_id DESC LIMIT 1"
            ).fetchone()
            if last is not None and abs(float(last[0]) - float(speed)) < 0.001:
                return
            self._database.execute(
                "INSERT INTO fan_log (at, speed, rh, mode, why) VALUES (?, ?, ?, ?, ?)",
                (_timestamp(datetime.now(timezone.utc)), float(speed),
                 None if rh is None else float(rh), mode, why))

    def fan_log(self, hours: int = 24) -> List[Dict[str, Any]]:
        """What the fan was told, oldest first, over a window."""
        since = _timestamp(datetime.now(timezone.utc) - timedelta(hours=hours))
        with self._condition:
            rows = self._database.execute(
                "SELECT at, speed, rh, mode, why FROM fan_log "
                "WHERE at >= ? ORDER BY at", (since,)).fetchall()
        return [{"at": r[0], "speed": r[1], "rh": r[2], "mode": r[3], "why": r[4]}
                for r in rows]

    def fan_control(self) -> Dict[str, Any]:
        """The one fan's settings. See docs/proposals/vzbot-humidity-control.md."""
        with self._condition:
            row = self._database.execute(
                f"SELECT {', '.join(self.FAN_FIELDS)} FROM fan_control "
                "WHERE singleton_id = 1").fetchone()
        assert row is not None
        settings = dict(zip(self.FAN_FIELDS, row))
        settings["enabled"] = bool(settings["enabled"])
        return settings

    def set_fan_control(self, **fields: Any) -> Dict[str, Any]:
        """Change what was mentioned, leaving the rest alone."""
        current = self.fan_control()
        wanted = {k: v for k, v in fields.items() if k in self.FAN_FIELDS}
        merged = {**current, **wanted}
        if merged["mode"] not in self.FAN_MODES:
            raise ValueError(f"a fan is {', '.join(self.FAN_MODES)}")
        url = str(merged["moonraker_url"] or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("the printer address must start with http:// or https://")
        if not str(merged["fan_name"] or "").strip():
            raise ValueError("a fan needs a name")
        if not 0 <= float(merged["target_rh"]) <= 100:
            raise ValueError("a humidity target is between 0 and 100")
        for speed in ("manual_speed", "min_speed", "max_speed"):
            if not 0 <= float(merged[speed]) <= 1:
                raise ValueError("a fan speed is between 0 and 1")
        if float(merged["min_speed"]) > float(merged["max_speed"]):
            raise ValueError("the floor cannot be above the ceiling")
        if not 20 <= int(merged["pulse_window_s"]) <= 900:
            raise ValueError("a pulse window is between 20 and 900 seconds")
        if int(merged["stale_after_s"]) < 60:
            raise ValueError("a reading is not stale inside a minute")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE fan_control SET "
                + ", ".join(f"{name} = ?" for name in self.FAN_FIELDS)
                + " WHERE singleton_id = 1",
                tuple(1 if name == "enabled" and merged[name] else
                      0 if name == "enabled" else merged[name]
                      for name in self.FAN_FIELDS))
        return self.fan_control()

    def set_alert_url(self, url: Optional[str]) -> Dict[str, Any]:
        """Where an alarm is sent, or nowhere.

        Checked for shape, not for reachability: whether the address
        answers is what the test button is for, and a hub that refused
        to save because a phone was off would be unusable on a bad
        afternoon.
        """
        address = (url or "").strip()
        if address and not address.startswith(("http://", "https://")):
            raise ValueError("the address must start with http:// or https://")
        if len(address) > 500:
            raise ValueError("that address is too long")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET ntfy_url = ? WHERE singleton_id = 1",
                (address or None,),
            )
        return self.hub_settings()

    def set_zigbee_reporting(self, sensor_id: str, max_seconds: Optional[int],
                             sensitivity: Optional[str]) -> Dict[str, Any]:
        """How often this device should speak, and how small a change counts.

        Per device rather than per hub, because a bedroom nobody is
        watching and a propagator under a lamp want different answers
        from identical hardware.

        Null for either means the hub's default for that kind of
        device, which is what every device starts on.
        """
        if max_seconds is not None:
            max_seconds = int(max_seconds)
            if not 60 <= max_seconds <= 43200:
                raise ValueError("report at least once every 12 hours, and at most once a minute")
        if sensitivity is not None and sensitivity not in ZIGBEE_SENSITIVITIES:
            raise ValueError(f"sensitivity must be one of {', '.join(ZIGBEE_SENSITIVITIES)}")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                   SET zigbee_report_max_seconds = ?, zigbee_report_sensitivity = ?
                 WHERE sensor_id = ?
                """,
                (max_seconds, sensitivity, sensor_id),
            )
        sensor = self.sensor(sensor_id)
        if sensor is None:
            raise LookupError("sensor not found")
        return sensor

    def set_zigbee_report_keep_days(self, days: Optional[int]) -> Dict[str, Any]:
        """How long to keep raw Zigbee reports, or None to keep them all.

        Setting a window prunes immediately rather than at some later
        moment: somebody who has just asked for less history expects to
        get it, and finding the old rows still there an hour later
        reads as the setting having failed.
        """
        if days is not None:
            days = int(days)
            if days < 1:
                raise ValueError("keep at least a day, or nothing at all")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET zigbee_report_keep_days = ? WHERE singleton_id = 1",
                (days,),
            )
        self.prune_zigbee_reports(days)
        return self.hub_settings()

    def zigbee_report_summary(self) -> Dict[str, Any]:
        """How many reports are kept and how far back they go.

        Shown next to the setting, because "keep everything" is only a
        comfortable choice for somebody who can see what everything
        amounts to.
        """
        with self._condition:
            row = self._database.execute(
                "SELECT COUNT(*), MIN(at) FROM zigbee_reports"
            ).fetchone()
        return {"reports": int(row[0] or 0), "oldest_at": row[1]}

    def set_hub_timezone(self, name: Optional[str]) -> Dict[str, Any]:
        """Choose the zone every hour-of-the-day decision is made in.

        None hands it back to the machine, which is the right answer for a
        hub on the kitchen computer and the only one before anybody chooses.
        """
        chosen = (name or "").strip() or None
        if chosen is not None:
            try:
                ZoneInfo(chosen)
            except Exception as error:
                raise ValueError(f"unknown time zone: {chosen}") from error
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET timezone = ? WHERE singleton_id = 1", (chosen,)
            )
            self._zone_cache = None
        return self.hub_settings()

    def hub_zone(self) -> Any:
        """The tzinfo every wall-clock decision in the hub is made against."""
        with self._condition:
            cached = self._zone_cache
            if cached is not None:
                return cached
            row = self._database.execute(
                "SELECT timezone FROM hub_settings WHERE singleton_id = 1"
            ).fetchone()
        name = row[0] if row else None
        zone: Any
        if name:
            try:
                zone = ZoneInfo(name)
            except Exception:
                # A zone that was valid when it was saved and is not now --
                # a renamed one, a trimmed tzdata -- should not stop the hub
                # keeping time. It falls back and says so in the log.
                LOG.warning("unknown time zone %r; using this machine's", name)
                zone = datetime.now().astimezone().tzinfo
        else:
            zone = datetime.now().astimezone().tzinfo
        with self._condition:
            self._zone_cache = zone
        return zone

    @staticmethod
    def _section_order(stored: Optional[str]) -> List[str]:
        """The saved order, repaired against the sections that exist.

        Names the hub does not know are dropped, and sections the saved
        order has never heard of are put back where they were built to go.
        A release that adds a section therefore shows it -- at the end,
        where it can be found and moved -- rather than not at all.
        """
        try:
            saved = json.loads(stored) if stored else []
        except (TypeError, ValueError):
            saved = []
        kept = list(dict.fromkeys(name for name in saved if name in PLANT_SECTIONS))
        # Anything the saved order has never heard of goes after it, in the
        # order it was built in. Trying to slot a new section in beside its
        # neighbours sounds kinder and is not: every rule for it reorders
        # things somebody deliberately arranged. At the end it is visible,
        # it is in one piece, and it is one drag from where they want it.
        return kept + [name for name in PLANT_SECTIONS if name not in set(kept)]

    @staticmethod
    def _hidden_kinds(stored: Optional[str]) -> List[str]:
        try:
            saved = json.loads(stored) if stored else []
        except (TypeError, ValueError):
            saved = []
        return [kind for kind in CARE_KINDS if kind in set(saved)]

    def set_care_log_hidden_kinds(self, kinds: List[str]) -> Dict[str, Any]:
        """Which kinds of event the care log leaves out, for every plant."""
        if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
            raise ValueError("care_log_hidden_kinds must be a list of kinds")
        unknown = [kind for kind in kinds if kind not in CARE_KINDS]
        if unknown:
            raise ValueError(f"unknown care kind: {unknown[0]}")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET care_log_hidden_kinds = ? WHERE singleton_id = 1",
                (json.dumps(self._hidden_kinds(json.dumps(kinds))),),
            )
        return self.hub_settings()

    def set_plant_section_order(self, order: List[str]) -> Dict[str, Any]:
        if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
            raise ValueError("plant_section_order must be a list of section names")
        unknown = [name for name in order if name not in PLANT_SECTIONS]
        if unknown:
            raise ValueError(f"unknown section: {unknown[0]}")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET plant_section_order = ? WHERE singleton_id = 1",
                (json.dumps(self._section_order(json.dumps(order))),),
            )
        return self.hub_settings()

    def set_reporting_interval(self, reporting_interval_minutes: int) -> Dict[str, int]:
        if not MIN_REPORTING_INTERVAL_MINUTES <= reporting_interval_minutes <= MAX_REPORTING_INTERVAL_MINUTES:
            raise ValueError("reporting_interval_minutes must be between 5 and 1440")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE hub_settings SET reporting_interval_minutes = ?
                WHERE singleton_id = 1
                """,
                (reporting_interval_minutes,),
            )
        return self.hub_settings()

    def rooms(self) -> List[Dict[str, Any]]:
        """Every room, with what is in it.

        Sensors and plants both, because both keep a room from being
        deleted, and a page offering to delete one should be able to
        say what is in the way.
        """
        with self._condition:
            return [
                {
                    "room_id": row[0],
                    "name": row[1],
                    "aspect": row[2],
                    "light": row[3],
                    "notes": row[4],
                    "sensor_count": row[5],
                    "plant_count": row[6],
                }
                for row in self._database.execute(
                    """
                    SELECT rooms.room_id, rooms.name, rooms.aspect, rooms.light, rooms.notes,
                           (SELECT COUNT(*) FROM sensors
                            WHERE sensors.room_id = rooms.room_id
                              AND sensors.enrollment_status = 'enrolled'),
                           (SELECT COUNT(*) FROM plants
                            WHERE plants.room_id = rooms.room_id
                              AND COALESCE(plants.archived, 0) = 0)
                    FROM rooms ORDER BY rooms.name
                    """
                )
            ]

    def rooms_overview(self) -> List[Dict[str, Any]]:
        """Every room with what is in it and what the air is doing.

        A room at a time rather than a plant at a time. Air belongs to the
        room, so a page of rooms is the natural place to read it: one
        thermometer answers for everything on that windowsill, and
        comparing rooms is how you notice that one of them is the problem.
        """
        rooms = self.rooms()
        for room in rooms:
            room["air_sources"] = self.room_air_sources(room["room_id"])
            room["air"] = self.room_air(room["room_id"])
        return rooms

    def room_detail(self, room_id: int) -> Optional[Dict[str, Any]]:
        """One room, with the sensors and plants that are in it.

        The room's own page answers "what is in here?", which the list
        of rooms could only answer with a number. Both come from the
        same two tables the delete check reads, so what the page shows
        is what stands in the way of deleting it.
        """
        room = next((r for r in self.rooms() if r["room_id"] == room_id), None)
        if room is None:
            return None
        with self._condition:
            room["sensors"] = [
                {"sensor_id": row[0], "display_name": row[1],
                 "transport": row[2], "last_seen_at": row[3]}
                for row in self._database.execute(
                    """
                    SELECT sensor_id, display_name, transport, last_seen_at
                    FROM sensors
                    WHERE room_id = ? AND enrollment_status = 'enrolled'
                    ORDER BY display_name
                    """,
                    (room_id,),
                )
            ]
            room["plants"] = [
                {"plant_id": row[0], "display_name": row[1], "profile_id": row[2]}
                for row in self._database.execute(
                    """
                    SELECT plant_id, display_name, profile_id
                    FROM plants
                    WHERE room_id = ? AND COALESCE(archived, 0) = 0
                    ORDER BY display_name
                    """,
                    (room_id,),
                )
            ]
        room["air"] = self.room_air(room_id)
        room["air_sources"] = self.room_air_sources(room_id)
        return room

    def _validate_room(self, name: str, aspect: str, light: str, notes: Optional[str]) -> str:
        name = name.strip()
        self._validate_device_text(name, "name", required=True)
        if aspect not in ROOM_ASPECTS:
            raise ValueError("aspect is invalid")
        if light not in ROOM_LIGHT_LEVELS:
            raise ValueError("light is invalid")
        if notes is not None and len(notes.encode("utf-8")) > ROOM_NOTES_MAX_BYTES:
            raise ValueError("notes is too long")
        return name

    def create_room(
        self,
        name: str,
        aspect: str = "unknown",
        light: str = "unknown",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = self._validate_room(name, aspect, light, notes)
        with self._condition, self._database:
            existing = self._database.execute(
                "SELECT 1 FROM rooms WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                raise ValueError("a room with that name already exists")
            self._database.execute(
                "INSERT INTO rooms (name, aspect, light, notes) VALUES (?, ?, ?, ?)",
                (name, aspect, light, notes or None),
            )
        return next(room for room in self.rooms() if room["name"] == name)

    def update_room(
        self,
        room_id: int,
        name: str,
        aspect: str = "unknown",
        light: str = "unknown",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = self._validate_room(name, aspect, light, notes)
        with self._condition, self._database:
            clash = self._database.execute(
                "SELECT 1 FROM rooms WHERE name = ? AND room_id != ?", (name, room_id)
            ).fetchone()
            if clash is not None:
                raise ValueError("a room with that name already exists")
            updated = self._database.execute(
                "UPDATE rooms SET name = ?, aspect = ?, light = ?, notes = ? WHERE room_id = ?",
                (name, aspect, light, notes or None, room_id),
            ).rowcount
            if updated == 0:
                raise ValueError("room_id is unknown")
        return next(room for room in self.rooms() if room["room_id"] == room_id)

    def delete_room(self, room_id: int) -> None:
        """Remove a room. A room still holding sensors or plants is kept.

        Both are counted, because both carry a foreign key to the room.
        Only sensors were checked once, so deleting a room with a plant
        in it raised IntegrityError out of the request thread: the
        connection died mid-reply and the page said "Failed to fetch"
        with nothing to explain it.
        """
        with self._condition, self._database:
            in_the_way = []
            sensors = self._database.execute(
                """
                SELECT COUNT(*) FROM sensors
                WHERE room_id = ? AND enrollment_status = 'enrolled'
                """,
                (room_id,),
            ).fetchone()[0]
            if sensors:
                in_the_way.append(f"{sensors} sensor" + ("s" if sensors > 1 else ""))
            plants = self._database.execute(
                """
                SELECT COUNT(*) FROM plants
                WHERE room_id = ? AND COALESCE(archived, 0) = 0
                """,
                (room_id,),
            ).fetchone()[0]
            if plants:
                in_the_way.append(f"{plants} plant" + ("s" if plants > 1 else ""))
            if in_the_way:
                # Named, so the page can say what to move rather than
                # only that something is in the way.
                raise ValueError(
                    f"This room still holds {' and '.join(in_the_way)}. Move "
                    + ("it" if sensors + plants == 1 else "them")
                    + " out first.")
            # Nothing the page shows is in this room. What is left
            # holding its id is archived or unenrolled -- counted
            # nowhere, shown nowhere, but still a foreign key -- so it
            # is released rather than refused on its behalf.
            self._database.execute(
                "UPDATE plants SET room_id = NULL WHERE room_id = ?", (room_id,))
            self._database.execute(
                "UPDATE sensors SET room_id = NULL WHERE room_id = ?", (room_id,))
            deleted = self._database.execute(
                "DELETE FROM rooms WHERE room_id = ?", (room_id,)
            ).rowcount
            if deleted == 0:
                raise ValueError("room_id is unknown")

    # --- grow lights ------------------------------------------------------
    # A light's settings belong to its driver; the store keeps them as JSON and
    # never looks inside. Which lights a plant uses, and when they are lit, belong
    # to the plant: they are keyed by plant, and reached by the plant's sensor
    # only because that is how every page addresses a plant.

    def lights(self) -> List[Dict[str, Any]]:
        with self._condition:
            return [
                self._light_payload(row)
                for row in self._database.execute(
                    LIGHT_QUERY + " ORDER BY lights.display_name COLLATE NOCASE, lights.light_id"
                )
            ]

    def light(self, light_id: str) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                LIGHT_QUERY + " WHERE lights.light_id = ?", (light_id,)
            ).fetchone()
        return self._light_payload(row) if row is not None else None

    def create_light(
        self, light_id: str, display_name: str, driver: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        display_name = self._validate_light_name(display_name)
        with self._condition, self._database:
            self._refuse_light_name_clash(display_name, light_id)
            self._database.execute(
                "INSERT INTO lights (light_id, display_name, driver, config) VALUES (?, ?, ?, ?)",
                (light_id, display_name, driver, json.dumps(config, sort_keys=True)),
            )
        light = self.light(light_id)
        assert light is not None
        return light

    def update_light(
        self, light_id: str, display_name: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        display_name = self._validate_light_name(display_name)
        with self._condition, self._database:
            self._refuse_light_name_clash(display_name, light_id)
            updated = self._database.execute(
                "UPDATE lights SET display_name = ?, config = ? WHERE light_id = ?",
                (display_name, json.dumps(config, sort_keys=True), light_id),
            ).rowcount
            if updated == 0:
                raise ValueError("light not found")
        light = self.light(light_id)
        assert light is not None
        return light

    def remember_light_facts(self, light_id: str, facts: Dict[str, Any]) -> None:
        """Keep what a driver found out, such as the light's new address."""
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT config FROM lights WHERE light_id = ?", (light_id,)
            ).fetchone()
            if row is None:
                return
            config = json.loads(row[0])
            config.update(facts)
            self._database.execute(
                "UPDATE lights SET config = ? WHERE light_id = ?",
                (json.dumps(config, sort_keys=True), light_id),
            )

    def delete_light(self, light_id: str) -> None:
        with self._condition, self._database:
            deleted = self._database.execute(
                "DELETE FROM lights WHERE light_id = ?", (light_id,)
            ).rowcount
        if deleted == 0:
            raise ValueError("light not found")

    def plant_lighting(self, sensor_id: str) -> Dict[str, Any]:
        """The lighting of whichever plant this sensor watches."""
        with self._condition:
            plant_id = self._plant_of_sensor(sensor_id)
        lighting = self.plant_lighting_of(plant_id)
        return {**lighting, "sensor_id": sensor_id}

    def plant_lighting_of(self, plant_id: int) -> Dict[str, Any]:
        """Which lamps a plant stands under.

        Lights have always belonged to the plant rather than to the sensor
        watching it, which is why a plant with no sensor has them too.

        When they are lit is not here: a lamp is one switch, so its hours
        belong to the lamp. A plant that asked for its own window would be
        asking a panel shared with three others to keep four.
        """
        with self._condition:
            if self.plant(plant_id) is None:
                raise ValueError("unknown plant")
            chosen = self._database.execute(
                "SELECT light_id FROM plant_lights"
                " WHERE plant_id = ? ORDER BY light_id",
                (plant_id,),
            ).fetchall()
        return {
            "plant_id": plant_id,
            "light_ids": [row[0] for row in chosen],
        }

    def set_plant_lighting(self, sensor_id: str, light_ids: List[str]) -> Dict[str, Any]:
        with self._condition:
            plant_id = self._plant_of_sensor(sensor_id)
        lighting = self.set_plant_lighting_of(plant_id, light_ids)
        return {**lighting, "sensor_id": sensor_id}

    def set_plant_lighting_of(self, plant_id: int, light_ids: List[str]) -> Dict[str, Any]:
        if not isinstance(light_ids, list) or not all(isinstance(i, str) for i in light_ids):
            raise ValueError("light_ids must be a list of light IDs")
        light_ids = list(dict.fromkeys(light_ids))
        with self._condition, self._database:
            if self.plant(plant_id) is None:
                raise ValueError("unknown plant")
            for light_id in light_ids:
                owner = self._database.execute(
                    """
                    SELECT lights.display_name, plant_lights.plant_id, plants.display_name
                    FROM lights
                    LEFT JOIN plant_lights ON plant_lights.light_id = lights.light_id
                    LEFT JOIN plants ON plants.plant_id = plant_lights.plant_id
                    WHERE lights.light_id = ?
                    """,
                    (light_id,),
                ).fetchone()
                if owner is None:
                    raise ValueError(f"light {light_id} not found")
                # A light may serve several plants: one panel over a bench
                # is the ordinary case, and each plant under it has its own
                # PPFD recorded on the pair.
            self._database.execute("DELETE FROM plant_lights WHERE plant_id = ?", (plant_id,))
            self._database.executemany(
                "INSERT INTO plant_lights (light_id, plant_id) VALUES (?, ?)",
                [(light_id, plant_id) for light_id in light_ids],
            )
        return self.plant_lighting_of(plant_id)

    def light_assignments(self) -> Dict[str, Dict[str, Any]]:
        """For each light: the plants under it, and the hours it keeps.

        The schedule is the lamp's own. A light can serve several plants --
        one panel over a bench -- and a switch cannot keep two sets of
        hours, so there is only ever one answer to ask for.
        """
        with self._condition:
            rows = self._database.execute(
                """
                SELECT plant_lights.light_id, plants.plant_id, plants.archived,
                       plants.display_name
                FROM plant_lights
                JOIN plants ON plants.plant_id = plant_lights.plant_id
                ORDER BY plant_lights.light_id, plants.archived, plants.plant_id
                """
            ).fetchall()
            schedules = {
                row[0]: {"enabled": bool(row[1]), "on": row[2], "off": row[3]}
                for row in self._database.execute(
                    "SELECT light_id, enabled, on_time, off_time FROM light_schedules"
                )
            }
        assignments: Dict[str, Dict[str, Any]] = {}
        for light_id, plant_id, archived, display_name in rows:
            entry = assignments.setdefault(
                light_id,
                {"plants": [], "plant_id": plant_id, "archived": True,
                 "schedule": schedules.get(light_id)},
            )
            entry["plants"].append({"plant_id": plant_id, "archived": bool(archived),
                                    "display_name": display_name})
            if not archived:
                # The light is live while any plant it serves is, and the
                # headline plant is the first living one.
                if entry["archived"]:
                    entry["plant_id"] = plant_id
                entry["archived"] = False
        return assignments

    def light_schedule(self, light_id: str) -> Dict[str, Any]:
        """When a lamp is lit. Never saved means the suggested window."""
        with self._condition:
            row = self._database.execute(
                "SELECT enabled, on_time, off_time FROM light_schedules WHERE light_id = ?",
                (light_id,),
            ).fetchone()
        return {
            "schedule": (
                {"enabled": bool(row[0]), "on": row[1], "off": row[2]}
                if row is not None
                else dict(DEFAULT_SCHEDULE)
            ),
            "schedule_saved": row is not None,
        }

    def set_light_schedule(self, light_id: str, schedule: Dict[str, Any]) -> Dict[str, Any]:
        schedule = validate_schedule(schedule)
        with self._condition, self._database:
            if self._database.execute(
                "SELECT 1 FROM lights WHERE light_id = ?", (light_id,)
            ).fetchone() is None:
                raise ValueError("light not found")
            self._database.execute(
                """
                INSERT INTO light_schedules (light_id, enabled, on_time, off_time, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (light_id) DO UPDATE SET
                    enabled = excluded.enabled, on_time = excluded.on_time,
                    off_time = excluded.off_time, updated_at = excluded.updated_at
                """,
                (
                    light_id,
                    int(schedule["enabled"]),
                    schedule["on"],
                    schedule["off"],
                    _timestamp(datetime.now(timezone.utc)),
                ),
            )
        return self.light_schedule(light_id)

    def record_light_event(
        self, light_id: str, source: str, wanted: bool, outcome: str, detail: Optional[str]
    ) -> None:
        with self._condition, self._database:
            if self._database.execute(
                "SELECT 1 FROM lights WHERE light_id = ?", (light_id,)
            ).fetchone() is None:
                return
            self._database.execute(
                """
                INSERT INTO light_events (light_id, at, source, wanted, outcome, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    light_id,
                    _timestamp(datetime.now(timezone.utc)),
                    source,
                    int(wanted),
                    outcome,
                    detail,
                ),
            )
            self._database.execute(
                """
                DELETE FROM light_events
                WHERE light_id = ? AND event_id NOT IN (
                    SELECT event_id FROM light_events WHERE light_id = ?
                    ORDER BY event_id DESC LIMIT ?
                )
                """,
                (light_id, light_id, LIGHT_EVENT_LIMIT),
            )
            self._record_lighting_care_events(light_id, source, wanted, outcome)

    def _record_lighting_care_events(
        self, light_id: str, source: str, wanted: bool, outcome: str
    ) -> None:
        """Put a light's switching in the care log of the plant it lights.

        The light's own event list is trimmed to the most recent few hundred, so
        it cannot answer how long a plant has been lit over its life. The care
        log is never trimmed, which makes it both the history somebody reads and
        the record the lifetime hours are counted from.
        """
        row = self._database.execute(
            "SELECT lights.display_name, plant_lights.plant_id FROM lights "
            "JOIN plant_lights ON plant_lights.light_id = lights.light_id "
            "WHERE lights.light_id = ?",
            (light_id,),
        ).fetchone()
        if row is None:
            # A light nobody has given to a plant lights no plant.
            return
        light_name, plant_id = row
        detected_at = _timestamp(datetime.now(timezone.utc))
        changes = {
            "light_id": light_id,
            "light": light_name,
            "wanted": bool(wanted),
            "source": source,
            "outcome": outcome,
        }
        # The light is the plant's, so the entry is the plant's. It used to
        # be written once per sensor watching the plant, which counted one
        # switch twice for anybody with two.
        self._database.execute(
            """
            INSERT OR IGNORE INTO care_events
            (event_id, plant_id, kind, detected_at, title, summary, confidence, changes_json)
            VALUES (?, ?, 'lighting', ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                plant_id,
                detected_at,
                f"{light_name} {'on' if wanted else 'off'}",
                _lighting_summary(source, wanted, outcome),
                "high" if outcome == "confirmed" else "low",
                json.dumps(changes),
            ),
        )
        self._journey_apply_event(plant_id, "lighting", detected_at, json.dumps(changes))

    def light_events(self, light_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._condition:
            return [
                {
                    "at": row[0],
                    "source": row[1],
                    "wanted": bool(row[2]),
                    "outcome": row[3],
                    "detail": row[4],
                }
                for row in self._database.execute(
                    """
                    SELECT at, source, wanted, outcome, detail FROM light_events
                    WHERE light_id = ? ORDER BY event_id DESC LIMIT ?
                    """,
                    (light_id, limit),
                )
            ]

    def _plant_id_for(self, sensor_id: str) -> Optional[int]:
        """The plant this sensor watches, if anybody has said which.

        A sensor in the inbox watches nothing yet. It still produces readings
        and the events detected from them, which are adopted by the plant when
        somebody enrolls it.
        """
        row = self._database.execute(
            "SELECT plant_id FROM sensors WHERE sensor_id = ?", (sensor_id,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _plant_of_sensor(self, sensor_id: str) -> int:
        row = self._database.execute(
            """
            SELECT plant_id FROM sensors
            WHERE sensor_id = ? AND enrollment_status = 'enrolled' AND plant_id IS NOT NULL
            """,
            (sensor_id,),
        ).fetchone()
        if row is None:
            raise ValueError("only an enrolled plant can have lights")
        return int(row[0])

    def _validate_light_name(self, display_name: str) -> str:
        display_name = display_name.strip()
        self._validate_device_text(display_name, "display_name", required=True)
        return display_name

    def _refuse_light_name_clash(self, display_name: str, light_id: str) -> None:
        clash = self._database.execute(
            "SELECT 1 FROM lights WHERE display_name = ? AND light_id != ?",
            (display_name, light_id),
        ).fetchone()
        if clash is not None:
            raise ValueError("a light with that name already exists")

    @staticmethod
    def _light_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "light_id": row[0],
            "display_name": row[1],
            "driver": row[2],
            "config": json.loads(row[3]),
            "created_at": row[4],
            "plant": (
                {
                    "plant_id": row[5],
                    "display_name": row[6],
                    "archived": bool(row[7]),
                    "sensor_id": row[8],
                }
                if row[5] is not None
                else None
            ),
        }

    def record_station_report(
        self, sensor_id: str, firmware_version: Optional[str]
    ) -> None:
        """Note what the sensor said about itself, and when it said it."""
        checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_version = COALESCE(?, firmware_version),
                    station_checked_at = ?
                WHERE sensor_id = ?
                """,
                (firmware_version, checked_at, sensor_id),
            )
            # An update succeeds when the sensor comes back running the image,
            # not when it says it installed one. A sensor that reports success
            # and then does not return has not updated anything.
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_update_state = 'succeeded',
                    firmware_update_percent = 100,
                    firmware_update_error = NULL
                WHERE sensor_id = ?
                  AND firmware_update_version IS NOT NULL
                  AND firmware_version = firmware_update_version
                  AND firmware_update_state IN
                      ('pending', 'commanded', 'downloading', 'installing', 'rebooting')
                """,
                (sensor_id,),
            )

    def expire_stalled_firmware_update(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        """Give up on an update the sensor never came back from.

        An update that has been under way for longer than any of it takes has
        not succeeded, and leaving it saying "restarting" for ever is the worst
        of both: nothing to act on, and no way to try again. The commonest cause
        is the one this is here for — the new image did not start, and the
        sensor is running the old one again.
        """
        sensor = self.sensor(sensor_id)
        if sensor is None or sensor["firmware_update_state"] not in (
            "commanded",
            "downloading",
            "installing",
            "rebooting",
        ):
            return None
        started_at = sensor["firmware_update_started_at"]
        if started_at is None:
            return None
        started = self._observation_datetime(started_at, datetime.now(timezone.utc))
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age < FIRMWARE_UPDATE_TIMEOUT_SECONDS:
            return None
        running = sensor["firmware_version"] or "the firmware it had"
        return self.record_firmware_update_state(
            sensor_id,
            "failed",
            None,
            f"the sensor did not come back running it; it is running {running}",
        )

    def station_report_age_seconds(self, sensor_id: str) -> Optional[int]:
        """How long since the sensor last told the hub about itself."""
        with self._condition:
            row = self._database.execute(
                "SELECT station_checked_at FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        checked = self._observation_datetime(row[0], datetime.now(timezone.utc))
        return max(0, int((datetime.now(timezone.utc) - checked).total_seconds()))

    def release_is_pending(self, sensor_id: str) -> bool:
        """Has this sensor been forgotten without being told yet?"""
        with self._condition:
            row = self._database.execute(
                "SELECT release_pending FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
        return bool(row and row[0])

    def mark_released(self, sensor_id: str) -> None:
        """The sensor has been told, so stop trying."""
        with self._condition, self._database:
            self._database.execute(
                "UPDATE sensors SET release_pending = 0 WHERE sensor_id = ?", (sensor_id,)
            )

    def add_firmware_image(self, image: Dict[str, Any]) -> Dict[str, Any]:
        """Record an image the hub holds. The same image twice is one row."""
        with self._condition, self._database:
            self._database.execute(
                """
                INSERT INTO firmware_images (
                    digest, version, project, chip, idf_version, size_bytes, built_at, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (
                    image["digest"],
                    image["version"],
                    image["project"],
                    image.get("chip", "esp32c3"),
                    image["idf_version"],
                    int(image["size_bytes"]),
                    image.get("built_at"),
                    image["uploaded_at"],
                ),
            )
        stored = self.firmware_image(image["digest"])
        assert stored is not None
        return stored

    def firmware_images(self) -> List[Dict[str, Any]]:
        with self._condition:
            rows = self._database.execute(
                """
                SELECT digest, version, project, idf_version, size_bytes, built_at, uploaded_at, chip
                FROM firmware_images
                ORDER BY uploaded_at DESC, version DESC
                """
            ).fetchall()
        return [self._firmware_payload(row) for row in rows]

    def firmware_image(self, digest: str) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT digest, version, project, idf_version, size_bytes, built_at, uploaded_at, chip
                FROM firmware_images WHERE digest = ?
                """,
                (digest,),
            ).fetchone()
        return None if row is None else self._firmware_payload(row)

    def delete_firmware_image(self, digest: str) -> bool:
        with self._condition, self._database:
            cursor = self._database.execute(
                "DELETE FROM firmware_images WHERE digest = ?", (digest,)
            )
            # A sensor still waiting for this image would wait for ever, so the
            # request goes with it rather than becoming a promise nothing keeps.
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_update_state = 'failed',
                    firmware_update_error = 'the image was removed from the hub'
                WHERE firmware_update_digest = ?
                  AND firmware_update_state IN ('pending', 'commanded', 'downloading')
                """,
                (digest,),
            )
        return cursor.rowcount > 0

    def request_firmware_update(self, sensor_id: str, digest: str) -> Dict[str, Any]:
        """Ask a sensor to install a stored image, once it can be reached."""
        image = self.firmware_image(digest)
        if image is None:
            raise ValueError("unknown firmware image")
        sensor = self.sensor(sensor_id)
        if sensor is None:
            raise ValueError("unknown sensor")
        if sensor["enrollment_status"] != "enrolled":
            raise ValueError("only an enrolled sensor can be updated")
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_update_digest = ?,
                    firmware_update_version = ?,
                    firmware_update_state = 'pending',
                    firmware_update_id = firmware_update_id + 1,
                    firmware_update_percent = 0,
                    firmware_update_error = NULL,
                    firmware_update_started_at = ?
                WHERE sensor_id = ?
                """,
                (digest, image["version"], started_at, sensor_id),
            )
        updated = self.sensor(sensor_id)
        assert updated is not None
        return updated

    def pending_firmware_update(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        """The update this sensor has not been told about yet, if any."""
        with self._condition:
            row = self._database.execute(
                """
                SELECT s.firmware_update_digest, s.firmware_update_id,
                       s.firmware_update_version, f.size_bytes
                FROM sensors s
                JOIN firmware_images f ON f.digest = s.firmware_update_digest
                WHERE s.sensor_id = ? AND s.firmware_update_state = 'pending'
                """,
                (sensor_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "digest": row[0],
            "update_id": int(row[1]),
            "version": row[2],
            "size_bytes": int(row[3]),
        }

    def record_firmware_update_state(
        self,
        sensor_id: str,
        state: str,
        percent: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Note where an update has got to, as the sensor reports it."""
        if state not in FIRMWARE_UPDATE_STATES:
            raise ValueError(f"unknown firmware update state: {state}")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_update_state = ?,
                    firmware_update_percent = COALESCE(?, firmware_update_percent),
                    firmware_update_error = ?
                WHERE sensor_id = ?
                """,
                (state, percent, error, sensor_id),
            )
        sensor = self.sensor(sensor_id)
        assert sensor is not None
        return sensor

    def cancel_firmware_update(self, sensor_id: str) -> Dict[str, Any]:
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET firmware_update_state = 'idle',
                    firmware_update_percent = 0,
                    firmware_update_error = NULL
                WHERE sensor_id = ?
                """,
                (sensor_id,),
            )
        sensor = self.sensor(sensor_id)
        assert sensor is not None
        return sensor

    @staticmethod
    def _firmware_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "digest": row[0],
            "version": row[1],
            "project": row[2],
            "idf_version": row[3],
            "size_bytes": row[4],
            "built_at": row[5],
            "uploaded_at": row[6],
            # Appended to the select, so it is last whatever comes before.
            "chip": row[7] if len(row) > 7 else "esp32c3",
        }

    def hub_wifi_settings(self) -> Dict[str, Any]:
        """Return the household network name, without any password."""
        with self._condition:
            row = self._database.execute(
                "SELECT wifi_ssid FROM hub_settings WHERE singleton_id = 1"
            ).fetchone()
        assert row is not None
        return {"wifi_ssid": row[0]}

    def set_hub_wifi_network(self, wifi_ssid: Optional[str]) -> Dict[str, Any]:
        """Store the household network name. An empty name forgets the network.

        The password is deliberately absent: it belongs in the operating system
        keychain, never in this database, which is backed up and exported.
        """
        if wifi_ssid is not None:
            wifi_ssid = wifi_ssid.strip()
            if not wifi_ssid:
                wifi_ssid = None
            else:
                self._validate_device_text(wifi_ssid, "wifi_ssid", required=True)
        with self._condition, self._database:
            self._database.execute(
                "UPDATE hub_settings SET wifi_ssid = ? WHERE singleton_id = 1",
                (wifi_ssid,),
            )
            if wifi_ssid is None:
                self._database.execute(
                    """
                    UPDATE sensors
                    SET wifi_enabled = 0, wifi_state = 'off',
                        wifi_failure = NULL, wifi_address = NULL
                    """
                )
        return self.hub_wifi_settings()

    def set_sensor_wifi_enabled(self, sensor_id: str, enabled: bool) -> Dict[str, Any]:
        """Switch a sensor's web console on or off.

        Switching it on asks the sensor to join the household network; the result
        is not known until the sensor reports back, so the state becomes pending
        rather than joined.
        """
        if not sensor_id:
            raise ValueError("sensor_id is required")
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT enrollment_status FROM sensors WHERE sensor_id = ?",
                (sensor_id,),
            ).fetchone()
            if row is None:
                raise ValueError("sensor_id has not been observed")
            if row[0] != "enrolled":
                raise ValueError("only an enrolled sensor can use the household network")
            if enabled:
                network = self._database.execute(
                    "SELECT wifi_ssid FROM hub_settings WHERE singleton_id = 1"
                ).fetchone()
                if network is None or not network[0]:
                    raise ValueError("no household network is configured")
            self._database.execute(
                """
                UPDATE sensors
                SET wifi_enabled = ?,
                    wifi_state = ?,
                    wifi_failure = NULL,
                    wifi_address = NULL,
                    device_config_revision = device_config_revision + 1
                WHERE sensor_id = ?
                """,
                (1 if enabled else 0, "pending" if enabled else "off", sensor_id),
            )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def remember_zigbee_address(self, sensor_id: str, address: str) -> Dict[str, Any]:
        """Where a Zigbee device says its own web page answers.

        Separate from record_sensor_wifi_result, which is the record of
        a join this hub asked for and insists on a state to go with it.
        This is a device volunteering where it can be found, which is
        not a join and has no outcome to report.
        """
        with self._condition, self._database:
            self._database.execute(
                "UPDATE sensors SET wifi_address = ? WHERE sensor_id = ?",
                (address, sensor_id),
            )
        sensor = self.sensor(sensor_id)
        if sensor is None:
            raise LookupError("sensor not found")
        return sensor

    def record_sensor_wifi_result(
        self,
        sensor_id: str,
        wifi_state: str,
        wifi_failure: Optional[str] = None,
        wifi_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record what a sensor reported back after trying to join."""
        if wifi_state not in WIFI_STATES:
            raise ValueError("invalid wifi_state")
        if wifi_failure is not None and wifi_failure not in WIFI_FAILURES:
            raise ValueError("invalid wifi_failure")
        if wifi_state == "failed" and wifi_failure is None:
            raise ValueError("a failed join must say why")
        if wifi_state != "failed" and wifi_failure is not None:
            raise ValueError("only a failed join carries a reason")
        if wifi_state != "joined" and wifi_address is not None:
            raise ValueError("only a joined sensor has an address")
        with self._condition, self._database:
            updated = self._database.execute(
                """
                UPDATE sensors
                SET wifi_state = ?, wifi_failure = ?, wifi_address = ?
                WHERE sensor_id = ?
                """,
                (wifi_state, wifi_failure, wifi_address, sensor_id),
            ).rowcount
        if updated == 0:
            raise ValueError("sensor_id has not been observed")
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def set_sensor_onboarding_state(self, sensor_id: str, onboarding_state: str) -> Dict[str, Any]:
        """Move a sensor between onboarding and onboarded.

        Returning to onboarding is a reset. The bond is gone, so the sensor no
        longer belongs to this hub: it leaves the enrolled fleet and reappears as
        an unclaimed device, exactly as a factory-fresh sensor does. Its readings
        are kept, so adding it again continues the same history rather than
        starting a new one; deleting a sensor outright is a separate action.
        """
        if onboarding_state not in ONBOARDING_STATES:
            raise ValueError("invalid onboarding_state")
        with self._condition, self._database:
            updated = self._database.execute(
                """
                UPDATE sensors
                SET onboarding_state = ?,
                    enrollment_status = CASE ?
                        WHEN 'onboarding' THEN 'unclaimed' ELSE enrollment_status END,
                    release_pending = CASE ? WHEN 'onboarding' THEN 1 ELSE 0 END,
                    wifi_enabled = CASE ? WHEN 'onboarding' THEN 0 ELSE wifi_enabled END,
                    wifi_state = CASE ? WHEN 'onboarding' THEN 'off' ELSE wifi_state END,
                    wifi_failure = CASE ? WHEN 'onboarding' THEN NULL ELSE wifi_failure END,
                    wifi_address = CASE ? WHEN 'onboarding' THEN NULL ELSE wifi_address END
                WHERE sensor_id = ?
                """,
                (onboarding_state,) * 7 + (sensor_id,),
            ).rowcount
        if updated == 0:
            raise ValueError("sensor_id has not been observed")
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def rename_sensor(self, sensor_id: str, display_name: str) -> Dict[str, Any]:
        display_name = display_name.strip()
        self._validate_device_text(display_name, "display_name", required=True)
        if not sensor_id:
            raise ValueError("sensor_id is required")
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT plant_id, display_name FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
            if row is None:
                raise ValueError("sensor_id has not been observed")
            self._database.execute(
                """
                UPDATE sensors
                SET display_name = ?,
                    device_config_revision = CASE
                        WHEN display_name IS NOT ? THEN device_config_revision + 1
                        ELSE device_config_revision
                    END,
                    device_config_error = CASE WHEN display_name IS NOT ? THEN NULL ELSE device_config_error END
                WHERE sensor_id = ?
                """,
                (display_name, display_name, display_name, sensor_id),
            )
            if row[0] is not None:
                self._database.execute(
                    "UPDATE plants SET display_name = ? WHERE plant_id = ?",
                    (display_name, row[0]),
                )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    # ---- what Zigbee devices said, kept rather than judged and dropped ----

    def record_zigbee_report(
        self,
        eui: str,
        values: Dict[str, Any],
        kind: Optional[str] = None,
        lqi: Optional[int] = None,
        rssi: Optional[int] = None,
        at: Optional[str] = None,
    ) -> str:
        """Keep a report exactly as the device sent it.

        No judgement here: whether the numbers mean anything is somebody
        else's question, asked later and possibly more than once. The point
        of writing it down is to be able to ask again.
        """
        when = at or _timestamp(datetime.now(timezone.utc))
        with self._condition, self._database:
            self._database.execute(
                """
                INSERT INTO zigbee_reports (eui, at, kind, values_json, lqi, rssi)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (eui, when, kind, json.dumps(values, sort_keys=True, default=str),
                 lqi, rssi),
            )
        return when

    def zigbee_reports(
        self,
        eui: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Reports newest first, for one device or for every one of them."""
        where: List[str] = []
        parameters: List[Any] = []
        if eui is not None:
            where.append("eui = ?")
            parameters.append(eui)
        if since is not None:
            where.append("at >= ?")
            parameters.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        wanted = max(1, int(limit))
        with self._condition:
            if since is None:
                rows = self._database.execute(
                    "SELECT report_id, eui, at, kind, values_json, lqi, rssi "
                    f"FROM zigbee_reports{clause} "
                    "ORDER BY at DESC, report_id DESC LIMIT ?",
                    tuple(parameters + [wanted]),
                ).fetchall()
            else:
                # Thinned across the window rather than cut off at the
                # newest. A scale sends about seven thousand reports a
                # day; asked for a month and capped at two thousand
                # rows, the answer used to be the newest two thousand
                # -- two days -- so the 7 day, 30 day and 1 year
                # buttons all drew the same two days.
                #
                # One report from each slice of time, the slice chosen
                # so the whole window fits in the rows allowed. Under
                # that many reports the slices are smaller than the
                # gaps between them and every row comes back.
                span = self._database.execute(
                    f"SELECT MIN(at), MAX(at), COUNT(*) FROM zigbee_reports{clause}",
                    tuple(parameters),
                ).fetchone()
                seconds = _seconds_between(span[0], span[1]) if span else 0
                if not span or span[2] <= wanted:
                    # It all fits. Bucketing anyway would throw away
                    # real reports -- two in the same slice become one
                    # -- and a day that fitted easily came back half
                    # the size.
                    rows = self._database.execute(
                        "SELECT report_id, eui, at, kind, values_json, lqi, rssi "
                        f"FROM zigbee_reports{clause} "
                        "ORDER BY at DESC, report_id DESC LIMIT ?",
                        tuple(parameters + [wanted]),
                    ).fetchall()
                    return self._reports_from(rows)
                bucket = max(1, int(seconds // wanted)) if seconds else 1
                rows = self._database.execute(
                    "SELECT report_id, eui, at, kind, values_json, lqi, rssi FROM ("
                    "  SELECT report_id, eui, at, kind, values_json, lqi, rssi,"
                    "         ROW_NUMBER() OVER ("
                    "           PARTITION BY CAST(strftime('%s', at) / ? AS INTEGER)"
                    "           ORDER BY at DESC, report_id DESC) AS pick"
                    f"  FROM zigbee_reports{clause}"
                    ") WHERE pick = 1 ORDER BY at DESC, report_id DESC LIMIT ?",
                    tuple([bucket] + parameters + [wanted]),
                ).fetchall()
        return self._reports_from(rows)

    def _reports_from(self, rows: Any) -> List[Dict[str, Any]]:
        """One shape for a report, however the rows were chosen."""
        reports: List[Dict[str, Any]] = []
        for row in rows:
            try:
                values = json.loads(row[4])
            except (TypeError, ValueError):
                # A row nobody can read is still a row that a device sent at
                # a time, and the time is worth having.
                values = {}
            reports.append({
                "report_id": row[0], "eui": row[1], "at": row[2], "kind": row[3],
                "values": values, "lqi": row[5], "rssi": row[6],
            })
        return reports


    def latest_zigbee_report(
        self, eui: str, within_seconds: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """The newest report from this device, optionally only a fresh one.

        `within_seconds` is what makes this usable on a schedule: at six in
        the morning the question is not "has it just spoken" but "is what it
        last said still true", and half an hour old usually is.
        """
        since = None
        if within_seconds is not None:
            since = _timestamp(
                datetime.now(timezone.utc) - timedelta(seconds=max(0, int(within_seconds)))
            )
        found = self.zigbee_reports(eui=eui, since=since, limit=1)
        return found[0] if found else None

    def prune_zigbee_reports(self, keep_days: Optional[int] = -1) -> int:
        """Drop reports older than the retention window; returns how many.

        Nothing is dropped unless a window has been chosen: a default
        that deletes is a default that loses data somebody wanted, and
        this is the only record of what a device actually said.
        """
        if keep_days == -1:
            keep_days = self.hub_settings()["zigbee_report_keep_days"]
        if keep_days is None:
            return 0
        cutoff = _timestamp(
            datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))
        )
        with self._condition, self._database:
            cursor = self._database.execute(
                "DELETE FROM zigbee_reports WHERE at < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)

    def delete_sensor(self, sensor_id: str) -> None:
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT plant_id FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
            if row is None:
                raise ValueError("sensor_id has not been observed")
            plant_id = row[0]
            self._database.execute(
                "UPDATE sensors SET replaced_by_sensor_id = NULL WHERE replaced_by_sensor_id = ?",
                (sensor_id,),
            )
            self._database.execute("DELETE FROM sensor_readings WHERE sensor_id = ?", (sensor_id,))
            self._database.execute(
                "DELETE FROM report_packet2 WHERE sensor_id = ?", (sensor_id,)
            )
            self._database.execute(
                "DELETE FROM care_events WHERE sensor_id = ? OR (plant_id IS NOT NULL "
                "AND plant_id = (SELECT plant_id FROM sensors WHERE sensor_id = ?))",
                (sensor_id, sensor_id),
            )
            self._database.execute("DELETE FROM advertisements WHERE sensor_id = ?", (sensor_id,))
            self._database.execute("DELETE FROM sensors WHERE sensor_id = ?", (sensor_id,))
            if plant_id is not None:
                self._database.execute(
                    """
                    DELETE FROM plants
                    WHERE plant_id = ?
                      AND NOT EXISTS (SELECT 1 FROM sensors WHERE plant_id = ?)
                    """,
                    (plant_id, plant_id),
                )
            self._history = deque(
                (
                    (reading, received_at)
                    for reading, received_at in self._history
                    if reading.sensor_id != sensor_id
                ),
                maxlen=self._history_size,
            )
            self._sensor_refill_below.pop(sensor_id, None)
            self._event_detector.forget_sensor(sensor_id)

    def manage_sensor(
        self,
        sensor_id: str,
        display_name: Optional[str],
        room: Optional[str],
        profile_id: Optional[str],
        moisture_low_percent: Optional[float],
        conductivity_high_us_cm: Optional[int],
        expected_interval_seconds: int,
        room_id: Optional[int] = None,
        attach_to_plant_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Adopt a sensor, or change what the hub asks of the device.

        What the plant is called, which room it is in and what kind of plant
        it is belong to the plant and are edited on the plant's own page.
        They are still taken here, because adopting a sensor is where a plant
        is born; left out, they are kept as they are rather than cleared.

        Unless it is not. A plant tracked by hand for a season -- weighings,
        photographs, a care log, the day it came home -- is the same plant
        when a probe finally goes into it, so attach_to_plant_id puts the
        sensor on that record rather than starting a second one beside it.
        """
        # What to fall back on for the plant's own fields. Joining an
        # existing plant, they are that plant's: a sensor arriving at a
        # plant called Spider does not get to leave it nameless.
        held = self._database.execute(
            """
            SELECT plants.display_name, rooms.name, plants.profile_id, plants.room_id
            FROM plants LEFT JOIN rooms ON rooms.room_id = plants.room_id
            WHERE plants.plant_id = ?
            """,
            (attach_to_plant_id,),
        ).fetchone() if attach_to_plant_id is not None else self._database.execute(
            """
            SELECT sensors.display_name, sensors.room, sensors.profile_id, sensors.room_id
            FROM sensors WHERE sensor_id = ?
            """,
            (sensor_id,),
        ).fetchone()
        if display_name is None:
            display_name = (held[0] if held else "") or ""
        # A room named here is a room, not a label.
        #
        # The sensor used to keep the name as text beside its room_id, and
        # the two drifted apart whenever a plant moved. The name is read
        # from the room now, so naming one has to find or make that room,
        # or the sensor ends up in no room at all.
        if room_id is None and room:
            room_id = self._room_id_named(room)
        if room is None and room_id is None:
            room, room_id = (held[1] if held else None) or "", held[3] if held else None
        if profile_id is None:
            profile_id = (held[2] if held else None) or load_plant_profiles()["default_profile"]
        display_name = display_name.strip()
        # Rooms are chosen, not typed. The name is still stored on the sensor so
        # every existing reader keeps working, but the room it points at is what
        # the household actually manages.
        if room_id is not None:
            named = self._database.execute(
                "SELECT name FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if named is None:
                raise ValueError("room_id is unknown")
            room = named[0]
        room = room.strip()
        catalog = load_plant_profiles()
        self._validate_device_text(display_name, "display_name", required=True)
        self._validate_device_text(room, "room", required=False)
        if not sensor_id:
            raise ValueError("sensor_id is required")
        if profile_id not in catalog["profiles"]:
            raise ValueError("profile_id is invalid")
        if moisture_low_percent is not None and not 0 <= moisture_low_percent <= 100:
            raise ValueError("moisture_low_percent must be between 0 and 100")
        if conductivity_high_us_cm is not None and conductivity_high_us_cm < 0:
            raise ValueError("conductivity_high_us_cm cannot be negative")
        if not MIN_REPORTING_INTERVAL_SECONDS <= expected_interval_seconds <= MAX_REPORTING_INTERVAL_SECONDS:
            raise ValueError(
                "expected_interval_seconds must be between "
                f"{MIN_REPORTING_INTERVAL_SECONDS} and {MAX_REPORTING_INTERVAL_SECONDS}"
            )

        with self._condition, self._database:
            current = self._database.execute(
                """
                SELECT plant_id, display_name, room_id, expected_interval_seconds,
                       device_config_revision, enrollment_status
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            if current is None:
                raise ValueError("sensor_id has not been observed")
            plant_id = current[0]
            if attach_to_plant_id is not None:
                if plant_id is not None and plant_id != attach_to_plant_id:
                    raise ValueError("this sensor already belongs to another plant")
                attached = self._database.execute(
                    "SELECT display_name FROM plants WHERE plant_id = ?",
                    (attach_to_plant_id,),
                ).fetchone()
                if attached is None:
                    raise ValueError("unknown plant")
                plant_id = attach_to_plant_id
            # Adopting a sensor always re-delivers its configuration, even when
            # every field matches what the hub last sent. A sensor being adopted
            # has just been reset or has come from another hub, so whatever it
            # holds cannot be trusted; and the delivery is what opens the
            # connection that pairing needs.
            adopting = current[5] != "enrolled"
            device_config_changed = (
                adopting
                or current[1] != display_name
                # Compared by room, not by a copy of its name: the name is
                # read from the room now, and comparing against a column
                # nothing writes made every save look like a change.
                or current[2] != room_id
                or current[3] != expected_interval_seconds
                or current[4] == 0
            )
            device_config_revision = (
                current[4] + 1 if device_config_changed else current[4]
            )
            if plant_id is None:
                cursor = self._database.execute(
                    "INSERT INTO plants (display_name, profile_id, room) VALUES (?, ?, ?)",
                    (display_name, profile_id, room or None),
                )
                plant_id = int(cursor.lastrowid)
            else:
                self._database.execute(
                    """
                    UPDATE plants SET display_name = ?, profile_id = ?, room = ?, archived = 0
                    WHERE plant_id = ?
                    """,
                    (display_name, profile_id, room or None, plant_id),
                )
            # Whatever this sensor noticed before anybody claimed it belongs
            # to the plant now: the plant did not begin when it was named.
            self._database.execute(
                "UPDATE care_events SET plant_id = ? WHERE sensor_id = ? AND plant_id IS NULL",
                (plant_id, sensor_id),
            )
            cursor = self._database.execute(
                """
                UPDATE sensors
                SET enrollment_status = 'enrolled', archived = 0,
                    onboarding_state = 'onboarded',
                    display_name = ?, room_id = ?, plant_id = ?, profile_id = ?,
                    moisture_low_percent = ?, conductivity_high_us_cm = ?,
                    expected_interval_seconds = ?, replaced_by_sensor_id = NULL,
                    device_config_revision = ?,
                    device_config_error = CASE WHEN ? THEN NULL ELSE device_config_error END
                WHERE sensor_id = ?
                """,
                (
                    display_name,
                    room_id,
                    plant_id,
                    profile_id,
                    moisture_low_percent,
                    conductivity_high_us_cm,
                    expected_interval_seconds,
                    device_config_revision,
                    device_config_changed,
                    sensor_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("sensor_id has not been observed")
            self._sensor_refill_below[sensor_id] = float(
                catalog["profiles"][profile_id]["watering"]["refill_below"]
            )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def pending_device_configuration(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        if self._is_scale(sensor_id):
            # A scale is reached over HTTP and has no device configuration to
            # deliver. Left to the ordinary path it sat for ever at "pending",
            # waiting for a Bluetooth conversation that will never happen.
            return None
        with self._condition:
            row = self._database.execute(
                """
                SELECT device_config_revision, expected_interval_seconds,
                       display_name,
                       COALESCE((SELECT rooms.name FROM rooms
                                 WHERE rooms.room_id = sensors.room_id), ''),
                       wifi_enabled
                FROM sensors
                WHERE sensor_id = ? AND enrollment_status = 'enrolled'
                  AND device_config_revision > device_config_applied_revision
                """,
                (sensor_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "revision": int(row[0]),
            "console_enabled": bool(row[4]),
            "reporting_interval_seconds": int(row[1]),
            "plant_name": row[2],
            "room": row[3],
        }

    def mark_device_configuration_applied(
        self, sensor_id: str, revision: int, reporting_interval_seconds: int
    ) -> None:
        attempted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET device_config_applied_revision = ?, device_config_attempted_at = ?,
                    device_config_error = CASE
                        WHEN device_config_revision = ? THEN NULL ELSE device_config_error
                    END,
                    sensor_reporting_interval_seconds = ?
                WHERE sensor_id = ? AND device_config_revision >= ?
                  AND device_config_applied_revision < ?
                """,
                (
                    revision,
                    attempted_at,
                    revision,
                    reporting_interval_seconds,
                    sensor_id,
                    revision,
                    revision,
                ),
            )

    def mark_device_configuration_error(self, sensor_id: str, revision: int, error: str) -> None:
        attempted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET device_config_attempted_at = ?, device_config_error = ?
                WHERE sensor_id = ? AND device_config_revision = ?
                """,
                (attempted_at, error[:240], sensor_id, revision),
            )

    def set_sensor_archived(self, sensor_id: str, archived: bool) -> Dict[str, Any]:
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT plant_id FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
            if row is None:
                raise ValueError("sensor_id has not been observed")
            if not archived:
                restore_state = self._database.execute(
                    "SELECT display_name, replaced_by_sensor_id FROM sensors WHERE sensor_id = ?",
                    (sensor_id,),
                ).fetchone()
                if restore_state[0] is None:
                    raise ValueError("an unclaimed sensor must be enrolled before it can be restored")
                if restore_state[1] is not None:
                    raise ValueError("a replaced sensor cannot be restored")
            self._database.execute(
                "UPDATE sensors SET enrollment_status = ?, archived = ? WHERE sensor_id = ?",
                ("archived" if archived else "enrolled", int(archived), sensor_id),
            )
            if row[0] is not None:
                self._database.execute(
                    "UPDATE plants SET archived = ? WHERE plant_id = ?", (int(archived), row[0])
                )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def replace_sensor(
        self, sensor_id: str, replacement_sensor_id: str, merge_history: bool = False
    ) -> Dict[str, Any]:
        if not sensor_id or not replacement_sensor_id or sensor_id == replacement_sensor_id:
            raise ValueError("two different sensor IDs are required")
        with self._condition:
            try:
                with self._database:
                    old = self._database.execute(
                        """
                        SELECT display_name, room, plant_id, profile_id,
                               moisture_low_percent, conductivity_high_us_cm,
                               expected_interval_seconds
                        FROM sensors WHERE sensor_id = ? AND enrollment_status = 'enrolled'
                        """,
                        (sensor_id,),
                    ).fetchone()
                    replacement = self._database.execute(
                        "SELECT enrollment_status FROM sensors WHERE sensor_id = ?",
                        (replacement_sensor_id,),
                    ).fetchone()
                    if old is None or replacement is None:
                        raise ValueError("an enrolled source and observed replacement are required")
                    if replacement[0] == "enrolled":
                        raise ValueError("replacement sensor is already enrolled")
                    self._database.execute(
                        """
                        UPDATE sensors
                        SET enrollment_status = 'enrolled', archived = 0,
                            onboarding_state = 'onboarded',
                            display_name = ?, room = ?, plant_id = ?, profile_id = ?,
                            moisture_low_percent = ?, conductivity_high_us_cm = ?,
                            expected_interval_seconds = ?, replaced_by_sensor_id = NULL,
                            device_config_revision = device_config_revision + 1,
                            device_config_applied_revision = 0,
                            device_config_error = NULL,
                            sensor_reporting_interval_seconds = NULL
                        WHERE sensor_id = ?
                        """,
                        (*old, replacement_sensor_id),
                    )
                    self._database.execute(
                        """
                        UPDATE sensors SET enrollment_status = 'archived', archived = 1,
                                           plant_id = NULL, replaced_by_sensor_id = ?
                        WHERE sensor_id = ?
                        """,
                        (replacement_sensor_id, sensor_id),
                    )
                    if merge_history:
                        # Report IDs belong to the sensor that numbered them.
                        # Carried across, they would collide with the numbers
                        # the replacement is yet to send and make its genuine
                        # reports look like repeats, so merged readings leave
                        # theirs behind.
                        self._database.execute(
                            """
                            UPDATE sensor_readings SET sensor_id = ?, report_id = NULL
                            WHERE sensor_id = ?
                            """,
                            (replacement_sensor_id, sensor_id),
                        )
                        # The events move with the history, and so does the
                        # plant they belong to: a replacement is the same
                        # plant watched by a different instrument.
                        self._database.execute(
                            "UPDATE care_events SET sensor_id = ?, plant_id = "
                            "(SELECT plant_id FROM sensors WHERE sensor_id = ?) "
                            "WHERE sensor_id = ?",
                            (replacement_sensor_id, replacement_sensor_id, sensor_id),
                        )
                        self._database.execute(
                            "UPDATE advertisements SET sensor_id = ? WHERE sensor_id = ?",
                            (replacement_sensor_id, sensor_id),
                        )
            except sqlite3.IntegrityError as error:
                raise ValueError("history cannot be merged because readings overlap") from error
            if merge_history:
                # History that moved between sensors takes its figures with it,
                # and the only honest way to move them is to count them again.
                for moved in (sensor_id, replacement_sensor_id):
                    plant_id = self._plant_id_for(moved)
                    if plant_id is not None:
                        self._rebuild_journey(plant_id)
            if old[3] is not None:
                self._sensor_refill_below[replacement_sensor_id] = self._sensor_refill_below.get(
                    sensor_id, self._default_refill_below
                )
            if merge_history:
                self._history = deque(
                    [
                        (
                            SensorReading(
                                **{
                                    **asdict(reading),
                                    "sensor_id": replacement_sensor_id,
                                    "report_id": None,
                                }
                            ),
                            received_at,
                        )
                        if reading.sensor_id == sensor_id
                        else (reading, received_at)
                        for reading, received_at in self._history
                    ],
                    maxlen=self._history.maxlen,
                )
        result = self.sensor(replacement_sensor_id)
        assert result is not None
        return result

    def attach_sensor(self, sensor_id: str, plant_id: Optional[int]) -> Dict[str, Any]:
        """Say which plant a sensor is watching, or that it is watching none.

        Adopting a device and deciding what it is pointed at are different
        decisions, made at different times: a probe moves from one pot to
        another, or comes out while a plant is repotted, without stopping
        being the hub's sensor.

        What each side keeps when they part is what it was always about.
        The readings are the sensor's. The care log, the weighings, the
        photographs and the journey are the plant's, including the events
        this sensor detected while it was there -- that watering did happen
        to that plant, and moving the probe does not unhappen it. Only what
        the sensor notices from now on belongs to wherever it has gone.
        """
        with self._condition, self._database:
            sensor = self._database.execute(
                "SELECT enrollment_status, plant_id FROM sensors WHERE sensor_id = ?",
                (sensor_id,),
            ).fetchone()
            if sensor is None:
                raise ValueError("sensor_id has not been observed")
            if sensor[0] != "enrolled":
                raise ValueError("a sensor must be enrolled before it watches a plant")
            if plant_id is not None:
                if self.plant(plant_id) is None:
                    raise ValueError("unknown plant")
                # Anything it saw before it was pointed anywhere belongs to
                # the plant it has been pointed at.
                self._database.execute(
                    "UPDATE care_events SET plant_id = ? WHERE sensor_id = ? AND plant_id IS NULL",
                    (plant_id, sensor_id),
                )
            if plant_id is None:
                self._database.execute(
                    "UPDATE sensors SET plant_id = NULL WHERE sensor_id = ?", (sensor_id,)
                )
            else:
                # A sensor is in the room its plant is in. Where else would
                # it be? The room was asked for separately back when adopting
                # a sensor created a plant, which left a scale adopted on its
                # own with no room_id and a text label matching no room. The
                # room matters: it is how a probe's air reading reaches every
                # plant in that room.
                self._database.execute(
                    """
                    UPDATE sensors
                    SET plant_id = ?,
                        room_id = (SELECT room_id FROM plants WHERE plant_id = ?)
                    WHERE sensor_id = ?
                    """,
                    (plant_id, plant_id, sensor_id),
                )
        if plant_id is not None:
            # The plant's kept figures were counted without this sensor's
            # events in them, or with the last plant's; they are counted
            # again rather than patched.
            self.rebuild_journey_of(plant_id)
        if sensor[1] is not None and sensor[1] != plant_id:
            self.rebuild_journey_of(sensor[1])
        found = self.sensor(sensor_id)
        assert found is not None
        return found

    def _room_id_named(self, name: str) -> Optional[int]:
        """The room with this name, made if the household has not got one."""
        wanted = name.strip()
        if not wanted:
            return None
        with self._condition:
            row = self._database.execute(
                "SELECT room_id FROM rooms WHERE lower(name) = lower(?)", (wanted,)
            ).fetchone()
        if row is not None:
            return int(row[0])
        return int(self.create_room(wanted)["room_id"])

    def set_sensor_profile(self, sensor_id: str, profile_id: str) -> Dict[str, Any]:
        catalog = load_plant_profiles()
        profile = catalog["profiles"].get(profile_id)
        if not sensor_id or profile is None:
            raise ValueError("sensor_id and a valid profile_id are required")
        refill_below = float(profile["watering"]["refill_below"])
        with self._condition:
            self._database.execute(
                "UPDATE sensors SET profile_id = ? WHERE sensor_id = ?",
                (profile_id, sensor_id),
            )
            self._sensor_refill_below[sensor_id] = refill_below
            self._database.commit()
        return {
            "sensor_id": sensor_id,
            "profile_id": profile_id,
            "refill_below": refill_below,
        }

    def latest(
        self, sensor_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """Return the newest complete reading, for one sensor or any.

        A contract-v3 report is complete once its packet 2 is
        stored too. The newest row can be a report whose second packet is
        seconds away, and showing it blanked pH, N/P/K and battery until then:
        with a report every few seconds the page flashed between the two.
        Readings from before contract v3 have no report ID, and readings
        stored directly with add() never had packets; both are complete.

        Newest means the highest report ID, not the row stored last: a drain
        backfills reports older than the one already heard over the air.
        Only rows without a report ID fall back to the order they were stored.
        """
        with self._condition:
            row = self._database.execute(
                f"""
                SELECT {READING_COLUMNS}
                FROM sensor_readings AS reading
                WHERE (? IS NULL OR sensor_id = ?)
                  AND (report_id IS NULL OR advertisement_id IS NULL OR EXISTS (
                      SELECT 1 FROM report_packet2 AS packet2
                      WHERE packet2.sensor_id = reading.sensor_id
                        AND packet2.report_id = reading.report_id
                  ))
                ORDER BY report_id IS NULL, report_id DESC, reading_id DESC
                LIMIT 1
                """,
                (sensor_id, sensor_id),
            ).fetchone()
            if row is None:
                return None
            payload = self._serialize_row(row)
            payload["chemistry"] = self._chemistry(
                payload, now or datetime.now(timezone.utc)
            )
            return payload

    def _chemistry(self, latest: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """Estimate pore-water conductivity for the latest reading, or the last one that had it.

        The latest reading is the basis when it yields an estimate. When it
        does not, usually because the soil is too dry for the probe, the basis
        is the most recent reading within CHEMISTRY_LOOKBACK that does,
        preferring one taken shortly after a detected watering.
        """
        reading = latest["reading"]
        sensor_id = reading["sensor_id"]
        latest_at = self._observation_datetime(
            reading["observed_at"], self._observation_datetime(latest["received_at"], now)
        )
        estimate = estimate_pore_water_ec(
            reading["conductivity_us_cm"],
            reading["moisture_percent"],
            reading["soil_temperature_c"],
        )
        # The probe's EC reads zero once the soil dries, a little either side of
        # MIN_MOISTURE_PERCENT, so zero is dryness too.
        too_dry = estimate is None and (
            is_too_dry(reading["moisture_percent"])
            or (
                reading["moisture_percent"] is not None
                and reading["conductivity_us_cm"] == 0
            )
        )
        if estimate is not None:
            status = "current"
            basis: Optional[ChemistryBasis] = (reading, latest["received_at"], estimate)
            basis_at = latest_at
            post_watering = self._follows_watering(sensor_id, latest_at)
        else:
            status = "last_valid"
            basis, post_watering = self._last_valid_chemistry_basis(sensor_id, now)
            if basis is None:
                return {
                    "status": "none",
                    "too_dry": too_dry,
                    "pore_water_ec_us_cm": None,
                    "ec25_us_cm": None,
                    "nutrient_level": None,
                    "basis": None,
                    "age_seconds": None,
                }
            basis_at = self._observation_datetime(
                basis[0]["observed_at"], self._observation_datetime(basis[1], now)
            )
        basis_reading, basis_received_at, estimate = basis
        return {
            "status": status,
            "too_dry": too_dry,
            "pore_water_ec_us_cm": round(estimate.pore_water_ec_us_cm),
            "ec25_us_cm": round(estimate.ec25_us_cm, 1),
            "nutrient_level": nutrient_level(
                estimate.pore_water_ec_us_cm, self._conductivity_ideal(sensor_id)
            ),
            "basis": {
                "report_id": basis_reading["report_id"],
                "observed_at": basis_reading["observed_at"],
                "received_at": basis_received_at,
                "conductivity_us_cm": basis_reading["conductivity_us_cm"],
                "moisture_percent": basis_reading["moisture_percent"],
                "soil_temperature_c": basis_reading["soil_temperature_c"],
                "post_watering": post_watering,
            },
            "age_seconds": max(0, int((now - basis_at).total_seconds())),
        }

    def _conductivity_ideal(self, sensor_id: str) -> Optional[List[float]]:
        row = self._database.execute(
            "SELECT profile_id FROM sensors WHERE sensor_id = ?", (sensor_id,)
        ).fetchone()
        profiles = self._catalog["profiles"]
        profile = profiles.get(row[0]) if row is not None and row[0] else None
        if profile is None:
            profile = profiles[self._catalog["default_profile"]]
        return profile.get("root_zone", {}).get("conductivity_us_cm", {}).get("ideal")

    def _follows_watering(self, sensor_id: str, observed: datetime) -> bool:
        """Return whether a reading taken at observed falls in a post-watering window."""
        return (
            self._database.execute(
                """
                SELECT 1
                FROM care_events
                WHERE plant_id IS (SELECT plant_id FROM sensors WHERE sensor_id = ?)
                  AND kind = 'watering'
                  AND detected_at >= ?
                  AND detected_at <= ?
                LIMIT 1
                """,
                (
                    sensor_id,
                    _timestamp(observed - POST_WATERING_BASIS_END),
                    _timestamp(observed - POST_WATERING_BASIS_START),
                ),
            ).fetchone()
            is not None
        )

    def _last_valid_chemistry_basis(
        self, sensor_id: str, now: datetime
    ) -> Tuple[Optional[ChemistryBasis], bool]:
        """Return the reading a last-valid estimate rests on, and whether it follows watering.

        Every lookup is bounded by the lookback window and walks the
        (sensor_id, observed_at) index newest first, stopping at the first
        reading that yields an estimate.
        """
        window_start = now - CHEMISTRY_LOOKBACK
        waterings = self._database.execute(
            """
            SELECT detected_at
            FROM care_events
            WHERE plant_id IS (SELECT plant_id FROM sensors WHERE sensor_id = ?)
              AND kind = 'watering'
              AND detected_at >= ?
            ORDER BY detected_at DESC
            """,
            (sensor_id, _timestamp(window_start - POST_WATERING_BASIS_END)),
        ).fetchall()
        for (detected_at,) in waterings:
            watered = self._observation_datetime(detected_at, now)
            basis = self._newest_estimable_reading(
                sensor_id,
                max(window_start, watered + POST_WATERING_BASIS_START),
                min(now, watered + POST_WATERING_BASIS_END),
            )
            if basis is not None:
                return basis, True
        basis = self._newest_estimable_reading(sensor_id, window_start, now)
        if basis is None:
            return None, False
        return basis, self._follows_watering(
            sensor_id,
            self._observation_datetime(
                basis[0]["observed_at"], self._observation_datetime(basis[1], now)
            ),
        )

    def _newest_estimable_reading(
        self, sensor_id: str, start: datetime, end: datetime
    ) -> Optional[ChemistryBasis]:
        """Return the newest reading in [start, end] that yields a pore-water estimate.

        A reading is placed by observed_at, or by received_at when the sensor
        did not know the time, as everywhere else in the store. The two are
        looked up separately so each walks an index range rather than every
        reading the sensor ever sent.
        """
        if start > end:
            return None
        start_at, end_at = _timestamp(start), _timestamp(end)
        estimable = """
            AND conductivity_us_cm > 0
            AND moisture_percent >= ?
            AND soil_temperature_c IS NOT NULL
        """
        newest: Optional[Tuple[datetime, ChemistryBasis]] = None
        for time_filter, order in (
            # Walking the index backwards already puts equal times newest
            # row first, and saves sorting the whole window before the first.
            ("observed_at >= ? AND observed_at <= ?", "observed_at DESC"),
            (
                "observed_at IS NULL AND received_at >= ? AND received_at <= ?",
                "received_at DESC, reading_id DESC",
            ),
        ):
            cursor = self._database.execute(
                f"""
                SELECT {READING_COLUMNS}
                FROM sensor_readings
                WHERE sensor_id = ? AND {time_filter} {estimable}
                ORDER BY {order}
                """,
                (sensor_id, start_at, end_at, MIN_MOISTURE_PERCENT),
            )
            for row in cursor:
                item = self._serialize_row(row)
                candidate = item["reading"]
                estimate = estimate_pore_water_ec(
                    candidate["conductivity_us_cm"],
                    candidate["moisture_percent"],
                    candidate["soil_temperature_c"],
                )
                if estimate is None:
                    continue
                observed = self._observation_datetime(
                    candidate["observed_at"],
                    self._observation_datetime(item["received_at"], end),
                )
                if newest is None or observed > newest[0]:
                    newest = (observed, (candidate, item["received_at"], estimate))
                break
            cursor.close()
        return newest[1] if newest is not None else None

    def history(self, sensor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._condition:
            if sensor_id is not None:
                rows = self._database.execute(
                    f"""
                    SELECT {READING_COLUMNS}
                    FROM sensor_readings
                    WHERE sensor_id = ?
                    ORDER BY reading_id DESC
                    LIMIT ?
                    """,
                    (sensor_id, self._history_size),
                ).fetchall()
                return [self._serialize_row(row) for row in reversed(rows)]
            return [
                self._serialize(reading, received_at)
                for reading, received_at in self._history
            ]

    def with_pore_water(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach the pore-water estimate to each reading of a history.

        The two published models behind it live in Python, and a chart that
        wants the line should not mean a second copy of Topp and Hilhorst in
        JavaScript drifting away from this one. None where the soil was too
        dry for the model, which the chart draws as a gap rather than a
        line through soil nobody measured.
        """
        for item in items:
            reading = item.get("reading") or {}
            estimate = estimate_pore_water_ec(
                reading.get("conductivity_us_cm"),
                reading.get("moisture_percent"),
                reading.get("soil_temperature_c"),
            )
            item["pore_water_ec_us_cm"] = (
                None if estimate is None else round(estimate.pore_water_ec_us_cm)
            )
        return items

    def history_range(
        self,
        sensor_id: str,
        start_at: str,
        end_at: str,
        max_points: int = 600,
    ) -> List[Dict[str, Any]]:
        """Return evenly sampled sensor history for an inclusive observation-time range.

        A reading whose sensor did not know the time is placed by when it was
        received, which is the best the hub knows, without claiming that as the
        time it was taken.
        """
        safe_limit = max(2, min(max_points, 1000))
        parameters = (sensor_id, _bound(start_at), _bound(end_at, after=True))
        with self._condition:
            total = int(
                self._database.execute(
                    """
                    SELECT COUNT(*)
                    FROM sensor_readings
                    WHERE sensor_id = ?
                      AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') >= ?
                      AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') <= ?
                    """,
                    parameters,
                ).fetchone()[0]
            )
            if total == 0:
                return []

            columns = READING_COLUMNS
            if total <= safe_limit:
                rows = self._database.execute(
                    f"""
                    SELECT {columns}
                    FROM sensor_readings
                    WHERE sensor_id = ?
                      AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') >= ?
                      AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') <= ?
                    ORDER BY COALESCE(observed_at, received_at), reading_id
                    """,
                    parameters,
                ).fetchall()
            else:
                sample_step = ceil((total - 1) / (safe_limit - 1))
                rows = self._database.execute(
                    f"""
                    WITH ranged AS (
                        SELECT {columns},
                               ROW_NUMBER() OVER (
                                   ORDER BY COALESCE(observed_at, received_at), reading_id
                               ) AS sample_number
                        FROM sensor_readings
                        WHERE sensor_id = ?
                          AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') >= ?
                          AND COALESCE(observed_at, substr(received_at, 1, 19) || 'Z') <= ?
                    )
                    SELECT {columns}
                    FROM ranged
                    WHERE (sample_number - 1) % ? = 0 OR sample_number = ?
                    ORDER BY sample_number
                    """,
                    (*parameters, sample_step, total),
                ).fetchall()
        return [self._serialize_row(row) for row in rows]

    def care_log(
        self,
        limit: int = 50,
        sensor_id: Optional[str] = None,
        plant_id: Optional[int] = None,
        hidden_kinds: Optional[Sequence[str]] = None,
        before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """What happened to a plant, asked for by the plant or by its sensor.

        Asked by sensor, the answer is still the plant's: everything that
        happened to it, including what a previous sensor noticed and what
        somebody recorded by hand with no sensor involved.

        The kinds somebody has switched off are left out here rather than in
        the browser. A plant under a lamp writes two lighting lines a day, so
        a window of fifty events is a fortnight of the switch going on and off
        and nothing else; taking them out first spends the window on what was
        asked for. `before` continues from the oldest event already shown.
        """
        safe_limit = max(1, min(limit, 200))
        with self._condition:
            query = """
                SELECT event_id, sensor_id, kind, detected_at, title, summary, confidence, changes_json
                FROM care_events
            """
            where: List[str] = []
            parameters: List[Any] = []
            if plant_id is None and sensor_id is not None:
                plant_id = self._plant_id_for(sensor_id)
            if plant_id is not None:
                where.append("plant_id = ?")
                parameters.append(plant_id)
            elif sensor_id is not None:
                # A sensor nobody has claimed: its events are its own until a
                # plant adopts them.
                where.append("sensor_id = ?")
                parameters.append(sensor_id)
            kinds = [kind for kind in (hidden_kinds or []) if kind in CARE_KINDS]
            if kinds:
                where.append(f"kind NOT IN ({','.join('?' * len(kinds))})")
                parameters.extend(kinds)
            if before:
                where.append("detected_at < ?")
                parameters.append(before)
            if where:
                query += " WHERE " + " AND ".join(where)
            parameters.append(safe_limit)
            rows = self._database.execute(
                query + " ORDER BY detected_at DESC LIMIT ?", tuple(parameters)
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "sensor_id": row[1],
                "kind": row[2],
                "detected_at": row[3],
                "title": row[4],
                "summary": row[5],
                "confidence": row[6],
                "changes": json.loads(row[7]),
            }
            for row in rows
        ]

    def drainage_assessments(self, sensor_id: str, limit: int = 3) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(limit, 20))
        with self._condition:
            rows = self._database.execute(
                """
                SELECT event_id, sensor_id, kind, detected_at, title, summary, confidence, changes_json
                FROM care_events
                WHERE sensor_id = ? AND kind = 'drainage_assessment'
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (sensor_id, safe_limit),
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "sensor_id": row[1],
                "kind": row[2],
                "detected_at": row[3],
                "title": row[4],
                "summary": row[5],
                "confidence": row[6],
                "changes": json.loads(row[7]),
            }
            for row in rows
        ]

    def plant_wetness_from_weight(self, plant_id: int, start_at: str,
                                  end_at: str) -> Dict[str, float]:
        """How wet each day was, worked out from what the pot weighed.

        A probe measures the soil directly; a scale measures the same thing
        from the other side, because nearly all of what a pot gains and
        loses is water. Most plants here are weighed by hand and have no
        probe, so without this their year is blank where a watched plant's
        is shaded -- and the information to shade it was already recorded.

        The scale is the pot's own: nought is the lightest it has been seen
        (or an explicit dry mark) and a hundred is its watered weight. That
        makes a plant comparable with itself over a year, which is what the
        calendar is for. It does not make two plants comparable with each
        other, and neither does a probe.
        """
        with self._condition:
            rows = self._database.execute(
                """
                SELECT at, grams, kind, observed FROM plant_weights
                WHERE plant_id = ?
                ORDER BY at DESC
                """,
                (plant_id,),
            ).fetchall()
        if not rows:
            return {}

        weighings = [row for row in rows if row[3]]
        watered = next((row for row in rows if row[2] == "watered"), None)
        dry = next((row for row in rows if row[2] == "dry"), None)
        if dry is None and weighings:
            dry = min(weighings, key=lambda row: row[1])
        if watered is None or dry is None or watered[1] <= dry[1]:
            return {}
        span = watered[1] - dry[1]

        # The last weighing of each day, which is the state the day ended in
        # -- the same thing the probe's column reports.
        grams_by_day: Dict[str, float] = {}
        for at, grams, _kind, observed in sorted(weighings, key=lambda row: row[0]):
            day = self._local_day(at)
            if not start_at[:10] <= day < end_at[:10]:
                continue
            grams_by_day[day] = float(grams)

        filled = self._fill_drying_days(grams_by_day)
        return {
            day: round(min(100.0, max(0.0, (grams - dry[1]) / span * 100)), 2)
            for day, grams in sorted(filled.items())
        }

    # A pot that is not watered only loses water, and it loses it steadily
    # enough that the day between two weighings is not a mystery. Beyond
    # a fortnight it is: a plant weighed in March and again in June was
    # not drying in a straight line for ninety days.
    DRYING_GAP_LIMIT_DAYS = 14

    def _fill_drying_days(self, grams_by_day: Dict[str, float]) -> Dict[str, float]:
        """Fill the days between weighings, where filling them is honest.

        Only downhill, and only across short gaps. A pot that is heavier
        than it was has been watered, and nothing here knows when during
        the gap that happened -- drawing a gentle rise across those days
        would show a watering that never occurred, spread over a week.
        """
        days = sorted(grams_by_day)
        if len(days) < 2:
            return dict(grams_by_day)
        filled = dict(grams_by_day)
        for earlier, later in zip(days, days[1:]):
            start = date.fromisoformat(earlier)
            end = date.fromisoformat(later)
            step_count = (end - start).days
            if step_count < 2 or step_count > self.DRYING_GAP_LIMIT_DAYS:
                continue
            first, last = grams_by_day[earlier], grams_by_day[later]
            if last >= first:          # watered somewhere in here
                continue
            for step in range(1, step_count):
                middle = start + timedelta(days=step)
                filled[middle.isoformat()] = first + (last - first) * step / step_count
        return filled

    def plant_wetness_source(self, plant_id: int) -> Optional[str]:
        """Whether a plant's wetness comes from a probe or from weight.

        The two are different evidence and the page should not pass one
        off as the other: a probe reads the soil, a scale infers it from
        what the pot weighs.
        """
        with self._condition:
            probed = self._database.execute(
                "SELECT 1 FROM sensor_readings r JOIN sensors s"
                " ON s.sensor_id = r.sensor_id"
                " WHERE s.plant_id = ? AND r.moisture_percent IS NOT NULL LIMIT 1",
                (plant_id,),
            ).fetchone()
        if probed:
            return "probe"
        return "weight" if self.plant_weight_status(plant_id).get("watered") else None

    def watering_calendar(self, plant_id: int, start_at: str, end_at: str) -> List[Dict[str, Any]]:
        """A year of a plant's care, whether or not anything measures it.

        This took a sensor id and derived the plant from it, so a plant
        with no probe had no care history -- though watering and feeding
        are recorded by hand and stored against the plant, not the
        sensor. Nine of the ten plants here have no sensor, and all nine
        had an empty calendar.

        The soil shading behind each day still needs a probe, so a plant
        without one gets the squares and not the shading. That is the
        right amount of missing: what was measured is absent, what was
        recorded is not.
        """
        start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        with self._condition:
            sensor_row = self._database.execute(
                "SELECT sensor_id FROM sensors WHERE plant_id = ? "
                "AND COALESCE(archived, 0) = 0 ORDER BY sensor_id LIMIT 1",
                (plant_id,),
            ).fetchone()
            sensor_id = sensor_row[0] if sensor_row else None
            event_rows = self._database.execute(
                """
                SELECT kind, detected_at
                FROM care_events
                WHERE plant_id IS ?
                  AND kind IN ('watering', 'watering_due')
                  AND detected_at < ?
                ORDER BY detected_at
                """,
                (plant_id, end_at),
            ).fetchall()
            fertilizing_rows = self._database.execute(
                """
                SELECT detected_at FROM care_events
                WHERE plant_id IS ?
                  AND kind = 'fertilizing' AND detected_at < ?
                """,
                (plant_id, end_at),
            ).fetchall()
            moisture_rows = self._database.execute(
                """
                WITH dated AS (
                    SELECT reading_id, moisture_percent,
                           COALESCE(observed_at, received_at) AS reading_at
                    FROM sensor_readings
                    WHERE sensor_id = ? AND moisture_percent IS NOT NULL
                ),
                ranked AS (
                    SELECT substr(reading_at, 1, 10) AS date,
                           moisture_percent,
                           MIN(moisture_percent) OVER (
                               PARTITION BY substr(reading_at, 1, 10)
                           ) AS minimum_moisture,
                           ROW_NUMBER() OVER (
                               PARTITION BY substr(reading_at, 1, 10)
                               ORDER BY reading_at DESC, reading_id DESC
                           ) AS recency
                    FROM dated
                    WHERE reading_at >= ? AND reading_at < ?
                )
                SELECT date, minimum_moisture, moisture_percent
                FROM ranked
                WHERE recency = 1
                """,
                (
                    sensor_id,
                    (start - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    end_at,
                ),
            ).fetchall()
            latest_row = self._database.execute(
                """
                SELECT MAX(COALESCE(observed_at, received_at))
                FROM sensor_readings
                WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()

        minimum_moisture_by_day = {row[0]: float(row[1]) for row in moisture_rows}
        final_moisture_by_day = {row[0]: float(row[2]) for row in moisture_rows}
        # A probe if there is one, the pot's weight if not. Never both
        # at once: two scales of wetness shaded into one row of squares
        # would be a chart that changes meaning halfway along.
        from_weight: Dict[str, float] = {}
        self._last_wetness_source = "probe" if final_moisture_by_day else None
        if not final_moisture_by_day:
            from_weight = self.plant_wetness_from_weight(plant_id, start_at, end_at)
            final_moisture_by_day = dict(from_weight)
            minimum_moisture_by_day = dict(from_weight)
            if from_weight:
                self._last_wetness_source = "weight"
        latest_at = datetime.fromisoformat(latest_row[0].replace("Z", "+00:00")) if latest_row[0] else start
        activity: Dict[str, Dict[str, Any]] = {
            day: {
                "date": day,
                "watering_count": 0,
                "fertilizing_count": 0,
                "drying_level": 0,
                "final_moisture_percent": round(moisture, 2),
            }
            for day, moisture in final_moisture_by_day.items()
            if start.date().isoformat() <= day < end.date().isoformat()
        }
        # Feeding is marked on the day somebody would say they did it, which is
        # the day its entry is named after, not the UTC day it was stored on.
        for (detected_at,) in fertilizing_rows:
            day = self._local_day(detected_at)
            if not start.date().isoformat() <= day < end.date().isoformat():
                continue
            item = activity.setdefault(day, _empty_calendar_day(day))
            item["fertilizing_count"] += 1
        events = [
            (kind, datetime.fromisoformat(detected_at.replace("Z", "+00:00")), detected_at)
            for kind, detected_at in event_rows
        ]
        for kind, detected_at, stored_at in events:
            if kind == "watering" and start <= detected_at < end:
                # The day somebody would say they watered it, as feeding
                # already is. Filed on the UTC day instead, an evening
                # watering west of Greenwich landed on tomorrow's square --
                # and a feeding done at the same moment landed on today's,
                # so the pair came apart on the page.
                day = self._local_day(stored_at)
                item = activity.setdefault(day, _empty_calendar_day(day))
                item["watering_count"] += 1

        for index, (kind, due_at, _stored_at) in enumerate(events):
            if kind != "watering_due":
                continue
            watered_at = next(
                (detected_at for next_kind, detected_at, _ in events[index + 1:]
                 if next_kind == "watering"),
                None,
            )
            overdue_at = due_at + timedelta(hours=24)
            overdue_end = min(watered_at or latest_at, latest_at, end)
            if overdue_at > overdue_end:
                continue
            day = max(start.date(), overdue_at.date())
            final_day = overdue_end.date()
            drying_level = 1
            previous_moisture: Optional[float] = None
            while day <= final_day:
                day_key = day.isoformat()
                moisture = minimum_moisture_by_day.get(day_key)
                if previous_moisture is not None and moisture is not None and moisture < previous_moisture:
                    drying_level = min(3, drying_level + 1)
                item = activity.setdefault(
                    day_key,
                    _empty_calendar_day(day_key, final_moisture_by_day.get(day_key)),
                )
                if not item["watering_count"]:
                    item["drying_level"] = max(item["drying_level"], drying_level)
                if moisture is not None:
                    previous_moisture = moisture
                day += timedelta(days=1)

        return [activity[day] for day in sorted(activity)]

    def watering_interval_summary(self, plant_id: int) -> Dict[str, Any]:
        """How often this plant actually gets watered.

        Keyed on the plant for the same reason the calendar is: a
        watering is something somebody did, and it is recorded whether
        or not a probe was watching.
        """
        with self._condition:
            rows = self._database.execute(
                """
                SELECT DISTINCT substr(detected_at, 1, 10)
                FROM care_events
                WHERE plant_id IS ?
                  AND kind = 'watering'
                ORDER BY detected_at
                """,
                (plant_id,),
            ).fetchall()
        watering_days = [datetime.fromisoformat(row[0]).date() for row in rows]
        intervals = [
            (current - previous).days
            for previous, current in zip(watering_days, watering_days[1:])
        ]
        return {
            "typical_days": round(float(median(intervals)), 1) if intervals else None,
            "interval_count": len(intervals),
        }

    def plant_journey(self, sensor_id: str) -> Dict[str, Any]:
        """The lifetime figures of the plant this sensor watches."""
        plant_id = self._plant_id_for(sensor_id)
        if plant_id is None:
            return dict(EMPTY_JOURNEY)
        return self.plant_journey_of(plant_id)

    def plant_journey_of(self, plant_id: int) -> Dict[str, Any]:
        """The lifetime figures, read rather than counted again.

        Everything that only changes when something happens is kept in the
        plant_journey row and updated as it happens. Everything whose answer is
        the clock — a lamp that is on now, a watering window that becomes missed
        because nothing came — is worked out here, from a handful of rows.
        """
        now = datetime.now(timezone.utc)
        with self._condition:
            row = self._database.execute(
                """
                SELECT first_reading_at, last_reading_at, watering_count,
                       fertilizing_count, settled_missed_count, lighting_seconds,
                       lighting_open_json
                FROM plant_journey WHERE plant_id = ?
                """,
                (plant_id,),
            ).fetchone()
            if row is None:
                if self._database.execute(
                    "SELECT 1 FROM plants WHERE plant_id = ?", (plant_id,)
                ).fetchone() is None:
                    # Asking about a plant this hub has never heard of is not an
                    # error; it is a plant with no journey.
                    return dict(EMPTY_JOURNEY)
                # A plant from before these figures were kept: count it once,
                # and it is kept from then on.
                row = self._rebuild_journey(plant_id)
                self._database.commit()
            first_at, last_at, waterings, fertilizings, settled_missed, lit_seconds, _ = row
            open_missed = self._open_missed_windows(plant_id, now)
            started = self._database.execute(
                "SELECT created_at FROM plants WHERE plant_id = ?", (plant_id,)
            ).fetchone()
            started_at = started[0] if started else None

        monitored_days = 0
        if first_at and last_at:
            first = self._observation_datetime(first_at, now)
            last = self._observation_datetime(last_at, now)
            monitored_days = (last.date() - first.date()).days + 1
        elif started_at:
            # A plant with no readings is still being looked after. Its days
            # run from the day somebody took it on to today, because nobody
            # stops caring for a plant between entries in a log.
            first_at = started_at
            first = self._observation_datetime(started_at, now)
            monitored_days = (now.date() - first.date()).days + 1

        return {
            "started_at": first_at,
            "monitored_days": monitored_days,
            "watering_count": waterings,
            "fertilizing_count": fertilizings,
            "missed_watering_count": settled_missed + open_missed,
            # Whole hours, and only from stretches that closed: a lamp that is
            # on right now earns its hours when it goes off.
            "lighting_hours": round(lit_seconds / 3600.0),
        }

    def _open_missed_windows(self, plant_id: int, now: datetime) -> int:
        """Watering windows still open, and already a day overdue.

        Only the windows since the last watering can still be open, so this
        reads those rather than the whole history.
        """
        rows = self._database.execute(
            """
            SELECT detected_at FROM care_events
            WHERE plant_id = ? AND kind = 'watering_due'
              AND detected_at > COALESCE(
                  (SELECT MAX(detected_at) FROM care_events
                   WHERE plant_id = ? AND kind = 'watering'), '')
            """,
            (plant_id, plant_id),
        ).fetchall()
        deadline = now - MISSED_WATERING_GRACE
        return sum(1 for (due_at,) in rows if self._observation_datetime(due_at, now) < deadline)

    def _local_day(self, detected_at: str) -> str:
        """The day a person would call it.

        Everything here is stored in UTC, but a feeding at eight in the evening
        is that evening's, not the small hours of the next day's, so the day a
        care event belongs to is read off the hub's chosen zone -- the same one
        a scale's daily weighing hour is read off, so that a hub set to
        Vancouver does not file the evening's watering under tomorrow while
        also weighing at the right time.
        """
        try:
            at = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        except ValueError:
            at = datetime.now(timezone.utc)
        return at.astimezone(self.hub_zone()).date().isoformat()

    def record_manual_care(
        self, sensor_id: str, kind: str, at: Optional[str] = None, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Record care given to the plant this sensor watches."""
        plant_id = self._plant_id_for(sensor_id)
        if plant_id is None:
            raise ValueError("sensor_id has not been observed")
        return self.record_plant_care(plant_id, kind, at, note)

    def record_plant_care(
        self,
        plant_id: int,
        kind: str,
        at: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record care somebody gave a plant, as told by them.

        A probe can suggest a watering from a rise in moisture, and a feeding
        from a rise in conductivity, but on a plant with no probe at all it can
        see nothing. What somebody did is not a guess, so it is recorded as
        theirs: once for the day, because a plant is watered or fed once on a
        day or not at all. A note is not, because two things worth writing down
        can happen between breakfast and bed.
        """
        if kind not in MANUAL_CARE_KINDS:
            raise ValueError(f"unknown care kind: {kind}")
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        note = (note or "").strip()
        if kind == "note" and not note:
            raise ValueError("a note needs something written in it")
        detected_at = self._validate_care_time(at)
        with self._condition:
            self._save_event(
                CareEvent(
                    # _save_event renames a once-a-day event after its day, so
                    # whichever of these ids it starts with is replaced.
                    event_id=f"manual-{kind}-{plant_id}-{uuid4().hex}",
                    sensor_id="",
                    kind=kind,
                    detected_at=detected_at,
                    title=MANUAL_CARE_KINDS[kind],
                    summary=note or "Recorded by hand.",
                    confidence="recorded",
                    changes={"source": "manual"},
                ),
                plant_id=plant_id,
            )
        return self.plant_journey_of(plant_id)

    def delete_care_event(self, event_id: str) -> Dict[str, Any]:
        """Strike a care event that should not have been written down.

        What somebody did is recorded as theirs, and so is what they did by
        mistake: a note filed under the wrong day, or a feeding entered twice.
        The journey figures are a cache over these rows, so they are counted
        again afterwards rather than nudged -- a count that is derived cannot
        be kept right by subtracting.
        """
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT plant_id FROM care_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise ValueError("no such care event")
            plant_id = None if row[0] is None else int(row[0])
            self._database.execute(
                "DELETE FROM care_events WHERE event_id = ?", (event_id,)
            )
            if plant_id is not None:
                self._rebuild_journey(plant_id)
        return EMPTY_JOURNEY if plant_id is None else self.plant_journey_of(plant_id)

    @staticmethod
    def _validate_care_time(at: Optional[str]) -> str:
        """When the care happened, which is often before it was written down.

        A plant is looked after with wet hands, and the note about it is typed
        that evening or the next morning. Any past moment is therefore allowed.
        A future one is not: nothing has been watered tomorrow.
        """
        now = datetime.now(timezone.utc)
        if not at:
            return _timestamp(now)
        try:
            when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("at must be a timestamp") from error
        if when.tzinfo is None:
            raise ValueError("at must say which time zone it is in")
        when = when.astimezone(timezone.utc)
        if when > now + timedelta(minutes=5):
            raise ValueError("care cannot have been given in the future")
        return _timestamp(when)

    # What counts as a sudden gain. Proportional, because a gain that
    # matters on a 500 g pot is noise on a 20 kg one, with a floor so a
    # small pot's scale drift is not read as a watering.
    REFILL_FRACTION = 0.08
    REFILL_FLOOR_GRAMS = 40

    def _is_a_refill(self, plant_id: int, grams: int, at: str) -> bool:
        """Whether this weighing is heavier than the last by enough to be water.

        A plant only ever loses weight between weighings; it cannot
        drink backwards. So a jump is somebody with a watering can, and
        the only question is how big a jump is beyond doubt. Repotting
        and adding a top dressing also add weight, which is why the
        weighing's own kind is left alone when it was given one -- this
        only fills in what would otherwise be recorded as an ordinary
        reading.
        """
        with self._condition:
            row = self._database.execute(
                """
                SELECT grams FROM plant_weights
                WHERE plant_id = ? AND observed = 1 AND at < ?
                ORDER BY at DESC, weight_id DESC LIMIT 1
                """,
                (plant_id, at),
            ).fetchone()
        if row is None:
            return False
        gained = grams - row[0]
        return gained >= max(self.REFILL_FLOOR_GRAMS, row[0] * self.REFILL_FRACTION)

    def record_plant_weight(
        self,
        plant_id: int,
        grams: int,
        kind: str = "reading",
        note: Optional[str] = None,
        at: Optional[str] = None,
        source: str = "manual",
        observed: bool = True,
    ) -> Dict[str, Any]:
        """Write down what the pot weighs.

        Recording the watered weight also records the watering: that weighing
        is taken because the plant has just been watered and drained, and
        making somebody say so twice invites the two to disagree.

        observed=False marks a claim rather than an observation -- an anchor
        typed in from memory, saying what the pot weighs full or dry, not what
        it weighs now. Such a row anchors but is not a weighing: it is not the
        latest weight, it is no part of the drying rate, and recording the
        full weight that way is not watering the plant.
        """
        if kind not in WEIGHT_KINDS:
            raise ValueError(f"unknown weight kind: {kind}")
        if source not in ("manual", "scale"):
            raise ValueError(f"unknown weight source: {source}")
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        grams = int(round(float(grams)))
        if not 1 <= grams <= 500_000:
            raise ValueError("a pot weighs between 1 and 500000 grams")
        at = self._validate_weighing_time(at)
        # What else was on the scale comes off what was measured.
        #
        # "The scale says X, so the plant weighs X minus the probe and
        # the cache pot" is a fact about reading a scale, and it holds
        # whether a person read it or the scale reported it. An anchor
        # typed in from memory is already the plant's own weight, so
        # nothing is taken off that.
        offset = 0
        if observed:
            offset = int(self.plant(plant_id).get("weight_offset_grams") or 0)
            if offset:
                grams = grams - offset
                if grams < 1:
                    raise ValueError(
                        f"{grams + offset} g is not more than the {offset} g "
                        "of probe and pot that come off it")
        if (kind == "reading" and observed and source == "manual"
                and self._is_a_refill(plant_id, grams, at)):
            kind = "watered"
        with self._condition, self._database:
            self._database.execute(
                """
                INSERT INTO plant_weights
                    (plant_id, at, grams, source, kind, note, observed, offset_grams)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plant_id, at, grams, source, kind,
                    (note or "").strip() or None, 1 if observed else 0, offset,
                ),
            )
        if kind == "watered" and observed:
            self.record_plant_care(plant_id, "watering", at)
        if observed and self._is_current_weighing(plant_id, at):
            # A new weighing is the only thing that can change what the water
            # use says, so it is the moment to look. A weighing written down
            # late is not: filling in last week says nothing about today, and
            # the part-built history it is read against would have the hub
            # announcing findings it will contradict two rows later.
            self.record_water_use_findings(plant_id)
        return self.plant_weight_status(plant_id)

    @staticmethod
    def _validate_weighing_time(at: Optional[str]) -> str:
        """When the pot was on the scale.

        A weighing can be entered late -- the number was written on a scrap of
        paper on Tuesday -- so any past moment is allowed. A future one is
        not: nothing has been weighed tomorrow, and a figure dated ahead would
        sit at the end of every history until the day caught up with it.
        """
        now = datetime.now(timezone.utc)
        if not at:
            return _timestamp(now)
        try:
            when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("at must be a timestamp") from error
        if when.tzinfo is None:
            raise ValueError("at must say which time zone it is in")
        when = when.astimezone(timezone.utc)
        if when > now + timedelta(minutes=5):
            raise ValueError("a pot cannot be weighed in the future")
        return _timestamp(when)

    def plant_weights(self, plant_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self._condition:
            rows = self._database.execute(
                """
                SELECT weight_id, at, grams, source, kind, note, observed, offset_grams
                FROM plant_weights WHERE plant_id = ?
                ORDER BY at DESC, weight_id DESC LIMIT ?
                """,
                (plant_id, safe_limit),
            ).fetchall()
        return [
            {
                "weight_id": row[0],
                "at": row[1],
                "grams": row[2],
                "source": row[3],
                "kind": row[4],
                "note": row[5],
                "observed": bool(row[6]),
                # What came off this one, so the number read off the
                # scale is still recoverable: gross = grams + offset.
                "offset_grams": int(row[7] or 0),
            }
            for row in rows
        ]

    def set_plant_weight_offset(self, plant_id: int, grams: int) -> Dict[str, Any]:
        """What else sits on the scale with this plant.

        Changed from today onwards, never backwards: weighings already
        taken had whatever was on the scale at the time taken off
        them, and a probe added last week did not affect the month
        before it.
        """
        grams = int(round(float(grams)))
        if not 0 <= grams <= 100_000:
            raise ValueError("an offset is between 0 and 100000 grams")
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE plants SET weight_offset_grams = ? WHERE plant_id = ?",
                (grams, plant_id))
        return self.plant(plant_id)

    def delete_plant_weight(self, weight_id: int) -> Dict[str, Any]:
        """Strike a weighing that should not have been written down.

        A number typed wrong is not a measurement, and leaving it in drags
        the drying rate, the forecast and the chart along with it. Nothing
        else is touched: a watering recorded the same moment still happened,
        and is still in the care log for whoever wants to take it out from
        there.
        """
        with self._condition, self._database:
            row = self._database.execute(
                "SELECT plant_id FROM plant_weights WHERE weight_id = ?", (weight_id,)
            ).fetchone()
            if row is None:
                raise ValueError("no such weighing")
            plant_id = int(row[0])
            self._database.execute(
                "DELETE FROM plant_weights WHERE weight_id = ?", (weight_id,)
            )
        return self.plant_weight_status(plant_id)

    def remember_zigbee_host(self, sensor_id: str, host: str) -> None:
        """Keep the name a device answers to, so a restart does not lose it."""
        if not host:
            return
        with self._condition, self._database:
            self._database.execute(
                "UPDATE sensors SET zigbee_host = ? WHERE sensor_id = ?",
                (host, sensor_id),
            )

    def record_gateway_restart(self, reason: Optional[str],
                               ran_for_s: Optional[int]) -> None:
        """Note that the gateway started, and how long the last run lasted."""
        with self._condition, self._database:
            self._database.execute(
                "INSERT INTO gateway_restarts (at, reason, ran_for_s) VALUES (?, ?, ?)",
                (_timestamp(datetime.now(timezone.utc)), reason,
                 None if ran_for_s is None else int(ran_for_s)),
            )
            # A rolling window: what matters is whether this is happening,
            # not every instance since the hub was built.
            self._database.execute(
                "DELETE FROM gateway_restarts WHERE restart_id NOT IN "
                "(SELECT restart_id FROM gateway_restarts ORDER BY at DESC LIMIT 50)"
            )

    def gateway_restarts(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._condition:
            rows = self._database.execute(
                "SELECT at, reason, ran_for_s FROM gateway_restarts"
                " ORDER BY at DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [{"at": r[0], "reason": r[1], "ran_for_s": r[2]} for r in rows]

    def plant_light_days(self, plant_id: int, days: int = 400) -> List[Dict[str, Any]]:
        """The light a plant gets in a day: its PPFD over the hours it is lit.

            DLI = ppfd * hours * 3600 / 1e6

        The hours come from the lamp's daily schedule, or -- for a plant
        with no lamp the hub switches -- from the hours typed on its
        configuration page. That is the whole calculation.

        KEEP THIS SIMPLE. It was once worked out by replaying every
        switching event the hub had recorded: carrying a lamp's state
        across midnight, merging overlapping runs from several lamps,
        and tracking "unsure" hours for commands that went unconfirmed.
        All of it defensible, and the result was a plant under a lamp
        that had been lit all day reporting 0.3 mol/m2/day, because the
        lamp's Wi-Fi had dropped a few times and the hub would not
        claim light it had not confirmed. The figure it produced was
        about the network, not the plant.

        A schedule says when the lamp is on. If it is not on when it
        should be, that is worth saying somewhere -- the light's own
        page says it, in words -- but it is not worth turning this
        number into a measurement of the hub's confidence. Resist
        adding event replay, confidence weighting or partial-day
        arithmetic back in: each one is reasonable on its own and
        together they produced a number nobody could explain.
        """
        zone = self.hub_zone()
        with self._condition:
            row = self._database.execute(
                "SELECT ppfd_umol_m2s, light_hours_per_day FROM plants WHERE plant_id = ?",
                (plant_id,),
            ).fetchone()
        if not row or not row[0]:
            return []
        ppfd, typed_hours = float(row[0]), row[1]
        hours = self._scheduled_hours(plant_id)
        if hours is None:
            hours = float(typed_hours) if typed_hours else None
        if not hours:
            return []
        # One entry, for the last day that has finished. The page shows
        # a whole day's light and today is not one yet; with the hours
        # coming from a schedule rather than a recording, every finished
        # day is the same day anyway.
        yesterday = (datetime.now(timezone.utc).astimezone(zone).date()
                     - timedelta(days=1)).isoformat()
        return [{
            "date": yesterday,
            "dli": round(ppfd * hours * 3600 / 1_000_000, 2),
            "lit_hours": round(hours, 1),
            "complete": True,
        }]

    def _scheduled_hours(self, plant_id: int) -> Optional[float]:
        """Hours a day this plant's lamps are scheduled on, or None.

        Merged rather than added: two lamps sharing a window light the
        plant once, and the PPFD measured at its leaves already has
        both of them in it.
        """
        with self._condition:
            rows = self._database.execute(
                """
                SELECT light_schedules.on_time, light_schedules.off_time
                FROM plant_lights
                JOIN light_schedules ON light_schedules.light_id = plant_lights.light_id
                WHERE plant_lights.plant_id = ? AND light_schedules.enabled = 1
                """,
                (plant_id,),
            ).fetchall()
        windows: List[Tuple[int, int]] = []
        for on_at, off_at in rows:
            start_minute = _minutes_of_day(on_at)
            end_minute = _minutes_of_day(off_at)
            if start_minute is None or end_minute is None or start_minute == end_minute:
                continue
            if end_minute < start_minute:
                # A window that runs past midnight is two windows of one day.
                windows.append((start_minute, 1440))
                windows.append((0, end_minute))
            else:
                windows.append((start_minute, end_minute))
        if not windows:
            return None
        covered = 0
        reach = 0
        for start_minute, end_minute in sorted(windows):
            start_minute = max(start_minute, reach)
            if end_minute > start_minute:
                covered += end_minute - start_minute
                reach = end_minute
        return covered / 60

    @staticmethod
    def _merge_runs(
        runs: List[Tuple[datetime, datetime, bool]]
    ) -> List[Tuple[datetime, datetime, bool]]:
        """Overlapping lit stretches, flattened into the time actually lit.

        A stretch is doubtful if any lamp contributing to it was. Doubt is
        not cancelled by a second lamp that answered: the hub still cannot
        say what the plant stood in.
        """
        merged: List[Tuple[datetime, datetime, bool]] = []
        for start, end, doubtful in sorted(runs):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                last_start, last_end, last_doubt = merged[-1]
                merged[-1] = (last_start, max(last_end, end), last_doubt or doubtful)
            else:
                merged.append((start, end, doubtful))
        return merged

    @staticmethod
    def _spread_seconds(into: Dict[str, float], start: datetime, end: datetime,
                        zone: Any) -> None:
        """Add a lit stretch to each local day it covers.

        A lamp on from 19:00 to 02:00 belongs to two days, in the
        proportions it actually ran, not wholly to whichever one the hub
        happened to look at.
        """
        if end <= start:
            return
        cursor = start
        while cursor < end:
            local = cursor.astimezone(zone)
            midnight = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                        + timedelta(days=1)).astimezone(timezone.utc)
            piece_end = min(end, midnight)
            seconds = (piece_end - cursor).total_seconds()
            into[local.date().isoformat()] = into.get(
                local.date().isoformat(), 0.0) + seconds
            cursor = piece_end

    def record_battery(self, sensor_id: str, percent: Optional[int],
                       volts: Optional[float], at: Optional[datetime] = None) -> None:
        """Note a battery's charge, at most once an hour per sensor.

        Nothing is recorded when there is no charge to record: a device
        with no way to measure its cell reports -1, and a row saying
        "unknown" every hour is a chart of our own ignorance.
        """
        if percent is None or percent < 0:
            return
        moment = at or datetime.now(timezone.utc)
        hour = moment.strftime("%Y-%m-%dT%H:00:00Z")
        with self._condition, self._database:
            self._database.execute(
                """
                INSERT INTO battery_history (sensor_id, hour, percent, volts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (sensor_id, hour) DO UPDATE
                    SET percent = excluded.percent, volts = excluded.volts
                """,
                (sensor_id, hour, int(percent),
                 None if volts is None else float(volts)),
            )

    def battery_history(self, sensor_id: str, since: Optional[datetime] = None,
                        limit: int = 4000) -> List[Dict[str, Any]]:
        """A sensor's charge over time, oldest first.

        The limit is a guard rather than a feature: an hourly row for ten
        years is under ninety thousand, and no chart wants them all at once.
        """
        query = "SELECT hour, percent, volts FROM battery_history WHERE sensor_id = ?"
        parameters: List[Any] = [sensor_id]
        if since is not None:
            query += " AND hour >= ?"
            parameters.append(since.strftime("%Y-%m-%dT%H:00:00Z"))
        query += " ORDER BY hour LIMIT ?"
        parameters.append(int(limit))
        with self._condition:
            rows = self._database.execute(query, tuple(parameters)).fetchall()
        return [{"at": row[0], "percent": row[1], "volts": row[2]} for row in rows]

    def plant_scale(self, plant_id: int) -> Optional[Dict[str, Any]]:
        """The scale that weighs this plant by itself, if one is attached.

        What the plant page needs to know before offering a form: a weight
        that arrives on its own and a weight somebody types are the same
        number from two sources, and the page should not ask for the second
        as though the first were not happening.
        """
        with self._condition:
            row = self._database.execute(
                """
                SELECT sensor_id, display_name, transport, identity_value,
                       scale_keep_every_seconds, scale_keep_at_hour
                FROM sensors
                WHERE plant_id = ? AND enrollment_status = 'enrolled'
                  AND transport IN ('http-scale', 'zigbee-scale')
                ORDER BY sensor_id
                LIMIT 1
                """,
                (plant_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "sensor_id": row[0],
            "display_name": row[1] or row[0],
            "transport": row[2],
            "eui": row[3] if str(row[2]).startswith("zigbee") else None,
            "keep_every_seconds": row[4],
            "keep_at_hour": row[5],
        }

    def plant_weight_status(self, plant_id: int) -> Dict[str, Any]:
        """What the weighings say about the water in the pot.

        Every figure here is absent rather than guessed. A pot with one
        weighing knows what it weighs and nothing else; it takes a ceiling to
        know how much water has gone, a floor to know how much is left to go,
        and two weighings far enough apart to know how fast.
        """
        now = datetime.now(timezone.utc)
        with self._condition:
            rows = self._database.execute(
                """
                SELECT at, grams, kind, observed FROM plant_weights
                WHERE plant_id = ? ORDER BY at DESC, weight_id DESC
                """,
                (plant_id,),
            ).fetchall()
        status: Dict[str, Any] = {
            "latest": None,
            "full": None,
            "watered": None,
            "thirsty": None,
            "dry": None,
            "dry_stated": False,
            "lightest": None,
            "used_grams": None,
            "capacity_grams": None,
            "remaining_percent": None,
            "thirsty_percent": None,
            "held_grams": None,
            "daily_grams": None,
            "days_left": None,
        }
        if not rows:
            return status

        def point(row: Tuple[Any, ...]) -> Dict[str, Any]:
            return {"at": row[0], "grams": row[1], "observed": bool(row[3])}

        # An anchor typed in from memory anchors; it is not a weighing, so it
        # is not what the pot weighs now.
        weighings = [row for row in rows if row[3]]
        latest = weighings[0] if weighings else None
        status["latest"] = point(latest) if latest else None
        # The ceiling and the event are two different rows now.
        #
        # They were one -- kind "watered" for both -- so the Set
        # button and a watering wrote the same thing and the ceiling
        # was whichever came last. Every watering redefined how much
        # the pot holds, and once waterings were tagged automatically
        # the number moved with nobody touching it.
        full = next((row for row in rows if row[2] == "full"), None)
        watered = next((row for row in rows if row[2] == "watered"), None)
        thirsty = next((row for row in rows if row[2] == "thirsty"), None)
        # What the pot weighs dry can be stated, and otherwise the scale has
        # already said it: a pot cannot have held less water than the least
        # it has ever weighed, so the lightest weighing stands until
        # somebody sets one. A typed one wins even when the pot has since
        # weighed less -- it is a claim about the soil, and the scale
        # catching it lighter is worth showing rather than silently
        # overruling. The lightest weighing is kept beside it for that.
        stated_dry = next((row for row in rows if row[2] == "dry"), None)
        lightest = min(weighings, key=lambda row: row[1]) if weighings else None
        dry = stated_dry or lightest
        # What the pot weighs full, as somebody stated it.
        status["full"] = point(full) if full else None
        # When it was last watered, which is an event and says nothing
        # about capacity.
        status["watered"] = point(watered) if watered else None
        status["thirsty"] = point(thirsty) if thirsty else None
        status["dry"] = point(dry) if dry else None
        # Which of the two the dry figure is, so a page can say "Dry at"
        # rather than "Driest seen" and mean it.
        status["dry_stated"] = stated_dry is not None
        status["lightest"] = point(lightest) if lightest else None
        # How much water the pot holds is the distance between its anchors,
        # which is known as soon as both are, weighed or typed in.
        capacity = None
        if full and thirsty and full[1] > thirsty[1]:
            capacity = full[1] - thirsty[1]
            status["capacity_grams"] = capacity
        # What the pot holds between dry and full is all the water there is to
        # hold. Capacity above is the part of it this plant is allowed to use
        # before somebody waters, which is a smaller and more arbitrary number.
        # The bottom of the water scale: the lowest weight this pot is
        # expected to reach.
        #
        # Usually the dry weight. But dry is inferred from the lightest
        # weighing when nobody states one, and a pot never let near dry
        # has a floor well above the weight its owner waters it at -- a
        # stated thirsty of 2000 g under a lightest weighing of 2585 g.
        # Measured from that floor such a pot reads empty whenever it
        # is at its own lightest, which is most of the time.
        floor = dry
        if thirsty and dry and thirsty[1] < dry[1]:
            floor = thirsty
        held = None
        if full and floor and full[1] > floor[1]:
            held = full[1] - floor[1]
            status["held_grams"] = held
        if latest is None:
            return status

        latest_grams = latest[1]
        # From the watering, not the ceiling: "how much has gone since
        # it was watered" is about an event, while the ceiling is a
        # property of the pot. Falling back to the ceiling lets a pot
        # that has never been watered still say how much it has lost
        # since it was full.
        from_what = watered or full
        if from_what:
            status["used_grams"] = max(0, from_what[1] - latest_grams)
        # "Water left" is measured from dry, the same scale "Water used"
        # is counted on.
        #
        # Against the thirsty weight it read 0 % on a pot still holding
        # sixteen grams, because the thirsty mark is where somebody
        # decided to water, not where the water runs out -- and it sat
        # beside "Water used 97 ml", counted from full, so the pair did
        # not add up. From dry they do: 97 ml used of the 113 ml this
        # pot holds, 14 % left, and 0 % means dry.
        #
        # Left uncapped at the bottom would be the honest reading, but a
        # pot lighter than its dry weight means the anchor is wrong or
        # the pot lost substrate, and neither is a wetness to report.
        if held:
            water = (latest_grams - floor[1]) / held * 100
            status["remaining_percent"] = round(min(100.0, max(0.0, water)))
            # Where this plant asks for water, on that same scale, so a
            # page can mark the line instead of guessing at one.
            if thirsty:
                mark = (thirsty[1] - floor[1]) / held * 100
                status["thirsty_percent"] = round(min(100.0, max(0.0, mark)))

        # How fast it is drying, averaged over the gaps between weighings
        # and skipping any gap where the pot gained weight.
        #
        # This was once the oldest weighing against the newest, which is
        # a drying rate only if nobody watered in between. Four
        # waterings recorded as plain weighings -- the tick not pressed
        # -- cancelled eleven days of drying down to sixteen grams, and
        # the page offered "about 124.8 days of water left" beside a
        # forecast of five. The forecast was right; this is the same
        # arithmetic, so there is one answer instead of two.
        since = watered[0] if watered else None
        cycle = [row for row in weighings if since is None or row[0] >= since]
        daily = self._drying_rate(cycle, now)
        if daily:
            status["daily_grams"] = round(daily)
            newest = cycle[0]
            if thirsty and newest[1] > thirsty[1]:
                status["days_left"] = round((newest[1] - thirsty[1]) / daily, 1)
        return status

    def _drying_rate(self, weighings: List[Tuple[Any, float]],
                     now: datetime) -> Optional[float]:
        """Grams a day lost, from the gaps that were drying.

        A gap where the pot grew heavier is somebody watering it, not
        the plant drinking backwards, so it is left out rather than
        subtracted. Without that one refill can hide a fortnight of use.
        """
        rates = []
        for newer, older in zip(weighings, weighings[1:]):
            span = (self._observation_datetime(newer[0], now)
                    - self._observation_datetime(older[0], now))
            lost = older[1] - newer[1]
            if span >= MIN_WEIGHT_SPAN and lost > 0:
                rates.append(lost / (span.total_seconds() / 86400.0))
        return sum(rates) / len(rates) if rates else None

    def water_use(
        self, plant_id: int, days: int = 90, with_air: bool = True
    ) -> Dict[str, Any]:
        """How much water this plant has been using, and over what.

        A pot on a scale is a weighing lysimeter: what it loses between two
        weighings, with nothing added, is water that left through the leaves
        and the soil surface. Soil scientists call that evapotranspiration --
        transpiration by the plant and evaporation from the pot, which
        gravimetry cannot tell apart without a second pot of bare soil to
        compare against.

        A gram of water is a millilitre, so no conversion is needed and none
        is invented.

        The awkward part is that nobody weighs a pot every day. The honest
        answer is a rate per interval between weighings rather than a figure
        per day: five days and 400 ml says 80 ml a day on average, and says
        nothing about which of the five days was thirstiest. Intervals that
        span a watering are left out entirely -- water went in as well as
        out, and one weighing cannot separate them.
        """
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=max(1, min(days, 3650)))
        with self._condition:
            if self.plant(plant_id) is None:
                raise ValueError("unknown plant")
            rows = self._database.execute(
                """
                SELECT at, grams FROM plant_weights
                WHERE plant_id = ? AND observed = 1 AND at >= ?
                ORDER BY at, weight_id
                """,
                (plant_id, _timestamp(since)),
            ).fetchall()
            # Both break a span: a watering puts water in, and a repot
            # changes what the pot is made of. Neither is the plant
            # drinking, and one weighing either side cannot tell them apart
            # from it.
            waterings = [
                row[0]
                for row in self._database.execute(
                    """
                    SELECT detected_at FROM care_events
                    WHERE plant_id = ? AND kind IN ('watering', 'repotting')
                      AND detected_at >= ?
                    ORDER BY detected_at
                    """,
                    (plant_id, _timestamp(since)),
                )
            ]

        plant = self.plant(plant_id)
        # Aggregating a season of air readings costs more than everything
        # else here together, and the household list wants only the mean
        # rate. It asks without the air.
        drying_hours = self._room_drying_hours(
            plant["room_id"] if plant else None, since, now
        ) if with_air else {}
        points = [(self._observation_datetime(row[0], now), row[1]) for row in rows]
        watered_at = [self._observation_datetime(stamp, now) for stamp in waterings]
        intervals: List[Dict[str, Any]] = []

        # Weighings close together are not a span, and the last of them is
        # the one to believe: a pot weighed twice within the hour was
        # weighed again because the first figure was wrong, or put on the
        # scale for a different reason. Taking the first would have a pot
        # that read 2762 and then 2055 twelve minutes later losing 700
        # grams of water before lunch.
        #
        # So a cluster collapses to its last member, and a span runs from
        # there to the next weighing far enough away to mean something.
        index = 0
        while index < len(points):
            cluster = index
            while (cluster + 1 < len(points)
                   and points[cluster + 1][0] - points[index][0] < MIN_WATER_USE_SPAN):
                cluster += 1
            start_at, start_grams = points[cluster]
            step = cluster + 1
            while step < len(points) and points[step][0] - start_at < MIN_WATER_USE_SPAN:
                step += 1
            if step >= len(points):
                break
            end_at, end_grams = points[step]
            poured = any(start_at < when <= end_at for when in watered_at)
            seconds = (end_at - start_at).total_seconds()
            lost = start_grams - end_grams
            if not poured and lost >= 0 and seconds > 0:
                per_day = lost / (seconds / 86400.0)
                drying, covered = self._mean_drying(drying_hours, start_at, end_at)
                intervals.append({
                    "from": _timestamp(start_at),
                    "to": _timestamp(end_at),
                    "days": round(seconds / 86400.0, 2),
                    "grams": lost,
                    "grams_per_day": round(per_day),
                    # How hard the air was pulling while this was measured,
                    # and the rate with that divided out: millilitres a day
                    # per kilopascal is the plant's own part of it.
                    "drying_kpa": drying,
                    "air_coverage": covered,
                    "grams_per_day_per_kpa": (
                        round(per_day / drying, 1) if drying else None
                    ),
                })
            index = step

        measured_days = sum(interval["days"] for interval in intervals)
        used = sum(interval["grams"] for interval in intervals)
        return {
            "intervals": intervals,
            "window_days": days,
            # What the average rests on, said plainly: a mean over four
            # measured days out of ninety is not the same claim as one over
            # eighty, and the page should be able to say which it has.
            "measured_days": round(measured_days, 1),
            "used_grams": used,
            "daily_grams": round(used / measured_days) if measured_days else None,
            # Whether the air was measured at all, so the page can say
            # whether the rates have been held against it.
            "drying_known": any(i["drying_kpa"] is not None for i in intervals),
            "lowest_daily_grams": min(
                (i["grams_per_day"] for i in intervals), default=None
            ),
            "highest_daily_grams": max(
                (i["grams_per_day"] for i in intervals), default=None
            ),
        }

    def _is_current_weighing(self, plant_id: int, at: str) -> bool:
        """Whether this weighing is the plant's newest, and of about now."""
        now = datetime.now(timezone.utc)
        when = self._observation_datetime(at, now)
        if now - when > timedelta(days=2):
            return False
        with self._condition:
            newest = self._database.execute(
                """
                SELECT MAX(at) FROM plant_weights WHERE plant_id = ? AND observed = 1
                """,
                (plant_id,),
            ).fetchone()[0]
        return newest is None or self._observation_datetime(newest, now) <= when

    def room_air_sources(self, room_id: Optional[int]) -> List[Dict[str, Any]]:
        """Every sensor measuring the air in this room, newest first.

        One entry per sensor, not one per room. Two thermometers in a
        room are usually two corners of it, and a room with a sensor at
        the window and another by the door has two honest answers --
        averaging them produces a third that is true nowhere.
        """
        if room_id is None:
            return []
        # The newest of each measurement, not the newest row. A Zigbee
        # thermometer sends temperature and humidity as separate frames
        # minutes apart, so its most recent row carries one of the two
        # and the other read as missing -- a sensor reporting perfectly
        # well, shown with a blank where its temperature should be.
        with self._condition:
            rows = self._database.execute(
                """
                SELECT s.sensor_id, s.display_name,
                       (SELECT r.air_temperature_c FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.air_temperature_c IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.observed_at FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.air_temperature_c IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.air_humidity_percent FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.air_humidity_percent IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.observed_at FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.air_humidity_percent IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1)
                FROM sensors s
                WHERE s.room_id = ? AND s.enrollment_status = 'enrolled'
                  AND COALESCE(s.transport, '') NOT IN (
                      'zigbee-soil', 'zigbee-scale', 'zigbee-plug')
                """,
                (room_id,),
            ).fetchall()
        sources = []
        for sensor_id, name, temperature, temp_at, humidity, humidity_at in rows:
            if temperature is None and humidity is None:
                continue
            sources.append({
                "sensor_id": sensor_id,
                "sensor_name": name or sensor_id,
                # The older of the two, so "measured 3 minutes ago" is
                # not claimed for a pair whose other half is an hour old.
                "observed_at": min(x for x in (temp_at, humidity_at) if x),
                "air_temperature_c": round(temperature, 1) if temperature is not None else None,
                "air_humidity_percent": round(humidity, 1) if humidity is not None else None,
                "drying_kpa": (round(vapour_pressure_deficit(temperature, humidity), 2)
                               if temperature is not None and humidity is not None else None),
            })
        sources.sort(key=lambda source: source["observed_at"], reverse=True)
        return sources

    def plant_soil_sources(self, plant_id: Optional[int]) -> List[Dict[str, Any]]:
        """Every sensor measuring this plant's soil, newest first.

        One pot can have more than one probe in it -- a Bluetooth spike
        and a Zigbee one, or two at different depths -- and they are
        measuring the same soil from different places. Each is its own
        answer, the way two thermometers in a room are.
        """
        if plant_id is None:
            return []
        with self._condition:
            rows = self._database.execute(
                """
                SELECT s.sensor_id, s.display_name,
                       (SELECT r.soil_temperature_c FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.soil_temperature_c IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.observed_at FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.soil_temperature_c IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.moisture_percent FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.moisture_percent IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1),
                       (SELECT r.observed_at FROM sensor_readings r
                         WHERE r.sensor_id = s.sensor_id
                           AND r.moisture_percent IS NOT NULL
                         ORDER BY r.observed_at DESC LIMIT 1)
                FROM sensors s
                WHERE s.plant_id = ? AND s.enrollment_status = 'enrolled'
                """,
                (plant_id,),
            ).fetchall()
        sources = []
        for sensor_id, name, temperature, temp_at, moisture, moisture_at in rows:
            if temperature is None and moisture is None:
                continue
            sources.append({
                "sensor_id": sensor_id,
                "sensor_name": name or sensor_id,
                "observed_at": min(x for x in (temp_at, moisture_at) if x),
                "soil_temperature_c": round(temperature, 1) if temperature is not None else None,
                "moisture_percent": round(moisture, 1) if moisture is not None else None,
            })
        sources.sort(key=lambda source: source["observed_at"], reverse=True)
        return sources

    def room_air(self, room_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """The air of a room, from whichever sensor in it last reported it.

        Air belongs to the room. A probe on one windowsill is measuring the
        same air as the pot beside it, so every plant in the room is told
        what it is in rather than only the one with hardware in its soil.

        Nothing is averaged across sensors: two in one room would usually
        mean two corners of it, and the most recent reading is a truer answer
        than the mean of a warm corner and a cold one. The newest of the
        sources rather than a query of its own, so the headline figure and
        the per-sensor list can never disagree about what was measured.
        """
        for source in self.room_air_sources(room_id):
            if (source["air_temperature_c"] is not None
                    and source["air_humidity_percent"] is not None):
                return source
        return None

    def _room_drying_hours(
        self, room_id: Optional[int], start: datetime, end: datetime
    ) -> Dict[str, float]:
        """The room's drying power, hour by hour, from whatever is in it.

        The air belongs to the room, not to the sensor that happens to
        report it: one probe on a windowsill speaks for every pot on it. A
        plant in a room with no sensor gets nothing, and is left to its raw
        rate rather than a borrowed one.

        Averaged by the hour before the deficit is worked out, because the
        deficit does not average linearly and an hour is short enough for
        that not to matter.
        """
        if room_id is None:
            return {}
        with self._condition:
            rows = self._database.execute(
                """
                SELECT substr(sensor_readings.observed_at, 1, 13) AS hour,
                       AVG(air_temperature_c), AVG(air_humidity_percent)
                FROM sensor_readings
                JOIN sensors ON sensors.sensor_id = sensor_readings.sensor_id
                WHERE sensors.room_id = ?
                  AND sensor_readings.observed_at >= ?
                  AND sensor_readings.observed_at <= ?
                  AND air_temperature_c IS NOT NULL
                  AND air_humidity_percent IS NOT NULL
                GROUP BY hour
                """,
                (room_id, _timestamp(start), _timestamp(end)),
            ).fetchall()
        return {
            row[0]: vapour_pressure_deficit(row[1], row[2])
            for row in rows
            if row[1] is not None and row[2] is not None
        }

    @staticmethod
    def _mean_drying(hours: Dict[str, float], start: datetime, end: datetime):
        """The room's mean drying power across one span, if it was measured.

        Half a span's worth of readings is not a mean for the span, so a
        span the sensor was mostly away for gets nothing rather than a
        figure drawn from the hours it happened to be there.
        """
        wanted = []
        hour = start.replace(minute=0, second=0, microsecond=0)
        while hour <= end:
            wanted.append(_timestamp(hour)[:13])
            hour += timedelta(hours=1)
        found = [hours[key] for key in wanted if key in hours]
        if not wanted or len(found) < max(2, len(wanted) * 0.5):
            return None, round(len(found) / len(wanted), 2) if wanted else 0.0
        return round(sum(found) / len(found), 3), round(len(found) / len(wanted), 2)

    def water_use_findings(self, plant_id: int) -> List[Dict[str, Any]]:
        """What a change in water use suggests, where it suggests anything.

        A plant that stops drinking is saying one of two things, and which
        one depends on whether there is water left to drink.

        With a full pot it is not thirst: the water is there and the plant is
        not taking it. Roots that have drowned or rotted cannot, and neither
        can a plant that has gone dormant in a cold dark week. Watering it
        again is the one response that makes the first case worse, which is
        why this is worth saying out loud.

        With a near-empty pot it is the ordinary thing: the plant has taken
        what it can reach. Soil scientists call the turn from a rate set by
        the air to one set by what is left in the soil the move from
        energy-limited to supply-limited drying, and the weight it happens at
        is this pot's real thirst threshold -- better evidence than anybody's
        guess at one.

        Drinking more is rarely a problem, but it means the next watering
        comes sooner than the last gap suggests.

        None of this is said from one measurement. It takes a baseline of
        three earlier spans, a latest span of a day or more, and a change of
        at least a third; below that it is the weather, the scale, or
        nothing.
        """
        use = self.water_use(plant_id, days=60)
        intervals = use["intervals"]
        if len(intervals) < 4:
            return []
        latest = intervals[-1]
        if latest["days"] < 0.75:
            return []
        def middle_of(values):
            ordered = sorted(values)
            if not ordered:
                return None
            half = len(ordered) // 2
            return (
                ordered[half]
                if len(ordered) % 2
                else (ordered[half - 1] + ordered[half]) / 2
            )

        baseline = middle_of([i["grams_per_day"] for i in intervals[:-1]])
        if not baseline:
            return []
        rate = latest["grams_per_day"]
        share = rate / baseline
        status = self.plant_weight_status(plant_id)
        left = status["remaining_percent"]

        # The same plant drinks two or three times as much in warm dry air
        # as in cool damp air. Where the room was measured, that is divided
        # out before anything is concluded: what is left is the plant's own
        # share, and a change in it is a change in the plant.
        held = middle_of([
            i["grams_per_day_per_kpa"] for i in intervals[:-1]
            if i["grams_per_day_per_kpa"] is not None
        ])
        normalised = latest["grams_per_day_per_kpa"]
        against_air = (normalised / held) if (held and normalised) else None

        evidence = {
            "grams_per_day": rate,
            "baseline_grams_per_day": round(baseline),
            "share_of_usual": round(share, 2),
            "remaining_percent": left,
            "drying_kpa": latest["drying_kpa"],
            "baseline_per_kpa": round(held, 1) if held else None,
            "share_against_air": round(against_air, 2) if against_air else None,
            "from": latest["from"],
            "to": latest["to"],
        }
        shortfall = f"{rate} ml a day against {round(baseline)} usually"

        # A change that would have been worth reporting, and stops being so
        # once the room is divided out, is the weather. Saying that is more
        # use than saying nothing: a fourfold jump in the chart wants an
        # explanation, and "the room got drier" is the explanation.
        #
        # "Stops being so" is deliberately loose. Transpiration does not
        # rise in step with the deficit -- stomata close as the air dries,
        # so the rate per kilopascal sags at the dry end for a perfectly
        # well plant. A change counts as the room's when dividing it out
        # takes it most of the way back and leaves it somewhere unremarkable,
        # not when the two cancel exactly.
        alarming = share <= 0.67 or share >= 1.5
        explained = (
            against_air is not None
            and 0.67 < against_air < 1.5
            and abs(math.log(against_air)) <= 0.5 * abs(math.log(share))
        )
        if alarming and explained:
            drier = "drier" if share > 1 else "damper"
            return [{
                "code": "the_air_changed",
                "title": f"Drinking {'more' if share > 1 else 'less'}, but so is the room",
                "summary": (
                    f"{shortfall}, in air that is {drier} by about as much "
                    f"({latest['drying_kpa']} kPa of drying power now). Held against "
                    "the air the plant is drinking as it was, so this is the room "
                    "changing rather than the plant."
                ),
                "evidence": evidence,
            }]
        # Where the air is known, the plant's own share is what is judged.
        if against_air is not None:
            share = against_air

        # Nearly empty and still full are judged against this plant's
        # own thirsty mark, not a fixed percentage: "water left" is
        # measured from the pot's dry weight, and the mark falls at a
        # different point on that scale for every pot -- 17 % for one
        # watered early, 30 % for another.
        mark = status["thirsty_percent"]
        # Bands within the range this plant uses, not fixed points.
        # "At or below the mark" is too strict to warn on: by then the
        # pot is not nearly empty, it is empty.
        nearly_empty = (mark + (100 - mark) * 0.25) if mark is not None else 30
        still_full = (mark + (100 - mark) * 0.5) if mark is not None else 50
        if share <= 0.67:
            if left is not None and left <= nearly_empty:
                return [{
                    "code": "drinking_less_and_low",
                    "title": "Drinking less, and nearly empty",
                    "summary": (
                        f"{shortfall}, with about {left}% of its water left. The pot "
                        "is down to what the roots cannot easily reach, which is "
                        "this plant telling you when it is thirsty rather than you "
                        "deciding for it."
                    ),
                    "evidence": evidence,
                }]
            if left is not None and left >= still_full:
                return [{
                    "code": "drinking_less_while_full",
                    "title": "Drinking less with water still in the pot",
                    "summary": (
                        f"{shortfall}, with about {left}% of its water left. Water "
                        "it is not taking is worth looking into before adding more: "
                        "roots that are drowned or rotting cannot drink, and nor can "
                        "a plant that has gone dormant in a cold or dark week."
                    ),
                    "evidence": evidence,
                }]
            return [{
                "code": "drinking_less",
                "title": "Drinking less than usual",
                "summary": (
                    f"{shortfall}. Cooler, darker or damper air will do this, and so "
                    "will a plant in trouble; a weighing or two more will say which."
                ),
                "evidence": evidence,
            }]

        if share >= 1.5:
            return [{
                "code": "drinking_more",
                "title": "Drinking more than usual",
                "summary": (
                    f"{rate} ml a day against {round(baseline)} usually. Warmth, "
                    "light, dry air and new growth all raise it. The next watering "
                    "will come sooner than the last gap suggests."
                ),
                "evidence": evidence,
            }]
        return []

    def record_water_use_findings(self, plant_id: int) -> List[Dict[str, Any]]:
        """Put what the water use implies into the plant's care log.

        One of each finding a day: saying the same thing twice on a Tuesday
        adds nothing, but a plant that was drinking less this morning and
        more this evening has changed its mind, and that is worth the second
        line.
        """
        findings = self.water_use_findings(plant_id)
        now = _timestamp(datetime.now(timezone.utc))
        with self._condition, self._database:
            for finding in findings:
                self._save_event(
                    CareEvent(
                        event_id=(
                            f"observation-{finding['code']}-plant-{plant_id}"
                            f"-{self._local_day(now)}"
                        ),
                        sensor_id="",
                        kind="observation",
                        detected_at=now,
                        title=finding["title"],
                        summary=finding["summary"],
                        confidence="derived",
                        changes={"source": "water-use", **finding["evidence"]},
                    ),
                    plant_id=plant_id,
                )
        return findings

    def _last_care_at(self, plant_id: int, kind: str) -> Optional[str]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT MAX(detected_at) FROM care_events
                WHERE plant_id = ? AND kind = ?
                """,
                (plant_id, kind),
            ).fetchone()
        return row[0] if row else None

    def watering_forecast(self, plant_id: int, with_air: bool = True) -> Dict[str, Any]:
        """When the pot will reach the weight at which this plant wants water.

        Three things are needed and the hub has no business guessing any of
        them: what the pot weighs now, what it weighs when the plant is
        thirsty, and how fast it is getting from one to the other. Where one
        is missing it is named, with what to do about it, because "no
        prediction" and "no prediction yet, and here is why" are different
        things to be told.

        The rate is taken from this drying cycle where there is one -- how
        fast it has drunk since it was last watered, which is the same
        plant in the same pot in the same week -- and from the whole window
        otherwise. The spread between the fastest and slowest spans gives
        the soonest and latest, because a single number pretends to a
        precision that four weighings do not have.
        """
        status = self.plant_weight_status(plant_id)
        use = self.water_use(plant_id, with_air=with_air)
        missing: List[Dict[str, str]] = []

        if status["latest"] is None:
            missing.append({
                "code": "no_weighing",
                "what": "What the pot weighs now",
                "how": "Put it on a scale and press Record.",
            })
        if status["thirsty"] is None:
            missing.append({
                "code": "no_thirsty_weight",
                "what": "What the pot weighs when this plant wants water",
                "how": "Set Thirsty at: weigh it on a day it is ready for water, "
                       "or type the weight if you know it.",
            })

        # Spans since the last watering: the same plant in the same pot in
        # the same weather, which is a better guide than a quarter's average.
        watered_at = status["watered"]["at"] if status["watered"] else None
        cycle = [i for i in use["intervals"] if watered_at is None or i["from"] >= watered_at]
        spans = cycle if cycle else use["intervals"]
        if not spans:
            missing.append({
                "code": "no_rate",
                "what": "How fast it is drinking",
                "how": "Weigh it again a day or more after the last weighing. "
                       "Two weighings far enough apart are what a rate is.",
            })

        # A repot changes what the pot weighs full and what it weighs
        # thirsty: same plant, different thing on the scale. Anchors set
        # before the last one are about a pot that no longer exists.
        cautions: List[Dict[str, str]] = []
        repotted = self._last_care_at(plant_id, "repotting")
        if repotted is not None:
            stale = [
                name for name, anchor in (("Full at", status["watered"]),
                                          ("Thirsty at", status["thirsty"]))
                if anchor is not None and anchor["at"] < repotted
            ]
            if stale:
                cautions.append({
                    "code": "anchors_predate_repot",
                    "what": f"{' and '.join(stale)} {'were' if len(stale) > 1 else 'was'} "
                            "set before this plant was repotted",
                    "how": "New soil weighs what it weighs. Weigh the pot after its "
                           "next watering and press Just watered to set the full "
                           "weight again.",
                })

        forecast: Dict[str, Any] = {
            "ready": False,
            "missing": missing,
            "cautions": cautions,
            "days": None,
            "days_soonest": None,
            "days_latest": None,
            "water_on": None,
            "grams_now": status["latest"]["grams"] if status["latest"] else None,
            "thirsty_grams": status["thirsty"]["grams"] if status["thirsty"] else None,
            "grams_to_go": None,
            "grams_per_day": None,
            "rate_from": None,
            "spans_used": len(spans),
            "band_spans_used": 0,
            "thirsty_now": False,
        }
        if missing:
            return forecast

        to_go = status["latest"]["grams"] - status["thirsty"]["grams"]
        rates = [i["grams_per_day"] for i in spans if i["grams_per_day"] > 0]
        if not rates:
            # It has been weighed often enough, and has not lost anything.
            forecast["missing"] = [{
                "code": "not_drinking",
                "what": "How fast it is drinking",
                "how": "The pot has not lost weight since it was last weighed. "
                       "Another weighing in a day or two will say whether it is "
                       "drinking at all.",
            }]
            return forecast

        typical = sum(rates) / len(rates)
        forecast.update({
            "ready": True,
            "grams_to_go": to_go,
            "grams_per_day": round(typical),
            "rate_from": "this cycle" if cycle else f"the last {use['window_days']} days",
        })
        if to_go <= 0:
            # Already at or past the weight it asks for water at. Nothing is
            # "minus fifty millilitres to go"; it is thirsty now.
            forecast.update({"grams_to_go": 0, "thirsty_now": True, "days": 0.0,
                             "days_soonest": 0.0, "days_latest": 0.0,
                             "water_on": _timestamp(datetime.now(timezone.utc))})
            return forecast

        now = datetime.now(timezone.utc)
        days = to_go / typical

        # How fast it drinks is read from this cycle, which is the same plant
        # in the same pot in the same weather. How much that varies cannot be:
        # a cycle one weighing old has a rate and no spread, and a band drawn
        # from it would say 2.3 to 2.3 days, which is a certainty nobody has.
        # So the band is every span in the window -- the fastest and slowest
        # it has drunk, which is what the chart has always claimed to show --
        # with this cycle's rate among them, so the estimate is always inside
        # its own band.
        window_rates = [
            span["grams_per_day"] for span in use["intervals"]
            if span["grams_per_day"] > 0
        ]
        band = (window_rates or rates) + [typical]
        forecast.update({
            "days": round(days, 1),
            # Fastest it has drunk brings the day forward; slowest pushes it out.
            "days_soonest": round(to_go / max(band), 1),
            "days_latest": round(to_go / min(band), 1),
            "band_spans_used": len(band) - 1,
            "water_on": _timestamp(now + timedelta(days=days)),
        })
        return forecast

    def plants(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """Every plant the household has, instrumented or not."""
        with self._condition:
            rows = self._database.execute(
                f"""
                SELECT plants.plant_id, plants.display_name, plants.profile_id,
                       plants.room_id, rooms.name, plants.archived, plants.created_at,
                       plants.expected_watering_days,
                       (SELECT sensor_id FROM sensors
                        WHERE sensors.plant_id = plants.plant_id
                          AND sensors.enrollment_status = 'enrolled'
                        ORDER BY sensor_id LIMIT 1),
                       (SELECT MAX(detected_at) FROM care_events
                        WHERE care_events.plant_id = plants.plant_id
                          AND kind = 'watering'),
                       plants.variety,
                       plants.acquired_at,
                       plants.cover_photo_id,
                       (SELECT digest FROM plant_photos
                        WHERE plant_photos.photo_id = plants.cover_photo_id),
                       (SELECT MAX(detected_at) FROM care_events
                        WHERE care_events.plant_id = plants.plant_id
                          AND kind = 'fertilizing'),
                       plants.cover_follows_latest,
                       plants.purchase_price_cents,
                       plants.purchase_place,
                       plants.ppfd_umol_m2s, plants.light_hours_per_day,
                       plants.started_at,
                       plants.weight_offset_grams
                FROM plants
                LEFT JOIN rooms ON rooms.room_id = plants.room_id
                {"" if include_archived else "WHERE plants.archived = 0"}
                ORDER BY plants.display_name COLLATE NOCASE
                """
            ).fetchall()
        # The household list says how much water each pot has left, which only
        # its weighings know. One small indexed query per plant, and a house
        # has tens of plants, not thousands.
        return [
            {
                **self._plant_payload(row),
                "weight": self.plant_weight_status(row[0]),
                # The mean rate only, over the same window the plant's own
                # page uses, so the two cannot disagree. Without the air,
                # which costs more than everything else here together and
                # changes nothing the card shows.
                "daily_grams": self.water_use(row[0], with_air=False)["daily_grams"],
                "water_in_days": self.watering_forecast(row[0], with_air=False)["days"],
            }
            for row in rows
        ]

    def plant(self, plant_id: int) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT plants.plant_id, plants.display_name, plants.profile_id,
                       plants.room_id, rooms.name, plants.archived, plants.created_at,
                       plants.expected_watering_days,
                       (SELECT sensor_id FROM sensors
                        WHERE sensors.plant_id = plants.plant_id
                          AND sensors.enrollment_status = 'enrolled'
                        ORDER BY sensor_id LIMIT 1),
                       (SELECT MAX(detected_at) FROM care_events
                        WHERE care_events.plant_id = plants.plant_id
                          AND kind = 'watering'),
                       plants.variety,
                       plants.acquired_at,
                       plants.cover_photo_id,
                       (SELECT digest FROM plant_photos
                        WHERE plant_photos.photo_id = plants.cover_photo_id),
                       (SELECT MAX(detected_at) FROM care_events
                        WHERE care_events.plant_id = plants.plant_id
                          AND kind = 'fertilizing'),
                       plants.cover_follows_latest,
                       plants.purchase_price_cents,
                       plants.purchase_place,
                       plants.ppfd_umol_m2s, plants.light_hours_per_day,
                       plants.started_at,
                       plants.weight_offset_grams
                FROM plants
                LEFT JOIN rooms ON rooms.room_id = plants.room_id
                WHERE plants.plant_id = ?
                """,
                (plant_id,),
            ).fetchone()
        return None if row is None else self._plant_payload(row)

    # Photographs. The bytes are the photo library's business; these are the
    # rows that say what each one is a picture of.

    def add_plant_photo(
        self,
        plant_id: int,
        digest: str,
        content_type: str,
        size: int,
        added_at: str,
        caption: Optional[str] = None,
        taken_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        taken_at = self._validate_taken_at(taken_at)
        with self._condition, self._database:
            cursor = self._database.execute(
                """
                INSERT INTO plant_photos
                    (plant_id, digest, content_type, bytes, added_at, caption, taken_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (plant_id, digest, content_type, size, added_at,
                 (caption or "").strip() or None, taken_at),
            )
            photo_id = int(cursor.lastrowid)
            # The first photograph of a plant is its cover, because a plant
            # with one picture and no cover is a question nobody wants asked.
            self._database.execute(
                """
                UPDATE plants SET cover_photo_id = ?
                WHERE plant_id = ? AND cover_photo_id IS NULL
                """,
                (photo_id, plant_id),
            )
            self._follow_latest_cover(plant_id)
        photo = self.plant_photo(photo_id)
        assert photo is not None
        return photo

    @staticmethod
    def _validate_taken_at(taken_at: Optional[str]) -> Optional[str]:
        """When the camera says the picture was taken.

        A camera with a flat clock, or one never set, reports 1970 or a date
        in the future. Neither is worth keeping over knowing nothing: a
        photograph with no date falls back to when it was uploaded, which is
        at least true of something.
        """
        if not taken_at:
            return None
        try:
            when = datetime.fromisoformat(str(taken_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if when.year < 1990 or when > now + timedelta(days=1):
            return None
        return _timestamp(when)

    def plant_photos(self, plant_id: int) -> List[Dict[str, Any]]:
        with self._condition:
            rows = self._database.execute(
                """
                SELECT photo_id, plant_id, digest, content_type, bytes, added_at, caption,
                       (SELECT cover_photo_id FROM plants WHERE plants.plant_id = ?),
                       taken_at
                FROM plant_photos WHERE plant_id = ?
                ORDER BY COALESCE(taken_at, added_at) DESC, photo_id DESC
                """,
                (plant_id, plant_id),
            ).fetchall()
        return [self._photo_payload(row) for row in rows]

    def plant_photo(self, photo_id: int) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT photo_id, plant_photos.plant_id, digest, content_type, bytes,
                       added_at, caption, plants.cover_photo_id, taken_at
                FROM plant_photos
                JOIN plants ON plants.plant_id = plant_photos.plant_id
                WHERE photo_id = ?
                """,
                (photo_id,),
            ).fetchone()
        return None if row is None else self._photo_payload(row)

    @staticmethod
    def _photo_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "photo_id": row[0],
            "plant_id": row[1],
            "digest": row[2],
            "content_type": row[3],
            "bytes": row[4],
            "added_at": row[5],
            "caption": row[6],
            "cover": row[0] == row[7],
            # When the camera says it was taken, where it said anything.
            "taken_at": row[8],
        }

    def set_photo_taken_at(self, photo_id: int, taken_at: Optional[str]) -> Dict[str, Any]:
        """Say when a photograph was taken, or unsay it.

        A camera that gave no date, a picture scanned years later, a file
        whose metadata was stripped before it ever reached here: the only
        source left is somebody who remembers, so they are allowed to say.
        """
        if self.plant_photo(photo_id) is None:
            raise ValueError("no such photograph")
        if taken_at:
            checked = self._validate_taken_at(taken_at)
            if checked is None:
                raise ValueError("that is not a date a photograph was taken")
        else:
            checked = None
        with self._condition, self._database:
            self._database.execute(
                "UPDATE plant_photos SET taken_at = ? WHERE photo_id = ?",
                (checked, photo_id),
            )
            self._follow_latest_cover(int(self.plant_photo(photo_id)["plant_id"]))
        photo = self.plant_photo(photo_id)
        assert photo is not None
        return photo

    def photo_digest_in_use(self, digest: str) -> bool:
        """Whether any row still refers to that file."""
        with self._condition:
            row = self._database.execute(
                "SELECT 1 FROM plant_photos WHERE digest = ? LIMIT 1", (digest,)
            ).fetchone()
        return row is not None

    def delete_plant_photo(self, photo_id: int) -> bool:
        with self._condition, self._database:
            # A plant whose cover is removed falls back to its newest
            # remaining photograph rather than to nothing.
            row = self._database.execute(
                """
                SELECT plant_photos.plant_id, plants.cover_photo_id = photo_id
                FROM plant_photos
                JOIN plants ON plants.plant_id = plant_photos.plant_id
                WHERE photo_id = ?
                """,
                (photo_id,),
            ).fetchone()
            if row is None:
                return False
            plant_id, was_cover = row[0], bool(row[1])
            self._database.execute("DELETE FROM plant_photos WHERE photo_id = ?", (photo_id,))
            # Asked before the delete, because the foreign key clears the
            # plant's cover on its way out and there would be nothing left to
            # recognise.
            if was_cover:
                self._database.execute(
                    """
                    UPDATE plants SET cover_photo_id = (
                        SELECT photo_id FROM plant_photos WHERE plant_id = ?
                        ORDER BY added_at DESC, photo_id DESC LIMIT 1
                    )
                    WHERE plant_id = ?
                    """,
                    (plant_id, plant_id),
                )
            self._follow_latest_cover(plant_id)
        return True

    def _newest_photo_id(self, plant_id: int) -> Optional[int]:
        """The plant's most recent photograph, by when it was taken."""
        row = self._database.execute(
            """
            SELECT photo_id FROM plant_photos WHERE plant_id = ?
            ORDER BY COALESCE(taken_at, added_at) DESC, photo_id DESC LIMIT 1
            """,
            (plant_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def _follow_latest_cover(self, plant_id: int) -> None:
        """Put the newest photograph on the cover, where that was asked for.

        Called wherever "newest" can have changed: a photograph arriving,
        one being struck, or one being told a different date -- a picture
        from last summer uploaded today is not the newest, whatever order
        it came in.
        """
        following = self._database.execute(
            "SELECT cover_follows_latest FROM plants WHERE plant_id = ?", (plant_id,)
        ).fetchone()
        if following is None or not following[0]:
            return
        self._database.execute(
            "UPDATE plants SET cover_photo_id = ? WHERE plant_id = ?",
            (self._newest_photo_id(plant_id), plant_id),
        )

    def set_cover_follows_latest(self, plant_id: int, following: bool) -> Dict[str, Any]:
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE plants SET cover_follows_latest = ? WHERE plant_id = ?",
                (1 if following else 0, plant_id),
            )
            self._follow_latest_cover(plant_id)
        plant = self.plant(plant_id)
        assert plant is not None
        return plant

    def set_cover_photo(self, plant_id: int, photo_id: Optional[int]) -> Dict[str, Any]:
        """Choose the one picture that stands for the plant.

        Choosing one by hand is a decision, and it turns off following the
        newest: the next photograph must not quietly overrule it.
        """
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        if photo_id is not None:
            photo = self.plant_photo(photo_id)
            if photo is None or photo["plant_id"] != plant_id:
                raise ValueError("that photograph is not this plant's")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE plants SET cover_photo_id = ?, cover_follows_latest = 0
                WHERE plant_id = ?
                """,
                (photo_id, plant_id),
            )
        plant = self.plant(plant_id)
        assert plant is not None
        return plant

    @staticmethod
    def _validate_acquired_at(acquired_at: Optional[str]) -> Optional[str]:
        """The day the plant came home. Not tomorrow, and not before plants."""
        if not acquired_at:
            return None
        try:
            when = datetime.fromisoformat(str(acquired_at).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("at-home date must be a date") from error
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
        if when > datetime.now(timezone.utc) + timedelta(days=1):
            raise ValueError("a plant cannot have come home tomorrow")
        if when.year < 1900:
            raise ValueError("that is not a date a houseplant came home")
        return _timestamp(when)

    @staticmethod
    def _validate_purchase_price_cents(price: Optional[Any]) -> Optional[int]:
        """What it cost, in whole cents. Free is a price; negative is not."""
        if price in (None, ""):
            return None
        try:
            cents = int(round(float(price)))
        except (TypeError, ValueError) as error:
            raise ValueError("a purchase price must be a number") from error
        if cents < 0:
            raise ValueError("a plant cannot have cost less than nothing")
        if cents > 100_000_000:
            raise ValueError("that is not what a houseplant costs")
        return cents

    @staticmethod
    def _validate_purchase_place(place: Optional[str]) -> Optional[str]:
        """Where it came from, as the household would say it aloud."""
        if place in (None, ""):
            return None
        place = str(place).strip()
        if not place:
            return None
        if len(place) > 120:
            raise ValueError("a place name is at most 120 characters")
        return place

    def _validate_plant_profile(self, profile_id: Optional[str]) -> Optional[str]:
        """A plant may have no profile; it may not have one that does not exist."""
        if profile_id in (None, ""):
            return None
        if profile_id not in load_plant_profiles()["profiles"]:
            raise ValueError("profile_id is invalid")
        return profile_id

    @staticmethod
    def _validate_plant_variety(
        profile_id: Optional[str], variety: Optional[str]
    ) -> Optional[str]:
        """A variety only means anything within the profile that lists it.

        Marble Queen is a pothos; it is not a bird of paradise. Changing the
        profile therefore drops a variety the new one does not know, rather
        than keeping a label that no longer refers to anything.
        """
        if variety in (None, ""):
            return None
        profile = load_plant_profiles()["profiles"].get(profile_id or "")
        if profile is None:
            raise ValueError("a variety needs a profile to belong to")
        if variety not in {kind["id"] for kind in profile.get("varieties", [])}:
            raise ValueError(f"{profile['name']} has no variety {variety}")
        return variety

    def create_plant(
        self,
        display_name: str,
        room_id: Optional[int] = None,
        expected_watering_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Take on a plant the hub has no sensor for."""
        display_name = self._validate_plant_name(display_name)
        room_id = self._validate_plant_room(room_id)
        expected_watering_days = self._validate_watering_days(expected_watering_days)
        with self._condition, self._database:
            cursor = self._database.execute(
                """
                INSERT INTO plants (display_name, room_id, expected_watering_days,
                                    created_at, archived)
                VALUES (?, ?, ?, ?, 0)
                """,
                (
                    display_name,
                    room_id,
                    expected_watering_days,
                    _timestamp(datetime.now(timezone.utc)),
                ),
            )
            plant_id = int(cursor.lastrowid)
        plant = self.plant(plant_id)
        assert plant is not None
        return plant

    def update_plant(
        self,
        plant_id: int,
        display_name: str,
        room_id: Optional[int] = None,
        expected_watering_days: Optional[int] = None,
        profile_id: Optional[str] = None,
        variety: Optional[str] = None,
        acquired_at: Optional[str] = None,
        purchase_price_cents: Optional[int] = None,
        purchase_place: Optional[str] = None,
        ppfd_umol_m2s: Optional[float] = None,
        light_hours_per_day: Optional[float] = None,
        started_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        display_name = self._validate_plant_name(display_name)
        room_id = self._validate_plant_room(room_id)
        expected_watering_days = self._validate_watering_days(expected_watering_days)
        profile_id = self._validate_plant_profile(profile_id)
        variety = self._validate_plant_variety(profile_id, variety)
        acquired_at = self._validate_acquired_at(acquired_at)
        started_at = self._validate_acquired_at(started_at)
        purchase_price_cents = self._validate_purchase_price_cents(purchase_price_cents)
        purchase_place = self._validate_purchase_place(purchase_place)
        if ppfd_umol_m2s is not None and not 0 <= ppfd_umol_m2s <= 5000:
            # Full summer sun is about 2000; past five thousand is a
            # typing mistake rather than a growing condition.
            raise ValueError("light reaching a plant must be between 0 and 5000 umol/m2/s")
        if light_hours_per_day is not None and not 0 <= light_hours_per_day <= 24:
            # A day has twenty-four hours whatever the lamp is doing.
            raise ValueError("hours of light a day must be between 0 and 24")
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE plants SET display_name = ?, room_id = ?, expected_watering_days = ?,
                                  profile_id = ?, variety = ?, acquired_at = ?,
                                  purchase_price_cents = ?, purchase_place = ?,
                                  ppfd_umol_m2s = ?, light_hours_per_day = ?,
                                  started_at = ?
                WHERE plant_id = ?
                """,
                (display_name, room_id, expected_watering_days, profile_id, variety,
                 acquired_at, purchase_price_cents, purchase_place, ppfd_umol_m2s,
                 light_hours_per_day, started_at, plant_id),
            )
            # A sensor watching it shows the same name, profile and room, or
            # its pages and the plant's disagree about the same plant.
            self._database.execute(
                """
                UPDATE sensors SET display_name = ?, room_id = ?,
                                   profile_id = COALESCE(?, profile_id)
                WHERE plant_id = ?
                """,
                (display_name, room_id, profile_id, plant_id),
            )
        plant = self.plant(plant_id)
        assert plant is not None
        return plant

    def set_plant_archived(self, plant_id: int, archived: bool) -> Dict[str, Any]:
        if self.plant(plant_id) is None:
            raise ValueError("unknown plant")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE plants SET archived = ? WHERE plant_id = ?",
                (1 if archived else 0, plant_id),
            )
        plant = self.plant(plant_id)
        assert plant is not None
        return plant

    def delete_plant(self, plant_id: int) -> None:
        """Forget a plant and everything recorded about it.

        Only a plant with no sensor: one with a sensor is forgotten by letting
        the sensor go, which has its own consequences for the device.
        """
        plant = self.plant(plant_id)
        if plant is None:
            raise ValueError("unknown plant")
        if plant["sensor_id"] is not None:
            raise ValueError("this plant has a sensor; forget the sensor instead")
        with self._condition, self._database:
            self._database.execute("DELETE FROM care_events WHERE plant_id = ?", (plant_id,))
            self._database.execute("DELETE FROM plant_journey WHERE plant_id = ?", (plant_id,))
            self._database.execute("DELETE FROM plants WHERE plant_id = ?", (plant_id,))

    def _validate_plant_name(self, display_name: str) -> str:
        display_name = (display_name or "").strip()
        self._validate_device_text(display_name, "display_name", required=True)
        return display_name

    def _validate_plant_room(self, room_id: Optional[int]) -> Optional[int]:
        if room_id in (None, ""):
            return None
        room_id = int(room_id)
        if self._database.execute(
            "SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone() is None:
            raise ValueError("unknown room")
        return room_id

    @staticmethod
    def _validate_watering_days(days: Optional[int]) -> Optional[int]:
        if days in (None, ""):
            return None
        days = int(days)
        if not 1 <= days <= 365:
            raise ValueError("expected_watering_days must be between 1 and 365")
        return days

    @staticmethod
    def _plant_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "plant_id": row[0],
            "display_name": row[1],
            "profile_id": row[2],
            "room_id": row[3],
            "room": row[4],
            "archived": bool(row[5]),
            "created_at": row[6],
            "expected_watering_days": row[7],
            # The sensor watching it, if one is. This is what tells the two
            # kinds of plant apart everywhere else.
            "sensor_id": row[8],
            "last_watered_at": row[9],
            # Which kind of the kind: the profile is the species, the variety
            # the named plant within it.
            "variety": row[10],
            # When it came home, as against when the hub first heard of it.
            "acquired_at": row[11],
            "cover_photo_id": row[12],
            # The cover's digest, so the card can ask for it by content. An
            # id is reused once its photograph is deleted; what the bytes are
            # is not.
            "cover_photo_digest": row[13],
            # When it was last fed, which is the other thing a plant is
            # given and the one nobody remembers.
            "last_fed_at": row[14],
            "cover_follows_latest": bool(row[15]),
            # What it cost, in whole cents, and the shop. Together with
            # acquired_at these are the receipt: when, where, how much.
            "purchase_price_cents": row[16],
            "purchase_place": row[17],
            # How much light reaches it where it stands, measured at the
            # leaf. A property of the spot, not of any one lamp.
            "ppfd_umol_m2s": row[18],
            # Hours typed by hand, for a plant lit by something the hub
            # does not switch. A standing assumption, not an observation.
            "light_hours_per_day": row[19],
            # When the plant began, not when it arrived. What its signs
            # mean depends on how old it is.
            "started_at": row[20],
            # What else sits on the scale with it: a probe, a cache
            # pot, a saucer. Taken off a weighing, never off a typed
            # anchor.
            "weight_offset_grams": int(row[21] or 0),
        }

    def rebuild_journey(self, sensor_id: str) -> Dict[str, Any]:
        plant_id = self._plant_id_for(sensor_id)
        if plant_id is None:
            return dict(EMPTY_JOURNEY)
        return self.rebuild_journey_of(plant_id)

    def rebuild_journey_of(self, plant_id: int) -> Dict[str, Any]:
        """Count a plant's journey again from its readings and care events.

        The stored row is a cache, never a second truth: anything it holds can
        be worked out again from what it was derived from. This is what the
        migration runs, and what to run if a figure is ever doubted.
        """
        with self._condition, self._database:
            self._rebuild_journey(plant_id)
        return self.plant_journey_of(plant_id)

    def _rebuild_journey(self, plant_id: int) -> Tuple[Any, ...]:
        # Across every sensor the plant has had, so replacing one does not
        # shorten the life of the plant it was watching.
        reading_range = self._database.execute(
            """
            SELECT MIN(COALESCE(observed_at, received_at)),
                   MAX(COALESCE(observed_at, received_at))
            FROM sensor_readings
            WHERE sensor_id IN (SELECT sensor_id FROM sensors WHERE plant_id = ?)
            """,
            (plant_id,),
        ).fetchone()
        events = self._database.execute(
            """
            SELECT kind, detected_at, changes_json FROM care_events
            WHERE plant_id = ? AND kind IN ('watering', 'watering_due', 'fertilizing', 'lighting')
            /* Written order breaks ties: timestamps have one-second
               resolution, and a lamp switched on and off inside a second
               must be folded in the order it happened, not whichever order
               an index happens to return. */
            ORDER BY detected_at, rowid
            """,
            (plant_id,),
        ).fetchall()

        waterings = fertilizings = settled_missed = 0
        open_due: List[datetime] = []
        lit: Dict[str, Any] = {}
        lighting_seconds = 0
        now = datetime.now(timezone.utc)
        for kind, detected_at, changes_json in events:
            at = self._observation_datetime(detected_at, now)
            if kind == "fertilizing":
                fertilizings += 1
            elif kind == "watering_due":
                open_due.append(at)
            elif kind == "watering":
                waterings += 1
                # Every window still open is answered by this watering, late or
                # in time, and is never revisited afterwards.
                settled_missed += sum(1 for due_at in open_due if at > due_at + MISSED_WATERING_GRACE)
                open_due = []
            elif kind == "lighting":
                lighting_seconds += self._apply_lighting_event(changes_json, at, lit)

        # A stretch still running is carried, not counted: it earns its hours
        # when it closes, like every other one.
        row = (
            reading_range[0],
            reading_range[1],
            waterings,
            fertilizings,
            settled_missed,
            lighting_seconds,
            json.dumps(lit),
        )
        self._database.execute(
            """
            INSERT INTO plant_journey (
                plant_id, first_reading_at, last_reading_at, watering_count,
                fertilizing_count, settled_missed_count, lighting_seconds,
                lighting_open_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plant_id) DO UPDATE SET
                first_reading_at = excluded.first_reading_at,
                last_reading_at = excluded.last_reading_at,
                watering_count = excluded.watering_count,
                fertilizing_count = excluded.fertilizing_count,
                settled_missed_count = excluded.settled_missed_count,
                lighting_seconds = excluded.lighting_seconds,
                lighting_open_json = excluded.lighting_open_json,
                updated_at = excluded.updated_at
            """,
            (plant_id,) + row + (_timestamp(now),),
        )
        return row

    @staticmethod
    def _apply_lighting_event(
        changes_json: str, at: datetime, lit: Dict[str, Any]
    ) -> int:
        """Fold one lighting event into what is lit, and return seconds earned.

        A stretch of light runs from the first lamp coming on to the last one
        going off, so two lamps over one plant are one stretch: this is how long
        the plant was lit, not how long lamps ran. Only a stretch that closes
        counts, and only if it is a believable length — a close that never
        arrived would otherwise be paid for at the next one, hours or days late.
        """
        try:
            changes = json.loads(changes_json)
        except json.JSONDecodeError:
            return 0
        # Only what a light confirmed: being told to come on is not being on.
        if changes.get("outcome") != "confirmed":
            return 0
        light_id = str(changes.get("light_id", ""))
        if not light_id:
            return 0
        lights = set(lit.get("lights") or ())
        if changes.get("wanted"):
            if not lights:
                lit["since"] = _timestamp(at)
            lights.add(light_id)
            lit["lights"] = sorted(lights)
            return 0
        if light_id not in lights:
            # Switched off something that was never recorded as on; there is no
            # stretch to close and nothing honest to add.
            return 0
        lights.discard(light_id)
        if lights:
            # Another lamp is still on, so the plant is still lit.
            lit["lights"] = sorted(lights)
            return 0
        since = lit.get("since")
        # Nothing is lit, so nothing is carried: the column says so plainly.
        lit.clear()
        if since is None:
            return 0
        seconds = (at - ReadingStore._parse_timestamp(since, at)).total_seconds()
        if seconds <= 0 or seconds > MAX_LIGHTING_STRETCH.total_seconds():
            # Longer than any day's lighting: a close went missing somewhere in
            # between, and the hours since are not hours of light.
            return 0
        return int(seconds)

    @staticmethod
    def _parse_timestamp(value: str, fallback: datetime) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback

    def lighting_hours(self, sensor_id: str) -> int:
        """How long this plant has had a light on, over its whole life."""
        return int(self.plant_journey(sensor_id)["lighting_hours"])

    def _journey_apply_event(
        self, plant_id: int, kind: str, detected_at: str, changes_json: str
    ) -> None:
        """Fold one care event into the plant's kept figures as it happens.

        The alternative is counting the whole history on every poll, which is a
        page that grows slower for as long as a plant is looked after.
        """
        if kind not in ("watering", "watering_due", "fertilizing", "lighting"):
            return
        row = self._database.execute(
            """
            SELECT settled_missed_count, lighting_seconds, lighting_open_json
            FROM plant_journey WHERE plant_id = ?
            """,
            (plant_id,),
        ).fetchone()
        if row is None:
            # Nothing kept for this plant yet, so there is nothing to add to:
            # counting it from scratch is both the fix and the update.
            self._rebuild_journey(plant_id)
            return
        settled_missed, lit_seconds, open_json = row
        now = datetime.now(timezone.utc)
        at = self._observation_datetime(detected_at, now)

        if kind == "fertilizing":
            self._database.execute(
                "UPDATE plant_journey SET fertilizing_count = fertilizing_count + 1, "
                "updated_at = ? WHERE plant_id = ?",
                (_timestamp(now), plant_id),
            )
            return
        if kind == "watering_due":
            # Nothing is settled by a window opening. Whether it was missed is
            # the clock's answer, and the reader asks it.
            return
        if kind == "watering":
            settled = self._open_missed_windows_before(plant_id, at)
            self._database.execute(
                "UPDATE plant_journey SET watering_count = watering_count + 1, "
                "settled_missed_count = settled_missed_count + ?, updated_at = ? "
                "WHERE plant_id = ?",
                (settled, _timestamp(now), plant_id),
            )
            return

        try:
            lit = json.loads(open_json or "{}")
        except json.JSONDecodeError:
            lit = {}
        earned = self._apply_lighting_event(changes_json, at, lit)
        self._database.execute(
            """
            UPDATE plant_journey
            SET lighting_seconds = ?, lighting_open_json = ?, updated_at = ?
            WHERE plant_id = ?
            """,
            (lit_seconds + earned, json.dumps(lit), _timestamp(now), plant_id),
        )

    def _open_missed_windows_before(self, plant_id: int, watered_at: datetime) -> int:
        """Windows this watering answers, and how many of them it answers late."""
        rows = self._database.execute(
            """
            SELECT detected_at FROM care_events
            WHERE plant_id = ? AND kind = 'watering_due'
              AND detected_at > COALESCE(
                  (SELECT MAX(detected_at) FROM care_events
                   WHERE plant_id = ? AND kind = 'watering' AND detected_at < ?), '')
              AND detected_at < ?
            """,
            (plant_id, plant_id, _timestamp(watered_at), _timestamp(watered_at)),
        ).fetchall()
        return sum(
            1
            for (due_at,) in rows
            if watered_at > self._observation_datetime(due_at, watered_at) + MISSED_WATERING_GRACE
        )

    def _journey_note_reading(self, sensor_id: str, observed_at: str, received_at: str) -> None:
        plant_id = self._plant_id_for(sensor_id)
        if plant_id is None:
            # Readings from a sensor nobody has claimed are kept; the figures
            # they feed start when a plant does.
            return
        """Keep the first and last time this plant reported.

        Taken from each reading as it lands, so the page never scans a lifetime
        of readings to draw one number.
        """
        at = observed_at or received_at
        self._database.execute(
            """
            INSERT INTO plant_journey (plant_id, first_reading_at, last_reading_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plant_id) DO UPDATE SET
                first_reading_at = MIN(COALESCE(plant_journey.first_reading_at, excluded.first_reading_at),
                                       excluded.first_reading_at),
                last_reading_at = MAX(COALESCE(plant_journey.last_reading_at, excluded.last_reading_at),
                                      excluded.last_reading_at),
                updated_at = excluded.updated_at
            """,
            (plant_id, at, at, _timestamp(datetime.now(timezone.utc))),
        )

    def wait_for_reading(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: bool(self._history), timeout=timeout)

    def close(self) -> None:
        with self._condition:
            self._database.close()

    def _save_event(self, event: CareEvent, plant_id: Optional[int] = None) -> None:
        if plant_id is None and event.sensor_id:
            plant_id = self._plant_id_for(event.sensor_id)
        if event.kind in ONE_PER_DAY_KINDS:
            # Named after the day it belongs to, and after whose day it is: a
            # second record of the same plant's feeding is the same row.
            subject = f"plant-{plant_id}" if plant_id is not None else f"sensor-{event.sensor_id}"
            event = replace(
                event,
                event_id=f"{event.kind}-{subject}-{self._local_day(event.detected_at)}",
            )
        cursor = self._database.execute(
            """
            INSERT OR IGNORE INTO care_events
            (event_id, plant_id, sensor_id, kind, detected_at, title, summary,
             confidence, changes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                plant_id,
                event.sensor_id,
                event.kind,
                event.detected_at,
                event.title,
                event.summary,
                event.confidence,
                json.dumps(event.changes, separators=(",", ":")),
            ),
        )
        if cursor.rowcount > 0 and plant_id is not None:
            # Only a row that was really written counts: saving the same event
            # twice must not add to a figure twice. A sensor nobody has claimed
            # has no plant to keep figures for.
            self._journey_apply_event(
                plant_id,
                event.kind,
                event.detected_at,
                json.dumps(event.changes, separators=(",", ":")),
            )
        self._database.commit()

    def record_air_reading(self, sensor_id: str,
                           temperature_c: Optional[float],
                           humidity_percent: Optional[float],
                           observed_at: Optional[str] = None) -> bool:
        """File what a thermometer measured, as a reading like any other.

        A Zigbee thermometer's reports were kept only in the raw Zigbee
        log, so nothing downstream could see them: the rooms page, the
        plant pages and every figure held against the air all read
        sensor_readings, and as far as they were concerned the device
        did not exist. Writing here rather than teaching each of those
        about a second place to look -- the transport a measurement
        arrived over is not something a room should have to know.

        Air only, and said so: there is no soil probe on this device,
        and a missing soil number must read as absent rather than as
        zero.
        """
        if temperature_c is None and humidity_percent is None:
            return False
        now = _timestamp(datetime.now(timezone.utc))
        reading = SensorReading(
            sensor_id=sensor_id,
            report_id=None,
            observed_at=observed_at or now,
            soil_temperature_c=None,
            moisture_percent=None,
            conductivity_us_cm=None,
            air_temperature_c=temperature_c,
            air_humidity_percent=humidity_percent,
            soil_source_status="unavailable",
            air_source_status="available",
        )
        with self._condition:
            return self._save_reading(reading, now)

    # How long a soil measurement stands in for the present.
    #
    # Comfortably longer than these probes' half-hourly reporting, not
    # equal to it: at thirty minutes a temperature frame arriving eight
    # seconds late lost the moisture that came with the previous one,
    # and the plant page fell back to "Waiting for a reading" while the
    # device's own page showed both. Soil is the slowest thing here --
    # an hour-old moisture reading is still a fair statement of now.
    SOIL_CARRY_SECONDS = 7200

    def _recent_soil(self, sensor_id: str) -> Dict[str, Any]:
        """The newest soil temperature and moisture, each if recent."""
        since = _timestamp(datetime.now(timezone.utc)
                           - timedelta(seconds=self.SOIL_CARRY_SECONDS))
        with self._condition:
            row = self._database.execute(
                """
                SELECT (SELECT soil_temperature_c FROM sensor_readings
                         WHERE sensor_id = ? AND soil_temperature_c IS NOT NULL
                           AND observed_at >= ?
                         ORDER BY observed_at DESC LIMIT 1),
                       (SELECT moisture_percent FROM sensor_readings
                         WHERE sensor_id = ? AND moisture_percent IS NOT NULL
                           AND observed_at >= ?
                         ORDER BY observed_at DESC LIMIT 1)
                """,
                (sensor_id, since, sensor_id, since),
            ).fetchone()
        return {"soil_temperature_c": row[0], "moisture_percent": row[1]}

    def record_soil_reading(self, sensor_id: str,
                            soil_temperature_c: Optional[float],
                            moisture_percent: Optional[float],
                            observed_at: Optional[str] = None) -> bool:
        """File what a probe measured in a pot, as a reading like any other.

        The same table the Bluetooth probes write to, so a Zigbee probe
        needs nothing taught to the plant pages, the journey or the
        watering figures: a soil reading is a soil reading whatever
        radio carried it.

        Soil only, and said so. This probe is in a pot and measures no
        air; a missing air number must read as absent rather than as a
        room that is nought degrees.
        """
        if soil_temperature_c is None and moisture_percent is None:
            return False
        if soil_temperature_c is None or moisture_percent is None:
            recent = self._recent_soil(sensor_id)
            if soil_temperature_c is None:
                soil_temperature_c = recent.get("soil_temperature_c")
            if moisture_percent is None:
                moisture_percent = recent.get("moisture_percent")
        now = _timestamp(datetime.now(timezone.utc))
        reading = SensorReading(
            sensor_id=sensor_id,
            report_id=None,
            observed_at=observed_at or now,
            soil_temperature_c=soil_temperature_c,
            moisture_percent=moisture_percent,
            conductivity_us_cm=None,
            air_temperature_c=None,
            air_humidity_percent=None,
            soil_source_status="available",
            air_source_status="unavailable",
        )
        with self._condition:
            return self._save_reading(reading, now)

    def _save_reading(self, reading: SensorReading, received_at: str) -> bool:
        cursor = self._insert_reading(reading, received_at)
        self._database.commit()
        return cursor.rowcount > 0

    def _insert_reading(
        self,
        reading: SensorReading,
        received_at: str,
        advertisement_id: Optional[int] = None,
    ) -> sqlite3.Cursor:
        self._journey_note_reading(reading.sensor_id, reading.observed_at, received_at)
        return self._database.execute(
            """
            INSERT OR IGNORE INTO sensor_readings (
                advertisement_id, sensor_id, report_id, observed_at, received_at,
                soil_temperature_c, moisture_percent, conductivity_us_cm,
                air_temperature_c, air_humidity_percent, soil_ph,
                nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg,
                battery_percent, battery_voltage_v, battery_charging,
                soil_source_status, air_source_status, contract_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advertisement_id,
                reading.sensor_id,
                reading.report_id,
                reading.observed_at,
                received_at,
                reading.soil_temperature_c,
                reading.moisture_percent,
                reading.conductivity_us_cm,
                reading.air_temperature_c,
                reading.air_humidity_percent,
                reading.soil_ph,
                reading.nitrogen_mg_kg,
                reading.phosphorus_mg_kg,
                reading.potassium_mg_kg,
                reading.battery_percent,
                reading.battery_voltage_v,
                _charging_column(reading.battery_charging),
                reading.soil_source_status,
                reading.air_source_status,
                reading.contract_version,
            ),
        )

    def _ensure_sensor(
        self,
        sensor_id: str,
        contract_version: int,
        received_at: str,
        transport: str,
        identity_kind: str,
        rssi: Optional[int] = None,
        commit: bool = True,
    ) -> None:
        self._database.execute(
            """
            INSERT INTO sensors (
                sensor_id, identity_kind, identity_value, enrollment_status,
                first_seen_at, last_seen_at, latest_rssi, transport,
                contract_version
            ) VALUES (?, ?, ?, 'unclaimed', ?, ?, ?, ?, ?)
            ON CONFLICT(sensor_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                latest_rssi = excluded.latest_rssi,
                contract_version = excluded.contract_version
            """,
            (
                sensor_id,
                identity_kind,
                sensor_id,
                received_at,
                received_at,
                rssi,
                transport,
                contract_version,
            ),
        )
        if commit:
            self._database.commit()

    def _trim_receive_diagnostics(self) -> None:
        # Only an accepted packet 1 has a reading pointing at it. Everything
        # else is diagnostics, beacons and packet 2 included: packet 2's
        # values are kept in report_packet2, not in its packet.
        self._database.execute(
            """
            DELETE FROM advertisements
            WHERE (decode_status != 'accepted' OR packet_kind IN ('packet2', 'beacon'))
              AND advertisement_id NOT IN (
                  SELECT advertisement_id
                  FROM advertisements
                  WHERE decode_status != 'accepted'
                     OR packet_kind IN ('packet2', 'beacon')
                  ORDER BY advertisement_id DESC
                  LIMIT ?
              )
            """,
            (RECEIVE_DIAGNOSTIC_LIMIT,),
        )

    def _restore_history(self, history_size: int) -> None:
        rows = self._database.execute(
            f"""
            SELECT {READING_COLUMNS}
            FROM sensor_readings
            ORDER BY reading_id DESC
            LIMIT ?
            """,
            (history_size,),
        ).fetchall()
        for row in reversed(rows):
            item = self._deserialize_row(row)
            self._history.append((item["reading"], item["received_at"]))

    @staticmethod
    def _sensor_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "sensor_id": row[0],
            "identity_kind": row[1],
            "identity_value": row[2],
            "enrollment_status": row[3],
            "display_name": row[4],
            "room": row[5],
            "plant_id": row[6],
            "profile_id": row[7],
            "first_seen_at": row[8],
            "last_seen_at": row[9],
            "latest_rssi": row[10],
            "transport": row[11],
            "contract_version": row[12],
            "moisture_low_percent": row[13],
            "conductivity_high_us_cm": row[14],
            "expected_interval_seconds": row[15],
            "replaced_by_sensor_id": row[16],
            "device_config_revision": row[17],
            "device_config_applied_revision": row[18],
            "device_config_attempted_at": row[19],
            "device_config_error": row[20],
            "sensor_reporting_interval_seconds": row[21],
            "onboarding_state": row[22],
            "wifi_enabled": bool(row[23]),
            "wifi_state": row[24],
            "wifi_failure": row[25],
            "wifi_address": row[26],
            "room_id": row[27],
            "firmware_version": row[28],
            "station_checked_at": row[29],
            "firmware_update_digest": row[30],
            "firmware_update_version": row[31],
            "firmware_update_state": row[32],
            "firmware_update_id": row[33],
            "firmware_update_percent": row[34],
            "firmware_update_error": row[35],
            "firmware_update_started_at": row[36],
            "soil_ph_offset": row[37],
            "soil_ph_reference": row[38],
            "soil_ph_reference_raw": row[39],
            "soil_ph_reference_at": row[40],
            "soil_ph_reference_ec": row[41],
            "scale_address": row[42],
            "scale_keep_every_seconds": row[43],
            # Appended to the end of the select so the indices above did not
            # all have to move; its position in the list is not its meaning.
            "scale_keep_at_hour": row[55],
            "zigbee_host": row[56] if len(row) > 56 else None,
            "plant_name": row[57] if len(row) > 57 else None,
            # How often this device is asked to report. None means the
            # hub's default for its kind.
            "zigbee_report_max_seconds": row[58] if len(row) > 58 else None,
            "zigbee_report_sensitivity": row[59] if len(row) > 59 else None,
            "scale": None if row[42] is None else {
                "grams": row[44],
                "status": row[45],
                "stable": None if row[46] is None else bool(row[46]),
                "read_at": row[47],
                "error": row[48],
                "interval_seconds": row[49],
                "firmware": row[50],
                "build": row[51],
                "seq": row[52],
                "battery_volts": row[53],
                "battery_percent": row[54],
            },
        }

    def _decorate_sensor(self, sensor: Dict[str, Any]) -> Dict[str, Any]:
        # The plant's cover, so a card shows the plant whether the hub knows
        # it through a sensor or not. The picture belongs to the plant; the
        # sensor is only how this card found it.
        sensor["cover_photo_id"] = None
        sensor["cover_photo_digest"] = None
        if sensor["plant_id"] is not None:
            cover = self._database.execute(
                """
                SELECT plants.cover_photo_id, plant_photos.digest
                FROM plants
                LEFT JOIN plant_photos ON plant_photos.photo_id = plants.cover_photo_id
                WHERE plants.plant_id = ?
                """,
                (sensor["plant_id"],),
            ).fetchone()
            if cover is not None:
                sensor["cover_photo_id"], sensor["cover_photo_digest"] = cover
        latest = self.latest(sensor["sensor_id"])
        received_at = latest["received_at"] if latest is not None else sensor["last_seen_at"]
        received = self._observation_datetime(received_at, datetime.now(timezone.utc))
        age_seconds = max(0, int((datetime.now(timezone.utc) - received).total_seconds()))
        active_interval_seconds = (
            sensor["sensor_reporting_interval_seconds"]
            if sensor["sensor_reporting_interval_seconds"] is not None
            else sensor["expected_interval_seconds"]
        )
        stale_after_seconds = active_interval_seconds * 2
        sensor["hub_reporting_interval_seconds"] = sensor["expected_interval_seconds"]
        if sensor["device_config_revision"] == 0:
            sensor["device_config_status"] = "not_configured"
        elif sensor["device_config_applied_revision"] == sensor["device_config_revision"]:
            sensor["device_config_status"] = "applied"
        elif sensor["device_config_error"]:
            sensor["device_config_status"] = "retrying"
        else:
            sensor["device_config_status"] = "pending"
        sensor["age_seconds"] = age_seconds
        # How long since the hub last heard the device at all, as opposed to
        # since it last sent a measurement. A sensor beaconing for adoption has
        # nothing to measure, so only this one says whether it is in the room.
        seen = self._observation_datetime(sensor["last_seen_at"], datetime.now(timezone.utc))
        sensor["seen_age_seconds"] = max(
            0, int((datetime.now(timezone.utc) - seen).total_seconds())
        )
        # Freshness answers "is this sensor reporting?", which is about being
        # heard. Judging it by the last measurement called a sensor with no probe
        # stale while it was announcing itself every few seconds, which described
        # the probe and blamed the radio. What the measurements are doing is a
        # separate answer, because a silent sensor and a blind one need different
        # things done about them.
        sensor["freshness"] = (
            "fresh" if sensor["seen_age_seconds"] <= stale_after_seconds else "stale"
        )
        if latest is None:
            sensor["measurements"] = "none"
        elif age_seconds <= stale_after_seconds:
            sensor["measurements"] = "current"
        else:
            sensor["measurements"] = "stale"
        sensor["latest"] = latest
        sensor["soil_chemistry"] = self.soil_chemistry(sensor["sensor_id"])
        sensor["reading_count"] = int(
            self._database.execute(
                "SELECT COUNT(*) FROM sensor_readings WHERE sensor_id = ?",
                (sensor["sensor_id"],),
            ).fetchone()[0]
        )
        return sensor

    def adopt_scale(self, address: str, name: str) -> Dict[str, Any]:
        """Take on a scale the hub can reach, or recognise one it already has.

        A scale is identified by its address rather than by anything it
        broadcasts, because it does not broadcast: the hub goes to it. The
        same address adopted twice is the same scale, so this is safe to
        call again after a name change or a move to a new network.

        Adopting one adds a sensor and nothing else. It is enrolled on the
        spot, because reaching a scale is the whole of pairing with it, and
        it is attached to no plant: a scale is a thing that weighs, not a
        thing that grows, and which plant sits on it is a separate sentence
        somebody says later. Going through manage_sensor instead would have
        created a plant to go with it, which is what a probe wants and a
        scale never does.
        """
        address = address.strip().rstrip("/")
        if not address:
            raise ValueError("a scale needs an address")
        if not name.strip():
            raise ValueError("a scale needs a name")
        sensor_id = "scale-" + "".join(
            character if character.isalnum() else "-" for character in address.lower()
        ).strip("-")
        now = _timestamp(datetime.now(timezone.utc))
        with self._condition, self._database:
            existing = self._database.execute(
                "SELECT sensor_id FROM sensors WHERE scale_address = ?", (address,)
            ).fetchone()
            if existing is not None:
                self._database.execute(
                    "UPDATE sensors SET last_seen_at = ? WHERE sensor_id = ?",
                    (now, existing[0]),
                )
                sensor_id = existing[0]
            else:
                self._database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, transport, contract_version,
                        first_seen_at, last_seen_at, onboarding_state, scale_address
                    ) VALUES (?, 'http-address', ?, 'enrolled', ?, 'http-scale', 0, ?, ?,
                              'onboarded', ?)
                    """,
                    (sensor_id, address, name.strip(), now, now, address),
                )
        sensor = self.sensor(sensor_id)
        assert sensor is not None
        return sensor

    def seen_zigbee_device(self, eui: str, kind: str) -> Dict[str, Any]:
        """Record a Zigbee device, or note that a known one spoke again.

        A device that joins the radio has announced itself and nothing more,
        so it arrives unclaimed: visible, inert, and waiting for somebody to
        say yes. That is the same state a sensor heard over Bluetooth starts
        in, and it is the reason this is a sensor row rather than an entry in
        a list of its own -- naming it, attaching it to a plant, putting it
        in a room and archiving it all already work for sensors.
        """
        eui = eui.strip().lower()
        if not eui:
            raise ValueError("a zigbee device needs an EUI-64")
        sensor_id = f"zigbee-{eui}"
        now = _timestamp(datetime.now(timezone.utc))
        with self._condition, self._database:
            existing = self._database.execute(
                "SELECT sensor_id FROM sensors WHERE identity_kind = 'eui64' AND identity_value = ?",
                (eui,),
            ).fetchone()
            if existing is not None:
                # A definite answer replaces whatever is there; only
                # "unknown" never overwrites. Restricted to replacing
                # "unknown", a device judged wrongly once was stuck with
                # it for ever -- a soil probe first read as a room
                # thermometer stayed one after the hub knew better.
                self._database.execute(
                    """
                    UPDATE sensors
                       SET last_seen_at = ?,
                           transport = CASE
                               WHEN ? <> 'unknown' THEN ? ELSE transport END
                     WHERE sensor_id = ?
                    """,
                    (now, kind, f"zigbee-{kind}", existing[0]),
                )
                sensor_id = existing[0]
            else:
                self._database.execute(
                    """
                    INSERT INTO sensors (
                        sensor_id, identity_kind, identity_value, enrollment_status,
                        display_name, transport, contract_version,
                        first_seen_at, last_seen_at, onboarding_state
                    ) VALUES (?, 'eui64', ?, 'unclaimed', ?, ?, 0, ?, ?, 'onboarding')
                    """,
                    (sensor_id, eui, None, f"zigbee-{kind}", now, now),
                )
        sensor = self.sensor(sensor_id)
        assert sensor is not None
        return sensor

    def set_scale_schedule(self, sensor_id: str, every_seconds: Optional[int],
                           at_hour: Optional[int]) -> Dict[str, Any]:
        """When this scale's weight is worth writing down.

        One or the other: an interval gives a dense drying curve, an hour
        gives one weight a day taken at the same time, which is what makes a
        week of them comparable. Setting the hour clears the interval's hold
        rather than running both, because two schedules writing to one log
        is a log nobody can read.
        """
        if at_hour is not None and not 0 <= int(at_hour) <= 23:
            raise ValueError("an hour is 0 to 23")
        if every_seconds is not None and int(every_seconds) < 60:
            raise ValueError("an interval of less than a minute would bury the log")
        with self._condition, self._database:
            if self._database.execute(
                "SELECT 1 FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone() is None:
                raise ValueError("no such sensor")
            self._database.execute(
                """
                UPDATE sensors
                SET scale_keep_at_hour = ?,
                    scale_keep_every_seconds = COALESCE(?, scale_keep_every_seconds)
                WHERE sensor_id = ?
                """,
                (None if at_hour is None else int(at_hour),
                 None if every_seconds is None else int(every_seconds),
                 sensor_id),
            )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def enroll_zigbee_device(self, eui: str, name: Optional[str] = None) -> Dict[str, Any]:
        """Let a Zigbee device in, without inventing a plant to go with it.

        manage_sensor is the usual way to adopt a sensor, and it creates a
        plant, because for a soil probe the sensor and the plant arrive
        together. A Zigbee device is like a scale: it is a thing that
        measures, and what it measures is a separate sentence somebody says
        afterwards -- a plant for a scale, a room for a thermometer. So this
        enrols the row and leaves both alone.
        """
        sensor = self.seen_zigbee_device(eui, "unknown")
        sensor_id = sensor["sensor_id"]
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET enrollment_status = 'enrolled', archived = 0,
                    onboarding_state = 'onboarded',
                    display_name = COALESCE(?, display_name)
                WHERE sensor_id = ?
                """,
                ((name or "").strip() or None, sensor_id),
            )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def set_sensor_room(self, sensor_id: str, room_id: Optional[int]) -> Dict[str, Any]:
        """Put a sensor in a room, for devices that are about a room.

        A soil probe gets its room from the plant it is in. A thermometer
        has no plant: the room is the whole of what it is about, so it is
        set directly.
        """
        with self._condition, self._database:
            if self._database.execute(
                "SELECT 1 FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone() is None:
                raise ValueError("no such sensor")
            if room_id is not None and self._database.execute(
                "SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone() is None:
                raise ValueError("no such room")
            self._database.execute(
                "UPDATE sensors SET room_id = ? WHERE sensor_id = ?", (room_id, sensor_id)
            )
        result = self.sensor(sensor_id)
        assert result is not None
        return result

    def zigbee_sensors(self) -> List[Dict[str, Any]]:
        """Every Zigbee device the hub has met, claimed or not."""
        return [
            sensor for sensor in self.sensors()
            if str(sensor.get("transport", "")).startswith("zigbee")
        ]

    def _is_scale(self, sensor_id: str) -> bool:
        with self._condition:
            row = self._database.execute(
                "SELECT transport FROM sensors WHERE sensor_id = ?", (sensor_id,)
            ).fetchone()
        return row is not None and row[0] == "http-scale"

    def scales(self) -> List[Dict[str, Any]]:
        """Every scale the hub knows, adopted or not, for the poller."""
        with self._condition:
            rows = self._database.execute(
                """
                SELECT sensor_id, scale_address, plant_id, scale_keep_every_seconds,
                       scale_seq, display_name, scale_keep_at_hour
                FROM sensors
                WHERE scale_address IS NOT NULL AND COALESCE(archived, 0) = 0
                """
            ).fetchall()
        return [
            {"sensor_id": row[0], "address": row[1], "plant_id": row[2],
             "keep_every_seconds": row[3], "seq": row[4], "display_name": row[5],
             "keep_at_hour": row[6]}
            for row in rows
        ]

    def record_scale_reading(
        self,
        sensor_id: str,
        grams: Optional[float],
        status: str,
        stable: Optional[bool],
        error: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        firmware: Optional[str] = None,
        build: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        """Keep what the scale last said, whether or not it was worth storing."""
        now = _timestamp(datetime.now(timezone.utc))
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors
                SET scale_grams = ?, scale_status = ?, scale_stable = ?,
                    scale_read_at = ?, scale_error = ?, last_seen_at = ?,
                    scale_interval_seconds = COALESCE(?, scale_interval_seconds),
                    scale_firmware = COALESCE(?, scale_firmware),
                    scale_build = COALESCE(?, scale_build),
                    scale_seq = COALESCE(?, scale_seq)
                WHERE sensor_id = ?
                """,
                (None if grams is None else int(round(grams)), status,
                 None if stable is None else (1 if stable else 0),
                 now, error, now, interval_seconds, firmware, build, seq, sensor_id),
            )

    def record_scale_battery(
        self, sensor_id: str, volts: Optional[float], percent: Optional[int]
    ) -> None:
        """What the scale said about its cell, when it was last asked."""
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors SET scale_battery_volts = ?, scale_battery_percent = ?
                WHERE sensor_id = ?
                """,
                (volts, percent, sensor_id),
            )

    def set_scale_keep(self, sensor_id: str, every_seconds: int) -> Dict[str, Any]:
        """How often a weight from this scale is written down.

        A week at the far end, because some pots are watered about that
        often and a weight a day from one of those is six readings nobody
        reads between the two that mattered. The scale keeps weighing
        either way; this is only how much of it is kept.
        """
        if not 30 <= int(every_seconds) <= 604800:
            raise ValueError("keep interval must be between 30 seconds and a week")
        if self.sensor(sensor_id) is None:
            raise ValueError("unknown sensor")
        with self._condition, self._database:
            self._database.execute(
                "UPDATE sensors SET scale_keep_every_seconds = ? WHERE sensor_id = ?",
                (int(every_seconds), sensor_id),
            )
        sensor = self.sensor(sensor_id)
        assert sensor is not None
        return sensor

    def soil_chemistry(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        """The last pH and nutrients this sensor actually measured.

        These ride on a reading taken a couple of times a day and on a
        watering, so the newest reading usually carries none of them and
        the figures worth showing are the last real ones with their age
        beside them. A number from this morning is useful; the same number
        presented as current is not.
        """
        # Read straight from the table rather than through sensor(), which
        # decorates every sensor with this and would call us back forever.
        with self._condition:
            calibration = self._database.execute(
                """
                SELECT soil_ph_offset, soil_ph_reference, soil_ph_reference_raw,
                       soil_ph_reference_at, soil_ph_reference_ec
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            if calibration is None:
                return None
            row = self._database.execute(
                """
                SELECT soil_ph, observed_at, conductivity_us_cm,
                       nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
                FROM sensor_readings
                WHERE sensor_id = ? AND soil_ph IS NOT NULL
                ORDER BY observed_at DESC LIMIT 1
                """,
                (sensor_id,),
            ).fetchone()
        offset = float(calibration[0] or 0.0)
        status: Dict[str, Any] = {
            "offset": round(offset, 2),
            "reference": calibration[1],
            "reference_raw": calibration[2],
            "reference_at": calibration[3],
            "reference_ec": calibration[4],
            "calibrated": calibration[1] is not None,
            "raw": None,
            "ph": None,
            "at": None,
            "age_seconds": None,
            "conductivity_us_cm": None,
            "nitrogen_mg_kg": None,
            "phosphorus_mg_kg": None,
            "potassium_mg_kg": None,
            "far_from_calibration": False,
        }
        if row is None or row[0] is None:
            return status
        raw = float(row[0])
        status.update({
            "raw": round(raw, 2),
            "ph": round(raw + offset, 2),
            "at": row[1],
            "age_seconds": max(0, int((
                datetime.now(timezone.utc)
                - self._observation_datetime(row[1], datetime.now(timezone.utc))
            ).total_seconds())),
            "conductivity_us_cm": row[2],
            "nitrogen_mg_kg": row[3],
            "phosphorus_mg_kg": row[4],
            "potassium_mg_kg": row[5],
        })
        # The probe's pH moves with conductivity, so a correction found at
        # one is not promised at another. Said rather than silently applied.
        reference_ec = calibration[4]
        if status["calibrated"] and reference_ec and row[2]:
            ratio = row[2] / float(reference_ec)
            status["far_from_calibration"] = ratio >= 2.0 or ratio <= 0.5
        return status

    def set_soil_ph_calibration(
        self, sensor_id: str, reference: Optional[float]
    ) -> Dict[str, Any]:
        """Pin the probe's pH to one measured reference, or let it go.

        The offset is worked out from the last pH the probe actually
        reported, so nobody has to type what it read -- and the conditions
        are kept with it, because that is what makes a calibration
        checkable later rather than a number of unknown origin.
        """
        if self.sensor(sensor_id) is None:
            raise ValueError("unknown sensor")
        if reference is None:
            with self._condition, self._database:
                self._database.execute(
                    """
                    UPDATE sensors SET soil_ph_offset = 0, soil_ph_reference = NULL,
                           soil_ph_reference_raw = NULL, soil_ph_reference_at = NULL,
                           soil_ph_reference_ec = NULL
                    WHERE sensor_id = ?
                    """,
                    (sensor_id,),
                )
            status = self.soil_chemistry(sensor_id)
            assert status is not None
            return status
        if not 0.0 <= float(reference) <= 14.0:
            raise ValueError("reference must be a pH between 0 and 14")
        with self._condition:
            row = self._database.execute(
                """
                SELECT soil_ph, observed_at, conductivity_us_cm
                FROM sensor_readings
                WHERE sensor_id = ? AND soil_ph IS NOT NULL
                ORDER BY observed_at DESC LIMIT 1
                """,
                (sensor_id,),
            ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("this sensor has not reported a pH to calibrate against")
        raw = float(row[0])
        with self._condition, self._database:
            self._database.execute(
                """
                UPDATE sensors SET soil_ph_offset = ?, soil_ph_reference = ?,
                       soil_ph_reference_raw = ?, soil_ph_reference_at = ?,
                       soil_ph_reference_ec = ?
                WHERE sensor_id = ?
                """,
                (round(float(reference) - raw, 2), float(reference), raw,
                 _timestamp(datetime.now(timezone.utc)), row[2], sensor_id),
            )
        status = self.soil_chemistry(sensor_id)
        assert status is not None
        return status

    @staticmethod
    def _validate_device_text(value: str, field: str, required: bool) -> None:
        encoded = value.encode("utf-8")
        if (required and not encoded) or len(encoded) > DEVICE_CONFIG_TEXT_MAX_BYTES:
            requirement = "1 to 80" if required else "at most 80"
            raise ValueError(f"{field} must contain {requirement} UTF-8 bytes")
        if any(byte < 0x20 or byte == 0x7F for byte in encoded):
            raise ValueError(f"{field} cannot contain control characters")

    @staticmethod
    def _serialize(reading: SensorReading, received_at: str) -> Dict[str, Any]:
        return {"reading": asdict(reading), "received_at": received_at}

    @classmethod
    def _serialize_row(cls, row: Tuple[Any, ...]) -> Dict[str, Any]:
        item = cls._deserialize_row(row)
        return cls._serialize(item["reading"], item["received_at"])

    @staticmethod
    def _deserialize_row(row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "reading": SensorReading(
                sensor_id=row[0],
                report_id=row[1],
                observed_at=row[2],
                soil_temperature_c=row[3],
                moisture_percent=row[4],
                conductivity_us_cm=row[5],
                air_temperature_c=row[6],
                air_humidity_percent=row[7],
                soil_ph=row[8],
                nitrogen_mg_kg=row[9],
                phosphorus_mg_kg=row[10],
                potassium_mg_kg=row[11],
                battery_percent=row[16],
                battery_voltage_v=row[17],
                battery_charging=_charging_value(row[18]),
                soil_source_status=row[13],
                air_source_status=row[14],
                contract_version=row[15],
            ),
            "received_at": row[12],
        }

    def _log_advertisement(
        self,
        sensor_id: str,
        report_id: Optional[int],
        packet_kind: str,
        decode_status: str,
        received_at: str,
        observed_identifier: str,
        source_adapter: str,
        rssi: Optional[int],
        contract_version: int,
        service_data: bytes,
    ) -> int:
        cursor = self._database.execute(
            """
            INSERT INTO advertisements (
                sensor_id, report_id, packet_kind, received_at, transport,
                source_adapter, observed_identifier, rssi, contract_version,
                payload_sha256, decode_status, decode_error, service_data
            ) VALUES (?, ?, ?, ?, 'bthome', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sensor_id,
                report_id,
                packet_kind,
                received_at,
                source_adapter[:64],
                observed_identifier[:240],
                rssi,
                contract_version,
                hashlib.sha256(service_data).hexdigest(),
                decode_status,
                (
                    f"report {report_id} is already stored with different content"
                    if decode_status == "conflict"
                    else None
                ),
                service_data,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _report_status(stored: Optional[Tuple[Any, ...]], content: Tuple[Any, ...]) -> str:
        if stored is None:
            return "accepted"
        return "duplicate" if tuple(stored) == content else "conflict"

    @staticmethod
    def _packet1_content(reading: SensorReading) -> Tuple[Any, ...]:
        return (
            reading.observed_at,
            reading.soil_temperature_c,
            reading.moisture_percent,
            reading.conductivity_us_cm,
            reading.air_temperature_c,
            reading.air_humidity_percent,
            reading.soil_source_status,
            reading.air_source_status,
        )

    @staticmethod
    def _packet2_content(packet2: ReportPacket2) -> Tuple[Any, ...]:
        return (
            packet2.battery_percent,
            packet2.battery_voltage_v,
            packet2.soil_ph,
            packet2.nitrogen_mg_kg,
            packet2.phosphorus_mg_kg,
            packet2.potassium_mg_kg,
            _charging_column(packet2.battery_charging),
        )

    @staticmethod
    def _with_packet2(reading: SensorReading, packet2: Tuple[Any, ...]) -> SensorReading:
        return replace(
            reading,
            battery_percent=packet2[0],
            battery_voltage_v=packet2[1],
            soil_ph=packet2[2],
            nitrogen_mg_kg=packet2[3],
            phosphorus_mg_kg=packet2[4],
            potassium_mg_kg=packet2[5],
            battery_charging=_charging_value(packet2[6]),
        )

    @staticmethod
    def _observation_datetime(observed_at: Optional[str], fallback: datetime) -> datetime:
        if not observed_at:
            return fallback
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if observed.tzinfo is None:
            return fallback
        return observed.astimezone(timezone.utc)