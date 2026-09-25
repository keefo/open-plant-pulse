import logging
from dataclasses import replace

from open_plant_pulse_hub.ingestion.advertisement import Advertisement
from open_plant_pulse_hub.ingestion.bthome import (
    decode_beacon_packet_id,
    decode_service_data,
    sensor_id_from_local_name,
)

from .store import ReadingStore

LOGGER = logging.getLogger(__name__)


class AdvertisementIngestionService:
    """Normalize every physical or replayed advertisement through one boundary."""

    def __init__(self, store: ReadingStore) -> None:
        self._store = store

    def ingest(self, advertisement: Advertisement) -> str:
        sensor_id = None
        try:
            sensor_id = sensor_id_from_local_name(advertisement.local_name)
            # An unclaimed sensor with nothing to measure still announces itself,
            # so it can be found and adopted. That is presence, not a reading, and
            # is deliberately not stored as one.
            beacon_packet_id = decode_beacon_packet_id(advertisement.service_data)
            if beacon_packet_id is not None:
                return self._store.record_beacon(
                    sensor_id=sensor_id,
                    packet_id=beacon_packet_id,
                    received_at=advertisement.received_at,
                    observed_identifier=advertisement.observed_identifier,
                    source_adapter=advertisement.source_adapter,
                    rssi=advertisement.rssi,
                    service_data=advertisement.service_data,
                )
            reading = decode_service_data(advertisement.service_data, sensor_id)
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

        reading = replace(reading, observed_at=advertisement.received_at)
        accepted = self._store.add_advertisement(
            reading,
            received_at=advertisement.received_at,
            observed_identifier=advertisement.observed_identifier,
            source_adapter=advertisement.source_adapter,
            rssi=advertisement.rssi,
            service_data=advertisement.service_data,
        )
        return "accepted" if accepted else "duplicate"