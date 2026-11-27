import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import tunnel


class TunnelUrlDetectionTests(unittest.TestCase):
    def setUp(self):
        tunnel.url_ready.clear()
        tunnel.tunnel_url = None

    def test_api_endpoint_in_request_error_is_not_treated_as_tunnel(self):
        proc = Mock(stdout=io.StringIO(
            "failed to request quick Tunnel: Post https://api.trycloudflare.com/tunnel: timeout\n"
        ))
        with redirect_stdout(io.StringIO()):
            tunnel.stream_output(proc, "tunnel", scan_url=True)
        self.assertFalse(tunnel.url_ready.is_set())
        self.assertIsNone(tunnel.tunnel_url)

    def test_only_url_after_creation_message_is_accepted(self):
        proc = Mock(stdout=io.StringIO(
            "INF Requesting new quick Tunnel on trycloudflare.com...\n"
            "INF | Your quick Tunnel has been created! Visit it at:\n"
            "INF | https://sample-123.trycloudflare.com |\n"
        ))
        with redirect_stdout(io.StringIO()):
            tunnel.stream_output(proc, "tunnel", scan_url=True)
        self.assertTrue(tunnel.url_ready.is_set())
        self.assertEqual(tunnel.tunnel_url, "https://sample-123.trycloudflare.com")

    def test_hostname_before_creation_message_is_not_accepted(self):
        proc = Mock(stdout=io.StringIO(
            "https://sample-123.trycloudflare.com\n"
            "INF Requesting new quick Tunnel on trycloudflare.com...\n"
        ))
        with redirect_stdout(io.StringIO()):
            tunnel.stream_output(proc, "tunnel", scan_url=True)
        self.assertFalse(tunnel.url_ready.is_set())
        self.assertIsNone(tunnel.tunnel_url)


if __name__ == "__main__":
    unittest.main()
