#!/usr/bin/env python3
"""Tests for scripts/cursor.py, behind `kiln cursor`, which points
Cursor at kiln's tunnel and keeps it there as the tunnel changes.

What is worth pinning down is what it gives back. Your own base URL and
"Use OpenAI API Key" toggle come back after any number of tunnels, a URL
you set yourself is never touched, and nothing changes while Cursor is
open -- it would write its own copy back over the change when it quits.

Hermetic: each test writes its own state.vscdb and its own .kiln folder
in a temp directory. The real Cursor database, clipboard and processes
are never touched.
"""
import contextlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import cursor  # noqa: E402
import opencode  # noqa: E402

TUNNEL = "https://first-quick-tunnel.trycloudflare.com/v1"
NEXT = "https://second-quick-tunnel.trycloudflare.com/v1"
MODELS = ["qwen3.8-27b", "qwen3.8-9b"]
MINE = {"useOpenAIKey": True, "openAIBaseUrl": "https://openrouter.ai/api/v1",
        "aiSettings": {"userAddedModels": ["my-own-model"],
                       "modelOverrideEnabled": ["my-own-model"],
                       "modelOverrideDisabled": ["qwen3.8-27b"]},
        "somethingElse": {"left": "alone"}}


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        state = os.path.join(self.tmp, "kiln-state")
        for name, path in (("STATE", state),
                           ("RECORD", os.path.join(state, "cursor.json")),
                           ("PENDING", os.path.join(state, "pending.json")),
                           ("HEARTBEAT", os.path.join(state, "alive")),
                           ("BACKUP", os.path.join(state, "backup.json"))):
            p = mock.patch.object(cursor, name, path)
            p.start()
            self.addCleanup(p.stop)
        self.db = os.path.join(self.tmp, "state.vscdb")
        self.open = False
        self.copied = []
        self.key = "k" * 48
        self.spawned = []
        for target, value in (
                ("cursor_running", lambda db: self.open),
                ("to_clipboard", lambda text: self.copied.append(text) or True),
                ("live_server", lambda: (TUNNEL, {"alias": MODELS[0]})),
                ("model_names", lambda facts=None: list(MODELS))):
            p = mock.patch.object(cursor, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(opencode, "read_key", lambda: self.key)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cursor.subprocess, "Popen",
                              lambda argv, **kw: self.spawned.append(argv))
        p.start()
        self.addCleanup(p.stop)
        self.write(json.loads(json.dumps(MINE)))

    def write(self, data):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS ItemTable "
                    "(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES (?, ?)",
                    (cursor.KEY, json.dumps(data)))
        con.commit()
        con.close()

    def read(self):
        return cursor.read_settings(self.db)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = cursor.main(list(argv))
        return code, out.getvalue()

    def kiln_cursor(self, *argv):
        return self.run_cli("--db", self.db, *argv)


class Connecting(Sandbox):
    def test_points_cursor_at_the_tunnel_and_lists_the_models(self):
        code, out = self.kiln_cursor()
        self.assertEqual(code, 0, out)
        data = self.read()
        self.assertEqual(data["openAIBaseUrl"], TUNNEL)
        self.assertTrue(data["useOpenAIKey"])
        ai = data["aiSettings"]
        for m in MODELS:
            self.assertIn(m, ai["userAddedModels"])
            self.assertIn(m, ai["modelOverrideEnabled"])
            self.assertNotIn(m, ai["modelOverrideDisabled"])
        self.assertEqual(data["somethingElse"], {"left": "alone"})

    def test_off_gives_back_your_own_settings_and_models(self):
        self.kiln_cursor()
        code, out = self.kiln_cursor("--off")
        self.assertEqual(code, 0, out)
        data = self.read()
        self.assertEqual(data["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertTrue(data["useOpenAIKey"])
        self.assertEqual(data["aiSettings"]["userAddedModels"],
                         ["my-own-model"])
        self.assertEqual(data["aiSettings"]["modelOverrideEnabled"],
                         ["my-own-model"])
        self.assertFalse(os.path.exists(cursor.RECORD))

    def test_a_fresh_profile_goes_back_to_off(self):
        self.write({})
        self.kiln_cursor()
        self.kiln_cursor("--off")
        data = self.read()
        self.assertFalse(data["useOpenAIKey"])
        self.assertEqual(data["openAIBaseUrl"], "")

    def test_a_new_tunnel_keeps_your_settings_on_record(self):
        # The second tunnel finds kiln's URL there. Recording that would
        # lose the originals for good.
        self.kiln_cursor()
        self.run_cli("--follow", NEXT)
        self.assertEqual(self.read()["openAIBaseUrl"], NEXT)
        self.kiln_cursor("--off")
        self.assertEqual(self.read()["openAIBaseUrl"], MINE["openAIBaseUrl"])

    def test_a_model_you_listed_yourself_stays_when_kiln_leaves(self):
        data = json.loads(json.dumps(MINE))
        data["aiSettings"]["userAddedModels"].append("qwen3.8-27b")
        self.write(data)
        self.kiln_cursor()
        self.kiln_cursor("--off")
        self.assertIn("qwen3.8-27b", self.read()["aiSettings"]["userAddedModels"])

    def test_the_old_alias_is_taken_out(self):
        data = json.loads(json.dumps(MINE))
        data["aiSettings"]["userAddedModels"].append(cursor.STALE_MODEL)
        self.write(data)
        self.kiln_cursor()
        self.assertNotIn(cursor.STALE_MODEL,
                         self.read()["aiSettings"]["userAddedModels"])

    def test_print_changes_nothing(self):
        before = self.read()
        code, out = self.kiln_cursor("--print")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(), before)
        self.assertFalse(os.path.exists(cursor.RECORD))
        self.assertIn(TUNNEL, out)

    def test_with_no_tunnel_cursor_waits_for_the_next_start(self):
        with mock.patch.object(cursor, "live_server", lambda: ("", {})):
            code, out = self.kiln_cursor()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read()["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertIn("kiln start", out)
        self.run_cli("--follow", TUNNEL)
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)

    def test_a_named_tunnel_of_your_own(self):
        self.kiln_cursor("--url", "https://llm.example.com")
        self.assertEqual(self.read()["openAIBaseUrl"],
                         "https://llm.example.com/v1")

    def test_only_a_public_https_address_is_taken(self):
        before = self.read()
        for url in ("http://127.0.0.1:8080/v1", "localhost:8080",
                    "https://host.example.com/v1/chat",
                    "https://user:pw@host.example.com"):
            with self.subTest(url):
                code, _ = self.kiln_cursor("--url", url)
                self.assertEqual(code, 1)
        self.assertEqual(self.read(), before)
        self.assertEqual(cursor.normalize("https://h.trycloudflare.com/"),
                         "https://h.trycloudflare.com/v1")

    def test_cursor_that_never_ran_is_refused(self):
        os.remove(self.db)
        code, out = self.kiln_cursor()
        self.assertEqual(code, 1)
        self.assertIn("not changed", out)

    @unittest.skipIf(os.name == "nt", "file modes are a Unix idea")
    def test_the_record_and_the_backup_are_private(self):
        self.kiln_cursor()
        for path in (cursor.RECORD, cursor.BACKUP):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_backup_is_the_settings_from_before_kiln(self):
        self.kiln_cursor()
        self.run_cli("--follow", NEXT)
        with open(cursor.BACKUP) as fh:
            self.assertEqual(json.load(fh)["openAIBaseUrl"],
                             MINE["openAIBaseUrl"])


class Following(Sandbox):
    def test_start_and_stop_do_nothing_until_kiln_cursor_has_run(self):
        before = self.read()
        self.assertEqual(self.run_cli("--follow", TUNNEL)[0], 0)
        self.assertEqual(self.run_cli("--unfollow")[0], 0)
        self.assertEqual(self.read(), before)
        self.assertFalse(os.path.exists(cursor.RECORD))

    def test_stop_turns_the_override_off_and_start_back_on(self):
        self.kiln_cursor()
        self.run_cli("--unfollow")
        data = self.read()
        self.assertEqual(data["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertTrue(os.path.exists(cursor.RECORD))
        self.run_cli("--follow", NEXT)
        self.assertEqual(self.read()["openAIBaseUrl"], NEXT)
        self.assertTrue(self.read()["useOpenAIKey"])

    def test_a_url_you_set_since_is_left_alone(self):
        self.kiln_cursor()
        data = self.read()
        data["openAIBaseUrl"] = "https://api.openai.com/v1"
        self.write(data)
        self.run_cli("--unfollow")
        self.assertEqual(self.read()["openAIBaseUrl"],
                         "https://api.openai.com/v1")

    def test_a_bad_follow_never_fails_the_start(self):
        self.kiln_cursor()
        code, _ = self.run_cli("--follow", "not a url")
        self.assertEqual(code, 1)
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)


class WhileCursorIsOpen(Sandbox):
    def test_nothing_changes_until_it_closes(self):
        self.kiln_cursor()
        self.open = True
        self.run_cli("--follow", NEXT)
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--apply-pending", self.spawned[0])
        self.open = False
        self.assertEqual(cursor.apply_pending(), 0)
        self.assertEqual(self.read()["openAIBaseUrl"], NEXT)
        self.assertFalse(os.path.exists(cursor.PENDING))
        self.assertFalse(os.path.exists(cursor.HEARTBEAT))

    def test_the_newest_change_wins_and_one_waiter_runs(self):
        self.kiln_cursor()
        self.open = True
        self.run_cli("--follow", NEXT)
        self.run_cli("--unfollow")
        self.assertEqual(len(self.spawned), 1)
        self.open = False
        cursor.apply_pending()
        self.assertEqual(self.read()["openAIBaseUrl"], MINE["openAIBaseUrl"])

    def test_kiln_cursor_waits_for_you_to_quit_it(self):
        states = iter([True, True, True, False])
        with mock.patch.object(cursor, "cursor_running",
                               lambda db: next(states)), \
                mock.patch.object(cursor.time, "sleep", lambda s: None):
            code, out = self.kiln_cursor()
        self.assertEqual(code, 0, out)
        self.assertIn("Quit Cursor", out)
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)
        self.assertEqual(self.spawned, [])

    def test_no_wait_hands_it_to_the_background(self):
        self.open = True
        code, out = self.kiln_cursor("--no-wait")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read()["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertEqual(len(self.spawned), 1)
        self.open = False
        cursor.apply_pending()
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)

    def test_off_while_open_waits_too(self):
        self.kiln_cursor()
        self.open = True
        self.kiln_cursor("--off", "--no-wait")
        self.assertEqual(self.read()["openAIBaseUrl"], TUNNEL)
        self.open = False
        cursor.apply_pending()
        self.assertEqual(self.read()["openAIBaseUrl"], MINE["openAIBaseUrl"])
        self.assertFalse(os.path.exists(cursor.RECORD))


class TheKey(Sandbox):
    def test_it_is_put_on_the_clipboard_once_per_key(self):
        _, out = self.kiln_cursor()
        self.assertEqual(self.copied, [self.key])
        self.assertIn("OpenAI API Key", out)
        self.kiln_cursor()
        self.run_cli("--follow", NEXT)
        self.assertEqual(self.copied, [self.key])

    def test_a_rotated_key_is_asked_for_again(self):
        self.kiln_cursor()
        self.key = "n" * 48
        _, out = self.run_cli("--follow", NEXT)
        self.assertEqual(self.copied[-1], self.key)
        self.assertIn("OpenAI API Key", out)

    def test_the_record_holds_no_key(self):
        self.kiln_cursor()
        with open(cursor.RECORD) as fh:
            self.assertNotIn(self.key, fh.read())


class FindingCursor(unittest.TestCase):
    def test_from_wsl_it_is_the_windows_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = cursor._db_under(tmp)
            os.makedirs(os.path.dirname(db))
            open(db, "w").close()
            with mock.patch.object(opencode, "environment", lambda: "wsl"), \
                    mock.patch.object(cursor, "windows_appdata", lambda: tmp):
                self.assertEqual(cursor.find_db(), db)

    def test_a_database_under_mnt_belongs_to_windows(self):
        self.assertTrue(cursor.on_windows(
            "/mnt/c/Users/me/AppData/Roaming/Cursor/User/globalStorage/state.vscdb"))
        if os.name != "nt":
            self.assertFalse(cursor.on_windows(
                os.path.expanduser("~/.config/Cursor/User/globalStorage/state.vscdb")))

    def test_mac_and_linux_folders(self):
        with tempfile.TemporaryDirectory() as home:
            for env, folder in (("mac", os.path.join(home, "Library",
                                                     "Application Support")),
                                ("linux", os.path.join(home, ".config"))):
                db = cursor._db_under(folder)
                os.makedirs(os.path.dirname(db), exist_ok=True)
                open(db, "w").close()
                with self.subTest(env), \
                        mock.patch.object(opencode, "environment",
                                          lambda env=env: env), \
                        mock.patch.object(cursor.os.path, "expanduser",
                                          lambda p: p.replace("~", home, 1)), \
                        mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("XDG_CONFIG_HOME", None)
                    self.assertEqual(cursor.find_db(), db)

    def test_a_shared_checkout_reads_the_other_sides_path(self):
        with mock.patch.object(cursor.os, "name", "nt"):
            self.assertEqual(cursor.local_path("/mnt/c/Users/me/x.vscdb"),
                             "C:\\Users\\me\\x.vscdb")
        if os.name != "nt":
            with mock.patch.object(opencode, "environment", lambda: "wsl"):
                self.assertEqual(cursor.local_path("C:\\Users\\me\\x.vscdb"),
                                 "/mnt/c/Users/me/x.vscdb")
                self.assertEqual(cursor.local_path("/home/me/x.vscdb"),
                                 "/home/me/x.vscdb")


class Wiring(unittest.TestCase):
    """kiln start follows, kiln stop unfollows, on every platform."""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_unix_start_and_stop(self):
        for f in ("start_linux.sh", "start_mac.sh", "start_fedora.sh"):
            with self.subTest(f):
                text = self.read("scripts", "unix", f)
                self.assertIn("kiln_cursor_follow", text)
                self.assertIn("kiln_cursor_unfollow", text)
        self.assertIn("kiln_cursor_unfollow", self.read("kiln.sh"))
        self.assertIn('scripts/cursor.py" "$@"', self.read("kiln.sh"))

    def test_windows_start_and_stop(self):
        start = self.read("scripts", "windows", "start.bat")
        self.assertIn("call :cursor_follow", start)
        self.assertIn("call :cursor_unfollow", start)
        kiln = self.read("scripts", "windows", "kiln.bat")
        self.assertIn('goto :cmd_cursor', kiln)
        self.assertIn('cursor.py" --unfollow', kiln)

    def test_the_tunnel_service_goes_through_cursor_py(self):
        text = self.read("scripts", "unix", "cursor_tunnel.sh")
        self.assertIn("scripts/cursor.py", text)
        self.assertNotIn("cursor_sync", text)

    @unittest.skipIf(os.name == "nt", "lib_tunnel.sh is the Unix half")
    def test_follow_runs_nothing_without_kiln_cursor(self):
        lib = os.path.join(ROOT, "scripts", "unix", "lib_tunnel.sh")
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "ran")
            fake = os.path.join(tmp, "py")
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\ntouch %s\n" % marker)
            os.chmod(fake, 0o755)
            script = ('. "$1"; KILN_PY="$2"; kiln_cursor_follow https://x/v1; '
                      'kiln_cursor_unfollow; echo done')
            r = subprocess.run(["bash", "-c", script, "-", lib, fake],
                               cwd=tmp, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "done", r.stderr)
            self.assertFalse(os.path.exists(marker))
            os.makedirs(os.path.join(tmp, ".kiln"))
            open(os.path.join(tmp, ".kiln", "cursor.json"), "w").close()
            subprocess.run(["bash", "-c", script, "-", lib, fake], cwd=tmp,
                           capture_output=True, text=True)
            self.assertTrue(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main(verbosity=2)
