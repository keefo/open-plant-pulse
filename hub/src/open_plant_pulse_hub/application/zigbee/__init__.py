"""Zigbee support, in three layers.

    _serial.py         bytes to and from a port
    serial_gateway.py  the ESP32-C6 gateway, behind the interface below
    ezsp_gateway.py    a Silicon Labs dongle, behind the same interface
    network.py         Zigbee as concepts: devices, joining, APS frames
    drivers.py         what a given device's payload means
    service.py         the hub's own glue: readings into the store

Both back ends implement ZigbeeNetwork and nothing above them knows
which is running. The hub uses the dongle; the C6 remains until the
devices have moved across to the dongle's network.
"""
from .drivers import DRIVERS, ScaleDriver
from .network import Delivery, Device, Reading, ZigbeeNetwork
from .ezsp_gateway import EzspGateway
from .serial_gateway import SerialGateway
from .service import ZigbeeService
from .updates import FirmwareUpdater

__all__ = [
    "DRIVERS",
    "Delivery",
    "Device",
    "Reading",
    "ScaleDriver",
    "EzspGateway",
    "SerialGateway",
    "ZigbeeNetwork",
    "ZigbeeService",
    "FirmwareUpdater",
]
