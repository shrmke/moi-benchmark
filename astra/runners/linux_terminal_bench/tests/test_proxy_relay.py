from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from astra.runners.linux_terminal_bench.proxy_relay import start_proxy_relay

class ProxyRelayTests(unittest.TestCase):
    def test_relays_a_loopback_proxy_to_an_ipv4_listener(self) -> None:
        server = Mock(server_address=("127.0.0.1", 18432))
        thread = Mock()
        with patch(
            "astra.runners.linux_terminal_bench.proxy_relay._ThreadingTCPServer",
            return_value=server,
        ), patch(
            "astra.runners.linux_terminal_bench.proxy_relay.threading.Thread",
            return_value=thread,
        ):
            relay = start_proxy_relay(
                "http://127.0.0.1:7890", "172.17.0.1"
            )

        self.assertIsNotNone(relay)
        self.assertEqual(
            relay.proxy_url,
            "http://host.docker.internal:18432",
        )
        thread.start.assert_called_once_with()

        relay.close()

        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        thread.join.assert_called_once_with(timeout=5)

    def test_external_proxy_does_not_need_a_relay(self) -> None:
        self.assertIsNone(
            start_proxy_relay("http://proxy.example:8080", "127.0.0.1")
        )


if __name__ == "__main__":
    unittest.main()
