#!/usr/bin/env python3
"""
Read kiln's state and run kiln verbs, for front ends that are not a
shell: the terminal UI (tui_app.py) and the benchmark (bench.py).

A FRONT DOOR, NOT A REWRITE
---------------------------
Every action runs a kiln verb -- kiln.bat on Windows, kiln.sh elsewhere
-- exactly as typing it would. The scripts stay the single source of
truth for installing, downloading and launching. This file only reads
state, through the same hardware.py and opencode.py the scripts use, and
captures a verb's output while it runs.

Verbs run with stdin closed. Every prompt in the scripts falls back to
its default on empty input: the recommended build and quant, and "no"
to going on past the free-space check. So nothing waits on a keypress
that cannot arrive.

Actions and their arguments are matched against a fixed table
(build_args). Nothing a user types reaches a command line.

THE SERVER OUTLIVES THE FRONT END
---------------------------------
Closing the TUI must not stop llama-server. Verbs run in a console of
their own on Windows and a session of their own elsewhere, so Ctrl+C in
the terminal reaches neither. `kiln stop` is the way to stop the
server, same as without a front end. Anything else still running when
the front end exits, a download say, is cancelled: nobody would be able
to see it any more, and hf_hub_download resumes where it left off.

Output goes to a temporary file rather than a pipe. On Windows,
start.bat launches llama-server with `start /B`, which hands it
start.bat's own stdout. A pipe would stay open for as long as the
server ran and the job would never be seen to finish. A file does not
care who else holds it.
"""

import codecs
import glob
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
# Importing hardware.py would otherwise leave __pycache__ in the checkout.
sys.dont_write_bytecode = True
import hardware  # noqa: E402  (the registry and the sizing arithmetic)
import opencode  # noqa: E402  (what is on disk, and what answers on the port)

WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

# What the board checks on Windows, and the only place start.bat looks.
CF_EXE_WINDOWS = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
CF_LOGS = ("cloudflared-err.log", "cloudflared.log")
TUNNEL = re.compile(r"https://[-a-z0-9]+(?:\.[-a-z0-9]+)*\.trycloudflare\.com",
                    re.I)

# Hardware and binaries change only when someone installs something, so
# probing them costs a few seconds once rather than on every poll. A
# finished job drops the cache, since that is when they do change.
SLOW_TTL = 300

# Families in the order start.bat and lib_variants.sh pick from when no
# variant is named: stock before abliterated, big before small.
START_ORDER = tuple(hardware.DISK_ORDER)

KEEP_JOBS = 20


# ---------------------------------------------------------------
#  Where we are
# ---------------------------------------------------------------

def platform_id():
    """What kiln.sh would call this machine, or "windows"."""
    if WINDOWS:
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                k, _, v = line.partition("=")
                if k in ("ID", "ID_LIKE") and re.search(
                        r"fedora|rhel|centos", v, re.I):
                    return "fedora"
    except OSError:
        pass
    return "linux"


def start_script(plat):
    if plat == "windows":
        return os.path.join(ROOT, "scripts", "windows", "start.bat")
    return os.path.join(ROOT, "scripts", "unix", "start_%s.sh" % plat)


def server_port(plat):
    """The port this platform's start script serves on. Read from the
    script rather than restated here, so the two cannot drift apart.
    Every start script uses 8080."""
    try:
        with open(start_script(plat), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s*(?:set\s+)?PORT=(\d+)\s*$", line, re.I)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return 8080


def llama_binary():
    name = "llama-server.exe" if WINDOWS else "llama-server"
    return os.path.join(ROOT, "llama-bin", name)


def _quiet():
    """Popen arguments that stop a console window flashing up for each
    probe when the front end itself has none."""
    return {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}


def _run(argv, timeout=20):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              errors="replace", timeout=timeout, **_quiet())
    except (OSError, subprocess.SubprocessError):
        return None


def _output(proc):
    return (proc.stdout or "") + (proc.stderr or "") if proc else ""


# ---------------------------------------------------------------
#  Reading state -- the same facts the status board prints
# ---------------------------------------------------------------

def kiln_facts():
    """The checkout's version, and the newer release if there is one.
    Run as its own process, as the board does, so its once-a-day cache
    and KILN_NO_UPDATE_CHECK behave exactly as they do there."""
    proc = _run([sys.executable, os.path.join(HERE, "selfupdate.py"),
                 "--notice"], timeout=30)
    pairs = dict(line.split("=", 1) for line in _output(proc).splitlines()
                 if "=" in line)
    return {"version": pairs.get("KILN_VERSION", ""),
            "update": pairs.get("KILN_UPDATE", "")}


def llama_facts():
    exe = llama_binary()
    if not os.path.isfile(exe) or not (WINDOWS or os.access(exe, os.X_OK)):
        return {"installed": False, "build": "", "mtp": False}
    # Newer builds print "version: 0.4.0-dev (build 10901, commit ...)",
    # older ones "version: 9431 (94ca829b6)". Either way, never match a
    # bare "build": the "built with Clang" line would answer -- see
    # NOTES.md.
    out = _output(_run([exe, "--version"]))
    m = re.search(r"\(build (\d+)", out) or re.search(r"version:\s*(\d+)", out)
    # MTP speculative decoding merged in b9180. Probe for the flag, as
    # the scripts do, rather than comparing build numbers.
    mtp = "draft-mtp" in _output(_run([exe, "--help"]))
    return {"installed": True, "build": m.group(1) if m else "", "mtp": mtp}


def cloudflared_installed(plat):
    if plat == "windows":
        return os.path.isfile(CF_EXE_WINDOWS)
    return shutil.which("cloudflared") is not None


def slow_facts(plat):
    vram, gpu, vendor = hardware.detect_gpu()
    _, tier = hardware.tier_for(vram)
    plan = hardware.recommend(vram) if vram else None
    rec = None
    if plan:
        fam = hardware.MODELS[plan["family"]]
        total = (plan["quant_bytes"]
                 + (fam["mtp_heads"][plan["mtp"]].bytes if plan["mtp"] else 0)
                 + (fam["mmproj"][plan["mmproj"]].bytes
                    if plan["mmproj"] else 0))
        rec = {"family": plan["family"], "label": fam["label"],
               "quant": plan["quant"], "ctx": plan["ctx"],
               "mtp": plan["mtp"] or "", "vision": plan["mmproj"] or "",
               "total_gb": round(total / 1e9, 1),
               "third_party": bool(fam.get("third_party")),
               "below_min": bool(plan.get("below_min"))}
    return {
        "kiln": kiln_facts(),
        "llama": llama_facts(),
        "cloudflared": cloudflared_installed(plat),
        "machine": {"gpu": gpu, "vendor": vendor, "vram_mib": vram,
                    "tier": tier if vram else "",
                    "cores": hardware.detect_cores(),
                    "ram_mib": hardware.detect_ram_mib()},
        "recommend": rec,
    }


def _exists(name):
    return os.path.isfile(os.path.join(opencode.MODELS_DIR, name))


def quant_of(fam_id, weights):
    """UD-Q5_K_XL out of models/Qwen3.8-27B-UD-Q5_K_XL.gguf."""
    prefix = hardware.MODELS[fam_id]["prefix"]
    return os.path.basename(weights)[len(prefix) + 1:-len(".gguf")]


def model_rows(vram, rec_family):
    """One row per family: what is on disk, the window it would open,
    and what `kiln get` would fetch for it on this card."""
    rows = []
    for fam_id, fam in hardware.MODELS.items():
        files = opencode.on_disk(fam_id)
        quant, ctx, ctx_no_mtp = "", 0, 0
        if files:
            quant = quant_of(fam_id, files[0])
            ctx = opencode.planned_ctx(fam_id, files, vram)
            if files[1]:
                ctx_no_mtp = opencode.planned_ctx(
                    fam_id, (files[0], "", files[2]), vram)
        plan = hardware.plan_for(vram, fam_id) if vram else None
        get = None
        if plan:
            # The shared MTP head and projector are skipped when the
            # exact file is already there, as the download scripts do.
            extra = 0
            if plan["mtp"]:
                head = fam["mtp_heads"][plan["mtp"]]
                extra += 0 if _exists(head.local) else head.bytes
            if plan["mmproj"]:
                proj = fam["mmproj"][plan["mmproj"]]
                extra += 0 if _exists(proj.local) else proj.bytes
            get = {"quant": plan["quant"], "ctx": plan["ctx"],
                   "gb": round((plan["quant_bytes"] + extra) / 1e9, 1)}
        rows.append({
            "id": fam_id, "label": fam["label"], "alias": fam["alias"],
            "params": fam["params"], "uncensored": fam["uncensored"],
            "third_party": bool(fam.get("third_party")),
            "min_build": fam["min_build"],
            "mtp": fam["mtp"], "vision": fam["vision"],
            "quant": quant, "ctx": ctx, "ctx_no_mtp": ctx_no_mtp,
            "has_mtp_head": bool(files and files[1]),
            "get": get, "recommended": fam_id == rec_family,
        })
    return rows


def shared_files():
    """An MTP head and a projector on disk, whichever family and
    precision they are for."""
    fams = list(hardware.MODELS.values())
    mtp = next((h.local for f in fams for h in f["mtp_heads"].values()
                if _exists(h.local)), "")
    mm = next((p.local for f in fams for p in f["mmproj"].values()
               if _exists(p.local)), "")
    return {"mtp": mtp, "vision": mm}


# kiln's own quick tunnel is the cloudflared forwarding to its port.
# Any other -- a named tunnel, T3 Code's remote access -- is not ours,
# and must not make a stale log's URL look live. The same rule as
# scripts/unix/lib_tunnel.sh and scripts/windows/kiln_tunnel.ps1.
TUNNEL_ARGS = r"tunnel .*--url http://(localhost|127\.0\.0\.1):%d( |$)"
# The same rule for llama-server: kiln's is the one serving kiln's port,
# as scripts/unix/lib_server.sh finds it.
SERVER_ARGS = r"llama-server.* --port %d( |$)"


def processes(plat, port):
    """(llama-server running, kiln's tunnel running)."""
    if plat == "windows":
        out = _output(_run(["tasklist", "/NH", "/FO", "CSV"])).lower()
        # The command line needs PowerShell, so only ask when some
        # llama-server.exe or cloudflared.exe is running at all.
        llama = False
        if '"llama-server.exe"' in out:
            proc = _run(["powershell", "-NoProfile", "-ExecutionPolicy",
                         "Bypass", "-File",
                         os.path.join(HERE, "windows", "kiln_server.ps1"),
                         "-Port", str(port)])
            llama = proc is not None and proc.returncode == 0
        tunnel = False
        if '"cloudflared.exe"' in out:
            proc = _run(["powershell", "-NoProfile", "-ExecutionPolicy",
                         "Bypass", "-File",
                         os.path.join(HERE, "windows", "kiln_tunnel.ps1"),
                         "-Port", str(port)])
            tunnel = proc is not None and proc.returncode == 0
        return llama, tunnel

    llama = False
    proc = _run(["pgrep", "-f", SERVER_ARGS % port])
    if proc is not None and proc.returncode == 0:
        # As for the tunnel below: pgrep -f matches the command line, and
        # ps names the program.
        for pid in proc.stdout.split():
            comm = _output(_run(["ps", "-o", "comm=", "-p", pid])).strip()
            if os.path.basename(comm) == "llama-server":
                llama = True
    tunnel = False
    proc = _run(["pgrep", "-f", TUNNEL_ARGS % port])
    if proc is not None and proc.returncode == 0:
        # pgrep -f also matches a script's interpreter; ps names the
        # program (the full path on macOS).
        for pid in proc.stdout.split():
            comm = _output(_run(["ps", "-o", "comm=", "-p", pid])).strip()
            if os.path.basename(comm) == "cloudflared":
                tunnel = True
    return llama, tunnel


def tunnel_url():
    """The newest quick-tunnel address any cloudflared log mentions."""
    found, newest = "", -1.0
    for name in CF_LOGS:
        path = os.path.join(ROOT, name)
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                hits = TUNNEL.findall(fh.read())
        except OSError:
            continue
        if hits and mtime > newest:
            found, newest = hits[-1], mtime
    return found


def roots(plat, port):
    """Where llama-server may be reached, best first.

    On Windows a WSL or Docker port forward binds 127.0.0.1:<port>, which
    outranks llama-server's 0.0.0.0:<port> -- a container publishing
    8080 is enough. 127.0.0.1 then reaches that program, not the model,
    while the machine's own name still reaches llama-server. opencode.py
    falls back the same way."""
    out = ["http://127.0.0.1:%d" % port]
    if plat == "windows":
        out.append("http://%s:%d" % (socket.gethostname(), port))
    return out


def llama_health(root, key):
    """200 serving, 503 loading -- but only when it is llama-server that
    answers, judged by its replies rather than by the port being open.
    0 for nothing, or for some other program. The key is sent only when an
    older llama-server asks for it, which is after it has identified itself."""
    status, body = opencode._get(root + "/health", "")
    doc = opencode._json(body)
    if status == 401 and opencode._auth_error(doc):
        status, body = opencode._get(root + "/health", key)
        doc = opencode._json(body)
    if not isinstance(doc, dict):
        return 0
    if status == 200 and doc.get("status") == "ok":
        return 200
    if status == 503 and isinstance(doc.get("error"), dict):
        return 503
    return 0


def locate(plat, port, key):
    """(root, status): the first address llama-server answers on and its
    /health status, or (the loopback address, 0) when it answers nowhere."""
    candidates = roots(plat, port)
    for root in candidates:
        status = llama_health(root, key)
        if status:
            return root, status
    return candidates[0], 0


def shadowed(port, key):
    """True when something other than llama-server answers on
    127.0.0.1:<port>. Cursor pointed at localhost, and the Cloudflare
    tunnel, which forwards to localhost, would reach that program."""
    root = "http://127.0.0.1:%d" % port
    return (opencode._get(root + "/health", "")[0] != 0
            and llama_health(root, key) == 0)


def server_facts(plat, port, key):
    """stopped, loading, ready, or foreign -- something else answers."""
    llama_up, cf_up = processes(plat, port)
    root, status = locate(plat, port, key)
    info = opencode.probe(root + "/v1", key) if status == 200 else None
    if status == 200:
        state = "ready" if info is not None else "foreign"
    else:
        state = "loading" if llama_up or status == 503 else "stopped"
    in_the_way = shadowed(port, key)
    if in_the_way and not status:
        # Not up yet: show the URL that will get past the program in
        # the way once it is (by machine name, on Windows).
        root = roots(plat, port)[-1]
    out = {"state": state, "port": port, "local_url": root + "/v1",
           "alias": "", "ctx": 0, "vision": False, "auth": True,
           "tunnel": "", "shadowed": in_the_way}
    if info:
        out.update(auth=info.get("auth", True),
                   alias=info.get("alias", ""), ctx=info.get("ctx", 0),
                   vision=info.get("vision", False))
    # Only meaningful while both halves are up: a log outlives the
    # tunnel it describes.
    if state in ("ready", "loading") and cf_up:
        url = tunnel_url()
        out["tunnel"] = url + "/v1" if url else ""
    return out


def too_old(llama, m):
    """True when llama.cpp is known to be older than family m needs,
    so kiln start would refuse it. An unreadable build is not."""
    build = int(llama.get("build") or 0)
    return bool(build and build < m.get("min_build", 0))


def first_on_disk(models):
    have = {m["id"] for m in models if m["quant"]}
    return next((f for f in START_ORDER if f in have), "")


def next_step(plat, llama, models, state, rec):
    """The board's "Next:" line: the first unmet dependency, so following
    it repeatedly walks an empty checkout to a served model."""
    if not llama["installed"]:
        return {"action": "setup", "label": "Install",
                "text": "Install llama.cpp"
                        + (" and cloudflared" if plat == "windows" else "")}
    if plat == "windows" and not llama["mtp"]:
        return {"action": "update", "label": "Upgrade",
                "text": "Upgrade llama.cpp for MTP speculative decoding"}
    # kiln.sh has no update verb, so elsewhere the model row says it.
    old = [m for m in models if m["quant"] and too_old(llama, m)]
    if plat == "windows" and old:
        return {"action": "update", "label": "Upgrade",
                "text": "Upgrade llama.cpp: %s needs b%d or newer"
                        % (old[0]["label"], old[0]["min_build"])}
    if not first_on_disk(models):
        return {"action": "get", "variant": rec["family"] if rec else "",
                "label": "Download",
                "text": "Download the weights this GPU is sized for"}
    if state == "stopped":
        return {"action": "start", "variant": first_on_disk(models),
                "label": "Start", "text": "Serve a model to Cursor"}
    return None


# ---------------------------------------------------------------
#  Actions -- the only path from a front end to a command line
# ---------------------------------------------------------------

def build_args(req):
    """(kiln arguments, extra environment) for a request. Raises
    ValueError for anything not in the table. Every value that reaches
    the command line is a literal from this function or a registry key.

    Start options:
      no_mtp  serve without the MTP draft head: a larger window for
              roughly half the generation speed
      local   no Cloudflare tunnel, so no public URL (the LAN can
              still reach the port, as always)
    """
    if not isinstance(req, dict):
        raise ValueError("expected a dict")
    action = req.get("action")
    variant = req.get("variant") or ""
    no_mtp = req.get("no_mtp") is True
    local = req.get("local") is True

    if variant and variant not in hardware.MODELS:
        raise ValueError("unknown variant")
    if (no_mtp or local) and action != "start":
        raise ValueError("no_mtp and local are start options")

    fixed = {
        "setup": ["setup"],
        "update": ["update"],
        "stop": ["stop"],
        "key-rotate": ["key", "rotate"],
        "opencode": ["opencode"],
        "pi": ["pi"],
        "self-update": ["self-update"],
        "self-update-check": ["self-update", "--check"],
    }
    if action in fixed:
        if variant:
            raise ValueError("%s takes no variant" % action)
        return list(fixed[action]), {}
    if action == "get":
        return (["get", variant] if variant else ["get"]), {}
    if action == "start":
        # Always named: with none, Windows opens an interactive picker.
        if not variant:
            raise ValueError("start needs a variant")
        args = ["start", variant] + (["--no-mtp"] if no_mtp else [])
        return args, ({"KILN_NO_TUNNEL": "1"} if local else {})
    raise ValueError("unknown action")


def allowed(action, plat):
    """kiln.sh has no update verb: llama.cpp is upgraded by re-running
    setup there."""
    return not (action == "update" and plat != "windows")


TITLES = {
    "setup": "Install llama.cpp",
    "update": "Upgrade llama.cpp",
    "get": "Download %s",
    "start": "Start %s",
    "stop": "Stop the server",
    "key-rotate": "Rotate the API key",
    "opencode": "Set up OpenCode",
    "pi": "Set up pi",
    "self-update": "Update kiln",
    "self-update-check": "Check for a kiln update",
}


def title_for(req):
    action = req["action"]
    variant = req.get("variant") or ""
    label = (hardware.MODELS[variant]["label"] if variant
             else "the recommended build")
    if req.get("no_mtp"):
        label += " without MTP"
    return TITLES[action] % label if "%s" in TITLES[action] else TITLES[action]


# ---------------------------------------------------------------
#  Jobs
# ---------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")

# Logs that could not be deleted when their job finished. On Windows a
# server the job started still holds the file open, so it is tried again
# when the next job finishes, and at shutdown.
_undeleted_logs = set()
_undeleted_lock = threading.Lock()


def remove_logs(paths=()):
    """Delete these job logs and any kept from before. Those that still
    cannot be deleted are kept for the next attempt."""
    with _undeleted_lock:
        pending = _undeleted_logs | set(paths)
        _undeleted_logs.clear()
        for path in pending:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                _undeleted_logs.add(path)


def sweep_logs():
    """Delete the job logs that earlier runs left behind. A log that a
    running server or job still holds open is skipped, as it should be."""
    remove_logs(glob.glob(os.path.join(glob.escape(tempfile.gettempdir()),
                                       "kiln-job-*.log")))


class Job:
    """One kiln verb, and everything it has printed so far."""

    MAX_TEXT = 512 * 1024

    def __init__(self, jid, action, title, args):
        self.id = jid
        self.action = action
        self.title = title
        self.args = args
        self.started = time.time()
        self.ended = None
        self.code = None
        self.cancelled = False
        self.proc = None
        self.log_path = ""
        self._text = ""
        self._base = 0      # characters dropped off the front
        self._carry = ""
        self._lock = threading.Lock()

    @property
    def running(self):
        return self.code is None

    def start(self, command, on_finish, env=None):
        fd, self.log_path = tempfile.mkstemp(prefix="kiln-job-", suffix=".log")
        full = dict(os.environ, NO_COLOR="1", TERM="dumb",
                    PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        full.update(env or {})
        own = ({"creationflags": CREATE_NO_WINDOW} if WINDOWS
               else {"start_new_session": True})
        self._append("$ %skiln %s\n\n" % (
            "".join("%s=%s " % kv for kv in sorted((env or {}).items())),
            " ".join(self.args)), final=True)
        with os.fdopen(fd, "wb") as out:
            try:
                self.proc = subprocess.Popen(
                    command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out,
                    stderr=subprocess.STDOUT, env=full, **own)
            except OSError as e:
                self._append("could not run kiln: %s\n" % e, final=True)
                self._finish(127, on_finish)
                return
        threading.Thread(target=self._pump, args=(on_finish,),
                         daemon=True).start()

    def _pump(self, on_finish):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        with open(self.log_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if chunk:
                    self._append(dec.decode(chunk))
                    continue
                code = self.proc.poll()
                if code is not None:
                    # Whatever landed between the last read and exit.
                    time.sleep(0.2)
                    self._append(dec.decode(fh.read(), final=True),
                                 final=True)
                    break
                time.sleep(0.25)
        self._finish(code, on_finish)

    def _finish(self, code, on_finish):
        # Removed before the exit code is published, so whoever sees the
        # job finish also sees its log gone.
        remove_logs([self.log_path])
        with self._lock:
            self.code = code
            self.ended = time.time()
        on_finish()

    def _append(self, text, final=False):
        text = self._carry + text
        self._carry = ""
        if not final:
            # Hold back what may be half of something split across two
            # reads: an escape sequence, or a CR whose LF is yet to come.
            cut = len(text)
            esc = text.rfind("\x1b")
            if esc != -1 and len(text) - esc < 32 and not ANSI.match(text,
                                                                     esc):
                cut = esc
            if text[:cut].endswith("\r"):
                cut -= 1
            text, self._carry = text[:cut], text[cut:]
        # Lone CRs stay: they are progress bars redrawing a line, and
        # front ends render them that way.
        text = CONTROL.sub("", ANSI.sub("", text.replace("\r\n", "\n")))
        if not text:
            return
        with self._lock:
            self._text += text
            over = len(self._text) - self.MAX_TEXT
            if over > 0:
                self._text = self._text[over:]
                self._base += over

    def cancel(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.cancelled = True
        if WINDOWS:
            # /T: cmd.exe is only the top of the tree.
            _run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)])
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def wait(self, timeout=None, poll=0.1):
        end = None if timeout is None else time.time() + timeout
        while self.running and (end is None or time.time() < end):
            time.sleep(poll)
        return self.code

    def summary(self):
        return {"id": self.id, "action": self.action, "title": self.title,
                "command": "kiln " + " ".join(self.args),
                "running": self.running, "code": self.code,
                "cancelled": self.cancelled, "started": self.started,
                "ended": self.ended}

    def view(self, since):
        with self._lock:
            out = self.summary()
            out["text"] = self._text[max(since - self._base, 0):]
            out["offset"] = self._base + len(self._text)
            out["truncated"] = since < self._base
        return out


class Busy(Exception):
    def __init__(self, job):
        Exception.__init__(self, job.title)
        self.job = job


# ---------------------------------------------------------------
#  The controller a front end holds
# ---------------------------------------------------------------

class Control:
    def __init__(self):
        self.plat = platform_id()
        self.port = server_port(self.plat)
        self.entry = os.path.join(ROOT, "kiln.bat" if WINDOWS else "kiln.sh")
        self.jobs = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._slow = None
        self._slow_at = 0.0
        self._slow_lock = threading.Lock()
        # Not on Unix: a removal never fails there, so nothing is left
        # behind, and a sweep could take a log another kiln tui is writing.
        if WINDOWS:
            sweep_logs()

    # ---- state ------------------------------------------------

    def invalidate(self):
        self._slow_at = 0.0

    def slow(self):
        with self._slow_lock:
            if self._slow is None or time.time() - self._slow_at > SLOW_TTL:
                self._slow = slow_facts(self.plat)
                self._slow_at = time.time()
            return self._slow

    def server(self):
        return server_facts(self.plat, self.port, opencode.read_key())

    def status(self):
        slow = self.slow()
        rec = slow["recommend"]
        models = model_rows(slow["machine"]["vram_mib"],
                            rec["family"] if rec else "")
        key = opencode.read_key()
        server = server_facts(self.plat, self.port, key)
        busy = self.blocking(server["state"])
        out = dict(slow)
        out.update({
            "platform": self.plat,
            "disk_free_gb": round(hardware.detect_free_disk_mib(ROOT) / 1024),
            "models": models,
            "shared": shared_files(),
            "server": server,
            "key": {"present": bool(key),
                    "masked": key[:6] + "\u2026" + key[-4:] if key else ""},
            "next": next_step(self.plat, slow["llama"], models,
                              server["state"], rec),
            "start_default": first_on_disk(models),
            "busy": busy.summary() if busy else None,
            "features": {"update": allowed("update", self.plat)},
        })
        return out

    def blocking(self, state=None):
        """The job that stops another from starting, or None. A start
        job stops counting once its server answers: on macOS and Linux
        the start script stays in the foreground for as long as the
        server runs, and that is not "busy"."""
        for job in reversed(list(self.jobs.values())):
            if not job.running:
                continue
            if job.action == "start":
                if state is None:
                    state = self.server()["state"]
                if state == "ready":
                    continue
            return job
        return None

    # ---- actions ----------------------------------------------

    def command(self, args):
        if WINDOWS:
            # /s strips exactly the outer pair of quotes and leaves the
            # inner ones around the path, which may hold spaces or
            # parentheses. args are literals from build_args.
            return 'cmd.exe /d /s /c ""%s" %s"' % (self.entry, " ".join(args))
        return ["bash", self.entry] + args

    def spawn(self, action, title, args, env=None):
        """Run kiln args now, without the busy check. For callers that
        already hold the machine, like the benchmark."""
        with self._lock:
            job = Job(str(self._next_id), action, title, args)
            self._next_id += 1
            self.jobs[job.id] = job
            for old in [j for j in self.jobs.values() if not j.running][
                    :-KEEP_JOBS]:
                del self.jobs[old.id]
        job.start(self.command(args), self.invalidate, env)
        return job

    def run(self, req):
        args, env = build_args(req)
        if not allowed(req["action"], self.plat):
            raise ValueError("%s is not available here" % req["action"])
        # Stop is always allowed: it is how a stuck start is undone.
        if req["action"] != "stop":
            busy = self.blocking()
            if busy:
                raise Busy(busy)
        return self.spawn(req["action"], title_for(req), args, env)

    def shutdown(self):
        """Cancel what nobody will be able to watch. The server stays.
        Returns the titles of what was cancelled."""
        gone = []
        for job in list(self.jobs.values()):
            if job.running and job.action != "start":
                job.cancel()
                gone.append(job.title)
        # Nothing reads a job's log once the front end has gone, and a
        # start job's log holds the key. On Unix a server still running
        # writes on into the deleted file; on Windows it holds the file
        # open, so the next kiln tui sweeps it up.
        remove_logs([job.log_path for job in self.jobs.values()
                     if job.log_path])
        return gone
