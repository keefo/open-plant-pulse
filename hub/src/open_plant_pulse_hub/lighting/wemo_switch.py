"""A light switched at the outlet by a Belkin Wemo Switch (F7C027).

Belkin shut the Wemo cloud down on 2026-01-31; the switch's local UPnP/SOAP API
is the only way left to drive it. Its firmware serves that API on one of ports
49152 to 49155, moves between them across reboots, and intermittently times out
on a port that answered seconds earlier, so every call walks the ports starting
with the last one that answered, twice, before calling the switch unreachable.
"""
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Optional
import urllib.request

from .driver import (
    CONFIRMED,
    UNCONFIRMED,
    UNREACHABLE,
    ConfigField,
    LightDriver,
    LightState,
    PowerResult,
)


LOG = logging.getLogger(__name__)
PORTS = (49153, 49152, 49154, 49155)
TIMEOUT_SECONDS = 3.0
POLL_SECONDS = 10.0
# One failed call is not an outage; three missed polls are.
STALE_SECONDS = 35.0
_STATE = re.compile(r"<BinaryState>([^<]*)</BinaryState>")


def envelope(action: str, arguments: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="urn:Belkin:service:basicevent:1">{arguments}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()


def parse_binary_state(reply: str) -> Optional[bool]:
    """The power in a reply. Insight models append "|..." fields; the first is power."""
    match = _STATE.search(reply)
    if match is None:
        return None
    first = match.group(1).split("|")[0]
    return bool(int(first)) if first.isdigit() else None


def _post(url: str, body: bytes, action: str) -> str:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:Belkin:service:basicevent:1#{action}"',
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode(errors="replace")


class WemoSwitch(LightDriver):
    kind = "wemo-switch"
    label = "Light on a Wemo switch"
    capabilities = ("power",)
    config_fields = (
        ConfigField(
            "host",
            "Switch address",
            "ipv4",
            required=True,
            help="The switch is found by address only, so give it a DHCP reservation.",
        ),
    )
    learned_fields = ("last_port",)

    def __init__(
        self,
        config: Dict[str, Any],
        post: Callable[[str, bytes, str], str] = _post,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        super().__init__(config)
        self._post = post
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._port: Optional[int] = config.get("last_port")
        self._power: Optional[bool] = None
        self._last_ok: Optional[float] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._poll, name=f"wemo-{self.config['host']}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def state(self) -> LightState:
        online = self._last_ok is not None and self._clock() - self._last_ok <= STALE_SECONDS
        if not online:
            return LightState(False, None, f"no answer from {self.config['host']}")
        return LightState(True, self._power)

    def set_power(self, on: bool) -> PowerResult:
        with self._lock:
            sent = False
            for _ in range(2):
                if self._call("SetBinaryState", f"<BinaryState>{int(on)}</BinaryState>") is not None:
                    sent = True
                    break
        if not sent:
            return PowerResult(UNREACHABLE, f"no answer from {self.config['host']}")
        # The reply to a set is not trusted: the switch answers Error when it is
        # already in the state asked for. A fresh read is.
        time.sleep(0.3)
        power = self.refresh()
        if power is None:
            return PowerResult(UNCONFIRMED, "the switch did not answer the read-back")
        if power != on:
            return PowerResult(UNCONFIRMED, f"the switch reads {'on' if power else 'off'}")
        return PowerResult(CONFIRMED)

    def refresh(self) -> Optional[bool]:
        with self._lock:
            for _ in range(2):
                reply = self._call("GetBinaryState")
                if reply is not None:
                    self._power = parse_binary_state(reply)
                    return self._power
        return None

    def _call(self, action: str, arguments: str = "") -> Optional[str]:
        body = envelope(action, arguments)
        for port in dict.fromkeys(p for p in (self._port, *PORTS) if p):
            url = f"http://{self.config['host']}:{port}/upnp/control/basicevent1"
            try:
                reply = self._post(url, body, action)
            except Exception:  # refused, timed out, reset mid-reply: next port
                continue
            if self._port != port:
                self._port = port
                self.learn(last_port=port)
            self._last_ok = self._clock()
            return reply
        return None

    def _poll(self) -> None:
        while not self._stopping.is_set():
            try:
                self.refresh()
            except Exception:
                LOG.exception("Wemo poll of %s failed", self.config["host"])
            self._stopping.wait(self._poll_seconds)
