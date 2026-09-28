"""What the hub asks of a light, whatever the light is.

Each kind of light is a driver: a class that knows one device's protocol and
nothing about plants or schedules. The light service, the API and the pages only
ever call the interface below, so a new kind of light is a new driver module and
one entry in DRIVERS, and nothing else changes.
"""
from dataclasses import dataclass
import ipaddress
import re
import threading
from typing import Any, ClassVar, Dict, Optional, Tuple


@dataclass(frozen=True)
class LightState:
    """What a driver last confirmed. Never a guess.

    power is None whenever the driver cannot vouch for it: a remembered "on"
    reported as current once hid a 90-minute outage of the light it described.
    """

    online: bool
    power: Optional[bool]
    detail: Optional[str] = None


CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
UNREACHABLE = "unreachable"
POWER_OUTCOMES = (CONFIRMED, UNCONFIRMED, UNREACHABLE)


@dataclass(frozen=True)
class PowerResult:
    outcome: str
    detail: Optional[str] = None

    @property
    def confirmed(self) -> bool:
        return self.outcome == CONFIRMED


@dataclass(frozen=True)
class ConfigField:
    """One setting the Add light form asks for, and how it is checked."""

    name: str
    label: str
    kind: str  # "ipv4", "mac" or "integer"
    required: bool = False
    default: Any = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    help: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "help": self.help,
        }


_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class LightDriver:
    """The interface every driver implements.

    state() answers from what the driver already knows and never waits on the
    device. set_power() acts and confirms before it returns, which may take
    seconds. A driver owns its own threads and locks, so one slow device never
    holds up another.
    """

    kind: ClassVar[str]
    label: ClassVar[str]
    capabilities: ClassVar[Tuple[str, ...]] = ("power",)
    config_fields: ClassVar[Tuple[ConfigField, ...]] = ()
    # Settings a driver learns and keeps for itself, which the form never shows
    # and an edit of the settings must not throw away.
    learned_fields: ClassVar[Tuple[str, ...]] = ()

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = dict(config)
        self._learned: Dict[str, Any] = {}
        self._learned_lock = threading.Lock()

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """The settings as stored: checked, defaulted, and nothing unknown."""
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        known = {field.name for field in cls.config_fields} | set(cls.learned_fields)
        unknown = sorted(set(config) - known)
        if unknown:
            raise ValueError(f"unknown setting: {unknown[0]}")
        clean: Dict[str, Any] = {}
        for field in cls.config_fields:
            value = config.get(field.name)
            if value in (None, ""):
                if field.required:
                    raise ValueError(f"{field.label} is required")
                value = field.default
                if value is None:
                    continue
            clean[field.name] = _check_field(field, value)
        for name in cls.learned_fields:
            if config.get(name) not in (None, ""):
                clean[name] = config[name]
        return clean

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        return {
            "kind": cls.kind,
            "label": cls.label,
            "capabilities": list(cls.capabilities),
            "fields": [field.public() for field in cls.config_fields],
        }

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def state(self) -> LightState:
        raise NotImplementedError

    def set_power(self, on: bool) -> PowerResult:
        raise NotImplementedError

    def learn(self, **facts: Any) -> None:
        """Record something found out about the device, for the hub to keep."""
        with self._learned_lock:
            for name, value in facts.items():
                if self.config.get(name) != value:
                    self.config[name] = value
                    self._learned[name] = value

    def config_updates(self) -> Dict[str, Any]:
        """Facts learned since the last call, such as a new address."""
        with self._learned_lock:
            learned, self._learned = self._learned, {}
        return learned


def _check_field(field: ConfigField, value: Any) -> Any:
    if field.kind == "ipv4":
        try:
            return str(ipaddress.IPv4Address(str(value).strip()))
        except ValueError as error:
            raise ValueError(f"{field.label} must be an IPv4 address") from error
    if field.kind == "mac":
        text = str(value).strip().lower().replace("-", ":")
        if not _MAC.match(text):
            raise ValueError(f"{field.label} must look like aa:bb:cc:dd:ee:ff")
        return text
    if field.kind == "integer":
        if isinstance(value, bool):
            raise ValueError(f"{field.label} must be a whole number")
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field.label} must be a whole number") from error
        if number != value and str(number) != str(value).strip():
            raise ValueError(f"{field.label} must be a whole number")
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"{field.label} must be at least {field.minimum}")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"{field.label} must be at most {field.maximum}")
        return number
    raise ValueError(f"unsupported setting kind {field.kind}")


class FakeLight(LightDriver):
    """A light that does what it is told, for tests and for trying the pages.

    Tests reach in to make it unreachable or to have it ignore commands, which is
    what real lights do on a bad day.
    """

    kind = "fake"
    label = "Simulated light"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.online = True
        self.power: Optional[bool] = None
        self.obeys = True
        self.commands: list = []

    def state(self) -> LightState:
        return LightState(self.online, self.power if self.online else None)

    def set_power(self, on: bool) -> PowerResult:
        self.commands.append(on)
        if not self.online:
            return PowerResult(UNREACHABLE, "simulated light is offline")
        if not self.obeys:
            return PowerResult(UNCONFIRMED, "simulated light ignored the command")
        self.power = on
        return PowerResult(CONFIRMED)
