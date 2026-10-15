#!/usr/bin/env python3
"""Tests for scripts/unix/cursor_sync.py, which points Cursor's OpenAI
settings at a kiln quick tunnel and gives them back when the tunnel stops.

What is worth pinning down is what it leaves alone. The user's own OpenAI
base URL and "Use OpenAI API Key" toggle come back after a tunnel, however
many times it was brought up. down changes nothing unless Cursor points at
a trycloudflare host, which is kiln's, and cursor_tunnel.sh runs down on
every exit, including runs where up never happened.

Hermetic: each test writes its own state.vscdb in a temp folder and passes
it with --db, so the real Cursor database is never opened.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts", "unix"))
sys.dont_write_bytecode = True
import cursor_sync  # noqa: E402

KEY = cursor_sync.KEY
MINE = {
    "useOpenAIKey": True,
    "openAIBaseUrl": "https://openrouter.ai/api/v1",
    "aiSettings": {"userAddedModels": ["gpt-4o"]},
    "theme": "dark",
}
FIRST = "https://first-tunnel.trycloudflare.com"
SECOND = "https://second-tunnel.trycloudflare.com"


class Sync(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "state.vscdb")
        self.record = self.db + ".kiln-prior.json"

    def seed(self, data):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
        con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                    (KEY, json.dumps(data)))
        con.commit()
        con.close()

    def raw(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT value FROM ItemTable WHERE key=?",
                               (KEY,)).fetchone()[0]
        finally:
            con.close()

    def settings(self):
        return json.loads(self.raw())

    def sync(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cursor_sync.main(["--db", self.db] + list(argv))
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_down_without_up_leaves_the_users_settings_alone(self):
        self.seed(MINE)
        before = self.raw()
        out = self.sync("down")
        self.assertEqual(self.raw(), before)
        self.assertIn("not pointed at a kiln tunnel", out)

    def test_up_then_down_gives_back_the_users_settings(self):
        self.seed(MINE)
        self.sync("up", "--url", FIRST)
        self.assertEqual(self.settings()["openAIBaseUrl"], FIRST + "/v1")
        self.sync("down")
        data = self.settings()
        self.assertIs(data["useOpenAIKey"], True)
        self.assertEqual(data["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertFalse(os.path.exists(self.record))

    def test_a_second_up_keeps_the_users_settings_on_record(self):
        self.seed(MINE)
        self.sync("up", "--url", FIRST)
        self.sync("up", "--url", SECOND)
        self.assertEqual(self.settings()["openAIBaseUrl"], SECOND + "/v1")
        with open(self.record) as fh:
            self.assertEqual(json.load(fh), {"useOpenAIKey": True,
                                             "openAIBaseUrl": MINE["openAIBaseUrl"]})
        self.sync("down")
        data = self.settings()
        self.assertIs(data["useOpenAIKey"], True)
        self.assertEqual(data["openAIBaseUrl"], MINE["openAIBaseUrl"])

    def test_a_fresh_profile_goes_back_to_off(self):
        self.seed({"theme": "dark"})
        self.sync("up", "--url", FIRST)
        self.sync("down")
        data = self.settings()
        self.assertIs(data["useOpenAIKey"], False)
        self.assertEqual(data["openAIBaseUrl"], "")

    def test_the_record_sits_beside_the_database(self):
        self.seed(MINE)
        self.sync("up", "--url", FIRST)
        self.assertTrue(os.path.isfile(self.record))

    @unittest.skipIf(sys.platform == "win32", "file modes are not POSIX on Windows")
    def test_the_record_is_private(self):
        self.seed(MINE)
        self.sync("up", "--url", FIRST)
        self.assertEqual(os.stat(self.record).st_mode & 0o777, 0o600)

    def test_a_refused_url_changes_nothing(self):
        self.seed(MINE)
        before = self.raw()
        with self.assertRaises(SystemExit):
            cursor_sync.main(["--db", self.db, "up", "--url", "https://example.com"])
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.record))

    def test_status_format_is_unchanged(self):
        self.seed(MINE)
        self.assertEqual(self.sync("status"),
                         "useOpenAIKey true\nbaseHost openrouter.ai\n")
        self.sync("up", "--url", FIRST)
        self.assertEqual(self.sync("status"),
                         "useOpenAIKey true\nbaseHost first-tunnel.trycloudflare.com\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
