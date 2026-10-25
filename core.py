"""Today (ai.today.android.app.cn) backend client — production endpoints.

Auth chain (verified against real traffic):
  session token  ──(GET /api/auth/token)──> ES256 JWT (~115s)  ──(Authorization: Bearer)──> api.todayai.cn
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

AUTH_BASE = "https://auth.todayai.cn"
API_BASE = "https://api.todayai.cn"
APP_VERSION = "1.23.0"
CLIENT_CAPS = ",".join([
    "chat.browser.v1", "connectors.account_management.v1", "chat.brief_card.v1",
    "chat.qa_cardflow_single.v1", "chat.health_metric_card.v1", "chat.automation_card.v1",
    "chat.follow_up_recommendations.v1", "chat.message_reactions.unicode.v1",
])
UA = ("Today/1.23.0 Version/1.23.0 Build/2768 "
      "Device/(OnePlus;OP5D2BL1)(PKG110; Android 15) NetType/WiFi")
ORIGIN = "https://todayai.cn"

MODEL_OPTIONS = {
    "model.deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "model.glm-5.3": "glm-5.3",
}

# 短名 / 长名 -> 上游 optionId
NAME2OPT = {
    "deepseek": "model.deepseek-v4.1-flash",
    "deepseek-v4.1-flash": "model.deepseek-v4.1-flash",
    "model.deepseek-v4.1-flash": "model.deepseek-v4.1-flash",
    "glm": "model.glm-5.3",
    "glm-5.3": "model.glm-5.3",
    "model.glm-5.3": "model.glm-5.3",
}


class TodayError(Exception):
    def __init__(self, msg, status=None, body=""):
        super().__init__(msg)
        self.status = status
        self.body = body


class TodayClient:
    def __init__(self, session_token, device_id=None, user_id=None):
        self.session = session_token
        self.device_id = device_id or ("d_" + uuid.uuid4().hex[:10])
        self.user_id = user_id
        self.jwt = None
        self.jwt_exp = 0
        self._lock = threading.Lock()
        # ---- pool bookkeeping (owned by pool.py) ----
        self.inflight = 0
        self.cooldown_until = 0.0
        self.last_error = ""
        self.fails = 0
        self.ok_count = 0
        self.err_count = 0
        self.usage_percent = None      # 0..100, None = unknown
        self.plan = None
        self.model = None
        self.last_used = 0.0

    # ---------------- raw http ----------------
    def _http(self, url, method="GET", body=None, headers=None, timeout=120):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        base = {
            "X-Client-Platform": "android",
            "X-Timezone": "Asia/Shanghai",
            "Accept-Language": "zh-CN",
            "X-App-Version": APP_VERSION,
            "X-Client-Capabilities": CLIENT_CAPS,
            "X-Device-Id": self.device_id,
            "User-Agent": UA,
            "Origin": ORIGIN,
            "Accept": "application/json",
        }
        if body is not None:
            base["Content-Type"] = "application/json"
        if self.user_id:
            base["X-User-Id"] = self.user_id
        if headers:
            base.update(headers)
        for k, v in base.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf8", "ignore")
        except Exception as e:
            raise TodayError("network: %s: %s" % (type(e).__name__, e))

    # ---------------- auth ----------------
    @staticmethod
    def _jwt_payload(tok):
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))

    def refresh_jwt(self, force=False):
        with self._lock:
            if not force and self.jwt and time.time() < self.jwt_exp - 20:
                return self.jwt
            st, out = self._http(AUTH_BASE + "/api/auth/token",
                                 headers={"Authorization": "Bearer " + self.session}, timeout=30)
            if st != 200:
                raise TodayError("session token exchange failed", st, out[:300])
            d = json.loads(out)
            self.jwt = d["token"]
            self.jwt_exp = time.time() + int(d.get("expiresIn") or 115)
            try:
                payload = self._jwt_payload(self.jwt)
                if not self.user_id:
                    self.user_id = payload.get("sub")
                self.jwt_exp = float(payload.get("exp", self.jwt_exp))
            except Exception:
                pass
            return self.jwt

    def api(self, path, method="GET", body=None, timeout=120, retry=True):
        jwt = self.refresh_jwt()
        st, out = self._http(API_BASE + path, method=method, body=body,
                             headers={"Authorization": "Bearer " + jwt}, timeout=timeout)
        if st == 401 and retry:
            jwt = self.refresh_jwt(force=True)
            st, out = self._http(API_BASE + path, method=method, body=body,
                                 headers={"Authorization": "Bearer " + jwt}, timeout=timeout)
        return st, out

    # ---------------- business ----------------
    def me(self):
        st, out = self.api("/v1/users/me")
        if st != 200:
            raise TodayError("users/me failed", st, out[:300])
        return json.loads(out)

    def pricing(self):
        st, out = self.api("/v1/pricing/status")
        if st != 200:
            raise TodayError("pricing/status failed", st, out[:300])
        return json.loads(out)["data"]["status"]

    def selected_model(self):
        try:
            opts = self.pricing().get("modelOptions") or {}
            return opts.get("selectedOptionId")
        except Exception:
            return None

    def available_models(self):
        try:
            opts = (self.pricing().get("modelOptions") or {}).get("options") or []
            return [{"id": o["id"], "title": o["display"]["title"],
                     "disabled": o.get("disabled", False)} for o in opts]
        except Exception:
            return [{"id": k, "title": v, "disabled": False} for k, v in MODEL_OPTIONS.items()]

    def set_model(self, option_id):
        if option_id not in MODEL_OPTIONS:
            raise TodayError("unknown model option: %s" % option_id, 400)
        if self.selected_model() == option_id:
            return
        st, out = self.api("/v1/pricing/model-option", method="PUT", body={"optionId": option_id})
        if st != 200:
            raise TodayError("model switch failed", st, out[:300])

    def send(self, text, thread_id=None):
        body = {"message": text, "clientMessageId": "msg_" + uuid.uuid4().hex}
        if thread_id:
            body["threadId"] = thread_id
        st, out = self.api("/v1/messages", method="POST", body=body)
        if st not in (200, 201, 202):
            raise TodayError("send failed", st, out[:500])
        return json.loads(out)

    def messages(self, limit=50, after=None):
        q = "/v1/conversations/messages?isMainChat=true&limit=%d" % limit
        if after:
            q += "&after=" + after
        st, out = self.api(q)
        if st != 200:
            raise TodayError("messages list failed", st, out[:300])
        return json.loads(out)["data"]

    def wait_reply(self, thread_id, timeout=180, poll=0.8, on_text=None):
        """Poll until the assistant message of this thread is final. Returns (text, usage_hint)."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            data = self.messages(limit=50)
            for m in data.get("items") or []:
                if m.get("threadId") != thread_id or m.get("role") != "assistant":
                    continue
                txt = message_text(m)
                last = txt or last
                if m.get("state") == "completed":
                    return last, m
                if on_text and txt != last:
                    on_text(txt)
            time.sleep(poll)
        if last:
            return last, None
        raise TodayError("upstream did not answer within %ss" % timeout, 504)


def message_text(msg):
    out = []
    for p in (msg.get("body") or {}).get("parts") or []:
        if p.get("messageType") == "text":
            out.append((p.get("content") or {}).get("text", ""))
    return "".join(out)


# ---------------- password-less login helpers ----------------
def send_otp(phone):
    c = TodayClient("", user_id=None)
    return c._http(AUTH_BASE + "/api/auth/phone-number/send-otp", "POST", {"phoneNumber": phone}, timeout=30)


def verify_otp(phone, code):
    c = TodayClient("", user_id=None)
    st, out = c._http(AUTH_BASE + "/api/auth/phone-number/verify", "POST",
                      {"phoneNumber": phone, "code": code}, timeout=30)
    if st == 200:
        d = json.loads(out)
        return d.get("token"), d.get("user")
    return None, out[:300]
