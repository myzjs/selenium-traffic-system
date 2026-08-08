#!/bin/bash
# 步骤2：计算本地md5

LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

EXCLUDE_DIRS="__pycache__ .git qa_sessions report test_reports feedback .uploads node_modules trae_feedback"
EXCLUDE_FILES="app.log config.json .env historical_tasks.json ua_usage_history.json fingerprint_stats.json sync_two_servers.py compare_remote.py _step1_remote.sh _step2_local.sh _step3_compare.sh compare_result.txt"

mkdir -p /tmp/compare_code

echo "步骤2: 计算本地文件MD5..."

EXCLUDE_DIR_ARGS=""
for d in $EXCLUDE_DIRS; do
  EXCLUDE_DIR_ARGS="$EXCLUDE_DIR_ARGS -not -path \"*/$d/*\""
done

EXCLUDE_FILE_ARGS=""
for f in $EXCLUDE_FILES; do
  EXCLUDE_FILE_ARGS="$EXCLUDE_FILE_ARGS -not -name \"$f\""
done

cd "$LOCAL_DIR"
eval "find . -type f $EXCLUDE_DIR_ARGS $EXCLUDE_FILE_ARGS -not -name \"*.log\" -not -name \"*.pyc\" -exec md5 -r {} \; 2>/dev/null" | sed 's| \./| |' | awk '{print $1"  "$2}' > /tmp/compare_code/local_md5.txt

echo "本地文件数量: $(wc -l < /tmp/compare_code/local_md5.txt)"
echo "步骤2完成"
echo
