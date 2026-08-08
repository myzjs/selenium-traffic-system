#!/bin/bash
# 步骤3：对比MD5并生成报告

OUTPUT="$(cd "$(dirname "$0")" && pwd)/compare_result.txt"

echo "步骤3: 对比MD5并生成报告 -> $OUTPUT"

mkdir -p /tmp/compare_code
cd /tmp/compare_code

{
echo "============================================================"
echo "  本地代码 与 东京VPS(107.148.2.75) 代码一致性对比报告"
echo "  生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo
echo "【统计】"
echo "  本地文件数: $(wc -l < local_md5.txt)"
echo "  远程文件数: $(wc -l < remote_md5.txt)"
echo

# 按文件名整理（去掉md5）
cut -d' ' -f3- local_md5.txt  | sort > local_files.txt
cut -d' ' -f3- remote_md5.txt | sort > remote_files.txt

# 仅本地
echo "【仅本地存在的文件】"
ONLY_LOCAL=$(comm -23 local_files.txt remote_files.txt)
ONLY_LOCAL_COUNT=$(echo "$ONLY_LOCAL" | grep -c . || true)
echo "  数量: $ONLY_LOCAL_COUNT"
if [ "$ONLY_LOCAL_COUNT" -gt 0 ]; then
  echo "$ONLY_LOCAL" | sed 's/^/  + /'
fi
echo

# 仅远程
echo "【仅远程存在的文件】"
ONLY_REMOTE=$(comm -13 local_files.txt remote_files.txt)
ONLY_REMOTE_COUNT=$(echo "$ONLY_REMOTE" | grep -c . || true)
echo "  数量: $ONLY_REMOTE_COUNT"
if [ "$ONLY_REMOTE_COUNT" -gt 0 ]; then
  echo "$ONLY_REMOTE" | sed 's/^/  - /'
fi
echo

# 找共同文件
sort -k2 local_md5.txt  > local_sorted.txt
sort -k2 remote_md5.txt > remote_sorted.txt
join -j 2 local_sorted.txt remote_sorted.txt > common_files.txt 2>/dev/null

echo "【MD5内容不一致的文件】"
DIFF_COUNT=0
> diff_files.txt
while read -r f lmd5 rmd5; do
  if [ -n "$lmd5" ] && [ -n "$rmd5" ] && [ "$lmd5" != "$rmd5" ]; then
    echo "  ! $f" >> diff_files.txt
    echo "      本地: $lmd5" >> diff_files.txt
    echo "      远程: $rmd5" >> diff_files.txt
    DIFF_COUNT=$((DIFF_COUNT + 1))
  fi
done < common_files.txt
echo "  数量: $DIFF_COUNT"
if [ "$DIFF_COUNT" -gt 0 ]; then
  cat diff_files.txt
fi
echo

echo "============================================================"
TOTAL=$((ONLY_LOCAL_COUNT + ONLY_REMOTE_COUNT + DIFF_COUNT))
if [ "$TOTAL" -eq 0 ]; then
  echo "✅ 结论: 本地代码 与 东京VPS 完全一致！"
else
  echo "❌ 结论: 存在差异，共 $TOTAL 处："
  echo "   本地独有: $ONLY_LOCAL_COUNT"
  echo "   远程独有: $ONLY_REMOTE_COUNT"
  echo "   内容不同: $DIFF_COUNT"
fi
echo "============================================================"

} | tee "$OUTPUT"

echo
echo "报告已保存到: $OUTPUT"
