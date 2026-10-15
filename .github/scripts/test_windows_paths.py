#!/usr/bin/env python3
"""Tests for the Windows batch scripts that handle paths and disk space.

Three things are pinned here. A path must never sit inside a
single-quoted PowerShell string: a ' in the folder name ends the string,
and [ ] are wildcards to -Path. The download free-space check is done by
Python, on the drive the checkout is on, with decimal numbers. And when
no quant in the download menu is recommended, the default is the
smallest file, not the largest.

The static checks read the .bat files and run anywhere. The rest run the
real batch lines under cmd.exe, so they need Windows and its Python and
are skipped elsewhere.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIN = os.path.join(ROOT, "scripts", "windows")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import hardware  # noqa: E402

WINDOWS = sys.platform == "win32"

# A %VAR% inside '...' is fine when its value never comes from the user's
# disk: GitHub release tags, asset names and URLs, and a port number.
NOT_PATHS = {"TAG", "URL", "ZIP", "CUDART", "PORT"}

PS_COMMAND = re.compile(r"""(?:^|[\s(`"])powershell(?:\.exe)?\s""", re.IGNORECASE)
SINGLE_QUOTED = re.compile(r"'[^']*'")
CMD_VAR = re.compile(r"%(\w+)%")


def read_lines(name):
    with open(os.path.join(WIN, name), encoding="utf-8") as fh:
        return fh.read().splitlines()


def code_lines_with(name, text):
    """Lines of a batch file that contain text, comments excluded."""
    return [line for line in read_lines(name)
            if text in line and not line.strip().startswith("::")]


def powershell_lines(name):
    """Yield (number, line) for each line of a PowerShell command: the
    powershell line itself, and the lines it continues with ^."""
    continued = False
    for number, line in enumerate(read_lines(name), 1):
        lower = line.strip().lower()
        if lower.startswith("::") or lower.startswith("rem "):
            continued = False
            continue
        if continued or PS_COMMAND.search(line):
            yield number, line
            continued = line.rstrip().endswith("^")
        else:
            continued = False


def path_interpolations(name):
    """(number, var) for each %VAR% inside a single-quoted PowerShell string."""
    for number, line in powershell_lines(name):
        for quoted in SINGLE_QUOTED.findall(line):
            for var in CMD_VAR.findall(quoted):
                yield number, var


def short_path(path):
    """The 8.3 form of a path. The download script runs its Python
    unquoted, so a space in the interpreter's path would split it."""
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
    return buf.value if n else path


class StaticChecks(unittest.TestCase):
    def test_no_path_inside_a_single_quoted_powershell_string(self):
        found = []
        for name in sorted(os.listdir(WIN)):
            if not name.endswith(".bat"):
                continue
            for number, var in path_interpolations(name):
                if var not in NOT_PATHS:
                    found.append("%s:%d %%%s%%" % (name, number, var))
        self.assertEqual(found, [],
                         "pass paths through $env:, never inside '...'")

    def test_free_space_is_not_read_from_c_alone(self):
        for name in ("download.bat", "cleanup.bat", "kiln.bat"):
            self.assertEqual(code_lines_with(name, "Get-PSDrive C"), [], name)

    def test_no_menu_falls_back_to_the_first_quant(self):
        self.assertEqual(code_lines_with("download.bat", 'set "CHOICE=1"'), [])

    def test_last_quant_row_is_the_smallest_in_every_family(self):
        # download.bat's default when nothing is marked is the last row.
        for fam_id, fam in hardware.MODELS.items():
            sizes = [nbytes for _, nbytes in fam["quants"]]
            self.assertEqual(sizes[-1], min(sizes), fam_id)


@unittest.skipUnless(WINDOWS, "runs the batch lines under cmd.exe")
class Behaviour(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.py = short_path(sys.executable)

    def run_cmd_lines(self, lines, stdin=b"", cwd=None):
        """Run lines as a CRLF .cmd file under cmd.exe. Returns (exit code, output)."""
        fd, path = tempfile.mkstemp(suffix=".cmd", dir=self.dir)
        with os.fdopen(fd, "w", encoding="ascii", newline="\r\n") as fh:
            fh.write("\n".join(lines) + "\n")
        r = subprocess.run(["cmd.exe", "/d", "/c", path], cwd=cwd or self.dir,
                           input=stdin, capture_output=True, timeout=300)
        return r.returncode, r.stdout.decode("utf-8", errors="replace")

    def test_free_space_check_exit_codes_and_drive(self):
        lines = code_lines_with("download.bat", "shutil.disk_usage")
        self.assertEqual(len(lines), 1, "download.bat needs one Python free-space check")
        drive = os.path.splitdrive(self.dir)[0]
        for total, want in (("1.0", 0), ("999999.0", 1)):
            code, out = self.run_cmd_lines([
                "@echo off",
                'set "PY_EXE=%s"' % self.py,
                'set "REC_TOTAL_GB=%s"' % total,
                lines[0],
                "echo EXIT=%errorlevel%",
            ])
            self.assertEqual(code, 0, out)
            self.assertIn("Free space on %s : " % drive, out)
            self.assertIn("Recommended free  : ", out)
            self.assertIn("EXIT=%d" % want, out)

    def menu_lines(self):
        lines = read_lines("download.bat")
        start = lines.index('set "N=0"')
        end = lines.index(":: Validate before indexing: an out-of-range pick would silently")
        return lines[start:end]

    def run_menu(self, rows):
        """The quant menu, fed a fake hardware.py listing and an empty answer."""
        hw = os.path.join(self.dir, "fake_hardware.py")
        with open(hw, "w", encoding="ascii") as fh:
            fh.write("for row in %r:\n    print(row)\n" % (rows,))
        return self.run_cmd_lines(
            ["@echo off", "setlocal enabledelayedexpansion",
             'set "PY_EXE=%s"' % self.py, 'set "HW=%s"' % hw, 'set "V_ID=base"']
            + self.menu_lines()
            + ["echo CHOICE=%CHOICE%"],
            stdin=b"\r\n")

    ROWS = [
        "UD-Q8_K_XL|31.5|0|-|-|0|0",
        "UD-Q6_K_XL|25.3|0|-|-|0|0",
        "UD-Q4_K_XL|17.6|0|-|-|0|0",
        "UD-IQ2_S|8.4|12288|off|off|1|0",
    ]

    def test_with_nothing_marked_the_default_is_the_smallest(self):
        code, out = self.run_menu(self.ROWS)
        self.assertEqual(code, 0, out)
        self.assertIn("CHOICE=4", out)
        self.assertIn("the default is the smallest", out)

    def test_a_marked_row_is_still_the_default(self):
        rows = list(self.ROWS)
        rows[1] = rows[1][:-1] + "1"
        code, out = self.run_menu(rows)
        self.assertEqual(code, 0, out)
        self.assertIn("CHOICE=2", out)
        self.assertNotIn("the default is the smallest", out)

    def test_api_key_is_written_into_a_folder_with_quote_and_brackets(self):
        for name in ("O'Brien [x]", "brackets [x]"):
            with self.subTest(folder=name):
                root = os.path.join(self.dir, name)
                shutil.copytree(WIN, os.path.join(root, "scripts", "windows"))
                bat = os.path.join(root, "scripts", "windows", "lib_api_key.bat")
                code, out = self.run_cmd_lines(
                    ["@echo off", 'call "%s"' % bat, "echo EXIT=%errorlevel%"])
                self.assertEqual(code, 0, out)
                key_file = os.path.join(root, "api_key.txt")
                self.assertTrue(os.path.isfile(key_file), out)
                with open(key_file, encoding="ascii") as fh:
                    key = fh.read()
                self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{48}", key))


if __name__ == "__main__":
    unittest.main()
