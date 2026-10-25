"""账号池：多账号负载均衡 + 额度感知调度 + 凭证逻辑分区。

为什么需要「分区」：
    Today 的单会话 conversationId 固定为 userId，且模型选择是**账号级**状态
    （PUT /v1/pricing/model-option 改的是整个账号的当前模型）。
    所以同一个账号不能同时跑 deepseek 和 glm —— 否则 A 请求切到 glm 时，
    正在等回复的 B 请求（本该是 deepseek）会被串味。
    每个账号维护多个「lane」（按模型分），lane 之间并发上限 = LANE_CONCURRENCY。

调度打分（越小越优先）：
    1. 不在冷却期（cooldown_until > now 直接踢出候选）
    2. 额度没打满（usage_percent >= MAX_USAGE 直接踢出候选）
    3. lane 内并发未满
    4. weighted least-load：inflight / weight
    5. 同分时按「最久没用过」轮转，避免总压同一个号
"""
import json
import os
import threading
import time

from core import TodayClient, TodayError, MODEL_OPTIONS, NAME2OPT

# 一个账号在同一模型上的并发上限（上游是串行的，>1 只会排队）
LANE_CONCURRENCY = int(os.environ.get("LANE_CONCURRENCY", "1"))
# 额度用到这个百分比就停止派单
MAX_USAGE = float(os.environ.get("MAX_USAGE_PERCENT", "98"))
# 401/403 冷却秒数（凭证失效，要人介入）
COOLDOWN_AUTH = int(os.environ.get("COOLDOWN_AUTH", "600"))
# 429/5xx 冷却基数，失败次数翻倍
COOLDOWN_TRANSIENT = int(os.environ.get("COOLDOWN_TRANSIENT", "30"))
COOLDOWN_MAX = int(os.environ.get("COOLDOWN_MAX", "900"))
# 空闲多久刷新一次额度
USAGE_TTL = int(os.environ.get("USAGE_TTL", "120"))
# 尝试多少个账号后放弃
MAX_ATTEMPTS = int(os.environ.get("POOL_ATTEMPTS", "3"))
# 无号可用时最多排队多少秒（0 = 立刻 503）
QUEUE_WAIT = float(os.environ.get("QUEUE_WAIT", "120"))
# 高权重号只是"忙"（lane 被占）时，最多让低权重号等多久（秒）
HOLD_WAIT = float(os.environ.get("HOLD_WAIT", "1.5"))


class NoAccount(TodayError):
    """所有账号都不可用（冷却/额度满/并发满）。"""


class Account:
    def __init__(self, token, weight=1.0, label=""):
        self.token = token
        self.weight = max(0.1, float(weight))
        self.label = label or ("acct_" + token[-6:] if len(token) > 6 else "acct")
        self.client = TodayClient(token)
        self._probe_lock = threading.Lock()
        self._probed_at = 0.0
        self.usage_percent = None
        self.plan = None

    # ---------- 凭据 ----------
    @property
    def user_id(self):
        return self.client.user_id

    def ensure_login(self):
        """换一次 JWT，顺手拿到 user_id。失败会抛 TodayError。"""
        self.client.refresh_jwt()

    # ---------- 额度 ----------
    def refresh_usage(self, force=False):
        now = time.time()
        c = self.client
        with self._probe_lock:
            if not force and now - self._probed_at < USAGE_TTL and c.usage_percent is not None:
                return
            st = c.pricing()
            usage = (st.get("usage") or {})
            pct = usage.get("progress")
            if pct is None:
                txt = usage.get("progressText") or ""
                num = "".join(ch for ch in txt.split("剩余")[-1] if ch.isdigit() or ch == ".")
                if num:
                    try:
                        pct = 100.0 - float(num)
                    except ValueError:
                        pct = None
            c.usage_percent = float(pct) if pct is not None else None
            c.plan = st.get("plan")
            c.model = (st.get("modelOptions") or {}).get("selectedOptionId")
            self._probed_at = now

    # ---------- 状态 ----------
    def snapshot(self):
        c = self.client
        now = time.time()
        return {
            "label": self.label,
            "user_id": c.user_id,
            "weight": self.weight,
            "plan": c.plan,
            "model": c.model,
            "usage_percent": c.usage_percent,
            "inflight": c.inflight,
            "cooldown": max(0, int(c.cooldown_until - now)),
            "last_error": c.last_error,
            "ok": c.ok_count,
            "err": c.err_count,
            "healthy": c.cooldown_until <= now and not self.usage_exhausted,
        }

    @property
    def usage_exhausted(self):
        return self.client.usage_percent is not None and self.client.usage_percent >= MAX_USAGE

    @property
    def available(self):
        c = self.client
        return (time.time() >= c.cooldown_until
                and not self.usage_exhausted
                and c.inflight < LANE_CONCURRENCY * self.weight)

    # ---------- 故障记账 ----------
    def penalize(self, err):
        c = self.client
        c.err_count += 1
        c.fails += 1
        c.last_error = str(err)[:200]
        status = getattr(err, "status", None)
        if status in (401, 403):
            c.cooldown_until = time.time() + COOLDOWN_AUTH
        else:
            cooldown = min(COOLDOWN_MAX, COOLDOWN_TRANSIENT * (2 ** (c.fails - 1)))
            c.cooldown_until = time.time() + cooldown

    def reward(self):
        c = self.client
        c.ok_count += 1
        c.fails = 0
        c.cooldown_until = 0.0
        c.last_error = ""


class Pool:
    def __init__(self, specs=None):
        """specs: [{"token":..., "weight":1.0, "label":"a"}, ...] 或 ["tok", ...]"""
        self.lock = threading.Lock()
        self.accounts = []
        self._lane_locks = {}      # (account_index, model) -> Lock
        self._add(specs or [])

    # ---------- 管理 ----------
    def _add(self, specs):
        know = {a.token for a in self.accounts}
        for s in specs:
            if isinstance(s, str):
                if s in know:
                    continue
                a = Account(s)
            else:
                tok = s.get("token")
                if not tok or tok in know:
                    continue
                a = Account(tok, s.get("weight", 1.0), s.get("label", ""))
            self.accounts.append(a)
            know.add(a.token)

    def add(self, token, weight=1.0, label=""):
        with self.lock:
            if any(a.token == token for a in self.accounts):
                return None
            a = Account(token, weight, label)
            self.accounts.append(a)
            return a

    def remove(self, key):
        """按 label 或 token 前缀删号，返回被删掉的 token 列表。"""
        removed = []
        with self.lock:
            keep = []
            for a in self.accounts:
                if a.label == key or a.token.startswith(key):
                    removed.append(a.token)
                else:
                    keep.append(a)
            self.accounts = keep
        return removed

    def __len__(self):
        return len(self.accounts)

    # ---------- 调度 ----------
    def pick(self, model, tried=None, wait=0.0):
        """返回 (account, lane_lock)。lane_lock 负责同账号同模型的串行。

        wait > 0 时，若所有账号都忙（冷却/额度满/并发满）会一直等到有号空出来，
        而不是立刻 503 —— 真实用户并发提问不该因为池子被占就失败。

        调度策略 = 加权轮询（smooth weighted round-robin）：
            每个账号维护 current_weight，空闲时 += weight；被选中时 -= total_weight。
            这样权重 1:3 就是严格的 1:3 分单，而不只是"负载低才优先"。
            冷却 / 额度满 / lane 被占的账号不参与加分。
        """
        tried = tried or set()
        deadline = time.time() + max(0.0, wait)
        idx_map = {id(a): i for i, a in enumerate(self.accounts)}
        while True:
            now = time.time()
            # 1) 收集所有可派单的账号
            cands = []
            for i, a in enumerate(self.accounts):
                if a.token in tried:
                    continue
                c = a.client
                if now < c.cooldown_until or a.usage_exhausted:
                    a._cw = 0.0          # 不可用（冷却/额度满）才清零
                    a._cw_frozen = False
                    continue
                lane = (i, model)
                lk = self._lane_locks.get(lane)
                if lk is None:
                    with self.lock:
                        lk = self._lane_locks.setdefault(lane, threading.Lock())
                if lk.locked():
                    # 忙：冻结权重（不清零），忙完后它攒的分还在，下次优先派给它
                    continue
                cands.append((a, lk))

            if cands:
                # 3) 挂起判断：若有更高权重的号只是"lane 被占"（而不是冷却/额度满），
                #    先等它一小会儿，别把单子派给低权重号，否则并发一高权重就被摊平。
                #    最多等 HOLD_WAIT 秒，超时就照常派单，不会饿死低权重号。
                if not tried and wait > 0:
                    busy_max = 0.0
                    for i, a in enumerate(self.accounts):
                        c = a.client
                        if a.token in tried or now < c.cooldown_until or a.usage_exhausted:
                            continue
                        lk = self._lane_locks.get((i, model))
                        if lk is not None and lk.locked():
                            busy_max = max(busy_max, a.weight)
                    idle_max = max(a.weight for a, _ in cands)
                    if busy_max > idle_max:
                        if not hasattr(self, "_hold"):
                            self._hold = {}
                        key = (model, round(busy_max, 3))
                        held = self._hold.get(key, 0.0)
                        if held < HOLD_WAIT and time.time() < deadline:
                            self._hold[key] = held + 0.05
                            time.sleep(0.05)
                            continue
                        self._hold[key] = 0.0

                # 4) smooth WRR：只给可派单的账号加分
                total = 0.0
                for a, _ in cands:
                    total += a.weight
                    a._cw = getattr(a, "_cw", 0.0) + a.weight
                best = max(cands, key=lambda x: (getattr(x[0], "_cw", 0.0), -x[0].client.last_used))
                best[0]._cw = getattr(best[0], "_cw", 0.0) - total
                return best

            if time.time() >= deadline:
                raise NoAccount("no usable account for model=%s (cooldown/exhausted/busy)" % model, 503)
            time.sleep(0.25)

    def healthcheck(self, force=False):
        out = []
        for a in self.accounts:
            rec = a.snapshot()
            try:
                a.ensure_login()
                a.refresh_usage(force=force)
                if a.client.user_id:
                    a.reward()          # 探活成功：清冷却、清失败计数
                rec.update(plan=a.client.plan, usage_percent=a.client.usage_percent,
                           model=a.client.model, user_id=a.client.user_id,
                           healthy=a.available, cooldown=0, last_error="")
            except TodayError as e:
                a.penalize(e)           # 凭证失效 / 网络错误：立刻进冷却，不再派单
                rec.update(last_error=str(e)[:200],
                           cooldown=max(0, int(a.client.cooldown_until - time.time())),
                           healthy=False)
            out.append(rec)
        return out

    def snapshot(self):
        return [a.snapshot() for a in self.accounts]


# ------------------------------------------------------------------ 调用入口
def run_with_pool(pool, model_name, prompt, timeout):
    """在池里跑一次对话，自动换号重试。返回 (account, thread_id, reply, meta)。"""
    opt = NAME2OPT.get(str(model_name).lower()) or NAME2OPT.get("deepseek")
    canon = MODEL_OPTIONS.get(opt, model_name)
    tried = set()
    last_err = None
    deadline = time.time() + timeout
    for _ in range(min(MAX_ATTEMPTS, max(1, len(pool)))):
        acct, lane = pool.pick(canon, tried, wait=QUEUE_WAIT)
        c = acct.client
        acquired = lane.acquire(timeout=min(max(1.0, deadline - time.time()), 300))
        if not acquired:
            tried.add(acct.token)
            continue
        c.inflight += 1
        c.last_used = time.time()
        try:
            if c.model != opt:
                c.set_model(opt)
                c.model = opt
            sent = c.send(prompt)
            tid = sent["threadId"]
            reply, meta = c.wait_reply(tid, timeout=max(1, int(deadline - time.time())))
            acct.reward()
            threading.Thread(target=_safe_usage, args=(acct,), daemon=True).start()
            return acct, tid, reply, meta
        except TodayError as e:
            last_err = e
            acct.penalize(e)
            tried.add(acct.token)
            continue
        finally:
            c.inflight -= 1
            lane.release()
    raise last_err or NoAccount("all accounts failed", 503)


def _safe_usage(acct):
    try:
        acct.refresh_usage(force=True)
    except Exception:
        pass


def load_specs_from_env(env_sessions, file_sessions=None, weights=None, labels=None):
    """把 TODAY_SESSION=a,b,c + POOL_WEIGHTS=1,2,1 拼成 specs。"""
    toks = [t for t in env_sessions if t]
    out = []
    for i, t in enumerate(toks):
        w = 1.0
        if weights and i < len(weights) and weights[i]:
            try:
                w = float(weights[i])
            except ValueError:
                w = 1.0
        lab = labels[i] if labels and i < len(labels) else ""
        out.append({"token": t, "weight": w, "label": lab})
    return out
