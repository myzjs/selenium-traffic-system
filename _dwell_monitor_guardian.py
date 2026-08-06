"""
独立监控守护进程：实时 tail -F `app.log`，检测风控红线并自动暂停任务 + 报警。
=============================================================
功能（3 合 1，按用户需求）：
  ① 实时读 `app.log`（支持日志轮转 Reopen，不丢一行）
  ② 风控红线检测（支持 4 类规则，阈值在 RULES 字典改）：
       - 红线A(CRITICAL)：单任务浏览网站时长 < 45s = 广告 0 收益 → 立刻暂停 + 报警
       - 红线B(WARNING) ：跳出型任务停留 < 60s / 全流程 < 60s
       - 红线C(CRITICAL)：滑窗 N 条任务里的「跳出率 > BOUNCE_RATE_HIGH_PCT %」（默认 N=30，>55%）
       - 红线D(INFO)    ：每 60s 滚动汇报停留健康度指标（CRIT_COUNT/WARN_COUNT/RUNAVG_DWELL）
  ③ 命中 CRITICAL 时自动 POST 到 Flask 主进程：
         http://127.0.0.1:5000/stop_task  （立即停止所有任务）
       同时写 3 种报警：
         - 控制台 ANSI 红底报警（人工跑脚本看）
         - 写入 `logs/monitor_alerts.log`（JSON lines 格式，后续推飞书/钉钉 webhook）
         - 可选：飞书 / 钉钉 / 企业微信 webhook（在 WEBHOOK 配置）

用法：
  1) 与主程序 app.py 在同一目录（它读取 ./app.log 并打到 127.0.0.1:5000）
  2) python3 _dwell_monitor_guardian.py

     可选参数：
       --host=0.0.0.0              默认 127.0.0.1（只允许本机，安全）
       --port=5000                 app.py Flask 端口
       --log=./app.log             被监控的日志文件
       --poll=0.15                 tail 轮询间隔（秒），macOS 下 inotify 不存在只能用 polling
       --webhook=                  飞书/钉钉/企微 Webhook（可选；留空=只写本地）
       --no-auto-pause             只报警不调 stop_task（调试用）
       --auto-pause-coolsec=600    命中后 cool down 秒数（默认 10 分钟内不重复暂停）

依赖：仅 Python 3.9+ stdlib，不需要第三方包。
=============================================================
"""

from __future__ import annotations

import argparse
import collections
import http.server
import json
import os
import pathlib
import re
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 0. 默认阈值（先从环境变量读，方便 systemd 覆盖；否则走内置）
# ---------------------------------------------------------------------------
RULES: Dict[str, float] = {
    # 红线A：单任务浏览网站时长 < 该值 → CRITICAL，立刻暂停
    "DWELL_CRITICAL_SEC": float(os.environ.get("DM_DWELL_CRITICAL_SEC", 45)),
    # 红线B：单任务 < 该值 → WARNING（不暂停，但计入健康度）
    "DWELL_WARN_SEC": float(os.environ.get("DM_DWELL_WARN_SEC", 60)),
    # 红线C：滑窗内「跳出率」超过该比例 → CRITICAL
    "BOUNCE_RATE_HIGH_PCT": float(os.environ.get("DM_BOUNCE_PCT", 55)),
    # 红线C的滑窗任务数
    "BOUNCE_WINDOW_TASKS": int(float(os.environ.get("DM_BOUNCE_WIN", 30))),
    # 健康度滚动汇报间隔秒
    "HEALTH_REPORT_EVERY_SEC": float(os.environ.get("DM_HEALTH_EVERY", 60)),
}


# ---------------------------------------------------------------------------
# 1. 日志正则：抓 app.py 里新加的 6 组停留节点
# ---------------------------------------------------------------------------
# [停留-02/红线] 浏览网站时长=32.5s < 红线45s ...
# [停留-03/红线] 跳出型任务停留=32.1s < 红线45s ...
# 🚫 P2-5[停留审计] 浏览网站时长=38.1s < 红线45s ...
# ⚠️ P2-5[停留审计] 浏览网站时长=58.8s < 建议阈值 60s ...
# ✅ P2-5[停留审计] 浏览网站时长=220.3s ≥ 60s 达标
# 🚪 本次任务为跳出型 ...

_RE_SEC_FLOAT: re.Pattern = re.compile(r"(\d+(?:\.\d+)?)s")  # 抓形如 "38.1s"
_RE_P25_CRIT: re.Pattern = re.compile(r"P2-5\[停留审计\].*浏览网站时长=(\d+(?:\.\d+)?)s.*红线")
_RE_P25_WARN: re.Pattern = re.compile(r"P2-5\[停留审计\].*浏览网站时长=(\d+(?:\.\d+)?)s.*建议阈值")
_RE_P25_OK: re.Pattern = re.compile(r"P2-5\[停留审计\].*浏览网站时长=(\d+(?:\.\d+)?)s.*达标")
_RE_DWELL_CRIT: re.Pattern = re.compile(r"\[停留-0[23]/红线\].*?(\d+(?:\.\d+)?)s")
_RE_DWELL_WARN: re.Pattern = re.compile(r"\[停留-0[23]/警告\].*?(\d+(?:\.\d+)?)s")
_RE_BOUNCE_TASK: re.Pattern = re.compile(r"🚪.*?跳出型(任务为跳出)?|Case1.*跳出型|本次任务为跳出型")


# ---------------------------------------------------------------------------
# 2. 环形任务滑窗（用于计算跳出率、平均停留）
# ---------------------------------------------------------------------------
@dataclass
class _TaskSample:
    ts: float
    dwell_sec: Optional[float]  # None = 还没抓到最终 P2-5 审计值
    is_bounce: Optional[bool]
    level: str  # "CRIT" | "WARN" | "OK" | "UNKNOWN"


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.crit_count: int = 0
        self.warn_count: int = 0
        self.ok_count: int = 0
        self.tasks: Deque[_TaskSample] = collections.deque(maxlen=500)
        self.dwells_for_avg: Deque[float] = collections.deque(maxlen=120)
        # 每次从 app.log 读到「进入网站锚点」就算一条新的 in-progress task；读到 P2-5 审计再回填
        self._in_progress: Optional[_TaskSample] = None

    # ---- 事件 ----
    def on_enter_site(self) -> None:
        with self._lock:
            self._in_progress = _TaskSample(
                ts=time.time(), dwell_sec=None, is_bounce=False, level="UNKNOWN"
            )

    def on_bounce_marker(self) -> None:
        with self._lock:
            if self._in_progress is not None:
                self._in_progress.is_bounce = True

    def on_dwell_crit(self, sec: float) -> None:
        self._complete_task(sec, "CRIT")
        with self._lock:
            self.crit_count += 1

    def on_dwell_warn(self, sec: float) -> None:
        self._complete_task(sec, "WARN")
        with self._lock:
            self.warn_count += 1

    def on_dwell_ok(self, sec: float) -> None:
        self._complete_task(sec, "OK")
        with self._lock:
            self.ok_count += 1

    def _complete_task(self, sec: float, level: str) -> None:
        with self._lock:
            samp = self._in_progress or _TaskSample(
                ts=time.time(), dwell_sec=sec, is_bounce=None, level=level
            )
            samp.dwell_sec = sec
            samp.level = level
            self.tasks.append(samp)
            self.dwells_for_avg.append(sec)
            self._in_progress = None

    # ---- 查询（滑窗跳出率、平均停留、统计）----
    def snapshot(self) -> Dict:
        with self._lock:
            win = list(self.tasks)[-int(RULES["BOUNCE_WINDOW_TASKS"]):]
            completed = [t for t in win if t.dwell_sec is not None]
            bounce_n = sum(1 for t in completed if t.is_bounce)
            bounce_pct = (bounce_n / len(completed) * 100) if completed else 0.0
            avg = (sum(self.dwells_for_avg) / len(self.dwells_for_avg)) if self.dwells_for_avg else 0.0
            return {
                "crit": self.crit_count,
                "warn": self.warn_count,
                "ok": self.ok_count,
                "win_completed_n": len(completed),
                "win_bounce_pct": bounce_pct,
                "avg_dwell_last120": avg,
            }


# ---------------------------------------------------------------------------
# 3. 报警：控制台 + JSONL 本地 + 可选 Webhook
# ---------------------------------------------------------------------------
@dataclass
class Alerter:
    webhook_url: str = ""
    alert_file: str = "logs/monitor_alerts.log"
    status_file: str = "logs/monitor_status.json"
    last_auto_pause_ts: float = 0.0
    auto_pause_coolsec: float = 600.0
    no_auto_pause: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = 5000

    def __post_init__(self):
        pathlib.Path(self.alert_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.status_file).parent.mkdir(parents=True, exist_ok=True)
        self._status_lock = threading.Lock()
        # 告警历史（最近100条），供 HTTP 端点 / 前端轮询
        self.alert_history: Deque[Dict] = collections.deque(maxlen=100)
        # Rule E: 连续 CRITICAL 计数器
        self._consecutive_crit: int = 0

    def write_status(self, metrics_snapshot: Dict) -> None:
        """健康度线程：每一轮汇报后把最新状态写到 JSON（供 Flask /dwell_monitor/status 读取）"""
        record = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot": metrics_snapshot,
            "no_auto_pause": self.no_auto_pause,
            "app": f"http://{self.app_host}:{self.app_port}",
            "alerts": list(self.alert_history),
            "consecutive_crit": self._consecutive_crit,
        }
        try:
            tmp = self.status_file + ".tmp"
            with self._status_lock, open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.status_file)
        except Exception as e:
            print(f"[WARN] 写 status.json 失败: {e}", file=sys.stderr)

    def _write_jsonl(self, data: Dict) -> None:
        try:
            pathlib.Path(self.alert_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.alert_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:  # pragma: no cover - 磁盘问题
            print(f"[WARN] 写报警日志失败: {e}", file=sys.stderr)

    def _post_webhook(self, text: str) -> None:
        """推送到飞书/钉钉/企微 Webhook（如果配置了）"""
        if not self.webhook_url:
            return
        payload: Dict
        u = self.webhook_url.lower()
        if "open.feishu.cn" in u or "lark" in u:
            payload = {"msg_type": "text", "content": {"text": text}}
        elif "dingtalk" in u:
            payload = {"msgtype": "text", "text": {"content": text}}
        elif "qyapi.weixin" in u or "weixin" in u:
            payload = {"msgtype": "markdown", "markdown": {"content": text}}
        else:
            payload = {"text": text}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:  # pragma: no cover - 网络问题
            print(f"[WARN] Webhook 推送失败: {e}", file=sys.stderr)

    def _pause_flask_task(self, reason: str) -> bool:
        """命中 CRITICAL 时：POST 到 app.py /stop_task，让主程序立即停止所有任务"""
        if self.no_auto_pause:
            print("[INFO] --no-auto-pause 启用，跳过自动暂停")
            return False
        now = time.time()
        if now - self.last_auto_pause_ts < self.auto_pause_coolsec:
            remain = int(self.auto_pause_coolsec - (now - self.last_auto_pause_ts))
            print(f"[INFO] 自动暂停冷却中，跳过本次（剩余 {remain}s）")
            return False
        url = f"http://{self.app_host}:{self.app_port}/stop_task"
        body = json.dumps({"reason": reason, "source": "dwell_monitor"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = (resp.read() or b"").decode("utf-8", errors="replace")
            self.last_auto_pause_ts = now
            print(f"\033[1;41m 🛑 已自动 POST {url} 结果: {ok[:200]} \033[0m", flush=True)
            return True
        except Exception as e:  # pragma: no cover
            print(f"[ERROR] 调用 /stop_task 失败（app.py 未启动?）: {e}", file=sys.stderr)
            return False

    def alert(
        self,
        severity: str,
        title: str,
        details: Dict,
        reason_for_pause: str = "",
        auto_pause: bool = False,
    ) -> None:
        """severity: CRITICAL | WARNING | INFO"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        color_red = "\033[1;41m" if severity == "CRITICAL" else "\033[1;33m"
        color_end = "\033[0m"
        line = f"{color_red}[{severity}] {now_str} {title}{color_end}\n  " + json.dumps(
            details, ensure_ascii=False
        )
        print(line, flush=True)
        record = {
            "ts": now_str,
            "severity": severity,
            "title": title,
            "details": details,
        }
        self._write_jsonl(record)
        self._post_webhook(f"[{severity}] {title}\n" + json.dumps(details, ensure_ascii=False, indent=2))

        # ---- 告警历史（最近100条） ----
        self.alert_history.append(record)

        # ---- Rule E: 连续3次 CRITICAL → 自动降级 ----
        if severity == "CRITICAL":
            self._consecutive_crit += 1
            if self._consecutive_crit >= 3:
                degrade_msg = (
                    f"Rule E 触发：连续 {self._consecutive_crit} 次 CRITICAL 告警，"
                    f"建议将任务量减半以保护广告收益。"
                )
                print(f"\033[1;45m ⚠️  [DEGRADE] {degrade_msg} \033[0m", flush=True)
                degrade_record = {
                    "ts": now_str,
                    "severity": "DEGRADE",
                    "title": degrade_msg,
                    "details": {
                        "consecutive_crit": self._consecutive_crit,
                        "suggested_action": "halve_task_volume",
                    },
                }
                self._write_jsonl(degrade_record)
                self.alert_history.append(degrade_record)
                self._post_webhook(f"[DEGRADE] {degrade_msg}")
                # 重置计数器，避免重复降级
                self._consecutive_crit = 0
        else:
            # WARNING / INFO 重置连续 CRITICAL 计数
            self._consecutive_crit = 0

        if auto_pause and severity == "CRITICAL":
            self._pause_flask_task(reason_for_pause or title)


# ---------------------------------------------------------------------------
# 4. Tailer：MacOS 没有 inotify 用户态，用 seek+size 方式安全轮询；
#    支持 RotatingFileHandler 的重命名（如果文件 inode 变了就 reopen）
# ---------------------------------------------------------------------------
class SafeLogTailer:
    def __init__(self, path: str, poll_sec: float = 0.15) -> None:
        self.path = path
        self.poll_sec = poll_sec
        self._fp = None
        self._inode: Optional[int] = None
        self._reopen_or_start_from_end()

    def _reopen_or_start_from_end(self) -> None:
        # 如果新文件不存在，一直等创建
        while not os.path.exists(self.path):
            time.sleep(0.5)
        self._fp = open(self.path, "r", encoding="utf-8", errors="replace")
        self._fp.seek(0, 2)  # start from EOF（不消费历史；如果要历史改成 0）
        st = os.stat(self.path)
        self._inode = st.st_ino

    def _ensure_alive(self) -> None:
        # 如果当前 handle 指向的文件 inode 与磁盘上不同（已被 rotate 重命名）→  reopen
        try:
            cur_inode = os.stat(self.path).st_ino
        except FileNotFoundError:
            cur_inode = None
        if cur_inode != self._inode:
            try:
                if self._fp:
                    self._fp.close()
            except Exception:
                pass
            self._reopen_or_start_from_end()

    def readlines_nonblock(self):
        """每次最多读一大坨 lines，超过 2000 条截断，避免被历史打爆"""
        self._ensure_alive()
        buf = []
        if self._fp is None:
            return buf
        for _ in range(2000):
            line = self._fp.readline()
            if not line:
                break
            buf.append(line.rstrip("\n"))
        return buf


# ---------------------------------------------------------------------------
# 5. 解析每一条日志 → 更新 metrics → 触发规则
# ---------------------------------------------------------------------------
def process_line(line: str, metrics: MetricsStore, alerter: Alerter) -> None:
    # 锚点：进入网站
    if "[停留-01]" in line or "enter_site_time锚点" in line:
        metrics.on_enter_site()
        return
    if _RE_BOUNCE_TASK.search(line):
        metrics.on_bounce_marker()
        return

    # CRIT / WARN / OK 三种 P2-5 审计
    m_crit = _RE_P25_CRIT.search(line) or _RE_DWELL_CRIT.search(line)
    if m_crit:
        try:
            sec = float(m_crit.group(1))
        except Exception:
            return
        snap = metrics.snapshot()
        metrics.on_dwell_crit(sec)
        alerter.alert(
            severity="CRITICAL",
            title=f"停留红线触发：单任务浏览时长 {sec:.1f}s < {RULES['DWELL_CRITICAL_SEC']:.0f}s",
            details={**snap, "reason": line[:300]},
            reason_for_pause=f"Dwell<{RULES['DWELL_CRITICAL_SEC']:.0f}s（{sec:.1f}s）",
            auto_pause=True,
        )
        _check_bounce_window(metrics, alerter)
        return

    m_warn = _RE_P25_WARN.search(line) or _RE_DWELL_WARN.search(line)
    if m_warn:
        try:
            sec = float(m_warn.group(1))
        except Exception:
            return
        metrics.on_dwell_warn(sec)
        snap = metrics.snapshot()
        if snap["win_completed_n"] >= 5 and snap["win_bounce_pct"] > RULES["BOUNCE_RATE_HIGH_PCT"]:
            return  # bounce 窗会在下面单独告警，这里不重复
        alerter.alert(
            severity="WARNING",
            title=f"停留警告：单任务浏览时长 {sec:.1f}s < {RULES['DWELL_WARN_SEC']:.0f}s（建议阈值）",
            details={**snap, "reason": line[:300]},
            auto_pause=False,
        )
        _check_bounce_window(metrics, alerter)
        return

    m_ok = _RE_P25_OK.search(line)
    if m_ok:
        try:
            sec = float(m_ok.group(1))
        except Exception:
            return
        metrics.on_dwell_ok(sec)
        _check_bounce_window(metrics, alerter)


def _check_bounce_window(metrics: MetricsStore, alerter: Alerter) -> None:
    snap = metrics.snapshot()
    if (
        snap["win_completed_n"] >= max(10, int(RULES["BOUNCE_WINDOW_TASKS"] // 2))
        and snap["win_bounce_pct"] > RULES["BOUNCE_RATE_HIGH_PCT"]
    ):
        alerter.alert(
            severity="CRITICAL",
            title=f"跳出率过高：最近{snap['win_completed_n']}个任务跳出率{snap['win_bounce_pct']:.1f}% > {RULES['BOUNCE_RATE_HIGH_PCT']:.0f}%",
            details={**snap},
            reason_for_pause=f"BounceRate={snap['win_bounce_pct']:.1f}% > {RULES['BOUNCE_RATE_HIGH_PCT']:.0f}%",
            auto_pause=True,
        )


# ---------------------------------------------------------------------------
# 6. 独立线程：每 HEALTH_REPORT_EVERY_SEC 输出一条 INFO 健康度汇总
# ---------------------------------------------------------------------------
def health_reporter_daemon(metrics: MetricsStore, alerter: Alerter) -> None:
    last = time.time()
    while True:
        time.sleep(max(2, RULES["HEALTH_REPORT_EVERY_SEC"] - (time.time() - last)))
        last = time.time()
        snap = metrics.snapshot()
        # ---- 关键：每轮汇报都写 status.json（Flask UI 按钮区读取它） ----
        alerter.write_status(snap)
        alerter.alert(
            severity="INFO",
            title="停留健康度滚动汇总",
            details=snap,
            auto_pause=False,
        )


# ---------------------------------------------------------------------------
# 7. 内置 HTTP 状态端点（在独立 daemon 线程中启动，监听 127.0.0.1:5010）
#    供前端 / 外部系统轮询实时监控数据
# ---------------------------------------------------------------------------
def _start_http_server(
    metrics: MetricsStore,
    alerter: Alerter,
    host: str = "127.0.0.1",
    port: int = 5010,
) -> None:
    """在独立 daemon 线程中启动一个轻量 HTTP server，返回 JSON 格式的实时监控数据。"""
    metrics_ref = metrics
    alerter_ref = alerter

    class _MonitorHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # 安静模式，不向 stderr 打 access log

        def _json_response(self, data: Dict, code: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/" or path == "/status":
                snap = metrics_ref.snapshot()
                alerts_list = list(alerter_ref.alert_history)
                self._json_response({
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "snapshot": snap,
                    "alerts": alerts_list[-20:],  # 只返回最近20条
                    "alert_count": len(alerts_list),
                    "consecutive_crit": alerter_ref._consecutive_crit,
                    "rules": {
                        "dwell_critical_sec": RULES["DWELL_CRITICAL_SEC"],
                        "dwell_warn_sec": RULES["DWELL_WARN_SEC"],
                        "bounce_rate_high_pct": RULES["BOUNCE_RATE_HIGH_PCT"],
                        "bounce_window_tasks": int(RULES["BOUNCE_WINDOW_TASKS"]),
                    },
                })
            elif path == "/health":
                self._json_response({"status": "ok", "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            else:
                self._json_response({"error": "not found", "path": path}, code=404)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    class _ReuseTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        server = _ReuseTCPServer((host, port), _MonitorHandler)
        print(f"🌐 HTTP 状态端点已启动: http://{host}:{port}/status", flush=True)
        server.serve_forever()
    except OSError as e:
        print(f"[WARN] HTTP 端点启动失败（端口 {port} 已被占用？）: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 8. main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Dwell Monitor Guardian（独立监控守护进程）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--http-port", type=int, default=5010, help="内置 HTTP 状态端点监听端口（默认 5010，设 0 禁用）")
    ap.add_argument("--log", default=str(pathlib.Path(__file__).with_name("app.log")))
    ap.add_argument("--poll", type=float, default=0.15)
    ap.add_argument("--webhook", default=os.environ.get("MONITOR_WEBHOOK", ""))
    ap.add_argument("--no-auto-pause", action="store_true")
    ap.add_argument("--auto-pause-coolsec", type=float, default=600.0)
    ap.add_argument(
        "--consume-history",
        action="store_true",
        help="启动时从 app.log 第 0 行开始消费（默认从 EOF 开始，仅消费新日志）",
    )
    args = ap.parse_args()

    print("=" * 72)
    print("🛡️  Dwell Monitor Guardian 启动（独立守护进程，与 app.py 解耦）")
    print(f"   监控日志: {args.log}")
    print(f"   自动暂停: {'OFF (--no-auto-pause)' if args.no_auto_pause else f'ON，冷却 {args.auto_pause_coolsec:.0f}s'}")
    print(f"   App Flask:  http://{args.host}:{args.port}/stop_task")
    print(f"   规则阈值:  CRIT < {RULES['DWELL_CRITICAL_SEC']:.0f}s / WARN < {RULES['DWELL_WARN_SEC']:.0f}s")
    print(f"             BOUNCE > {RULES['BOUNCE_RATE_HIGH_PCT']:.0f}%（滑窗 {int(RULES['BOUNCE_WINDOW_TASKS'])} 个任务）")
    print("=" * 72, flush=True)

    # 如果启用 consume-history：tailer seek(0) 而不是 EOF
    tailer = SafeLogTailer(args.log, poll_sec=args.poll)
    if args.consume_history and tailer._fp is not None:
        tailer._fp.seek(0)

    metrics = MetricsStore()
    alerter = Alerter(
        webhook_url=args.webhook,
        no_auto_pause=args.no_auto_pause,
        auto_pause_coolsec=args.auto_pause_coolsec,
        app_host=args.host,
        app_port=args.port,
    )

    t = threading.Thread(
        target=health_reporter_daemon,
        args=(metrics, alerter),
        daemon=True,
        name="health-reporter",
    )
    t.start()

    # 启动内置 HTTP 状态端点（如果 --http-port > 0）
    if args.http_port > 0:
        http_thread = threading.Thread(
            target=_start_http_server,
            args=(metrics, alerter, "127.0.0.1", args.http_port),
            daemon=True,
            name="http-status-server",
        )
        http_thread.start()

    try:
        while True:
            lines = tailer.readlines_nonblock()
            if not lines:
                time.sleep(args.poll)
                continue
            for ln in lines:
                try:
                    process_line(ln, metrics, alerter)
                except Exception as e:
                    print(f"[WARN] 解析行异常: {type(e).__name__}: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[INFO] Ctrl-C 退出 Dwell Monitor Guardian")
        return 0


if __name__ == "__main__":
    sys.exit(main())
