#!/usr/bin/env python3
"""
kiln cursor -- point Cursor at the model kiln serves, and keep it there.

Cursor sends a custom model's requests from its own servers, not from
your machine, so it can reach llama-server only through the public
tunnel `kiln start` opens -- and that address changes every time the
tunnel starts. This writes it into Cursor's "Override OpenAI Base URL",
turns on "Use OpenAI API Key", and lists the downloaded models in
Cursor's model picker. Run it once. After that `kiln start` points
Cursor at each new tunnel, and `kiln stop` turns the override off
again, so Cursor's own OpenAI models work while no tunnel is up.

WHAT IT CHANGES
---------------
Three things in Cursor's settings and nothing else: the base URL, that
toggle, and the model names. What the first two were is recorded in
.kiln/cursor.json in this checkout before anything changes, and
`kiln cursor --off` puts them back -- a base URL of your own, for
OpenRouter say, included -- and takes out the models it added.

CURSOR HAS TO BE CLOSED
-----------------------
Cursor keeps these settings in memory and writes them back when it
quits, so a change made while it is open is lost. `kiln cursor` waits
for you to quit it. From `kiln start`, `kiln stop` and `kiln tui`
nothing waits: the change is made in the background the moment Cursor
closes, so it is right the next time you open it.

THE KEY IS PASTED ONCE
----------------------
Cursor encrypts its OpenAI key with the desktop's credential store, so
kiln does not write it. It puts api_key.txt on the clipboard instead:
paste it into Settings > Models > OpenAI API Key once. kiln asks again
only after `kiln key rotate`.

WSL
---
From WSL this edits the Cursor installed on Windows, the one you run.
The tunnel is the same whichever side the server is on.

Usage:
    cursor.py                 point Cursor at the tunnel, and follow it
    cursor.py --url URL       use this address, a named tunnel of yours
    cursor.py --print         show what would change, change nothing
    cursor.py --status        what Cursor points at now
    cursor.py --off           put Cursor's own settings back, stop following
    cursor.py --no-wait       if Cursor is open, change it once it closes
    cursor.py --db PATH       edit this state.vscdb instead

    cursor.py --follow URL    what kiln start runs with each new tunnel
    cursor.py --unfollow      what kiln stop runs
Both do nothing until `kiln cursor` has been run in this checkout.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import opencode  # noqa: E402  (the key, the models on disk)

# Cursor's settings live in one JSON value of this row in state.vscdb.
KEY = ("src.vs.platform.reactivestorage.browser."
       "reactiveStorageServiceImpl.persistentStorage.applicationUser")
# The alias older kiln served the 27B as. It never answers now.
STALE_MODEL = "qwen3.8-27b-local"

STATE = os.path.join(ROOT, ".kiln")
RECORD = os.path.join(STATE, "cursor.json")
PENDING = os.path.join(STATE, "cursor-pending.json")
HEARTBEAT = os.path.join(STATE, "cursor-waiter.alive")
BACKUP = os.path.join(STATE, "cursor-settings.bak.json")

# How long `kiln cursor` waits for Cursor to close, and how long the
# background change waits before giving up.
WAIT_HERE = 20 * 60
WAIT_BEHIND = 12 * 3600
POLL = 1
# Two seconds behind the scenes: tasklist.exe runs on every check from WSL,
# and quitting and reopening Cursor takes longer than that.
POLL_BEHIND = 2


class Refused(Exception):
    """Changing Cursor's settings would not be safe or would not work."""


# ---------------------------------------------------------------
#  Where Cursor is
# ---------------------------------------------------------------

def _db_under(folder):
    return os.path.join(folder, "Cursor", "User", "globalStorage",
                        "state.vscdb")


def windows_appdata():
    """%APPDATA% on the Windows side, as a WSL path, or ""."""
    try:
        # From /mnt/c: cmd.exe warns about, and then ignores, a Linux
        # working directory.
        out = subprocess.run(["cmd.exe", "/d", "/c", "echo %APPDATA%"],
                             cwd="/mnt/c", capture_output=True, text=True,
                             timeout=15)
        win = out.stdout.strip()
        if out.returncode != 0 or not win or "%" in win:
            return ""
        out = subprocess.run(["wslpath", "-u", win], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def find_db():
    """Cursor's state.vscdb, or "" when Cursor has never run here."""
    env = opencode.environment()
    home = os.path.expanduser("~")
    if env == "windows":
        folders = [os.environ.get("APPDATA")
                   or os.path.join(home, "AppData", "Roaming")]
    elif env == "mac":
        folders = [os.path.join(home, "Library", "Application Support")]
    else:
        folders = []
        if env == "wsl":
            # The Cursor you run from WSL is the Windows one: it opens
            # WSL folders through its remote extension.
            appdata = windows_appdata()
            if appdata:
                folders.append(appdata)
        folders.append(os.environ.get("XDG_CONFIG_HOME")
                       or os.path.join(home, ".config"))
    for folder in folders:
        if os.path.isfile(_db_under(folder)):
            return _db_under(folder)
    return ""


def local_path(db):
    """db as this side of a WSL machine names it. A checkout shared by
    Windows and WSL keeps one record, written by whichever side ran
    `kiln cursor`, so a path in it may be in the other side's form."""
    if os.name == "nt":
        m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", db)
        if m:
            return "%s:\\%s" % (m.group(1).upper(),
                                  m.group(2).replace("/", "\\"))
    elif opencode.environment() == "wsl":
        m = re.match(r"^([a-zA-Z]):[\\/](.*)$", db)
        if m:
            return "/mnt/%s/%s" % (m.group(1).lower(),
                                   m.group(2).replace("\\", "/"))
    return db


def on_windows(db):
    """True when the Cursor that owns db is the Windows program, which
    from WSL is any database under /mnt/<drive>/."""
    return os.name == "nt" or db.startswith("/mnt/")


def _output(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out


def _linux_cursor_running():
    proc = "/proc"
    if not os.path.isdir(proc):
        return False
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        # cursor-agent is a different program and does not hold the file.
        if not cmd or "cursor-agent" in cmd or "cursor.py" in cmd:
            continue
        name = os.path.basename(cmd.split(" ", 1)[0])
        if name in ("cursor", "Cursor", "cursor.AppImage"):
            return True
    return False


def cursor_running(db):
    """True while the Cursor app that owns db is open. When that cannot
    be told, False: the cost is a change Cursor may undo, not a wait
    that never ends."""
    if on_windows(db):
        out = _output(["tasklist" if os.name == "nt" else "tasklist.exe",
                       "/FI", "IMAGENAME eq Cursor.exe", "/NH", "/FO", "CSV"])
        return bool(out and '"cursor.exe"' in out.stdout.lower())
    if sys.platform == "darwin":
        out = _output(["pgrep", "-x", "Cursor"])
        return bool(out and out.returncode == 0)
    return _linux_cursor_running()


# ---------------------------------------------------------------
#  Cursor's settings
# ---------------------------------------------------------------

def _open(db):
    if not os.path.isfile(db):
        raise Refused("Cursor's settings were not found at %s." % db)
    con = sqlite3.connect(db, timeout=15)
    con.isolation_level = None
    return con


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def read_settings(db):
    con = _open(db)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key=?",
                          (KEY,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise Refused("Cursor has no settings yet. Open it once, sign in, "
                      "and quit it.")
    return json.loads(_text(row[0]))


def edit(db, fn):
    """Run fn(settings) inside one transaction and write the result back,
    unless fn changed nothing. Returns what fn returns."""
    con = _open(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM ItemTable WHERE key=?",
                          (KEY,)).fetchone()
        if row is None:
            raise Refused("Cursor has no settings yet. Open it once, sign "
                          "in, and quit it.")
        raw = _text(row[0])
        data = json.loads(raw)
        keep_backup(raw)
        result = fn(data)
        if data != json.loads(raw):
            con.execute("UPDATE ItemTable SET value=? WHERE key=?",
                        (json.dumps(data, ensure_ascii=False,
                                    separators=(",", ":")), KEY))
        con.execute("COMMIT")
        return result
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def keep_backup(raw):
    """The settings as they were before kiln first touched them, once."""
    if not os.path.exists(BACKUP):
        _write_private(BACKUP, raw)


def host_of(url):
    return urlparse(url or "").hostname or ""


def normalize(url):
    """https://HOST[/v1] as https://HOST/v1, or Refused. Cursor reaches
    the model from the internet, so only a public https address works."""
    text = (url or "").strip().rstrip("/")
    if text.endswith("/v1"):
        text = text[:-3]
    p = urlparse(text)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.path not in ("", "/")):
        raise Refused("%r is not an https://HOST address. Cursor calls the "
                      "model from its own servers, so it needs the public "
                      "tunnel address, not localhost." % url)
    return "https://%s/v1" % p.netloc


# ---------------------------------------------------------------
#  What kiln did, so it can be undone
# ---------------------------------------------------------------

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def load_record():
    """.kiln/cursor.json: which Cursor, the settings it had before kiln,
    the models kiln added, the address kiln set. None until `kiln
    cursor` has been run here, which is also what turns following on."""
    return load_json(RECORD)


def save_record(rec):
    _write_private(RECORD, json.dumps(rec, indent=2) + "\n")


def forget():
    for path in (RECORD, PENDING):
        try:
            os.remove(path)
        except OSError:
            pass


def kilns(data, rec):
    """True when Cursor's base URL is one kiln put there."""
    base = data.get("openAIBaseUrl") or ""
    return bool(base) and (base == rec.get("url")
                           or host_of(base).endswith(".trycloudflare.com"))


def connect(data, rec, url, models):
    """Point the settings at url and list models. Records what they were
    first, unless they already point at kiln: a second tunnel must not
    replace the record of your own settings with kiln's."""
    ai = data.setdefault("aiSettings", {})
    if not isinstance(ai, dict):
        raise Refused("Cursor's aiSettings is not an object.")
    if not kilns(data, rec):
        rec["prior"] = {"useOpenAIKey": bool(data.get("useOpenAIKey")),
                        "openAIBaseUrl": data.get("openAIBaseUrl") or ""}
    data["openAIBaseUrl"] = url
    data["useOpenAIKey"] = True

    added = set(rec.get("added") or [])
    listed = [m for m in (ai.get("userAddedModels") or [])
              if m != STALE_MODEL]
    enabled = [m for m in (ai.get("modelOverrideEnabled") or [])
               if m != STALE_MODEL]
    for m in models:
        if m not in listed:
            listed.append(m)
            added.add(m)
        if m not in enabled:
            enabled.append(m)
    ai["userAddedModels"] = listed
    ai["modelOverrideEnabled"] = enabled
    ai["modelOverrideDisabled"] = [
        m for m in (ai.get("modelOverrideDisabled") or [])
        if m not in models and m != STALE_MODEL]
    rec["added"] = sorted(added)
    rec["url"] = url


def disconnect(data, rec, drop_models=False):
    """Give back the base URL and toggle Cursor had before kiln, if it
    still points at kiln; a URL you have set since is left alone."""
    if kilns(data, rec):
        prior = rec.get("prior") or {}
        data["useOpenAIKey"] = bool(prior.get("useOpenAIKey"))
        data["openAIBaseUrl"] = prior.get("openAIBaseUrl") or ""
    ai = data.get("aiSettings")
    if drop_models and isinstance(ai, dict):
        ours = set(rec.get("added") or [])
        for field in ("userAddedModels", "modelOverrideEnabled"):
            if isinstance(ai.get(field), list):
                ai[field] = [m for m in ai[field] if m not in ours]
    rec["url"] = ""


def perform(change):
    """Make a change now. The record is saved inside the transaction,
    before the commit, so a failed save leaves Cursor untouched."""
    rec = load_record() or {}
    db = rec["db"] = local_path(change["db"])

    def fn(data):
        if change["op"] == "connect":
            connect(data, rec, change["url"], change["models"])
        else:
            disconnect(data, rec, change.get("off", False))
        if not change.get("off"):
            save_record(rec)

    edit(db, fn)
    if change.get("off"):
        forget()


# ---------------------------------------------------------------
#  When Cursor is open
# ---------------------------------------------------------------

def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        pass
    os.utime(path, None)


def waiter_alive():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT) < 5 * POLL_BEHIND + 5
    except OSError:
        return False


def later(change):
    """Make change once Cursor closes, from a process of its own, so the
    caller -- a start script, kiln tui -- does not wait. The newest
    change wins: a start then a stop while Cursor stays open ends with
    the stop."""
    _write_private(PENDING, json.dumps(change) + "\n")
    if waiter_alive():
        return
    _touch(HEARTBEAT)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": ROOT}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console, and it
        # outlives the window kiln start runs in.
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__),
                      "--apply-pending"], **kw)


def apply_pending():
    """The background half of later(): wait for Cursor to close, then make
    the newest pending change."""
    end = time.time() + WAIT_BEHIND
    try:
        while time.time() < end:
            _touch(HEARTBEAT)
            change = load_json(PENDING)
            if change is None:
                return 0
            if not cursor_running(local_path(change["db"])):
                try:
                    perform(change)
                except (Refused, sqlite3.Error, OSError, ValueError):
                    pass
                # A newer change may have arrived meanwhile; it goes on
                # the next pass.
                if load_json(PENDING) == change:
                    try:
                        os.remove(PENDING)
                    except OSError:
                        pass
                continue
            time.sleep(POLL_BEHIND)
    finally:
        try:
            os.remove(HEARTBEAT)
        except OSError:
            pass
    return 0


def wait_here(db):
    """Wait for Cursor to close. False if it did not, or on Ctrl+C."""
    if not cursor_running(db):
        return True
    print(" Cursor is open, and it writes these settings back when it")
    print(" quits, which would undo this. Quit Cursor and this finishes")
    print(" on its own. (Ctrl+C leaves it to happen when Cursor closes.)")
    end = time.time() + WAIT_HERE
    try:
        while time.time() < end:
            time.sleep(POLL)
            if not cursor_running(db):
                return True
    except KeyboardInterrupt:
        print()
    return False


# ---------------------------------------------------------------
#  The key, and the clipboard
# ---------------------------------------------------------------

def key_tag(key):
    """Enough of a hash to tell that the key changed. Not the key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def to_clipboard(text):
    if os.name == "nt":
        tools = [["clip"]]
    elif sys.platform == "darwin":
        tools = [["pbcopy"]]
    elif opencode.environment() == "wsl":
        tools = [["clip.exe"]]
    else:
        tools = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]]
    for argv in tools:
        if not shutil.which(argv[0]):
            continue
        try:
            out = subprocess.run(argv, input=text.encode("utf-8"),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return True
    return False


def key_step(rec, always):
    """Ask for the key to be pasted when Cursor has not been given this one
    yet. Returns the lines to print; records the key as given."""
    key = opencode.read_key()
    if not key:
        return []
    tag = key_tag(key)
    if rec.get("key") == tag:
        if not always:
            return []
        return ["The API key is the one Cursor already has. If Cursor says",
                "it is wrong, `kiln key show` prints it."]
    rec["key"] = tag
    where = ("It is on the clipboard." if to_clipboard(key)
             else "`kiln key show` prints it.")
    return ["One step kiln cannot do for you: paste the API key into",
            "Cursor > Settings > Models > OpenAI API Key. %s" % where,
            "You only do this again after `kiln key rotate`."]


# ---------------------------------------------------------------
#  What to point Cursor at
# ---------------------------------------------------------------

def live_server():
    """(the tunnel's URL ending /v1, or "" with no tunnel up; and what
    control.server_facts says about the server)."""
    import control  # noqa: E402  (imported late: it probes the machine)
    plat = control.platform_id()
    facts = control.server_facts(plat, control.server_port(plat),
                                 opencode.read_key())
    return facts.get("tunnel", ""), facts


def model_names(facts=None):
    live = None
    if facts and facts.get("alias"):
        live = {"alias": facts["alias"], "ctx": facts.get("ctx", 0),
                "vision": facts.get("vision", False)}
    return [m["alias"] for m in opencode.local_models(live, [])]


# ---------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------

def say(lines):
    for line in lines:
        print(" " + line if line else "")


def cmd_connect(db, url, dry, wait):
    tunnel, facts = live_server() if not url else ("", None)
    url = normalize(url) if url else tunnel
    models = model_names(facts)
    rec = load_record() or {}
    if rec.get("db") and local_path(rec["db"]) != db:
        rec = {}

    print()
    print(" Cursor")
    print(" ------------------------------------------------------------")
    print("  %-10s %s" % ("settings", db))
    print("  %-10s %s" % ("base URL", url or "(none yet: no tunnel is up)"))
    for m in models:
        print("  %-10s %s" % ("model", m))
    print(" ------------------------------------------------------------")
    if dry:
        print(" Nothing was changed (--print).")
        print()
        return 0

    rec["db"] = db
    if not url:
        # Following needs no tunnel today: the next kiln start fills it in.
        lines = key_step(rec, True)
        save_record(rec)
        say(["No tunnel is up, and Cursor can reach the model only through",
             "one. kiln will point Cursor at it when you next run",
             "`kiln start`."] + ([""] + lines if lines else []))
        print()
        return 0

    change = {"op": "connect", "db": db, "url": url, "models": models}
    lines = key_step(rec, True)
    save_record(rec)
    if cursor_running(db) and not (wait and wait_here(db)):
        later(change)
        say(["Cursor is open, so this is made the moment it closes. Open",
             "it again after that and the models are in its picker."])
    else:
        perform(change)
        say(["Done. Open Cursor and pick %s in its model menu."
             % " or ".join(models)])
    if lines:
        print()
        say(lines)
    print()
    say(["From now on kiln start points Cursor at each new tunnel, and",
         "kiln stop turns the override off. `kiln cursor --off` undoes",
         "all of it."])
    print()
    return 0


def cmd_follow(url):
    rec = load_record()
    if not rec or not rec.get("db"):
        return 0
    url = normalize(url)
    db = local_path(rec["db"])
    change = {"op": "connect", "db": db, "url": url,
              "models": model_names()}
    lines = key_step(rec, False)
    if lines:
        save_record(rec)
    if cursor_running(db):
        later(change)
        say(["Cursor is open: it will use this tunnel once you quit and",
             "reopen it."])
    else:
        perform(change)
        say(["Cursor now points at this tunnel."])
    say(lines)
    return 0


def cmd_unfollow():
    rec = load_record()
    if not rec or not rec.get("db"):
        return 0
    db = local_path(rec["db"])
    change = {"op": "disconnect", "db": db}
    if cursor_running(db):
        later(change)
    else:
        perform(change)
    return 0


def cmd_off(db, wait):
    rec = load_record()
    if not rec:
        print(" kiln has not changed Cursor's settings here.")
        return 0
    db = local_path(rec.get("db") or db)
    change = {"op": "disconnect", "db": db, "off": True}
    if cursor_running(db) and not (wait and wait_here(db)):
        later(change)
        print(" Cursor is open, so its settings go back the moment it "
              "closes.")
    else:
        perform(change)
        print(" Cursor has its own settings back, and kiln no longer "
              "follows the tunnel.")
    return 0


def cmd_status(db):
    data = read_settings(db)
    rec = load_record() or {}
    ours = set(rec.get("added") or [])
    listed = [m for m in ((data.get("aiSettings") or {})
                          .get("userAddedModels") or []) if m in ours]
    pending = load_json(PENDING)
    print()
    print(" Cursor")
    print(" ------------------------------------------------------------")
    print("  %-10s %s" % ("settings", db))
    print("  %-10s %s" % ("open", "yes" if cursor_running(db) else "no"))
    print("  %-10s %s" % ("override", "on" if data.get("useOpenAIKey")
                          else "off"))
    print("  %-10s %s" % ("base URL", host_of(data.get("openAIBaseUrl"))
                          or "-"))
    print("  %-10s %s" % ("kiln's", "yes" if kilns(data, rec) else "no"))
    print("  %-10s %s" % ("models", ", ".join(listed) or "-"))
    print("  %-10s %s" % ("following", "yes" if rec.get("db") else "no"))
    if pending:
        print("  %-10s %s" % ("waiting", "to %s once Cursor closes"
                              % pending["op"]))
    print(" ------------------------------------------------------------")
    print()
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        prog="kiln cursor", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="https address to use, ending /v1 or not")
    p.add_argument("--db", help="Cursor state.vscdb to edit")
    p.add_argument("--no-wait", action="store_true",
                   help="if Cursor is open, change it once it closes")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="dry", action="store_true",
                      help="show what would change, change nothing")
    mode.add_argument("--status", action="store_true",
                      help="what Cursor points at now")
    mode.add_argument("--off", action="store_true",
                      help="put Cursor's own settings back")
    mode.add_argument("--follow", metavar="URL", help=argparse.SUPPRESS)
    mode.add_argument("--unfollow", action="store_true",
                      help=argparse.SUPPRESS)
    mode.add_argument("--apply-pending", action="store_true",
                      help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    try:
        # Called by kiln start and kiln stop: never fatal to them.
        if args.follow:
            return cmd_follow(args.follow)
        if args.unfollow:
            return cmd_unfollow()
        if args.apply_pending:
            return apply_pending()

        db = args.db or find_db()
        if not db:
            print("kiln: Cursor was not found. Install it, open it once,"
                  " and run this again.", file=sys.stderr)
            return 1
        if args.status:
            return cmd_status(db)
        if args.off:
            return cmd_off(db, not args.no_wait)
        return cmd_connect(db, args.url, args.dry, not args.no_wait)
    except (Refused, sqlite3.Error, ValueError, OSError) as e:
        print("kiln: %s" % e, file=sys.stderr)
        print("kiln: Cursor's settings were not changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
