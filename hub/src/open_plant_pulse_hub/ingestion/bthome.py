from open_plant_pulse_hub.domain import SensorReading


BTHOME_V2_UNENCRYPTED = 0x40
EXPECTED_OBJECT_IDS = (0x02, 0x14, 0x56)
SERVICE_DATA_LENGTH = 10


def decode_service_data(service_data: bytes) -> SensorReading:
    """Decode the Open Plant Pulse BTHome service data after UUID 0xFCD2."""
    if len(service_data) != SERVICE_DATA_LENGTH:
        raise ValueError(f"expected {SERVICE_DATA_LENGTH} service-data bytes")
    if service_data[0] != BTHOME_V2_UNENCRYPTED:
        raise ValueError("expected unencrypted, regular BTHome v2 device info")

    object_ids = (service_data[1], service_data[4], service_data[7])
    if object_ids != EXPECTED_OBJECT_IDS:
        raise ValueError("unexpected BTHome object order or measurement type")

    temperature_raw = int.from_bytes(service_data[2:4], "little", signed=True)
    moisture_raw = int.from_bytes(service_data[5:7], "little", signed=False)
    conductivity = int.from_bytes(service_data[8:10], "little", signed=False)
    return SensorReading(
        sensor_id="unenrolled-bthome",
        sequence=0,
        observed_at="",
        soil_temperature_c=temperature_raw * 0.01,
        moisture_percent=moisture_raw * 0.01,
        conductivity_us_cm=conductivity,
    )