#!/usr/bin/env python3
"""Tests for scripts/opencode.py, the `kiln opencode` backend.

It edits a config file other tools depend on, so the failure modes are
the part worth pinning: never overwrite a file it cannot parse, never
write a reference to a key file that does not exist, never mistake
another service on the port for llama-server.

Hermetic: every path it touches is under a temporary directory, the GPU
probe is stubbed, and the port it probes is one nothing listens on.
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.dont_write_bytecode = True
import opencode  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"


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
    n_ctx = 212992

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


class OtherService(BaseHTTPRequestHandler):
    """Shaped like the docker container that shadowed port 8080."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"missing authorization header\n"
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NotHttp(threading.Thread):
    """Answers with bytes that are not an HTTP status line."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.url = "http://127.0.0.1:%d/v1" % self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.key_file = os.path.join(self.dir, "repo", "api_key.txt")
        self.models = os.path.join(self.dir, "repo", "models")
        os.makedirs(self.models)
        with open(self.key_file, "w") as fh:
            fh.write(KEY)
        self.cfg = os.path.join(self.dir, "cfg", "opencode.jsonc")

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
            code = opencode.main(["--config", self.cfg] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_cfg(self, text):
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read_cfg(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return fh.read()


class JsoncTest(unittest.TestCase):
    def test_comments_and_trailing_commas(self):
        src = """{
          // line comment
          "a": "http://keep//this", /* block */
          "b": [1, 2, ],
          "c": {"d": "say \\"hi\\" // not a comment", }, /* before brace */
        }"""
        self.assertEqual(json.loads(opencode.strip_jsonc(src)), {
            "a": "http://keep//this", "b": [1, 2],
            "c": {"d": 'say "hi" // not a comment'}})


class WriteTest(Base):
    def test_creates_a_fresh_config(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(doc["$schema"], opencode.SCHEMA)
        opts = doc["provider"]["kiln"]["options"]
        self.assertEqual(opts["apiKey"], "{file:%s}"
                         % os.path.abspath(self.key_file).replace("\\", "/"))
        self.assertNotIn(KEY, self.read_cfg())
        self.assertIn("Restart OpenCode", out)

    def test_merges_keeps_the_rest_and_backs_up_once(self):
        original = """{
  // mine
  "$schema": "https://opencode.ai/config.json",
  "provider": {"other": {"options": {"baseURL": "https://x/v1"}},},
  "model": "other/foo",
}
"""
        self.write_cfg(original)
        self.assertEqual(self.run_cli()[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(sorted(doc["provider"]), ["kiln", "other"])
        self.assertEqual(doc["model"], "other/foo")
        with open(self.cfg + ".kiln.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)

        first = self.read_cfg()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.read_cfg(), first)
        with open(self.cfg + ".kiln.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertFalse(os.path.exists(self.cfg + ".kiln.tmp"))

    @unittest.skipIf(os.name == "nt", "no POSIX modes on Windows")
    def test_backup_keeps_the_files_mode(self):
        self.write_cfg('{"provider": {"other": {"options": {"apiKey": "sk-x"}}}}')
        os.chmod(self.cfg, 0o600)
        self.assertEqual(self.run_cli()[0], 0)
        for p in (self.cfg, self.cfg + ".kiln.bak"):
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), oct(0o600), p)

    def test_print_writes_nothing(self):
        code, out, _ = self.run_cli("--print")
        self.assertEqual(code, 0)
        self.assertIn('"kiln"', out)
        self.assertFalse(os.path.exists(self.cfg))

    def test_hidden_by_enabled_providers_is_reported(self):
        self.write_cfg('{"enabled_providers": ["other"]}')
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("enabled_providers", out)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinked_config_stays_a_symlink(self):
        target = os.path.join(self.dir, "dotfiles", "opencode.jsonc")
        os.makedirs(os.path.dirname(target))
        with open(target, "w") as fh:
            fh.write("{}")
        os.makedirs(os.path.dirname(self.cfg))
        os.symlink(target, self.cfg)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertTrue(os.path.islink(self.cfg))
        with open(target) as fh:
            self.assertIn("kiln", json.load(fh)["provider"])


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
        self.write_cfg('{"provider": {oops}')
        self.assertIn("cannot parse", self.assert_refused())

    def test_provider_that_is_not_an_object(self):
        self.write_cfg('{"provider": ["not", "an", "object"]}')
        self.assertIn("not an object", self.assert_refused())

    def test_missing_key_file(self):
        os.remove(self.key_file)
        self.assertIn("api_key.txt", self.assert_refused())

    def test_key_path_opencode_cannot_reference(self):
        odd = os.path.join(self.dir, "we}ird", "api_key.txt")
        os.makedirs(os.path.dirname(odd))
        with open(odd, "w") as fh:
            fh.write(KEY)
        opencode.KEY_FILE = odd
        self.assertIn("{file:}", self.assert_refused())

    def test_url_without_scheme(self):
        self.assertIn("http://", self.assert_refused("--url", "localhost:8080/v1"))


class RemoveTest(Base):
    def test_remove_restores_the_rest(self):
        self.write_cfg('{"provider": {"other": {}}, "model": "other/x"}')
        self.run_cli()
        self.assertEqual(self.run_cli("--remove")[0], 0)
        doc = json.loads(self.read_cfg())
        self.assertEqual(doc["provider"], {"other": {}})
        self.assertEqual(doc["model"], "other/x")

    def test_remove_without_a_config_creates_nothing(self):
        self.assertEqual(self.run_cli("--remove")[0], 0)
        self.assertFalse(os.path.exists(self.cfg))


class ProbeTest(Base):
    def test_llama_server(self):
        with serve(FakeLlama) as url:
            info = opencode.probe(url, KEY)
        self.assertEqual(info, {"auth": True, "alias": "qwen3.8-27b",
                                "ctx": 212992, "vision": True})

    def test_wrong_key_is_still_recognised(self):
        with serve(FakeLlama) as url:
            self.assertEqual(opencode.probe(url, "wrong"), {"auth": False})

    def test_other_service_on_the_port(self):
        with serve(OtherService) as url:
            self.assertIsNone(opencode.probe(url, KEY))
            self.assertTrue(opencode.occupied(url))

    def test_non_http_service_on_the_port(self):
        t = NotHttp()
        t.start()
        try:
            self.assertIsNone(opencode.probe(t.url, KEY))
        finally:
            t.sock.close()

    def test_nothing_listening(self):
        url = "http://127.0.0.1:%d/v1" % free_port()
        self.assertIsNone(opencode.probe(url, KEY))
        self.assertFalse(opencode.occupied(url))


class ContextTest(Base):
    def setUp(self):
        super().setUp()
        open(os.path.join(self.models, "Qwen3.8-27B-UD-Q5_K_XL.gguf"), "w").close()
        self.saved_plan = opencode.planned_ctx
        opencode.planned_ctx = lambda *a: 114688

    def tearDown(self):
        opencode.planned_ctx = self.saved_plan
        super().tearDown()

    def ctx_for(self, live_ctx):
        live = {"auth": True, "alias": "qwen3.8-27b", "ctx": live_ctx,
                "vision": False}
        models = opencode.build_models(live, [])
        return models["qwen3.8-27b"]["limit"]

    def test_larger_live_window_is_capped_to_the_planned_one(self):
        self.assertEqual(self.ctx_for(212992)["context"], 114688)

    def test_smaller_live_window_wins(self):
        self.assertEqual(self.ctx_for(65536)["context"], 65536)

    def test_output_leaves_room_for_input(self):
        limit = self.ctx_for(16384)
        self.assertLess(limit["output"], limit["context"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
