"""Today reverse proxy — OpenAI / Anthropic compatible, stdlib only, account pool.

    python3 server.py
    TODAY_SESSION=tok1,tok2,tok3 POOL_WEIGHTS=1,2,1 AUTH_TOKENS=sk-x python3 server.py

Routes:
    GET  /health              liveness + pool summary
    GET  /v1/models           OpenAI model list
    POST /v1/chat/completions OpenAI chat (stream=true -> SSE)
    POST /v1/messages         Anthropic messages (minis / claude clients)
    POST /v1/complete         Anthropic legacy complete
    GET  /ui                  控制台：登录 / 号池 / 额度 / 切模型
    GET  /ui/pool             号池状态 JSON
    POST /ui/pool/add         加号 {"token":..., "weight":1, "label":"a"}
    POST /ui/pool/remove      {"key":"<label 或 token 前缀>"}
    POST /ui/pool/refresh     强制刷额度

Auth: client sends Authorization: Bearer <AUTH_TOKENS 里任意一个> 或 x-api-key。
      AUTH_TOKENS 为空则服务不鉴权（仅内网）。
      Today session token 只存在服务端，客户端永远看不到。
"""
import json
import os
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import TodayError, MODEL_OPTIONS, NAME2OPT, send_otp, verify_otp
from pool import Pool, run_with_pool, NoAccount

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

SESSION_FILE = os.path.join(DATA, "session.txt")
ACCOUNTS_FILE = os.path.join(DATA, "accounts.json")
CONFIG_FILE = os.path.join(DATA, "config.json")


# ----------------------------------------------------------------- config
class Config:
    def __init__(self):
        self.lock = threading.RLock()
        self.auth_tokens = [t.strip() for t in os.environ.get("AUTH_TOKENS", "").split(",") if t.strip()]
        self.sessions = [t.strip() for t in os.environ.get("TODAY_SESSION", "").split(",") if t.strip()]
        self.weights = [w.strip() for w in os.environ.get("POOL_WEIGHTS", "").split(",") if w.strip()]
        self.labels = [l.strip() for l in os.environ.get("POOL_LABELS", "").split(",")]
        self.default_model = os.environ.get("DEFAULT_MODEL", "deepseek")
        self.reply_timeout = int(os.environ.get("REPLY_TIMEOUT", "180"))
        # token -> (weight, label)，和 sessions 列表解耦，避免索引错位
        self.meta = {}
        self._load()
        self.save()

    def _set_meta(self, token, weight, label):
        self.meta[token] = {"weight": float(weight or 1), "label": label or ""}

    def _load(self):
        env_w = self.weights
        env_l = self.labels
        for i, t in enumerate(self.sessions):
            w = env_w[i] if i < len(env_w) else 1.0
            lab = env_l[i] if i < len(env_l) else ""
            self._set_meta(t, w, lab)
        if os.path.exists(SESSION_FILE):
            for line in open(SESSION_FILE).read().splitlines():
                line = line.strip()
                if line and line not in self.sessions:
                    self.sessions.append(line)
                    self._set_meta(line, 1, "")
        if os.path.exists(ACCOUNTS_FILE):
            try:
                for a in json.load(open(ACCOUNTS_FILE)):
                    t = a.get("token")
                    if not t:
                        continue
                    if t not in self.sessions:
                        self.sessions.append(t)
                    self._set_meta(t, a.get("weight", 1), a.get("label", ""))
            except Exception:
                pass
        if os.path.exists(CONFIG_FILE):
            try:
                c = json.load(open(CONFIG_FILE))
                self.default_model = c.get("default_model", self.default_model)
                for t in c.get("sessions", []):
                    if t and t not in self.sessions:
                        self.sessions.append(t)
                for t, m in (c.get("meta") or {}).items():
                    self._set_meta(t, m.get("weight", 1), m.get("label", ""))
                for t in c.get("auth_tokens", []):
                    if t not in self.auth_tokens:
                        self.auth_tokens.append(t)
            except Exception:
                pass

    def save(self):
        with self.lock:
            json.dump({"default_model": self.default_model,
                       "sessions": self.sessions,
                       "meta": self.meta,
                       "auth_tokens": self.auth_tokens},
                      open(CONFIG_FILE, "w"), indent=2)
            if os.name != "nt":
                os.chmod(CONFIG_FILE, 0o600)

    def account_specs(self):
        specs = []
        for t in self.sessions:
            m = self.meta.get(t, {})
            specs.append({"token": t, "weight": m.get("weight", 1.0),
                          "label": m.get("label", "")})
        return specs

    def add_account(self, token, weight=1.0, label=""):
        with self.lock:
            if token in self.sessions:
                return False
            self.sessions.append(token)
            self._set_meta(token, weight, label)
            self._rewrite_session_file()
            self.save()
            return True

    def remove_accounts(self, tokens):
        with self.lock:
            self.sessions = [s for s in self.sessions if s not in tokens]
            for t in tokens:
                self.meta.pop(t, None)
            self._rewrite_session_file()
            self.save()

    def _rewrite_session_file(self):
        try:
            with open(SESSION_FILE, "w") as f:
                f.write("".join(t + "\n" for t in self.sessions))
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass


CFG = Config()
POOL = Pool(CFG.account_specs())


def pool_bootstrap():
    """启动时后台预热：换 JWT + 拉额度，失败不阻塞启动。"""
    def run():
        for rec in POOL.healthcheck(force=True):
            sys.stderr.write("[pool] %s user=%s plan=%s usage=%s ok=%s\n" % (
                rec["label"], (rec.get("user_id") or "-")[:8], rec.get("plan"),
                rec.get("usage_percent"), rec.get("healthy")))
    threading.Thread(target=run, daemon=True).start()


# ----------------------------------------------------------------- prompt shaping
def flatten_messages(messages):
    system = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]
    if not turns:
        return "\n".join(system)
    if len(turns) == 1 and not system:
        return _as_text(turns[0].get("content"))
    buf = []
    if system:
        buf.append("[系统指令]\n" + "\n\n".join(system))
    if len(turns) > 1:
        buf.append("[历史对话]")
        for m in turns[:-1]:
            role = "用户" if m.get("role") == "user" else "助手"
            buf.append(role + "：" + _as_text(m.get("content")))
        buf.append("[当前消息]")
    buf.append(_as_text(turns[-1].get("content")))
    return "\n".join(buf)


def _as_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return "" if c is None else str(c)


def anthropic_to_openai(body):
    msgs = []
    if body.get("system"):
        msgs.append({"role": "system", "content": _as_text(body.get("system"))})
    for m in body.get("messages") or []:
        role = "assistant" if m.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": _as_text(m.get("content"))})
    return msgs


# ----------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "today-proxy/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _err(self, code, msg, etype="invalid_request_error"):
        self._json(code, {"error": {"message": str(msg), "type": etype, "code": code}})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            raise TodayError("invalid JSON body", 400)

    def _token(self):
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return self.headers.get("x-api-key", "").strip()

    def _authorized(self):
        if not CFG.auth_tokens:
            return True
        return self._token() in CFG.auth_tokens

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, anthropic-version")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path).path
        if u in ("/health", "/healthz"):
            pool = POOL.snapshot()
            healthy = [a for a in pool if a["healthy"]]
            return self._json(200, {
                "ok": bool(healthy), "accounts": len(pool), "healthy": len(healthy),
                "auth_required": bool(CFG.auth_tokens),
                "pool": [{"label": a["label"], "healthy": a["healthy"],
                          "usage": a["usage_percent"], "cooldown": a["cooldown"],
                          "inflight": a["inflight"]} for a in pool],
            })
        if u == "/v1/models":
            data = []
            for oid, short in MODEL_OPTIONS.items():
                data.append({"id": short, "object": "model", "owned_by": "today"})
                data.append({"id": oid, "object": "model", "owned_by": "today"})
            return self._json(200, {"object": "list", "data": data})
        if u == "/ui":
            return self._html()
        if u == "/ui/state":
            return self._json(200, self._ui_state())
        if u == "/ui/pool":
            return self._json(200, {"accounts": POOL.snapshot(),
                                    "concurrency": __import__("pool").LANE_CONCURRENCY,
                                    "max_usage": __import__("pool").MAX_USAGE})
        self._err(404, "not found", "not_found")

    def _ui_state(self):
        pool = POOL.snapshot()
        healthy = [a for a in pool if a["healthy"]]
        return {
            "accounts": len(pool), "healthy": len(healthy),
            "default_model": CFG.default_model,
            "pool": pool,
            "models": [{"id": k, "title": MODEL_OPTIONS[k]} for k in MODEL_OPTIONS],
            "selected": next((a["model"] for a in pool if a.get("model")), None),
        }

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/ui/login/send":
                st, out = send_otp(self._body().get("phone", ""))
                return self._json(st, {"raw": out[:300]})
            if path == "/ui/login/verify":
                b = self._body()
                tok, user = verify_otp(b.get("phone", ""), b.get("code", ""))
                if not tok:
                    return self._err(400, user)
                added = CFG.add_account(tok, float(b.get("weight") or 1), b.get("label") or "")
                POOL.add(tok, float(b.get("weight") or 1), b.get("label") or "")
                threading.Thread(target=POOL.healthcheck, kwargs={"force": True}, daemon=True).start()
                return self._json(200, {"ok": True, "added": added, "user": user})
            if path == "/ui/pool/add":
                b = self._body()
                tok = (b.get("token") or "").strip()
                if not tok:
                    return self._err(400, "token required")
                w = float(b.get("weight") or 1)
                CFG.add_account(tok, w, b.get("label") or "")
                POOL.add(tok, w, b.get("label") or "")
                threading.Thread(target=POOL.healthcheck, kwargs={"force": True}, daemon=True).start()
                return self._json(200, {"ok": True, "accounts": len(POOL)})
            if path == "/ui/pool/remove":
                key = (self._body().get("key") or "").strip()
                if not key:
                    return self._err(400, "key required")
                removed = POOL.remove(key)
                if removed:
                    CFG.remove_accounts(removed)
                return self._json(200, {"ok": bool(removed), "removed": len(removed),
                                        "accounts": len(POOL)})
            if path == "/ui/pool/refresh":
                recs = POOL.healthcheck(force=True)
                return self._json(200, {"accounts": recs})

            if not self._authorized():
                return self._err(401, "invalid api key", "authentication_error")
            if path == "/v1/chat/completions":
                return self._chat(self._body())
            if path in ("/v1/messages", "/v1/complete"):
                return self._anthropic(self._body(), legacy=(path == "/v1/complete"))
            return self._err(404, "not found: " + path, "not_found")
        except NoAccount as e:
            self._err(503, str(e), "insufficient_capacity")
        except TodayError as e:
            code = e.status if getattr(e, "status", None) and 400 <= e.status < 600 else 502
            self._err(code, str(e) + (": " + e.body if getattr(e, "body", "") else ""), "upstream_error")
        except Exception as e:
            self.log_message("ERROR %s", traceback.format_exc().splitlines()[-1])
            self._err(500, "%s: %s" % (type(e).__name__, e))

    # ---------------- chat ----------------
    def _ask(self, messages, model, timeout):
        text = flatten_messages(messages)
        if not text.strip():
            raise TodayError("empty prompt", 400)
        return run_with_pool(POOL, model or CFG.default_model, text, timeout)

    def _chat(self, body):
        model = body.get("model") or CFG.default_model
        stream = bool(body.get("stream"))
        timeout = int(body.get("timeout") or CFG.reply_timeout)
        acct, tid, reply, meta = self._ask(body.get("messages") or [], model, timeout)
        cid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        usage = {"prompt_tokens": 0, "completion_tokens": len(reply),
                 "total_tokens": len(reply)}
        head = {"id": cid, "created": created, "model": model, "thread_id": tid,
                "account": acct.label}
        if not stream:
            return self._json(200, dict(head, object="chat.completion", usage=usage,
                choices=[{"index": 0, "message": {"role": "assistant", "content": reply},
                          "finish_reason": "stop"}]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def chunk(delta, finish=None):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [{"index": 0, "delta": delta,
                                                    "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            chunk({"content": reply})
            chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _anthropic(self, body, legacy=False):
        model = body.get("model") or CFG.default_model
        timeout = int(body.get("timeout") or CFG.reply_timeout)
        if legacy:
            msgs = [{"role": "user", "content": body.get("prompt") or ""}]
        else:
            msgs = anthropic_to_openai(body)
        acct, tid, reply, meta = self._ask(msgs, model, timeout)
        mid = "msg_" + uuid.uuid4().hex[:24]
        if legacy:
            return self._json(200, {"completion": "\n" + reply, "stop_reason": "stop", "model": model})
        result = {
            "id": mid, "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": reply}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": len(reply)},
            "thread_id": tid, "account": acct.label,
        }
        if not body.get("stream"):
            return self._json(200, result)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def ev(name, obj):
            self.wfile.write(("event: %s\ndata: %s\n\n"
                              % (name, json.dumps(obj, ensure_ascii=False))).encode())
            self.wfile.flush()

        try:
            ev("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})
            ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": reply}})
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            ev("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": len(reply)}})
            ev("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self):
        page = open(os.path.join(ROOT, "ui.html"), "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def main():
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8770"))
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("today-proxy on http://%s:%d  accounts=%d  auth=%s\n"
                     % (host, port, len(POOL), "on" if CFG.auth_tokens else "off"))
    pool_bootstrap()
    srv.serve_forever()


if __name__ == "__main__":
    main()
