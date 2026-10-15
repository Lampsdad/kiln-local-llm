"""
The Textual app behind `kiln tui`, started by tui.py once Textual is
importable.

Everything it shows comes from control.py, and everything it runs is a
kiln verb through control.Control, so this file is layout, keys and
wording. The benchmark is bench.py; BenchScreen is its interactive face.

Job output and anything read from the machine is shown as plain text,
never parsed as markup: a model name or a log line with brackets in it
must not restyle, or hide, what is on screen.
"""

import asyncio
import atexit
import os
import shutil
import signal
import subprocess
import sys
import threading

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (Button, DataTable, Footer, Header, Label,
                             ProgressBar, RichLog, SelectionList, Static)
from textual.widgets.selection_list import Selection

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import bench  # noqa: E402
import control  # noqa: E402
import opencode  # noqa: E402

KILN_THEME = Theme(
    name="kiln",
    primary="#ff8a3d", secondary="#c27a4a", accent="#ffb36b",
    warning="#f0c04d", error="#ff6b6b", success="#5cd08a",
    foreground="#ede7e0", background="#12100f", surface="#1a1816",
    panel="#221f1c", dark=True,
)

POLL_STATUS = 3.0
POLL_JOB = 0.4

# Columns below this: the machine panel folds into one line of the
# server panel, and tables drop their least useful columns.
NARROW = 100


def kctx(n):
    return "%dK" % (n // 1024) if n else "-"


def gib(mib):
    return ("%.0f GB" if mib >= 10240 else "%.1f GB") % (mib / 1024)


def plain(text):
    """Text shown exactly as given -- no markup."""
    return Content(text)


def cell(text):
    """The same, for DataTable, which parses a str as markup and can
    only measure Rich text for its column widths."""
    return Text(text)


def sentence(text):
    """First letter up, the rest as written (str.capitalize lowercases
    it: "cursor's", "96k"), and a full stop."""
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def facts(rows):
    """Label and value columns, for the side panels: a value too long
    for the panel wraps inside its column, not back under the labels."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, value if isinstance(value, Text) else Text(value))
    return grid


def log_view(**kw):
    # Wrapped, so a long download line is readable without scrolling
    # sideways; never markup, since it shows whatever a script printed.
    return RichLog(wrap=True, markup=False, highlight=False, min_width=20,
                   **kw)


def say(log, line):
    log.write(Text(line))


def visible(line):
    """What a terminal would show for a line with carriage returns in
    it: the last thing written over the start of the line."""
    line = line.rstrip("\r")
    return line.rsplit("\r", 1)[-1]


def copy_native(text):
    """The platform's own clipboard tool. Textual's copy uses OSC 52,
    which the classic Windows console and some Linux terminals ignore,
    so this runs as well where a tool exists."""
    if control.WINDOWS:
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    else:
        cmd = None
        for tool, args in (("wl-copy", []),
                           ("xclip", ["-selection", "clipboard"]),
                           ("xsel", ["--clipboard", "--input"]),
                           ("clip.exe", [])):          # WSL
            if shutil.which(tool):
                cmd = [tool] + args
                break
    if not cmd:
        return False
    try:
        return subprocess.run(cmd, input=text.encode("utf-8"), timeout=5,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              **control._quiet()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------
#  Dialogs
# ---------------------------------------------------------------

class Confirm(ModalScreen):
    """Yes / no. Dismisses with True or False."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog {
        width: 70; max-width: 90%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 2; }
    """
    BINDINGS = [Binding("y", "answer(True)", "Yes"),
                Binding("n,escape", "answer(False)", "No")]

    def __init__(self, heading, body, yes="Yes"):
        super().__init__()
        self.heading, self.body, self.yes = heading, body, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(plain(self.heading), id="dialog-title")
            yield Static(plain(self.body))
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel  (n)", id="no")
                yield Button(self.yes + "  (y)", variant="primary", id="yes")

    def on_mount(self):
        self.query_one("#yes", Button).focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value):
        self.dismiss(value)


HELP = """\
On the main screen
  n        do the next step the status suggests
  up/down  choose a model
  s        start the chosen model (switches if one is serving)
  d        download the chosen model
  x        stop the server and the tunnel
  m        start with or without the MTP draft head
             (without: a larger window, roughly half the speed)
  l        start with or without the Cloudflare tunnel
  b        benchmark: measure each setup and pick one
  k / u / t  copy the API key / local URL / tunnel URL
  o        list the models in OpenCode
  p        list the models in pi
  i        install llama.cpp, or re-run setup
  g        upgrade llama.cpp (Windows)
  U        update kiln itself
  R        rotate the API key
  c        cancel what is running
  r        refresh now
  q        quit -- a running model keeps serving

On the benchmark screen
  space    include or leave out a setup
  t        quick or thorough
  r        run
  c        cancel the run
  a        start the selected result and make it the default
  p        copy the results as a markdown table
  escape   back
"""


class Help(ModalScreen):
    DEFAULT_CSS = """
    Help { align: center middle; }
    #help {
        width: 72; max-width: 95%; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    """
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(plain(HELP), id="help")

    def action_close(self):
        self.dismiss(None)


# ---------------------------------------------------------------
#  Benchmark
# ---------------------------------------------------------------

BENCH_INTRO = (
    "Starts each setup exactly as `kiln start` would and times real "
    "requests against it, so you can see what each one gets you on this "
    "machine. The tunnel stays off while measuring. Running this stops "
    "the server; start the one you want afterwards with a.")

# (id, header). Deep only appears for a thorough run, the one that
# measures it; Load and VRAM give way on a narrow terminal.
RESULT_COLUMNS = (("setup", "Setup"), ("ctx", "Context"), ("load", "Load"),
                  ("prefill", "Prefill t/s"), ("gen", "Gen t/s"),
                  ("accept", "MTP accept"), ("deep", "Deep t/s"),
                  ("vram", "VRAM"))


def result_columns(deep, narrow):
    return [(k, "Accept" if narrow and k == "accept" else h)
            for k, h in RESULT_COLUMNS
            if (deep or k != "deep")
            and not (narrow and k in ("load", "vram"))]


def _num(x, fmt="%.0f"):
    return fmt % x if isinstance(x, (int, float)) else "-"


def result_cells(r, pick_id=None, state=None):
    """{column id: text} for one row. The quant is left out of the
    title: there is one per model, and the list above names it."""
    failed = bool(r.get("error")) and not state
    mark = "✗" if failed else "★" if r.get("id") == pick_id else " "
    title = "%s %s, %s" % (mark, r["label"],
                           "MTP" if r.get("mtp") else "no MTP")
    if state or failed:
        return {"setup": title, "ctx": kctx(r.get("ctx_expected", 0)),
                "gen": state or "failed"}
    vram = r.get("vram_used_mib") or 0
    return {"setup": title, "ctx": kctx(r.get("ctx", 0)),
            "load": _num(r.get("load_s"), "%.0f s"),
            "prefill": _num(r.get("prefill_tps")),
            "gen": _num(r.get("gen_tps"), "%.1f"),
            "accept": _num(r.get("accept_pct"), "%.0f%%"),
            "deep": _num(r.get("deep_gen_tps"), "%.1f"),
            "vram": gib(vram) if vram else "-"}


class BenchScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "run", "Run"),
        Binding("t", "depth", "Quick/thorough"),
        Binding("c", "cancel", "Cancel run"),
        Binding("a", "use", "Use selected"),
        Binding("p", "copy_report", "Copy report"),
    ]

    def __init__(self, ctl, status, preferred=""):
        super().__init__()
        self.ctl = ctl
        self.status = status
        self.setups = bench.setups(status)
        # The kind of build the user runs: the pick comes from those,
        # and only those are ticked to start with (see bench.recommend).
        self.uncensored = bench.runs_uncensored(status, preferred)
        self.line = bench.runs_line(status, preferred)
        self.ticked = {s.id for s in bench.default_setups(
            self.setups, self.uncensored, self.line)}
        self.depth = "quick"
        self.runner = None
        self.doc = bench.load()
        self.partial = ""
        self.row_ids = []
        self.results = {}
        self.states = {}       # id -> "waiting" / "measuring", mid-run
        self.pick_id = None
        self.deep = False
        self.narrow = None
        self.verdict = []

    def compose(self) -> ComposeResult:
        yield Header()
        # Scrolls, so 24 rows still reach the verdict.
        with VerticalScroll(id="bench"):
            yield Static(plain(BENCH_INTRO), id="bench-intro")
            with Vertical(id="bench-pick"):
                yield SelectionList(*[
                    Selection(plain("%s  ·  %s context"
                                    % (s.title, kctx(s.ctx))), s.id,
                              s.id in self.ticked)
                    for s in self.setups], id="setups")
                yield Static(id="bench-depth")
            with Vertical(id="bench-out"):
                yield DataTable(id="results", cursor_type="row",
                                classes="empty")
                yield Static(plain("No results yet. Choose the setups to "
                                   "compare and press r."), id="verdict")
            yield ProgressBar(id="progress", show_eta=False)
            yield Static(id="step")
            yield log_view(id="bench-log", max_lines=2000)
        yield Footer()

    def on_mount(self):
        self.sub_title = "benchmark"
        self.query_one("#bench-pick").border_title = "Setups to compare"
        self.query_one("#bench-out").border_title = "Results"
        self.query_one("#bench-log").border_title = "kiln start output"
        self.show_depth()
        if not self.setups:
            self.query_one("#step", Static).update(plain(
                "Nothing to measure: no weights are downloaded yet. "
                "Press escape, then d to download a model."))
        if self.doc:
            self.show_results(self.doc["results"])
            self.query_one("#step", Static).update(plain(
                "Last run: %s (%s). Press r to measure again."
                % (self.doc.get("when", "?"), self.doc.get("depth", "quick"))))
        self.query_one("#setups").focus()

    # ---- display ----------------------------------------------

    def show_depth(self):
        n = len(self.query_one("#setups", SelectionList).selected)
        what = ("Quick: one prefill and three generation runs per setup"
                if self.depth == "quick" else
                "Thorough: more runs, plus generation with 32K tokens of "
                "context already loaded")
        self.query_one("#bench-depth", Static).update(plain(
            "%s.  %d selected, about %d min.  t changes depth."
            % (what, n, bench.estimate_minutes(max(n, 1), self.depth))))

    def show_results(self, results):
        rec = bench.recommend(results, uncensored=self.uncensored,
                              line=self.line)
        self.pick_id = rec["pick"]["id"] if rec else None
        self.row_ids = [r["id"] for r in results]
        self.results = {r["id"]: r for r in results}
        self.states = {}
        self.deep = any(r.get("deep_gen_tps") for r in results)
        self.fill_table()
        if self.pick_id in self.row_ids:
            self.query_one("#results", DataTable).move_cursor(
                row=self.row_ids.index(self.pick_id))
        self.show_verdict(results, rec)

    def fill_table(self, width=None):
        """The results table from row_ids and results, with the columns
        that fit the terminal."""
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        self.narrow = (width or self.app.size.width) < NARROW
        cols = result_columns(self.deep, self.narrow)
        table.clear(columns=True)
        for key, header in cols:
            table.add_column(header, key=key)
        for sid in self.row_ids:
            cells = result_cells(self.results[sid], self.pick_id,
                                 self.states.get(sid))
            table.add_row(*[cell(cells.get(k, "")) for k, _ in cols],
                          key=sid)
        table.set_class(not self.row_ids, "empty")
        if row is not None and row < len(self.row_ids):
            table.move_cursor(row=row)

    def on_resize(self, event):
        if self.narrow is not None and \
                (event.size.width < NARROW) != self.narrow:
            self.fill_table(event.size.width)

    def show_verdict(self, results, rec):
        rows = []      # (mark, sentence): the grid keeps wrapped lines
        if rec:        # indented under their sentence, not at column 0
            p = rec["pick"]
            rows.append(("★", "Pick: %s  —  %.1f tok/s at %s context"
                         % (bench.setup_title(p), p["gen_tps"],
                            kctx(p.get("ctx", 0)))))
            rows.append(("", sentence(rec["reason"])))
            if rec["close"]:
                rows.append(("", "Within run-to-run noise of it, so as good "
                             "a choice: %s." % ", ".join(
                                 bench.setup_title(r) for r in rec["close"])))
            if rec["fastest"]["id"] != p["id"]:
                f = rec["fastest"]
                rows.append(("", "Fastest: %s, %.1f tok/s at %s." % (
                    bench.setup_title(f), f["gen_tps"],
                    kctx(f.get("ctx", 0)))))
            if rec["roomiest"]["id"] != p["id"]:
                f = rec["roomiest"]
                rows.append(("", "Most context: %s, %s at %.1f tok/s." % (
                    bench.setup_title(f), kctx(f.get("ctx", 0)),
                    f["gen_tps"])))
            if rec["set_aside"]:
                rows.append(("", sentence(bench.set_aside_note(
                    rec, self.uncensored, self.line))))
            rows.append(("", "a starts the selected row and makes it the "
                         "default. p copies a report."))
        for r in results:
            if r.get("error"):
                rows.append(("✗", "%s: %s" % (bench.setup_title(r),
                                              r["error"])))
        self.verdict = rows
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, width=1)
        grid.add_column()
        for mark, text in rows:
            grid.add_row(mark, Text(text))
        self.query_one("#verdict", Static).update(grid)

    def feed(self, text):
        log = self.query_one("#bench-log", RichLog)
        lines = (self.partial + text).split("\n")
        self.partial = lines.pop()
        for line in lines:
            say(log, visible(line))

    # ---- actions ----------------------------------------------

    def check_action(self, action, parameters):
        running = self.runner is not None
        if action == "run":
            return None if running or not self.setups else True
        if action == "depth":
            return None if running else True
        if action == "cancel":
            return True if running else None
        if action in ("use", "copy_report"):
            return True if self.doc and not running else None
        return True

    def on_selection_list_selected_changed(self, event):
        self.show_depth()

    def action_depth(self):
        self.depth = "thorough" if self.depth == "quick" else "quick"
        self.show_depth()

    def action_back(self):
        if self.runner is not None:
            self.notify("Cancel the run first (c).", severity="warning")
            return
        self.app.pop_screen()

    def action_run(self):
        ids = set(self.query_one("#setups", SelectionList).selected)
        chosen = [s for s in self.setups if s.id in ids]
        if not chosen:
            self.notify("Choose at least one setup (space).",
                        severity="warning")
            return
        busy = self.ctl.blocking()
        if busy:
            self.notify("Wait for “%s” to finish first."
                        % busy.title, severity="warning")
            return
        state = self.app.status["server"]["state"] if self.app.status else ""
        if state in ("ready", "loading"):
            self.app.push_screen(Confirm(
                "Stop the server and benchmark?",
                "The model serving now is stopped while each setup is "
                "measured. Cursor loses its connection until you start one "
                "again.", yes="Benchmark"),
                lambda ok: ok and self.start_run(chosen))
        else:
            self.start_run(chosen)

    def start_run(self, chosen):
        self.query_one("#progress").add_class("running")
        self.row_ids = [s.id for s in chosen]
        self.results = {s.id: {"id": s.id, "label": s.label,
                               "quant": s.quant, "mtp": s.mtp,
                               "ctx_expected": s.ctx} for s in chosen}
        self.states = {s.id: "waiting" for s in chosen}
        self.pick_id = None
        self.deep = self.depth == "thorough"
        self.fill_table()
        self.verdict = []
        self.query_one("#verdict", Static).update("")
        self.query_one("#bench-log", RichLog).clear()
        self.query_one("#progress", ProgressBar).update(total=len(chosen),
                                                        progress=0)
        self.runner = bench.Runner(
            self.ctl, chosen, self.depth,
            emit=lambda *a: self.app.call_from_thread(self.on_bench, *a))
        self.refresh_bindings()
        self.measure()

    @work(thread=True, group="bench")
    def measure(self):
        self.runner.run()

    def set_row(self, sid):
        table = self.query_one("#results", DataTable)
        cells = result_cells(self.results[sid], self.pick_id,
                             self.states.get(sid))
        for key in table.columns:
            table.update_cell(sid, key, cell(cells.get(key.value, "")),
                              update_width=True)

    def on_bench(self, kind, *a):
        if kind == "setup":
            index, total, setup = a
            self.query_one("#progress", ProgressBar).update(progress=index)
            self.states[setup.id] = "measuring"
            self.set_row(setup.id)
            self.query_one("#results", DataTable).move_cursor(
                row=self.row_ids.index(setup.id))
            say(self.query_one("#bench-log", RichLog),
                "--- %s ---" % setup.title)
        elif kind == "step":
            self.query_one("#step", Static).update(plain(a[0]))
        elif kind == "log":
            self.feed(a[0])
        elif kind == "result":
            r = a[0]
            self.results[r["id"]] = r
            self.states.pop(r["id"], None)
            self.set_row(r["id"])
        elif kind == "done":
            doc, cancelled = a
            self.runner = None
            self.query_one("#progress").remove_class("running")
            if doc:
                self.doc = doc
                self.show_results(doc["results"])
            self.query_one("#step", Static).update(plain(
                "Cancelled. The server is stopped." if cancelled else
                "Done. The server is stopped; press a to start a setup."))
            self.notify("Benchmark cancelled" if cancelled else
                        "Benchmark finished")
            self.refresh_bindings()
            self.app.refresh_status()

    def action_cancel(self):
        if self.runner is not None:
            self.runner.cancel()
            self.query_one("#step", Static).update(plain(
                "Cancelling, then stopping the server..."))

    def selected_result(self):
        table = self.query_one("#results", DataTable)
        if not self.row_ids or table.cursor_row is None:
            return None
        return self.results.get(self.row_ids[table.cursor_row])

    def action_use(self):
        r = self.selected_result()
        if not r or "gen_tps" not in r and not r.get("error"):
            self.notify("Choose a measured row first.", severity="warning")
            return
        if r.get("error"):
            self.notify("That setup did not start on this machine.",
                        severity="error")
            return
        cli = "kiln start %s%s" % (r["variant"],
                                   " --no-mtp" if r.get("no_mtp") else "")
        m = next((x for x in self.app.status["models"]
                  if x["id"] == r["variant"]), None)
        notes = self.app.start_notes(m, bool(r.get("no_mtp"))) if m else []
        self.app.push_screen(Confirm(
            "Start %s?" % bench.setup_title(r),
            "\n\n".join(["It becomes the default when you press s in kiln "
                         "tui. From the command line, the same thing is:  "
                         + cli] + notes),
            yes="Start"),
            lambda ok: ok and self.use(r))

    def use(self, r):
        bench.choose(r["id"])
        self.app.prefer(r["variant"], bool(r.get("no_mtp")))
        self.app.pop_screen()
        self.app.start_model(r["variant"], confirm=False)

    def action_copy_report(self):
        if self.doc:
            self.app.copy_text(bench.report(self.doc, self.uncensored,
                                            self.line),
                               "Benchmark report")

    def on_unmount(self):
        if self.runner is not None:
            self.runner.cancel()


# ---------------------------------------------------------------
#  Main screen
# ---------------------------------------------------------------

SHADOWED = {
    # Windows lets that program's 127.0.0.1 bind sit in front of
    # llama-server's 0.0.0.0 one; the URL above goes by machine name.
    "windows": ("Another program answers on 127.0.0.1:%d, usually a WSL or "
                "Docker port forward. Cursor pointed at localhost, and the "
                "tunnel, would reach it instead of the model. Use the URL "
                "above, or move that program off port %d."),
    # Elsewhere the two binds clash, so the model cannot start at all.
    "other": ("Another program is using port %d, so llama-server cannot "
              "start there. Stop it, or move it off port %d."),
}

SERVER_STATES = {
    "stopped": ("○ stopped", "muted"),
    "loading": ("◔ loading the model", "warning"),
    "ready": ("● serving", "success"),
    "foreign": ("✗ port taken by another program", "error"),
}

# Console events that mean the window is going rather than a key being
# pressed: its close button, logoff, and shutdown. Windows gives the
# process about five seconds after each.
CLOSING_EVENTS = (2, 5, 6)


def closing_event(event, stop):
    """The body of a Windows console control handler: runs stop for the
    events that close the window, and says whether it handled the event."""
    if event not in CLOSING_EVENTS:
        return False
    stop()
    return True


def console_close_hook(stop):
    """Registers closing_event with Windows. The caller must keep the
    returned callback, or ctypes frees it and Windows calls nothing."""
    import ctypes
    from ctypes import wintypes
    handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(
        lambda event: closing_event(event, stop))
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler


class KilnApp(App):
    TITLE = "kiln"
    # Classes on every screen, for the CSS below and NARROW in code.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (NARROW, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (32, "-tall")]
    CSS = """
    Screen { background: $background; }
    #next {
        height: auto; margin: 1 1 0 1; padding: 0 1;
        border: round $primary; background: $primary 12%;
        display: none;
    }
    #next.shown { display: block; }
    #top { height: auto; }
    #server, #machine {
        height: auto; padding: 0 1; margin: 1 0 0 1;
        border: round $secondary; border-title-color: $primary;
    }
    #server { width: 3fr; }
    /* Whole margins only: margin-right alone zeroes the other sides. */
    #machine { width: 2fr; margin: 1 1 0 1; }
    #models {
        height: auto; max-height: 9; margin: 1 1 0 1;
        border: round $secondary; border-title-color: $primary;
    }
    #models:focus { border: round $primary; }
    #activity {
        height: 1fr; min-height: 6; margin: 1 1 0 1;
        border: round $secondary; border-title-color: $primary;
    }
    #log { height: 1fr; background: $surface; overflow-x: hidden; }
    #live { height: 1; color: $text-muted; }
    Screen.-narrow #machine { display: none; }
    Screen.-narrow #server { margin: 1 1 0 1; }
    /* 24 rows has no room for gaps between panels. */
    Screen.-short #next, Screen.-short #machine, Screen.-short #models,
    Screen.-short #activity, Screen.-short #bench-pick,
    Screen.-short #bench-out, Screen.-short #bench-log { margin: 0 1; }
    Screen.-short #server { margin: 0 0 0 1; }
    Screen.-short.-narrow #server { margin: 0 1; }
    Screen.-short #step, Screen.-short #progress { margin: 0 2; }
    Screen.-short #activity { min-height: 4; }

    #bench { height: 1fr; scrollbar-size-vertical: 1; }
    #bench-intro { margin: 1 2 0 2; color: $text-muted; }
    #bench-pick, #bench-out {
        height: auto; margin: 1 1 0 1;
        border: round $secondary; border-title-color: $primary;
    }
    #setups { height: auto; max-height: 10; border: none; }
    #bench-depth { color: $text-muted; padding: 0 1; }
    #results { height: auto; max-height: 10; }
    #results.empty { display: none; }
    #verdict { padding: 0 1; height: auto; }
    #progress { display: none; margin: 1 2 0 2; }
    #progress.running { display: block; }
    #progress Bar > .bar--bar, #progress Bar > .bar--indeterminate {
        color: $primary;
    }
    #step { margin: 1 2 0 2; height: auto; color: $accent; }
    RichLog {
        scrollbar-background: $surface; scrollbar-color: $secondary;
        scrollbar-color-hover: $primary; scrollbar-color-active: $primary;
    }
    #bench-log {
        height: 1fr; min-height: 4; margin: 1 1 0 1;
        border: round $secondary; border-title-color: $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("n", "next_step", "Next step"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("d", "download", "Download"),
        Binding("b", "benchmark", "Benchmark"),
        Binding("k", "copy('key')", "Copy key"),
        Binding("u", "copy('url')", "Copy URL"),
        Binding("t", "copy('tunnel')", "Copy tunnel", show=False),
        Binding("m", "toggle_mtp", "MTP on/off", show=False),
        Binding("l", "toggle_local", "Tunnel on/off", show=False),
        Binding("o", "simple('opencode')", "OpenCode", show=False),
        Binding("p", "simple('pi')", "pi", show=False),
        Binding("i", "simple('setup')", "Setup", show=False),
        Binding("g", "simple('update')", "Upgrade llama.cpp", show=False),
        Binding("U", "simple('self-update')", "Update kiln", show=False),
        Binding("R", "simple('key-rotate')", "Rotate key", show=False),
        Binding("c", "cancel_job", "Cancel", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ctl=None):
        super().__init__()
        self.ctl = ctl or control.Control()
        self.status = None
        self.no_mtp = False
        self.local = False
        self.preferred = ""
        self._prefs_applied = False
        self._refreshing = False
        self.row_ids = []
        self._rows_sig = None
        self.job_id = None
        self.job_offset = 0
        self.partial = ""
        self.job_seen_running = False
        self.narrow = False
        self._jobs_stopped = False
        self._jobs_lock = threading.Lock()
        self._close_hook = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="next")
        with Horizontal(id="top"):
            yield Static(plain("Reading this machine..."), id="server")
            yield Static(id="machine")
        yield DataTable(id="models", cursor_type="row")
        with Vertical(id="activity"):
            yield log_view(id="log", max_lines=5000)
            yield Static(id="live")
        yield Footer()

    def on_mount(self):
        self.register_theme(KILN_THEME)
        self.theme = "kiln"
        self.query_one("#server").border_title = "Server"
        self.query_one("#machine").border_title = "This machine"
        self.query_one("#models").border_title = "Models"
        self.query_one("#activity").border_title = "Activity"
        self.narrow = self.size.width < NARROW
        self.query_one("#models", DataTable).focus()
        say(self.query_one("#log", RichLog),
            "Output from each action appears here. Press ? for every key.")
        # Closing the terminal, or a kill, must not leave a download or a
        # setup running where nobody can see it or cancel it.
        if control.WINDOWS:
            self._close_hook = console_close_hook(self.stop_jobs)
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGHUP, signal.SIGTERM):
                loop.add_signal_handler(sig, self.quit_on_signal)
        self.refresh_status()
        self.set_interval(POLL_STATUS, self.refresh_status)
        self.set_interval(POLL_JOB, self.poll_job)

    # ---- state ------------------------------------------------

    @work(thread=True, group="status")
    def refresh_status(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            s = self.ctl.status()
            s["benchmark"] = bench.load()
        except Exception as e:  # keep the app alive; say what broke
            self.call_from_thread(self.notify, "Could not read status: %s"
                                  % e, severity="error", markup=False)
            return
        finally:
            self._refreshing = False
        self.call_from_thread(self.show_status, s)

    def is_busy(self):
        if self.status and self.status.get("busy"):
            return True
        job = self.ctl.jobs.get(self.job_id) if self.job_id else None
        return bool(job and job.running and job.action != "start")

    def selected(self):
        table = self.query_one("#models", DataTable)
        if not self.status or not self.row_ids or table.cursor_row is None:
            return None
        mid = self.row_ids[min(table.cursor_row, len(self.row_ids) - 1)]
        return next((m for m in self.status["models"] if m["id"] == mid),
                    None)

    def prefer(self, variant, no_mtp):
        self.preferred = variant
        self.no_mtp = no_mtp
        if variant in self.row_ids:
            self.query_one("#models", DataTable).move_cursor(
                row=self.row_ids.index(variant))

    def show_status(self, s):
        first = self.status is None
        self.status = s
        chosen = (s.get("benchmark") or {}).get("chosen")
        if not self._prefs_applied:
            self._prefs_applied = True
            on_disk = {m["id"] for m in s["models"] if m["quant"]}
            if chosen and chosen.get("variant") in on_disk:
                self.preferred = chosen["variant"]
                self.no_mtp = bool(chosen.get("no_mtp"))
            else:
                self.preferred = s["start_default"]
        kiln = s["kiln"]
        self.sub_title = (kiln["version"] or "") + (
            "  ·  %s available, U to update" % kiln["update"]
            if kiln["update"] else "")
        self.show_next(s)
        self.show_models(s, first)
        self.show_server(s)
        self.show_machine(s)
        self.refresh_bindings()

    def show_next(self, s):
        box = self.query_one("#next", Static)
        n = s["next"]
        box.set_class(bool(n), "shown")
        if n:
            box.update(plain("Next:  %s    press n" % n["text"]))

    def show_models(self, s, first):
        live = s["server"]["alias"] if s["server"]["state"] == "ready" else ""
        pick = ((s.get("benchmark") or {}).get("chosen") or {}).get("variant")
        rows = []
        for m in s["models"]:
            marks = []
            if live and live == m["alias"]:
                marks.append("●")
            if pick == m["id"]:
                marks.append("◆")
            if m["recommended"]:
                marks.append("★")
            if m["quant"]:
                disk = m["quant"]
                ctx = kctx(m["ctx"])
                if m["has_mtp_head"] and m["ctx_no_mtp"]:
                    ctx += " / %s no MTP" % kctx(m["ctx_no_mtp"])
            elif m["get"]:
                disk = "no"
                ctx = "%s, ~%s GB" % (kctx(m["get"]["ctx"]), m["get"]["gb"])
            else:
                disk = "no"
                ctx = "does not fit" if s["machine"]["vram_mib"] else "-"
            notes = []
            if m["uncensored"]:
                notes.append("abliterated")
            if m["third_party"]:
                notes.append("third-party distill")
            if not m["vision"]:
                notes.append("text only")
            if m["quant"] and control.too_old(s["llama"], m):
                notes.append("needs llama.cpp b%d+" % m["min_build"])
            if m["recommended"]:
                notes.append("best fit")
            cells = (" ".join(marks), m["label"], disk, ctx, ", ".join(notes))
            # Notes give way first: the label and the marks carry most
            # of what they say.
            rows.append((m["id"], cells[:4] if self.narrow else cells))
        sig = repr(rows)
        if sig == self._rows_sig:
            return
        self._rows_sig = sig
        table = self.query_one("#models", DataTable)
        keep = self.row_ids[table.cursor_row] if (
            self.row_ids and table.cursor_row is not None
            and table.cursor_row < len(self.row_ids)) else None
        table.clear(columns=True)
        table.add_columns(*("", "Model", "On disk", "Context",
                            "Notes")[:len(rows[0][1]) if rows else 5])
        self.row_ids = []
        for mid, cells in rows:
            table.add_row(*[cell(c) for c in cells], key=mid)
            self.row_ids.append(mid)
        target = self.preferred if first else keep
        if target in self.row_ids:
            table.move_cursor(row=self.row_ids.index(target))

    def show_server(self, s):
        srv = s["server"]
        theme = self.current_theme
        color = {"warning": theme.warning or "yellow",
                 "success": theme.success or "green",
                 "error": theme.error or "red"}
        label, style = SERVER_STATES.get(srv["state"], (srv["state"], ""))
        head = Text(label, style=color.get(style, "dim"))
        if srv["state"] == "ready" and srv["alias"]:
            head.append("  %s  ·  %s context%s" % (
                srv["alias"], kctx(srv["ctx"]),
                "  ·  vision" if srv["vision"] else ""))
        tunnel = srv["tunnel"] or (
            "cloudflared not installed, so no public URL"
            if not s["cloudflared"] else
            "opens with the server" if srv["state"] == "stopped" else "none")
        key = s["key"]["masked"] or "made on the first start"
        m = self.selected()
        start = "-"
        if m and m["quant"]:
            mtp = ("no MTP head" if not m["has_mtp_head"] else
                   "MTP off" if self.no_mtp else "MTP on")
            start = "%s %s  ·  %s  ·  tunnel %s" % (
                m["label"], m["quant"], mtp, "off" if self.local else "on")
        rows = []
        for name, value, hint in (
                ("URL", srv["local_url"], "u copy"),
                ("Tunnel", tunnel, "t copy" if srv["tunnel"] else ""),
                ("Key", key, "k copy" if s["key"]["present"] else ""),
                ("Start", start, "m/l")):
            value = Text(value)
            if hint:
                value.append("   " + hint, style="dim")
            rows.append((name, value))
        if self.narrow:
            rows.append(("Machine", self.machine_line(s)))
        parts = [head, facts(rows)]
        if srv["state"] == "ready" and srv["auth"] is False:
            parts.append(Text("The server holds an older key. Restart it to "
                              "load the current one.", style=color["warning"]))
        if srv.get("shadowed"):
            text = SHADOWED.get(self.ctl.plat, SHADOWED["other"])
            parts.append(Text(text % (srv["port"], srv["port"]),
                              style=color["error"]))
        self.query_one("#server", Static).update(Group(*parts))

    def machine_line(self, s):
        """The machine panel in one line, for a narrow terminal."""
        mc, ll = s["machine"], s["llama"]
        out = ["%s  ·  %s" % (mc["gpu"], gib(mc["vram_mib"]))
               if mc["gpu"] else "GPU not detected"]
        out.append("llama.cpp %s" % (ll["build"] or "?")
                   if ll["installed"] else "llama.cpp not installed")
        return "  ·  ".join(out)

    def show_machine(self, s):
        mc, ll, rec = s["machine"], s["llama"], s["recommend"]
        gpu = ("%s  ·  %s" % (mc["gpu"], gib(mc["vram_mib"]))
               if mc["gpu"] else "not detected")
        rows = [("GPU", gpu), ("Profile", mc["tier"] or "unknown")]
        if rec:
            rows.append(("", "runs %s %s at %s" % (rec["label"], rec["quant"],
                                                   kctx(rec["ctx"]))))
        if ll["installed"]:
            llama = "build %s  ·  MTP %s" % (
                ll["build"] or "?",
                "available" if ll["mtp"] else "unavailable, needs b9180+")
        else:
            llama = "not installed  (i to install)"
        rows += [
            ("llama", llama),
            ("Tunnel", "cloudflared installed" if s["cloudflared"]
             else "cloudflared missing, no public URL"),
            ("CPU/RAM", "%s cores  ·  %s" % (mc["cores"], gib(mc["ram_mib"]))),
            ("Disk", "%s GB free" % s["disk_free_gb"]),
        ]
        self.query_one("#machine", Static).update(facts(rows))

    def on_resize(self, event):
        # event.size: self.size has not caught up yet.
        narrow = event.size.width < NARROW
        if narrow != self.narrow:
            self.narrow = narrow
            if self.status:
                self.show_models(self.status, False)
                self.show_server(self.status)

    def on_data_table_row_highlighted(self, event):
        if self.status and event.data_table.id == "models":
            self.show_server(self.status)

    # ---- jobs -------------------------------------------------

    def run_request(self, req):
        try:
            job = self.ctl.run(req)
        except control.Busy as e:
            self.notify("Wait for “%s” to finish first."
                        % e.job.title, severity="warning")
            return None
        except ValueError as e:
            self.notify(str(e), severity="error")
            return None
        self.attach(job)
        self.refresh_status()
        return job

    def attach(self, job):
        self.job_id = job.id
        self.job_offset = 0
        self.partial = ""
        self.job_seen_running = False
        self.query_one("#log", RichLog).clear()
        self.query_one("#live", Static).update("")
        box = self.query_one("#activity")
        box.border_title = "Activity  ·  " + job.title
        box.border_subtitle = "running"
        self.refresh_bindings()

    def poll_job(self):
        job = self.ctl.jobs.get(self.job_id) if self.job_id else None
        if job is None:
            return
        v = job.view(self.job_offset)
        log = self.query_one("#log", RichLog)
        if v["truncated"]:
            say(log, "[earlier output trimmed]")
        if v["text"]:
            lines = (self.partial + v["text"]).split("\n")
            self.partial = lines.pop()
            for line in lines:
                say(log, visible(line))
            self.query_one("#live", Static).update(
                plain(visible(self.partial)))
        self.job_offset = v["offset"]
        box = self.query_one("#activity")
        if v["running"]:
            box.border_subtitle = ("serving" if job.action == "start" and
                                   self.status and self.status["server"][
                                       "state"] == "ready" else "running")
            self.job_seen_running = True
        else:
            box.border_subtitle = ("cancelled" if v["cancelled"] else
                                   "done" if v["code"] == 0 else
                                   "failed, exit %s" % v["code"])
            if self.job_seen_running:
                self.job_seen_running = False
                self.finished(v)

    def finished(self, v):
        self.refresh_bindings()
        self.refresh_status()
        if v["cancelled"]:
            self.notify("Cancelled: " + v["title"])
        elif v["code"] == 0:
            if v["action"] == "self-update":
                self.notify("kiln is updated. Quit and run kiln tui again "
                            "to load the new version.", timeout=15)
            else:
                self.notify("Done: " + v["title"])
        else:
            self.notify("%s failed (exit %s). The output is under Activity."
                        % (v["title"], v["code"]), severity="error",
                        timeout=10)

    # ---- actions ----------------------------------------------

    # App bindings stay live under every screen. These belong to the
    # main one: "s" on the benchmark screen must not start a model.
    MAIN_ONLY = {"next_step", "start", "stop", "download", "benchmark",
                 "copy", "toggle_mtp", "toggle_local", "simple",
                 "cancel_job", "refresh"}

    def check_action(self, action, parameters):
        if action in self.MAIN_ONLY and len(self.screen_stack) > 1:
            return False
        s = self.status
        if s is None:
            return True if action in ("help", "quit", "refresh") else None
        busy = self.is_busy()
        if action in ("start", "download", "next_step", "benchmark"):
            return None if busy else True
        if action == "simple":
            if parameters and parameters[0] == "update" and not s[
                    "features"]["update"]:
                return False
            return None if busy else True
        if action == "stop":
            return True if s["server"]["state"] != "stopped" else None
        if action == "cancel_job":
            job = self.ctl.jobs.get(self.job_id) if self.job_id else None
            return True if job and job.running else None
        return True

    def ask(self, heading, body, then, yes="Yes"):
        self.push_screen(Confirm(heading, body, yes),
                         lambda ok: ok and then())

    def action_next_step(self):
        n = self.status and self.status["next"]
        if not n:
            self.notify("Nothing to do: the server is up.")
        elif n["action"] == "start":
            self.prefer(n["variant"], self.no_mtp)
            self.start_model(n["variant"])
        elif n["action"] == "get":
            self.download(n.get("variant") or "")
        else:
            self.action_simple(n["action"])

    def action_start(self):
        m = self.selected()
        if not m:
            return
        if not m["quant"]:
            self.notify("%s is not downloaded. Press d to get it."
                        % m["label"], severity="warning")
            return
        self.start_model(m["id"])

    def start_model(self, variant, confirm=True):
        s = self.status
        m = next(x for x in s["models"] if x["id"] == variant)
        no_mtp = self.no_mtp and m["has_mtp_head"]
        req = {"action": "start", "variant": variant, "no_mtp": no_mtp,
               "local": self.local}
        if not confirm:
            self.run_request(req)
            return
        parts = self.start_notes(m, no_mtp)
        self.ask("Start %s %s?" % (m["label"], m["quant"]),
                 "\n\n".join(parts) or "Serves it to Cursor on this machine.",
                 lambda: self.run_request(req), yes="Start")

    def start_notes(self, m, no_mtp):
        """What to say before starting m: shared by the Start key and the
        benchmark's "use this setup", so neither skips a warning."""
        s = self.status
        tunnel = s["cloudflared"] and not self.local
        parts = []
        if s["server"].get("shadowed") and tunnel:
            parts.append("WARNING: another program answers on "
                         "127.0.0.1:%d. The tunnel forwards to localhost, so "
                         "it would publish that program, not the model. "
                         "Say no, then press l to start without the tunnel."
                         % s["server"]["port"])
        if s["server"]["state"] in ("ready", "loading"):
            parts.append("The model running now is stopped first.")
        if tunnel:
            parts.append("This opens a public Cloudflare tunnel. The API key "
                         "is what keeps strangers out. Press l first to "
                         "skip the tunnel.")
        if m["uncensored"]:
            parts.append("This build has had its refusal behaviour removed "
                         "and has had no safety evaluation.")
        if no_mtp:
            parts.append("Without the MTP head, generation is roughly half "
                         "as fast; the window grows to %s."
                         % kctx(m["ctx_no_mtp"]))
        return parts

    def action_download(self):
        m = self.selected()
        if m:
            if m["quant"]:
                self.notify("%s is already on disk." % m["label"])
                return
            self.download(m["id"])

    def download(self, variant):
        m = next((x for x in self.status["models"] if x["id"] == variant),
                 None)
        if m and m["get"]:
            body = "%s, about %s GB, opening %s of context on this GPU." % (
                m["get"]["quant"], m["get"]["gb"], kctx(m["get"]["ctx"]))
        elif m and self.status["machine"]["vram_mib"]:
            body = "kiln does not expect it to fit this GPU."
        else:
            body = "kiln picks the quant this machine is sized for."
        self.ask("Download %s?" % (m["label"] if m else
                                   "the recommended build"), body,
                 lambda: self.run_request({"action": "get",
                                           "variant": variant}),
                 yes="Download")

    def action_stop(self):
        self.ask("Stop the server?", "Stops llama-server and the Cloudflare "
                 "tunnel. Cursor loses its connection.",
                 lambda: self.run_request({"action": "stop"}), yes="Stop")

    SIMPLE = {
        "setup": ("Run kiln setup?", "Installs llama.cpp and cloudflared."),
        "update": ("Upgrade llama.cpp?", "Fetches the newest build and keeps "
                   "the current one as a backup."),
        "self-update": ("Update kiln?", "Moves to the newest release. Your "
                        "models, llama.cpp and API key are not touched."),
        "key-rotate": ("Rotate the API key?", "Cursor stops working until you "
                       "paste in the new key, and the server only loads it "
                       "on its next start."),
        "opencode": None,
        "pi": None,
    }

    def action_simple(self, action):
        if action not in self.SIMPLE:
            return
        req = {"action": action}
        text = self.SIMPLE[action]
        if text is None:
            self.run_request(req)
        else:
            self.ask(text[0], text[1], lambda: self.run_request(req))

    def action_toggle_mtp(self):
        m = self.selected()
        if m and not m["has_mtp_head"]:
            self.notify("%s has no MTP head to drop." % m["label"])
            return
        self.no_mtp = not self.no_mtp
        self.notify("Next start: MTP %s" % ("off, larger window"
                                            if self.no_mtp else "on, faster"))
        self.show_server(self.status)

    def action_toggle_local(self):
        self.local = not self.local
        self.notify("Next start: %s" % ("no tunnel, so no public URL"
                                        if self.local else
                                        "with the Cloudflare tunnel"))
        self.show_server(self.status)

    def copy_text(self, text, what):
        self.copy_to_clipboard(text)
        copy_native(text)
        self.notify("%s copied" % what)

    def action_copy(self, what):
        srv = self.status["server"]
        if what == "key":
            key = opencode.read_key()
            if not key:
                self.notify("No key yet: one is made on the first start.",
                            severity="warning")
                return
            self.copy_text(key, "API key")
        elif what == "url":
            self.copy_text(srv["local_url"], "Local URL")
        elif srv["tunnel"]:
            self.copy_text(srv["tunnel"], "Tunnel URL")
        else:
            self.notify("No tunnel is open.", severity="warning")

    def action_cancel_job(self):
        job = self.ctl.jobs.get(self.job_id) if self.job_id else None
        if job and job.running:
            job.cancel()

    def action_refresh(self):
        self.ctl.invalidate()
        self.refresh_status()

    def action_benchmark(self):
        if not self.status["llama"]["installed"]:
            self.notify("Install llama.cpp first (i), then benchmark.",
                        severity="warning")
            return
        if not bench.setups(self.status):
            self.notify("Download a model first (d), then benchmark it.",
                        severity="warning")
            return
        self.push_screen(BenchScreen(self.ctl, self.status, self.preferred))

    def action_help(self):
        self.push_screen(Help())

    async def action_quit(self):
        # Anywhere in the stack: ctrl+q still quits from the help
        # screen or the command palette opened over a run.
        if any(isinstance(s, BenchScreen) and s.runner
               for s in self.screen_stack):
            self.notify("Cancel the benchmark first (c).", severity="warning")
            return
        running = [j for j in self.ctl.jobs.values()
                   if j.running and j.action != "start"]
        if running:
            self.ask("Quit?", "“%s” is still running and will be "
                     "cancelled. A model that is serving keeps serving."
                     % running[0].title, self.quit_now, yes="Quit")
        else:
            self.quit_now()

    def stop_jobs(self):
        """Cancel what nobody will be able to watch. Runs once: quitting
        does it, and so does every other way out of the app. A model that
        is serving keeps serving."""
        with self._jobs_lock:
            if self._jobs_stopped:
                return []
            self._jobs_stopped = True
        return self.ctl.shutdown()

    def quit_on_signal(self):
        # The terminal has gone (SIGHUP), or something asked for a stop
        # (SIGTERM). Leave the way q does, so nothing is left running.
        self.stop_jobs()
        self.exit()

    def quit_now(self):
        self.stop_jobs()
        self.exit()


def main(argv=None):
    app = KilnApp()
    # However the app ends, an uncaught error included, the jobs stop.
    atexit.register(app.stop_jobs)
    app.run()
    return 0
