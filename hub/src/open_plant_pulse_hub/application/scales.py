"""Weights read from a scale the hub polls.

A Wi-Fi scale is not a probe that announces itself; it sits at an address
and answers when asked. So this is a poller rather than a listener, one
thread per scale, in the shape the light service already uses.

What it produces goes into plant_weights beside the hand-typed weights,
because everything built on those -- water left, evapotranspiration, the
watering forecast, the drainage reading -- should not care whether a
number was typed or measured. A pot that lives on a scale simply produces
far more of them.

The hard part is not reading the number. It is knowing when the number is
not about the plant: when the pot has been lifted off, when somebody is
leaning on the shelf, when the same reading is fetched twice. Those are
all weights, and none of them is the plant drinking.

Firmware 1.0.0 stores the zero as well as the calibration, so a plant left
on the scale keeps its weight through a reboot; earlier firmware tared
itself at every power-on, which would silently have turned the pot into
the new zero.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

LOG = logging.getLogger(__name__)

POLL_SECONDS = 20.0
# How often to ask a scale about its battery. It is a separate request, and
# a cell does not move between one weighing and the next, so it is asked
# rarely: every wake of the radio is charge spent to report charge.
BATTERY_SECONDS = 600.0
REQUEST_TIMEOUT_SECONDS = 2.5
# How long a queued command is given to show up in the readings.
APPLY_TIMEOUT_SECONDS = 6.0
# A reading this far below what the plant last weighed is the pot being off
# the scale, not the plant losing that much water in twenty seconds.
POT_REMOVED_FRACTION = 0.5
# Nor is a jump this large the plant. Watering adds weight quickly, so the
# ceiling is generous; it is here to catch somebody leaning on the shelf.
IMPLAUSIBLE_RISE_FRACTION = 2.0


def read_scale(address: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Ask a scale what it weighs. Raises on anything that is not an answer."""
    url = address.rstrip("/") + "/api/weight"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        payload = json.load(reply)
    if not isinstance(payload, dict) or "status" not in payload:
        raise ValueError("that address did not answer like a scale")
    return payload


def command_scale(address: str, body: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> str:
    """Send a scale one of its plain-text commands.

    The reply only says the command was queued. Whether it took is read
    back from the scale afterwards, never assumed.
    """
    request = urllib.request.Request(
        address.rstrip("/") + "/api/cmd", data=body.encode(), method="POST",
        headers={"Content-Type": "text/plain"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read().decode(errors="replace")[:200]


def set_sampling_interval(address: str, seconds: int) -> Dict[str, Any]:
    """Ask a scale to weigh every `seconds`, and check that it did.

    Zero means continuously, ten times a second. The scale accepts the
    command and then does as it likes with it -- a value out of range is
    rejected in a log line nobody is watching -- so the setting is read
    back and what it actually is, is what gets reported.
    """
    if seconds != 0 and not 1 <= seconds <= 3600:
        raise ValueError("sampling interval must be 0, or 1 to 3600 seconds")
    command_scale(address, f"i {int(seconds)}")
    # The reply means queued, not done: the scale needs about a second to
    # pick the command up. Reading back at once saw the old value and
    # reported a failure that had not happened, and the page then put the
    # old setting back. So it is asked again until it agrees, or until it
    # has had long enough that it plainly is not going to.
    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    reading = read_scale(address)
    while reading.get("interval_s") != seconds and time.monotonic() < deadline:
        time.sleep(0.4)
        reading = read_scale(address)
    applied = reading.get("interval_s")
    if applied != seconds:
        raise ValueError(
            f"the scale is still weighing every {applied} s, not {seconds} s"
        )
    return reading


def read_scale_status(address: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Ask a scale about itself: the charge, mostly.

    `since` is given an impossible log sequence so the scale leaves its
    message log out, which is two thirds of the answer and none of what
    the hub wants from it.
    """
    url = address.rstrip("/") + "/api/status?since=999999999"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        payload = json.load(reply)
    if not isinstance(payload, dict):
        raise ValueError("that address did not answer like a scale")
    return payload


class ScaleService:
    """Polls each adopted scale and keeps the weights worth keeping."""

    def __init__(self, store: Any, poll_seconds: float = POLL_SECONDS) -> None:
        self._store = store
        self._poll_seconds = poll_seconds
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # The last weight kept per plant, so a reading is judged against what
        # the plant actually weighed rather than against the previous poll.
        self._last_kept: Dict[int, Any] = {}
        self._battery_asked: Dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, name="scales", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                for scale in self._store.scales():
                    if self._stopping.is_set():
                        break
                    self.poll_once(scale)
            except Exception:
                LOG.exception("scale poll failed")
            self._stopping.wait(self._poll_seconds)

    def poll_once(self, scale: Dict[str, Any]) -> Optional[int]:
        """Read one scale and keep the weight if it is worth keeping.

        Returns the weight recorded, or None when nothing was.
        """
        sensor_id = scale["sensor_id"]
        try:
            reading = read_scale(scale["address"])
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as error:
            self._store.record_scale_reading(
                sensor_id, None, "unreachable", None, str(error)[:200]
            )
            return None

        status = str(reading.get("status"))
        grams = reading.get("grams")
        stable = bool(reading.get("stable"))
        seq = reading.get("seq")
        self._store.record_scale_reading(
            sensor_id,
            grams if isinstance(grams, (int, float)) else None,
            status,
            stable,
            None,
            interval_seconds=_whole(reading.get("interval_s")),
            firmware=_text(reading.get("fw")),
            build=_text(reading.get("build")),
            seq=_whole(seq),
        )
        self._maybe_ask_battery(sensor_id, scale["address"])
        if status != "ok" or not stable or not isinstance(grams, (int, float)):
            return None
        plant_id = scale.get("plant_id")
        if plant_id is None:
            # A scale nobody has pointed at a plant still reports itself, so
            # it can be seen working before it is given a job.
            return None
        # Weighing once every few minutes means the same reading is fetched
        # several times over. It is one weighing, so it is stored once.
        if seq is not None and scale.get("seq") == _whole(seq):
            return None
        # When it was weighed, not when it was fetched. In interval mode a
        # reading can be a whole interval old, and the drying rate is worked
        # out from exactly these minutes.
        age = _whole(reading.get("age_s")) or 0
        taken = datetime.now(timezone.utc) - timedelta(seconds=max(0, min(age, 3600)))
        return self._keep(plant_id, int(round(grams)), scale, taken)

    def _maybe_ask_battery(self, sensor_id: str, address: str) -> None:
        last = self._battery_asked.get(sensor_id, 0.0)
        now = time.monotonic()
        if now - last < BATTERY_SECONDS:
            return
        self._battery_asked[sensor_id] = now
        try:
            status = read_scale_status(address)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            # The weight is what matters; a charge the hub could not ask for
            # is not worth marking the scale as faulty over.
            return
        volts = status.get("batt")
        percent = status.get("batt_pct")
        self._store.record_scale_battery(
            sensor_id,
            float(volts) if isinstance(volts, (int, float)) else None,
            int(percent) if isinstance(percent, (int, float)) else None,
        )

    def _keep(self, plant_id: int, grams: int, scale: Dict[str, Any],
              taken: Optional[datetime] = None) -> Optional[int]:
        last = self._last_kept.get(plant_id)
        if last is None:
            last = self._latest_weight(plant_id)
            if last is not None:
                self._last_kept[plant_id] = last
        now = taken or datetime.now(timezone.utc)

        if last is not None:
            previous = int(last["grams"])
            # The pot is off the scale, or something else is on it. Either
            # way it is not this plant losing water, and writing it would
            # tell the forecast the plant drank fifteen kilos this morning.
            if previous > 0 and grams < previous * POT_REMOVED_FRACTION:
                return None
            if previous > 0 and grams > previous * IMPLAUSIBLE_RISE_FRACTION:
                return None
            since = (now - _parse(last["at"])).total_seconds()
            if since < scale["keep_every_seconds"]:
                return None

        # Signed, so a weight nobody remembers taking is accounted for.
        # Somebody reading the log a year from now should be able to tell a
        # number they put there from a number that arrived by itself.
        self._store.record_plant_weight(
            plant_id, grams, source="scale",
            at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            note=f"Weighed by {scale.get('display_name') or scale['sensor_id']}",
        )
        self._last_kept[plant_id] = {"grams": grams, "at": now.isoformat()}
        return grams

    def _latest_weight(self, plant_id: int) -> Optional[Dict[str, Any]]:
        status = self._store.plant_weight_status(plant_id)
        return status.get("latest") if status else None


def _whole(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) else None


def _text(value: Any) -> Optional[str]:
    return str(value)[:64] if isinstance(value, str) and value else None


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc) - timedelta(days=1)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
