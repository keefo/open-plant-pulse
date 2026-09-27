from datetime import datetime, timezone
import re
from typing import Dict, Optional, Tuple, Union

from open_plant_pulse_hub.domain import ReportSupplement, SensorReading


CONTRACT_VERSION = 3
BTHOME_V2_UNENCRYPTED = 0x40
BTHOME_SERVICE_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
SENSOR_NAME = re.compile(r"^sensor-([0-9a-f]{12})$")
# Object ID to (size, signed). Soil extras are variable length and handled
# separately; everything else has a fixed size.
OBJECT_FORMATS: Dict[int, Tuple[int, bool]] = {
    0x01: (1, False),
    0x02: (2, True),
    0x0C: (2, False),
    0x16: (1, False),
    0x2E: (1, False),
    0x2F: (1, False),
    0x3E: (4, False),
    0x45: (2, True),
    0x50: (4, False),
    0x56: (2, False),
}
REPORT_ID_OBJECT = 0x3E
SOIL_EXTRAS_OBJECT = 0x54
SOIL_EXTRAS_LENGTH = 8
SOIL_EXTRAS_LAYOUT_VERSION = 1
MAIN_OBJECTS = frozenset({0x02, 0x2E, 0x2F, 0x45, 0x50, 0x56})
SOIL_OBJECTS = frozenset({0x02, 0x2F, 0x56})
AIR_OBJECTS = frozenset({0x2E, 0x45})
BATTERY_OBJECTS = frozenset({0x01, 0x0C, 0x16})
# The sensor only sends a timestamp its clock can vouch for, inside this window.
# Anything outside it is not a time the sensor would have sent.
EARLIEST_TIMESTAMP = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
LATEST_TIMESTAMP = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()) - 1


def sensor_id_from_local_name(local_name: Optional[str]) -> str:
    """Return a portable identity from the sensor-owned BLE local name."""
    match = SENSOR_NAME.fullmatch(local_name or "")
    if match is None:
        raise ValueError("sensor local name must be sensor- followed by 12 lowercase hex digits")
    return f"sensor-{match.group(1)}"


def is_beacon(service_data: bytes) -> bool:
    """Return whether this is a beacon: device info and nothing else.

    A sensor with no report to send beacons so that it can still be found and
    adopted. It says the sensor exists and is in range, and nothing more.
    """
    return service_data == bytes((BTHOME_V2_UNENCRYPTED,))


def decode_service_data(
    service_data: bytes, sensor_id: str
) -> Union[SensorReading, ReportSupplement]:
    """Decode one contract-v3 report packet, the service data after UUID 0xFCD2.

    A main packet becomes a reading and a supplementary packet becomes the
    supplement for the reading with the same report ID. Anything else, beacons
    included, is refused.
    """
    if not service_data:
        raise ValueError("BTHome service data is empty")
    if service_data[0] != BTHOME_V2_UNENCRYPTED:
        raise ValueError("expected unencrypted, regular BTHome v2 device info")

    values: Dict[int, int] = {}
    soil_extras = b""
    offset = 1
    previous_object_id = -1
    while offset < len(service_data):
        object_id = service_data[offset]
        offset += 1
        if object_id not in OBJECT_FORMATS and object_id != SOIL_EXTRAS_OBJECT:
            raise ValueError(f"unsupported BTHome object 0x{object_id:02x}")
        if object_id <= previous_object_id:
            raise ValueError("BTHome objects must be unique and in ascending order")
        previous_object_id = object_id
        if object_id == SOIL_EXTRAS_OBJECT:
            if offset >= len(service_data):
                raise ValueError("truncated BTHome object 0x54")
            if service_data[offset] != SOIL_EXTRAS_LENGTH:
                raise ValueError("soil extras must be 8 bytes long")
            offset += 1
            if offset + SOIL_EXTRAS_LENGTH > len(service_data):
                raise ValueError("truncated BTHome object 0x54")
            soil_extras = service_data[offset : offset + SOIL_EXTRAS_LENGTH]
            values[object_id] = 0
            offset += SOIL_EXTRAS_LENGTH
            continue
        size, signed = OBJECT_FORMATS[object_id]
        if offset + size > len(service_data):
            raise ValueError(f"truncated BTHome object 0x{object_id:02x}")
        values[object_id] = int.from_bytes(
            service_data[offset : offset + size], "little", signed=signed
        )
        offset += size

    if REPORT_ID_OBJECT not in values:
        raise ValueError("a report packet requires report ID object 0x3e")
    report_id = values[REPORT_ID_OBJECT]
    if report_id == 0:
        raise ValueError("report ID 0 is never used")
    present = set(values)
    # Every object is either main, supplementary or the report ID, so a packet
    # with no main object is supplementary, the report ID alone included: the
    # sensor sends one with every report so that the hub knows it is complete.
    if not present & MAIN_OBJECTS:
        return _supplement(values, soil_extras, sensor_id, report_id)
    if present - MAIN_OBJECTS - {REPORT_ID_OBJECT}:
        raise ValueError("a packet cannot mix main and supplementary objects")
    return _main_reading(values, sensor_id, report_id)


def _main_reading(values: Dict[int, int], sensor_id: str, report_id: int) -> SensorReading:
    present = set(values)
    if present & SOIL_OBJECTS not in (set(), SOIL_OBJECTS):
        raise ValueError("soil source must be complete or omitted")
    if present & AIR_OBJECTS not in (set(), AIR_OBJECTS):
        raise ValueError("air source must be complete or omitted")
    soil_available = SOIL_OBJECTS <= present
    air_available = AIR_OBJECTS <= present
    if not soil_available and not air_available:
        raise ValueError("advertisement contains no supported measurement source")
    if soil_available and not 0 <= values[0x2F] <= 100:
        raise ValueError("soil moisture must be between 0 and 100 percent")
    if air_available and not 0 <= values[0x2E] <= 100:
        raise ValueError("air humidity must be between 0 and 100 percent")
    observed_at = None
    if 0x50 in values:
        if not EARLIEST_TIMESTAMP <= values[0x50] <= LATEST_TIMESTAMP:
            raise ValueError("acquisition timestamp is outside the plausible window")
        observed_at = (
            datetime.fromtimestamp(values[0x50], timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    return SensorReading(
        sensor_id=sensor_id,
        report_id=report_id,
        observed_at=observed_at,
        soil_temperature_c=round(values[0x02] * 0.01, 2) if soil_available else None,
        moisture_percent=float(values[0x2F]) if soil_available else None,
        conductivity_us_cm=values[0x56] if soil_available else None,
        air_temperature_c=round(values[0x45] * 0.1, 1) if air_available else None,
        air_humidity_percent=float(values[0x2E]) if air_available else None,
        soil_source_status="available" if soil_available else "unavailable",
        air_source_status="available" if air_available else "unavailable",
        contract_version=CONTRACT_VERSION,
    )


def _supplement(
    values: Dict[int, int], soil_extras: bytes, sensor_id: str, report_id: int
) -> ReportSupplement:
    present = set(values)
    if present & BATTERY_OBJECTS not in (set(), BATTERY_OBJECTS):
        raise ValueError("battery must carry level, voltage and charging together or none")
    has_battery = BATTERY_OBJECTS <= present
    if has_battery and not 0 <= values[0x01] <= 100:
        raise ValueError("battery level must be between 0 and 100 percent")
    if has_battery and values[0x16] not in (0, 1):
        raise ValueError("battery charging must be 0 or 1")
    soil_ph = nitrogen = phosphorus = potassium = None
    if soil_extras:
        if soil_extras[0] != SOIL_EXTRAS_LAYOUT_VERSION:
            raise ValueError(f"soil extras layout {soil_extras[0]} is unsupported")
        if soil_extras[1] > 140:
            raise ValueError("soil pH must be between 0 and 14")
        soil_ph = round(soil_extras[1] * 0.1, 1)
        nitrogen = int.from_bytes(soil_extras[2:4], "little")
        phosphorus = int.from_bytes(soil_extras[4:6], "little")
        potassium = int.from_bytes(soil_extras[6:8], "little")

    return ReportSupplement(
        sensor_id=sensor_id,
        report_id=report_id,
        battery_percent=values[0x01] if has_battery else None,
        battery_voltage_v=round(values[0x0C] * 0.001, 3) if has_battery else None,
        battery_charging=values[0x16] == 1 if has_battery else None,
        soil_ph=soil_ph,
        nitrogen_mg_kg=nitrogen,
        phosphorus_mg_kg=phosphorus,
        potassium_mg_kg=potassium,
        contract_version=CONTRACT_VERSION,
    )
