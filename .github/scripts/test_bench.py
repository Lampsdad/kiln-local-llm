#!/usr/bin/env python3
"""Tests for scripts/bench.py, the benchmark behind kiln tui.

The run itself is what is worth pinning down: every setup is started
through `kiln start` with the tunnel switched off, the previous server
is gone before the next is timed, a setup that fails to load is
recorded and the run moves on, and the GPU is freed at the end however
the run ends. The recommendation is pinned too, since it is what the
user acts on.

Hermetic: kiln is replaced by a Python one-liner, and llama-server by a
fake that answers /health, /props, /tokenize and /completion the way
the real one does.
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import bench  # noqa: E402
import control  # noqa: E402
import opencode  # noqa: E402


def model(fam, quant="UD-Q5_K_XL", head=True, ctx=114688, ctx_no_mtp=204800):
    return {"id": fam, "label": "Label " + fam, "alias": "alias-" + fam,
            "quant": quant, "has_mtp_head": head, "ctx": ctx,
            "ctx_no_mtp": ctx_no_mtp if head else 0,
            "uncensored": fam == "ablit"}


def status(models, installed=True, mtp=True, plat="linux"):
    return {"llama": {"installed": installed, "mtp": mtp, "build": "1"},
            "models": models, "platform": plat}


class Setups(unittest.TestCase):
    def test_with_and_without_the_head(self):
        found = bench.setups(status([model("ablit"), model("base"),
                                     model("9b", quant="", head=False)]))
        # Stock before abliterated, MTP before not; nothing undownloaded.
        self.assertEqual([s.id for s in found],
                         ["base", "base-nomtp", "ablit", "ablit-nomtp"])
        self.assertEqual(found[0].args, ["start", "base"])
        self.assertEqual(found[1].args, ["start", "base", "--no-mtp"])
        self.assertEqual(found[1].request,
                         {"action": "start", "variant": "base",
                          "no_mtp": True})
        self.assertEqual((found[0].ctx, found[1].ctx), (114688, 204800))
        self.assertEqual([s.uncensored for s in found],
                         [False, False, True, True])

    def test_no_head_means_one_setup_and_no_flag(self):
        found = bench.setups(status([model("9b", quant="Q8_0", head=False,
                                           ctx=262144)]))
        self.assertEqual([(s.id, s.args) for s in found],
                         [("9b", ["start", "9b"])])

    def test_head_the_build_cannot_use(self):
        m = model("base")
        win = bench.setups(status([m], mtp=False, plat="windows"))
        nix = bench.setups(status([m], mtp=False, plat="linux"))
        self.assertEqual([s.id for s in win], ["base"])
        self.assertEqual(win[0].ctx, 204800)     # start.bat sizes without it
        self.assertEqual(nix[0].ctx, 114688)     # lib_select.sh with it

    def test_by_default_only_the_kind_of_build_you_run(self):
        st = dict(status([model("base"), model("ablit")]),
                  start_default="base")
        saved = bench.load
        self.addCleanup(setattr, bench, "load", saved)
        bench.load = lambda: None
        self.assertFalse(bench.runs_uncensored(st))
        self.assertTrue(bench.runs_uncensored(st, "ablit"))
        # A setup started from the benchmark counts as the one you run.
        bench.load = lambda: {"chosen": {"variant": "ablit"}}
        self.assertTrue(bench.runs_uncensored(st))

        found = bench.setups(st)
        self.assertEqual([s.id for s in bench.default_setups(found, False)],
                         ["base", "base-nomtp"])
        self.assertEqual([s.id for s in bench.default_setups(found, True)],
                         ["ablit", "ablit-nomtp"])
        # Nothing of that kind on disk: measure what there is.
        only = bench.setups(status([model("ablit")]))
        self.assertEqual(bench.default_setups(only, False), only)

    def test_nothing_without_llama_cpp(self):
        self.assertEqual(bench.setups(status([model("base")],
                                             installed=False)), [])

    def test_gemma_needs_its_build(self):
        models = [model("base"), dict(model("gemma-12b"), min_build=11059)]
        old = status(models)
        old["llama"]["build"] = "10901"
        self.assertEqual([s.variant for s in bench.setups(old)],
                         ["base", "base"])
        new = status(models)
        new["llama"]["build"] = "11624"
        self.assertIn("gemma-12b", [s.variant for s in bench.setups(new)])


class Lines(unittest.TestCase):
    """Qwen or Gemma is a choice about the model, like abliterated: the
    benchmark measures and picks within the line you run, and never
    picks Gemma 4 E4B while anything else measured."""

    def found(self, *fams):
        st = status([model(f) for f in fams])
        st["llama"]["build"] = "11624"
        return bench.setups(st)

    def test_by_default_only_your_model_line(self):
        found = self.found("base", "gemma-12b", "gemma-e4b")
        self.assertEqual(
            [s.id for s in bench.default_setups(found, False)],
            ["base", "base-nomtp"])
        self.assertEqual(
            [s.id for s in bench.default_setups(found, False, "gemma")],
            ["gemma-12b", "gemma-12b-nomtp"])
        only = self.found("base", "gemma-e4b")
        self.assertEqual(
            [s.id for s in bench.default_setups(only, False, "gemma")],
            ["gemma-e4b", "gemma-e4b-nomtp"])

    def test_runs_line(self):
        saved = bench.load
        self.addCleanup(setattr, bench, "load", saved)
        bench.load = lambda: None
        st = dict(status([]), start_default="base")
        self.assertEqual(bench.runs_line(st), "qwen")
        self.assertEqual(bench.runs_line(st, "gemma-26b"), "gemma")
        bench.load = lambda: {"chosen": {"variant": "gemma-31b"}}
        self.assertEqual(bench.runs_line(st), "gemma")

    def test_pick_stays_on_your_line(self):
        results = [result("base", 85.0, 114688),
                   result("gemma-12b", 200.0, 262144, variant="gemma-12b"),
                   result("gemma-e4b", 275.0, 131072, variant="gemma-e4b")]
        rec = bench.recommend(results, min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "base")
        self.assertIn("other model line", bench.set_aside_note(rec))
        rec = bench.recommend(results, min_ctx=98304, line="gemma")
        self.assertEqual(rec["pick"]["id"], "gemma-12b")

    def test_e4b_is_picked_only_when_alone(self):
        both = [result("gemma-12b", 200.0, 262144, variant="gemma-12b"),
                result("gemma-e4b", 275.0, 131072, variant="gemma-e4b")]
        rec = bench.recommend(both, min_ctx=98304, line="gemma")
        self.assertEqual(rec["pick"]["id"], "gemma-12b")
        self.assertIn("E4B", bench.set_aside_note(rec, line="gemma"))
        rec = bench.recommend(both[1:], min_ctx=98304, line="gemma")
        self.assertEqual(rec["pick"]["id"], "gemma-e4b")


def result(rid, gen, ctx, error=None, **kw):
    r = {"id": rid, "variant": rid.split("-")[0], "label": "L", "quant": "Q",
         "mtp": not rid.endswith("nomtp"), "no_mtp": rid.endswith("nomtp"),
         "ctx": ctx, "gen_tps": gen, "ctx_expected": ctx}
    if error:
        r = dict(r, error=error, gen_tps=None)
    r.update(kw)
    return r


class Recommend(unittest.TestCase):
    def test_fastest_that_clears_the_context_bar(self):
        rec = bench.recommend([result("base", 85.0, 114688),
                               result("base-nomtp", 45.0, 204800),
                               result("9b", 140.0, 65536)], min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "base")
        self.assertEqual(rec["fastest"]["id"], "9b")
        self.assertEqual(rec["roomiest"]["id"], "base-nomtp")
        self.assertIn("96K", rec["reason"])

    def test_a_near_tie_is_called_a_tie(self):
        rec = bench.recommend([result("base", 100.0, 114688),
                               result("9b", 95.0, 131072),
                               result("base-nomtp", 60.0, 204800)],
                              min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "base")
        self.assertEqual([r["id"] for r in rec["close"]], ["9b"])

    def test_overlapping_runs_are_a_tie_whatever_the_medians(self):
        # MTP's runs swing more than 10%; 80 is 14% below 92.8, but one
        # of the pick's own runs was slower than 80's fastest.
        rec = bench.recommend([
            result("base", 92.8, 114688, gen_runs=[123.7, 81.1, 92.8]),
            result("9b", 80.0, 131072, gen_runs=[78.0, 80.0, 85.0]),
            result("base-nomtp", 62.6, 204800, gen_runs=[62.6, 62.5, 62.6]),
        ], min_ctx=98304)
        self.assertEqual([r["id"] for r in rec["close"]], ["9b"])

    def test_real_5090_run(self):
        # kiln tui on an RTX 5090, llama.cpp build 10901, quick depth.
        results = [
            result("base", 92.8, 114688, gen_runs=[123.7, 81.1, 92.8]),
            result("base-nomtp", 62.6, 204800, gen_runs=[62.6, 62.5, 62.6]),
            result("ablit", 115.6, 118784, gen_runs=[115.6, 116.0, 107.2]),
            result("ablit-nomtp", 63.5, 208896, gen_runs=[63.4, 63.5, 63.9]),
        ]
        rec = bench.recommend(results, min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "base")
        self.assertEqual(rec["fastest"]["id"], "base")
        self.assertEqual(rec["roomiest"]["id"], "base-nomtp")
        self.assertEqual(rec["close"], [])
        self.assertEqual([r["id"] for r in rec["set_aside"]],
                         ["ablit", "ablit-nomtp"])

    def test_abliterated_is_compared_not_picked(self):
        both = [result("base", 90.0, 114688, uncensored=False),
                result("ablit", 120.0, 118784, uncensored=True)]
        rec = bench.recommend(both, min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "base")
        self.assertIn("not picked", bench.set_aside_note(rec))
        # Someone who runs the abliterated build gets the reverse.
        rec = bench.recommend(both, min_ctx=98304, uncensored=True)
        self.assertEqual(rec["pick"]["id"], "ablit")
        self.assertEqual([r["id"] for r in rec["set_aside"]], ["base"])
        # With nothing else measured it is picked, and nothing is set
        # aside to explain.
        rec = bench.recommend(both[1:], min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "ablit")
        self.assertEqual(bench.set_aside_note(rec), "")

    def test_files_saved_before_the_flag_use_the_registry(self):
        self.assertTrue(bench.is_uncensored({"variant": "ablit"}))
        self.assertFalse(bench.is_uncensored({"variant": "base"}))
        self.assertFalse(bench.is_uncensored({"variant": "ablit",
                                              "uncensored": False}))

    def test_nothing_clears_the_bar(self):
        rec = bench.recommend([result("a", 90.0, 32768),
                               result("b", 50.0, 65536)], min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "b")
        self.assertIn("most room", rec["reason"])

    def test_failures_are_never_picked(self):
        self.assertIsNone(bench.recommend([result("a", None, 0, "oom")]))
        rec = bench.recommend([result("a", None, 0, "oom"),
                               result("b", 10.0, 131072)], min_ctx=98304)
        self.assertEqual(rec["pick"]["id"], "b")


class Parsing(unittest.TestCase):
    def test_acceptance(self):
        self.assertEqual(bench.acceptance({"draft_n": 200,
                                           "draft_n_accepted": 150}), 0.75)
        self.assertIsNone(bench.acceptance({"predicted_n": 10}))

    def test_missing_timings_fail_the_setup(self):
        with self.assertRaises(bench.Failed):
            bench.timings({"content": "x"})

    def test_report_is_a_table_with_the_failures_in_it(self):
        doc = {"depth": "quick", "machine": {"gpu": "GPU", "vram_mib": 1,
                                             "llama_build": "9", "kiln": "v"},
               "results": [result("base", 85.25, 114688, prefill_tps=3100.4,
                                  accept_pct=72.0, load_s=40.0,
                                  vram_used_mib=30720),
                           result("ablit", None, 0, "kiln start failed")]}
        text = bench.report(doc)
        self.assertIn("| L Q, MTP | 112K | 40 s | 3100 | 85.2 | 72% | - | "
                      "30.0 GB |", text)
        self.assertIn("failed: kiln start failed", text)
        self.assertIn("Pick: L Q, MTP", text)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (bench.RESULTS_FILE, bench.machine)
        bench.RESULTS_FILE = os.path.join(self.tmp.name, "b.json")
        bench.machine = lambda: {"gpu": "test"}

    def tearDown(self):
        bench.RESULTS_FILE, bench.machine = self.saved
        self.tmp.cleanup()

    def test_choice_survives_a_rerun_only_if_it_still_works(self):
        bench.save([result("base", 80.0, 114688)], "quick")
        self.assertEqual(bench.choose("base")["chosen"],
                         {"id": "base", "variant": "base", "no_mtp": False})
        self.assertIsNone(bench.choose("nope"))
        doc = bench.save([result("base", 81.0, 114688)], "quick")
        self.assertEqual(doc["chosen"]["id"], "base")
        doc = bench.save([result("base", None, 0, "oom")], "quick")
        self.assertIsNone(doc["chosen"])

    def test_unreadable_file_is_no_results(self):
        with open(bench.RESULTS_FILE, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(bench.load())


# ---------------------------------------------------------------
#  A whole run, against a fake llama-server
# ---------------------------------------------------------------

class FakeServer:
    """What /health, /props, /tokenize and /completion would say."""

    def __init__(self, fail=(), root="http://127.0.0.1:8081"):
        self.root = root
        self.state = "stopped"
        self.setup = None
        self.polls = 0
        self.fail = set(fail)
        self.requests = []
        self.urls = []
        self.lock = threading.Lock()

    def started(self, args, env):
        with self.lock:
            self.setup = args
            self.state = "stopped" if args[1] in self.fail else "loading"
            self.polls = 0

    def stopped(self):
        with self.lock:
            self.state = "stopped"

    def locate(self, plat, port, key):
        with self.lock:
            if self.state == "loading":
                self.polls += 1
                if self.polls >= 2:
                    self.state = "ready"
                return self.root, 503
            return self.root, 200 if self.state == "ready" else 0

    def probe(self, base, key):
        no_mtp = "--no-mtp" in self.setup
        return {"auth": True, "alias": "x",
                "ctx": 204800 if no_mtp else 114688}

    def post(self, url, body, key, timeout=None):
        self.urls.append(url)
        self.requests.append((url.rsplit("/", 1)[-1], body))
        if url.endswith("/tokenize"):
            return {"tokens": list(range(3000))}
        assert self.state == "ready", "request to a server that is not up"
        no_mtp = "--no-mtp" in self.setup
        t = {"prompt_n": len(body["prompt"]), "prompt_per_second": 3000.0,
             "predicted_n": body["n_predict"],
             "predicted_per_second": 45.0 if no_mtp else 85.0}
        if not no_mtp:
            t.update(draft_n=100, draft_n_accepted=70)
        return {"content": "", "timings": t}


class FakeControl(control.Control):
    def __init__(self, server, start_exit=0, says="kiln says hi"):
        control.Control.__init__(self)
        self.server = server
        self.calls = []
        self.start_exit = start_exit
        self.says = says

    def spawn(self, action, title, args, env=None):
        self.calls.append((action, list(args), dict(env or {})))
        if action == "start":
            self.server.started(args, env or {})
            code = 1 if args[1] in self.server.fail else self.start_exit
        else:
            self.server.stopped()
            code = 0
        self.command = lambda a, code=code: [
            sys.executable, "-c", "import sys; print(%r); sys.exit(%d)"
            % (self.says, code)]
        return control.Control.spawn(self, action, title, args, env)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (bench.RESULTS_FILE, bench.machine, bench._post,
                      bench.vram_used_mib, control.locate, opencode.probe)
        bench.RESULTS_FILE = os.path.join(self.tmp.name, "b.json")
        bench.machine = lambda: {"gpu": "test"}
        bench.vram_used_mib = lambda: 30000

    def tearDown(self):
        (bench.RESULTS_FILE, bench.machine, bench._post, bench.vram_used_mib,
         control.locate, opencode.probe) = self.saved
        self.tmp.cleanup()

    def run_bench(self, server, setups, depth="quick", cancel_after=None,
                  says="kiln says hi"):
        bench._post = server.post
        control.locate = server.locate
        opencode.probe = server.probe
        ctl = FakeControl(server, says=says)
        events = []

        def emit(kind, *a):
            events.append((kind,) + a)
            if cancel_after and kind == "result" and len(
                    [e for e in events if e[0] == "result"]) == cancel_after:
                runner.cancel()

        runner = bench.Runner(ctl, setups, depth, emit)
        doc = runner.run()
        return ctl, events, doc

    def setups(self):
        return bench.setups(status([model("base"), model("9b", quant="Q8_0",
                                                          head=False)]))

    def test_each_setup_started_by_kiln_with_the_tunnel_off(self):
        server = FakeServer()
        ctl, events, doc = self.run_bench(server, self.setups())
        starts = [c for c in ctl.calls if c[0] == "start"]
        self.assertEqual([c[1] for c in starts],
                         [["start", "base"], ["start", "base", "--no-mtp"],
                          ["start", "9b"]])
        for c in starts:
            self.assertEqual(c[2], {"KILN_NO_TUNNEL": "1"})
        # A stop before every start, and one at the end.
        actions = [c[0] for c in ctl.calls]
        self.assertEqual(actions, ["stop", "start"] * 3 + ["stop"])
        self.assertEqual(server.state, "stopped")

        by_id = {r["id"]: r for r in doc["results"]}
        self.assertEqual(by_id["base"]["gen_tps"], 85.0)
        self.assertEqual(by_id["base"]["accept_pct"], 70.0)
        self.assertEqual(by_id["base-nomtp"]["gen_tps"], 45.0)
        self.assertIsNone(by_id["base-nomtp"]["accept_pct"])
        self.assertEqual(by_id["base-nomtp"]["ctx"], 204800)
        self.assertEqual(by_id["base"]["vram_used_mib"], 30000)
        self.assertNotIn("deep_gen_tps", by_id["base"])   # quick
        self.assertEqual(bench.load()["results"], doc["results"])
        self.assertEqual(events[-1][0], "done")
        self.assertIn(("log", ), [e[:1] for e in events])

    def test_requests_are_cold_and_sized(self):
        server = FakeServer()
        self.run_bench(server, self.setups()[:1])
        completions = [b for name, b in server.requests
                       if name == "completion"]
        self.assertTrue(all(b["cache_prompt"] is False for b in completions))
        # Seeded, so a rerun of the same setup samples the same tokens.
        self.assertTrue(all("seed" in b for b in completions))
        lengths = sorted({len(b["prompt"]) for b in completions})
        self.assertIn(bench.PREFILL_TOKENS, lengths)
        self.assertIn(bench.GEN_PROMPT_TOKENS, lengths)

    def test_thorough_adds_the_deep_test(self):
        server = FakeServer()
        _, _, doc = self.run_bench(server, self.setups()[:1], "thorough")
        r = doc["results"][0]
        self.assertEqual(r["deep_gen_tps"], 85.0)
        deep = [b for name, b in server.requests if name == "completion"
                and len(b["prompt"]) == bench.DEEP_TOKENS]
        self.assertEqual(len(deep), 1)

    def test_a_setup_that_will_not_load_is_recorded_and_skipped(self):
        server = FakeServer(fail={"base"})
        ctl, _, doc = self.run_bench(server, self.setups())
        by_id = {r["id"]: r for r in doc["results"]}
        self.assertIn("kiln start failed", by_id["base"]["error"])
        self.assertIn("kiln says hi", by_id["base"]["log_tail"])
        self.assertEqual(by_id["9b"]["gen_tps"], 85.0)

    def test_the_key_stays_out_of_a_failed_setups_log(self):
        # start.bat prints the key, and the failing setup's last lines are
        # saved to disk and can be copied from the benchmark screen.
        saved = opencode.KEY_FILE
        self.addCleanup(setattr, opencode, "KEY_FILE", saved)
        opencode.KEY_FILE = os.path.join(self.tmp.name, "api_key.txt")
        secret = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        with open(opencode.KEY_FILE, "w") as fh:
            fh.write(secret)
        server = FakeServer(fail={"base"})
        _, _, doc = self.run_bench(server, self.setups()[:1],
                                   says="API key : " + secret)
        tail = doc["results"][0]["log_tail"]
        self.assertIn("API key : <api key>", tail)
        self.assertFalse(secret in tail, "the key reached the log tail")
        with open(bench.RESULTS_FILE, encoding="utf-8") as fh:
            self.assertFalse(secret in fh.read(),
                             "the key reached the saved results")

    def test_requests_go_where_llama_server_answered(self):
        # Windows with a WSL or Docker forward holding 127.0.0.1:8080:
        # llama-server is found by the machine's name, and every timed
        # request must go there rather than to the other program.
        server = FakeServer(root="http://desktop-test:8080")
        self.run_bench(server, self.setups()[:1])
        self.assertTrue(server.urls)
        self.assertTrue(all(u.startswith("http://desktop-test:8080/")
                            for u in server.urls), server.urls)

    def test_cancel_stops_the_server_and_keeps_what_finished(self):
        server = FakeServer()
        ctl, events, doc = self.run_bench(server, self.setups(),
                                          cancel_after=1)
        self.assertEqual([r["id"] for r in doc["results"]], ["base"])
        self.assertEqual(ctl.calls[-1][0], "stop")
        self.assertEqual(server.state, "stopped")
        self.assertEqual(events[-1], ("done", doc, True))

    def test_cancel_mid_request_is_not_recorded_as_a_failure(self):
        # Cancelling kills the start job and the server with it, so the
        # request in flight fails. The setup did not.
        server = FakeServer()
        post = server.post

        def dies_under_cancel(url, body, key, timeout=None):
            if url.endswith("/completion") and len(server.requests) > 3:
                runner.cancel()
                server.stopped()
                raise bench.Failed("request to completion failed: reset")
            return post(url, body, key, timeout)

        bench._post = dies_under_cancel
        control.locate = server.locate
        opencode.probe = server.probe
        ctl = FakeControl(server)
        runner = bench.Runner(ctl, self.setups(), "quick")
        self.assertIsNone(runner.run())
        self.assertIsNone(bench.load())
        self.assertEqual(ctl.calls[-1][0], "stop")

    def test_the_key_is_read_after_the_first_start_makes_it(self):
        saved = opencode.KEY_FILE
        self.addCleanup(setattr, opencode, "KEY_FILE", saved)
        opencode.KEY_FILE = os.path.join(self.tmp.name, "api_key.txt")
        server = FakeServer()
        started, post = server.started, server.post

        def makes_the_key(args, env):
            if not os.path.exists(opencode.KEY_FILE):
                with open(opencode.KEY_FILE, "w") as fh:
                    fh.write("k3y")
            started(args, env)

        def wants_the_key(url, body, key, timeout=None):
            if key != "k3y":
                raise bench.Failed("%s answered 401" % url.rsplit("/", 1)[-1])
            return post(url, body, key, timeout)

        server.started, server.post = makes_the_key, wants_the_key
        _, _, doc = self.run_bench(server, self.setups()[:1])
        self.assertNotIn("error", doc["results"][0])
        self.assertEqual(doc["results"][0]["gen_tps"], 85.0)

    def test_the_server_is_stopped_after_the_front_end_has_gone(self):
        # A front end that quit mid-run fails every emit after it.
        server = FakeServer()
        bench._post = server.post
        control.locate = server.locate
        opencode.probe = server.probe
        ctl = FakeControl(server)

        def gone(kind, *a):
            if kind == "step" and a[0] == "Stopping the server":
                raise RuntimeError("App is not running")

        with self.assertRaises(RuntimeError):
            bench.Runner(ctl, self.setups()[:1], "quick", gone).run()
        self.assertEqual(ctl.calls[-1][0], "stop")
        self.assertEqual(server.state, "stopped")


    def test_ctrl_c_on_the_command_line_keeps_what_finished(self):
        server = FakeServer()
        post = server.post

        def interrupted(url, body, key, timeout=None):
            # Ctrl+C while the second setup is being measured.
            if len([c for c in ctl.calls if c[0] == "start"]) == 2:
                raise KeyboardInterrupt
            return post(url, body, key, timeout)

        bench._post = interrupted
        control.locate = server.locate
        opencode.probe = server.probe
        ctl = FakeControl(server)
        ctl.status = lambda: status([model("base")])
        real = control.Control
        self.addCleanup(setattr, control, "Control", real)
        control.Control = type("Patched", (real,), {"__new__": lambda c: ctl})
        with contextlib.redirect_stdout(io.StringIO()):
            code = bench.main(["base", "base-nomtp"])
        self.assertEqual(code, 130)
        self.assertEqual([r["id"] for r in bench.load()["results"]], ["base"])
        self.assertEqual(ctl.calls[-1][0], "stop")
        self.assertEqual(server.state, "stopped")

if __name__ == "__main__":
    unittest.main(verbosity=2)
