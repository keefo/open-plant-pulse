from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
import math
import time
import logging
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from open_plant_pulse_hub.application.ingestion import AdvertisementIngestionService

from .advertisement import Advertisement
from .bthome import (
    BTHOME_SERVICE_UUID,
    decode_service_data,
    is_beacon,
    sensor_id_from_local_name,
)
from .device_configuration import DeviceConfigurationSynchronizer

LOGGER = logging.getLogger(__name__)

# How soon a failed configuration attempt may be tried again. It exists to stop
# a retry storm against a sensor that is advertising every few seconds, not to
# slow down the first attempt, which happens as soon as the sensor is heard.
CONFIGURATION_RETRY_SECONDS = 30.0
# How often to ask a sensor whether its console reached the network. Joining
# takes a few seconds, so asking more often than this mostly asks too early.
STATION_STATUS_POLL_SECONDS = 10.0
# How often an owned sensor with unacknowledged reports is drained when nothing
# was missed. The newest report arrives over the air the moment it is made; a
# drain only backfills and lets the sensor delete, and it pauses the sensor's
# advertising for a couple of seconds, so draining after every report would
# cost the page its freshness.
DRAIN_INTERVAL_SECONDS = 30.0
# The least time between two drains of one sensor, whatever triggers them, so
# that a drain that keeps failing cannot become a connection per packet.
DRAIN_SPACING_SECONDS = 2.0
# A drain that is due waits for the packet that completes a report, which marks
# the start of the quiet part of the sensor's reporting cycle: a connection then
# pauses advertising after the fresh report was heard, not across it. If no
# report completes over the air for this long, it drains anyway.
DRAIN_DEFER_LIMIT_SECONDS = 10.0

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


# How many packets to hold for one device while its name is not yet known.
# A contract-v3 report is two packets advertised in turn, so a single slot let
# the second overwrite the first and lost the first report after every restart.
PENDING_PER_DEVICE = 8


def _detection_callback(
    queue: asyncio.Queue[Advertisement],
    local_names: dict[str, str],
    pending: dict[str, deque[Advertisement]],
) -> Callable[[Any, Any], None]:
    def detected(device: Any, data: Any) -> None:
        identifier = str(device.address)
        if data.local_name is not None:
            local_names[identifier] = data.local_name
        local_name = local_names.get(identifier)
        advertisement = advertisement_from_bleak(device, data)

        # The name arrives in the scan response, often after the packet it
        # belongs to. Hold packets until it is known, then release them all in
        # the order they arrived, whichever callback brought the name.
        ready: list[Advertisement] = []
        if local_name is not None:
            ready.extend(
                replace(held, local_name=local_name) for held in pending.pop(identifier, ())
            )
        if advertisement is not None:
            if advertisement.local_name is not None:
                ready.append(advertisement)
            elif local_name is not None:
                ready.append(replace(advertisement, local_name=local_name))
            else:
                pending.setdefault(identifier, deque(maxlen=PENDING_PER_DEVICE)).append(
                    advertisement
                )
        for item in ready:
            try:
                queue.put_nowait(item)
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
        # Per sensor: when a drain last ended, whatever became of it; when one
        # last emptied the queue or went as far as it could; which sensors'
        # last drain stopped at a report it could not acknowledge; and which
        # are being drained right now.
        self._last_drain_attempt: dict[str, float] = {}
        self._drain_due_since: dict[str, float] = {}
        self._last_drain: dict[str, float] = {}
        self._drain_stalled: set[str] = set()
        self._draining: set[str] = set()
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
                pending: dict[str, deque[Advertisement]] = {}
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
                        sensor_id = sensor_id_from_local_name(advertisement.local_name)
                        now = time.monotonic()
                        target = (
                            advertisement.observed_identifier
                            if advertisement.connection_target is None
                            else advertisement.connection_target
                        )
                        # Rate limit by time, not by payload bytes. Payloads
                        # change from one report to the next, so comparing bytes
                        # let nearly every report trigger an attempt. An
                        # unclaimed sensor announces itself every three
                        # seconds, and each attempt asks the
                        # operating system to pair, which made a popup storm out
                        # of a retry that was invisible at a slower cadence.
                        # A sensor that has been forgotten is told so the next
                        # time it is heard from, because it may have been asleep
                        # or out of range when somebody clicked.
                        pending_release = getattr(
                            self._configuration_synchronizer, "has_release_pending", None
                        )
                        if pending_release is not None and pending_release(sensor_id):
                            await self._configuration_synchronizer.release(sensor_id, target)
                            continue
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
                        status_wanted = (
                            needs_status is not None
                            and needs_status(sensor_id)
                            and now - self._last_status_time.get(sensor_id, -math.inf)
                            >= STATION_STATUS_POLL_SECONDS
                        )
                        if self._drain_is_due(sensor_id, advertisement.service_data, now):
                            # Whatever else is due rides on the same connection,
                            # before the drain, so a sensor with a backlog is
                            # never too busy to be configured or updated.
                            if configuration_due:
                                self._last_attempted_revision[sensor_id] = revision
                                self._last_configuration_time[sensor_id] = now
                            if update_due:
                                self._last_attempted_update[sensor_id] = update_id
                                self._last_update_time[sensor_id] = now
                            if status_wanted:
                                self._last_status_time[sensor_id] = now
                            await self._drain(
                                sensor_id,
                                advertisement.observed_identifier,
                                target,
                                configure=configuration_due,
                                station_status=status_wanted,
                                firmware_update=update_due,
                            )
                            continue
                        if sensor_id in self._drain_due_since:
                            # A drain is due and waits for this report to
                            # complete; whatever else is due rides on its
                            # connection in a moment rather than open another.
                            continue
                        status_due = status_wanted and not configuration_due and not update_due
                        if status_due:
                            self._last_status_time[sensor_id] = now
                            await self._configuration_synchronizer.refresh_station(
                                sensor_id, target
                            )
                            continue
                        if update_due and not configuration_due:
                            self._last_attempted_update[sensor_id] = update_id
                            self._last_update_time[sensor_id] = now
                            await self._configuration_synchronizer.send_firmware_update(
                                sensor_id, target
                            )
                            continue
                        if configuration_due:
                            self._last_attempted_revision[sensor_id] = revision
                            self._last_configuration_time[sensor_id] = now
                            await self._configuration_synchronizer.synchronize(sensor_id, target)
            except Exception:
                LOGGER.exception("failed to persist BTHome advertisement")
            finally:
                queue.task_done()

    def _drain_is_due(self, sensor_id: str, service_data: bytes, now: float) -> bool:
        """Return whether hearing this packet should drain the sensor's queue.

        Only a packet of a report not yet acknowledged, from a sensor this hub
        owns, can start one. The sensor advertises its newest report as soon as
        it is made, and that is how the page stays fresh; a drain is the
        guarantee behind it. So one runs when the reports heard since the last
        acknowledgement have a gap, which means one was missed over the air,
        when the sensor has not been drained for a while, and on the first
        report heard after the hub starts. Never two at once, and never more
        often than every couple of seconds.
        """
        synchronizer = self._configuration_synchronizer
        if getattr(synchronizer, "drain", None) is None or is_beacon(service_data):
            return False
        try:
            report_id = decode_service_data(service_data, sensor_id).report_id
        except ValueError:
            return False
        if report_id is None or sensor_id in self._draining:
            return False
        if now - self._last_drain_attempt.get(sensor_id, -math.inf) < DRAIN_SPACING_SECONDS:
            return False
        if not synchronizer.owns(sensor_id):
            return False
        acknowledged, missing = synchronizer.report_delivery(sensor_id, report_id)
        if acknowledged:
            self._drain_due_since.pop(sensor_id, None)
            return False
        last_drain = self._last_drain.get(sensor_id)
        # A report the last drain could not acknowledge holds everything after
        # it on the sensor, so the gap it leaves is not one a drain can close.
        due = (
            last_drain is None
            or now - last_drain >= DRAIN_INTERVAL_SECONDS
            or (missing and sensor_id not in self._drain_stalled)
        )
        if not due:
            self._drain_due_since.pop(sensor_id, None)
            return False
        # When, not whether: once the report just heard is complete, so the
        # connection falls after a fresh report instead of cutting across it.
        # The first packet of a new report waits for its second.
        report_complete = self._ingestion.report_is_acknowledgeable(sensor_id, report_id)
        due_since = self._drain_due_since.setdefault(sensor_id, now)
        if report_complete or now - due_since >= DRAIN_DEFER_LIMIT_SECONDS:
            self._drain_due_since.pop(sensor_id, None)
            return True
        return False

    async def _drain(
        self,
        sensor_id: str,
        observed_identifier: str,
        target: Any,
        *,
        configure: bool,
        station_status: bool,
        firmware_update: bool,
    ) -> None:
        self._draining.add(sensor_id)
        try:
            result = await self._configuration_synchronizer.drain(
                sensor_id,
                observed_identifier,
                target,
                self._ingestion.ingest,
                configure=configure,
                station_status=station_status,
                firmware_update=firmware_update,
            )
        finally:
            self._draining.discard(sensor_id)
            self._last_drain_attempt[sensor_id] = time.monotonic()
        # A failed drain, or one that stopped at its page limit, is retried on
        # the next advertisement after the spacing. One that emptied the queue
        # waits for a gap or the interval, and so does one that stopped at a
        # report it could not acknowledge, which no retry would change.
        if result.outcome in ("drained", "stopped"):
            self._last_drain[sensor_id] = self._last_drain_attempt[sensor_id]
            if result.outcome == "stopped":
                self._drain_stalled.add(sensor_id)
            else:
                self._drain_stalled.discard(sensor_id)

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