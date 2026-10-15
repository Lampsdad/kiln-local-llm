#!/usr/bin/env python3
"""Tests for how kiln finds and stops its Cloudflare tunnel:
scripts/unix/lib_tunnel.sh, behind `kiln stop` and the start scripts,
and control.processes(), behind kiln tui's tunnel row.

What is worth pinning down is what it leaves alone. kiln stop used to
`pkill -x cloudflared`, which also took down T3 Code's remote access, a
named tunnel, or any other project's quick tunnel. kiln's tunnel is the
quick tunnel to kiln's own port, and nothing else is kiln's to stop.

Hermetic: the "cloudflared" processes are shell scripts by that name,
on a random high port, so a real tunnel on this machine is never
matched. The Windows half (kiln_tunnel.ps1) needs Windows processes,
so only its wiring is checked here.
"""
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "unix", "lib_tunnel.sh")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import control  # noqa: E402


@unittest.skipIf(sys.platform == "win32", "lib_tunnel.sh is the Unix half")
class OnlyOurs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.port = random.randint(50000, 59000)
        self.fake = os.path.join(tmp.name, "cloudflared")
        with open(self.fake, "w") as fh:
            fh.write("#!/bin/sh\nwhile :; do sleep 1; done\n")
        os.chmod(self.fake, 0o755)
        self.procs = {}
        self.addCleanup(self.kill_all)

    def kill_all(self):
        # Each child leads its own session, so the `sleep` a fake starts
        # goes with it rather than outliving the test.
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()

    def spawn(self, name, *argv):
        self.procs[name] = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            start_new_session=True)

    def tunnel(self, name, *args):
        self.spawn(name, self.fake, "tunnel", *args)

    def start_everything(self):
        p = self.port
        # kiln's: start_linux.sh, start_mac.sh / start_fedora.sh,
        # cursor_tunnel.sh.
        self.tunnel("linux", "--url", "http://127.0.0.1:%d" % p)
        self.tunnel("mac", "--url", "http://localhost:%d" % p)
        self.tunnel("cursor", "--no-autoupdate", "--protocol", "quic",
                    "--url", "http://127.0.0.1:%d" % p)
        # Not kiln's.
        self.tunnel("t3", "--no-autoupdate", "--loglevel", "info",
                    "--output", "default", "run")
        self.tunnel("next_port", "--url", "http://127.0.0.1:%d" % (p + 1))
        self.tunnel("longer_port", "--url", "http://127.0.0.1:%d0" % p)
        self.spawn("not_cloudflared", "sh", "-c",
                   "sleep 300; : cloudflared tunnel --url http://127.0.0.1:%d" % p)
        time.sleep(0.5)

    def lib(self, call):
        return subprocess.run(["bash", "-c", '. "$1"; ' + call, "-", LIB],
                              capture_output=True, text=True, timeout=30)

    def alive(self):
        time.sleep(0.5)
        return sorted(n for n, p in self.procs.items() if p.poll() is None)

    def test_lists_only_kilns_tunnels(self):
        self.start_everything()
        r = self.lib("kiln_tunnel_pids %d" % self.port)
        want = sorted(str(self.procs[n].pid) for n in ("linux", "mac", "cursor"))
        self.assertEqual(sorted(r.stdout.split()), want)

    def test_stop_leaves_everyone_elses_running(self):
        self.start_everything()
        self.assertEqual(self.lib("kiln_stop_tunnel %d" % self.port).returncode, 0)
        self.assertEqual(self.alive(), ["longer_port", "next_port",
                                        "not_cloudflared", "t3"])

    def test_stop_with_nothing_to_stop_says_so(self):
        self.tunnel("t3", "--no-autoupdate", "run")
        time.sleep(0.3)
        self.assertEqual(self.lib("kiln_stop_tunnel %d" % self.port).returncode, 1)
        self.assertEqual(self.alive(), ["t3"])

    def test_tui_sees_only_kilns_tunnel(self):
        # Another program's tunnel must not make a stale log URL look live.
        self.tunnel("t3", "--no-autoupdate", "run")
        time.sleep(0.3)
        self.assertFalse(control.processes("linux", self.port)[1])
        self.tunnel("linux", "--url", "http://127.0.0.1:%d" % self.port)
        time.sleep(0.3)
        self.assertTrue(control.processes("linux", self.port)[1])


class NothingKillsByName(unittest.TestCase):
    """Every script goes through the rule above, on both platforms."""

    def scripts(self):
        out = subprocess.run(["git", "ls-files", "*.sh", "*.bat", "*.py"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
        return [f for f in out if not f.startswith(".github/")]

    def test_no_kill_by_process_name(self):
        for f in self.scripts():
            with open(os.path.join(ROOT, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read().lower()
            with self.subTest(f):
                self.assertNotIn("pkill -x cloudflared", text)
                self.assertNotIn("/im cloudflared.exe", text)
                # The llama-server rule, and its tests, are in
                # test_server.py and test_windows_server.py. These are the
                # name-based forms it bans.
                self.assertNotIn("pkill -x llama-server", text)
                self.assertNotIn("pgrep -x llama-server", text)
                self.assertNotIn('"-x", "llama-server"', text)
                self.assertNotIn("/im llama-server.exe", text)
                self.assertNotIn("imagename eq llama-server.exe", text)

    def test_windows_stop_and_start_use_kiln_tunnel_ps1(self):
        for f in ("kiln.bat", "start.bat"):
            with open(os.path.join(ROOT, "scripts", "windows", f)) as fh:
                self.assertIn('"%~dp0kiln_tunnel.ps1" -Port', fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
