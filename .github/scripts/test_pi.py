#!/usr/bin/env python3
"""Tests for scripts/pi_agent.py, the `kiln pi` backend.

pi rejects all of models.json when any part breaks its schema, so a bad
kiln block would take the user's other providers down too. Everything
written here is validated against pi-models.schema.json, which is
schemas/models.schema.json from @earendil-works/pi-coding-agent 1.2.0
(MIT), and more strictly than pi does: a key the schema does not know
is an error, which catches a misspelt field pi would silently ignore.

The rest pins the same refusals as test_opencode.py: never overwrite a
file it cannot parse, never point at a key file that does not exist.

Hermetic: every path it touches is under a temporary directory, the GPU
probe is stubbed, and the port it probes is one nothing listens on.
"""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import opencode  # noqa: E402
import pi_agent  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"

with open(os.path.join(HERE, "pi-models.schema.json"), encoding="utf-8") as _fh:
    SCHEMA = json.load(_fh)


# ---------------------------------------------------------------
#  A JSON Schema checker for the keywords pi's schema uses
# ---------------------------------------------------------------

TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def violations(schema, value, path="$"):
    """Every way value breaks schema, as messages. Stricter than pi in
    one respect: an object key matching no property is a violation."""
    if "$ref" in schema:
        return violations(SCHEMA["$defs"][schema["$ref"].split("/")[-1]],
                          value, path)
    if "anyOf" in schema:
        if all(violations(s, value, path) for s in schema["anyOf"]):
            return ["%s matches no allowed form: %r" % (path, value)]
        return []
    t = schema.get("type")
    if t and not TYPES[t](value):
        return ["%s is not a %s: %r" % (path, t, value)]
    out = []
    if "const" in schema and value != schema["const"]:
        out.append("%s is not %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        out.append("%s is not one of %r" % (path, schema["enum"]))
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        out.append("%s is too short" % path)
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            out.append("%s is below %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append("%s is above %s" % (path, schema["maximum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append("%s is not above %s" % (path, schema["exclusiveMinimum"]))
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            out.append("%s has too many items" % path)
        for i, item in enumerate(value):
            out += violations(schema.get("items", {}), item, "%s[%d]" % (path, i))
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                out.append("%s lacks %s" % (path, name))
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        extra = schema.get("additionalProperties")
        for k, v in value.items():
            sub = "%s.%s" % (path, k)
            matched = [s for p, s in patterns.items() if re.search(p, k)]
            if k in props:
                out += violations(props[k], v, sub)
            for s in matched:
                out += violations(s, v, sub)
            if k in props or matched:
                continue
            if isinstance(extra, dict):
                out += violations(extra, v, sub)
            elif extra is not True:
                out.append("%s is not a key pi knows" % sub)
    return out


# ---------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/v1" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


class FakeLlama(BaseHTTPRequestHandler):
    alias = "qwen3.8-27b"
    n_ctx = 65536

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._send(401, {"error": {
                "code": 401, "message": "Invalid API Key",
                "type": "authentication_error"}})
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [
                {"id": self.alias, "object": "model", "owned_by": "llamacpp"}]})
        if self.path == "/props":
            return self._send(200, {
                "default_generation_settings": {"n_ctx": self.n_ctx},
                "modalities": {"vision": True}})
        self._send(404, {})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.key_file = os.path.join(self.dir, "repo", "api_key.txt")
        self.models = os.path.join(self.dir, "repo", "models")
        os.makedirs(self.models)
        with open(self.key_file, "w") as fh:
            fh.write(KEY)
        self.cfg = os.path.join(self.dir, "agent", "models.json")

        self.saved = {n: getattr(opencode, n) for n in
                      ("KEY_FILE", "MODELS_DIR", "ROOT", "PORT", "environment")}
        self.saved_gpu = opencode.hardware.detect_gpu
        opencode.KEY_FILE = self.key_file
        opencode.MODELS_DIR = self.models
        opencode.ROOT = os.path.join(self.dir, "repo")
        opencode.PORT = free_port()
        opencode.environment = lambda: "linux"
        opencode.hardware.detect_gpu = lambda: (0, "", "unknown")

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(opencode, n, v)
        opencode.hardware.detect_gpu = self.saved_gpu
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pi_agent.main(["--config", self.cfg] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_cfg(self, text, path=None):
        path = path or self.cfg
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read_cfg(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return fh.read()

    def kiln_block(self):
        return json.loads(self.read_cfg())["providers"]["kiln"]


# ---------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------

class SchemaTest(Base):
    def test_the_checker_is_not_vacuous(self):
        doc = {"providers": {"kiln": {
            "compat": {"thinkingFormat": "bogus"},
            "models": [{"id": "x", "contextwindow": 1, "maxTokens": 0}]}}}
        found = "\n".join(violations(SCHEMA, doc))
        self.assertIn("thinkingFormat", found)
        self.assertIn("contextwindow", found)
        self.assertIn("maxTokens", found)
        self.assertTrue(violations(SCHEMA, {}))

    def test_written_file_is_valid(self):
        open(os.path.join(self.models, "Qwen3.8-27B-UD-Q5_K_XL.gguf"), "w").close()
        open(os.path.join(self.models, "mmproj-F16.gguf"), "w").close()
        open(os.path.join(self.models, "Qwen3.8-9B-Q4_K_M.gguf"), "w").close()
        self.assertEqual(self.run_cli()[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(violations(SCHEMA, doc), [])
        self.assertGreaterEqual(len(doc["providers"]["kiln"]["models"]), 1)

    def test_live_server_entry_is_valid(self):
        with serve(FakeLlama) as url:
            self.assertEqual(self.run_cli("--url", url)[0], 0)
        self.assertEqual(violations(SCHEMA, json.loads(self.read_cfg())), [])


class WriteTest(Base):
    def test_creates_a_fresh_config(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(doc["$schema"], pi_agent.SCHEMA)
        block = doc["providers"]["kiln"]
        self.assertEqual(block["api"], "openai-completions")
        self.assertEqual(block["apiKey"], "!cat '%s'"
                         % os.path.abspath(self.key_file).replace("\\", "/"))
        self.assertNotIn(KEY, self.read_cfg())
        self.assertIn("/model", out)

    def test_merges_keeps_the_rest_and_backs_up_once(self):
        original = """{
  // mine
  "providers": {"ollama": {"baseUrl": "http://localhost:11434/v1",
                           "api": "openai-completions",
                           "models": [{"id": "qwen2.5-coder:7b"}]}}
}
"""
        self.write_cfg(original)
        self.assertEqual(self.run_cli()[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(sorted(doc["providers"]), ["kiln", "ollama"])
        self.assertEqual(doc["providers"]["ollama"]["models"],
                         [{"id": "qwen2.5-coder:7b"}])
        self.assertNotIn("$schema", doc)
        with open(self.cfg + ".kiln.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)

        first = self.read_cfg()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.read_cfg(), first)
        with open(self.cfg + ".kiln.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertFalse(os.path.exists(self.cfg + ".kiln.tmp"))

    def test_file_without_providers_gets_them(self):
        # pi requires "providers"; a file without it was already broken.
        self.write_cfg('{"$schema": "https://pi.dev/schemas/models.schema.json"}')
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(violations(SCHEMA, json.loads(self.read_cfg())), [])

    def test_null_providers_gets_an_object(self):
        self.write_cfg('{"providers": null}')
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(violations(SCHEMA, json.loads(self.read_cfg())), [])

    @unittest.skipIf(os.name == "nt", "no POSIX modes on Windows")
    def test_backup_keeps_the_files_mode(self):
        # models.json can hold other providers' literal keys.
        self.write_cfg('{"providers": {"other": {"apiKey": "sk-secret"}}}')
        os.chmod(self.cfg, 0o600)
        self.assertEqual(self.run_cli()[0], 0)
        for p in (self.cfg, self.cfg + ".kiln.bak"):
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), oct(0o600), p)

    @unittest.skipIf(os.name == "nt", "no POSIX modes on Windows")
    def test_a_new_agent_dir_is_private(self):
        # pi makes ~/.pi and ~/.pi/agent 0700; kiln creating them first
        # must not leave them more open than pi would.
        self.cfg = os.path.join(self.dir, "home", ".pi", "agent", "models.json")
        self.assertEqual(self.run_cli()[0], 0)
        for d in (os.path.join(self.dir, "home", ".pi"),
                  os.path.dirname(self.cfg)):
            self.assertEqual(oct(os.stat(d).st_mode & 0o777), oct(0o700), d)
        self.assertEqual(oct(os.stat(self.cfg).st_mode & 0o777), oct(0o600))

    def test_print_writes_nothing(self):
        code, out, _ = self.run_cli("--print")
        self.assertEqual(code, 0)
        self.assertIn("kiln", json.loads(out)["providers"])
        self.assertFalse(os.path.exists(self.cfg))

    def test_agent_dir_follows_the_environment(self):
        saved = os.environ.get("PI_CODING_AGENT_DIR")
        try:
            os.environ["PI_CODING_AGENT_DIR"] = os.path.join(self.dir, "elsewhere")
            self.assertEqual(pi_agent.config_path(None), os.path.join(
                self.dir, "elsewhere", "models.json"))
            del os.environ["PI_CODING_AGENT_DIR"]
            self.assertEqual(pi_agent.config_path(None), os.path.join(
                os.path.expanduser("~"), ".pi", "agent", "models.json"))
        finally:
            if saved is None:
                os.environ.pop("PI_CODING_AGENT_DIR", None)
            else:
                os.environ["PI_CODING_AGENT_DIR"] = saved

    def test_shell_style_agent_dirs_read_as_pi_reads_them(self):
        # pi on Windows turns these into drive paths; kiln must edit the
        # same file pi reads.
        conv = pi_agent.windows_shell_path
        self.assertEqual(conv("/c/Users/me/.pi/agent"), "C:\\Users\\me\\.pi\\agent")
        self.assertEqual(conv("/mnt/d/pi"), "D:\\pi")
        self.assertEqual(conv("/cygdrive/c/x/y"), "C:\\x\\y")
        self.assertEqual(conv("/c"), "C:\\")
        for unchanged in ("/home/me/.pi", "//server/share", "C:\\x", "rel/dir"):
            self.assertEqual(conv(unchanged), unchanged)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinked_config_stays_a_symlink(self):
        target = os.path.join(self.dir, "dotfiles", "models.json")
        self.write_cfg('{"providers": {}}', target)
        os.makedirs(os.path.dirname(self.cfg))
        os.symlink(target, self.cfg)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertTrue(os.path.islink(self.cfg))
        with open(target) as fh:
            self.assertIn("kiln", json.load(fh)["providers"])


class KeyCommandTest(Base):
    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
    def test_command_prints_the_key_from_an_awkward_path(self):
        odd = os.path.join(self.dir, "my $HOME repo", "api_key.txt")
        os.makedirs(os.path.dirname(odd))
        with open(odd, "w") as fh:
            fh.write(KEY)
        opencode.KEY_FILE = odd
        cmd = pi_agent.key_command()
        self.assertTrue(cmd.startswith("!"))
        out = subprocess.run(["sh", "-c", cmd[1:]], capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), KEY)


class WindowsShellTest(Base):
    """pi on Windows runs !commands in Git Bash from Program Files, else
    the first bash.exe on PATH, else cmd."""

    def note(self, program_files, which):
        env = {"ProgramFiles": program_files}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(pi_agent.shutil, "which", lambda _: which):
            os.environ.pop("ProgramFiles(x86)", None)
            return pi_agent.windows_shell_note()

    def test_git_bash_in_program_files_is_fine(self):
        pf = os.path.join(self.dir, "Program Files")
        os.makedirs(os.path.join(pf, "Git", "bin"))
        open(os.path.join(pf, "Git", "bin", "bash.exe"), "w").close()
        self.assertEqual(self.note(pf, r"C:\Windows\System32\bash.exe"), "")

    def test_wsl_bash_is_named(self):
        note = self.note(self.dir, r"C:\WINDOWS\system32\bash.exe")
        self.assertIn("WSL", note)
        self.assertIn("Git for Windows", note)

    def test_no_bash_means_cmd(self):
        self.assertIn("cmd", self.note(self.dir, None))

    def test_another_bash_reads_drive_paths(self):
        self.assertEqual(self.note(self.dir, r"C:\msys64\usr\bin\bash.exe"), "")


class RefuseTest(Base):
    def assert_refused(self, *argv):
        before = self.read_cfg() if os.path.exists(self.cfg) else None
        code, _, err = self.run_cli(*argv)
        self.assertEqual(code, 1)
        self.assertIn("was not changed", err)
        after = self.read_cfg() if os.path.exists(self.cfg) else None
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(self.cfg + ".kiln.bak"))
        return err

    def test_unparseable_config(self):
        self.write_cfg('{"providers": {oops}')
        self.assertIn("cannot parse", self.assert_refused())

    def test_providers_that_is_not_an_object(self):
        self.write_cfg('{"providers": ["not", "an", "object"]}')
        self.assertIn("not an object", self.assert_refused())

    def test_missing_key_file(self):
        os.remove(self.key_file)
        self.assertIn("api_key.txt", self.assert_refused())

    def test_key_path_that_cannot_be_quoted(self):
        odd = os.path.join(self.dir, "it's", "api_key.txt")
        os.makedirs(os.path.dirname(odd))
        with open(odd, "w") as fh:
            fh.write(KEY)
        opencode.KEY_FILE = odd
        self.assertIn("cannot quote", self.assert_refused())

    def test_url_without_scheme(self):
        self.assertIn("http://", self.assert_refused("--url", "localhost:8080/v1"))


class RemoveTest(Base):
    def test_remove_restores_the_rest(self):
        self.write_cfg('{"providers": {"other": {"baseUrl": "http://x/v1"}}}')
        self.run_cli()
        self.assertEqual(self.run_cli("--remove")[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(doc["providers"], {"other": {"baseUrl": "http://x/v1"}})

    def test_remove_keeps_an_empty_providers(self):
        # Dropping the key would make the file invalid for pi.
        self.run_cli()
        self.assertEqual(self.run_cli("--remove")[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(doc["providers"], {})
        self.assertEqual(violations(SCHEMA, doc), [])

    def test_remove_without_a_config_creates_nothing(self):
        self.assertEqual(self.run_cli("--remove")[0], 0)
        self.assertFalse(os.path.exists(self.cfg))

    def test_remove_mentions_a_kiln_default(self):
        self.run_cli()
        self.write_cfg('{"defaultProvider": "kiln", "defaultModel": "qwen3.8-27b"}',
                       os.path.join(os.path.dirname(self.cfg), "settings.json"))
        code, out, _ = self.run_cli("--remove")
        self.assertEqual(code, 0)
        self.assertIn("settings.json", out)


class ModelTest(Base):
    def setUp(self):
        super().setUp()
        open(os.path.join(self.models, "Qwen3.8-27B-UD-Q5_K_XL.gguf"), "w").close()
        self.saved_plan = opencode.planned_ctx
        opencode.planned_ctx = lambda *a: 114688

    def tearDown(self):
        opencode.planned_ctx = self.saved_plan
        super().tearDown()

    def entry(self, alias="qwen3.8-27b"):
        for m in self.kiln_block()["models"]:
            if m["id"] == alias:
                return m
        self.fail("%s not listed" % alias)

    def test_planned_window_without_a_server(self):
        self.assertEqual(self.run_cli()[0], 0)
        m = self.entry()
        self.assertEqual(m["contextWindow"], 114688)
        self.assertLess(m["maxTokens"], m["contextWindow"])
        self.assertEqual(m["input"], ["text"])

    def test_smaller_live_window_wins_and_vision_is_seen(self):
        with serve(FakeLlama) as url:
            code, out, _ = self.run_cli("--url", url)
        self.assertEqual(code, 0)
        m = self.entry()
        self.assertEqual(m["contextWindow"], 65536)
        self.assertEqual(m["input"], ["text", "image"])
        self.assertIn("running, serving qwen3.8-27b", out)

    def test_thinking_is_on_or_off_only(self):
        self.assertEqual(self.run_cli()[0], 0)
        levels = self.entry()["thinkingLevelMap"]
        self.assertEqual(sorted(k for k, v in levels.items() if v), ["medium", "off"])
        self.assertEqual(self.kiln_block()["compat"]["thinkingFormat"],
                         "qwen-chat-template")


if __name__ == "__main__":
    unittest.main(verbosity=2)
