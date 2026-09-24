import json
from pathlib import Path
from typing import Any, Dict, List, Union

from open_plant_pulse_hub.application.ingestion import AdvertisementIngestionService

from .advertisement import Advertisement

REPLAY_SCHEMA = "open-plant-pulse.bthome-replay.v1"


class AdvertisementReplay:
    def __init__(self, ingestion: AdvertisementIngestionService) -> None:
        self._ingestion = ingestion

    def replay(self, fixture_path: Union[str, Path]) -> List[str]:
        fixture: Dict[str, Any] = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        if fixture.get("schema") != REPLAY_SCHEMA or not isinstance(fixture.get("events"), list):
            raise ValueError("unsupported BTHome replay fixture")

        results: List[str] = []
        for event in fixture["events"]:
            if not isinstance(event, dict):
                raise ValueError("replay events must be objects")
            try:
                advertisement = Advertisement(
                    received_at=str(event["received_at"]),
                    local_name=event.get("local_name"),
                    observed_identifier=str(event["observed_identifier"]),
                    rssi=event.get("rssi"),
                    service_data=bytes.fromhex(str(event["service_data_hex"])),
                    source_adapter="replay",
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("invalid BTHome replay event") from error
            results.append(self._ingestion.ingest(advertisement))
        return results