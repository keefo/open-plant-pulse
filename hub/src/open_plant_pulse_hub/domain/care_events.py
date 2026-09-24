from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from .models import SensorReading


WATERING_RISE_PERCENT = 15.0
DRAINAGE_MIN_OBSERVATION = timedelta(minutes=10)
DRAINAGE_STABLE_SAMPLES = 3
DRAINAGE_STABLE_RANGE_PERCENT = 1.5
WATERING_REARM_MARGIN_PERCENT = 5.0
DRY_ALERT_REARM_MARGIN_PERCENT = 3.0
FERTILIZER_EC_RISE_US_CM = 250
FERTILIZER_NUTRIENT_RISES = {
    "nitrogen_mg_kg": 15,
    "phosphorus_mg_kg": 10,
    "potassium_mg_kg": 20,
}
EVENT_COOLDOWN = timedelta(minutes=30)


@dataclass(frozen=True)
class CareEvent:
    event_id: str
    sensor_id: str
    kind: str
    detected_at: str
    title: str
    summary: str
    confidence: str
    changes: Dict[str, Any]


@dataclass
class DrainageObservation:
    baseline: float
    peak: float
    started_at: datetime
    recent: List[float]


class CareEventDetector:
    def __init__(self) -> None:
        self._previous: Dict[str, SensorReading] = {}
        self._last_event_at: Dict[Tuple[str, str], datetime] = {}
        self._drainage_observations: Dict[str, DrainageObservation] = {}
        self._watering_rearm_below: Dict[str, float] = {}
        self._dry_alerted: Set[str] = set()

    def forget_sensor(self, sensor_id: str) -> None:
        self._previous.pop(sensor_id, None)
        self._drainage_observations.pop(sensor_id, None)
        self._watering_rearm_below.pop(sensor_id, None)
        self._dry_alerted.discard(sensor_id)
        self._last_event_at = {
            key: detected_at
            for key, detected_at in self._last_event_at.items()
            if key[0] != sensor_id
        }

    def detect(
        self,
        reading: SensorReading,
        detected_at: datetime,
        refill_below: float = 35.0,
    ) -> List[CareEvent]:
        if reading.moisture_percent is None:
            return []
        previous = self._previous.get(reading.sensor_id)
        self._previous[reading.sensor_id] = reading
        if (
            previous is None
            or previous.moisture_percent is None
            or (
                reading.contract_version != 2
                and reading.sequence <= previous.sequence
            )
        ):
            return []

        events: List[CareEvent] = []
        if reading.moisture_percent >= refill_below + DRY_ALERT_REARM_MARGIN_PERCENT:
            self._dry_alerted.discard(reading.sensor_id)
        if (
            previous.moisture_percent > refill_below
            and reading.moisture_percent <= refill_below
            and reading.sensor_id not in self._dry_alerted
        ):
            self._dry_alerted.add(reading.sensor_id)
            events.append(
                self._event(
                    reading,
                    detected_at,
                    "watering_due",
                    "Watering recommended",
                    f"Soil moisture crossed below the {refill_below:.0f}% refill marker.",
                    "high",
                    {
                        "moisture_percent": round(reading.moisture_percent, 2),
                        "refill_below_percent": round(refill_below, 2),
                    },
                )
            )

        rearm_below = self._watering_rearm_below.get(reading.sensor_id)
        if rearm_below is not None and reading.moisture_percent <= rearm_below:
            self._last_event_at.pop((reading.sensor_id, "watering"), None)
            del self._watering_rearm_below[reading.sensor_id]

        moisture_rise = reading.moisture_percent - previous.moisture_percent
        if moisture_rise >= WATERING_RISE_PERCENT and self._ready(reading.sensor_id, "watering", detected_at):
            self._watering_rearm_below[reading.sensor_id] = (
                previous.moisture_percent + WATERING_REARM_MARGIN_PERCENT
            )
            self._drainage_observations[reading.sensor_id] = DrainageObservation(
                baseline=previous.moisture_percent,
                peak=reading.moisture_percent,
                started_at=detected_at,
                recent=[reading.moisture_percent],
            )
            events.append(
                self._event(
                    reading,
                    detected_at,
                    "watering",
                    "Watering detected",
                    f"Soil moisture rose from {previous.moisture_percent:.1f}% to {reading.moisture_percent:.1f}%.",
                    "high",
                    {"moisture_percent": round(moisture_rise, 2)},
                )
            )

        drainage_event = self._observe_drainage(reading, detected_at, moisture_rise >= WATERING_RISE_PERCENT)
        if drainage_event is not None:
            events.append(drainage_event)

        nutrient_changes = self._nutrient_changes(previous, reading)
        conductivity_rise = (
            reading.conductivity_us_cm - previous.conductivity_us_cm
            if reading.conductivity_us_cm is not None
            and previous.conductivity_us_cm is not None
            else 0
        )
        if (
            conductivity_rise >= FERTILIZER_EC_RISE_US_CM
            and nutrient_changes
            and self._ready(reading.sensor_id, "fertilizing", detected_at)
        ):
            events.append(
                self._event(
                    reading,
                    detected_at,
                    "fertilizing",
                    "Possible fertilizing detected",
                    "Conductivity and nutrient readings rose together. Confirm whether fertilizer was applied.",
                    "medium",
                    {"conductivity_us_cm": float(conductivity_rise), **nutrient_changes},
                )
            )
        return events

    def _observe_drainage(
        self, reading: SensorReading, detected_at: datetime, watering_started: bool
    ) -> Optional[CareEvent]:
        observation = self._drainage_observations.get(reading.sensor_id)
        if observation is None or watering_started:
            return None

        observation.peak = max(observation.peak, reading.moisture_percent)
        observation.recent.append(reading.moisture_percent)
        observation.recent = observation.recent[-DRAINAGE_STABLE_SAMPLES:]
        if detected_at - observation.started_at < DRAINAGE_MIN_OBSERVATION:
            return None
        if len(observation.recent) < DRAINAGE_STABLE_SAMPLES:
            return None
        if max(observation.recent) - min(observation.recent) > DRAINAGE_STABLE_RANGE_PERCENT:
            return None

        settled = sum(observation.recent) / len(observation.recent)
        rise = observation.peak - observation.baseline
        drain_drop = max(0.0, observation.peak - settled)
        retained_fraction = max(0.0, min(1.0, (settled - observation.baseline) / rise))
        response_class = self._drainage_response_class(retained_fraction)
        settle_minutes = (detected_at - observation.started_at).total_seconds() / 60
        del self._drainage_observations[reading.sensor_id]
        return self._event(
            reading,
            detected_at,
            "drainage_assessment",
            f"Pot response: {response_class}",
            "Post-watering moisture stabilized; this estimates retention and redistribution, not measured runoff.",
            "medium",
            {
                "baseline_moisture_percent": round(observation.baseline, 2),
                "peak_moisture_percent": round(observation.peak, 2),
                "settled_moisture_percent": round(settled, 2),
                "drain_drop_percent": round(drain_drop, 2),
                "retained_fraction": round(retained_fraction, 3),
                "settle_minutes": round(settle_minutes, 1),
                "response_class": response_class,
            },
        )

    @staticmethod
    def _drainage_response_class(retained_fraction: float) -> str:
        if retained_fraction < 0.45:
            return "fast"
        if retained_fraction <= 0.8:
            return "balanced"
        return "retaining"

    def _ready(self, sensor_id: str, kind: str, detected_at: datetime) -> bool:
        key = (sensor_id, kind)
        previous_at = self._last_event_at.get(key)
        if previous_at is not None and detected_at - previous_at < EVENT_COOLDOWN:
            return False
        self._last_event_at[key] = detected_at
        return True

    @staticmethod
    def _nutrient_changes(previous: SensorReading, reading: SensorReading) -> Dict[str, float]:
        changes: Dict[str, float] = {}
        for field, minimum_rise in FERTILIZER_NUTRIENT_RISES.items():
            old_value: Optional[int] = getattr(previous, field)
            new_value: Optional[int] = getattr(reading, field)
            if old_value is not None and new_value is not None and new_value - old_value >= minimum_rise:
                changes[field] = float(new_value - old_value)
        return changes

    @staticmethod
    def _event(
        reading: SensorReading,
        detected_at: datetime,
        kind: str,
        title: str,
        summary: str,
        confidence: str,
        changes: Dict[str, Any],
    ) -> CareEvent:
        return CareEvent(
            event_id=str(uuid4()),
            sensor_id=reading.sensor_id,
            kind=kind,
            detected_at=detected_at.isoformat().replace("+00:00", "Z"),
            title=title,
            summary=summary,
            confidence=confidence,
            changes=changes,
        )