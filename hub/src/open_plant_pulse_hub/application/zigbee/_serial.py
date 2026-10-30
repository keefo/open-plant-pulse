"""Layer 1: bytes to and from a serial port, using only the standard library.

pyserial would do this, but the hub carries one dependency today and a tty
needs termios and two ioctls. Keeping it here also keeps the hub installable
without the ESP-IDF environment.
"""
from __future__ import annotations

import fcntl
import glob
import os
import struct
import termios
from typing import List, Optional

# BSD/macOS modem-control ioctls: set and clear bits of the status register.
_TIOCMBIS = 0x8004746C
_TIOCMBIC = 0x8004746B
_TIOCM_DTR = 0x002
_TIOCM_RTS = 0x004


def candidate_ports() -> List[str]:
    """Ports a gateway might be on, in the order worth trying."""
    return sorted(glob.glob("/dev/cu.wchusbserial*")) + sorted(glob.glob("/dev/cu.usbmodem*"))


class SerialLine:
    def __init__(self, path: str, baud: int = 115200) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            self._configure(baud)
            # RTS and DTR are wired to EN and BOOT on these boards. Asserting
            # RTS holds the chip in reset, which looks exactly like a
            # gateway that has nothing to say.
            self._clear_modem_lines()
        except Exception:
            os.close(self.fd)
            raise
        self._buf = b""

    def _configure(self, baud: int) -> None:
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{baud}")
        iflag &= ~(termios.IXON | termios.IXOFF | termios.IXANY | termios.ICRNL |
                   termios.INLCR | termios.IGNCR | termios.ISTRIP | termios.INPCK)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHOE | termios.ECHONL |
                   termios.ICANON | termios.ISIG | termios.IEXTEN)
        # HUPCL cleared, which is the difference between a gateway that
        # runs for weeks and one that reboots whenever the hub does.
        #
        # "Hang up on last close" drops DTR and RTS when the last
        # process lets go of the port, and on these boards those lines
        # drive EN and BOOT through the auto-reset circuit. So every
        # hub restart reset the coordinator, which lost its device
        # table with it and then silently discarded every reading from
        # a scale it could no longer place.
        cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE |
                   termios.CRTSCTS | termios.HUPCL)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 2           # a read returns within 200 ms
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

    def _clear_modem_lines(self) -> None:
        try:
            fcntl.ioctl(self.fd, _TIOCMBIC, struct.pack("I", _TIOCM_RTS | _TIOCM_DTR))
        except OSError:
            pass                        # not every adapter exposes these

    def readline(self) -> Optional[bytes]:
        """One line, or None when nothing arrived before the read timed out."""
        while b"\n" not in self._buf:
            try:
                chunk = os.read(self.fd, 1024)
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
