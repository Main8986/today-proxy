#!/usr/bin/env bash
# today-proxy 一键部署（Ubuntu / Debian / CentOS / Alpine）
#   sudo bash install.sh
#   sudo bash install.sh --uninstall
set -euo pipefail

APP_DIR="/opt/today-proxy"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/etc/today-proxy.env"
UNIT="/etc/systemd/system/today-proxy.service"
PORT="${PORT:-8770}"

c_g="\033[32m"; c_y="\033[33m"; c_r="\033[31m"; c_0="\033[0m"
say() { printf "${c_g}==>${c_0} %s\n" "$*"; }
warn() { printf "${c_y}!! ${c_0}%s\n" "$*"; }
die() { printf "${c_r}xx ${c_0}%s\n" "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请用 root 跑：sudo bash install.sh"

if [ "${1:-}" = "--uninstall" ]; then
  say "停止并移除服务"
  systemctl disable --now today-proxy 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  warn "保留 $APP_DIR 与 $ENV_FILE（含 session token），要彻底删：rm -rf $APP_DIR $ENV_FILE"
  exit 0
fi

# ---------- 依赖 ----------
say "检查 python3"
if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq python3
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3
  elif command -v yum >/dev/null 2>&1; then yum install -y -q python3
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3
  else die "没找到包管理器，手动装 python3 >= 3.8"; fi
fi
python3 - <<'PY' || die "需要 Python >= 3.8"
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
say "python3 $(python3 -c 'import platform;print(platform.python_version())') 可用（纯标准库，无 pip 依赖）"

# ---------- 代码 ----------
say "安装代码到 $APP_DIR"
mkdir -p "$APP_DIR/data"
for f in core.py pool.py server.py ui.html login.py README.md; do
  [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$APP_DIR/$f"
done
if [ -f "$SRC_DIR/data/session.txt" ] && [ ! -s "$APP_DIR/data/session.txt" ]; then
  cp -f "$SRC_DIR/data/session.txt" "$APP_DIR/data/session.txt"
fi
chmod 600 "$APP_DIR/data"/* 2>/dev/null || true

# ---------- 配置 ----------
if [ ! -f "$ENV_FILE" ]; then
  say "生成 $ENV_FILE（随机 API Key）"
  KEY="sk-today-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "$ENV_FILE" <<EOF
# today-proxy 环境配置
LISTEN_HOST=0.0.0.0
LISTEN_PORT=$PORT
# 客户端用的 API Key，多个用逗号分隔
AUTH_TOKENS=$KEY
# Today session token（可选，多个逗号分隔）；更推荐用 login.py 写 data/session.txt
# 号池多个账号：TODAY_SESSION=tok1,tok2,tok3
TODAY_SESSION=
# 号池权重，跟 TODAY_SESSION 一一对应，不填默认 1
POOL_WEIGHTS=
# 号池标签（控制台显示名），逗号分隔，可空
POOL_LABELS=
# 单账号单模型并发上限
LANE_CONCURRENCY=1
# 额度用到百分之几就停止派单
MAX_USAGE_PERCENT=98
# 无号可用时最多排队多少秒（0=立刻 503）
QUEUE_WAIT=120
DEFAULT_MODEL=deepseek
REPLY_TIMEOUT=180
EOF
  chmod 600 "$ENV_FILE"
  NEWKEY="$KEY"
else
  say "$ENV_FILE 已存在，保留原配置"
  NEWKEY="$(grep -E '^AUTH_TOKENS=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
fi

# ---------- systemd ----------
if command -v systemctl >/dev/null 2>&1; then
  say "安装 systemd 服务"
  cp -f "$SRC_DIR/today-proxy.service" "$UNIT"
  sed -i "s#WorkingDirectory=.*#WorkingDirectory=$APP_DIR#; s#ExecStart=.*#ExecStart=$(command -v python3) $APP_DIR/server.py#" "$UNIT"
  systemctl daemon-reload
  systemctl enable --now today-proxy
  sleep 2
  systemctl --no-pager --lines=0 status today-proxy | head -5 || true
else
  warn "没有 systemd，用 nohup 起（重启后不会自动拉起）"
  (cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && \
    nohup python3 server.py > /var/log/today-proxy.log 2>&1 &)
fi

# ---------- 自检 ----------
sleep 1
say "健康检查 http://127.0.0.1:$PORT/health"
if command -v curl >/dev/null 2>&1; then
  curl -s -m 8 "http://127.0.0.1:$PORT/health" || warn "服务还没起来，看 journalctl -u today-proxy -n 50"
else
  python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$PORT/health',timeout=8).read().decode())" || true
fi

cat <<EOF

${c_g}部署完成${c_0}

  API Key   : ${NEWKEY:-（见 $ENV_FILE）}
  服务地址  : http://<本机IP>:$PORT
  OpenAI    : http://<本机IP>:$PORT/v1
  Anthropic : http://<本机IP>:$PORT
  控制台    : http://<本机IP>:$PORT/ui

下一步（必须做，否则没有模型可用）：

  1) 登录 Today 账号
       cd $APP_DIR && python3 login.py +8613800000000
       cd $APP_DIR && python3 login.py +8613800000000 <验证码>
  2) 重启服务
       systemctl restart today-proxy
  3) 或直接浏览器打开 /ui 在网页里收码登录

日志：journalctl -u today-proxy -f   或   tail -f /var/log/today-proxy.log

EOF
