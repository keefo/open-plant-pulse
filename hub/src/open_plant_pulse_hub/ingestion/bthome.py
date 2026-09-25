import re
from typing import Dict, Optional, Tuple

from open_plant_pulse_hub.domain import SensorReading


BTHOME_V2_UNENCRYPTED = 0x40
BTHOME_SERVICE_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
SENSOR_NAME = re.compile(r"^sensor-([0-9a-f]{12})$")
LEGACY_SENSOR_NAME = re.compile(r"^sensor-([0-9A-F]{12})$")
LEGACY_OPEN_PLANT_PULSE_NAME = re.compile(r"^OPP-([0-9A-F]{12})$")
LEGACY_OBJECT_IDS = (0x02, 0x14, 0x56)
LEGACY_SERVICE_DATA_LENGTH = 10
OBJECT_FORMATS: Dict[int, Tuple[int, bool, float]] = {
    0x00: (1, False, 1.0),
    0x02: (2, True, 0.01),
    0x03: (2, False, 0.01),
    0x14: (2, False, 0.01),
    0x3A: (1, False, 1.0),
    0x45: (2, True, 0.1),
    0x56: (2, False, 1.0),
}


def sensor_id_from_local_name(local_name: Optional[str]) -> str:
    """Return a portable identity from the sensor-owned BLE local name."""
    name = local_name or ""
    match = (
        SENSOR_NAME.fullmatch(name)
        or LEGACY_SENSOR_NAME.fullmatch(name)
        or LEGACY_OPEN_PLANT_PULSE_NAME.fullmatch(name)
    )
    if match is None:
        raise ValueError("sensor local name must be sensor- followed by 12 lowercase hex digits")
    return f"sensor-{match.group(1).lower()}"


def decode_beacon_packet_id(service_data: bytes) -> Optional[int]:
    """Return the packet ID when this is an onboarding beacon, else None.

    A beacon is exactly device info plus a packet ID and nothing else. An
    unclaimed sensor sends one when it has no reading, so that a sensor whose
    probe is absent or broken can still be found and adopted rather than being
    invisible. The shape is checked strictly: anything carrying a partial
    measurement source is a malformed reading, not a beacon, and must still be
    rejected as one.
    """
    if len(service_data) != 3:
        return None
    if service_data[0] != BTHOME_V2_UNENCRYPTED or service_data[1] != 0x00:
        return None
    return int(service_data[2])


def decode_service_data(
    service_data: bytes, sensor_id: str = "unenrolled-bthome"
) -> SensorReading:
    """Decode the Open Plant Pulse BTHome service data after UUID 0xFCD2."""
    if not service_data:
        raise ValueError("BTHome service data is empty")
    if service_data[0] != BTHOME_V2_UNENCRYPTED:
        raise ValueError("expected unencrypted, regular BTHome v2 device info")

    if sensor_id == "unenrolled-bthome":
        return _decode_legacy(service_data, sensor_id)

    values: Dict[int, float] = {}
    offset = 1
    previous_object_id = -1
    while offset < len(service_data):
        object_id = service_data[offset]
        offset += 1
        if object_id not in OBJECT_FORMATS:
            raise ValueError(f"unsupported BTHome object 0x{object_id:02x}")
        if object_id <= previous_object_id:
            raise ValueError("BTHome objects must be unique and in ascending order")
        size, signed, factor = OBJECT_FORMATS[object_id]
        if offset + size > len(service_data):
            raise ValueError(f"truncated BTHome object 0x{object_id:02x}")
        raw = int.from_bytes(service_data[offset : offset + size], "little", signed=signed)
        values[object_id] = round(raw * factor, 2)
        previous_object_id = object_id
        offset += size

    if 0x00 not in values:
        raise ValueError("contract version 2 requires BTHome packet ID 0x00")
    soil_objects = {0x02, 0x14, 0x56}
    air_objects = {0x03, 0x45}
    present = set(values)
    if present.intersection(soil_objects) not in (set(), soil_objects):
        raise ValueError("soil source must be complete or omitted")
    if present.intersection(air_objects) not in (set(), air_objects):
        raise ValueError("air source must be complete or omitted")
    soil_available = soil_objects.issubset(present)
    air_available = air_objects.issubset(present)
    if not soil_available and not air_available:
        raise ValueError("advertisement contains no supported measurement source")
    if soil_available and not 0 <= values[0x14] <= 100:
        raise ValueError("soil moisture must be between 0 and 100 percent")
    if air_available and not 0 <= values[0x03] <= 100:
        raise ValueError("air humidity must be between 0 and 100 percent")
    if 0x3A in values and values[0x3A] != 1:
        raise ValueError("button event must be a press")

    return SensorReading(
        sensor_id=sensor_id,
        sequence=int(values[0x00]),
        observed_at="",
        soil_temperature_c=values.get(0x02),
        moisture_percent=values.get(0x14),
        conductivity_us_cm=int(values[0x56]) if 0x56 in values else None,
        air_temperature_c=values.get(0x45),
        air_humidity_percent=values.get(0x03),
        soil_source_status="available" if soil_available else "unavailable",
        air_source_status="available" if air_available else "unavailable",
        contract_version=2,
    )


def has_force_report_event(service_data: bytes) -> bool:
    """Return whether validated contract-v2 data contains a button-press event."""
    if not service_data or service_data[0] != BTHOME_V2_UNENCRYPTED:
        return False
    offset = 1
    while offset < len(service_data):
        object_id = service_data[offset]
        offset += 1
        object_format = OBJECT_FORMATS.get(object_id)
        if object_format is None:
            return False
        size, _, _ = object_format
        if offset + size > len(service_data):
            return False
        if object_id == 0x3A:
            return size == 1 and service_data[offset] == 1
        offset += size
    return False


def _decode_legacy(service_data: bytes, sensor_id: str) -> SensorReading:
    """Decode the preserved contract-v1 soil-only fixture."""
    if len(service_data) != LEGACY_SERVICE_DATA_LENGTH:
        raise ValueError(f"expected {LEGACY_SERVICE_DATA_LENGTH} service-data bytes")

    object_ids = (service_data[1], service_data[4], service_data[7])
    if object_ids != LEGACY_OBJECT_IDS:
        raise ValueError("unexpected BTHome object order or measurement type")

    temperature_raw = int.from_bytes(service_data[2:4], "little", signed=True)
    moisture_raw = int.from_bytes(service_data[5:7], "little", signed=False)
    conductivity = int.from_bytes(service_data[8:10], "little", signed=False)
    return SensorReading(
        sensor_id=sensor_id,
        sequence=0,
        observed_at="",
        soil_temperature_c=temperature_raw * 0.01,
        moisture_percent=moisture_raw * 0.01,
        conductivity_us_cm=conductivity,
        air_source_status="unavailable",
        contract_version=1,
    )