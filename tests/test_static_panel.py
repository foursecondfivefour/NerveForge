import re
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PanelMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for name in ("src", "href"):
            value = attrs.get(name, "")
            if tag in ("script", "link") and value:
                self.assets.append(value)


class StaticPanelTests(unittest.TestCase):
    def test_javascript_element_references_exist_and_ids_are_unique(self):
        parser = PanelMarkup()
        parser.feed((ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        referenced = set(re.findall(r"\$\('([^']+)'\)", script))
        counts = Counter(parser.ids)
        self.assertFalse(referenced - set(parser.ids), referenced - set(parser.ids))
        self.assertEqual([item for item, count in counts.items() if count > 1], [])

    def test_windows_installer_requires_explicit_dependency_opt_in(self):
        installer_path = ROOT / "setup_windows.ps1"
        self.assertTrue(installer_path.read_bytes().startswith(b"\xef\xbb\xbf"), "Windows PowerShell requires a UTF-8 BOM for Russian text")
        installer = installer_path.read_text(encoding="utf-8-sig")
        self.assertIn("[switch]$InstallMissing", installer)
        self.assertIn("Введите Y для подтверждения", installer)
        self.assertIn("NERVEFORGE_WORKSPACE", installer)
        self.assertIn("Administrator", installer)
        self.assertIn("RUN AS ADMIN", installer)
        self.assertIn("chr(46)", installer, "Avoid PowerShell native-argument quote stripping in the Python -c probe")

    def test_panel_warns_about_always_on_anonymous_script_upload(self):
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Публичная загрузка Unity-файлов включена без токена", markup)
        self.assertIn("Unity компилирует C#-файлы", markup)
        self.assertIn("/api/public/scripts", markup)

    def test_panel_assets_are_local(self):
        parser = PanelMarkup()
        parser.feed((ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        external = [asset for asset in parser.assets if asset.startswith(("http://", "https://", "//"))]
        self.assertEqual(external, [])
        for asset in ("/static/app.css", "/static/app.js", "/assets/nerveforge-mark.png"):
            self.assertIn(asset, parser.assets)


if __name__ == "__main__":
    unittest.main()
