"""Sensors for a test to work with, made the way the hub makes them.

Tests used to conjure these by replaying a file of BTHome
advertisements through the Bluetooth ingestion path -- three sensors
with readings, from one fixture, in one line. It was convenient and it
tied the lighting, rooms, plants and browser tests to a transport none
of them was about, so the Bluetooth code could not be removed without
them.

This makes the same shapes through the calls the live Zigbee path
uses, so a test asking for "a sensor with some readings" no longer
depends on how the readings arrived.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

# The three the old fixture produced, in the order it produced them,
# because tests index into the list it left behind.
FIXTURE_SENSORS = (
    ("aabbccddeeff", "soil"),
    ("001122334455", "soil"),
    ("102030405060", "soil"),
)


def place_sensors(store: Any, how_many: int = 3) -> List[str]:
    """Unclaimed sensors with a reading each, newest last.

    Returns their ids in a stable order, which is the order the
    fixture gave and the order `sensors("unclaimed")` returns.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    made: List[str] = []
    for index, (eui, kind) in enumerate(FIXTURE_SENSORS[:how_many]):
        sensor = store.seen_zigbee_device(eui, kind)
        sensor_id = sensor["sensor_id"]
        at = (now - timedelta(minutes=10 - index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.record_soil_reading(
            sensor_id, soil_temperature_c=21.0 + index,
            moisture_percent=40.0 + index, observed_at=at)
        store.record_battery(sensor_id, 90 - index, 3.0)
        made.append(sensor_id)
    return made


def unclaimed_ids(store: Any) -> List[str]:
    """The ids a test should enroll, in a settled order."""
    return [row["sensor_id"] for row in store.sensors("unclaimed")]
