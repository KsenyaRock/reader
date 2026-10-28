"""Статичні перевірки репозиторію: узгодженість сайту, відсутність секретів."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SITE = os.path.join(ROOT, "site")


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf8") as f:
        return f.read()


class Site(unittest.TestCase):
    def test_version_matches_service_worker_cache(self):
        v = re.search(r'const VERSION = "([^"]+)"', read("site", "index.html")).group(1)
        cache = re.search(r'const CACHE = "([^"]+)"', read("site", "sw.js")).group(1)
        self.assertTrue(cache.endswith("-v" + v), f"версія {v} і кеш {cache} мають збігатися (інакше iPhone не оновить оболонку)")

    def test_manifest_and_icons(self):
        m = json.loads(read("site", "manifest.webmanifest"))
        self.assertEqual(m["display"], "standalone")
        for icon in m["icons"]:
            self.assertTrue(os.path.exists(os.path.join(SITE, icon["src"])), icon["src"])
        self.assertTrue(os.path.exists(os.path.join(SITE, "icon-180.png")))  # apple-touch-icon

    def test_service_worker_shell_files_exist(self):
        shell = re.search(r"const SHELL = \[(.*?)\]", read("site", "sw.js"), re.S).group(1)
        for name in re.findall(r'"([^"]+)"', shell):
            if name != "./":
                self.assertTrue(os.path.exists(os.path.join(SITE, name)), name)

    def test_index_has_no_external_resources(self):
        html = read("site", "index.html")
        self.assertEqual(re.findall(r'(?:src|href)="(https?://[^"]+)"', html), [], "плеєр має працювати без інтернету")

    def test_zoom_is_forbidden(self):
        html = read("site", "index.html")
        vp = re.search(r'<meta name="viewport" content="([^"]+)"', html).group(1)
        self.assertIn("user-scalable=no", vp)
        self.assertIn("maximum-scale=1", vp)
        self.assertIn("gesturestart", html, "iOS ігнорує user-scalable: потрібне гасіння жестів")

    def test_plus_opens_voice_sheet_and_old_buttons_are_gone(self):
        html = read("site", "index.html")
        self.assertRegex(html, r'\$\("addBtn"\)\.onclick = \(\) => \{ openSheet\("sheetVoice"\)')
        for gone in ('id="linkBtn"', 'id="addLink"', 'id="addFile"', 'id="sheetAdd"', 'id="addVoice"'):
            self.assertNotIn(gone, html)

    def test_netlify_publishes_site_without_build(self):
        toml = read("netlify.toml")
        self.assertIn('publish = "site"', toml)
        self.assertRegex(toml, r'command\s*=\s*""')


class NoSecrets(unittest.TestCase):
    PATTERNS = {
        "JWT/ключ Supabase": r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}",
        "токен Telegram-бота": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
        "приватний ключ": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "токен GitHub": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        "секретний ключ Supabase": r"\bsb_secret_[A-Za-z0-9_-]{10,}",
        "service_role": r"service_role\s*[:=]\s*['\"]?[A-Za-z0-9._-]{20,}",
        "ключ OpenAI-подібний": r"\bsk-[A-Za-z0-9]{20,}",
    }
    SKIP_DIRS = {".git", "__pycache__", "node_modules"}
    SELF = os.path.join("tests", "site", "test_site.py")

    def test_no_secrets_in_repository(self):
        found = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, ROOT)
                if rel == self.SELF or name.endswith((".png", ".mp3", ".zip", ".pyc")):
                    continue
                try:
                    with open(path, encoding="utf8") as f:
                        text = f.read()
                except (UnicodeDecodeError, OSError):
                    continue
                for label, rx in self.PATTERNS.items():
                    if re.search(rx, text):
                        found.append(f"{rel}: {label}")
        self.assertEqual(found, [], "у репозиторії не повинно бути секретів")


if __name__ == "__main__":
    unittest.main()
