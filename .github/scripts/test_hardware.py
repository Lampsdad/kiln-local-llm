#!/usr/bin/env python3
"""Tests for scripts/hardware.py, the model registry and the sizing
arithmetic every launch goes through.

What is worth pinning down is what the scripts trust it for: that two
families never fight over one file in models/, that a weights file is
recognised as the family it belongs to, that the opt-in families stay
opt-in, and that the flags it hands to the start scripts are safe to
splice into a command line unquoted. The Qwen numbers the README and
NOTES.md quote are pinned too, so a registry change that moves them
has to say so.

Hermetic: no GPU, no network; VRAM is passed in.
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import hardware  # noqa: E402

M = hardware.MODELS
CARD_32 = 32607
CARD_24 = 24564
CARD_16 = 16384
CARD_12 = 12288
GEMMA = [f for f in M if M[f]["group"] == "gemma"]


def keys(argv):
    """Run hardware.py's main with argv; KEY=VALUE output as a dict."""
    out = io.StringIO()
    saved = sys.argv
    sys.argv = ["hardware.py"] + argv
    try:
        with contextlib.redirect_stdout(out):
            hardware.main()
    finally:
        sys.argv = saved
    return dict(line.split("=", 1) for line in out.getvalue().splitlines()
                if "=" in line)


class Files(unittest.TestCase):
    def test_one_local_name_is_one_file(self):
        # mmproj-F16.gguf is the name unsloth gives both the Qwen and the
        # Gemma projector. Two different files under one local name and
        # the download skips the second as "already present".
        seen = {}
        for fid, fam in M.items():
            for extra in list(fam["mtp_heads"].values()) + list(fam["mmproj"].values()):
                src = (extra.repo, extra.remote)
                self.assertEqual(seen.setdefault(extra.local, src), src,
                                 "%s: %s" % (fid, extra.local))

    def test_weights_names_do_not_collide(self):
        names = {}
        for fid, fam in M.items():
            for q, _ in fam["quants"]:
                name = "%s-%s.gguf" % (fam["prefix"], q)
                self.assertNotIn(name, names)
                names[name] = fid

    def test_weights_are_recognised_as_their_family(self):
        for fid, fam in M.items():
            for q, _ in fam["quants"]:
                self.assertEqual(
                    hardware.family_of("%s-%s.gguf" % (fam["prefix"], q)), fid)

    def test_extras_are_not_weights(self):
        # The status boards find Gemma weights with gemma-4-*.gguf; a head
        # or projector matching that would be listed as a model.
        for fam in M.values():
            for extra in list(fam["mtp_heads"].values()) + list(fam["mmproj"].values()):
                self.assertIsNone(hardware.family_of(extra.local), extra.local)


class Registry(unittest.TestCase):
    SAFE = re.compile(r"^[A-Za-z0-9 ._=-]*$")

    def test_every_family_has_its_fields(self):
        for fid in M:
            v = keys(["--variant", fid])
            self.assertEqual(v["V_ID"], fid)
            for k in ("V_LABEL", "V_REPO", "V_PREFIX", "V_ALIAS", "V_QUANTS",
                      "V_SAMPLING", "V_SPEC_N", "V_MIN_BUILD"):
                self.assertTrue(v.get(k), "%s %s" % (fid, k))

    def test_flags_splice_unquoted(self):
        # start.bat and lib_select.sh paste these into the command line
        # as they are. A quote, % or & would be parsed by cmd.exe.
        for fid in M:
            v = keys(["--variant", fid])
            for k in ("V_SAMPLING", "V_REASONING", "V_MTP_FILES",
                      "V_MMPROJ_FILES", "V_SPEC_N", "V_MIN_BUILD"):
                self.assertRegex(v[k], self.SAFE, "%s %s" % (fid, k))

    def test_aliases_are_distinct(self):
        aliases = [fam["alias"] for fam in M.values()]
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_orders_cover_the_registry(self):
        self.assertEqual(sorted(hardware.DISK_ORDER), sorted(M))
        for order in hardware.SIBLINGS.values():
            self.assertTrue(set(order) <= set(M))

    def test_qwen_launches_as_before(self):
        for fid in ("base", "ablit", "9b", "4b"):
            v = keys(["--variant", fid])
            self.assertEqual(v["V_SAMPLING"],
                             "--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0")
            self.assertEqual(v["V_SPEC_N"], "3")
            self.assertEqual(v["V_REASONING"], "")
            self.assertEqual(v["V_MIN_BUILD"], "0")


class OptIn(unittest.TestCase):
    def test_gemma_is_never_picked_for_you(self):
        for vram in range(6000, 98000, 1024):
            plan = hardware.recommend(vram)
            if plan:
                self.assertNotIn(plan["family"], GEMMA, vram)

    def test_e4b_is_never_suggested(self):
        quiet = [f for f in M if not M[f].get("suggest", True)]
        self.assertEqual(quiet, ["gemma-e4b"])
        for order in [hardware.FAMILY_ORDER] + list(hardware.SIBLINGS.values()):
            self.assertFalse(set(quiet) & set(order))
        with tempfile.TemporaryDirectory() as tmp:
            # A 31B on cards too small for it points at a sibling.
            path = os.path.join(tmp, "gemma-4-31B-it-UD-Q5_K_XL.gguf")
            with open(path, "wb") as fh:
                fh.truncate(21890429888)
            for vram in (CARD_24, CARD_16, CARD_12, 8192, 6144):
                rec = keys(["--size-model", path, "--vram", str(vram)])
                self.assertNotEqual(rec.get("REC_FAMILY"), "gemma-e4b", vram)
            self.assertEqual(keys(["--size-model", path, "--vram",
                                   str(CARD_24)])["REC_FAMILY"], "gemma-26b")

    def test_disk_pick_prefers_qwen(self):
        with tempfile.TemporaryDirectory() as tmp:
            def pick():
                return keys(["--pick-on-disk", "--models-dir", tmp])["VARIANT"]
            self.assertEqual(pick(), "")
            open(os.path.join(tmp, "gemma-4-E4B-it-Q8_0.gguf"), "w").close()
            self.assertEqual(pick(), "gemma-e4b")
            open(os.path.join(tmp, "gemma-4-12b-it-IQ4_XS.gguf"), "w").close()
            self.assertEqual(pick(), "gemma-12b")
            open(os.path.join(tmp, "Qwen3.8-9B-Q8_0.gguf"), "w").close()
            self.assertEqual(pick(), "9b")


class Sizing(unittest.TestCase):
    def plan(self, vram, fam=None):
        return hardware.recommend(vram, family=fam)

    def test_qwen_numbers_the_docs_quote(self):
        # README "What each card is recommended", NOTES.md "114,688".
        p = self.plan(CARD_32)
        self.assertEqual((p["family"], p["quant"], p["mtp"], p["mmproj"],
                          p["ctx"]),
                         ("base", "UD-Q5_K_XL", "q8_0", "f16", 114688))
        p = self.plan(CARD_24)
        self.assertEqual((p["family"], p["quant"], p["mtp"]),
                         ("base", "UD-IQ4_XS", "q4_0"))

    def test_gemma_per_card(self):
        # The README's "if you ask for Gemma 4" table.
        # 32 GB: UD-Q5_K_XL with MTP and vision needs ~33 GB at 96K on
        # the 5090 (measured), so the smaller quant is the right answer.
        want = {CARD_32: ("gemma-31b", "UD-Q4_K_XL", "q8_0"),
                CARD_24: ("gemma-26b", "UD-Q4_K_XL", "q8_0"),
                CARD_16: ("gemma-12b", "UD-Q5_K_XL", "q8_0"),
                CARD_12: ("gemma-12b", "IQ4_XS", None)}
        for vram, expect in want.items():
            plan = next(p for p in (hardware.plan_for(vram, f)
                                    for f in hardware.SIBLINGS["gemma"]) if p)
            self.assertEqual((plan["family"], plan["quant"], plan["mtp"]),
                             expect)
            self.assertGreaterEqual(plan["ctx"], hardware.MIN_CTX)

    def test_e4b_weights_leave_the_embeddings_in_ram(self):
        fam = M["gemma-e4b"]
        q8 = dict(fam["quants"])["UD-Q8_K_XL"]
        self.assertEqual(hardware.gpu_mib(fam, "UD-Q8_K_XL", q8),
                         hardware.mib(q8) - 2856)
        self.assertEqual(hardware.gpu_mib(M["gemma-12b"], "Q8_0", 2 ** 30), 1024)

    def test_e4b_stops_at_its_window(self):
        self.assertEqual(self.plan(CARD_32, "gemma-e4b")["ctx"], 131072)


class Build(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "a shell script stands in for the binary")
    def test_both_version_formats(self):
        for text, want in (
                ("version: 0.6.0-dev (build 11624, commit af0579c78)", 11624),
                ("version: 9431 (94ca829b6)\nbuilt with Clang 20", 9431),
                ("built with Clang 20", 0)):
            with tempfile.TemporaryDirectory() as tmp:
                exe = os.path.join(tmp, "llama-server")
                with open(exe, "w") as fh:
                    fh.write("#!/bin/sh\necho '%s' >&2\n" % text)
                os.chmod(exe, 0o755)
                self.assertEqual(hardware.llama_build(exe), want, text)

    def test_missing_binary(self):
        self.assertEqual(hardware.llama_build("/no/such/llama-server"), 0)



class Mac(unittest.TestCase):
    RAM_16 = str(16 * 2 ** 30)

    def gpu(self, sysctl):
        """detect_gpu() on a Mac whose sysctl answers are `sysctl`, a dict of
        key to output. A key not in it makes its command fail, as it does on
        an Intel Mac that lacks it."""
        def run(cmd):
            if cmd[0] == "sysctl":
                return sysctl.get(cmd[-1])
            return None   # no NVIDIA or ROCm tools on a Mac
        with mock.patch.object(hardware.sys, "platform", "darwin"), \
                mock.patch.object(hardware, "_run", run):
            return hardware.detect_gpu()

    def test_apple_silicon_keeps_the_unified_budget(self):
        self.assertEqual(self.gpu({"hw.memsize": self.RAM_16,
                                   "hw.optional.arm64": "1\n",
                                   "machdep.cpu.brand_string": "Apple M2\n"}),
                         (12288, "Apple M2 (unified memory)", "apple"))

    def test_intel_mac_is_budgeted_as_cpu(self):
        brand = "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz\n"
        for arm in (None, "", "0\n"):
            answers = {"hw.memsize": self.RAM_16, "machdep.cpu.brand_string": brand}
            if arm is not None:
                answers["hw.optional.arm64"] = arm
            self.assertEqual(
                self.gpu(answers),
                (8192, "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz (CPU only, no Metal)",
                 "cpu"), repr(arm))
        self.assertEqual(self.gpu({"hw.memsize": self.RAM_16}),
                         (8192, "Intel Mac (CPU only, no Metal)", "cpu"))


class Build(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "a shell script stands in for the binary")
    def test_both_version_formats(self):
        for text, want in (
                ("version: 0.6.0-dev (build 11624, commit af0579c78)", 11624),
                ("version: 9431 (94ca829b6)\nbuilt with Clang 20", 9431),
                ("built with Clang 20", 0)):
            with tempfile.TemporaryDirectory() as tmp:
                exe = os.path.join(tmp, "llama-server")
                with open(exe, "w") as fh:
                    fh.write("#!/bin/sh\necho '%s' >&2\n" % text)
                os.chmod(exe, 0o755)
                self.assertEqual(hardware.llama_build(exe), want, text)

    def test_missing_binary(self):
        self.assertEqual(hardware.llama_build("/no/such/llama-server"), 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
