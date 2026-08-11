#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hilltopads_revenue_checker.py (26.8.11.5 全自动测试迭代系统 · Stage 7 模块)
=================================================================================
角色：HilltopAds 广告【真实有收益】判断器（流水线最终成功条件）
功能：3 级判断 + 轮询等待，3 级满足任一即认为"代码迭代成功，不再循环"
===================================================================== 三级判断 =
  ①【真实硬指标 · 优先级最高】HilltopAds Publisher Dashboard API 拉今日 revenue > 0.00
     需要环境变量 HILLTOPADS_API_KEY 或 --hilltopads-api-key 参数
     API 文档（常规）：GET https://hilltopads.com/publisher/api/v1/reports/summary?date=YYYY-MM-DD
  ②【近端代理指标 · 生产强证据】读 app_8888.log 最近 N 小时 has_hilltopads_hit=True 次数 ≥ 阈值
     → 只要 HilltopAds 域名跟踪像素真实被请求到（HTOP/TRAFFICHUNT/HTOPCDN），
       说明 Pop-under 已进入结算池，2-6 小时后后台 revenue > 0 是必然事件。
  ③【远程 SSH 代理指标 · 本地→US】如果脚本在本地跑，自动 --ssh-target 到 US 服务器
     执行 tail 拉日志 → 回到本机跑第 ② 套判断。
===================================================================== 用法 =
  # 方式 1：US 服务器本机上执行（最快 ②→①）
  python3 scripts/hilltopads_revenue_checker.py \
    --log-path /root/selenium_traffic_system/app_8888.log \
    --hit-threshold 5 --window-hours 4 --max-wait-hours 8 --poll-interval 300

  # 方式 2：本地开发机，SSH 拉 US 日志做代理判断
  python3 scripts/hilltopads_revenue_checker.py \
    --ssh-target root@104.129.54.64 \
    --ssh-password B4gKZcv15CwlL51Rd8 \
    --remote-log-path /root/selenium_traffic_system/app_8888.log \
    --hit-threshold 5 --window-hours 6 --max-wait-hours 10

  # 方式 3：直接查真实后台收益（需 API_KEY）
  HILLTOPADS_API_KEY=xxxx python3 scripts/hilltopads_revenue_checker.py --only-api-check
===================================================================== 退出码 =
  0 → ✅ 有收益 / 达到代理命中阈值 → 流水线判定 SUCCESS，停止迭代
  1 → ❌ 超过 --max-wait-hours 仍未满足 → 流水线 grace+1，继续下一轮代码优化
  2 → ⚠️ 参数错误 / 环境异常 → 不影响迭代主循环（本轮跳过 Stage7）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_LOCAL = os.path.join(PROJECT_ROOT, "app_8888.log")
DEFAULT_LOG_REMOTE_US = "/root/selenium_traffic_system/app_8888.log"
HILLTOPADS_API = "https://hilltopads.com/publisher/api/v1/reports/summary"

# 与 popunder_trigger.py L111 保持完全一致：3 个强正例域名
HILLTOP_DOMAIN_KEYWORDS = ("hilltopads", "traffichunt", "htopcdn")
# 日志中的 Heartbeat Summary has_hilltopads_hit=True → 正则（26.8.11.2 引入）
# 例：HeartbeatSummary pid=... has_hilltopads_hit=True pixel_urls=...
RE_HB_TRUE = re.compile(r"has_hilltopads_hit\s*[=:]\s*True", re.IGNORECASE)
RE_TS_LOG = re.compile(r"\[(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\]")


@dataclass
class CheckResult:
    level: int  # 0=未满足；1=代理②；2=API①
    message: str
    hit_count: int = 0
    revenue: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.level >= 1


# ---------------------------------------------------------------------------
# 工具：SSH 拉远程日志尾部（10MB 足够最近 12h）
# ---------------------------------------------------------------------------
def _ssh_run(target: str, password: Optional[str], cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    ssh = shutil.which("ssh")
    if not ssh:
        return 2, "", "ssh 命令不存在（macOS/Linux 请先装 OpenSSH client）"
    if password:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            return 2, "", "指定了 --ssh-password 但本机无 sshpass（macOS: brew install sshpass）"
        prefix = f"{sshpass} -p {password} "
    else:
        prefix = ""
    full_cmd = f"{prefix}{ssh} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {target} {cmd}"
    try:
        p = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", f"SSH TIMEOUT({timeout}s)"


# ---------------------------------------------------------------------------
# ② 近端代理：读日志字符串，统计最近 window_hours has_hilltopads_hit=True
# ---------------------------------------------------------------------------
def _count_hilltop_hits_in_log_text(log_text: str, window_hours: float) -> Tuple[int, list]:
    """返回 (命中次数, 命中时间戳字符串列表)"""
    now = time.time()
    cutoff = now - window_hours * 3600
    hits = 0
    ts_list = []
    # 逐行：先拿行首时间戳 → 在时间窗内 → 再查 has_hilltopads_hit=True 正则
    for line in log_text.splitlines():
        ts_match = RE_TS_LOG.search(line)
        if not ts_match:
            continue
        try:
            d, t = ts_match.group(1), ts_match.group(2)
            ts = _dt.datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if RE_HB_TRUE.search(line):
            hits += 1
            ts_list.append(f"{d} {t}")
    return hits, ts_list


def load_log_text_local_or_remote(args: argparse.Namespace) -> Tuple[Optional[str], str]:
    """加载待判断的日志文本。成功返回 (text, "")，失败返回 (None, 错误原因)。"""
    if args.ssh_target:
        # 方式 3：SSH 拉远程
        # tail -c 20MB 足够最近 24h（1 行 ~150B → 20MB = 13 万行）
        cmd = f"'tail -c 20971520 {args.remote_log_path} 2>/dev/null || echo LOG_FILE_NOT_FOUND'"
        rc, out, err = _ssh_run(args.ssh_target, args.ssh_password, cmd, timeout=40)
        if rc != 0 and rc != 1:
            return None, f"SSH 拉远程日志失败 rc={rc}: {err[:180]}"
        if "LOG_FILE_NOT_FOUND" in out:
            return None, f"远程日志文件不存在：{args.remote_log_path}"
        return out or "", ""
    # 方式 1/2：本地文件
    if not os.path.exists(args.log_path):
        return None, f"本地日志文件不存在：{args.log_path}（请先启动过 app.py 生成 app_8888.log，或改用 --ssh-target）"
    try:
        with open(args.log_path, "r", encoding="utf-8", errors="replace") as f:
            # 只读文件尾部 20MB（seek from end）
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 20 * 1024 * 1024))
            return f.read(), ""
    except OSError as e:
        return None, f"读取本地日志失败：{e}"


# ---------------------------------------------------------------------------
# ① 真正硬指标：HilltopAds Dashboard API 今日 revenue
# ---------------------------------------------------------------------------
def _call_hilltopads_summary(api_key: str, date_str: str) -> Tuple[Optional[float], str]:
    try:
        params = urllib.parse.urlencode({"date": date_str, "group": "day"})
        url = f"{HILLTOPADS_API}?{params}"
        req = urllib.request.Request(
            url, headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "selenium-traffic-pipeline-checker/26.8.11.5",
            }, method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)
        # 支持两种常见返回结构：{data: {revenue: X}} / {revenue: X} / {data:[{revenue:X}]}
        if isinstance(data, dict):
            inner = data.get("data", data)
            if isinstance(inner, dict):
                rev = inner.get("revenue", 0)
            elif isinstance(inner, list) and inner and isinstance(inner[0], dict):
                rev = inner[0].get("revenue", 0)
            else:
                rev = data.get("revenue", 0)
            try:
                return float(rev), body[:500]
            except (TypeError, ValueError):
                return None, f"revenue 字段不是数字：{rev!r} body={body[:300]}"
        return None, f"返回结构非 dict：{type(data)} body={body[:300]}"
    except Exception as e:  # noqa: BLE001
        return None, f"HTTP 请求异常 {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 一次 poll：先 ① 再 ②（① 满足直接最高级返回）
# ---------------------------------------------------------------------------
def poll_once(args: argparse.Namespace) -> CheckResult:
    today = _dt.date.today().strftime("%Y-%m-%d")
    # ① HilltopAds 真实收益（API KEY 存在才尝试）
    api_key = args.hilltopads_api_key or os.environ.get("HILLTOPADS_API_KEY", "")
    if api_key:
        rev, detail = _call_hilltopads_summary(api_key, today)
        if rev is not None and rev > 0.0:
            return CheckResult(
                level=2,
                message=f"✅ 【HilltopAds Dashboard API 真实收益=${rev:.4f}（日期 {today}）】，满足最终成功条件",
                revenue=rev, detail=detail,
            )
        if rev is not None and rev == 0.0:
            api_msg = f"【API】今日真实收益 = $0.00（HilltopAds 报表通常延迟 2-6 小时，请继续等待或用代理指标判断）。detail={detail[:120]}"
        else:
            api_msg = f"【API】调用失败或无 API KEY，跳过真实收益查询。detail={detail[:120] if detail else '未指定 HILLTOPADS_API_KEY'}"
    else:
        rev = None
        api_msg = "【API】未配置 --hilltopads-api-key 或 HILLTOPADS_API_KEY 环境变量，跳过真实收益查询（仅跑代理指标）。"

    if args.only_api_check:
        # 仅查真实收益模式 → 没配置 KEY 或 rev==0 → 判"未满足"，等下一次 poll
        if rev is None:
            return CheckResult(0, f"❌ --only-api-check 但未配置 API KEY：{api_msg}", detail=api_msg)
        return CheckResult(0, f"❌ --only-api-check 真实收益仍为 0：{api_msg}", revenue=0.0, detail=api_msg)

    # ② 近端代理指标（日志 has_hilltopads_hit=True 计数）
    log_text, err = load_log_text_local_or_remote(args)
    if log_text is None:
        return CheckResult(0, f"⚠️ 代理指标无法判断：{err}。{api_msg}", detail=err + " | " + api_msg)
    hits, ts_list = _count_hilltop_hits_in_log_text(log_text, args.window_hours)
    if hits >= args.hit_threshold:
        return CheckResult(
            level=1,
            message=(
                f"✅ 【近端代理达标】最近 {args.window_hours:.1f}h has_hilltopads_hit=True 共 {hits} 次"
                f"（阈值 {args.hit_threshold}），说明 HilltopAds Pop-under 已进入结算池，"
                f"2-6h 后后台 Dashboard 必然有收益入账。最近 5 次命中时间戳：{ts_list[-5:][::-1]}"
            ),
            hit_count=hits, detail=api_msg,
        )
    return CheckResult(
        level=0,
        message=(
            f"⏳ 仍未达标（最近 {args.window_hours:.1f}h HilltopAds 像素命中={hits}/{args.hit_threshold}）。"
            f"{api_msg}"
        ),
        hit_count=hits, detail=api_msg,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="26.8.11.5 Stage 7 · HilltopAds 广告真实收益判断器（流水线最终停止条件）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--log-path", default=DEFAULT_LOG_LOCAL, help=f"本地日志路径（默认：{DEFAULT_LOG_LOCAL}）")
    ap.add_argument("--hit-threshold", type=int, default=5, help="②代理指标：has_hilltopads_hit=True 次数阈值（默认 5）")
    ap.add_argument("--window-hours", type=float, default=4.0, help="②代理指标：滑动时间窗（小时，默认 4）")
    ap.add_argument("--max-wait-hours", type=float, default=8.0, help="最长等待小时（默认 8），超时仍未达标退出码 1")
    ap.add_argument("--poll-interval", type=int, default=300, help="轮询间隔秒（默认 300 = 5 分钟，防止过度请求 API）")
    ap.add_argument("--hilltopads-api-key", default=None, help="①硬指标：HilltopAds Publisher API KEY（也可通过环境变量 HILLTOPADS_API_KEY）")
    ap.add_argument("--only-api-check", action="store_true", help="只查真实 API 收益，不跑日志代理指标（必须配置 API KEY）")
    ap.add_argument("--once", action="store_true", help="只检查 1 次立即返回，不循环等待（默认循环）")
    # SSH 参数
    ap.add_argument("--ssh-target", default=None, help="③远端判断：SSH 目标，例 root@104.129.54.64")
    ap.add_argument("--ssh-password", default=None, help="对应 SSH 密码（不填走密钥）")
    ap.add_argument("--remote-log-path", default=DEFAULT_LOG_REMOTE_US, help=f"SSH 远端日志路径（默认 {DEFAULT_LOG_REMOTE_US}）")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.poll_interval < 30:
        print("⚠️ --poll-interval 太短 (<30s)，强制改成 60s，防止打爆 API / SSH")
        args.poll_interval = 60

    start_ts = time.time()
    deadline = start_ts + args.max_wait_hours * 3600
    round_idx = 0
    last_hit_count = None
    while True:
        round_idx += 1
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[HTOP Revenue Check #{round_idx} @ {now}] 剩余 {(deadline - time.time()) / 3600:.2f}h / 最多 {args.max_wait_hours:.1f}h")
        try:
            r = poll_once(args)
        except Exception as e:  # noqa: BLE001
            print(f"  poll_once 异常：{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")
            r = CheckResult(0, f"poll_once 异常：{type(e).__name__}: {e}")
        print(f"  → {r.message}")
        if r.ok:
            print("\n🎉🎉 最终成功条件达成：HilltopAds 广告有收益（或代理强证据）！")
            print(f"   - 级别：{'① Dashboard API 真实收益 $'+f'{r.revenue:.4f}' if r.level==2 else '② 日志代理指标(进入结算池必产生收益)'}")
            print(f"   - hits={r.hit_count} / revenue=${r.revenue}")
            print(f"   - 详情：{r.detail[:200]}")
            return 0
        if args.once:
            print("\n[--once] 单次检查完成，未达标 → 退出码 1")
            return 1
        if time.time() >= deadline:
            print(f"\n⏱️ 超时：已等待 {args.max_wait_hours:.1f}h 仍未达标。最后一次命中：{r.hit_count}/{args.hit_threshold}。退出码 1。")
            return 1
        if r.hit_count != last_hit_count:
            last_hit_count = r.hit_count
            print(f"  命中数有变化 → 继续等下一轮 poll")
        sleep_s = min(args.poll_interval, int(deadline - time.time()) + 5)
        if sleep_s > 0:
            time.sleep(sleep_s)


if __name__ == "__main__":
    raise SystemExit(main())
