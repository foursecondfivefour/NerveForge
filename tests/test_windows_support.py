import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import bridge, platform_support as host, uzhas_manager as uzhas, workspace_manager as ws


class WindowsShellTests(unittest.TestCase):
    def test_powershell_argv_uses_no_profile_or_execution_policy_bypass(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "available_shells", return_value=[{"id": "powershell", "label": "PowerShell"}]), \
             patch.object(uzhas.shutil, "which", side_effect=lambda name: "C:/Windows/pwsh.exe" if name == "pwsh.exe" else None):
            argv = uzhas.build_argv("Write-Output 'hello world'", "powershell")
        self.assertEqual(argv[:4], ["C:/Windows/pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive"])
        self.assertEqual(argv[-2:], ["-Command", "Write-Output 'hello world'"])
        self.assertNotIn("-ExecutionPolicy", argv)

    def test_cmd_argv_is_noninteractive_and_disables_auto_run(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "available_shells", return_value=[{"id": "cmd", "label": "CMD"}]), \
             patch.object(uzhas.shutil, "which", side_effect=lambda name: "C:/Windows/cmd.exe" if name == "cmd.exe" else None):
            argv = uzhas.build_argv("echo hello", "cmd")
        self.assertEqual(argv, ["C:/Windows/cmd.exe", "/d", "/s", "/c", "echo hello"])

    def test_windows_run_dispatches_to_selected_powershell(self):
        proc = Mock()
        proc.communicate.return_value = ("PS_OK", None)
        proc.returncode = 0
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "available_shells", return_value=[{"id": "powershell", "label": "PowerShell"}]), \
             patch.object(uzhas.shutil, "which", side_effect=lambda name: "C:/Windows/pwsh.exe" if name == "pwsh.exe" else None), \
             patch.object(uzhas.subprocess, "Popen", return_value=proc) as popen:
            result = uzhas.run("Write-Output PS_OK", 5, shell="powershell")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["output"], "PS_OK")
        self.assertEqual(popen.call_args.args[0][0], "C:/Windows/pwsh.exe")
        self.assertEqual(popen.call_args.args[0][-2:], ["-Command", "Write-Output PS_OK"])

    def test_windows_shell_never_accepts_sudo(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "available_shells", return_value=[{"id": "cmd", "label": "CMD"}]):
            with self.assertRaisesRegex(uzhas.UzhasError, "текущего пользователя"):
                uzhas.build_argv("whoami", "cmd", sudo=True)

    def test_unknown_shell_is_rejected(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "available_shells", return_value=[{"id": "cmd", "label": "CMD"}]):
            with self.assertRaisesRegex(uzhas.UzhasError, "недоступна"):
                uzhas.build_argv("whoami", "powershell")

    def test_windows_child_environment_drops_unrelated_secrets(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.dict(os.environ, {"NERVEFORGE_TEST_SECRET": "do-not-pass", "SYSTEMROOT": r"C:\Windows"}, clear=True):
            env = uzhas._child_environment()
        self.assertNotIn("NERVEFORGE_TEST_SECRET", env)
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertIn("PATH", env)

    def test_windows_termination_uses_taskkill_tree(self):
        proc = Mock()
        proc.poll.side_effect = [None, 0]
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(uzhas.shutil, "which", return_value="C:/Windows/taskkill.exe"), \
             patch.object(uzhas.subprocess, "run") as run:
            uzhas._terminate_process_tree(proc)
        args = run.call_args.args[0]
        self.assertEqual(args[1:], ["/PID", str(proc.pid), "/T", "/F"])
        proc.kill.assert_not_called()

    def test_windows_workspace_command_execution_fails_closed(self):
        with patch.object(ws.host, "is_windows", return_value=True):
            with self.assertRaisesRegex(ws.WorkspaceError, "изоляционной песочницы"):
                ws.run_command("Write-Output unsafe", 10)

    def test_windows_path_validation_blocks_ads_and_device_names(self):
        with patch.object(ws.host, "is_windows", return_value=True):
            for name in ("file.txt:stream", "NUL", "COM1.log", "trailing."):
                with self.subTest(name=name), self.assertRaises(ws.WorkspaceError):
                    ws.safe_path(name)


class WindowsPlatformInfoTests(unittest.TestCase):
    def test_platform_info_exposes_available_shells_and_sandbox_capability(self):
        with patch.object(host, "is_windows", return_value=True), \
             patch.object(host, "is_linux", return_value=False), \
             patch.object(host, "available_shells", return_value=[
                 {"id": "powershell", "label": "PowerShell"}, {"id": "cmd", "label": "CMD"}
             ]), patch.object(host.platform, "system", return_value="Windows"), \
             patch.object(host.platform, "release", return_value="11"):
            info = host.platform_info()
        self.assertEqual(info["os"], "windows")
        self.assertEqual(info["default_shell"], "powershell")
        self.assertFalse(info["workspace_command_sandbox"])
        self.assertEqual([item["id"] for item in info["shells"]], ["powershell", "cmd"])


class UzhasTimerTests(unittest.TestCase):
    def setUp(self):
        self.saved = (bridge.uzhas_enabled, bridge.uzhas_last_activity,
                      bridge.uzhas_duration_seconds, bridge.uzhas_armed_until,
                      dict(bridge.uzhas_job_cancel), dict(bridge.uzhas_jobs))
        bridge.uzhas_enabled = True
        bridge.uzhas_last_activity = __import__("time").monotonic()
        bridge.uzhas_duration_seconds = 120
        bridge.uzhas_armed_until = bridge.uzhas_last_activity + 120
        bridge.uzhas_job_cancel.clear()
        bridge.uzhas_jobs.clear()

    def tearDown(self):
        (bridge.uzhas_enabled, bridge.uzhas_last_activity,
         bridge.uzhas_duration_seconds, bridge.uzhas_armed_until,
         events, jobs) = self.saved
        bridge.uzhas_job_cancel.clear()
        bridge.uzhas_job_cancel.update(events)
        bridge.uzhas_jobs.clear()
        bridge.uzhas_jobs.update(jobs)

    def test_state_reports_fixed_expiration_and_idle_remaining(self):
        state = bridge.uzhas_state()
        self.assertTrue(state["armed"])
        self.assertGreater(state["max_seconds_remaining"], 0)
        self.assertGreater(state["idle_seconds_remaining"], 0)
        self.assertEqual(state["duration_seconds"], 120)

    def test_max_duration_expiry_disarms_and_cancels_running_job(self):
        event = threading.Event()
        bridge.uzhas_job_cancel["job"] = event
        bridge.uzhas_armed_until = __import__("time").monotonic() - 1
        self.assertFalse(bridge.uzhas_state()["armed"])
        self.assertTrue(event.is_set())

    def test_idle_timeout_waits_until_active_command_finishes(self):
        bridge.uzhas_last_activity = __import__("time").monotonic() - bridge.UZHAS_IDLE_SECONDS - 1
        bridge.uzhas_jobs["active"] = {"status": "running"}
        state = bridge.uzhas_state()
        self.assertTrue(state["armed"])
        self.assertTrue(state["job_running"])

    def test_idle_expiry_disarms_and_cancels_running_job(self):
        event = threading.Event()
        bridge.uzhas_job_cancel["job"] = event
        bridge.uzhas_last_activity = __import__("time").monotonic() - bridge.UZHAS_IDLE_SECONDS - 1
        self.assertFalse(bridge.uzhas_state()["armed"])
        self.assertTrue(event.is_set())

    def test_manual_disarm_signals_all_running_jobs(self):
        event = threading.Event()
        bridge.uzhas_job_cancel["job"] = event
        bridge.disarm_uzhas("test")
        self.assertFalse(bridge.uzhas_enabled)
        self.assertTrue(event.is_set())


if __name__ == "__main__":
    unittest.main()
