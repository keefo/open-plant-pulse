import json
from datetime import datetime
from typing import Any, Dict

from open_plant_pulse_hub.domain import SensorReading


SIMULATION_SCHEMA = "open-plant-pulse.simulation.v1"


def decode_simulation_datagram(datagram: bytes) -> SensorReading:
    """Decode one development-only simulated sensor datagram."""
    try:
        envelope: Dict[str, Any] = json.loads(datagram.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("simulation datagram must be UTF-8 JSON") from error

    if envelope.get("schema") != SIMULATION_SCHEMA:
        raise ValueError("unsupported simulation schema")

    reading = envelope.get("reading")
    if not isinstance(reading, dict):
        raise ValueError("simulation datagram requires a reading object")

    required = {
        "sensor_id",
        "sequence",
        "observed_at",
        "soil_temperature_c",
        "moisture_percent",
        "conductivity_us_cm",
        "air_temperature_c",
        "air_humidity_percent",
        "soil_ph",
        "nitrogen_mg_kg",
        "phosphorus_mg_kg",
        "potassium_mg_kg",
    }
    missing = required.difference(reading)
    if missing:
        raise ValueError(f"simulation reading missing fields: {', '.join(sorted(missing))}")

    result = SensorReading(**{key: reading[key] for key in required})
    if not result.sensor_id or result.sequence < 0:
        raise ValueError("invalid simulated sensor identity or sequence")
    if not 0 <= result.moisture_percent <= 100:
        raise ValueError("soil moisture must be between 0 and 100 percent")
    if result.air_humidity_percent is None or not 0 <= result.air_humidity_percent <= 100:
        raise ValueError("air humidity must be between 0 and 100 percent")
    if result.soil_ph is None or not 0 <= result.soil_ph <= 14:
        raise ValueError("soil pH must be between 0 and 14")
    try:
        observed_at = datetime.fromisoformat(result.observed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("simulated observed_at must be an ISO 8601 timestamp") from error
    if observed_at.tzinfo is None:
        raise ValueError("simulated observed_at must include a timezone")
    return result