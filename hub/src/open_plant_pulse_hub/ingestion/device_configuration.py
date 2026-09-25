from __future__ import annotations

from dataclasses import dataclass
import logging
import socket
from typing import Any, Callable, Optional

from open_plant_pulse_hub.application.store import ReadingStore


LOGGER = logging.getLogger(__name__)
DEVICE_CONFIG_CHARACTERISTIC_UUID = "7f510002-1b15-4c28-9a4a-8d0f4f505000"
REPORT_ACK_CHARACTERISTIC_UUID = DEVICE_CONFIG_CHARACTERISTIC_UUID
DEVICE_CONFIG_PROTOCOL_VERSION = 6
# Written to give a sensor up. The sensor forgets its configuration and network,
# leaves the household network, stops serving its console, and drops the bond.
DEVICE_RELEASE_PAYLOAD = bytes((5, 0x5A))
# Appended by the sensor to a configuration read-back: whether it is on the
# household network and the address it was given, neither of which the hub can
# see over Bluetooth.
STATION_STATUS_MARKER = 0xA1
STATION_STATUS_SIZE = 9
# Written to start an over-the-air update. The sensor downloads the image over
# the household network; this link only says which image, and from where.
FIRMWARE_UPDATE_PROTOCOL_VERSION = 7
FIRMWARE_UPDATE_PAYLOAD_SIZE = 47
# Appended by the sensor before the station status, so a hub reading the last
# nine bytes still finds the station status where it has always been.
FIRMWARE_STATUS_MARKER = 0xA2
FIRMWARE_STATUS_SIZE = 4
# What the sensor is doing, in the order it does it.
FIRMWARE_STATUS_STATES = {
    0: "idle",
    1: "commanded",
    2: "downloading",
    3: "installing",
    4: "rebooting",
    5: "failed",
}
# Why it stopped. Every reason is named, so the interface never has to show that
# an update failed without saying what failed.
FIRMWARE_STATUS_FAILURES = {
    0: None,
    1: "the sensor is not on the household network",
    2: "the sensor could not download the image",
    3: "the downloaded image did not match its digest",
    4: "the sensor could not write the image to flash",
    5: "the sensor rejected the image",
    6: "the image is too large for the sensor",
    7: "the sensor has no second slot to install into",
}
# How long a reported firmware version is trusted before the hub asks again. A
# version cached for ever keeps naming the firmware a sensor ran before it was
# reflashed, which is exactly when it changes.
STATION_REPORT_MAX_AGE_SECONDS = 900
DEVICE_CONFIG_TEXT_MAX_BYTES = 80
DEVICE_CONFIG_PAYLOAD_MAX_SIZE = 171
MIN_REPORTING_INTERVAL_SECONDS = 1
MAX_REPORTING_INTERVAL_SECONDS = 86400
REPORT_ACK_PROTOCOL_VERSION = 1
REPORT_ACK_PAYLOAD_SIZE = 6
ClientFactory = Callable[..., Any]


@dataclass(frozen=True)
class DeviceConfiguration:
    revision: int
    reporting_interval_seconds: int
    plant_name: str
    room: str
    # A setting, not a secret, so it travels with the configuration: the hub
    # must be able to switch a console off without knowing a password.
    console_enabled: bool = False


@dataclass(frozen=True)
class ReportAcknowledgement:
    request_id: int
    packet_id: int


def encode_report_acknowledgement(acknowledgement: ReportAcknowledgement) -> bytes:
    if not 1 <= acknowledgement.request_id <= 0xFFFFFFFF:
        raise ValueError("report request ID must be between 1 and 4294967295")
    if not 0 <= acknowledgement.packet_id <= 0xFF:
        raise ValueError("report packet ID must be between 0 and 255")
    return b"".join(
        (
            bytes((REPORT_ACK_PROTOCOL_VERSION,)),
            acknowledgement.request_id.to_bytes(4, "little"),
            bytes((acknowledgement.packet_id,)),
        )
    )


def decode_report_acknowledgement(payload: bytes) -> ReportAcknowledgement:
    if len(payload) != REPORT_ACK_PAYLOAD_SIZE:
        raise ValueError("report acknowledgement payload length is invalid")
    if payload[0] != REPORT_ACK_PROTOCOL_VERSION:
        raise ValueError("report acknowledgement protocol version is unsupported")
    acknowledgement = ReportAcknowledgement(
        request_id=int.from_bytes(payload[1:5], "little"),
        packet_id=payload[5],
    )
    if encode_report_acknowledgement(acknowledgement) != payload:
        raise ValueError("report acknowledgement payload is not canonical")
    return acknowledgement


def encode_device_configuration(config: DeviceConfiguration) -> bytes:
    plant_name = _encoded_text(config.plant_name, "plant_name", required=True)
    room = _encoded_text(config.room, "room", required=False)
    if not 1 <= config.revision <= 0xFFFFFFFF:
        raise ValueError("configuration revision must be between 1 and 4294967295")
    if not MIN_REPORTING_INTERVAL_SECONDS <= config.reporting_interval_seconds <= MAX_REPORTING_INTERVAL_SECONDS:
        raise ValueError("reporting interval must be between 1 and 86400 seconds")
    return b"".join(
        (
            bytes((DEVICE_CONFIG_PROTOCOL_VERSION,)),
            config.revision.to_bytes(4, "little"),
            config.reporting_interval_seconds.to_bytes(4, "little"),
            bytes((len(plant_name), len(room), 1 if config.console_enabled else 0)),
            plant_name,
            room,
        )
    )


@dataclass(frozen=True)
class FirmwareUpdateCommand:
    update_id: int
    size_bytes: int
    address: str
    port: int
    digest: str


def encode_firmware_update(command: FirmwareUpdateCommand) -> bytes:
    """Say which image to install and where the hub is serving it.

    No file name travels: the sensor builds the address from the digest, so this
    command cannot ask a sensor to fetch anything else.
    """
    if not 1 <= command.update_id <= 0xFFFFFFFF:
        raise ValueError("update ID must be between 1 and 4294967295")
    if not 1 <= command.size_bytes <= 0xFFFFFFFF:
        raise ValueError("image size must be between 1 and 4294967295 bytes")
    if not 1 <= command.port <= 0xFFFF:
        raise ValueError("hub port must be between 1 and 65535")
    octets = command.address.split(".")
    if len(octets) != 4 or not all(octet.isdigit() and 0 <= int(octet) <= 255 for octet in octets):
        raise ValueError("hub address must be an IPv4 address")
    if len(command.digest) != 64:
        raise ValueError("image digest must be 64 hexadecimal characters")
    try:
        digest = bytes.fromhex(command.digest)
    except ValueError as error:
        raise ValueError("image digest must be hexadecimal") from error
    return b"".join(
        (
            bytes((FIRMWARE_UPDATE_PROTOCOL_VERSION,)),
            command.update_id.to_bytes(4, "little"),
            command.size_bytes.to_bytes(4, "little"),
            bytes(int(octet) for octet in octets),
            command.port.to_bytes(2, "little"),
            digest,
        )
    )


def decode_firmware_update(payload: bytes) -> FirmwareUpdateCommand:
    if len(payload) != FIRMWARE_UPDATE_PAYLOAD_SIZE:
        raise ValueError("firmware update payload length is invalid")
    if payload[0] != FIRMWARE_UPDATE_PROTOCOL_VERSION:
        raise ValueError("firmware update protocol version is unsupported")
    command = FirmwareUpdateCommand(
        update_id=int.from_bytes(payload[1:5], "little"),
        size_bytes=int.from_bytes(payload[5:9], "little"),
        address=".".join(str(octet) for octet in payload[9:13]),
        port=int.from_bytes(payload[13:15], "little"),
        digest=payload[15:47].hex(),
    )
    if encode_firmware_update(command) != payload:
        raise ValueError("firmware update payload is not canonical")
    return command


def split_firmware_status(payload: bytes) -> tuple[bytes, Optional[tuple[str, int, Optional[str]]]]:
    """Separate an update report from whatever it was appended to.

    Read after the station status has been taken off the end, because it sits in
    front of it: the older suffix keeps its place so that removing this one
    changes nothing for a hub that does not know about it.
    """
    if len(payload) <= FIRMWARE_STATUS_SIZE:
        return payload, None
    suffix = payload[-FIRMWARE_STATUS_SIZE:]
    if suffix[0] != FIRMWARE_STATUS_MARKER:
        return payload, None
    state = FIRMWARE_STATUS_STATES.get(suffix[1])
    if state is None:
        return payload[:-FIRMWARE_STATUS_SIZE], None
    percent = min(100, suffix[2])
    failure = FIRMWARE_STATUS_FAILURES.get(suffix[3])
    return payload[:-FIRMWARE_STATUS_SIZE], (state, percent, failure)


def split_station_status(payload: bytes) -> tuple[bytes, Optional[tuple[bool, str, str]]]:
    """Separate a configuration read-back from the station status appended to it.

    Returned as a suffix rather than a new payload version so that a hub which
    knows nothing about it still decodes the configuration it asked for.
    """
    if len(payload) <= STATION_STATUS_SIZE:
        return payload, None
    suffix = payload[-STATION_STATUS_SIZE:]
    if suffix[0] != STATION_STATUS_MARKER:
        return payload, None
    joined = suffix[1] == 1
    address = ".".join(str(octet) for octet in suffix[2:6]) if joined else ""
    version = f"{suffix[6]}.{suffix[7]}.{suffix[8]}"
    return payload[:-STATION_STATUS_SIZE], (joined, address, version)


def decode_device_configuration(payload: bytes) -> DeviceConfiguration:
    if not 12 <= len(payload) <= DEVICE_CONFIG_PAYLOAD_MAX_SIZE:
        raise ValueError("device configuration payload length is invalid")
    if payload[0] != DEVICE_CONFIG_PROTOCOL_VERSION:
        raise ValueError("device configuration protocol version is unsupported")
    plant_name_length, room_length = payload[9], payload[10]
    console_enabled = payload[11]
    if console_enabled > 1:
        raise ValueError("console flag is invalid")
    if plant_name_length == 0 or plant_name_length > DEVICE_CONFIG_TEXT_MAX_BYTES:
        raise ValueError("plant_name length is invalid")
    if room_length > DEVICE_CONFIG_TEXT_MAX_BYTES or len(payload) != 12 + plant_name_length + room_length:
        raise ValueError("room length or payload length is invalid")
    try:
        plant_name = payload[12 : 12 + plant_name_length].decode("utf-8")
        room = payload[12 + plant_name_length :].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("device configuration text must be UTF-8") from error
    config = DeviceConfiguration(
        console_enabled=console_enabled == 1,
        revision=int.from_bytes(payload[1:5], "little"),
        reporting_interval_seconds=int.from_bytes(payload[5:9], "little"),
        plant_name=plant_name,
        room=room,
    )
    if encode_device_configuration(config) != payload:
        raise ValueError("device configuration payload is not canonical")
    return config


class DeviceConfigurationSynchronizer:
    """Deliver pending hub configuration through the sensor's bounded GATT window."""

    def __init__(
        self,
        store: ReadingStore,
        client_factory: ClientFactory | None = None,
        timeout: float = 5.0,
        firmware_port: int | None = None,
        address_resolver: Callable[[str], Optional[str]] | None = None,
    ) -> None:
        self._store = store
        self._client_factory = client_factory
        self._timeout = timeout
        self._firmware_port = firmware_port
        self._address_resolver = address_resolver

    def has_pending(self, sensor_id: str) -> bool:
        return self._store.pending_device_configuration(sensor_id) is not None

    def pending_revision(self, sensor_id: str) -> Optional[int]:
        """Which configuration revision is waiting, if any.

        The revision, not the clock, is what says whether an attempt is a fresh
        change or a repeat of one that already failed.
        """
        desired = self._store.pending_device_configuration(sensor_id)
        return None if desired is None else int(desired["revision"])

    def needs_station_status(self, sensor_id: str) -> bool:
        """Is a console waiting on an answer the sensor has not given yet?

        The sensor only reports whether it reached the network when the hub
        connects, and the hub only connects when something is pending. Without
        this, a console that joined seconds after being switched on would read
        as waiting for ever.
        """
        sensor = self._store.sensor(sensor_id)
        if sensor is None or sensor["enrollment_status"] != "enrolled":
            return False
        if sensor["wifi_enabled"] and sensor["wifi_state"] == "pending":
            return True
        # An update in flight is the one time the sensor has something to say
        # every few seconds, and the only way anybody watching sees it move.
        if sensor["firmware_update_state"] in (
            "commanded",
            "downloading",
            "installing",
            "rebooting",
        ):
            return True
        # Nothing has ever been reported, or what was reported is old enough to
        # be describing a sensor that has since been reflashed.
        age = self._store.station_report_age_seconds(sensor_id)
        return age is None or age >= STATION_REPORT_MAX_AGE_SECONDS

    async def refresh_station(self, sensor_id: str, observed_identifier: str) -> str:
        """Read the sensor's network state without changing anything."""
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier) as client:
                payload = bytes(await client.read_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID))
            _, station = split_station_status(payload)
            if station is None:
                return "unknown"
            joined, address, version = station
            # Progress first, then the version: a sensor that has come back
            # running the target image has succeeded, whatever it last said it
            # was doing, and recording the version is what establishes that.
            self._record_reported_progress(sensor_id, payload)
            self._store.record_station_report(sensor_id, version)
            sensor = self._store.sensor(sensor_id)
            if sensor and sensor["wifi_enabled"]:
                self._store.record_sensor_wifi_result(
                    sensor_id,
                    "joined" if joined else "pending",
                    None,
                    address or None,
                )
            return "joined" if joined else "pending"
        except Exception as error:  # noqa: BLE001 - the sensor may simply be away
            LOGGER.warning(
                "could not read %s network state: %s", sensor_id, str(error)[:240]
            )
            return "failed"

    def pending_firmware_update_id(self, sensor_id: str) -> Optional[int]:
        """Which update is waiting to be commanded, if any."""
        pending = self._store.pending_firmware_update(sensor_id)
        return None if pending is None else int(pending["update_id"])

    async def send_firmware_update(self, sensor_id: str, observed_identifier: str) -> str:
        """Tell a sensor which image to install, and where to fetch it.

        The image itself never crosses this link. A sensor that is not on the
        household network is told nothing, because it could not fetch anything:
        that is a precondition, not a failure to discover halfway through.
        """
        pending = self._store.pending_firmware_update(sensor_id)
        if pending is None:
            return "not-needed"
        sensor = self._store.sensor(sensor_id)
        sensor_address = (sensor or {}).get("wifi_address")
        if not sensor_address:
            self._store.record_firmware_update_state(
                sensor_id, "failed", 0, "the sensor is not on the household network"
            )
            return "failed"
        hub_address = self._hub_address(sensor_address)
        if hub_address is None or self._firmware_port is None:
            self._store.record_firmware_update_state(
                sensor_id, "failed", 0, "the hub is not serving firmware on the network"
            )
            return "failed"
        payload = encode_firmware_update(
            FirmwareUpdateCommand(
                update_id=pending["update_id"],
                size_bytes=pending["size_bytes"],
                address=hub_address,
                port=self._firmware_port,
                digest=pending["digest"],
            )
        )
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier, timeout=self._timeout) as client:
                await client.write_gatt_char(
                    DEVICE_CONFIG_CHARACTERISTIC_UUID, payload, response=True
                )
                report = bytes(await client.read_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID))
            self._record_reported_progress(sensor_id, report, fallback="commanded")
            LOGGER.info(
                "commanded %s to install %s from %s:%s",
                sensor_id,
                pending["version"],
                hub_address,
                self._firmware_port,
            )
            return "commanded"
        except Exception as error:  # noqa: BLE001 - the sensor may simply be away
            message = str(error)[:240] or error.__class__.__name__
            self._store.record_firmware_update_state(sensor_id, "failed", 0, message)
            LOGGER.warning("could not command %s to update: %s", sensor_id, message)
            return "failed"

    def _record_reported_progress(
        self, sensor_id: str, report: bytes, fallback: Optional[str] = None
    ) -> Optional[str]:
        """Record what a read-back says about an update in progress."""
        remainder, _ = split_station_status(report)
        _, firmware = split_firmware_status(remainder)
        if firmware is None:
            if fallback is not None:
                self._store.record_firmware_update_state(sensor_id, fallback, 0, None)
            return fallback
        state, percent, failure = firmware
        if state == "idle":
            return None
        self._store.record_firmware_update_state(sensor_id, state, percent, failure)
        return state

    def _hub_address(self, sensor_address: str) -> Optional[str]:
        """This hub's address on the network the sensor is on.

        Asked of the routing table rather than configured, because a laptop hub
        moves between networks and a stale address would send the sensor
        somewhere that answers nothing.
        """
        if self._address_resolver is not None:
            return self._address_resolver(sensor_address)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((sensor_address, 9))
                return str(probe.getsockname()[0])
        except OSError:
            return None

    def has_release_pending(self, sensor_id: str) -> bool:
        return self._store.release_is_pending(sensor_id)

    async def release(self, sensor_id: str, observed_identifier: str) -> str:
        """Tell a sensor it is no longer owned, over the link it is owned by.

        Only the hub that owns the sensor can connect at all, so arriving here is
        the proof. The sensor forgets its configuration and network, leaves the
        household network, stops serving its console, and drops the bond.
        """
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier) as client:
                await client.write_gatt_char(
                    DEVICE_CONFIG_CHARACTERISTIC_UUID, DEVICE_RELEASE_PAYLOAD, response=True
                )
            self._store.mark_released(sensor_id)
            LOGGER.info("released %s", sensor_id)
            return "released"
        except Exception as error:  # noqa: BLE001 - the sensor may simply be gone
            LOGGER.warning("could not reach %s to release it: %s", sensor_id, str(error)[:240])
            return "unreachable"

    async def synchronize(
        self,
        sensor_id: str,
        observed_identifier: str,
        force_report_packet_id: int | None = None,
    ) -> str:
        desired = self._store.pending_device_configuration(sensor_id)
        if desired is None and force_report_packet_id is None:
            return "not-needed"
        config = DeviceConfiguration(**desired) if desired is not None else None
        payload = encode_device_configuration(config) if config is not None else None
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier, timeout=self._timeout) as client:
                if force_report_packet_id is not None:
                    report_payload = bytes(
                        await client.read_gatt_char(REPORT_ACK_CHARACTERISTIC_UUID)
                    )
                    report_acknowledgement = decode_report_acknowledgement(report_payload)
                    if report_acknowledgement.packet_id != force_report_packet_id:
                        raise ValueError("forced report packet ID did not match sensor request")
                    await client.write_gatt_char(
                        REPORT_ACK_CHARACTERISTIC_UUID, report_payload, response=True
                    )
                if config is not None and payload is not None:
                    await client.write_gatt_char(
                        DEVICE_CONFIG_CHARACTERISTIC_UUID, payload, response=True
                    )
                    acknowledgement = bytes(
                        await client.read_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID)
                    )
                    acknowledgement, station = split_station_status(acknowledgement)
                    if acknowledgement != payload:
                        raise ValueError("sensor configuration acknowledgement did not match")
                    acknowledged = decode_device_configuration(acknowledgement)
                    self._store.mark_device_configuration_applied(
                        sensor_id,
                        acknowledged.revision,
                        acknowledged.reporting_interval_seconds,
                    )
                    if station is not None:
                        # The sensor is the only one who knows whether it reached
                        # the network, so a console switched on stops saying it is
                        # waiting the moment the sensor answers. A console that is
                        # switched off is not waiting for anything, so not being
                        # joined is simply off rather than pending.
                        joined, address, version = station
                        self._store.record_station_report(sensor_id, version)
                        if not config.console_enabled:
                            state, address = "off", None
                        else:
                            state = "joined" if joined else "pending"
                        self._store.record_sensor_wifi_result(
                            sensor_id, state, None, address or None
                        )
            if config is not None and force_report_packet_id is not None:
                return "applied-and-acknowledged"
            return "applied" if config is not None else "acknowledged"
        except Exception as error:  # noqa: BLE001 - transport failures are persisted and retried
            message = str(error)[:240] or error.__class__.__name__
            if config is not None:
                self._store.mark_device_configuration_error(sensor_id, config.revision, message)
            LOGGER.warning("could not synchronize %s over BLE: %s", sensor_id, message)
            return "failed"

    @staticmethod
    def _load_client_factory() -> ClientFactory:
        from bleak import BleakClient

        return BleakClient


def _encoded_text(value: str, field: str, required: bool) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    encoded = value.encode("utf-8")
    if (required and not encoded) or len(encoded) > DEVICE_CONFIG_TEXT_MAX_BYTES:
        requirement = "1 to 80" if required else "at most 80"
        raise ValueError(f"{field} must contain {requirement} UTF-8 bytes")
    if any(byte < 0x20 or byte == 0x7F for byte in encoded):
        raise ValueError(f"{field} cannot contain control characters")
    return encoded