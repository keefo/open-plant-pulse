import logging

from open_plant_pulse_hub.domain import SensorReading
from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.bthome import (
    CONTRACT_VERSION,
    decode_service_data,
    is_beacon,
    sensor_id_from_local_name,
)

from .store import ReadingStore

LOGGER = logging.getLogger(__name__)


class AdvertisementIngestionService:
    """Normalize every physical or replayed advertisement through one boundary."""

    def __init__(self, store: ReadingStore) -> None:
        self._store = store

    def ingest(self, advertisement: Advertisement) -> str:
        """Store one advertisement and say what became of it.

        The answer is accepted, duplicate, conflict or rejected. A conflict is a
        packet whose report ID is already stored with different content; it is
        logged and nothing is overwritten.
        """
        sensor_id = None
        try:
            sensor_id = sensor_id_from_local_name(advertisement.local_name)
            # A sensor with nothing to report still announces itself, so it can
            # be found and adopted. That is presence, not a reading, and is
            # deliberately not stored as one.
            if is_beacon(advertisement.service_data):
                return self._store.record_beacon(
                    sensor_id=sensor_id,
                    received_at=advertisement.received_at,
                    observed_identifier=advertisement.observed_identifier,
                    source_adapter=advertisement.source_adapter,
                    rssi=advertisement.rssi,
                    service_data=advertisement.service_data,
                    contract_version=CONTRACT_VERSION,
                )
            packet = decode_service_data(advertisement.service_data, sensor_id)
        except ValueError as error:
            self._store.record_rejected_advertisement(
                received_at=advertisement.received_at,
                observed_identifier=advertisement.observed_identifier,
                source_adapter=advertisement.source_adapter,
                rssi=advertisement.rssi,
                service_data=advertisement.service_data,
                error=str(error),
                sensor_id=sensor_id,
            )
            LOGGER.warning(
                "ignored BTHome advertisement from %s: %s",
                advertisement.observed_identifier,
                error,
            )
            return "rejected"

        if isinstance(packet, SensorReading):
            status = self._store.add_advertisement(
                packet,
                received_at=advertisement.received_at,
                observed_identifier=advertisement.observed_identifier,
                source_adapter=advertisement.source_adapter,
                rssi=advertisement.rssi,
                service_data=advertisement.service_data,
            )
        else:
            status = self._store.add_supplement(
                packet,
                received_at=advertisement.received_at,
                observed_identifier=advertisement.observed_identifier,
                source_adapter=advertisement.source_adapter,
                rssi=advertisement.rssi,
                service_data=advertisement.service_data,
            )
        if status == "conflict":
            LOGGER.warning(
                "ignored BTHome report %d from %s: already stored with different content",
                packet.report_id,
                sensor_id,
            )
        return status

    def report_is_acknowledgeable(self, sensor_id: str, report_id: int) -> bool:
        """Return whether this report is stored in full, without conflict, from a
        sensor this hub owns.

        Only then may the sensor be told it arrived, and drop it.
        """
        return self._store.report_is_acknowledgeable(sensor_id, report_id)
