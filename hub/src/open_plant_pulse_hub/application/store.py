from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
from threading import Condition
from typing import Any, Deque, Dict, List, Optional, Tuple

from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.domain.care_events import CareEvent, CareEventDetector
from open_plant_pulse_hub.domain.plant_profiles import load_plant_profiles


DATABASE_SCHEMA_VERSION = 1


class ReadingStore:
    def __init__(self, history_size: int = 180, database_path: Optional[str] = None) -> None:
        self._condition = Condition()
        self._history: Deque[Tuple[SensorReading, str]] = deque(maxlen=history_size)
        self._event_detector = CareEventDetector()
        catalog = load_plant_profiles()
        default_profile = catalog["profiles"][catalog["default_profile"]]
        self._default_refill_below = float(default_profile["watering"]["refill_below"])
        self._sensor_refill_below: Dict[str, float] = {}
        if database_path and database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._database = sqlite3.connect(database_path or ":memory:", check_same_thread=False)
        if database_path and database_path != ":memory:":
            self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute("PRAGMA synchronous=NORMAL")
        schema_version = self._database.execute("PRAGMA user_version").fetchone()[0]
        if schema_version > DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {schema_version} is newer than supported version {DATABASE_SCHEMA_VERSION}"
            )
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS care_events (
                event_id TEXT PRIMARY KEY,
                sensor_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                confidence TEXT NOT NULL,
                changes_json TEXT NOT NULL
            )
            """
        )
        self._database.execute(
            """
            CREATE INDEX IF NOT EXISTS care_events_by_sensor_kind_time
            ON care_events(sensor_id, kind, detected_at)
            """
        )
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_readings (
                reading_id INTEGER PRIMARY KEY,
                sensor_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                soil_temperature_c REAL NOT NULL,
                moisture_percent REAL NOT NULL,
                conductivity_us_cm INTEGER NOT NULL,
                air_temperature_c REAL,
                air_humidity_percent REAL,
                soil_ph REAL,
                nitrogen_mg_kg INTEGER,
                phosphorus_mg_kg INTEGER,
                potassium_mg_kg INTEGER,
                UNIQUE(sensor_id, sequence, observed_at)
            )
            """
        )
        self._database.execute(
            """
            CREATE INDEX IF NOT EXISTS sensor_readings_by_sensor_time
            ON sensor_readings(sensor_id, observed_at DESC)
            """
        )
        self._database.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")
        self._database.commit()
        self._restore_history(history_size)

    def add(self, reading: SensorReading) -> None:
        received_datetime = datetime.now(timezone.utc)
        received_at = received_datetime.isoformat().replace("+00:00", "Z")
        observed_datetime = self._observation_datetime(reading.observed_at, received_datetime)
        with self._condition:
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

    def set_sensor_profile(self, sensor_id: str, profile_id: str) -> Dict[str, Any]:
        catalog = load_plant_profiles()
        profile = catalog["profiles"].get(profile_id)
        if not sensor_id or profile is None:
            raise ValueError("sensor_id and a valid profile_id are required")
        refill_below = float(profile["watering"]["refill_below"])
        with self._condition:
            self._sensor_refill_below[sensor_id] = refill_below
        return {
            "sensor_id": sensor_id,
            "profile_id": profile_id,
            "refill_below": refill_below,
        }

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._condition:
            if not self._history:
                return None
            return self._serialize(*self._history[-1])

    def history(self) -> List[Dict[str, Any]]:
        with self._condition:
            return [self._serialize(reading, received_at) for reading, received_at in self._history]

    def care_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self._condition:
            rows = self._database.execute(
                """
                SELECT event_id, sensor_id, kind, detected_at, title, summary, confidence, changes_json
                FROM care_events
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (safe_limit,),
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
                SELECT daily.date, daily.minimum_moisture, readings.moisture_percent
                FROM (
                    SELECT substr(observed_at, 1, 10) AS date,
                           MIN(moisture_percent) AS minimum_moisture,
                           MAX(observed_at) AS final_observed_at
                    FROM sensor_readings
                    WHERE sensor_id = ? AND observed_at >= ? AND observed_at < ?
                    GROUP BY substr(observed_at, 1, 10)
                ) AS daily
                JOIN sensor_readings AS readings
                  ON readings.sensor_id = ? AND readings.observed_at = daily.final_observed_at
                """,
                (
                    sensor_id,
                    (start - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    end_at,
                    sensor_id,
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
        cursor = self._database.execute(
            """
            INSERT OR IGNORE INTO sensor_readings (
                sensor_id, sequence, observed_at, received_at,
                soil_temperature_c, moisture_percent, conductivity_us_cm,
                air_temperature_c, air_humidity_percent, soil_ph,
                nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
        )
        self._database.commit()
        return cursor.rowcount > 0

    def _restore_history(self, history_size: int) -> None:
        rows = self._database.execute(
            """
            SELECT sensor_id, sequence, observed_at, soil_temperature_c,
                   moisture_percent, conductivity_us_cm, air_temperature_c,
                   air_humidity_percent, soil_ph, nitrogen_mg_kg,
                   phosphorus_mg_kg, potassium_mg_kg, received_at
            FROM sensor_readings
            ORDER BY reading_id DESC
            LIMIT ?
            """,
            (history_size,),
        ).fetchall()
        for row in reversed(rows):
            reading = SensorReading(
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
            )
            self._history.append((reading, row[12]))

    @staticmethod
    def _serialize(reading: SensorReading, received_at: str) -> Dict[str, Any]:
        return {"reading": asdict(reading), "received_at": received_at}

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