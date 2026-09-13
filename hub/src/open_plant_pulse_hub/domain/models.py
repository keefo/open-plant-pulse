from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    sequence: int
    observed_at: str
    soil_temperature_c: float
    moisture_percent: float
    conductivity_us_cm: int
    air_temperature_c: Optional[float] = None
    air_humidity_percent: Optional[float] = None
    soil_ph: Optional[float] = None
    nitrogen_mg_kg: Optional[int] = None
    phosphorus_mg_kg: Optional[int] = None
    potassium_mg_kg: Optional[int] = None