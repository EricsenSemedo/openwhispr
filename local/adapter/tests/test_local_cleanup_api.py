"""Loopback HTTP tests for local_cleanup_shim with a fake NeMo backend.

Starts both servers on ephemeral ports inside this process; touches no live
service, profile or port in use by OpenWhispr.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_cleanup_shim as shim  # noqa: E402


class FakeNemo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"ready": True, "capabilities": ["asr"], "device": "fake"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Quiet(shim.LocalCleanupShim):
    def log_message(self, *a):
        pass


def start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def request(url, payload=None, origin=None, method=None, ctype="application/json"):
    data = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if origin:
        req.add_header("Origin", origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.options = Path(cls.tmp.name) / "local-dictation-options.json"
        cls.nemo = start(FakeNemo)
        shim.asr.NEMO_BASE_URL = f"http://127.0.0.1:{cls.nemo.server_port}"
        shim.OPTIONS = cls.options
        cls.server = start(Quiet)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.nemo.shutdown(); cls.nemo.server_close()
        cls.tmp.cleanup()

    def chat(self, text, **kw):
        return request(self.base + "/v1/chat/completions",
                       {"model": shim.MODEL, "messages": [{"role": "user", "content": text}]}, **kw)

    def test_missing_options_preserve_text(self):
        if self.options.exists():
            self.options.unlink()
        status, body, _ = self.chat("um hello")
        self.assertEqual((status, body["choices"][0]["message"]["content"]), (200, "um hello"))

    def test_enabled_cleanup_and_envelope(self):
        self.options.write_text(json.dumps({"conservativeCleanup": True,
                                            "removeRepeatedFunctionWords": True,
                                            "protectedTerms": []}), encoding="utf-8")
        try:
            status, body, _ = self.chat(shim.APP_TRANSCRIPT_PREFIX + "um I I agree" + shim.APP_TRANSCRIPT_SUFFIX)
            self.assertEqual(status, 200)
            self.assertEqual(body["choices"][0]["message"]["content"], "I agree")
            self.assertTrue(body["local_cleanup"]["changed"])
            status, body, _ = self.chat("um <transcript> uh go")
            self.assertEqual(body["choices"][0]["message"]["content"], "um <transcript> uh go")
        finally:
            self.options.unlink()

    def test_cors_and_origin(self):
        status, _, headers = self.chat("hi", origin="null")
        self.assertEqual((status, headers.get("Access-Control-Allow-Origin"), headers.get("Vary")),
                         (200, "null", "Origin"))
        status, body, _ = self.chat("hi", origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _, headers = request(self.base + "/v1/chat/completions", method="OPTIONS", origin="null")
        self.assertEqual((status, headers.get("Access-Control-Allow-Methods")), (204, "POST, OPTIONS"))

    def test_errors(self):
        url = self.base + "/v1/chat/completions"
        self.assertEqual(request(url, {"model": "x"})[0], 400)
        self.assertEqual(request(url, {"model": shim.MODEL, "messages": []})[1]["error"],
                         "Provide exactly one user message containing the raw transcript")
        self.assertEqual(request(url, b"{}", ctype="text/plain")[0], 415)
        self.assertEqual(request(url, b"x" * (1024 * 1024 + 1))[0], 413)
        self.assertEqual(request(self.base + "/v1/nope", {"a": 1})[0], 404)

    def test_models_and_health_delegate_to_backend(self):
        status, body, _ = request(self.base + "/v1/models")
        self.assertEqual([m["id"] for m in body["data"]], ["parakeet-v3-q8", shim.MODEL])
        self.assertEqual(request(self.base + "/health")[1]["ready"], True)

    def test_audio_path_unchanged(self):
        status, body, _ = request(self.base + "/audio/transcriptions", b"junk", ctype="text/plain")
        self.assertEqual((status, body["error"]),
                         (400, "bad multipart: missing multipart boundary in Content-Type"))


if __name__ == "__main__":
    unittest.main()
