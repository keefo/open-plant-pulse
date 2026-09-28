import json
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from open_plant_pulse_hub.application import ReadingStore
from open_plant_pulse_hub.web import create_server, is_this_computer, server_address
from threading import Thread


class ListeningTests(unittest.TestCase):
    def test_serves_ipv4_and_ipv6_from_one_server(self) -> None:
        server = create_server(ReadingStore(), "::", 0)
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            _, port = server_address(server)
            for url in (f"http://127.0.0.1:{port}/api/health", f"http://[::1]:{port}/api/health"):
                with urlopen(url, timeout=5) as response:
                    self.assertEqual(response.status, 200, url)
                    self.assertIn("database", json.loads(response.read()))
        finally:
            server.shutdown()
            server.server_close()

    def test_answers_only_this_computer(self) -> None:
        # Loopback, in either family.
        self.assertTrue(is_this_computer("127.0.0.1", "127.0.0.1"))
        self.assertTrue(is_this_computer("::1", "::1"))
        self.assertTrue(is_this_computer("::ffff:127.0.0.1", "::ffff:127.0.0.1"))
        # A browser here that reached the hub through the computer's own
        # network or link-local address: both ends carry that address.
        self.assertTrue(is_this_computer("::ffff:192.168.0.231", "::ffff:192.168.0.231"))
        self.assertTrue(is_this_computer("fe80::4c8:2bd0:8579:5fa5%en0", "fe80::4c8:2bd0:8579:5fa5%en0"))
        # Another computer on the household network.
        self.assertFalse(is_this_computer("::ffff:192.168.0.50", "::ffff:192.168.0.231"))
        self.assertFalse(is_this_computer("fe80::1234%en0", "fe80::4c8:2bd0:8579:5fa5%en0"))

    def test_refuses_other_computers_unless_the_network_is_allowed(self) -> None:
        for allow_network, expected in ((False, 403), (True, 200)):
            server = create_server(ReadingStore(), "127.0.0.1", 0, allow_network=allow_network)
            Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                _, port = server_address(server)
                with mock.patch("open_plant_pulse_hub.web.is_this_computer", return_value=False):
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                            status = response.status
                    except HTTPError as error:
                        status = error.code
                self.assertEqual(status, expected, allow_network)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
