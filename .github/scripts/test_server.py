#!/usr/bin/env python3
"""Tests for kiln's llama-server, and the start scripts that run it:
scripts/unix/lib_server.sh, behind `kiln stop`, the status board, the
start scripts and the benchmark; control.processes(), behind kiln tui's
server row; and the wiring of the start scripts themselves.

What is worth pinning down is which llama-server is kiln's. `pkill -x
llama-server` stopped every llama-server on the machine, another
project's and another checkout's on another port included. kiln's is the
one serving kiln's port, as the start scripts launch it. Stopping it has
to wait for the memory to be freed, or the next launch fails to load.

The start scripts cannot be run here: they stop whatever serves port
8080. So their order (the Ctrl+C trap before the health poll, the tunnel
URL polled rather than slept for) is checked as text.

Hermetic: the "llama-server" processes are shell scripts by that name,
on a random high port, so a real server on this machine is never
matched. The MTP probe runs against a fake binary with a large help text.
"""
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNIX_DIR = os.path.join(ROOT, "scripts", "unix")
LIB = os.path.join(UNIX_DIR, "lib_server.sh")
SELECT = os.path.join(UNIX_DIR, "lib_select.sh")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import control  # noqa: E402

LOOP = "while :; do sleep 1; done"


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.port = random.randint(50000, 59000)
        self.procs = {}
        self.addCleanup(self.kill_all)

    def kill_all(self):
        # Each child leads its own session, so a `sleep` that a fake shell
        # started is stopped with it. Only while the leader is unreaped:
        # after that its PID, and so its group, could belong to anyone.
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            p.wait()

    def fake(self, name, body, exe="llama-server"):
        """argv for a stand-in that ps names exe: a copy of bash under
        that name, running body. A script would not do: macOS's ps names
        a script's interpreter, so it would never be recognised there."""
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, exe)
        shutil.copy(shutil.which("bash"), path)
        body_path = os.path.join(d, "body.sh")
        with open(body_path, "w") as fh:
            fh.write(body + "\n")
        return [path, body_path]

    def script(self, name, body, exe="llama-server"):
        # Each fake lives in a directory of its own, so that every one can
        # be called llama-server.
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, exe)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def spawn(self, name, *argv):
        self.procs[name] = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            start_new_session=True)
        return self.procs[name]

    def server(self, name, *args, body=LOOP):
        self.spawn(name, *self.fake(name, body), *args)

    def lib(self, call, *args):
        return subprocess.run(["bash", "-c", "set -euo pipefail; . \"$1\"; " + call, "-", LIB, *args],
                              capture_output=True, text=True, timeout=60)

    def alive(self):
        time.sleep(0.5)
        return sorted(n for n, p in self.procs.items() if p.poll() is None)


@unittest.skipIf(sys.platform == "win32", "lib_server.sh is the Unix half")
class OnlyOurs(Sandbox):
    def start_everything(self):
        p = self.port
        # kiln's, as start_linux.sh and friends launch it.
        self.server("kiln", "--model", "m.gguf", "--port", str(p), "--host", "0.0.0.0")
        # Not kiln's: the next port, a longer port that starts with kiln's,
        # and a process that only mentions kiln's command line in its args.
        self.server("next_port", "--port", str(p + 1))
        self.server("longer_port", "--port", "%d0" % p)
        self.spawn("not_llama", "sh", "-c",
                   "sleep 300; : llama-server --model m.gguf --port %d" % p)
        time.sleep(0.5)

    def test_lists_only_kilns_server(self):
        self.start_everything()
        r = self.lib("kiln_server_pids %d" % self.port)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), [str(self.procs["kiln"].pid)])

    def test_stop_leaves_everyone_elses_running(self):
        self.start_everything()
        self.assertEqual(self.lib("kiln_stop_server %d" % self.port).returncode, 0)
        self.assertEqual(self.alive(), ["longer_port", "next_port", "not_llama"])

    def test_stop_with_nothing_to_stop_says_so(self):
        self.server("next_port", "--port", str(self.port + 1))
        self.spawn("not_llama", "sh", "-c",
                   "sleep 300; : llama-server --model m.gguf --port %d" % self.port)
        time.sleep(0.5)
        self.assertEqual(self.lib("kiln_stop_server %d" % self.port).returncode, 1)
        self.assertEqual(self.alive(), ["next_port", "not_llama"])

    def test_stop_waits_until_the_server_has_gone(self):
        # A server that takes a moment to exit still holds its memory, and
        # the next launch needs it free: stop must not return before then.
        self.server("slow", "--port", str(self.port),
                    body="trap 'sleep 2; exit 0' TERM\n" + LOOP)
        time.sleep(0.5)
        self.assertEqual(self.lib("kiln_stop_server %d" % self.port).returncode, 0)
        self.assertIsNotNone(self.procs["slow"].poll())

    def test_stop_kills_one_that_ignores_term(self):
        self.server("stubborn", "--port", str(self.port), body="trap '' TERM\n" + LOOP)
        time.sleep(0.5)
        self.assertEqual(self.lib("kiln_stop_server %d" % self.port).returncode, 0)
        self.assertIsNotNone(self.procs["stubborn"].poll())

    def test_tui_sees_only_kilns_server(self):
        # Another program's llama-server must not make the board say "running".
        self.server("next_port", "--port", str(self.port + 1))
        time.sleep(0.3)
        self.assertFalse(control.processes("linux", self.port)[0])
        self.server("kiln", "--model", "m.gguf", "--port", str(self.port))
        time.sleep(0.3)
        self.assertTrue(control.processes("linux", self.port)[0])


@unittest.skipIf(sys.platform == "win32", "lib_server.sh is the Unix half")
class TunnelAddress(Sandbox):
    URL = "https://quiet-fox-1.trycloudflare.com"

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "cloudflared.log")

    def wait_for_url(self, pid):
        started = time.time()
        r = subprocess.run(["bash", "-c",
                            "set -euo pipefail; . \"$1\"; "
                            "url=\"$(kiln_wait_tunnel_url \"$2\" \"$3\")\"; echo \"url:[$url]\"",
                            "-", LIB, self.log, str(pid)],
                           capture_output=True, text=True, timeout=90)
        return r, time.time() - started

    def dead_cloudflared(self):
        p = subprocess.Popen(self.fake("dead", "exit 1", exe="cloudflared") + [self.log])
        p.wait()
        return p.pid

    def test_waits_for_an_address_printed_late(self):
        # cloudflared prints its address a few seconds in, as it does on a
        # slow link; the start script must not give up before then.
        body = 'sleep 3\necho "| %s |" >> "$1"\n%s' % (self.URL, LOOP)
        p = self.spawn("cf", *self.fake("late", body, exe="cloudflared"), self.log)
        r, took = self.wait_for_url(p.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "url:[%s]" % self.URL)
        self.assertLess(took, 60)

    def test_takes_an_address_already_printed(self):
        with open(self.log, "w") as fh:
            fh.write("| %s |\n" % self.URL)
        r, _ = self.wait_for_url(self.dead_cloudflared())
        self.assertEqual(r.stdout.strip(), "url:[%s]" % self.URL)

    def test_gives_up_quickly_when_cloudflared_has_died(self):
        # Nothing printed and no process: the caller warns, and carries on.
        open(self.log, "w").close()
        r, took = self.wait_for_url(self.dead_cloudflared())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "url:[]")
        self.assertLess(took, 10)


@unittest.skipIf(sys.platform == "win32", "lib_server.sh is the Unix half")
class ShutdownHandler(Sandbox):
    """kiln_shutdown is the trap the start scripts install. It is run as
    the trap would run it, with the PIDs of two stand-in processes."""

    def test_ctrl_c_stops_the_server_and_the_tunnel(self):
        server = self.spawn("server", "sleep", "300")
        tunnel = self.spawn("tunnel", "sleep", "300")
        r = self.lib('kiln_shutdown "$2" "$3"; echo notreached',
                     str(server.pid), str(tunnel.pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Shutting down...", r.stdout)
        self.assertNotIn("notreached", r.stdout)
        server.wait(timeout=5)
        tunnel.wait(timeout=5)

    def test_a_reused_tunnel_is_left_running(self):
        # start_linux.sh leaves a tunnel that was already up, and sets
        # CF_PID empty for it.
        server = self.spawn("server", "sleep", "300")
        tunnel = self.spawn("tunnel", "sleep", "300")
        r = self.lib('kiln_shutdown "$2" "$3"; echo notreached', str(server.pid), "")
        self.assertEqual(r.returncode, 0, r.stderr)
        server.wait(timeout=5)
        time.sleep(0.5)
        self.assertIsNone(tunnel.poll())


class StartScripts(unittest.TestCase):
    SCRIPTS = ("start_linux.sh", "start_mac.sh", "start_fedora.sh")

    def read(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            return fh.read()

    def test_tunnel_url_is_polled_not_slept_for(self):
        for f in self.SCRIPTS:
            text = self.read(os.path.join("scripts", "unix", f))
            with self.subTest(f):
                self.assertNotIn("sleep 12", text)
                self.assertNotIn("grep -o 'https", text)
                self.assertIn("kiln_wait_tunnel_url", text)
                self.assertIn("WARNING: the tunnel URL has not appeared", text)

    def test_stops_its_own_server_by_port(self):
        for f in self.SCRIPTS:
            text = self.read(os.path.join("scripts", "unix", f))
            with self.subTest(f):
                self.assertIn('kiln_stop_server "$PORT"', text)
                self.assertNotIn("pkill", text)

    def test_ctrl_c_is_handled_from_the_moment_the_server_starts(self):
        for f in self.SCRIPTS:
            lines = self.read(os.path.join("scripts", "unix", f)).splitlines()
            with self.subTest(f):
                traps = [i for i, line in enumerate(lines) if line.startswith("trap ")]
                launch = lines.index('"$BINARY" \\')
                poll = next(i for i, line in enumerate(lines) if line.startswith("until printf"))
                self.assertEqual(len(traps), 1)
                # Single quotes, so the PIDs are read when Ctrl+C arrives.
                self.assertEqual(lines[traps[0]],
                                 "trap 'kiln_shutdown \"$SERVER_PID\" \"$CF_PID\"' INT TERM")
                self.assertLess(launch, traps[0])
                self.assertLess(traps[0], poll)
                # The trap reads CF_PID before any tunnel exists.
                self.assertLess(lines.index('CF_PID=""'), launch)

    def test_no_install_script_advice(self):
        # The front door is kiln; the install scripts are not run by hand.
        for path in [os.path.join("scripts", "unix", f) for f in self.SCRIPTS] + ["kiln.sh"]:
            with self.subTest(path):
                self.assertNotIn("./install_", self.read(path))
        for f in self.SCRIPTS:
            self.assertIn("Run: ./kiln.sh setup", self.read(os.path.join("scripts", "unix", f)))

    def test_kiln_status_and_stop_find_the_server_by_port(self):
        text = self.read("kiln.sh")
        self.assertIn('kiln_stop_server "$port"', text)
        self.assertIn('kiln_server_pids "$port"', text)
        self.assertNotIn("pgrep -x", text)

    def test_benchmark_and_quality_stop_only_kilns_server(self):
        for f in ("benchmark.sh", "quality.sh"):
            text = self.read(os.path.join("scripts", "unix", f))
            with self.subTest(f):
                self.assertIn("kiln_stop_server 8080", text)
                self.assertNotIn("pkill", text)


@unittest.skipIf(sys.platform == "win32", "the probe runs under bash")
class MtpProbe(Sandbox):
    """llama-server's --help is about 60 KB. Piped into grep -q, it quits
    at the first match, and the SIGPIPE for the rest of the help fails
    the probe under pipefail: MTP is then turned off without a word."""

    def test_no_probe_pipes_help_into_grep(self):
        for f in ("kiln.sh", os.path.join("scripts", "unix", "lib_select.sh")):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(f):
                self.assertNotIn("--help 2>&1 | grep", text)

    def probe(self, help_text):
        with open(os.path.join(self.tmp, "help.txt"), "w") as fh:
            fh.write(help_text)
        binary = self.script("bin", 'cat "%s"' % os.path.join(self.tmp, "help.txt"))
        os.makedirs(os.path.join(self.tmp, "models"))
        open(os.path.join(self.tmp, "models", "mtp.gguf"), "w").close()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp,
               "V_PREFIX": "model", "V_ID": "base", "V_LABEL": "test",
               "V_QUANTS": "", "V_MTP_FILES": "mtp.gguf", "V_MMPROJ_FILES": ""}
        cmd = 'set -euo pipefail; . "$1"; kiln_model_args "$2"; printf "%s\\n" "${EXTRA[*]}"'
        return subprocess.run(["bash", "-c", cmd, "-", SELECT, binary], cwd=self.tmp,
                              env=env, capture_output=True, text=True, timeout=60)

    def test_mtp_is_found_behind_a_large_help_text(self):
        padding = "  --flag  padding that makes this help as long as the real one\n"
        help_text = ("usage: llama-server [options]\n"
                     "  --spec-type draft-mtp   MTP speculative decoding\n"
                     + padding * 4000)
        self.assertGreater(len(help_text), 200000)
        r = self.probe(help_text)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MTP speculative decoding: enabled", r.stdout)
        self.assertIn("--spec-type draft-mtp", r.stdout)

    def test_a_build_without_mtp_says_so(self):
        r = self.probe("usage: llama-server [options]\n" + "  --flag  padding\n" * 4000)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MTP speculative decoding: unavailable", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
