#!/usr/bin/env python3
"""Tests for scripts/control.py, which kiln tui and the benchmark use to
read state and run kiln verbs.

What is worth pinning down is what it will run: nothing outside the
action table reaches a command line, and the start options turn into
exactly the flag and environment the scripts read. Job output handling
is covered too, because a lost line or a stray escape is what the user
would actually see go wrong.

Hermetic: jobs run a Python one-liner rather than kiln.
"""
import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import control  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"


class BuildArgs(unittest.TestCase):
    def ok(self, req, args, env=None):
        self.assertEqual(control.build_args(req), (args, env or {}))

    def bad(self, req):
        with self.assertRaises(ValueError):
            control.build_args(req)

    def test_table(self):
        self.ok({"action": "setup"}, ["setup"])
        self.ok({"action": "update"}, ["update"])
        self.ok({"action": "stop"}, ["stop"])
        self.ok({"action": "key-rotate"}, ["key", "rotate"])
        self.ok({"action": "opencode"}, ["opencode"])
        self.ok({"action": "pi"}, ["pi"])
        self.ok({"action": "self-update"}, ["self-update"])
        self.ok({"action": "self-update-check"}, ["self-update", "--check"])
        self.ok({"action": "get"}, ["get"])
        self.ok({"action": "get", "variant": "9b"}, ["get", "9b"])
        self.ok({"action": "start", "variant": "ablit"}, ["start", "ablit"])

    def test_start_options(self):
        self.ok({"action": "start", "variant": "base", "no_mtp": True},
                ["start", "base", "--no-mtp"])
        # Local means the scripts' own switch, not a different command.
        self.ok({"action": "start", "variant": "base", "local": True},
                ["start", "base"], {"KILN_NO_TUNNEL": "1"})
        # True, not truthy.
        self.ok({"action": "start", "variant": "base", "no_mtp": "yes",
                 "local": 1}, ["start", "base"])

    def test_nothing_free_text_reaches_the_command_line(self):
        self.bad({"action": "get", "variant": "base & calc"})
        self.bad({"action": "get", "variant": "../../x"})
        self.bad({"action": "start", "variant": "base\" & del *"})
        self.bad({"action": "bench"})
        self.bad({"action": "key", "variant": "set"})
        self.bad({"action": "stop", "variant": "base"})
        self.bad({"action": "get", "no_mtp": True})
        self.bad({"action": "stop", "local": True})
        self.bad({"action": "start"})          # Windows would open a picker
        self.bad({})
        self.bad([1, 2])
        self.bad("setup")

    def test_update_is_windows_only(self):
        self.assertTrue(control.allowed("update", "windows"))
        self.assertFalse(control.allowed("update", "linux"))
        self.assertTrue(control.allowed("setup", "mac"))


class Ports(unittest.TestCase):
    def test_read_from_each_start_script(self):
        # Read from the scripts, so this pins what they say today; if
        # one changes, the TUI follows without an edit here.
        self.assertEqual(control.server_port("windows"), 8080)
        self.assertEqual(control.server_port("mac"), 8080)
        self.assertEqual(control.server_port("fedora"), 8080)
        self.assertEqual(control.server_port("linux"), 8080)

    def test_clients_are_configured_for_the_port_servers_use(self):
        # kiln opencode and kiln pi write this port into their configs.
        # start_linux.sh once served on 8081 and every WSL and Linux
        # client config pointed at nothing.
        import opencode
        for plat in ("windows", "mac", "fedora", "linux"):
            with self.subTest(plat):
                self.assertEqual(control.server_port(plat), opencode.PORT)

    def test_missing_script_falls_back(self):
        self.assertEqual(control.server_port("plan9"), 8080)


class Stub(http.server.BaseHTTPRequestHandler):
    """/health the way llama-server, or some other program, answers. With
    guarded, it wants the key, as an older llama-server does. When a test
    sets seen to a list, each request's Authorization header is added."""
    reply = (200, '{"status": "ok"}')
    ctype = "application/json"
    guarded = False
    seen = None

    def do_GET(self):
        auth = self.headers.get("Authorization")
        if self.seen is not None:
            self.seen.append(auth)
        code, body = self.reply
        if self.guarded and auth != "Bearer " + KEY:
            code, body = 401, ('{"error": {"code": 401, "message": '
                               '"Invalid API Key", '
                               '"type": "authentication_error"}}')
        self.send_response(code)
        self.send_header("Content-Type", self.ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


class FindingTheServer(unittest.TestCase):
    def serve(self, reply, **attrs):
        handler = type("H", (Stub,), dict(attrs, reply=reply))
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def test_llama_server_is_known_by_its_replies(self):
        ok = self.serve((200, '{"status": "ok"}'))
        loading = self.serve((503, '{"error": {"code": 503, "message": '
                                   '"Loading model"}}'))
        other = self.serve((200, "<html>OpenTDF</html>"))
        other_json = self.serve((404, '{"detail": "Not Found"}'))
        url = "http://127.0.0.1:%d"
        self.assertEqual(control.llama_health(url % ok, ""), 200)
        self.assertEqual(control.llama_health(url % loading, ""), 503)
        self.assertEqual(control.llama_health(url % other, ""), 0)
        self.assertEqual(control.llama_health(url % other_json, ""), 0)
        # Something other than llama-server holding the port is called
        # out; llama-server itself, or nothing at all, is not.
        self.assertTrue(control.shadowed(other, ""))
        self.assertFalse(control.shadowed(ok, ""))
        self.assertFalse(control.shadowed(_free_port(), ""))

    def test_windows_also_looks_by_machine_name(self):
        self.assertEqual(control.roots("linux", 8081),
                         ["http://127.0.0.1:8081"])
        win = control.roots("windows", 8080)
        self.assertEqual(win[0], "http://127.0.0.1:8080")
        self.assertEqual(win[1], "http://%s:8080" % socket.gethostname())

    def test_a_stopped_server_behind_another_program_gets_a_way_past(self):
        saved = control.processes
        self.addCleanup(setattr, control, "processes", saved)
        control.processes = lambda plat, port: (False, False)
        port = self.serve((200, "<html>OpenTDF</html>"))
        facts = control.server_facts("windows", port, "")
        self.assertEqual((facts["state"], facts["shadowed"]),
                         ("stopped", True))
        # Not 127.0.0.1, which is what reaches the other program.
        self.assertEqual(facts["local_url"], "http://%s:%d/v1"
                         % (socket.gethostname(), port))

    def test_locate_reports_where_it_answered(self):
        port = self.serve((200, '{"status": "ok"}'))
        self.assertEqual(control.locate("linux", port, ""),
                         ("http://127.0.0.1:%d" % port, 200))
        self.assertEqual(control.locate("linux", _free_port(), "")[1], 0)

    def test_the_key_stays_home_from_other_programs(self):
        replies = [((200, '{"hello": 1}'), "application/json"),
                   ((404, "<html>nope</html>"), "text/html")]
        for reply, ctype in replies:
            with self.subTest(status=reply[0]):
                seen = []
                port = self.serve(reply, ctype=ctype, seen=seen)
                root = "http://127.0.0.1:%d" % port
                self.assertEqual(control.llama_health(root, KEY), 0)
                self.assertTrue(control.shadowed(port, KEY))
                self.assertTrue(seen)
                self.assertFalse(any(seen), "the key went out")

    def test_a_public_llama_server_is_never_sent_the_key(self):
        seen = []
        port = self.serve((200, '{"status": "ok"}'), seen=seen)
        root = "http://127.0.0.1:%d" % port
        self.assertEqual(control.llama_health(root, KEY), 200)
        self.assertFalse(control.shadowed(port, KEY))
        self.assertTrue(seen)
        self.assertFalse(any(seen), "the key went out")

    def test_an_older_llama_server_is_sent_the_key_only_when_it_asks(self):
        seen = []
        port = self.serve((200, '{"status": "ok"}'), guarded=True, seen=seen)
        root = "http://127.0.0.1:%d" % port
        self.assertEqual(control.llama_health(root, KEY), 200)
        # Bare first, then the same request with the key once it has
        # answered with its own kind of 401.
        self.assertEqual([a is not None for a in seen], [False, True])


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class JobLogs(unittest.TestCase):
    """A job's log is deleted when the job ends. On Windows a server the
    job started can still hold the file open, so a log that will not go is
    kept and tried again later, rather than left behind for good."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved_tmp = tempfile.tempdir
        tempfile.tempdir = self.tmp.name
        self.addCleanup(setattr, tempfile, "tempdir", saved_tmp)
        saved_windows = control.WINDOWS
        self.addCleanup(setattr, control, "WINDOWS", saved_windows)
        control._undeleted_logs.clear()
        self.addCleanup(control._undeleted_logs.clear)
        # Stands in for a file that a server holds open.
        self.refusing = False
        self.locked = set()
        real_remove = os.remove

        def remove(path):
            if self.refusing or path in self.locked:
                raise PermissionError(13, "The file is in use", path)
            real_remove(path)

        os.remove = remove
        self.addCleanup(setattr, os, "remove", real_remove)

    def touch(self, name):
        path = os.path.join(self.tmp.name, name)
        open(path, "w").close()
        return path

    def finished(self, jid):
        job = control.Job(jid, "get", "t", ["get"])
        job.start([sys.executable, "-c", "print('hi')"], lambda: None)
        self.assertEqual(job.wait(20), 0)
        return job

    def test_a_log_that_will_not_delete_is_tried_again(self):
        self.refusing = True
        first = self.finished("1")
        self.assertTrue(os.path.exists(first.log_path))
        self.assertEqual(control._undeleted_logs, {first.log_path})
        self.refusing = False
        second = self.finished("2")
        self.assertFalse(os.path.exists(first.log_path))
        self.assertFalse(os.path.exists(second.log_path))
        self.assertEqual(control._undeleted_logs, set())

    def test_shutdown_tries_the_kept_logs_again(self):
        self.refusing = True
        job = self.finished("1")
        self.refusing = False
        control.Control().shutdown()
        self.assertFalse(os.path.exists(job.log_path))

    def test_windows_sweeps_logs_that_earlier_runs_left(self):
        old = self.touch("kiln-job-old.log")
        held = self.touch("kiln-job-held.log")
        other = self.touch("notes.log")
        self.locked.add(held)
        control.WINDOWS = True
        control.Control()
        self.assertFalse(os.path.exists(old))
        # Still held by something: skipped, and kept for a later try.
        self.assertTrue(os.path.exists(held))
        self.assertTrue(os.path.exists(other))
        self.assertEqual(control._undeleted_logs, {held})

    def test_unix_does_not_sweep_at_startup(self):
        # Another kiln tui may be writing its log right now.
        stale = self.touch("kiln-job-stale.log")
        control.WINDOWS = False
        control.Control()
        self.assertTrue(os.path.exists(stale))


class NextStep(unittest.TestCase):
    LLAMA = {"installed": True, "build": "1", "mtp": True}
    HAVE = [{"id": "base", "quant": "UD-Q5_K_XL"}]
    NONE = [{"id": "base", "quant": ""}]

    def step(self, plat="linux", llama=None, models=None, state="stopped"):
        n = control.next_step(plat, llama or self.LLAMA,
                              models or self.HAVE, state, {"family": "base"})
        return n and n["action"]

    def test_first_unmet_dependency_wins(self):
        self.assertEqual(self.step(llama={"installed": False, "mtp": False},
                                   models=self.NONE), "setup")
        self.assertEqual(self.step("windows",
                                   llama={"installed": True, "mtp": False}),
                         "update")
        # The shell board has no update verb to suggest.
        self.assertEqual(self.step("linux",
                                   llama={"installed": True, "mtp": False}),
                         "start")
        self.assertEqual(self.step(models=self.NONE), "get")
        self.assertEqual(self.step(), "start")
        self.assertIsNone(self.step(state="ready"))

    def test_start_names_a_variant_on_disk(self):
        n = control.next_step("windows", self.LLAMA,
                              [{"id": "base", "quant": ""},
                               {"id": "9b", "quant": "Q8_0"}], "stopped", None)
        self.assertEqual(n["variant"], "9b")


class Output(unittest.TestCase):
    def job(self):
        return control.Job("1", "get", "t", ["get"])

    def test_crlf_ansi_and_control_bytes(self):
        j = self.job()
        j._append("\x1b[92mok\x1b[0m\r\nnext\x07 line\r\n", final=True)
        self.assertEqual(j.view(0)["text"], "ok\nnext line\n")

    def test_pieces_split_across_reads(self):
        j = self.job()
        j._append("one\r")          # the LF may still be coming
        j._append("\ntwo \x1b[9")   # half an escape
        j._append("2mgreen\x1b[0m\n")
        self.assertEqual(j.view(0)["text"], "one\ntwo green\n")

    def test_progress_bars_keep_their_carriage_returns(self):
        j = self.job()
        j._append(" 10%\r 20%\r", final=True)
        self.assertEqual(j.view(0)["text"], " 10%\r 20%\r")

    def test_offsets_survive_trimming(self):
        j = self.job()
        j.MAX_TEXT = 10
        j._append("0123456789", final=True)
        first = j.view(0)
        self.assertEqual(first["offset"], 10)
        j._append("abcde", final=True)
        later = j.view(first["offset"])
        self.assertEqual(later["text"], "abcde")
        self.assertFalse(later["truncated"])
        # A reader that fell behind the trim is told so.
        stale = j.view(0)
        self.assertTrue(stale["truncated"])
        self.assertEqual(stale["text"], "56789abcde")


class Jobs(unittest.TestCase):
    def test_output_exit_code_and_environment(self):
        done = threading.Event()
        job = control.Job("1", "start", "t", ["start", "base"])
        job.start([sys.executable, "-c",
                   "import os, sys; print('hello');"
                   " print(os.environ.get('KILN_NO_TUNNEL'),"
                   " os.environ['NO_COLOR']);"
                   " print('err', file=sys.stderr); sys.exit(3)"],
                  done.set, {"KILN_NO_TUNNEL": "1"})
        self.assertEqual(job.wait(20), 3)
        self.assertTrue(done.is_set())
        text = job.view(0)["text"]
        # The command line shown says what the scripts were given.
        self.assertIn("$ KILN_NO_TUNNEL=1 kiln start base", text)
        self.assertIn("hello\n", text)
        self.assertIn("1 1\n", text)
        self.assertIn("err\n", text)
        self.assertFalse(os.path.exists(job.log_path))

    def test_cancel(self):
        job = control.Job("1", "get", "t", ["get"])
        job.start([sys.executable, "-c", "import time; time.sleep(60)"],
                  lambda: None)
        time.sleep(0.5)
        job.cancel()
        self.assertIsNotNone(job.wait(20))
        self.assertTrue(job.cancelled)
        self.assertNotEqual(job.code, 0)


class Serialising(unittest.TestCase):
    def setUp(self):
        self.ctl = control.Control()
        self.ctl.command = lambda args: [sys.executable, "-c",
                                         "import time; time.sleep(30)"]

    def tearDown(self):
        for job in list(self.ctl.jobs.values()):
            job.cancel()

    def test_one_job_at_a_time_but_stop_always_runs(self):
        first = self.ctl.run({"action": "opencode"})
        with self.assertRaises(control.Busy) as caught:
            self.ctl.run({"action": "setup"})
        self.assertIs(caught.exception.job, first)
        self.ctl.run({"action": "stop"})

    def test_update_is_refused_off_windows(self):
        self.ctl.plat = "linux"
        with self.assertRaises(ValueError):
            self.ctl.run({"action": "update"})

    def test_shutdown_cancels_all_but_a_start(self):
        self.ctl.run({"action": "opencode"})
        start = self.ctl.spawn("start", "s", ["start", "base"])
        gone = self.ctl.shutdown()
        self.assertEqual(len(gone), 1)
        self.assertFalse(start.cancelled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
