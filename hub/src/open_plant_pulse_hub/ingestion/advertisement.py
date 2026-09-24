from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Advertisement:
    received_at: str
    local_name: Optional[str]
    observed_identifier: str
    rssi: Optional[int]
    service_data: bytes
    source_adapter: str
    connection_target: Any = None

    def __post_init__(self) -> None:
        try:
            received = datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("advertisement received_at must be an ISO 8601 timestamp") from error
        if received.tzinfo is None:
            raise ValueError("advertisement received_at must include a timezone")
        if not self.observed_identifier or not self.source_adapter:
            raise ValueError("advertisement source metadata is required")