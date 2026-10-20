"""Deciding which weights from a scale are worth writing down.

One judgement, wherever the reading came from. This lived inside the HTTP
scale poller, which was fine while that was the only way a scale could
reach the hub; a Zigbee scale would otherwise need either a second copy of
these rules or no rules at all, and both are worse than moving them here.

The rules are about a pot on a scale, not about a number:

  - a weight far below the last one means the pot is off the scale, not
    that the plant drank fifteen kilos this morning
  - a weight far above it means something else is on there
  - and most readings are simply too soon after the last to be worth
    keeping, because a plant changes over days

How often "too soon" is depends on what the scale has been told to do:
either every so many seconds, or once a day at a chosen hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import logging
from typing import Any, Dict, Optional

LOG = logging.getLogger(__name__)

# Below half the last kept weight, the pot is not on the scale.
POT_REMOVED_FRACTION = 0.5
# Above double it, something else is.
IMPLAUSIBLE_RISE_FRACTION = 2.0


def _parse(value: Any) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due(last_at: Optional[datetime], now: datetime,
        every_seconds: Optional[int], at_hour: Optional[int],
        zone: Optional[tzinfo] = None) -> bool:
    """Is a weight due, by whichever schedule this scale is keeping?

    An hour means one a day: the first reading at or after that hour, on a
    day that has not had one yet. Waiting for the exact minute would mean
    losing the day's weight whenever the scale was asleep as the clock
    turned over.

    The hour is the one on the wall, in the zone the hub has been set to.
    Times are stored and compared in UTC, and reading the hour straight off
    a UTC clock made "six in the morning" mean six in the morning in
    Greenwich: a hub in Vancouver opened the window at eleven the night
    before and wrote the day's weight somewhere in the small hours. Passing
    the zone in, rather than reading the machine's, is what lets a hub on a
    server that keeps UTC still weigh at breakfast where the plant lives.
    """
    if last_at is None:
        return True
    if at_hour is not None:
        local_now = now.astimezone(zone) if zone is not None else now.astimezone()
        if local_now.hour < at_hour:
            return False
        # The window opens at that hour each day; a reading counts if the
        # last kept one was before today's opening. Built from a naive local
        # time so the offset is the one in force on the day -- replacing the
        # hour on an aware datetime would keep the offset of the moment and
        # be an hour out on the two days a year that it changes.
        naked = local_now.replace(
            tzinfo=None, hour=at_hour, minute=0, second=0, microsecond=0
        )
        opened = naked.replace(tzinfo=zone) if zone is not None else naked.astimezone()
        return last_at < opened
    return (now - last_at).total_seconds() >= (every_seconds or 0)


class WeightKeeper:
    """Writes a plant's weight when it is worth writing, and not otherwise."""

    def __init__(self, store: Any) -> None:
        self._store = store
        # The last weight kept per plant, so a reading is judged against what
        # the plant actually weighed rather than against the previous poll.
        self._last_kept: Dict[int, Any] = {}

    def _zone(self) -> Optional[tzinfo]:
        """The hub's chosen zone, where the store is one that keeps settings.

        A store that does not -- an older one, or a stand-in in a test --
        gets the machine's zone, which is what happened before there was a
        setting at all.
        """
        getter = getattr(self._store, "hub_zone", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            LOG.exception("could not read the hub's time zone")
            return None

    def forget(self, plant_id: int) -> None:
        self._last_kept.pop(plant_id, None)

    def keep(self, plant_id: int, grams: int, source_name: str,
             every_seconds: Optional[int] = None, at_hour: Optional[int] = None,
             taken: Optional[datetime] = None,
             asked_for: bool = False) -> Optional[int]:
        """Record this weight for the plant, or decide it is not worth it.

        Returns the weight written, or None when it was rejected.

        `asked_for` is somebody pressing a button, which skips the
        schedule: the point of asking is to have a reading now rather
        than at the next half hour. It does not skip the sanity checks.
        A weight saying the pot is off the scale is wrong whether or
        not it was asked for, and writing it down anyway is how a
        drying curve gets a cliff in it.
        """
        last = self._last_kept.get(plant_id)
        if last is None:
            last = self._latest_weight(plant_id)
            if last is not None:
                self._last_kept[plant_id] = last
        now = taken or datetime.now(timezone.utc)

        if last is not None:
            previous = int(last["grams"])
            if previous > 0 and grams < previous * POT_REMOVED_FRACTION:
                return None
            if previous > 0 and grams > previous * IMPLAUSIBLE_RISE_FRACTION:
                return None
            # The zone the hub has been set to, so an hour of the day means
            # the same thing here as it does on the settings page.
            if not asked_for and not due(_parse(last["at"]), now,
                                         every_seconds, at_hour,
                                         zone=self._zone()):
                return None

        # Signed, so a weight nobody remembers taking is accounted for.
        # Somebody reading the log a year from now should be able to tell a
        # number they put there from a number that arrived by itself.
        self._store.record_plant_weight(
            plant_id, grams, source="scale",
            at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            note=f"Weighed by {source_name}",
        )
        self._last_kept[plant_id] = {"grams": grams, "at": now.isoformat()}
        return grams

    def _latest_weight(self, plant_id: int) -> Optional[Dict[str, Any]]:
        status = self._store.plant_weight_status(plant_id)
        latest = (status or {}).get("latest")
        if not latest or latest.get("grams") is None:
            return None
        return {"grams": latest["grams"], "at": latest["at"]}
