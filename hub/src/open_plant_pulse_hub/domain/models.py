from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    sequence: int
    observed_at: str
    soil_temperature_c: Optional[float]
    moisture_percent: Optional[float]
    conductivity_us_cm: Optional[int]
    air_temperature_c: Optional[float] = None
    air_humidity_percent: Optional[float] = None
    soil_ph: Optional[float] = None
    nitrogen_mg_kg: Optional[int] = None
    phosphorus_mg_kg: Optional[int] = None
    potassium_mg_kg: Optional[int] = None
    soil_source_status: str = "available"
    air_source_status: str = "available"
    contract_version: int = 0
