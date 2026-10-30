"""A serial port using only the standard library.

pyserial lives inside the ESP-IDF environment, and sourcing export.sh to get
it costs five seconds before anything happens. Talking to a tty needs termios
and two ioctls, so the dependency is not worth that wait.
"""
import fcntl
import os
import struct
import termios

# BSD/macOS modem-control ioctls: set and clear bits of the status register.
TIOCMBIS = 0x8004746C
TIOCMBIC = 0x8004746B
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004


class Port:
    def __init__(self, path, baud=115200):
        # O_NONBLOCK so the open itself cannot hang on modem lines; cleared
        # afterwards so reads block normally.
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            self._configure(baud)
            # Leave RTS and DTR alone. On these boards they are wired to EN
            # and BOOT: asserting RTS holds the chip in reset, which looks
            # exactly like a board that has nothing to say.
            self.clear_reset_lines()
        except Exception:
            os.close(self.fd)
            raise
        self._buf = b""

    def _configure(self, baud):
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        speed = getattr(termios, f"B{baud}")
        # Raw: no echo, no line discipline, no flow control, 8N1.
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
        cc[termios.VTIME] = 2           # a read returns after at most 200 ms
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

    def clear_reset_lines(self):
        try:
            fcntl.ioctl(self.fd, TIOCMBIC, struct.pack("I", TIOCM_RTS | TIOCM_DTR))
        except OSError:
            pass                        # not every adapter exposes these

    def pulse_reset(self):
        """Reset the board: RTS drives EN, briefly held low."""
        import time
        fcntl.ioctl(self.fd, TIOCMBIS, struct.pack("I", TIOCM_RTS))
        time.sleep(0.15)
        fcntl.ioctl(self.fd, TIOCMBIC, struct.pack("I", TIOCM_RTS))

    def readline(self):
        """One line, or b"" when a second passes with nothing."""
        while b"\n" not in self._buf:
            try:
                chunk = os.read(self.fd, 512)
            except OSError:
                return b""
            if not chunk:
                return b""
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def write(self, data):
        os.write(self.fd, data)

    def flush(self):
        pass        # os.write already went straight to the device

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
