#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
traffic_monitor.py  26.8.13.8
==============================================================
双日志实时风控监控系统（VPS 7x24 小时守护脚本 + Flask 监控 API 两用）

功能（按用户"随时监控网站+流量工具任务+触发风控自动告警+建议修复"需求设计）：
  1. 双日志实时 follow（类 tail -F，支持文件轮转）：
       - Nginx access.log（NCSA Combined Format）→ 网站机器人流量/异常访问
       - 流量系统 app.log → Selenium 任务执行、IPDeep、HilltopAds 广告位/popunder
  2. 内置 10 维风控规则引擎（与 redteam_scenarios.py 10 检测维度对齐）：
       R01 同 IP 频次异常        R02 爬虫/Headless UA       R03 Referer 缺失/错配
       R04 CTR 分布异常           R05 IP 数据中心/代理        R06 指纹三要素冲突
       R07 停留 <10s 占比过高     R08 单页浅浏览             R09 IPDeep 连续失败
       R10 HilltopAds 弹出/展示 0 次
  3. 分级告警：
       🟡 WARN = 可疑（仅记录 + 打印）
       🔴 CRIT = 已触发风控（写入 monitor/rt_events.jsonl + 打印修复建议 + 可选项自动改 config）
  4. Flask Blueprint /monitoring 暴露 API + 事件流（SSE）
  5. 每 5 分钟计算 HilltopAds 入账概率评分（8 项清单加权）

用法一（独立守护 7x24 监控）：
    python3 traffic_monitor.py \
        --nginx-log   /www/server/nginx/logs/freestoryweb-access.log \
        --traffic-log /root/selenium_traffic_system/app.log \
        --daemon \
        --events-dir  /root/selenium_traffic_system/monitor

用法二（挂 Flask /monitoring 蓝图 + 主线程启动监控）：
    from traffic_monitor import monitor_bp, start_background_monitor
    app.register_blueprint(monitor_bp, url_prefix="/monitoring")
    start_background_monitor(nginx_log="...", traffic_log="...")

Author: 26.8.13.5（规则三版本号，与 app.py APP_VERSION 同步）
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import queue
import re
import signal
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

# ---------------- 常量 ----------------
APP_VERSION = "26.8.13.8"
_MON: logging.Logger = logging.getLogger("traffic_monitor")
if not _MON.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    _MON.addHandler(_handler)
    _MON.setLevel(logging.INFO)

# 10 维规则 ID
RULES = (
    "R01_IP_FREQ",
    "R02_BOT_UA",
    "R03_REFERER_MISMATCH",
    "R04_CTR_DISTRIBUTION",
    "R05_DC_PROXY_IP",
    "R06_FP_MISMATCH",
    "R07_SHORT_STAY",
    "R08_SINGLE_PAGE_SHALLOW",
    "R09_IPDEEP_FAIL",
    "R10_HT_ZERO_IMP",
)

# Nginx Combined Log Regex（兼容宝塔默认格式、含中国标准括号）
_NGINX_COMBINED_RE = re.compile(
    r'(?P<ip>\S+)\s+\S+\s+\S+\s+'
    r'\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+(?P<proto>HTTP/[\d.]+)"\s+'
    r'(?P<status>\d{3})\s+(?P<bytes>\d+)\s+'
    r'"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)"\s*'
)

# 流量系统 app.log 关键字抽取
_IP_TYPE_RE = re.compile(r"ip_type\s*[=:]\s*(\w+)")
_IP_EXIT_RE = re.compile(r"(?:exit[_ -]?ip|出口IP)\s*[=:：]\s*([0-9a-fA-F.:]+)")
_COUNTRY_RE = re.compile(r"country(?:_code)?\s*[=:：]\s*([A-Z]{2})")
_TZ_RE = re.compile(r"timezone\s*[=:：]\s*([/\w-]+)")
_LANG_RE = re.compile(r"(?:language|lang)\s*[=:：]\s*([a-z]{2}(?:-[A-Z]{2})?)")
_UA_RE = re.compile(r"ua\s*[=:：]\s*['\"]?([^'\"]{3,})['\"]?\s", re.IGNORECASE)  # 26.8.13.7 ★ bot UA 漏报补：traffic 日志 ua='...'提取
_HT_IMP_RE = re.compile(r"(?:HT_触发|popunder.*?success|广告位.*?检测到|ad_impressions?\s*[=:：]\s*(\d+))")
# 26.8.13.7 ★ 停留秒数：既支持 "stay=28s" 也支持 "stay_sec=28" / "停留28秒" 无 s 后缀
_STAY_RE = re.compile(r"(?:浏览网站时长|stay(?:_actual|_sec)?|停留.*?审计|停留.*?(?=\d))\s*[=:：]?\s*(\d+(?:\.\d+)?)\s*s?")
_PAGE_CNT_RE = re.compile(r"(?:浏览\s*(\d+)\s*页|pages?\s*[=:：]\s*(\d+))")
_CTR_RE = re.compile(r"(?:ad_click|clicks?)\s*[=:：]\s*(\d+)")
# 26.8.13.7 ★ IPDeep 漏报修复：之前只认 "[IPDeep]" 前缀，但实际 ip_provider 日志多为 [ERROR] [ip_provider] IPDeep ...
#   现在：①任意位置出现 "IPDeep" + 失败关键字 ② 显式 407/502/504/timeout ③ 拒绝/unknown 都命中
_IPDEEP_ERR_RE = re.compile(
    r"(?:\[IPDeep\]|IPDeep\s*(?:接口|取号|获取|拉取|申请)?)\s*.*?(?:失败|timeout|auth|407|502|504|拒绝|unknown且无出口|空响应|认证失败|Proxy Authentication Required)",
    re.IGNORECASE,
)


# ---------------- 数据结构 ----------------
@dataclass
class RTEvent:
    ts: str                          # ISO8601 UTC
    rule_id: str                     # R01~R10
    severity: str                    # WARN|CRIT
    summary: str                     # 人读描述
    facts: Dict[str, Any] = field(default_factory=dict)   # 证据
    auto_fix: Optional[str] = None   # 自动修复建议（给用户/Agent 直接落地的修改代码片段）
    sample_line: Optional[str] = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class RTStats:
    """滑动窗口统计（默认 5 分钟 / 1 小时）"""
    w5_ip_counter: Counter = field(default_factory=Counter)
    w60_ip_counter: Counter = field(default_factory=Counter)
    w5_events: Deque[RTEvent] = field(default_factory=lambda: deque(maxlen=500))
    w5_page_count: Counter = field(default_factory=Counter)      # session_id → 浏览页数
    w5_stays: List[float] = field(default_factory=list)           # 停留秒数样本
    w5_ad_clicks: int = 0
    w5_ad_impressions: int = 0
    ipdeep_consec_fail: int = 0
    last_ht_impression_ts: Optional[float] = None                 # 最近一次 HilltopAds 展示成功时间
    # 26.8.13.7 ★ 新增：R10/R01 计数器 & 时间基准
    _consec_ht_zero: int = 0                                      # 连续 HT 0 展示任务数
    _last_ingest_epoch: float = 0.0                               # 已处理日志的最新时间戳(秒，用于回放历史日志时 R01 窗口判断)


# ---------------- 10 维规则引擎 ----------------
class RiskRuleEngine:
    def __init__(self, stats: RTStats):
        self.stats = stats

    # ----- 规则定义 -----
    def check_R01_IP_FREQ(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R01 同 IP 1 分钟内请求 ≥ 12 次 或 5 分钟 ≥ 40 次（爬虫刷量特征）"""
        global _ip_time_buf
        ip = parsed.get("ip") or parsed.get("exit_ip")
        if not ip:
            return None
        w60_count = sum(
            1 for (_ts, pip) in _ip_time_buf if pip == ip and time.time() - _ts < 60
        )
        w5_count = self.stats.w5_ip_counter.get(ip, 0)
        if w60_count >= 12 or w5_count >= 40:
            sev = "CRIT" if (w60_count >= 20 or w5_count >= 60) else "WARN"
            return RTEvent(
                ts=_iso_now(), rule_id="R01_IP_FREQ", severity=sev,
                summary=f"IP {ip} 请求频率异常: 1min={w60_count}  5min={w5_count}",
                facts={"ip": ip, "w60": w60_count, "w5": w5_count},
                auto_fix=(
                    "【临时】在 nginx site conf 加 limit_req_zone $binary_remote_addr zone=bot:10m rate=30r/m; "
                    "server块 location / { limit_req zone=bot burst=5 nodelay; }\n"
                    "【根因】app.py worker_task 里加 session 级 sleep(0.3~1.2) 随机抖动，"
                    "把 requests_per_minute 上限从 60 → 30"
                ),
            )
        return None

    def check_R02_BOT_UA(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R02 UA 命中 bot/headless/脚本关键字（广告联盟直接拒收）"""
        ua = (parsed.get("ua") or "").lower()
        if not ua:
            return None
        hit = [k for k in (
            "headless", "bot", "crawl", "spider", "phantom", "selenium",
            "webdriver", "curl/", "wget", "python-requests", "scrapy",
            "httpclient", "java/", "go-http-client", "phantomjs", "puppeteer",
        ) if k in ua]
        if hit:
            return RTEvent(
                ts=_iso_now(), rule_id="R02_BOT_UA", severity="CRIT",
                summary=f"UA 命中机器人关键字: {hit} — HilltopAds 判定时 100% 清洗 $0",
                facts={"ua": ua, "hit_keywords": hit},
                auto_fix=(
                    "【根因】selenium_bridge.py new_context 参数中不要自己传固定 UA；"
                    "用 ChromiumDriver 默认真实 UA 再叠加 execute_cdp_cmd 把 UA version 调到 browserVersion；"
                    "app.py 检查  config['ua_use_real_chrome']=True"
                ),
                sample_line=ua,
            )
        return None

    def check_R03_REFERER_MISMATCH(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R03 内页请求 Referer 缺失/或 referer 是外站（直接暴力请求内页）"""
        path = parsed.get("path") or ""
        referer = (parsed.get("referer") or "-").strip()
        host = parsed.get("host") or "freestoryweb.com"
        is_inner_page = path != "/" and not re.search(r"\.(css|js|png|jpg|svg|woff|ico|webp)$", path, re.I)
        if not is_inner_page:
            return None
        missing = referer in ("", "-") or referer == "/"
        wrong_host = (not missing) and host not in referer and "google" not in referer.lower() and "bing" not in referer.lower() and "baidu" not in referer.lower()
        if missing or wrong_host:
            return RTEvent(
                ts=_iso_now(), rule_id="R03_REFERER_MISMATCH", severity="WARN",
                summary=f"内页 {path} Referer{'缺失' if missing else '错配'}: {referer!r}",
                facts={"path": path, "referer": referer},
                auto_fix=(
                    "【根因】worker_task 进入内页时不要 driver.get(url) 直接打开；"
                    "改成从首页 search/href 点击进入；或 page.evaluate 在 request 前加 document.referrer。"
                    "另在 run_drill 中 set_extra_http_headers 给 Referer=上一个页面URL"
                ),
            )
        return None

    def check_R04_CTR_DISTRIBUTION(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R04 CTR（点击/展示）异常：>15%（作弊点击）或 <0.05%（程序化无交互），或 5min 展示 0 次或点击 0"""
        imp = self.stats.w5_ad_impressions
        clk = self.stats.w5_ad_clicks
        if imp == 0 and time.time() - (self.stats.last_ht_impression_ts or time.time()) > 300:
            return RTEvent(
                ts=_iso_now(), rule_id="R04_CTR_DISTRIBUTION", severity="WARN",
                summary="5 分钟窗口广告展示=0，任务侧未触发任何 popunder/广告位",
                facts={"w5_imp": imp, "w5_click": clk},
                auto_fix=(
                    "【优先排查】hilltopads.enabled=false？在 config.json 确认；再确认广告位 zone 不是停用状态。"
                    "app.py worker_task 里 _try_hilltopads_popunder 的 window.open 被 popupblocker 拦了 → "
                    "需要在 browser.new_context 设置 accept_downloads=True + 用 Playwright 自带 route 不拦第三方弹出"
                ),
            )
        if imp > 20:
            ctr = clk / max(imp, 1)
            if ctr > 0.15 or ctr < 0.0005:
                return RTEvent(
                    ts=_iso_now(), rule_id="R04_CTR_DISTRIBUTION", severity="CRIT",
                    summary=f"CTR {ctr*100:.3f}% 严重偏离行业正常(0.2%~2.0%)",
                    facts={"impressions": imp, "clicks": clk, "ctr": round(ctr, 5)},
                    auto_fix=(
                        f"【根因】config['ad_click_prob'] 从 {ctr:.3f} 改成 0.005~0.012 区间随机；"
                        "并强制用户真实点击时必须带 mousedown→mouseup→click 真实事件（不是 page.click() 合成）"
                    ),
                )
        return None

    def check_R05_DC_PROXY_IP(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R05 出口 IP 类型=datacenter/hosting/proxy/vpn 或 ASN 云厂商 → HilltopAds 100% 清洗"""
        ip_type = (parsed.get("ip_type") or "").lower()
        dc_keywords = ("datacenter", "hosting", "proxy", "vpn", "idc", "cloud", "enterprise")
        dc_hit = any(k in ip_type for k in dc_keywords)
        asn_org = (parsed.get("asn_org") or "").lower()
        cloud_hit = any(c in asn_org for c in ("vultr", "digitalocean", "amazon", "aws",
                                               "linode", "ovh", "aliyun", "tencent cloud", "alibaba", "huawei",
                                               "chinatelecom", "chinaunicom-bgp", "ucloud"))
        if dc_hit or cloud_hit:
            return RTEvent(
                ts=_iso_now(), rule_id="R05_DC_PROXY_IP", severity="CRIT",
                summary=f"出口IP类型/ASN 命中数据中心：ip_type={ip_type} asn={asn_org!r} — HilltopAds 无收益",
                facts=parsed,
                auto_fix=(
                    "【根因】ip_provider.py: 给 IPDeep 加参数 ip_type_filter=residential+isp；"
                    "config['proxy_require_residential']=True；若仍出 datacenter，应升级 IPDeep 套餐到 Residential 档位。"
                    "另：禁止 Selenium 任务不加代理直接跑（出口 IP 永远是 VPS 机房 IP，必然 $0）。"
                ),
            )
        return None

    def check_R06_FP_MISMATCH(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R06 四要素冲突：国家/时区/语言/UA locale — 任意一组矛盾即触发"""
        cc = (parsed.get("country") or parsed.get("country_code") or "").upper()
        tz = parsed.get("timezone") or ""
        lang = (parsed.get("language") or parsed.get("lang") or "").lower()
        ua = (parsed.get("ua") or "").lower()
        contradictions = []
        if cc == "US":
            if "asia" in tz or "shanghai" in tz or "tokyo" in tz:
                contradictions.append("US 但 timezone=Asia")
            if lang.startswith("zh") or lang.startswith("ja"):
                contradictions.append("US 但 lang=zh/ja")
            if "zh-cn" in ua and "en-us" not in ua:
                contradictions.append("US UA 中没有 en-US locale")
        elif cc == "CN":
            if "america" in tz or "europe" in tz:
                contradictions.append("CN 但 timezone=欧美")
            if lang.startswith("en") and not lang.startswith("en-us"):
                contradictions.append("CN 但 lang=en（常见代理时区乱配）")
        elif cc == "JP":
            if "shanghai" in tz:
                contradictions.append("JP 但 timezone=上海")
            if not lang.startswith("ja"):
                contradictions.append("JP 但 lang≠ja")
        if contradictions:
            return RTEvent(
                ts=_iso_now(), rule_id="R06_FP_MISMATCH", severity="CRIT",
                summary=f"指纹三要素冲突: {contradictions}",
                facts={"country": cc, "timezone": tz, "lang": lang, "ua": ua[:200]},
                auto_fix=(
                    "【根因】traffic_distribution.py 里 COUNTRY_TZ_STANDARD_OFFSET_HOUR + "
                    "redteam_scenarios.py apply_scenario_to_task 要保证四要素强绑定："
                    " country↔timezone↔language↔UA Accept-Language。推荐用 lookup_country_defaults(cc) 同时取 4 项"
                ),
            )
        return None

    def check_R07_SHORT_STAY(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R07 停留过短：
        ★ CRIT（阻断级）- 单个任务停留 <15s（HilltopAds 计费硬门槛，<15s 必然$0）
        ★ WARN（隐患）- 5分钟窗口内停留<15s样本占比≥60%（批量机器人停留曲线）
        """
        stay = parsed.get("stay_seconds")
        if stay is not None:
            self.stats.w5_stays.append(stay)
        self.stats.w5_stays = [s for s in self.stats.w5_stays if s and time.time() - getattr(s, "_t", 0) < 300] or self.stats.w5_stays[-200:]  # 滑动清理
        buf = _global_stay_buf
        if stay is not None:
            buf.append((time.time(), float(stay)))
        buf_cleaned = [(t, s) for (t, s) in buf if time.time() - t < 300]
        buf.clear(); buf.extend(buf_cleaned)
        # 26.8.13.8 ★ 根因修复：单任务停留<15s 直接 CRIT（HilltopAds 计费硬门槛）
        task_finished = parsed.get("task_finished")
        if stay is not None and task_finished and stay < 15.0:
            return RTEvent(
                ts=_iso_now(), rule_id="R07_SHORT_STAY", severity="CRIT",
                summary=f"任务结束停留={stay:.1f}s，低于 HilltopAds 计费门槛 15s → 必然 $0",
                facts={"stay_seconds": stay, "threshold": 15.0, "task_finished": True},
                auto_fix=(
                    "【根因】app.py worker_task 必须保证停留审计通过后才结束任务："
                    "1) worker_task 尾部 stay_actual < stay_min 时不要 exit，"
                    "   改为 driver.wait_for_timeout( (stay_min-stay_actual)*1000 + jitter )；"
                    "2) Playwright context 的 default_timeout 不要设太矮导致 wait_for_timeout 提前抛；"
                    "3) task_finished 日志仅在 P2-5[停留审计] 达标时才 emit ✅。"
                ),
                sample_line=parsed.get("_raw"),
            )
        if len(buf) >= 5:
            under = sum(1 for (_, s) in buf if s < 15.0)
            ratio = under / len(buf)
            if ratio >= 0.6:
                return RTEvent(
                    ts=_iso_now(), rule_id="R07_SHORT_STAY", severity="WARN",
                    summary=f"5分钟{len(buf)}个停留样本中 <15s 占 {ratio*100:.0f}%（批量机器人停留曲线）",
                    facts={"samples": len(buf), "under_15s": under, "ratio": ratio},
                    auto_fix=(
                        "【根因】worker_task 的 session_deadline 分布过窄，建议换成 lognormal(μ=4.5,σ=0.35) 停留；"
                        "另在 P2-5 停留审计阶段前不要让 close_context 逻辑触发退出。"
                    ),
                )
        return None

    def check_R08_SINGLE_PAGE_SHALLOW(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R08 连续 10 个会话全部只浏览 1 页即离开（无头程序打开即关）"""
        sid = parsed.get("session_id") or parsed.get("ip") or "unknown"
        pc = parsed.get("page_count") or 0
        if pc and pc > 0:
            _session_pages[sid] = pc
        # 只保留最近 20 个会话
        while len(_session_pages) > 20:
            _session_pages.pop(next(iter(_session_pages)))
        vals = list(_session_pages.values())
        if len(vals) >= 10 and vals:
            single = sum(1 for v in vals if v == 1)
            if single / len(vals) >= 0.9:
                return RTEvent(
                    ts=_iso_now(), rule_id="R08_SINGLE_PAGE_SHALLOW", severity="WARN",
                    summary=f"最近{len(vals)}个会话中 {single} 个仅浏览1页（纯机器人打开即跑）",
                    facts={"recent_pages_per_session": vals[:20]},
                    auto_fix=(
                        "【根因】worker_task layer_2 跳转概率(0.35)太低，且 关键词→内页 链接抽取 0 命中。"
                        "改：首页所有 <a href> 都加入候选列表 + 跳转概率 0.7 + 对真实链接点击 mousedown+mouseup。"
                    ),
                )
        return None

    def check_R09_IPDEEP_FAIL(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R09 IPDeep 连续 3 次 auth/timeout 失败 → 任务无代理直连 VPS 机房，收益必然 0"""
        if parsed.get("ipdeep_fail"):
            self.stats.ipdeep_consec_fail += 1
        if parsed.get("ip_type") and parsed["ip_type"] not in ("", "unknown"):
            self.stats.ipdeep_consec_fail = 0  # 有一次成功就重置
        if self.stats.ipdeep_consec_fail >= 3:
            return RTEvent(
                ts=_iso_now(), rule_id="R09_IPDEEP_FAIL", severity="CRIT",
                summary=f"IPDeep 连续失败 {self.stats.ipdeep_consec_fail} 次 — 所有任务都会裸跑 VPS IP！",
                facts={"consecutive_failures": self.stats.ipdeep_consec_fail},
                auto_fix=(
                    "【根因】ip_provider.py: 已在 26.8.13.1 双认证+timeout 120s；若仍失败，检查 proxy_user/proxy_pwd 在"
                    "config.json 中是否正确；再确认 gate.ipdeep.com:8082 从 VPS 本机 telnet 可达（traceroute）。"
                    "临时 fail-open 已在 26.8.13.1 打开（出口IP存在但类型未知时标记 isp_trust_unknown）。"
                ),
            )
        return None

    def check_R10_HT_ZERO_IMP(self, parsed: Dict[str, Any]) -> Optional[RTEvent]:
        """R10 HilltopAds 弹出/展示 0 次 — 任务跑完但广告没触发，必然无收益"""
        task_finished = parsed.get("task_finished")
        imp = parsed.get("ad_impressions") or 0
        popup_ok = parsed.get("hilltopads_popup_ok")
        if task_finished and (imp == 0 or not popup_ok):
            return RTEvent(
                ts=_iso_now(), rule_id="R10_HT_ZERO_IMP", severity="CRIT",
                summary=f"任务结束：HilltopAds 展示次数={imp}, popunder成功={popup_ok} → 必然 $0",
                facts=parsed,
                auto_fix=(
                    "【8项排查清单】1)hilltopads.enabled=true？"
                    "2)出口IP ip_type=residential/isp？非datacenter？"
                    "3)window.open 触发时有真实 user-gesture？不是 setTimeout 里调用？"
                    "4)go.hilltopads.com 在浏览器 Performance 资源里 200 OK？"
                    "5)弹窗停留>15s？6)四要素一致？7)广告位≥1 检测命中？8)主站停留>60s + 浏览≥2页"
                ),
            )
        return None

    # ----- 统一入口 -----
    def run_all(self, parsed: Dict[str, Any]) -> List[RTEvent]:
        events: List[RTEvent] = []
        for rid in RULES:
            try:
                fn = getattr(self, f"check_{rid}", None)
                if fn is None:
                    continue
                ev = fn(parsed)
                if ev:
                    events.append(ev)
            except Exception as e:
                _MON.warning("rule %s raised %s: %s", rid, type(e).__name__, e)
        return events


# ---------------- 全局滑动窗口 ----------------
_ip_time_buf: Deque[Tuple[float, str]] = deque(maxlen=5000)
_global_stay_buf: Deque[Tuple[float, float]] = deque(maxlen=200)
_session_pages: "OrderedDict" = None  # type: ignore[assignment]
try:
    from collections import OrderedDict as _OD
    _session_pages = _OD()
except Exception:
    _session_pages = {}


# ---------------- 日志解析器 ----------------
def parse_nginx_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.rstrip("\r\n")
    m = _NGINX_COMBINED_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    try:
        d["status"] = int(d["status"])
    except Exception:
        pass
    try:
        d["bytes"] = int(d["bytes"])
    except Exception:
        d["bytes"] = 0
    d["_src"] = "nginx"
    d["_raw"] = line
    # 窗口统计
    _ip_time_buf.append((time.time(), d["ip"]))
    return d


def parse_traffic_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    d: Dict[str, Any] = {"_src": "traffic", "_raw": line}
    for (key, rx, fn) in (
        ("ip_type", _IP_TYPE_RE, None),
        ("exit_ip", _IP_EXIT_RE, None),
        ("country", _COUNTRY_RE, None),
        ("timezone", _TZ_RE, None),
        ("language", _LANG_RE, None),
        ("stay_seconds", _STAY_RE, float),
        ("ipdeep_fail", _IPDEEP_ERR_RE, lambda m: True),
    ):
        m = rx.search(line)
        if m:
            try:
                d[key] = fn(m.group(1)) if fn and m.lastindex else (True if fn else m.group(1))
            except Exception:
                pass
    # ad_impressions / clicks / pages
    m = re.search(r"ad_impressions?\s*[=:：]\s*(\d+)", line)
    if m:
        d["ad_impressions"] = int(m.group(1))
    m = re.search(r"(?:ad_click|clicks?)\s*[=:：]\s*(\d+)", line)
    if m:
        d["ad_clicks"] = int(m.group(1))
    m = re.search(r"(?:浏览\s*(\d+)\s*页|pages?\s*[=:：]\s*(\d+))", line)
    if m:
        d["page_count"] = int(m.group(1) or m.group(2) or 0)
    d["hilltopads_popup_ok"] = bool(
        re.search(r"(popunder\s*trigger\s*success|HT弹出.*?ok|HT_触发.*?success|🎯.*?popunder)", line, re.I)
    )
    # 26.8.13.8 ★ task_finished 判定扩展：
    #   ①明确结束标记 ② P2-5停留审计行（不管达标/不达标，都代表该任务"停留阶段已出结论 → 等价任务结束"，让R07能拿到 task_finished+stay_seconds 双命中）
    d["task_finished"] = bool(
        re.search(
            r"(P2-5\[停留审计\]|worker_task.*?finish|✅ 任务.*?结束|task.*?success:\s*(True|False)|任务.*?(结束|完成))",
            line, re.I,
        )
    )
    if d["hilltopads_popup_ok"]:
        d["ad_impressions"] = max(d.get("ad_impressions", 0), 1)
    return d


# ---------------- Log Tailer（文件轮转兼容） ----------------
class FileTailer:
    """tail -F：文件被 rotate/inode 变化时自动重开"""

    def __init__(self, path: str, *, idle_sleep: float = 0.3, block: bool = True):
        self.path = path
        self.idle_sleep = idle_sleep
        self.block = block
        self._fh = None
        self._inode = None
        self._open()

    def _open(self):
        try:
            p = Path(self.path)
            if not p.exists():
                self._fh = None
                return
            self._fh = p.open("r", encoding="utf-8", errors="ignore")
            self._fh.seek(0, os.SEEK_END)
            self._inode = p.stat().st_ino
        except Exception as e:
            _MON.warning("tail open %s failed: %s", self.path, e)
            self._fh = None

    def __iter__(self) -> Iterable[str]:
        while True:
            if self._fh is None:
                self._open()
                time.sleep(self.idle_sleep)
                continue
            line = self._fh.readline()
            if line:
                yield line
                continue
            # EOF: check rotate
            try:
                now_inode = Path(self.path).stat().st_ino
            except FileNotFoundError:
                now_inode = None
            if now_inode != self._inode:
                _MON.info("log rotated, re-opening %s", self.path)
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._open()
            if not self.block:
                break
            time.sleep(self.idle_sleep)

    def close(self):
        try:
            if self._fh:
                self._fh.close()
        finally:
            self._fh = None


# ---------------- 事件持久化 + 事件流 ----------------
def _ensure_writable_dir(requested_dir: str, fallback_subdir: str = "traffic_monitor") -> Path:
    """26.8.13.7 ★ 当 requested_dir 因权限/沙箱/ACL 不可写时，回退到 ~/.cache/<fallback_subdir>.

    原因: macOS 沙箱或 com.apple.quarantine 会对指定目录抛 Operation not permitted;
          宝塔 VPS 若 supervisord 以只读挂载 APP_DIR 也会失败。
    """
    d = Path(requested_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 实际写一个临时文件确认（mkdir成功但文件写入失败依然存在）
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except Exception as _e1:
        fallback_base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        fall = fallback_base / fallback_subdir
        try:
            fall.mkdir(parents=True, exist_ok=True)
            probe = fall / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _MON.warning(
                "[事件存储降级] 请求目录不可写(%s: %s) → 回退到: %s",
                _e1.__class__.__name__, str(_e1)[:100], fall,
            )
            return fall
        except Exception as _e2:
            raise RuntimeError(
                f"无法写入事件目录。首选: {d} ({_e1}), 回退: {fall} ({_e2})"
            ) from _e2


class EventStore:
    def __init__(self, events_dir: str):
        self.dir: Path = _ensure_writable_dir(events_dir, "traffic_monitor")
        self.jsonl_path = self.dir / "rt_events.jsonl"
        self._lock = threading.Lock()
        self._ring: Deque[RTEvent] = deque(maxlen=500)
        self._subscribers: List[queue.Queue] = []

    def add(self, ev: RTEvent) -> None:
        with self._lock:
            self._ring.append(ev)
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(ev.to_jsonl() + "\n")
            for q in list(self._subscribers):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass
        # 控制台彩色输出
        color = "\033[33m" if ev.severity == "WARN" else "\033[31m"
        reset = "\033[0m"
        icon = "🟡" if ev.severity == "WARN" else "🔴"
        _MON.log(
            logging.WARNING if ev.severity == "WARN" else logging.ERROR,
            "%s %s[%s] %s%s\n      修复建议: %s",
            icon, color, ev.rule_id, ev.summary, reset, ev.auto_fix or "暂无",
        )

    def recent(self, limit: int = 100, severity: Optional[str] = None,
               rule_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for ev in reversed(self._ring):
                if severity and ev.severity != severity:
                    continue
                if rule_id and ev.rule_id != rule_id:
                    continue
                out.append(asdict(ev))
                if len(out) >= limit:
                    break
            return out

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


# ---------------- HilltopAds 8 项清单评分器 ----------------
def hilltopads_score(stats: RTStats, last_event: Optional[RTEvent]) -> Dict[str, Any]:
    checks: Dict[str, Tuple[str, bool]] = {}
    # 近似评分（没日志字段时基于最近告警推断）
    crit = any(e.severity == "CRIT" for e in list(stats.w5_events)[-50:])
    # 项权重 0-100
    checks["R01 no frequency abuse"] = ("R01_IP_FREQ", not any(e.rule_id == "R01_IP_FREQ" and e.severity == "CRIT" for e in list(stats.w5_events)[-50:]))
    checks["R02 no bot UA"] = ("R02_BOT_UA", not any(e.rule_id == "R02_BOT_UA" for e in list(stats.w5_events)[-50:]))
    checks["R03 Referer OK"] = ("R03_REFERER_MISMATCH", not any(e.rule_id == "R03_REFERER_MISMATCH" and e.severity == "CRIT" for e in list(stats.w5_events)[-50:]))
    checks["R05 no datacenter IP"] = ("R05_DC_PROXY_IP", not any(e.rule_id == "R05_DC_PROXY_IP" for e in list(stats.w5_events)[-50:]))
    checks["R06 4-way fingerprint consistent"] = ("R06_FP_MISMATCH", not any(e.rule_id == "R06_FP_MISMATCH" for e in list(stats.w5_events)[-50:]))
    checks["R07 stay>15s OK"] = ("R07_SHORT_STAY", not any(e.rule_id == "R07_SHORT_STAY" for e in list(stats.w5_events)[-50:]))
    checks["R09 IPDeep not failing"] = ("R09_IPDEEP_FAIL", not any(e.rule_id == "R09_IPDEEP_FAIL" for e in list(stats.w5_events)[-50:]))
    checks["R10 HT popunder triggered"] = ("R10_HT_ZERO_IMP", not any(e.rule_id == "R10_HT_ZERO_IMP" for e in list(stats.w5_events)[-50:]))
    score = round(100 * sum(v for (_, v) in checks.values()) / max(len(checks), 1))
    verdict = {
        (90, 101): "🟢 极高概率入账",
        (70, 90):  "🟡 大概率入账（偶有小瑕疵）",
        (35, 70):  "🟠 偏低概率（至少 1 项阻断级命中）",
        (0, 35):   "🔴 必然 $0（多项阻断级）",
    }
    v_label = "🔴 必然 $0"
    for (a, b), label in verdict.items():
        if a <= score < b:
            v_label = label
            break
    return {
        "ts": _iso_now(),
        "score_0_100": score,
        "verdict": v_label,
        "blocked_by_CRIT": crit,
        "checks_breakdown": {k: {"rule_id": r, "pass": ok} for k, (r, ok) in checks.items()},
    }


# 26.8.13.8 ★ 兼容别名（部分调用方习惯用 compute_ 前缀）
def compute_hilltopads_score(stats: RTStats, last_event: Optional[RTEvent] = None) -> Dict[str, Any]:
    """hilltopads_score 的别名，兼容旧代码和 test_client 调用"""
    data = hilltopads_score(stats, last_event)
    # checks_breakdown: {"R01 xxx": {"rule_id": "R01_XXX", "pass": True/False}}
    passed = sum(1 for v in data["checks_breakdown"].values() if v["pass"])
    total = len(data["checks_breakdown"])
    breakdown = []
    for k, v in data["checks_breakdown"].items():
        score_12 = round(12 * (1 if v["pass"] else 0))
        breakdown.append({
            "name": k, "score": score_12, "verdict": "OK" if v["pass"] else "FAIL",
            "rule_id": v["rule_id"],
        })
    return {
        "ts": data["ts"],
        "score": data["score_0_100"],
        "verdict": data["verdict"],
        "breakdown": breakdown,
        "passed": passed,
        "total": total,
        "raw": data,
    }


# ---------------- 监控主循环（线程或独立进程） ----------------
_STATS = RTStats()
_ENGINE = RiskRuleEngine(_STATS)
_STORE: Optional[EventStore] = None
_running = False


def _window_cleaner_worker():
    """5 秒一次清理 w5/w60 计数器"""
    global _running
    w5_start = time.time()
    ip_counter_snapshot: Counter = Counter()
    while _running:
        try:
            now = time.time()
            # 滑动窗口：w5_ip_counter 采用简单分段重置
            if now - w5_start > 300:
                _STATS.w5_ip_counter.clear()
                ip_counter_snapshot.clear()
                w5_start = now
            # 清理 w5_events 超过 5 分钟的
            cutoff = now - 300
            while _STATS.w5_events and (datetime.fromisoformat(_STATS.w5_events[0].ts).timestamp() < cutoff):
                _STATS.w5_events.popleft()
        except Exception as e:
            _MON.warning("window_cleaner err: %s", e)
        time.sleep(5)


def _ingest_line(line: str, source: str):
    """单条日志行→解析→跑引擎→落事件"""
    try:
        if source == "nginx":
            parsed = parse_nginx_line(line)
        else:
            parsed = parse_traffic_line(line)
        if parsed is None:
            return
        # 累积统计
        if source == "nginx":
            _STATS.w5_ip_counter[parsed["ip"]] += 1
            if parsed.get("status") in (200, 304):
                _STATS.w60_ip_counter[parsed["ip"]] += 1
        else:
            ip = parsed.get("exit_ip")
            if ip:
                _STATS.w5_ip_counter[ip] += 1
            if "ad_impressions" in parsed:
                imp = parsed["ad_impressions"] or 0
                if imp > 0:
                    _STATS.w5_ad_impressions += imp
                    _STATS.last_ht_impression_ts = time.time()
            if "ad_clicks" in parsed:
                _STATS.w5_ad_clicks += int(parsed.get("ad_clicks") or 0)
        events = _ENGINE.run_all(parsed)
        for ev in events:
            ev.sample_line = parsed.get("_raw")[:400] if isinstance(parsed.get("_raw"), str) else None
            if _STORE:
                _STORE.add(ev)
            _STATS.w5_events.append(ev)
    except Exception as e:
        _MON.warning("ingest_line %s failed: %s: %s", source, type(e).__name__, e)


def _tail_worker(path: str, source: str):
    global _running
    tailer = FileTailer(path)
    try:
        for line in tailer:
            if not _running:
                break
            _ingest_line(line, source)
    finally:
        tailer.close()


def start_background_monitor(
    *,
    nginx_log: Optional[str] = None,
    traffic_log: Optional[str] = None,
    events_dir: str = "./monitor",
) -> threading.Thread:
    """作为 Flask 后台线程启动监控（与 Web 共用进程）"""
    global _running, _STORE
    if _running:
        raise RuntimeError("monitor already running")
    _STORE = EventStore(events_dir)
    _running = True
    atexit.register(stop_background_monitor)

    threads: List[threading.Thread] = []
    if nginx_log:
        t = threading.Thread(target=_tail_worker, args=(nginx_log, "nginx"),
                             name="mon-nginx", daemon=True)
        threads.append(t)
    if traffic_log:
        t = threading.Thread(target=_tail_worker, args=(traffic_log, "traffic"),
                             name="mon-traffic", daemon=True)
        threads.append(t)
    threads.append(threading.Thread(target=_window_cleaner_worker, name="mon-window", daemon=True))
    for t in threads:
        t.start()
    _MON.info("✅ traffic_monitor started (nginx=%s traffic=%s events_dir=%s)",
              nginx_log, traffic_log, str(_STORE.dir))

    # 返回一个对外 handle 线程，方便调用方 join
    supervisor = threading.Thread(
        target=lambda: [t.join() for t in threads if t.is_alive()],
        name="mon-supervisor", daemon=True,
    )
    supervisor.start()
    return supervisor


def stop_background_monitor():
    global _running
    if not _running:
        return
    _running = False
    _MON.info("traffic_monitor stopping...")


# ---------------- Flask Blueprint: /monitoring API ----------------
from flask import Blueprint, Response, jsonify, request, stream_with_context, render_template_string  # noqa: E402

monitor_bp = Blueprint("monitor", __name__, url_prefix="/monitoring")


@monitor_bp.route("/api/status")
def api_status():
    data = {
        "app_version": APP_VERSION,
        "running": _running,
        "events_file": str(_STORE.jsonl_path) if _STORE else None,
        "stats": {
            "w5_events_total": len(_STATS.w5_events),
            "w5_ad_impressions": _STATS.w5_ad_impressions,
            "w5_ad_clicks": _STATS.w5_ad_clicks,
            "ipdeep_consec_failures": _STATS.ipdeep_consec_fail,
            "w5_ip_counter_top5": _STATS.w5_ip_counter.most_common(5),
        },
    }
    return jsonify({"success": True, "data": data})


@monitor_bp.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 100) or 100)
    severity = request.args.get("severity") or None
    rule_id = request.args.get("rule_id") or None
    data = _STORE.recent(limit=limit, severity=severity, rule_id=rule_id) if _STORE else []
    return jsonify({"success": True, "count": len(data), "data": data})


@monitor_bp.route("/api/hilltopads-score")
def api_ht_score():
    last = _STATS.w5_events[-1] if _STATS.w5_events else None
    return jsonify({"success": True, "data": hilltopads_score(_STATS, last)})


@monitor_bp.route("/api/stream")
def api_stream():
    """Server-Sent Events 实时推送告警事件"""
    if _STORE is None:
        return jsonify({"success": False, "message": "monitor not started"}), 500
    q = _STORE.subscribe()

    def gen():
        try:
            yield ":ok\n\n"
            while True:
                try:
                    ev: RTEvent = q.get(timeout=15)
                except queue.Empty:
                    yield ":ping\n\n"
                    continue
                yield f"event: {ev.severity.lower()}\n"
                yield f"data: {ev.to_jsonl()}\n\n"
        finally:
            _STORE.unsubscribe(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><title>🚦 Traffic Monitor 26.8.13.7</title>
<style>
body{background:#020617;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif;margin:0;padding:20px}
h1{font-size:18px;margin:0 0 14px}
.card{background:#0f172a;border-radius:10px;padding:14px;margin-bottom:14px;border:1px solid #1e293b}
.score{font-size:34px;font-weight:700;margin:4px 0}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-right:6px}
.tag-WARN{background:#facc1522;color:#facc15;border:1px solid #facc1555}
.tag-CRIT{background:#ef444422;color:#ef4444;border:1px solid #ef444455}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
th,td{padding:5px 8px;border-bottom:1px solid #1e293b;text-align:left;vertical-align:top}
th{color:#94a3b8;font-weight:500}
tr:hover td{background:#1e293b44}
.rule-link{color:#38bdf8}
.fix{background:#020617;border:1px dashed #1e293b;border-radius:6px;padding:8px;color:#cbd5e1;line-height:1.6}
.btns{display:flex;gap:6px;margin:8px 0;flex-wrap:wrap}
button{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
button.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
#streamLog{max-height:36vh;overflow-y:auto;background:#020617;border:1px solid #1e293b;border-radius:6px;padding:8px;font-size:12px;line-height:1.6}
</style></head><body>
<h1>🚦 Traffic Monitor <span style="color:#64748b">v26.8.13.7</span>
 <a href="/monitoring/api/status" style="color:#64748b;font-size:12px">[status.json]</a>
 <a href="/monitoring/api/hilltopads-score" style="color:#64748b;font-size:12px">[HT评分.json]</a>
 <a href="/monitoring/api/events?limit=50" style="color:#64748b;font-size:12px">[最近50事件.json]</a>
</h1>
<div class="card"><div id="htScore">加载中…</div></div>
<div class="card">
  <div class="btns">
    <button data-f="all" class="active">全部事件</button>
    <button data-f="CRIT">🔴 阻断级</button>
    <button data-f="WARN">🟡 警告级</button>
    <button onclick="location.reload()">刷新</button>
  </div>
  <table><thead><tr><th style="width:160px">时间(UTC)</th><th style="width:80px">级别</th>
    <th style="width:110px">规则ID</th><th>摘要</th><th style="width:48px">详情</th></tr></thead>
  <tbody id="evtBody"></tbody></table>
</div>
<div class="card">
  <div style="color:#94a3b8;font-size:12px;margin-bottom:6px;">📡 SSE 实时事件流（新事件自动推送，无需手动刷新）</div>
  <div id="streamLog"></div>
</div>
<script>
let curFilter='all';
function fmtSeverity(s){return '<span class="tag tag-'+s+'">'+s+'</span>'}
async function loadScore(){
  const d=await fetch('/monitoring/api/hilltopads-score').then(r=>r.json()).catch(()=>null);
  const el=document.getElementById('htScore');
  if(!d||!d.success){el.innerHTML='<span style="color:#94a3b8">监控未启动（VPS请先运行 python3 traffic_monitor.py --daemon）</span>';return}
  const s=d.data;let color='#94a3b8';
  if(s.score_0_100>=90)color='#22c55e';else if(s.score_0_100>=70)color='#facc15';else if(s.score_0_100>=35)color='#fb923c';else color='#ef4444';
  let html=`<div>🏁 HilltopAds 入账概率评分 <span style="font-size:12px;color:#64748b">${s.ts}</span></div>
    <div class="score" style="color:${color}">${s.score_0_100}/100  <span style="font-size:14px">${s.verdict}</span></div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px;font-size:12px;">`;
  for(const [k,v] of Object.entries(s.checks_breakdown)){
    html+=`<div style="background:#020617;padding:6px 8px;border-radius:6px;border:1px solid #1e293b;">
      <div style="color:${v.pass?'#22c55e':'#ef4444'};font-weight:600">${v.pass?'✅':'❌'} ${k}</div>
      <div style="color:#64748b;font-size:11px">rule: ${v.rule_id}</div></div>`;
  }
  html+='</div>';el.innerHTML=html;
}
async function loadEvents(){
  const q=curFilter==='all'?'':('severity='+curFilter);
  const d=await fetch('/monitoring/api/events?limit=200&'+q).then(r=>r.json()).catch(()=>({success:false}));
  const b=document.getElementById('evtBody');if(!b)return;
  if(!d.success){b.innerHTML='<tr><td colspan=5 style="color:#94a3b8">暂无事件 / 监控未启动</td></tr>';return}
  b.innerHTML=d.data.map(e=>`<tr><td>${e.ts.replace('T',' ').slice(0,19)}</td><td>${fmtSeverity(e.severity)}</td>
    <td><a class="rule-link" href="#${e.rule_id}">${e.rule_id}</a></td>
    <td>${e.summary}${e.auto_fix?'<br><div class=fix>🔧修复建议：<br>'+e.auto_fix.replace(/\n/g,'<br>')+'</div>':''}</td>
    <td style="font-size:11px;color:#64748b;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${(e.sample_line||'').replace(/"/g,'&quot;')}">${(e.sample_line||'').slice(0,60)||'—'}</td></tr>`).join('')||'<tr><td colspan=5 style="color:#94a3b8">暂无事件</td></tr>';
}
document.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button[data-f]').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');curFilter=b.dataset.f;loadEvents();
});
// SSE stream
function startSSE(){
  const es=new EventSource('/monitoring/api/stream');
  const log=document.getElementById('streamLog');
  ['warn','crit'].forEach(lv=>es.addEventListener(lv,ev=>{
    try{const d=JSON.parse(ev.data);
      const sev=lv.toUpperCase();
      const col=sev==='CRIT'?'#ef4444':'#facc15';
      const line=`<div style="border-bottom:1px dashed #1e293b;padding:2px 0;">
        <span style="color:${col}">${d.rule_id}</span>
        <span style="color:#64748b">${d.ts.replace('T',' ').slice(11,19)}</span>
        <span>${d.summary}</span></div>`;
      log.innerHTML=line+log.innerHTML;loadScore();loadEvents();
    }catch(_){}
  }));
  es.onerror=()=>{es.close();setTimeout(startSSE,3000);};
}
loadScore();loadEvents();startSSE();
setInterval(()=>{loadScore()},60000);
</script></body></html>
"""


@monitor_bp.route("/")
def dashboard():
    return Response(render_template_string(_DASHBOARD_HTML), mimetype="text/html; charset=utf-8")


# ---------------- CLI 入口（独立守护） ----------------
def _cli_self_check_smoke():
    """最小自检：生成 1 条 nginx + 1 条 traffic 日志，跑规则引擎，确认能输出 CRIT"""
    _MON.info("🧪 内置 smoke test 开始")
    tmp_dir = "/tmp/traffic_monitor_smoke"
    os.makedirs(tmp_dir, exist_ok=True)
    ev = EventStore(tmp_dir)
    global _STORE
    _bak = _STORE
    _STORE = ev
    try:
        # 故意一条 Headless Chrome UA
        nginx_line = (
            '45.76.12.9 - - [13/Aug/2026:10:45:12 +0000] '
            '"GET /article/123 HTTP/1.1" 200 23450 "-" '
            '"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/129.0 Safari/537.36"'
        )
        traffic_line = (
            "2026-08-13 10:45:00 INFO  出口IP=45.76.12.90 ip_type=datacenter "
            "country=US timezone=Asia/Shanghai language=zh-CN 浏览网站时长=4.8s "
            "浏览 1 页 ad_impressions=0 popunder trigger failed"
        )
        _ingest_line(nginx_line, "nginx")
        _ingest_line(traffic_line, "traffic")
        crit_count = sum(1 for e in ev.recent(limit=50) if e["severity"] == "CRIT")
        if crit_count < 3:
            _MON.error("smoke test fail: CRIT events expected >=3, got %d", crit_count)
            return False
        _MON.info("🧪 smoke test 通 过: CRIT events=%d ≥ 3 ✅", crit_count)
        return True
    finally:
        _STORE = _bak
        try:
            os.remove(os.path.join(tmp_dir, "rt_events.jsonl"))
            os.rmdir(tmp_dir)
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description="traffic_monitor (v%s)" % APP_VERSION)
    p.add_argument("--nginx-log", help="Nginx access.log 绝对路径（宝塔：/www/wwwlogs/<域名>.log）")
    p.add_argument("--traffic-log", default=None, help="流量系统 app.log 绝对路径（建议：/root/selenium_traffic_system/app.log）")
    p.add_argument("--events-dir", default="./monitor", help="事件/告警持久化目录")
    p.add_argument("--daemon", action="store_true", help="前台守护式 tail -F 持续监控（Ctrl+C 停止）")
    p.add_argument("--smoke-test", action="store_true", help="内置最小自检，立即退出")
    args = p.parse_args()

    global _running, _STORE
    if args.smoke_test:
        ok = _cli_self_check_smoke()
        raise SystemExit(0 if ok else 1)

    if not (args.nginx_log or args.traffic_log):
        _MON.error("至少传 --nginx-log 或 --traffic-log 一个")
        p.print_help()
        raise SystemExit(2)
    # 自动推断 traffic-log（与 app.py 26.8.13.4+ 版本号匹配：日志落在 APP_DIR/app.log）
    if not args.traffic_log:
        candidate = os.path.join(os.path.abspath(os.path.dirname(__file__)), "app.log")
        if os.path.exists(candidate):
            _MON.info("--traffic-log 未传，自动用脚本同目录 app.log = %s", candidate)
            args.traffic_log = candidate

    _STORE = EventStore(args.events_dir)
    _running = True

    def _shutdown(signum, _frame):
        global _running
        _running = False
        _MON.info("收到信号 %s，准备退出…", signum)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _shutdown)
        except Exception:
            pass

    threads: List[threading.Thread] = []
    if args.nginx_log:
        threads.append(threading.Thread(target=_tail_worker, args=(args.nginx_log, "nginx"),
                                        name="mon-nginx", daemon=True))
    if args.traffic_log:
        threads.append(threading.Thread(target=_tail_worker, args=(args.traffic_log, "traffic"),
                                        name="mon-traffic", daemon=True))
    threads.append(threading.Thread(target=_window_cleaner_worker, name="mon-window", daemon=True))
    for t in threads:
        t.start()

    # 同时把监控蓝图挂在一个小 Flask（若用户想直接看 dashboard）
    if args.daemon:
        try:
            from flask import Flask
            app = Flask(__name__)
            app.register_blueprint(monitor_bp)
            port = int(os.environ.get("MONITOR_PORT", "8787"))
            _MON.info("📊 Dashboard: http://<VPS_IP>:8787/monitoring/")
            # 用 debug=False，避免 Werkzeug reloader 把 tail 线程再拉一遍
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop_background_monitor()
    else:
        # 只做 tail 不占端口：挂到主线程等信号
        try:
            while _running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop_background_monitor()


# ---------------- 工具函数 ----------------
def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    main()
