#!/usr/bin/env python3
"""Point Cursor at a kiln quick tunnel, or turn that workaround off.

Cursor stores "Use OpenAI API Key" and "Override OpenAI Base URL" in one
JSON blob inside state.vscdb. While the toggle is on, OpenAI-family
requests (Composer and Grok included) go to that URL, so the toggle has
to come back off when the tunnel stops.

    cursor_sync.py up --url https://HOST.trycloudflare.com
    cursor_sync.py down
    cursor_sync.py status

up records the OpenAI base URL and toggle Cursor had before it. down puts
them back, but only while Cursor still points at a kiln tunnel.

The OpenAI key Cursor already saved stays where it is. It is encrypted
with the desktop's safeStorage and this script does not try to replace it.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

KEY = (
    "src.vs.platform.reactivestorage.browser."
    "reactiveStorageServiceImpl.persistentStorage.applicationUser"
)
DEFAULT_MODEL = "qwen3.8-27b-abliterated"
STALE_MODEL = "qwen3.8-27b-local"


def default_db():
    return Path.home() / ".config/Cursor/User/globalStorage/state.vscdb"


def backup_path():
    return Path.home() / ".config/kiln/cursor-applicationUser.bak.json"


def cursor_app_running():
    """True when the Cursor desktop app is open.

    It keeps this JSON in memory and writes it back on the way out, so a
    change made underneath it would be lost. cursor-agent is a different
    program and does not hold this file.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if "cursor-agent" in cmd or "cursor_sync.py" in cmd:
            continue
        name = os.path.basename(cmd.split(" ", 1)[0])
        if name in ("cursor", "Cursor", "cursor.AppImage"):
            return True
    return False


def normalize_base(url):
    text = url.strip().rstrip("/")
    if text.endswith("/v1"):
        text = text[: -len("/v1")]
    parsed = urlparse(text)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host.endswith(".trycloudflare.com")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise SystemExit(
            "kiln: refusing URL %r (want https://HOST.trycloudflare.com)" % url
        )
    return "https://%s/v1" % host


def _swap(items, model):
    out = []
    for item in items:
        name = model if item == STALE_MODEL else item
        if name not in out:
            out.append(name)
    if model not in out:
        out.append(model)
    return out


def apply_up(data, url, model):
    base = normalize_base(url)
    data["openAIBaseUrl"] = base
    data["useOpenAIKey"] = True
    ai = data.setdefault("aiSettings", {})
    if not isinstance(ai, dict):
        raise SystemExit("kiln: Cursor aiSettings is not an object")
    ai["userAddedModels"] = _swap(list(ai.get("userAddedModels") or []), model)
    ai["modelOverrideEnabled"] = _swap(
        list(ai.get("modelOverrideEnabled") or []), model
    )
    disabled = [
        item
        for item in (ai.get("modelOverrideDisabled") or [])
        if item not in (model, STALE_MODEL)
    ]
    ai["modelOverrideDisabled"] = disabled
    return base


def connect(db):
    if not Path(db).is_file():
        raise SystemExit("kiln: Cursor state database not found: %s" % db)
    con = sqlite3.connect(str(db), timeout=15)
    con.isolation_level = None
    return con


def load(con):
    row = con.execute("SELECT value FROM ItemTable WHERE key=?", (KEY,)).fetchone()
    if row is None:
        raise SystemExit("kiln: Cursor has no applicationUser settings row")
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def save(con, data):
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    con.execute("UPDATE ItemTable SET value=? WHERE key=?", (blob, KEY))


def maybe_backup(db, raw_text):
    if Path(db).resolve() != default_db().resolve():
        return
    dest = backup_path()
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(raw_text)
    os.chmod(dest, 0o600)


def host_of(url):
    return urlparse(url or "").hostname or ""


def points_at_kiln(data):
    return host_of(data.get("openAIBaseUrl")).endswith(".trycloudflare.com")


def prior_path(db):
    """Where up keeps the user's own OpenAI settings until down restores them.

    The real database gets a file beside the backup. Any other database
    gets one beside itself, so tests never touch the real config folder.
    """
    if Path(db).resolve() == default_db().resolve():
        return backup_path().with_name("cursor-openai-prior.json")
    return Path(str(db) + ".kiln-prior.json")


def save_prior(db, values):
    path = prior_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))
    os.chmod(path, 0o600)


def load_prior(db):
    """The values up recorded, or {} when there is no usable record."""
    try:
        values = json.loads(prior_path(db).read_text())
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def forget_prior(db):
    try:
        prior_path(db).unlink()
    except FileNotFoundError:
        pass


def cmd_status(db):
    con = connect(db)
    try:
        data = load(con)
    finally:
        con.close()
    flag = bool(data.get("useOpenAIKey"))
    host = host_of(data.get("openAIBaseUrl") or "")
    print("useOpenAIKey %s" % ("true" if flag else "false"))
    print("baseHost %s" % (host or "-"))
    return 0


def mutate(db, fn):
    if Path(db).resolve() == default_db().resolve() and cursor_app_running():
        raise SystemExit(
            "kiln: Cursor is open, so its settings file would be overwritten. "
            "Close Cursor and run this again."
        )
    con = connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key=?", (KEY,)
        ).fetchone()
        if row is None:
            con.execute("ROLLBACK")
            raise SystemExit("kiln: Cursor has no applicationUser settings row")
        raw = row[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        maybe_backup(db, raw)
        result = fn(data)
        # A no-op is not written back, so Cursor's own JSON stays as it was.
        if data != json.loads(raw):
            save(con, data)
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


def cmd_up(db, url, model):
    def change(data):
        # Only the first up records the user's settings. A later up sees
        # kiln's tunnel URL, and recording that would lose the originals.
        was_kiln = points_at_kiln(data)
        mine = {
            "useOpenAIKey": data.get("useOpenAIKey", False),
            "openAIBaseUrl": data.get("openAIBaseUrl", ""),
        }
        base = apply_up(data, url, model)
        if not was_kiln:
            # Written before the commit, so a failed write leaves Cursor alone.
            save_prior(db, mine)
        return base

    return mutate(db, change)


def cmd_down(db):
    prior = load_prior(db)

    def change(data):
        if not points_at_kiln(data):
            return False
        data["useOpenAIKey"] = prior.get("useOpenAIKey", False)
        data["openAIBaseUrl"] = prior.get("openAIBaseUrl", "")
        return True

    if mutate(db, change):
        forget_prior(db)
    else:
        print("kiln: Cursor is not pointed at a kiln tunnel, "
              "so its settings were left alone.")
    return cmd_status(db)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("CURSOR_STATE_DB") or str(default_db()),
        help="state.vscdb to edit (tests pass a copy)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--url", required=True)
    up.add_argument("--model", default=DEFAULT_MODEL)
    sub.add_parser("down")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args.db)
    if args.cmd == "up":
        base = cmd_up(args.db, args.url, args.model)
        print("useOpenAIKey true")
        print("baseHost %s" % host_of(base))
        return 0
    return cmd_down(args.db)


if __name__ == "__main__":
    sys.exit(main())
