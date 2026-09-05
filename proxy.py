"""
muse-sub-proxy: expose a Muse Code subscription through OpenAI-compatible APIs.

Architecture (deliberately boring):
  External tool (OpenCode/etc)
    -> POST /v1/chat/completions (OpenAI format) to this proxy
    -> proxy shells out to the REAL `muse exec` CLI (subscription auth via
       macOS keychain, XDG_CONFIG_HOME pinned to a clean sub-mode config)
    -> proxy translates CLI stdout back to OpenAI chat.completion JSON (+SSE)

Why subprocess instead of token extraction?
- The sub credential is OAuth in keychain + entitlement-checked server side.
  There is no static Bearer key to steal (unlike pay-as-you-go). Driving the
  real CLI keeps auth/refresh/entitlement on Meta's own binary, so this
  survives CLI updates that would break a hand-rolled REST reimplementation.
- Trade-off: one HTTP request = one `muse exec` turn (no token streaming,
  latency = full agent turn). Fine for value-testing, not for interactive use.

Setup:
  1. `muse login` with the sub account (device-code flow, one time).
  2. Clean sub-mode config dir (NO endpoint_transport pin, NO META_API_KEY):
       SUB_PROXY_XDG=/tmp/muse-sub-test   # contains muse/settings.json +
                                          # muse/auth.json (keychain pointer)
  3. Run:  python3 proxy.py
     Env: SUB_PROXY_PORT (default 8920), SUB_PROXY_MODEL (default
     muse-spark-1.2), SUB_PROXY_XDG, SUB_PROXY_WORKSPACE, SUB_PROXY_TIMEOUT,
     SUB_PROXY_MAX_STEPS.

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
import shutil

PORT = int(os.environ.get("SUB_PROXY_PORT", "8920"))
MODEL_DEFAULT = os.environ.get("SUB_PROXY_MODEL", "muse-spark-1.2")
XDG = os.environ.get("SUB_PROXY_XDG", "/tmp/muse-sub-test")
WORKSPACE = os.environ.get("SUB_PROXY_WORKSPACE", "/tmp")
TIMEOUT = int(os.environ.get("SUB_PROXY_TIMEOUT", "600"))
MAX_STEPS = os.environ.get("SUB_PROXY_MAX_STEPS", "")
MUSE_BIN = shutil.which("muse") or os.path.expanduser("~/.local/bin/muse")

MODELS = [m.strip() for m in os.environ.get(
    "SUB_PROXY_MODELS", "muse-spark-1.2,muse-spark-1.3").split(",") if m.strip()]


def messages_to_prompt(messages):
    """Flatten OpenAI chat messages into one `muse exec` prompt."""
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # content parts (text/image_url)
            texts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            content = "\n".join(texts)
        if role == "system":
            parts.append(f"[System Instructions]\n{content}\n[End System Instructions]")
        elif role == "assistant":
            parts.append(f"[Previous assistant message]\n{content}")
        else:
            parts.append(str(content))
    return "\n\n".join(p for p in parts if p.strip())


def run_muse(prompt, model):
    """Run one subscribed `muse exec` turn, return (text, elapsed_s)."""
    cmd = [MUSE_BIN, "exec", "--model", model, prompt]
    if MAX_STEPS:
        cmd[2:2] = ["--max-model-steps", str(MAX_STEPS)]
    env = dict(os.environ)
    env.pop("META_API_KEY", None)  # gateway key would shadow sub login
    env["XDG_CONFIG_HOME"] = XDG
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT, cwd=WORKSPACE)
    except subprocess.TimeoutExpired:
        return f"[muse-sub-proxy] ERROR: turn timed out after {TIMEOUT}s", TIMEOUT
    dt = time.time() - t0
    if p.returncode != 0:
        err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()[-2000:]
        return f"[muse-sub-proxy] ERROR: muse exec failed: {err}", dt
    return p.stdout.strip(), dt


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
            body = json.dumps({"object": "list", "data": [
                {"id": m, "object": "model", "created": 1234567890,
                 "owned_by": "meta-subscription"} for m in MODELS]}).encode()
            self._send(200, body)
            return
        if self.path in ("/", "/healthz"):
            self._send(200, b'{"ok":true,"proxy":"muse-sub-proxy"}')
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
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            self._send(400, b'{"error":"invalid json"}')
            return
        model = req.get("model") or MODEL_DEFAULT
        prompt = messages_to_prompt(req.get("messages", []))
        if not prompt:
            self._send(400, b'{"error":"empty prompt"}')
            return
        stream = bool(req.get("stream"))
        text, _dt = run_muse(prompt, model)
        created = int(time.time())
        cid = f"chatcmpl-sub-{created}"

        if not stream:
            body = json.dumps({
                "id": cid, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0,
                          "note": "token counts unavailable via CLI subprocess"},
            }).encode()
            self._send(200, body)
            return

        # SSE stream: emit the full text in chunks, then [DONE]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(
                f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n".encode())
            for i in range(0, len(text), 2000):
                chunk = json.dumps({"id": cid, "object": "chat.completion.chunk",
                                    "created": created, "model": model,
                                    "choices": [{"index": 0,
                                                 "delta": {"content": text[i:i + 2000]},
                                                 "finish_reason": None}]})
                self.wfile.write(f"data: {chunk}\n\n".encode())
            self.wfile.write(
                f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n".encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a, **k):
        pass


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"muse-sub-proxy on 127.0.0.1:{PORT} model={MODEL_DEFAULT} "
          f"xdg={XDG} muse={MUSE_BIN}", flush=True)
    Threaded(("127.0.0.1", PORT), H).serve_forever()
