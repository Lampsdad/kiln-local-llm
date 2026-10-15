#!/usr/bin/env python3
"""Tests for the Unix side of the API key: scripts/unix/lib_api_key.sh,
which every start script sources, and `kiln key` in kiln.sh.

What is worth pinning down: the key is 24 bytes whichever tool makes it,
the key file is never readable by anyone else -- not while it is being
written, and not after an older run left it world-readable -- and
`kiln key set` writes the value it was given and refuses what llama-server
could not read back.

Hermetic: every run happens in a temporary directory, so the real
api_key.txt is never read. Skipped on Windows, which has its own library.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "unix", "lib_api_key.sh")
KILN = os.path.join(ROOT, "kiln.sh")
BASH = shutil.which("bash")

# The line the library's /dev/urandom branch tests. Replaced in a copy
# of the library to reach the uuidgen branch, which Linux never gets to.
URANDOM_TEST = "[ -r /dev/urandom ]"

# Values a user might type. None of them is a real credential.
VALUES = ["plain-key", "with spaces in it", "it's \"quoted\"",
          "$HOME $(echo hi) `id`", "bang!bang", "back\\slash", "tab\there"]


# The library echoes its own progress to stdout, so the key is printed
# after a marker and read back from behind it.
PRINT_KEY = "printf 'KEY=%s' \"$API_KEY\""


def printed_key(done):
    return done.stdout.rsplit("KEY=", 1)[-1]


def key_bytes(d):
    with open(os.path.join(d, "api_key.txt"), "rb") as fh:
        return fh.read()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def toolbin(dest, *names):
    """A directory holding only these tools, so a test can take others away."""
    os.makedirs(dest)
    for name in names:
        os.symlink(shutil.which(name), os.path.join(dest, name))
    return dest


# A stand-in for openssl. It records its arguments, and the mode of the
# key file at the moment it runs. The shell opens that file before the
# command starts, so this is the earliest a reader could look at it.
FAKE_OPENSSL = """#!%(python)s
import os, stat, sys, time
for _ in range(200):
    if os.path.exists("api_key.txt"):
        break
    time.sleep(0.05)
with open(%(log)r, "w") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
    if os.path.exists("api_key.txt"):
        fh.write("%%o\\n" %% stat.S_IMODE(os.stat("api_key.txt").st_mode))
sys.stdout.write("ab" * 24)
"""

FAKE_UUIDGEN = "#!/bin/sh\necho AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE\n"


@unittest.skipIf(os.name == "nt", "the Unix key library runs under bash")
class Library(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def source(self, script="", path=None, lib=LIB, umask="022"):
        """Source the library from a fresh shell, the way a start script does.
        The shell reports only what it prints; the key itself is compared
        in code and never shown."""
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        return subprocess.run(
            [BASH, "-c", 'umask %s; . "$1"; %s' % (umask, script), "bash", lib],
            cwd=self.dir, env=env, capture_output=True, text=True, timeout=60)

    def assert_is_a_key(self, data, length=48):
        self.assertEqual(len(data), length)
        self.assertTrue(set(data.decode()) <= set("0123456789abcdef"))

    def test_openssl_is_asked_for_24_bytes(self):
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        log = os.path.join(self.dir, "openssl.log")
        fake = os.path.join(bindir, "openssl")
        with open(fake, "w") as fh:
            fh.write(FAKE_OPENSSL % {"python": sys.executable, "log": log})
        os.chmod(fake, 0o755)
        path = bindir + os.pathsep + os.environ.get("PATH", "")

        done = self.source(PRINT_KEY, path=path)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(log) as fh:
            argv = fh.readline().split()
        self.assertEqual(argv, ["rand", "-hex", "24"])
        self.assert_is_a_key(key_bytes(self.dir))
        self.assertTrue(printed_key(done) == key_bytes(self.dir).decode())

    def test_the_key_file_is_never_visible_to_others_while_written(self):
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        log = os.path.join(self.dir, "openssl.log")
        fake = os.path.join(bindir, "openssl")
        with open(fake, "w") as fh:
            fh.write(FAKE_OPENSSL % {"python": sys.executable, "log": log})
        os.chmod(fake, 0o755)
        path = bindir + os.pathsep + os.environ.get("PATH", "")

        # umask 022 is what makes a careless write world-readable.
        self.source(path=path, umask="022")
        with open(log) as fh:
            seen = fh.read().splitlines()
        self.assertEqual(seen[1:], ["600"])
        self.assertEqual(mode(os.path.join(self.dir, "api_key.txt")), 0o600)

    def test_od_fallback_makes_24_bytes(self):
        bindir = toolbin(os.path.join(self.dir, "bin"),
                         "tr", "od", "cut", "chmod")
        done = self.source(PRINT_KEY, path=bindir)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_is_a_key(key_bytes(self.dir))
        self.assertEqual(mode(os.path.join(self.dir, "api_key.txt")), 0o600)
        self.assertTrue(printed_key(done) == key_bytes(self.dir).decode())

    def test_uuidgen_is_the_last_resort_and_takes_two(self):
        # Without /dev/urandom the uuidgen branch is the one left. A copy
        # of the library with that check pointed at a missing file reaches it.
        with open(LIB) as fh:
            src = fh.read()
        self.assertEqual(src.count(URANDOM_TEST), 1)
        lib = os.path.join(self.dir, "lib_copy.sh")
        with open(lib, "w") as fh:
            fh.write(src.replace(URANDOM_TEST, "[ -r /nonexistent/urandom ]"))
        bindir = toolbin(os.path.join(self.dir, "bin"), "tr", "cut", "chmod")
        uuidgen = os.path.join(bindir, "uuidgen")
        with open(uuidgen, "w") as fh:
            fh.write(FAKE_UUIDGEN)
        os.chmod(uuidgen, 0o755)

        done = self.source(PRINT_KEY, path=bindir, lib=lib)
        self.assertEqual(done.returncode, 0, done.stderr)
        # Two uuids, dashes and case dropped, cut to 24 bytes' worth.
        expected = ("aaaaaaaabbbbccccddddeeeeeeeeeeee" * 2)[:48]
        self.assertTrue(key_bytes(self.dir).decode() == expected)
        self.assertEqual(mode(os.path.join(self.dir, "api_key.txt")), 0o600)

    def test_no_tool_at_all_is_an_error(self):
        with open(LIB) as fh:
            src = fh.read()
        lib = os.path.join(self.dir, "lib_copy.sh")
        with open(lib, "w") as fh:
            fh.write(src.replace(URANDOM_TEST, "[ -r /nonexistent/urandom ]"))
        bindir = toolbin(os.path.join(self.dir, "bin"), "tr", "chmod")
        done = self.source(path=bindir, lib=lib)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no way to generate", done.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "api_key.txt")))

    def test_existing_world_readable_key_is_locked_down(self):
        # Written by hand, or by an older kiln, under a loose umask.
        path = os.path.join(self.dir, "api_key.txt")
        with open(path, "w") as fh:
            fh.write("hand-made-key")
        os.chmod(path, 0o644)
        before = key_bytes(self.dir)

        done = self.source(PRINT_KEY)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(mode(path), 0o600)
        self.assertTrue(key_bytes(self.dir) == before)
        self.assertTrue(printed_key(done) == before.decode())


@unittest.skipIf(os.name == "nt", "kiln.sh is the Unix front door")
class KilnKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A copy of the checkout: kiln.sh works out its own root, so every
        # file it touches lands here and not in the real checkout.
        self.dir = os.path.join(self.tmp.name, "kiln")
        os.makedirs(self.dir)
        shutil.copy(KILN, self.dir)
        shutil.copytree(os.path.join(ROOT, "scripts"),
                        os.path.join(self.dir, "scripts"))
        shutil.copytree(os.path.join(ROOT, "assets"),
                        os.path.join(self.dir, "assets"))
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.key_file = os.path.join(self.dir, "api_key.txt")

    def kiln(self, *args):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run(
            [BASH, os.path.join(self.dir, "kiln.sh")] + list(args),
            cwd=self.dir, env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=120,
            preexec_fn=lambda: os.umask(0o022))

    def write_key(self, text, perm=0o600):
        with open(self.key_file, "w") as fh:
            fh.write(text)
        os.chmod(self.key_file, perm)

    def test_set_writes_the_value_exactly(self):
        for value in VALUES:
            with self.subTest(i=VALUES.index(value)):
                done = self.kiln("key", "set", value)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(key_bytes(self.dir) == value.encode())
                self.assertEqual(mode(self.key_file), 0o600)
                self.assertIn("Restart the server", done.stdout)

    def test_set_on_a_loose_file_locks_it_down(self):
        self.write_key("old-key", perm=0o644)
        done = self.kiln("key", "set", "new-key")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(key_bytes(self.dir) == b"new-key")
        self.assertEqual(mode(self.key_file), 0o600)

    def test_refuses_what_cannot_be_read_back(self):
        for value in ["", "two\nlines", "cr\rhere", "trailing\n"]:
            with self.subTest(value=repr(value)):
                done = self.kiln("key", "set", value)
                self.assertEqual(done.returncode, 1)
                self.assertIn("usage: kiln key", done.stderr)
                self.assertFalse(os.path.exists(self.key_file))
        self.write_key("keep-me")
        for value in ["", "two\nlines"]:
            done = self.kiln("key", "set", value)
            self.assertEqual(done.returncode, 1)
        self.assertTrue(key_bytes(self.dir) == b"keep-me")

    def test_set_without_a_value_is_refused(self):
        done = self.kiln("key", "set")
        self.assertEqual(done.returncode, 1)
        self.assertIn("usage: kiln key", done.stderr)
        self.assertFalse(os.path.exists(self.key_file))

    def test_unknown_option_prints_usage_to_stderr(self):
        done = self.kiln("key", "bogus")
        self.assertEqual(done.returncode, 1)
        self.assertIn("usage: kiln key [show|rotate|set KEY]", done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertFalse(os.path.exists(self.key_file))

    def test_show_generates_once_and_keeps_the_key(self):
        done = self.kiln("key")
        self.assertEqual(done.returncode, 0, done.stderr)
        first = key_bytes(self.dir)
        self.assertTrue(re.match(rb"^[0-9a-f]{48}$", first) is not None)
        self.assertEqual(mode(self.key_file), 0o600)
        self.assertTrue(first.decode() in done.stdout)

        done = self.kiln("key", "show")
        self.assertTrue(key_bytes(self.dir) == first)
        self.assertTrue(first.decode() in done.stdout)

    def test_rotate_replaces_the_key(self):
        self.write_key("old-key")
        done = self.kiln("key", "rotate")
        self.assertEqual(done.returncode, 0, done.stderr)
        new = key_bytes(self.dir)
        self.assertFalse(new == b"old-key")
        self.assertTrue(re.match(rb"^[0-9a-f]{48}$", new) is not None)
        self.assertEqual(mode(self.key_file), 0o600)

    def test_the_start_scripts_read_back_what_was_set(self):
        value = "it's $HOME \"x\" ! \\ end"
        self.assertEqual(self.kiln("key", "set", value).returncode, 0)
        lib = os.path.join(self.dir, "scripts", "unix", "lib_api_key.sh")
        done = subprocess.run([BASH, "-c", '. "$1"; ' + PRINT_KEY, "bash", lib],
                              cwd=self.dir, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(printed_key(done) == value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
