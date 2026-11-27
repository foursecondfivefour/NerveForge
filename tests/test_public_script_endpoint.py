import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import bridge


class PublicScriptEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Assets"
        self.root.mkdir()
        self.p_root = patch.object(bridge.ws, "ROOT", self.root)
        self.p_root.start()
        self.p_audit = patch.object(bridge, "workspace_audit")
        self.p_audit.start()
        self.previous = (list(bridge._public_script_calls_minute),
                         list(bridge._public_script_calls_hour), bridge.yolo_enabled)
        bridge._public_script_calls_minute.clear()
        bridge._public_script_calls_hour.clear()
        bridge.yolo_enabled = False
        self.server = bridge.ReusableHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        minute, hour, bridge.yolo_enabled = self.previous
        bridge._public_script_calls_minute.clear()
        bridge._public_script_calls_minute.extend(minute)
        bridge._public_script_calls_hour.clear()
        bridge._public_script_calls_hour.extend(hour)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.p_audit.stop()
        self.p_root.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        raw = response.read()
        conn.close()
        return status, response_headers, json.loads(raw) if raw else {}

    def test_anonymous_create_only_write_works_without_yolo_or_bearer(self):
        status, headers, body = self.request("POST", "/api/public/scripts", {
            "filename": "передатчик.cs", "content": "public class SmokeTest {}\n"
        })
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(body["result"]["path"], "передатчик.cs")
        self.assertEqual((self.root / "передатчик.cs").read_text(encoding="utf-8"), "public class SmokeTest {}\n")
        self.assertFalse(bridge.yolo_enabled)

    def test_public_assets_endpoint_accepts_shader_without_bearer(self):
        shader = 'Shader "NerveForge/Test" { SubShader { Pass {} } }'
        status, _, body = self.request("POST", "/api/public/assets", {
            "filename": "TestUi.shader", "content": shader
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["result"]["path"], "TestUi.shader")
        self.assertEqual((self.root / "TestUi.shader").read_text(encoding="utf-8"), shader)

    def test_public_write_never_overwrites_existing_script(self):
        path = self.root / "Existing.cs"
        path.write_text("original", encoding="utf-8")
        status, _, body = self.request("POST", "/api/public/scripts", {
            "filename": "Existing.cs", "content": "replacement"
        })
        self.assertEqual(status, 409)
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertIn("never overwrites", body["error"])

    def test_public_write_rejects_paths_and_non_cs_files(self):
        for name in ("../escape.cs", "nested.cs/evil.cs", "readme.txt"):
            with self.subTest(filename=name):
                status, _, _ = self.request("POST", "/api/public/scripts", {
                    "filename": name, "content": "x"
                })
                self.assertEqual(status, 400)

    def test_preflight_is_available_for_public_endpoint_only(self):
        status, headers, _ = self.request("OPTIONS", "/api/public/scripts")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        status, headers, _ = self.request("OPTIONS", "/api/public/assets")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        status, _, _ = self.request("OPTIONS", "/api/workspace/write")
        self.assertEqual(status, 405)

    def test_anonymous_script_upload_is_rate_limited(self):
        with patch.object(bridge, "PUBLIC_SCRIPT_MAX_PER_MINUTE", 1):
            first, _, _ = self.request("POST", "/api/public/scripts", {
                "filename": "one.cs", "content": ""
            })
            second, _, body = self.request("POST", "/api/public/scripts", {
                "filename": "two.cs", "content": ""
            })
        self.assertEqual(first, 201)
        self.assertEqual(second, 429)
        self.assertIn("limit", body["error"])


if __name__ == "__main__":
    unittest.main()
