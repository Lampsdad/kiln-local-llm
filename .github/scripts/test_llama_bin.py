#!/usr/bin/env python3
"""Tests for scripts/unix/lib_llama_bin.sh, which every Unix setup uses to
fetch llama.cpp into llama-bin/.

What is worth pinning down is which build it takes. GitHub's
/releases/latest is llama.cpp's v0.x line, which has no binaries; the
builds are the bNNNN releases. A Mac has to get the macOS tarball for its
own CPU from the newest release that has one, never an Ubuntu one, and
only Apple Silicon gets Metal. The macOS dylibs reach each other through
versioned symlinks, so those have to survive the copy into llama-bin/.

Hermetic: curl is a stub that serves fixture release JSON and tarballs
built here, and nvidia-smi is a stub that finds no card. The asset names
are the ones llama.cpp published for b11669.
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "unix", "lib_llama_bin.sh")
URL = "https://github.com/ggml-org/llama.cpp/releases/download/%s/%s"

# Stands in for llama-server: --help is all setup runs.
SERVER = "#!/bin/sh\necho '  --spec-type [none|ngram|draft-mtp]'\n"

# b11669 as published, minus the Windows, Android, CUDA and other builds
# no Unix setup picks.
B11669 = [
    "llama-b11669-bin-macos-arm64.tar.gz",
    "llama-b11669-bin-macos-x64.tar.gz",
    "llama-b11669-bin-ubuntu-arm64.tar.gz",
    "llama-b11669-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b11669-bin-ubuntu-x64.tar.gz",
    "llama-b11669-bin-win-cpu-x64.zip",
    "llama-b11669-xcframework.zip",
]
# A newer build whose macOS tarballs have not been uploaded yet.
B11670 = [
    "llama-b11670-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b11670-bin-ubuntu-x64.tar.gz",
]


def release(tag, names):
    return {"tag_name": tag,
            "assets": [{"name": n, "browser_download_url": URL % (tag, n)}
                       for n in names]}


def add(tar, name, data=b"", mode=0o644, link=None):
    info = tarfile.TarInfo(name)
    if link:
        info.type, info.linkname = tarfile.SYMTYPE, link
    else:
        info.size, info.mode = len(data), mode
    tar.addfile(info, None if link else io.BytesIO(data))


def build_tarball(path, name, server=SERVER):
    """The layout of llama.cpp's tarballs: one llama-bNNNN/ folder holding
    llama-server and its libraries. On macOS each dylib is a versioned
    file plus symlinks, and only the arm64 build has a Metal backend."""
    tag = name.split("-")[1]
    top = "llama-%s/" % tag
    libs = []
    if "-macos-" in name:
        libs.append("ggml-base")
    if name.endswith("-macos-arm64.tar.gz"):
        libs.append("ggml-metal")
    with tarfile.open(path, "w:gz") as tar:
        add(tar, top + "llama-server", server.encode(), 0o755)
        for lib in libs:
            add(tar, top + "lib%s.0.27.0.dylib" % lib, lib.encode())
            add(tar, top + "lib%s.0.dylib" % lib, link="lib%s.0.27.0.dylib" % lib)
            add(tar, top + "lib%s.dylib" % lib, link="lib%s.0.dylib" % lib)


CURL = r'''#!%s
import os, shutil, sys
fix = os.environ["FIXTURES"]
args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else None
url = [a for a in args if a.startswith("https://")][0]
with open(os.path.join(fix, "requests"), "a") as fh:
    fh.write(url + "\n")
if url.startswith("https://api.github.com/repos/ggml-org/llama.cpp/releases?"):
    src = os.path.join(fix, "releases.json")
else:
    src = os.path.join(fix, os.path.basename(url))
if not os.path.isfile(src):
    sys.stderr.write("curl: (22) 404 " + url + "\n")
    sys.exit(22)
if out:
    shutil.copyfile(src, out)
else:
    sys.stdout.write(open(src).read())
''' % sys.executable

# Real mv, except that moving the staged build into place fails when the
# fixtures folder holds a fail-mv marker.
MV = r'''#!/bin/sh
if [ -e "$FIXTURES/fail-mv" ] && [ "${1##*/}" = out ]; then
    exit 1
fi
exec %s "$@"
'''


class Sandbox(unittest.TestCase):
    """A repo folder, fixture releases and stubs for curl and nvidia-smi."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "repo")
        self.fix = os.path.join(tmp.name, "fixtures")
        self.stubs = os.path.join(tmp.name, "stubs")
        for d in (self.root, self.fix, self.stubs):
            os.makedirs(d)
        self.write_stub("curl", CURL)
        self.write_stub("nvidia-smi", "#!/bin/sh\nexit 0\n")
        self.releases([release("v0.7.0", ["nightly-tag.txt"]),
                       release("b11670", B11670),
                       release("b11669", B11669)])
        for name in B11669 + B11670:
            if name.endswith(".tar.gz"):
                build_tarball(os.path.join(self.fix, name), name)

    def write_stub(self, name, body):
        path = os.path.join(self.stubs, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def releases(self, rels):
        with open(os.path.join(self.fix, "releases.json"), "w") as fh:
            json.dump(rels, fh)

    def install(self, *args):
        env = dict(os.environ, FIXTURES=self.fix,
                   PATH=self.stubs + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run(
            ["bash", "-c", 'set -euo pipefail; . "$1"; shift; kiln_install_llama_bin "$@"',
             "_", LIB, *args],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=120)

    def requests(self):
        try:
            with open(os.path.join(self.fix, "requests")) as fh:
                return fh.read().split()
        except FileNotFoundError:
            return []

    def bin(self, *parts):
        return os.path.join(self.root, "llama-bin", *parts)


@unittest.skipIf(sys.platform == "win32", "lib_llama_bin.sh is the macOS/Linux half")
class Install(Sandbox):
    def test_apple_silicon_gets_the_metal_build(self):
        r = self.install("macos", "arm64")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("b11669 (metal)", r.stdout)
        # b11670 is newer but has no macOS tarball yet.
        self.assertEqual(self.requests()[-1],
                         URL % ("b11669", "llama-b11669-bin-macos-arm64.tar.gz"))
        self.assertTrue(os.stat(self.bin("llama-server")).st_mode & stat.S_IXUSR)
        self.assertNotIn("does not advertise draft-mtp", r.stdout)

    def test_the_dylib_symlinks_survive(self):
        self.assertEqual(self.install("macos", "arm64").returncode, 0)
        self.assertEqual(os.readlink(self.bin("libggml-metal.0.dylib")),
                         "libggml-metal.0.27.0.dylib")
        self.assertEqual(os.readlink(self.bin("libggml-metal.dylib")),
                         "libggml-metal.0.dylib")
        with open(self.bin("libggml-metal.dylib"), "rb") as fh:
            self.assertEqual(fh.read(), b"ggml-metal")

    def test_intel_gets_the_x64_build_on_the_cpu(self):
        r = self.install("macos", "x86_64")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("b11669 (cpu)", r.stdout)
        self.assertEqual(self.requests()[-1],
                         URL % ("b11669", "llama-b11669-bin-macos-x64.tar.gz"))
        self.assertTrue(os.path.isfile(self.bin("llama-server")))
        self.assertFalse(os.path.lexists(self.bin("libggml-metal.dylib")))

    def test_asks_for_the_release_list_not_releases_latest(self):
        self.install("macos", "arm64")
        reqs = self.requests()
        self.assertTrue(reqs[0].startswith(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases?"), reqs)
        self.assertFalse([u for u in reqs if "/releases/latest" in u])

    def test_says_so_when_no_release_has_a_mac_build(self):
        self.releases([release("v0.7.0", ["nightly-tag.txt"]),
                       release("b11670", B11670)])
        r = self.install("macos", "arm64")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no macOS arm64 llama.cpp build", r.stdout)
        self.assertFalse(os.path.exists(self.bin()))

    def test_a_populated_llama_bin_is_left_alone(self):
        os.makedirs(self.bin())
        with open(self.bin("llama-server"), "w") as fh:
            fh.write("old")
        r = self.install("macos", "arm64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already populated", r.stdout)
        self.assertEqual(self.requests(), [])

    def test_linux_still_gets_ubuntu_builds(self):
        r = self.install("vulkan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("b11670 (vulkan)", r.stdout)
        self.assertEqual(self.requests()[-1],
                         URL % ("b11670", "llama-b11670-bin-ubuntu-vulkan-x64.tar.gz"))

    def test_a_windows_build_is_refused_before_any_download(self):
        os.makedirs(self.bin())
        with open(self.bin("llama-server.exe"), "w") as fh:
            fh.write("windows")
        r = self.install("auto")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("holds the Windows build", r.stdout)
        self.assertIn("git clone https://github.com/Lampsdad/kiln-local-llm ~/kiln-local-llm",
                      r.stdout)
        self.assertIn("mv llama-bin llama-bin-windows", r.stdout)
        self.assertEqual(self.requests(), [])
        with open(self.bin("llama-server.exe")) as fh:
            self.assertEqual(fh.read(), "windows")

    def test_an_installed_build_replaces_the_old_one_cleanly(self):
        os.makedirs(self.bin())
        with open(self.bin("marker.txt"), "w") as fh:
            fh.write("old")
        r = self.install("vulkan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.bin("marker.txt")))
        self.assertTrue(os.path.isfile(self.bin("llama-server")))
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith("llama-bin")],
                         ["llama-bin"])

    def test_a_failed_move_puts_the_old_build_back(self):
        self.write_stub("mv", MV % shutil.which("mv"))
        os.makedirs(self.bin())
        with open(self.bin("marker.txt"), "w") as fh:
            fh.write("old")
        with open(os.path.join(self.fix, "fail-mv"), "w"):
            pass
        r = self.install("vulkan")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("The previous llama-bin/ is back in place", r.stdout)
        with open(self.bin("marker.txt")) as fh:
            self.assertEqual(fh.read(), "old")
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith("llama-bin")],
                         ["llama-bin"])

    def test_a_failing_nvidia_smi_falls_back_with_a_warning(self):
        self.write_stub("nvidia-smi", "#!/bin/sh\n"
                        "echo 'Failed to initialize NVML: driver/library version mismatch' >&2\n"
                        "exit 9\n")
        r = self.install("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WARNING: could not read the driver's CUDA version", r.stdout)
        self.assertIn("b11670 (vulkan)", r.stdout)

    def test_a_long_help_text_still_advertises_mtp(self):
        # About 200 KB of help, with draft-mtp in the first line. A pipe to
        # grep -q would cut the server off partway and report no MTP.
        big = r"""#!/bin/sh
echo '  --spec-type [none|ngram|draft-mtp]'
head -c 200000 /dev/zero | tr '\0' 'x'
echo
"""
        build_tarball(os.path.join(self.fix, "llama-b11670-bin-ubuntu-vulkan-x64.tar.gz"),
                      "llama-b11670-bin-ubuntu-vulkan-x64.tar.gz", server=big)
        r = self.install("vulkan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("does not advertise draft-mtp", r.stdout)


# b12600's Ubuntu CUDA builds: 12.8 and 13.5, each with its cudart.
B12600_CUDA = [
    "llama-b12600-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "cudart-llama-b12600-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b12600-bin-ubuntu-cuda-13.5-x64.tar.gz",
    "cudart-llama-b12600-bin-ubuntu-cuda-13.5-x64.tar.gz",
    "llama-b12600-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b12600-bin-ubuntu-x64.tar.gz",
]


@unittest.skipIf(sys.platform == "win32", "lib_llama_bin.sh is the macOS/Linux half")
class CudaChoice(Sandbox):
    """Which CUDA build a driver gets. A build runs on any driver of its
    own major version, so a 13.4 driver takes the 13.5 build. A 12.x
    driver cannot start a 13.x build at all."""

    def setUp(self):
        super().setUp()
        self.use(B12600_CUDA)

    def use(self, names):
        self.releases([release("b12600", names)])
        for name in names:
            build_tarball(os.path.join(self.fix, name), name)

    def driver(self, version):
        self.write_stub("nvidia-smi", "#!/bin/sh\n"
                        "echo '| NVIDIA-SMI 616.64   KMD Version: 616.64   "
                        "CUDA UMD Version: %s |'\n" % version)

    def fetched(self):
        return [u.rsplit("/", 1)[1] for u in self.requests()
                if "/releases/download/" in u]

    def test_a_13_driver_takes_the_newest_13_build(self):
        self.driver("13.4")
        r = self.install("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("b12600 (cuda)", r.stdout)
        self.assertEqual(self.fetched(),
                         ["llama-b12600-bin-ubuntu-cuda-13.5-x64.tar.gz",
                          "cudart-llama-b12600-bin-ubuntu-cuda-13.5-x64.tar.gz"])

    def test_a_12_driver_never_gets_a_13_build(self):
        self.driver("12.9")
        r = self.install("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fetched()[0], "llama-b12600-bin-ubuntu-cuda-12.8-x64.tar.gz")

    def test_a_12_driver_with_only_13_builds_gets_vulkan(self):
        self.use([n for n in B12600_CUDA if "cuda-12.8" not in n])
        self.driver("12.9")
        r = self.install("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("b12600 (vulkan)", r.stdout)

    def test_a_build_without_its_cudart_is_skipped(self):
        self.use([n for n in B12600_CUDA if n != "cudart-llama-b12600-bin-ubuntu-cuda-13.5-x64.tar.gz"])
        self.driver("13.4")
        r = self.install("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fetched()[0], "llama-b12600-bin-ubuntu-cuda-12.8-x64.tar.gz")


class SetupUsesIt(unittest.TestCase):
    """Every Unix installer goes through the one bNNNN lookup."""

    def read(self, name):
        with open(os.path.join(ROOT, "scripts", "unix", name), encoding="utf-8") as fh:
            return fh.read()

    def test_every_unix_installer(self):
        for name, call in (("install_linux.sh", "kiln_install_llama_bin auto"),
                           ("install_fedora.sh", "kiln_install_llama_bin auto"),
                           ("install_mac.sh", 'kiln_install_llama_bin macos "$ARCH"')):
            with self.subTest(name):
                text = self.read(name)
                self.assertIn(call, text)
                self.assertNotIn("llama.cpp/releases/latest", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
