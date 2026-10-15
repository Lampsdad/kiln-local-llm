#!/usr/bin/env python3
"""Tests for how kiln finds and stops its llama-server on Windows:
scripts/windows/kiln_server.ps1, behind `kiln stop`, start.bat, the
benchmark and quality scripts, and control.processes(), behind kiln
tui's server row.

What is worth pinning down is what it leaves alone. `taskkill /IM
llama-server.exe` stopped every llama-server.exe on the machine, and the
status checks counted any of them as kiln's. kiln's server is the one
started with `--port PORT` for kiln's port, and nothing else is kiln's
to stop.

The wiring checks run everywhere. The behaviour checks need Windows
processes, so they run only there. Their llama-server.exe is a copy of
cmd.exe under that name, waiting for a line on stdin. It starts no
children, so stopping it leaves nothing running behind.
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.join(ROOT, "scripts", "windows", "kiln_server.ps1")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import control  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class Wiring(unittest.TestCase):
    def test_server_script_is_crlf_and_ascii(self):
        with open(SERVER, "rb") as fh:
            data = fh.read()
        self.assertTrue(all(b < 0x80 for b in data))
        stripped = data.replace(b"\r\n", b"")
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", stripped)
        self.assertNotIn(b"\r", stripped)

    def test_start_checks_and_stops_only_its_port(self):
        text = read("scripts", "windows", "start.bat")
        self.assertIn('"%~dp0kiln_server.ps1" -Port %PORT% -Stop', text)
        # A server that died during startup is seen by its exit code.
        self.assertRegex(text, r'kiln_server\.ps1" -Port %PORT% >nul 2>&1\nif errorlevel 1 \(')

    def test_kiln_stop_and_status_use_port_8080(self):
        text = read("scripts", "windows", "kiln.bat")
        self.assertRegex(text, r'kiln_server\.ps1" -Port 8080 -Stop\nif not errorlevel 1 set "HIT=1"')
        self.assertRegex(text, r'kiln_server\.ps1" -Port 8080 >nul 2>&1\nif not errorlevel 1 \(')

    def test_benchmark_and_quality_free_only_kilns_server(self):
        for f in ("benchmark.bat", "quality.bat"):
            with self.subTest(f):
                self.assertIn('"%~dp0kiln_server.ps1" -Port 8080 -Stop',
                              read("scripts", "windows", f))

    def test_tui_asks_the_script(self):
        self.assertIn('"kiln_server.ps1"', read("scripts", "control.py"))


@unittest.skipUnless(sys.platform == "win32", "needs Windows processes")
class OnlyKilnsServer(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.port = random.randint(50000, 59000)
        root = os.environ.get("SystemRoot", r"C:\Windows")
        self.fake = os.path.join(tmp.name, "llama-server.exe")
        shutil.copyfile(os.path.join(root, "System32", "cmd.exe"), self.fake)
        self.procs = {}
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdin.close()

    def spawn(self, name, args):
        # `set /p` blocks until a line arrives on stdin, so the fake does
        # nothing else and has no child process to outlive a kill.
        cmdline = '"%s" /d /c "set /p x= & rem" %s' % (self.fake, args)
        self.procs[name] = subprocess.Popen(cmdline, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.3)

    def script(self, *args):
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", SERVER, "-Port", str(self.port), *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120)

    def alive(self):
        time.sleep(0.5)
        return sorted(n for n, p in self.procs.items() if p.poll() is None)

    def test_finds_only_the_server_on_its_port(self):
        self.spawn("next", "--port %d" % (self.port + 1))
        self.spawn("longer", "--port %d0" % self.port)
        self.assertEqual(self.script().returncode, 1)
        self.spawn("kiln", "--port %d" % self.port)
        self.assertEqual(self.script().returncode, 0)

    def test_stop_takes_only_kilns_server(self):
        self.spawn("kiln", "--port %d" % self.port)
        self.spawn("next", "--port %d" % (self.port + 1))
        self.spawn("longer", "--port %d0" % self.port)
        r = self.script("-Stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.alive(), ["longer", "next"])
        self.assertEqual(self.script().returncode, 1)
        self.assertEqual(self.script("-Stop").returncode, 1)
        self.assertEqual(self.alive(), ["longer", "next"])

    def test_equals_form_is_kilns_too(self):
        self.spawn("kiln", "--port=%d" % self.port)
        self.assertEqual(self.script().returncode, 0)
        self.assertEqual(self.script("-Stop").returncode, 0)
        self.assertEqual(self.alive(), [])

    def test_tui_sees_only_kilns_server(self):
        self.spawn("next", "--port %d" % (self.port + 1))
        self.assertFalse(control.processes("windows", self.port)[0])
        self.spawn("kiln", "--port %d" % self.port)
        self.assertTrue(control.processes("windows", self.port)[0])
        self.assertEqual(self.script("-Stop").returncode, 0)
        self.assertFalse(control.processes("windows", self.port)[0])
        self.assertEqual(self.alive(), ["next"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
