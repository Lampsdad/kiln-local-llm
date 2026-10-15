#!/usr/bin/env python3
"""
Point pi (the coding agent at pi.dev) at the local llama-server, so the
Qwen builds kiln serves show up in its /model list as "kiln/<alias>".

Writes one provider, "kiln", into pi's models.json and leaves every
other key in that file alone. Re-running replaces the kiln entry and
nothing else, so it is safe to run after every download.

WHERE PI LOOKS
--------------
$PI_CODING_AGENT_DIR/models.json, else ~/.pi/agent/models.json
(C:\\Users\\<you>\\.pi\\agent on Windows). pi reads it again every time
/model opens, so a running pi picks the change up without a restart.

pi rejects the whole file when any of it breaks its schema, which would
take every other custom provider down with it. Everything written here
is checked against that schema by the tests.

WHY NOT PI'S OWN llama.cpp PROVIDER
-----------------------------------
pi ships one, but it drives llama-server's router mode, which runs
without -m, and lists only models the router reports a load status
for. kiln serves one model with its MTP draft head and vision projector,
which is single-model mode, so that provider would list nothing. The
compat settings below are the ones it sends to llama-server.

WSL IS TWO MACHINES
-------------------
pi inside WSL and pi on Windows have separate agent directories, so run
this from each side you use. The URL is worked out exactly as for
OpenCode; see opencode.py.

THE API KEY IS REFERENCED, NOT COPIED
-------------------------------------
apiKey is "!cat '<repo>/api_key.txt'". pi runs that command on the first
request and keeps the answer until it exits, so `kiln key rotate` takes
effect on pi's next launch with no re-run. On Windows pi runs it through
Git Bash, which it needs for its own bash tool anyway, and the path is
written with forward slashes, which Git Bash accepts. If the file goes
missing, only the kiln models fail; the rest of pi keeps working.

Usage:
    pi_agent.py                   write the kiln provider
    pi_agent.py --url URL         use this base URL (ending /v1) as-is
    pi_agent.py --print           show the provider block, write nothing
    pi_agent.py --remove          take the kiln provider back out
    pi_agent.py --config PATH     edit this file instead

Exit status is 0 on success and 1 when nothing was written because it
would not have been safe to.
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Importing opencode.py would otherwise leave __pycache__ in the checkout.
sys.dont_write_bytecode = True
import opencode  # noqa: E402  (the probe, the URL and the sizing)

PROVIDER_ID = "kiln"
SCHEMA = "https://pi.dev/schemas/models.schema.json"
PACKAGE = "@earendil-works/pi-coding-agent"

Refused = opencode.Refused

# What pi's own llama.cpp provider sends to llama-server, which is
# OpenAI-compatible but not OpenAI: no developer role, no store field,
# no reasoning_effort, no strict tool schemas.
COMPAT = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "supportsUsageInStreaming": True,
    "supportsStrictMode": False,
    "maxTokensField": "max_tokens",
    # chat_template_kwargs.enable_thinking, which Qwen's template reads.
    "thinkingFormat": "qwen-chat-template",
}

# Qwen's template has thinking on or off and nothing between, so pi
# offers just those two levels. null marks a level as unsupported.
THINKING_LEVELS = {"off": "off", "minimal": None, "low": None,
                   "medium": "medium", "high": None, "xhigh": None,
                   "max": None}


# ---------------------------------------------------------------
#  What to write
# ---------------------------------------------------------------

def key_command():
    """The !command pi runs to read api_key.txt.

    Single quotes keep spaces and $ literal in sh and Git Bash alike,
    so the only path that cannot be quoted is one containing a quote.
    """
    path = os.path.abspath(opencode.KEY_FILE).replace("\\", "/")
    if "'" in path or not path.isprintable():
        raise Refused("pi cannot be pointed at %s: the path contains a"
                      " character the key command cannot quote. Move the"
                      " repo to a plainer path." % path)
    return "!cat '%s'" % path


def model_entry(m):
    return {
        "id": m["alias"],
        "name": m["name"],
        "reasoning": True,
        "thinkingLevelMap": dict(THINKING_LEVELS),
        "input": ["text", "image"] if m["vision"] else ["text"],
        "contextWindow": m["ctx"],
        "maxTokens": opencode.output_budget(m["ctx"]),
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


def provider_block(base_url, models):
    return {
        "name": "kiln (local llama.cpp)",
        "baseUrl": base_url,
        "api": "openai-completions",
        "apiKey": key_command(),
        "compat": dict(COMPAT),
        "models": [model_entry(m) for m in models],
    }


# ---------------------------------------------------------------
#  The config file
# ---------------------------------------------------------------

def agent_dir():
    d = os.environ.get("PI_CODING_AGENT_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    return os.path.join(os.path.expanduser("~"), ".pi", "agent")


def config_path(override):
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(agent_dir(), "models.json")


def load_config(path):
    """(config dict, raw text or None when the file does not exist).

    pi strips comments before parsing, so a file may hold them; they do
    not survive the rewrite, which is what the .kiln.bak is for.
    """
    fresh = {"$schema": SCHEMA, "providers": {}}
    if not os.path.isfile(path):
        if os.path.exists(path):
            raise Refused("%s exists but is not a file" % path)
        return fresh, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = fh.read()
    except (OSError, ValueError) as e:
        raise Refused("cannot read %s (%s)" % (path, e)) from None
    if not raw.strip():
        return fresh, raw
    try:
        doc = json.loads(opencode.strip_jsonc(raw))
    except ValueError as e:
        raise Refused("cannot parse %s (%s). Fix it, or point --config at"
                      " another file." % (path, e)) from None
    if not isinstance(doc, dict):
        raise Refused("%s does not hold a JSON object" % path)
    providers = doc.get("providers")
    if providers is not None and not isinstance(providers, dict):
        raise Refused("\"providers\" in %s is not an object; kiln will not"
                      " overwrite it" % path)
    return doc, raw


def default_is_kiln(path):
    """pi keeps its startup model in settings.json, beside models.json."""
    settings = os.path.join(os.path.dirname(path), "settings.json")
    try:
        with open(settings, encoding="utf-8-sig") as fh:
            doc = json.loads(opencode.strip_jsonc(fh.read()))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("defaultProvider") == PROVIDER_ID


# ---------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------

def cmd_remove(path):
    doc, raw = load_config(path)
    providers = doc.get("providers")
    if raw is None or not isinstance(providers, dict) \
            or PROVIDER_ID not in providers:
        print("\n pi has no kiln provider in %s\n" % path)
        return 0
    # "providers" stays, even empty: pi requires it.
    del providers[PROVIDER_ID]
    opencode.save_config(path, doc, raw)
    print("\n Removed the kiln provider from %s" % path)
    if default_is_kiln(path):
        print(" pi's settings.json still starts on a kiln model. Pick another"
              " with /model and press Ctrl+S to make it the default.")
    print()
    return 0


def cmd_write(path, url, dry):
    env = opencode.environment()
    notes = []

    key = opencode.read_key()
    if not key and not dry:
        raise Refused("%s is missing or empty, so llama-server would turn"
                      " away every request pi sends. Run `kiln key show` to"
                      " create it." % opencode.KEY_FILE)

    if url:
        url = opencode.check_url(url)
        live = opencode.probe(url, key)
        if not url.endswith("/v1"):
            notes.append("%s does not end in /v1; llama-server serves the"
                         " OpenAI API under /v1." % url)
    else:
        url, live = opencode.choose_url(env, key, notes, client="pi")
    if live and not live["auth"]:
        notes.append("llama-server at %s rejected api_key.txt. Restart it"
                     " so it loads the current key." % url)

    block = provider_block(url, opencode.local_models(live, notes))
    if dry:
        print(json.dumps({"providers": {PROVIDER_ID: block}}, indent=2))
        return 0

    doc, raw = load_config(path)
    doc.setdefault("providers", {})[PROVIDER_ID] = block
    backup = opencode.save_config(path, doc, raw)
    if not shutil.which("pi"):
        notes.append("pi is not on PATH here. Install it with `npm install"
                     " -g --ignore-scripts %s` (Node 22.19 or newer), or see"
                     " https://pi.dev." % PACKAGE)
    report(env, path, url, live, block, backup, notes)
    return 0


def report(env, path, url, live, block, backup, notes):
    if live and live["auth"]:
        state = "running, serving %s" % live["alias"]
    elif live:
        state = "running, but rejected the key"
    else:
        state = "not running"

    print()
    print(" pi (%s)" % env)
    print(" ------------------------------------------------------------")
    print("  %-10s %s" % ("config", path))
    print("  %-10s %s" % ("server", url))
    print("  %-10s %s" % ("status", state))
    for m in block["models"]:
        extra = "  vision" if "image" in m["input"] else ""
        print("  %-10s %s/%s  %dK ctx%s" % (
            "model", PROVIDER_ID, m["id"], m["contextWindow"] // 1024, extra))
    if backup:
        print("  %-10s %s" % ("backup", backup))
    print(" ------------------------------------------------------------")
    for n in notes:
        print(" note: " + n)
    if notes:
        print()
    print(" pi reads this whenever /model opens, so a running pi needs no")
    print(" restart. Pick a model there, or start pi on one:")
    print("   pi --model %s/%s" % (PROVIDER_ID, block["models"][0]["id"]))
    print(" Only the model `kiln start` is serving answers; the others")
    print(" are listed so switching builds needs no re-run.")
    print()


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        prog="kiln pi", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="base URL ending in /v1, used as-is")
    p.add_argument("--config", help="pi models.json to edit")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="dry", action="store_true",
                      help="print the provider block and write nothing")
    mode.add_argument("--remove", action="store_true",
                      help="remove the kiln provider")
    args = p.parse_args(argv)

    path = config_path(args.config)
    try:
        if args.remove:
            return cmd_remove(path)
        return cmd_write(path, args.url, args.dry)
    except Refused as e:
        print("kiln: %s" % e, file=sys.stderr)
        print("kiln: pi's models.json was not changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
