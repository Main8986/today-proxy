# today-proxy — Today.app AI 反代（VPS 部署版）

把 Today（`ai.today.android.app.cn`）App 内置的 **DeepSeek V4.1 Flash / GLM 5.3**
包装成 **OpenAI** 和 **Anthropic** 双协议接口，用自己的账号额度供 Minis / OpenWebUI /
LobeChat / Cherry Studio / 任意 SDK 使用。

纯 Python 标准库，**零依赖**，单文件起服务。

```
┌──────────┐   OpenAI /v1/chat/completions   ┌─────────────┐   轮询    ┌──────────────────┐
│ Minis    │ ──────────────────────────────► │ today-proxy │ ────────► │ api.todayai.cn   │
│ OpenWebUI│   Anthropic /v1/messages        │  号池调度   │  202+GET  │ DeepSeek / GLM   │
│ 任意 SDK │ ◄────────────────────────────── │  + SSE      │ ◄──────── │                  │
└──────────┘                                 └─────────────┘           └──────────────────┘
```

**特性**

- OpenAI + Anthropic 双协议，都支持 `stream: true`（SSE）
- 多账号号池：加权轮询、额度感知、故障冷却、并发排队
- 网页控制台：短信登录加号、额度进度条、一键切模型
- 单号 / 多号 / Docker / systemd 都能跑，零第三方依赖

一键部署：

```bash
git clone https://github.com/Main8986/today-proxy.git /opt/today-proxy
cd /opt/today-proxy && sudo bash install.sh
python3 login.py +8613800000000 123456   # 登自己的账号
sudo systemctl restart today-proxy
```

---

## 1. 上游链路（抓包 + 反编译双向确认）

```
① 手机号 + 短信验证码
   POST https://auth.todayai.cn/api/auth/phone-number/send-otp   {"phoneNumber":"+86..."}
   POST https://auth.todayai.cn/api/auth/phone-number/verify     {"phoneNumber":"+86...","code":"700455"}
        → {"status":true,"token":"<session token，32 位>","user":{...},"adTrackingEnabled":true}

② session → JWT（短命，约 115 秒）
   GET  https://auth.todayai.cn/api/auth/token    Authorization: Bearer <session>
        → {"token":"<ES256 JWT>","expiresIn":115}
   JWT claims: {iat,id,email,role,createdAt,sub,exp,iss:auth.todayai.cn,aud:api.todayai.cn}

③ JWT → 业务接口
   GET  https://api.todayai.cn/v1/users/me
   GET  https://api.todayai.cn/v1/pricing/status
   PUT  https://api.todayai.cn/v1/pricing/model-option   {"optionId":"model.glm-5.3"}
   POST https://api.todayai.cn/v1/messages               {"message":"...","clientMessageId":"msg_<32hex>"}
        → 202 {"threadId":"01a1...","messageId":"..."}
   GET  https://api.todayai.cn/v1/conversations/messages?isMainChat=true&limit=50
        → {items:[{role,state,threadId,body.parts[].messageType=="text"...}]}
```

必需请求头（缺 `X-Client-Platform` / `User-Agent` 会被当浏览器拒绝）：

```
Authorization: Bearer <jwt>
X-Client-Platform: android
X-App-Version: 1.23.0
X-Client-Capabilities: chat.browser.v1,connectors.account_management.v1,...
X-Device-Id: d_<rand>        X-User-Id: <jwt.sub>
Origin: https://todayai.cn
User-Agent: Today/1.23.0 Version/1.23.0 Build/2768 Device/(...) NetType/WiFi
```

模型 ID：
| 短名 | 上游 optionId | 额度倍率 |
|---|---|---|
| `deepseek` | `model.deepseek-v4.1-flash` | 1× |
| `glm-5.3` | `model.glm-5.3` | 4× |

> APK 里写死的 `api.today.ai` / `api.t.ai` / `api.todayai.dev` 是测试环境，
> 真实生产域名 `api.todayai.cn` / `auth.todayai.cn` 不在包内，靠抓包拿到。

---

## 2. 部署（Ubuntu/Debian/CentOS 通用，1 核 512M 足够）

```bash
# ① 拿到代码
scp -r today-proxy root@<vps>:/opt/today-proxy      # 或 git clone

# ② 一键安装（建 venv 不需要——纯标准库，只装 systemd 单元）
cd /opt/today-proxy
sudo bash install.sh
```

`install.sh` 干三件事：装 `python3`（如果缺）、随机生成 `AUTH_TOKENS` 写进
`/etc/today-proxy.env`、装并启动 `today-proxy.service`。

```bash
# ③ 登录 Today 账号（VPS 上收短信）
cd /opt/today-proxy
python3 login.py +8613800000000          # 收验证码
python3 login.py +8613800000000 123456   # 写入 data/session.txt

sudo systemctl restart today-proxy
curl localhost:8770/health
# {"ok":true,"sessions":1,"auth_required":true,"user":"GpaK...","model":"model.glm-5.3"}
```

也可以不开终端，直接浏览器开 `http://<vps>:8770/ui` 在网页里发码登录、切模型、看额度。

---

## 3. 客户端接入

**API Key** = `/etc/today-proxy.env` 里的 `AUTH_TOKENS`（逗号分隔可配多个）。

### OpenAI 协议
```bash
curl http://<vps>:8770/v1/chat/completions \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"你好"}],"stream":true}'
```
```python
from openai import OpenAI
c = OpenAI(base_url="http://<vps>:8770/v1", api_key="<AUTH_TOKEN>")
print(c.chat.completions.create(model="deepseek",
      messages=[{"role":"user","content":"你好"}]).choices[0].message.content)
```

### Anthropic 协议（Minis / Claude 客户端 / Cherry Studio）
```
Base URL : http://<vps>:8770
API Key  : <AUTH_TOKEN>
Model    : deepseek  /  glm-5.3
```

### Minis 里怎么填
Settings → Providers → 添加：

| 字段 | 值 |
|---|---|
| 类型 | Anthropic 或 OpenAI 兼容（两者都支持） |
| Base URL | `https://your.domain`（Anthropic）／`https://your.domain/v1`（OpenAI） |
| API Key | `<AUTH_TOKEN>` |
| Model | `deepseek` / `glm-5.3` |

---

## 4. 生产建议

**TLS + 反代**（`server.py` 说 HTTP，别裸奔公网）：

```nginx
server {
  listen 443 ssl http2;
  server_name today.example.com;
  ssl_certificate     /etc/letsencrypt/live/today.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/today.example.com/privkey.pem;

  location / {
    proxy_pass http://127.0.0.1:8770;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;          # SSE 必须关
    proxy_read_timeout 300s;      # 上游轮询最长 180s
  }
}
```
配好 nginx 后把 `LISTEN_HOST` 改成 `127.0.0.1`（`/etc/today-proxy.env`），只留反代对外。

**多账号号池**：三种加号方式，任选

```bash
# ① 环境变量批量加（权重一一对应；不填默认 1）
TODAY_SESSION=tok1,tok2,tok3 POOL_WEIGHTS=1,2,1 POOL_LABELS=主力,备用,小号

# ② 命令行登录，登完自动进池
python3 login.py +8613800000000 123456

# ③ 控制台 http://<vps>:8770/ui 粘贴 token 加号、改权重、移除
```

号池行为：

| 能力 | 说明 |
|---|---|
| 加权轮询 | 权重 1:3 实测约 22%:78%，见下方验证数据 |
| 额度感知 | 某号额度 ≥ `MAX_USAGE_PERCENT`(98%) 自动停派，不会撞死 |
| 故障隔离 | 401/403 冷却 600s；5xx 从 30s 起指数退避，上限 900s |
| 并发排队 | 全忙时最多等 `QUEUE_WAIT`(120s)，不立刻 503 |
| 探活预热 | 启动时后台换 JWT + 拉额度，坏号立刻进冷却 |
| 模型分区 | 同号可同时跑 deepseek / glm 两条 lane，互不串味 |

**这几种情况会被自动跳过**：session token 过期/失效（冷却 600s）、额度打满、
该号该模型正在跑（lane 占用）、连续报错（指数退避）。控制台 `/ui` 能看到每个号的
额度条、在跑数、成功/失败计数、冷却倒计时。

**防火墙**：只开 443。`/ui` 控制台没有独立鉴权（能发短信就能登号），
不想暴露就只绑 `127.0.0.1` 用 SSH 隧道访问。

---

## 5. 配置项（`/etc/today-proxy.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8770` | 监听端口 |
| `AUTH_TOKENS` | 安装时随机 | 客户端 API Key，逗号分隔；为空=不鉴权 |
| `TODAY_SESSION` | 空 | Today session token，逗号分隔多账号；也可用 `data/session.txt` |
| `POOL_WEIGHTS` | 空 | 与 `TODAY_SESSION` 一一对应的权重，如 `1,2,1` |
| `POOL_LABELS` | 空 | 控制台显示的账号名，逗号分隔 |
| `LANE_CONCURRENCY` | `1` | 单账号单模型并发上限（上游串行，>1 只是排队） |
| `MAX_USAGE_PERCENT` | `98` | 额度用到这个百分比就停派该号 |
| `QUEUE_WAIT` | `120` | 无号可用时最多排队秒数，0=立刻 503 |
| `HOLD_WAIT` | `1.5` | 高权重号只是忙时，低权重号最多等多久再顶班 |
| `COOLDOWN_AUTH` | `600` | 401/403 冷却秒数 |
| `COOLDOWN_TRANSIENT` | `30` | 5xx 冷却基数（失败次数翻倍） |
| `USAGE_TTL` | `120` | 额度缓存刷新间隔 |
| `DEFAULT_MODEL` | `deepseek` | 请求未指定 model 时用哪个 |
| `REPLY_TIMEOUT` | `180` | 等待上游回复的秒数 |

---

## 6. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 + 上游账号/模型状态 |
| GET | `/v1/models` | OpenAI 模型列表 |
| POST | `/v1/chat/completions` | OpenAI 对话，支持 `stream:true` (SSE) |
| POST | `/v1/messages` | Anthropic Messages API，支持 SSE |
| POST | `/v1/complete` | Anthropic 旧版 complete |
| GET | `/ui` | 网页控制台（登录/额度/切模型） |
| GET | `/ui/state` | 控制台状态 JSON |
| POST | `/ui/login/send` `/ui/login/verify` `/ui/model` | 控制台动作 |

流式说明：上游 `/v1/messages` 只回 202，正文靠轮询，所以**真正的 token 级流式做不到**。
本服务在拿到整段回复后按 SSE 协议一次性 flush，客户端不会报错、也不会卡住，
但首字延迟 = 上游生成完的时间（通常 2–6 秒）。

---

## 7. 限制（别指望它变成官方 API）

- **非 token 流式**：见上，延迟 2–6s，长回答会更久。
- **单会话串行**：Today 主对话 `conversationId` 固定为 userId，
  并发请求会挤在同一个 assistant 上下文里。已用 lane 串行 + 排队消化，
  **多账号是提升吞吐的唯一手段**：N 个号 ≈ N 路并发。
- **吃自己的 Pro 额度**：`/ui/state` 里有实时剩余百分比，GLM 5.3 是 4× 消耗。
- **JWT 115 秒**：服务内自动刷新，session token 长期有效（登出才失效）。
- **无工具调用 / 无图片**：上游只吃 `{"message": "<纯文本>"}`，
  `tools`、`function_call`、`image_url` 一律会被拍平成文本丢弃。
- 邮件 OTP 端点对内部域账号返回 403，只能手机号登录。

---

## 8. 文件

```
today-proxy/
├── core.py        上游客户端（认证链 / 发送 / 轮询 / 模型切换）
├── pool.py        号池调度（加权轮询 / 额度感知 / 冷却 / 并发排队）
├── server.py      HTTP 服务（OpenAI + Anthropic 双协议 + SSE + 控制台）
├── ui.html        网页控制台（登录 / 号池 / 额度）
├── login.py       手机号验证码登录 CLI
├── install.sh     一键部署（systemd + 随机密钥）
├── today-proxy.service
├── docker-compose.yml / Dockerfile
└── data/                    # 运行时生成，已在 .gitignore 里排除
    ├── session.txt   Today session token（每行一个）
    └── config.json   运行配置（账号权重/标签，权限 600）
```

---

## 9. 免责与声明

本项目仅用于**个人学习与自己账号的接口封装**，把你在 App 里已有的会员额度
接到自己的客户端（Minis / OpenWebUI / LobeChat 等）上使用。

- 不提供任何账号、不绕过任何付费，所有模型调用消耗的都是**你自己账号的额度**。
- 上游接口随时可能变更，本项目不保证长期可用。
- 使用本项目可能违反 Today 的用户协议，账号风险自负。
- 请勿用于公开分发额度、转售、搭建对外服务等商业或滥用场景。

若权利人认为本项目侵犯权益，请提 issue，会立即处理。

---

## 10. 致谢

号池调度思路参考了 nginx 的 smooth weighted round-robin
和 Go 标准库 `net/http/httputil` 的反向代理设计。

