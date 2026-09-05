"""
muse-sub-proxy: expose a Meta subscription through OpenAI-compatible APIs.

Standalone: NO `muse` CLI anywhere in the loop. Pure Python + stdlib.

  External tool (OpenCode/Hermes/etc)
    -> POST /v1/chat/completions (OpenAI format) to this proxy
    -> proxied as POST https://api.meta.ai/v1/responses (Bearer LLM| key, stream=true)
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
  GET  /v1/models          live model ids (upstream, fallback: static list)
  GET  /v1/usage           latest subscription quota snapshot captured from
                           the upstream ``response.subscription_usage`` SSE event
  GET  /healthz
  POST /v1/chat/completions   (stream: true/false)

Stretch knobs (all opt-in, all verified live):
  ``reasoning_effort`` request field (or SUB_PROXY_EFFORT env): e.g.
  "minimal" cut output ~9x on trivia vs upstream default; "none" is
  rejected upstream. Absent = upstream default (quality-first).
  1.3 also takes low/medium/high/xhigh/max; 1.2 tops out at xhigh
  (max rejected) — verified live against the upstream error strings.
  ``sub_session`` request field: name a server-side conversation. First call
  sends full history; follow-ups send only the newest user message plus
  previous_response_id (verified: 46 input tokens recalled full context).
  Stateless clients omitting it are unaffected.

Update-survival design (survives `muse` CLI updates untouched):
  - never shells out to, imports, or reads version state from the CLI;
  - model list is fetched live from upstream, never pinned;
  - SSE parsing is tolerant: unknown events ignored, usage snapshot optional,
    completion reconstructed from deltas if `response.completed` is absent;
  - on HTTP 401 the cached key is dropped and re-read once (survives key
    rotation/re-login without a proxy restart).
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
UA = "muse-sub-proxy/0.2"

MODELS = [m.strip() for m in os.environ.get(
    "SUB_PROXY_MODELS",
    "muse-spark-1.2,muse-spark-1.3,muse-spark-1.2-contributor,"
    "muse-spark-1.3-contributor").split(",") if m.strip()]

CONF_DIR = os.path.expanduser("~/.config/muse-sub-proxy")
USAGE_PATH = os.path.join(CONF_DIR, "usage.json")
_usage_lock = threading.Lock()

# opt-in server-side conversation chains: sub_session name -> last response id.
# Stateless clients are unaffected; only requests carrying "sub_session" use it.
_sessions = {}
_sessions_lock = threading.Lock()

# Client-controllable reasoning knob: request field "reasoning_effort" or env.
# Upstream default (field absent) = quality-first. "minimal" cut output ~9x
# on trivia (248 -> 27 tokens) with correct answers; "none" is rejected
# upstream, "minimal" is the floor.
EFFORT_DEFAULT = os.environ.get("SUB_PROXY_EFFORT", "").strip()

_key_cache = {"key": "", "at": 0}


def get_api_key(fresh=False):
    """Resolve the LLM| key. Cached 60s so keychain isn't hit per request."""
    if not fresh and _key_cache["key"] and time.time() - _key_cache["at"] < 60:
        return _key_cache["key"]
    key = (os.environ.get("MUSE_SUB_API_KEY", "").strip()
           or _keychain_key() or _file_key())
    if not key:
        raise RuntimeError("no API key: set MUSE_SUB_API_KEY, approve the "
                           "keychain item, or write ~/.config/muse-sub-proxy/api_key.txt")
    _key_cache.update(key=key, at=time.time())
    return key


def drop_key_cache():
    _key_cache.update(key="", at=0)


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


def save_usage(snapshot, model):
    """Persist the latest subscription quota snapshot (best-effort)."""
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = USAGE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"subscription": snapshot, "model": model,
                       "updated_at": int(time.time())}, f)
        os.replace(tmp, USAGE_PATH)
    except OSError:
        pass


def load_usage():
    try:
        with open(USAGE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _authed_request(url, data=None, stream=False):
    """Build an upstream request. Returns (Request, key_is_fresh)."""
    headers = {"Authorization": "Bearer " + get_api_key(),
               "User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if stream:
        headers["Accept"] = "text/event-stream"
    return urllib.request.Request(url, data=data, headers=headers,
                                  method="GET" if data is None else "POST")


def _read_error(e):
    try:
        return e.code, json.loads(e.read().decode())
    except Exception:
        return getattr(e, "code", 500), {"error": {"message": "http_error"}}


def upstream_stream(path, payload):
    """POST upstream with stream=true. Returns (status, resp_obj, sub_snapshot).

    resp_obj is the `response.completed` object when present, else a minimal
    object rebuilt from text deltas. sub_snapshot is the subscription quota
    dict from `response.subscription_usage`, or None.
    """
    payload = dict(payload, stream=True)
    data = json.dumps(payload).encode()
    try:
        req = _authed_request(UPSTREAM + path, data=data, stream=True)
        resp_obj, sub = _consume_sse(req)
        return 200, resp_obj, sub
    except urllib.error.HTTPError as e:
        if e.code == 401:  # key may have rotated -> re-read once, retry
            drop_key_cache()
            try:
                req = _authed_request(UPSTREAM + path, data=data, stream=True)
                resp_obj, sub = _consume_sse(req)
                return 200, resp_obj, sub
            except urllib.error.HTTPError as e2:
                return _read_error(e2) + (None,)
        return _read_error(e) + (None,)


def _consume_sse(req):
    """Read an SSE stream. Returns (resp_obj, sub_snapshot). Never raises
    on shape changes — unknown events are ignored."""
    resp_obj = None
    sub = None
    deltas = []
    usage = {}
    rid, model = "", ""
    cur_event = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw_line in r:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                cur_event = None
                continue
            if line.startswith("event:"):
                cur_event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data_s = line[5:].strip()
            if data_s == "[DONE]":
                break
            try:
                d = json.loads(data_s)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            etype = d.get("type", cur_event or "")
            if etype == "response.subscription_usage" or "subscription" in d:
                s = d.get("subscription")
                if isinstance(s, dict):
                    sub = s
            elif etype in ("response.completed", "response.incomplete",
                           "response.failed", "response.cancelled"):
                # terminal events all carry the full response object
                if isinstance(d.get("response"), dict):
                    resp_obj = d["response"]
                else:
                    resp_obj = {k: v for k, v in d.items() if k != "type"}
            elif etype in ("response.output_text.delta",):
                delta = d.get("delta", "")
                if isinstance(delta, str):
                    deltas.append(delta)
            # keep id/model hints if completed never arrives
            if not rid and isinstance(d.get("id"), str):
                rid = d["id"]
            if not model and isinstance(d.get("model"), str):
                model = d["model"]
    if resp_obj is None:
        text = "".join(deltas)
        resp_obj = {"id": rid, "model": model, "usage": usage,
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": text}]}] if text else []}
    return resp_obj, sub


def upstream_get(path):
    try:
        with urllib.request.urlopen(_authed_request(UPSTREAM + path), timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            drop_key_cache()
            try:
                with urllib.request.urlopen(
                        _authed_request(UPSTREAM + path), timeout=30) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e2:
                return _read_error(e2)
        return _read_error(e)


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


def newest_user_text(messages):
    """Latest user message as plain text (for chained session turns)."""
    for m in reversed(messages or []):
        if m.get("role", "user") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
            content = str(content).strip()
            if content:
                return content
    return ""


def response_text(resp):
    """Pull assistant text out of a /responses object."""
    texts = []
    for item in (resp or {}).get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
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
        if "usage" in self.path:
            u = load_usage()
            if u is None:
                self._send(200, b'{"error":"no usage captured yet - send a chat completion first"}')
            else:
                self._send(200, json.dumps(u).encode())
            return
        if "models" in self.path:
            try:
                _, up = upstream_get("/models")
                ids = [m.get("id") for m in (up.get("data", []) or [])
                       if isinstance(m, dict) and m.get("id")]
                names = ids or MODELS  # live list wins; statics are fallback
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
        payload = {"model": model, "input": prompt,
                   "max_output_tokens": req.get("max_tokens") or 4096}
        effort = (req.get("reasoning_effort") or EFFORT_DEFAULT or "").strip()
        if effort:
            payload["reasoning"] = {"effort": effort}
        # Opt-in server-side session: follow-ups send only the newest user
        # message plus previous_response_id instead of the full history.
        sess = (req.get("sub_session") or "").strip() if isinstance(
            req.get("sub_session"), str) else ""
        if sess:
            with _sessions_lock:
                prev = _sessions.get(sess)
            if prev:
                delta = newest_user_text(req.get("messages", []))
                if delta:
                    payload["input"] = delta
                    payload["previous_response_id"] = prev
        try:
            status, resp, sub = upstream_stream("/responses", payload)
        except RuntimeError as e:
            self._send(500, json.dumps({"error": str(e)}).encode())
            return
        except Exception as e:
            self._send(502, json.dumps({"error": "upstream: %s" % e}).encode())
            return
        if status // 100 != 2:
            self._send(status, json.dumps(resp).encode())
            return
        if sess and isinstance(resp, dict) and resp.get("id"):
            with _sessions_lock:
                _sessions[sess] = resp["id"]
        if sub is not None:
            save_usage(sub, resp.get("model", model))
        text = response_text(resp)
        usage = resp.get("usage", {}) or {}
        status = (resp.get("status") or "completed") if isinstance(resp, dict) else "completed"
        finish = "length" if status == "incomplete" else "stop"
        created = int(time.time())
        cid = resp.get("id", "chatcmpl-sub-%d" % created) or "chatcmpl-sub-%d" % created

        if not stream:
            out = {
                "id": cid, "object": "chat.completion", "created": created,
                "model": resp.get("model", model),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": finish}],
                "usage": usage,
            }
            if sub is not None:
                out["subscription"] = sub  # extra: quota snapshot for this call
            self._send(200, json.dumps(out).encode())
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
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": usage}
            if sub is not None:
                tail["subscription"] = sub
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
