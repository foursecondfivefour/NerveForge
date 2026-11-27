import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import platform_support as host
from nerveforge import uzhas_manager as uzhas


class UzhasManagerTests(unittest.TestCase):
    def test_command_runs_as_user(self):
        command = "Write-Output UZHAS_TEST_OK" if host.is_windows() else "printf UZHAS_TEST_OK"
        result = uzhas.run(command, 5, False)
        self.assertEqual(result["status"], "done")
        self.assertIn("UZHAS_TEST_OK", result["output"])
        self.assertFalse(result["sudo_requested"])

    def test_timeout_kills_process_group(self):
        command = "Start-Sleep -Seconds 2" if host.is_windows() else "sleep 2"
        result = uzhas.run(command, 1, False)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["return_code"], 124)

    def test_cancel_event_stops_running_process(self):
        command = "Start-Sleep -Seconds 15" if host.is_windows() else "sleep 15"
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        try:
            result = uzhas.run(command, 20, False, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["return_code"], 130)

    def test_invalid_timeout_rejected(self):
        for value in (0, -1, 3601, True, "30"):
            with self.subTest(value=value), self.assertRaises(uzhas.UzhasError):
                uzhas.run("true", value, False)

    def test_sudo_must_be_boolean(self):
        with self.assertRaises(uzhas.UzhasError):
            uzhas.run("true", 5, "yes")


if __name__ == "__main__":
    unittest.main()
