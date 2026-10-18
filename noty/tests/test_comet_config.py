import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("noty_comet_config_under_test", HERE / "comet_config.py")
cc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cc
spec.loader.exec_module(cc)

URL = "http://127.0.0.1:33811/cometa"


class CometConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "AppData", "Roaming", "comet", "config.toml")

    def put(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read(self):
        with open(self.path, encoding="utf-8", newline="") as f:
            return f.read()

    def test_creates_the_file_and_its_folders(self):
        self.assertTrue(cc.ensure_block(self.path, URL))
        self.assertEqual(self.read(), cc.block(URL))
        self.assertTrue(cc.has_url(self.path, URL))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["config.toml"])  # no temp file left

    def test_is_idempotent(self):
        cc.ensure_block(self.path, URL)
        before = os.stat(self.path).st_mtime_ns
        self.assertFalse(cc.ensure_block(self.path, URL))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_only_its_own_block_is_replaced(self):
        mine = cc.block("http://127.0.0.1:1/cometa")
        self.put('[overlay]\nposition = "top_left"\n\n' + mine + '\n[overlay.notifications.chat]\nenabled = false\n')
        self.assertTrue(cc.ensure_block(self.path, URL))
        text = self.read()
        self.assertEqual(text, '[overlay]\nposition = "top_left"\n\n' + cc.block(URL) + '\n[overlay.notifications.chat]\nenabled = false\n')
        self.assertTrue(cc.has_url(self.path, URL))
        self.assertFalse(cc.has_url(self.path, "http://127.0.0.1:1/cometa"))

    def test_it_is_added_after_what_the_user_has(self):
        self.put('[overlay]\nposition = "top_left"')  # no final newline
        self.assertTrue(cc.ensure_block(self.path, URL))
        self.assertEqual(self.read(), '[overlay]\nposition = "top_left"\n\n' + cc.block(URL))

    def test_windows_line_endings_are_kept(self):
        self.put('[overlay]\r\nposition = "top_left"\r\n')
        cc.ensure_block(self.path, URL)
        self.assertNotIn("\n", self.read().replace("\r\n", ""))
        cc.ensure_block(self.path, "http://127.0.0.1:2/cometa")
        self.assertNotIn("\n", self.read().replace("\r\n", ""))
        self.assertEqual(self.read().count("noty:begin"), 1)

    def test_the_users_own_listener_is_enough(self):
        text = f'[[custom_event]]\nenabled = true\nurl = "{URL}"\n'
        self.put(text)
        self.assertFalse(cc.ensure_block(self.path, URL))
        self.assertEqual(self.read(), text)
        self.put(f'[[custom_event]]\nenabled = false\nurl = "{URL}"\n')  # disabled: not enough
        self.assertTrue(cc.ensure_block(self.path, URL))

    def test_other_listeners_are_kept(self):
        text = '[[custom_event]]\nenabled = true\nurl = "http://127.0.0.1:9970/x"\n'
        self.put(text)
        cc.ensure_block(self.path, URL)
        self.assertTrue(self.read().startswith(text))
        self.assertTrue(cc.has_url(self.path, URL))
        self.assertTrue(cc.has_url(self.path, "http://127.0.0.1:9970/x"))

    @unittest.skipIf(cc.tomllib is None, "needs tomllib")
    def test_a_broken_file_is_never_written(self):
        self.put("this is [not toml")
        with self.assertRaises(ValueError):
            cc.ensure_block(self.path, URL)
        self.assertEqual(self.read(), "this is [not toml")
        self.assertFalse(cc.has_url(self.path, URL))

    def test_has_url_without_a_file(self):
        self.assertFalse(cc.has_url(self.path, URL))

    def test_a_url_with_odd_characters_is_quoted(self):
        weird = 'http://127.0.0.1:1/a"b\\c'
        cc.ensure_block(self.path, weird)
        self.assertTrue(cc.has_url(self.path, weird))


if __name__ == "__main__":
    unittest.main()
