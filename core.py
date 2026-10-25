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
        self.reset_count = None      # 可用重置次数，None = 未查过
        self.onboarding_finished_flag = None   # 新手引导是否完成

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

    def usage_detail(self):
        """完整用量明细：套餐 / 周期 / 各项百分比 / 重置次数。纯只读。"""
        st = self.pricing()
        u = st.get("usage") or {}
        items = []
        for it in (u.get("breakdown") or {}).get("items") or []:
            items.append({
                "key": it.get("key"),
                "title": (it.get("display") or {}).get("title"),
                "percent": it.get("usedPercent"),
                "text": (it.get("display") or {}).get("percentText"),
            })
        return {
            "plan": st.get("plan"),
            "accessPhase": st.get("accessPhase"),
            "periodEnd": st.get("currentPeriodEndAt"),
            "entitlementEndsAt": st.get("entitlementEndsAt"),
            "period": u.get("period"),
            "resetAt": u.get("resetAt"),
            "progress": u.get("progress"),
            "progressText": u.get("progressText"),
            "breakdown": items,
            "resets": self.reset_quota(),
        }

    def reset_quota(self):
        """查询可用的重置次数。GET /v1/pricing/resets 只读，不消耗次数。

        注意：这里**只查询**，绝不调用重置接口 —— 那会真的消耗一次配额，
        必须由用户在界面上明确确认后才执行（见 consume_reset）。
        """
        try:
            st, out = self.api("/v1/pricing/resets")
            if st != 200:
                return {"error": "HTTP %s" % st}
            d = json.loads(out).get("data") or {}
            return {
                "total": d.get("totalCount"),
                "items": [{"id": i.get("resetId"), "expiresAt": i.get("expiresAt")}
                          for i in (d.get("items") or [])],
            }
        except Exception as e:
            return {"error": str(e)[:120]}

    def consume_reset(self, reset_id=None):
        """**会真的消耗一次重置**。调用前必须由用户明确确认。

        实测抓包（2026-10-25）：
            POST /v1/pricing/resets  {"resetId": "<uuid>"}
            → {"data":{"receipt":{"consumedCount":1,"availableCountAfter":2,...}}}
        reset_id 不传就取当前第一个可用的。
        """
        q = self.reset_quota()
        if q.get("error"):
            raise TodayError("query resets failed: " + q["error"], 502)
        items = q.get("items") or []
        if not items:
            raise TodayError("no reset quota available", 409)
        rid = reset_id or items[0]["id"]
        if not any(i["id"] == rid for i in items):
            raise TodayError("resetId %s not in available list" % rid, 400)
        st, out = self.api("/v1/pricing/resets", method="POST",
                           body={"resetId": rid})
        if st != 200:
            raise TodayError("reset failed", st, out[:300])
        d = json.loads(out).get("data") or {}
        rc = d.get("receipt") or {}
        self.usage_percent = 0.0
        self.reset_count = rc.get("availableCountAfter")
        return {"resetId": rid, "consumed": rc.get("consumedCount"),
                "availableAfter": rc.get("availableCountAfter"),
                "resetAt": rc.get("resetAt"),
                "operationId": rc.get("operationId")}

    def onboarding_finished(self):
        """查 /v1/users/me 的 onboarding.finished，这是唯一可靠的判断依据。"""
        st, out = self.api("/v1/users/me")
        if st != 200:
            return None
        d = json.loads(out)
        ob = d.get("onboarding") or {}
        return bool(ob.get("finished"))

    def onboarding_status(self):
        """查引导进度（只读）。返回 branch 步骤列表和每一步是否已完成。"""
        try:
            st, out = self.api("/v1/onboarding/v2/steps/init")
            if st != 200:
                return {"error": "HTTP %s" % st}
            d = json.loads(out).get("data") or {}
            branch = d.get("branch") or {}
            done = d.get("step") or d.get("currentStep")
            return {"branch": branch, "currentStep": done, "raw": d}
        except Exception as e:
            return {"error": str(e)[:150]}

    def onboarding_auto(self):
        """自动走完引导，触发新号赠送的 Pro。

        实测接口（2026-10-25）：
            GET  /v1/onboarding/v2/steps/init     → {branch:{name,steps:[...]}}
            GET  /v1/onboarding/v2/steps/{step}   → 该步需要什么字段
            POST /v1/onboarding/v2/steps/{step}   → 提交
            POST /v1/onboarding/v2/steps/done     → {} 即可，返回 {ok,finished}

        策略：先试直接 done（最小改动）；不行再逐步提交后再 done。
        全程用账号自己已有的数据填，不编造。
        """
        log = []
        # 1) 先看当前状态
        st, out = self.api("/v1/onboarding/v2/steps/init")
        if st != 200:
            raise TodayError("onboarding init failed", st, out[:200])
        branch = (json.loads(out).get("data") or {}).get("branch") or {}
        steps = branch.get("steps") or []
        log.append("branch=%s steps=%s" % (branch.get("name"), steps))

        # 2) 逐步提交（失败不中断，记下来）
        for s in steps:
            try:
                gst, gout = self.api("/v1/onboarding/v2/steps/" + s)
                if gst != 200:
                    log.append("%s: GET %s" % (s, gst))
                    continue
                info = json.loads(gout)
                payload = self._onboarding_payload(s, info)
                pst, pout = self.api("/v1/onboarding/v2/steps/" + s, method="POST",
                                     body=payload)
                log.append("%s: POST %s %s" % (s, pst, pout[:80]))
            except TodayError as e:
                log.append("%s: ERR %s" % (s, e))

        # 3) 收尾
        st, out = self.api("/v1/onboarding/v2/steps/done", method="POST", body={})
        log.append("done: %s %s" % (st, out[:100]))
        return {"ok": st == 200, "log": log, "steps": steps}

    def _onboarding_payload(self, step, info):
        """按步骤类型生成提交内容。

        字段名全部来自 APK 里的 kotlinx.serialization 类（实测 2026-10-25）：
            OnboardingAgentNameRequest(agentName=...)
            OnboardingAgentAvatarRequest(agentAvatarUrl=...)
        用错名字服务端直接 400 Invalid request。
        """
        if step in ("connectors", "connectors-supplemental"):
            # 不授权任何连接器，直接跳过
            return {"connectorIds": [], "skipped": True}
        if step == "agent-name":
            opts = info.get("agentNameOptions") or []
            return {"agentName": opts[0] if opts else "小树"}
        if step == "agent-avatar":
            opts = info.get("avatarOptions") or []
            return {"agentAvatarUrl": opts[0] if opts else ""}
        if step == "memory":
            content = info.get("content")
            return {"content": content} if content else {}
        if step == "user-name":
            return {"name": "用户"}
        if step == "notification":
            return {"enabled": False}
        return {}

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
