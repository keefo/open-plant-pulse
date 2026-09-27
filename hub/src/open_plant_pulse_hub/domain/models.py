from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    # The sensor's own number for this report. Null only for readings stored
    # before contract v3, which had no report IDs.
    report_id: Optional[int]
    # When the sensor took the measurements, from its clock. Null when the sensor
    # did not know the time; when it was received is recorded separately and is
    # never put here in its place.
    observed_at: Optional[str]
    soil_temperature_c: Optional[float]
    moisture_percent: Optional[float]
    conductivity_us_cm: Optional[int]
    air_temperature_c: Optional[float] = None
    air_humidity_percent: Optional[float] = None
    soil_ph: Optional[float] = None
    nitrogen_mg_kg: Optional[int] = None
    phosphorus_mg_kg: Optional[int] = None
    potassium_mg_kg: Optional[int] = None
    battery_percent: Optional[int] = None
    battery_voltage_v: Optional[float] = None
    # Whether current was flowing into the pack. Null when the report carried no
    # battery, and for every reading stored before the sensor said.
    battery_charging: Optional[bool] = None
    soil_source_status: str = "available"
    air_source_status: str = "available"
    contract_version: int = 0


@dataclass(frozen=True)
class ReportPacket2:
    """The part of a report that travels in packet 2.

    It belongs to the reading with the same sensor and report ID, and may arrive
    before or after that reading's packet 1.
    """

    sensor_id: str
    report_id: int
    battery_percent: Optional[int] = None
    battery_voltage_v: Optional[float] = None
    battery_charging: Optional[bool] = None
    soil_ph: Optional[float] = None
    nitrogen_mg_kg: Optional[int] = None
    phosphorus_mg_kg: Optional[int] = None
    potassium_mg_kg: Optional[int] = None
    contract_version: int = 0
