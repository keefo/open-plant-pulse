"""A plant's daily lighting window."""
from datetime import datetime
import re
from typing import Any, Dict

# The window gl1cd ran before the hub took the lights over, so a plant given
# lights starts where the lights already were.
DEFAULT_SCHEDULE = {"enabled": True, "on": "08:00", "off": "21:30"}

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def minutes(hhmm: str) -> int:
    match = _HHMM.match(hhmm)
    if match is None:
        raise ValueError("times must be HH:MM, 00:00 to 23:59")
    return int(match.group(1)) * 60 + int(match.group(2))


def validate_schedule(schedule: Any) -> Dict[str, Any]:
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    enabled = schedule.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("schedule enabled must be a boolean")
    on, off = schedule.get("on"), schedule.get("off")
    if not isinstance(on, str) or not isinstance(off, str):
        raise ValueError("schedule on and off are required")
    if minutes(on) == minutes(off):
        # An empty window would never light the plant, which nobody means.
        raise ValueError("on and off must be different times")
    return {"enabled": enabled, "on": on, "off": off}


def in_window(on: str, off: str, now: datetime) -> bool:
    """Whether now is lit: on <= now < off, wrapping midnight when on > off."""
    start, end = minutes(on), minutes(off)
    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end
