from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
from threading import Condition
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from open_plant_pulse_hub.domain import ReportPacket2, SensorReading
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
    LEFT JOIN plant_lights ON plant_lights.light_id = lights.light_id
    LEFT JOIN plants ON plants.plant_id = plant_lights.plant_id
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


def _timestamp(moment: datetime) -> str:
    # The form every stored time takes, so that text comparison orders them.
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _charging_column(charging: Optional[bool]) -> Optional[int]:
    # SQLite has no boolean; charging is stored as 0 or 1, null when not said.
    return None if charging is None else int(charging)


def _charging_value(column: Optional[int]) -> Optional[bool]:
    return None if column is None else bool(column)


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
                       display_name, room, plant_id, profile_id, first_seen_at,
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
                       firmware_update_error, firmware_update_started_at
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
                       display_name, room, plant_id, profile_id, first_seen_at,
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
                       firmware_update_error, firmware_update_started_at
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            return self._decorate_sensor(self._sensor_payload(row)) if row is not None else None

    def hub_settings(self) -> Dict[str, int]:
        with self._condition:
            row = self._database.execute(
                "SELECT reporting_interval_minutes FROM hub_settings WHERE singleton_id = 1"
            ).fetchone()
        assert row is not None
        return {"reporting_interval_minutes": int(row[0])}

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
        """Every room, with how many sensors are in it."""
        with self._condition:
            return [
                {
                    "room_id": row[0],
                    "name": row[1],
                    "aspect": row[2],
                    "light": row[3],
                    "notes": row[4],
                    "sensor_count": row[5],
                }
                for row in self._database.execute(
                    """
                    SELECT rooms.room_id, rooms.name, rooms.aspect, rooms.light, rooms.notes,
                           (SELECT COUNT(*) FROM sensors
                            WHERE sensors.room_id = rooms.room_id
                              AND sensors.enrollment_status = 'enrolled')
                    FROM rooms ORDER BY rooms.name
                    """
                )
            ]

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
            # The sensor keeps a readable copy of its room, so renaming a room
            # must not leave sensors labelled with the old name.
            self._database.execute(
                "UPDATE sensors SET room = ? WHERE room_id = ?", (name, room_id)
            )
        return next(room for room in self.rooms() if room["room_id"] == room_id)

    def delete_room(self, room_id: int) -> None:
        """Remove a room. A room still holding sensors is kept."""
        with self._condition, self._database:
            occupied = self._database.execute(
                """
                SELECT COUNT(*) FROM sensors
                WHERE room_id = ? AND enrollment_status = 'enrolled'
                """,
                (room_id,),
            ).fetchone()[0]
            if occupied:
                raise ValueError("move the sensors out of this room first")
            deleted = self._database.execute(
                "DELETE FROM rooms WHERE room_id = ?", (room_id,)
            ).rowcount
            if deleted == 0:
                raise ValueError("room_id is unknown")
            self._database.execute(
                "UPDATE sensors SET room_id = NULL WHERE room_id = ?", (room_id,)
            )

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
        """The lights a plant uses and when they are lit.

        A plant that has never been given a schedule is shown the default one,
        marked unsaved; it takes effect only once saved.
        """
        with self._condition:
            plant_id = self._plant_of_sensor(sensor_id)
            light_ids = [
                row[0]
                for row in self._database.execute(
                    "SELECT light_id FROM plant_lights WHERE plant_id = ? ORDER BY light_id",
                    (plant_id,),
                )
            ]
            row = self._database.execute(
                """
                SELECT enabled, on_time, off_time FROM plant_light_schedules
                WHERE plant_id = ?
                """,
                (plant_id,),
            ).fetchone()
        schedule = (
            {"enabled": bool(row[0]), "on": row[1], "off": row[2]}
            if row is not None
            else dict(DEFAULT_SCHEDULE)
        )
        return {
            "sensor_id": sensor_id,
            "plant_id": plant_id,
            "light_ids": light_ids,
            "schedule": schedule,
            "schedule_saved": row is not None,
        }

    def set_plant_lighting(
        self, sensor_id: str, light_ids: List[str], schedule: Dict[str, Any]
    ) -> Dict[str, Any]:
        schedule = validate_schedule(schedule)
        if not isinstance(light_ids, list) or not all(isinstance(i, str) for i in light_ids):
            raise ValueError("light_ids must be a list of light IDs")
        light_ids = list(dict.fromkeys(light_ids))
        with self._condition, self._database:
            plant_id = self._plant_of_sensor(sensor_id)
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
                if owner[1] is not None and owner[1] != plant_id:
                    raise ValueError(f"{owner[0]} already lights {owner[2]}")
            self._database.execute("DELETE FROM plant_lights WHERE plant_id = ?", (plant_id,))
            self._database.executemany(
                "INSERT INTO plant_lights (light_id, plant_id) VALUES (?, ?)",
                [(light_id, plant_id) for light_id in light_ids],
            )
            self._database.execute(
                """
                INSERT INTO plant_light_schedules (plant_id, enabled, on_time, off_time, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (plant_id) DO UPDATE SET
                    enabled = excluded.enabled, on_time = excluded.on_time,
                    off_time = excluded.off_time, updated_at = excluded.updated_at
                """,
                (
                    plant_id,
                    int(schedule["enabled"]),
                    schedule["on"],
                    schedule["off"],
                    _timestamp(datetime.now(timezone.utc)),
                ),
            )
        return self.plant_lighting(sensor_id)

    def light_assignments(self) -> Dict[str, Dict[str, Any]]:
        """For each light that belongs to a plant: that plant and its schedule."""
        with self._condition:
            return {
                row[0]: {
                    "plant_id": row[1],
                    "archived": bool(row[2]),
                    "schedule": (
                        {"enabled": bool(row[3]), "on": row[4], "off": row[5]}
                        if row[3] is not None
                        else None
                    ),
                }
                for row in self._database.execute(
                    """
                    SELECT plant_lights.light_id, plants.plant_id, plants.archived,
                           schedules.enabled, schedules.on_time, schedules.off_time
                    FROM plant_lights
                    JOIN plants ON plants.plant_id = plant_lights.plant_id
                    LEFT JOIN plant_light_schedules AS schedules
                        ON schedules.plant_id = plants.plant_id
                    """
                )
            }

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
                    digest, version, project, idf_version, size_bytes, built_at, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(digest) DO NOTHING
                """,
                (
                    image["digest"],
                    image["version"],
                    image["project"],
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
                SELECT digest, version, project, idf_version, size_bytes, built_at, uploaded_at
                FROM firmware_images
                ORDER BY uploaded_at DESC, version DESC
                """
            ).fetchall()
        return [self._firmware_payload(row) for row in rows]

    def firmware_image(self, digest: str) -> Optional[Dict[str, Any]]:
        with self._condition:
            row = self._database.execute(
                """
                SELECT digest, version, project, idf_version, size_bytes, built_at, uploaded_at
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
            self._database.execute("DELETE FROM care_events WHERE sensor_id = ?", (sensor_id,))
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
        display_name: str,
        room: str,
        profile_id: str,
        moisture_low_percent: Optional[float],
        conductivity_high_us_cm: Optional[int],
        expected_interval_seconds: int,
        room_id: Optional[int] = None,
    ) -> Dict[str, Any]:
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
                SELECT plant_id, display_name, room, expected_interval_seconds,
                       device_config_revision, enrollment_status
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            if current is None:
                raise ValueError("sensor_id has not been observed")
            plant_id = current[0]
            # Adopting a sensor always re-delivers its configuration, even when
            # every field matches what the hub last sent. A sensor being adopted
            # has just been reset or has come from another hub, so whatever it
            # holds cannot be trusted; and the delivery is what opens the
            # connection that pairing needs.
            adopting = current[5] != "enrolled"
            device_config_changed = (
                adopting
                or current[1] != display_name
                or (current[2] or "") != room
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
            cursor = self._database.execute(
                """
                UPDATE sensors
                SET enrollment_status = 'enrolled', archived = 0,
                    onboarding_state = 'onboarded',
                    display_name = ?, room = ?, room_id = ?, plant_id = ?, profile_id = ?,
                    moisture_low_percent = ?, conductivity_high_us_cm = ?,
                    expected_interval_seconds = ?, replaced_by_sensor_id = NULL,
                    device_config_revision = ?,
                    device_config_error = CASE WHEN ? THEN NULL ELSE device_config_error END
                WHERE sensor_id = ?
                """,
                (
                    display_name,
                    room or None,
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
        with self._condition:
            row = self._database.execute(
                """
                SELECT device_config_revision, expected_interval_seconds,
                       display_name, COALESCE(room, ''), wifi_enabled
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
                        self._database.execute(
                            "UPDATE care_events SET sensor_id = ? WHERE sensor_id = ?",
                            (replacement_sensor_id, sensor_id),
                        )
                        self._database.execute(
                            "UPDATE advertisements SET sensor_id = ? WHERE sensor_id = ?",
                            (replacement_sensor_id, sensor_id),
                        )
            except sqlite3.IntegrityError as error:
                raise ValueError("history cannot be merged because readings overlap") from error
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
                WHERE sensor_id = ? AND kind = 'watering'
                  AND julianday(detected_at) >= julianday(?)
                  AND julianday(detected_at) <= julianday(?)
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
            WHERE sensor_id = ? AND kind = 'watering'
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
        parameters = (sensor_id, start_at, end_at)
        with self._condition:
            total = int(
                self._database.execute(
                    """
                    SELECT COUNT(*)
                    FROM sensor_readings
                    WHERE sensor_id = ?
                      AND julianday(COALESCE(observed_at, received_at)) >= julianday(?)
                      AND julianday(COALESCE(observed_at, received_at)) <= julianday(?)
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
                      AND julianday(COALESCE(observed_at, received_at)) >= julianday(?)
                      AND julianday(COALESCE(observed_at, received_at)) <= julianday(?)
                    ORDER BY julianday(COALESCE(observed_at, received_at)), reading_id
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
                                   ORDER BY julianday(COALESCE(observed_at, received_at)), reading_id
                               ) AS sample_number
                        FROM sensor_readings
                        WHERE sensor_id = ?
                          AND julianday(COALESCE(observed_at, received_at)) >= julianday(?)
                          AND julianday(COALESCE(observed_at, received_at)) <= julianday(?)
                    )
                    SELECT {columns}
                    FROM ranged
                    WHERE (sample_number - 1) % ? = 0 OR sample_number = ?
                    ORDER BY sample_number
                    """,
                    (*parameters, sample_step, total),
                ).fetchall()
        return [self._serialize_row(row) for row in rows]

    def care_log(self, limit: int = 50, sensor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self._condition:
            query = """
                SELECT event_id, sensor_id, kind, detected_at, title, summary, confidence, changes_json
                FROM care_events
            """
            parameters: Tuple[Any, ...]
            if sensor_id is not None:
                query += " WHERE sensor_id = ?"
                parameters = (sensor_id, safe_limit)
            else:
                parameters = (safe_limit,)
            rows = self._database.execute(
                query + " ORDER BY detected_at DESC LIMIT ?", parameters
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

    def watering_calendar(self, sensor_id: str, start_at: str, end_at: str) -> List[Dict[str, Any]]:
        start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        with self._condition:
            event_rows = self._database.execute(
                """
                SELECT kind, detected_at
                FROM care_events
                WHERE sensor_id = ? AND kind IN ('watering', 'watering_due')
                  AND detected_at < ?
                ORDER BY detected_at
                """,
                (sensor_id, end_at),
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
        latest_at = datetime.fromisoformat(latest_row[0].replace("Z", "+00:00")) if latest_row[0] else start
        activity: Dict[str, Dict[str, Any]] = {
            day: {
                "date": day,
                "watering_count": 0,
                "drying_level": 0,
                "final_moisture_percent": round(moisture, 2),
            }
            for day, moisture in final_moisture_by_day.items()
            if start.date().isoformat() <= day < end.date().isoformat()
        }
        events = [
            (kind, datetime.fromisoformat(detected_at.replace("Z", "+00:00")))
            for kind, detected_at in event_rows
        ]
        for kind, detected_at in events:
            if kind == "watering" and start <= detected_at < end:
                day = detected_at.date().isoformat()
                item = activity.setdefault(
                    day,
                    {"date": day, "watering_count": 0, "drying_level": 0, "final_moisture_percent": None},
                )
                item["watering_count"] += 1

        for index, (kind, due_at) in enumerate(events):
            if kind != "watering_due":
                continue
            watered_at = next(
                (detected_at for next_kind, detected_at in events[index + 1:] if next_kind == "watering"),
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
                    {
                        "date": day_key,
                        "watering_count": 0,
                        "drying_level": 0,
                        "final_moisture_percent": final_moisture_by_day.get(day_key),
                    },
                )
                if not item["watering_count"]:
                    item["drying_level"] = max(item["drying_level"], drying_level)
                if moisture is not None:
                    previous_moisture = moisture
                day += timedelta(days=1)

        return [activity[day] for day in sorted(activity)]

    def watering_interval_summary(self, sensor_id: str) -> Dict[str, Any]:
        with self._condition:
            rows = self._database.execute(
                """
                SELECT DISTINCT substr(detected_at, 1, 10)
                FROM care_events
                WHERE sensor_id = ? AND kind = 'watering'
                ORDER BY detected_at
                """,
                (sensor_id,),
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
        with self._condition:
            reading_range = self._database.execute(
                """
                SELECT MIN(COALESCE(observed_at, received_at)),
                       MAX(COALESCE(observed_at, received_at))
                FROM sensor_readings
                WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            event_rows = self._database.execute(
                """
                SELECT kind, detected_at
                FROM care_events
                WHERE sensor_id = ?
                  AND kind IN ('watering', 'watering_due', 'fertilizing')
                ORDER BY detected_at
                """,
                (sensor_id,),
            ).fetchall()

        first_at, latest_at = reading_range
        monitored_days = 0
        latest = None
        if first_at and latest_at:
            first = datetime.fromisoformat(first_at.replace("Z", "+00:00"))
            latest = datetime.fromisoformat(latest_at.replace("Z", "+00:00"))
            monitored_days = (latest.date() - first.date()).days + 1

        events = [
            (kind, datetime.fromisoformat(detected_at.replace("Z", "+00:00")))
            for kind, detected_at in event_rows
        ]
        missed_waterings = 0
        for index, (kind, due_at) in enumerate(events):
            if kind != "watering_due":
                continue
            next_watering = next(
                (event_at for event_kind, event_at in events[index + 1:] if event_kind == "watering"),
                None,
            )
            if next_watering is not None:
                missed_waterings += int(next_watering > due_at + timedelta(hours=24))
            elif latest is not None:
                missed_waterings += int(latest > due_at + timedelta(hours=24))

        return {
            "started_at": first_at,
            "monitored_days": monitored_days,
            "watering_count": sum(kind == "watering" for kind, _ in events),
            "fertilizing_count": sum(kind == "fertilizing" for kind, _ in events),
            "missed_watering_count": missed_waterings,
        }

    def wait_for_reading(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: bool(self._history), timeout=timeout)

    def close(self) -> None:
        with self._condition:
            self._database.close()

    def _save_event(self, event: CareEvent) -> None:
        self._database.execute(
            """
            INSERT OR IGNORE INTO care_events
            (event_id, sensor_id, kind, detected_at, title, summary, confidence, changes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.sensor_id,
                event.kind,
                event.detected_at,
                event.title,
                event.summary,
                event.confidence,
                json.dumps(event.changes, separators=(",", ":")),
            ),
        )
        self._database.commit()

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
        }

    def _decorate_sensor(self, sensor: Dict[str, Any]) -> Dict[str, Any]:
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
        sensor["reading_count"] = int(
            self._database.execute(
                "SELECT COUNT(*) FROM sensor_readings WHERE sensor_id = ?",
                (sensor["sensor_id"],),
            ).fetchone()[0]
        )
        return sensor

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