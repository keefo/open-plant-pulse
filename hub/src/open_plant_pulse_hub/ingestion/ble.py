from __future__ import annotations

import asyncio
import math
import time
import logging
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from open_plant_pulse_hub.application.ingestion import AdvertisementIngestionService

from .advertisement import Advertisement
from .bthome import BTHOME_SERVICE_UUID, has_force_report_event
from .device_configuration import DeviceConfigurationSynchronizer

LOGGER = logging.getLogger(__name__)

# How soon a failed configuration attempt may be tried again. It exists to stop
# a retry storm against a sensor that is advertising every few seconds, not to
# slow down the first attempt, which happens as soon as the sensor is heard.
CONFIGURATION_RETRY_SECONDS = 30.0
# How often to ask a sensor whether its console reached the network. Joining
# takes a few seconds, so asking more often than this mostly asks too early.
STATION_STATUS_POLL_SECONDS = 10.0

ScannerFactory = Callable[..., Any]


def advertisement_from_bleak(device: Any, data: Any) -> Advertisement | None:
    service_data = data.service_data.get(BTHOME_SERVICE_UUID)
    if service_data is None:
        return None
    return Advertisement(
        received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        local_name=data.local_name,
        observed_identifier=str(device.address),
        rssi=data.rssi,
        service_data=bytes(service_data),
        source_adapter="bleak",
        connection_target=device,
    )


def _detection_callback(
    queue: asyncio.Queue[Advertisement],
    local_names: dict[str, str],
    pending: dict[str, Advertisement],
) -> Callable[[Any, Any], None]:
    def detected(device: Any, data: Any) -> None:
        identifier = str(device.address)
        if data.local_name is not None:
            local_names[identifier] = data.local_name
        advertisement = advertisement_from_bleak(device, data)
        if advertisement is None:
            advertisement = pending.pop(identifier, None)
            if advertisement is None or data.local_name is None:
                return
            advertisement = Advertisement(
                received_at=advertisement.received_at,
                local_name=data.local_name,
                observed_identifier=advertisement.observed_identifier,
                rssi=advertisement.rssi,
                service_data=advertisement.service_data,
                source_adapter=advertisement.source_adapter,
                connection_target=advertisement.connection_target,
            )
        elif advertisement.local_name is None:
            local_name = local_names.get(identifier)
            if local_name is None:
                pending[identifier] = advertisement
                return
            advertisement = Advertisement(
                received_at=advertisement.received_at,
                local_name=local_name,
                observed_identifier=advertisement.observed_identifier,
                rssi=advertisement.rssi,
                service_data=advertisement.service_data,
                source_adapter=advertisement.source_adapter,
            )
        try:
            queue.put_nowait(advertisement)
        except asyncio.QueueFull:
            LOGGER.warning("BLE ingestion queue full; advertisement dropped")

    return detected


class BleakSubscriber:
    """Continuously scan BTHome advertisements with bounded restart backoff."""

    def __init__(
        self,
        ingestion: AdvertisementIngestionService,
        scanner_factory: ScannerFactory | None = None,
        configuration_synchronizer: DeviceConfigurationSynchronizer | None = None,
        initial_backoff: float = 1.0,
        maximum_backoff: float = 30.0,
    ) -> None:
        self._ingestion = ingestion
        self._scanner_factory = scanner_factory
        self._configuration_synchronizer = configuration_synchronizer
        self._initial_backoff = initial_backoff
        self._maximum_backoff = maximum_backoff
        self._stop = Event()
        self._thread = Thread(target=self._run, name="bthome-bleak", daemon=True)
        self._state_lock = Lock()
        self._state = "stopped"
        self._last_error: str | None = None
        self._last_receive_at: str | None = None
        self._last_configuration_time: dict[str, float] = {}
        self._last_attempted_revision: dict[str, int] = {}
        self._last_status_time: dict[str, float] = {}
        self._last_force_report_attempt: dict[str, bytes] = {}
        self._last_attempted_update: dict[str, int] = {}
        self._last_update_time: dict[str, float] = {}

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def health(self) -> dict[str, str | None]:
        with self._state_lock:
            return {
                "status": self._state,
                "last_error": self._last_error,
                "last_receive_at": self._last_receive_at,
            }

    def _run(self) -> None:
        asyncio.run(self._scan_forever())

    async def _scan_forever(self) -> None:
        delay = self._initial_backoff
        while not self._stop.is_set():
            try:
                scanner_factory = self._scanner_factory or self._load_scanner_factory()
                queue: asyncio.Queue[Advertisement] = asyncio.Queue(maxsize=256)
                local_names: dict[str, str] = {}
                pending: dict[str, Advertisement] = {}
                detected = _detection_callback(queue, local_names, pending)

                consumer = asyncio.create_task(self._consume(queue))
                try:
                    # Filter in the callback rather than at scanner construction.
                    # CoreBluetooth can suppress valid BTHome service-data packets
                    # when 0xFCD2 is supplied as a discovery service filter.
                    async with scanner_factory(detected):
                        self._set_state("scanning", None)
                        delay = self._initial_backoff
                        while not self._stop.is_set():
                            await asyncio.sleep(0.1)
                finally:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
            except Exception as error:  # noqa: BLE001 - scanner failures must not stop retries
                self._set_state("unavailable", str(error)[:240])
                LOGGER.warning(
                    "BLE scanner unavailable; retrying in %.1f seconds: %s",
                    delay,
                    error,
                )
                await self._sleep_until_stopped(delay)
                delay = min(self._maximum_backoff, delay * 2)
        self._set_state("stopped", None)

    async def _consume(self, queue: asyncio.Queue[Advertisement]) -> None:
        while True:
            advertisement = await queue.get()
            try:
                status = await asyncio.to_thread(self._ingestion.ingest, advertisement)
                if status != "rejected":
                    with self._state_lock:
                        self._last_receive_at = advertisement.received_at
                    if (
                        self._configuration_synchronizer is not None
                        and advertisement.local_name is not None
                    ):
                        from .bthome import sensor_id_from_local_name

                        sensor_id = sensor_id_from_local_name(advertisement.local_name)
                        forced = has_force_report_event(advertisement.service_data)
                        if not forced:
                            self._last_force_report_attempt.pop(sensor_id, None)
                        force_report_due = (
                            forced
                            and self._last_force_report_attempt.get(sensor_id)
                            != advertisement.service_data
                        )
                        # Rate limit by time, not by payload bytes. Every
                        # advertisement carries a new packet ID, so comparing
                        # bytes never matches and every single advertisement
                        # triggered an attempt. An unclaimed sensor announces
                        # itself every three seconds, and each attempt asks the
                        # operating system to pair, which made a popup storm out
                        # of a retry that was invisible at a slower cadence.
                        # A sensor that has been forgotten is told so the next
                        # time it is heard from, because it may have been asleep
                        # or out of range when somebody clicked.
                        pending_release = getattr(
                            self._configuration_synchronizer, "has_release_pending", None
                        )
                        if pending_release is not None and pending_release(sensor_id):
                            await self._configuration_synchronizer.release(
                                sensor_id,
                                advertisement.observed_identifier
                                if advertisement.connection_target is None
                                else advertisement.connection_target,
                            )
                            continue
                        now = time.monotonic()
                        # A revision this hub has not tried yet goes out on the
                        # very next advertisement, however recently something
                        # else was attempted. Only a repeat of the same revision
                        # is throttled, because that is the case that can storm.
                        pending_revision = getattr(
                            self._configuration_synchronizer, "pending_revision", None
                        )
                        revision = (
                            pending_revision(sensor_id) if pending_revision is not None else None
                        )
                        if revision is None:
                            configuration_due = False
                        elif self._last_attempted_revision.get(sensor_id) != revision:
                            configuration_due = True
                        else:
                            configuration_due = (
                                now - self._last_configuration_time.get(sensor_id, -math.inf)
                                >= CONFIGURATION_RETRY_SECONDS
                            )
                        # An update the sensor has not been told about goes out
                        # on the next advertisement, throttled the same way and
                        # for the same reason: a sensor that is out of range or
                        # refusing must not be retried every three seconds.
                        pending_update = getattr(
                            self._configuration_synchronizer, "pending_firmware_update_id", None
                        )
                        update_id = (
                            pending_update(sensor_id) if pending_update is not None else None
                        )
                        if update_id is None:
                            update_due = False
                        elif self._last_attempted_update.get(sensor_id) != update_id:
                            update_due = True
                        else:
                            update_due = (
                                now - self._last_update_time.get(sensor_id, -math.inf)
                                >= CONFIGURATION_RETRY_SECONDS
                            )
                        # A console that has been switched on but has not yet
                        # said whether it joined is worth asking again, or the
                        # answer never arrives.
                        needs_status = getattr(
                            self._configuration_synchronizer, "needs_station_status", None
                        )
                        status_due = (
                            not configuration_due
                            and not force_report_due
                            and not update_due
                            and needs_status is not None
                            and needs_status(sensor_id)
                            and now - self._last_status_time.get(sensor_id, -math.inf)
                            >= STATION_STATUS_POLL_SECONDS
                        )
                        if status_due:
                            self._last_status_time[sensor_id] = now
                            await self._configuration_synchronizer.refresh_station(
                                sensor_id,
                                advertisement.observed_identifier
                                if advertisement.connection_target is None
                                else advertisement.connection_target,
                            )
                            continue
                        if update_due and not configuration_due and not force_report_due:
                            self._last_attempted_update[sensor_id] = update_id
                            self._last_update_time[sensor_id] = now
                            await self._configuration_synchronizer.send_firmware_update(
                                sensor_id,
                                advertisement.observed_identifier
                                if advertisement.connection_target is None
                                else advertisement.connection_target,
                            )
                            continue
                        if force_report_due or configuration_due:
                            if force_report_due:
                                self._last_force_report_attempt[sensor_id] = (
                                    advertisement.service_data
                                )
                            if configuration_due:
                                self._last_attempted_revision[sensor_id] = revision
                                self._last_configuration_time[sensor_id] = now
                            await self._configuration_synchronizer.synchronize(
                                sensor_id, advertisement.observed_identifier
                                if advertisement.connection_target is None
                                else advertisement.connection_target,
                                advertisement.service_data[2] if force_report_due else None,
                            )
            except Exception:
                LOGGER.exception("failed to persist BTHome advertisement")
            finally:
                queue.task_done()

    async def _sleep_until_stopped(self, delay: float) -> None:
        elapsed = 0.0
        while elapsed < delay and not self._stop.is_set():
            interval = min(0.1, delay - elapsed)
            await asyncio.sleep(interval)
            elapsed += interval

    def _set_state(self, state: str, error: str | None) -> None:
        with self._state_lock:
            self._state = state
            self._last_error = error

    @staticmethod
    def _load_scanner_factory() -> ScannerFactory:
        from bleak import BleakScanner

        return BleakScanner