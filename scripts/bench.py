#!/usr/bin/env python3
"""
Find the setup that suits this machine by measuring it.

    bench.py --list                   the setups worth comparing here
    bench.py [--thorough] [ID ...]    measure them and print a table

kiln tui drives this interactively. The command line is here for
scripts, and for pasting results into a hardware report.

WHY NOT llama-bench
-------------------
`kiln bench` runs llama-bench, which has no speculative-decoding path,
so it cannot see MTP -- the largest single speed factor this model has,
and the thing the choice between setups mostly turns on. So this
measures what Cursor actually gets instead. Each setup is started with
`kiln start`, exactly as you would start it -- the same script, flags
and context sizing -- except that KILN_NO_TUNNEL keeps the Cloudflare
tunnel off, so benchmarking never opens a public URL. Then real
requests are timed against it:

  load        seconds from `kiln start` to the server answering
  context     the window the server actually opened, from /props
  VRAM        memory in use once loaded (NVIDIA only)
  prefill     prompt tokens/s over an 8K-token prompt
  generate    output tokens/s continuing code, the median of a few runs
  MTP accept  the share of drafted tokens the model kept
  deep        (thorough) prefill and generation with 32K tokens of
              context, which is closer to what an agent loop sends

The prompts are this repo's own source files, so they are real code in
every checkout and identical across machines. Timings come from
llama-server's own per-request `timings`, not from a stopwatch around
the HTTP call.

WHAT IT RECOMMENDS
------------------
The fastest setup that still opens at least KILN_MIN_CTX of context
(96K unless set) -- the bar hardware.py sizes everything against,
because Cursor's agent sends tens of thousands of tokens before the
model writes a line. If nothing reaches it, the setup with the most
room. Results go to kiln-benchmark.json, so the TUI can show them and
offer the pick as the default start.
"""

import argparse
import glob
import http.client
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import control  # noqa: E402
import hardware  # noqa: E402
import opencode  # noqa: E402

RESULTS_FILE = os.path.join(ROOT, "kiln-benchmark.json")

LOAD_TIMEOUT = 600      # a 30 GB model off a slow disk
STOP_TIMEOUT = 30
REQUEST_TIMEOUT = 300

PREFILL_TOKENS = 8192
GEN_PROMPT_TOKENS = 1024
DEEP_TOKENS = 32768
DEEP_GEN = 128
SEED = 4242

# Two setups' generation speeds count as a tie when their runs overlap
# or their medians are closer than this; the verdict says so rather
# than crowning one.
NOISE = 0.10

DEPTHS = {
    # name: (prefill runs, generation runs, tokens per run, deep test)
    #
    # MTP speed swings with how predictable the text is -- 94 and 136
    # tok/s on two slices of the same files, measured on a 5090 -- so
    # generation takes an odd number of runs and reports the median.
    "quick": (1, 3, 384, False),
    "thorough": (2, 5, 512, True),
}

# Rough minutes per setup, for the estimate shown before a run: load
# plus the requests. Load dominates and depends on the disk.
MINUTES = {"quick": 1.5, "thorough": 3.0}


class Failed(Exception):
    """This setup did not get as far as being measured."""


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------
#  What to compare
# ---------------------------------------------------------------

class Setup:
    """One way of starting a model that kiln start supports."""

    def __init__(self, model, mtp, has_head, ctx):
        self.variant = model["id"]
        self.label = model["label"]
        self.quant = model["quant"]
        self.alias = model["alias"]
        self.uncensored = bool(model.get("uncensored"))
        self.line = line_of(self.variant)
        self.suggest = suggestable(self.variant)
        self.mtp = mtp
        # --no-mtp only means something when there is a head to drop.
        self.no_mtp = has_head and not mtp
        self.id = self.variant + ("-nomtp" if self.no_mtp else "")
        self.ctx = ctx      # what kiln expects; the run reads the real one

    @property
    def title(self):
        return "%s %s, %s" % (self.label, self.quant,
                              "MTP" if self.mtp else "no MTP")

    @property
    def args(self):
        return ["start", self.variant] + (["--no-mtp"] if self.no_mtp else [])

    @property
    def request(self):
        """What control.Control.run takes to start this for real."""
        req = {"action": "start", "variant": self.variant}
        if self.no_mtp:
            req["no_mtp"] = True
        return req


def setups(status):
    """Every setup worth comparing on this machine: each downloaded
    model, with and without the MTP head where there is one to drop.
    None at all without llama.cpp: nothing could be started."""
    if not status["llama"]["installed"]:
        return []
    mtp_ok = status["llama"]["mtp"]
    by_id = {m["id"]: m for m in status["models"]}
    out = []
    for fam_id in control.START_ORDER:
        m = by_id.get(fam_id)
        if not m or not m["quant"] or control.too_old(status["llama"], m):
            continue
        if m["has_mtp_head"] and mtp_ok:
            out.append(Setup(m, True, True, m["ctx"]))
            out.append(Setup(m, False, True, m["ctx_no_mtp"]))
            continue
        # A head the build cannot use: start.bat sizes without it, the
        # shell scripts with it (and then leave it unloaded).
        ctx = m["ctx"]
        if m["has_mtp_head"] and status["platform"] == "windows":
            ctx = m["ctx_no_mtp"]
        out.append(Setup(m, False, False, ctx))
    return out


def runs_uncensored(status, preferred=""):
    """Whether the build the user runs is an uncensored one: the one
    they chose, else the one kiln start would pick."""
    if not preferred:
        preferred = ((load() or {}).get("chosen") or {}).get("variant")
    want = preferred or status.get("start_default")
    m = next((m for m in status["models"] if m["id"] == want), None)
    return bool(m and m.get("uncensored"))


def line_of(variant):
    """The model line a family belongs to: "qwen" or "gemma"."""
    return hardware.MODELS.get(variant, {}).get("group", "qwen")


def suggestable(variant):
    """False for a family kiln offers but never recommends (Gemma 4 E4B)."""
    return hardware.MODELS.get(variant, {}).get("suggest", True)


def runs_line(status, preferred=""):
    """The model line of the build the user runs, chosen as for
    runs_uncensored."""
    if not preferred:
        preferred = ((load() or {}).get("chosen") or {}).get("variant")
    return line_of(preferred or status.get("start_default") or "")


def _narrow(rows, *keeps):
    """Apply each filter in turn, skipping any that would leave none."""
    for keep in keeps:
        rows = [r for r in rows if keep(r)] or rows
    return rows


def default_setups(found, uncensored, line="qwen"):
    """What a run measures unless told otherwise: the setups of the kind
    the user runs (see recommend), or all of them if there are none."""
    return _narrow(found, lambda s: s.line == line,
                   lambda s: s.uncensored == uncensored,
                   lambda s: s.suggest)


def estimate_minutes(n, depth):
    return max(1, round(n * MINUTES[depth]))


# ---------------------------------------------------------------
#  Talking to llama-server
# ---------------------------------------------------------------

def _post(url, body, key, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise Failed("%s answered %d: %s" % (url.rsplit("/", 1)[-1], e.code,
                                             detail))
    except (OSError, ValueError, http.client.HTTPException) as e:
        # HTTPException: a reply cut short, as when the server dies
        # mid-answer. Anything uncaught here would end the run.
        raise Failed("request to %s failed: %s" % (url.rsplit("/", 1)[-1], e))


def corpus():
    """This repo's own source, as one long piece of real code."""
    parts = []
    for pattern in ("scripts/*.py", "scripts/windows/*.bat",
                    "scripts/unix/*.sh", "README.md"):
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read())
            except OSError:
                pass
    return "\n\n".join(parts)


def vram_used_mib():
    """Memory in use on the largest NVIDIA card, or 0 when there is no
    way to ask."""
    out = control._run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                        "--format=csv,noheader,nounits"])
    best_total, used = 0, 0
    for line in control._output(out).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            if int(parts[1]) > best_total:
                best_total, used = int(parts[1]), int(parts[0])
    return used


def timings(resp):
    t = resp.get("timings") if isinstance(resp, dict) else None
    if not isinstance(t, dict):
        raise Failed("llama-server returned no timings")
    return t


def acceptance(t):
    """Accepted / drafted, or None when nothing was drafted."""
    drafted = t.get("draft_n") or 0
    if not drafted:
        return None
    return (t.get("draft_n_accepted") or 0) / drafted


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


# ---------------------------------------------------------------
#  A run
# ---------------------------------------------------------------

class Runner:
    """Measures each setup in turn. Blocking: run it in a thread, and
    hear about progress through emit(kind, *details):

        ("setup", index, total, setup)   starting the next setup
        ("step", text)                   what it is doing now
        ("log", text)                    output from kiln start
        ("result", result)               one setup measured, or failed
        ("done", document, cancelled)    finished; saved to disk, or
                                         document["save_error"] says why not
    """

    def __init__(self, ctl, chosen, depth="quick", emit=None):
        self.ctl = ctl
        self.setups = list(chosen)
        self.depth = depth
        self.emit = emit or (lambda *a: None)
        self.results = []
        self._cancel = threading.Event()
        self._job = None
        self._offset = 0
        self.key = opencode.read_key()
        self.base = "http://127.0.0.1:%d" % ctl.port

    def cancel(self):
        self._cancel.set()
        job = self._job
        if job is not None:
            job.cancel()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _step(self, text):
        self.emit("step", text)

    # ---- the loop ---------------------------------------------

    def run(self):
        try:
            for i, setup in enumerate(self.setups):
                self._check()
                self.emit("setup", i, len(self.setups), setup)
                result = self.measure(setup)
                self.results.append(result)
                self.emit("result", result)
        except Cancelled:
            pass
        finally:
            # Leave the GPU as free as we found it at worst. The pick is
            # started on request, with the tunnel, by the caller. Stop
            # even when nobody is left to hear about it: a front end
            # that has quit fails the step, not the stop.
            try:
                self._step("Stopping the server")
            finally:
                self._stop()
        doc = record(self.results, self.depth) if self.results else None
        self.emit("done", doc, self.cancelled)
        return doc

    def _stop(self):
        self.ctl.spawn("stop", "Benchmark: stop the server", ["stop"]).wait(
            STOP_TIMEOUT)
        # The old server must be gone before the next one is timed, or
        # its /health answers for a model that is not loaded yet.
        end = time.time() + STOP_TIMEOUT
        while time.time() < end and self._status() == 200:
            time.sleep(0.5)

    def _status(self):
        root, status = control.locate(self.ctl.plat, self.ctl.port, self.key)
        if status == 200:
            # Wherever it answered: on Windows that is the machine's own
            # name when a port forward holds 127.0.0.1 -- see control.roots.
            self.base = root
        return status

    def _forward(self):
        job = self._job
        if job is None:
            return
        view = job.view(self._offset)
        self._offset = view["offset"]
        if view["text"]:
            self.emit("log", view["text"])

    # ---- one setup --------------------------------------------

    def measure(self, setup):
        result = {"id": setup.id, "variant": setup.variant,
                  "label": setup.label, "quant": setup.quant,
                  "mtp": setup.mtp, "no_mtp": setup.no_mtp,
                  "alias": setup.alias, "uncensored": setup.uncensored,
                  "ctx_expected": setup.ctx}
        try:
            self._step("Stopping anything already running")
            self._stop()
            self._check()
            self._step("Starting %s" % setup.title)
            started = time.time()
            self._offset = 0
            self._job = self.ctl.spawn("start", "Benchmark: " + setup.title,
                                       setup.args, {"KILN_NO_TUNNEL": "1"})
            self._wait_ready(started)
            result["load_s"] = round(time.time() - started, 1)
            # Read now, not when the run began: on a machine that has
            # never served, this first kiln start is what made the key.
            self.key = opencode.read_key()
            info = opencode.probe(self.base + "/v1", self.key) or {}
            result["ctx"] = info.get("ctx", 0)
            result["vram_used_mib"] = vram_used_mib()
            result.update(self._requests(result["ctx"]))
        except Failed as e:
            # Cancelling kills the server under a load or a request in
            # progress. That is the run stopping, not this setup failing.
            self._check()
            result["error"] = str(e)
            if self._job is not None:
                tail = self._job.view(0)["text"].strip().splitlines()[-12:]
                result["log_tail"] = "\n".join(tail)
        return result

    def _wait_ready(self, started):
        exited_ok = None
        while True:
            if self._cancel.is_set():
                self._job.cancel()
                raise Cancelled()
            self._forward()
            if self._status() == 200:
                self._forward()
                return
            job = self._job
            if not job.running:
                if job.code != 0:
                    raise Failed("kiln start failed (exit %s) -- usually "
                                 "out of memory" % job.code)
                # Windows: start.bat returns once the server answers. A
                # clean exit with nothing answering is not going to
                # improve with waiting.
                exited_ok = exited_ok or time.time()
                if time.time() - exited_ok > 15:
                    raise Failed("kiln start finished but llama-server "
                                 "does not answer on port %d" % self.ctl.port)
            if time.time() - started > LOAD_TIMEOUT:
                job.cancel()
                raise Failed("not serving after %d s" % LOAD_TIMEOUT)
            time.sleep(1)

    def _complete(self, tokens, n_predict, seed=SEED):
        self._check()
        # Server sampling, as Cursor gets it, but a fixed seed per run:
        # the same run on the same setup then samples the same tokens,
        # so a rerun is comparable rather than a fresh roll of the dice.
        resp = _post(self.base + "/completion",
                     {"prompt": tokens, "n_predict": n_predict,
                      "cache_prompt": False, "seed": seed}, self.key)
        self._check()
        return timings(resp)

    def _requests(self, ctx):
        prefill_runs, gen_runs, gen_tokens, deep = DEPTHS[self.depth]
        self._step("Reading the test prompt")
        toks = _post(self.base + "/tokenize", {"content": corpus()},
                     self.key).get("tokens") or []
        if len(toks) < 1024:
            raise Failed("could not tokenize the test prompt")
        while len(toks) < DEEP_TOKENS + 4096:
            toks = toks + toks

        out = {}
        self._step("Warming up")
        self._complete(toks[:256], 8)

        prefill = []
        for i in range(prefill_runs):
            self._step("Prefill: %dK-token prompt (%d/%d)"
                       % (PREFILL_TOKENS // 1024, i + 1, prefill_runs))
            # A different slice each run, so nothing is reused.
            start = i * 997
            t = self._complete(toks[start:start + PREFILL_TOKENS], 1)
            prefill.append(t.get("prompt_per_second"))
        out["prefill_tps"] = median(prefill)

        gen, accepts = [], []
        for i in range(gen_runs):
            self._step("Generating %d tokens (%d/%d)"
                       % (gen_tokens, i + 1, gen_runs))
            start = 4096 + i * 1531
            t = self._complete(toks[start:start + GEN_PROMPT_TOKENS],
                               gen_tokens, SEED + i)
            gen.append(t.get("predicted_per_second"))
            accepts.append(acceptance(t))
        out["gen_tps"] = median(gen)
        out["gen_runs"] = [round(g, 1) for g in gen if g is not None]
        acc = median([a * 100 for a in accepts if a is not None])
        out["accept_pct"] = acc

        if deep and ctx >= DEEP_TOKENS + 2048:
            self._step("Deep context: %dK tokens, then %d more"
                       % (DEEP_TOKENS // 1024, DEEP_GEN))
            t = self._complete(toks[:DEEP_TOKENS], DEEP_GEN)
            out["deep_prefill_tps"] = median([t.get("prompt_per_second")])
            out["deep_gen_tps"] = median([t.get("predicted_per_second")])
        return out


# ---------------------------------------------------------------
#  Results
# ---------------------------------------------------------------

def _runs(r):
    return r.get("gen_runs") or [r["gen_tps"]]


def is_uncensored(r):
    """From the result, or the registry for files saved before results
    said."""
    if "uncensored" in r:
        return bool(r["uncensored"])
    return bool(hardware.MODELS.get(r.get("variant"), {}).get("uncensored"))


def tied(a, b):
    """True when a's and b's generation speeds are within run-to-run
    noise of each other: their runs overlap, or the medians are within
    NOISE. MTP swings far more between runs than any fixed margin."""
    ra, rb = _runs(a), _runs(b)
    if min(ra) <= max(rb) and min(rb) <= max(ra):
        return True
    lo, hi = sorted((a["gen_tps"], b["gen_tps"]))
    return lo >= hi * (1 - NOISE)


def recommend(results, min_ctx=None, uncensored=False, line="qwen"):
    """{"pick", "fastest", "roomiest", "reason", "close", "set_aside"}
    over the setups that measured cleanly, or None when none did.

    Only builds of the kind the user runs are weighed: the same model
    line (Qwen unless line says Gemma), and stock unless uncensored is
    set. Which line, and whether abliterated, are choices about the
    model, not the hardware. E4B is not weighed while anything else
    is: it is too weak at tool calls to recommend. The others land in
    "set_aside"; when nothing of the kind measured, everything is
    weighed. "close" lists the setups that also clear the bar and tie
    with the pick on speed (see tied)."""
    floor = hardware.MIN_CTX if min_ctx is None else min_ctx
    ok = [r for r in results if not r.get("error") and r.get("gen_tps")]
    if not ok:
        return None
    pool = _narrow(ok, lambda r: line_of(r.get("variant")) == line,
                   lambda r: is_uncensored(r) == uncensored,
                   lambda r: suggestable(r.get("variant")))
    set_aside = [r for r in ok if r not in pool]
    fastest = max(pool, key=lambda r: r["gen_tps"])
    roomiest = max(pool, key=lambda r: (r.get("ctx") or 0, r["gen_tps"]))
    fits = [r for r in pool if (r.get("ctx") or 0) >= floor]
    if fits:
        pick = max(fits, key=lambda r: r["gen_tps"])
        reason = ("the fastest setup that keeps at least %dK of context, "
                  "which Cursor's agent needs" % (floor // 1024))
    else:
        pick = roomiest
        reason = ("nothing reached %dK of context, so the one with the "
                  "most room" % (floor // 1024))
    close = [r for r in fits if r is not pick and tied(r, pick)]
    return {"pick": pick, "fastest": fastest, "roomiest": roomiest,
            "reason": reason, "close": close, "set_aside": set_aside}


def set_aside_note(rec, uncensored=False, line="qwen"):
    """One sentence on the builds recommend left out, or ""."""
    if not rec["set_aside"]:
        return ""
    if any(line_of(r.get("variant")) != line for r in rec["set_aside"]):
        return ("Rows from the other model line are compared but not "
                "picked: Qwen or Gemma is your call, not the hardware's")
    if all(not suggestable(r.get("variant")) for r in rec["set_aside"]):
        return ("Gemma 4 E4B rows are compared but not picked: it is too "
                "weak at tool calls to recommend")
    if uncensored:
        return ("Only abliterated rows are picked from, since that is the "
                "build you run; the stock rows are there to compare")
    return ("Abliterated rows are compared but not picked: that build is "
            "the same size and speed as the stock one, so which to run is "
            "your call, not the hardware's")


def machine():
    vram, gpu, _ = hardware.detect_gpu()
    return {"gpu": gpu, "vram_mib": vram,
            "llama_build": control.llama_facts()["build"],
            "kiln": control.kiln_facts()["version"]}


def load():
    try:
        with open(RESULTS_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) and doc.get("results") else None
    except (OSError, ValueError):
        return None


def write(doc):
    tmp = RESULTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, RESULTS_FILE)


def document(results, depth):
    """The file's contents for a run. Built before anything is written,
    so the results survive when writing them fails."""
    old = load() or {}
    return {"version": 1, "when": time.strftime("%Y-%m-%d %H:%M"),
            "depth": depth, "machine": machine(), "results": results,
            # A choice survives a re-run as long as that setup still
            # measured cleanly.
            "chosen": old.get("chosen") if any(
                r["id"] == (old.get("chosen") or {}).get("id")
                and not r.get("error") for r in results) else None}


def save(results, depth, ctl=None):
    doc = document(results, depth)
    write(doc)
    return doc


def record(results, depth):
    """save, but a file that cannot be written (locked, read-only, full
    disk) is reported in the document's save_error, not raised: the
    minutes already measured are still shown."""
    doc = document(results, depth)
    try:
        write(doc)
    except OSError as e:
        doc["save_error"] = "could not save %s: %s" % (
            os.path.basename(RESULTS_FILE), e)
    return doc


def choose(setup_id):
    """Remember the setup the user picked, as the TUI's default start."""
    doc = load()
    if not doc:
        return None
    r = next((r for r in doc["results"] if r["id"] == setup_id), None)
    if r is None:
        return None
    doc["chosen"] = {"id": r["id"], "variant": r["variant"],
                     "no_mtp": bool(r.get("no_mtp"))}
    write(doc)
    return doc


def _num(x, fmt="%.0f"):
    return fmt % x if isinstance(x, (int, float)) else "-"


def kctx(n):
    return "%dK" % (n // 1024) if n else "-"


def setup_title(r):
    return "%s %s, %s" % (r["label"], r["quant"],
                          "MTP" if r.get("mtp") else "no MTP")


def report(doc, uncensored=False, line="qwen"):
    """A markdown table for a hardware report or an issue."""
    m = doc.get("machine", {})
    lines = [
        "**kiln benchmark** (%s): %s, %s MiB, llama.cpp build %s, kiln %s"
        % (doc.get("depth", "quick"), m.get("gpu") or "unknown GPU",
           m.get("vram_mib", "?"), m.get("llama_build") or "?",
           m.get("kiln") or "?"),
        "",
        "| Setup | Context | Load | Prefill t/s | Generate t/s | MTP accept"
        " | Deep gen t/s | VRAM used |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in doc["results"]:
        if r.get("error"):
            lines.append("| %s | %s | failed: %s | | | | | |"
                         % (setup_title(r), kctx(r.get("ctx_expected", 0)),
                            r["error"]))
            continue
        lines.append("| %s | %s | %s s | %s | %s | %s | %s | %s |" % (
            setup_title(r), kctx(r.get("ctx", 0)), _num(r.get("load_s")),
            _num(r.get("prefill_tps")), _num(r.get("gen_tps"), "%.1f"),
            _num(r.get("accept_pct"), "%.0f%%"),
            _num(r.get("deep_gen_tps"), "%.1f"),
            _num((r.get("vram_used_mib") or 0) / 1024 or None, "%.1f GB")))
    rec = recommend(doc["results"], uncensored=uncensored, line=line)
    if rec:
        lines += ["", "Pick: %s -- %s." % (setup_title(rec["pick"]),
                                           rec["reason"])]
        if rec["close"]:
            lines.append("Within run-to-run noise of it: %s."
                         % ", ".join(setup_title(r) for r in rec["close"]))
        if rec["set_aside"]:
            lines.append(set_aside_note(rec, uncensored, line) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------
#  Command line
# ---------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Measure each way of starting the downloaded models "
                    "and recommend one.")
    p.add_argument("ids", nargs="*",
                   help="setups to measure (default: those of the build you "
                        "run; --list marks them)")
    p.add_argument("--list", action="store_true",
                   help="show the setups and measure nothing")
    p.add_argument("--thorough", action="store_true",
                   help="more runs, plus a 32K-token deep-context test")
    p.add_argument("--report", action="store_true",
                   help="print the last results as markdown")
    args = p.parse_args(argv)

    ctl = control.Control()
    status = ctl.status()
    uncensored = runs_uncensored(status)
    line = runs_line(status)
    if args.report:
        doc = load()
        if not doc:
            print("No benchmark results yet.")
            return 1
        print(report(doc, uncensored, line))
        return 0

    found = setups(status)
    if not found:
        print("Nothing to measure: no weights downloaded. Run: kiln get")
        return 1
    usual = default_setups(found, uncensored, line)
    if args.list:
        width = max(len(s.id) for s in found)
        for s in found:
            print("%s %-*s %s  (%s context)" % ("*" if s in usual else " ",
                                                width, s.id, s.title,
                                                kctx(s.ctx)))
        print("\n* measured when no setups are named")
        return 0
    chosen = [s for s in found if s.id in args.ids] if args.ids else usual
    unknown = set(args.ids) - {s.id for s in found}
    if unknown:
        print("Unknown setup: %s. Try --list." % ", ".join(sorted(unknown)))
        return 1

    depth = "thorough" if args.thorough else "quick"
    print("Measuring %d setup(s), about %d min. The server is stopped first "
          "and the tunnel stays off." % (len(chosen),
                                         estimate_minutes(len(chosen), depth)))

    def emit(kind, *a):
        if kind == "setup":
            print("\n[%d/%d] %s" % (a[0] + 1, a[1], a[2].title))
        elif kind == "step":
            print("  " + a[0])
        elif kind == "result" and a[0].get("error"):
            print("  failed: " + a[0]["error"])

    runner = Runner(ctl, chosen, depth, emit)
    interrupted = False
    try:
        doc = runner.run()
    except KeyboardInterrupt:
        # run() has stopped the server on the way out. Keep the setups
        # that finished, as cancelling in kiln tui does.
        runner.cancel()
        interrupted = True
        doc = record(runner.results, depth) if runner.results else None
    if doc:
        print()
        print(report(doc, uncensored, line))
        if "save_error" in doc:
            print("\n" + doc["save_error"])
        else:
            print("\nSaved to %s" % os.path.basename(RESULTS_FILE))
    if interrupted:
        return 130
    return 0 if doc and "save_error" not in doc else 1


if __name__ == "__main__":
    sys.exit(main())
