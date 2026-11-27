import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import platform_support as host
from nerveforge import workspace_manager as ws


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "NerveForgeProjects"
        self.backups = base / "app-data" / "workspace-backups"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.p_root = patch.object(ws, "ROOT", self.root)
        self.p_backup = patch.object(ws, "BACKUP_ROOT", self.backups)
        self.p_root.start()
        self.p_backup.start()

    def tearDown(self):
        self.p_backup.stop()
        self.p_root.stop()
        self.tmp.cleanup()

    def test_read_write_and_backup(self):
        ws.write_file("notes/a.txt", "first")
        self.assertEqual(ws.read_file("notes/a.txt")["content"], "first")
        result = ws.write_file("notes/a.txt", "second")
        self.assertEqual(ws.read_file("notes/a.txt")["content"], "second")
        self.assertTrue(result["backup"])
        backup = self.backups / result["backup"]
        self.assertEqual(backup.read_text(), "first")
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_parent_traversal_rejected(self):
        for path in ("../escape", "nested/../../escape", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(ws.WorkspaceError):
                ws.safe_path(path)

    def test_symlink_read_and_write_rejected(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret")
        try:
            (self.root / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(ws.WorkspaceError):
            ws.read_file("link.txt")
        with self.assertRaises(ws.WorkspaceError):
            ws.write_file("link.txt", "overwrite")
        self.assertEqual(outside.read_text(), "secret")

    def test_overwrite_large_or_binary_file_is_rejected(self):
        large = self.root / "large.txt"
        large.write_bytes(b"x" * (ws.MAX_FILE_BYTES + 1))
        with self.assertRaises(ws.WorkspaceError):
            ws.write_file("large.txt", "new")
        binary = self.root / "image.bin"
        binary.write_bytes(b"\x00PNG")
        with self.assertRaises(ws.WorkspaceError):
            ws.write_file("image.bin", "new")

    def test_list_does_not_follow_symlink(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        result = ws.list_files(".")
        item = next(x for x in result["entries"] if x["name"] == "link")
        self.assertEqual(item["type"], "symlink-blocked")

    def test_public_script_create_only_and_filename_constraints(self):
        result = ws.create_public_script("передатчик.cs", "public class SmokeTest {}\n")
        self.assertEqual(result["path"], "передатчик.cs")
        self.assertEqual((self.root / "передатчик.cs").read_text(encoding="utf-8"), "public class SmokeTest {}\n")
        with self.assertRaisesRegex(ws.WorkspaceError, "already exists"):
            ws.create_public_script("передатчик.cs", "replacement")
        self.assertEqual((self.root / "передатчик.cs").read_text(encoding="utf-8"), "public class SmokeTest {}\n")
        for name in ("../escape.cs", "nested/script.cs", "bad.txt", "C:\\escape.cs"):
            with self.subTest(name=name), self.assertRaises(ws.WorkspaceError):
                ws.create_public_script(name, "content")

    def test_public_asset_shader_allowed_and_other_extensions_rejected(self):
        shader = 'Shader "NerveForge/Hud" { SubShader { Pass {} } }'
        ws.create_public_asset("Hud.shader", shader)
        self.assertEqual((self.root / "Hud.shader").read_text(encoding="utf-8"), shader)
        with self.assertRaisesRegex(ws.WorkspaceError, "Only .cs and .shader"):
            ws.create_public_asset("notes.txt", "no")

    def test_public_script_size_and_nul_are_rejected(self):
        with self.assertRaisesRegex(ws.WorkspaceError, "64 KiB"):
            ws.create_public_script("large.cs", "x" * (ws.PUBLIC_SCRIPT_MAX_BYTES + 1))
        with self.assertRaises(ws.WorkspaceError):
            ws.create_public_script("nul.cs", "bad\x00content")

    def test_run_rejects_timeout_outside_policy(self):
        for value in (0, -1, 3601, True, "30"):
            with self.subTest(value=value), self.assertRaises(ws.WorkspaceError):
                ws.run_command("echo hello", value)

    def test_no_unsandboxed_fallback(self):
        if host.is_windows():
            with self.assertRaisesRegex(ws.WorkspaceError, "Windows.*изоляционной песочницы"):
                ws.run_command("Write-Output hello", 5)
        else:
            with patch.object(ws.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ws.WorkspaceError, "bubblewrap"):
                    ws.run_command("echo hello", 5)


if __name__ == "__main__":
    unittest.main()
