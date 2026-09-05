"""
muse-sub-proxy: expose a Meta subscription through OpenAI-compatible APIs.

Standalone: NO `muse` CLI anywhere in the loop. Pure Python + stdlib.

  External tool (OpenCode/Hermes/etc)
    -> POST /v1/chat/completions (OpenAI format) to this proxy
    -> proxied as POST https://api.meta.ai/v1/responses (Bearer LLM| key)
    -> translated back to OpenAI chat.completion JSON (+SSE)

Credential: the Meta API key (``LLM|...``) minted by Meta OAuth login,
resolved in this order:
  1. ``MUSE_SUB_API_KEY`` env var (Linux/portable),
  2. macOS keychain item ``ai.meta.dev.credentials`` / account ``meta``
     (read via ``security`` CLI; one-time Always-Allow approval),
  3. ``~/.config/muse-sub-proxy/api_key.txt`` (0600 file fallback).

Onboarding a new machine currently takes one ``muse login`` (or any Meta
OAuth login that populates the keychain item) — the raw device flow only
yields a ``dca:`` token that api.meta.ai rejects, so the proxy does NOT
implement its own login. See README for the full reverse-engineering notes.

Env: SUB_PROXY_PORT (default 8920), SUB_PROXY_MODEL (default muse-spark-1.2),
MUSE_SUB_API_KEY.

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (stream: true/false)
"""
import json
import os
import subprocess
import threading
import time
import http.server
import socketserver
import urllib.request
import urllib.error

PORT = int(os.environ.get("SUB_PROXY_PORT", "8920"))
MODEL_DEFAULT = os.environ.get("SUB_PROXY_MODEL", "muse-spark-1.2")
UPSTREAM = "https://api.meta.ai/v1"
UA = "muse-sub-proxy/0.1"

MODELS = [m.strip() for m in os.environ.get(
    "SUB_PROXY_MODELS",
    "muse-spark-1.2,muse-spark-1.3,muse-spark-1.2-contributor,"
    "muse-spark-1.3-contributor").split(",") if m.strip()]

_key_cache = {"key": "", "at": 0}


def get_api_key():
    """Resolve the LLM| key. Cached 60s so keychain isn't hit per request."""
    if _key_cache["key"] and time.time() - _key_cache["at"] < 60:
        return _key_cache["key"]
    key = (os.environ.get("MUSE_SUB_API_KEY", "").strip()
           or _keychain_key() or _file_key())
    if not key:
        raise RuntimeError("no API key: set MUSE_SUB_API_KEY, approve the "
                           "keychain item, or write ~/.config/muse-sub-proxy/api_key.txt")
    _key_cache.update(key=key, at=time.time())
    return key


def _keychain_key():
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "ai.meta.dev.credentials",
             "-a", "meta", "-w"],
            capture_output=True, text=True, timeout=25)
        if raw.returncode != 0:
            return ""
        return json.loads(raw.stdout).get("api_key", "")
    except Exception:
        return ""


def _file_key():
    try:
        p = os.path.expanduser("~/.config/muse-sub-proxy/api_key.txt")
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return ""


def upstream(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        UPSTREAM + path, data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": "Bearer " + get_api_key(), "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": {"message": "http_%d" % e.code}}


def upstream_get(path):
    req = urllib.request.Request(
        UPSTREAM + path,
        headers={"Accept": "application/json",
                 "Authorization": "Bearer " + get_api_key(), "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


def messages_to_input(messages):
    """Flatten OpenAI chat messages to a /responses input string."""
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
        if role == "system":
            parts.append("[System Instructions]\n%s\n[End System Instructions]" % content)
        elif role == "assistant":
            parts.append("[Previous assistant message]\n%s" % content)
        else:
            parts.append(str(content))
    return "\n\n".join(p for p in parts if p.strip())


def response_text(resp):
    """Pull assistant text out of a /responses object."""
    texts = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
    return "".join(texts)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "models" in self.path:
            try:
                _, up = upstream_get("/models")
                ids = [m.get("id") for m in up.get("data", []) if m.get("id")]
                names = [m for m in MODELS if m in ids] or ids
            except Exception:
                names = MODELS
            body = json.dumps({"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "meta"}
                for m in names]}).encode()
            self._send(200, body)
            return
        if self.path in ("/", "/healthz"):
            self._send(200, b'{"ok":true,"proxy":"muse-sub-proxy-standalone"}')
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if "chat/completions" not in self.path:
            self._send(404, b'{"error":"only /v1/chat/completions supported"}')
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        try:
            req = json.loads(self.rfile.read(length) if length else b"{}")
        except Exception:
            self._send(400, b'{"error":"invalid json"}')
            return
        model = req.get("model") or MODEL_DEFAULT
        prompt = messages_to_input(req.get("messages", []))
        if not prompt:
            self._send(400, b'{"error":"empty prompt"}')
            return
        stream = bool(req.get("stream"))
        try:
            status, resp = upstream("/responses", {"model": model, "input": prompt})
        except RuntimeError as e:
            self._send(500, json.dumps({"error": str(e)}).encode())
            return
        if status // 100 != 2:
            self._send(status, json.dumps(resp).encode())
            return
        text = response_text(resp)
        created = int(time.time())
        cid = resp.get("id", "chatcmpl-sub-%d" % created)

        if not stream:
            self._send(200, json.dumps({
                "id": cid, "object": "chat.completion", "created": created,
                "model": resp.get("model", model),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": resp.get("usage", {}),
            }).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            head = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"},
                                 "finish_reason": None}]}
            self.wfile.write(("data: %s\n\n" % json.dumps(head)).encode())
            for i in range(0, len(text), 2000):
                chunk = {"id": cid, "object": "chat.completion.chunk",
                         "created": created, "model": model,
                         "choices": [{"index": 0,
                                      "delta": {"content": text[i:i + 2000]},
                                      "finish_reason": None}]}
                self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode())
            tail = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(("data: %s\n\ndata: [DONE]\n\n" % json.dumps(tail)).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a, **k):
        pass


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("muse-sub-proxy (standalone) on 127.0.0.1:%s model=%s" % (PORT, MODEL_DEFAULT),
          flush=True)
    Threaded(("127.0.0.1", PORT), H).serve_forever()
