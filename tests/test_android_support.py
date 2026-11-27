import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import platform_support as host, uzhas_manager as uzhas


class AndroidSupportTests(unittest.TestCase):
    def test_android_detection_and_platform_info(self):
        with patch.object(host, "is_android", return_value=True), \
             patch.object(host, "is_windows", return_value=False), \
             patch.object(host, "is_linux", return_value=False), \
             patch.object(host.platform, "system", return_value="Linux"), \
             patch.object(host.platform, "release", return_value="14-android"):
            self.assertTrue(host.is_android())
            self.assertFalse(host.is_linux())
            info = host.platform_info()
            self.assertEqual(info["os"], "android")
            self.assertIn("Android", info["label"])

    def test_termux_shells_resolution(self):
        with patch.object(host, "is_android", return_value=True), \
             patch.object(host, "is_windows", return_value=False):
            shells = host.available_shells()
            self.assertTrue(len(shells) > 0)
            self.assertIn("bash", [s["id"] for s in shells] or ["sh"])

    def test_termux_build_argv(self):
        with patch.object(host, "is_windows", return_value=False), \
             patch.object(uzhas.shutil, "which", side_effect=lambda cmd: "/data/data/com.termux/files/usr/bin/" + cmd):
            argv = uzhas.build_argv("ls -la", "bash")
            self.assertEqual(argv, ["/data/data/com.termux/files/usr/bin/bash", "-lc", "ls -la"])


if __name__ == "__main__":
    unittest.main()
