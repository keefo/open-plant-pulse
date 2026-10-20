"""Zigbee support, in three layers.

    _serial.py         bytes to and from a port
    serial_gateway.py  the ESP32-C6 gateway, behind the interface below
    network.py         Zigbee as concepts: devices, joining, APS frames
    drivers.py         what a given device's payload means
    service.py         the hub's own glue: readings into the store

Swapping the C6 for a USB dongle means a second implementation of
ZigbeeNetwork and nothing else.
"""
from .drivers import DRIVERS, ScaleDriver
from .network import Delivery, Device, Reading, ZigbeeNetwork
from .serial_gateway import SerialGateway
from .service import ZigbeeService
from .updates import FirmwareUpdater

__all__ = [
    "DRIVERS",
    "Delivery",
    "Device",
    "Reading",
    "ScaleDriver",
    "SerialGateway",
    "ZigbeeNetwork",
    "ZigbeeService",
    "FirmwareUpdater",
]
