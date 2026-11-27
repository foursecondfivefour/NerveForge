import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nerveforge import tunnel


class ElevatedModeTests(unittest.TestCase):
    def test_standard_user_does_not_need_elevation_confirmation(self):
        with patch.object(tunnel, "is_elevated", return_value=False), \
             patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            self.assertTrue(tunnel.confirm_elevated_run())

    def test_elevated_run_requires_exact_confirmation(self):
        with patch.object(tunnel, "is_elevated", return_value=True), \
             patch("builtins.input", return_value="RUN AS ADMIN"):
            self.assertTrue(tunnel.confirm_elevated_run())

    def test_elevated_run_rejects_wrong_confirmation(self):
        with patch.object(tunnel, "is_elevated", return_value=True), \
             patch("builtins.input", return_value="yes"):
            self.assertFalse(tunnel.confirm_elevated_run())

    def test_elevated_run_fails_closed_without_interactive_input(self):
        with patch.object(tunnel, "is_elevated", return_value=True), \
             patch("builtins.input", side_effect=EOFError):
            self.assertFalse(tunnel.confirm_elevated_run())


if __name__ == "__main__":
    unittest.main()
