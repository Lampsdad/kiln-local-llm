#!/usr/bin/env python3
"""Tests for the Windows front door: scripts\\windows\\kiln.bat, and the
shim at the root that calls it.

What is worth pinning down: a verb exits with the code of the script it
hands off to, because kiln tui judges success by exit code; an unknown
command fails; `kiln key set` writes the key it was given, byte for byte,
whatever cmd would otherwise do to it; `kiln quality` reaches quality.bat;
and a checkout in a folder cmd cannot handle is refused, not run.

The Windows half runs the real batch files under cmd.exe, in copies made
in a temporary folder, so api_key.txt is never written in the checkout.
Anything that would download, install or stop a server is replaced by a
stub in that copy, except the unknown-variant path, which is refused
before any of that happens. The static checks run on any platform.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIN = sys.platform == "win32"

# NO_COLOR keeps lib_ui from asking PowerShell for an ESC byte, and keeps
# the output free of escape codes for the string checks.
ENV = dict(os.environ, NO_COLOR="1")

# Keys cmd would mangle if the batch files did not quote them carefully.
# "pair%...%" has a complete pair of percent signs, and the name is one no
# environment variable has, so cmd leaves it alone on a command line.
KEYS = [
    "plainkey123",
    "with spaces in it",
    "bang!here",
    "caret^here",
    "it's",
    "amp&here",
    "gt>here",
    "lt<here",
    "pipe|here",
    "pct%here",
    "pair%KILN_NO_SUCH_NAME%b",
    "all ! ^ ' & > < | % spaces",
]

BATCH_FILES = [("kiln.bat",)] + [
    ("scripts", "windows", f)
    for f in sorted(os.listdir(os.path.join(ROOT, "scripts", "windows")))
    if f.endswith(".bat")
]

Result = namedtuple("Result", "returncode out")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def section(text, label):
    """The lines after `label` up to the next label."""
    lines = text.split("\n")
    out = []
    for line in lines[lines.index(label) + 1:]:
        if line.startswith(":") and not line.startswith("::"):
            break
        out.append(line)
    return "\n".join(out)


class StaticChecks(unittest.TestCase):
    def test_no_errorlevel_is_read_inside_an_if_block(self):
        # The house style indents every line inside a ( ) block. Inside
        # one, %errorlevel% is expanded when the block is parsed, before
        # the command that sets it has run, so the block has to test it
        # with `if errorlevel` or read it as !errorlevel!.
        bad = []
        for parts in BATCH_FILES:
            for n, line in enumerate(read(*parts).split("\n"), 1):
                if "%errorlevel%" in line.lower() and line[:1] in (" ", "\t"):
                    bad.append("%s:%d" % ("/".join(parts), n))
        self.assertEqual(bad, [])

    def test_quality_is_dispatched_to_quality_bat(self):
        text = read("scripts", "windows", "kiln.bat")
        self.assertRegex(text, r'if /I "%CMD%"=="quality"\s+goto :cmd_quality')
        body = section(text, ":cmd_quality")
        self.assertIn('call "%~dp0quality.bat"', body)
        self.assertIn("exit /b %errorlevel%", body)

    def test_quality_is_in_the_help_and_the_header(self):
        text = read("scripts", "windows", "kiln.bat")
        self.assertIn("quality", section(text, ":cmd_help"))
        self.assertIn("KL-divergence of each quant vs Q8_0", section(text, ":cmd_help"))
        self.assertIn("kiln bench | quality | clean | help", text)

    def test_unknown_command_prints_help_then_fails(self):
        text = read("scripts", "windows", "kiln.bat")
        self.assertRegex(text, r"(?s)Unknown command:.*?\ncall :cmd_help\nexit /b 1\n")

    def test_key_set_takes_the_key_from_the_third_argument_not_arg2(self):
        body = section(read("scripts", "windows", "kiln.bat"), ":key_set")
        self.assertNotIn("%ARG2%", body)
        self.assertLess(body.index("setlocal DisableDelayedExpansion"),
                        body.index('set "KILN_NEWKEY=%~3"'))
        self.assertLess(body.index('set "KILN_NEWKEY=%~3"'), body.index("powershell"))
        self.assertIn("$env:KILN_NEWKEY", body)

    def test_key_set_usage_shows_the_quoting(self):
        self.assertIn('kiln key set "YOUR-KEY"', read("scripts", "windows", "kiln.bat"))
        self.assertIn('kiln key set "MY-SECRET"', read("README.md"))

    def test_path_guard_runs_before_anything_reads_the_path(self):
        guard = 'findstr /L "! %% [ ]"'
        inner = read("scripts", "windows", "kiln.bat")
        self.assertIn(guard, inner)
        self.assertIn("setlocal enabledelayedexpansion", inner)
        self.assertLess(inner.index(guard), inner.index("setlocal enabledelayedexpansion"))

    def test_root_shim_runs_the_cli_as_its_last_line_with_no_call(self):
        # call re-reads % and ^ in the arguments, and returns to this file.
        lines = [l for l in read("kiln.bat").split("\n")
                 if l.strip() and not l.startswith("::")]
        self.assertEqual(lines[-1], '"%~dp0scripts\\windows\\kiln.bat" %*')
        self.assertNotIn("call", " ".join(lines).lower())

    def test_status_powershell_takes_its_paths_from_the_environment(self):
        text = read("scripts", "windows", "kiln.bat")
        self.assertNotIn("(Get-PSDrive C)", text)
        self.assertNotIn("'%CF_LOG%'", text)
        self.assertIn("-LiteralPath $env:KILN_PS_FILE", text)
        # A caret inside the quotes reaches PowerShell as a literal ^ and
        # stops the pipeline with a parse error, so the URL row never showed.
        self.assertNotIn("^| Select-Object", text)

    def test_kiln_path_shim_chains_and_doubles_percent_signs(self):
        # kiln path writes a kiln.cmd that chains to kiln.bat without call,
        # so a percent sign in the checkout path has to be doubled there.
        ps1 = read("scripts", "windows", "kiln_path.ps1")
        self.assertIn('.Replace("%", "%%")', ps1)
        self.assertIn('"`"$target`" %*', ps1)


@unittest.skipUnless(WIN, "runs the batch files under cmd.exe")
class FrontDoor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def checkout(self, name="kiln", stubs=None):
        """A copy of the files kiln runs from, in a folder called `name`.
        `stubs` maps a path under the copy to replacement batch text."""
        dst = os.path.join(tempfile.mkdtemp(dir=self.tmp), name)
        os.makedirs(dst)
        shutil.copy(os.path.join(ROOT, "kiln.bat"), dst)
        shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(dst, "scripts"))
        shutil.copytree(os.path.join(ROOT, "assets"), os.path.join(dst, "assets"))
        for rel, body in (stubs or {}).items():
            with open(os.path.join(dst, *rel.split("/")), "w", newline="\r\n") as fh:
                fh.write(body)
        return dst

    def run_line(self, line):
        """Run a command line the way control.py's Job does: stdin from NUL,
        output captured, the same environment kiln tui gives it."""
        p = subprocess.run(line, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=ENV, timeout=180)
        return Result(p.returncode, p.stdout.decode("latin-1"))

    def run_kiln(self, target, tail):
        """Run `target` the way a user types it at a prompt, through cmd /c."""
        args = " " + tail if tail else ""
        return self.run_line('cmd.exe /d /c ""%s"%s"' % (target, args))

    def shim_for(self, root_kiln):
        """Write a kiln.cmd the way kiln path does, into a temporary folder
        so nothing on the real PATH is touched. Returns its path."""
        shim = os.path.join(tempfile.mkdtemp(dir=self.tmp), "kiln.cmd")
        target = root_kiln.replace("%", "%%")
        with open(shim, "w", newline="\r\n") as fh:
            fh.write('@echo off\n"%s" %%*\n' % target)
        return shim

    def test_root_shim_passes_the_exit_code_as_control_runs_it(self):
        # Control.command() builds /s /c with only the outer quotes stripped.
        root = os.path.join(self.checkout(), "kiln.bat")
        for tail, want in (("help", 0), ("bogus", 1), ("get nosuchvariant", 1)):
            with self.subTest(tail=tail):
                p = self.run_line('cmd.exe /d /s /c ""%s" %s"' % (root, tail))
                self.assertEqual(p.returncode, want, p.out)
                p = self.run_kiln(root, tail)
                self.assertEqual(p.returncode, want, p.out)

    def test_bare_kiln_without_a_terminal_shows_the_status_board(self):
        # The TUI needs a terminal. With stdin from NUL kiln prints the board
        # and returns; it must not wait for a key.
        root = os.path.join(self.checkout(), "kiln.bat")
        p = self.run_kiln(root, "")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("llama.cpp", p.out)

    def test_kiln_cmd_on_path_chains_to_the_checkout(self):
        dst = self.checkout(name="ok")
        shim = self.shim_for(os.path.join(dst, "kiln.bat"))
        p = self.run_kiln(shim, "bogus")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("Unknown command: bogus", p.out)
        p = self.run_kiln(shim, 'key set "pct%here"')
        self.assertEqual(p.returncode, 0, p.out)
        with open(os.path.join(dst, "api_key.txt"), "rb") as fh:
            self.assertTrue(fh.read() == b"pct%here", "key was not stored exactly as typed")

        dst = self.checkout(name="a%b")
        p = self.run_kiln(self.shim_for(os.path.join(dst, "kiln.bat")), "help")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn("cannot run from a folder", p.out)

    def test_unknown_variant_is_refused_before_anything_is_fetched(self):
        dst = self.checkout()
        p = self.run_kiln(os.path.join(dst, "kiln.bat"), "get nosuchvariant")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn('unknown variant "nosuchvariant"', p.out)
        self.assertNotIn("Installing/upgrading", p.out)
        self.assertNotIn("Downloading", p.out)

    def test_unknown_variant_is_refused_before_any_server_is_stopped(self):
        dst = self.checkout()
        p = self.run_kiln(os.path.join(dst, "kiln.bat"), "start nosuchvariant")
        self.assertEqual(p.returncode, 1, p.out)
        self.assertIn('unknown variant "nosuchvariant"', p.out)
        self.assertNotIn("Stopping any existing instances", p.out)

    def test_each_verb_exits_with_the_code_of_the_script_it_runs(self):
        # Stubs stand in for the scripts, so nothing downloads or starts.
        # A stub that exits 7 is what a failed download or start looks like.
        stub = "@echo off\necho STUB-RAN\nexit /b 7\n"
        stubs = {"scripts/windows/%s" % n: stub
                 for n in ("download.bat", "start.bat", "launch.bat")}
        dst = self.checkout(stubs=stubs)
        kiln = os.path.join(dst, "kiln.bat")
        for tail in ("get base", "get", "start base", "start"):
            with self.subTest(tail=tail):
                p = self.run_kiln(kiln, tail)
                self.assertEqual(p.returncode, 7, p.out)
                self.assertIn("STUB-RAN", p.out)

    def test_quality_runs_quality_bat_and_passes_its_exit_code(self):
        stubs = {"scripts/windows/quality.bat":
                 "@echo off\necho QUALITY-STUB\nexit /b 5\n"}
        dst = self.checkout(stubs=stubs)
        p = self.run_kiln(os.path.join(dst, "kiln.bat"), "quality")
        self.assertEqual(p.returncode, 5, p.out)
        self.assertIn("QUALITY-STUB", p.out)

    def test_unknown_command_fails_and_still_prints_help(self):
        dst = self.checkout()
        kiln = os.path.join(dst, "kiln.bat")
        bad = self.run_kiln(kiln, "bogus")
        self.assertEqual(bad.returncode, 1, bad.out)
        self.assertIn("Unknown command: bogus", bad.out)
        self.assertIn("Usage:", bad.out)
        good = self.run_kiln(kiln, "help")
        self.assertEqual(good.returncode, 0, good.out)
        self.assertIn("quality", good.out)

    def test_key_set_stores_the_key_exactly_as_typed(self):
        for via_root in (False, True):
            for i, key in enumerate(KEYS):
                with self.subTest(root=via_root, key=i):
                    dst = self.checkout()
                    path = os.path.join(dst, "api_key.txt")
                    self.assertFalse(os.path.exists(path), "api_key.txt already present")
                    if via_root:
                        target = os.path.join(dst, "kiln.bat")
                    else:
                        target = os.path.join(dst, "scripts", "windows", "kiln.bat")
                    p = self.run_kiln(target, 'key set "%s"' % key)
                    self.assertEqual(p.returncode, 0, p.out)
                    self.assertTrue(os.path.exists(path), "no api_key.txt was written")
                    with open(path, "rb") as fh:
                        stored = fh.read()
                    # Compared as bytes: a byte order mark or a trailing
                    # newline would make them unequal.
                    self.assertTrue(stored == key.encode("utf-8"),
                                    "key was not stored exactly as typed")

    def test_key_set_refuses_an_empty_key_and_a_double_quote(self):
        for tail, want in (('key set ""', "empty"), ('key set "a""b"', "double quote")):
            with self.subTest(tail=tail):
                dst = self.checkout()
                self.assertFalse(os.path.exists(os.path.join(dst, "api_key.txt")),
                                 "api_key.txt already present")
                p = self.run_kiln(os.path.join(dst, "kiln.bat"), tail)
                self.assertEqual(p.returncode, 1, p.out)
                self.assertIn(want, p.out)
                self.assertFalse(os.path.exists(os.path.join(dst, "api_key.txt")))

    def test_a_bang_percent_or_bracket_in_the_folder_is_refused(self):
        for name in ("a!b", "a%b", "a[1]b"):
            with self.subTest(folder=name):
                dst = self.checkout(name=name)
                for target in (os.path.join(dst, "kiln.bat"),
                               os.path.join(dst, "scripts", "windows", "kiln.bat")):
                    p = self.run_kiln(target, "help")
                    self.assertEqual(p.returncode, 1, p.out)
                    self.assertIn("contains ! or % or [ or ].", p.out)

    def test_a_plain_folder_still_runs(self):
        # An apostrophe is fine; only ! % [ and ] are refused.
        for name in ("ok", "a'b"):
            with self.subTest(folder=name):
                dst = self.checkout(name=name)
                for target in (os.path.join(dst, "kiln.bat"),
                               os.path.join(dst, "scripts", "windows", "kiln.bat")):
                    p = self.run_kiln(target, "help")
                    self.assertEqual(p.returncode, 0, p.out)
                    self.assertIn("Usage:", p.out)
                    self.assertNotIn("cannot run from a folder", p.out)

    def test_status_board_names_the_drive_the_checkout_is_on(self):
        root = os.path.join(self.checkout(), "kiln.bat")
        drive = os.path.splitdrive(root)[0].upper()
        p = self.run_kiln(root, "status")
        self.assertEqual(p.returncode, 0, p.out)
        self.assertIn("ON " + drive, p.out.upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
