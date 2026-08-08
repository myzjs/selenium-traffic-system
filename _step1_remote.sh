#!/bin/bash
# 分步对比脚本：东京VPS vs 本地代码

HOST=107.148.2.75
PORT=31141
USER=root
PASS='Zhanjisheng@@7263'
REMOTE_DIR=/root/selenium_traffic_system
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

EXCLUDE_DIRS="__pycache__ .git qa_sessions report test_reports feedback .uploads node_modules trae_feedback"
EXCLUDE_FILES="app.log config.json .env historical_tasks.json ua_usage_history.json fingerprint_stats.json sync_two_servers.py compare_remote.py compare_result.txt"

mkdir -p /tmp/compare_code

# ===== 步骤1：获取远程md5 =====
echo "步骤1: 从东京VPS获取文件MD5..."

EXCLUDE_DIR_ARGS=""
for d in $EXCLUDE_DIRS; do
  EXCLUDE_DIR_ARGS="$EXCLUDE_DIR_ARGS -not -path \"*/$d/*\""
done

EXCLUDE_FILE_ARGS=""
for f in $EXCLUDE_FILES; do
  EXCLUDE_FILE_ARGS="$EXCLUDE_FILE_ARGS -not -name \"$f\""
done

REMOTE_CMD="cd $REMOTE_DIR && find . -type f $EXCLUDE_DIR_ARGS $EXCLUDE_FILE_ARGS -not -name \"*.log\" -not -name \"*.pyc\" -exec md5sum {} \; 2>/dev/null | sed 's|  \./|  |'"

echo "执行SSH获取远程MD5..."
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" "$USER@$HOST" "$REMOTE_CMD" > /tmp/compare_code/remote_md5.txt
SSH_EXIT=$?

echo "SSH退出码: $SSH_EXIT"
echo "远程文件数量: $(wc -l < /tmp/compare_code/remote_md5.txt)"

if [ ! -s /tmp/compare_code/remote_md5.txt ]; then
    echo "❌ 获取远程MD5失败，文件为空"
    exit 1
fi

echo "步骤1完成"
echo
