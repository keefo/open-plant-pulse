"""Layer 2: Zigbee as concepts, with no gateway in sight.

The gateway is an ESP32-C6 today and may be an off-the-shelf USB dongle
later. Those are not interchangeable parts -- the C6 runs the Zigbee stack
itself, while a dongle is a radio and the stack moves into this process via
zigpy -- so the seam has to be placed where both can meet.

This is that seam. Everything above it is written against these types; a
second backend implements the same interface and nothing above changes.

The rule that keeps it honest: this layer speaks Zigbee, not C6. A
``send_text("w 0")`` call would be the gateway leaking upward, because a
dongle has no idea what that string means. An APS frame to a cluster is
something either can carry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

# The scale's manufacturer-specific cluster and endpoint. A second device kind
# brings its own; the network layer only passes them through.
SCALE_CLUSTER = 0xFC00
SCALE_ENDPOINT = 10

# What a gateway reports when it will not deliver to an address it does not
# know. Distinct from a Zigbee status so the two cannot be confused.
STATUS_UNKNOWN_DEVICE = 0xFF


@dataclass(frozen=True)
class Device:
    """A device on the network, identified the only stable way there is.

    Short addresses are handed out at join and reused: one scale held 0xc458,
    then 0x63a1, then 0x40e5 across an evening of rejoining, while its EUI-64
    never changed. ``short`` is kept for diagnostics and must not be stored.
    """

    eui: str
    short: Optional[int] = None
    last_seen_s: Optional[int] = None
    # What the device says it is, asked of it in the standard way when it
    # joins: ManufacturerName and ModelIdentifier from the Basic cluster,
    # and the device type from its endpoint descriptor. Worth carrying
    # because an address identifies a device without describing it, and a
    # page listing three "Zigbee device" rows helps nobody.
    #
    # A backend that cannot ask leaves them None rather than guessing.
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    device_type: Optional[int] = None
    # What the device can do. The device type is a label and two very
    # different devices can wear the same one; the clusters are the
    # capabilities and are what the hub decides a kind from.
    clusters: tuple = ()
    # True of every Zigbee device, and between them the answer to most
    # of "why has this gone quiet?": a battery end device is meant to
    # sleep, and a mains router never is.
    power_source: Optional[str] = None
    role: Optional[str] = None
    firmware: Optional[str] = None


@dataclass(frozen=True)
class Reading:
    """One measurement, still in the device's own terms."""

    eui: str
    kind: str
    values: Dict[str, object] = field(default_factory=dict)
    lqi: Optional[int] = None
    rssi: Optional[int] = None


@dataclass(frozen=True)
class Delivery:
    """Whether a command reached the device, rather than merely leaving.

    A frame queued for a sleepy device that never collects it fails with APS
    status 0xa7. Without this, that is indistinguishable from success.
    """

    request_id: int
    status: int
    note: str = ""

    @property
    def delivered(self) -> bool:
        return self.status == 0


class ZigbeeNetwork(Protocol):
    """What every backend provides, and all the hub is allowed to assume."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def connected(self) -> bool:
        """Whether the gateway is actually reachable.

        Worth asking separately from "are there devices": an unplugged
        gateway on macOS leaves a device node that opens happily and returns
        nothing forever, which otherwise looks exactly like a quiet network.
        """
        ...

    def devices(self) -> List[Device]: ...

    def permit_join(self, seconds: int) -> None: ...

    def send(self, eui: str, cluster: int, endpoint: int, payload: bytes) -> int:
        """Sends one APS frame. Returns a request id to match the delivery."""
        ...

    def on_reading(self, handler: Callable[[Reading], None]) -> None: ...

    def on_joined(self, handler: Callable[[Device], None]) -> None: ...

    def on_delivery(self, handler: Callable[[Delivery], None]) -> None: ...
