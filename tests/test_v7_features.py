import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import bridge, tunnel


class V7BridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.saved = (bridge.uzhas_enabled, bridge.uzhas_last_activity,
                      bridge.uzhas_duration_seconds, bridge.uzhas_armed_until,
                      bridge.yolo_enabled, dict(bridge.uzhas_jobs))
        bridge.uzhas_enabled = False
        bridge.uzhas_last_activity = 0.0
        bridge.uzhas_armed_until = 0.0
        bridge.yolo_enabled = False
        bridge.uzhas_jobs.clear()

    def tearDown(self):
        (bridge.uzhas_enabled, bridge.uzhas_last_activity,
         bridge.uzhas_duration_seconds, bridge.uzhas_armed_until,
         bridge.yolo_enabled, jobs) = self.saved
        bridge.uzhas_jobs.clear()
        bridge.uzhas_jobs.update(jobs)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = None if body is None else json.dumps(body)
        headers = {"Authorization": "Bearer " + bridge.TOKEN}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def arm(self, confirmation=bridge.FULL_ACCESS_PHRASE, duration=3600):
        return self.call("POST", "/api/full-access/config",
                         {"armed": True, "duration_seconds": duration, "confirmation": confirmation})

    def test_full_access_requires_exact_phrase(self):
        status, body = self.arm("yes i know")
        self.assertEqual(status, 400)
        self.assertFalse(bridge.uzhas_enabled)
        self.assertIn(bridge.FULL_ACCESS_PHRASE, body["error"])
        status, body = self.arm()
        self.assertEqual(status, 200)
        self.assertTrue(body["armed"])
        status, body = self.call("POST", "/api/full-access/stop", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["armed"])

    def test_full_access_run_accepts_arbitrary_command_and_cwd(self):
        self.assertEqual(self.arm()[0], 200)
        with patch.object(bridge.uzhas, "run", return_value={
            "status": "done", "return_code": 0, "output": "ALLOWED"
        }) as runner:
            status, body = self.call("POST", "/api/full-access/run", {
                "command": "anything --goes", "timeout_seconds": 5,
                "shell": bridge.uzhas.default_shell(), "cwd": "."
            })
            self.assertEqual(status, 202)
            job_id = body["id"]
            deadline = time.monotonic() + 3
            job = {}
            while time.monotonic() < deadline:
                status, job = self.call("GET", "/api/full-access/jobs/" + job_id)
                if job.get("status") not in ("queued", "running"):
                    break
                time.sleep(0.05)
            self.assertEqual(job.get("status"), "done")
            self.assertEqual(job.get("output"), "ALLOWED")
            self.assertEqual(runner.call_args.kwargs.get("cwd"), ".")

    def test_workspace_file_ops_unlock_by_full_access(self):
        with patch.object(bridge.ws, "list_files", return_value={"entries": []}):
            status, _ = self.call("POST", "/api/workspace/list", {"path": "."})
            self.assertEqual(status, 403)
            self.assertEqual(self.arm()[0], 200)
            status, body = self.call("POST", "/api/workspace/list", {"path": "."})
            self.assertEqual(status, 200)
            self.assertEqual(body["result"], {"entries": []})

    def test_quick_and_tunnel_endpoints(self):
        status, body = self.call("GET", "/api/quick")
        self.assertEqual(status, 200)
        self.assertEqual(body["phrase"], bridge.FULL_ACCESS_PHRASE)
        self.assertIn("capabilities", body)
        status, body = self.call("GET", "/api/tunnel")
        self.assertEqual(status, 200)
        self.assertIn("mode", body)
        self.assertIn("url", body)


class TunnelModeTests(unittest.TestCase):
    def test_ts_urls_are_extracted_from_funnel_status(self):
        payload = {"Web": [{"HTTPS": "https://work-PC.tailnet.ts.net/"}],
                   "Services": [{"URL": "https://other.ts.net"}]}
        urls = list(tunnel._iter_urls(payload))
        self.assertEqual(urls[0], "https://work-PC.tailnet.ts.net")
        self.assertIn("https://other.ts.net", urls)

    def test_mode_constants(self):
        self.assertEqual(tunnel.MODES, ("auto", "tailscale", "cloudflared", "none"))


if __name__ == "__main__":
    unittest.main()


class LogAndBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.public = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.public_thread = threading.Thread(target=self.public.serve_forever, daemon=True)
        self.public_thread.start()
        self.admin = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.LocalAdminHandler)
        self.admin_thread = threading.Thread(target=self.admin.serve_forever, daemon=True)
        self.admin_thread.start()
        self.origin = f"http://127.0.0.1:{self.admin.server_port}"
        self.patch_origin = patch.object(bridge, "LOCAL_ADMIN_ORIGINS",
                                         bridge.LOCAL_ADMIN_ORIGINS | {self.origin})
        self.patch_origin.start()

    def tearDown(self):
        self.patch_origin.stop()
        for server, thread in ((self.public, self.public_thread), (self.admin, self.admin_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def _call(self, server, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        hdrs = dict(headers or {})
        payload = None if body is None else json.dumps(body)
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def test_public_bootstrap_is_closed_but_loopback_bootstrap_works(self):
        spoofed = {"Host": f"127.0.0.1:{bridge.PORT}", "Origin": f"http://127.0.0.1:{bridge.PORT}"}
        with patch.dict("os.environ", {"NERVEFORGE_PREVIEW_BOOTSTRAP": "0"}):
            status, body = self._call(self.public, "POST", "/api/local-bootstrap", {}, spoofed)
        self.assertEqual(status, 403)
        self.assertIn("8767", body["error"])
        status, body = self._call(self.admin, "GET", "/bootstrap",
                                  headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertEqual(body["token"], bridge.TOKEN)
        status, _ = self._call(self.admin, "GET", "/bootstrap",
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_http_requests_are_exposed_in_panel(self):
        self._call(self.public, "GET", "/api/quick", headers={
            "Authorization": "Bearer " + bridge.TOKEN})
        status, body = self._call(self.public, "GET", "/api/requests?limit=5", headers={
            "Authorization": "Bearer " + bridge.TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(any(item["path"] == "/api/quick" for item in body["requests"]))
