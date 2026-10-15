#!/usr/bin/env python3
"""Tests for `kiln path` on macOS and Linux: scripts/unix/kiln_path.sh,
which the setup scripts also run.

What is worth pinning down is what it will replace and delete. It owns a
~/.local/bin/kiln link to some kiln.sh, and nothing else by that name:
another tool's kiln is left alone, both when adding and when removing.

Hermetic: HOME is a temporary directory, so the real ~/.local/bin is
never touched. The Windows half (kiln_path.ps1) edits the user PATH in
the registry, so it is not run here.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "unix", "kiln_path.sh")
KILN_SH = os.path.join(os.path.realpath(ROOT), "kiln.sh")


@unittest.skipIf(sys.platform == "win32", "kiln_path.sh is the macOS/Linux half")
class KilnPath(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.bin = os.path.join(self.home, ".local", "bin")
        self.link = os.path.join(self.bin, "kiln")
        self.path = "/usr/bin:/bin"

    def run_script(self, *args, cmd=None, cwd=None):
        env = {"HOME": self.home, "PATH": self.path, "SHELL": "/bin/bash"}
        return subprocess.run(cmd or ["bash", SCRIPT, *args], env=env, cwd=cwd,
                              capture_output=True, text=True, timeout=60)

    def foreign(self):
        """Something else called kiln, already in ~/.local/bin."""
        os.makedirs(self.bin)
        with open(self.link, "w") as fh:
            fh.write("#!/bin/sh\necho other kiln\n")

    def test_links_kiln_sh_and_it_runs_from_another_directory(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(self.link), KILN_SH)
        r = self.run_script(cmd=[self.link, "help"], cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("path [--remove]", r.stdout)

    def test_kiln_sh_dispatches_path(self):
        r = self.run_script(cmd=["bash", os.path.join(ROOT, "kiln.sh"), "path"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(self.link), KILN_SH)

    def test_says_how_to_add_local_bin_only_when_it_is_missing(self):
        self.assertIn("not on your PATH", self.run_script().stdout)
        self.path = self.bin + ":/usr/bin:/bin"
        self.assertNotIn("not on your PATH", self.run_script().stdout)
        self.path = "/usr/bin:" + self.bin + "/:/bin"
        self.assertNotIn("not on your PATH", self.run_script().stdout)

    def test_running_it_again_is_harmless(self):
        self.run_script()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(self.link), KILN_SH)

    def test_a_link_to_another_checkout_moves_here(self):
        os.makedirs(self.bin)
        os.symlink("/elsewhere/kiln-local-llm/kiln.sh", self.link)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(os.readlink(self.link), KILN_SH)

    def test_leaves_another_tools_kiln_alone(self):
        self.foreign()
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not made by kiln", r.stderr)
        with open(self.link) as fh:
            self.assertIn("other kiln", fh.read())

    def test_leaves_another_tools_kiln_link_alone(self):
        os.makedirs(self.bin)
        os.symlink("/opt/other/bin/kiln", self.link)
        self.assertEqual(self.run_script().returncode, 1)
        self.assertEqual(os.readlink(self.link), "/opt/other/bin/kiln")

    def test_remove_undoes_it(self):
        self.run_script()
        r = self.run_script("--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.lexists(self.link))
        r = self.run_script("--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing to remove", r.stdout)

    def test_remove_leaves_another_tools_kiln_alone(self):
        self.foreign()
        self.assertEqual(self.run_script("--remove").returncode, 1)
        self.assertTrue(os.path.isfile(self.link))

    def test_rejects_an_unknown_argument(self):
        r = self.run_script("--frobnicate")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.lexists(self.link))


class SetupRunsIt(unittest.TestCase):
    """Setup puts kiln on PATH on every platform, through this one script."""

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_every_unix_installer(self):
        for name in ("install_linux.sh", "install_fedora.sh", "install_mac.sh"):
            with self.subTest(name):
                self.assertIn("scripts/unix/kiln_path.sh", self.read("scripts", "unix", name))

    def test_windows_installer_and_verb(self):
        self.assertIn('"%~dp0kiln_path.ps1"', self.read("scripts", "windows", "install.bat"))
        kiln_bat = self.read("scripts", "windows", "kiln.bat")
        self.assertIn('if /I "%CMD%"=="path"     goto :cmd_path', kiln_bat)
        self.assertIn('"%~dp0kiln_path.ps1" %PATH_FLAG%', kiln_bat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
