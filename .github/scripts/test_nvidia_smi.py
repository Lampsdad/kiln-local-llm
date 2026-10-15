#!/usr/bin/env python3
"""Tests for the build choice in scripts/unix/lib_llama_bin.sh: which
llama.cpp build an NVIDIA card gets on Linux and WSL.

The CUDA version comes from nvidia-smi's header, whose wording changed:
"CUDA Version: 12.8" on older drivers, "CUDA UMD Version: 13.4" on
newer ones. Missing the new wording sent every NVIDIA card to the
Vulkan build, which under WSL sees no GPU and runs on the CPU.

Hermetic: nvidia-smi and curl are stubs on PATH. curl answers the
release list from a fixture and fails every download, so the test
reads the choice from what the installer prints before downloading.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "unix", "lib_llama_bin.sh")

TAG = "b11669"
ASSETS = [
    "llama-%s-bin-ubuntu-cuda-12.8-x64.tar.gz" % TAG,
    "llama-%s-bin-ubuntu-cuda-13.4-x64.tar.gz" % TAG,
    "cudart-llama-%s-bin-ubuntu-cuda-12.8-x64.tar.gz" % TAG,
    "cudart-llama-%s-bin-ubuntu-cuda-13.4-x64.tar.gz" % TAG,
    "llama-%s-bin-ubuntu-vulkan-x64.tar.gz" % TAG,
    "llama-%s-bin-ubuntu-x64.tar.gz" % TAG,
]
RELEASES = [
    {"tag_name": "v0.7.0", "assets": [{"name": "nightly-tag.txt",
                                       "browser_download_url": "https://x/n"}]},
    {"tag_name": TAG, "assets": [{"name": n, "browser_download_url": "https://x/" + n}
                                 for n in ASSETS]},
]

OLD_HEADER = ("| NVIDIA-SMI 570.86.15    Driver Version: 570.86.15    "
              "CUDA Version: 12.8     |")
NEW_HEADER = ("| NVIDIA-SMI 615.65.07    KMD Version: 616.64    "
              "CUDA UMD Version: 13.4     |")


@unittest.skipIf(sys.platform == "win32", "lib_llama_bin.sh is the Linux half")
class BuildChoice(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.dir, "releases.json"), "w") as fh:
            json.dump(RELEASES, fh)
        self.stub("curl", '''
case "$*" in
    *api.github.com*) cat "%s" ;;
    *) exit 22 ;;
esac
''' % os.path.join(self.dir, "releases.json"))
        # python3 for the plan, found by lib_python.sh.
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body.lstrip("\n"))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def choose(self):
        work = os.path.join(self.dir, "repo")
        os.makedirs(work, exist_ok=True)
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.dir}
        r = subprocess.run(
            ["bash", "-c", '. "$1"; kiln_install_llama_bin auto', "-", LIB],
            cwd=work, env=env, capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr

    def test_new_nvidia_smi_header_gets_cuda_13_4(self):
        self.stub("nvidia-smi", "echo '%s'\n" % NEW_HEADER)
        out = self.choose()
        self.assertIn("Build %s (cuda)" % TAG, out)
        self.assertIn("ubuntu-cuda-13.4-x64.tar.gz", out)

    def test_old_nvidia_smi_header_gets_cuda_12_8(self):
        self.stub("nvidia-smi", "echo '%s'\n" % OLD_HEADER)
        out = self.choose()
        self.assertIn("Build %s (cuda)" % TAG, out)
        self.assertIn("ubuntu-cuda-12.8-x64.tar.gz", out)

    def test_no_nvidia_smi_gets_vulkan(self):
        out = self.choose()
        self.assertIn("Build %s (vulkan)" % TAG, out)

    def test_linux_setup_asks_for_the_cuda_build(self):
        # WSL's Vulkan sees no GPU, so install_linux.sh must not pin it.
        with open(os.path.join(ROOT, "scripts", "unix", "install_linux.sh")) as fh:
            self.assertIn("kiln_install_llama_bin auto", fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
