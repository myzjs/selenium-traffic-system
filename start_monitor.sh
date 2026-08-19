#!/bin/bash
# =============================================================================
#  🚦 start_monitor.sh  ——  VPS 一键启动 + 守护 + 健康检查 + 告警
#  版本: 26.8.13.7   项目: selenium_traffic_system（广告联盟风控演练）
#
#  作用:
#    1) 自动探测 nginx access.log 路径（宝塔/标准/自定义 3 套 fallback）
#    2) 用 supervisord 守护 python3 app.py（Flask + Selenium 流量 + 监控3合1）
#    3) 没装 supervisor 时 fallback 到 nohup（自带看门狗 5s 重启）
#    4) 2 秒起健康检查 /monitoring/api/status ；CRIT 告警实时 tail
#    5) 版本号：自动 export APP_VERSION=26.8.13.7（规则三：YY.M.D.N）
#
#  使用:
#     chmod +x start_monitor.sh
#     ./start_monitor.sh              # 后台启动
#     ./start_monitor.sh logs         # 实时 tail -F 告警日志
#     ./start_monitor.sh stop         # 停止所有进程
#     ./start_monitor.sh status       # 健康检查 + supervisor/nohup 状态
#     ./start_monitor.sh htscore      # 查看 HilltopAds 8 项评分
# =============================================================================
set -u

APP_VERSION="26.8.13.7"
export APP_VERSION

# ===== 路径: 永远基于脚本所在目录（不依赖 CWD） =====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PID_FILE="$SCRIPT_DIR/.app.pid"
NOHUP_LOG="$SCRIPT_DIR/nohup_monitor.out"
SUPERVISOR_CONF="$SCRIPT_DIR/selenium_traffic.supervisor.conf"

# ===== 1) 自动探测 nginx access.log（宝塔 / 标准 / 自定义 环境变量 都试） =====
detect_nginx_log() {
  if [ -n "${NGINX_ACCESS_LOG:-}" ] && [ -f "$NGINX_ACCESS_LOG" ]; then
    echo "$NGINX_ACCESS_LOG"; return 0
  fi
  # 宝塔: /www/wwwlogs/ 下按修改时间最新的 .log（通常是 <domain>.log）
  local bt
  bt="$(ls -t /www/wwwlogs/*.log 2>/dev/null | head -1)"
  if [ -n "$bt" ] && [ -f "$bt" ]; then echo "$bt"; return 0; fi
  for cand in /var/log/nginx/access.log /www/server/nginx/logs/access.log; do
    if [ -f "$cand" ]; then echo "$cand"; return 0; fi
  done
  echo ""
}

NGINX_LOG="$(detect_nginx_log)"
export NGINX_ACCESS_LOG="$NGINX_LOG"

echo "====== 🚦 Traffic Monitor Launcher v$APP_VERSION ======"
echo "  SCRIPT_DIR=$SCRIPT_DIR"
echo "  NGINX_ACCESS_LOG=$NGINX_LOG (传 app.py 自动探测)"
echo "  APP_LOG=$SCRIPT_DIR/app.log (若不可写则自动回退 ~/.cache/traffic_monitor/app.log)"
echo

# ===== 2) 动作分发 =====
action="${1:-start}"
case "$action" in
# ------- start -------
start)
  # 清理旧 pid
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "[已有进程] PID=$OLD_PID 仍在运行，先执行 stop 清理"
      "$0" stop >/dev/null 2>&1
      sleep 1
    fi
    rm -f "$PID_FILE"
  fi

  # ---- 优先用 supervisor（VPS 长期运行推荐） ----
  if command -v supervisorctl >/dev/null 2>&1 && command -v supervisord >/dev/null 2>&1; then
    cat > "$SUPERVISOR_CONF" <<EOF
; supervisor 4.2+ config —— Traffic Monitor + Flask + Selenium Worker (v$APP_VERSION)
; 安装方式:
;   sudo ln -sf "$SUPERVISOR_CONF" /etc/supervisor/conf.d/selenium_traffic.conf
;   sudo supervisorctl reread && sudo supervisorctl update
; 或 直接本脚本启动: supervisord -c "$SUPERVISOR_CONF" -n (前台调试)
[unix_http_server]
file=/tmp/supervisor-selenium.sock

[supervisord]
logfile=$SCRIPT_DIR/supervisord.log
pidfile=$SCRIPT_DIR/.supervisord.pid
childlogdir=$SCRIPT_DIR

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///tmp/supervisor-selenium.sock

[program:selenium_traffic]
command=python3 $SCRIPT_DIR/app.py
directory=$SCRIPT_DIR
environment=
  PATH="$SCRIPT_DIR/.venv/bin:%(ENV_PATH)s",
  PYTHONUNBUFFERED="1",
  NGINX_ACCESS_LOG="$NGINX_LOG",
  APP_VERSION="$APP_VERSION",
  FLASK_PORT="${FLASK_PORT:-5000}"
user=$(whoami)
autostart=true
autorestart=true
startsecs=3
startretries=9999
stopasgroup=true
killasgroup=true
stopsignal=TERM
stopwaitsecs=15
stdout_logfile=$SCRIPT_DIR/app.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
stderr_logfile=$SCRIPT_DIR/app.err.log
stderr_logfile_maxbytes=20MB
stderr_logfile_backups=3
EOF
    # 优先已安装全局 supervisor（有 sudo 权限的 root 用户），否则 supervisord -c 本地启动
    if [ -w /etc/supervisor/conf.d/ ] 2>/dev/null; then
      ln -sf "$SUPERVISOR_CONF" /etc/supervisor/conf.d/selenium_traffic.conf 2>/dev/null || true
      if [ "$(command -v sudo 2>/dev/null)" ]; then
        sudo supervisorctl reread 2>/dev/null || true
        sudo supervisorctl update 2>/dev/null || true
        sudo supervisorctl restart selenium_traffic 2>/dev/null || true
        SUPERVISED=$(sudo supervisorctl status selenium_traffic 2>/dev/null | awk '{print $2}')
        if [ "$SUPERVISED" = "RUNNING" ]; then
          echo "✅ [supervisor global] selenium_traffic RUNNING"
        fi
      fi
    fi
    if ! (command -v sudo >/dev/null && sudo supervisorctl status selenium_traffic 2>/dev/null | grep -q RUNNING); then
      # Fallback: 启动本地 supervisord 实例（不依赖 /etc 写权限）
      supervisord -c "$SUPERVISOR_CONF"
      sleep 2
      LOCAL_SUP_PID=$(pgrep -f "supervisord -c $SUPERVISOR_CONF" 2>/dev/null | head -1 || true)
      if [ -n "${LOCAL_SUP_PID:-}" ]; then
        echo "✅ [supervisor local] 启动成功 PID=$LOCAL_SUP_PID，conf=$SUPERVISOR_CONF"
      fi
    fi
  fi

  # ---- supervisor 没装上时: nohup + 看门狗 loop（保证崩溃 5s 自启） ----
  if ! pgrep -f "python3 $SCRIPT_DIR/app.py" >/dev/null 2>&1; then
    echo "⚠️  未检测到运行中的 python3 app.py，fallback 到 nohup + 看门狗"
    cat > "$SCRIPT_DIR/.app_watchdog.sh" <<WDEOF
#!/bin/bash
# watchdog: python3 app.py 异常退出 → 5s 自启（永久循环）
cd "$SCRIPT_DIR"
export NGINX_ACCESS_LOG="$NGINX_LOG"
export APP_VERSION="$APP_VERSION"
while true; do
  echo "[watchdog] \$(date '+%F %T') 启动 python3 $SCRIPT_DIR/app.py" >> "$NOHUP_LOG"
  python3 "$SCRIPT_DIR/app.py" >> "$NOHUP_LOG" 2>&1
  EXIT_CODE=\$?
  echo "[watchdog] \$(date '+%F %T') 退出 code=\$EXIT_CODE → 5s 后重启" >> "$NOHUP_LOG"
  sleep 5
done
WDEOF
    chmod +x "$SCRIPT_DIR/.app_watchdog.sh"
    nohup "$SCRIPT_DIR/.app_watchdog.sh" >/dev/null 2>&1 &
    WD_PID=$!
    # 记录 watchdog pid（stop 时一起杀）
    echo "$WD_PID" > "$PID_FILE.wd"
    # 等 4s 看 python3 app.py 有没有被 watchdog 拉起
    sleep 4
  fi

  # ---- 无论哪种启动方式：最终都应该有 python3 app.py 进程；记录主 pid ----
  MAIN_PID=""
  for _ in 1 2 3 4 5; do
    MAIN_PID=$(pgrep -f "python3 $SCRIPT_DIR/app.py" 2>/dev/null | head -1 || true)
    [ -n "${MAIN_PID:-}" ] && break
    sleep 2
  done
  if [ -n "${MAIN_PID:-}" ]; then
    echo "$MAIN_PID" > "$PID_FILE"
    echo "✅ 主进程 python3 app.py 已启动 PID=$MAIN_PID (见 $PID_FILE)"
  else
    echo "❌ 启动失败：5 次重试后找不到 python3 app.py 进程"
    echo "   请查看 $NOHUP_LOG 或 $SCRIPT_DIR/app.log 或 $SCRIPT_DIR/supervisord.log"
    exit 2
  fi

  # ---- 3) 健康检查：/monitoring/api/status 必须 200 ----
  PORT="${FLASK_PORT:-5000}"
  HEALTH_URL="http://127.0.0.1:${PORT}/monitoring/api/status"
  HT_URL="http://127.0.0.1:${PORT}/monitoring/api/hilltopads-score"
  for i in $(seq 1 20); do
    sleep 2
    HTTP_CODE=$(curl -s -o /tmp/.mon_health.body -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null || echo 000)
    if [ "$HTTP_CODE" = "200" ]; then
      echo "✅ [第${i}次] 健康检查 $HEALTH_URL → HTTP 200, body=$(cat /tmp/.mon_health.body 2>/dev/null | tr -d '\n' | cut -c1-140)"
      break
    fi
    echo "⏳ [第${i}/20次] 等待监控启动: $HEALTH_URL → HTTP $HTTP_CODE"
    if [ "$i" = "20" ]; then
      echo "❌ 20次健康检查均失败，请查看 nohup/app.log:"
      echo "   tail -n 80 $SCRIPT_DIR/app.log"
      echo "   tail -n 80 $NOHUP_LOG"
      exit 3
    fi
  done

  # ---- 4) 打印 Dashboard URL + HilltopAds 实时评分 ----
  echo
  echo "========================================================================"
  echo "  📊 Dashboard 面板       : http://<YOUR_VPS_IP>:${PORT}/monitoring/"
  echo "  📊 HT 8 项评分 JSON     : $HT_URL"
  echo "  📊 最近 50 事件 JSON    : http://127.0.0.1:${PORT}/monitoring/api/events?limit=50"
  echo "  📊 实时 SSE 流 (告警)   : http://127.0.0.1:${PORT}/monitoring/stream"
  echo "  📄 事件持久化(CRIT/WARN): $SCRIPT_DIR/monitor/rt_events.jsonl  (或 ~/.cache/traffic_monitor/rt_events.jsonl)"
  echo
  HT_BODY=$(curl -s --max-time 5 "$HT_URL" 2>/dev/null || true)
  if [ -n "$HT_BODY" ]; then
    SCORE=$(python3 -c "import sys,json;d=json.loads('''$HT_BODY''').get('data',{});print(d.get('score_0_100','?'))" 2>/dev/null || echo "?")
    VERDICT=$(python3 -c "import sys,json;d=json.loads('''$HT_BODY''').get('data',{});print(d.get('verdict','?'))" 2>/dev/null || echo "?")
    echo "  🎯 HilltopAds 当前评分  : $SCORE/100 → $VERDICT"
  fi
  echo "========================================================================"
  echo
  echo "提示：查看实时 CRIT 告警 →  ./start_monitor.sh logs"
  ;;

# ------- logs: 实时 tail CRIT/WARN -------
logs)
  echo "📋 CRIT/WARN 实时流 (Ctrl-C 退出)"
  echo "   events jsonl: $SCRIPT_DIR/monitor/rt_events.jsonl (~/.cache 回退时自动重选)"
  if [ -f "$SCRIPT_DIR/monitor/rt_events.jsonl" ]; then
    tail -F "$SCRIPT_DIR/monitor/rt_events.jsonl"
  elif [ -f "$HOME/.cache/traffic_monitor/rt_events.jsonl" ]; then
    tail -F "$HOME/.cache/traffic_monitor/rt_events.jsonl"
  else
    echo "⚠️  还没生成事件文件（启动后首次命中规则才会写入），先 tail app.log + nohup_monitor.out："
    tail -F "$SCRIPT_DIR/app.log" "$NOHUP_LOG" 2>/dev/null
  fi
  ;;

# ------- stop: clean kill -------
stop)
  echo "🛑 停止 watchdog + supervisord + python3 app.py + chrome/chromedriver (仅孤儿组)..."
  # watchdog pid
  if [ -f "$PID_FILE.wd" ]; then kill -TERM "$(cat "$PID_FILE.wd")" 2>/dev/null || true; rm -f "$PID_FILE.wd"; fi
  # local supervisord
  if [ -f "$SCRIPT_DIR/.supervisord.pid" ]; then
    kill -TERM "$(cat "$SCRIPT_DIR/.supervisord.pid")" 2>/dev/null || true
    rm -f "$SCRIPT_DIR/.supervisord.pid"
  fi
  # global supervisor
  command -v sudo >/dev/null && sudo supervisorctl stop selenium_traffic 2>/dev/null || true
  # 主进程 + 组
  if [ -f "$PID_FILE" ]; then
    MAIN_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "${MAIN_PID:-}" ]; then
      pkill -TERM -P "$MAIN_PID" 2>/dev/null || true
      kill -TERM "$MAIN_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # 兜底 pgrep
  pgrep -f "python3 $SCRIPT_DIR/app.py" 2>/dev/null | xargs -r kill -TERM 2>/dev/null || true
  pgrep -f "$SCRIPT_DIR/.app_watchdog.sh" 2>/dev/null | xargs -r kill -TERM 2>/dev/null || true
  pgrep -f "supervisord -c $SUPERVISOR_CONF" 2>/dev/null | xargs -r kill -TERM 2>/dev/null || true
  sleep 2
  # 强制剩余
  pgrep -f "python3 $SCRIPT_DIR/app.py" 2>/dev/null | xargs -r kill -KILL 2>/dev/null || true
  pgrep -f "supervisord -c $SUPERVISOR_CONF" 2>/dev/null | xargs -r kill -KILL 2>/dev/null || true
  # 清理 chromedriver 僵尸（父进程已死）
  pgrep -P 1 chromedriver 2>/dev/null | xargs -r kill -KILL 2>/dev/null || true
  echo "✅ 已清理"
  ;;

# ------- status -------
status)
  echo "--- 进程状态 ---"
  (pgrep -af "python3 $SCRIPT_DIR/app.py" 2>/dev/null | sed 's/^/   /') || echo "   (python3 app.py 未运行)"
  (pgrep -af "$SCRIPT_DIR/.app_watchdog.sh" 2>/dev/null | sed 's/^/   /') || true
  (pgrep -af "supervisord -c $SUPERVISOR_CONF" 2>/dev/null | sed 's/^/   /') || true
  if command -v sudo >/dev/null; then sudo supervisorctl status selenium_traffic 2>/dev/null | sed 's/^/   /' || true; fi
  echo
  PORT="${FLASK_PORT:-5000}"
  echo "--- HTTP 健康 (port=$PORT) ---"
  for pair in "/monitoring/api/status:健康" "/monitoring/api/hilltopads-score:HilltopAds评分" "/monitoring/api/events?limit=3:最近事件"; do
    P="${pair%%:*}"; L="${pair##*:}"
    HC=$(curl -s -w "\nHTTP%{http_code} B%{size_download}" --max-time 5 "http://127.0.0.1:${PORT}$P" 2>/dev/null || echo "ERR")
    echo "   $L [$P] → $(echo "$HC" | tail -1) body_sample=$(echo "$HC" | head -1 | cut -c1-140)"
  done
  ;;

# ------- htscore -------
htscore)
  PORT="${FLASK_PORT:-5000}"
  curl -s --max-time 8 "http://127.0.0.1:${PORT}/monitoring/api/hilltopads-score" | python3 -m json.tool 2>/dev/null || curl -s --max-time 8 "http://127.0.0.1:${PORT}/monitoring/api/hilltopads-score"
  echo
  ;;

# ------- unknown -------
*)
  echo "用法: $0 {start|stop|status|logs|htscore}"
  echo "  start   - 启动并守护监控+Flask+Selenium (优先 supervisor，否则 nohup+看门狗)"
  echo "  stop    - 安全停止所有相关进程 + chromedriver 孤儿"
  echo "  status  - 健康检查（进程 + 4 个 HTTP 接口）"
  echo "  logs    - 实时 tail CRIT/WARN 告警 (rt_events.jsonl)"
  echo "  htscore - 查看当前 HilltopAds 8 项评分 JSON"
  exit 1
  ;;
esac
