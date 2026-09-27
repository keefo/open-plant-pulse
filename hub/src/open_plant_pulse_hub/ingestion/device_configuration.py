from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import socket
from typing import Any, Callable, List, Optional

from open_plant_pulse_hub.application.store import ReadingStore
from open_plant_pulse_hub.domain import SensorReading

from .advertisement import Advertisement
from .bthome import decode_service_data


LOGGER = logging.getLogger(__name__)
DEVICE_CONFIG_CHARACTERISTIC_UUID = "7f510002-1b15-4c28-9a4a-8d0f4f505000"
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
# Bulk drain. The drain request turns reads of the characteristic into queue
# pages until the end token or the disconnect; a page holds up to eight of the
# oldest queued reports, and the cumulative acknowledgement removes every queued
# report up to and including the one it names.
DRAIN_REQUEST = bytes((0x20,))
QUEUE_PAGE_MARKER = 0x20
QUEUE_PAGE_MAX_REPORTS = 8
QUEUE_PAGE_MAX_SIZE = 512
CUMULATIVE_ACK_MARKER = 0x21
DRAIN_END = bytes((0x22,))
# How many pages one connection reads before it stops and leaves the rest for
# the next. A sensor adding reports as fast as they are read would otherwise
# hold the link for ever.
DRAIN_MAX_PAGES = 16
# How drained packets are marked in the raw report log, so they can be told
# from the same packets heard over the air.
DRAIN_SOURCE_ADAPTER = "bleak-drain"
ClientFactory = Callable[..., Any]
Ingest = Callable[[Advertisement], str]


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
class QueuedReport:
    """One report from a queue page: exactly the two packets it is advertised as."""

    report_id: int
    packet1: bytes
    packet2: bytes


@dataclass(frozen=True)
class DrainResult:
    """What one drain did.

    The outcome is drained when an empty page was reached, more when the page
    limit stopped it first, stopped when a report could not be acknowledged,
    and failed when the link or a page failed.
    """

    outcome: str
    stored_packets: int = 0
    pages: int = 0
    acknowledged_report_id: Optional[int] = None


def encode_cumulative_acknowledgement(report_id: int) -> bytes:
    """Say that every queued report up to and including this one is stored."""
    if not 1 <= report_id <= 0xFFFFFFFF:
        raise ValueError("report ID must be between 1 and 4294967295")
    return bytes((CUMULATIVE_ACK_MARKER,)) + report_id.to_bytes(4, "little")


def decode_queue_page(payload: bytes, sensor_id: str) -> List[QueuedReport]:
    """Decode a queue page, refusing all of it if any part is wrong.

    Each record must be a packet 1 and a packet 2 that decode
    under the contract and carry the record's report ID, and the reports must be
    in ascending order, since the cumulative acknowledgement relies on it. A page
    that is anything else says the sensor and the hub disagree about the
    contract, and nothing on it is safe to acknowledge.
    """
    if len(payload) < 2 or len(payload) > QUEUE_PAGE_MAX_SIZE:
        raise ValueError("queue page length is invalid")
    if payload[0] != QUEUE_PAGE_MARKER:
        raise ValueError("queue page marker is wrong")
    count = payload[1]
    if count > QUEUE_PAGE_MAX_REPORTS:
        raise ValueError("queue page holds more than eight reports")
    reports: List[QueuedReport] = []
    offset = 2
    for _ in range(count):
        if offset + 5 > len(payload):
            raise ValueError("queue page record is truncated")
        report_id = int.from_bytes(payload[offset : offset + 4], "little")
        offset += 4
        packets = []
        for _kind in ("packet1", "packet2"):
            if offset >= len(payload):
                raise ValueError("queue page record is truncated")
            length = payload[offset]
            offset += 1
            if length == 0 or offset + length > len(payload):
                raise ValueError("queue page record is truncated")
            packets.append(payload[offset : offset + length])
            offset += length
        packet1, packet2 = packets
        decoded_packet1 = decode_service_data(packet1, sensor_id)
        decoded_packet2 = decode_service_data(packet2, sensor_id)
        if not isinstance(decoded_packet1, SensorReading) or isinstance(
            decoded_packet2, SensorReading
        ):
            raise ValueError("queue page record is not a packet 1 and a packet 2")
        if decoded_packet1.report_id != report_id or decoded_packet2.report_id != report_id:
            raise ValueError("queue page record report ID does not match its packets")
        if reports and report_id <= reports[-1].report_id:
            raise ValueError("queue page reports are not in ascending order")
        reports.append(QueuedReport(report_id, packet1, packet2))
    if offset != len(payload):
        raise ValueError("queue page has bytes after its last record")
    return reports


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
                return await self._read_station(client, sensor_id)
        except Exception as error:  # noqa: BLE001 - the sensor may simply be away
            LOGGER.warning(
                "could not read %s network state: %s", sensor_id, str(error)[:240]
            )
            return "failed"

    async def _read_station(self, client: Any, sensor_id: str) -> str:
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
        # A sensor answering while an update is still "in progress" is a
        # sensor that has come back without it: either it is about to, or
        # the new image never started and this one is the old one.
        self._store.expire_stalled_firmware_update(sensor_id)
        sensor = self._store.sensor(sensor_id)
        if sensor and sensor["wifi_enabled"]:
            self._store.record_sensor_wifi_result(
                sensor_id,
                "joined" if joined else "pending",
                None,
                address or None,
            )
        return "joined" if joined else "pending"

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
        command = self._firmware_update_command(sensor_id)
        if command is None or isinstance(command, str):
            return command or "not-needed"
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier, timeout=self._timeout) as client:
                return await self._command_firmware_update(client, sensor_id, *command)
        except Exception as error:  # noqa: BLE001 - the sensor may simply be away
            self._firmware_update_failed(sensor_id, error)
            return "failed"

    def _firmware_update_command(
        self, sensor_id: str
    ) -> Optional[str | tuple[dict[str, Any], bytes, str]]:
        """The pending update and its payload, "failed" if it cannot be sent, or
        None when nothing is pending."""
        pending = self._store.pending_firmware_update(sensor_id)
        if pending is None:
            return None
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
        return pending, payload, hub_address

    async def _command_firmware_update(
        self, client: Any, sensor_id: str, pending: dict[str, Any], payload: bytes, hub_address: str
    ) -> str:
        await client.write_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID, payload, response=True)
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

    def _firmware_update_failed(self, sensor_id: str, error: Exception) -> None:
        message = str(error)[:240] or error.__class__.__name__
        self._store.record_firmware_update_state(sensor_id, "failed", 0, message)
        LOGGER.warning("could not command %s to update: %s", sensor_id, message)

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

    async def synchronize(self, sensor_id: str, observed_identifier: str) -> str:
        """Deliver pending configuration and read it back."""
        desired = self._store.pending_device_configuration(sensor_id)
        if desired is None:
            return "not-needed"
        config = DeviceConfiguration(**desired)
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(observed_identifier, timeout=self._timeout) as client:
                await self._apply_configuration(client, sensor_id, config)
            return "applied"
        except Exception as error:  # noqa: BLE001 - transport failures are persisted and retried
            self._configuration_failed(sensor_id, config, error)
            return "failed"

    async def _apply_configuration(
        self, client: Any, sensor_id: str, config: DeviceConfiguration
    ) -> None:
        payload = encode_device_configuration(config)
        await client.write_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID, payload, response=True)
        acknowledgement = bytes(await client.read_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID))
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
            # The sensor is the only one who knows whether it reached the
            # network, so a console switched on stops saying it is waiting the
            # moment the sensor answers. A console that is switched off is not
            # waiting for anything, so not being joined is simply off rather
            # than pending.
            joined, address, version = station
            self._store.record_station_report(sensor_id, version)
            if not config.console_enabled:
                state, address = "off", None
            else:
                state = "joined" if joined else "pending"
            self._store.record_sensor_wifi_result(sensor_id, state, None, address or None)

    def _configuration_failed(
        self, sensor_id: str, config: DeviceConfiguration, error: Exception
    ) -> None:
        message = str(error)[:240] or error.__class__.__name__
        self._store.mark_device_configuration_error(sensor_id, config.revision, message)
        LOGGER.warning("could not synchronize %s over BLE: %s", sensor_id, message)

    def owns(self, sensor_id: str) -> bool:
        """Is this a sensor the hub may connect to for its reports?

        The same rule configuration delivery follows: only an enrolled sensor
        has the bond the characteristic requires.
        """
        sensor = self._store.sensor(sensor_id)
        return sensor is not None and sensor["enrollment_status"] == "enrolled"

    def report_delivery(self, sensor_id: str, report_id: int) -> tuple[bool, bool]:
        """Whether this report is acknowledged, and whether one before it is missing."""
        return self._store.report_delivery(sensor_id, report_id)

    async def drain(
        self,
        sensor_id: str,
        observed_identifier: str,
        connection_target: Any,
        ingest: Ingest,
        *,
        configure: bool = False,
        station_status: bool = False,
        firmware_update: bool = False,
    ) -> DrainResult:
        """Move the sensor's whole queue to the hub over one connection.

        Configuration, status and update work that is due goes first on the same
        link, so a sensor with a backlog is never too busy to be configured.
        Each goes on failing and succeeding exactly as it does alone, and none
        of them failing stops the drain.
        """
        desired = self._store.pending_device_configuration(sensor_id) if configure else None
        config = DeviceConfiguration(**desired) if desired is not None else None
        command = self._firmware_update_command(sensor_id) if firmware_update else None
        if isinstance(command, str):
            command = None
        # Each step records its own outcome once it has been tried on the link;
        # only the ones the link never reached are failed by losing it.
        untried = {"configuration", "firmware_update"}
        try:
            factory = self._client_factory or self._load_client_factory()
            async with factory(connection_target, timeout=self._timeout) as client:
                if config is not None:
                    untried.discard("configuration")
                    try:
                        await self._apply_configuration(client, sensor_id, config)
                    except Exception as error:  # noqa: BLE001 - kept pending and retried
                        self._configuration_failed(sensor_id, config, error)
                    else:
                        # The read-back already carried the station status.
                        station_status = False
                if station_status:
                    try:
                        await self._read_station(client, sensor_id)
                    except Exception as error:  # noqa: BLE001 - asked again later
                        LOGGER.warning(
                            "could not read %s network state: %s", sensor_id, str(error)[:240]
                        )
                if command is not None:
                    untried.discard("firmware_update")
                    try:
                        await self._command_firmware_update(client, sensor_id, *command)
                    except Exception as error:  # noqa: BLE001 - recorded, retried later
                        self._firmware_update_failed(sensor_id, error)
                return await self._drain_queue(client, sensor_id, observed_identifier, ingest)
        except Exception as error:  # noqa: BLE001 - the next advertisement tries again
            if config is not None and "configuration" in untried:
                self._configuration_failed(sensor_id, config, error)
            if command is not None and "firmware_update" in untried:
                self._firmware_update_failed(sensor_id, error)
            LOGGER.warning(
                "could not drain %s over BLE: %s",
                sensor_id,
                str(error)[:240] or error.__class__.__name__,
            )
            return DrainResult("failed")

    async def _drain_queue(
        self, client: Any, sensor_id: str, observed_identifier: str, ingest: Ingest
    ) -> DrainResult:
        stored_packets = pages = 0
        acknowledged: Optional[int] = None
        outcome = "more"
        await client.write_gatt_char(
            DEVICE_CONFIG_CHARACTERISTIC_UUID, DRAIN_REQUEST, response=True
        )
        try:
            while pages < DRAIN_MAX_PAGES:
                payload = bytes(await client.read_gatt_char(DEVICE_CONFIG_CHARACTERISTIC_UUID))
                received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                pages += 1
                try:
                    reports = decode_queue_page(payload, sensor_id)
                except ValueError as error:
                    LOGGER.warning(
                        "refused a queue page from %s: %s (%s)", sensor_id, error, payload.hex()
                    )
                    outcome = "failed"
                    break
                if not reports:
                    outcome = "drained"
                    break
                if acknowledged is not None and reports[0].report_id <= acknowledged:
                    # The sensor has not removed what it was told to. Reading
                    # the same page again would only loop.
                    LOGGER.warning(
                        "queue page from %s repeats report %d, already acknowledged",
                        sensor_id,
                        reports[0].report_id,
                    )
                    outcome = "failed"
                    break
                page_acknowledged: list[int] = []
                blocked = False
                for report in reports:
                    statuses = [
                        await asyncio.to_thread(
                            ingest,
                            Advertisement(
                                received_at=received_at,
                                local_name=sensor_id,
                                observed_identifier=observed_identifier,
                                rssi=None,
                                service_data=packet,
                                source_adapter=DRAIN_SOURCE_ADAPTER,
                            ),
                        )
                        for packet in (report.packet1, report.packet2)
                    ]
                    stored_packets += statuses.count("accepted")
                    # Everything on the page is stored, but only an unbroken
                    # run of good reports is acknowledged: a report in
                    # conflict stays on the sensor, and so does everything
                    # after it, because the acknowledgement is cumulative.
                    if blocked:
                        continue
                    if all(
                        status in ("accepted", "duplicate") for status in statuses
                    ) and self._store.report_is_acknowledgeable(sensor_id, report.report_id):
                        page_acknowledged.append(report.report_id)
                    else:
                        LOGGER.warning(
                            "report %d from %s cannot be acknowledged: %s",
                            report.report_id,
                            sensor_id,
                            "/".join(statuses),
                        )
                        blocked = True
                if page_acknowledged:
                    await client.write_gatt_char(
                        DEVICE_CONFIG_CHARACTERISTIC_UUID,
                        encode_cumulative_acknowledgement(page_acknowledged[-1]),
                        response=True,
                    )
                    self._store.mark_reports_acknowledged(sensor_id, page_acknowledged)
                    acknowledged = page_acknowledged[-1]
                if blocked:
                    outcome = "stopped"
                    break
        finally:
            # Reads return the configuration again after this; a link that has
            # already dropped has ended drain mode by itself.
            try:
                await client.write_gatt_char(
                    DEVICE_CONFIG_CHARACTERISTIC_UUID, DRAIN_END, response=True
                )
            except Exception as error:  # noqa: BLE001 - the disconnect ends it too
                LOGGER.debug("could not end the drain of %s: %s", sensor_id, error)
        LOGGER.info(
            "drained %s: %s, %d new packets over %d pages, acknowledged up to %s",
            sensor_id,
            outcome,
            stored_packets,
            pages,
            acknowledged,
        )
        return DrainResult(outcome, stored_packets, pages, acknowledged)

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