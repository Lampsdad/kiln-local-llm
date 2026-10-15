#!/usr/bin/env python3
"""Tests for kiln tui: scripts/tui.py (the bootstrap) and
scripts/tui_app.py (the Textual app).

The bootstrap tests run anywhere. The app tests need Textual and are
skipped without it; CI installs it.

What is pinned: keys reach the right kiln verb with the right options,
keys meant for the main screen do nothing under another one, output
from a script is shown as text and never as markup, and the benchmark
screen turns a run into a pick the user can start.

Hermetic: the controller is a fake with a canned status, jobs run a
Python one-liner, and the benchmark runner is replaced by one that
reports made-up results straight away.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import control  # noqa: E402
import tui  # noqa: E402

try:
    import textual  # noqa: F401
    HAVE_TEXTUAL = True
except ImportError:
    HAVE_TEXTUAL = False

# CI sets this wherever it installs Textual. Then a Textual that will not
# import fails the run, rather than quietly skipping the app tests.
REQUIRE_TEXTUAL = os.environ.get("KILN_REQUIRE_TEXTUAL") == "1"


class Textual(unittest.TestCase):
    @unittest.skipUnless(REQUIRE_TEXTUAL, "KILN_REQUIRE_TEXTUAL is not 1")
    def test_textual_imports_when_it_is_required(self):
        try:
            import textual  # noqa: F401,F811
        except ImportError as err:
            self.fail("KILN_REQUIRE_TEXTUAL=1, but Textual will not import: %s" % err)


class Bootstrap(unittest.TestCase):
    def test_one_venv_per_platform_inside_the_checkout(self):
        self.assertTrue(tui.VENV.startswith(os.path.join(tui.ROOT, ".kiln")))
        self.assertTrue(tui.VENV.endswith("venv-" + sys.platform))
        self.assertTrue(tui.venv_python().startswith(tui.VENV))

    def test_old_python_is_told_rather_than_crashing(self):
        saved = sys.version_info
        out = io.StringIO()
        try:
            sys.version_info = (3, 8, 0, "final", 0)
            with contextlib.redirect_stdout(out):
                self.assertEqual(tui.main([]), 1)
        finally:
            sys.version_info = saved
        self.assertIn("needs Python 3.9", out.getvalue())

    def test_a_venv_that_failed_halfway_is_made_again_next_time(self):
        # Debian without python3-venv: venv makes bin/python, then
        # fails at ensurepip. Left behind, the next run, after the
        # apt install it advises, would go straight to a missing pip.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def call(argv, **kw):
            os.makedirs(os.path.dirname(tui.venv_python()))
            open(tui.venv_python(), "w").close()
            return 1

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(tui, "VENV", os.path.join(tmp.name, "v")), \
                mock.patch.object(tui.subprocess, "call", call), \
                mock.patch.object(sys, "stdin", Terminal()), \
                mock.patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(tui.install())
            self.assertFalse(os.path.exists(tui.VENV))

    def test_requirement_matches_the_major_it_checks(self):
        self.assertIn(">=%d." % tui.MAJOR, tui.REQUIREMENT)
        self.assertIn("<%d" % (tui.MAJOR + 1), tui.REQUIREMENT)

    def test_kiln_venv_is_ignored_by_git(self):
        with open(os.path.join(ROOT, ".gitignore")) as fh:
            ignored = fh.read().split()
        self.assertIn(".kiln/", ignored)
        self.assertIn("kiln-benchmark.json", ignored)


def canned_status(state="stopped", bench_doc=None):
    def model(fam, label, quant, head, ctx, no_mtp_ctx, **kw):
        m = {"id": fam, "label": label, "alias": "alias-" + fam,
             "params": "27B", "uncensored": fam == "ablit",
             "third_party": False, "mtp": head, "vision": head,
             "quant": quant, "ctx": ctx, "ctx_no_mtp": no_mtp_ctx,
             "has_mtp_head": head, "get": None, "recommended": fam == "base"}
        m.update(kw)
        return m
    return {
        "kiln": {"version": "v9.9.9", "update": ""},
        "llama": {"installed": True, "build": "10901", "mtp": True},
        "cloudflared": True,
        "machine": {"gpu": "Test GPU", "vendor": "nvidia", "vram_mib": 32607,
                    "tier": "32 GB", "cores": 12, "ram_mib": 65536},
        "recommend": {"family": "base", "label": "Qwen3.8-27B",
                      "quant": "UD-Q5_K_XL", "ctx": 114688},
        "platform": "linux", "disk_free_gb": 300,
        "models": [
            model("base", "Qwen3.8-27B", "UD-Q5_K_XL", True, 114688, 204800),
            model("ablit", "Qwen3.8-27B abliterated", "UD-Q5_K_XL", True,
                  118784, 208896),
            model("9b", "Qwen3.8-9B distill [b]x[/b]", "", False, 0, 0,
                  get={"quant": "Q8_0", "ctx": 262144, "gb": 9.8}),
        ],
        "shared": {"mtp": "mtp.gguf", "vision": "mmproj.gguf"},
        "server": {"state": state, "port": 8081,
                   "local_url": "http://127.0.0.1:8081/v1",
                   "alias": "alias-base" if state == "ready" else "",
                   "ctx": 114688, "vision": True, "auth": True, "tunnel": ""},
        "key": {"present": True, "masked": "abcdef…wxyz"},
        "next": {"action": "start", "variant": "base", "label": "Start",
                 "text": "Serve a model to Cursor"},
        "start_default": "base", "busy": None,
        "features": {"update": False},
    }


class FakeControl:
    plat = "linux"
    port = 8081

    def __init__(self, status=None, output="[red]boom[/red] done"):
        self._status = status or canned_status()
        self.jobs = {}
        self.requests = []
        self.output = output

    def status(self):
        return self._status

    def blocking(self, state=None):
        return None

    def invalidate(self):
        pass

    def shutdown(self):
        return []

    def run(self, req):
        control.build_args(req)        # the real table, so typos fail
        self.requests.append(req)
        job = control.Job(str(len(self.jobs) + 1), req["action"],
                          control.title_for(req), ["x"])
        self.jobs[job.id] = job
        job.start([sys.executable, "-c", "print(%r)" % self.output],
                  lambda: None)
        return job


@unittest.skipUnless(HAVE_TEXTUAL, "Textual is not installed")
class App(unittest.TestCase):
    def setUp(self):
        import bench
        import tui_app
        self.bench = bench
        self.tui_app = tui_app
        self.saved = (bench.load, bench.choose, bench.Runner)
        bench.load = lambda: None
        self.chosen = []
        bench.choose = lambda sid: self.chosen.append(sid)

    def tearDown(self):
        self.bench.load, self.bench.choose, self.bench.Runner = self.saved

    def drive(self, coro_fn, ctl=None):
        ctl = ctl or FakeControl()
        app = self.tui_app.KilnApp(ctl)

        async def go():
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(50):
                    await pilot.pause(0.05)
                    if app.status:
                        break
                await coro_fn(app, pilot)
        asyncio.run(go())
        return app, ctl

    def test_status_is_shown(self):
        async def check(app, pilot):
            self.assertEqual(app.row_ids, ["base", "ablit", "9b"])
            self.assertIn("v9.9.9", app.sub_title)
        self.drive(check)

    def test_start_carries_the_chosen_options(self):
        async def go(app, pilot):
            await pilot.press("down")          # the abliterated build
            await pilot.press("m", "l", "s")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, self.tui_app.Confirm)
            await pilot.press("y")
            await pilot.pause(0.2)
        _, ctl = self.drive(go)
        self.assertEqual(ctl.requests, [{"action": "start", "variant": "ablit",
                                         "no_mtp": True, "local": True}])

    def test_a_shadowed_port_is_called_out_before_a_tunnel_opens(self):
        st = canned_status()
        st["server"]["shadowed"] = True

        async def go(app, pilot):
            await pilot.press("s")
            await pilot.pause(0.1)
            self.assertIn("would publish that program", app.screen.body)
            await pilot.press("n", "l", "s")
            await pilot.pause(0.1)
            # Without the tunnel there is nothing to publish.
            self.assertNotIn("would publish", app.screen.body)
        self.drive(go, FakeControl(st))

    def test_saying_no_runs_nothing(self):
        async def go(app, pilot):
            await pilot.press("x")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, self.tui_app.Confirm)
            await pilot.press("n")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, self.tui_app.Confirm)
        _, ctl = self.drive(go, FakeControl(canned_status("ready")))
        self.assertEqual(ctl.requests, [])

    def test_stop_is_disabled_when_nothing_runs(self):
        async def go(app, pilot):
            await pilot.press("x")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, self.tui_app.Confirm)
        _, ctl = self.drive(go)
        self.assertEqual(ctl.requests, [])

    def test_download_goes_to_the_selected_model(self):
        async def go(app, pilot):
            await pilot.press("down", "down", "d")
            await pilot.pause(0.1)
            await pilot.press("y")
            await pilot.pause(0.2)
        _, ctl = self.drive(go)
        self.assertEqual(ctl.requests, [{"action": "get", "variant": "9b"}])

    def test_main_screen_keys_do_nothing_under_another_screen(self):
        async def go(app, pilot):
            await pilot.press("b")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, self.tui_app.BenchScreen)
            await pilot.press("s", "x", "d", "n")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, self.tui_app.BenchScreen)
        _, ctl = self.drive(go)
        self.assertEqual(ctl.requests, [])

    def test_pi_runs_without_asking(self):
        async def go(app, pilot):
            await pilot.press("p")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, self.tui_app.Confirm)
        _, ctl = self.drive(go)
        self.assertEqual(ctl.requests, [{"action": "pi"}])

    def test_script_output_is_text_not_markup(self):
        async def go(app, pilot):
            await pilot.press("o")             # OpenCode: no confirmation
            for _ in range(40):
                await pilot.pause(0.1)
                job = app.ctl.jobs.get(app.job_id)
                if job and not job.running:
                    break
            await pilot.pause(0.6)
            log = app.query_one("#log")
            text = "\n".join(line.text for line in log.lines)
            self.assertIn("[red]boom[/red] done", text)
        self.drive(go)

    def test_benchmark_run_ends_in_a_pick_that_starts(self):
        bench = self.bench

        class FakeRunner:
            def __init__(self, ctl, chosen, depth, emit):
                self.chosen, self.depth, self.emit = chosen, depth, emit

            def cancel(self):
                pass

            def run(self):
                results = []
                for i, s in enumerate(self.chosen):
                    self.emit("setup", i, len(self.chosen), s)
                    self.emit("step", "Generating")
                    self.emit("log", "starting [b]%s[/b]\n" % s.id)
                    r = {"id": s.id, "variant": s.variant, "label": s.label,
                         "quant": s.quant, "mtp": s.mtp, "no_mtp": s.no_mtp,
                         "ctx_expected": s.ctx, "ctx": s.ctx, "load_s": 30.0,
                         "prefill_tps": 3000.0, "accept_pct": 70.0,
                         "gen_tps": 45.0 if s.no_mtp else 85.0}
                    results.append(r)
                    self.emit("result", r)
                doc = {"when": "now", "depth": self.depth, "machine": {},
                       "results": results}
                self.emit("done", doc, False)
                return doc

        bench.Runner = FakeRunner

        async def go(app, pilot):
            await pilot.press("b")
            await pilot.pause(0.1)
            screen = app.screen
            # The stock build is what this user runs, so only its two
            # setups start ticked; the abliterated pair is there to add.
            self.assertEqual(
                screen.query_one("#setups").selected, ["base", "base-nomtp"])
            await pilot.press("r")
            for _ in range(40):
                await pilot.pause(0.1)
                if screen.doc:
                    break
            self.assertEqual(screen.row_ids, ["base", "base-nomtp"])
            verdict = "\n".join(text for _, text in screen.verdict)
            self.assertIn("Pick: Qwen3.8-27B UD-Q5_K_XL, MTP", verdict)
            # Capitalised by hand: str.capitalize would print "cursor's".
            self.assertIn("which Cursor's agent needs.", verdict)
            # Pick the larger window instead, and start it.
            await pilot.press("tab", "down", "a")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, self.tui_app.Confirm)
            await pilot.press("y")
            await pilot.pause(0.3)
            self.assertNotIsInstance(app.screen, self.tui_app.BenchScreen)
        _, ctl = self.drive(go)
        self.assertEqual(self.chosen, ["base-nomtp"])
        self.assertEqual(ctl.requests, [{"action": "start", "variant": "base",
                                         "no_mtp": True, "local": False}])

    def test_no_quitting_from_a_dialog_over_a_running_benchmark(self):
        class SlowRunner:
            def __init__(self, ctl, chosen, depth, emit):
                self.stopped = threading.Event()

            def cancel(self):
                self.stopped.set()

            def run(self):
                self.stopped.wait(20)

        self.bench.Runner = SlowRunner

        async def go(app, pilot):
            await pilot.press("b")
            await pilot.pause(0.1)
            screen = app.screen
            await pilot.press("r")
            await pilot.pause(0.2)
            self.assertIsNotNone(screen.runner)
            # ctrl+q belongs to the app, so it reaches action_quit from
            # the help screen too. Quitting there would leave the run's
            # server behind.
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            await pilot.press("ctrl+q")
            await pilot.pause(0.2)
            self.assertTrue(app.is_running)
            self.assertFalse(screen.runner.stopped.is_set())
            screen.runner.cancel()
        self.drive(go)


if __name__ == "__main__":
    unittest.main(verbosity=2)
