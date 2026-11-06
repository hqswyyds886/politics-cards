#!/usr/bin/env bash
# 一键安装 考研政治刷题站。零第三方依赖，只需系统自带 python3。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/politics-cards}"
PORT="${PORT:-8001}"
SVC="politics-cards"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "== 1. 目录 =="
mkdir -p "$APP_DIR/data" "$APP_DIR/static"

echo "== 2. 拷贝源码 =="
install -m 644 "$SRC"/server.py "$APP_DIR"/
install -m 644 "$SRC"/fitness_agent.py "$APP_DIR"/
install -m 644 "$SRC"/nutrition_engine.py "$APP_DIR"/
install -m 644 "$SRC"/dev_agent.py "$APP_DIR"/
install -m 644 "$SRC"/traffic_guard.py "$APP_DIR"/
install -m 644 "$SRC"/static/index.html "$APP_DIR"/static/index.html
# 首次安装才放种子库，避免覆盖线上已有数据
if [ ! -f "$APP_DIR/data/kaoyan.db" ] && [ -f "$SRC/seed/kaoyan.db" ]; then
  install -m 644 "$SRC/seed/kaoyan.db" "$APP_DIR/data/kaoyan.db"
  echo "   种子题库已就位"
fi

echo "== 3. 服务单元 =="
sed "s#/opt/politics-cards#$APP_DIR#g; s#KAOYAN_PORT=8001#KAOYAN_PORT=$PORT#" \
  "$SRC/deploy/politics-cards.service" > /etc/systemd/system/$SVC.service
systemctl daemon-reload

echo "== 4. 防火墙 =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow "$PORT"/tcp >/dev/null 2>&1 || true
  echo "   ufw: $PORT/tcp 已放行"
fi

echo "== 5. 启动 =="
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
sleep 2
systemctl is-active "$SVC"

echo "== 6. 自检 =="
curl -s -o /dev/null -w "   local HTTP %{http_code}\n" "http://127.0.0.1:$PORT/api/meta" || true
echo "完成。访问：http://<服务器IP>:$PORT/"
