from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
from threading import Condition
from typing import Any, Deque, Dict, List, Optional, Tuple

from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.domain.care_events import CareEvent, CareEventDetector
from open_plant_pulse_hub.domain.plant_profiles import load_plant_profiles

from .migrations import migrate_database


RECEIVE_DIAGNOSTIC_LIMIT = 1000
RAW_REPORT_LOG_LIMIT = 50
DEVICE_CONFIG_TEXT_MAX_BYTES = 80
MIN_REPORTING_INTERVAL_MINUTES = 5
MAX_REPORTING_INTERVAL_MINUTES = 1440
MIN_REPORTING_INTERVAL_SECONDS = 1
MAX_REPORTING_INTERVAL_SECONDS = 86400
ONBOARDING_STATES = ("onboarding", "onboarded")
WIFI_STATES = ("off", "pending", "joined", "failed")
# Every reason a sensor can give for failing to join, so the browser never has to
# show "it did not work" without saying what went wrong.
WIFI_FAILURES = (
    "wrong_password",
    "network_not_found",
    "association_timeout",
    "no_address",
    "unsupported_band",
)


class ReadingStore:
    def __init__(self, history_size: int = 180, database_path: Optional[str] = None) -> None:
        self._condition = Condition()
        self._history_size = history_size
        self._history: Deque[Tuple[SensorReading, str]] = deque(maxlen=history_size)
        self._event_detector = CareEventDetector()
        catalog = load_plant_profiles()
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
        received_datetime = datetime.now(timezone.utc)
        received_at = received_datetime.isoformat().replace("+00:00", "Z")
        observed_datetime = self._observation_datetime(reading.observed_at, received_datetime)
        with self._condition:
            self._ensure_sensor(
                reading,
                received_at,
                transport="simulation",
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
    ) -> bool:
        """Persist one decoded BLE advertisement and reading atomically."""
        received_datetime = self._observation_datetime(received_at, datetime.now(timezone.utc))
        payload_sha256 = hashlib.sha256(service_data).hexdigest()
        with self._condition:
            with self._database:
                self._ensure_sensor(
                    reading,
                    received_at,
                    transport="bthome",
                    identity_kind="device-local-name",
                    rssi=rssi,
                    commit=False,
                )
                previous = self._database.execute(
                    """
                    SELECT packet_id
                    FROM advertisements
                    WHERE sensor_id = ? AND decode_status = 'accepted'
                    ORDER BY advertisement_id DESC
                    LIMIT 1
                    """,
                    (reading.sensor_id,),
                ).fetchone()
                captured = self._database.execute(
                    """
                    SELECT 1
                    FROM advertisements
                    WHERE sensor_id = ? AND packet_id = ? AND received_at = ?
                      AND payload_sha256 = ?
                    LIMIT 1
                    """,
                    (
                        reading.sensor_id,
                        reading.sequence,
                        received_at,
                        payload_sha256,
                    ),
                ).fetchone()
                duplicate = captured is not None or (
                    previous is not None and previous[0] == reading.sequence
                )
                cursor = self._database.execute(
                    """
                    INSERT INTO advertisements (
                        sensor_id, packet_id, received_at, transport, source_adapter,
                        observed_identifier, rssi, contract_version, payload_sha256,
                        decode_status, service_data
                    ) VALUES (?, ?, ?, 'bthome', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reading.sensor_id,
                        reading.sequence,
                        received_at,
                        source_adapter[:64],
                        observed_identifier[:240],
                        rssi,
                        reading.contract_version,
                        payload_sha256,
                        "duplicate" if duplicate else "accepted",
                        service_data,
                    ),
                )
                advertisement_id = int(cursor.lastrowid)
                stored = False
                if not duplicate:
                    reading_cursor = self._insert_reading(
                        reading,
                        received_at,
                        advertisement_id,
                    )
                    stored = reading_cursor.rowcount > 0
                    if not stored:
                        self._database.execute(
                            """
                            UPDATE advertisements
                            SET decode_status = 'duplicate'
                            WHERE advertisement_id = ?
                            """,
                            (advertisement_id,),
                        )
                self._trim_receive_diagnostics()
            if duplicate or not stored:
                return False

            self._history.append((reading, received_at))
            refill_below = self._sensor_refill_below.get(
                reading.sensor_id,
                self._default_refill_below,
            )
            for event in self._event_detector.detect(reading, received_datetime, refill_below):
                self._save_event(event)
            self._condition.notify_all()
            return True

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
                SELECT advertisement_id, received_at, packet_id, decode_status,
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
                "packet_id": row[2],
                "decode_status": row[3],
                "rssi": row[4],
                "source_adapter": row[5],
                "observed_identifier": row[6],
                "contract_version": row[7],
                "service_data_hex": bytes(row[8]).hex() if row[8] is not None else None,
                "payload_sha256": row[9],
                "decode_error": row[10],
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
                       wifi_enabled, wifi_state, wifi_failure, wifi_address
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
                       wifi_enabled, wifi_state, wifi_failure, wifi_address
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
                    wifi_address = NULL
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
                    wifi_enabled = CASE ? WHEN 'onboarding' THEN 0 ELSE wifi_enabled END,
                    wifi_state = CASE ? WHEN 'onboarding' THEN 'off' ELSE wifi_state END,
                    wifi_failure = CASE ? WHEN 'onboarding' THEN NULL ELSE wifi_failure END,
                    wifi_address = CASE ? WHEN 'onboarding' THEN NULL ELSE wifi_address END
                WHERE sensor_id = ?
                """,
                (onboarding_state,) * 6 + (sensor_id,),
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
    ) -> Dict[str, Any]:
        display_name = display_name.strip()
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
            raise ValueError("expected_interval_seconds must be between 1 and 86400")

        with self._condition, self._database:
            current = self._database.execute(
                """
                SELECT plant_id, display_name, room, expected_interval_seconds,
                       device_config_revision
                FROM sensors WHERE sensor_id = ?
                """,
                (sensor_id,),
            ).fetchone()
            if current is None:
                raise ValueError("sensor_id has not been observed")
            plant_id = current[0]
            device_config_changed = (
                current[1] != display_name
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
                    display_name = ?, room = ?, plant_id = ?, profile_id = ?,
                    moisture_low_percent = ?, conductivity_high_us_cm = ?,
                    expected_interval_seconds = ?, replaced_by_sensor_id = NULL,
                    device_config_revision = ?,
                    device_config_error = CASE WHEN ? THEN NULL ELSE device_config_error END
                WHERE sensor_id = ?
                """,
                (
                    display_name,
                    room or None,
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
                       display_name, COALESCE(room, '')
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
                        self._database.execute(
                            "UPDATE sensor_readings SET sensor_id = ? WHERE sensor_id = ?",
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

    def latest(self, sensor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._condition:
            if sensor_id is not None:
                row = self._database.execute(
                    """
                    SELECT sensor_id, sequence, observed_at, soil_temperature_c,
                           moisture_percent, conductivity_us_cm, air_temperature_c,
                           air_humidity_percent, soil_ph, nitrogen_mg_kg,
                           phosphorus_mg_kg, potassium_mg_kg, received_at,
                           soil_source_status, air_source_status, contract_version
                    FROM sensor_readings
                    WHERE sensor_id = ?
                    ORDER BY reading_id DESC
                    LIMIT 1
                    """,
                    (sensor_id,),
                ).fetchone()
                return self._serialize_row(row) if row is not None else None
            for reading, received_at in reversed(self._history):
                return self._serialize(reading, received_at)
            return None

    def history(self, sensor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._condition:
            if sensor_id is not None:
                rows = self._database.execute(
                    """
                    SELECT sensor_id, sequence, observed_at, soil_temperature_c,
                           moisture_percent, conductivity_us_cm, air_temperature_c,
                           air_humidity_percent, soil_ph, nitrogen_mg_kg,
                           phosphorus_mg_kg, potassium_mg_kg, received_at,
                           soil_source_status, air_source_status, contract_version
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
        """Return evenly sampled sensor history for an inclusive observation-time range."""
        safe_limit = max(2, min(max_points, 1000))
        parameters = (sensor_id, start_at, end_at)
        with self._condition:
            total = int(
                self._database.execute(
                    """
                    SELECT COUNT(*)
                    FROM sensor_readings
                    WHERE sensor_id = ?
                      AND julianday(observed_at) >= julianday(?)
                      AND julianday(observed_at) <= julianday(?)
                    """,
                    parameters,
                ).fetchone()[0]
            )
            if total == 0:
                return []

            columns = """
                sensor_id, sequence, observed_at, soil_temperature_c,
                moisture_percent, conductivity_us_cm, air_temperature_c,
                air_humidity_percent, soil_ph, nitrogen_mg_kg,
                phosphorus_mg_kg, potassium_mg_kg, received_at,
                soil_source_status, air_source_status, contract_version
            """
            if total <= safe_limit:
                rows = self._database.execute(
                    f"""
                    SELECT {columns}
                    FROM sensor_readings
                    WHERE sensor_id = ?
                      AND julianday(observed_at) >= julianday(?)
                      AND julianday(observed_at) <= julianday(?)
                    ORDER BY julianday(observed_at), reading_id
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
                                   ORDER BY julianday(observed_at), reading_id
                               ) AS sample_number
                        FROM sensor_readings
                        WHERE sensor_id = ?
                          AND julianday(observed_at) >= julianday(?)
                          AND julianday(observed_at) <= julianday(?)
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
                WITH ranked AS (
                    SELECT substr(observed_at, 1, 10) AS date,
                           moisture_percent,
                           MIN(moisture_percent) OVER (
                               PARTITION BY substr(observed_at, 1, 10)
                           ) AS minimum_moisture,
                           ROW_NUMBER() OVER (
                               PARTITION BY substr(observed_at, 1, 10)
                               ORDER BY observed_at DESC, reading_id DESC
                           ) AS recency
                    FROM sensor_readings
                    WHERE sensor_id = ? AND observed_at >= ? AND observed_at < ?
                      AND moisture_percent IS NOT NULL
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
                "SELECT MAX(observed_at) FROM sensor_readings WHERE sensor_id = ?",
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
                SELECT MIN(observed_at), MAX(observed_at)
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
                advertisement_id, sensor_id, sequence, observed_at, received_at,
                soil_temperature_c, moisture_percent, conductivity_us_cm,
                air_temperature_c, air_humidity_percent, soil_ph,
                nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg,
                soil_source_status, air_source_status, contract_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advertisement_id,
                reading.sensor_id,
                reading.sequence,
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
                reading.soil_source_status,
                reading.air_source_status,
                reading.contract_version,
            ),
        )

    def _ensure_sensor(
        self,
        reading: SensorReading,
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
                reading.sensor_id,
                identity_kind,
                reading.sensor_id,
                received_at,
                received_at,
                rssi,
                transport,
                reading.contract_version,
            ),
        )
        if commit:
            self._database.commit()

    def _trim_receive_diagnostics(self) -> None:
        self._database.execute(
            """
            DELETE FROM advertisements
            WHERE decode_status != 'accepted'
              AND advertisement_id NOT IN (
                  SELECT advertisement_id
                  FROM advertisements
                  WHERE decode_status != 'accepted'
                  ORDER BY advertisement_id DESC
                  LIMIT ?
              )
            """,
            (RECEIVE_DIAGNOSTIC_LIMIT,),
        )

    def _restore_history(self, history_size: int) -> None:
        rows = self._database.execute(
            """
            SELECT sensor_id, sequence, observed_at, soil_temperature_c,
                   moisture_percent, conductivity_us_cm, air_temperature_c,
                   air_humidity_percent, soil_ph, nitrogen_mg_kg,
                   phosphorus_mg_kg, potassium_mg_kg, received_at,
                   soil_source_status, air_source_status, contract_version
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
        sensor["freshness"] = "fresh" if age_seconds <= stale_after_seconds else "stale"
        if sensor["device_config_revision"] == 0:
            sensor["device_config_status"] = "not_configured"
        elif sensor["device_config_applied_revision"] == sensor["device_config_revision"]:
            sensor["device_config_status"] = "applied"
        elif sensor["device_config_error"]:
            sensor["device_config_status"] = "retrying"
        else:
            sensor["device_config_status"] = "pending"
        sensor["age_seconds"] = age_seconds
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
                sequence=row[1],
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
                soil_source_status=row[13],
                air_source_status=row[14],
                contract_version=row[15],
            ),
            "received_at": row[12],
        }

    @staticmethod
    def _observation_datetime(observed_at: str, fallback: datetime) -> datetime:
        if not observed_at:
            return fallback
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if observed.tzinfo is None:
            return fallback
        return observed.astimezone(timezone.utc)