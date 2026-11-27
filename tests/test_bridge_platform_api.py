import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import bridge


class BridgePlatformApiTests(unittest.TestCase):
    def setUp(self):
        self.server = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.saved = (bridge.uzhas_enabled, bridge.uzhas_last_activity,
                      bridge.uzhas_duration_seconds, bridge.uzhas_armed_until)
        bridge.uzhas_enabled = False
        bridge.uzhas_last_activity = 0.0
        bridge.uzhas_armed_until = 0.0

    def tearDown(self):
        bridge.uzhas_enabled = self.saved[0]
        bridge.uzhas_last_activity = self.saved[1]
        bridge.uzhas_duration_seconds = self.saved[2]
        bridge.uzhas_armed_until = self.saved[3]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = None if body is None else json.dumps(body)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def test_platform_detection_is_returned_with_state(self):
        status, body = self.call("GET", "/api/state", token=bridge.TOKEN)
        self.assertEqual(status, 200)
        self.assertIn(body["platform"]["os"], ("linux", "windows", "unsupported"))
        self.assertIsInstance(body["platform"]["shells"], list)
        self.assertIn("workspace_root", body["platform"])

    def test_state_requires_bearer_token(self):
        status, _ = self.call("GET", "/api/state")
        self.assertEqual(status, 401)

    def test_remote_shell_is_rejected_when_disarmed(self):
        status, body = self.call("POST", "/api/uzhas/run",
                                {"command": "echo must-not-run", "timeout_seconds": 2}, bridge.TOKEN)
        self.assertEqual(status, 403)
        self.assertIn("локально", body["error"])

    def test_unknown_shell_is_rejected_in_approval_queue(self):
        status, body = self.call("POST", "/api/commands",
                                {"command": "echo no", "shell": "not-a-shell"}, bridge.TOKEN)
        self.assertEqual(status, 400)
        self.assertIn("оболочка", body["error"].lower())

    def test_unsupported_shell_fails_before_job_creation(self):
        now = time.monotonic()
        bridge.uzhas_enabled = True
        bridge.uzhas_last_activity = now
        bridge.uzhas_armed_until = now + 120
        status, body = self.call("POST", "/api/uzhas/run",
                                {"command": "Write-Output no", "timeout_seconds": 2, "shell": "powershell"},
                                bridge.TOKEN)
        if sys.platform.startswith("linux"):
            self.assertEqual(status, 400)
            self.assertIn("недоступна", body["error"])
        else:
            self.assertIn(status, (202, 400))


class LocalArmApiTests(unittest.TestCase):
    def setUp(self):
        self.server = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.LocalAdminHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.allowed = patch.object(bridge, "LOCAL_ADMIN_ORIGINS", bridge.LOCAL_ADMIN_ORIGINS | {self.origin})
        self.allowed.start()
        self.previous = (bridge.uzhas_enabled, bridge.uzhas_armed_until,
                         bridge.uzhas_duration_seconds, dict(bridge.uzhas_job_cancel))
        bridge.uzhas_enabled = False
        bridge.uzhas_armed_until = 0.0
        bridge.uzhas_job_cancel.clear()

    def tearDown(self):
        (bridge.uzhas_enabled, bridge.uzhas_armed_until,
         bridge.uzhas_duration_seconds, events) = self.previous
        bridge.uzhas_job_cancel.clear()
        bridge.uzhas_job_cancel.update(events)
        self.allowed.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, body, *, token=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = json.dumps(body)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        conn.request("POST", "/uzhas/config", body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def test_arm_duration_is_validated_and_local_origin_required(self):
        status, _ = self.post({"armed": True, "duration_seconds": 30}, token=bridge.TOKEN, origin=self.origin)
        self.assertEqual(status, 400)
        self.assertFalse(bridge.uzhas_enabled)
        status, body = self.post({"armed": True, "duration_seconds": 120}, token=bridge.TOKEN)
        self.assertEqual(status, 403)
        self.assertIn("origin", body["error"])

    def test_valid_arm_uses_fixed_expiration_and_quick_off_cancels(self):
        status, body = self.post({"armed": True, "duration_seconds": 120}, token=bridge.TOKEN, origin=self.origin)
        self.assertEqual(status, 200)
        self.assertTrue(body["armed"])
        self.assertGreater(body["max_seconds_remaining"], 0)
        event = threading.Event()
        bridge.uzhas_job_cancel["test-job"] = event
        status, body = self.post({"armed": False}, token=bridge.TOKEN, origin=self.origin)
        self.assertEqual(status, 200)
        self.assertFalse(body["armed"])
        self.assertTrue(event.is_set())


if __name__ == "__main__":
    unittest.main()
