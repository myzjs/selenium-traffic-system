from flask import Flask, render_template_string, request, jsonify, Response, send_from_directory
import requests
from requests.auth import HTTPBasicAuth
import urllib.parse
import re
from urllib.parse import urljoin
import threading
import logging
import json
import copy
import subprocess
import os
import time
import random
import uuid
import math
import pytz
from datetime import datetime
from selenium_bridge import sync_playwright, PlaywrightTimeoutError, Stealth
import selenium_bridge as _selenium_bridge

# 向 selenium_bridge 注册停止检查回调：任一任务停止时，让 bridge 内部的
# goto/wait 等阻塞循环能及时中断（解决"点停止后仍卡在页面加载等待里"的问题）。
def _bridge_should_stop():
    return not globals().get("task_running", True)
_selenium_bridge.set_stop_check(_bridge_should_stop)

# 导入新创建的模块
import sys, os

# ========== 加载 .env 文件（若存在）==========
def _load_dotenv(dotenv_path=None):
    """简易 .env 加载器，避免引入 python-dotenv 依赖"""
    if dotenv_path is None:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:  # 不覆盖已有环境变量
                os.environ[key] = value

_load_dotenv()

print("Current directory:", os.getcwd())
print("sys.path:", sys.path)
from seo_query_module import get_seo_query
from ip_region_module import get_ip_recognizer, REGION_CHINA, REGION_US_EU, REGION_OTHER, REGION_FAILED
from ip_info_resolver import resolve_ip_info
import ip_provider as _ip_provider

_xvfb_process = None
_xvfb_lock = threading.Lock()

# ========== 时区和工作时间判断函数 ==========
COUNTRY_TIMEZONE_MAP = {
    # 原有国家
    "US": "America/New_York",      # 美国 - 纽约
    "GB": "Europe/London",         # 英国 - 伦敦
    "DE": "Europe/Berlin",         # 德国 - 柏林
    "FR": "Europe/Paris",          # 法国 - 巴黎
    "JP": "Asia/Tokyo",            # 日本 - 东京
    
    # 新增国家
    "SG": "Asia/Singapore",        # 新加坡
    "HK": "Asia/Hong_Kong",        # 香港
    "ID": "Asia/Jakarta",          # 印度尼西亚 - 雅加达
    "AU": "Australia/Sydney",      # 澳大利亚 - 悉尼
    "NZ": "Pacific/Auckland",      # 新西兰 - 奥克兰
}

def get_timezone_for_country(country_code):
    """根据国家代码获取主城市时区"""
    return COUNTRY_TIMEZONE_MAP.get(country_code.upper(), "America/New_York")  # 默认美国东部

def is_working_hours(country_code):
    """
    判断指定国家是否处于工作时间（当地时间7:00-24:00）
    
    Args:
        country_code: 国家代码（如 US, SG, HK 等）
    
    Returns:
        bool: 是否在工作时间内
    """
    timezone_str = get_timezone_for_country(country_code)
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.exceptions.UnknownTimeZoneError:
        log.warning(f"⚠️ 未知时区 {timezone_str}，默认使用美国东部时区")
        tz = pytz.timezone("America/New_York")
    
    # 获取当前当地时间
    local_now = datetime.now(tz)
    hour = local_now.hour
    
    # 判断是否在 7:00-24:00 之间
    is_working = 7 <= hour < 24
    
    log.debug(
        f"🕐 国家 {country_code} 时区 {timezone_str}, "
        f"当地时间 {local_now.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"工作时间: {'✓ 是' if is_working else '✗ 否'}"
    )
    
    return is_working

def get_available_proxies(proxy_pool):
    """
    从代理池中筛选出当前处于工作时间的代理
    
    Args:
        proxy_pool: 代理池列表
    
    Returns:
        list: 可用的代理列表
    """
    available_proxies = []
    for proxy in proxy_pool:
        country_code = proxy.get("country_code", "US")
        if is_working_hours(country_code):
            available_proxies.append(proxy)
            log.debug(
                f"✅ 代理 {country_code} 处于工作时间，加入可用池"
            )
        else:
            log.debug(
                f"⏰ 代理 {country_code} 不在工作时间，跳过"
            )
    return available_proxies

# ========== 流量模型函数 ==========
def clamp_hour(h):
    """限制在 0-24 小时，循环边界"""
    return ((h % 24) + 24) % 24

def generate_normal_hours(num_tasks):
    """普通正态分布模型：平稳，均值12点，标准差6小时"""
    hours = []
    for _ in range(num_tasks):
        h = clamp_hour(random.normalvariate(12, 6))
        hours.append(h)
    return sorted(hours)

def generate_gamma_hours(num_tasks):
    """伽马分布模型：活动突增，形状2.5，速率0.2"""
    hours = []
    for _ in range(num_tasks):
        h = clamp_hour(random.gammavariate(2.5, 5))
        hours.append(h)
    return sorted(hours)

def generate_poisson_hours(num_tasks):
    """泊松分布模型：秒级脉冲，均匀分布在 0-24 小时内"""
    hours = []
    # 从随机起点开始（避免总是从0点开始）
    cumulative = random.uniform(0, 24)
    for _ in range(num_tasks):
        hours.append(cumulative % 24)
        # 平均间隔 1/12 小时 = 5 分钟
        interval_h = random.expovariate(12)
        cumulative += interval_h
    return sorted([h % 24 for h in hours])

def generate_bimodal_hours(num_tasks):
    """双峰混合正态分布模型：早晚高峰（早9晚9）"""
    hours = []
    for _ in range(num_tasks):
        if random.random() < 0.5:
            h = clamp_hour(random.normalvariate(9, 2))
        else:
            h = clamp_hour(random.normalvariate(21, 2))
        hours.append(h)
    return sorted(hours)

def generate_burst_hours(num_tasks):
    """突发流量模型：模拟真实用户的突发访问行为（如热点事件、社交分享）"""
    hours = []
    if num_tasks <= 0:
        return []
    # 随机选择 1-3 个突发时段
    num_bursts = random.randint(1, min(3, max(1, num_tasks // 5)))
    burst_centers = [random.uniform(6, 23) for _ in range(num_bursts)]
    tasks_per_burst = num_tasks // num_bursts
    remainder = num_tasks % num_bursts
    
    for i, center in enumerate(burst_centers):
        count = tasks_per_burst + (1 if i < remainder else 0)
        # 突发时段内使用较集中的分布（标准差 0.5-1.5 小时）
        spread = random.uniform(0.5, 1.5)
        for _ in range(count):
            h = clamp_hour(random.normalvariate(center, spread))
            hours.append(h)
    
    # 添加少量背景流量（10-20%）
    background_count = max(1, int(num_tasks * random.uniform(0.1, 0.2)))
    for _ in range(background_count):
        h = clamp_hour(random.uniform(7, 23))
        hours.append(h)
    
    return sorted(hours[:num_tasks])  # 确保不超过 num_tasks


def get_weekend_holiday_multiplier(date_obj):
    """根据日期返回流量倍率（模拟真实网站的周末/节假日流量波动）
    
    - 工作日: ~1.0（基准，周一略低、周五略高）
    - 周六: 0.75~0.85（周末流量通常下降）
    - 周日: 0.65~0.80（周日更低）
    - 节假日: 0.50~0.70（重大节日流量大幅下降）
    """
    weekday = date_obj.weekday()  # 0=周一, 6=周日
    month, day = date_obj.month, date_obj.day
    
    # 简单节假日检测（主要西方节日）
    major_holidays = [
        (1, 1),   # 元旦
        (12, 25), # 圣诞节
        (12, 24), # 平安夜
        (7, 4),   # 美国独立日
    ]
    # 感恩节（11月第四个周四）
    if month == 11 and weekday == 3 and 22 <= day <= 28:
        return random.uniform(0.50, 0.70)
    if (month, day) in major_holidays:
        return random.uniform(0.50, 0.70)
    
    if weekday == 5:  # 周六
        return random.uniform(0.75, 0.85)
    elif weekday == 6:  # 周日
        return random.uniform(0.65, 0.80)
    else:  # 工作日微小波动
        base = {0: 0.92, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.05}.get(weekday, 1.0)
        return base * random.uniform(0.95, 1.05)


MODEL_FUNCTIONS = {
    "normal": generate_normal_hours,
    "gamma": generate_gamma_hours,
    "poisson": generate_poisson_hours,
    "bimodal": generate_bimodal_hours,
    "burst": generate_burst_hours
}

def get_site_age_category(site_creation_date_str):
    """根据建站日期字符串（YYYY-MM-DD）返回：new/mid/old"""
    if not site_creation_date_str:
        return "old"
    from datetime import datetime
    try:
        creation_date = datetime.strptime(site_creation_date_str, "%Y-%m-%d")
        today = datetime.now()
        age_days = (today - creation_date).days
        if age_days <= 30:
            return "new"
        elif age_days <= 60:
            return "mid"
        else:
            return "old"
    except Exception:
        return "old"

# ========== 全球时段调度辅助函数（24小时全球分布）==========
def get_country_working_seconds_today(country_code, base_date=None):
    """
    获取指定国家"今天"的工作时段（转换为UTC秒数，从今天UTC 00:00 起）
    工作时段：当地时间 7:00-24:00（17小时）
    
    Returns:
        list of tuples: [(start_utc_sec, end_utc_sec), ...]
        可能跨日，所以可能返回1或2段
    """
    import datetime as _dt
    
    tz_str = get_timezone_for_country(country_code)
    try:
        tz = pytz.timezone(tz_str)
    except Exception:
        tz = pytz.timezone("America/New_York")
    
    if base_date is None:
        base_date = _dt.datetime.now(pytz.UTC).date()
    
    # 今日UTC 00:00 锚点
    utc_today_start = _dt.datetime.combine(base_date, _dt.time(0, 0), tzinfo=pytz.UTC)
    
    # 构造该国家今天的 7:00 和明天的 0:00（即24:00）
    segments = []
    # 检查前一天、当天、后一天的本地工作时段，看哪些与今天UTC重叠
    for day_offset in [-1, 0, 1]:
        local_date = base_date + _dt.timedelta(days=day_offset)
        local_start = tz.localize(_dt.datetime.combine(local_date, _dt.time(7, 0)))
        local_end = tz.localize(_dt.datetime.combine(local_date, _dt.time(0, 0))) + _dt.timedelta(days=1)
        
        utc_start = local_start.astimezone(pytz.UTC)
        utc_end = local_end.astimezone(pytz.UTC)
        
        # 求与今天UTC的交集
        utc_today_end = utc_today_start + _dt.timedelta(days=1)
        overlap_start = max(utc_start, utc_today_start)
        overlap_end = min(utc_end, utc_today_end)
        
        if overlap_start < overlap_end:
            start_sec = (overlap_start - utc_today_start).total_seconds()
            end_sec = (overlap_end - utc_today_start).total_seconds()
            segments.append((start_sec, end_sec))
    
    return segments


def get_global_coverage(proxy_pool):
    """
    计算所有启用代理国家的全局覆盖时段（UTC秒数，今天 00:00 起）
    
    Returns:
        dict: {
            'covered_segments': [(start, end), ...],  # 合并后的覆盖区间
            'uncovered_segments': [(start, end), ...], # 空白时段
            'coverage_pct': float,  # 覆盖百分比
            'country_segments': {country: [(start, end)]},  # 各国时段
        }
    """
    enabled_countries = [p.get("country_code", "US") for p in proxy_pool if p.get("enabled", False)]
    
    if not enabled_countries:
        return {
            'covered_segments': [],
            'uncovered_segments': [(0, 86400)],
            'coverage_pct': 0.0,
            'country_segments': {}
        }
    
    # 1. 每个国家的工作时段
    country_segments = {}
    all_segments = []
    for cc in set(enabled_countries):
        segs = get_country_working_seconds_today(cc)
        country_segments[cc] = segs
        all_segments.extend(segs)
    
    # 2. 合并所有时段
    all_segments.sort()
    merged = []
    for start, end in all_segments:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    
    # 3. 计算空白时段
    uncovered = []
    prev_end = 0
    for start, end in merged:
        if start > prev_end:
            uncovered.append((prev_end, start))
        prev_end = end
    if prev_end < 86400:
        uncovered.append((prev_end, 86400))
    
    # 4. 覆盖百分比
    covered_total = sum(e - s for s, e in merged)
    coverage_pct = (covered_total / 86400) * 100
    
    return {
        'covered_segments': merged,
        'uncovered_segments': uncovered,
        'coverage_pct': coverage_pct,
        'country_segments': country_segments
    }


def get_countries_at_utc_sec(utc_sec, country_segments):
    """
    返回在指定 UTC 秒数时刻处于工作时段的国家列表
    """
    in_work = []
    for cc, segs in country_segments.items():
        for s, e in segs:
            if s <= utc_sec < e:
                in_work.append(cc)
                break
    return in_work


def soft_boundary_probability(utc_sec, country_code):
    """
    软边界概率衰减
    返回 0.0 ~ 1.0，表示该时刻该国家有"真实流量"的概率
    
    - 当地 9-22 点（核心时段）：100%
    - 当地 7-9, 22-24 点（边缘时段）：50%
    - 当地 0-7 点（凌晨时段）：10%
    """
    import datetime as _dt
    tz_str = get_timezone_for_country(country_code)
    try:
        tz = pytz.timezone(tz_str)
    except Exception:
        return 1.0
    
    # 把今日 UTC 秒数转成本地小时
    base_date = _dt.datetime.now(pytz.UTC).date()
    utc_dt = _dt.datetime.combine(base_date, _dt.time(0, 0), tzinfo=pytz.UTC) + _dt.timedelta(seconds=utc_sec)
    local_dt = utc_dt.astimezone(tz)
    h = local_dt.hour + local_dt.minute / 60.0
    
    if 9 <= h < 22:
        return 1.0
    elif 7 <= h < 9 or 22 <= h < 24:
        return 0.5
    else:
        return 0.1


def select_country_by_quota(candidates, country_quota_used, country_quota_target):
    """
    从候选国家列表中智能选一个：
    - 优先选剩余配额最多的国家
    - 配额已满的国家排除
    - 多个候选时按剩余配额加权随机
    """
    # 过滤：保留还没用满配额的国家
    available = []
    weights = []
    for cc in candidates:
        used = country_quota_used.get(cc, 0)
        target = country_quota_target.get(cc, 0)
        remaining = max(0, target - used)
        if remaining > 0:
            available.append(cc)
            weights.append(remaining)
    
    if not available:
        # 全都满了，随机选一个
        return random.choice(candidates) if candidates else None
    
    # 按剩余配额加权随机
    total = sum(weights)
    r = random.uniform(0, total)
    acc = 0
    for cc, w in zip(available, weights):
        acc += w
        if r <= acc:
            return cc
    return available[-1]




def validate_web_navigation_config(cfg, fail_hard=False):
    """校验 web_navigation 配置（网页浏览模式）。
    - 检查 loop_count / loop_interval 必须为 {min, max} 且 min <= max
    - 检查 layer_1..layer_5 的 stay_ratio 不能全为 0
    - 返回 (success: bool, errors: list[str])
    """
    errors = []
    wn = cfg.get("web_navigation")
    if not isinstance(wn, dict):
        errors.append("缺少 web_navigation 配置")
        return (False, errors)

    # loop_count
    lc = wn.get("loop_count")
    if not isinstance(lc, dict) or "min" not in lc or "max" not in lc:
        errors.append("loop_count 必须包含 min/max")
    else:
        try:
            if int(lc["min"]) < 1:
                errors.append("loop_count.min 必须 >= 1")
            if int(lc["max"]) < int(lc["min"]):
                errors.append("loop_count.max 必须 >= min")
        except Exception:
            errors.append("loop_count 必须为整数")

    # loop_interval
    li = wn.get("loop_interval")
    if not isinstance(li, dict) or "min" not in li or "max" not in li:
        errors.append("loop_interval 必须包含 min/max")
    else:
        try:
            if float(li["min"]) < 0:
                errors.append("loop_interval.min 必须 >= 0")
            if float(li["max"]) < float(li["min"]):
                errors.append("loop_interval.max 必须 >= min")
        except Exception:
            errors.append("loop_interval 必须为数字")

    # layer stay_ratio 检查（只检查 layer_1 到 layer_5）
    ratio_sum = 0.0
    for li2 in range(1, 6):
        layer = wn.get(f"layer_{li2}", {}) if isinstance(wn.get(f"layer_{li2}", {}), dict) else {}
        r = layer.get("stay_ratio", 0)
        try:
            ratio_sum += float(r)
        except Exception:
            errors.append(f"layer_{li2}.stay_ratio 必须为数字")
    if ratio_sum <= 0 and not errors:
        errors.append("layer_1..layer_5 的 stay_ratio 之和必须 > 0")

    if fail_hard and errors:
        raise ValueError("网页浏览模式配置错误: " + "; ".join(errors))
    return (len(errors) == 0, errors)


def generate_daily_tasks_legacy(cfg):
    """生成今日完整任务清单 - 24小时全球分布版
    
    核心逻辑：
    1. 计算全球覆盖时段（自动检测启用代理的工作时间）
    2. 国家配额平均 + ±20% 随机抖动
    3. 全局流量模型生成时间点 (UTC秒数)
    4. 软边界概率衰减（核心100%/边缘50%/凌晨10%）
    5. 智能选代理（优先剩余配额多的国家）
    6. 任务时间冲突时顺延，过期/超24:00 作废
    7. 检测任务密度并给出警告
    """
    import datetime as _dt
    
    auto_mode = False
    site_age = get_site_age_category(cfg.get("site_creation_date", ""))
    traffic_range = cfg.get("daily_traffic_range", {
        "new": {"min": 50, "max": 100},
        "mid": {"min": 200, "max": 300},
        "old": {"min": 500, "max": 600}
    })

    if auto_mode:
        range_cfg = traffic_range.get(site_age, traffic_range["old"])
        total_tasks_planned = random.randint(range_cfg["min"], range_cfg["max"])
    else:
        total_tasks_planned = cfg.get("plan_days", 1)

    # 准备代理池
    proxy_pool_enabled = [p for p in cfg.get("proxy_pool", []) if p.get("enabled", False) and p.get("proxy_api_url")]
    if not proxy_pool_enabled:
        proxy_pool_enabled = [{
            "country_code": "US",
            "proxy_api_url": cfg.get("ip_proxy_api", ""),
            "proxy_user": cfg.get("ip_proxy_user", ""),
            "proxy_pwd": cfg.get("ip_proxy_pwd", "")
        }]

    # 配置参数（网页浏览模式：任务时长=浏览网页时长，不再叠加视频观看时长）
    total_stay_cfg = cfg.get("total_stay", {"min": 120, "max": 300})
    interval_cfg = cfg.get("task_interval", {"min": 20, "max": 40})
    # ↓ 安全兜底：min_watch_time/max_watch_time 在网页浏览模式下恒为 0（无视频观看）
    min_watch_time = 0.0
    max_watch_time = 0.0
    
    # 1. 计算从现在往后24小时的全球覆盖时段
    now_utc = _dt.datetime.now(pytz.UTC)
    today_utc_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_now_utc = (now_utc - today_utc_start).total_seconds()
    end_of_window = seconds_now_utc + 86400  # 从现在往后 24h
    
    # 收集计划期间各代理国家的工作时段（直接计算绝对时间）
    all_covered_segments = []
    enabled_countries = list({p.get("country_code", "US") for p in proxy_pool_enabled})
    country_segments = {}
    
    for cc in enabled_countries:
        tz_str = get_timezone_for_country(cc)
        try:
            tz = pytz.timezone(tz_str)
        except Exception:
            tz = pytz.timezone("America/New_York")
        
        if cc not in country_segments:
            country_segments[cc] = []
        
        # 检查从昨天到后天的本地工作时段，覆盖我们的24小时窗口
        for day_offset in [-1, 0, 1, 2]:
            local_date = today_utc_start.date() + _dt.timedelta(days=day_offset)
            # 本地时间 7:00 到次日 0:00
            local_start = tz.localize(_dt.datetime.combine(local_date, _dt.time(7, 0)))
            local_end = tz.localize(_dt.datetime.combine(local_date, _dt.time(0, 0))) + _dt.timedelta(days=1)
            
            # 转换为 UTC 时间
            utc_start = local_start.astimezone(pytz.UTC)
            utc_end = local_end.astimezone(pytz.UTC)
            
            # 转换为相对于 today_utc_start 的秒数
            start_sec = (utc_start - today_utc_start).total_seconds()
            end_sec = (utc_end - today_utc_start).total_seconds()
            
            # 只添加有效的时段
            if start_sec < end_sec:
                all_covered_segments.append((start_sec, end_sec))
                country_segments[cc].append((start_sec, end_sec))
    
    # 合并重叠的覆盖时段
    all_covered_segments.sort()
    merged_covered = []
    for s, e in all_covered_segments:
        if merged_covered and s <= merged_covered[-1][1]:
            merged_covered[-1] = (merged_covered[-1][0], max(merged_covered[-1][1], e))
        else:
            merged_covered.append((s, e))
    
    # 只保留从现在往后24小时窗口内的覆盖时段
    future_covered = []
    for s, e in merged_covered:
        if e <= seconds_now_utc:
            continue
        if s >= end_of_window:
            continue
        new_s = max(s, seconds_now_utc)
        new_e = min(e, end_of_window)
        future_covered.append((new_s, new_e))
    
    # 计算覆盖百分比
    total_coverage_seconds = sum(e - s for s, e in future_covered)
    coverage_pct = (total_coverage_seconds / 86400) * 100
    coverage = {
        "coverage_pct": coverage_pct,
        "covered_segments": future_covered,
        "uncovered_segments": [],
        "country_segments": country_segments
    }
    
    # 检查是否有启用的国家
    if not enabled_countries:
        return {
            "total_tasks": 0,
            "planned_tasks": total_tasks_planned,
            "discarded_tasks": total_tasks_planned,
            "discard_reasons": {},
            "compensated_count": 0,
            "model_used": "none",
            "site_age": site_age,
            "tasks": [],
            "coverage": coverage,
            "country_distribution": {},
            "country_quota_target": {},
            "warnings": ["⚠️ 没有启用任何代理"]
        }
    
    # 2. 国家配额分配（基础平均 + ±20% 抖动）
    base_quota = total_tasks_planned / len(enabled_countries)
    country_quota_target = {}
    for cc in enabled_countries:
        quota = base_quota * random.uniform(0.8, 1.2)
        country_quota_target[cc] = max(1, int(round(quota)))
    
    # 3. 生成全局任务时间点（从现在开始偏移）
    chosen_model = "simple"
    raw_time_points = []  # UTC秒数列表
    
    if auto_mode:
        selected_models = cfg.get("selected_models", ["normal", "gamma", "bimodal", "poisson"])
        selected_models = [m for m in selected_models if m in MODEL_FUNCTIONS]
        if not selected_models:
            selected_models = ["normal"]
        chosen_model = random.choice(selected_models)
        model_func = MODEL_FUNCTIONS[chosen_model]
        hour_list = model_func(total_tasks_planned)
        
        for h in hour_list:
            tp = seconds_now_utc + h * 3600
            if tp >= seconds_now_utc and tp < end_of_window:
                raw_time_points.append(tp)
    else:
        # 非自动模式：从当前时间均匀排列
        chosen_model = "simple"
        est_task_len = (total_stay_cfg["min"] + total_stay_cfg["max"]) / 2 + (min_watch_time + max_watch_time) / 2
        avg_gap = (interval_cfg["min"] + interval_cfg["max"]) / 2
        cursor = seconds_now_utc
        for i in range(total_tasks_planned):
            raw_time_points.append(cursor)
            cursor += est_task_len + avg_gap
    
    raw_time_points.sort()
    
    # 4. 应用软边界概率筛选 + 落到覆盖时段内（带原因统计）
    valid_time_points = []
    discard_reasons = {
        "past_time": 0,
        "out_of_window": 0,   # 超过24小时窗口
        "out_of_coverage": 0,
        "soft_boundary": 0,
    }
    
    for tp in raw_time_points:
        if tp < seconds_now_utc:
            discard_reasons["past_time"] += 1
            continue
        if tp >= end_of_window:
            discard_reasons["out_of_window"] += 1
            continue
        in_coverage = any(s <= tp < e for s, e in future_covered)
        if not in_coverage:
            discard_reasons["out_of_coverage"] += 1
            continue
        countries_at = get_countries_at_utc_sec(tp, country_segments)
        if not countries_at:
            discard_reasons["out_of_coverage"] += 1
            continue
        max_prob = max(soft_boundary_probability(tp, cc) for cc in countries_at)
        if random.random() <= max_prob:
            valid_time_points.append(tp)
        else:
            discard_reasons["soft_boundary"] += 1
    
    valid_time_points.sort()
    
    # 4.5 智能补偿：如果有效时间点 < 计划 × 80%，按覆盖时段均匀补充
    target_min = int(total_tasks_planned * 0.8)
    compensated_count = 0
    if len(valid_time_points) < target_min and future_covered:
        deficit = target_min - len(valid_time_points)
        # 在剩余覆盖时段内均匀生成补偿点
        total_future = sum(e - s for s, e in future_covered)
        if total_future > 0:
            avg_gap_for_comp = total_future / max(deficit + 1, 2)
            for i in range(deficit):
                # 在覆盖段中按比例选时间点
                cursor = (i + 0.5) * avg_gap_for_comp
                acc = 0
                for s, e in future_covered:
                    seg_len = e - s
                    if cursor <= acc + seg_len:
                        tp = s + (cursor - acc)
                        # 软边界过滤：补偿点也要尊重核心时段
                        countries_at = get_countries_at_utc_sec(tp, country_segments)
                        if countries_at:
                            max_prob = max(soft_boundary_probability(tp, cc) for cc in countries_at)
                            # 补偿点降低软边界要求（×1.5），尽量多保留
                            if random.random() <= min(1.0, max_prob * 1.5):
                                valid_time_points.append(tp)
                                compensated_count += 1
                        break
                    acc += seg_len
            valid_time_points.sort()
    
    # 5. 逐个生成任务（智能代理选择 + 顺延冲突 + 配额均衡）
    tasks = []
    country_quota_used = {cc: 0 for cc in enabled_countries}
    proxy_by_country = {}
    for p in proxy_pool_enabled:
        cc = p.get("country_code", "US")
        proxy_by_country.setdefault(cc, []).append(p)

    # 获取本地时区
    local_tz = pytz.timezone('Asia/Shanghai')
    local_now = _dt.datetime.now(local_tz)
    today_local_start = local_tz.localize(_dt.datetime(local_now.year, local_now.month, local_now.day, 0, 0, 0))
    today_local_start_utc = today_local_start.astimezone(pytz.UTC)
    seconds_now_local = (local_now - today_local_start).total_seconds()
    
    prev_end_time = seconds_now_local
    is_first = True

    # 找到最接近当前时间的任务时间点作为第一条任务
    if valid_time_points:
        local_time_points = []
        for tp in valid_time_points:
            utc_datetime = today_utc_start + _dt.timedelta(seconds=tp)
            local_datetime = utc_datetime.astimezone(local_tz)
            local_seconds = (local_datetime - today_local_start).total_seconds()
            local_time_points.append(local_seconds)
        
        closest_idx = min(range(len(local_time_points)), key=lambda i: abs(local_time_points[i] - seconds_now_local))
        closest_time = local_time_points[closest_idx]
        
        time_diff = abs(closest_time - seconds_now_local)
        if time_diff > 300:
            new_time = seconds_now_local
            local_covered_segments = []
            for s, e in future_covered:
                utc_s = today_utc_start + _dt.timedelta(seconds=s)
                utc_e = today_utc_start + _dt.timedelta(seconds=e)
                local_s = utc_s.astimezone(local_tz)
                local_e = utc_e.astimezone(local_tz)
                ls = (local_s - today_local_start).total_seconds()
                le = (local_e - today_local_start).total_seconds()
                local_covered_segments.append((ls, le))
                
            in_coverage = False
            for s, e in local_covered_segments:
                if s <= new_time < e:
                    in_coverage = True
                    break
            
            if in_coverage:
                valid_time_points.insert(0, (today_local_start_utc + _dt.timedelta(seconds=new_time) - today_utc_start).total_seconds())
                local_time_points.insert(0, new_time)
            else:
                valid_time_points.insert(0, valid_time_points.pop(closest_idx))
        else:
            valid_time_points.insert(0, valid_time_points.pop(closest_idx))

    for tp in valid_time_points:
        utc_datetime = today_utc_start + _dt.timedelta(seconds=tp)
        local_datetime = utc_datetime.astimezone(local_tz)
        local_tp = (local_datetime - today_local_start).total_seconds()
        
        # 任务间隔（增强随机性：非线性抖动 + 偶尔分心暂停）
        if is_first:
            task_gap = 0
        else:
            base_gap = random.uniform(interval_cfg["min"], interval_cfg["max"])
            # 10% 概率出现"分心暂停"（模拟用户去倒水、看手机等）
            if random.random() < 0.10:
                base_gap += random.uniform(30, 120)
            # 5% 概率出现"短暂快速操作"（模拟用户快速连续浏览）
            elif random.random() < 0.05:
                base_gap = max(3, base_gap * 0.3)
            # 添加 ±15% 高斯微抖动
            task_gap = max(2, base_gap * (1 + random.gauss(0, 0.15)))
        is_first = False
        
        # 顺延冲突处理：确保任务不会重叠（使用本地时间）
        actual_start = max(local_tp, prev_end_time + task_gap, seconds_now_local)
        
        # 超过24小时窗口作废（使用本地时间）
        end_of_window_local = seconds_now_local + 86400
        if actual_start >= end_of_window_local:
            break
        
        # 选代理：找到此时在工作的国家中，配额未满的（需要使用UTC时间戳）
        tp_utc_for_proxy = (today_local_start_utc + _dt.timedelta(seconds=actual_start) - today_utc_start).total_seconds()
        countries_at = get_countries_at_utc_sec(tp_utc_for_proxy, country_segments)
        if not countries_at:
            # 顺延后已超出覆盖区，作废本任务
            continue
        
        chosen_country = select_country_by_quota(countries_at, country_quota_used, country_quota_target)
        if chosen_country is None:
            chosen_country = random.choice(countries_at)
        
        proxy = random.choice(proxy_by_country.get(chosen_country, proxy_pool_enabled))
        
        # 随机执行时长（网页浏览模式：任务时长=浏览网页时长，与配置一致）
        browse_duration = random.uniform(total_stay_cfg["min"], total_stay_cfg["max"])
        task_duration = browse_duration
        actual_end = actual_start + task_duration
        
        # 计划时间字符串（本地时区）- 使用调整后的实际开始时间
        local_datetime_for_plan = today_local_start + _dt.timedelta(seconds=actual_start)
        plan_time_str = local_datetime_for_plan.strftime('%Y-%m-%d %H:%M:%S')
        
        # 将本地时间秒数转换为UTC时间秒数，供前端显示使用
        actual_start_utc = (today_local_start_utc + _dt.timedelta(seconds=actual_start) - today_utc_start).total_seconds()
        actual_end_utc = (today_local_start_utc + _dt.timedelta(seconds=actual_end) - today_utc_start).total_seconds()
        
        tasks.append({
            "idx": len(tasks) + 1,
            "plan_time": plan_time_str,
            "ideal_start": tp,
            "actual_start": int(actual_start_utc),
            "actual_end": int(actual_end_utc),
            "task_gap": task_gap,
            "browse_duration": browse_duration,
            "task_duration": int(task_duration),
            "proxy_api_url": proxy.get("proxy_api_url"),
            "proxy_user": proxy.get("proxy_user"),
            "proxy_pwd": proxy.get("proxy_pwd"),
            "proxy_country": chosen_country,
            "status": "未完成"
        })
        
        country_quota_used[chosen_country] = country_quota_used.get(chosen_country, 0) + 1
        prev_end_time = actual_end
    
    # 6. 警告
    warnings = []
    if coverage_pct < 100:
        warnings.append(f"⚠️ 全球覆盖率 {coverage_pct:.1f}%")
    
    # 密度检测
    window = 600
    for i, t in enumerate(tasks):
        count_in_window = sum(1 for tt in tasks[i:i+10] if tt['actual_start'] - t['actual_start'] < window)
        if count_in_window > 5:
            h = int(t['actual_start'] // 3600)
            m = int((t['actual_start'] % 3600) // 60)
            warnings.append(f"⚠️ {h:02d}:{m:02d} 附近有密集任务")
            break
    
    # 7. 实际国家分布统计
    country_distribution = {}
    for t in tasks:
        cc = t['proxy_country']
        country_distribution[cc] = country_distribution.get(cc, 0) + 1
    
    discarded = total_tasks_planned - len(tasks)
    
    return {
        "total_tasks": len(tasks),
        "planned_tasks": total_tasks_planned,
        "discarded_tasks": discarded,
        "discard_reasons": discard_reasons,
        "compensated_count": compensated_count,
        "model_used": chosen_model,
        "site_age": site_age,
        "tasks": tasks,
        "coverage": {
            "coverage_pct": coverage['coverage_pct'],
            "covered_segments": coverage['covered_segments'],
            "uncovered_segments": coverage['uncovered_segments']
        },
        "country_distribution": country_distribution,
        "country_quota_target": country_quota_target,
        "warnings": warnings
    }

def generate_daily_tasks(cfg):
    """生成多天任务计划：按计划天数、网站属性日流量、每日随机模型自动生成。"""
    import datetime as _dt

    site_age = get_site_age_category(cfg.get("site_creation_date", ""))
    traffic_range = cfg.get("daily_traffic_range", {
        "new": {"min": 50, "max": 100},
        "mid": {"min": 200, "max": 300},
        "old": {"min": 500, "max": 600}
    })
    try:
        plan_days = max(1, min(7, int(cfg.get("plan_days", 1))))
    except Exception:
        plan_days = 1

    proxy_pool_enabled = [p for p in cfg.get("proxy_pool", []) if p.get("enabled", False) and p.get("proxy_api_url")]
    if not proxy_pool_enabled:
        proxy_pool_enabled = [{
            "country_code": "US",
            "proxy_api_url": cfg.get("ip_proxy_api", ""),
            "proxy_user": cfg.get("ip_proxy_user", ""),
            "proxy_pwd": cfg.get("ip_proxy_pwd", "")
        }]

    enabled_countries = list({p.get("country_code", "US") for p in proxy_pool_enabled})
    total_stay_cfg = cfg.get("total_stay", {"min": 120, "max": 300})
    interval_cfg = cfg.get("task_interval", {"min": 20, "max": 40})
    range_cfg = traffic_range.get(site_age, traffic_range.get("old", {"min": 500, "max": 600}))
    day_min = int(range_cfg.get("min", 50))
    day_max = int(range_cfg.get("max", max(day_min, 50)))
    if day_max < day_min:
        day_min, day_max = day_max, day_min

    now_utc = _dt.datetime.now(pytz.UTC)
    today_utc_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_now_utc = (now_utc - today_utc_start).total_seconds()
    local_tz = pytz.timezone('Asia/Shanghai')
    local_now = now_utc.astimezone(local_tz)
    today_local_start = local_tz.localize(_dt.datetime(local_now.year, local_now.month, local_now.day, 0, 0, 0))
    today_local_start_utc = today_local_start.astimezone(pytz.UTC)
    seconds_now_local = (local_now - today_local_start).total_seconds()
    plan_window_end = seconds_now_utc + plan_days * 86400

    country_segments = {}
    all_covered_segments = []
    for cc in enabled_countries:
        tz_str = get_timezone_for_country(cc)
        try:
            tz = pytz.timezone(tz_str)
        except Exception:
            tz = pytz.timezone("America/New_York")
        country_segments.setdefault(cc, [])
        for day_offset in range(-1, plan_days + 3):
            local_date = today_utc_start.date() + _dt.timedelta(days=day_offset)
            local_start = tz.localize(_dt.datetime.combine(local_date, _dt.time(7, 0)))
            local_end = tz.localize(_dt.datetime.combine(local_date, _dt.time(0, 0))) + _dt.timedelta(days=1)
            start_sec = (local_start.astimezone(pytz.UTC) - today_utc_start).total_seconds()
            end_sec = (local_end.astimezone(pytz.UTC) - today_utc_start).total_seconds()
            if start_sec < end_sec:
                country_segments[cc].append((start_sec, end_sec))
                all_covered_segments.append((start_sec, end_sec))

    all_covered_segments.sort()
    merged_covered = []
    for s, e in all_covered_segments:
        if merged_covered and s <= merged_covered[-1][1]:
            merged_covered[-1] = (merged_covered[-1][0], max(merged_covered[-1][1], e))
        else:
            merged_covered.append((s, e))
    future_covered = []
    for s, e in merged_covered:
        if e <= seconds_now_utc or s >= plan_window_end:
            continue
        future_covered.append((max(s, seconds_now_utc), min(e, plan_window_end)))
    coverage_pct = (sum(e - s for s, e in future_covered) / max(1, plan_days * 86400 - seconds_now_utc)) * 100

    if not enabled_countries:
        return {
            "total_tasks": 0, "planned_tasks": 0, "discarded_tasks": 0,
            "discard_reasons": {}, "compensated_count": 0, "model_used": "none",
            "site_age": site_age, "plan_days": plan_days, "tasks": [],
            "daily_summaries": [],
            "coverage": {"coverage_pct": 0, "covered_segments": [], "uncovered_segments": []},
            "country_distribution": {}, "country_quota_target": {},
            "warnings": ["⚠️ 没有启用任何代理"]
        }

    proxy_by_country = {}
    for p in proxy_pool_enabled:
        cc = p.get("country_code", "US")
        proxy_by_country.setdefault(cc, []).append(p)

    tasks = []
    daily_summaries = []
    warnings = []
    discard_reasons = {"past_time": 0, "out_of_window": 0, "out_of_coverage": 0, "soft_boundary": 0}
    country_distribution = {}
    country_quota_target = {cc: 0 for cc in enabled_countries}
    country_quota_used = {cc: 0 for cc in enabled_countries}
    compensated_count = 0
    total_tasks_planned = 0
    prev_end_time = seconds_now_local
    is_first_task = True

    for day_idx in range(plan_days):
        day_local_start = today_local_start + _dt.timedelta(days=day_idx)
        day_local_end = day_local_start + _dt.timedelta(days=1)
        day_start_sec = (day_local_start.astimezone(pytz.UTC) - today_utc_start).total_seconds()
        day_end_sec = (day_local_end.astimezone(pytz.UTC) - today_utc_start).total_seconds()
        available_start = max(day_start_sec, seconds_now_utc)
        if available_start >= day_end_sec:
            daily_summaries.append({
                "date": day_local_start.strftime('%Y-%m-%d'), "site_age": site_age,
                "model_used": "none", "planned_tasks": 0, "generated_tasks": 0,
                "discarded_tasks": 0
            })
            continue

        full_day_tasks = random.randint(day_min, day_max)
        # 周末/节假日流量调整（模拟真实网站流量波动）
        _day_date = day_local_start.date() if hasattr(day_local_start, 'date') else day_local_start
        _wk_multiplier = get_weekend_holiday_multiplier(_day_date)
        full_day_tasks = max(1, int(round(full_day_tasks * _wk_multiplier)))
        if day_idx == 0:
            remain_ratio = max(0.0, (day_end_sec - available_start) / 86400.0)
            planned_for_day = int(round(full_day_tasks * remain_ratio))
            if full_day_tasks > 0 and remain_ratio > 0 and planned_for_day < 1:
                planned_for_day = 1
        else:
            planned_for_day = full_day_tasks
        total_tasks_planned += planned_for_day
        if planned_for_day <= 0:
            daily_summaries.append({
                "date": day_local_start.strftime('%Y-%m-%d'), "site_age": site_age,
                "model_used": "none", "planned_tasks": 0, "generated_tasks": 0,
                "discarded_tasks": 0
            })
            continue

        chosen_model = random.choice(list(MODEL_FUNCTIONS.keys()))
        hour_list = MODEL_FUNCTIONS[chosen_model](planned_for_day)
        raw_time_points = []
        for h in hour_list:
            tp = day_start_sec + h * 3600
            if available_start <= tp < day_end_sec:
                raw_time_points.append(tp)
        raw_time_points.sort()

        valid_time_points = []
        for tp in raw_time_points:
            if tp < seconds_now_utc:
                discard_reasons["past_time"] += 1
                continue
            if tp >= plan_window_end:
                discard_reasons["out_of_window"] += 1
                continue
            countries_at = get_countries_at_utc_sec(tp, country_segments)
            if not countries_at:
                discard_reasons["out_of_coverage"] += 1
                continue
            max_prob = max(soft_boundary_probability(tp, cc) for cc in countries_at)
            if random.random() <= max_prob:
                valid_time_points.append(tp)
            else:
                discard_reasons["soft_boundary"] += 1

        target_min = int(planned_for_day * 0.8)
        if len(valid_time_points) < target_min:
            deficit = target_min - len(valid_time_points)
            day_segments = [(max(s, available_start), min(e, day_end_sec)) for s, e in future_covered if e > available_start and s < day_end_sec]
            total_future = sum(e - s for s, e in day_segments if e > s)
            if total_future > 0:
                avg_gap_for_comp = total_future / max(deficit + 1, 2)
                for i in range(deficit):
                    cursor = (i + 0.5) * avg_gap_for_comp
                    acc = 0
                    for s, e in day_segments:
                        seg_len = e - s
                        if cursor <= acc + seg_len:
                            tp = s + (cursor - acc)
                            countries_at = get_countries_at_utc_sec(tp, country_segments)
                            if countries_at:
                                valid_time_points.append(tp)
                                compensated_count += 1
                            break
                        acc += seg_len
        valid_time_points = sorted(set(valid_time_points))

        generated_before = len(tasks)
        day_quota_base = max(1, planned_for_day / len(enabled_countries))
        for cc in enabled_countries:
            country_quota_target[cc] += max(1, int(round(day_quota_base * random.uniform(0.8, 1.2))))

        for tp in valid_time_points:
            local_datetime = (today_utc_start + _dt.timedelta(seconds=tp)).astimezone(local_tz)
            local_tp = (local_datetime - today_local_start).total_seconds()
            task_gap = 0 if is_first_task else random.uniform(interval_cfg["min"], interval_cfg["max"])
            is_first_task = False
            actual_start = max(local_tp, prev_end_time + task_gap, seconds_now_local)
            actual_start_utc = (today_local_start_utc + _dt.timedelta(seconds=actual_start) - today_utc_start).total_seconds()
            if actual_start_utc >= plan_window_end:
                break
            countries_at = get_countries_at_utc_sec(actual_start_utc, country_segments)
            if not countries_at:
                continue
            chosen_country = select_country_by_quota(countries_at, country_quota_used, country_quota_target) or random.choice(countries_at)
            proxy = random.choice(proxy_by_country.get(chosen_country, proxy_pool_enabled))
            browse_duration = random.uniform(total_stay_cfg["min"], total_stay_cfg["max"])
            actual_end = actual_start + browse_duration
            actual_end_utc = (today_local_start_utc + _dt.timedelta(seconds=actual_end) - today_utc_start).total_seconds()
            actual_start_epoch = int((today_utc_start + _dt.timedelta(seconds=actual_start_utc)).timestamp())
            actual_end_epoch = int((today_utc_start + _dt.timedelta(seconds=actual_end_utc)).timestamp())
            plan_time_str = (today_local_start + _dt.timedelta(seconds=actual_start)).strftime('%Y-%m-%d %H:%M:%S')
            tasks.append({
                "idx": len(tasks) + 1,
                "date": (today_local_start + _dt.timedelta(seconds=actual_start)).strftime('%Y-%m-%d'),
                "plan_time": plan_time_str,
                "ideal_start": tp,
                "actual_start": int(actual_start_utc),
                "actual_end": int(actual_end_utc),
                "actual_start_epoch": actual_start_epoch,
                "actual_end_epoch": actual_end_epoch,
                "task_gap": task_gap,
                "browse_duration": browse_duration,
                "task_duration": int(browse_duration),
                "proxy_api_url": proxy.get("proxy_api_url"),
                "proxy_user": proxy.get("proxy_user"),
                "proxy_pwd": proxy.get("proxy_pwd"),
                "proxy_country": chosen_country,
                "status": "未完成"
            })
            country_quota_used[chosen_country] = country_quota_used.get(chosen_country, 0) + 1
            country_distribution[chosen_country] = country_distribution.get(chosen_country, 0) + 1
            prev_end_time = actual_end

        generated_for_day = len(tasks) - generated_before
        daily_summaries.append({
            "date": day_local_start.strftime('%Y-%m-%d'),
            "site_age": site_age,
            "model_used": chosen_model,
            "planned_tasks": planned_for_day,
            "generated_tasks": generated_for_day,
            "discarded_tasks": max(0, planned_for_day - generated_for_day)
        })

    if coverage_pct < 100:
        warnings.append(f"⚠️ 计划窗口全球覆盖率 {coverage_pct:.1f}%")
    discarded = max(0, total_tasks_planned - len(tasks))
    return {
        "total_tasks": len(tasks),
        "planned_tasks": total_tasks_planned,
        "discarded_tasks": discarded,
        "discard_reasons": discard_reasons,
        "compensated_count": compensated_count,
        "model_used": "multi_day",
        "site_age": site_age,
        "plan_days": plan_days,
        "daily_summaries": daily_summaries,
        "tasks": tasks,
        "coverage": {"coverage_pct": coverage_pct, "covered_segments": future_covered, "uncovered_segments": []},
        "country_distribution": country_distribution,
        "country_quota_target": country_quota_target,
        "warnings": warnings
    }


# ========== 原有代码继续 ==========

# 贝塞尔曲线鼠标移动函数
def get_random_value(range_config):
    """从 {min, max} 配置中随机取值"""
    return random.uniform(range_config["min"], range_config["max"])

def get_random_int(range_config):
    """从 {min, max} 配置中随机取整数值"""
    return random.randint(range_config["min"], range_config["max"])

def bezier_curve(p0, p1, p2, t):
    """计算二次贝塞尔曲线上的点"""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return x, y

def human_mouse_move(page, start_x, start_y, end_x, end_y, config):
    """使用贝塞尔曲线模拟真人鼠标移动"""
    # 生成随机控制点，制造自然的弯曲
    control_x = random.uniform(min(start_x, end_x), max(start_x, end_x))
    control_y = random.uniform(min(start_y, end_y) - 100, max(start_y, end_y) + 100)
    
    # 从配置中随机取步数
    steps = get_random_int(config["mouse_move_steps"])
    
    for i in range(steps + 1):
        t = i / steps
        # 使用 ease-out 缓动函数，模拟真人鼠标先快后慢的特点
        eased_t = 1 - math.pow(1 - t, 3)
        
        x, y = bezier_curve((start_x, start_y), (control_x, control_y), (end_x, end_y), eased_t)
        page.mouse.move(x, y)
        
        # 随机小停顿，模拟真人移动时的微小停顿（使用配置参数）
        pause_prob = get_random_value(config["bezier_pause_prob"])
        if random.random() < pause_prob:
            pause_time = get_random_value(config["mouse_move_pause"])
            time.sleep(pause_time)

QA_SESSION_DIR = "qa_sessions"
QA_AD_COOKIE_CLEANUP_DAYS = 7
QA_SESSION_MAX_DAYS = 7
QA_AD_COOKIE_DOMAINS = (
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "adservice.google.",
)
QA_AD_COOKIE_PREFIXES = ("gcl_", "_gcl", "IDE", "DSID", "FLC", "AID", "TAID")


def _qa_safe_name(value):
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value or "unknown"))[:80]


def _qa_site_host(url):
    host = urllib.parse.urlparse(url or "").hostname or "unknown"
    return host.lower()


def _qa_session_paths(site_url, country):
    import os
    site = _qa_safe_name(_qa_site_host(site_url))
    cc = _qa_safe_name((country or "US").upper())
    base_dir = os.path.join(os.getcwd(), QA_SESSION_DIR, site, cc)
    return base_dir, os.path.join(base_dir, "storage_state.json"), os.path.join(base_dir, "meta.json")


def _qa_cookie_domain_matches(cookie_domain, site_host):
    domain = (cookie_domain or "").lstrip(".").lower()
    site_host = (site_host or "").lower()
    return domain == site_host or site_host.endswith("." + domain) or domain.endswith("." + site_host)


def _qa_is_ad_cookie(cookie):
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    name = cookie.get("name") or ""
    if any(ad_domain in domain for ad_domain in QA_AD_COOKIE_DOMAINS):
        return True
    return any(name.startswith(prefix) for prefix in QA_AD_COOKIE_PREFIXES)


def _qa_filter_storage_state(state, site_url, clean_ad_cookies=True):
    site_host = _qa_site_host(site_url)
    filtered_cookies = []
    for cookie in state.get("cookies", []):
        if clean_ad_cookies and _qa_is_ad_cookie(cookie):
            continue
        if _qa_cookie_domain_matches(cookie.get("domain"), site_host):
            filtered_cookies.append(cookie)
    filtered_origins = []
    allowed_origin = f"https://{site_host}"
    allowed_http_origin = f"http://{site_host}"
    for origin in state.get("origins", []):
        if origin.get("origin") in (allowed_origin, allowed_http_origin):
            filtered_origins.append(origin)
    return {"cookies": filtered_cookies, "origins": filtered_origins}


def _qa_load_json(path, default=None):
    import os
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def prepare_qa_storage_state(site_url, country):
    """内部 QA 会话：按站点+国家加载第一方会话状态；7天封存旧状态。"""
    import os
    import shutil
    base_dir, state_path, meta_path = _qa_session_paths(site_url, country)
    os.makedirs(base_dir, exist_ok=True)
    now = time.time()
    meta = _qa_load_json(meta_path, {}) or {}
    created_at = float(meta.get("created_at") or now)
    if os.path.exists(state_path) and now - created_at >= QA_SESSION_MAX_DAYS * 86400:
        archive_dir = os.path.join(base_dir, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.move(state_path, os.path.join(archive_dir, f"storage_state_{stamp}.json"))
        if os.path.exists(meta_path):
            shutil.move(meta_path, os.path.join(archive_dir, f"meta_{stamp}.json"))
        log.info(f"[QA会话] {country}/{_qa_site_host(site_url)} 已满7天，旧会话已封存，本次新建上下文")
        return None, state_path, meta_path
    if not os.path.exists(state_path):
        return None, state_path, meta_path
    last_ad_cleanup_at = float(meta.get("last_ad_cleanup_at") or created_at)
    if now - last_ad_cleanup_at >= QA_AD_COOKIE_CLEANUP_DAYS * 86400:
        state = _qa_load_json(state_path, {"cookies": [], "origins": []}) or {"cookies": [], "origins": []}
        state = _qa_filter_storage_state(state, site_url, clean_ad_cookies=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        meta["last_ad_cleanup_at"] = now
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        log.info(f"[QA会话] 已执行7天周期第三方广告/跟踪Cookie清理: {country}/{_qa_site_host(site_url)}")
    log.info(f"[QA会话] 加载站点+国家会话状态: {country}/{_qa_site_host(site_url)}")
    return state_path, state_path, meta_path


def save_qa_storage_state(context, site_url, country, state_path, meta_path):
    """保存内部 QA 第一方会话状态，不做全局清空。"""
    import os
    if not context or not state_path or not meta_path:
        return
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    state = context.storage_state()
    state = _qa_filter_storage_state(state, site_url, clean_ad_cookies=False)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    now = time.time()
    meta = _qa_load_json(meta_path, {}) or {}
    meta.setdefault("created_at", now)
    meta.setdefault("last_ad_cleanup_at", now)
    meta["last_saved_at"] = now
    meta["site_host"] = _qa_site_host(site_url)
    meta["country"] = (country or "US").upper()
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log.info(f"[QA会话] 已保存第一方Cookie/localStorage: {meta['country']}/{meta['site_host']}")


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ========== Flask 安全配置 ==========
import secrets
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ========== HTTP Basic Auth 中间件（已禁用）==========
# from functools import wraps

# AUTH_USER = os.environ.get("FLASK_AUTH_USER", "admin")
# AUTH_PASS = os.environ.get("FLASK_AUTH_PASS", "")  # 默认无密码，生产环境必须设置

# def _check_basic_auth():
#     """检查 HTTP Basic Auth，返回是否认证通过"""
#     if not AUTH_PASS:
#         # 未配置密码时仅允许本地访问
#         if request.remote_addr not in ("127.0.0.1", "::1"):
#             return False
#         return True
#     auth = request.authorization
#     if not auth:
#         return False
#     return auth.username == AUTH_USER and auth.password == AUTH_PASS

# @app.before_request
# def require_auth():
#     """全局认证中间件，跳过健康检查接口"""
#     if request.path in ("/health", "/ping"):
#         return None
#     if not _check_basic_auth():
#         return Response(
#             "Authentication required",
#             status=401,
#             headers={"WWW-Authenticate": 'Basic realm="Login Required"'}
#         )
#     return None

# 全局变量
config = {
    # 网络Tab参数
    "ip_proxy_api": "",
    "ip_proxy_user": "",
    "ip_proxy_pwd": "",
    "skip_browser_ip_check": False,
    "webrtc_leak_check_enabled": True,
    "qa_session_enabled": True,
    "session_mode": "country_host_7d",
    "ua_repeat_max_rate": 0.2,
    
    # 任务Tab参数
    "target_urls": [
        {"url": "https://baidu.com", "enabled": True},
        {"url": "", "enabled": False},
        {"url": "", "enabled": False},
        {"url": "", "enabled": False},
        {"url": "", "enabled": False},
    ],
    "site_creation_date": "",
    "selected_models": ["normal"],
    "daily_traffic_range": {
        "new": {"min": 50, "max": 150},
        "mid": {"min": 150, "max": 500},
        "old": {"min": 500, "max": 1500}
    },
    "plan_days": 1,
    "ip_provider_type": "proxy_api",
    "task_interval": {"min": 10, "max": 60},
    
    # 模型Tab参数（与config.json保持一致）
    "ad_stay_time": {"min": 3, "max": 40},
    "page_load_wait": {"min": 1, "max": 8},
    "scroll_pixels": {"min": 200, "max": 1000},
    "scroll_wait": {"min": 0.5, "max": 5},
    "ad_click_prob": {"min": 0.005, "max": 0.05},
    "ad_click_wait": {"min": 2, "max": 20},
    "random_click_count": {"min": 3, "max": 10},
    "random_click_wait": {"min": 0.5, "max": 3},
    "qa_human_profile": "standard",
    "total_stay": {"min": 120, "max": 300},
    "mouse_move_count": {"min": 2, "max": 20},
    "mouse_move_steps": {"min": 10, "max": 60},
    "mouse_move_wait": {"min": 0.1, "max": 1},
    "scroll_count": {"min": 2, "max": 10},
    "mouse_move_steps": {"min": 50, "max": 250},
    "bezier_pause_prob": {"min": 0.05, "max": 0.2},
    "mouse_move_pause": {"min": 0.01, "max": 0.1},
    
    # 其他参数
    "enabled": False,
    "ad_selector": ".ad-container, [class*='ad'], [id*='ad']",
    "enable_seo": True,
    "skip_timezone_check": False,
    "skip_ip_leak_check": False,
    "headless": True,
    "log_mode": "test",
    "use_real_chrome": True,
    "proxy_timeout": 30,
    "max_retries": 3,
    
    "seo": {
        "search_engines": [
            {"id": "google", "name": "谷歌", "url": "https://www.google.com/search?q=", "language": "en", "type": "search"},
            {"id": "bing", "name": "必应", "url": "https://www.bing.com/search?q=", "language": "en", "type": "search"},
            {"id": "baidu", "name": "百度", "url": "https://www.baidu.com/s?wd=", "language": "zh", "type": "search"},
            {"id": "sogou", "name": "搜狗", "url": "https://www.sogou.com/web?query=", "language": "zh", "type": "search"},
            {"id": "facebook", "name": "Facebook", "url": "https://www.facebook.com/", "language": "en", "type": "social"},
            {"id": "twitter", "name": "Twitter/X", "url": "https://x.com/", "language": "en", "type": "social"},
            {"id": "reddit", "name": "Reddit", "url": "https://www.reddit.com/", "language": "en", "type": "social"},
            {"id": "instagram", "name": "Instagram", "url": "https://www.instagram.com/", "language": "en", "type": "social"},
            {"id": "linkedin", "name": "LinkedIn", "url": "https://www.linkedin.com/", "language": "en", "type": "social"},
            {"id": "tiktok", "name": "TikTok", "url": "https://www.tiktok.com/", "language": "en", "type": "social"}
        ],
        "region_engine_map": {
            "US": ["google", "bing", "facebook", "twitter", "reddit", "instagram"],
            "GB": ["google", "bing", "facebook", "twitter", "reddit"],
            "AU": ["google", "bing", "facebook", "reddit", "instagram"],
            "DE": ["google", "bing", "facebook", "instagram"],
            "FR": ["google", "bing", "facebook", "instagram"],
            "JP": ["google", "bing", "twitter", "instagram", "tiktok"],
            "CN": ["baidu", "sogou", "tiktok"]
        },
        "keyword_pools": {
            "zh": ["广告联盟", "SEO优化", "网站推广", "网络营销", "数字营销"],
            "en": ["affiliate marketing", "SEO optimization", "website promotion", "digital marketing", "online marketing"]
        },
        "referer_mode": "dynamic"
    },
    
    # 视频广告配置
    "video_ad": {
        "enabled": False,
        "video_urls": [
            "https://www.example.com/video1",
            "https://www.example.com/video2"
        ],
        "min_watch_time": 30,
        "max_watch_time": 60,
        "click_probability": 0.3,
        "skip_intro": 20,
        "skip_outro": 30,
        "playback_rate_min": 1.2,
        "playback_rate_max": 1.5
    },
    "vt_video_urls": "",
    "vt_entry_mode": "auto",
    "vt_layer2_video_mode": "auto",
    "vt_watch_count": 3,
    "vt_task_days": 1,
    "vt_duration_min": 30,
    "vt_duration_max": 120,
    "vt_speed_min": 1.0,
    "vt_speed_max": 2.0,
    "vt_interval_min": 5,
    "vt_interval_max": 15,
    "vt_udis_referer": "https://udisxxx.com/",
    "qa_run_phases": {
        "website": True,
        "video": True
    },
    "vt_layer1_keywords": [],
    "vt_layer2_keywords": [],
    "vt_layer1_fallback_urls": [],
    "vt_layer2_fallback_urls": [],

    # 网页跳转配置（网页浏览模式专用）
    "web_navigation": {
        # 第1层（首页）→ 第2层
        "layer_1": {
            "keywords": ["all book", "全部书籍"],
            "fallback_urls": [],
            "stay_ratio": 0.1,
            "min_stay": 10
        },
        # 第2层 → 第3层
        "layer_2": {
            "keywords": ["Chapter", "章节"],
            "fallback_urls": [],
            "stay_ratio": 0.1,
            "min_stay": 10
        },
        # 第3层 → 第4层
        "layer_3": {
            "keywords": ["Chapter 1", "Chapter 2", "Chapter 3"],
            "fallback_urls": [],
            "stay_ratio": 0.2,
            "min_stay": 10
        },
        # 第4层 → 第5层（可选）
        "layer_4": {
            "keywords": [],
            "fallback_urls": [],
            "stay_ratio": 0.3,
            "min_stay": 10
        },
        # 第5层
        "layer_5": {
            "keywords": [],
            "fallback_urls": [],
            "stay_ratio": 0.2,
            "min_stay": 10
        },
        # 循环次数配置
        "loop_count": {"min": 1, "max": 3},
        # 每轮浏览的间隔时长（秒）
        "loop_interval": {"min": 1, "max": 5},
    },
    
    # 代理池配置
    "proxy_pool": [
        {"enabled": True, "country_code": "US", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "GB", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "DE", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "FR", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "JP", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "SG", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "HK", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "ID", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "AU", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""},
        {"enabled": True, "country_code": "NZ", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""}
    ]
}

DEFAULT_CONFIG = copy.deepcopy(config)


def deep_merge_defaults(defaults, overrides):
    merged = copy.deepcopy(defaults)
    if not isinstance(overrides, dict):
        return merged
    for key, value in overrides.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = deep_merge_defaults(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def ensure_config_defaults():
    global config
    fixed = deep_merge_defaults(DEFAULT_CONFIG, config)
    config.clear()
    config.update(fixed)


task_running = False
_single_task_mode = False  # 单独任务模式标志：不影响网站任务状态显示
pending_plan = None
current_task_idx = -1  # 当前正在执行的任务索引（-1表示无）
current_plan = None    # 当前执行的计划

# 历史任务存储
historical_tasks = []
HISTORICAL_TASKS_FILE = "historical_tasks.json"

# 指纹和UA统计存储
fingerprint_stats = {
    "ua_usage": {},  # key: ua_string, value: count
    "fingerprint_usage": {},  # key: fingerprint_id, value: count
    "history": []  # 每次任务的记录
}
FINGERPRINT_STATS_FILE = "fingerprint_stats.json"

# ==================== UA 池管理器 ====================
class IPSessionManager:
    """IP 会话管理器，确保单 IP 单次会话≥5分钟，24小时内不超过4次访问"""
    
    def __init__(self):
        import os
        import json
        self.session_file = "ip_session_history.json"
        self.session_history = self._load_session_history()
        self.max_session_duration = 300  # 5分钟
        self.max_daily_visits = 4       # 24小时内不超过4次访问
        self.daily_window = 86400       # 24小时
        
    def _load_session_history(self):
        """加载会话历史记录"""
        import os
        import json
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"加载 IP 会话历史记录失败: {e}")
                return {}
        return {}
        
    def _save_session_history(self):
        """保存会话历史记录"""
        import json
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(self.session_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存 IP 会话历史记录失败: {e}")
            
    def _clean_old_sessions(self):
        """清理过期的会话记录"""
        import time
        current_time = time.time()
        cleaned_history = {}
        for ip, sessions in self.session_history.items():
            # 只保留 24 小时内的会话记录
            valid_sessions = [
                s for s in sessions if current_time - s["timestamp"] <= self.daily_window
            ]
            if valid_sessions:
                cleaned_history[ip] = valid_sessions
        self.session_history = cleaned_history
        self._save_session_history()
        
    def is_ip_available(self, ip):
        """检查 IP 是否可用"""
        import time
        current_time = time.time()
        self._clean_old_sessions()
        
        if ip not in self.session_history:
            return True
        
        # 检查 24 小时内的访问次数
        daily_visits = len(self.session_history[ip])
        if daily_visits >= self.max_daily_visits:
            log.warning(f"IP {ip} 24小时内访问次数已达 {daily_visits} 次，超过限制（{self.max_daily_visits}次）")
            return False
            
        # 检查上次会话结束时间
        last_session = self.session_history[ip][-1]
        session_duration = current_time - last_session["timestamp"]
        if session_duration < self.max_session_duration:
            log.warning(f"IP {ip} 上次会话结束时间小于 {self.max_session_duration} 秒，需等待 {self.max_session_duration - session_duration:.1f} 秒")
            return False
            
        return True
        
    def record_ip_session(self, ip):
        """记录 IP 会话"""
        import time
        current_time = time.time()
        if ip not in self.session_history:
            self.session_history[ip] = []
        self.session_history[ip].append({
            "timestamp": current_time
        })
        self._save_session_history()
        
    def get_ip_session_info(self, ip):
        """获取 IP 会话信息"""
        if ip not in self.session_history:
            return {
                "daily_visits": 0,
                "last_visit": None,
                "available": True
            }
        daily_visits = len(self.session_history[ip])
        last_visit = self.session_history[ip][-1]["timestamp"]
        available = self.is_ip_available(ip)
        return {
            "daily_visits": daily_visits,
            "last_visit": last_visit,
            "available": available
        }
        
ip_session_manager = IPSessionManager()

class UAPoolManager:
    """UA 池管理器，负责 24 小时内的 UA 去重（与 IP 去重窗口对齐）"""
    
    UA_HISTORY_FILE = "ua_usage_history.json"
    WINDOW_HOURS = 24
    
    def _safe_log(self, level, message):
        """安全记录日志，log 不可用时回退到 print"""
        try:
            if level == "info":
                log.info(message)
            elif level == "warning":
                log.warning(message)
            elif level == "error":
                log.error(message)
            elif level == "debug":
                log.debug(message)
        except NameError:
            print(f"[{level.upper()}] {message}")
    
    # 超大幅扩充的 UA 基础库
    BASE_UA_POOL = {
        "en": [
            # Chrome Windows (不同版本)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            # Edge Windows
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
            # Safari Mac (不同版本)
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
            # Chrome Mac
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            # Chrome Linux
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            # Firefox Windows
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
            # Firefox Mac
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.14; rv:123.0) Gecko/20100101 Firefox/123.0",
            # Windows 11 UA variants
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ],
        "zh": [
            # Chrome Windows (CN)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            # Edge Windows (CN)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
            # Safari Mac (CN)
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            # Chrome Mac (CN)
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            # Chrome Linux (CN)
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            # Firefox Windows (CN)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            # Firefox Mac (CN)
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
            # 360 Browser
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.81 Safari/537.36 QIHU 360SE",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36 QIHU 360EE",
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36 QIHU 360SE",
            # QQ Browser
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.25 Safari/537.36 Core/1.70.3880.400 QQBrowser/10.8.4554.400",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.60 Safari/537.36 QQBrowser/11.2.5121.400",
            # Sogou Browser
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 SE 2.X MetaSr 1.0",
            # UC Browser
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 UBrowser/6.2.4098.1004"
        ]
    }
    
    # Chrome 版本号池（用于动态生成变体）
    CHROME_VERSIONS = ["122.0.0.0", "123.0.0.0", "124.0.0.0", "125.0.0.0", "126.0.0.0", "127.0.0.0", "128.0.0.0", "129.0.0.0", "130.0.0.0", "131.0.0.0"]
    FIREFOX_VERSIONS = ["122.0", "123.0", "124.0", "125.0", "126.0", "127.0", "128.0", "129.0", "130.0", "131.0", "132.0", "133.0"]
    SAFARI_VERSIONS = ["16.5", "16.6", "17.0", "17.1", "17.2", "17.3", "17.4", "17.5", "17.6"]
    EDGE_VERSIONS = ["122.0.0.0", "123.0.0.0", "124.0.0.0", "125.0.0.0", "126.0.0.0", "127.0.0.0", "128.0.0.0", "129.0.0.0", "130.0.0.0", "131.0.0.0"]
    
    def __init__(self):
        import os
        import json
        self.ua_history = {}  # {ua: last_used_timestamp}
        self.total_ua_used = 0
        self.reused_ua_count = 0
        
        # 加载 fake_useragent 作为补充
        try:
            from fake_useragent import UserAgent
            self.ua_generator = UserAgent()
            self._safe_log("info", "fake_useragent 加载成功")
        except Exception as e:
            self._safe_log("warning", f"fake_useragent 加载失败: {e}，仅使用基础库")
            self.ua_generator = None
        
        # 从文件加载历史记录
        if os.path.exists(self.UA_HISTORY_FILE):
            try:
                with open(self.UA_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ua_history = data.get("ua_history", {})
                    self.total_ua_used = data.get("total_ua_used", 0)
                    self.reused_ua_count = data.get("reused_ua_count", 0)
                self._safe_log("info", f"UA 历史记录加载成功，当前记录 {len(self.ua_history)} 个 UA")
            except Exception as e:
                self._safe_log("error", f"加载 UA 历史记录失败: {e}")
                self.ua_history = {}
    
    def _generate_ua_variant(self, base_ua):
        """基于基础 UA 生成变体（修改版本号等）"""
        import random
        import re
        
        ua = base_ua
        
        # 替换 Chrome 版本号
        if "Chrome/" in ua:
            current_ver = re.search(r"Chrome/([\d.]+)", ua)
            if current_ver:
                new_ver = random.choice(self.CHROME_VERSIONS)
                ua = ua.replace(f"Chrome/{current_ver.group(1)}", f"Chrome/{new_ver}")
        
        # 替换 Firefox 版本号
        if "Firefox/" in ua:
            current_ver = re.search(r"Firefox/([\d.]+)", ua)
            if current_ver:
                new_ver = random.choice(self.FIREFOX_VERSIONS)
                ua = ua.replace(f"Firefox/{current_ver.group(1)}", f"Firefox/{new_ver}")
        
        # 替换 Safari 版本号
        if "Version/" in ua and "Safari/" in ua:
            current_ver = re.search(r"Version/([\d.]+)", ua)
            if current_ver:
                new_ver = random.choice(self.SAFARI_VERSIONS)
                ua = ua.replace(f"Version/{current_ver.group(1)}", f"Version/{new_ver}")
        
        # 替换 Edge 版本号
        if "Edg/" in ua:
            current_ver = re.search(r"Edg/([\d.]+)", ua)
            if current_ver:
                new_ver = random.choice(self.EDGE_VERSIONS)
                ua = ua.replace(f"Edg/{current_ver.group(1)}", f"Edg/{new_ver}")
        
        # 随机修改 Windows 版本显示
        if "Windows NT 10.0" in ua and random.random() < 0.3:
            if random.random() < 0.5:
                ua = ua.replace("Windows NT 10.0", "Windows NT 10.0; WOW64")
            else:
                ua = ua.replace("Windows NT 10.0", "Windows NT 10.0")  # 保持原样
        
        # 随机修改 Mac OS X 版本
        if "Mac OS X 10_15_7" in ua and random.random() < 0.2:
            mac_versions = ["10_15_5", "10_15_6", "10_15_7", "10_14_6"]
            ua = ua.replace("Mac OS X 10_15_7", f"Mac OS X {random.choice(mac_versions)}")
        
        return ua
    
    def _clean_old_records(self):
        """清理超出去重窗口（WINDOW_HOURS 小时）前的记录"""
        import time
        cutoff_time = time.time() - (self.WINDOW_HOURS * 3600)
        
        cleaned_history = {}
        for ua, ts in self.ua_history.items():
            if ts >= cutoff_time:
                cleaned_history[ua] = ts
        
        if len(cleaned_history) != len(self.ua_history):
            self._safe_log("debug", f"清理了 {len(self.ua_history) - len(cleaned_history)} 条旧的 UA 记录")
        self.ua_history = cleaned_history
    
    def _get_ua_pool(self, lang_prefix):
        """获取指定语言的 UA 池（基础库 + 变体 + fake_useragent）"""
        import random
        ua_pool = []
        base_uas = list(self.BASE_UA_POOL.get(lang_prefix, self.BASE_UA_POOL["en"]))
        
        # 1. 添加所有基础 UA
        ua_pool.extend(base_uas)
        
        # 2. 生成并添加变体（每个基础 UA 生成 3-5 个变体）
        for base_ua in base_uas:
            for _ in range(random.randint(3, 5)):
                variant = self._generate_ua_variant(base_ua)
                if variant not in ua_pool:
                    ua_pool.append(variant)
        
        # 3. 添加 fake_useragent 生成的 UA
        if self.ua_generator:
            try:
                if lang_prefix == "zh":
                    # 中文环境
                    for _ in range(20):
                        try:
                            ua = self.ua_generator.chrome
                            if ua and ua not in ua_pool:
                                ua_pool.append(ua)
                        except Exception:
                            pass
                else:
                    # 英文环境
                    for _ in range(30):
                        try:
                            ua = self.ua_generator.random
                            if ua and ua not in ua_pool:
                                ua_pool.append(ua)
                        except Exception:
                            pass
            except Exception as e:
                self._safe_log("debug", f"fake_useragent 生成失败: {e}")
        
        random.shuffle(ua_pool)
        self._safe_log("info", f"UA 池初始化完成，共 {len(ua_pool)} 个 UA（基础 {len(base_uas)} + 变体 + fake）")
        return ua_pool
    
    @staticmethod
    def _is_valid_ua(ua):
        """UA 字符串格式合法性校验：拦截畸形/恶意构造 UA。

        合法浏览器 UA 必须：以 Mozilla/5.0 开头、长度合理、含括号平台段、
        含至少一个已知浏览器标记，且不含换行/控制字符。
        """
        try:
            if not ua or not isinstance(ua, str):
                return False
            if not (40 <= len(ua) <= 512):
                return False
            if not ua.startswith("Mozilla/5.0"):
                return False
            if "(" not in ua or ")" not in ua:
                return False
            # 不允许换行/制表/控制字符
            if any(ord(c) < 32 for c in ua):
                return False
            markers = ("Chrome/", "Firefox/", "Safari/", "Edg/", "Edge/", "Chromium/", "OPR/", "Version/")
            if not any(m in ua for m in markers):
                return False
            return True
        except Exception:
            return False

    def get_ua(self, lang_prefix, browser_family="chromium"):
        import time
        import random
        
        # 先清理旧记录
        self._clean_old_records()
        
        # 获取 UA 池
        ua_pool = self._get_ua_pool(lang_prefix)
        if browser_family == "chromium":
            chromium_pool = [ua for ua in ua_pool if ("Chrome/" in ua or "Edg/" in ua or "Chromium/" in ua) and "Firefox/" not in ua and "Version/" not in ua]
            if chromium_pool:
                ua_pool = chromium_pool
            else:
                self._safe_log("warning", "Chromium UA 池为空，回退使用完整 UA 池")
        
        # 计算当前重复率
        if self.total_ua_used > 0:
            current_repeat_rate = self.reused_ua_count / self.total_ua_used
        else:
            current_repeat_rate = 0
        
        self._safe_log("debug", f"当前 UA 池大小: {len(ua_pool)}, 已使用 UA: {len(self.ua_history)}, 总任务: {self.total_ua_used}, 复用数: {self.reused_ua_count}, 重复率: {current_repeat_rate:.2%}")
        
        # 找出未使用的 UA
        unused_uas = [ua for ua in ua_pool if ua not in self.ua_history]
        
        # 优先使用未使用的 UA
        if unused_uas:
            selected_ua = random.choice(unused_uas)
            is_reused = False
            self._safe_log("debug", f"选择了新的 UA: {selected_ua[:60]}...")
        else:
            # 所有 UA 都用过了，检查是否允许复用（重复率 < 20%）
            if current_repeat_rate < 0.2:
                # 允许复用，选择使用时间最早的 UA
                sorted_uas = sorted(self.ua_history.items(), key=lambda x: x[1])
                selected_ua = sorted_uas[0][0]
                is_reused = True
                self._safe_log("debug", f"复用了 UA: {selected_ua[:60]}...")
            else:
                # 重复率超过 20%，需要生成全新的 UA 变体
                self._safe_log("warning", f"需要生成新的 UA 变体（当前重复率: {current_repeat_rate:.2%}）")
                selected_ua = self._generate_ua_variant(random.choice(ua_pool))
                # 确保这个变体没有被使用过
                attempts = 0
                while selected_ua in self.ua_history and attempts < 20:
                    selected_ua = self._generate_ua_variant(random.choice(ua_pool))
                    attempts += 1
                is_reused = selected_ua in self.ua_history
        
        # UA 字符串格式合法性自检：若选中的 UA 畸形，回退到池中第一个合法 UA，避免发送异常 UA 被风控识别
        if not self._is_valid_ua(selected_ua):
            self._safe_log("warning", f"⚠️ 选中的 UA 格式非法，已回退: {selected_ua[:60]}")
            _valid_candidates = [ua for ua in ua_pool if self._is_valid_ua(ua)]
            if _valid_candidates:
                selected_ua = random.choice(_valid_candidates)
                is_reused = selected_ua in self.ua_history
        
        # 更新记录
        self.ua_history[selected_ua] = time.time()
        self.total_ua_used += 1
        if is_reused:
            self.reused_ua_count += 1
        
        # 保存到文件
        self._save_history()
        
        # 再次检查并报警高重复率
        new_repeat_rate = self.reused_ua_count / self.total_ua_used
        if new_repeat_rate >= 0.2:
            self._safe_log("warning", f"⚠️ 当前 UA 重复率: {new_repeat_rate:.2%}（超过 20% 警戒线），总任务: {self.total_ua_used}，复用: {self.reused_ua_count}")
        elif new_repeat_rate >= 0.15:
            self._safe_log("info", f"UA 重复率: {new_repeat_rate:.2%}（接近警戒线）")
        
        return selected_ua
    
    def _save_history(self):
        """保存历史记录到文件"""
        import json
        try:
            with open(self.UA_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "ua_history": self.ua_history,
                    "total_ua_used": self.total_ua_used,
                    "reused_ua_count": self.reused_ua_count
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._safe_log("error", f"保存 UA 历史记录失败: {e}")

def load_historical_tasks():
    """从文件加载历史任务"""
    global historical_tasks
    import os
    import json
    if os.path.exists(HISTORICAL_TASKS_FILE):
        try:
            with open(HISTORICAL_TASKS_FILE, "r", encoding="utf-8") as f:
                historical_tasks = json.load(f)
        except Exception as e:
            log.error(f"加载历史任务失败: {e}")
            historical_tasks = []
    else:
        historical_tasks = []

def save_historical_tasks():
    """保存历史任务到文件"""
    import json
    try:
        with open(HISTORICAL_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(historical_tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"保存历史任务失败: {e}")

def load_fingerprint_stats():
    """从文件加载指纹统计"""
    global fingerprint_stats
    import os
    import json
    if os.path.exists(FINGERPRINT_STATS_FILE):
        try:
            with open(FINGERPRINT_STATS_FILE, "r", encoding="utf-8") as f:
                fingerprint_stats = json.load(f)
        except Exception as e:
            log.error(f"加载指纹统计失败: {e}")
            fingerprint_stats = {"ua_usage": {}, "fingerprint_usage": {}, "history": []}
    else:
        fingerprint_stats = {"ua_usage": {}, "fingerprint_usage": {}, "history": []}

def save_fingerprint_stats():
    """保存指纹统计到文件"""
    import json
    try:
        with open(FINGERPRINT_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(fingerprint_stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"保存指纹统计失败: {e}")

def _today_key():
    """返回当前 UTC 日期字符串作为单日点击统计的键"""
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")

def get_daily_ad_clicks():
    """获取今日累计广告点击次数（跨任务/跨会话持久化）"""
    try:
        daily = fingerprint_stats.setdefault("daily_ad_clicks", {})
        return int(daily.get(_today_key(), 0))
    except Exception:
        return 0

def record_ad_click(n=1):
    """记录广告点击次数并持久化，返回今日累计点击数"""
    try:
        daily = fingerprint_stats.setdefault("daily_ad_clicks", {})
        today = _today_key()
        daily[today] = int(daily.get(today, 0)) + int(n)
        # 仅保留最近 7 天的每日点击记录
        keys = sorted(daily.keys())
        for k in keys[:-7]:
            daily.pop(k, None)
        save_fingerprint_stats()
        return daily[today]
    except Exception:
        return 0

def daily_ad_click_limit_reached():
    """是否已达今日单日点击上限（config.daily_ad_click_limit，0=不限）"""
    try:
        limit = int(config.get("daily_ad_click_limit", 0) or 0)
        if limit <= 0:
            return False
        return get_daily_ad_clicks() >= limit
    except Exception:
        return False

def record_fingerprint_usage(fingerprint_id, user_agent, country_code):
    """记录指纹和UA使用情况"""
    import datetime as _dt
    import pytz
    
    # 记录UA使用
    if user_agent in fingerprint_stats["ua_usage"]:
        fingerprint_stats["ua_usage"][user_agent] += 1
    else:
        fingerprint_stats["ua_usage"][user_agent] = 1
    
    # 记录指纹使用
    if fingerprint_id in fingerprint_stats["fingerprint_usage"]:
        fingerprint_stats["fingerprint_usage"][fingerprint_id] += 1
    else:
        fingerprint_stats["fingerprint_usage"][fingerprint_id] = 1
    
    # 记录历史
    now = _dt.datetime.now(pytz.UTC)
    fingerprint_stats["history"].append({
        "timestamp": now.isoformat(),
        "timestamp_local": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint_id": fingerprint_id,
        "user_agent": user_agent,
        "country_code": country_code
    })
    
    # 只保留近三天的历史记录
    three_days_ago = now - _dt.timedelta(days=3)
    fingerprint_stats["history"][:] = [
        h for h in fingerprint_stats["history"]
        if _dt.datetime.fromisoformat(h["timestamp"]) >= three_days_ago
    ]
    
    save_fingerprint_stats()

def add_to_historical_tasks(plan):
    """将完成的任务计划添加到历史记录中"""
    import datetime as _dt
    import pytz
    import copy
    
    now = _dt.datetime.now(pytz.UTC)
    plan_copy = copy.deepcopy(plan)
    plan_copy["created_at"] = now.isoformat()
    plan_copy["created_at_local"] = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    
    historical_tasks.append(plan_copy)
    
    # 只保留近三天的记录
    three_days_ago = now - _dt.timedelta(days=3)
    historical_tasks[:] = [
        p for p in historical_tasks
        if _dt.datetime.fromisoformat(p["created_at"]) >= three_days_ago
    ]
    
    save_historical_tasks()


def interruptible_sleep(seconds, check_interval=0.5):
    """
    可中断的 sleep：分片休眠，期间持续检查 task_running，
    支持「点击停止按钮后 1 秒内立即中断当前等待」。
    返回 True 表示完整睡完，False 表示被中断。
    """
    import time as _t
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if not task_running:
            return False
        step = min(check_interval, remaining)
        _t.sleep(step)
        remaining -= step
    return True


def video_interruptible_sleep(seconds, check_interval=0.5):
    """
    可中断 sleep：分片休眠，期间持续检查 task_running，
    支持「点击停止按钮后 1 秒内立即中断当前等待」。
    返回 True 表示完整睡完，False 表示被中断。
    """
    import time as _t
    global task_running
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if not task_running:
            return False
        # 每 5 秒更新一次真人模型心跳，避免超时
        if int(remaining) % 5 == 0:
            human_model_tick("video_interruptible_sleep")
        step = min(check_interval, remaining)
        _t.sleep(step)
        remaining -= step
    return True


def _human_model_supervisor_loop():
    return  # 已禁用心跳监督，不再强制停止任何任务


def start_human_model(task_type):
    global human_model_thread
    with human_model_lock:
        human_model_state.update({
            "running": True,
            "task_type": task_type,
            "last_heartbeat": time.time(),
            "last_source": "start",
            "last_error": ""
        })
    human_model_stop_event.clear()
    if human_model_thread is None or not human_model_thread.is_alive():
        human_model_thread = threading.Thread(target=_human_model_supervisor_loop, daemon=True)
        human_model_thread.start()
    log.info(f"[真人模型监督] 已启动: {task_type}")


def stop_human_model():
    with human_model_lock:
        human_model_state["running"] = False
        human_model_state["task_type"] = ""
    human_model_stop_event.set()
    log.info("[真人模型监督] 已停止")


def human_model_tick(source):
    with human_model_lock:
        if human_model_state.get("running"):
            human_model_state["last_heartbeat"] = time.time()
            human_model_state["last_source"] = source


def ensure_human_model_alive():
    return True


def get_global_session_mode():
    if not config.get("qa_session_enabled", True):
        return "new_each_task"
    mode = config.get("session_mode", "country_host_7d")
    return mode if mode in ("new_each_task", "country_host_7d") else "country_host_7d"


def simulate_human_in_window(page, duration, stats, current_x, current_y, config, page_name="页面", deadline=None):
    """
    在 duration 秒的时间窗内，穿插执行真人行为（鼠标移动/滚动/键盘/随机停顿/随机点击）。
    所有动作参数均从 config 中读取（random.uniform(min, max)）。
    deadline: 绝对时间戳。若提供且早于 duration 自然结束时间，则提前停。
    返回更新后的 (current_x, current_y)。
    """
    import time as _t
    import random as _rnd

    # 补全 stats 默认字段
    for _k in ("mouse_moves", "scrolls", "scroll_distance", "clicks",
               "waits", "key_presses", "total_stay"):
        stats.setdefault(_k, 0)

    start = _t.time()
    duration = max(0.0, float(duration))
    window_end = start + duration
    if deadline is not None and deadline < window_end:
        window_end = float(deadline)

    # ========== 从配置读取所有真人模型参数 ==========
    scroll_cfg = config.get("scroll_pixels", {"min": 100, "max": 800})
    scroll_wait_cfg = config.get("scroll_wait", {"min": 0.5, "max": 2.0})
    scroll_count_cfg = config.get("scroll_count", {"min": 2, "max": 10})
    mouse_steps_cfg = config.get("mouse_move_steps", {"min": 50, "max": 250})
    mouse_wait_cfg = config.get("mouse_move_wait", {"min": 0.1, "max": 1.0})
    mouse_pause_cfg = config.get("mouse_move_pause", {"min": 0.01, "max": 0.1})
    bezier_pause_cfg = config.get("bezier_pause_prob", {"min": 0.05, "max": 0.2})
    click_count_cfg = config.get("random_click_count", {"min": 0, "max": 3})
    click_wait_cfg = config.get("random_click_wait", {"min": 0.5, "max": 2.0})
    page_load_cfg = config.get("page_load_wait", {"min": 1.0, "max": 3.0})

    # 页面加载后先等待（模拟真人反应）
    _init_wait = _rnd.uniform(page_load_cfg.get("min", 1.0), page_load_cfg.get("max", 3.0))
    _init_wait = min(_init_wait, max(0, window_end - _t.time()))
    if _init_wait > 0:
        _t.sleep(_init_wait)
        stats["total_stay"] += _init_wait
        stats["waits"] += 1

    log.info(
        f"[{page_name}] 🎭 真人模拟窗口启动: 时长 {duration:.1f}s，"
        f"动作随机（滚动/鼠标/点击/键盘），参数均从配置读取"
    )

    # 动作权重：滚动和鼠标为主
    actions = (
        ["scroll"] * 4 +
        ["mouse"] * 4 +
        ["click"] * 1 +
        ["key"] * 1
    )

    action_errors = 0
    loop_count = 0
    summary_interval = 6
    next_summary_at = summary_interval
    bezier_prob = _rnd.uniform(bezier_pause_cfg.get("min", 0.05), bezier_pause_cfg.get("max", 0.2))

    while True:
        human_model_tick(page_name)
        if not task_running or not ensure_human_model_alive():
            break
        remaining = window_end - _t.time()
        if remaining <= 0:
            break
        
        # 动作间隔：0.5-2s
        gap = min(remaining, _rnd.uniform(0.5, 2.0))
        _t.sleep(gap)
        stats["total_stay"] += gap
        if _t.time() >= window_end or not task_running:
            break

        loop_count += 1
        action = _rnd.choice(actions)
        try:
            if action == "scroll":
                dy = _rnd.randint(int(scroll_cfg.get("min", 100)), int(scroll_cfg.get("max", 800)))
                if _rnd.random() < 0.15:
                    dy = -dy
                page.evaluate(f"window.scrollBy(0, {dy})")
                stats["scrolls"] += 1
                stats["scroll_distance"] += abs(dy)
                # 滚动后等待（从配置读取）
                _sw = min(window_end - _t.time(), _rnd.uniform(scroll_wait_cfg.get("min", 0.5), scroll_wait_cfg.get("max", 2.0)))
                if _sw > 0:
                    _t.sleep(_sw)
                    stats["total_stay"] += _sw
            elif action == "mouse":
                tx = _rnd.randint(100, 1100)
                ty = _rnd.randint(100, 700)
                # 鼠标移动步数从配置读取
                steps = _rnd.randint(int(mouse_steps_cfg.get("min", 50)) // 10, int(mouse_steps_cfg.get("max", 250)) // 10)
                steps = max(3, steps)
                for s in range(steps):
                    if _t.time() >= window_end:
                        break
                    tt = (s + 1) / steps
                    mx = int(current_x + (tx - current_x) * tt + _rnd.randint(-2, 2))
                    my = int(current_y + (ty - current_y) * tt + _rnd.randint(-2, 2))
                    page.mouse.move(mx, my)
                    # 每步等待从配置读取
                    _step_wait = _rnd.uniform(mouse_pause_cfg.get("min", 0.01), mouse_pause_cfg.get("max", 0.1))
                    _t.sleep(_step_wait)
                    # 贝塞尔暂停概率
                    if _rnd.random() < bezier_prob:
                        _t.sleep(_rnd.uniform(0.1, 0.4))
                current_x, current_y = tx, ty
                stats["mouse_moves"] += 1
                # 鼠标移动后等待
                _mw = min(window_end - _t.time(), _rnd.uniform(mouse_wait_cfg.get("min", 0.1), mouse_wait_cfg.get("max", 1.0)))
                if _mw > 0:
                    _t.sleep(_mw)
                    stats["total_stay"] += _mw
            elif action == "click":
                # 随机点击（从配置读取次数和等待）
                cx = _rnd.randint(100, 1000)
                cy = _rnd.randint(100, 600)
                page.mouse.click(cx, cy)
                stats["clicks"] += 1
                _cw = min(window_end - _t.time(), _rnd.uniform(click_wait_cfg.get("min", 0.5), click_wait_cfg.get("max", 2.0)))
                if _cw > 0:
                    _t.sleep(_cw)
                    stats["total_stay"] += _cw
            elif action == "key":
                key = _rnd.choice(["PageDown", "PageUp", "ArrowDown", "ArrowUp", "End", "Home", "Space"])
                page.keyboard.press(key)
                stats["key_presses"] += 1
        except Exception:
            action_errors += 1

        # 周期摘要
        if loop_count >= next_summary_at:
            next_summary_at = loop_count + summary_interval
            elapsed = _t.time() - start
            log.info(
                f"[{page_name}] 🎭 真人模拟·实时摘要: 已进行 {elapsed:.1f}s，"
                f"滚动 {stats['scrolls']} 次({stats['scroll_distance']}px)，"
                f"鼠标 {stats['mouse_moves']} 次，"
                f"点击 {stats['clicks']} 次，"
                f"键盘 {stats['key_presses']} 次"
            )

    actual = _t.time() - start
    log.info(
        f"[{page_name}] 🎭 真人模拟窗口结束: 实耗 {actual:.1f}s / 计划 {duration:.1f}s，"
        f"动作循环 {loop_count} 次，鼠标 {stats['mouse_moves']} 次，"
        f"滚动 {stats['scrolls']} 次({stats['scroll_distance']}px)，"
        f"点击 {stats['clicks']} 次，"
        f"键盘 {stats['key_presses']} 次"
        + (f"，动作失败 {action_errors} 次" if action_errors else "")
    )
    return current_x, current_y


def create_ad_monitor():
    return {
        "containers": set(),
        "visible": set(),
        "exposed": set(),
        "exposed50": set(),
        "signatures": {},
        "refresh_count": 0,
        "scan_count": 0,
        "events": [],
        # 各广告位累计 ≥50% 可见的曝光时长（key -> 毫秒）
        "exposure_duration_ms": {},
        # 有效曝光达标的广告位（累计曝光时长 >= 阈值，符合 AdSense ≥50%可见且持续≥1秒标准）
        "effective_exposed": set(),
        # 用于跨扫描计算时长：上次扫描时间戳 + 上次处于 ≥50% 曝光的广告位集合
        "last_scan_ts": None,
        "prev_exposed50": set(),
    }


def scan_ads_during_task(page, ad_monitor, stage="页面"):
    """全过程 AdSense/GAM 广告监控：按元素去重，曝光/可见累计，不影响任务成功。"""
    if ad_monitor is None:
        return create_ad_monitor()
    try:
        result = page.evaluate("""
        () => {
            const selectors = [
                'ins.adsbygoogle',
                '.adsbygoogle',
                '[id*="google_ads_iframe"]',
                '[id^="google_ads_iframe"]',
                'iframe[id*="google_ads"]',
                'iframe[name*="google_ads"]',
                'iframe[src*="googlesyndication"]',
                'iframe[src*="doubleclick"]',
                '[data-ad-client]',
                '[data-ad-slot]'
            ];
            const seen = new Set();
            const vw = window.innerWidth || document.documentElement.clientWidth || 0;
            const vh = window.innerHeight || document.documentElement.clientHeight || 0;
            const items = [];
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach((el) => {
                    if (seen.has(el)) return;
                    seen.add(el);
                    const r = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const width = Math.round(r.width || 0);
                    const height = Math.round(r.height || 0);
                    if (width <= 0 || height <= 0) return;
                    const id = el.id || '';
                    const cls = typeof el.className === 'string' ? el.className : '';
                    const src = el.getAttribute('src') || el.getAttribute('data-ad-slot') || el.getAttribute('data-ad-client') || '';
                    const key = id || `${el.tagName}:${cls}:${Math.round(r.left)}:${Math.round(r.top)}:${width}x${height}`;
                    const visible = style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
                    const inViewport = visible && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
                    // AdSense 有效曝光标准：广告面积在视口内可见比例 >= 50%
                    let visibleRatio = 0;
                    if (inViewport && width > 0 && height > 0) {
                        const visW = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
                        const visH = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                        visibleRatio = (visW * visH) / (width * height);
                    }
                    const exposed = inViewport && width >= 20 && height >= 20;
                    const exposed50 = exposed && visibleRatio >= 0.5;
                    const loaded = !!src || el.children.length > 0 || (el.tagName || '').toLowerCase() === 'ins';
                    items.push({key, tag: el.tagName, id, cls, src, width, height, visible, inViewport, exposed, exposed50, visibleRatio: Math.round(visibleRatio*100)/100, loaded});
                });
            }
            return items;
        }
        """)
        if not isinstance(result, list):
            result = []

        before_count = len(ad_monitor["containers"])
        before_visible = len(ad_monitor["visible"])
        before_exposed = len(ad_monitor["exposed"])
        loaded_count = 0
        cur_exposed50 = set()

        for item in result:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            signature = f"{item.get('src') or ''}|{item.get('width')}x{item.get('height')}"
            if key in ad_monitor["signatures"] and ad_monitor["signatures"][key] != signature:
                ad_monitor["refresh_count"] += 1
            ad_monitor["signatures"][key] = signature
            ad_monitor["containers"].add(key)
            if item.get("visible") or item.get("inViewport"):
                ad_monitor["visible"].add(key)
            if item.get("exposed"):
                ad_monitor["exposed"].add(key)
            if item.get("exposed50"):
                ad_monitor["exposed50"].add(key)
                cur_exposed50.add(key)
            if item.get("loaded"):
                loaded_count += 1

        # ===== 广告位累计曝光时长 + 有效曝光达标判定 =====
        # 思路：相邻两次扫描间，对“两次都处于 ≥50% 可见”的广告位累加其间隔时长；
        # 累计时长达到阈值(默认1000ms，AdSense“≥50%可见且持续≥1秒”)即判定为有效曝光。
        now_ts = time.time()
        effective_threshold_ms = int(config.get("ad_effective_exposure_ms", 1000) or 1000)
        last_ts = ad_monitor.get("last_scan_ts")
        prev_exposed50 = ad_monitor.get("prev_exposed50") or set()
        if last_ts is not None:
            delta_ms = int(max(0.0, now_ts - last_ts) * 1000)
            # 单次间隔过长（如长时间停留）做上限保护，避免高估
            delta_ms = min(delta_ms, int(config.get("ad_exposure_max_gap_ms", 30000) or 30000))
            # 只对“上次和本次都 ≥50% 可见”的广告位累加（说明这段时间它持续曝光）
            for key in (prev_exposed50 & cur_exposed50):
                acc = ad_monitor["exposure_duration_ms"].get(key, 0) + delta_ms
                ad_monitor["exposure_duration_ms"][key] = acc
                if acc >= effective_threshold_ms:
                    ad_monitor["effective_exposed"].add(key)
        ad_monitor["last_scan_ts"] = now_ts
        ad_monitor["prev_exposed50"] = cur_exposed50

        ad_monitor["scan_count"] += 1
        new_containers = len(ad_monitor["containers"]) - before_count
        new_visible = len(ad_monitor["visible"]) - before_visible
        new_exposed = len(ad_monitor["exposed"]) - before_exposed
        event = {
            "stage": stage,
            "found_now": len(result),
            "loaded_now": loaded_count,
            "new_containers": new_containers,
            "new_visible": new_visible,
            "new_exposed": new_exposed,
            "exposed50_now": len(cur_exposed50),
            "effective_exposed": len(ad_monitor["effective_exposed"]),
        }
        ad_monitor["events"].append(event)
        _max_dur = max(ad_monitor["exposure_duration_ms"].values()) if ad_monitor["exposure_duration_ms"] else 0
        log.info(
            f"[广告监控][{stage}] scan={ad_monitor['scan_count']} "
            f"本次={len(result)} 新增容器={new_containers} "
            f"累计容器={len(ad_monitor['containers'])} "
            f"累计可见={len(ad_monitor['visible'])} 累计曝光={len(ad_monitor['exposed'])} "
            f"有效曝光达标={len(ad_monitor['effective_exposed'])} 最长曝光={_max_dur}ms "
            f"加载={loaded_count} 刷新={ad_monitor['refresh_count']}"
        )
    except Exception as e:
        ad_monitor["scan_count"] = ad_monitor.get("scan_count", 0) + 1
        log.warning(f"[广告监控][{stage}] 扫描失败: {str(e)[:120]}")
    return ad_monitor


def perform_real_search(page, target_url, selected_engine_id, selected_keyword, stats, current_x, current_y, config):
    """
    执行完整搜索引擎搜索跳转流程（带真人模拟），支持所有搜索引擎
    :param page: 浏览器页面
    :param target_url: 目标网址（要从搜索结果里找它）
    :param selected_engine_id: 引擎ID（google/bing/baidu/sogou/so360等）
    :param selected_keyword: 搜索关键词
    :param stats: 页面统计字典（给真人模拟用）
    :param current_x, current_y: 当前鼠标坐标
    :param config: 系统配置
    :return: (success, current_x, current_y)
    """
    from urllib.parse import urlparse
    import random
    import sys, os
    from seo_query_module import get_seo_query

    # ===== 各搜索引擎专属选择器 =====
    ENGINE_SELECTORS = {
        "google": {
            "search_box": 'input[name="q"]',
            "result_links": [
                'a[data-ved][href*="http"]',
                'a[data-ved]',
            ],
            "privacy_buttons": [
                'button[aria-label*="Accept all"]',
                'button:has-text("Accept all")',
                'button:has-text("Accept")',
                'div[id*="L2AGLb"] button',
            ],
        },
        "bing": {
            "search_box": 'input[name="q"]',
            "result_links": [
                'li.b_algo h2 a',
                'li.b_algo a[href*="http"]',
            ],
            "privacy_buttons": [
                'button[id*="bnp_btn_accept"]',
                'button:has-text("Accept")',
                'button[aria-label*="Accept"]',
            ],
        },
        "baidu": {
            "search_box": 'input[id="kw"], input[name="wd"]',
            "result_links": [
                'h3.t a[href*="http"]',
                'h3.c-title a',
                '.result h3 a',
                '.c-container h3 a',
            ],
            "privacy_buttons": [],  # 百度一般无隐私弹窗
        },
        "sogou": {
            "search_box": 'input[id="upquery"], input[name="query"]',
            "result_links": [
                'h3.vr-title a',
                '.results h3 a',
                '.vrwrap h3 a',
            ],
            "privacy_buttons": [],
        },
        "so360": {
            "search_box": 'input[id="keyword"], input[name="q"]',
            "result_links": [
                'h3.res-title a',
                '.result h3 a',
                'li.res-list h3 a',
            ],
            "privacy_buttons": [],
        },
    }

    try:
        log.info(f"🔍 [真搜索] 开始完整搜索跳转流程，引擎={selected_engine_id}, 关键词={selected_keyword}")
        
        # 1. 获取SEO查询实例和引擎配置
        seo_query = get_seo_query()
        selected_engine = seo_query.get_engine_by_id(selected_engine_id)
        if not selected_engine:
            log.warning(f"[真搜索] 找不到引擎配置，直接访问目标页")
            return False, current_x, current_y
        
        engine_url = selected_engine.get("url")
        homepage_url = seo_query.get_engine_homepage(engine_url)
        if not homepage_url:
            log.warning(f"[真搜索] 提取主页失败，直接访问目标页")
            return False, current_x, current_y

        # 获取该引擎的选择器（无则用通用兜底）
        selectors = ENGINE_SELECTORS.get(selected_engine_id, {
            "search_box": 'input[type="text"], input[name="q"], input[name="wd"], input[name="query"]',
            "result_links": ['h3 a[href*="http"]', '.result a', 'a[href*="' + urlparse(target_url).netloc + '"]'],
            "privacy_buttons": [],
        })

        log.info(f"🔍 [真搜索] 访问搜索引擎主页: {homepage_url}")
        
        # 2. 访问搜索引擎主页
        page.goto(homepage_url, timeout=60000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))

        # 2.5 在搜索引擎主页加入真人模拟窗口
        homepage_duration = random.uniform(1.5, 4.0)
        log.info(f"🔍 [真搜索] 在引擎主页停留：{homepage_duration:.1f}s，真人模型介入")
        current_x, current_y = simulate_human_in_window(page, homepage_duration, stats, current_x, current_y, config, page_name=f"搜索引擎主页({selected_engine_id})")

        # 3. 处理隐私弹窗（按引擎类型）
        privacy_buttons = selectors.get("privacy_buttons", [])
        if privacy_buttons:
            log.info(f"🔍 [真搜索] 尝试处理隐私弹窗")
            for selector in privacy_buttons:
                try:
                    btn = page.wait_for_selector(selector, timeout=5000)
                    if btn:
                        btn.click()
                        log.info(f"🔍 [真搜索] {selected_engine_id} 隐私弹窗已同意")
                        time.sleep(random.uniform(1.0, 2.5))
                        break
                except Exception:
                    continue

        # 4. 定位搜索框
        log.info(f"🔍 [真搜索] 定位搜索框")
        search_selector = selectors["search_box"]
        try:
            search_box = page.wait_for_selector(search_selector, timeout=15000)
        except Exception as e:
            log.warning(f"[真搜索] 定位搜索框失败: {str(e)[:100]}，直接访问目标页")
            return False, current_x, current_y

        # 5. 移动鼠标到搜索框、点击
        search_box.scroll_into_view_if_needed()
        rect = search_box.bounding_box()
        if rect:
            center_x = rect["x"] + rect["width"]/2 + random.uniform(-8, 8)
            center_y = rect["y"] + rect["height"]/2 + random.uniform(-4, 4)
            page.mouse.move(center_x, center_y)
            time.sleep(random.uniform(0.3, 0.8))
            page.mouse.click(center_x, center_y)
            log.info(f"🔍 [真搜索] 已点击搜索框")
        else:
            search_box.click()

        time.sleep(random.uniform(0.5, 1.2))

        # 6. 清空搜索框（如果有默认值）
        page.keyboard.down("Control")
        page.keyboard.press("a")
        page.keyboard.up("Control")
        time.sleep(random.uniform(0.15, 0.4))
        page.keyboard.press("Backspace")
        time.sleep(random.uniform(0.2, 0.5))

        # 7. 模拟真人分段输入关键词（删改1-2次模拟思考）
        log.info(f"🔍 [真搜索] 模拟真人输入关键词")
        words = selected_keyword.split(" ")
        i = 0
        while i < len(words):
            chunk = " ".join(words[i:i+2])
            # 使用原生按字符
            for c in chunk:
                page.keyboard.type(c)
                time.sleep(random.uniform(0.06, 0.18))
            
            # 10%概率删改1个词模拟思考
            if random.random() < 0.1 and i < len(words)-1:
                log.info(f"🔍 [真搜索] 模拟思考删改")
                time.sleep(random.uniform(0.3, 0.9))
                # 删掉刚才输入的词
                for _ in range(len(chunk)):
                    page.keyboard.press("Backspace")
                    time.sleep(random.uniform(0.04, 0.1))
                continue
            
            i += 2
            time.sleep(random.uniform(0.4, 1.1))

        # 8. 回车执行搜索
        log.info(f"🔍 [真搜索] 执行搜索")
        page.keyboard.press("Enter")
        time.sleep(random.uniform(0.5, 1.5))
        # 等待搜索结果页
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(random.uniform(1.5, 3.5))

        # 9. 新增：在搜索结果页加入真人模拟窗口！
        results_duration = random.uniform(3.0, 6.0)
        log.info(f"🔍 [真搜索] 在搜索结果页停留：{results_duration:.1f}s，真人模型介入")
        current_x, current_y = simulate_human_in_window(page, results_duration, stats, current_x, current_y, config, page_name=f"搜索结果页({selected_engine_id})")

        # 10. 找目标链接（使用引擎专属结果选择器 + 通用兜底）
        log.info(f"🔍 [真搜索] 查找目标链接: {target_url}")
        target_parsed = urlparse(target_url)
        target_host = target_parsed.netloc
        target_link_found = None

        # 构建结果链接选择器列表：引擎专属 + 通用域名匹配
        result_selectors = list(selectors.get("result_links", []))
        result_selectors.append('a[href*="' + target_host + '"]')

        # 遍历搜索结果中的 a 标签
        for selector in result_selectors:
            try:
                links = page.query_selector_all(selector)
                for l in links:
                    href = l.get_attribute("href")
                    if href and target_host in href:
                        target_link_found = l
                        log.info(f"🔍 [真搜索] 找到目标搜索结果链接: {href}")
                        break
                if target_link_found:
                    break
            except Exception:
                continue
        
        if target_link_found:
            # 滚动到可见
            target_link_found.scroll_into_view_if_needed()
            time.sleep(random.uniform(0.6, 1.2))
            # 贝塞尔曲线移动鼠标到链接、点击
            rect2 = target_link_found.bounding_box()
            if rect2:
                cx = rect2["x"] + rect2["width"]/2 + random.uniform(-10, 10)
                cy = rect2["y"] + rect2["height"]/2 + random.uniform(-5, 5)
                page.mouse.move(cx, cy)
                time.sleep(random.uniform(0.4, 0.9))
                page.mouse.click(cx, cy)
                current_x, current_y = cx, cy
                log.info(f"🔍 [真搜索] 已点击目标搜索结果链接")
            else:
                target_link_found.click()
            
            # 11. 等待跳转到目标页
            page.wait_for_load_state("networkidle", timeout=90000)
            time.sleep(random.uniform(1.2, 2.5))
            current_url = page.url
            log.info(f"🔍 [真搜索] 当前URL: {current_url[:100]}")
            if target_host in current_url:
                log.info(f"✅ [真搜索] 成功跳转到目标页！")
                return True, current_x, current_y
            else:
                log.warning(f"[真搜索] 跳转后URL不匹配，直接导航目标页")
                return False, current_x, current_y
        else:
            log.warning(f"[真搜索] 搜索结果页没找到目标链接，直接访问目标页")
            return False, current_x, current_y

    except Exception as e:
        log.warning(f"[真搜索] 流程异常: {str(e)[:180]}，直接访问目标页")
        return False, current_x, current_y


stats = {
    "total": 0, 
    "success": 0, 
    "fail": 0,
    "video_item_success": 0,
    "video_item_fail": 0,
    "country_video_views": {}  # key: country_code, value: count
}

adsl_status = {
    "running": False,
    "status": "停止",
    "total": 0,
    "completed": 0,
    "current": 0,
    "current_ip": "",
    "country": "",
    "last_redial_time": "",
    "last_error": ""
}
_adsl_last_redial_ts = 0
_adsl_redial_timestamps = []  # 最近重拨时间序列，用于切换频率自我监测
ADSL_IP_HISTORY_FILE = "adsl_ip_history.json"
_adsl_ip_history_lock = threading.Lock()

# 记录当前计划的总任务数（用于显示）
planned_total_tasks = 0

# 视频任务全局变量
human_model_state = {
    "running": False,
    "task_type": "",
    "last_heartbeat": 0,
    "last_source": "",
    "last_error": ""
}
human_model_lock = threading.Lock()
human_model_stop_event = threading.Event()
human_model_thread = None
HUMAN_MODEL_HEARTBEAT_TIMEOUT = 120

# HTML 模板
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>专业级广告联盟流量模拟系统</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, Ubuntu, sans-serif; background: #1a1a1a; color: white; padding: 20px; }
        .container { max-width: 95%; margin: 0 auto; }
        
        /* 顶部栏 */
        .top-bar { 
            display: flex; 
            align-items: center; 
            justify-content: space-between;
            gap: 20px; 
            margin-bottom: 15px; 
        }
        
        /* 蓝框 - 系统名称 */
        .system-name { 
            background: #007bff; 
            padding: 15px 30px; 
            border-radius: 8px; 
            font-size: 24px; 
            font-weight: bold; 
            white-space: nowrap;
        }
        
        /* 按钮区域 */
        .button-panel { 
            display: flex; 
            align-items: center; 
            gap: 10px; 
            flex-wrap: wrap; 
            flex: 1;
            justify-content: center;
        }
        .btn { 
            padding: 10px 20px; 
            border: none; 
            border-radius: 4px; 
            cursor: pointer; 
            font-weight: bold; 
            transition: all 0.3s; 
            font-size: 14px; 
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.3); }
        .btn-blue { background: #007bff; color: white; }
        .btn-green { background: #28a745; color: white; }
        .btn-red { background: #dc3545; color: white; }
        .btn-yellow { background: #ffc107; color: black; }
        
        /* 黑框 - 状态信息区 */
        .status-panel { 
            background: #2d2d2d; 
            padding: 15px 20px; 
            border-radius: 8px; 
            display: flex; 
            align-items: center; 
            gap: 30px;
            white-space: nowrap;
        }
        .status-item { 
            display: flex; 
            align-items: center; 
            gap: 10px;
        }
        .status-label { 
            color: #ccc; 
            font-size: 14px;
        }
        .status-value { 
            font-size: 18px; 
            font-weight: bold;
        }
        .status { 
            padding: 6px 15px; 
            border-radius: 4px; 
            font-weight: bold; 
            font-size: 14px; 
        }
        .running { background: #28a745; }
        .stopped { background: #dc3545; }
        
        /* 统计值显示 */
        .stat-display { 
            display: flex; 
            align-items: center; 
            gap: 5px;
        }
        .stat-number { 
            font-size: 20px; 
            font-weight: bold; 
            color: #007bff;
        }
        
        /* 主要内容区 - 红框配置区和黄框日志区 */
        .main-content { 
            display: flex; 
            gap: 15px;
            align-items: stretch;
            width: 100%;
        }
        
        /* 红框 - 配置区域（左侧，约2/3宽度） */
        .config-panel { 
            flex: 0 0 58%; 
            min-width: 0;
            background: #dc3545; 
            padding: 5px; 
            border-radius: 8px; 
            display: flex;
        }
        .config-inner { 
            background: #2d2d2d; 
            padding: 15px; 
            border-radius: 4px; 
            flex: 1;
            min-width: 0;
            overflow: hidden;
        }
        .tab-buttons { display: flex; gap: 5px; margin-bottom: 15px; }
        .tab-btn { 
            padding: 8px 20px; 
            border: none; 
            border-radius: 4px 4px 0 0; 
            cursor: pointer; 
            background: #444; 
            color: #ccc; 
            font-size: 13px;
        }
        .tab-btn.active { background: #007bff; color: white; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        /* 表单样式 */
        .form-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; }
        .form-group { margin-bottom: 12px; }
        label { 
            display: block; 
            margin-bottom: 3px; 
            color: #ccc; 
            font-size: 12px;
        }
        .tab-content label { 
            color: #e5e7eb; 
            font-size: 12px;
            font-weight: normal;
        }
        .tab-content .seo-panel label,
        .tab-content .config-panel label {
            color: #e5e7eb; 
            font-size: 12px;
            font-weight: normal;
        }
        input { 
            width: 100%; 
            padding: 8px; 
            border: 1px solid #444; 
            border-radius: 4px; 
            background: #333; 
            color: white; 
            font-size: 12px; 
            box-sizing: border-box;
            text-align: center;
            font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, Ubuntu, sans-serif;
        }
        textarea { 
            width: 100%; 
            padding: 8px; 
            border: 1px solid #444; 
            border-radius: 4px; 
            background: #333; 
            color: white; 
            font-size: 12px; 
            box-sizing: border-box;
            text-align: center;
            font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, Ubuntu, sans-serif;
        }
        select {
            width: 100%;
            padding: 8px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #333;
            color: white;
            font-size: 12px;
            box-sizing: border-box;
            font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, Ubuntu, sans-serif;
        }
        .input-group { display: flex; gap: 5px; }
        .input-group input { flex: 1; }
        /* 所有Tab统一字体（input/textarea/select 12px；label 12px；h4 14px） */
        .tab-content input,
        .tab-content textarea,
        .tab-content select {
            font-size: 12px !important;
        }
        .tab-content h4 {
            font-size: 14px !important;
        }
        /* 顶部运行模式：单选框与文字垂直居中对齐 */
        .button-panel label {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            margin: 0;
            font-size: 13px;
            color: #fff;
            cursor: pointer;
        }
        .button-panel input[type="radio"] {
            width: auto;
            margin: 0;
            padding: 0;
        }
        #planPreviewPanel {
            display: none !important;
        }
        
        /* 黄框 - 日志区域（右侧，约1.5倍原宽度，与配置区等高） */
        .log-panel { 
            flex: 0 0 41%; 
            min-width: 0;
            background: #ffc107; 
            padding: 5px; 
            border-radius: 8px; 
            display: flex;
            flex-direction: column;
        }
        .log-inner { 
            background: #2d2d2d; 
            padding: 15px; 
            border-radius: 4px; 
            flex: 1;
            display: flex;
            flex-direction: column;
            min-width: 0;
        }
        .log-header { 
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px; 
        }
        .log-title { 
            color: #ffc107; 
            font-size: 16px; 
            font-weight: bold; 
        }
        .log-select { 
            background: #4a4a4a;
            color: white;
            border: 1px solid #666;
            border-radius: 4px;
            padding: 4px 8px;
            font-size: 12px;
        }
        .log-box { 
            flex: 1; 
            min-height: 0;
            overflow-y: auto; 
            background: #1a1a1a; 
            padding: 10px; 
            border-radius: 4px; 
            font-family: "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, Ubuntu, sans-serif; 
            font-size: 12px; 
            color: #0f0; 
            white-space: pre-wrap; 
        }
        .log-module { color: #ffd700; font-weight: bold; }
        .log-success { color: #00ff00; }
        .log-error { color: #ff0000; }
        .log-info { color: #87ceeb; }
        .log-task-separator { color: #ff3333; font-style: italic; font-weight: 900; font-size: 18px; display: block; text-align: center; }
        .log-web-round { color: #ffd700; font-style: italic; font-weight: 700; display: block; }
        .log-video-round { color: #1e40ff; font-style: italic; font-weight: 700; display: block; }
        
        .seo-panel { margin-top: 15px; background: #2a2a2a; padding: 15px; border-radius: 6px; }
        .seo-panel h3 { color: #007bff; margin-bottom: 10px; }
        .seo-item { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: #333; border-radius: 4px; margin-bottom: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <!-- 顶部栏 -->
        <div class="top-bar">
            <!-- 蓝框 - 系统名称 -->
            <div class="system-name">Selenium流量系统</div>
            
            <!-- 运行模式区域 -->
            <div class="button-panel">
                <div style="display:inline-flex; align-items:center; gap:8px; color:#fff; margin-left:14px; padding:6px 10px; background:rgba(255,255,255,0.12); border:1px solid #ffd54f; border-radius:6px; user-select:none; white-space:nowrap;">
                    <span style="font-size:13px; font-weight:bold; color:#ffd54f;">浏览器</span>
                    <label><input type="radio" name="headless_mode" value="false" {{ 'checked' if not config.headless else '' }} onchange="saveRuntimeMode()"> 有头</label>
                    <label><input type="radio" name="headless_mode" value="true" {{ 'checked' if config.headless else '' }} onchange="saveRuntimeMode()"> 无头</label>
                </div>
                <div style="display:inline-flex; align-items:center; gap:8px; color:#fff; margin-left:8px; padding:6px 10px; background:rgba(255,255,255,0.12); border:1px solid #00d4aa; border-radius:6px; user-select:none; white-space:nowrap;">
                    <span style="font-size:13px; font-weight:bold; color:#00d4aa;">日志模式</span>
                    <label><input type="radio" name="log_mode" value="test" {{ 'checked' if config.get('log_mode', 'test') == 'test' else '' }} onchange="saveRuntimeMode()"> 测试</label>
                    <label><input type="radio" name="log_mode" value="prod" {{ 'checked' if config.get('log_mode', 'test') == 'prod' else '' }} onchange="saveRuntimeMode()"> 生产</label>
                </div>
            </div>
            
            <!-- 黑框 - 状态信息区 -->
            <div class="status-panel">
                <div class="status-item">
                    <span class="status-label">网站任务:</span>
                    <span id="websiteTopStatus" class="status {{ 'running' if runningtask else 'stopped' }}">
                        {{ '运行中' if runningtask else '已停止' }}
                    </span>
                </div>

                <div class="status-item">
                    <span class="status-label">总任务:</span>
                    <span class="stat-number">{{ planned_total if planned_total > 0 else 0 }}</span>
                </div>
                <div class="status-item">
                    <span class="status-label">成功:</span>
                    <span class="stat-number" style="color: #28a745;">{{ statssuccess }}</span>
                </div>
                <div class="status-item">
                    <span class="status-label">失败:</span>
                    <span class="stat-number" style="color: #dc3545;">{{ statsfail }}</span>
                </div>
            </div>
        </div>
        
        <!-- 主要内容区 -->
        <div class="main-content">
            <!-- 红框 - 配置区域（2/3宽度） -->
            <div class="config-panel">
                <div class="config-inner">
            <div class="tab-buttons">
                <button class="tab-btn active" onclick="switchTab('websitetraffic', this)">网站流量</button>
                <button class="tab-btn" onclick="switchTab('network', this)">网络</button>
                <button class="tab-btn" onclick="switchTab('seo', this)">SEO</button>
                <button class="tab-btn" onclick="switchTab('model', this)">模型</button>
                <button class="tab-btn" onclick="switchTab('taskvalidation', this)">任务验证</button>
            </div>
            
            <!-- QA任务Tab -->
            
            
            <!-- 网络Tab -->
            <div class="tab-content" id="tab-network">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn btn-blue" onclick="console.log('saveNetworkConfig called'); saveNetworkConfig();">保存配置</button>
                    <button class="btn btn-yellow" onclick="resetNetworkConfig()">恢复默认</button>
                </div>
                <!-- 流量模型配置 -->
                <div style="margin-bottom: 15px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 15px;">
                        <div style="flex: 1;">
                            <h4 style="margin-top: 0; margin-bottom: 10px; color: #4a9eff;">流量模型选择（可多选，运行时随机用一个）</h4>
                            <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="checkbox" class="model-check" data-model="normal" {{ 'checked' if 'normal' in config.selected_models else '' }}>
                                    正态分布（平稳）
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="checkbox" class="model-check" data-model="gamma" {{ 'checked' if 'gamma' in config.selected_models else '' }}>
                                    伽马分布（活动突增）
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="checkbox" class="model-check" data-model="bimodal" {{ 'checked' if 'bimodal' in config.selected_models else '' }}>
                                    双峰分布（早晚高峰）
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="checkbox" class="model-check" data-model="poisson" {{ 'checked' if 'poisson' in config.selected_models else '' }}>
                                    泊松分布（秒级脉冲）
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="checkbox" class="model-check" data-model="burst" {{ 'checked' if 'burst' in config.selected_models else '' }}>
                                    突发流量（热点事件）
                                </label>
                            </div>
                        </div>
                    </div>
                    
                    <h4 style="margin-top: 12px; margin-bottom: 8px; color: #4a9eff;">日流量区间配置</h4>
                    <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                        <div class="form-group" style="flex: 1; min-width: 180px;">
                            <label>新站日流量（<=30天）</label>
                            <div class="input-group">
                                <input type="number" id="dt_new_min" value="{{ config.daily_traffic_range.new.min }}" placeholder="最小">
                                <input type="number" id="dt_new_max" value="{{ config.daily_traffic_range.new.max }}" placeholder="最大">
                            </div>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 180px;">
                            <label>中站日流量（31-60天）</label>
                            <div class="input-group">
                                <input type="number" id="dt_mid_min" value="{{ config.daily_traffic_range.mid.min }}" placeholder="最小">
                                <input type="number" id="dt_mid_max" value="{{ config.daily_traffic_range.mid.max }}" placeholder="最大">
                            </div>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 180px;">
                            <label>老站日流量（>60天）</label>
                            <div class="input-group">
                                <input type="number" id="dt_old_min" value="{{ config.daily_traffic_range.old.min }}" placeholder="最小">
                                <input type="number" id="dt_old_max" value="{{ config.daily_traffic_range.old.max }}" placeholder="最大">
                            </div>
                        </div>
                    </div>
                </div>
                
                <div id="proxy-pool-container" style="display:flex; flex-direction:column; gap:10px; margin-bottom:20px;">
                    {% for idx in range([config.proxy_pool|length, 5]|min) %}
                    {% set p = config.proxy_pool[idx] %}
                    <div class="proxy-item" data-idx="{{ idx }}" style="display:flex; gap:8px; align-items:center; padding:8px; background:#2a2a2a; border-radius:8px;">
                        <div style="width:80px;">
                            <label style="display:flex; align-items:center; gap:5px;">
                                <input type="checkbox" class="proxy-enabled" {{ 'checked' if p.enabled else '' }}>
                                启用
                            </label>
                        </div>
                        <div style="width:80px; font-weight:bold; font-size:16px;">
                            <input type="text" class="proxy-country" value="{{ p.country_code }}" maxlength="8" style="width:100%; font-weight:bold; font-size:14px; text-transform:uppercase;">
                        </div>
                        <div style="flex:1;">
                            <input type="text" class="proxy-api-url" value="{{ p.proxy_api_url }}" style="width:100%; font-size:12px; color:#aaa;">
                        </div>
                        <div style="width:80px;">
                            <input type="text" class="proxy-user" value="{{ p.proxy_user }}" style="width:100%; font-size:12px;">
                        </div>
                        <div style="width:80px;">
                            <input type="password" class="proxy-pwd" value="{{ p.proxy_pwd }}" style="width:100%; font-size:12px;">
                        </div>
                        <button class="btn btn-red" onclick="removeProxy(this)" style="padding:4px 8px; font-size:12px;">删除</button>
                    </div>
                    {% endfor %}
                </div>
                <button class="btn btn-green" onclick="addProxy()" style="margin-bottom:20px;">+ 添加代理</button>

                <hr style="border-color:#444; margin:20px 0;">

                <h4 style="margin-top:0; color:#4a9eff;">IPDeep 代理配置</h4>
                <div class="form-grid">
                    <div>
                        <div class="form-group">
                            <label for="ip_proxy_api">IPDeep API URL</label>
                            <input type="text" id="ip_proxy_api" value="{{ config.ip_proxy_api }}">
                        </div>
                        <div class="form-group">
                            <label for="ip_proxy_user">IPDeep User</label>
                            <input type="text" id="ip_proxy_user" value="{{ config.ip_proxy_user }}">
                        </div>
                        <div class="form-group">
                            <label for="ip_proxy_pwd">IPDeep Pwd</label>
                            <input type="password" id="ip_proxy_pwd" value="{{ config.ip_proxy_pwd }}">
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- 模型Tab -->
            <div class="tab-content" id="tab-model">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; gap: 10px;">
                    <div class="form-group" style="margin:0; min-width:300px;">
                        <label>QA真人模型强度</label>
                        <div style="display:flex; gap:12px; align-items:center;">
                            <label><input type="radio" name="qa_human_profile" value="light" {{ 'checked' if config.get('qa_human_profile', 'standard') == 'light' else '' }}> 轻度</label>
                            <label><input type="radio" name="qa_human_profile" value="standard" {{ 'checked' if config.get('qa_human_profile', 'standard') == 'standard' else '' }}> 标准</label>
                            <label><input type="radio" name="qa_human_profile" value="heavy" {{ 'checked' if config.get('qa_human_profile', 'standard') == 'heavy' else '' }}> 中度</label>
                        </div>
                    </div>
                    <div style="display:flex; gap:10px;">
                        <button class="btn btn-blue" onclick="saveModelConfig()">保存配置</button>
                        <button class="btn btn-yellow" onclick="resetModelConfig()">恢复默认</button>
                    </div>
                </div>
                <div class="form-grid">
                    <div>
                        <div class="form-group">
                            <label>在广告区域停留时间（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="ad_stay_time_min" value="{{ config.ad_stay_time.min }}">
                                <input type="number" step="0.1" id="ad_stay_time_max" value="{{ config.ad_stay_time.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>页面加载后先等待，模拟真人反应（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="page_load_wait_min" value="{{ config.page_load_wait.min }}">
                                <input type="number" step="0.1" id="page_load_wait_max" value="{{ config.page_load_wait.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>每次滚动的像素数</label>
                            <div class="input-group">
                                <input type="number" id="scroll_pixels_min" value="{{ config.scroll_pixels.min }}">
                                <input type="number" id="scroll_pixels_max" value="{{ config.scroll_pixels.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>每次滚动后的等待（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="scroll_wait_min" value="{{ config.scroll_wait.min }}">
                                <input type="number" step="0.1" id="scroll_wait_max" value="{{ config.scroll_wait.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>模拟真人点击广告的概率（0.005-0.05）</label>
                            <div class="input-group">
                                <input type="number" step="0.001" id="ad_click_prob_min" value="{{ config.ad_click_prob.min }}">
                                <input type="number" step="0.001" id="ad_click_prob_max" value="{{ config.ad_click_prob.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>点击广告后的停留时间（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="ad_click_wait_min" value="{{ config.ad_click_wait.min }}">
                                <input type="number" step="0.1" id="ad_click_wait_max" value="{{ config.ad_click_wait.max }}">
                            </div>
                        </div>
                    </div>
                    <div>
                        <div class="form-group">
                            <label>模拟真人随机点击页面其他位置（次数）</label>
                            <div class="input-group">
                                <input type="number" id="random_click_count_min" value="{{ config.random_click_count.min }}">
                                <input type="number" id="random_click_count_max" value="{{ config.random_click_count.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>随机点击后的等待时间（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="random_click_wait_min" value="{{ config.random_click_wait.min }}">
                                <input type="number" step="0.1" id="random_click_wait_max" value="{{ config.random_click_wait.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>模拟真人在页面上移动鼠标（次数）</label>
                            <div class="input-group">
                                <input type="number" id="mouse_move_count_min" value="{{ config.mouse_move_count.min }}">
                                <input type="number" id="mouse_move_count_max" value="{{ config.mouse_move_count.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>模拟真人平滑移动鼠标（步数）</label>
                            <div class="input-group">
                                <input type="number" id="mouse_move_steps_min" value="{{ config.mouse_move_steps.min }}">
                                <input type="number" id="mouse_move_steps_max" value="{{ config.mouse_move_steps.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>每次移动鼠标后的等待（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.1" id="mouse_move_wait_min" value="{{ config.mouse_move_wait.min }}">
                                <input type="number" step="0.1" id="mouse_move_wait_max" value="{{ config.mouse_move_wait.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>模拟真人滚动浏览页面（次数）</label>
                            <div class="input-group">
                                <input type="number" id="scroll_count_min" value="{{ config.scroll_count.min }}">
                                <input type="number" id="scroll_count_max" value="{{ config.scroll_count.max }}">
                            </div>
                        </div>
                    </div>
                    <div>
                        <div class="form-group">
                            <label>移动的步数（步）</label>
                            <div class="input-group">
                                <input type="number" id="mouse_move_steps_min" value="{{ config.mouse_move_steps.min }}">
                                <input type="number" id="mouse_move_steps_max" value="{{ config.mouse_move_steps.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>移动随机停顿（0.05-0.2）</label>
                            <div class="input-group">
                                <input type="number" step="0.01" id="bezier_pause_prob_min" value="{{ config.bezier_pause_prob.min }}">
                                <input type="number" step="0.01" id="bezier_pause_prob_max" value="{{ config.bezier_pause_prob.max }}">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>停顿时间（秒）</label>
                            <div class="input-group">
                                <input type="number" step="0.01" id="mouse_move_pause_min" value="{{ config.mouse_move_pause.min }}">
                                <input type="number" step="0.01" id="mouse_move_pause_max" value="{{ config.mouse_move_pause.max }}">
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- 网站流量配置Tab -->
            <div class="tab-content active" id="tab-websitetraffic">
                <div class="seo-panel">
                    <div style="padding: 8px 12px; margin-bottom: 12px; background:#16213e; border:1px solid #00aaff; border-radius:6px; color:#dbeafe; font-size:13px;">
                        <div>网站任务状态：<b id="websiteConfigStatus" style="color:#ffd54f;">{{ '运行中' if runningtask else '已停止' }}</b></div>
                    </div>
                    <!-- 按钮区域 -->
                    <div style="display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap;">
                        <button class="btn" onclick="saveWebsiteTrafficConfig()" style="background: #3b82f6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">💾 保存配置</button>
                        <button class="btn" onclick="resetWebsiteTrafficConfig()" style="background: #ffc107; color: #1a1a1a; padding: 5.4px 14.4px; font-size: 12.6px;">🔄 恢复默认</button>
                        <button class="btn" id="btn-generate-plan" onclick="generatePlan()" style="background: #10b981; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📋 生成计划</button>
                        <button class="btn" id="btn-single-task" onclick="startSingleTask()" style="background: #06b6d4; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">⚡ 单独任务</button>
                        <button class="btn" id="btn-execute-plan" onclick="executePlan()" style="background: #8b5cf6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">▶️ 执行计划</button>
                        <button class="btn" id="btn-clear-plan" onclick="clearPlan()" style="background: #f97316; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">🗑️ 清除计划</button>
                        <button class="btn" onclick="stopTask()" style="background: #ef4444; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">⏹️ 停止任务</button>
                    </div>
                    <!-- 基础配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin: 0 0 8px 0; color: #4a9eff;">目标网站池（固定3个，勾选的串联浏览）</h4>
                        <div style="display: flex; flex-direction: column; gap: 6px;">
                            {% for i in range(1, 4) %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <label style="width: 50px; color: #bbb; margin: 0;">URL{{ i }}</label>
                                <input type="checkbox" id="target_url_{{ i }}_enabled" style="width: auto; margin: 0;" {{ 'checked' if (config.target_urls[i-1].get('enabled', False) if config.get('target_urls') and config.target_urls[i-1] else (True if i == 1 else False)) else '' }}>
                                <input type="text" id="target_url_{{ i }}" value="{{ config.target_urls[i-1].get('url', '') if config.get('target_urls') and config.target_urls[i-1] else (config.get('target_url', '') if i == 1 else '') }}" style="flex: 1;" placeholder="https://example.com">
                            </div>
                            {% endfor %}
                        </div>
                    </div>

                    <div style="display: flex; flex-direction: row; gap: 8px; margin-bottom: 8px; align-items: flex-start;">
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label for="site_creation_date">建站日期</label>
                            <input type="date" id="site_creation_date" value="{{ config.site_creation_date }}" style="width: 100%;">
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label for="plan_days">任务计划天数（天）</label>
                            <input type="number" id="plan_days" min="1" max="7" value="{{ config.plan_days|default(1) }}" style="width: 100%;">
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label>任务间隔（秒）</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" step="0.1" id="task_interval_min" value="{{ config.task_interval.min }}" placeholder="最小">
                                <input type="number" step="0.1" id="task_interval_max" value="{{ config.task_interval.max }}" placeholder="最大">
                            </div>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label>浏览网页时长（秒）</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" step="0.1" id="total_stay_min" value="{{ config.total_stay.min }}" placeholder="最小">
                                <input type="number" step="0.1" id="total_stay_max" value="{{ config.total_stay.max }}" placeholder="最大">
                            </div>
                        </div>
                    </div>
                    <div style="display: flex; flex-direction: row; gap: 8px; margin-bottom: 8px; align-items: flex-start;">
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label><input type="checkbox" id="webrtc_leak_check_enabled" {{ 'checked' if config.get('webrtc_leak_check_enabled', True) else '' }}> 启用WebRTC防泄漏检测</label>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label>全局Session策略</label>
                            <select id="session_mode" style="width:100%;">
                                <option value="country_host_7d" {{ 'selected' if config.get('session_mode', 'country_host_7d') == 'country_host_7d' else '' }}>国家+Host复用7天</option>
                                <option value="new_each_task" {{ 'selected' if config.get('session_mode', 'country_host_7d') == 'new_each_task' else '' }}>每任务新会话</option>
                            </select>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label>循环次数</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" id="webnav_loop_count_min" value="{{ config.web_navigation.loop_count.min }}" placeholder="最小">
                                <input type="number" id="webnav_loop_count_max" value="{{ config.web_navigation.loop_count.max }}" placeholder="最大">
                            </div>
                        </div>
                        <div class="form-group" style="flex: 1; min-width: 0;">
                            <label>每轮浏览的间隔时长(秒)</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" step="0.1" id="webnav_loop_interval_min" value="{{ config.web_navigation.loop_interval.min }}" placeholder="最小">
                                <input type="number" step="0.1" id="webnav_loop_interval_max" value="{{ config.web_navigation.loop_interval.max }}" placeholder="最大">
                            </div>
                        </div>
                    </div>

                    <!-- 第1层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #9b59b6;">第1层（首页 → 第2层）</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer1_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_1.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer1_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_1.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例</label>
                                <input type="text" id="webnav_layer1_stay_ratio" value="{{ (config.web_navigation.layer_1.stay_ratio * 100)|int }}%" placeholder="10%" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第2层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #8e44ad;">第2层 → 第3层</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer2_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_2.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer2_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_2.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例</label>
                                <input type="text" id="webnav_layer2_stay_ratio" value="{{ (config.web_navigation.layer_2.stay_ratio * 100)|int }}%" placeholder="10%" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第3层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #e74c3c;">第3层 → 第4层</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer3_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_3.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer3_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_3.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例</label>
                                <input type="text" id="webnav_layer3_stay_ratio" value="{{ (config.web_navigation.layer_3.stay_ratio * 100)|int }}%" placeholder="10%" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第4层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #c0392b;">第4层 → 第5层（可选）</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer4_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_4.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer4_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_4.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例</label>
                                <input type="text" id="webnav_layer4_stay_ratio" value="{{ (config.web_navigation.layer_4.stay_ratio * 100)|int }}%" placeholder="10%" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第5层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #e67e22;">第5层</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer5_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_5.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer5_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_5.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例</label>
                                <input type="text" id="webnav_layer5_stay_ratio" value="{{ (config.web_navigation.layer_5.stay_ratio * 100)|int }}%" placeholder="10%" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                </div>
            </div>
            
            
            <!-- SEO配置Tab -->
            <div class="tab-content" id="tab-seo">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn btn-blue" onclick="saveSeoConfig()">保存配置</button>
                    <button class="btn btn-yellow" onclick="resetSeoConfig()">恢复默认</button>
                </div>
                <div class="seo-panel">

                    <!-- 第一组：搜索引擎 & 社媒平台管理 -->
                    <div style="margin-bottom: 20px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">1. 搜索引擎 & 社媒平台（Referer来源）</h4>
                        <p style="color:#94a3b8;font-size:12px;margin:0 0 12px 0;">任务执行时根据IP国别语言自动匹配对应平台作为Referer进入目标网站（严禁直跳）</p>
                        <div id="engines-container">
                            {% for engine in config.seo.search_engines %}
                            <div class="engine-item" style="display: flex; gap: 8px; margin-bottom: 8px; align-items: center; padding: 6px 8px; background: {{ '#1a2332' if engine.get('type') == 'social' else '#1e1e1e' }}; border-radius: 6px; border-left: 3px solid {{ '#8b5cf6' if engine.get('type') == 'social' else '#3b82f6' }};">
                                <span style="width:60px; font-size:11px; color:{{ '#a78bfa' if engine.get('type') == 'social' else '#60a5fa' }}; font-weight:bold;">{{ '📱社媒' if engine.get('type') == 'social' else '🔍搜索' }}</span>
                                <input type="text" class="engine-id" placeholder="ID" value="{{ engine.id }}" style="width: 80px; font-size: 12px;">
                                <input type="text" class="engine-name" placeholder="名称" value="{{ engine.name }}" style="width: 100px; font-size: 12px;">
                                <input type="text" class="engine-url" placeholder="Referer URL" value="{{ engine.url }}" style="flex: 1; font-size: 12px;">
                                <select class="engine-lang" style="width: 70px; font-size: 12px;">
                                    <option value="zh" {{ 'selected' if engine.language == 'zh' else '' }}>中文</option>
                                    <option value="en" {{ 'selected' if engine.language == 'en' else '' }}>英文</option>
                                </select>
                                <select class="engine-type" style="width: 70px; font-size: 12px;">
                                    <option value="search" {{ 'selected' if engine.get('type') == 'search' else '' }}>搜索</option>
                                    <option value="social" {{ 'selected' if engine.get('type') == 'social' else '' }}>社媒</option>
                                </select>
                                <button class="btn btn-red" onclick="removeEngine(this)" style="padding: 4px 8px; font-size: 11px;">删除</button>
                            </div>
                            {% endfor %}
                        </div>
                        <button class="btn btn-green" onclick="addEngine()" style="margin-top: 10px;">+ 添加平台</button>
                    </div>

                    <!-- 第二组：国别-平台映射 -->
                    <div style="margin-bottom: 20px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">2. 国别语言 → 平台映射（IP国别自动匹配）</h4>
                        <p style="color:#94a3b8;font-size:12px;margin:0 0 12px 0;">根据IP出口国别自动选择对应的搜索引擎/社媒平台作为Referer</p>
                        <div id="region-map-container" style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                            {% for region, engines in config.seo.region_engine_map.items() %}
                            <div class="region-item" style="padding: 8px; background: #1e1e1e; border-radius: 6px;">
                                <label style="font-weight:bold; color:#fbbf24; font-size:13px;">{{ region }}</label>
                                <input type="text" class="region-engines" data-region="{{ region }}" value="{{ engines | join(', ') }}" style="width:100%; margin-top:4px; font-size:12px;" placeholder="平台ID，逗号分隔">
                            </div>
                            {% endfor %}
                        </div>
                    </div>

                    <!-- 第三组：关键词池 -->
                    <div style="margin-bottom: 20px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">3. 关键词池（按语言分组）</h4>
                        <div style="display: flex; gap: 10px; flex-wrap: nowrap;">
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>中文关键词（逗号分隔）</label>
                                <input type="text" id="seo_keywords_zh" value="{{ config.seo.keyword_pools.zh | join(', ') }}" style="height: 56px; font-size: 14px;">
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>英文关键词（逗号分隔）</label>
                                <input type="text" id="seo_keywords_en" value="{{ config.seo.keyword_pools.en | join(', ') }}" style="height: 56px; font-size: 14px;">
                            </div>
                        </div>
                    </div>

                    <!-- 第四组：Referer模式 -->
                    <div style="margin-bottom: 20px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">4. Referer模式</h4>
                        <div class="form-group">
                            <div style="display: flex; gap: 20px;">
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="radio" id="seo_referer_dynamic" {{ 'checked' if config.seo.referer_mode == 'dynamic' else '' }} name="referer-mode">
                                    动态模式（根据IP国别自动选择平台）
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="radio" id="seo_referer_static" {{ 'checked' if config.seo.referer_mode == 'static' else '' }} name="referer-mode">
                                    静态模式（固定使用第一个平台）
                                </label>
                            </div>
                        </div>
                    </div>

                </div>
            </div>
            
            <!-- 任务验证配置Tab -->
            <div class="tab-content" id="tab-taskvalidation">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn btn-blue" onclick="saveTaskValidationConfig()">保存配置</button>
                    <button class="btn btn-yellow" onclick="resetTaskValidationConfig()">恢复默认</button>
                    <button class="btn" style="background:#dc2626;color:#fff;" id="btnSecurityDrill" onclick="startSecurityDrill()">🛡️ 攻防演练</button>
                    <button class="btn" style="background:linear-gradient(135deg, #f093fb 0%, #f5576c 100%);color:#fff;" id="btnKeywordExplore" onclick="startKeywordExplore()">🔍 关键词探索</button>
                </div>
                <div class="seo-panel">
                    <h4 style="margin-top: 0; color: #4a9eff;">攻防演练（风控漏洞检测）</h4>
                    <p style="color:#94a3b8;font-size:13px;margin-top:0;">基于 risk_check.py，对带反检测注入的浏览器访问已勾选目标站进行风控漏洞探测，生成演练报告（保存于 report/ 目录）。</p>
                    <!-- 演练进度条 -->
                    <div style="margin:16px 0;">
                        <div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:6px;">
                            <span id="drillStage">未开始</span>
                            <span id="drillPercent">0%</span>
                        </div>
                        <div style="width:100%;height:18px;background:#1e293b;border-radius:9px;overflow:hidden;">
                            <div id="drillBar" style="height:100%;width:0%;background:linear-gradient(90deg,#3b82f6,#22c55e);transition:width .4s ease;"></div>
                        </div>
                    </div>
                    <!-- 演练结果摘要 -->
                    <div id="drillResult" style="display:none;background:#0f172a;border-radius:8px;padding:14px;margin-top:12px;font-size:13px;line-height:1.7;"></div>
                </div>
                
                <!-- 关键词探索面板 -->
                <div class="seo-panel" style="margin-top:20px;">
                    <h4 style="margin-top: 0; color: #f59e0b;"> 关键词探索（网站内容挖掘）</h4>
                    <p style="color:#94a3b8;font-size:13px;margin-top:0;">自动爬取目标网站多层页面，提取关键词、标题、描述等SEO要素，生成兜底链接池。</p>
                    
                    <!-- 探索进度条 -->
                    <div id="keywordExploreProgress" style="display:none; margin:16px 0;">
                        <div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:6px;">
                            <span id="keywordStage">准备中...</span>
                            <span id="keywordPercent">0%</span>
                        </div>
                        <div style="width:100%;height:18px;background:#1e293b;border-radius:9px;overflow:hidden;">
                            <div id="keywordBar" style="height:100%;width:0%;background:linear-gradient(90deg,#f093fb,#f5576c);transition:width .4s ease;"></div>
                        </div>
                    </div>
                    
                    <!-- 探索结果摘要 -->
                    <div id="keywordResult" style="display:none;background:#0f172a;border-radius:8px;padding:14px;margin-top:12px;font-size:13px;line-height:1.7;"></div>
                </div>

                <!-- 生产准入五层测试面板 -->
                <div class="seo-panel" style="margin-top:20px; border:2px solid #22c55e;">
                    <h4 style="margin-top:0; color:#22c55e;">🏭 生产准入五层测试</h4>
                    <p style="color:#94a3b8;font-size:13px;margin-top:0;">任一条不达标 = 禁止上线。对应“代码检查／环境伪装／对抗验证／真人行为验证／工程可靠性”五层，汇总 8 项准入检查单。</p>
                    <!-- 五层按钮 -->
                    <div style="display:flex; flex-wrap:wrap; gap:8px; margin:12px 0;">
                        <button class="btn" style="background:#3b82f6;color:#fff;" onclick="startProductionTest('code')">📝 代码检查</button>
                        <button class="btn" style="background:#8b5cf6;color:#fff;" onclick="startProductionTest('env')">🕵️ 环境伪装</button>
                        <button class="btn" style="background:#ef4444;color:#fff;" onclick="startProductionTest('adversarial')">⚔️ 对抗验证</button>
                        <button class="btn" style="background:#f59e0b;color:#fff;" onclick="startProductionTest('behavior')">🧍 真人行为验证</button>
                        <button class="btn" style="background:#06b6d4;color:#fff;" onclick="startProductionTest('reliability')">🔧 工程可靠性</button>
                        <button class="btn" style="background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;" onclick="startProductionTest('all')">🚀 全量准入测试</button>
                    </div>
                    <!-- 进度条 -->
                    <div id="prodTestProgress" style="display:none; margin:16px 0;">
                        <div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:6px;">
                            <span id="prodTestStage">准备中...</span>
                            <span id="prodTestPercent">0%</span>
                        </div>
                        <div style="width:100%;height:18px;background:#1e293b;border-radius:9px;overflow:hidden;">
                            <div id="prodTestBar" style="height:100%;width:0%;background:linear-gradient(90deg,#22c55e,#16a34a);transition:width .4s ease;"></div>
                        </div>
                    </div>
                    <!-- 准入检查单报告 -->
                    <div id="prodTestReport" style="display:none;margin-top:12px;"></div>
                </div>
            </div>
            
            <!-- 原网页跳转配置Tab，现在改名为网站流量 -->
            <div class="tab-content" id="tab-webnav">
                <div class="seo-panel">
                    <h4 style="margin-top: 0; color: #4a9eff;">网站流量导航配置</h4>
                    <!-- 这里将放置网站流量导航相关的配置 -->
                    <div class="form-group">
                        <label>网站流量导航配置内容</label>
                        <textarea placeholder="网站流量导航配置内容" style="width: 100%; min-height: 200px;"></textarea>
                    </div>
                    
                    <div style="display: flex; gap: 10px; margin-top: 20px;">
                        <button class="btn btn-blue" onclick="saveWebNavConfig()">保存配置</button>
                        <button class="btn btn-yellow" onclick="resetWebNavConfig()">恢复默认</button>
                    </div>
                </div>
            </div><!-- /#tab-webnav -->
                </div><!-- /.config-inner -->
        </div><!-- /.config-panel -->

        <!-- 黄框 - 日志区域（右侧1/4宽度，作为 main-content 内的兄弟列，与配置区等高） -->
            <div class="log-panel">
                <!-- 任务计划状态面板（蓝色边框） -->
                <div id="planStatusPanel" style="display:none; background:#1a1a2e; border:2px solid #00aaff; border-radius:8px; padding:6px 10px; margin-bottom:8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <h3 style="color:#00aaff; margin:0; font-size:13px;">📊 任务计划状态</h3>
                        <span id="planStatusProgress" style="color:#fff; font-size:11px;"></span>
                    </div>
                    <div id="planStatusInfo" style="font-size:11px; color:#ccc; line-height:1.35;"></div>
                </div>
                
                <!-- 当前执行任务展示区域（任务运行时显示） -->
                <div id="currentTaskPanel" style="display:none; background:#1a1a2e; border:2px solid #ff9900; border-radius:8px; padding:12px; margin-bottom:15px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <h3 style="color:#ff9900; margin:0; font-size:14px;">🚀 当前执行任务</h3>
                        <span id="currentTaskProgress" style="color:#fff; font-size:12px;"></span>
                    </div>
                    <div id="currentTaskInfo" style="font-size:12px; color:#ccc;"></div>
                </div>
                
                <!-- 任务计划预览区域 -->
                <div id="planPreviewPanel" style="display:none; background:#1a1a2e; border:2px solid #00d4aa; border-radius:8px; padding:15px; margin-bottom:15px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <h3 style="color:#00d4aa; margin:0;">📋 计划预览</h3>
                        <span id="planSummary" style="color:#fff; font-size:13px;"></span>
                    </div>
                    
                    <!-- ⭐ 国家分布统计 -->
                    <div style="margin-bottom:8px;">
                        <div style="color:#aaa; font-size:11px; margin-bottom:4px;">🌍 国家分布：</div>
                        <div id="countryDistribStats"></div>
                    </div>
                    
                    <!-- ⭐ 覆盖时段提示 -->
                    <div id="coverageInfo" style="margin-bottom:8px; font-size:12px;"></div>
                    
                    <!-- ⭐ 警告区域 -->
                    <div id="planWarnings" style="display:none; background:#2a1a1a; border:1px solid #ff9966; border-radius:4px; padding:6px 10px; margin-bottom:8px; font-size:12px;"></div>
                    
                    <div id="planTableContainer" style="max-height:600px; overflow-y:auto; background:#0a0a14; border-radius:4px; padding:8px;">
                        <table id="planTable" style="width:100%; color:#fff; font-size:12px; border-collapse:collapse;">
                            <thead style="position:sticky; top:0; background:#1a1a2e;">
                                <tr>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">序号</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">开始时间</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">预估时长</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">结束时间</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">代理国家</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">完成状态</th>
                                </tr>
                            </thead>
                            <tbody id="planTableBody"></tbody>
                        </table>
                    </div>
                    <p style="color:#ffaa00; font-size:12px; margin-top:8px; margin-bottom:0;">
                        ⚠️ 请确认计划无误后，点击"▶️ 执行计划"开始执行
                    </p>
                </div>
                
                <!-- 历史任务查询面板 -->
                <div id="historicalTasksPanel" style="display:none; background:#1a1a2e; border:2px solid #00aaff; border-radius:8px; padding:15px; margin-bottom:15px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <h3 style="color:#00aaff; margin:0;">📊 近三天任务完成记录</h3>
                        <button class="btn btn-yellow" onclick="hideHistoricalTasks()" style="padding:4px 8px; font-size:12px;">关闭</button>
                    </div>
                    <div id="historicalTasksContainer" style="max-height:500px; overflow-y:auto; background:#0a0a14; border-radius:4px; padding:8px;">
                    </div>
                </div>

                <!-- 指纹和UA统计面板 -->
                <div id="fingerprintStatsPanel" style="display:none; background:#1a1a2e; border:2px solid #28a745; border-radius:8px; padding:15px; margin-bottom:15px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <h3 style="color:#28a745; margin:0;">🔍 指纹和UA统计</h3>
                        <button class="btn btn-yellow" onclick="hideFingerprintStats()" style="padding:4px 8px; font-size:12px;">关闭</button>
                    </div>
                    <div id="fingerprintStatsContainer" style="max-height:500px; overflow-y:auto; background:#0a0a14; border-radius:4px; padding:8px;">
                    </div>
                </div>

                <div class="log-inner">
                    <div class="log-header" style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;">
                        <div class="log-title" style="flex-shrink: 0;">实时运行日志</div>
                        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end;">
                            <select class="log-select" id="logFilter">
                                <option value="all">全部日志</option>
                                <option value="info">信息日志</option>
                                <option value="warning">警告日志</option>
                                <option value="error">错误日志</option>
                            </select>
                            <label style="display: flex; align-items: center; gap: 6px; color: #fff; font-size: 13px; cursor: pointer; background: #444; padding: 4px 10px; border-radius: 4px; white-space: nowrap;">
                                <input type="checkbox" id="autoScroll" checked style="cursor: pointer; width: 16px; height: 16px;">
                                自动滚动
                            </label>
                        </div>
                    </div>
                    <div class="log-box" id="logBox">
                        {% for log in logs %}
                        <p>{{ log|safe }}</p>
                        {% endfor %}
                    </div>
                </div>
            </div><!-- /.log-panel -->
        </div><!-- /.main-content -->
    </div><!-- /.container -->

    <script>
        // 保存原始的完整日志HTML
        let originalLogHTML = '';
        
        function switchTab(tabName, btn) {
            // 隐藏所有Tab
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            // 显示选中的Tab
            const targetTab = document.getElementById('tab-' + tabName);
            if (targetTab) {
                targetTab.classList.add('active');
            }
            if (btn) {
                btn.classList.add('active');
            }
        }
        
        // 日志筛选函数
        function filterLogs() {
            const logBox = document.getElementById('logBox');
            const filterSelect = document.getElementById('logFilter');
            if (!logBox || !filterSelect) return;
            
            const filter = filterSelect.value;
            
            // 如果是全部显示，恢复原始内容
            if (filter === 'all') {
                if (originalLogHTML) {
                    logBox.innerHTML = originalLogHTML;
                }
                return;
            }
            
            // 保存原始内容（第一次筛选时）
            if (!originalLogHTML) {
                originalLogHTML = logBox.innerHTML;
            }
            
            // 获取所有日志行
            const logLines = logBox.querySelectorAll('p');
            
            // 筛选显示
            logLines.forEach(line => {
                const text = line.textContent || '';
                const html = line.innerHTML || '';
                let shouldShow = true;
                
                if (filter === 'info') {
                    shouldShow = text.includes('[INFO]') || text.includes('[DEBUG]') || 
                                 text.includes('【信息】') || text.includes('【代理模块】') ||
                                 text.includes('【指纹浏览器模块】') || text.includes('【页面 & 广告模块】') ||
                                 text.includes('【真人行为模块】') || text.includes('【任务结果】');
                } else if (filter === 'warning') {
                    shouldShow = text.includes('[WARNING]') || text.includes('【警告】');
                } else if (filter === 'error') {
                    shouldShow = text.includes('[ERROR]') || text.includes('【错误】') ||
                                 html.includes('log-error');
                }
                
                line.style.display = shouldShow ? 'block' : 'none';
            });
        }
        
        // 直接绑定筛选事件（延迟执行确保DOM已加载）
        setTimeout(function() {
            const filterSelect = document.getElementById('logFilter');
            if (filterSelect) {
                filterSelect.addEventListener('change', filterLogs);
            }
        }, 100);
        
        function parseCommaList(text) {
            if (!text || !text.trim()) {
                return [];
            }
            var parts = text.split(/[\n,]+/);
            var result = [];
            for (var i = 0; i < parts.length; i++) {
                var trimmed = parts[i].trim();
                if (trimmed.length > 0) {
                    result.push(trimmed);
                }
            }
            return result;
        }
        
        function saveConfig() {
            // 收集代理池配置
            const proxyPoolItems = document.querySelectorAll('#proxy-pool-container .proxy-item');
            const proxyPool = [];
            proxyPoolItems.forEach(item => {
                const idx = parseInt(item.getAttribute('data-idx'));
                proxyPool.push({
                    enabled: item.querySelector('.proxy-enabled').checked,
                    country_code: item.querySelector('.proxy-country').value,
                    proxy_api_url: item.querySelector('.proxy-api-url').value,
                    proxy_user: item.querySelector('.proxy-user').value,
                    proxy_pwd: item.querySelector('.proxy-pwd').value
                });
            });

            // 收集流量模型选择
            const selectedModels = [];
            document.querySelectorAll('.model-check:checked').forEach(cb => {
                selectedModels.push(cb.getAttribute('data-model'));
            });

            // 收集日流量区间
            const dtNewMin = parseInt(document.getElementById('dt_new_min').value) || 50;
            const dtNewMax = parseInt(document.getElementById('dt_new_max').value) || 100;
            const dtMidMin = parseInt(document.getElementById('dt_mid_min').value) || 200;
            const dtMidMax = parseInt(document.getElementById('dt_mid_max').value) || 300;
            const dtOldMin = parseInt(document.getElementById('dt_old_min').value) || 500;
            const dtOldMax = parseInt(document.getElementById('dt_old_max').value) || 600;

            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    // 网络Tab
                    ip_proxy_api: document.getElementById('ip_proxy_api').value,
                    ip_proxy_user: document.getElementById('ip_proxy_user').value,
                    ip_proxy_pwd: document.getElementById('ip_proxy_pwd').value,
                    webrtc_leak_check_enabled: document.getElementById('webrtc_leak_check_enabled') ? document.getElementById('webrtc_leak_check_enabled').checked : true,
                    session_mode: document.getElementById('session_mode') ? document.getElementById('session_mode').value : 'country_host_7d',

                    // 代理池
                    proxy_pool: proxyPool,
                    
                    // 运行模式
                    headless: (document.querySelector('input[name="headless_mode"]:checked') || {}).value !== 'false',
                    log_mode: (document.querySelector('input[name="log_mode"]:checked') || {}).value || 'test',
                    
                    // 任务Tab
                    target_urls: (function() {
                        const urls = [];
                        for (let i = 1; i <= 5; i++) {
                            const urlEl = document.getElementById('target_url_' + i);
                            const enabledEl = document.getElementById('target_url_' + i + '_enabled');
                            urls.push({
                                url: urlEl ? urlEl.value.trim() : '',
                                enabled: enabledEl ? enabledEl.checked : (i === 1)
                            });
                        }
                        return urls;
                    })(),
                    site_creation_date: document.getElementById('site_creation_date').value,
                    plan_days: Math.min(7, Math.max(1, parseInt(document.getElementById('plan_days').value) || 1)),
                    selected_models: selectedModels,
                    daily_traffic_range: {
                        new: {min: dtNewMin, max: dtNewMax},
                        mid: {min: dtMidMin, max: dtMidMax},
                        old: {min: dtOldMin, max: dtOldMax}
                    },
                    task_interval: {
                        min: parseFloat(document.getElementById('task_interval_min').value),
                        max: parseFloat(document.getElementById('task_interval_max').value)
                    },
                    
                    // 模型Tab
                    qa_human_profile: (document.querySelector('input[name="qa_human_profile"]:checked') || {}).value || 'standard',
                    ad_stay_time: {
                        min: parseFloat(document.getElementById('ad_stay_time_min').value),
                        max: parseFloat(document.getElementById('ad_stay_time_max').value)
                    },
                    page_load_wait: {
                        min: parseFloat(document.getElementById('page_load_wait_min').value),
                        max: parseFloat(document.getElementById('page_load_wait_max').value)
                    },
                    scroll_pixels: {
                        min: parseInt(document.getElementById('scroll_pixels_min').value),
                        max: parseInt(document.getElementById('scroll_pixels_max').value)
                    },
                    scroll_wait: {
                        min: parseFloat(document.getElementById('scroll_wait_min').value),
                        max: parseFloat(document.getElementById('scroll_wait_max').value)
                    },
                    ad_click_prob: {
                        min: parseFloat(document.getElementById('ad_click_prob_min').value),
                        max: parseFloat(document.getElementById('ad_click_prob_max').value)
                    },
                    ad_click_wait: {
                        min: parseFloat(document.getElementById('ad_click_wait_min').value),
                        max: parseFloat(document.getElementById('ad_click_wait_max').value)
                    },
                    random_click_count: {
                        min: parseInt(document.getElementById('random_click_count_min').value),
                        max: parseInt(document.getElementById('random_click_count_max').value)
                    },
                    random_click_wait: {
                        min: parseFloat(document.getElementById('random_click_wait_min').value),
                        max: parseFloat(document.getElementById('random_click_wait_max').value)
                    },
                    total_stay: {
                        min: parseFloat(document.getElementById('total_stay_min').value),
                        max: parseFloat(document.getElementById('total_stay_max').value)
                    },
                    mouse_move_count: {
                        min: parseInt(document.getElementById('mouse_move_count_min').value),
                        max: parseInt(document.getElementById('mouse_move_count_max').value)
                    },
                    mouse_move_steps: {
                        min: parseInt(document.getElementById('mouse_move_steps_min').value),
                        max: parseInt(document.getElementById('mouse_move_steps_max').value)
                    },
                    mouse_move_wait: {
                        min: parseFloat(document.getElementById('mouse_move_wait_min').value),
                        max: parseFloat(document.getElementById('mouse_move_wait_max').value)
                    },
                    scroll_count: {
                        min: parseInt(document.getElementById('scroll_count_min').value),
                        max: parseInt(document.getElementById('scroll_count_max').value)
                    },
                    bezier_pause_prob: {
                        min: parseFloat(document.getElementById('bezier_pause_prob_min').value),
                        max: parseFloat(document.getElementById('bezier_pause_prob_max').value)
                    },
                    mouse_move_pause: {
                        min: parseFloat(document.getElementById('mouse_move_pause_min').value),
                        max: parseFloat(document.getElementById('mouse_move_pause_max').value)
                    },
                    
                    // 网页跳转配置
                    web_navigation: {
                        loop_count: {
                            min: parseInt(document.getElementById('webnav_loop_count_min').value) || 1,
                            max: parseInt(document.getElementById('webnav_loop_count_max').value) || 3
                        },
                        loop_interval: {
                            min: parseFloat(document.getElementById('webnav_loop_interval_min').value) || 1,
                            max: parseFloat(document.getElementById('webnav_loop_interval_max').value) || 1
                        },
                        layer_1: {
                            keywords: parseCommaList(document.getElementById('webnav_layer1_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer1_fallback_urls').value),
                            stay_ratio: (parseFloat(document.getElementById('webnav_layer1_stay_ratio').value) || 0) / 100,
                            min_stay: 10
                        },
                        layer_2: {
                            keywords: parseCommaList(document.getElementById('webnav_layer2_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer2_fallback_urls').value),
                            stay_ratio: (parseFloat(document.getElementById('webnav_layer2_stay_ratio').value) || 0) / 100,
                            min_stay: 10
                        },
                        layer_3: {
                            keywords: parseCommaList(document.getElementById('webnav_layer3_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer3_fallback_urls').value),
                            stay_ratio: (parseFloat(document.getElementById('webnav_layer3_stay_ratio').value) || 0) / 100,
                            min_stay: 10
                        },
                        layer_4: {
                            keywords: parseCommaList(document.getElementById('webnav_layer4_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer4_fallback_urls').value),
                            stay_ratio: (parseFloat(document.getElementById('webnav_layer4_stay_ratio').value) || 0) / 100,
                            min_stay: 10
                        },
                        layer_5: {
                            keywords: parseCommaList(document.getElementById('webnav_layer5_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer5_fallback_urls').value),
                            stay_ratio: (parseFloat(document.getElementById('webnav_layer5_stay_ratio').value) || 0) / 100,
                            min_stay: 10
                        },
                    }
                })
            }).then(response => response.json())
            .then(result => {
                alert('配置已保存');
                location.reload();
            });
        }

        // 收集所有配置参数
        function collectConfigPayload() {
            const videoAdEl = document.getElementById('video_ad_enabled');

            // 收集代理池配置（使用全局选择器，确保即使不在网络标签页也能收集到）
            const proxyPoolItems = document.querySelectorAll('.proxy-item');
            const proxyPool = [];
            proxyPoolItems.forEach(item => {
                proxyPool.push({
                    enabled: item.querySelector('.proxy-enabled').checked,
                    country_code: item.querySelector('.proxy-country').value,
                    proxy_api_url: item.querySelector('.proxy-api-url').value,
                    proxy_user: item.querySelector('.proxy-user').value,
                    proxy_pwd: item.querySelector('.proxy-pwd').value
                });
            });
            
            // 收集IPDeep代理配置
            const ipProxyApi = document.getElementById('ip_proxy_api')?.value || '';
            const ipProxyUser = document.getElementById('ip_proxy_user')?.value || '';
            const ipProxyPwd = document.getElementById('ip_proxy_pwd')?.value || '';

            // 收集流量模型选择
            const selectedModels = [];
            document.querySelectorAll('.model-check:checked').forEach(cb => {
                selectedModels.push(cb.getAttribute('data-model'));
            });

            // 收集日流量区间
            const dtNewMin = parseInt(document.getElementById('dt_new_min').value) || 50;
            const dtNewMax = parseInt(document.getElementById('dt_new_max').value) || 100;
            const dtMidMin = parseInt(document.getElementById('dt_mid_min').value) || 200;
            const dtMidMax = parseInt(document.getElementById('dt_mid_max').value) || 300;
            const dtOldMin = parseInt(document.getElementById('dt_old_min').value) || 500;
            const dtOldMax = parseInt(document.getElementById('dt_old_max').value) || 600;

            // 收集目标网站池（3个URL）
            const targetUrls = [];
            for (let i = 1; i <= 3; i++) {
                const urlEl = document.getElementById('target_url_' + i);
                const enabledEl = document.getElementById('target_url_' + i + '_enabled');
                targetUrls.push({
                    url: urlEl ? urlEl.value.trim() : '',
                    enabled: enabledEl ? enabledEl.checked : (i === 1)
                });
            }

            const payload = {
                headless: (document.querySelector('input[name="headless_mode"]:checked') || {}).value !== 'false',
                log_mode: (document.querySelector('input[name="log_mode"]:checked') || {}).value || 'test',
                webrtc_leak_check_enabled: document.getElementById('webrtc_leak_check_enabled') ? document.getElementById('webrtc_leak_check_enabled').checked : true,
                session_mode: document.getElementById('session_mode') ? document.getElementById('session_mode').value : 'country_host_7d',
                vt_adsl_task_count: document.getElementById('qa_task_count') ? Math.min(999, Math.max(1, parseInt(document.getElementById('qa_task_count').value) || 1)) : 1,
                target_urls: targetUrls,
                site_creation_date: document.getElementById('site_creation_date').value,
                plan_days: Math.min(7, Math.max(1, parseInt(document.getElementById('plan_days').value) || 1)),
                selected_models: selectedModels,
                daily_traffic_range: {
                    new: {min: dtNewMin, max: dtNewMax},
                    mid: {min: dtMidMin, max: dtMidMax},
                    old: {min: dtOldMin, max: dtOldMax}
                },
                proxy_pool: proxyPool,
                total_stay: {
                    min: parseFloat(document.getElementById('total_stay_min').value) || 120,
                    max: parseFloat(document.getElementById('total_stay_max').value) || 300
                },
                qa_human_profile: (document.querySelector('input[name="qa_human_profile"]:checked') || {}).value || 'standard',
                ip_proxy_api: ipProxyApi,
                ip_proxy_user: ipProxyUser,
                ip_proxy_pwd: ipProxyPwd,

                web_navigation: {
                    loop_count: {
                        min: parseInt(document.getElementById('webnav_loop_count_min').value) || 1,
                        max: parseInt(document.getElementById('webnav_loop_count_max').value) || 3
                    },
                    loop_interval: {
                        min: parseFloat(document.getElementById('webnav_loop_interval_min').value) || 1,
                        max: parseFloat(document.getElementById('webnav_loop_interval_max').value) || 1
                    },
                    layer_1: {
                        keywords: parseCommaList(document.getElementById('webnav_layer1_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer1_fallback_urls').value),
                        stay_ratio: (parseFloat(document.getElementById('webnav_layer1_stay_ratio').value) || 0) / 100,
                        min_stay: 10
                    },
                    layer_2: {
                        keywords: parseCommaList(document.getElementById('webnav_layer2_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer2_fallback_urls').value),
                        stay_ratio: (parseFloat(document.getElementById('webnav_layer2_stay_ratio').value) || 0) / 100,
                        min_stay: 10
                    },
                    layer_3: {
                        keywords: parseCommaList(document.getElementById('webnav_layer3_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer3_fallback_urls').value),
                        stay_ratio: (parseFloat(document.getElementById('webnav_layer3_stay_ratio').value) || 0) / 100,
                        min_stay: 10
                    },
                    layer_4: {
                        keywords: parseCommaList(document.getElementById('webnav_layer4_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer4_fallback_urls').value),
                        stay_ratio: (parseFloat(document.getElementById('webnav_layer4_stay_ratio').value) || 0) / 100,
                        min_stay: 10
                    },
                    layer_5: {
                        keywords: parseCommaList(document.getElementById('webnav_layer5_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer5_fallback_urls').value),
                        stay_ratio: (parseFloat(document.getElementById('webnav_layer5_stay_ratio').value) || 0) / 100,
                        min_stay: 10
                    },
                }
            };
            if (videoAdEl) {
                payload.video_ad_enabled_only = videoAdEl.checked;
            }
            return payload;
        }

        // 渲染计划预览
        function renderPlan(plan) {
            console.log('renderPlan called with:', plan);
            if (!plan || !plan.tasks) {
                console.error('Invalid plan data:', plan);
                document.getElementById('planPreviewPanel').style.display = 'none';
                return;
            }
            document.getElementById('planPreviewPanel').style.display = 'block';
            
            // 模型中文名映射
            const modelNames = {
                'simple': '简单随机',
                'normal': '正态分布(平稳)',
                'gamma': '伽马分布(活动突增)',
                'bimodal': '双峰分布(早晚高峰)',
                'poisson': '泊松分布(秒级脉冲)',
                'burst': '突发流量(热点事件)'
            };
            const siteAgeNames = {
                'new': '新站(≤30天)',
                'mid': '中站(31-60天)',
                'old': '老站(>60天)'
            };
            
            // 计算总时长（最后一个任务的结束时间 - 第一个任务的开始时间）
            let totalSec = 0;
            if (plan.tasks.length > 0) {
                const lastTask = plan.tasks[plan.tasks.length - 1];
                const firstTask = plan.tasks[0];
                totalSec = (lastTask.actual_end || 0) - (firstTask.actual_start || 0);
            }
            const hours = Math.floor(totalSec / 3600);
            const mins = Math.floor((totalSec % 3600) / 60);
            
            // 作废任务数提示（按原因分类）
            let discardedTip = '';
            if (plan.discarded_tasks && plan.discarded_tasks > 0) {
                const reasons = plan.discard_reasons || {};
                const details = [];
                if (reasons.past_time) details.push(`过去${reasons.past_time}`);
                if (reasons.out_of_coverage) details.push(`非覆盖${reasons.out_of_coverage}`);
                if (reasons.soft_boundary) details.push(`凌晨${reasons.soft_boundary}`);
                if (reasons.out_of_window) details.push(`超窗口${reasons.out_of_window}`);
                const detailStr = details.length > 0 ? `（${details.join('/')}）` : '';
                discardedTip = ` | <span style="color:#ff5555;">作废: ${plan.discarded_tasks}${detailStr}</span>`;
            }
            // 智能补偿提示
            let compensateTip = '';
            if (plan.compensated_count && plan.compensated_count > 0) {
                compensateTip = ` | <span style="color:#00aaff;">🔄 补偿: ${plan.compensated_count}</span>`;
            }
            
            // 国家数量
            const countryCount = Object.keys(plan.country_distribution || {}).length;
            // 平均每小时任务数
            const avgPerHour = totalSec > 0 ? (plan.total_tasks / (totalSec / 3600)).toFixed(1) : 0;
            
            let dailySummaryHtml = '';
            if (plan.daily_summaries && plan.daily_summaries.length > 0) {
                dailySummaryHtml = '<div style="margin-top:6px; display:flex; flex-wrap:wrap; gap:6px;">' +
                    plan.daily_summaries.map(d => {
                        const model = modelNames[d.model_used] || d.model_used;
                        return `<span style="background:#1f2937; padding:3px 7px; border-radius:4px; color:#d1d5db;">${d.date}：${model}，${d.generated_tasks}/${d.planned_tasks}</span>`;
                    }).join('') + '</div>';
            }
            document.getElementById('planSummary').innerHTML = 
                `计划天数: <b style="color:#00d4aa;">${plan.plan_days || 1}</b> | ` +
                `有效任务: <b style="color:#00d4aa;">${plan.total_tasks}</b>` +
                (plan.planned_tasks ? ` / 计划: <b>${plan.planned_tasks}</b>` : '') +
                ` | 模型: <b style="color:#ffaa00;">${modelNames[plan.model_used] || plan.model_used}</b> | ` +
                `网站: <b style="color:#ffaa00;">${siteAgeNames[plan.site_age] || plan.site_age}</b> | ` +
                `国家: <b style="color:#00aaff;">${countryCount}</b> | ` +
                `跨度: <b style="color:#00d4aa;">${hours}h ${mins}m</b> | ` +
                `平均: <b style="color:#00d4aa;">${avgPerHour}/h</b>` + discardedTip + compensateTip + dailySummaryHtml;
            
            // ⭐ 渲染国家分布统计
            const distribDiv = document.getElementById('countryDistribStats');
            if (distribDiv) {
                const distrib = plan.country_distribution || {};
                const quotaTarget = plan.country_quota_target || {};
                const sortedCountries = Object.keys(distrib).sort((a,b) => distrib[b] - distrib[a]);
                const countryFlags = {
                    'US':'🇺🇸','GB':'🇬🇧','DE':'🇩🇪','FR':'🇫🇷','JP':'🇯🇵',
                    'SG':'🇸🇬','HK':'🇭🇰','ID':'🇮🇩','AU':'🇦🇺','NZ':'🇳🇿'
                };
                let html = '<div style="display:flex; flex-wrap:wrap; gap:6px;">';
                sortedCountries.forEach(cc => {
                    const count = distrib[cc];
                    const target = quotaTarget[cc] || 0;
                    const pct = ((count / plan.total_tasks) * 100).toFixed(0);
                    const flag = countryFlags[cc] || '🏳️';
                    html += `<span style="background:#222; padding:3px 8px; border-radius:4px; font-size:11px;">` +
                            `${flag} <b>${cc}</b>: ${count}/${target} (${pct}%)</span>`;
                });
                html += '</div>';
                distribDiv.innerHTML = html;
            }
            
            // ⭐ 渲染覆盖时段提示
            const coverageDiv = document.getElementById('coverageInfo');
            if (coverageDiv && plan.coverage) {
                const pct = plan.coverage.coverage_pct.toFixed(1);
                const gaps = plan.coverage.uncovered_segments || [];
                let coverageColor = '#00d4aa';
                let icon = '✅';
                if (pct < 80) { coverageColor = '#ffaa00'; icon = '⚠️'; }
                if (pct < 50) { coverageColor = '#ff5555'; icon = '❌'; }
                let html = `<span style="color:${coverageColor};">${icon} 全球覆盖率: <b>${pct}%</b></span>`;
                if (gaps.length > 0 && pct < 100) {
                    const gapTexts = gaps.slice(0, 3).map(([s, e]) => {
                        return `${secToHHMMSS(s)}-${secToHHMMSS(e)}`;
                    });
                    html += ` <span style="color:#ff9966; font-size:11px;">（空白: ${gapTexts.join(', ')}${gaps.length>3?'...':''}）</span>`;
                }
                coverageDiv.innerHTML = html;
            }
            
            // ⭐ 渲染警告
            const warnDiv = document.getElementById('planWarnings');
            if (warnDiv) {
                const warnings = plan.warnings || [];
                if (warnings.length === 0) {
                    warnDiv.style.display = 'none';
                } else {
                    warnDiv.style.display = 'block';
                    warnDiv.innerHTML = warnings.map(w => `<div style="color:#ff9966;">${w}</div>`).join('');
                }
            }
            
            // 把秒数（今日00:00起）转 HH:MM:SS，支持超过86400（跨天）
            function secToHHMMSS(utcSecToday) {
                try {
                    // 处理大数和非数字的情况
                    if (typeof utcSecToday !== 'number' || isNaN(utcSecToday)) {
                        return '00:00:00';
                    }
                    
                    const todayUTC = new Date();
                    todayUTC.setUTCHours(0,0,0,0);
                    const d = new Date(todayUTC.getTime() + utcSecToday * 1000);
                    
                    // 直接获取本地时区的时间
                    const h = d.getHours();
                    const m = d.getMinutes();
                    const s = d.getSeconds();
                    
                    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
                } catch (e) {
                    console.error('secToHHMMSS error:', e);
                    return '00:00:00';
                }
            }
            
            // 渲染表格（多天计划按日期分组）
            const tbody = document.getElementById('planTableBody');
            tbody.innerHTML = '';
            let lastDate = null;
            plan.tasks.forEach(t => {
                const taskDate = t.date || (t.plan_time ? t.plan_time.slice(0, 10) : '未分组');
                if (taskDate !== lastDate) {
                    lastDate = taskDate;
                    const groupRow = document.createElement('tr');
                    groupRow.innerHTML = `<td colspan="6" style="padding:5px 6px; background:#111827; color:#fbbf24; font-weight:bold; border-bottom:1px solid #374151;">📅 ${taskDate}</td>`;
                    tbody.appendChild(groupRow);
                }
                const startStr = t.plan_time || secToHHMMSS(t.actual_start || 0);
                const endStr = secToHHMMSS(t.actual_end || 0);
                const duration = (t.task_duration || 0).toFixed(1);
                const status = t.status || '未完成';
                let statusColor = '#aaa';
                if (status === '已完成') statusColor = '#00d4aa';
                if (status === '失败') statusColor = '#ff5555';
                
                const row = document.createElement('tr');
                row.innerHTML = 
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${t.idx}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${startStr}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${duration}s</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${endStr}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${t.proxy_country || '-'}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222; color:${statusColor}; font-weight:bold;">${status}</td>`;
                tbody.appendChild(row);
            });
            
            // 启用执行按钮
            document.getElementById('btn-execute-plan').disabled = false;
            document.getElementById('btn-clear-plan').disabled = false;
        }

        // 生成计划
        function generatePlan() {
            console.log('✅ 生成计划按钮被点击');
            const payload = collectConfigPayload();
            console.log('✅ 收集到的配置:', payload);
            // 先保存配置，再生成计划
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(() => {
                console.log('✅ 配置保存成功');
                return fetch('/generate_plan', {method: 'POST'});
            }).then(r => {
                console.log('✅ 获取计划响应:', r);
                return r.json();
            }).then(result => {
                console.log('✅ 解析的结果:', result);
                if (result.status === 'ok') {
                    console.log('✅ 开始渲染计划');
                    renderPlan(result.plan);
                    // 确保显示计划预览界面
                    document.getElementById('planPreviewPanel').style.display = 'block';
                    alert('✅ 计划已生成，请在右侧查看，确认无误后点击"执行计划"');
                } else {
                    alert('❌ 计划生成失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                console.error('❌ 请求错误:', err);
                alert('❌ 请求失败: ' + err);
            });
        }

        // 清除日志显示
        function clearLogBox() {
            const logBox = document.getElementById('logBox');
            if (logBox) {
                logBox.innerHTML = '';
                originalLogHTML = '';
            }
        }

        // 执行任务
        function executePlan() {
            if (!confirm('确定要开始执行计划吗？')) return;
            clearLogBox();
            fetch('/start_task', {method: 'POST'}).then(() => location.reload());
        }

        // 单独任务：保存当前配置后，立即执行 1 个任务，不生成/消费计划
        function startSingleTask() {
            if (!confirm('确定要立即执行一个单独网站任务吗？不会生成或使用计划。')) return;
            clearLogBox();
            const payload = collectConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.status === 'error' || result.success === false) {
                    throw new Error(result.message || '配置保存失败');
                }
                return fetch('/start_single_task', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status !== 'ok') {
                    throw new Error(result.message || '启动失败');
                }
                location.reload();
            }).catch(err => {
                alert('❌ 单独任务启动失败: ' + err.message);
            });
        }

        // 清除计划
        function clearPlan() {
            if (!confirm('确定要清除当前计划吗？')) return;
            fetch('/clear_plan', {method: 'POST'}).then(() => {
                document.getElementById('planPreviewPanel').style.display = 'none';
                alert('✅ 计划已清除');
            });
        }

        // 显示历史任务
        function showHistoricalTasks() {
            fetch('/get_historical_tasks')
                .then(r => r.json())
                .then(data => {
                    const panel = document.getElementById('historicalTasksPanel');
                    const container = document.getElementById('historicalTasksContainer');
                    
                    if (data.historical_tasks && data.historical_tasks.length > 0) {
                        let html = '';
                        data.historical_tasks.slice().reverse().forEach((plan, idx) => {
                            const createdTime = plan.created_at_local || '';
                            const completedCount = (plan.tasks || []).filter(t => t.status === '已完成').length;
                            const failedCount = (plan.tasks || []).filter(t => t.status === '失败').length;
                            const totalCount = (plan.tasks || []).length;
                            
                            html += `<div style="background:#1a1a2e; border:1px solid #333; border-radius:4px; padding:10px; margin-bottom:10px;">
                                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; cursor:pointer;" onclick="toggleHistoricalPlan(${idx})">
                                    <div style="color:#00aaff; font-weight:bold;">📅 ${createdTime}</div>
                                    <div style="color:#aaa; font-size:12px;">总:${totalCount} 成功:${completedCount} 失败:${failedCount}</div>
                                </div>
                                <div id="historicalPlan_${idx}" style="display:none;">
                                    <div style="max-height:300px; overflow-y:auto;">
                                        <table style="width:100%; color:#fff; font-size:11px; border-collapse:collapse;">
                                            <thead style="background:#1a1a2e;">
                                                <tr>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">序号</th>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">开始</th>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">时长</th>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">结束</th>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">代理</th>
                                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">状态</th>
                                                </tr>
                                            </thead>
                                            <tbody>`;
                            
                            (plan.tasks || []).forEach(t => {
                                const startStr = secToHHMMSS(t.actual_start || 0);
                                const endStr = secToHHMMSS(t.actual_end || 0);
                                const duration = (t.task_duration || 0).toFixed(1);
                                const status = t.status || '未完成';
                                let statusColor = '#aaa';
                                if (status === '已完成') statusColor = '#00d4aa';
                                if (status === '失败') statusColor = '#ff5555';
                                
                                html += `<tr>
                                    <td style="padding:4px; border-bottom:1px solid #222;">${t.idx}</td>
                                    <td style="padding:4px; border-bottom:1px solid #222;">${startStr}</td>
                                    <td style="padding:4px; border-bottom:1px solid #222;">${duration}s</td>
                                    <td style="padding:4px; border-bottom:1px solid #222;">${endStr}</td>
                                    <td style="padding:4px; border-bottom:1px solid #222;">${t.proxy_country || '-'}</td>
                                    <td style="padding:4px; border-bottom:1px solid #222; color:${statusColor}; font-weight:bold;">${status}</td>
                                </tr>`;
                            });
                            
                            html += `</tbody></table></div></div></div>`;
                        });
                        
                        container.innerHTML = html;
                    } else {
                        container.innerHTML = '<div style="color:#aaa; text-align:center; padding:20px;">暂无历史任务记录</div>';
                    }
                    
                    panel.style.display = 'block';
                })
                .catch(err => {
                    alert('❌ 获取历史任务失败: ' + err);
                });
        }

        // 隐藏历史任务面板
        function hideHistoricalTasks() {
            document.getElementById('historicalTasksPanel').style.display = 'none';
        }

        // 显示指纹和UA统计
        function showFingerprintStats() {
            fetch('/get_fingerprint_stats')
                .then(r => r.json())
                .then(data => {
                    const panel = document.getElementById('fingerprintStatsPanel');
                    const container = document.getElementById('fingerprintStatsContainer');
                    
                    let html = '';
                    
                    // 总任务数
                    html += `<div style="margin-bottom:15px; padding:10px; background:#1a1a2e; border:1px solid #333; border-radius:4px;">
                        <div style="color:#28a745; font-weight:bold; font-size:16px;">📊 总任务数：${data.total_tasks}</div>
                    </div>`;
                    
                    // UA统计
                    if (data.ua_stats && data.ua_stats.length > 0) {
                        html += `<div style="margin-bottom:15px;">
                            <h4 style="color:#ffc107; margin:0 0 10px 0;">🎯 User-Agent使用统计</h4>
                            <div style="max-height:200px; overflow-y:auto;">
                                <table style="width:100%; color:#fff; font-size:11px; border-collapse:collapse;">
                                    <thead style="background:#1a1a2e;">
                                        <tr>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left; width:60px;">使用次数</th>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">User-Agent</th>
                                        </tr>
                                    </thead>
                                    <tbody>`;
                        data.ua_stats.forEach(([ua, count]) => {
                            const color = count > 5 ? '#ff5555' : (count > 2 ? '#ffc107' : '#28a745');
                            html += `<tr>
                                <td style="padding:4px; border-bottom:1px solid #222; color:${color}; font-weight:bold;">${count}次</td>
                                <td style="padding:4px; border-bottom:1px solid #222; word-break:break-all;">${ua}</td>
                            </tr>`;
                        });
                        html += `</tbody></table></div></div>`;
                    }
                    
                    // 指纹统计
                    if (data.fingerprint_stats && data.fingerprint_stats.length > 0) {
                        html += `<div style="margin-bottom:15px;">
                            <h4 style="color:#00aaff; margin:0 0 10px 0;">🔑 指纹ID使用统计</h4>
                            <div style="max-height:200px; overflow-y:auto;">
                                <table style="width:100%; color:#fff; font-size:11px; border-collapse:collapse;">
                                    <thead style="background:#1a1a2e;">
                                        <tr>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left; width:60px;">使用次数</th>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">指纹ID</th>
                                        </tr>
                                    </thead>
                                    <tbody>`;
                        data.fingerprint_stats.forEach(([fingerprintId, count]) => {
                            const color = count > 5 ? '#ff5555' : (count > 2 ? '#ffc107' : '#28a745');
                            html += `<tr>
                                <td style="padding:4px; border-bottom:1px solid #222; color:${color}; font-weight:bold;">${count}次</td>
                                <td style="padding:4px; border-bottom:1px solid #222;">${fingerprintId}</td>
                            </tr>`;
                        });
                        html += `</tbody></table></div></div>`;
                    }
                    
                    // 使用历史
                    if (data.history && data.history.length > 0) {
                        html += `<div>
                            <h4 style="color:#17a2b8; margin:0 0 10px 0;">📝 近三天使用记录</h4>
                            <div style="max-height:200px; overflow-y:auto;">
                                <table style="width:100%; color:#fff; font-size:11px; border-collapse:collapse;">
                                    <thead style="background:#1a1a2e;">
                                        <tr>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">时间</th>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">国家</th>
                                            <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">指纹ID</th>
                                        </tr>
                                    </thead>
                                    <tbody>`;
                        data.history.slice().reverse().forEach(record => {
                            html += `<tr>
                                <td style="padding:4px; border-bottom:1px solid #222;">${record.timestamp_local || ''}</td>
                                <td style="padding:4px; border-bottom:1px solid #222;">${record.country_code || '-'}</td>
                                <td style="padding:4px; border-bottom:1px solid #222;">${record.fingerprint_id}</td>
                            </tr>`;
                        });
                        html += `</tbody></table></div></div>`;
                    }
                    
                    if (!html) {
                        html = '<div style="color:#aaa; text-align:center; padding:20px;">暂无统计数据</div>';
                    }
                    
                    container.innerHTML = html;
                    panel.style.display = 'block';
                })
                .catch(err => {
                    alert('❌ 获取指纹统计失败: ' + err);
                });
        }

        // 隐藏指纹和UA统计面板
        function hideFingerprintStats() {
            document.getElementById('fingerprintStatsPanel').style.display = 'none';
        }

        // 切换历史计划展开/收起
        function toggleHistoricalPlan(idx) {
            const el = document.getElementById('historicalPlan_' + idx);
            if (el.style.display === 'none') {
                el.style.display = 'block';
            } else {
                el.style.display = 'none';
            }
        }

        // 页面加载时检查是否已有计划，并加载代理池配置
        document.addEventListener('DOMContentLoaded', function() {
            // 1. 加载计划预览
            fetch('/get_plan').then(r => r.json()).then(data => {
                if (data.plan) {
                    renderPlan(data.plan);
                }
            });
            
            // 2. 从服务器加载代理池配置（保持上次保存的参数）
            fetch('/get_config').then(r => r.json()).then(data => {
                if (data.config && data.config.proxy_pool) {
                    const proxyPool = data.config.proxy_pool;
                    const container = document.getElementById('proxy-pool-container');
                    if (container && proxyPool.length > 0) {
                        // 清空现有内容
                        container.innerHTML = '';
                        // 渲染每个代理配置
                        proxyPool.forEach(proxy => {
                            const proxyHtml = `
                                <div class="proxy-item" style="display:flex; gap:8px; align-items:center; padding:8px; background:#2a2a2a; border-radius:8px;">
                                    <div style="width:80px;">
                                        <label style="display:flex; align-items:center; gap:5px;">
                                            <input type="checkbox" class="proxy-enabled" ${proxy.enabled ? 'checked' : ''}>
                                            启用
                                        </label>
                                    </div>
                                    <div style="width:80px; font-weight:bold; font-size:16px;">
                                        <input type="text" class="proxy-country" value="${proxy.country_code || 'US'}" maxlength="8" style="width:100%; font-weight:bold; font-size:14px; text-transform:uppercase;">
                                    </div>
                                    <div style="flex:1;">
                                        <input type="text" class="proxy-api-url" value="${proxy.proxy_api_url || ''}" style="width:100%; font-size:12px; color:#aaa;">
                                    </div>
                                    <div style="width:80px;">
                                        <input type="text" class="proxy-user" value="${proxy.proxy_user || ''}" style="width:100%; font-size:12px;">
                                    </div>
                                    <div style="width:80px;">
                                        <input type="password" class="proxy-pwd" value="${proxy.proxy_pwd || ''}" style="width:100%; font-size:12px;">
                                    </div>
                                    <button class="btn btn-red" onclick="removeProxy(this)" style="padding:4px 8px; font-size:12px;">删除</button>
                                </div>
                            `;
                            container.insertAdjacentHTML('beforeend', proxyHtml);
                        });
                        console.log('✅ 已加载代理池配置:', proxyPool.length, '个代理');
                    }
                    
                    // 4. 加载ADSL配置
                    if (data.config.adsl_username) {
                        const el = document.getElementById('adsl_username');
                        if (el) el.value = data.config.adsl_username;
                    }
                    if (data.config.adsl_password) {
                        const el = document.getElementById('adsl_password');
                        if (el) el.value = data.config.adsl_password;
                    }
                    if (data.config.adsl_interface) {
                        const el = document.getElementById('adsl_interface');
                        if (el) el.value = data.config.adsl_interface;
                    }
                }
            }).catch(err => {
                console.error('❌ 加载代理池配置失败:', err);
            });
        });

        function startTask() {
            // 兼容旧调用，直接生成并执行
            const payload = collectConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(() => {
                fetch('/start_task', {method: 'POST'}).then(() => location.reload());
            });
        }

        function stopTask() {
            if (!confirm('确定要停止当前网站/综合QA/视频任务吗？')) return;
            Promise.allSettled([
                fetch('/stop_task', {method: 'POST'}),
                fetch('/stop_video_tasks', {method: 'POST'})
            ]).then(() => {
                alert('✅ 已发送停止信号，当前任务会在等待/重拨/浏览器清理点尽快中断');
                location.reload();
            }).catch(err => {
                alert('❌ 停止请求失败: ' + err);
            });
        }

        function resetConfig() {
            resetDefaults('all');
        }

        function resetDefaults(scope) {
            if (!confirm('确定要恢复默认参数吗？这会覆盖当前配置并清除未执行计划。')) return;
            fetch('/reset_config_defaults', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({scope: scope || 'all'})
            }).then(r => r.json()).then(data => {
                if (data.success || data.status === 'ok') {
                    alert('✅ ' + (data.message || '配置已恢复默认'));
                    location.reload();
                } else {
                    alert('❌ 恢复默认失败: ' + (data.message || '未知错误'));
                }
            }).catch(err => {
                alert('❌ 恢复默认请求失败: ' + err);
            });
        }

        function saveRuntimeMode() {
            const headlessEl = document.querySelector('input[name="headless_mode"]:checked');
            const logModeEl = document.querySelector('input[name="log_mode"]:checked');
            const payload = {
                headless: headlessEl ? headlessEl.value !== 'false' : true,
                log_mode: logModeEl ? logModeEl.value : 'test'
            };
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    console.log('✅ 运行模式已保存:', payload);
                }
            })
            .catch(err => {
                console.error('❌ 保存运行模式失败:', err);
            });
        }

        function saveHeadlessMode(checkbox) {
            saveRuntimeMode();
        }

        function capLogDom(logBox, maxLines) {
            if (!logBox) return;
            const lines = logBox.querySelectorAll('p');
            const overflow = lines.length - maxLines;
            if (overflow > 0) {
                // 最新日志在顶部，超出上限时从底部（最旧）开始删除
                for (let i = 0; i < overflow; i++) {
                    lines[lines.length - 1 - i].remove();
                }
            }
        }

        // 更新日志
        setInterval(() => {
            // 更新日志
            const logModeEl = document.querySelector('input[name="log_mode"]:checked');
            const logMode = logModeEl ? logModeEl.value : 'test';
            fetch('/get_logs?mode=' + encodeURIComponent(logMode) + '&limit=500').then(r => r.text()).then(html => {
                const logBox = document.getElementById('logBox');
                const autoScrollCheckbox = document.getElementById('autoScroll');
                const filterSelect = document.getElementById('logFilter');
                const isAutoScroll = autoScrollCheckbox ? autoScrollCheckbox.checked : true;
                
                if (logBox) {
                    // 更新日志内容，接口和前端双重限制日志行数，避免 DOM 过大卡死
                    if (isAutoScroll) {
                        logBox.innerHTML = html;
                        capLogDom(logBox, 500);
                        originalLogHTML = logBox.innerHTML;
                        logBox.scrollTop = 0;
                    } else {
                        const currentScrollTop = logBox.scrollTop;
                        logBox.innerHTML = html;
                        capLogDom(logBox, 500);
                        originalLogHTML = logBox.innerHTML;
                        logBox.scrollTop = currentScrollTop;
                    }
                    
                    // 如果有筛选，应用筛选
                    if (filterSelect && filterSelect.value !== 'all') {
                        filterLogs();
                    }
                }
            });
            
            // 更新统计
            fetch('/api/status').then(r => r.json()).then(status => {
                // 更新网站顶部状态
                const websiteTopStatus = document.getElementById('websiteTopStatus');
                if (websiteTopStatus) {
                    websiteTopStatus.className = 'status ' + (status.running ? 'running' : 'stopped');
                    websiteTopStatus.textContent = status.running ? '运行中' : '已停止';
                }
                const websiteConfigStatus = document.getElementById('websiteConfigStatus');
                if (websiteConfigStatus) {
                    websiteConfigStatus.textContent = status.running ? '运行中' : '已停止';
                    websiteConfigStatus.style.color = status.running ? '#00d4aa' : '#ffd54f';
                }
                
                // 更新 ADSL 状态
                if (status.adsl) {
                    const adslStatusEl = document.getElementById('adslTaskStatus');
                    const adslCompletedEl = document.getElementById('adslCompletedCount');
                    if (adslStatusEl) {
                        adslStatusEl.textContent = `${status.adsl.status || '停止'} ${status.adsl.current || 0}/${status.adsl.total || 0}`;
                    }
                    if (adslCompletedEl) {
                        adslCompletedEl.textContent = status.adsl.completed || 0;
                    }
                }

                // 更新网站流量任务状态（显示蓝色边框的任务计划状态面板）
                fetch('/get_website_task_status').then(r => r.json()).then(websiteStatus => {
                    const planStatusPanel = document.getElementById('planStatusPanel');
                    const planPreviewPanel = document.getElementById('planPreviewPanel');
                    
                    if (websiteStatus.running && websiteStatus.current_task) {
                        // 任务运行中：显示任务计划状态面板和计划面板
                        planStatusPanel.style.display = 'block';
                        planPreviewPanel.style.display = 'block';
                        
                        // 更新任务计划状态进度
                        const task = websiteStatus.current_task;
                        const progress = `第 ${task.idx}/${websiteStatus.total_tasks} 个任务`;
                        document.getElementById('planStatusProgress').textContent = progress;
                        
                        // 更新任务计划状态信息
                        let statusColor = '#aaa';
                        let statusText = task.status || '执行中';
                        if (task.status === '已完成') {
                            statusColor = '#00d4aa';
                        } else if (task.status === '失败') {
                            statusColor = '#ff5555';
                        }
                        
                        const statusInfo = `
                            <div style="display:flex; flex-direction:column; gap:2px;">
                                <div><span style="color:#888;">计划:</span> <span style="color:#00aaff; font-weight:bold;">${task.idx}/${websiteStatus.total_tasks}</span> <span style="color:#888;">时间:</span> <span>${task.plan_time || '-'}</span></div>
                                <div><span style="color:#888;">代理:</span> <span>${task.proxy_country || '-'}</span> <span style="color:#888;">开始:</span> <span>${task.start_time || '-'}</span> <span style="color:#888;">状态:</span> <span style="color:${statusColor}; font-weight:bold;">${statusText}</span></div>
                            </div>
                        `;
                        document.getElementById('planStatusInfo').innerHTML = statusInfo;
                        
                        // 更新计划面板中的当前任务状态（高亮当前任务行）
                        const tbody = document.getElementById('planTableBody');
                        if (tbody) {
                            const rows = tbody.querySelectorAll('tr');
                            if (rows.length > websiteStatus.current_task_idx) {
                                // 高亮当前任务行
                                rows.forEach((row, idx) => {
                                    row.style.backgroundColor = idx === websiteStatus.current_task_idx ? '#2a4a6a' : '';
                                    row.style.fontWeight = idx === websiteStatus.current_task_idx ? 'bold' : 'normal';
                                });
                                
                                // 更新当前任务的状态显示
                                const currentRow = rows[websiteStatus.current_task_idx];
                                if (currentRow) {
                                    const statusCell = currentRow.querySelector('td:last-child');
                                    if (statusCell) {
                                        statusCell.textContent = statusText;
                                        statusCell.style.color = statusColor;
                                    }
                                }
                            }
                        }
                        
                        // 更新计划摘要中的进度
                        const planSummary = document.getElementById('planSummary');
                        if (planSummary) {
                            const currentIdx = websiteStatus.current_task_idx + 1;
                            const total = websiteStatus.total_tasks;
                            const progressText = ` | <span style="color:#00d4aa;">进度: ${currentIdx}/${total}</span>`;
                            if (!planSummary.innerHTML.includes('进度:')) {
                                planSummary.innerHTML += progressText;
                            } else {
                                planSummary.innerHTML = planSummary.innerHTML.replace(/进度: \d+\/\d+/, `进度: ${currentIdx}/${total}`);
                            }
                        }
                    } else {
                        // 任务未运行：隐藏任务计划状态面板
                        planStatusPanel.style.display = 'none';
                    }
                });
                
                // 更新成功和失败统计
                const statusItems = document.querySelectorAll('.status-item');
                if (statusItems.length >= 7) {
                    statusItems[3].querySelector('.stat-number').textContent = status.success;
                    statusItems[4].querySelector('.stat-number').textContent = status.fail;
                    statusItems[5].querySelector('.stat-number').textContent = status.video_view_count + '次';
                    statusItems[6].querySelector('.stat-number').textContent = status.total_video_watch_time + 's';
                }
            });
        }, 1000);
        
        function addEngine() {
            const container = document.getElementById('engines-container');
            const engineHtml = `
                <div class="engine-item" style="display: flex; gap: 10px; margin-bottom: 10px; align-items: center;">
                    <input type="text" class="engine-id" placeholder="引擎ID" style="flex: 1;">
                    <input type="text" class="engine-name" placeholder="引擎名称" style="flex: 1;">
                    <input type="text" class="engine-url" placeholder="搜索URL" style="flex: 2;">
                    <select class="engine-lang" style="width: 100px;">
                        <option value="zh">中文</option>
                        <option value="en">英文</option>
                    </select>
                    <button class="btn btn-red" onclick="removeEngine(this)" style="padding: 5px 10px;">删除</button>
                </div>
            `;
            container.insertAdjacentHTML('beforeend', engineHtml);
        }
        
        function removeEngine(btn) {
            const container = document.getElementById('engines-container');
            if (container.children.length > 1) {
                btn.parentElement.remove();
            } else {
                alert('至少需要保留一个搜索引擎');
            }
        }
        
        function addProxy() {
            const container = document.getElementById('proxy-pool-container');
            const proxyHtml = `
                <div class="proxy-item" style="display:flex; gap:8px; align-items:center; padding:8px; background:#2a2a2a; border-radius:8px;">
                    <div style="width:80px;">
                        <label style="display:flex; align-items:center; gap:5px;">
                            <input type="checkbox" class="proxy-enabled" checked>
                            启用
                        </label>
                    </div>
                    <div style="width:80px; font-weight:bold; font-size:16px;">
                        <input type="text" class="proxy-country" value="US" maxlength="8" style="width:100%; font-weight:bold; font-size:14px; text-transform:uppercase;">
                    </div>
                    <div style="flex:1;">
                        <input type="text" class="proxy-api-url" style="width:100%; font-size:12px; color:#aaa;">
                    </div>
                    <div style="width:80px;">
                        <input type="text" class="proxy-user" style="width:100%; font-size:12px;">
                    </div>
                    <div style="width:80px;">
                        <input type="password" class="proxy-pwd" style="width:100%; font-size:12px;">
                    </div>
                    <button class="btn btn-red" onclick="removeProxy(this)" style="padding:4px 8px; font-size:12px;">删除</button>
                </div>
            `;
            container.insertAdjacentHTML('beforeend', proxyHtml);
        }
        
        function removeProxy(btn) {
            const container = document.getElementById('proxy-pool-container');
            if (container.children.length > 1) {
                btn.parentElement.remove();
            } else {
                alert('至少需要保留一个代理配置');
            }
        }
        
        // 网站流量配置
        function saveWebsiteTrafficConfig() {
            const payload = collectConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.success || result.status === 'ok') {
                    alert('✅ 网站流量配置已保存');
                } else {
                    alert('❌ 保存失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        function resetWebsiteTrafficConfig() {
            resetDefaults('website');
        }
        
        // SEO配置
        function saveSEOConfig() {
            alert('SEO配置已保存');
        }
        
        function resetSEOConfig() {
            resetDefaults('seo');
        }
        
        // 任务验证配置
        function saveTaskValidationConfig() {
            alert('任务验证配置已保存');
        }
        
        function resetTaskValidationConfig() {
            resetDefaults('task_validation');
        }
        
        // ===== 攻防演练 =====
        let _drillPolling = null;
        function startSecurityDrill() {
            const btn = document.getElementById('btnSecurityDrill');
            btn.disabled = true;
            document.getElementById('drillResult').style.display = 'none';
            setDrillProgress(0, '启动中');
            fetch('/start_security_drill', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({headless: true})
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    alert('启动失败: ' + (d.message || '未知错误'));
                    btn.disabled = false;
                    setDrillProgress(0, '未开始');
                    return;
                }
                _drillPolling = setInterval(pollDrillStatus, 1000);
            }).catch(e => {
                alert('请求异常: ' + e);
                btn.disabled = false;
            });
        }
        function setDrillProgress(pct, stage) {
            document.getElementById('drillBar').style.width = pct + '%';
            document.getElementById('drillPercent').textContent = pct + '%';
            document.getElementById('drillStage').textContent = stage || '';
        }
        function pollDrillStatus() {
            fetch('/get_security_drill_status').then(r => r.json()).then(d => {
                setDrillProgress(d.progress || 0, d.stage || '');
                if (!d.running) {
                    clearInterval(_drillPolling);
                    _drillPolling = null;
                    document.getElementById('btnSecurityDrill').disabled = false;
                    if (d.full_report) {
                        renderDrillReport(d.full_report, d.html_path);
                    }
                }
            }).catch(() => {});
        }
        
        // 渲染演练报告（精简版，直接列出问题）
        function renderDrillReport(report, htmlPath) {
            const rc = report.risk_calc || {};
            const dimensions = rc.detection_dimensions || {};
            const riskList = rc.risk_reason_list || [];
            
            let html = '';
            
            // 1. 风险总览
            html += '<div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:12px;">';
            html += '<div style="font-size:18px;font-weight:bold;color:#f59e0b;margin-bottom:8px;">';
            html += '🛡️ 风险评分: ' + (rc.total_score ?? '-') + ' - ' + (rc.risk_level || '未知') + '</div>';
            html += '</div>';
            
            // 2. 八维度检测仪表盘
            if (Object.keys(dimensions).length > 0) {
                html += '<div style="margin-bottom:12px;">';
                html += '<div style="font-size:14px;font-weight:bold;color:#93c5fd;margin-bottom:8px;">📊 八维度检测结果</div>';
                html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;">';
                for (const [key, status] of Object.entries(dimensions)) {
                    const color = status === '✅' ? '#22c55e' : (status === '⚠️' ? '#f59e0b' : '#ef4444');
                    html += '<div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">';
                    html += '<div style="font-size:20px;margin-bottom:4px;">' + status + '</div>';
                    html += '<div style="font-size:12px;color:#cbd5e1;">' + key + '</div>';
                    html += '</div>';
                }
                html += '</div></div>';
            }
            
            // 3. 具体问题列表（核心）
            if (riskList.length > 0) {
                html += '<div style="margin-bottom:12px;">';
                html += '<div style="font-size:14px;font-weight:bold;color:#f59e0b;margin-bottom:8px;">❌ 检测到的问题 (' + riskList.length + '项)</div>';
                html += '<ul style="margin:0;padding-left:20px;line-height:1.8;">';
                for (const item of riskList) {
                    // 提取关键信息高亮显示
                    let displayItem = item;
                    if (item.includes('[高风险]')) {
                        displayItem = '<span style="color:#ef4444;font-weight:bold;">' + item + '</span>';
                    } else if (item.includes('[中风险]')) {
                        displayItem = '<span style="color:#f59e0b;">' + item + '</span>';
                    } else if (item.includes('[低风险]')) {
                        displayItem = '<span style="color:#93c5fd;">' + item + '</span>';
                    }
                    html += '<li style="margin-bottom:4px;">' + displayItem + '</li>';
                }
                html += '</ul></div>';
            } else {
                html += '<div style="background:#065f46;border-radius:8px;padding:12px;margin-bottom:12px;text-align:center;">';
                html += '<div style="font-size:16px;color:#22c55e;font-weight:bold;">✅ 未检测到明显风险</div>';
                html += '</div>';
            }
            
            // 4. 详细数据折叠区（可选查看）
            html += '<details style="margin-bottom:12px;">';
            html += '<summary style="cursor:pointer;color:#93c5fd;font-size:13px;">📋 查看详细检测数据（点击展开）</summary>';
            html += '<div style="background:#0f172a;border-radius:8px;padding:12px;margin-top:8px;font-size:12px;line-height:1.6;">';
            
            // 自动化探针结果
            if (report.automation_probe) {
                html += '<div style="margin-bottom:8px;"><strong>自动化探针:</strong><br>';
                const autoProbe = report.automation_probe;
                if (autoProbe.nav_webdriver) html += '- webdriver泄漏: <br>';
                if (autoProbe.cdc_trace_leak) html += '- cdc_特征残留: ❌<br>';
                if (autoProbe.playwright_residuals && autoProbe.playwright_residuals.length > 0) {
                    html += '- Playwright残留: ' + autoProbe.playwright_residuals.join(', ') + '<br>';
                }
                html += '</div>';
            }
            
            // HTTP请求头检测
            if (report.http_header_deep) {
                html += '<div style="margin-bottom:8px;"><strong>HTTP请求头:</strong><br>';
                const httpDeep = report.http_header_deep;
                if (httpDeep.completeness_pct !== undefined) html += '- 完整性: ' + httpDeep.completeness_pct + '%<br>';
                if (httpDeep.ua_sec_ch_ua_version_match === false) html += '- UA与Sec-Ch-Ua版本不一致: ❌<br>';
                if (httpDeep.ua_sec_ch_ua_platform_mismatch === true) html += '- UA与平台不一致: ❌<br>';
                html += '</div>';
            }
            
            // 广告合规检测
            if (report.ad_risk) {
                html += '<div style="margin-bottom:8px;"><strong>广告合规:</strong><br>';
                const adRisk = report.ad_risk;
                if (adRisk.hidden_ad_count > 0) html += '- 隐藏广告: ' + adRisk.hidden_ad_count + '个<br>';
                if (adRisk.non_standard_size_count > 0) html += '- 非标准尺寸: ' + adRisk.non_standard_size_count + '个<br>';
                if (adRisk.css_distorted_count > 0) html += '- CSS变形: ' + adRisk.css_distorted_count + '个<br>';
                html += '</div>';
            }
            
            html += '</div></details>';
            
            // 5. 报告文件路径
            if (htmlPath) {
                html += '<div style="color:#94a3b8;font-size:12px;margin-top:8px;">📄 完整报告已保存: ' + htmlPath + '</div>';
            }
            
            document.getElementById('drillResult').style.display = 'block';
            document.getElementById('drillResult').innerHTML = html;
        }
        
        // ===== 生产准入五层测试 =====
        let _prodTestPolling = null;
        function startProductionTest(layers) {
            document.querySelectorAll('#tab-taskvalidation button[onclick^="startProductionTest"]').forEach(b => b.disabled = true);
            document.getElementById('prodTestReport').style.display = 'none';
            document.getElementById('prodTestProgress').style.display = 'block';
            setProdTestProgress(0, '启动中...');
            fetch('/start_production_test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({layers: layers, headless: true})
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    alert('启动失败: ' + (d.message || '未知错误'));
                    enableProdTestButtons();
                    document.getElementById('prodTestProgress').style.display = 'none';
                    return;
                }
                _prodTestPolling = setInterval(pollProductionTest, 1000);
            }).catch(e => {
                alert('请求异常: ' + e);
                enableProdTestButtons();
                document.getElementById('prodTestProgress').style.display = 'none';
            });
        }
        function enableProdTestButtons() {
            document.querySelectorAll('#tab-taskvalidation button[onclick^="startProductionTest"]').forEach(b => b.disabled = false);
        }
        function setProdTestProgress(pct, stage) {
            document.getElementById('prodTestBar').style.width = pct + '%';
            document.getElementById('prodTestPercent').textContent = pct + '%';
            document.getElementById('prodTestStage').textContent = stage || '';
        }
        function pollProductionTest() {
            fetch('/get_production_test_status').then(r => r.json()).then(d => {
                setProdTestProgress(d.progress || 0, d.stage || '');
                if (!d.running) {
                    clearInterval(_prodTestPolling);
                    _prodTestPolling = null;
                    enableProdTestButtons();
                    renderProductionReport(d);
                }
            }).catch(() => {});
        }
        // 渲染生产准入检查单报告
        function renderProductionReport(data) {
            const gate = data.gate;
            const layers = data.layers || {};
            let html = '';
            if (!gate) {
                html = '<div style="background:#7f1d1d;border-radius:8px;padding:14px;color:#fecaca;">测试未产生有效报告，请查看运行日志。</div>';
                document.getElementById('prodTestReport').style.display = 'block';
                document.getElementById('prodTestReport').innerHTML = html;
                return;
            }
            // 1. 总体结论横幅
            const allPass = gate.all_pass;
            const bannerBg = allPass ? 'linear-gradient(135deg,#16a34a,#22c55e)' : 'linear-gradient(135deg,#dc2626,#ef4444)';
            html += '<div style="background:' + bannerBg + ';border-radius:10px;padding:16px;margin-bottom:14px;text-align:center;">';
            html += '<div style="font-size:18px;font-weight:bold;color:#fff;">' + gate.verdict + '</div>';
            html += '<div style="font-size:13px;color:rgba(255,255,255,.9);margin-top:6px;">达标 ' + gate.pass_count + ' / ' + gate.total + ' 项 · ' + gate.time + '</div>';
            html += '</div>';
            // 2. 生产准入检查单（8项）
            html += '<div style="background:#0f172a;border:2px solid #334155;border-radius:10px;padding:14px;margin-bottom:14px;">';
            html += '<div style="font-size:15px;font-weight:bold;color:#f59e0b;margin-bottom:10px;text-align:center;">📋 生产准入检查单（任一条不达标 = 禁止上线）</div>';
            html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">';
            html += '<thead><tr style="background:#1e293b;color:#93c5fd;">';
            html += '<th style="padding:8px;text-align:left;border-bottom:1px solid #334155;">编号</th>';
            html += '<th style="padding:8px;text-align:left;border-bottom:1px solid #334155;">检查项</th>';
            html += '<th style="padding:8px;text-align:left;border-bottom:1px solid #334155;">准入阈值</th>';
            html += '<th style="padding:8px;text-align:left;border-bottom:1px solid #334155;">实测</th>';
            html += '<th style="padding:8px;text-align:center;border-bottom:1px solid #334155;">结论</th>';
            html += '</tr></thead><tbody>';
            for (const it of gate.items) {
                const stColor = it.status === 'pass' ? '#22c55e' : (it.status === 'manual' ? '#f59e0b' : '#ef4444');
                html += '<tr style="border-bottom:1px solid #1e293b;">';
                html += '<td style="padding:8px;color:#fbbf24;font-weight:bold;">' + it.gate + '</td>';
                html += '<td style="padding:8px;color:#e2e8f0;">' + it.name + '</td>';
                html += '<td style="padding:8px;color:#94a3b8;">' + it.threshold + '</td>';
                html += '<td style="padding:8px;color:#cbd5e1;font-size:12px;">' + (it.value || '-') + '</td>';
                html += '<td style="padding:8px;text-align:center;color:' + stColor + ';font-weight:bold;">' + it.status_text + '</td>';
                html += '</tr>';
            }
            html += '</tbody></table></div>';
            // 3. 各层明细（折叠）
            html += '<details style="margin-bottom:12px;">';
            html += '<summary style="cursor:pointer;color:#93c5fd;font-size:14px;font-weight:bold;">🔬 五层检测明细（点击展开）</summary>';
            html += '<div style="margin-top:10px;">';
            const layerOrder = ['code','env','adversarial','behavior','reliability'];
            for (const lid of layerOrder) {
                const lr = layers[lid];
                if (!lr) continue;
                const lhColor = lr.passed ? '#22c55e' : (lr.error ? '#f59e0b' : '#ef4444');
                const lhIcon = lr.passed ? '✅' : (lr.error ? '⚠️' : '❌');
                html += '<div style="background:#0f172a;border-radius:8px;padding:12px;margin-bottom:10px;border-left:4px solid ' + lhColor + ';">';
                html += '<div style="font-size:14px;font-weight:bold;color:' + lhColor + ';margin-bottom:8px;">' + lhIcon + ' ' + lr.name + ' <span style="color:#64748b;font-size:12px;font-weight:normal;">(耗时 ' + lr.elapsed + 's)</span></div>';
                if (lr.error) html += '<div style="color:#fca5a5;font-size:12px;margin-bottom:6px;">异常: ' + lr.error + '</div>';
                for (const c of (lr.checks || [])) {
                    const cColor = c.status === 'pass' ? '#22c55e' : (c.status === 'manual' ? '#f59e0b' : (c.status === 'info' ? '#93c5fd' : '#ef4444'));
                    const cIcon = c.status === 'pass' ? '✅' : (c.status === 'manual' ? '🟡' : (c.status === 'info' ? 'ℹ️' : '❌'));
                    html += '<div style="padding:6px 0;border-bottom:1px dashed #1e293b;font-size:12px;">';
                    html += '<div style="color:#e2e8f0;">' + cIcon + ' <strong>' + c.name + '</strong>：' + (c.value || '') + ' <span style="color:#64748b;">(阈值 ' + (c.threshold || '') + ')</span></div>';
                    if (c.detail) html += '<div style="color:#94a3b8;margin-top:2px;padding-left:22px;">' + c.detail + '</div>';
                    html += '</div>';
                }
                html += '</div>';
            }
            html += '</div></details>';
            // 4. 报告路径
            if (data.report_path) {
                html += '<div style="color:#94a3b8;font-size:12px;">📄 完整报告已保存: ' + data.report_path + '</div>';
            }
            document.getElementById('prodTestReport').style.display = 'block';
            document.getElementById('prodTestReport').innerHTML = html;
        }
        
        // ===== 关键词探索 =====
        let _keywordPolling = null;
        function startKeywordExplore() {
            const btn = document.getElementById('btnKeywordExplore');
            btn.disabled = true;
            document.getElementById('keywordResult').style.display = 'none';
            document.getElementById('keywordExploreProgress').style.display = 'block';
            setKeywordProgress(0, '启动中...');
            
            // 获取目标URL（从配置中取第一个勾选的目标站）
            let targetUrl = '';
            for (let i = 1; i <= 3; i++) {
                const checkbox = document.getElementById(`target_url_${i}_enabled`);
                if (checkbox && checkbox.checked) {
                    const urlInput = document.getElementById(`target_url_${i}`);
                    if (urlInput && urlInput.value.trim()) {
                        targetUrl = urlInput.value.trim();
                        break;
                    }
                }
            }
            
            if (!targetUrl) {
                alert('请先在网站流量Tab勾选一个目标网站');
                btn.disabled = false;
                document.getElementById('keywordExploreProgress').style.display = 'none';
                return;
            }
            
            fetch('/api/keyword_explore', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    target_url: targetUrl,
                    max_layer: 5,
                    concurrency: 4
                })
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    alert('启动失败: ' + (d.message || '未知错误'));
                    btn.disabled = false;
                    document.getElementById('keywordExploreProgress').style.display = 'none';
                    return;
                }
                _keywordPolling = setInterval(pollKeywordStatus, 2000);
            }).catch(e => {
                alert('请求异常: ' + e);
                btn.disabled = false;
                document.getElementById('keywordExploreProgress').style.display = 'none';
            });
        }
        
        function setKeywordProgress(pct, stage) {
            document.getElementById('keywordBar').style.width = pct + '%';
            document.getElementById('keywordPercent').textContent = pct + '%';
            document.getElementById('keywordStage').textContent = stage || '准备中...';
        }
        
        function pollKeywordStatus() {
            fetch('/api/keyword_explore/status').then(r => r.json()).then(d => {
                if (!d.success) return;
                const data = d.data;
                
                // 更新进度
                if (data.current_layer !== undefined && data.max_layer !== undefined) {
                    const pct = Math.round((data.current_layer / data.max_layer) * 100);
                    setKeywordProgress(pct, data.progress || `正在探索第 ${data.current_layer} 层`);
                } else {
                    setKeywordProgress(0, data.progress || '准备中...');
                }
                
                if (!data.is_running) {
                    clearInterval(_keywordPolling);
                    _keywordPolling = null;
                    document.getElementById('btnKeywordExplore').disabled = false;
                    
                    if (data.result) {
                        renderKeywordResult(data.result);
                    } else if (data.error) {
                        document.getElementById('keywordStage').textContent = '探索失败: ' + data.error;
                        document.getElementById('keywordBar').style.background = '#ef4444';
                    }
                }
            }).catch(() => {});
        }
        
        function renderKeywordResult(result) {
            let html = '';
            
            // 1. 结果总览
            html += '<div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:12px;">';
            html += '<div style="font-size:16px;font-weight:bold;color:#f59e0b;margin-bottom:8px;">';
            html += ' 关键词探索完成</div>';
            html += '<div style="display:flex;gap:20px;font-size:14px;color:#cbd5e1;">';
            html += '<span>关键词(锚文本): <strong style="color:#22c55e;">' + result.total_keywords + '</strong></span>';
            html += '<span>兜底链接: <strong style="color:#3b82f6;">' + (result.total_fallback_links || 0) + '</strong></span>';
            html += '<span>层级: <strong style="color:#a78bfa;">' + result.layers_crawled + '</strong></span>';
            html += '</div></div>';
            
            // 2. 各层关键词+兜底链接统计
            if (result.layer_summary) {
                html += '<div style="margin-bottom:12px;">';
                html += '<div style="font-size:14px;font-weight:bold;color:#93c5fd;margin-bottom:8px;">📊 各层关键词与兜底链接</div>';
                html += '<div style="display:flex;flex-wrap:wrap;gap:8px;">';
                for (const [layer, count] of Object.entries(result.layer_summary)) {
                    const fbCount = (result.fb_summary && result.fb_summary[layer]) || 0;
                    html += '<div style="background:#0f172a;border-radius:6px;padding:8px 12px;text-align:center;">';
                    html += '<div style="font-size:18px;font-weight:bold;color:#f093fb;">L' + layer + '</div>';
                    html += '<div style="font-size:12px;color:#22c55e;">关键词: ' + count + '个</div>';
                    html += '<div style="font-size:12px;color:#3b82f6;">兜底: ' + fbCount + '个</div>';
                    html += '</div>';
                }
                html += '</div></div>';
            }
            
            // 3. 下载报告按钮
            html += '<div style="margin-top:12px;">';
            html += '<a href="/api/keyword_explore/download/' + result.filename + '" '; 
            html += 'class="btn" style="display:inline-block;padding:8px 16px;background:linear-gradient(135deg,#f093fb,#f5576c);color:#fff;border-radius:6px;text-decoration:none;cursor:pointer;">';
            html += '📥 下载报告 (' + result.filename + ')</a>';
            html += '</div>';
            
            // 4. 文件路径提示
            html += '<div style="color:#94a3b8;font-size:12px;margin-top:8px;"> 报告已保存: data/keyword_explore/' + result.filename + '</div>';
            
            document.getElementById('keywordResult').style.display = 'block';
            document.getElementById('keywordResult').innerHTML = html;
            
            // 保存结果数据
            window._keywordExploreResult = result;
            
            // 自动填写配置到每层关键词池和兜底链接
            applyKeywordToConfig(true);
        }
        
        // 应用关键词到配置：自动填写每层关键词池和兜底链接
        function applyKeywordToConfig(silent) {
            const result = window._keywordExploreResult;
            if (!result || !result.layer_data) {
                if (!silent) alert('没有可用的探索数据');
                return;
            }
            
            const layerData = result.layer_data;
            const mergedKws = result.merged_keywords || [];
            const mergedFbs = result.merged_fallback_urls || [];
            
            for (let i = 1; i <= 6; i++) {
                const layerKey = 'layer_' + i;
                const data = layerData[layerKey];
                if (!data) continue;
                
                const kwTextarea = document.getElementById('webnav_' + layerKey + '_keywords');
                const fbTextarea = document.getElementById('webnav_' + layerKey + '_fallback_urls');
                
                if (kwTextarea) {
                    // 如果该层没有关键词，使用合并的关键词
                    const kws = data.keywords.length > 0 ? data.keywords : mergedKws;
                    kwTextarea.value = kws.join(',');
                }
                
                if (fbTextarea) {
                    // 如果该层没有兜底链接，使用合并的兜底链接
                    const fbs = data.fallback_urls.length > 0 ? data.fallback_urls : mergedFbs;
                    fbTextarea.value = fbs.join(',');
                }
            }
            
            if (!silent) {
                alert('✅ 已将关键词和兜底链接自动填写到各层配置！\n\n请切换到“网站流量”Tab查看，\n并点击“保存配置”按钮保存。');
            }
        }
        
        // 网络配置
        function saveNetworkConfig() {
            // 收集流量模型配置
            const selectedModels = [];
            document.querySelectorAll('.model-check').forEach(cb => {
                if (cb.checked) selectedModels.push(cb.dataset.model);
            });

            // 收集日流量区间配置
            const dailyTrafficRange = {
                new: {
                    min: parseInt(document.getElementById('dt_new_min').value),
                    max: parseInt(document.getElementById('dt_new_max').value)
                },
                mid: {
                    min: parseInt(document.getElementById('dt_mid_min').value),
                    max: parseInt(document.getElementById('dt_mid_max').value)
                },
                old: {
                    min: parseInt(document.getElementById('dt_old_min').value),
                    max: parseInt(document.getElementById('dt_old_max').value)
                }
            };

            // 收集ADSL配置
            const adslConfig = {
                adsl_username: document.getElementById('adsl_username')?.value || '',
                adsl_password: document.getElementById('adsl_password')?.value || '',
                adsl_interface: document.getElementById('adsl_interface')?.value || ''
            };

            // 收集代理池配置
            const proxyItems = document.querySelectorAll('.proxy-item');
            const proxyPool = [];
            proxyItems.forEach(item => {
                proxyPool.push({
                    enabled: item.querySelector('.proxy-enabled').checked,
                    country_code: item.querySelector('.proxy-country').value,
                    proxy_api_url: item.querySelector('.proxy-api-url').value,
                    proxy_user: item.querySelector('.proxy-user').value,
                    proxy_pwd: item.querySelector('.proxy-pwd').value
                });
            });

            // 收集VPS配置
            const data = {
                selected_models: selectedModels,
                daily_traffic_range: dailyTrafficRange,

                ip_proxy_api: document.getElementById('ip_proxy_api').value,
                ip_proxy_user: document.getElementById('ip_proxy_user').value,
                ip_proxy_pwd: document.getElementById('ip_proxy_pwd').value,
                proxy_pool: proxyPool
            };

            console.log('发送到服务器的网络配置数据:', data);

            // 发送到服务器保存
            fetch('/save_config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            })
            .then(response => response.json())
            .then(result => {
                console.log('服务器响应:', result);
                
                if (result.success) {
                    alert('网络配置已保存');
                    
                    // 强制刷新页面以重新加载最新配置
                    window.location.reload();
                } else {
                    alert('保存失败: ' + result.message);
                }
            })
            .catch(error => {
                console.error('保存配置时发生错误:', error);
                alert('保存配置时发生错误');
            });
        }
        
        function resetNetworkConfig() {
            resetDefaults('network');
        }
        
        // 模型配置
        function saveModelConfig() {
            const payload = collectConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.status === 'error' || result.success === false) {
                    throw new Error(result.message || '配置保存失败');
                }
                alert('模型配置已保存');
            }).catch(err => {
                alert('❌ 模型配置保存失败: ' + err.message);
            });
        }
        
        function resetModelConfig() {
            resetDefaults('model');
        }
        
        function saveSeoConfig() {
            // 收集搜索引擎 & 社媒平台数据（含type字段）
            const engineItems = document.querySelectorAll('#engines-container .engine-item');
            const searchEngines = [];
            engineItems.forEach(item => {
                const id = item.querySelector('.engine-id').value.trim();
                const name = item.querySelector('.engine-name').value.trim();
                const url = item.querySelector('.engine-url').value.trim();
                const language = item.querySelector('.engine-lang').value;
                const type = item.querySelector('.engine-type').value;
                if (id && name && url) {
                    searchEngines.push({ id, name, url, language, type });
                }
            });
            
            // 收集国别-平台映射
            const regionMap = {};
            document.querySelectorAll('#region-map-container .region-item').forEach(item => {
                const region = item.querySelector('label').textContent.trim();
                const engines = item.querySelector('.region-engines').value.split(',').map(s => s.trim()).filter(s => s);
                if (region && engines.length > 0) {
                    regionMap[region] = engines;
                }
            });
            
            const data = {
                search_engines: searchEngines,
                region_engine_map: regionMap,
                seo_keywords_zh: document.getElementById('seo_keywords_zh').value,
                seo_keywords_en: document.getElementById('seo_keywords_en').value,
                seo_referer_mode: document.getElementById('seo_referer_dynamic').checked ? 'dynamic' : 'static'
            };
            
            fetch('/save_seo_config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            }).then(response => response.json())
            .then(result => {
                if (result.status === 'ok') {
                    alert('SEO配置已保存');
                    location.reload();
                }
            });
        }
        
        function resetSeoConfig() {
            resetDefaults('seo');
        }
        
    </script>
</body>
</html>
"""

class StructuredLogger:
    """结构化日志记录器"""
    def __init__(self, max_lines=500):
        self.messages = []
        self.max_lines = max_lines
    
    def _add_log(self, module, content):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        log_line = f"{timestamp} <span class='log-module'>【{module}】</span> {content}"
        self.messages.append(log_line)
        if len(self.messages) > self.max_lines:
            self.messages.pop(0)
    
    def proxy_module(self, layer1_success, ipdeep_success, ip_success, exit_ip, real_ip, country, region, city, timezone, language):
        content = (
            f"头层代理连接：<span class='{'log-success' if layer1_success else 'log-error'}'>{'成功' if layer1_success else '失败'}</span>、"
            f"IPDeep代理连接：<span class='{'log-success' if ipdeep_success else 'log-error'}'>{'成功' if ipdeep_success else '失败'}</span>、"
            f"出口IP获取：<span class='{'log-success' if ip_success else 'log-error'}'>{'成功' if ip_success else '失败'}</span>、"
            f"出口IP：{exit_ip}、"
            f"本机真实IP(泄漏检测)：{real_ip}、"
            f"IP国家：{country}、"
            f"IP区域：{region}、"
            f"IP城市：{city}、"
            f"IP时区：{timezone}、"
            f"IP语言：{language}"
        )
        self._add_log("代理模块", content)
    
    def fingerprint_module(self, browser_success, fingerprint_success, fingerprint_id, user_agent, resolution, language, timezone, webrtc, canvas, webgl, consistency, consistency_details):
        content = (
            f"浏览器启动：<span class='{'log-success' if browser_success else 'log-error'}'>{'成功' if browser_success else '失败'}</span>、"
            f"指纹生成：<span class='{'log-success' if fingerprint_success else 'log-error'}'>{'成功' if fingerprint_success else '失败'}</span>、"
            f"指纹唯一ID：{fingerprint_id}、"
            f"User-Agent：{user_agent}、"
            f"分辨率：{resolution}、"
            f"浏览器语言：{language}、"
            f"时区：{timezone}、"
            f"WebRTC：{webrtc}、"
            f"Canvas指纹：{canvas[:8]}...、"
            f"WebGL指纹：{webgl[:8]}...、"
            f"指纹IP一致性检查：<span class='{'log-success' if consistency else 'log-error'}'>{'匹配' if consistency else '不匹配'}</span>"
        )
        if not consistency:
            content += f"、详细不匹配：{consistency_details}"
        self._add_log("指纹浏览器模块", content)
    
    def page_ad_module(self, target_url, load_success, load_time, ad_found, ad_in_viewport, ad_loaded, ad_impressions, ad_refreshes):
        content = (
            f"目标URL：{target_url}、"
            f"页面加载状态：<span class='{'log-success' if load_success else 'log-error'}'>{'成功' if load_success else '失败'}</span>、"
            f"页面加载耗时：{load_time:.1f}s、"
            f"广告容器检测：<span class='{'log-success' if ad_found else 'log-info'}'>{'找到' if ad_found else '未找到'}</span>、"
            f"广告进入视口：<span class='{'log-success' if ad_in_viewport else 'log-info'}'>{'是' if ad_in_viewport else '否'}</span>、"
            f"广告加载完成：<span class='{'log-success' if ad_loaded else 'log-info'}'>{'是' if ad_loaded else '否'}</span>、"
            f"广告成功曝光：{ad_impressions}次、"
            f"广告刷新次数：{ad_refreshes}次"
        )
        self._add_log("页面 & 广告模块", content)
    
    def behavior_module(self, mouse_moves, scrolls, scroll_distance, clicks, waits, focus_switches, refreshes, ad_stay, total_stay, key_presses=0):
        content = (
            f"鼠标模拟移动：{mouse_moves}次、"
            f"页面滚动：{scrolls}次、"
            f"滚动总距离：{scroll_distance}px、"
            f"鼠标点击：{clicks}次、"
            f"键盘操作：{key_presses}次、"
            f"随机等待：{waits}次、"
            f"页面焦点切换：{focus_switches}次、"
            f"页面刷新：{refreshes}次、"
            f"广告区域停留：{ad_stay:.1f}s、"
            f"页面总停留：{total_stay:.1f}s"
        )
        self._add_log("真人行为模块", content)
    
    def task_result(self, task_time, success, valid_traffic, fail_reason=""):
        content = (
            f"任务耗时：{task_time:.1f}s、"
            f"任务状态：<span class='{'log-success' if success else 'log-error'}'>{'成功' if success else '失败'}</span>、"
            f"有效流量：<span class='{'log-success' if valid_traffic else 'log-error'}'>{'是' if valid_traffic else '否'}</span>"
        )
        if fail_reason:
            content += f"、失败原因：{fail_reason}"
        self._add_log("任务结果", content)
    
    def info(self, message):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        self.messages.append(f"{timestamp} <span class='log-info'>[INFO]</span> {message}")
        if len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.info(f"[worker] {message}")

    def error(self, message):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        self.messages.append(f"{timestamp} <span class='log-error'>[ERROR]</span> {message}")
        if len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.error(f"[worker] {message}")

    def debug(self, message):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        self.messages.append(f"{timestamp} <span class='log-info'>[DEBUG]</span> {message}")
        if len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.debug(f"[worker] {message}")

    def warning(self, message):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        self.messages.append(f"{timestamp} <span class='log-warning'>[WARNING]</span> {message}")
        if len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.warning(f"[worker] {message}")
    
    def task_separator(self, round_num, total):
        """新任务开始的红色分隔线（明显标识）"""
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        text = f"====={round_num}/{total}次====="
        self.messages.append(f"{timestamp} <span class='log-task-separator'>{text}</span>")
        while len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.info(f"[worker] {text}")

    def web_round_separator(self, round_num, total):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        text = f"-----{round_num}/{total}轮网页-----"
        self.messages.append(f"{timestamp} <span class='log-web-round'>{text}</span>")
        while len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.info(f"[worker] {text}")

    def video_round_separator(self, round_num, total):
        timestamp = time.strftime('[%Y-%m-%d %H:%M:%S]')
        text = f"-----{round_num}/{total}轮视频-----"
        self.messages.append(f"{timestamp} <span class='log-video-round'>{text}</span>")
        while len(self.messages) > self.max_lines:
            self.messages.pop(0)
        logging.info(f"[worker] {text}")
    
    def video_ad_module(self, video_url, watch_time, clicked, watched):
        content = (
            f"视频URL: {video_url}, "
            f"观看时长: {watch_time:.1f}秒, "
            f"是否点击: {'是' if clicked else '否'}, "
            f"是否观看: {'是' if watched else '否'}"
        )
        self._add_log("视频广告模块", content)

log = StructuredLogger()

# 全局 UA 池管理器实例
ua_pool_manager = UAPoolManager()

def get_proxy_from_vps():
    """从 IPDeep 获取代理和IP信息（兼容旧调用）"""
    return get_proxy_from_api_url(config["ip_proxy_api"], config.get("ip_proxy_user", ""), config.get("ip_proxy_pwd", ""), "US")

def get_proxy_from_api_url(api_url, api_user, api_pwd, country_code="US"):
    """直连 IPDeep API 获取代理（代理池方式）
    
    统一使用 ip_provider 模块，消除重复代码。
    内部复用 IPProvider._fetch_proxy_from_ipdeep 的实现。
    """
    # 确保 ip_provider 使用最新配置
    try:
        _ip_provider.configure_ip_provider(config)
    except Exception as e:
        log.debug(f"ip_provider配置同步（非致命错误）: {e}")
    
    return _ip_provider.get_proxy_from_api_url(
        api_url=api_url,
        api_user=api_user,
        api_pwd=api_pwd,
        country_code=country_code,
        use_cache=True,
    )


# ============================================================
# 页面操作安全工具（标准化 evaluate 返回值，避免非基础类型导致的 TypeError）
# ============================================================

def page_eval(page, script, default=""):
    """
    安全地调用 page.evaluate()。
    Playwright 偶发会返回 JSHandle / ElementHandle 之类的对象，Python 侧直接
    len() / strip() 会抛 `len(object) is not supported for sync_wrappers` 之类的
    异常。这里统一把返回值标准化成基础类型（str / int / float / bool / None）。
    """
    try:
        result = page.evaluate(script)
    except Exception:
        return default
    # 如果是 dict / list，原封不动返回（例如 JSON 数据）
    if isinstance(result, (dict, list, tuple, int, float, bool)) or result is None:
        return result if result is not None else default
    # 其余情况强转为字符串
    try:
        s = str(result)
    except Exception:
        return default
    if not isinstance(s, str):
        return default
    return s


def page_title_safe(page, default=""):
    """安全地获取页面标题（避免目标站 title 过长 / 非字符串导致的异常）。"""
    try:
        t = page.title()
    except Exception:
        return default
    if not isinstance(t, str):
        return default
    return t.strip() or default


def page_content_safe(page, default=""):
    """安全地获取页面 HTML 源码。"""
    try:
        c = page.content()
    except Exception:
        return default
    if not isinstance(c, str):
        return default
    return c


def page_url_safe(page, default=""):
    try:
        u = page.url
    except Exception:
        return default
    if isinstance(u, str):
        return u
    try:
        return str(u)
    except Exception:
        return default


def page_body_inner_text(page, default=""):
    """安全地读取 document.body.innerText（短文本用于检测"是否有内容"）。"""
    try:
        raw = page_eval(page, "() => (document.body && document.body.innerText) || ''")
        if not isinstance(raw, str):
            return default
        return raw or default
    except Exception:
        return default


def page_body_text_content(page, default=""):
    try:
        raw = page_eval(page, "() => (document.body && document.body.textContent) || ''")
        if not isinstance(raw, str):
            return default
        return raw or default
    except Exception:
        return default


def page_has_meaningful_content(page, min_chars=30):
    """
    温和地判断页面是否加载出了内容：
    - URL 必须是 http/https
    - body.innerText / body.textContent 任一 ≥ min_chars 字符
    - 兜底：document.title ≥ 2 字符且 body 有内容
    """
    try:
        url = page_url_safe(page, "")
        if not url or not url.lower().startswith(("http://", "https://")):
            return False, 0, url
        # 尝试 innerText
        txt1 = page_body_inner_text(page, "")
        if isinstance(txt1, str):
            bl1 = len(txt1.strip())
        else:
            bl1 = 0
        # 尝试 textContent
        if bl1 < min_chars:
            txt2 = page_body_text_content(page, "")
            bl2 = len(txt2.strip()) if isinstance(txt2, str) else 0
        else:
            bl2 = 0
        bl = bl1 if bl1 >= bl2 else bl2
        if bl >= min_chars:
            return True, bl, url
        # 退而求其次：标题有 + 文本 > 5 字符
        try:
            title = page.evaluate("() => document.title || ''") or ""
            if isinstance(title, str) and len(title.strip()) >= 2 and bl >= 5:
                return True, bl, url
        except Exception:
            pass
        return False, bl, url
    except Exception:
        return False, 0, ""


def page_query_selector_count(page, selector):
    try:
        n = page_eval(
            page,
            f"() => {{ const arr = document.querySelectorAll({_js_quote(selector)}); return arr ? arr.length : 0; }}",
            default=0,
        )
        if isinstance(n, int):
            return n
        try:
            return int(n)
        except Exception:
            return 0
    except Exception:
        return 0


def _js_quote(s):
    """极简的 JS 单引号安全转义，仅供 selectors 等短常量使用。"""
    if not isinstance(s, str):
        s = str(s or "")
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "") + "'"


# ============================================================
# 结束：页面安全工具
# ============================================================


def is_cloudflare_challenge(page):
    """检测是否存在Cloudflare验证挑战"""
    try:
        # 检查页面是否包含Cloudflare验证标志
        has_cloudflare_challenge = page_eval(page, """
            () => {
                const challengeSelectors = [
                    "#challenge-form",
                    "[name='jschl_vc']",
                    "[name='pass']",
                    "#cf-challenge-running",
                    ".cf-browser-verification"
                ];
                return challengeSelectors.some(selector => document.querySelector(selector) !== null);
            }
        """, default=False)
        return bool(has_cloudflare_challenge)
    except Exception as e:
        log.debug(f"检测Cloudflare验证挑战时出错: {e}")
        return False


def solve_cloudflare_challenge(page):
    """尝试解决Cloudflare验证挑战"""
    try:
        log.info("🔐 尝试解决Cloudflare验证挑战...")
        
        # 检查是否需要点击验证按钮
        try:
            # 等待验证按钮出现
            page.wait_for_selector("[name='jschl_vc']", timeout=5000)
            log.info("📝 发现Cloudflare JavaScript挑战")
            
            # 尝试执行挑战解决逻辑
            challenge_solved = execute_cloudflare_javascript_challenge(page)
            
            if challenge_solved:
                log.info("✅ Cloudflare JavaScript挑战解决成功")
            else:
                log.warning("⚠️ Cloudflare JavaScript挑战解决失败")
        except Exception:
            pass
            
        # 等待验证过程完成
        page.wait_for_load_state("networkidle", timeout=60000)
        
        # 检查是否还有挑战
        if is_cloudflare_challenge(page):
            log.error("❌ Cloudflare验证挑战未解决")
            return False
            
        log.info("✅ Cloudflare验证挑战解决成功")
        return True
        
    except Exception as e:
        log.error(f"解决Cloudflare验证挑战时出错: {e}")
        return False


def execute_cloudflare_javascript_challenge(page):
    """执行Cloudflare JavaScript验证挑战"""
    try:
        # 获取挑战参数（使用 page_eval 处理空值情况）
        jschl_vc = page_eval(
            page,
            "() => { const el = document.querySelector('[name=\\'jschl_vc\\']'); return el ? el.value : ''; }",
            default=""
        )
        pass_value = page_eval(
            page,
            "() => { const el = document.querySelector('[name=\\'pass\\']'); return el ? el.value : ''; }",
            default=""
        )
        if not jschl_vc or not pass_value:
            log.debug("未找到 jschl_vc / pass 字段，无需解决")
            return False

        # 获取网站主机名
        host = page_eval(page, "() => location.hostname || ''", default="")

        # 解析挑战脚本
        challenge_script = page_eval(
            page,
            """
            () => {
                try {
                    const scripts = document.querySelectorAll('script');
                    for (let i = 0; i < scripts.length; i++) {
                        const t = scripts[i].textContent || '';
                        if (t.includes('jschl_answer') || t.includes('s,t,o,p,b,r,e,a,k,i,n,g')) {
                            return t;
                        }
                    }
                    return '';
                } catch (e) { return ''; }
            }
            """,
            default=""
        )
        if not isinstance(challenge_script, str) or not challenge_script:
            log.debug("未找到Cloudflare挑战脚本")
            return False

        log.debug("找到Cloudflare挑战脚本，尝试解析jschl_answer")

        jschl_answer = calculate_jschl_answer(challenge_script, host)
        if jschl_answer is None:
            return False

        log.debug(f"计算出jschl_answer: {jschl_answer}")

        params = {"jschl_vc": jschl_vc, "pass": pass_value, "jschl_answer": jschl_answer}

        page_eval(
            page,
            f"""
            () => {{
                const params = {json.dumps(params)};
                const form = document.querySelector('#challenge-form');
                if (form) {{
                    Object.entries(params).forEach(([key, value]) => {{
                        const input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = key;
                        input.value = value;
                        form.appendChild(input);
                    }});
                    try {{ form.submit(); }} catch (_) {{}}
                }}
            }}
            """,
            default=None,
        )
        return True
    except Exception as e:
        log.error(f"执行Cloudflare JavaScript挑战时出错: {e}")
        return False


def calculate_jschl_answer(challenge_script, host):
    """计算Cloudflare jschl_answer挑战答案（简单实现）"""
    try:
        # 这是一个简化的实现，实际情况会更复杂
        log.debug(f"解析挑战脚本: {challenge_script[:200]}...")
        
        # 寻找挑战脚本中的计算逻辑
        # 通常格式为: a = ...; b = ...; c = a + b + host.length
        
        # 这里我们使用一个简单的策略，实际应根据挑战脚本调整
        import re
        
        # 查找可能的计算模式
        math_patterns = [
            r'\((.*?)\+.*?location\.hostname.*?\)',
            r'(\w+)\s*=\s*.*?\+.*?\('
        ]
        
        for pattern in math_patterns:
            matches = re.findall(pattern, challenge_script)
            if matches:
                log.debug(f"找到计算模式: {matches[0]}")
                return len(host) + 123  # 简化的计算
        
        log.debug("未找到明确的jschl_answer计算模式")
        return None
        
    except Exception as e:
        log.error(f"计算jschl_answer时出错: {e}")
        return None


def qa_country_language_default(country_code):
    mapping = {
        "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", "NZ": "en-NZ",
        "DE": "de-DE", "FR": "fr-FR", "JP": "ja-JP", "SG": "en-SG", "HK": "zh-HK",
        "ID": "id-ID", "CN": "zh-CN", "TW": "zh-TW", "IN": "en-IN"
    }
    return mapping.get((country_code or "US").upper(), "en-US")


def qa_infer_platform_from_ua(user_agent):
    ua = user_agent or ""
    if "Windows" in ua:
        return "Win32"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "MacIntel"
    if "Linux" in ua or "X11" in ua:
        return "Linux x86_64"
    return "Win32"


def qa_log_fingerprint_ip_consistency(ip_info, fingerprint):
    country_code = (ip_info or {}).get("country_code") or "未知"
    expected_tz = get_timezone_for_country(country_code) if country_code != "未知" else None
    expected_lang = qa_country_language_default(country_code)
    actual_lang = fingerprint.get("language")
    actual_tz = fingerprint.get("timezone")
    ua = fingerprint.get("user_agent", "")
    platform = fingerprint.get("platform", "")
    checks = {
        "country": country_code,
        "timezone_expected": expected_tz,
        "timezone_actual": actual_tz,
        "timezone_ok": bool(expected_tz and actual_tz == expected_tz) or bool(actual_tz),
        "language_expected": expected_lang,
        "language_actual": actual_lang,
        "language_prefix_ok": (actual_lang or "").split("-")[0] == expected_lang.split("-")[0],
        "ua_family_ok": ("Chrome/" in ua or "Edg/" in ua or "Chromium/" in ua) and "Firefox/" not in ua and "Version/" not in ua,
        "platform_ok": qa_infer_platform_from_ua(ua) == platform,
        "resolution": fingerprint.get("resolution"),
    }
    log.info(
        f"[QA一致性] country={checks['country']} lang={actual_lang}/{expected_lang} "
        f"tz={actual_tz}/{expected_tz} ua_chromium={checks['ua_family_ok']} "
        f"platform={platform} platform_ok={checks['platform_ok']} resolution={checks['resolution']}"
    )
    return checks


def generate_fingerprint(ip_info):
    """根据 IP 信息生成完全匹配的浏览器指纹。

    严格精准规则（绝不模糊 fallback）：
      1. timezone 必须是标准 IANA 格式（如 America/New_York），否则返回 None（该 IP 不可用）。
      2. language 优先读取传入 ip_info.language（BCP 47 格式，如 en-US）；缺失则用时区反查；否则返回 None。
      3. 返回的 timezone/language 保证和 Playwright 的 context(timezone_id=/locale=) 精确匹配。
    """
    import re as _re

    log.debug(f"生成指纹 - IP信息: {ip_info}")

    if not ip_info or not isinstance(ip_info, dict):
        log.error("❌ ip_info 为空或非 dict，无法生成指纹")
        return None

    # ---- 内部工具函数 ----
    _iana_re = _re.compile(r"^[A-Z][a-zA-Z_]+/[A-Za-z_][A-Za-z0-9_\-+/]*$")

    def _is_iana_tz(s):
        return isinstance(s, str) and bool(_iana_re.match(s)) and " " not in s

    _bcp47_re = _re.compile(r"^[a-z]{2,3}(-[A-Z0-9]{1,8})*(-[A-Za-z0-9]{1,8})*$", _re.I)

    def _is_bcp47(s):
        return isinstance(s, str) and bool(_bcp47_re.match(s)) and len(s) <= 20

    # ---- 1. 时区：必须 IANA，否则失败 ----
    raw_tz = ip_info.get("timezone")
    if not _is_iana_tz(raw_tz):
        log.error(f"❌ 时区='{raw_tz}' 非 IANA 标准格式，该 IP 不可用")
        return None
    ip_timezone = raw_tz

    # 常用时区到语言映射需在语言反查前初始化，避免局部变量未绑定
    timezone_lang_map = {
        "Europe/London": "en-GB", "Europe/Paris": "fr-FR", "Europe/Berlin": "de-DE",
        "America/New_York": "en-US", "America/Chicago": "en-US", "America/Denver": "en-US", "America/Los_Angeles": "en-US",
        "Asia/Shanghai": "zh-CN", "Asia/Tokyo": "ja-JP", "Asia/Singapore": "en-SG", "Asia/Hong_Kong": "zh-HK",
        "Asia/Jakarta": "id-ID", "Australia/Sydney": "en-AU", "Pacific/Auckland": "en-NZ"
    }

    # ---- 2. 语言：优先 ip_info.language，其次时区反查，均失败则返回 None ----
    explicit_lang = ip_info.get("language")
    if explicit_lang and _is_bcp47(explicit_lang):
        ip_language = explicit_lang
    else:
        ip_language = timezone_lang_map.get(ip_timezone)
        if not ip_language or not _is_bcp47(ip_language):
            log.error(
                f"❌ 无法确定语言：ip_info.language='{explicit_lang}'，"
                f"timezone='{ip_timezone}' 未在映射表中，该 IP 不可用"
            )
            return None

    log.debug(f"生成指纹 - 最终时区: {ip_timezone}, 语言: {ip_language}")

    # 分辨率列表
    resolutions = [
        "1920x1080", "1366x768", "1440x900", "1536x864",
        "1600x900", "1280x720", "1280x1024", "1024x768",
        "2560x1440", "3840x2160", "1680x1050", "1920x1200"
    ]
    
    # 屏幕色深列表（常见值）
    color_depths = [24, 32]
    
    # 字体列表（按语言分组）
    fonts_by_language = {
        "zh": ["Noto Sans SC", "Microsoft YaHei", "SimHei", "PingFang SC", "Hiragino Sans GB"],
        "ja": ["Noto Sans JP", "Hiragino Kaku Gothic Pro", "Yu Gothic", "MS Gothic"],
        "ko": ["Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo"],
        "ru": ["Noto Sans SC", "Arial", "Times New Roman", "Georgia"],
        "de": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica"],
        "fr": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica"],
        "es": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica"],
        "pt": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica"],
        "en": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica", "Verdana", "Trebuchet MS"],
        "default": ["Noto Sans", "Arial", "Times New Roman", "Georgia", "Helvetica"]
    }
    
    # 全球时区到语言的精确映射
    timezone_lang_map = {
        "Europe/Belgrade": "sr-RS",
        "Europe/London": "en-GB",
        "Europe/Paris": "fr-FR",
        "Europe/Berlin": "de-DE",
        "Europe/Madrid": "es-ES",
        "Europe/Rome": "it-IT",
        "Europe/Moscow": "ru-RU",
        "Europe/Amsterdam": "nl-NL",
        "Europe/Brussels": "nl-BE",
        "Europe/Vienna": "de-AT",
        "Europe/Zurich": "de-CH",
        "Europe/Stockholm": "sv-SE",
        "Europe/Oslo": "no-NO",
        "Europe/Copenhagen": "da-DK",
        "Europe/Helsinki": "fi-FI",
        "Europe/Warsaw": "pl-PL",
        "Europe/Prague": "cs-CZ",
        "Europe/Budapest": "hu-HU",
        "Europe/Bucharest": "ro-RO",
        "Europe/Sofia": "bg-BG",
        "Europe/Athens": "el-GR",
        "Europe/Lisbon": "pt-PT",
        "Europe/Dublin": "en-IE",
        "America/New_York": "en-US",
        "America/Chicago": "en-US",
        "America/Denver": "en-US",
        "America/Los_Angeles": "en-US",
        "America/Toronto": "en-CA",
        "America/Vancouver": "en-CA",
        "America/Mexico_City": "es-MX",
        "America/Sao_Paulo": "pt-BR",
        "America/Buenos_Aires": "es-AR",
        "America/Santiago": "es-CL",
        "America/Bogota": "es-CO",
        "America/Lima": "es-PE",
        "Asia/Shanghai": "zh-CN",
        "Asia/Tokyo": "ja-JP",
        "Asia/Seoul": "ko-KR",
        "Asia/Bangkok": "th-TH",
        "Asia/Singapore": "en-SG",
        "Asia/Hong_Kong": "zh-HK",
        "Asia/Taipei": "zh-TW",
        "Asia/Kolkata": "en-IN",
        "Asia/Dubai": "en-AE",
        "Asia/Jerusalem": "he-IL",
        "Asia/Tehran": "fa-IR",
        "Asia/Karachi": "ur-PK",
        "Asia/Beirut": "en-US",
        "Asia/Muscat": "en-US",
        "Australia/Sydney": "en-AU",
        "Australia/Melbourne": "en-AU",
        "New Zealand/Auckland": "en-NZ",
        "Africa/Johannesburg": "en-ZA",
        "Africa/Cairo": "ar-EG",
        "Africa/Lagos": "en-NG",
        "America/Fortaleza": "pt-BR"
    }
    
    # 根据语言前缀选择 User-Agent（使用 UA 池管理器）
    lang_prefix = ip_language.split("-")[0]
    user_agent = ua_pool_manager.get_ua(lang_prefix, browser_family="chromium")
    platform = qa_infer_platform_from_ua(user_agent)
    
    # 根据语言选择字体列表
    fonts = fonts_by_language.get(lang_prefix, fonts_by_language["default"])
    
    # 随机选择字体顺序（模拟真实浏览器的字体优先级）
    fonts_shuffled = fonts.copy()
    random.shuffle(fonts_shuffled)
    
    # 真实显卡 vendor/renderer 组合（用于 WebGL UNMASKED_VENDOR/RENDERER，避免返回随机串被识别）
    _gpu_combos = [
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
    ]
    _gpu_vendor, _gpu_renderer = random.choice(_gpu_combos)
    
    return {
        "fingerprint_id": str(uuid.uuid4()),
        "user_agent": user_agent,
        "resolution": random.choice(resolutions),
        "color_depth": random.choice(color_depths),
        "language": ip_language,
        "timezone": ip_timezone,
        "canvas": uuid.uuid4().hex,
        "webgl": uuid.uuid4().hex,
        "webgl_vendor": _gpu_vendor,
        "webgl_renderer": _gpu_renderer,
        "canvas_noise_seed": random.randint(1, 2**31 - 1),
        "fonts": fonts_shuffled,
        "platform": platform,
        "hardware_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16, 32])
    }

def simulate_human_behavior(page, ad_selector, config):
    """模拟真人行为"""
    behavior_stats = {
        "mouse_moves": 0,
        "scrolls": 0,
        "scroll_distance": 0,
        "clicks": 0,
        "waits": 0,
        "focus_switches": 0,
        "refreshes": 0,
        "ad_stay": 0,
        "total_stay": 0,
        "key_presses": 0
    }
    
    start_time = time.time()
    
    # 随机等待页面加载完成
    wait_time = get_random_value(config["page_load_wait"])
    time.sleep(wait_time)
    behavior_stats["waits"] += 1
    behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 真实用户行为模拟：视频观看过程中的交互
    log.info("🎭 开始真实用户行为模拟")
    
    # 初始鼠标位置
    current_x, current_y = random.randint(100, 500), random.randint(100, 300)
    
    # 1. 鼠标移动到视频播放器区域
    log.info("👆 移动鼠标到视频播放器")
    video_player_loc = page.locator("video")
    if video_player_loc.is_visible():
        video_player_loc.hover()
        behavior_stats["mouse_moves"] += 1
        wait_time = random.uniform(0.5, 1.5)
        time.sleep(wait_time)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 2. 模拟点击播放按钮（如果视频未自动播放）
    log.info("▶️ 检查并点击播放按钮")
    play_button = page.locator("button[aria-label='Play'], button[aria-label='播放']")
    if play_button.is_visible():
        play_button.click()
        behavior_stats["clicks"] += 1
        wait_time = random.uniform(0.5, 2)
        time.sleep(wait_time)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 3. 模拟鼠标移动到音量按钮并调整音量
    log.info("🔊 调整音量")
    volume_button = page.locator("button[aria-label*='Volume'], button[title*='音量']")
    if volume_button.is_visible():
        volume_button.hover()
        behavior_stats["mouse_moves"] += 1
        wait_time = random.uniform(0.3, 1)
        time.sleep(wait_time)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
        
        # 随机调整音量
        if random.choice([True, False]):
            volume_adjust = random.uniform(-0.3, 0.3)
            page.evaluate(f"document.querySelector('video').volume = Math.max(0, Math.min(1, document.querySelector('video').volume + {volume_adjust}))")
            wait_time = random.uniform(0.2, 0.8)
            time.sleep(wait_time)
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 4. 模拟页面滚动（观看过程中可能会滚动页面）
    log.info("📜 模拟页面滚动")
    scroll_count = random.randint(0, 2)  # 随机滚动0-2次
    for _ in range(scroll_count):
        scroll_amount = random.randint(-200, 200)
        page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        behavior_stats["scrolls"] += 1
        behavior_stats["scroll_distance"] += abs(scroll_amount)
        wait_time = random.uniform(0.5, 1.5)
        time.sleep(wait_time)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 5. 模拟鼠标移动到随机位置（使用贝塞尔曲线）
    log.info("🖱️ 随机移动鼠标")
    mouse_move_count = random.randint(2, 5)
    for _ in range(mouse_move_count):
        target_x = random.randint(100, 1800)
        target_y = random.randint(100, 900)
        
        # 使用贝塞尔曲线移动鼠标
        human_mouse_move(page, current_x, current_y, target_x, target_y, config)
        
        current_x, current_y = target_x, target_y
        behavior_stats["mouse_moves"] += 1
        
        move_wait = random.uniform(0.5, 1.5)
        time.sleep(move_wait)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(move_wait * 1000)  # 转换为毫秒
    
    # 6. 模拟暂停和播放视频（随机）
    log.info("⏸️ 模拟暂停/播放")
    if random.choice([True, False]):
        page.evaluate("document.querySelector('video').pause()")
        wait_time1 = random.uniform(2, 5)
        time.sleep(wait_time1)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time1 * 1000)  # 转换为毫秒
        
        page.evaluate("document.querySelector('video').play()")
        wait_time2 = random.uniform(0.5, 1.5)
        time.sleep(wait_time2)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time2 * 1000)  # 转换为毫秒
    
    log.info("✅ 真实用户行为模拟完成")
    
    # 检查广告并模拟停留
    ad_elements = page.query_selector_all(ad_selector)
    if ad_elements:
        ad_element = random.choice(ad_elements)
        try:
            # === 自然滚动到广告位置（渐进式，模拟用户边读内容边往下翻） ===
            try:
                ad_box_raw = ad_element.bounding_box()
                if ad_box_raw:
                    # 分 2~4 次渐进滚动到广告附近（而不是一步到位）
                    _scroll_steps = random.randint(2, 4)
                    for _si in range(_scroll_steps):
                        _step_px = random.randint(150, 450)
                        page.evaluate(f"window.scrollBy(0, {_step_px})")
                        behavior_stats["scrolls"] += 1
                        behavior_stats["scroll_distance"] += _step_px
                        # 每次滚动后“阅读”停顿
                        _read_pause = random.uniform(0.8, 2.5)
                        time.sleep(_read_pause)
                        behavior_stats["waits"] += 1
                        behavior_stats["total_stay"] += int(_read_pause * 1000)
                    # 最后确保广告可见
                    ad_element.scroll_into_view_if_needed()
                    behavior_stats["scrolls"] += 1
            except Exception:
                ad_element.scroll_into_view_if_needed()
                behavior_stats["scrolls"] += 1
            
            # 获取广告元素的中心位置
            box = ad_element.bounding_box()
            if box:
                # 不精确瞄准中心，添加随机偏移（模拟人类视觉焦点不精确）
                ad_center_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                ad_center_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                
                # 使用贝塞尔曲线移动到广告附近
                human_mouse_move(page, current_x, current_y, ad_center_x, ad_center_y, config)
                current_x, current_y = ad_center_x, ad_center_y
                
                # 微观犹豫：人类看到广告后会有短暂停顿（0.3~1.2s）
                _hesitation = random.uniform(0.3, 1.2)
                time.sleep(_hesitation)
                behavior_stats["total_stay"] += int(_hesitation * 1000)
            
            # 在广告区域停留
            ad_stay_time = get_random_value(config["ad_stay_time"])
            time.sleep(ad_stay_time)
            behavior_stats["ad_stay"] = int(ad_stay_time * 1000)  # 转换为毫秒
            behavior_stats["total_stay"] += int(ad_stay_time * 1000)
            
            # 模拟点击广告的概率
            ad_click_prob = get_random_value(config["ad_click_prob"])
            if random.random() < ad_click_prob:
                # 单日点击上限校验（跨任务/跨会话持久化）
                if daily_ad_click_limit_reached():
                    log.warning(f"🚫 今日广告点击已达上限({config.get('daily_ad_click_limit')})，本次跳过点击")
                    raise StopIteration
                # 记录点击前的标签页句柄，用于检测广告落地页新标签
                try:
                    _driver = getattr(page, "driver", None)
                    _handles_before = list(_driver.window_handles) if _driver else []
                    _main_handle = _driver.current_window_handle if _driver else None
                except Exception:
                    _driver, _handles_before, _main_handle = None, [], None

                # 鼠标已通过贝塞尔曲线移动到广告中心，用 CDP 真实鼠标点击（失败降级元素 click）
                try:
                    page.mouse.click(ad_center_x, ad_center_y)
                except Exception:
                    ad_element.click(force=True)
                behavior_stats["clicks"] += 1
                _today_clicks = record_ad_click(1)
                log.info(f"🖱️ 广告点击已记录（今日累计 {_today_clicks} 次）")
                
                ad_click_wait = get_random_value(config["ad_click_wait"])
                time.sleep(ad_click_wait)
                behavior_stats["waits"] += 1
                behavior_stats["total_stay"] += int(ad_click_wait * 1000)

                # ========== 广告点击后落地页真人行为（切换到新标签→停留→滚动→返回） ==========
                try:
                    if _driver is not None:
                        _handles_after = list(_driver.window_handles)
                        _new_handles = [h for h in _handles_after if h not in _handles_before]
                        if _new_handles:
                            _landing = _new_handles[-1]
                            _driver.switch_to.window(_landing)
                            log.info("🛬 广告落地页已打开，开始真人浏览落地页")
                            # 等待落地页加载
                            _lp_load = get_random_value(config.get("page_load_wait", {"min": 2, "max": 5}))
                            time.sleep(_lp_load)
                            # 落地页滚动浏览（2~4 次）
                            _lp_scrolls = random.randint(2, 4)
                            for _i in range(_lp_scrolls):
                                try:
                                    _dist = random.randint(300, 900)
                                    _driver.execute_script(f"window.scrollBy(0, {_dist});")
                                    behavior_stats["scrolls"] += 1
                                    behavior_stats["scroll_distance"] += _dist
                                    time.sleep(get_random_value(config.get("scroll_wait", {"min": 1, "max": 3})))
                                except Exception:
                                    break
                            # 落地页停留
                            _lp_stay = get_random_value(config.get("ad_landing_stay_time", {"min": 5, "max": 15}))
                            time.sleep(_lp_stay)
                            behavior_stats["total_stay"] += int((_lp_load + _lp_stay) * 1000)
                            log.info(f"🛬 落地页浏览完成（停留≈{_lp_load + _lp_stay:.1f}s，滚动{_lp_scrolls}次），关闭并返回原站")
                            # 关闭落地页标签，返回原标签
                            try:
                                _driver.close()
                            except Exception:
                                pass
                            try:
                                _driver.switch_to.window(_main_handle or _handles_before[0])
                            except Exception:
                                if _driver.window_handles:
                                    _driver.switch_to.window(_driver.window_handles[0])
                except Exception as _lp_err:
                    log.debug(f"落地页行为处理异常（忽略）: {type(_lp_err).__name__}: {str(_lp_err)[:80]}")
        except Exception:
            pass
    
    # 随机点击页面其他位置（使用贝塞尔曲线移动过去）
    random_click_count = get_random_int(config["random_click_count"])
    for _ in range(random_click_count):
        try:
            target_x = random.randint(100, 1800)
            target_y = random.randint(100, 900)
            
            # 使用贝塞尔曲线移动到目标位置
            human_mouse_move(page, current_x, current_y, target_x, target_y, config)
            current_x, current_y = target_x, target_y
            
            page.mouse.click(target_x, target_y)
            behavior_stats["clicks"] += 1
            
            random_wait = get_random_value(config["random_click_wait"])
            time.sleep(random_wait)
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(random_wait * 1000)
        except Exception:
            pass
    
    # 总停留时间
    total_stay = get_random_value(config["total_stay"])
    remaining_time = total_stay - (time.time() - start_time)
    if remaining_time > 0:
        time.sleep(remaining_time)
        behavior_stats["total_stay"] += int(remaining_time * 1000)
    
    return behavior_stats

def enhance_video_player(page, video_config):
    """
    注入视频播放增强脚本
    功能：倍速播放、最高清晰度、跳过片头片尾
    
    Args:
        page: Playwright 页面对象
        video_config: 视频配置字典，包含 skip_intro, skip_outro, playback_rate_min, playback_rate_max
    """
    log.info("🎬 注入视频播放增强脚本...")
    
    # 从配置中获取参数
    skip_intro = video_config.get('skip_intro', 20)
    skip_outro = video_config.get('skip_outro', 30)
    playback_rate_min = video_config.get('playback_rate_min', 1.2)
    playback_rate_max = video_config.get('playback_rate_max', 1.5)
    
    # 使用普通字符串拼接，避免 f-string 与 JavaScript 花括号冲突
    enhance_script = """
    (function() {
        console.log('视频增强脚本启动');
        
        // 等待视频元素加载
        const waitForVideo = setInterval(() => {
            const video = document.querySelector('video');
            if (video) {
                clearInterval(waitForVideo);
                enhanceVideo(video);
            }
        }, 500);
        
        function enhanceVideo(video) {
            console.log('找到视频元素，开始增强');
            
            // 1. 设置倍速播放 (在配置范围内随机)
            const playbackRate = PLAYBACK_RATE_MIN + Math.random() * (PLAYBACK_RATE_MAX - PLAYBACK_RATE_MIN);
            video.playbackRate = playbackRate;
            console.log('设置播放倍速:', playbackRate);
            
            // 2. 尝试设置最高清晰度
            trySetHighestQuality(video);
            
            // 3. 跳过片头和片尾
            const skipIntro = SKIP_INTRO;  // 跳过前N秒
            const skipOutro = SKIP_OUTRO;  // 跳过后N秒
            
            let introSkipped = false;
            
            video.addEventListener('loadedmetadata', function() {
                console.log('视频元数据加载完成，时长:', video.duration);
                
                // 跳过片头
                if (!introSkipped && video.duration > skipIntro) {
                    setTimeout(() => {
                        video.currentTime = skipIntro;
                        console.log('已跳过片头', skipIntro, '秒');
                        introSkipped = true;
                    }, 1000);
                }
            });
            
            // 监听时间更新，处理片尾
            video.addEventListener('timeupdate', function() {
                if (video.duration && video.currentTime > video.duration - skipOutro - 5) {
                    // 快到结尾了，标记一下
                    console.log('即将到达片尾');
                }
            });
        }
        
        function trySetHighestQuality(video) {
            // 尝试多种常见的清晰度选择方式
            console.log('尝试选择最高清晰度...');
            
            // 方式1: YouTube-style
            try {
                const qualityButtons = document.querySelectorAll('.ytp-menuitem, [class*="quality"], [class*="hd"], [class*="1080"], [class*="720"]');
                if (qualityButtons.length > 0) {
                    // 优先选最高清的关键词
                    const priorities = ['4k', '2160', '1440', '1080', '720', '480', '360'];
                    let selected = false;
                    for (const p of priorities) {
                        for (const btn of qualityButtons) {
                            const text = btn.textContent.toLowerCase();
                            if (text.includes(p)) {
                                btn.click();
                                console.log('已选择清晰度:', p);
                                selected = true;
                                break;
                            }
                        }
                        if (selected) break;
                    }
                }
            } catch(e) {
                console.log('清晰度选择方式1失败', e);
            }
            
            // 方式2: 通过video元素的videoHeight/videoWidth
            try {
                console.log('当前视频尺寸:', video.videoWidth, 'x', video.videoHeight);
            } catch(e) {}
        }
    })();
    """
    # 替换占位符
    enhance_script = enhance_script.replace('SKIP_INTRO', str(skip_intro))
    enhance_script = enhance_script.replace('SKIP_OUTRO', str(skip_outro))
    enhance_script = enhance_script.replace('PLAYBACK_RATE_MIN', str(playback_rate_min))
    enhance_script = enhance_script.replace('PLAYBACK_RATE_MAX', str(playback_rate_max))
    
    try:
        page.evaluate(enhance_script)
        log.info("✓ 视频增强脚本注入成功")
    except Exception as e:
        log.warning(f"视频增强脚本注入失败: {str(e)}")


def is_udis_video_url(url):
    """
    判断是否是udis或vids.st视频链接
    
    Args:
        url: 视频链接
    
    Returns:
        bool: 是否是udis或vids.st视频链接
    """
    if not url:
        return False
    url_str = url.lower()
    return "udis" in url_str or "udisxxx" in url_str or "vids.st" in url_str or "upbolt.to" in url_str


def navigate_vids_st_to_video(page, config, video_url):
    """
    从vids.st主页导航到指定视频页面
    
    Args:
        page: Playwright页面对象
        config: 配置字典
        video_url: 目标视频URL
    
    Returns:
        bool: 导航是否成功
    """
    log.info("🔍 开始从vids.st主页导航到视频页面")
    
    try:
        # 访问主页
        page.goto("https://vids.st", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        
        # 查找导航菜单或视频列表
        # 查找视频链接或相关导航
        if page.query_selector('a[href*="/videos"]'):
            page.click('a[href*="/videos"]')
            log.info("✅ 点击视频导航菜单")
            page.wait_for_load_state("networkidle", timeout=30000)
            
            # 在视频列表中查找目标视频
            # 这里只是一个示例，实际需要根据网站结构调整
            video_links = page.query_selector_all('a[href*="/v/"]')
            if video_links:
                log.info(f"✅ 在视频列表中找到 {len(video_links)} 个视频")
                # 可以尝试点击与目标视频相关的链接
                # 或者直接跳转到目标视频页面
                page.goto(video_url, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                log.info("✅ 成功导航到视频页面")
                return True
            else:
                log.warning("⚠️ 在视频列表中未找到视频")
                # 直接访问视频页面
                page.goto(video_url, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                return True
        else:
            log.warning("⚠️ 未找到视频导航菜单，直接访问视频页面")
            page.goto(video_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            return True
            
    except Exception as e:
        log.error(f"❌ 导航到视频页面失败: {e}")
        return False


def simulate_vids_st_login(page, config):
    """
    模拟vids.st网站登录过程
    
    Args:
        page: Playwright页面对象
        config: 配置字典
    
    Returns:
        bool: 登录是否成功
    """
    log.info("🔐 开始模拟vids.st登录过程")
    
    # 尝试访问登录页面
    try:
        page.goto("https://vids.st/login", timeout=60000)
        log.info("✅ 成功访问登录页面")
    except Exception as e:
        log.error(f"❌ 访问登录页面失败: {e}")
        return False
    
    # 等待页面加载
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        log.error(f"❌ 登录页面加载超时: {e}")
        return False
    
    # 尝试查找登录表单
    try:
        # 查找用户名和密码输入框
        # 注意：这只是一个示例，实际的选择器需要根据网站实际情况调整
        if page.query_selector('input[name="email"]') and page.query_selector('input[name="password"]'):
            log.info("✅ 找到登录表单")
            
            # 输入模拟的用户名和密码
            page.fill('input[name="email"]', "testuser@example.com")
            page.fill('input[name="password"]', "password123")
            
            # 点击登录按钮
            if page.query_selector('button[type="submit"]'):
                page.click('button[type="submit"]')
                log.info("✅ 点击登录按钮")
                
                # 等待登录完成
                page.wait_for_load_state("networkidle", timeout=30000)
                
                # 检查是否登录成功（通过检查是否有用户信息或仪表盘）
                if page.query_selector('a[href="/dashboard"]') or page.query_selector('a[href="/profile"]'):
                    log.info("✅ 登录成功")
                    return True
                else:
                    log.warning("⚠️ 可能登录失败，未找到用户信息")
                    return False
            else:
                log.warning("⚠️ 未找到登录按钮")
                return False
        else:
            log.warning("⚠️ 未找到登录表单，可能已经登录")
            return True  # 可能已经登录
        
    except Exception as e:
        log.error(f"❌ 登录过程失败: {e}")
        return False


def convert_udis_video_url(video_url, config, current_task=None):
    """
    阶段3：视频 URL 不再拼接 HTTP 代理地址。
    浏览器数据面统一通过 IPDeep HTTP 代理出网，因此视频链接必须保持原始 URL。
    """
    log.info("IPDeep 代理模式：视频URL保持原始地址，由浏览器代理统一出网")
    return video_url


def watch_video_ad(page, video_url, config, current_x, current_y, referer_url=None):
    """
    观看视频广告，模拟真人行为（支持udis视频混合中转方案）
    
    返回: (actual_watch_time, new_x, new_y)
    """
    log.info(f"========== 开始视频广告观看 ==========")
    log.debug(f"🎬 视频参数: 视频URL={video_url}, 当前坐标=({current_x},{current_y}), Referer={referer_url}")
    
    # 处理视频链接替换（支持udis视频混合中转方案）
    original_video_url = video_url
    if is_udis_video_url(video_url):
        # 尝试从当前任务信息中获取代理信息（通过config.get('current_task')）
        current_task = config.get('current_task', None)
        video_url = convert_udis_video_url(video_url, config, current_task)
        log.info(f"阶段3：udis视频链接保持原始URL，由浏览器SOCKS5代理统一出网: {video_url}")
    
    log.info(f"视频URL: {video_url}")
    
    # 真人行为统计 - 一开始就初始化，确保有数据
    behavior_stats = {
        "mouse_moves": 0,
        "scrolls": 0,
        "scroll_distance": 0,
        "clicks": 0,
        "waits": 0,
        "focus_switches": 0,
        "refreshes": 0,
        "ad_stay": 0,
        "total_stay": 0,
        "key_presses": 0
    }
    
    actual_watch_time = 0
    
    try:
        if not task_running:
            log.warning("⛔ 任务已停止，视频广告观看已取消")
            return 0, current_x, current_y, behavior_stats
            
        # ==================== 第一步：如果是vids.st，先尝试登录 ====================
        if "vids.st" in original_video_url:
            login_success = simulate_vids_st_login(page, config)
            if not login_success:
                log.warning("⚠️ 登录失败，但继续尝试访问视频页面")
        
        # ==================== 第二步：从主页导航到视频页面 ====================
        if "vids.st" in original_video_url:
            navigate_success = navigate_vids_st_to_video(page, config, video_url)
            if not navigate_success:
                log.warning("⚠️ 导航失败，但继续尝试直接访问视频页面")
                page.goto(video_url, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
        else:
            log.info(f"正在访问视频页面...")
            page.goto(video_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
        
        # 设置视频请求的Referer
        final_referer = referer_url
        
        # 对于udis视频，使用当前任务已选择的Referer；若没有则按任务序号0取列表首项
        if is_udis_video_url(original_video_url):
            final_referer = config.get('current_video_referer') or select_video_referer_for_task(config, 0)[0]
            log.info(f"🎯 udis视频，使用当前任务Referer: {final_referer}")
        else:
            # 非udis视频，如果没有传入referer，使用当前任务Referer或列表首项
            if not final_referer:
                final_referer = config.get('current_video_referer') or select_video_referer_for_task(config, 0)[0]
            log.info(f"📋 使用Referer: {final_referer}")
        
        # ==================== 第一步：Cloudflare 验证绕过 ====================
        log.info("🛡️ 开始 Cloudflare 验证绕过...")
        try:
            # 访问目标页面，等待验证加载（使用更宽松的超时设置）
            response = page.goto(video_url, timeout=180000, wait_until="domcontentloaded", referer=final_referer)
            page.wait_for_load_state("networkidle", timeout=90000)
            
            # 检查是否存在Cloudflare验证挑战
            if is_cloudflare_challenge(page):
                log.info("🔐 检测到Cloudflare验证挑战，开始处理...")
                if not solve_cloudflare_challenge(page):
                    log.error("❌ Cloudflare验证挑战未通过")
                    return 0, current_x, current_y, behavior_stats
            
            # 随机滚动、鼠标移动，模拟真实行为
            page.evaluate("""
                window.scrollTo({top: Math.random() * 300, behavior: 'smooth'});
            """)
            wait_time = random.randint(1000, 3000) / 1000
            if not video_interruptible_sleep(wait_time):
                log.warning("⛔ 任务已停止（滚动等待中）")
                return 0, current_x, current_y, behavior_stats
            
            # 等待验证完成（页面跳转到视频内容）
            try:
                # 等待视频播放器元素出现（增加超时时间）
                page.wait_for_selector("video", timeout=60000)
                log.info("✅ Cloudflare 验证通过，视频已加载")
            except Exception as e:
                log.warning(f"⚠️ 等待视频播放器失败，但页面已加载完成: {e}")
        except Exception as e:
            log.error(f"❌ Cloudflare 验证失败: {e}")
            
            # 尝试直接访问原始URL而不进行Cloudflare验证
            log.info("🔄 尝试直接访问原始URL，不进行Cloudflare验证...")
            try:
                response = page.goto(video_url, timeout=180000, wait_until="domcontentloaded", referer=final_referer)
                page.wait_for_load_state("networkidle", timeout=90000)
                
                # 检查是否加载了视频播放器
                try:
                    page.wait_for_selector("video", timeout=60000)
                    log.info("✅ 直接访问成功，视频已加载")
                except Exception as e2:
                    log.error(f"❌ 直接访问也失败: {e2}")
                    return 0, current_x, current_y, behavior_stats
            except Exception as e2:
                log.error(f"❌ 直接访问失败: {e2}")
                return 0, current_x, current_y, behavior_stats
        
        # ==================== 第二步：真实用户交互 - 页面滚动 ====================
        log.info("🎭 模拟真实用户页面滚动...")
        scroll_amount = random.randint(0, 500)
        page.evaluate(f"""
            window.scrollTo({{
                top: {scroll_amount},
                behavior: 'smooth'
            }});
        """)
        behavior_stats["scrolls"] += 1
        behavior_stats["scroll_distance"] += scroll_amount
        scroll_wait = random.randint(1000, 3000)
        if not video_interruptible_sleep(scroll_wait / 1000):
            log.warning("⛔ 任务已停止（滚动等待中）")
            return 0, current_x, current_y, behavior_stats
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += scroll_wait
        
        # ==================== 第三步：真实用户交互 - 鼠标移动到视频播放器 ====================
        log.info("🎭 模拟鼠标移动到视频播放器...")
        try:
            player = page.locator("video")
            player.hover()
            behavior_stats["mouse_moves"] += 1
            hover_wait = random.randint(500, 1500)
            if not video_interruptible_sleep(hover_wait / 1000):
                log.warning("⛔ 任务已停止（悬停等待中）")
                return 0, current_x, current_y, behavior_stats
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += hover_wait
        except Exception as e:
            log.debug(f"视频播放器定位失败: {str(e)}")
        
        # ==================== 第四步：真实用户交互 - 点击播放按钮（如果有） ====================
        log.info("🎭 检查并点击播放按钮...")
        play_buttons = [
            "button[aria-label='Play']", 
            "button[aria-label*='播放']",
            "[class*='play-button']",
            "[class*='play-btn']"
        ]
        
        play_button_found = False
        for selector in play_buttons:
            try:
                if page.locator(selector).is_visible():
                    page.click(selector)
                    behavior_stats["clicks"] += 1
                    play_button_found = True
                    log.info(f"✅ 点击播放按钮: {selector}")
                    click_wait = random.randint(500, 1500)
                    if not video_interruptible_sleep(click_wait / 1000):
                        log.warning("⛔ 任务已停止（点击等待中）")
                        return 0, current_x, current_y, behavior_stats
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += click_wait
                    break
            except Exception as e:
                log.debug(f"点击播放按钮失败 {selector}: {str(e)}")
        
        if not play_button_found:
            log.info("📺 未找到播放按钮，视频可能自动播放")
        
        # 尝试访问页面（增加重试机制）
        page_loaded = False
        max_retries = 2
        
        for attempt in range(max_retries + 1):
            try:
                log.info(f"🔄 尝试访问视频页面 (第 {attempt + 1}/{max_retries + 1} 次)...")
                
                # 检查当前URL和准备访问的URL
                log.info(f"📍 当前页面URL: {page_url_safe(page, '未知')}")
                log.info(f"🎯 目标视频URL: {video_url}")
                log.info(f"🔗 Referer: {final_referer}")
                
                # 尝试访问页面
                response = page.goto(video_url, timeout=60000, wait_until="domcontentloaded", referer=final_referer)
                
                # 检查响应状态
                if response:
                    status = response.status
                    log.info(f"📊 HTTP响应状态码: {status}")
                    if status >= 400:
                        log.warning(f"⚠️ HTTP错误: {status}")
                
                log.info(f"✓ 视频页面加载完成")
                page_loaded = True
                break
                
            except Exception as e:
                log.error(f"✗ 页面加载失败 (尝试 {attempt + 1}): {str(e)}")
                if attempt < max_retries:
                    log.info(f"⏳ 等待2秒后重试...")
                    if not video_interruptible_sleep(2):
                        log.warning("⛔ 任务已停止（重试等待中）")
                        return 0, current_x, current_y, behavior_stats
        
        if not page_loaded:
            log.error(f"❌ 所有尝试都失败了，跳过此视频")
            return (0, current_x, current_y, behavior_stats)
        
        # 等一会儿，确保页面稳定
        wait_time = random.uniform(3.0, 5.0)
        log.info(f"⏱️ 等待 {wait_time:.1f} 秒让页面稳定...")
        if not video_interruptible_sleep(wait_time):
            log.warning("⛔ 任务已停止（页面稳定等待中）")
            return 0, current_x, current_y, behavior_stats
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)
        
        # 检查页面标题和内容
        try:
            page_title = page_title_safe(page, "")
            if page_title:
                log.info(f"📄 页面标题: {page_title[:200]}")
            
            # 检查页面内容是否为空或错误页
            page_content = page_content_safe(page, "")
            if page_content and isinstance(page_content, str):
                if "403" in page_content or "Forbidden" in page_content:
                    log.error("❌ 页面返回403 Forbidden，可能被封禁")
                elif "404" in page_content or "Not Found" in page_content:
                    log.error("❌ 页面返回404 Not Found")
                elif len(page_content) < 1000:
                    log.warning(f"⚠️ 页面内容过短 ({len(page_content)}字符)，可能是错误页")
        except Exception as e:
            log.debug(f"获取页面信息失败: {str(e)}")
        
        # 先检查一下有没有video元素
        try:
            has_video = page_eval(
                page,
                "() => { const v = document.querySelector('video'); return !!v; }",
                default=False,
            )
            log.info(f"页面上有video元素吗: {'是' if has_video else '否'}")
            
            # 如果没有video元素，尝试查找iframe中的视频
            if not has_video:
                has_iframe = page_eval(
                    page,
                    "() => { const v = document.querySelector('iframe'); return !!v; }",
                    default=False,
                )
                log.info(f"页面上有iframe元素吗: {'是' if has_iframe else '否'}")
        except Exception as e:
            log.debug(f"检查video元素失败: {str(e)}")
        
        if not task_running:
            log.warning("⛔ 任务已停止，视频播放已取消")
            return 0, current_x, current_y, behavior_stats
            
        # ==================== 第二步：尝试播放视频（完整版） ====================
        video_is_playing = False
        
        # 增加初始的滚动和移动，确保有统计数据
        for i in range(3):
            try:
                scroll_amount = 50 + int(random.random() * 150)
                page.evaluate("window.scrollBy({ top: " + str(scroll_amount) + ", behavior: 'smooth' })")
                behavior_stats["scrolls"] += 1
                behavior_stats["scroll_distance"] += scroll_amount
                if not video_interruptible_sleep(random.uniform(0.3, 0.7)):
                    log.warning("⛔ 任务已停止（滚动等待中）")
                    return 0, current_x, current_y, behavior_stats
            except Exception as e:
                pass
        
        for i in range(2):
            try:
                target_x = 100 + random.random() * 1600
                target_y = 100 + random.random() * 750
                human_mouse_move(page, current_x, current_y, target_x, target_y, config)
                current_x, current_y = target_x, target_y
                behavior_stats["mouse_moves"] += 1
                if not video_interruptible_sleep(random.uniform(0.3, 0.6)):
                    log.warning("⛔ 任务已停止（鼠标移动等待中）")
                    return 0, current_x, current_y, behavior_stats
            except Exception as e:
                pass
        
        # 方法1: 查找并点击播放按钮（尝试多种常见的播放按钮选择器）
        play_buttons = [
            'button[class*="play"], [class*="play-button"], [class*="play-btn"]',
            'div[class*="play"], [class*="play-button"], [class*="play-btn"]',
            '[aria-label*="play"], [title*="play"]',
            'svg[class*="play"], [class*="play-icon"]',
            '.vjs-big-play-button'
        ]
        
        for selector in play_buttons:
            try:
                buttons = page.query_selector_all(selector)
                if buttons:
                    log.info(f"找到了播放按钮: {selector}")
                    # 尝试点击第一个可见的播放按钮
                    for btn in buttons:
                        try:
                            # 检查元素是否可见
                            is_visible = page.evaluate("""
                                (el) => {
                                    const rect = el.getBoundingClientRect();
                                    return rect.width > 0 && rect.height > 0 && el.offsetParent !== null;
                                }
                            """, btn)
                            
                            if is_visible:
                                # 滚动到按钮可见区域
                                btn.scroll_into_view_if_needed()
                                if not video_interruptible_sleep(random.uniform(0.5, 1.0)):
                                    log.warning("⛔ 任务已停止（滚动到可见区域等待中）")
                                    return 0, current_x, current_y, behavior_stats
                                
                                # 获取按钮位置并点击
                                bbox = btn.bounding_box()
                                if bbox:
                                    click_x = bbox['x'] + bbox['width'] / 2
                                    click_y = bbox['y'] + bbox['height'] / 2
                                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                                    current_x, current_y = click_x, click_y
                                    if not video_interruptible_sleep(random.uniform(0.5, 1.0)):
                                        log.warning("⛔ 任务已停止（点击前等待中）")
                                        return 0, current_x, current_y, behavior_stats
                                    btn.click()
                                    behavior_stats["clicks"] += 1
                                    log.info(f"✅ 点击播放按钮成功！")
                                    # 等待一下让播放开始
                                    wait_time = random.uniform(2.0, 3.0)
                                    if not video_interruptible_sleep(wait_time):
                                        log.warning("⛔ 任务已停止（播放开始等待中）")
                                        return 0, current_x, current_y, behavior_stats
                                    behavior_stats["waits"] += 1
                                    behavior_stats["total_stay"] += int(wait_time * 1000)
                                    break
                        except Exception as e:
                            log.debug(f"点击按钮失败: {str(e)}")
                            continue
                    if video_is_playing:
                        break
            except Exception as e:
                log.debug(f"查找播放按钮失败: {str(e)}")
                continue
        
        # 方法2: 如果没找到播放按钮，直接用JS尝试播放
        if not video_is_playing:
            log.info(f"未找到播放按钮，尝试直接播放视频...")
            try:
                page.evaluate("""
                    () => {
                        const video = document.querySelector('video');
                        if (video) {
                            console.log('找到video元素', video);
                            video.muted = true;
                            video.playsInline = true;
                            
                            const playPromise = video.play();
                            if (playPromise !== undefined) {
                                playPromise.then(() => {
                                    console.log('播放成功！');
                                }).catch(err => {
                                    console.log('播放失败:', err);
                                });
                            }
                            
                            return true;
                        } else {
                            console.log('没有找到video元素');
                            return false;
                        }
                    }
                """)
                log.info(f"✓ 发送了播放命令")
            except Exception as e:
                log.debug(f"JS播放执行异常: {str(e)}")
        
        # 等一下
        wait_time = random.uniform(3.0, 5.0)
        if not video_interruptible_sleep(wait_time):
            log.warning("⛔ 任务已停止（播放检查等待中）")
            return 0, current_x, current_y, behavior_stats
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)
        
        # 检查播放状态
        try:
            play_result = page.evaluate("""
                () => {
                    const video = document.querySelector('video');
                    if (!video) {
                        return { playing: false, time: 0, hasVideo: false };
                    }
                    
                    const playing = !video.paused && !video.ended && video.currentTime > 0;
                    
                    return {
                        playing: playing,
                        paused: video.paused,
                        ended: video.ended,
                        time: video.currentTime,
                        duration: video.duration,
                        hasVideo: true
                    };
                }
            """)
            
            log.info(f"播放检查结果: {play_result}")
            
            if play_result.get("playing") or play_result.get("time", 0) > 0:
                log.info(f"✅ 视频在播放！")
                video_is_playing = True
            else:
                log.warning(f"⚠️ 视频没播放，不过继续观看流程...")
                
        except Exception as e:
            log.debug(f"检查播放状态异常: {str(e)}")
        
        if not task_running:
            log.warning("⛔ 任务已停止，观看流程已取消")
            return 0, current_x, current_y, behavior_stats
            
        # ==================== 第三步：观看流程（优化版 - 防止卡死） ====================
        log.info(f"🎭 开始观看流程（优化版 - 防止卡死）...")
        
        # 获取视频观看时间（严格按照配置执行）
        video_ad_cfg = config.get("video_ad", {})
        min_time = max(video_ad_cfg.get("min_watch_time", 30), 15)
        max_time = video_ad_cfg.get("max_watch_time", 60)
        
        # 确保最大值不超过配置值
        if max_time > 60:
            max_time = 60
            log.warning(f"⚠️ 配置的最大观看时间超过限制，已调整为 60 秒")
        
        watch_time = random.uniform(min_time, max_time)
        log.info(f"📋 配置值 - 最小: {min_time}秒, 最大: {max_time}秒, 实际: {watch_time:.1f}秒")
        
        log.info(f"⏱️ 计划观看时间: {watch_time:.1f} 秒")
        
        # 开始计时
        watch_start = time.time()
        elapsed = 0
        last_log = 0
        
        # 简化的行为模拟（避免卡死）
        # 根据观看时间动态调整行为次数
        max_mouse_moves = max(2, int(watch_time / 5))  # 每5秒最多一次鼠标移动
        max_scrolls = max(1, int(watch_time / 10))       # 每10秒最多一次滚动
        max_clicks = max(1, int(watch_time / 15))        # 每15秒最多一次点击
        
        mouse_move_wait = config["mouse_move_wait"]
        scroll_pixels = config["scroll_pixels"]
        scroll_wait = config["scroll_wait"]
        
        log.info(f"📋 行为模拟参数: 鼠标移动最多{max_mouse_moves}次, 滚动最多{max_scrolls}次, 点击最多{max_clicks}次")
        
        # 阶段1: 模拟鼠标移动（简化版）
        mouse_move_count = min(random.randint(2, 8), max_mouse_moves)
        log.info(f"🖱️ 阶段1: 鼠标移动 {mouse_move_count} 次")
        for _ in range(mouse_move_count):
            if elapsed >= watch_time:
                break
                
            target_x = random.randint(100, page.viewport_size.get('width', 1920) - 100)
            target_y = random.randint(100, page.viewport_size.get('height', 1080) - 100)
            
            # 使用简化的线性移动（避免贝塞尔曲线计算过多）
            page.mouse.move(target_x, target_y, steps=random.randint(10, 20))
            current_x, current_y = target_x, target_y
            behavior_stats["mouse_moves"] += 1
            
            move_wait = random.uniform(0.5, 1.5)
            sleep_time = min(move_wait, watch_time - elapsed)
            if not video_interruptible_sleep(sleep_time):
                log.warning("⛔ 任务已停止（鼠标移动等待中）")
                return 0, current_x, current_y, behavior_stats
            elapsed += sleep_time
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
        
        # 阶段2: 模拟页面滚动
        scroll_count = min(random.randint(1, 4), max_scrolls)
        log.info(f"📜 阶段2: 页面滚动 {scroll_count} 次")
        for _ in range(scroll_count):
            if elapsed >= watch_time:
                break
                
            scroll_amount = random.randint(100, 400)
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            behavior_stats["scrolls"] += 1
            behavior_stats["scroll_distance"] += scroll_amount
            
            scroll_wait_time = random.uniform(1, 3)
            sleep_time = min(scroll_wait_time, watch_time - elapsed)
            if not video_interruptible_sleep(sleep_time):
                log.warning("⛔ 任务已停止（滚动等待中）")
                return 0, current_x, current_y, behavior_stats
            elapsed += sleep_time
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
        
        # 阶段3: 随机点击页面
        click_count = min(random.randint(1, 3), max_clicks)
        log.info(f"👆 阶段3: 随机点击 {click_count} 次")
        for _ in range(click_count):
            if elapsed >= watch_time:
                break
                
            try:
                target_x = random.randint(100, page.viewport_size.get('width', 1920) - 100)
                target_y = random.randint(100, page.viewport_size.get('height', 1080) - 100)
                
                page.mouse.move(target_x, target_y, steps=random.randint(5, 10))
                page.mouse.click(target_x, target_y)
                behavior_stats["clicks"] += 1
                
                click_wait_time = random.uniform(1, 2)
                sleep_time = min(click_wait_time, watch_time - elapsed)
                if not video_interruptible_sleep(sleep_time):
                    log.warning("⛔ 任务已停止（点击等待中）")
                    return 0, current_x, current_y, behavior_stats
                elapsed += sleep_time
                behavior_stats["waits"] += 1
                behavior_stats["total_stay"] += int(sleep_time * 1000)
            except Exception as e:
                log.debug(f"点击异常: {str(e)}")
        
        # 阶段4: 等待剩余时间（保持页面活跃）
        log.info(f"⏱️ 阶段4: 等待剩余时间")
        while elapsed < watch_time:
            # 定期移动鼠标保持活跃
            if random.random() > 0.7:
                target_x = random.randint(100, page.viewport_size.get('width', 1920) - 100)
                target_y = random.randint(100, page.viewport_size.get('height', 1080) - 100)
                page.mouse.move(target_x, target_y, steps=5)
                current_x, current_y = target_x, target_y
                behavior_stats["mouse_moves"] += 1
            
            sleep_time = min(random.uniform(1, 2), watch_time - elapsed)
            if not video_interruptible_sleep(sleep_time):
                log.warning("⛔ 任务已停止（剩余时间等待中）")
                return 0, current_x, current_y, behavior_stats
            elapsed += sleep_time
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
            
            # 定期输出观看进度日志
            if elapsed - last_log >= 10:
                log.info(f"⏱️ 已观看: {elapsed:.1f}/{watch_time:.1f} 秒 | 行为统计: 鼠标移动={behavior_stats['mouse_moves']}, 滚动={behavior_stats['scrolls']}, 点击={behavior_stats['clicks']}")
                last_log = elapsed
        
        actual_watch_time = time.time() - watch_start
        log.info(f"✓ 观看完成，实际: {actual_watch_time:.1f} 秒 | 总行为统计: 鼠标移动={behavior_stats['mouse_moves']}, 滚动={behavior_stats['scrolls']}, 点击={behavior_stats['clicks']}, 按键={behavior_stats['key_presses']}")
        
    except Exception as e:
        log.error(f"✗ 视频观看出错: {str(e)}")
        import traceback
        log.debug(f"异常: {traceback.format_exc()}")
        actual_watch_time = 0
    
    # ==================== 视频广告中的真人行为统计将在主流程中合并 ====================
    
    log.info(f"========== 视频广告结束 ==========")
    return actual_watch_time, current_x, current_y, behavior_stats

def click_chicken_soup_link(page, target_url, current_x, current_y, config):
    """
    在首页上找到包含 "chicken soup" 或类似文字的链接并点击，进入视频页面
    找不到链接的话，才fallback到直接访问
    
    返回: (success, new_current_x, new_current_y)
    """
    log.info(f"🔍 尝试在页面上找到并点击 Chicken Soup 链接...")
    
    try:
        # 查找所有a标签
        all_links = page.query_selector_all('a[href]')
        
        # 筛选包含相关文本的链接（不区分大小写）
        target_links = []
        for link in all_links:
            try:
                text = link.text_content().lower()
                href = link.get_attribute('href')
                if "chicken" in text or "soup" in text or "/chicken-soup" in href:
                    target_links.append(link)
                    log.debug(f"✅ 找到候选链接: {text[:50]} → {href}")
            except Exception:
                continue
        
        if target_links:
            # 找到目标链接
            target_link = target_links[0]
            
            # 滚动到可见区域
            target_link.scroll_into_view_if_needed()
            time.sleep(random.uniform(0.5, 1.5))
            
            # 获取链接位置并移动鼠标
            try:
                bbox = target_link.bounding_box()
                if bbox:
                    # 计算点击点（中间位置）
                    click_x = bbox['x'] + bbox['width'] / 2
                    click_y = bbox['y'] + bbox['height'] / 2
                    
                    # 真人鼠标移动
                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                    current_x, current_y = click_x, click_y
                    
                    # 等待一下
                    time.sleep(random.uniform(0.5, 1.0))
                    
                    # 点击！
                    target_link.click()
                    log.info(f"✅ 点击 Chicken Soup 链接成功！")
                    
                    # 等待页面加载
                    wait_load = random.uniform(2, 4)
                    time.sleep(wait_load)
                    page.wait_for_load_state("domcontentloaded", timeout=60000)
                    
                    return True, current_x, current_y
            except Exception as e:
                log.debug(f"点击链接时位置获取失败: {str(e)}")
                # fallback: 直接用Playwright的click
                target_link.click()
                time.sleep(random.uniform(2, 4))
                page.wait_for_load_state("domcontentloaded", timeout=60000)
                return True, current_x, current_y
        
        else:
            log.warning(f"⚠️ 没有找到包含 Chicken Soup 的链接，fallback到直接访问")
    
    except Exception as e:
        log.warning(f"⚠️ 查找并点击链接失败: {str(e)[:100]}")
    
    # Fallback: 直接访问
    chapter_url = target_url.rstrip('/') + "/chicken-soup-for-the-soul"
    try:
        page.goto(chapter_url, timeout=60000, wait_until="domcontentloaded")
        log.info(f"✅ 已通过fallback访问章节页: {chapter_url}")
        return True, current_x, current_y
    except Exception as e:
        log.warning(f"⚠️ fallback访问也失败: {str(e)[:100]}")
        return True, current_x, current_y

def click_book_link_to_list(page, target_url, current_x, current_y, config):
    """
    在首页上点击包含 "book" 的链接，进入列表页
    """
    log.info("🔍 查找包含 'book' 的链接...")
    
    try:
        all_links = page.query_selector_all('a[href]')
        target_links = []
        for link in all_links:
            try:
                text = link.text_content().lower()
                href = link.get_attribute('href')
                if "book" in text:
                    target_links.append(link)
                    log.debug(f"✅ 找到候选链接: {text[:50]}")
            except Exception:
                continue
        
        if target_links:
            target_link = target_links[0]
            target_link.scroll_into_view_if_needed(timeout=10000)
            time.sleep(random.uniform(0.5, 1.5))
            
            try:
                bbox = target_link.bounding_box()
                if bbox:
                    click_x = bbox['x'] + bbox['width'] / 2
                    click_y = bbox['y'] + bbox['height'] / 2
                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                    current_x, current_y = click_x, click_y
                    time.sleep(random.uniform(0.5, 1.0))
                    target_link.click()
                    log.info("✅ 点击 book 链接进入列表页成功！")
                    wait_load = random.uniform(2, 4)
                    time.sleep(wait_load)
                    page.wait_for_load_state('domcontentloaded', timeout=60000)
                    return True, current_x, current_y
            except Exception as e:
                log.debug(f"点击链接失败: {str(e)}")
                target_link.click()
                time.sleep(random.uniform(2, 4))
                page.wait_for_load_state('domcontentloaded', timeout=60000)
                return True, current_x, current_y
        else:
            log.warning("⚠️ 没找到 book 链接，fallback 到手动访问")
    except Exception as e:
        log.warning(f"⚠️ 查找 book 链接失败: {str(e)[:100]}")
    
    return False, current_x, current_y

def click_chapter_link_to_page(page, target_url, current_x, current_y, config):
    """
    在列表页上优先点击包含 "Chapter" 的链接进入章节页；
    如果没有 Chapter 链接，则点击页面上任意一个可点击的链接进入任意章节页
    """
    log.info("🔍 查找包含 'Chapter' 的链接...")
    
    try:
        all_links = page.query_selector_all('a[href]')
        chapter_links = []
        other_links = []
        for link in all_links:
            try:
                text = (link.text_content() or "").lower()
                href = link.get_attribute('href')
                if not href or not href.strip():
                    continue
                # 排除空白、锚点、JS 链接
                href_strip = href.strip()
                if href_strip.startswith('#') or href_strip.lower().startswith('javascript:'):
                    continue
                if "chapter" in text or "chapter" in href_strip.lower():
                    chapter_links.append(link)
                    log.debug(f"✅ 找到 Chapter 候选链接: {text[:50]}")
                else:
                    other_links.append(link)
            except Exception:
                continue
        
        if chapter_links:
            target_links = chapter_links
            log.info(f"✅ 共找到 {len(chapter_links)} 个 Chapter 链接，从中随机点击 1 个")
        elif other_links:
            target_links = other_links[:20]
            log.info(f"⚠️ 没有 Chapter 链接，从其他 {len(target_links)} 个可点击链接中随机点击 1 个进入任意章节页")
        else:
            target_links = []
        
        if target_links:
            target_link = random.choice(target_links)
            try:
                target_link.scroll_into_view_if_needed(timeout=5000)
            except Exception as e:
                log.warning(f"⚠️ 滚动到 chapter 链接超时，使用 JS 兜底滚动: {str(e)[:60]}")
                try:
                    page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", target_link)
                except Exception:
                    pass
            time.sleep(random.uniform(0.5, 1.5))
            
            try:
                bbox = target_link.bounding_box()
                if bbox:
                    click_x = bbox['x'] + bbox['width'] / 2
                    click_y = bbox['y'] + bbox['height'] / 2
                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                    current_x, current_y = click_x, click_y
                    time.sleep(random.uniform(0.5, 1.0))
                    target_link.click()
                    log.info("✅ 点击 chapter/可点击链接进入章节页成功！")
                    wait_load = random.uniform(2, 4)
                    time.sleep(wait_load)
                    page.wait_for_load_state('domcontentloaded', timeout=60000)
                    return True, current_x, current_y
            except Exception as e:
                log.debug(f"点击链接失败: {str(e)}")
                try:
                    target_link.click()
                    time.sleep(random.uniform(2, 4))
                    page.wait_for_load_state('domcontentloaded', timeout=60000)
                    return True, current_x, current_y
                except Exception as e2:
                    log.warning(f"⚠️ 直接点击也失败：{str(e2)[:80]}")
        else:
            log.warning("⚠️ 没找到任何可点击链接")
    except Exception as e:
        log.warning(f"⚠️ 查找 chapter 链接失败: {str(e)[:100]}")
    
    return False, current_x, current_y

def click_back_home_button(page, target_url, current_x, current_y, config):
    """
    在章节页/视频页上点击包含 "返回" 或 "back" 的按钮/链接，回到首页
    找不到时使用浏览器后退（go_back）作为兜底
    """
    log.info("🔍 查找包含 '返回' 或 'back' 的按钮...")
    
    try:
        all_clickable = []
        all_links = page.query_selector_all('a[href]')
        all_buttons = page.query_selector_all('button')
        all_clickable.extend(all_links)
        all_clickable.extend(all_buttons)
        
        target_elements = []
        for elem in all_clickable:
            try:
                text = elem.text_content().lower()
                if "返回" in text or "back" in text or "home" in text or "首页" in text:
                    target_elements.append(elem)
                    log.debug(f"✅ 找到候选按钮: {text[:50]}")
            except Exception:
                continue
        
        if target_elements:
            target_elem = target_elements[0]
            try:
                target_elem.scroll_into_view_if_needed(timeout=5000)
            except Exception as e:
                log.warning(f"⚠️ 滚动返回按钮超时，尝试 JS 兜底滚动: {str(e)[:60]}")
                try:
                    page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", target_elem)
                except Exception:
                    pass
            time.sleep(random.uniform(0.5, 1.5))
            
            try:
                bbox = target_elem.bounding_box()
                if bbox:
                    click_x = bbox['x'] + bbox['width'] / 2
                    click_y = bbox['y'] + bbox['height'] / 2
                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                    current_x, current_y = click_x, click_y
                    time.sleep(random.uniform(0.5, 1.0))
                    target_elem.click()
                    log.info("✅ 点击返回按钮回到首页成功！")
                    wait_load = random.uniform(2, 4)
                    time.sleep(wait_load)
                    page.wait_for_load_state('domcontentloaded', timeout=60000)
                    return True, current_x, current_y
            except Exception as e:
                log.debug(f"点击返回按钮失败: {str(e)}")
                try:
                    target_elem.click()
                    time.sleep(random.uniform(2, 4))
                    page.wait_for_load_state('domcontentloaded', timeout=60000)
                    return True, current_x, current_y
                except Exception as e2:
                    log.warning(f"⚠️ 直接点击返回按钮也失败：{str(e2)[:80]}")
        else:
            log.warning("⚠️ 没找到返回按钮，使用浏览器后退作为兜底")
    except Exception as e:
        log.warning(f"⚠️ 查找返回按钮失败: {str(e)[:100]}")
    
    # 兜底：浏览器后退
    try:
        log.info("⬅️ 调用浏览器后退（page.go_back）")
        page.go_back(timeout=30000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 3))
        log.info("✅ 浏览器后退成功")
        return True, current_x, current_y
    except Exception as e:
        log.warning(f"⚠️ 浏览器后退失败: {str(e)[:80]}，最后兜底直接 goto 首页")
        try:
            page.goto(target_url, timeout=60000, wait_until='domcontentloaded')
            return True, current_x, current_y
        except Exception:
            pass
    
    return False, current_x, current_y

def click_link_containing_text(page, text_list, current_x, current_y, config):
    """
    在页面上找到包含任一指定文本的链接并点击
    text_list: 字符串列表，链接文本包含其中任一个即可匹配（不区分大小写）
    — 增强：
        1. 同时匹配 href（如 /books、/chapter、/home、index.html 等路径关键字）
        2. 自动补充常用中英文导航词（home、首页、book、books、chapter 等），
           避免用户仅配置中文关键词但目标站为英文的漏匹配

    返回: (success, new_current_x, new_current_y)
    """
    # 自动补充常用中英文导航兜底关键词（不重复）
    _extra_defaults = [
        "home", "首页", "index", "主页", "main",
        "book", "books", "books/", "bookshelf", "library",
        "chapter", "章节", "chapter/", "/chapter",
        "all", "全部", "list", "列表", "目录", "contents", "toc",
        "read", "阅读", "阅读全文",
        "next", "下一页", "下一章", "previous", "上一页",
    ]
    _seen = set()
    _normalized_user = [t.lower().strip() for t in (text_list or []) if t and t.strip()]
    _merged = list(_normalized_user)
    for t in _normalized_user:
        _seen.add(t)
    for t in _extra_defaults:
        if t not in _seen:
            _merged.append(t)
            _seen.add(t)
    log.info(f"🔍 尝试在页面上找到并点击链接（用户关键词={text_list}, 扩展后共 {len(_merged)} 个）...")

    # 先等待页面稳定
    time.sleep(random.uniform(1, 2))
    
    try:
        # 多次尝试查找，避免DOM更新问题
        target_href = None
        target_text_found = None
        # 获取当前页面URL，用于稍后规范化相对路径
        _base_url = ""
        try:
            _base_url = page.url or ""
        except Exception:
            _base_url = ""

        for attempt in range(3):
            try:
                # 查找所有a标签
                all_links = page.query_selector_all('a[href]')
                
                # ★ 收集所有命中关键词的候选链接，最后随机选一个
                #   （解决 chapter1~chapter2000 永远只点第一个的问题，实现分散访问）
                _candidates = []
                _seen_href = set()
                # 筛选包含相关文本的链接（不区分大小写；同时检查 href 路径中是否包含目标关键字）
                for link in all_links:
                    try:
                        text = link.text_content().lower()
                        href = link.get_attribute('href')
                        # —— 过滤无效 href：空、mailto、tel、javascript、纯锚点 ——
                        if not href or not isinstance(href, str):
                            continue
                        href_low = href.strip().lower()
                        if not href_low:
                            continue
                        if href_low.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
                            continue
                        # —— URL 规范化：相对路径 → 完整 URL ——
                        try:
                            from urllib.parse import urljoin as _urljoin
                            normalized = _urljoin(_base_url, href.strip())
                        except Exception:
                            normalized = href.strip()
                        # 再次安全检查：规范化后仍需是 http(s):// 协议
                        if not normalized.lower().startswith(("http://", "https://")):
                            continue
                        # 排除跳转到自身（与当前页完全相同的 URL，避免点击空白锚点）
                        if _base_url and normalized.rstrip("/#") == _base_url.rstrip("/#"):
                            continue
                        # 匹配规则：链接文本 OR href路径 包含任一目标关键字
                        for target_text in _merged:
                            if not target_text:
                                continue
                            if target_text in text or target_text in href_low or target_text in normalized.lower():
                                if normalized not in _seen_href:
                                    _seen_href.add(normalized)
                                    _candidates.append((normalized, text or href_low, target_text))
                                break
                    except Exception:
                        continue
                
                if _candidates:
                    # ★ 从所有命中链接中随机选一个，实现 chapter1~N 分散访问
                    _chosen = random.choice(_candidates)
                    target_href = _chosen[0]
                    target_text_found = _chosen[1]
                    log.info(
                        f"✅ 命中 {len(_candidates)} 个关键词链接，随机选中: "
                        f"{str(target_text_found)[:40]} | match={_chosen[2]} | {target_href}"
                    )
                    break
            except Exception:
                time.sleep(0.5)
                continue
        
        if target_href:
            # 使用更稳定的方式：先获取href，然后点击或导航
            try:
                # 重新查找，确保元素存在（用规范化后的 href 反向匹配）
                all_links = page.query_selector_all('a[href]')
                target_link = None
                for link in all_links:
                    try:
                        href = link.get_attribute('href')
                        if not href:
                            continue
                        # 反向规范化并比较
                        try:
                            from urllib.parse import urljoin as _urljoin2
                            normalized_href = _urljoin2(_base_url, href.strip())
                        except Exception:
                            normalized_href = href.strip()
                        if normalized_href == target_href:
                            target_link = link
                            break
                    except Exception:
                        continue
                
                if target_link:
                    # 滚动到可见区域
                    try:
                        target_link.scroll_into_view_if_needed(timeout=5000)
                    except Exception as e:
                        log.warning(f"⚠️ 滚动链接超时，使用 JS 兜底滚动: {str(e)[:60]}")
                        try:
                            page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", target_link)
                        except Exception:
                            pass
                    time.sleep(random.uniform(0.8, 1.5))
                    
                    # 获取链接位置并移动鼠标
                    try:
                        bbox = target_link.bounding_box()
                        if bbox:
                            # 计算点击点（中间位置）
                            click_x = bbox['x'] + bbox['width'] / 2
                            click_y = bbox['y'] + bbox['height'] / 2
                            
                            # 模拟人类鼠标移动
                            human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                            current_x, current_y = click_x, click_y
                            
                            # 点击前暂停，模拟人类思考时间
                            time.sleep(random.uniform(0.5, 1.0))
                            
                            # 点击链接
                            target_link.click()
                            log.info(f"✅ 点击包含 {text_list} 的链接成功！")
                            
                            # 等待页面加载
                            wait_load = random.uniform(3, 5)
                            time.sleep(wait_load)
                            try:
                                page.wait_for_load_state('domcontentloaded', timeout=30000)
                            except Exception:
                                log.warning("等待页面load状态超时，但继续执行...")
                            return True, current_x, current_y
                    except Exception as e:
                        log.debug(f"点击链接失败（带移动）: {str(e)}")
                
                # 兜底方案：直接导航到规范化的完整URL
                log.info(f"🚀 使用兜底方案：直接导航到 {target_href}")
                try:
                    page.goto(target_href, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2, 3))
                    log.info(f"✅ 导航到 {target_href} 成功！")
                    return True, current_x, current_y
                except Exception as e2:
                    log.warning(f"⚠️ 直接导航也失败：{str(e2)[:80]}")
            except Exception as e:
                log.warning(f"⚠️ 链接操作失败：{str(e)[:100]}")
        else:
            log.warning(f"⚠️ 没找到包含 {text_list} 的链接")
    except Exception as e:
        log.warning(f"⚠️ 查找链接失败: {str(e)[:100]}")
    
    return False, current_x, current_y

def click_link_with_fallback(page, text_list, fallback_urls, current_x, current_y, config, final_fallback_url=None):
    """
    在页面上找到包含任一指定文本的链接并点击，如果找不到则尝试使用 fallback_urls
    text_list: 字符串列表；若为空则不做文本匹配，直接走 fallback_urls
    fallback_urls: URL 列表；若找不到链接则依次尝试这些 URL
    final_fallback_url: （可选）终极兜底 URL，当所有常规方式都失败时使用（例如返回首页）

    永远不会抛异常；失败返回 (False, current_x, current_y)，成功返回 (True, new_x, new_y)
    """
    log.info(
        f"🔍 尝试在页面上找到并点击链接"
        f"（关键词={text_list}, fallback={len(fallback_urls or [])}个, final_fallback={bool(final_fallback_url)}）"
    )

    # 先尝试正常的链接点击（关键词或 href 匹配）——加30秒总超时，避免代理慢导致卡死
    try:
        _has_kw = bool(text_list and any(str(k).strip() for k in text_list))
        if _has_kw:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
            def _do_click():
                return click_link_containing_text(page, text_list, current_x, current_y, config)
            try:
                with ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(_do_click)
                    success, new_x, new_y = _fut.result(timeout=30)
                if success:
                    log.info("✅ 通过关键词链接跳转成功")
                    return True, new_x, new_y
            except _FutTimeout:
                log.warning("⚠️ 关键词链接查找超时(30s)，跳过直接走兜底URL")
            except Exception as e:
                log.warning(f"⚠️ click_link_containing_text 异常: {str(e)[:80]}")
    except Exception as e:
        log.warning(f"⚠️ click_link_containing_text 异常: {str(e)[:80]}")

    # 如果失败（或关键词为空），尝试使用 fallback_urls
    # ★ 打乱兜底链接池顺序，实现 chapter1~N 分散访问（避免每次只点第一个）
    _fbs = list(fallback_urls or [])
    random.shuffle(_fbs)
    if final_fallback_url:
        _fbs.append(final_fallback_url)  # 终极兜底始终最后尝试

    if _fbs:
        log.warning(f"⚠️ 未找到关键词链接，尝试使用 {len(_fbs)} 个兜底URL...")
        for url in _fbs:
            try:
                log.info(f"🚀 尝试兜底URL: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(2, 4))
                log.info(f"✅ 兜底URL跳转成功：{url}")
                return True, current_x or 300, current_y or 300
            except Exception as e:
                log.warning(f"⚠️ 兜底URL跳转失败：{str(e)[:120]}")
                # goto 抛异常但内容实际已加载 → 视为成功
                try:
                    _u = page_url_safe(page, default="")
                    _b = len(page_body_inner_text(page, default="") or "")
                    if _u.lower().startswith(("http://", "https://")) and _b >= 10:
                        log.info(f"🔎 兜底URL的 goto 抛异常但内容已加载（body≈{_b}字符），视为成功")
                        time.sleep(random.uniform(1.5, 3.0))
                        return True, current_x or 300, current_y or 300
                except Exception:
                    pass
                continue

    log.warning("⚠️ 关键词链接和所有兜底URL都失败，但不会终止任务")
    return False, current_x, current_y

def click_back_to_toc(page, current_x, current_y, config):
    """
    在 chapter 页上点击包含配置的返回链接关键词或使用浏览器后退，回到章节页
    — 增强：复用 click_link_containing_text（自动补中英文导航词 + href 匹配），
      并兜底使用 page.go_back / 刷新，避免让整个任务失败
    """
    web_config = config.get("web_navigation", {})
    back_keywords = list(web_config.get("back_links", []) or [])
    # 如果用户没配置，使用常见兜底
    if not any(k for k in back_keywords if str(k).strip()):
        back_keywords = ["返回", "back", "目录", "contents", "toc", "chapter", "首页", "home"]
    # 直接使用增强版链接点击
    try:
        success, new_x, new_y = click_link_containing_text(
            page, back_keywords, current_x, current_y, config
        )
        if success:
            log.info("✅ 通过关键词返回目录")
            return True, new_x, new_y
    except Exception as e:
        log.warning(f"⚠️ click_back_to_toc: 点击返回链接异常: {str(e)[:80]}")

    # 兜底：浏览器后退
    try:
        log.info("⬅️ click_back_to_toc: 调用浏览器后退（page.go_back）")
        page.go_back(timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 3))
        return True, current_x, current_y
    except Exception as e:
        log.warning(f"⚠️ 浏览器后退失败: {str(e)[:80]}")

    # 再兜底：刷新当前页
    try:
        log.info("🔄 click_back_to_toc: 刷新当前页作为兜底")
        page.reload(timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 3))
        return True, current_x, current_y
    except Exception as e:
        log.warning(f"⚠️ 刷新失败: {str(e)[:80]}")

    return False, current_x, current_y

def click_chapter_page_link(page, current_x, current_y, config, keywords=None, fallback_urls=None):
    """
    在章节页上找到包含指定关键词的链接并点击
    keywords: 关键词列表，链接文本包含其中任一个即可匹配
    fallback_urls: 兜底URL列表，如果找不到链接则尝试这些URL
    
    返回: (success, new_current_x, new_current_y)
    """
    # 获取配置
    web_config = config.get("web_navigation", {})
    if keywords is None:
        layer3 = web_config.get("layer_3", {})
        keywords = layer3.get("keywords", ["Chapter 1", "Chapter 2", "Chapter 3"])
    if fallback_urls is None:
        layer3 = web_config.get("layer_3", {})
        fallback_urls = layer3.get("fallback_urls", [])
    
    log.info(f"🔍 尝试在页面上找到并点击包含 {keywords} 的链接...")
    
    # 先尝试正常的链接点击
    success, new_x, new_y = click_link_containing_text(page, keywords, current_x, current_y, config)
    
    if success:
        log.info("✅ 通过关键词链接跳转成功")
        return True, new_x, new_y
    
    # 如果失败，尝试使用 fallback_urls
    if fallback_urls and len(fallback_urls) > 0:
        log.warning(f"⚠️ 未找到关键词链接，尝试使用 {len(fallback_urls)} 个兜底URL...")
        for url in fallback_urls:
            try:
                log.info(f"🚀 尝试兜底URL：{url}")
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(2, 4))
                log.info(f"✅ 兜底URL跳转成功：{url}")
                return True, current_x or 300, current_y or 300
            except Exception as e:
                log.warning(f"⚠️ 兜底URL跳转失败：{url}，错误：{str(e)[:80]}")
                continue
    
    log.error("❌ 关键词链接和所有兜底URL都失败")
    return False, current_x, current_y

def watch_video_ad_from_page(page, config, current_x, current_y):
    """
    从当前页面上查找所有视频 iframe 并观看（支持udis视频混合中转方案）
    
    返回: (watch_time, behavior_stats)
    """
    log.info(f"========== 从当前页面iframe观看视频 ==========")
    
    # 初始化统计
    behavior_stats = {
        "mouse_moves": 0,
        "scrolls": 0,
        "scroll_distance": 0,
        "clicks": 0,
        "waits": 0,
        "focus_switches": 0,
        "refreshes": 0,
        "ad_stay": 0,
        "total_stay": 0,
        "key_presses": 0
    }
    
    try:
        # 1. 等待页面上的 iframe 出现
        log.info("查找页面上的所有视频 iframe...")
        
        # 查找所有的 iframe（不硬编码域名）
        all_iframes = page.query_selector_all('iframe')
        # 过滤出有 src 属性的 iframe
        iframes = []
        for iframe in all_iframes:
            try:
                src = iframe.get_attribute('src')
                if src and src.strip():
                    # 处理udis视频链接替换（支持udis视频混合中转方案）
                    original_src = src
                    if is_udis_video_url(src):
                        # 尝试从当前任务信息中获取代理信息（通过config.get('current_task')）
                        current_task = config.get('current_task', None)
                        src = convert_udis_video_url(src, config, current_task)
                        # 更新iframe的src属性
                        iframe.set_attribute('src', src)
                        log.info(f"已替换udis视频iframe链接: {original_src} -> {src}")
                    iframes.append(iframe)
            except Exception:
                pass
        
        log.info(f"找到 {len(iframes)} 个有 src 的 iframe")
        
        if not iframes:
            log.warning("没有找到视频 iframe，无法观看视频")
            return 0, behavior_stats
        
        # 随机打乱 iframe 顺序，依次尝试，超时则跳过换下一个
        random.shuffle(iframes)
        log.info(f"将依次尝试 {len(iframes)} 个 iframe，超时则跳过换下一个")
        
        selected_iframe = None
        frame = None
        for idx, candidate in enumerate(iframes):
            log.info(f"尝试第 {idx+1}/{len(iframes)} 个 iframe...")
            # 2. 滚动到 iframe 位置（缩短超时，超时即跳过）
            scrolled = False
            try:
                candidate.scroll_into_view_if_needed(timeout=5000)
                scrolled = True
            except Exception as e:
                log.warning(f"⚠️ 第 {idx+1} 个 iframe 滚动超时（5s），尝试 JS 兜底滚动")
                try:
                    page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", candidate)
                    scrolled = True
                except Exception as e2:
                    log.warning(f"⚠️ JS 兜底滚动失败：{str(e2)[:80]}，跳过该 iframe 换下一个")
                    continue
            
            # 滚动后的随机等待
            wait_scroll = random.uniform(1, 2)
            time.sleep(wait_scroll)
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(wait_scroll * 1000)
            
            # 3. 获取 iframe 的 contentFrame
            try:
                candidate_frame = candidate.content_frame()
            except Exception as e:
                log.warning(f"⚠️ 获取 iframe 内容失败：{str(e)[:80]}，跳过换下一个")
                continue
            
            if not candidate_frame:
                log.warning(f"⚠️ 第 {idx+1} 个 iframe 无 contentFrame，跳过换下一个")
                continue
            
            selected_iframe = candidate
            frame = candidate_frame
            log.info(f"✅ 成功选定第 {idx+1} 个 iframe 进行观看")
            break
        
        if not frame:
            log.error("❌ 所有 iframe 都无法观看，放弃")
            return 0, behavior_stats
        
        # 等待 iframe 页面加载完成
        try:
            frame.wait_for_load_state('networkidle', timeout=10000)
            wait_load = random.uniform(1, 2)
            time.sleep(wait_load)
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(wait_load * 1000)
            log.info("iframe 内容加载完成")
        except Exception as e:
            log.warning(f"等待 iframe 加载超时: {str(e)}")
        
        # 4. 检查是否有 VPN/代理限制（参考代码核心检查）
        log.info("检查是否有 VPN/代理限制...")
        try:
            has_proxy_error = frame.query_selector('text=This video owner does not allow VPN or proxy traffic')
            if has_proxy_error:
                log.warning("❌ 此视频禁止 VPN/代理访问，跳过")
                return 0, behavior_stats
            log.info("✅ 没有 VPN/代理限制")
        except Exception as e:
            log.debug(f"检查 VPN 限制时出错: {str(e)}")
        
        # 5. 等待广告加载（参考代码步骤）
        log.info("等待广告加载...")
        try:
            ad_selectors = ['.adsbygoogle', '[class*="ad-container"]']
            found_ads = False
            for sel in ad_selectors:
                ad_elements = frame.query_selector_all(sel)
                if ad_elements:
                    log.info(f"找到 {len(ad_elements)} 个广告元素")
                    found_ads = True
                    # 随机等待一下广告
                    wait_ad = random.uniform(1, 2)
                    time.sleep(wait_ad)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(wait_ad * 1000)
                    break
            if not found_ads:
                log.info("没有找到特定广告元素，继续")
        except Exception as e:
            log.debug(f"广告检查出错: {str(e)}")
        
        # 6. 点击播放按钮（多种选择器，按优先级排序）
        log.info("尝试点击播放按钮...")
        play_button_clicked = False
        # 高优先级播放按钮选择器（按视频站常见 player 排序）
        play_selectors = [
            'button.vjs-big-play-button',           # video.js
            '.vjs-big-play-button',                  # video.js (non-button)
            'button[aria-label*="lay" i]',           # aria-label="Play"
            'button[title*="lay" i]',                # title="Play"
            '.plyr__control--overlaid',              # plyr.io
            '.jw-icon-display',                      # jwplayer
            '.shaka-play-button',                    # shaka-player
            'button.ytp-large-play-button',          # youtube
            '.play-button',
            '#play',
            'button:has-text("Play")',
            '[role="button"][aria-label*="lay" i]',
            'button[class*="play" i]:not([class*="player" i]):not([class*="playlist" i]):not([class*="display" i])',
            'div[class*="play-btn" i]',
            'video',                                  # 兜底：直接点视频元素
        ]
        play_buttons = []
        for sel in play_selectors:
            try:
                found = frame.query_selector_all(sel)
                if found:
                    log.info(f"  选择器 '{sel}' 找到 {len(found)} 个候选")
                    play_buttons.extend(found)
                    if play_buttons:
                        break  # 找到第一类候选就够了
            except Exception:
                continue
        
        log.info(f"共找到 {len(play_buttons)} 个候选播放按钮")
        
        for btn in play_buttons:
            try:
                if btn.is_visible():
                    box = btn.bounding_box()
                    if box:
                        # 移动鼠标到播放按钮
                        target_x = box["x"] + box["width"] / 2
                        target_y = box["y"] + box["height"] / 2
                        human_mouse_move(frame, current_x, current_y, target_x, target_y, config)
                        behavior_stats["mouse_moves"] += 1
                        current_x, current_y = target_x, target_y
                        
                        btn.click()
                        behavior_stats["clicks"] += 1
                        log.info("✅ 点击了播放按钮")
                        play_button_clicked = True
                        
                        # 等待一下
                        wait_play = random.uniform(2, 3)
                        time.sleep(wait_play)
                        behavior_stats["waits"] += 1
                        behavior_stats["total_stay"] += int(wait_play * 1000)
                        break
            except Exception as e:
                continue
        
        if not play_button_clicked:
            # 如果没找到播放按钮，尝试点击视频元素
            try:
                video_elem = frame.query_selector('video')
                if video_elem:
                    box = video_elem.bounding_box()
                    if box:
                        target_x = box["x"] + box["width"] / 2
                        target_y = box["y"] + box["height"] / 2
                        human_mouse_move(frame, current_x, current_y, target_x, target_y, config)
                        behavior_stats["mouse_moves"] += 1
                        current_x, current_y = target_x, target_y
                        
                        video_elem.click()
                        behavior_stats["clicks"] += 1
                        log.info("✅ 点击了视频元素")
                        
                        wait_vid = random.uniform(2, 3)
                        time.sleep(wait_vid)
                        behavior_stats["waits"] += 1
                        behavior_stats["total_stay"] += int(wait_vid * 1000)
            except Exception as e:
                log.warning("无法点击视频元素")
        
        # 6.5 强力 video.play() 兜底：iframe 内所有 video 元素，静音 + 强制播放（绕过 autoplay 限制）
        try:
            force_played = frame.evaluate("""
                () => {
                    const results = [];
                    const videos = document.querySelectorAll('video');
                    videos.forEach((v, i) => {
                        try {
                            v.muted = true;          // 静音 -> 允许 autoplay
                            v.autoplay = true;
                            v.removeAttribute('controls');
                            const p = v.play();
                            if (p && p.then) {
                                p.then(() => results.push({i, ok: true, src: v.currentSrc || v.src}))
                                 .catch(e => results.push({i, ok: false, err: String(e)}));
                            } else {
                                results.push({i, ok: 'sync'});
                            }
                        } catch(e) {
                            results.push({i, err: String(e)});
                        }
                    });
                    return {count: videos.length, results};
                }
            """)
            log.info(f"🎬 强制播放 video.play() → {force_played}")
            time.sleep(2)  # 给一点时间让 play promise 解析
        except Exception as e:
            log.debug(f"强力播放兜底失败: {str(e)[:80]}")
        
        # 7. 确认视频正在播放
        video_playing = False
        try:
            check_play = frame.evaluate("""
                () => {
                    const video = document.querySelector('video');
                    if (!video) return false;
                    return !video.paused && !video.ended && video.currentTime > 0;
                }
            """)
            video_playing = check_play
            if video_playing:
                log.info("✅ 确认视频正在播放")
            else:
                log.warning("⚠️ 视频没有在播放，尝试JS直接播放...")
                # 尝试用JS强制播放
                frame.evaluate("""
                    () => {
                        const video = document.querySelector('video');
                        if (video) {
                            video.muted = true;
                            video.play().catch(() => {});
                        }
                    }
                """)
                wait_force = random.uniform(1, 2)
                time.sleep(wait_force)
                behavior_stats["waits"] += 1
                behavior_stats["total_stay"] += int(wait_force * 1000)
        except Exception as e:
            log.debug(f"检查播放状态失败: {str(e)}")
        
        # 观看视频（随机时长，来自配置，完全独立）
        min_watch = config["video_ad"].get("min_watch_time", 30)
        max_watch = config["video_ad"].get("max_watch_time", 90)
        
        watch_time = random.uniform(min_watch, max_watch)
        log.info(f"⏱️ 将观看 {watch_time:.1f} 秒视频（完全独立于页面停留时间）")
        
        watch_start = time.time()
        elapsed = 0
        last_log = 0
        last_play_check = 0
        
        while elapsed < watch_time:
            # —— 停止信号检查 ——
            if not task_running:
                log.warning("⛔ 任务已停止（视频观看中）")
                break
            # 每10秒检查一次视频是否真的在播放
            if elapsed - last_play_check >= 10:
                try:
                    is_playing = frame.evaluate("""
                        () => {
                            const video = document.querySelector('video');
                            if (!video) return false;
                            if (video.paused) return false;
                            if (video.ended) return false;
                            return video.currentTime > 0;
                        }
                    """)
                    
                    if not is_playing:
                        log.warning("⚠️ 视频似乎暂停了，尝试再次播放...")
                        # 尝试再次播放
                        frame.evaluate("""
                            () => {
                                const video = document.querySelector('video');
                                if (video) {
                                    video.muted = true;
                                    video.play().catch(() => {});
                                }
                            }
                        """)
                        wait_replay = random.uniform(1, 2)
                        time.sleep(wait_replay)
                        behavior_stats["waits"] += 1
                        behavior_stats["total_stay"] += int(wait_replay * 1000)
                    last_play_check = elapsed
                except Exception as e:
                    log.debug(f"检查播放状态失败: {str(e)}")
            
            # 随机滚动（在主页面或frame中滚动）
            if random.random() > 0.7:  # 30% 概率滚动
                try:
                    scroll_amount = random.randint(-50, 100)
                    if random.random() > 0.5:
                        page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                    else:
                        frame.evaluate(f"window.scrollBy(0, {scroll_amount})")
                    behavior_stats["scrolls"] += 1
                    behavior_stats["scroll_distance"] += abs(scroll_amount)
                    log.debug(f"模拟滚动页面")
                except Exception as e:
                    pass
            
            # 随机鼠标移动（贝塞尔曲线平滑移动）
            if random.random() > 0.5:
                try:
                    target_x = random.randint(100, 800)
                    target_y = random.randint(100, 500)
                    human_mouse_move(page, current_x, current_y, target_x, target_y, config)
                    behavior_stats["mouse_moves"] += 1
                    current_x, current_y = target_x, target_y
                except Exception as e:
                    pass
            
            # 增加键盘事件（随机按下空格键或方向键）
            if random.random() > 0.8:  # 20%概率触发键盘事件
                try:
                    key = random.choice(["Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"])
                    page.keyboard.press(key)
                    behavior_stats["key_presses"] = behavior_stats.get("key_presses", 0) + 1
                    log.debug(f"模拟键盘事件：{key}")
                except Exception as e:
                    pass
            
            # 增加鼠标点击事件（随机位置点击）
            if random.random() > 0.85:  # 15%概率点击
                try:
                    click_x = random.randint(100, 800)
                    click_y = random.randint(100, 500)
                    human_mouse_move(page, current_x, current_y, click_x, click_y, config)
                    page.mouse.click(click_x, click_y)
                    behavior_stats["clicks"] += 1
                    behavior_stats["mouse_moves"] += 1
                    current_x, current_y = click_x, click_y
                    log.debug(f"模拟鼠标点击：({click_x}, {click_y})")
                except Exception as e:
                    pass
            
            # 等待（随机间隔）
            sleep_time = min(random.uniform(0.8, 3.0), watch_time - elapsed)
            time.sleep(sleep_time)
            elapsed = time.time() - watch_start
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
            
            if elapsed - last_log >= 10:
                log.info(f"⏱️ 已观看 {elapsed:.1f}/{watch_time:.1f} 秒")
                last_log = elapsed
        
        actual_watch_time = time.time() - watch_start
        log.info(f"✅ 视频观看完成！总共 {actual_watch_time:.1f} 秒")
        
        log.info(f"========== 从页面iframe观看视频结束 ==========")
        return actual_watch_time, behavior_stats
        
    except Exception as e:
        log.error(f"❌ 视频观看流程出错: {str(e)}")
        import traceback
        log.debug(f"异常: {traceback.format_exc()}")
        return 0, behavior_stats

def navigate_page_hierarchy(page, home_url, config, min_clicks=2):
    """
    实现页面层级浏览：首页 -> 点击 -> 内页 -> 点击 -> ...
    
    参数:
        page: Playwright页面对象
        home_url: 首页URL
        config: 配置对象
        min_clicks: 最少点击次数（默认2次）
    
    返回:
        (final_url, behavior_stats): (最终页面URL, 真人行为统计数据)
    """
    log.info(f"========== 开始页面层级浏览 ==========")
    log.info(f"目标: 至少{min_clicks}次点击")
    current_url = home_url
    
    # 初始化真人行为统计
    behavior_stats = {
        "mouse_moves": 0,
        "scrolls": 0,
        "scroll_distance": 0,
        "clicks": 0,
        "waits": 0,
        "focus_switches": 0,
        "refreshes": 0,
        "ad_stay": 0,
        "total_stay": 0,
        "key_presses": 0
    }
    
    try:
        # 1. 访问首页
        log.info(f"第1步：访问首页: {home_url}")
        try:
            page.goto(home_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log.warning(f"页面加载超时或失败，但继续尝试: {str(e)}")
        
        # 停留3-5秒
        home_wait = random.uniform(3, 5)
        log.info(f"首页停留: {home_wait:.1f}秒")
        time.sleep(home_wait)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(home_wait * 1000)
        
        # 滚动页面
        log.info("滚动首页...")
        try:
            scroll_amount = random.randint(300, 800)
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            behavior_stats["scrolls"] += 1
            behavior_stats["scroll_distance"] += scroll_amount
        except Exception:
            pass
        wait_after_scroll = random.uniform(0.5, 1.5)
        time.sleep(wait_after_scroll)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_after_scroll * 1000)
        
        current_url = home_url
        
        # 2. 执行点击操作
        for click_count in range(1, min_clicks + 1):
            log.info(f"第{click_count}次点击操作...")
            
            # 寻找页面上的可点击链接
            try:
                links = page.query_selector_all('a[href]')
            except Exception:
                links = []
            
            if not links:
                log.warning("未找到可点击链接，尝试寻找按钮")
                try:
                    links = page.query_selector_all('button, [role="button"], [onclick]')
                except Exception:
                    links = []
            
            if not links:
                log.warning("未找到任何可点击元素，停止层级浏览")
                break
            
            # 过滤掉明显不相关的链接
            valid_links = []
            # 优先匹配配置中的关键词链接（从 web_navigation 各层 link_keywords 汇总，无配置则不偏好）
            preferred_keywords = []
            try:
                _wn = config.get("web_navigation", {}) or {}
                for _lk, _lv in _wn.items():
                    if isinstance(_lv, dict):
                        _kw = _lv.get("link_keywords") or _lv.get("keywords") or []
                        if isinstance(_kw, str):
                            _kw = [k.strip() for k in _kw.split(",") if k.strip()]
                        if isinstance(_kw, list):
                            preferred_keywords.extend([str(k).strip() for k in _kw if str(k).strip()])
                # 去重
                preferred_keywords = list(dict.fromkeys(preferred_keywords))
            except Exception:
                preferred_keywords = []
            preferred_links = []
            for link in links:
                try:
                    href = link.get_attribute('href') if link.get_attribute('href') else ''
                    # 排除javascript链接和锚点
                    if href and not href.startswith('javascript') and not href.startswith('#'):
                        valid_links.append(link)
                        
                        # 检查链接文本是否包含配置的关键词
                        if preferred_keywords:
                            try:
                                link_text = link.inner_text().strip()
                                if any(kw.lower() in link_text.lower() for kw in preferred_keywords):
                                    preferred_links.append(link)
                                    log.info(f"✅ 找到关键词匹配链接: {link_text[:40]}")
                            except Exception:
                                pass
                except Exception:
                    continue
            
            if not valid_links:
                log.warning("未找到有效链接，使用原始链接列表")
                valid_links = links
            
            # 优先选择关键词匹配的链接
            if preferred_links:
                log.info(f"✅ 优先点击关键词匹配链接")
                target_link = random.choice(preferred_links)
            else:
                log.info("链接无关键词偏好，随机选择链接")
                target_link = random.choice(valid_links)
            
            try:
                # 获取链接位置
                box = target_link.bounding_box()
                if box:
                    target_x = box['x'] + box['width'] / 2
                    target_y = box['y'] + box['height'] / 2
                    
                    # 使用贝塞尔曲线移动鼠标
                    # 随机起始位置
                    start_x = random.randint(100, 800)
                    start_y = random.randint(100, 600)
                    human_mouse_move(page, start_x, start_y, target_x, target_y, config)
                    behavior_stats["mouse_moves"] += 1
                    
                    # 点击
                    target_link.click()
                    behavior_stats["clicks"] += 1
                    log.info(f"✓ 第{click_count}次点击完成")
                    
                    # 等待页面加载
                    wait_load = random.uniform(2, 4)
                    time.sleep(wait_load)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(wait_load * 1000)
                    
                    # 停留并滚动
                    page_wait = random.uniform(2, 4)
                    log.info(f"页面停留: {page_wait:.1f}秒")
                    time.sleep(page_wait)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(page_wait * 1000)
                    
                    # 滚动新页面
                    try:
                        scroll_amount = random.randint(200, 600)
                        page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                        behavior_stats["scrolls"] += 1
                        behavior_stats["scroll_distance"] += scroll_amount
                    except Exception:
                        pass
                    wait_after_click_scroll = random.uniform(0.5, 1)
                    time.sleep(wait_after_click_scroll)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(wait_after_click_scroll * 1000)
                    
                    # 更新当前URL
                    try:
                        current_url = page.url
                        log.info(f"当前页面: {current_url}")
                    except Exception:
                        pass
                else:
                    log.warning("无法获取链接位置，跳过此链接")
            except Exception as e:
                log.warning(f"点击失败: {str(e)}")
                continue
        
        log.info(f"✓ 页面层级浏览完成，最终页面: {current_url}")
    except Exception as e:
        log.error(f"✗ 页面层级浏览过程出错: {str(e)}")
        import traceback
        log.debug(f"异常详情: {traceback.format_exc()}")
    
    log.info(f"========== 页面层级浏览结束 ==========")
    return current_url, behavior_stats


def _bcp47_prefix_equal(a, b):
    """比较 BCP 47 语言标签的主语言前缀（忽略地区后缀）。

    例："en-US" == "en" → True；"en-US" == "en-GB" → True（主语言均为 en）；
    "zh-CN" == "en-US" → False。用于放宽 navigator.language 与 fingerprint.language 的比较。
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if a == b:
        return True
    prefix_a = (a.split("-")[0] or "").lower()
    prefix_b = (b.split("-")[0] or "").lower()
    if not prefix_a or not prefix_b:
        return False
    return prefix_a == prefix_b


def _load_adsl_ip_history():
    try:
        with open(ADSL_IP_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_adsl_ip_history(history):
    with open(ADSL_IP_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _prune_adsl_ip_history(history, now_ts, window_sec):
    return {
        ip: rec for ip, rec in history.items()
        if isinstance(rec, dict) and now_ts - float(rec.get('last_seen', 0) or 0) < window_sec
    }


def _adsl_ip_seen_recently(ip, window_sec):
    now_ts = time.time()
    with _adsl_ip_history_lock:
        history = _prune_adsl_ip_history(_load_adsl_ip_history(), now_ts, window_sec)
        _save_adsl_ip_history(history)
        rec = history.get(ip)
        if not rec:
            return False, None
        return True, rec


def _record_adsl_ip_use(ip, resolved=None):
    now_ts = time.time()
    window_hours = float(config.get("adsl_ip_blacklist_hours", 24) or 24)
    window_sec = max(1, int(window_hours * 3600))
    with _adsl_ip_history_lock:
        history = _prune_adsl_ip_history(_load_adsl_ip_history(), now_ts, window_sec)
        history[ip] = {
            "last_seen": now_ts,
            "last_seen_text": time.strftime('%Y-%m-%d %H:%M:%S'),
            "country_code": (resolved or {}).get("country_code") or "",
            "country_name": (resolved or {}).get("country_name") or (resolved or {}).get("country") or ""
        }
        _save_adsl_ip_history(history)


def ensure_xvfb_for_headed_mode(headless):
    """服务器无 DISPLAY 时，为 headed 模式自动启动 Xvfb 虚拟显示器。"""
    global _xvfb_process
    if headless or os.environ.get("DISPLAY"):
        return
    with _xvfb_lock:
        if _xvfb_process and _xvfb_process.poll() is None:
            if not os.environ.get("DISPLAY"):
                os.environ["DISPLAY"] = getattr(_xvfb_process, "_display", ":99")
            return
        last_error = None
        for display_num in range(99, 110):
            display = f":{display_num}"
            cmd = [
                "Xvfb", display,
                "-screen", "0", "1920x1080x24",
                "-ac",
                "+extension", "RANDR",
            ]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                time.sleep(1)
                if proc.poll() is not None:
                    err = proc.stderr.read().decode('utf-8', 'ignore')[:160] if proc.stderr else ""
                    last_error = err or "Xvfb 启动后立即退出"
                    continue
                _xvfb_process = proc
                _xvfb_process._display = display
                os.environ["DISPLAY"] = display
                log.info(f"[Xvfb] headed模式已自动启用虚拟显示器 DISPLAY={display}")
                return
            except FileNotFoundError:
                raise RuntimeError("当前服务器未安装 Xvfb，无法运行有界面模式；请安装 xvfb 或保持无头模式")
        raise RuntimeError(f"Xvfb 虚拟显示器启动失败: {last_error or '未知错误'}")


def _validate_resolved_ip_info(ip, resolved):
    if not isinstance(resolved, dict) or not resolved.get("success"):
        return False, "IP三要素解析失败"
    missing = [key for key in ("country_code", "timezone", "language") if not resolved.get(key)]
    if missing:
        return False, "IP三要素缺失: " + ",".join(missing)
    try:
        pytz.timezone(resolved["timezone"])
    except Exception:
        return False, f"IP时区不是有效IANA时区: {resolved.get('timezone')}"
    if "-" not in str(resolved.get("language")):
        return False, f"IP语言不是有效BCP47格式: {resolved.get('language')}"
    if ip and resolved.get("ip") and str(resolved.get("ip")) != str(ip):
        return False, f"解析IP不匹配: expected={ip}, resolved={resolved.get('ip')}"
    # 强制 ADSL 拨号 IP 必须为指定国家（默认美国 US；后续可在配置面板调整 adsl_required_country）
    required_country = str(config.get("adsl_required_country", "US") or "US").upper()
    actual_country = str(resolved.get("country_code") or "").upper()
    if actual_country != required_country:
        return False, f"IP国家不符: 要求={required_country}, 实际={actual_country}"
    return True, "ok"


def sync_process_timezone_to_ip(resolved):
    """按 IP 时区同步系统/进程 TZ；浏览器层仍由 timezone_id/locale 严格设置。"""
    timezone_name = (resolved or {}).get("timezone")
    language = (resolved or {}).get("language")
    if not timezone_name or not language:
        raise RuntimeError("缺少 timezone/language，无法同步时区")
    # H-4: 白名单校验时区字符串，防止外部 API 返回恶意值导致命令注入
    if not re.match(r'^[A-Za-z0-9_/+\-]+$', timezone_name):
        raise RuntimeError(f"时区名称包含非法字符: {timezone_name!r}")
    pytz.timezone(timezone_name)
    try:
        subprocess.run(["timedatectl", "set-timezone", timezone_name], check=True, capture_output=True, timeout=10)
        subprocess.run(["timedatectl", "set-ntp", "true"], check=False, capture_output=True, timeout=10)
        log.info(f"[时间同步] OS系统时区已同步为IP时区: {timezone_name}")
    except FileNotFoundError:
        log.warning("[时间同步] timedatectl 不存在，仅同步当前进程TZ")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"OS系统时区同步失败: {e.stderr.decode('utf-8', 'ignore')[:120]}")
    except Exception as e:
        raise RuntimeError(f"OS系统时区同步失败: {type(e).__name__}: {str(e)[:120]}")
    os.environ["TZ"] = timezone_name
    if hasattr(time, "tzset"):
        time.tzset()
    log.info(f"[时间同步] 当前进程TZ已同步为IP时区: timezone={timezone_name}, language={language}, local_time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def get_direct_public_ip(timeout=10):
    """获取当前本机公网 IP，供 ADSL 直连模式使用。"""
    check_urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
    ]
    last_error = None
    for url in check_urls:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "curl/8.0"})
            ip = (resp.text or "").strip().split()[0]
            if ip:
                return ip
        except Exception as e:
            last_error = e
    raise RuntimeError(f"获取本机公网IP失败: {last_error}")


def redial_adsl_and_get_ip(profile=None, min_interval=None, sleep_func=None, status_obj=None):
    """本机执行 ADSL/PPPoE 重拨并返回 24 小时内未使用过的公网 IP 信息。"""
    global _adsl_last_redial_ts, adsl_status, _adsl_redial_timestamps
    profile = profile or config.get("adsl_profile", "pppoe")
    min_interval = int(min_interval or config.get("adsl_min_redial_interval", 30) or 30)
    max_attempts = max(1, int(config.get("adsl_ip_redial_max_attempts", 10) or 10))
    blacklist_hours = float(config.get("adsl_ip_blacklist_hours", 24) or 24)
    blacklist_window = max(1, int(blacklist_hours * 3600))
    sleeper = sleep_func or interruptible_sleep
    status_ref = status_obj or adsl_status

    for attempt in range(1, max_attempts + 1):
        elapsed = time.time() - _adsl_last_redial_ts if _adsl_last_redial_ts else min_interval
        if elapsed < min_interval:
            wait_sec = min_interval - elapsed
            status_ref["status"] = "等待重拨间隔"
            log.warning(f"[ADSL] 距离上次重拨不足 {min_interval}s，等待 {wait_sec:.1f}s")
            if not sleeper(wait_sec):
                raise RuntimeError("ADSL任务已停止")

        status_ref["status"] = f"断开拨号中 {attempt}/{max_attempts}"
        log.info(f"[ADSL] 正在断开旧拨号连接: poff {profile}（第 {attempt}/{max_attempts} 次）")
        subprocess.run(["poff", profile], check=False, capture_output=True, timeout=5)
        if not sleeper(3):
            raise RuntimeError("ADSL任务已停止")

        status_ref["status"] = f"重新拨号中 {attempt}/{max_attempts}"
        log.info(f"[ADSL] 正在重新拨号: pon {profile}（第 {attempt}/{max_attempts} 次）")
        subprocess.run(["pon", profile], check=False, capture_output=True, timeout=10)
        _adsl_last_redial_ts = time.time()
        status_ref["last_redial_time"] = time.strftime('%Y-%m-%d %H:%M:%S')
        # IP 切换频率自我监测：统计最近 5 分钟内的重拨次数，过于频繁时告警（避免短时频繁换IP被关联）
        _now_ts = time.time()
        _adsl_redial_timestamps.append(_now_ts)
        _adsl_redial_timestamps[:] = [t for t in _adsl_redial_timestamps if _now_ts - t <= 300]
        _redial_freq_threshold = int(config.get("adsl_redial_freq_warn_5min", 12) or 12)
        if len(_adsl_redial_timestamps) > _redial_freq_threshold:
            log.warning(f"[ADSL] ⚠️ 最近5分钟已重拨 {len(_adsl_redial_timestamps)} 次（阈值{_redial_freq_threshold}），IP切换过于频繁存在被关联风险，建议放慢任务节奏")

        status_ref["status"] = "等待网络恢复"
        log.info(f"[ADSL] 等待网络恢复 {min_interval}s")
        if not sleeper(min_interval):
            raise RuntimeError("ADSL任务已停止")

        status_ref["status"] = "获取本机IP"
        public_ip = get_direct_public_ip()
        status_ref["current_ip"] = public_ip
        log.info(f"[ADSL] 当前本机公网IP: {public_ip}")

        seen_recently, recent_record = _adsl_ip_seen_recently(public_ip, blacklist_window)
        if seen_recently:
            last_seen_text = (recent_record or {}).get("last_seen_text") or "未知时间"
            status_ref["status"] = "IP重复，重新拨号"
            log.warning(f"[ADSL] IP {public_ip} 在 {blacklist_hours:g} 小时内已出现（上次: {last_seen_text}），废弃并重新拨号")
            continue

        status_ref["status"] = "解析IP信息"
        resolved = resolve_ip_info(public_ip)
        if not isinstance(resolved, dict):
            resolved = {}
        resolved["ip"] = public_ip
        valid_ip_info, invalid_reason = _validate_resolved_ip_info(public_ip, resolved)
        if not valid_ip_info:
            status_ref["status"] = "IP三要素无效，重新拨号"
            log.warning(f"[ADSL] IP {public_ip} 三要素硬校验失败: {invalid_reason}，废弃并重新拨号")
            continue
        # IP 类型提示（住宅/数据中心/移动/代理）；数据中心IP对广告风控不利，给出告警
        _ip_type = resolved.get("ip_type")
        if _ip_type:
            if _ip_type in ("datacenter", "proxy"):
                log.warning(f"[ADSL] ⚠️ IP {public_ip} 类型={_ip_type}（数据中心/代理，广告风控高危，建议关注）")
            else:
                log.info(f"[ADSL] IP {public_ip} 类型={_ip_type}")
        sync_process_timezone_to_ip(resolved)
        _record_adsl_ip_use(public_ip, resolved)
        status_ref["country"] = resolved.get("country_code") or resolved.get("country_name") or ""
        log.info(f"[ADSL] IP三要素硬校验通过并写入24小时黑名单: ip={public_ip}, country={status_ref['country']} timezone={resolved.get('timezone')} language={resolved.get('language')}")
        return public_ip, resolved

    raise RuntimeError(f"ADSL连续 {max_attempts} 次重拨都未获得 {blacklist_hours:g} 小时内未使用的新IP")


def check_ip_leak_robust(page, expected_ip):
    """
    结构化 IP 泄漏检测 —— 保留多端点验证，但把失败语义明确分开：

    返回值 (status, real_ip, webrtc_status)：
      status = "pass"        → 至少一个检测端点返回 IP，且精确等于 expected_ip
      status = "mismatch"    → 检测端点返回的 IP 明确不等于 expected_ip（真正泄漏）
      status = "unreachable" → 所有检测端点都访问不到（代理不稳定，不是泄漏）
      status = "skip"        → 配置明确跳过

    注意：检测完毕必须尽量把页面恢复到原来 URL，否则后续操作会在检测站点上执行。
    """
    # 阶段2默认不再让浏览器访问外部 IP 检测站。
    # 这些检测站在 SOCKS5/动态住宅出口下经常超时，会把页面停在 about:blank，干扰后续目标站导航。
    # 出口可用性由 6666 控制面 + VPS 目标站测速保证；这里只保留 WebRTC 本地状态检查。
    if False and config.get("skip_browser_ip_check", False):
        try:
            actual_webrtc_status = page_eval(page, """() => {
                try {
                    if (typeof window.RTCPeerConnection === 'undefined' &&
                        typeof window.webkitRTCPeerConnection === 'undefined') {
                        return 'disabled';
                    }
                    return 'enabled';
                } catch(e) {
                    return 'disabled';
                }
            }""", default="disabled")
        except Exception:
            actual_webrtc_status = "disabled"
        log.warning("[IP检测] 已跳过浏览器外部IP检测URL（阶段2默认）；避免检测站超时影响目标站导航")
        return "unreachable", expected_ip or "unreachable", actual_webrtc_status

    # 明确跳过
    if config.get("skip_ip_leak_check", False) or not config.get("webrtc_leak_check_enabled", True):
        log.warning("[IP检测] WebRTC/IP泄漏检测已关闭，返回 skip")
        return "skip", expected_ip or "skip", "disabled"

    actual_webrtc_status = "disabled"

    # 记录当前 URL（用于检测后恢复）
    _origin_url = None
    try:
        _u = page_url_safe(page, "")
        if isinstance(_u, str) and _u.lower().startswith(("http://", "https://")):
            _origin_url = _u
    except Exception:
        _origin_url = None

    def _restore():
        if _origin_url:
            try:
                page.goto(_origin_url, timeout=15000, wait_until="domcontentloaded")
            except Exception:
                pass

    try:
        # ========== 1. 多端点 IP 验证 ==========
        # 优先选纯文本响应、速度快的端点；同时支持 IPv4/IPv6 匹配
        ip_check_urls = [
            "https://api.ipify.org?format=text",
            "https://ifconfig.me/ip",
            "https://checkip.amazonaws.com",
            "https://ident.me",
            "http://icanhazip.com",
        ]
        log.info(f"[IP检测] 开始验证 - 期望IP: {expected_ip}")

        import re
        _ipv4_re = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
        # IPv6：8 组 1-4 位十六进制，允许 :: 压缩
        _ipv6_re = re.compile(
            r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|"
            r"(?:[0-9a-fA-F]{1,4}:){1,7}:|"
            r"::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}"
        )
        ip_errors = []
        detected_ip_final = None
        pass_count = 0
        mismatch_details = []  # 收集 mismatch，只当多数不匹配时才返回 mismatch

        for check_url in ip_check_urls:
            try:
                log.debug(f"[IP检测] 正在访问 {check_url}...")
                human_model_tick("check_ip_leak_robust")
                page.goto(check_url, timeout=10000, wait_until="domcontentloaded")
                # 用 page_eval 安全地读取 body 文本
                raw_text = page_eval(page, "() => (document.body.innerText || document.body.textContent || '').trim()", default="")
                if not isinstance(raw_text, str):
                    raw_text = ""
                raw_text = raw_text.strip()
                detected_ip = None
                if raw_text:
                    # 先匹配 IPv4，再匹配 IPv6
                    m4 = _ipv4_re.search(raw_text)
                    if m4:
                        detected_ip = m4.group(0)
                    else:
                        m6 = _ipv6_re.search(raw_text)
                        if m6:
                            detected_ip = m6.group(0)
                if not detected_ip:
                    ip_errors.append(f"{check_url}: 响应格式异常({raw_text[:80]})")
                    human_model_tick("check_ip_leak_robust")
                    continue
                # 精确比对（大小写不敏感，便于 IPv6）
                if detected_ip.strip().lower() != expected_ip.strip().lower():
                    # 单个端点不匹配不要立刻终止，可能是检测站的缓存/代理问题
                    mismatch_details.append(f"{check_url}: expect={expected_ip}, actual={detected_ip}")
                    log.debug(f"[IP检测] 单端不匹配: {check_url} -> {detected_ip}")
                    human_model_tick("check_ip_leak_robust")
                    continue
                # 精确匹配成功
                pass_count += 1
                detected_ip_final = detected_ip
                if pass_count >= 2:
                    log.info(f"[IP检测] ✓ pass: {detected_ip}（检测站={check_url}，已累计 {pass_count} 次通过）")
                    human_model_tick("check_ip_leak_robust")
                    break
            except Exception as e:
                ip_errors.append(f"{check_url}: {type(e).__name__}")
                log.debug(f"[IP检测] {check_url}: {type(e).__name__}")
                human_model_tick("check_ip_leak_robust")

        # 如果只拿到 mismatch，且没有任何 pass —— 才认为真正的 IP 泄漏
        if detected_ip_final is None:
            # 看看是否有有效的 mismatch 信息（多数端点返回不同 IP）
            if len(mismatch_details) >= 2 and len(mismatch_details) >= len(ip_errors):
                first_mismatch = mismatch_details[0].split("actual=")[-1] if mismatch_details else "unknown"
                log.warning(f"[IP检测] ⚠️ mismatch: 多数检测站返回与预期不同的 IP —— {'; '.join(mismatch_details[:3])}")
                _restore()
                return "mismatch", first_mismatch, actual_webrtc_status
            detail = " | ".join(ip_errors[:5]) if ip_errors else "所有IP检测端点均失败"
            log.warning(
                f"[IP检测] ⚠️ unreachable: 检测端点不可达（仅诊断，不终止任务）"
                f" —— {detail}"
            )
            _restore()
            # 不可达不等于泄漏：把 expected_ip 作为真实 IP，继续任务
            return "unreachable", expected_ip or "unreachable", "disabled"

        # ========== 2. WebRTC 简单验证 ==========
        try:
            actual_webrtc_status = page_eval(page, """() => {
                try {
                    if (typeof window.RTCPeerConnection === 'undefined' &&
                        typeof window.webkitRTCPeerConnection === 'undefined') {
                        return 'disabled';
                    }
                    return 'enabled';
                } catch(e) {
                    return 'disabled';
                }
            }""", default="disabled")
            if actual_webrtc_status == "enabled":
                log.warning("[IP检测] WebRTC=enabled（应进一步排查，但不直接终止任务）")
            else:
                log.debug("[IP检测] WebRTC=disabled ✓")
        except Exception:
            actual_webrtc_status = "disabled"

        # ========== 3. 通过 ==========
        log.info("[IP检测] ✓ pass")
        _restore()
        return "pass", detected_ip_final, actual_webrtc_status

    except Exception as e:
        log.error(f"[IP检测] 异常: {str(e)[:200]}")
        # 异常时仍然返回 "unreachable"（检测异常≠泄漏），避免把一个普通异常变成整个任务失败
        return "unreachable", expected_ip or "unreachable", "disabled"

def worker_task(single_task=False, adsl_ip_task=False):
    global task_running, _single_task_mode, stats, pending_plan, planned_total_tasks, current_task_idx, current_plan, adsl_status
    stats["total"] = 0
    stats["success"] = 0
    stats["fail"] = 0

    log.info("任务已启动")
    task_running = True
    _single_task_mode = single_task
    start_human_model("website_adsl" if adsl_ip_task else "website")
    if adsl_ip_task:
        adsl_status.update({
            "running": True,
            "status": "准备中",
            "total": max(1, min(999, int(config.get("adsl_task_count", 1) or 1))),
            "completed": 0,
            "current": 0,
            "current_ip": "",
            "country": "",
            "last_error": ""
        })

    # ========== Step 0: 执行前校验网页浏览模式配置
    try:
        ok, errors = validate_web_navigation_config(config, fail_hard=False)
        if not ok:
            log.error(f"❌ 网页浏览模式配置错误，任务终止: {'; '.join(errors)}")
            stats["fail"] += 0
            task_running = False
            _single_task_mode = False
            return
    except Exception as e:
        log.error(f"❌ 网页浏览模式配置校验出错: {str(e)}")
        task_running = False
        _single_task_mode = False
        return

    # ========== Step A: 获取任务清单
    if single_task:
        log.info("🧪 单独任务模式：不使用/不生成计划，仅立即执行网站任务")
        try:
            task_count = max(1, min(999, int(config.get("adsl_task_count", 1) or 1))) if adsl_ip_task else 1
            selected_proxy = None
            if adsl_ip_task:
                log.info(f"[ADSL] ADSL IP任务模式：计划执行 {task_count} 次，每轮本机重拨后直连")
            else:
                proxy_pool_enabled = [p for p in config.get("proxy_pool", []) if p.get("enabled", False) and p.get("proxy_api_url")]
                log.info(f"🧪 单独任务：勾选代理池数量={len(proxy_pool_enabled)}")
                if not proxy_pool_enabled:
                    proxy_pool_enabled = [{
                        "country_code": "US",
                        "proxy_api_url": config.get("ip_proxy_api", ""),
                        "proxy_user": config.get("ip_proxy_user", ""),
                        "proxy_pwd": config.get("ip_proxy_pwd", "")
                    }]
                    log.warning("🧪 单独任务：未找到勾选代理池，回退旧单代理配置")
                available_proxies = get_available_proxies(proxy_pool_enabled) or proxy_pool_enabled
                selected_proxy = random.choice(available_proxies)
                log.info(f"🧪 单独任务：选择代理国家={selected_proxy.get('country_code', 'US')}")
            total_stay_cfg = config.get("total_stay", {"min": 120, "max": 300})
            stay_min = float(total_stay_cfg.get("min", 120))
            stay_max = float(total_stay_cfg.get("max", 300))
            if stay_max < stay_min:
                stay_min, stay_max = stay_max, stay_min
            import datetime as _dt
            _now_utc = _dt.datetime.now(pytz.UTC)
            _today_utc_start = _now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            _now_sec_utc = int((_now_utc - _today_utc_start).total_seconds())
            tasks = []
            
            if adsl_ip_task:
                country_code = "ADSL"
            elif selected_proxy:
                country_code = selected_proxy.get("country_code", "US")
            else:
                log.warning("🧪 单独任务：未找到可用代理，使用默认国家 US")
                country_code = "US"
                selected_proxy = {"proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""}
            for i in range(task_count):
                browse_duration = random.uniform(stay_min, stay_max)
                tasks.append({
                    "idx": i + 1,
                    "plan_time": _now_utc.astimezone().strftime('%Y-%m-%d %H:%M:%S'),
                    "ideal_start": _now_sec_utc,
                    "actual_start": _now_sec_utc,
                    "actual_end": _now_sec_utc + int(browse_duration),
                    "task_gap": 0,
                    "browse_duration": browse_duration,
                    "task_duration": int(browse_duration),
                    "proxy_api_url": "" if adsl_ip_task else selected_proxy.get("proxy_api_url"),
                    "proxy_user": "" if adsl_ip_task else selected_proxy.get("proxy_user"),
                    "proxy_pwd": "" if adsl_ip_task else selected_proxy.get("proxy_pwd"),
                    "proxy_country": country_code,
                    "ip_mode": "adsl" if adsl_ip_task else "proxy",
                    "status": "未完成"
                })
            daily_plan = {
                "total_tasks": task_count,
                "planned_tasks": task_count,
                "model_used": "adsl_single_task" if adsl_ip_task else "single_task",
                "site_age": get_site_age_category(config.get("site_creation_date", "")),
                "tasks": tasks,
                "country_distribution": {country_code: task_count},
                "warnings": []
            }
            current_plan = daily_plan
        except Exception as e:
            import traceback as _tb
            log.error(f"❌ 单独任务创建失败: {type(e).__name__}: {e}")
            log.error(f"❌ 单独任务创建堆栈: {_tb.format_exc()[:800]}")
            stats["fail"] += 1
            task_running = False
            _single_task_mode = False
            current_task_idx = -1
            current_plan = None
            return
    elif pending_plan is not None:
        log.info("📋 使用已存在的待执行计划...")
        daily_plan = pending_plan
        current_plan = pending_plan
        pending_plan = None
    else:
        log.info("📋 正在生成今日任务清单...")
        try:
            daily_plan = generate_daily_tasks(config)
        except Exception as e:
            import traceback as _tb
            _err = f"生成任务清单失败: {type(e).__name__}: {e}"
            log.error(f"❌ {_err}")
            log.error(f"❌ 堆栈: {_tb.format_exc()[:800]}")
            stats["fail"] += 1
            task_running = False
            _single_task_mode = False
            return
        current_plan = daily_plan  # 确保current_plan被正确设置
    total_tasks = daily_plan["total_tasks"]
    model_used = daily_plan["model_used"]
    site_age = daily_plan["site_age"]
    tasks_list = daily_plan["tasks"]
    log.info(
        f"✅ 任务清单生成成功: total={total_tasks}, model={model_used}, site_age={site_age}"
    )
    
    # 设置显示的总任务数
    planned_total_tasks = total_tasks
    
    # 获取启用的代理池
    proxy_pool_enabled = [p for p in config.get("proxy_pool", []) if p.get("enabled", False) and p.get("proxy_api_url")]
    if not proxy_pool_enabled:
        # 回退到旧代理
        proxy_pool_enabled = [
            {
                "country_code": "US",
                "proxy_api_url": config.get("ip_proxy_api", ""),
                "proxy_user": config.get("ip_proxy_user", ""),
                "proxy_pwd": config.get("ip_proxy_pwd", "")
            }
        ]
    log.info(f"📍 可用代理池: {len(proxy_pool_enabled)} 个代理")
    
    # 初始化SEO相关模块
    seo_query = get_seo_query()
    # 注：旧版本的 ip_recognizer (本地IP段库) 已被 ip_info_resolver 取代，不再需要
    
    # 记录上一个任务的结束时间
    last_task_end_time = 0
    
    with sync_playwright() as p:
        for task_idx, task in enumerate(tasks_list):
            # 更新当前任务索引
            current_task_idx = task_idx
            if task_idx < len(tasks_list):
                tasks_list[task_idx]["status"] = "执行中"
                if current_plan and task_idx < len(current_plan.get('tasks', [])):
                    current_plan['tasks'][task_idx]['status'] = "执行中"
            
            if not task_running:
                log.warning("⛔ 任务已停止（任务清单遍历中）")
                break
                        
            # ========== Step B: 使用任务计划中预定的代理国家（v1.60） ==========
            planned_country = task.get('proxy_country', 'US')
            current_task = dict(task)
            # 在代理池中找到匹配的代理
            log.info(f"🎯 任务计划代理国家: {planned_country}")
            matched_proxies = [p for p in proxy_pool_enabled if p.get('country_code') == planned_country]
            if not matched_proxies:
                # 如果找不到预定国家的代理，从可用代理中选一个（兆底切换）
                log.warning(f"⚠️ 未找到 {planned_country} 代理，启动兆底切换")
                available_proxies = get_available_proxies(proxy_pool_enabled)
                if not available_proxies:
                    log.error("❌ 没有可用代理（工作时间内），跳过本任务")
                    stats["fail"] += 1
                    stats["total"] += 1
                    continue
                selected_proxy = random.choice(available_proxies)
                log.info(f"🔄 兆底切换至: {selected_proxy.get('country_code')}")
            else:
                selected_proxy = random.choice(matched_proxies)
                log.info(f"✅ 使用计划代理: {selected_proxy.get('country_code')}")
            current_task['proxy_api_url'] = selected_proxy.get('proxy_api_url')
            current_task['proxy_user'] = selected_proxy.get('proxy_user')
            current_task['proxy_pwd'] = selected_proxy.get('proxy_pwd')
            current_task['proxy_country'] = selected_proxy.get('country_code', 'US')
            
            task_start_time = time.time()
            log.task_separator(task_idx + 1, total_tasks)
            
            # 多天计划优先显示完整计划时间
            _actual_start_sec = current_task.get("actual_start", 0)
            _start_str = current_task.get("plan_time") or "00:00:00"
            log.info(
                f"📌 当前任务: {current_task['idx']}/{total_tasks}, "
                f"计划开始时间={_start_str}, "
                f"预估时长={current_task.get('task_duration', 0):.1f}s, "
                f"代理国家={current_task['proxy_country']}"
            )

            # ========== 计算等待时间 ==========
            import datetime as _dt
            _now_utc = _dt.datetime.now(pytz.UTC)
            _now_epoch = int(_now_utc.timestamp())
            
            wait_sec = 0
            
            # 按计划时间执行；多天计划优先使用 epoch，避免跨天任务被连续跑完
            if current_task.get("actual_start_epoch"):
                wait_sec = max(0, int(current_task.get("actual_start_epoch", _now_epoch)) - _now_epoch)
            elif task_idx == 0:
                _today_utc_start = _now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                _now_sec_utc = (_now_utc - _today_utc_start).total_seconds()
                wait_sec = max(0, _actual_start_sec - _now_sec_utc)
            else:
                interval_min = config.get("task_interval", {}).get("min", 20)
                interval_max = config.get("task_interval", {}).get("max", 40)
                wait_sec = random.uniform(interval_min, interval_max)
                # 增强随机性：10% 分心暂停 + 高斯微抖动
                if random.random() < 0.10:
                    wait_sec += random.uniform(30, 90)
                wait_sec = max(3, wait_sec * (1 + random.gauss(0, 0.12)))
                log.info(f"⏳ 任务间隔等待 {wait_sec:.1f} 秒...")
            
            if wait_sec > 0:
                log.info(f"⏳ 等待 {wait_sec:.1f} 秒后开始任务...")
                if not interruptible_sleep(wait_sec):
                    log.warning("⛔ 任务已停止（等待中）")
                    break
            
            # 增加总任务计数
            stats["total"] += 1
            
            browser = None
            try:
                # ==================== 代理模块（IP 三要素 + SEO 可用性 严格验证 + 重试 3 次） ====================
                proxy_info = None
                layer1_success = False
                ipdeep_success = False
                ip_success = False
                
                # 更新任务状态为执行中
                if current_plan and task_idx < len(current_plan.get('tasks', [])):
                    current_plan['tasks'][task_idx]['status'] = '执行中'
                    current_plan['tasks'][task_idx]['start_time'] = time.strftime('%H:%M:%S')
                exit_ip = "未知"
                real_ip = "未知"
                country = "未知"
                region = "未知"
                city = "未知"
                timezone = "未知"
                language = "未知"
                resolved_ip_info = None
                
                # —— SEO 在重试循环里准备 ——
                selected_engine_id = None
                selected_keyword = None
                generated_referer = None
                ip_region = None
                
                ip_retry_max = 3  # 舍弃 IP 后最多重试 3 次
                seo_ready = False  # 是否成功准备好 SEO（必须为 True 才能继续）

                # ⏱️ 前置流程计时起点：从拨号/取IP开始
                dial_start_time = time.time()
                enter_site_time = None  # 进入网站（首页加载完成）时间锚点

                for ip_attempt in range(ip_retry_max):
                    # —— 关键：每次循环开头都检查停止信号 ——
                    if not task_running:
                        log.warning("⛔ 任务已停止（IP 重试循环中）")
                        break
                    try:
                        # 直连 IPDeep 获取代理
                        if ip_attempt > 0:
                            log.warning(f"🔁 重新获取代理 IP（第 {ip_attempt+1}/{ip_retry_max} 次）...")
                        else:
                            log.info(f"正在从 IPDeep 获取代理 (国家: {current_task['proxy_country']})...")
                        # ========== 超时保护：防止请求无限卡死 ==========
                        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                        
                        def _fetch_proxy_with_timeout():
                            """带超时的代理获取函数"""
                            try:
                                result = get_proxy_from_api_url(
                                    current_task["proxy_api_url"],
                                    current_task["proxy_user"],
                                    current_task["proxy_pwd"],
                                    current_task["proxy_country"]
                                )
                                return result
                            except Exception as e:
                                log.error(f"❌ IPDeep代理获取异常: {type(e).__name__}: {e}")
                                return {"success": False, "error": f"{type(e).__name__}: {e}"}
                        
                        # 使用线程池执行器，设置45秒超时
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(_fetch_proxy_with_timeout)
                            try:
                                proxy_info = future.result(timeout=45)
                                log.info(f"✅ IPDeep代理获取完成")
                            except FuturesTimeoutError:
                                log.error(f"⛔ IPDeep代理获取超时(45s)，强制取消任务")
                                proxy_info = {"success": False, "error": "IPDeep请求超时(45s)"}
                            except Exception as e:
                                log.error(f" IPDeep代理获取失败: {type(e).__name__}: {e}")
                                proxy_info = {"success": False, "error": f"{type(e).__name__}: {e}"}
                        
                        # 记录获取结果
                        if proxy_info and proxy_info.get("success"):
                            log.info(f"✅ IPDeep代理获取成功: {proxy_info.get('proxy_host')}:{proxy_info.get('proxy_port')}")
                        else:
                            err_msg = proxy_info.get('error', '未知错误')[:100] if proxy_info else '返回None'
                            log.warning(f"❌ IPDeep代理获取失败: {err_msg}")
                        
                        # 检查代理获取是否成功
                        if proxy_info is None:
                            log.warning(f"⚠️ 第 {ip_attempt+1} 次代理获取失败（返回 None）")
                            continue
                        
                        # 检查代理是否包含成功标记
                        if not isinstance(proxy_info, dict) or "success" not in proxy_info:
                            log.warning(f"⚠️ 第 {ip_attempt+1} 次代理响应格式不正确")
                            continue
                        
                        layer1_success = proxy_info.get("success", False)
                        
                        if not layer1_success:
                            log.warning(f"⚠️ 第 {ip_attempt+1} 次代理获取失败")
                            continue
                        
                        ipdeep_success = True
                        ip_info = proxy_info.get("ip_info", {})
                        if not ip_info.get("success", False):
                            log.warning(
                                f"⚠️ 第 {ip_attempt+1} 次代理 ip_info 失败/缺失 "
                                f"(ip_info={str(ip_info)[:200]})，舍弃换下一个 IP"
                            )
                            continue

                        exit_ip = ip_info.get("ip", "未知")
                        region = ip_info.get("region", "未知") or "未知"
                        city = ip_info.get("city", "未知") or "未知"

                        # ===== 新增：proxy_host / proxy_port 校验（避免后续拼接字符串崩溃）=====
                        proxy_host = proxy_info.get("proxy_host")
                        proxy_port = proxy_info.get("proxy_port")
                        if not proxy_host or not proxy_port:
                            log.warning(
                                f"⚠️ 第 {ip_attempt+1} 次代理缺少 proxy_host/proxy_port "
                                f"(host={proxy_host}, port={proxy_port})，舍弃换下一个 IP"
                            )
                            continue
                        # 统一转为字符串，避免拼接 f"http://{host}:{port}" 时崩溃
                        proxy_host = str(proxy_host).strip()
                        proxy_port = str(proxy_port).strip()
                        if not proxy_host or not proxy_port:
                            log.warning(
                                f"⚠️ 第 {ip_attempt+1} 次代理 proxy_host/proxy_port 为空串，舍弃"
                            )
                            continue

                        # ===== C段分散检查：避免同/24子网集中访问触发 AdSense 风控 =====
                        if exit_ip and exit_ip != "未知":
                            if not _ip_provider.check_c_segment_diversity(exit_ip):
                                log.warning(
                                    f"⚠️ IP {exit_ip} 的C段({_ip_provider.get_c_segment(exit_ip)}.0/24)"
                                    f"近期使用过多，舍弃换下一个 IP"
                                )
                                continue
                            _ip_provider.record_c_segment_use(exit_ip)

                        # —— Step 1: 精准识别 IP 三要素 country/timezone/language ——
                        log.info(f"🔎 精准识别 IP 信息: {exit_ip}")
                        resolved_ip_info = resolve_ip_info(exit_ip, proxy_ip_info=ip_info)

                        # 统一把 resolved_ip_info 规范化为 dict（即使外部 API 出问题也不崩）
                        if not isinstance(resolved_ip_info, dict):
                            log.warning(
                                f"⚠️ resolve_ip_info 返回非 dict: {type(resolved_ip_info).__name__} "
                                f"→ 回退为 VPS ip_info"
                            )
                            resolved_ip_info = {
                                "success": False,
                                "ip": exit_ip,
                                "country_code": None,
                                "country_name": None,
                                "timezone": None,
                                "language": None,
                                "source": [],
                            }

                        if not resolved_ip_info.get("success"):
                            # 主数据源失败 → 回退到 VPS 返回的 ip_info
                            log.warning(
                                f"⚠️ IP {exit_ip} resolve_ip_info 失败 "
                                f"(country={resolved_ip_info.get('country_code')}, "
                                f"timezone={resolved_ip_info.get('timezone')}, "
                                f"language={resolved_ip_info.get('language')}) "
                                f"→ 回退使用 VPS 返回的 ip_info"
                            )
                            _cc = (
                                (ip_info.get("country_code") or "")
                                or (ip_info.get("country") or "")
                            ).strip().upper()
                            _country_name = (
                                (ip_info.get("country_name") or "")
                                or (ip_info.get("country") or "")
                            ).strip()
                            _region = (ip_info.get("region") or "").strip()
                            _city = (ip_info.get("city") or "").strip()
                            _timezone = (
                                (ip_info.get("timezone") or "")
                                or ip_info.get("tz")
                                or ""
                            ).strip()
                            _language = (ip_info.get("language") or "").strip()

                            if not _cc:
                                log.warning(
                                    f"⚠️ VPS 返回的 ip_info 也没有国家代码，"
                                    f"无法构造完整三要素，舍弃换下一个 IP"
                                )
                                continue

                            resolved_ip_info = {
                                "success": True,
                                "source": "fallback:ip_info_from_vps",
                                "ip": exit_ip,
                                "country_code": resolved_ip_info.get("country_code") or _cc,
                                "country_name": resolved_ip_info.get("country_name") or _country_name or _cc,
                                "region": resolved_ip_info.get("region") or _region,
                                "city": resolved_ip_info.get("city") or _city,
                                "timezone": resolved_ip_info.get("timezone") or _timezone,
                                "language": resolved_ip_info.get("language") or _language,
                            }
                            if not resolved_ip_info["timezone"]:
                                resolved_ip_info["timezone"] = "Etc/UTC"
                            if not resolved_ip_info["language"]:
                                resolved_ip_info["language"] = "en-US"
                            log.info(
                                f"🗂️ 使用 VPS ip_info 构造三要素: "
                                f"country={resolved_ip_info['country_name']}, "
                                f"timezone={resolved_ip_info['timezone']}, "
                                f"language={resolved_ip_info['language']}"
                            )

                        country = resolved_ip_info.get("country_name") or resolved_ip_info.get("country_code") or "未知"
                        timezone = resolved_ip_info.get("timezone") or "Etc/UTC"
                        language = resolved_ip_info.get("language") or "en-US"
                        cc_upper = (resolved_ip_info.get("country_code") or "").upper()
                        
                        lang_lower = (language or "").lower()
                        
                        log.info(
                            f"✅ IP 三要素识别成功 country={country}, timezone={timezone}, "
                            f"language={language}, source={resolved_ip_info.get('source')}"
                        )
                        # —— Step 2: 决定 SEO 区域（严禁跳过 SEO，不可支持的语言直接舍弃 IP） ——
                        if not config.get("enable_seo", True):
                            log.error("❌ enable_seo=False，无法启动任务（严禁跳过 SEO）")
                            ip_region = REGION_FAILED
                            break
                        
                        # 根据IP国别代码匹配region_engine_map
                        region_map = config.get('seo', {}).get('region_engine_map', {})
                        if cc_upper in region_map:
                            ip_region = cc_upper
                            log.info(f"✓ country={cc_upper} → 匹配国别平台映射 {ip_region}")
                        elif lang_lower.startswith("zh"):
                            ip_region = "CN"
                            log.info(f"✓ language=zh → CN 平台映射")
                        elif lang_lower.startswith("en"):
                            ip_region = "US"
                            log.info(f"✓ language={language} → US 平台映射")
                        elif lang_lower.startswith(("de", "fr", "it", "es", "nl", "sv", "no", "da", "fi", "pl", "pt", "el", "ja", "ko")):
                            # 非英语欧美/日韩 → 用US（英文）平台兜底
                            ip_region = "US"
                            log.info(f"✓ language={language}（欧美/日韩）→ US 平台兜底")
                        else:
                            log.warning(f"⚠️ language={language} 不在 SEO 支持范围，舍弃 IP 换下一个")
                            continue
                        
                        # —— Step 3: 选搜索引擎/社媒平台 / 关键词 / 生成 Referer ——
                        log.info(f"根据国别选择平台: {ip_region}")
                        selected_engine_id = seo_query.get_random_engine_for_region(ip_region)
                        if not selected_engine_id:
                            log.warning(f"⚠️ 国别 {ip_region} 没有可用平台，舍弃 IP 换下一个")
                            continue
                        
                        selected_keyword = seo_query.get_random_keyword_for_engine(selected_engine_id)
                        if not selected_keyword:
                            log.warning(f"⚠️ 平台 {selected_engine_id} 没有可用关键词，舍弃 IP 换下一个")
                            continue
                        
                        generated_referer = seo_query.generate_referer(selected_engine_id, selected_keyword)
                        if not generated_referer:
                            log.warning(f"⚠️ Referer 生成失败，舍弃 IP 换下一个")
                            continue
                        
                        # —— 全部成功 ——
                        ip_success = True
                        seo_ready = True
                        log.info(
                            f"✓ SEO流量模拟准备就绪: 地域={ip_region}, 引擎={selected_engine_id}, "
                            f"关键词={selected_keyword}, Referer={generated_referer}"
                        )
                        break  # 成功，跳出 IP 重试循环
                    except Exception as e:
                        log.error(f"获取/识别代理 IP 失败（第 {ip_attempt+1} 次）: {str(e)}")
                
                log.proxy_module(layer1_success, ipdeep_success, ip_success, exit_ip, real_ip, country, region, city, timezone, language)
                
                # 中途停止或最终失败 → 直接进入下一轮
                if not task_running:
                    stats["fail"] += 1
                    task_time = time.time() - task_start_time
                    log.task_result(task_time, False, False, "任务已停止")
                    continue
                
                if not seo_ready:
                    stats["fail"] += 1
                    task_time = time.time() - task_start_time
                    log.task_result(task_time, False, False, f"严禁跳过SEO：IP+SEO 准备失败（已重试 {ip_retry_max} 次）")
                    continue
                
                # SEO 模块已合并到 IP 重试循环中（严禁跳过 SEO，IP+SEO 一起验证）
                
                # ==================== 指纹浏览器模块 ====================
                browser_success = False
                fingerprint_success = False
                fingerprint_id = "未知"
                user_agent = "未知"
                resolution = "未知"
                webrtc = "disabled"
                canvas = "未知"
                webgl = "未知"
                consistency = False
                consistency_details = ""
                
                # 检查 proxy_info 是否为 None
                if proxy_info is None:
                    log.error("❌ 代理信息为 None，无法生成指纹和配置")
                    stats["fail"] += 1
                    continue
                
                # 生成与IP完全匹配的指纹
                try:
                    fingerprint = generate_fingerprint(resolved_ip_info)
                    fingerprint_success = True
                    fingerprint_id = fingerprint["fingerprint_id"]
                    user_agent = fingerprint["user_agent"]
                    qa_log_fingerprint_ip_consistency(resolved_ip_info, fingerprint)
                    resolution = fingerprint["resolution"]
                    stable_desktop_resolutions = [
                        "1366x768", "1440x900", "1536x864",
                        "1600x900", "1680x1050", "1920x1080"
                    ]
                    if resolution not in stable_desktop_resolutions:
                        original_resolution = resolution
                        resolution = random.choice(stable_desktop_resolutions)
                        fingerprint["resolution"] = resolution
                        log.warning(f"🖥️ 分辨率规整: 原始指纹分辨率={original_resolution}，网站任务使用常见桌面随机分辨率={resolution}")
                    width, height = map(int, resolution.split("x"))
                    canvas = fingerprint["canvas"]
                    webgl = fingerprint["webgl"]
                    webgl_vendor = fingerprint.get("webgl_vendor", "Google Inc. (Intel)")
                    webgl_renderer = fingerprint.get("webgl_renderer", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)")
                    canvas_noise_seed = int(fingerprint.get("canvas_noise_seed", 12345))
                    hardware_concurrency = int(fingerprint.get("hardware_concurrency", 8))
                    device_memory = int(fingerprint.get("device_memory", 8))
                    color_depth = int(fingerprint.get("color_depth", 24))
                    fonts_json = json.dumps(fingerprint.get("fonts", []))
                    # 会话存储随机化种子（仅 new_each_task 模式注入，避免覆盖 country_host_7d 持久化会话）
                    storage_randomize_js = "true" if (get_global_session_mode() != "country_host_7d") else "false"
                    ls_seed_client_id = f"{random.randint(10**9, 10**10-1)}.{random.randint(10**9, 10**10-1)}"
                    ls_seed_session_id = uuid.uuid4().hex
                    log.debug(f"生成的指纹: {fingerprint}")
                except Exception as e:
                    log.error(f"❌ 生成指纹失败: {e}")
                    stats["fail"] += 1
                    continue
                
                # 构建代理配置（直连 IPDeep 代理出网）
                try:
                    proxy_host = proxy_info.get('proxy_host', 'UNKNOWN')
                    proxy_port = proxy_info.get('proxy_port', 'UNKNOWN')
                    
                    log.info(f"[代理配置] proxy_host: {proxy_host}")
                    log.info(f"[代理配置] proxy_port: {proxy_port}")
                    log.info(f"[代理配置] proxy_info 完整内容: {proxy_info}")
                    
                    if not proxy_host or not proxy_port:
                        log.error(f"❌ 代理信息不完整: host={proxy_host}, port={proxy_port}")
                        stats["fail"] += 1
                        continue
                    
                    # 直接使用 IPDeep 返回的 HTTP 代理出网
                    proxy_username = proxy_info.get('proxy_username', '')
                    proxy_password = proxy_info.get('proxy_password', '')
                    if proxy_username and proxy_password:
                        proxy_server = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
                    else:
                        proxy_server = f"http://{proxy_host}:{proxy_port}"
                    proxy_config = {
                        "server": proxy_server,
                    }
                    log.info(f"[代理配置] ✅ 浏览器数据面使用 IPDeep HTTP 代理: {proxy_host}:{proxy_port}")
                except Exception as e:
                    log.error(f"❌ 构建代理配置失败: {e}")
                    log.error(f"❌ 错误类型: {type(e).__name__}")
                    stats["fail"] += 1
                    continue
                
                # 确认代理使用方式
                try:
                    log.info(f"✓ 使用代理访问目标网站: {proxy_host}:{proxy_port}")
                except Exception as e:
                    log.error(f"❌ 确认代理使用方式失败: {e}")
                    stats["fail"] += 1
                    continue

                # ========== Step C-1: 目标网站健康检测（通过代理，仅诊断用，不影响任务执行） ==========
                # 说明：这里仅做诊断记录，不因为检测失败而跳过任务 —— requests 的 TCP/TLS/HTTP2 行为与真实浏览器不同
                _target_urls_cfg = config.get("target_urls")
                if isinstance(_target_urls_cfg, list) and _target_urls_cfg:
                    _target_url = next((item.get("url", "").strip() for item in _target_urls_cfg if item.get("enabled") and item.get("url", "").strip()), config.get("target_url", ""))
                else:
                    _target_url = config.get("target_url", "")
                if _target_url:
                    try:
                        # 使用 IPDeep 代理进行诊断
                        _diag_host = proxy_info.get('proxy_host', '')
                        _diag_port = proxy_info.get('proxy_port', '')
                        _diag_user = proxy_info.get('proxy_username', '')
                        _diag_pwd = proxy_info.get('proxy_password', '')
                        if _diag_user and _diag_pwd:
                            _diag_url = f"http://{_diag_user}:{_diag_pwd}@{_diag_host}:{_diag_port}"
                        else:
                            _diag_url = f"http://{_diag_host}:{_diag_port}"
                        _proxy_for_check = {
                            "http": _diag_url,
                            "https": _diag_url
                        }
                        log.info(f"🩺 [诊断] 通过 IPDeep 代理访问 {_target_url} ...")
                        _health_resp = requests.get(
                            _target_url,
                            proxies=_proxy_for_check,
                            timeout=15,
                            headers={"User-Agent": user_agent}
                        )
                        log.info(
                            f"🩺 [诊断] 目标站访问: HTTP {_health_resp.status_code} "
                            f"(长度≈{len(_health_resp.content or b'')}字节，仅用于诊断，不用于判定任务)"
                        )
                    except requests.exceptions.ConnectionError as _ce:
                        log.warning(
                            f"⚠️ [诊断] 目标站访问 ConnectionError: {str(_ce)[:120]} "
                            f"(仅用于诊断，继续浏览器访问)"
                        )
                    except requests.exceptions.Timeout as _to:
                        log.warning(
                            f"⚠️ [诊断] 目标站访问 Timeout(15s) (仅用于诊断，继续浏览器访问): {str(_to)[:80]}"
                        )
                    except requests.exceptions.ProxyError as _pe:
                        log.warning(
                            f"⚠️ [诊断] 目标站访问 ProxyError (代理认证或链路问题，仅诊断): {str(_pe)[:120]}"
                        )
                    except Exception as _he:
                        log.warning(
                            f"⚠️ [诊断] 目标站访问异常（忽略，继续浏览器访问）: "
                            f"{type(_he).__name__}: {str(_he)[:120]}"
                        )

                # 启动浏览器，强制关闭WebRTC，添加反检测参数
                log.info("正在启动浏览器...")
                _headless_mode = bool(config.get("headless", True))
                _use_real_chrome = bool(config.get("use_real_chrome", True))
                if proxy_config and str(proxy_config.get("server", "")).startswith("http://"):
                    log.info("IPDeep HTTP代理模式：通过Selenium Chrome访问，噪音请求已通过--host-rules和JS hook拦截")
                log.info(f"浏览器模式: {'无头(headless=True)' if _headless_mode else '有界面(headless=False, 调试用)'}，浏览器内核: {'本地 Chrome（带 H.264/AAC）' if _use_real_chrome else '系统Chrome（无专有 codec）'}")
                
                # 使用指纹生成的 User-Agent（与IP匹配）
                selected_ua = user_agent
                
                # 如果使用无头模式，清理 UA 中的 Headless 关键词
                if _headless_mode and selected_ua:
                    selected_ua = selected_ua.replace("HeadlessChrome", "Chrome")
                    log.info(f"[反检测] 无头模式下清理 UA 关键词: {selected_ua[:80]}...")
                
                # 检查 _launch_kwargs 参数类型
                if proxy_config is not None:
                    assert isinstance(proxy_config, dict), f"proxy_config 类型错误，期望 dict，实际类型: {type(proxy_config).__name__}"
                    assert "server" in proxy_config, "proxy_config 缺少 server 字段"
                
                assert isinstance(_headless_mode, bool), f"_headless_mode 类型错误，期望 bool，实际类型: {type(_headless_mode).__name__}"
                
                assert isinstance(selected_ua, str), f"selected_ua 类型错误，期望 str，实际类型: {type(selected_ua).__name__}"
                
                # 构建 _launch_kwargs 字典
                _launch_lang = fingerprint.get("language", "en-US")
                _launch_tz = fingerprint.get("timezone", "America/New_York")
                _launch_args = []
                if proxy_config is not None:
                    _launch_args.extend([
                        f"--proxy-server={proxy_config['server']}",
                        "--proxy-bypass-list=*.google.com;*.googleapis.com;*.gstatic.com;*.gvt1.com;accounts.google.com;clients2.google.com;safebrowsing.googleapis.com;safebrowsinghttpgateway.googleapis.com;httpbin.org;api.ipify.org;icanhazip.com;ifconfig.me;checkip.amazonaws.com;ident.me",
                    ])
                # WebRTC防护与配置面板同步
                if config.get("webrtc_leak_check_enabled", True):
                    _launch_args.extend([
                        "--disable-webrtc",
                        "--disable-webrtc-encryption",
                        "--disable-webrtc-stun-origin",
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--webrtc-max-packet-size=0"
                    ])
                    log.info("WebRTC防泄漏已启用")
                else:
                    log.warning("WebRTC防泄漏已禁用（可能导致IP泄漏风险）")
                
                _launch_args.extend([
                        f"--lang={_launch_lang}",
                        f"--window-size={width},{height}",
                        "--autoplay-policy=no-user-gesture-required",
                        "--mute-audio",
                        "--disable-features=AutoplayIgnoreWebAudio,MediaRouter,Translate,TranslateUI,LanguageDetection,OptimizationHints",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-infobars",
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-popup-blocking",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--safebrowsing-disable-auto-update",
                        "--disable-domain-reliability",
                        "--disable-translate",
                        "--translate-ranker-model-url=0.0.0.0",
                        "--translate-security-origin=0.0.0.0",
                        "--metrics-recording-only",
                        "--disable-component-update",
                        "--disable-component-extensions-with-background-pages",
                        "--disable-sync",
                        "--disable-default-apps",
                        "--disable-hang-monitor",
                        "--disable-prompt-on-repost",
                        "--disable-client-side-phishing-detection",
                        "--disable-password-manager-reauthentication",
                        "--disable-ipc-flooding-protection"
                    ])
                _launch_kwargs = dict(headless=_headless_mode, args=_launch_args)
                
                log.debug(f"_launch_kwargs: {_launch_kwargs}")
                # ✅ 统一改为主线程启动（Selenium WebDriver 需要在主线程操作）
                # ❌ 之前用子线程 _lworker 启动 chromium，会导致
                #    "Task was destroyed but it is pending! / task switched to a different thread"
                #    因为 Selenium WebDriver 实例需要绑定主线程，子线程操作会导致会话异常。
                # ========== 修复：使用 ThreadPoolExecutor 实现可靠的超时保护（子线程兼容） ==========
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                
                def _launch_browser_with_timeout(_pw, _use_chrome_channel, _kwargs, _max_wait_sec=30):
                    """启动浏览器，带超时保护和异常处理（使用ThreadPoolExecutor，支持子线程）"""
                    def _do_launch():
                        try:
                            if _use_chrome_channel:
                                _browser = _pw.chromium.launch(channel="chrome", **_kwargs)
                            else:
                                _browser = _pw.chromium.launch(**_kwargs)
                            return _browser, None
                        except Exception as _e:
                            err_msg = f"{type(_e).__name__}: {str(_e)[:300]}"
                            log.debug(f"️ 浏览器启动异常: {err_msg}")
                            return None, err_msg
                    
                    # 使用线程池执行器实现超时保护
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(_do_launch)
                        try:
                            browser, error = future.result(timeout=_max_wait_sec)
                            return browser, error
                        except FuturesTimeoutError:
                            timeout_err = f"浏览器启动超时（>{_max_wait_sec}s）"
                            log.warning(f"️ {timeout_err}")
                            return None, timeout_err
                        except Exception as e:
                            exec_err = f"执行器异常: {type(e).__name__}: {str(e)[:200]}"
                            log.error(f"❌ {exec_err}")
                            return None, exec_err

                def _minimal_launch_kwargs():
                    args = [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                        f"--lang={_launch_lang}",
                        f"--window-size={width},{height}",
                    ]
                    if proxy_config is not None:
                        args.insert(0, f"--proxy-server={proxy_config['server']}")
                    return {"headless": True, "args": args}

                browser = None
                launch_errors = []
                
                # 启动策略：先尝试本地Chrome（支持H.264），失败后快速降级到系统Chrome
                if _use_real_chrome:
                    log.info("🚀 尝试使用本地 Chrome 启动...")
                    browser, _lerr = _launch_browser_with_timeout(p, True, _launch_kwargs, 30)
                    if browser is not None:
                        log.info("✅ 使用本地 Chrome 启动成功（支持 H.264/AAC）")
                    else:
                        launch_errors.append(f"chrome-full={_lerr or '未知'}")
                        log.warning(f"️ 本地 Chrome 启动失败: {_lerr}")
                
                # 如果本地Chrome失败，尝试系统Chrome（完整参数）
                if browser is None:
                    log.info("🚀 尝试使用系统 Chrome 启动（完整参数）...")
                    browser, _lerr2 = _launch_browser_with_timeout(p, False, _launch_kwargs, 30)
                    if browser is not None:
                        log.info("✅ 使用系统 Chrome 启动成功（可能不支持 HLS/H.264）")
                    else:
                        launch_errors.append(f"chromium-full={_lerr2 or '未知'}")
                        log.warning(f"⚠️ 系统 Chrome 完整参数启动失败: {_lerr2}")
                        
                        # 最后尝试极简参数
                        log.info("🚀 尝试使用极简参数启动...")
                        browser, _lerr3 = _launch_browser_with_timeout(p, False, _minimal_launch_kwargs(), 20)
                        if browser is not None:
                            log.info("✅ 使用 Chrome 极简参数启动成功")
                        else:
                            launch_errors.append(f"chromium-minimal={_lerr3 or '未知'}")
                            log.error(f"❌ 所有浏览器启动方式均失败！")
                            raise RuntimeError(
                                f"浏览器启动失败（已尝试3种方式）:\n" +
                                "\n".join([f"  - {err}" for err in launch_errors])
                            )
                
                # 创建浏览器上下文，严格应用指纹配置
                
                # 构建额外头部：只设置业务需要的 Referer，避免发送非法空代理头
                extra_http_headers = {}
                if generated_referer:
                    extra_http_headers["Referer"] = generated_referer
                    log.info(f"设置Referer头部: {generated_referer}")
                else:
                    default_referers = ["https://www.google.com/", "https://www.bing.com/", "https://www.baidu.com/"]
                    default_referer = random.choice(default_referers)
                    extra_http_headers["Referer"] = default_referer
                    log.info(f"设置默认Referer头部: {default_referer}")
                
                # 添加 Accept-Language 请求头，确保与指纹语言一致
                lang_prefix = fingerprint["language"].split("-")[0]
                accept_language = f"{fingerprint['language']},{lang_prefix};q=0.9,en-US;q=0.8,en;q=0.7"
                extra_http_headers["Accept-Language"] = accept_language
                log.info(f"设置Accept-Language头部: {accept_language}")
                
                # 补全常规浏览器文档请求头，贴近真实 Chrome 导航请求（防止头缺失/异常被风控识别）
                extra_http_headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")
                extra_http_headers.setdefault("Accept-Encoding", "gzip, deflate, br, zstd")
                extra_http_headers.setdefault("Upgrade-Insecure-Requests", "1")
                extra_http_headers.setdefault("Sec-Fetch-Site", "cross-site")
                extra_http_headers.setdefault("Sec-Fetch-Mode", "navigate")
                extra_http_headers.setdefault("Sec-Fetch-User", "?1")
                extra_http_headers.setdefault("Sec-Fetch-Dest", "document")

                # 添加缺失的 Sec-Ch-Ua 请求头（动态匹配UA中的Chrome版本）
                _ua_chrome_ver = "126"  # 默认值
                _ua_match = re.search(r'Chrome/(\d+)', user_agent)
                if _ua_match:
                    _ua_chrome_ver = _ua_match.group(1)
                extra_http_headers["Sec-Ch-Ua"] = f'"Not_A Brand";v="8", "Chromium";v="{_ua_chrome_ver}", "Google Chrome";v="{_ua_chrome_ver}"'
                extra_http_headers["Sec-Ch-Ua-Mobile"] = "?0"
                # 平台一致性：根据UA中的平台信息动态设置
                _ua_platform_str = '"Windows"'
                if 'Mac OS X' in user_agent or 'Macintosh' in user_agent:
                    _ua_platform_str = '"macOS"'
                elif 'Linux' in user_agent and 'Android' not in user_agent:
                    _ua_platform_str = '"Linux"'
                elif 'Android' in user_agent:
                    _ua_platform_str = '"Android"'
                extra_http_headers["Sec-Ch-Ua-Platform"] = _ua_platform_str 
                
                # 根据IP信息动态配置浏览器上下文（与代理IP严格匹配）
                browser_locale = fingerprint.get("language", "en-US")
                browser_timezone = fingerprint.get("timezone", "America/New_York")
                
                log.info(f"🌍 动态配置浏览器 - 语言: {browser_locale}, 时区: {browser_timezone}")
                
                qa_storage_state_path = None
                qa_save_state_path = None
                qa_meta_path = None
                if get_global_session_mode() == "country_host_7d":
                    _urls_cfg = config.get("target_urls")
                    if isinstance(_urls_cfg, list) and _urls_cfg:
                        _first_url = next((item.get("url", "").strip() for item in _urls_cfg if item.get("enabled") and item.get("url", "").strip()), config.get("target_url", ""))
                    else:
                        _first_url = config.get("target_url", "")
                    qa_storage_state_path, qa_save_state_path, qa_meta_path = prepare_qa_storage_state(
                        _first_url,
                        current_task.get("proxy_country", "US")
                    )
                    log.info(f"[QA会话] 使用全局session策略: {get_global_session_mode()}")
                else:
                    log.info("[QA会话] 使用全局session策略: new_each_task，本轮不加载历史会话")

                context_kwargs = dict(
                    user_agent=selected_ua,
                    viewport={"width": width, "height": height},
                    locale=browser_locale,
                    timezone_id=browser_timezone,
                    permissions=[],
                    geolocation=None,
                    device_scale_factor=1,
                    is_mobile=False,
                    has_touch=False,
                    color_scheme="light",
                    extra_http_headers=extra_http_headers if extra_http_headers else None
                )
                if qa_storage_state_path:
                    context_kwargs["storage_state"] = qa_storage_state_path
                context = browser.new_context(**context_kwargs)
                
                log.info(f"✅ 浏览器上下文配置完成 - 语言: {browser_locale}, 时区: {browser_timezone}, 分辨率: {resolution}")
                
                # ========== 覆盖Canvas和WebGL指纹，添加完整反检测 ==========
                context.add_init_script(rf"""
                    // ========== 0. WebRTC IP泄露防护（保留API但过滤内网IP，完全禁用会被风控识别） ==========
                    (function() {{
                        const _OrigRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
                        if (!_OrigRTC) return;
                        const _SafeRTC = function(config) {{
                            // 清除ICE服务器，阻止STUN/TURN探测
                            if (config && config.iceServers) {{ config.iceServers = []; }}
                            const pc = new _OrigRTC(config);
                            const _origAddIce = pc.addIceCandidate.bind(pc);
                            pc.addIceCandidate = function(candidate) {{
                                if (candidate && candidate.candidate) {{
                                    const c = candidate.candidate;
                                    // 过滤内网IP泄露
                                    if (/((10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|0\.0\.0\.0))/.test(c)) {{
                                        return Promise.resolve();
                                    }}
                                }}
                                return _origAddIce(candidate);
                            }};
                            // 监听icecandidate事件，过滤内网IP
                            pc.addEventListener('icecandidate', function(e) {{
                                if (e.candidate && e.candidate.candidate) {{
                                    const c = e.candidate.candidate;
                                    if (/((10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|0\.0\.0\.0))/.test(c)) {{
                                        e.stopImmediatePropagation && e.stopImmediatePropagation();
                                    }}
                                }}
                            }});
                            return pc;
                        }};
                        _SafeRTC.prototype = _OrigRTC.prototype;
                        Object.defineProperty(_SafeRTC, 'toString', {{
                            value: function() {{ return 'function RTCPeerConnection() {{ [native code] }}'; }}
                        }});
                        window.RTCPeerConnection = _SafeRTC;
                        if (window.webkitRTCPeerConnection) {{ window.webkitRTCPeerConnection = _SafeRTC; }}
                    }})();
                    
                    // ========== 1. 隐藏自动化特征 ==========
                    // 隐藏navigator.webdriver
                    Object.defineProperty(navigator, 'webdriver', {{
                        value: undefined,
                        writable: false,
                        configurable: false
                    }});
                    
                    // 修复 headless 模式下 document.visibilityState="hidden" 的致命检测点
                    // 真实用户的当前标签页始终是 "visible"
                    Object.defineProperty(document, 'visibilityState', {{
                        get: function() {{ return 'visible'; }},
                        configurable: true
                    }});
                    Object.defineProperty(document, 'hidden', {{
                        get: function() {{ return false; }},
                        configurable: true
                    }});
                    // 拦截 visibilitychange 事件派发，确保永不触发“页面不可见”回调
                    // （允许注册监听器，但事件永远不会被派发，因为 visibilityState 始终为 visible）
                    const _origDispatchEvent = document.dispatchEvent.bind(document);
                    document.dispatchEvent = function(event) {{
                        if (event && event.type === 'visibilitychange') {{
                            return true;  // 吞掉事件，不派发给监听器
                        }}
                        return _origDispatchEvent(event);
                    }};
                    
                    // 删除chrome的自动化属性
                    delete window.__playwright;
                    delete window.__pw_manual__;
                    delete window.__PW_inspect;
                    delete window.__selenium_unwrapped;
                    delete window.__webdriver_evaluate;
                    delete window.__driver_evaluate;
                    delete window.__webdriver_script_fn;
                    delete window.__fxdriver_evaluate;
                    delete window.__driver_unwrapped;
                    delete window._Selenium_IDE_Recorder;
                    delete window.callSelenium;
                    delete window._selenium;
                    delete window.__webdriver;
                    delete window.__selenium_evaluate;
                    delete window.domAutomationController;
                    delete window.domAutomation;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                    delete window.$cdc_asdjflasutopfhvcZLmcfl_;
                    // 动态清除所有 cdc_ 开头的属性
                    try {{
                        for (const k of Object.getOwnPropertyNames(window)) {{
                            if (k.indexOf('cdc_') === 0 || k.indexOf('$cdc_') === 0) {{
                                try {{ delete window[k]; }} catch(e) {{}}
                            }}
                        }}
                    }} catch(e) {{}}
                    
                    // 隐藏CDP特征（permissions.query 保护）
                    try {{
                        const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
                        window.navigator.permissions.query = function(parameters) {{
                            if (parameters && parameters.name === 'notifications') {{
                                const _perm = (typeof Notification !== 'undefined') ? Notification.permission : 'denied';
                                return Promise.resolve({{ state: _perm }});
                            }}
                            return originalQuery(parameters);
                        }};
                        Object.defineProperty(window.navigator.permissions.query, 'toString', {{
                            value: function() {{ return 'function query() {{ [native code] }}'; }},
                            configurable: true
                        }});
                    }} catch(e) {{}}
                    

                    // ========== 2. 补充真实浏览器属性 ==========
                    // 模拟plugins（不使用DOM创建，init_script时document.body为null）
                    if (!navigator.plugins.length) {{
                        const _makePlugin = (name, filename, desc) => {{
                            const p = Object.create(Plugin.prototype);
                            Object.defineProperties(p, {{
                                name: {{ value: name, enumerable: true }},
                                filename: {{ value: filename, enumerable: true }},
                                description: {{ value: desc || '', enumerable: true }},
                                length: {{ value: 1, enumerable: true }}
                            }});
                            return p;
                        }};
                        const _plugins = [
                            _makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
                            _makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', 'Portable Document Format'),
                            _makePlugin('Native Client', 'internal-nacl-plugin', 'Native Client Executable')
                        ];
                        Object.defineProperty(navigator, 'plugins', {{
                            get: function() {{ return _plugins; }},
                            configurable: true
                        }});
                    }}
                    
                    // 模拟mimeTypes
                    if (!navigator.mimeTypes.length) {{
                        const _mimes = [
                            {{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
                            {{ type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
                            {{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }}
                        ];
                        Object.defineProperty(navigator, 'mimeTypes', {{
                            get: function() {{ return _mimes; }},
                            configurable: true
                        }});
                    }}
                    
                    // ========== 3. Canvas和WebGL指纹（合规化：噪声扰动 + 真实GPU字符串 + toString保护） ==========
                    // Canvas：对真实渲染结果注入稳定的逐像素微噪声（基于会话种子），而非返回固定串
                    (function() {{
                        const _seed = {canvas_noise_seed} >>> 0;
                        let _s = _seed || 1;
                        const _rnd = function() {{ _s = (_s * 1103515245 + 12345) & 0x7fffffff; return _s / 0x7fffffff; }};
                        const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                        const _hookedGetImageData = function() {{
                            const data = _origGetImageData.apply(this, arguments);
                            try {{
                                const d = data.data;
                                for (let i = 0; i < d.length; i += 4) {{
                                    if (_rnd() < 0.02) {{
                                        const n = (_rnd() * 3 | 0) - 1;
                                        d[i]   = Math.max(0, Math.min(255, d[i]   + n));
                                        d[i+1] = Math.max(0, Math.min(255, d[i+1] + n));
                                        d[i+2] = Math.max(0, Math.min(255, d[i+2] + n));
                                    }}
                                }}
                            }} catch(e) {{}}
                            return data;
                        }};
                        // toString保护：让hook函数返回native code格式
                        Object.defineProperty(_hookedGetImageData, 'toString', {{
                            value: function() {{ return 'function getImageData() {{ [native code] }}'; }},
                            configurable: true
                        }});
                        CanvasRenderingContext2D.prototype.getImageData = _hookedGetImageData;
                        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
                        const _hookedToDataURL = function() {{
                            try {{
                                const ctx = this.getContext('2d');
                                if (ctx) {{ ctx.getImageData(0, 0, Math.max(1,this.width), Math.max(1,this.height)); }}
                            }} catch(e) {{}}
                            return _origToDataURL.apply(this, arguments);
                        }};
                        Object.defineProperty(_hookedToDataURL, 'toString', {{
                            value: function() {{ return 'function toDataURL() {{ [native code] }}'; }},
                            configurable: true
                        }});
                        HTMLCanvasElement.prototype.toDataURL = _hookedToDataURL;
                        const _origToBlob = HTMLCanvasElement.prototype.toBlob;
                        if (_origToBlob) {{
                            const _hookedToBlob = function() {{
                                try {{
                                    const ctx = this.getContext('2d');
                                    if (ctx) {{ ctx.getImageData(0, 0, Math.max(1,this.width), Math.max(1,this.height)); }}
                                }} catch(e) {{}}
                                return _origToBlob.apply(this, arguments);
                            }};
                            Object.defineProperty(_hookedToBlob, 'toString', {{
                                value: function() {{ return 'function toBlob() {{ [native code] }}'; }},
                                configurable: true
                            }});
                            HTMLCanvasElement.prototype.toBlob = _hookedToBlob;
                        }}
                        // measureText 噪声（防止通过字体宽度测量生成指纹）
                        const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
                        const _hookedMeasureText = function(text) {{
                            const metrics = _origMeasureText.call(this, text);
                            // 对宽度添加微小噪声（±0.1-0.5px，基于种子稳定）
                            try {{
                                const _noise = ((_rnd() - 0.5) * 0.8);
                                Object.defineProperty(metrics, 'width', {{
                                    value: metrics.width + _noise,
                                    writable: false
                                }});
                            }} catch(e) {{}}
                            return metrics;
                        }};
                        Object.defineProperty(_hookedMeasureText, 'toString', {{
                            value: function() {{ return 'function measureText() {{ [native code] }}'; }},
                            configurable: true
                        }});
                        CanvasRenderingContext2D.prototype.measureText = _hookedMeasureText;
                    }})();
                    
                    // WebGL：UNMASKED_VENDOR(37445)/UNMASKED_RENDERER(37446) 返回真实GPU字符串，覆盖 WebGL1+WebGL2
                    (function() {{
                        const _vendor = "{webgl_vendor}";
                        const _renderer = "{webgl_renderer}";
                        const _patch = function(proto) {{
                            if (!proto || !proto.getParameter) return;
                            const _orig = proto.getParameter;
                            const _hookedGetParam = function(param) {{
                                if (param === 37445) return _vendor;
                                if (param === 37446) return _renderer;
                                return _orig.call(this, param);
                            }};
                            // toString保护
                            Object.defineProperty(_hookedGetParam, 'toString', {{
                                value: function() {{ return 'function getParameter() {{ [native code] }}'; }},
                                configurable: true
                            }});
                            proto.getParameter = _hookedGetParam;
                        }};
                        try {{ _patch(WebGLRenderingContext.prototype); }} catch(e) {{}}
                        try {{ _patch(WebGL2RenderingContext.prototype); }} catch(e) {{}}
                    }})();
                    
                    // ========== 3.1 硬件信息注入（hardwareConcurrency / deviceMemory / connection） ==========
                    try {{
                        Object.defineProperty(navigator, 'hardwareConcurrency', {{
                            get: function() {{ return {hardware_concurrency}; }}, configurable: true
                        }});
                    }} catch(e) {{}}
                    try {{
                        Object.defineProperty(navigator, 'deviceMemory', {{
                            get: function() {{ return {device_memory}; }}, configurable: true
                        }});
                    }} catch(e) {{}}
                    // Network Information API（headless模式可能缺失或异常，补充真实值）
                    try {{
                        if (!navigator.connection) {{
                            Object.defineProperty(navigator, 'connection', {{
                                get: function() {{
                                    return {{
                                        effectiveType: '4g',
                                        rtt: 50,
                                        downlink: 10,
                                        saveData: false,
                                        onchange: null,
                                        addEventListener: function() {{}},
                                        removeEventListener: function() {{}}
                                    }};
                                }},
                                configurable: true
                            }});
                        }}
                    }} catch(e) {{}}
                    
                    // ========== 3.2 媒体设备伪造（替代粗暴删除，返回合理设备列表） ==========
                    (function() {{
                        try {{
                            const _fakeDevices = [
                                {{ deviceId: "default", kind: "audioinput", label: "", groupID: "grp1" }},
                                {{ deviceId: "default", kind: "audiooutput", label: "", groupID: "grp1" }},
                                {{ deviceId: "cam01", kind: "videoinput", label: "", groupID: "grp2" }}
                            ];
                            if (navigator.mediaDevices) {{
                                navigator.mediaDevices.enumerateDevices = function() {{
                                    return Promise.resolve(_fakeDevices.map(function(d) {{ return Object.assign({{toJSON:function(){{return d;}}}}, d); }}));
                                }};
                            }}
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== 3.3 字体指纹（限制可枚举字体集合，防止字体探测发现异常字体） ==========
                    (function() {{
                        try {{
                            const _allowFonts = new Set({fonts_json});
                            const _origCheck = (document.fonts && document.fonts.check) ? document.fonts.check.bind(document.fonts) : null;
                            if (_origCheck) {{
                                document.fonts.check = function(font, text) {{
                                    try {{
                                        const fam = (font || "").split(/\s+/).pop().replace(/['"]/g, "");
                                        if (fam && _allowFonts.size && !_allowFonts.has(fam)) return false;
                                    }} catch(e) {{}}
                                    return _origCheck(font, text);
                                }};
                            }}
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== 3.4 会话存储随机化（仅非持久化会话模式，避免覆盖已有真实数据） ==========
                    (function() {{
                        if (!{storage_randomize_js}) return;
                        try {{
                            // 仅在键不存在时写入随机种子，模拟分析类脚本(_ga/_gid)留下的痕迹，但不破坏真实会话
                            if (!localStorage.getItem('_app_cid')) {{
                                localStorage.setItem('_app_cid', '{ls_seed_client_id}');
                            }}
                            if (!sessionStorage.getItem('_app_sid')) {{
                                sessionStorage.setItem('_app_sid', '{ls_seed_session_id}');
                            }}
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== 4. 语言设置 ==========
                    // 强制覆盖语言
                    Object.defineProperty(navigator, 'language', {{
                        value: "{browser_locale}",
                        writable: false,
                        configurable: false
                    }});
                    
                    // 语言列表：根据实际locale生成合理的回退链（不同语言环境不应总是en-US回退）
                    (function() {{
                        const _loc = "{browser_locale}";
                        const _langPrefix = _loc.split('-')[0];
                        let _langs = [_loc];
                        // 根据语言前缀生成合理的回退链
                        const _fallbacks = {{
                            'zh': ['zh-CN', 'zh', 'en-US', 'en'],
                            'ja': ['ja-JP', 'ja', 'en-US', 'en'],
                            'ko': ['ko-KR', 'ko', 'en-US', 'en'],
                            'de': ['de-DE', 'de', 'en-US', 'en'],
                            'fr': ['fr-FR', 'fr', 'en-US', 'en'],
                            'es': ['es-ES', 'es', 'en-US', 'en'],
                            'pt': ['pt-BR', 'pt', 'en-US', 'en'],
                            'ru': ['ru-RU', 'ru', 'en-US', 'en'],
                            'en': ['en-US', 'en']
                        }};
                        _langs = _fallbacks[_langPrefix] || [_loc, _langPrefix, 'en-US', 'en'];
                        // 确保当前locale在第一位
                        if (_langs[0] !== _loc) {{ _langs = [_loc].concat(_langs.filter(function(l){{ return l !== _loc; }})); }}
                        Object.defineProperty(navigator, 'languages', {{
                            get: function() {{ return Object.freeze(_langs.slice()); }},
                            configurable: true
                        }});
                    }})();
                    
                    // ========== 4.1 时区覆盖（与 context timezone_id 双保险）==========
                    // - 用自定义 Intl.DateTimeFormat.resolvedOptions() 返回目标时区
                    // - 覆盖 Date.prototype.getTimezoneOffset()
                    (function () {{
                        const _TZ = "{browser_timezone}";
                        // 预估该时区相对 UTC 的偏移（分钟，正数表示在 UTC 西，负数表示东）
                        // 注意：由于 JS 在 context timezone_id 下实际时间会被改写，这里只做兜底
                        // 先在 context 层已应用的时区基础上，再覆盖 JS 访问点
                        let _offsetMin = 0;
                        try {{
                            const df = new Intl.DateTimeFormat("en-US", {{
                                timeZone: _TZ,
                                timeZoneName: "shortOffset"
                            }});
                            const parts = df.formatToParts(new Date());
                            for (const part of parts) {{
                                if (part.type === "timeZoneName") {{
                                    // 例如 "GMT+5" / "GMT-8" / "UTC"
                                    const m = part.value.match(/GMT([+-]?)([\d]+)?(?::([\d]+))?/);
                                    if (m) {{
                                        const sign = m[1] === "-" ? -1 : 1;
                                        const h = parseInt(m[2] || "0", 10);
                                        const mi = parseInt(m[3] || "0", 10);
                                        // getTimezoneOffset 约定：UTC+8 返回 -480，UTC-5 返回 +300
                                        _offsetMin = -(sign * (h * 60 + mi));
                                    }}
                                }}
                            }}
                        }} catch (_) {{}}

                        // 覆盖 Intl.DateTimeFormat.prototype.resolvedOptions().timeZone
                        const _origResolved = Intl.DateTimeFormat.prototype.resolvedOptions;
                        Intl.DateTimeFormat.prototype.resolvedOptions = function () {{
                            const r = _origResolved.call(this);
                            try {{ Object.defineProperty(r, "timeZone", {{ value: _TZ, writable: true, configurable: true }}); }} catch (_) {{}}
                            return r;
                        }};

                        // 覆盖 Date.prototype.getTimezoneOffset
                        Date.prototype.getTimezoneOffset = function () {{ return _offsetMin; }};
                    }})();
                    
                    // ========== 5. 其他指纹伪装 ==========
                    // chrome.runtime 保留对象但限制敏感API（完全删除会被检测）
                    if (window.chrome) {{
                        if (!window.chrome.runtime) {{
                            window.chrome.runtime = {{}};
                        }}
                        // 删除可能暴露自动化的属性，保留基本结构
                        delete window.chrome.runtime.connectNative;
                        delete window.chrome.runtime.sendNativeMessage;
                    }} else {{
                        // 非 Chrome 环境补充 chrome 对象
                        window.chrome = {{ runtime: {{}}, loadTimes: function(){{}}, csi: function(){{}} }};
                    }}
                    
                    // 模拟真实的窗口属性（outerWidth/Height 必须大于 innerWidth/Height，因为浏览器UI框架占用空间）
                    (function() {{
                        const _vw = {width};
                        const _vh = {height};
                        // 真实浏览器：outerWidth = innerWidth + 左右滚动条/边框(0-16px)
                        // outerHeight = innerHeight + 工具栏+标签栏+地址栏(85-120px)
                        const _chromeBarH = 85 + Math.floor(Math.random() * 30);  // 85-115px
                        const _scrollbarW = Math.random() > 0.5 ? 15 : 0;  // 部分系统有滚动条
                        Object.defineProperty(window, 'outerWidth', {{
                            get: function() {{ return _vw + _scrollbarW; }},
                            configurable: true
                        }});
                        Object.defineProperty(window, 'outerHeight', {{
                            get: function() {{ return _vh + _chromeBarH; }},
                            configurable: true
                        }});
                        // screen 属性：屏幕分辨率 >= 视口，availHeight < height（任务栏/Dock占用）
                        const _taskbarH = Math.random() > 0.5 ? 40 : 48;  // Windows任务栏40px / macOS Dock 48px
                        Object.defineProperty(screen, 'width', {{
                            get: function() {{ return _vw; }},
                            configurable: true
                        }});
                        Object.defineProperty(screen, 'height', {{
                            get: function() {{ return _vh; }},
                            configurable: true
                        }});
                        Object.defineProperty(screen, 'availWidth', {{
                            get: function() {{ return _vw; }},
                            configurable: true
                        }});
                        Object.defineProperty(screen, 'availHeight', {{
                            get: function() {{ return _vh - _taskbarH; }},
                            configurable: true
                        }});
                        // colorDepth / pixelDepth
                        Object.defineProperty(screen, 'colorDepth', {{
                            get: function() {{ return {color_depth}; }},
                            configurable: true
                        }});
                        Object.defineProperty(screen, 'pixelDepth', {{
                            get: function() {{ return {color_depth}; }},
                            configurable: true
                        }});
                    }})();
                """)
                
                # ========== 安装隐私保护扩展 ==========
                # 注意：Selenium下扩展需在ChromeOptions中设置，此处仅记录（核心反检测已通过CDP和init_script实现）
                import os as _os_ext
                extensions_dir = _os_ext.path.join(_os_ext.path.dirname(__file__), 'extensions')
                if _os_ext.path.exists(extensions_dir):
                    for ext_file in _os_ext.listdir(extensions_dir):
                        if ext_file.endswith('.crx'):
                            log.info(f"[Selenium] 检测到隐私扩展 {ext_file}（启动时加载，核心反检测已通过CDP实现）")
                
                # 添加请求拦截器，优化视频请求的User-Agent和Referer设置
                def handle_request(route, request):
                    url = request.url
                    
                    # 对视频请求进行特殊处理（支持udis视频混合中转方案）
                    if is_udis_video_url(url):
                        # 对udis视频请求使用特殊的User-Agent（浏览器内置播放器UA）
                        custom_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
                            "Referer": "https://udisxxx.com/"  # 使用udis的真实域名作为Referer
                        }
                        
                        # 使用自定义头部继续请求
                        route.continue_(headers={**request.headers, **custom_headers})
                    else:
                        # 对其他请求使用默认行为
                        route.continue_()
                
                context.route("**", handle_request)
                
                # 先在空页面上检查一致性
                page = context.new_page()
                try:
                    def _block_background_noise(route):
                        req_url = route.request.url.lower()
                        noisy_hosts = (
                            "googleapis.com", "accounts.google.com", "clients2.google.com",
                            "safebrowsing", "gvt1.com", "gstatic.com/generate_204",
                            "httpbin.org", "api.ipify.org", "icanhazip.com", "ifconfig.me",
                            "checkip.amazonaws.com", "ident.me"
                        )
                        if any(host in req_url for host in noisy_hosts):
                            return route.abort()
                        return route.continue_()
                    context.route("**/*", _block_background_noise)
                    log.debug("已启用阶段2请求过滤：屏蔽Chrome后台与外部IP检测域名")
                except Exception as e:
                    log.warning(f"阶段2请求过滤启用失败（忽略）: {e}")

                if not fingerprint or not fingerprint.get('language') or not fingerprint.get('timezone'):
                    log.error("❌ 指纹信息不完整，无法继续任务")
                    stats["fail"] += 1
                    continue

                browser_success = True

                # 阶段2：不再让浏览器访问 httpbin/ipify/icanhazip 等外部探测端点。
                # 代理可用性由 6666 控制面与 VPS 目标站测速保证，避免探测 URL 抢占 SOCKS5 链路并污染当前页面。
                log.warning("🩺 代理链路浏览器探测已跳过（阶段2）：直接访问目标站")
                
                # 立即检查语言一致性
                actual_language = page_eval(
                    page, "() => navigator.language || 'en-US'", default="en-US"
                )
                if not isinstance(actual_language, str) or not actual_language:
                    actual_language = "en-US"

                log.info(f"🧪 一致性检查 - 语言: 预期={fingerprint['language']}, 实际={actual_language}, 状态={'✅' if _bcp47_prefix_equal(actual_language, fingerprint['language']) else '❌'}")

                # 然后再检查IP泄漏
                # ❌ 已按需求删除"浏览器出口IP泄漏一致性检测"(check_ip_leak_robust)。
                # 原因：ADSL 模式下浏览器本机直连，出口IP 恒等于拨号IP；
                #       代理模式下出口IP 由 SOCKS5 链路保证；浏览器再访问 ipify 比对纯属冗余且常超时。
                # 这里只保留本地 WebRTC 是否禁用的展示，不再用浏览器IP参与一致性判定。
                _webrtc_local = page_eval(page, """() => {
                    try {
                        if (typeof window.RTCPeerConnection === 'undefined' &&
                            typeof window.webkitRTCPeerConnection === 'undefined') {
                            return 'disabled';
                        }
                        return 'enabled';
                    } catch(e) { return 'disabled'; }
                }""", default="disabled")
                webrtc = _webrtc_local if isinstance(_webrtc_local, str) else "disabled"
                leak_status, real_ip = "skip", exit_ip
                # IP 一致性不再由浏览器检测决定，恒为通过（出口由链路层保证）
                leak_ok = True

                # 最终一致性检查（严格精准确认）
                consistency_details = ""
                if config["skip_timezone_check"]:
                    consistency = leak_ok and _bcp47_prefix_equal(actual_language, fingerprint["language"])
                    if not consistency:
                        consistency_details = []
                        if not leak_ok:
                            consistency_details.append(f"IP泄漏: 预期={exit_ip}, 实际={real_ip}")
                        if not _bcp47_prefix_equal(actual_language, fingerprint["language"]):
                            consistency_details.append(f"语言不匹配: 预期={fingerprint['language']}, 实际={actual_language}")
                        consistency_details = "; ".join(consistency_details)
                else:
                    actual_timezone = page_eval(
                        page,
                        "() => Intl.DateTimeFormat().resolvedOptions().timeZone",
                        default=fingerprint.get("timezone", "America/New_York"),
                    )
                    if not isinstance(actual_timezone, str) or not actual_timezone:
                        actual_timezone = fingerprint.get("timezone", "America/New_York")
                    log.info(
                        f"🧪 一致性检查 - 时区: 预期={fingerprint['timezone']}, "
                        f"实际={actual_timezone}, "
                        f"状态={'✅' if actual_timezone == fingerprint['timezone'] else '❌'}"
                    )

                    consistency = (
                        leak_ok and
                        actual_timezone == fingerprint["timezone"] and
                        _bcp47_prefix_equal(actual_language, fingerprint["language"])
                    )

                    if not consistency:
                        # 失败时输出完整对比（包含 UA、分辨率等，方便诊断）
                        log.warning(
                            f"⚠️ 指纹与IP不一致，任务将被终止。完整对比：\n"
                            f"    language  : 期望={fingerprint['language']:10} | 实际={actual_language}\n"
                            f"    timezone  : 期望={fingerprint['timezone']:25} | 实际={actual_timezone}\n"
                            f"    ip_leak   : 期望={exit_ip} | 实际={real_ip} (leak_status={leak_status})\n"
                            f"    ua        : {user_agent}\n"
                            f"    resolution: {resolution}"
                        )
                        consistency_details = []
                        if not leak_ok:
                            consistency_details.append(f"IP泄漏: 预期={exit_ip}, 实际={real_ip}")
                        if actual_timezone != fingerprint["timezone"]:
                            consistency_details.append(f"时区不匹配: 预期={fingerprint['timezone']}, 实际={actual_timezone}")
                        if not _bcp47_prefix_equal(actual_language, fingerprint["language"]):
                            consistency_details.append(f"语言不匹配: 预期={fingerprint['language']}, 实际={actual_language}")
                        consistency_details = "; ".join(consistency_details)
                
                log.fingerprint_module(browser_success, fingerprint_success, fingerprint_id, user_agent, resolution, fingerprint["language"], fingerprint["timezone"], webrtc, canvas, webgl, consistency, consistency_details)
                
                # 记录指纹和UA使用情况
                if browser_success and consistency:
                    # 获取国家代码
                    country_code = ""
                    # 尝试使用 cc_upper 变量（用 locals().get 替代 exec）
                    try:
                        _cc = locals().get("cc_upper", "")
                        if _cc:
                            country_code = _cc
                        else:
                            # 从 resolved_ip_info 中获取国家代码
                            _rii = locals().get("resolved_ip_info")
                            if _rii is not None:
                                country_code = (_rii.get("country_code") or "").upper()
                            else:
                                log.warning("⚠️ 无法获取国家代码")
                    except Exception as e:
                        log.warning(f"⚠️ 无法获取 cc_upper 变量: {e}")
                    record_fingerprint_usage(fingerprint_id, user_agent, country_code)
                
                if not browser_success or not consistency:
                    stats["fail"] += 1
                    task_time = time.time() - task_start_time
                    log.task_result(task_time, False, False, f"浏览器启动失败或指纹不一致: {consistency_details}")
                    continue
                
                # ==================== 页面 & 广告模块 ====================
                # 获取目标网站池（串联浏览）
                _target_urls_cfg = config.get("target_urls")
                if isinstance(_target_urls_cfg, list) and _target_urls_cfg:
                    _active_urls = [item.get("url", "").strip() for item in _target_urls_cfg if item.get("enabled") and item.get("url", "").strip()]
                else:
                    _legacy = config.get("target_url", "")
                    _active_urls = [_legacy] if _legacy else []
                if not _active_urls:
                    stats["fail"] += 1
                    task_time = time.time() - task_start_time
                    log.task_result(task_time, False, False, "没有勾选的目标网站")
                    continue
                for _url_idx, target_url in enumerate(_active_urls):
                    log.info(f"🔗 串联浏览: 第{_url_idx+1}/{len(_active_urls)}个网站 - {target_url}")
                    final_url = target_url  # 初始化final_url为首页
                    load_success = False
                    load_time = 0
                    ad_found = False
                    ad_in_viewport = False
                    ad_loaded = False
                    ad_impressions = 0
                    ad_refreshes = 0
                    ad_monitor = create_ad_monitor()
                    page_behavior_stats = {
                        "mouse_moves": 0,
                        "scrolls": 0,
                        "scroll_distance": 0,
                        "clicks": 0,
                        "waits": 0,
                        "focus_switches": 0,
                        "refreshes": 0,
                        "ad_stay": 0,
                        "total_stay": 0,
                        "key_presses": 0
                    }
                    
                    # 初始化视频相关变量
                    video_watched = False
                    video_watch_time = 0
                    video_clicked = False
                    video_behavior_stats = None
                    
                    try:
                        # 根据配置实现流量分配：30%首页，30%列表页，40%章节页
                        traffic_route = random.random()
                        log.info(f"流量分配路由: {traffic_route:.2f}")
                    
                        page_start_time = time.time()
                    
                        # ========== 随机选择流程模式 ==========
                        # 只使用网页浏览模式（100%权重）
                        mode = "mode3"
                        log.info("📋 流程模式：网页浏览模式")

                        # ========== [新增] 真搜索跳转流程 ==========
                        search_mode = config.get("seo", {}).get("search_mode", "direct_referer")
                        already_on_target = False
                        current_x, current_y = 0, 0  # 初始化鼠标坐标（避免 UnboundLocalError）
                        if search_mode == "real_search":
                            # 执行完整搜索跳转流程（带真人模拟，支持所有搜索引擎）
                            search_success, current_x, current_y = perform_real_search(page, target_url, selected_engine_id, selected_keyword, page_behavior_stats, current_x, current_y, config)
                            if search_success:
                                log.info(f"🔍 [真搜索] 已成功跳转至目标页，跳过直接导航")
                                already_on_target = True
                                # [调整] 把 enter_site_time 设置为现在（真搜索跳转完成即进入网站）
                                enter_site_time = time.time()
                            else:
                                log.warning(f"🔍 [真搜索] 未成功跳转，继续直接导航目标页")
                        # =========================================

                        # 获取网页浏览模式配置（各层+循环+间隔）
                        web_config = config.get("web_navigation", {})
                        loop_cfg = web_config.get("loop_count", {"min": 1, "max": 3})
                        interval_cfg = web_config.get("loop_interval", {"min": 1, "max": 5})
                        back_links = web_config.get("back_links", []) or []
                        back_home_links = web_config.get("back_home_links", []) or []

                        # 随机：循环次数（来自 loop_count 配置）
                        chapter_loop_count = random.randint(
                            int(loop_cfg.get("min", 1)),
                            int(loop_cfg.get("max", 1))
                        )
                        chapter_loop_count = max(1, chapter_loop_count)

                        # 随机：每轮间隔（秒，不计入每轮浏览时长，仅轮与轮之间的停顿）
                        loop_interval = random.uniform(
                            float(interval_cfg.get("min", 1)),
                            float(interval_cfg.get("max", 1))
                        )
                        loop_interval = max(0.0, loop_interval)
                        if chapter_loop_count <= 1:
                            loop_interval = 0.0

                        # 读取 6 层配置（停留比例 + 最小停留 + 关键字 + 兜底 URL）
                        layers = []
                        total_ratio = 0.0
                        for li in range(1, 7):
                            layer_cfg = web_config.get(f"layer_{li}", {})
                            ratio = float(layer_cfg.get("stay_ratio", 0.0) or 0.0)
                            min_stay = float(layer_cfg.get("min_stay", 10) or 10)
                            keywords = layer_cfg.get("keywords", []) or []
                            fallback_urls = layer_cfg.get("fallback_urls", []) or []
                            layers.append({
                                "idx": li,
                                "ratio": max(0.0, ratio),
                                "min_stay": max(0.0, min_stay),
                                "keywords": keywords,
                                "fallback_urls": fallback_urls,
                            })
                            total_ratio += max(0.0, ratio)

                        # 比例为 0 时使用默认（首页 20%，其余层平均）
                        if total_ratio <= 0.0:
                            log.warning("⚠️ 各层 stay_ratio 之和为 0，使用默认比例（首页 20%，其余平均）")
                            layers[0]["ratio"] = 0.2
                            others = [l for l in layers[1:] if (l["keywords"] or l["fallback_urls"])]
                            if others:
                                share = 0.8 / len(others)
                                for l in others:
                                    l["ratio"] = share
                        total_ratio = sum(l["ratio"] for l in layers) or 1.0

                        # ★ 修正逻辑：配置时长优先，保险绳保底
                        #   每轮独立随机，但总时长不超过7-9分钟保险绳
                        if enter_site_time is None:
                            enter_site_time = time.time()
                        task_deadline = enter_site_time + random.uniform(420, 540)  # 7-9分钟（420-540秒）随机保险绳
                        
                        round_total_stays = []
                        remaining_time = task_deadline - time.time()  # 剩余可运行时间
                        
                        for _r in range(chapter_loop_count):
                            max_round_time = config["total_stay"]["max"]
                            # 计算该轮最大可分配时间（不超过配置和剩余时间）
                            available_time = min(max_round_time, remaining_time / (chapter_loop_count - _r))
                            round_time = random.uniform(config["total_stay"]["min"], available_time)
                            round_total_stays.append(round_time)
                            remaining_time -= round_time
                            
                        total_task_stay = sum(round_total_stays)

                        # 每轮每层停留时长矩阵：round_layer_stays[轮][层] = 该轮时长 × (层比率/总比率)
                        # ★ 纯比率瓜分：严格保证 Σ(各层) = 该轮时长，不再用 min_stay 抬高（避免总时长超标）
                        round_layer_stays = []
                        for _ridx, _rt in enumerate(round_total_stays):
                            _per_layer = [_rt * (_l["ratio"] / total_ratio) for _l in layers]
                            round_layer_stays.append(_per_layer)

                        log.info(
                            f"🎯 浏览循环次数: {chapter_loop_count}次，每轮间隔: {loop_interval:.1f}秒，"
                            f"各轮随机时长: {', '.join(f'{s:.1f}s' for s in round_total_stays)}，"
                            f"任务总浏览时长≈{total_task_stay:.1f}秒（=各轮之和，不含前置/拨号/间隔）"
                        )
                        for _ridx in range(chapter_loop_count):
                            log.info(
                                f"📊 第{_ridx+1}轮每层停留(L1-L5): "
                                + ", ".join(f"L{i+1}≈{round_layer_stays[_ridx][i]:.1f}s" for i in range(min(5, len(round_layer_stays[_ridx]))))
                            )
    
                        # ========== 第 1 步：访问首页（layer_1） ==========
                        log.info("第 1 步：访问首页（layer_1）")
                        # 统一设置更宽松的导航超时，避免网络抖动时过早失败
                        try:
                            page.set_default_navigation_timeout(120000)
                            page.set_default_timeout(60000)
                        except Exception:
                            pass
    
                        # ---------- 辅助：检测"页面是否真的有内容" ----------
                        # 使用 page_has_meaningful_content（底层 page_eval 已处理 JSHandle）
                        _detect = page_has_meaningful_content
    
                        home_load_success = False
                        _home_page_reason = "未执行"

                        # ========== 处理 already_on_target ==========
                        if already_on_target:
                            log.info("🔍 [真搜索] 已在目标页，跳过直接导航，直接检测当前页面")
                            _ok, _bl, _u = _detect(page)
                            if _ok:
                                home_load_success = True
                                _home_page_reason = "真搜索跳转成功，当前页面有内容"
                            else:
                                home_load_success = True
                                _home_page_reason = "真搜索跳转成功"
                            if "enter_site_time" not in locals() or enter_site_time is None:
                                enter_site_time = time.time()
                        else:
                            # ========== 严禁直跳：先访问Referer来源页（搜索引擎/社媒），再跳转目标网站 ==========
                            if generated_referer:
                                try:
                                    log.info(f"🔗 [严禁直跳] 先访问Referer来源页: {generated_referer[:80]}")
                                    page.goto(generated_referer, timeout=30000, wait_until="domcontentloaded")
                                    time.sleep(random.uniform(1.5, 3.5))
                                    log.info(f"✅ [严禁直跳] Referer来源页已访问，准备跳转目标网站")
                                except Exception as e:
                                    log.warning(f"⚠️ [严禁直跳] Referer来源页访问失败({str(e)[:60]})，继续跳转目标")
                            
                            _retry_wait_list = [6, 10]  # 第1、2次失败后的等待（秒）
                            for retry in range(3):
                                try:
                                    # wait_until="commit" 最快返回（响应头到达即算成功），后续自行等待内容
                                    # 优化网络请求：添加重试机制和资源拦截
                                    def optimized_page_goto(page, url, max_retries=2, referer=None):
                                        # 拦截不必要的资源（图片、视频等，提升加载速度）
                                        try:
                                            page.route("**/*.png", lambda route: route.abort())
                                            page.route("**/*.jpg", lambda route: route.abort())
                                            page.route("**/*.jpeg", lambda route: route.abort())
                                            page.route("**/*.mp4", lambda route: route.abort())
                                            page.route("**/*.gif", lambda route: route.abort())
                                            page.route("**/*.webp", lambda route: route.abort())
                                            page.route("**/*.svg", lambda route: route.abort())
                                            # 拦截已知广告域名（不用过于宽泛的模式，避免误伤正常JS）
                                            page.route("**/*googleadservices*.com*", lambda route: route.abort())
                                            page.route("**/*googlesyndication*.com*", lambda route: route.abort())
                                            page.route("**/*doubleclick*.net*", lambda route: route.abort())
                                        except Exception:
                                            pass
                                        for attempt in range(max_retries):
                                            try:
                                                # 优化页面加载（带Referer严禁直跳）
                                                page.goto(url, timeout=25000, wait_until="domcontentloaded", referer=referer)
                                                return True
                                            except Exception as e:
                                                log.warning(f"第{attempt+1}次访问失败: {e}")
                                                if attempt < max_retries - 1:
                                                    time.sleep(2)
                                        return False
                                        
                                    # 执行优化后的页面访问（带Referer严禁直跳）
                                    if not optimized_page_goto(page, target_url, referer=generated_referer):
                                        log.error(f"页面访问多次失败，任务终止")
                                        return False
                                        
                                    # 给页面 JavaScript 渲染 1.5-3 秒时间
                                    time.sleep(random.uniform(1.5, 3.0))
                                    _ok, _bl, _u = _detect(page)
                                    if _ok:
                                        home_load_success = True
                                        _home_page_reason = f"goto成功，body≈{_bl}字符"
                                        log.info(f"✅ 首页访问成功，页面已响应（URL={str(_u)[:80]}，body≈{_bl}字符）")
                                        break
                                    # 内容为空，但 URL 是正常的——也算成功（某些 SPA 首屏渲染延迟）
                                    _u_str = str(_u or "")
                                    if _u_str and _u_str.lower().startswith(("http://", "https://")):
                                        time.sleep(random.uniform(2.5, 4.0))
                                        _ok2, _bl2, _u2 = _detect(page)
                                        if _ok2 or (isinstance(_bl2, int) and _bl2 >= 10):
                                            home_load_success = True
                                            _home_page_reason = f"延迟加载成功，body≈{_bl2}字符"
                                            log.info(f"✅ 首页访问成功（二次检测）：URL={str(_u2)[:80]}，body≈{_bl2}字符")
                                            break
                                    home_load_success = True
                                    _home_page_reason = f"goto完成，但内容少（{_bl}字符）"
                                    log.info(f"✅ 首页 goto 完成，视为成功（body≈{_bl}字符）")
                                    break
                                except Exception as e:
                                    _err_short = str(e)[:120]
                                    log.warning(f"⚠️ 访问首页失败（第{retry+1}/3次）: {_err_short}")
                                    # --- 关键修复：即使 goto 抛异常，也检查页面是否已经实际加载 ---
                                    try:
                                        _ok, _bl, _u = _detect(page)
                                    except Exception:
                                        _ok, _bl, _u = False, 0, ""
                                    if _ok:
                                        log.info(
                                            f"🔎 发现页面在失败前已加载（URL={str(_u)[:80]}, body≈{_bl}字符），"
                                            f"视为访问成功，继续..."
                                        )
                                        home_load_success = True
                                        _home_page_reason = f"异常后被动检测：body≈{_bl}字符"
                                        time.sleep(random.uniform(2.0, 3.5))
                                        break
                                    if retry < 2:
                                        _wait = _retry_wait_list[retry]
                                        log.info(f"⏳ 等待 {_wait}s 后重试（给代理/目标服务器恢复时间）")
                                        time.sleep(_wait)

                            if not home_load_success:
                                # --- 终极兜底：再发一次 goto（不过度等待），然后给 6-10 秒被动等待 ---
                                log.warning("⚠️ 3 次显式 goto 失败，尝试终极兜底：被动等待页面自行加载...")
                                try:
                                    page.goto(target_url, timeout=15000, wait_until="commit")
                                except Exception:
                                    pass
                                time.sleep(random.uniform(6, 10))
                                try:
                                    _ok, _bl, _u = _detect(page)
                                except Exception:
                                    _ok, _bl, _u = False, 0, ""
                                if _ok:
                                    home_load_success = True
                                    _home_page_reason = f"终极兜底成功，body≈{_bl}字符"
                                    log.info(f"✅ 终极兜底成功：页面最终加载（URL={str(_u)[:80]}，body≈{_bl}字符）")
                                else:
                                    _home_page_reason = f"终极兜底失败，body≈{_bl}字符"
                                    log.warning(f"⚠️ 首页访问始终失败（{_home_page_reason}），进入降级浏览模式（仍会执行停留/行为模拟/广告检测）")

                        # --- 降级模式标记：失败不再抛出异常，改为继续走后续流程 ---
                        log.info(f"🏁 首页访问状态: home_load_success={home_load_success}, reason={_home_page_reason}")
                        # ⏱️ 进入网站时间锚点（首页加载完成 → 浏览正式开始）
                        if "enter_site_time" not in locals() or enter_site_time is None:
                            enter_site_time = time.time()
                        # ========== 本地IP泄露检测（已简化） ==========
                        # 注释：浏览器出口IP由SOCKS5链路层保证，不再通过访问外部IP检测服务验证
                        # 外部服务(ipify/icanhazip/ifconfig.me)经常超时导致任务卡死
                        log.info(f"🛡️ IP泄漏检测：跳过外部探测（由链路层保证出口IP={exit_ip}）")
                        
                        ad_monitor = scan_ads_during_task(page, ad_monitor, "首页加载后")

                        # ========== 任务总时长保险绳（防止任务无限延长） ==========
                        task_deadline = enter_site_time + random.uniform(420, 540)  # 7-9分钟（420-540秒）随机时长
                        
                        def _check_rope(stage_desc=""):
                            if not task_running:
                                raise RuntimeError("任务已停止")
                            if time.time() >= task_deadline:
                                raise RuntimeError(f"任务超时（已运行 {time.time() - enter_site_time:.1f}秒）")
    
                        current_x, current_y = 100, 100

                        # ========== 网页浏览模式循环：每轮 首页(L1)→列表页(L2)→L3→L4→L5→L6→返回首页 ==========
                        # 每轮使用该轮独立随机时长（round_layer_stays[loop_idx]），各层按 stay_ratio 比率停留。
                        log.info(f"🔄 网页浏览模式循环次数: {chapter_loop_count}次（每轮走完 L1→L6，任务总时长=各轮之和）")

                        if True:
                            for loop_idx in range(chapter_loop_count):
                                try:
                                    _check_rope(f"循环第 {loop_idx+1} 次前")
                                except RuntimeError:
                                    break

                                # ==== 🔴 循环头：红色标记 ====
                                log.info(
                                    f"===== <span style='color:red'>网页浏览模式循环 "
                                    f"第 {loop_idx+1}/{chapter_loop_count} 次</span> ====="
                                )
                                _layer_stays = round_layer_stays[loop_idx]

                                # 每轮起点确保在首页：第1轮已加载首页；后续轮由上一轮"返回首页"落到首页
                                if loop_idx > 0:
                                    try:
                                        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                                    except Exception as e:
                                        log.warning(f"⚠️ 第{loop_idx+1}轮回到首页 goto 失败: {str(e)[:80]}")

                                # —— L1 首页停留 ——
                                home_stay = _layer_stays[0]
                                log.info(f"[第{loop_idx+1}轮] 首页(L1)停留窗口: {home_stay:.1f}秒")
                                current_x, current_y = simulate_human_in_window(
                                    page, home_stay, page_behavior_stats, current_x or 100, current_y or 100,
                                    config, page_name=f"[T{task_idx+1}] 首页", deadline=task_deadline
                                )
                                ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮首页停留后")

                                # —— L1 → L2 进入列表页 ——
                                layer1_cfg = layers[0]
                                success_list, current_x, current_y = click_link_with_fallback(
                                    page, layer1_cfg["keywords"], layer1_cfg["fallback_urls"],
                                    current_x, current_y, config
                                )
                                if success_list:
                                    page_behavior_stats["clicks"] += 1
                                    page_behavior_stats["mouse_moves"] += 1
                                    list_stay = _layer_stays[1]
                                    log.info(f"[第{loop_idx+1}轮] 列表页(L2)停留窗口: {list_stay:.1f}秒")
                                    current_x, current_y = simulate_human_in_window(
                                        page, list_stay, page_behavior_stats,
                                        current_x or 300, current_y or 300,
                                        config, page_name="列表页", deadline=task_deadline
                                    )
                                    ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮列表页停留后")
                                else:
                                    log.warning(f"⚠️ 第{loop_idx+1}轮进入列表页失败，本轮跳过深层")

                                # 尝试更深层浏览：layer_3 → layer_4 → ... → layer_6
                                _broken = False
                                for level_idx in range(2, 6):
                                    if not success_list:
                                        break
                                    try:
                                        _check_rope(f"layer_{level_idx+1} 前")
                                    except RuntimeError:
                                        _broken = True
                                        break

                                    target_layer = layers[level_idx]
                                    has_link = bool(target_layer["keywords"]) or bool(target_layer["fallback_urls"])
                                    if not has_link:
                                        log.info(f"layer_{level_idx+1} 未配置关键字/兜底 URL，停止深入")
                                        break

                                    log.info(f"→ 进入 layer_{level_idx+1}")
                                    success_click, current_x, current_y = click_link_with_fallback(
                                        page, target_layer["keywords"], target_layer["fallback_urls"],
                                        current_x, current_y, config
                                    )
                                    if success_click:
                                        page_behavior_stats["clicks"] += 1
                                        page_behavior_stats["mouse_moves"] += 1
                                    if not success_click:
                                        log.warning(f"进入 layer_{level_idx+1} 失败，停止深入")
                                        break

                                    stay = _layer_stays[level_idx]
                                    log.info(f"[第{loop_idx+1}轮] layer_{level_idx+1} 停留: {stay:.1f}秒")
                                    current_x, current_y = simulate_human_in_window(
                                        page, stay, page_behavior_stats,
                                        current_x or 300, current_y or 300,
                                        config, page_name=f"layer_{level_idx+1}", deadline=task_deadline
                                    )
                                    ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮layer_{level_idx+1}停留后")

                                if _broken:
                                    break

                                # 返回首页：优先点击返回链接/首页关键字；失败则 goto 首页
                                try:
                                    _check_rope("返回首页前")
                                except RuntimeError:
                                    break
                                log.info("→ 返回首页（优先点击返回链接/首页链接，失败则 goto）")
                                back_keywords = list(back_links) + list(back_home_links)
                                success_back, current_x, current_y = click_link_with_fallback(
                                    page, back_keywords, [], current_x, current_y, config,
                                    final_fallback_url=target_url,
                                )
                                if success_back:
                                    page_behavior_stats["clicks"] += 1
                                    page_behavior_stats["mouse_moves"] += 1
                                if not success_back:
                                    try:
                                        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                                    except Exception as e:
                                        log.warning(f"⚠️ 返回首页 goto 失败: {str(e)[:80]}")
                                ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮返回首页后")
    
                                # 每轮间隔（真人行为持续，不停止）
                                if loop_idx < chapter_loop_count - 1 and loop_interval > 0:
                                    try:
                                        _check_rope("每轮间隔前")
                                    except RuntimeError:
                                        break
                                    log.info(f"⏸ 每轮浏览间隔: {loop_interval:.1f}秒（真人行为持续）")
                                    current_x, current_y = simulate_human_in_window(
                                        page, loop_interval, page_behavior_stats,
                                        current_x or 100, current_y or 100,
                                        config, page_name=f"[轮间间隔{loop_idx+1}]", deadline=task_deadline
                                    )
    
                            # ========== 网页浏览模式结束 → 全程真人行为统计 ==========
                            log.info(
                                "✅ 网页浏览模式完成！全程真人行为统计："
                                f"鼠标移动 {page_behavior_stats['mouse_moves']} 次，"
                                f"点击 {page_behavior_stats['clicks']} 次，"
                                f"滚动 {page_behavior_stats['scrolls']} 次({page_behavior_stats['scroll_distance']}px)，"
                                f"键盘 {page_behavior_stats['key_presses']} 次，"
                                f"随机等待 {page_behavior_stats['waits']} 次，"
                                f"总停留 {page_behavior_stats['total_stay']:.1f}s"
                            )
    
                        final_url = target_url
                        
                        # 广告检测
                        load_time = time.time() - page_start_time
                        load_success = bool(home_load_success)
                        ad_found = False
                        ad_in_viewport = False
                        ad_loaded = False
                        ad_impressions = 0
    
                        # 降级模式：如果首页访问失败，也至少执行一次轻量的模拟停留，
                        # 保证后续 ad/行为模块有数据而非空，使任务不会被判定为完全失败
                        if not load_success:
                            try:
                                _sim_stay = max(3.0, min(8.0, float(total_task_stay or 0) * 0.05))
                                log.warning(
                                    f"⚠️ 首页访问失败 → 进入降级模式：执行 {_sim_stay:.1f}秒"
                                    f"轻量停留（不影响统计核心）"
                                )
                                # 执行一次轻量的 simulate_human — 使用相同的 current_x/y
                                current_x, current_y = simulate_human_in_window(
                                    page, _sim_stay, page_behavior_stats,
                                    current_x or 100, current_y or 100, config,
                                    page_name="降级模式-首页",
                                )
                            except Exception as _e:
                                log.warning(f"⚠️ 降级模式停留失败: {str(_e)[:80]}")
                        
                        try:
                            _check_rope("广告检测前")
                        except Exception:
                            pass
                        ad_monitor = scan_ads_during_task(page, ad_monitor, "任务结束汇总前")
                        ad_found = len(ad_monitor.get("containers", set())) > 0
                        ad_in_viewport = len(ad_monitor.get("visible", set())) > 0
                        ad_loaded = ad_found
                        ad_impressions = len(ad_monitor.get("exposed", set()))
                        ad_refreshes = int(ad_monitor.get("refresh_count", 0) or 0)
                        _eff_exposed = len(ad_monitor.get("effective_exposed", set()))
                        _dur_map = ad_monitor.get("exposure_duration_ms", {}) or {}
                        _total_dur = sum(_dur_map.values())
                        _max_dur = max(_dur_map.values()) if _dur_map else 0
                        log.info(
                            f"[广告监控汇总] 扫描={ad_monitor.get('scan_count', 0)} "
                            f"容器去重={len(ad_monitor.get('containers', set()))} "
                            f"曾进入视口={len(ad_monitor.get('visible', set()))} "
                            f"曾曝光={ad_impressions} 刷新={ad_refreshes} "
                            f"有效曝光达标(≥50%可见且累计≥{int(config.get('ad_effective_exposure_ms', 1000) or 1000)}ms)={_eff_exposed} "
                            f"累计曝光时长={_total_dur}ms 单广告位最长={_max_dur}ms"
                        )
                        
                        log.page_ad_module(target_url, load_success, load_time, ad_found, ad_in_viewport, ad_loaded, ad_impressions, ad_refreshes)
                        
                        # ==================== 真人行为模块 ====================
                        # 合并所有真人行为统计
                        total_behavior_stats = {
                            "mouse_moves": 0,
                            "scrolls": 0,
                            "scroll_distance": 0,
                            "clicks": 0,
                            "waits": 0,
                            "focus_switches": 0,
                            "refreshes": 0,
                            "ad_stay": 0,
                            "total_stay": 0,
                            "key_presses": 0
                        }
                        
                        # 合并页面层级浏览的统计
                        if load_success and 'page_behavior_stats' in locals():
                            for key in total_behavior_stats:
                                total_behavior_stats[key] += page_behavior_stats.get(key, 0)
                        
                        # 合并视频广告的统计
                        if video_behavior_stats:
                            for key in total_behavior_stats:
                                total_behavior_stats[key] += video_behavior_stats.get(key, 0)
                        
                        # 层级浏览中已经做了很多行为模拟
                        if load_success:
                            log.info("页面层级浏览已完成真人行为模拟")
                        
                        log.behavior_module(
                            total_behavior_stats["mouse_moves"],
                            total_behavior_stats["scrolls"],
                            total_behavior_stats["scroll_distance"],
                            total_behavior_stats["clicks"],
                            total_behavior_stats["waits"],
                            total_behavior_stats["focus_switches"],
                            total_behavior_stats["refreshes"],
                            total_behavior_stats["ad_stay"],
                            total_behavior_stats["total_stay"],
                            total_behavior_stats["key_presses"]
                        )
                        
                        # ==================== 任务结果 ====================
                        task_time = time.time() - task_start_time
                        traffic_valid = bool(ad_loaded and ad_impressions > 0)
                        valid_traffic = traffic_valid
                        success = bool(load_success and consistency)
                        if success and not traffic_valid:
                            log.warning("⚠️ 任务流程已完成，但广告未形成有效曝光（任务不判失败，流量标记为无效）")
                        
                        # 更新任务状态
                        if task_idx < len(tasks_list):
                            if success:
                                tasks_list[task_idx]["status"] = "已完成"
                                # 更新current_plan中的状态
                                if current_plan and task_idx < len(current_plan.get('tasks', [])):
                                    current_plan['tasks'][task_idx]['status'] = "已完成"
                            else:
                                tasks_list[task_idx]["status"] = "失败"
                                # 更新current_plan中的状态
                                if current_plan and task_idx < len(current_plan.get('tasks', [])):
                                    current_plan['tasks'][task_idx]['status'] = "失败"
                        
                        if success:
                            stats["success"] += 1
                        else:
                            stats["fail"] += 1
                        
                        # ⏱️ 时间统计：前置流程时长（拨号→进入网站）、浏览网站时长（进入网站→任务结束）
                        _task_end_time = time.time()
                        if enter_site_time is not None:
                            _pre_dur = max(0.0, enter_site_time - dial_start_time)
                            _browse_dur = max(0.0, _task_end_time - enter_site_time)
                        else:
                            # 未成功进入网站（首页一直失败）：前置=全程，浏览=0
                            _pre_dur = max(0.0, _task_end_time - dial_start_time)
                            _browse_dur = 0.0
                        log.info(
                            f"<span style='color:#ff3333;font-weight:bold'>前置流程时长（拨号→进入网站）: {_pre_dur:.1f}秒</span>"
                        )
                        log.info(
                            f"<span style='color:#ff3333;font-weight:bold'>浏览网站时长（进入网站→任务结束）: {_browse_dur:.1f}秒</span>"
                        )

                        log.task_result(task_time, success, valid_traffic)
                    
                    except Exception as e:
                        stats["fail"] += 1
                        task_time = time.time() - task_start_time
                        log.error(f"任务异常: {str(e)}")
                        import traceback
                        log.debug(f"异常详情: {traceback.format_exc()}")
    
                        # ==================== （异常路径也输出）真人行为模块 ====================
                        _total_bs = {
                            "mouse_moves": 0, "scrolls": 0, "scroll_distance": 0,
                            "clicks": 0, "waits": 0, "focus_switches": 0,
                            "refreshes": 0, "ad_stay": 0, "total_stay": 0, "key_presses": 0
                        }
                        if 'page_behavior_stats' in locals() and page_behavior_stats:
                            for _k in _total_bs:
                                _total_bs[_k] += page_behavior_stats.get(_k, 0)
                        if video_behavior_stats:
                            for _k in _total_bs:
                                _total_bs[_k] += video_behavior_stats.get(_k, 0)
                        log.info(
                            f"⚠️ 任务异常，但已完成的真人行为："
                            f"鼠标移动 {_total_bs['mouse_moves']} 次，"
                            f"点击 {_total_bs['clicks']} 次，"
                            f"滚动 {_total_bs['scrolls']} 次({_total_bs['scroll_distance']}px)，"
                            f"键盘 {_total_bs['key_presses']} 次，"
                            f"随机等待 {_total_bs['waits']} 次"
                        )
                        log.behavior_module(
                            _total_bs["mouse_moves"], _total_bs["scrolls"],
                            _total_bs["scroll_distance"], _total_bs["clicks"],
                            _total_bs["waits"], _total_bs["focus_switches"],
                            _total_bs["refreshes"], _total_bs["ad_stay"],
                            float(_total_bs["total_stay"]), _total_bs["key_presses"]
                        )
    
                        # ⏱️ 异常路径也输出时间统计
                        _task_end_time = time.time()
                        _dst = locals().get("dial_start_time")
                        _est = locals().get("enter_site_time")
                        if _dst is not None:
                            if _est is not None:
                                _pre_dur = max(0.0, _est - _dst)
                                _browse_dur = max(0.0, _task_end_time - _est)
                            else:
                                _pre_dur = max(0.0, _task_end_time - _dst)
                                _browse_dur = 0.0
                            log.info(
                                f"<span style='color:#ff3333;font-weight:bold'>前置流程时长（拨号→进入网站）: {_pre_dur:.1f}秒</span>"
                            )
                            log.info(
                                f"<span style='color:#ff3333;font-weight:bold'>浏览网站时长（进入网站→任务结束）: {_browse_dur:.1f}秒</span>"
                            )

                        # 增强报警信息：包含异常类型、阶段、堆栈摘要
                        import traceback as _tb_inner
                        _err_type = type(e).__name__
                        _err_msg = str(e)[:200]
                        _tb_lines = _tb_inner.format_exc().splitlines()
                        _tb_summary = _tb_lines[-1] if _tb_lines else "无堆栈"
                        _stage = "浏览中" if locals().get("enter_site_time") else "前置流程"
                        log.error(
                            f"🚨 [任务报警] 任务#{task_idx+1} {_stage}异常 | "
                            f"类型={_err_type} | 信息={_err_msg} | 堆栈={_tb_summary}"
                        )
                        log.task_result(task_time, False, False, f"系统异常[{_err_type}]: {_err_msg}")
                        
                        # 更新任务状态为失败
                        if task_idx < len(tasks_list):
                            tasks_list[task_idx]["status"] = "失败"
                            # 更新current_plan中的状态
                            if current_plan and task_idx < len(current_plan.get('tasks', [])):
                                current_plan['tasks'][task_idx]['status'] = "失败"
                    finally:
                        # 按 page → context → browser 顺序关闭，避免 Chrome 进程残留
                        # 所有 close 操作加 timeout 保护，防止任务失败后卡死
                        import threading as _th
    
                        def _safe_close(_obj, _method_name, _timeout_ms, _desc):
                            """在独立线程中执行 close，超时则放弃，避免主流程卡死"""
                            _result = {"done": False, "err": None}
    
                            def _worker():
                                try:
                                    _fn = getattr(_obj, _method_name)
                                    # 安全关闭浏览器（timeout参数兼容Playwright API）
                                    try:
                                        _fn(timeout=_timeout_ms)
                                    except TypeError:
                                        # 某些版本不支持 timeout，直接调用
                                        _fn()
                                    _result["done"] = True
                                except Exception as _e:
                                    _result["err"] = str(_e)[:80]
    
                            _t = _th.Thread(target=_worker, daemon=True)
                            _t.start()
                            _t.join(_timeout_ms / 1000 + 1)  # 多给 1 秒缓冲
                            return _result["done"], _result["err"]
    
                        _cur_page = locals().get("page")
                        if _cur_page is not None:
                            _ok, _err = _safe_close(_cur_page, "close", 10000, "page")
                            if _ok:
                                log.debug("🧹 已关闭 page")
                            elif _err:
                                log.warning(f"⚠️ 关闭 page 失败: {_err}")
                            else:
                                log.warning("⚠️ 关闭 page 超时，跳过")
    
                        _cur_ctx = locals().get("context")
                        if _cur_ctx is not None and get_global_session_mode() == "country_host_7d":
                            try:
                                _urls_cfg = config.get("target_urls")
                                if isinstance(_urls_cfg, list) and _urls_cfg:
                                    _save_url = next((item.get("url", "").strip() for item in _urls_cfg if item.get("enabled") and item.get("url", "").strip()), config.get("target_url", ""))
                                else:
                                    _save_url = config.get("target_url", "")
                                save_qa_storage_state(
                                    _cur_ctx,
                                    _save_url,
                                    current_task.get("proxy_country", "US") if 'current_task' in locals() else "US",
                                    locals().get("qa_save_state_path"),
                                    locals().get("qa_meta_path")
                                )
                            except Exception as _qa_e:
                                log.warning(f"[QA会话] 保存失败: {str(_qa_e)[:120]}")
                        if _cur_ctx is not None:
                            _ok, _err = _safe_close(_cur_ctx, "close", 15000, "context")
                            if _ok:
                                log.debug("🧹 已关闭 context")
                            elif _err:
                                log.warning(f"⚠️ 关闭 context 失败: {_err}")
                            else:
                                log.warning("⚠️ 关闭 context 超时，跳过")
    
                        if browser:
                            _ok, _err = _safe_close(browser, "close", 20000, "browser")
                            if _ok:
                                log.debug("🧹 已关闭 browser")
                            elif _err:
                                log.warning(f"⚠️ 关闭 browser 失败: {_err}")
                            else:
                                log.warning("⚠️ 关闭 browser 超时，已跳过强制杀进程，等待系统自然回收")
    
                        # 不再强杀 Chrome/Chromium 进程：正常关闭失败时只记录，并给系统自然回收时间
                        if browser:
                            try:
                                import random as _rnd_cleanup
                                import time as _t_cleanup
                                _cleanup_wait = _rnd_cleanup.uniform(5, 10)
                                log.debug(f"🧹 浏览器关闭流程完成，等待 {_cleanup_wait:.1f}s 后进入下一任务")
                                _t_cleanup.sleep(_cleanup_wait)
                            except Exception:
                                pass
            except Exception as outer_e:
                log.error(f"外层任务异常: {str(outer_e)}")
                import traceback
                log.debug(f"外层异常详情: {traceback.format_exc()}")
            

    
    # 将任务计划添加到历史记录
    add_to_historical_tasks(daily_plan)
    record_kpi_snapshot()
    
    task_running = False
    _single_task_mode = False
    if adsl_ip_task:
        adsl_status["running"] = False
        adsl_status["status"] = "已停止" if adsl_status.get("completed", 0) < adsl_status.get("total", 0) else "完成"
    current_task_idx = -1
    current_plan = None
    stop_human_model()
    log.info("任务已停止")


def get_current_ip_context():
    ip = adsl_status.get("current_ip") or ""
    country = adsl_status.get("country") or ""
    language = ""
    timezone_name = os.environ.get("TZ") or ""
    local_time = ""
    if ip:
        try:
            from datetime import datetime
            local_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return {"ip": ip, "country": country, "language": language, "timezone": timezone_name, "local_time": local_time}

@app.route('/')
def index():
    ensure_config_defaults()
    return render_template_string(HTML_TEMPLATE, config=config, logs=list(reversed(log.messages[-500:])), 
                                  statstotal=stats['total'], statssuccess=stats['success'], 
                                  statsfail=stats['fail'],
                                  stats=stats, runningtask=task_running,
                                  planned_total=planned_total_tasks)




@app.route('/get_global_task_status', methods=['GET'])
def get_global_task_status():
    current_website_task = None
    if current_plan and current_task_idx >= 0 and current_task_idx < len(current_plan.get('tasks', [])):
        current_website_task = current_plan['tasks'][current_task_idx]
    qa_running = False
    with human_model_lock:
        human_model = dict(human_model_state)
    return jsonify({
        "website": {
            "running": bool(task_running or qa_running),
            "current_task_idx": current_task_idx,
            "current_task": current_website_task,
            "total_tasks": current_plan['total_tasks'] if current_plan else 0
        },

        "ip": get_current_ip_context(),
        "human_model": human_model,
        "stats": stats
    })


@app.route('/get_website_task_status', methods=['GET'])
def get_website_task_status():
    """获取网站流量任务状态"""
    global task_running, _single_task_mode, current_task_idx, current_plan
    current_task = None
    if current_plan and current_task_idx >= 0 and current_task_idx < len(current_plan.get('tasks', [])):
        current_task = current_plan['tasks'][current_task_idx]
    return jsonify({
        "running": task_running,
        "current_task_idx": current_task_idx,
        "current_task": current_task,
        "total_tasks": current_plan['total_tasks'] if current_plan else 0
    })


@app.route('/get_video_stats', methods=['GET'])
def get_video_stats():
    try:
        video_stats = {
            "total_views": get_total_video_views(),
            "country_views": get_country_video_views(),
            "video_item_success": stats.get("video_item_success", 0),
            "video_item_fail": stats.get("video_item_fail", 0)
        }
        return jsonify({"stats": video_stats})
    except Exception as e:
        log.error(f"获取视频任务统计失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/get_video_task_history', methods=['GET'])
def get_video_task_history():
    """获取视频任务历史记录（最多2天内的任务计划和7天完成情况）"""
    try:
        import datetime as dt
        
        # 获取历史任务记录
        global historical_tasks
        video_plans = []
        
        for plan in historical_tasks:
            # 检查是否是视频任务计划（model_used为simple_video）
            if plan.get("model_used") == "simple_video":
                video_plans.append(plan)
        
        # 获取当前时间
        now = dt.datetime.now(pytz.UTC)
        two_days_ago = now - dt.timedelta(days=2)
        seven_days_ago = now - dt.timedelta(days=7)
        
        recent_plans = []  # 2天内的任务计划
        completed_tasks = []  # 7天内完成的任务详情
        
        for plan in video_plans:
            plan_time = dt.datetime.fromisoformat(plan.get("created_at")) if plan.get("created_at") else None
            
            if plan_time:
                # 检查是否在7天内
                if plan_time >= seven_days_ago:
                    # 收集已完成的任务
                    if plan.get("tasks"):
                        for task in plan["tasks"]:
                            if task.get("status") == "已完成":
                                # 添加计划信息到任务中
                                task_with_plan = task.copy()
                                task_with_plan["plan_created_at"] = plan.get("created_at_local", "")
                                completed_tasks.append(task_with_plan)
                    
                    # 检查是否在2天内（任务计划）
                    if plan_time >= two_days_ago:
                        recent_plans.append(plan)
        
        return jsonify({
            "recent_plans": recent_plans,
            "completed_tasks": completed_tasks,
            "total_planned": len(recent_plans),
            "total_completed": len(completed_tasks),
            "plan_details": [{
                "created_at": p.get("created_at_local", ""),
                "total_tasks": p.get("total_tasks", 0),
                "completed_count": len([t for t in p.get("tasks", []) if t.get("status") == "已完成"]),
                "country_distribution": p.get("country_distribution", {})
            } for p in recent_plans]
        })
    except Exception as e:
        log.error(f"获取视频任务历史记录失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/save_config', methods=['POST'])
def save_config():
    global config, pending_plan
    data = request.get_json()
    
    # ⭐ 修复：识别“仅同步视频广告启用开关”的特殊字段，避免覆盖整个 video_ad 子配置
    if 'video_ad_enabled_only' in data:
        if 'video_ad' not in config or not isinstance(config.get('video_ad'), dict):
            config['video_ad'] = {}
        config['video_ad']['enabled'] = bool(data.pop('video_ad_enabled_only'))
    
    # 更新新增的配置项
    if 'site_creation_date' in data:
        config['site_creation_date'] = data['site_creation_date']
    if 'plan_days' in data:
        try:
            config['plan_days'] = max(1, min(7, int(data['plan_days'])))
        except Exception:
            config['plan_days'] = 1
    if 'adsl_task_count' in data:
        try:
            config['adsl_task_count'] = max(1, min(999, int(data['adsl_task_count'])))
        except Exception:
            config['adsl_task_count'] = 1
    if 'vt_adsl_task_count' in data:
        try:
            config['vt_adsl_task_count'] = max(1, min(999, int(data['vt_adsl_task_count'])))
        except Exception:
            config['vt_adsl_task_count'] = 1
    if 'session_mode' in data:
        config['session_mode'] = data['session_mode'] if data['session_mode'] in ("new_each_task", "country_host_7d") else "country_host_7d"
    if 'ua_repeat_max_rate' in data:
        try:
            config['ua_repeat_max_rate'] = max(0.0, min(1.0, float(data['ua_repeat_max_rate'])))
        except Exception:
            config['ua_repeat_max_rate'] = 0.2
    if 'selected_models' in data:
        config['selected_models'] = data['selected_models']
    if 'daily_traffic_range' in data:
        config['daily_traffic_range'] = data['daily_traffic_range']
    if 'proxy_pool' in data:
        config['proxy_pool'] = data['proxy_pool']
    
    # 明确处理网络配置字段
    if 'ip_proxy_api' in data:
        config['ip_proxy_api'] = data['ip_proxy_api']
    if 'ip_proxy_user' in data:
        config['ip_proxy_user'] = data['ip_proxy_user']
    if 'ip_proxy_pwd' in data:
        config['ip_proxy_pwd'] = data['ip_proxy_pwd']
    
    # 配置变更时清除待执行计划
    pending_plan = None
    
    # 处理网页跳转配置（深合并：保留默认值中新字段，如 loop_count/loop_interval/min_stay）
    if 'web_navigation' in data:
        if 'web_navigation' not in config or not isinstance(config.get('web_navigation'), dict):
            config['web_navigation'] = {}
        def _deep_merge_wn(base, update):
            if not isinstance(update, dict):
                return update
            result = dict(base) if isinstance(base, dict) else {}
            for k, v in update.items():
                if isinstance(v, dict) and isinstance(result.get(k), dict):
                    result[k] = _deep_merge_wn(result[k], v)
                else:
                    result[k] = v
            return result
        config['web_navigation'] = _deep_merge_wn(config['web_navigation'], data['web_navigation'])

    # 保存前校验：网页浏览模式配置
    try:
        ok, errors = validate_web_navigation_config(config, fail_hard=False)
        if not ok:
            return jsonify({"success": False, "status": "error", "message": "; ".join(errors)}), 400
    except Exception as e:
        return jsonify({"success": False, "status": "error", "message": str(e)}), 400
    
    # 保留原有逻辑
    config.update({k: v for k, v in data.items() if k not in [
        'site_creation_date', 'plan_days', 'adsl_task_count', 'vt_adsl_task_count',
        'session_mode', 'ua_repeat_max_rate', 'selected_models',
        'daily_traffic_range', 'proxy_pool', 'video_ad_enabled_only',
        'web_navigation'
    ]})
    
    with open('config.json', 'w') as f:
        json.dump(config, f, indent=4)
    
    # 记录配置审计日志
    changed_keys = list(data.keys())[:20]  # 最多记录20个字段
    record_config_audit("save_config", changed_keys=changed_keys, source="web_ui")
    
    log.info("配置已保存")
    return jsonify({"success": True, "status": "ok"})

@app.route('/get_config', methods=['GET'])
def get_config():
    """获取当前配置，用于页面加载时恢复代理池等配置"""
    return jsonify({
        "status": "ok",
        "config": config
    })


@app.route('/reset_config_defaults', methods=['POST'])
def reset_config_defaults():
    global config, pending_plan, video_plan, planned_total_tasks
    data = request.get_json(silent=True) or {}
    if task_running:
        return jsonify({"success": False, "status": "error", "message": "任务运行中，请先停止任务再恢复默认"}), 409
    config.clear()
    config.update(copy.deepcopy(DEFAULT_CONFIG))
    ensure_config_defaults()
    pending_plan = None
    planned_total_tasks = 0
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    record_config_audit("reset_config_defaults", changed_keys=["all"], source="web_ui")
    log.info(f"配置已恢复默认: scope={data.get('scope', 'all')}")
    return jsonify({"success": True, "status": "ok", "message": "配置已恢复默认"})

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    global pending_plan, planned_total_tasks
    try:
        # 生成计划但不执行
        pending_plan = generate_daily_tasks(config)
        log.info(f"✅ 计划生成成功: days={pending_plan.get('plan_days', 1)}, total={pending_plan['total_tasks']}, model={pending_plan['model_used']}")
        # 设置显示的总任务数
        planned_total_tasks = pending_plan['total_tasks']
        return jsonify({
            "status": "ok",
            "plan": pending_plan
        })
    except Exception as e:
        log.error(f"❌ 计划生成失败: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/get_plan')
def get_plan():
    global pending_plan
    return jsonify({
        "plan": pending_plan
    })

@app.route('/clear_plan', methods=['POST'])
def clear_plan():
    global pending_plan, planned_total_tasks
    pending_plan = None
    planned_total_tasks = 0
    log.info("✅ 计划已清除")
    return jsonify({"status": "ok"})


def clean_logs():
    """清空日志（包括前端显示和文件日志）"""
    import os
    # 1. 清空前端显示的内存日志列表
    log.messages = []
    
    # 2. 清空 logs/ 目录下的 .log 文件（VPS终端日志）
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    if os.path.exists(logs_dir):
        for filename in os.listdir(logs_dir):
            if filename.endswith('.log'):
                try:
                    with open(os.path.join(logs_dir, filename), 'w') as f:
                        f.write('')
                except Exception:
                    pass
@app.route('/start_task', methods=['POST'])
def start_task():
    global task_running
    if not task_running:
        # 清除以往日志
        clean_logs()
        threading.Thread(target=worker_task, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route('/start_single_task', methods=['POST'])
def start_single_task():
    global task_running
    if task_running:
        return jsonify({"status": "error", "message": "已有任务正在运行"}), 409
    # 清除历史日志
    clean_logs()
    threading.Thread(target=worker_task, kwargs={"single_task": True}, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route('/stop_task', methods=['POST'])
def stop_task():
    global task_running, _single_task_mode, adsl_status
    task_running = False
    _single_task_mode = False
    stop_human_model()
    if adsl_status.get("running"):
        adsl_status["status"] = "已停止"
        adsl_status["running"] = False
    # 强制关闭所有活跃浏览器（Selenium下停止标志位无法主动关闭浏览器，需直接quit driver）
    try:
        import selenium_bridge
        closed = selenium_bridge.force_quit_all()
        if closed:
            log.info(f"🛑 停止任务：已强制关闭 {closed} 个浏览器实例")
    except Exception as e:
        log.warning(f"强制关闭浏览器异常: {e}")
    return jsonify({"status": "ok"})

@app.route('/get_historical_tasks', methods=['GET'])
def get_historical_tasks():
    """获取近三天的历史任务记录"""
    return jsonify({
        "status": "ok",
        "historical_tasks": historical_tasks
    })

@app.route('/get_fingerprint_stats', methods=['GET'])
def get_fingerprint_stats():
    """获取指纹和UA统计数据"""
    # 对UA统计按使用次数排序
    sorted_ua_stats = sorted(
        fingerprint_stats["ua_usage"].items(),
        key=lambda x: x[1],
        reverse=True
    )
    
    # 对指纹统计按使用次数排序
    sorted_fingerprint_stats = sorted(
        fingerprint_stats["fingerprint_usage"].items(),
        key=lambda x: x[1],
        reverse=True
    )
    
    return jsonify({
        "status": "ok",
        "ua_stats": sorted_ua_stats,
        "fingerprint_stats": sorted_fingerprint_stats,
        "history": fingerprint_stats["history"],
        "total_tasks": len(fingerprint_stats["history"])
    })

# ===== 攻防演练（risk_check.py）状态与路由 =====
_drill_state = {"running": False, "progress": 0, "stage": "未开始", "report": None, "json_path": None, "html_path": None}
_drill_lock = threading.Lock()


def _run_drill_thread(target_url, headless):
    global _drill_state
    try:
        import risk_check
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        def _log_fn(msg):
            log.info(f"[攻防演练] {msg}")
        def _progress_fn(pct, stage):
            _drill_state["progress"] = int(pct)
            _drill_state["stage"] = stage
        def _do_drill():
            return risk_check.run_drill(
                target_url, headless=headless, log_fn=_log_fn, progress_fn=_progress_fn, with_stealth=True
            )
        # 整体超时保护：120秒
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_drill)
            try:
                report, json_path, html_path = future.result(timeout=120)
            except FuturesTimeout:
                log.error("[攻防演练] 超时（120秒），强制结束")
                _drill_state["stage"] = "超时结束"
                _drill_state["progress"] = 100
                return
        # 保存完整报告用于前端展示
        _drill_state["report"] = (report or {}).get("risk_calc", {})
        _drill_state["full_report"] = report or {}
        _drill_state["json_path"] = json_path
        _drill_state["html_path"] = html_path
    except Exception as e:
        log.error(f"[攻防演练] 执行失败: {type(e).__name__}: {str(e)[:160]}")
        _drill_state["stage"] = f"异常: {type(e).__name__}"
    finally:
        _drill_state["running"] = False


@app.route('/start_security_drill', methods=['POST'])
def start_security_drill():
    global _drill_state
    with _drill_lock:
        if _drill_state["running"]:
            return jsonify({"status": "error", "success": False, "message": "已有攻防演练正在运行"}), 409
        # 取配置面板中已勾选的第一个目标站
        target_url = ""
        _urls_cfg = config.get("target_urls")
        if isinstance(_urls_cfg, list) and _urls_cfg:
            target_url = next((item.get("url", "").strip() for item in _urls_cfg
                               if item.get("enabled") and item.get("url", "").strip()), "")
        if not target_url:
            target_url = config.get("target_url", "") or ""
        try:
            body = request.get_json(silent=True) or {}
        except Exception:
            body = {}
        if body.get("target_url"):
            target_url = str(body["target_url"]).strip()
        if not target_url:
            return jsonify({"status": "error", "success": False, "message": "未配置目标网站，请先在网站流量Tab勾选目标站"}), 400
        headless = bool(body.get("headless", True))
        _drill_state.update({"running": True, "progress": 0, "stage": "启动中",
                             "report": None, "json_path": None, "html_path": None})
    from threading import Thread
    Thread(target=_run_drill_thread, args=(target_url, headless), daemon=True).start()
    log.info(f"✅ 攻防演练线程已启动，目标: {target_url}")
    return jsonify({"status": "ok", "success": True, "message": "攻防演练已启动", "target_url": target_url})


@app.route('/get_security_drill_status')
def get_security_drill_status():
    return jsonify({
        "running": _drill_state["running"],
        "progress": _drill_state["progress"],
        "stage": _drill_state["stage"],
        "report": _drill_state["report"],
        "full_report": _drill_state.get("full_report"),  # 新增：返回完整报告
        "json_path": _drill_state["json_path"],
        "html_path": _drill_state["html_path"],
    })


# ===== 生产准入五层测试（production_test.py）状态与路由 =====
_prodtest_state = {
    "running": False, "progress": 0, "stage": "未开始",
    "layers": {}, "gate": None, "report_path": None, "logs": [],
}
_prodtest_lock = threading.Lock()


def _run_production_test_thread(layers, headless, target_url):
    global _prodtest_state
    try:
        import production_test

        def _log_fn(msg):
            log.info(f"[生产准入] {msg}")
            _prodtest_state["logs"].append(str(msg))
            if len(_prodtest_state["logs"]) > 300:
                _prodtest_state["logs"] = _prodtest_state["logs"][-300:]

        def _progress_fn(pct, stage):
            _prodtest_state["progress"] = int(pct)
            _prodtest_state["stage"] = stage

        result = production_test.run_production_test(
            layers=layers, progress=_progress_fn, log=_log_fn,
            config=config, target_url=target_url, headless=headless,
        )
        _prodtest_state["layers"] = result.get("layers", {})
        _prodtest_state["gate"] = result.get("gate")
        _prodtest_state["report_path"] = result.get("report_path")
        _prodtest_state["stage"] = "完成"
        _prodtest_state["progress"] = 100
    except Exception as e:
        log.error(f"[生产准入] 执行失败: {type(e).__name__}: {str(e)[:160]}")
        _prodtest_state["stage"] = f"异常: {type(e).__name__}"
    finally:
        _prodtest_state["running"] = False


@app.route('/start_production_test', methods=['POST'])
def start_production_test():
    global _prodtest_state
    with _prodtest_lock:
        if _prodtest_state["running"]:
            return jsonify({"status": "error", "success": False, "message": "已有生产准入测试正在运行"}), 409
        try:
            body = request.get_json(silent=True) or {}
        except Exception:
            body = {}
        layers = body.get("layers") or "all"
        headless = bool(body.get("headless", True))
        # 从配置取第一个已勾选目标站（L3对抗验证需要真实目标）
        target_url = ""
        _urls_cfg = config.get("target_urls")
        if isinstance(_urls_cfg, list) and _urls_cfg:
            target_url = next((item.get("url", "").strip() for item in _urls_cfg
                               if item.get("enabled") and item.get("url", "").strip()), "")
        if not target_url:
            target_url = config.get("target_url", "") or ""
        _prodtest_state.update({"running": True, "progress": 0, "stage": "启动中",
                                "layers": {}, "gate": None, "report_path": None, "logs": []})
    from threading import Thread
    Thread(target=_run_production_test_thread, args=(layers, headless, target_url), daemon=True).start()
    log.info(f"✅ 生产准入测试线程已启动，层级: {layers}")
    return jsonify({"status": "ok", "success": True, "message": "生产准入测试已启动", "layers": layers})


@app.route('/get_production_test_status')
def get_production_test_status():
    return jsonify({
        "running": _prodtest_state["running"],
        "progress": _prodtest_state["progress"],
        "stage": _prodtest_state["stage"],
        "layers": _prodtest_state["layers"],
        "gate": _prodtest_state["gate"],
        "report_path": _prodtest_state["report_path"],
        "logs": _prodtest_state["logs"][-60:],
    })


# ==================== 关键词探索功能 ====================
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import re

KEYWORD_EXPLORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'keyword_explore')
os.makedirs(KEYWORD_EXPLORE_DIR, exist_ok=True)

keyword_explore_manager = {
    'is_running': False,
    'progress': '',
    'current_layer': 0,
    'max_layer': 6,
    'result': None,
    'error': None
}


# -------- 关键词提取：从<a>标签提取可点击链接文本 --------
# 需要过滤的导航/功能性链接文本
_NAV_FILTER = {
    'home', '首页', 'back', '返回', 'top', '顶部', 'up', 'next', 'prev',
    'previous', 'last', 'first', 'newer', 'older', 'more', 'load more',
    'read more', 'view all', 'see all', 'show more', 'show less',
    'menu', '导航', '搜索', 'search', 'login', '登录', 'register', '注册',
    'sign in', 'sign up', 'logout', '退出', 'account', 'my account',
    'cart', '购物车', 'checkout', '结算', 'wishlist', '收藏',
    'share', '分享', 'print', '打印', 'email', '邮件', 'contact', '联系',
    'about', '关于', 'privacy', '隐私', 'terms', '条款', 'cookie',
    'facebook', 'twitter', 'instagram', 'youtube', 'tiktok', 'weibo',
    'weixin', 'wechat', 'whatsapp', 'telegram', 'linkedin',
    'rss', 'sitemap', 'archive', '归档', 'tag', '标签', 'category', '分类',
    'subscribe', '订阅', 'unsubscribe', '取消订阅', 'follow', '关注',
    'download', '下载', 'upload', '上传', 'submit', '提交', 'cancel', '取消',
    'ok', 'yes', 'no', 'close', '关闭', 'skip', '跳过', 'continue', '继续',
    # 常见英文停用词（可能出现在锚文本中）
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'could', 'should', 'may', 'might', 'can', 'shall', 'it',
    'its', 'this', 'that', 'these', 'those', 'i', 'you', 'he', 'she',
    'we', 'they', 'me', 'him', 'her', 'us', 'them', 'my', 'your', 'his',
    'our', 'their', 'what', 'which', 'who', 'whom', 'when', 'where', 'why',
    'how', 'all', 'each', 'every', 'both', 'few', 'many', 'some', 'any',
    'most', 'other', 'such', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 'just', 'because', 'if', 'then', 'else', 'not', 'no', 'nor',
    'into', 'over', 'after', 'before', 'between', 'under', 'again',
    'there', 'here', 'once', 'during', 'while', 'through', 'above', 'below',
    'until', 'against', 'further', 'down', 'off', 'out', 'up', 'about',
}

# 常见英文停用词（用于过滤锚文本中的无意义短词）
_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
    'been', 'it', 'this', 'that', 'i', 'you', 'he', 'she', 'we', 'they',
    'me', 'my', 'your', 'his', 'our', 'their', 'what', 'which', 'who',
    'how', 'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other',
    'some', 'any', 'no', 'not', 'so', 'if', 'then', 'than', 'too', 'very',
    'can', 'will', 'just', 'do', 'did', 'does', 'had', 'have', 'has',
    'would', 'could', 'should', 'may', 'might', 'shall', 'need', 'must',
}

def _extract_anchor_texts_from_html(html, url):
    """从HTML页面中提取所有<a>标签的可点击链接文本作为关键词"""
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')

    keywords = set()
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        if not text:
            # 尝试从 title 或 aria-label 获取
            text = a.get('title', '') or a.get('aria-label', '') or ''
            text = text.strip()
        if not text:
            continue
        # 过滤：太短、纯数字、导航/功能性文本
        if len(text) < 2:
            continue
        if text.isdigit() or text.isnumeric():
            continue
        text_lower = text.lower()
        if text_lower in _NAV_FILTER:
            continue
        # 过滤停用词
        if text_lower in _STOP_WORDS:
            continue
        # 过滤纯标点
        if all(not c.isalnum() for c in text):
            continue
        keywords.add(text)
    return keywords


def _extract_page_links(html, base_url, target_domain):
    """从HTML中提取同域 outgoing 链接（用于兜底链接池）"""
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')

    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:') or href.startswith('tel:'):
            continue
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        if parsed.scheme not in ['http', 'https']:
            continue
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # 同域检查（允许子域名）
        if parsed.netloc and (target_domain in parsed.netloc or parsed.netloc.endswith('.' + target_domain)):
            if not _is_forbidden_url(parsed.path):
                links.add(clean_url)
    return links


# -------- 辅助函数 --------
FORBIDDEN_PATTERNS = [
    '/login', '/logout', '/admin', '/wp-admin', '/wp-login',
    '/register', '/signup', '/cart', '/checkout', '/account'
]

def _is_forbidden_url(url_path):
    """检查 URL 路径是否匹配 FORBIDDEN_PATTERNS"""
    path_lower = url_path.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.lower() in path_lower:
            return True
    return False


def _is_same_origin(url, target_domain):
    """检查 URL 是否同域"""
    try:
        parsed = urlparse(url)
        return parsed.netloc.split(':')[0] == target_domain
    except Exception:
        return False


def _fetch_page(url, session=None, timeout=10):
    """获取单个页面 HTML"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    }
    try:
        if session:
            r = session.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.get(url, headers=headers, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.text
    except Exception:
        return None



# -------- 爬取主逻辑 --------
def _crawl_keyword_explore(target_url, max_layer=5, concurrency=4):
    """执行关键词探索爬取：
    - 关键词 = 每层页面上<a>标签的可点击链接文本
    - 兜底链接 = 该层页面中，没有匹配到关键词池的页面的 outgoing 链接
    """
    parsed = urlparse(target_url)
    target_domain = parsed.netloc.split(':')[0]

    mgr = keyword_explore_manager
    mgr['is_running'] = True
    mgr['progress'] = '准备开始爬取...'
    mgr['current_layer'] = 0
    mgr['result'] = None
    mgr['error'] = None

    session = requests.Session()
    session.verify = False

    visited = set()
    layer_anchor_texts = {}   # {layer_num: set(anchor_text)} 每层的关键词（可点击链接文本）
    layer_fallback_links = {} # {layer_num: set(url)} 每层的兜底链接
    all_urls = set()          # 所有层级的去重URL汇总

    current_urls = {target_url.rstrip('/')}

    try:
        log.info(f'[关键词探索] 开始 | 目标: {target_url} | 最大层数: {max_layer}')

        for layer in range(max_layer + 1):
            mgr['current_layer'] = layer
            mgr['progress'] = f'正在探索第 {layer} 层（共 {len(current_urls)} 个页面）...'
            log.info(f'[关键词探索] === 第 {layer}/{max_layer} 层 | {len(current_urls)} 个页面 ===')

            if not current_urls:
                log.warning(f'[关键词探索] 第 {layer} 层无新页面，提前结束')
                break

            urls_to_fetch = [
                u for u in current_urls
                if _is_same_origin(u, target_domain)
                and not _is_forbidden_url(urlparse(u).path)
            ]
            current_urls = set()

            # 每层最多50个页面
            if len(urls_to_fetch) > 50:
                log.warning(f'[关键词探索] 第 {layer} 层页面过多，截断为50个')
                urls_to_fetch = urls_to_fetch[:50]

            log.info(f'[关键词探索] 第 {layer} 层实际待抓取: {len(urls_to_fetch)} 个页面')

            # 存储每个页面的 (anchor_texts, outgoing_links)
            page_data = []  # [(url, anchor_texts_set, outgoing_links_set), ...]

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {}
                for url in urls_to_fetch:
                    visited.add(url)
                    time.sleep(random.uniform(0.1, 0.3))
                    futures[executor.submit(_fetch_page, url, session)] = url

                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        html = future.result(timeout=15)
                        if not html:
                            log.warning(f'[关键词探索] 获取失败: {url}')
                            continue

                        # 提取锚文本（作为关键词）
                        anchor_texts = _extract_anchor_texts_from_html(html, url)
                        # 提取 outgoing 链接
                        outgoing_links = _extract_page_links(html, url, target_domain)

                        page_data.append((url, anchor_texts, outgoing_links))
                        log.info(f'[关键词探索] ✅ {url} | 锚文本: {len(anchor_texts)} | 出站链接: {len(outgoing_links)}')

                    except Exception as e:
                        log.warning(f'[关键词探索] 页面异常 {url}: {e}')

            # ---- 构建本层关键词池和兜底链接 ----
            # 本层关键词 = 所有页面的锚文本并集（可点击链接文本）
            layer_kw_pool = set()
            for _, ats, _ in page_data:
                layer_kw_pool.update(ats)

            # 本层兜底链接 = 该层所有页面的出站链接合并去重
            layer_fb = set()
            for _, _, out_links in page_data:
                layer_fb.update(out_links)
            # 兜底链接去除已经作为关键词来源的页面URL
            layer_fb -= {u for u, _, _ in page_data}

            layer_anchor_texts[layer] = layer_kw_pool
            layer_fallback_links[layer] = layer_fb
            log.info(f'[关键词探索] 第 {layer} 层完成 | 关键词(锚文本): {len(layer_kw_pool)} | 兜底链接: {len(layer_fb)}')

            # 下一层的URL = 本层所有页面的 outgoing 链接（去重后未访问的）
            next_layer_urls = set()
            for _, _, out_links in page_data:
                for link in out_links:
                    if link not in visited:
                        next_layer_urls.add(link)
                        all_urls.add(link)
            current_urls = next_layer_urls

        # 构建报告数据
        total_keywords = sum(len(v) for v in layer_anchor_texts.values())
        total_fallback = sum(len(v) for v in layer_fallback_links.values())
        mgr['progress'] = '生成报告文件...'

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        domain_clean = re.sub(r'[^\w.]', '_', target_domain)
        ts_file = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'keywords_{domain_clean}_{ts_file}.txt'
        filepath = os.path.join(KEYWORD_EXPLORE_DIR, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('# 关键词探索报告\n')
            f.write(f'# 目标网站: {target_url}\n')
            f.write(f'# 生成时间: {timestamp}\n')
            f.write(f'# 总关键词数: {total_keywords}\n')
            f.write(f'# 总兜底链接数: {total_fallback}\n\n')

            for layer_num in sorted(layer_anchor_texts.keys()):
                kws = layer_anchor_texts[layer_num]
                fbs = layer_fallback_links.get(layer_num, set())
                f.write(f'## Layer {layer_num} 关键词 (共{len(kws)}个，可点击链接文本)\n')
                for kw in sorted(kws):
                    f.write(f'{kw}\n')
                f.write(f'\n## Layer {layer_num} 兜底链接 (共{len(fbs)}个)\n')
                for url in sorted(fbs):
                    f.write(f'{url}\n')
                f.write('\n')

            # 合并所有层的关键词和兜底链接
            all_merged_kws = set()
            all_merged_fbs = set()
            for kws in layer_anchor_texts.values():
                all_merged_kws.update(kws)
            for fbs in layer_fallback_links.values():
                all_merged_fbs.update(fbs)

            f.write(f'## 合并关键词池 (共{len(all_merged_kws)}个)\n')
            for kw in sorted(all_merged_kws):
                f.write(f'{kw}\n')
            f.write(f'\n## 合并兜底链接池 (共{len(all_merged_fbs)}个，去重)\n')
            for url in sorted(all_merged_fbs):
                f.write(f'{url}\n')

        # 构建返回给前端的数据
        layer_summary = {str(k): len(v) for k, v in layer_anchor_texts.items()}
        fb_summary = {str(k): len(v) for k, v in layer_fallback_links.items()}

        # 按层整理数据供前端自动填写配置
        # 后端层号是 0-based (0,1,2...)，前端UI层号是 1-based (1,2,3...)
        # 所以后端 layer 0 → 前端 layer_1，后端 layer 1 → 前端 layer_2 ...
        layer_data_for_frontend = {}
        for backend_layer in range(0, max_layer + 1):
            frontend_layer = backend_layer + 1  # 0→1, 1→2, ...
            if frontend_layer > 5:
                break
            kws_list = sorted(layer_anchor_texts.get(backend_layer, set()))
            fbs_list = sorted(layer_fallback_links.get(backend_layer, set()))
            layer_data_for_frontend[f'layer_{frontend_layer}'] = {
                'keywords': kws_list,
                'fallback_urls': fbs_list
            }

        mgr['result'] = {
            'file_path': filepath,
            'filename': filename,
            'total_keywords': total_keywords,
            'total_fallback_links': total_fallback,
            'total_links': len(all_urls),
            'layer_summary': layer_summary,
            'fb_summary': fb_summary,
            'layers_crawled': len(layer_anchor_texts),
            'layer_data': layer_data_for_frontend,
            'merged_keywords': sorted(all_merged_kws),
            'merged_fallback_urls': sorted(all_merged_fbs)
        }
        mgr['progress'] = f'探索完成！共 {total_keywords} 个关键词，{total_fallback} 个兜底链接'
        log.info(f'[关键词探索] ✅ 完成！关键词: {total_keywords} | 兜底链接: {total_fallback} | 文件: {filename}')

    except Exception as e:
        mgr['error'] = str(e)
        mgr['progress'] = f'探索失败: {str(e)}'
        log.error(f'[关键词探索] 异常: {str(e)}')
    finally:
        mgr['is_running'] = False


# -------- 路由 --------
@app.route('/api/keyword_explore', methods=['POST'])
def start_keyword_explore():
    if keyword_explore_manager['is_running']:
        return jsonify({'success': False, 'message': '关键词探索正在进行中，请等待完成'})

    data = request.json or {}
    target_url = data.get('target_url', '').strip()
    if not target_url:
        return jsonify({'success': False, 'message': '请填写目标网址'})

    try:
        parsed = urlparse(target_url)
        if not parsed.scheme or not parsed.netloc:
            return jsonify({'success': False, 'message': '目标网址格式无效'})
    except Exception:
        return jsonify({'success': False, 'message': '目标网址格式无效'})

    max_layer = int(data.get('max_layer', 5))
    concurrency = int(data.get('concurrency', 4))

    thread = threading.Thread(
        target=_crawl_keyword_explore,
        args=(target_url, max_layer, concurrency)
    )
    thread.daemon = True
    thread.start()

    log.info(f'用户启动关键词探索 | 目标: {target_url}')
    return jsonify({'success': True, 'message': '关键词探索已启动'})


@app.route('/api/keyword_explore/status', methods=['GET'])
def get_keyword_explore_status():
    return jsonify({
        'success': True,
        'data': {
            'is_running': keyword_explore_manager['is_running'],
            'progress': keyword_explore_manager['progress'],
            'current_layer': keyword_explore_manager['current_layer'],
            'max_layer': keyword_explore_manager['max_layer'],
            'result': keyword_explore_manager['result'],
            'error': keyword_explore_manager['error']
        }
    })


@app.route('/api/keyword_explore/download/<filename>', methods=['GET'])
def download_keyword_explore(filename):
    try:
        safe_name = os.path.basename(filename)
        if not safe_name or '..' in filename:
            return jsonify({'success': False, 'message': '无效的文件名'}), 400
        
        file_path = os.path.join(KEYWORD_EXPLORE_DIR, safe_name)
        if not os.path.exists(file_path):
            log.error(f'[关键词探索] 文件不存在: {file_path}')
            return jsonify({'success': False, 'message': f'文件不存在: {safe_name}'}), 404
        
        return send_from_directory(KEYWORD_EXPLORE_DIR, safe_name, as_attachment=True)
    except Exception as e:
        log.error(f'[关键词探索] 下载失败: {str(e)}')
        return jsonify({'success': False, 'message': f'下载失败: {str(e)}'}), 500


@app.route('/get_logs')
def get_logs():
    mode = request.args.get('mode') or config.get('log_mode', 'test')
    messages = log.messages
    if mode == 'prod':
        hide_keywords = (
            '127.0.0.1 - -', 'GET /get_logs', 'GET /api/status',
            'GET /get_video_task_status', 'GET /get_website_task_status',
            'proxy_info 完整内容', 'proxy_host:', 'proxy_port:',
            '关闭 page', '关闭 context', '关闭 browser', '浏览器关闭流程完成',
            'DEBUG', '异常详情', '外层异常详情'
        )
        messages = [m for m in messages if not any(k in m for k in hide_keywords)]
    try:
        limit = int(request.args.get('limit', 500))
    except Exception:
        limit = 500
    limit = max(50, min(limit, 500))
    messages = messages[-limit:]
    # 从下往上展示：最新日志置顶（倒序输出）
    return ''.join([f"<p>{msg}</p>" for msg in reversed(messages)])

@app.route('/api/status')
def api_status():
    return jsonify({
        "running": task_running,
        "total": stats["total"],
        "success": stats["success"],
        "fail": stats["fail"],
        "video_view_count": stats.get("video_view_count", 0),
        "total_video_watch_time": stats.get("total_video_watch_time", 0),
        "adsl": adsl_status
    })

@app.route('/api/debug_config')
def debug_config():
    """调试接口：查看当前配置内容"""
    return jsonify({
        "proxy_pool_count": len(config.get('proxy_pool', [])),
        "proxy_pool": config.get('proxy_pool', [])
    })

@app.route('/save_seo_config', methods=['POST'])
def save_seo_config():
    global config
    data = request.get_json()
    
    # 更新搜索引擎 & 社媒平台列表（含type字段）
    config['seo']['search_engines'] = data.get('search_engines', [])
    
    # 更新国别-平台映射（前端直接传递完整map）
    region_map = data.get('region_engine_map', {})
    if region_map:
        config['seo']['region_engine_map'] = region_map
    
    # 更新关键词池
    config['seo']['keyword_pools']['zh'] = [s.strip() for s in data.get('seo_keywords_zh', '').split(',') if s.strip()]
    config['seo']['keyword_pools']['en'] = [s.strip() for s in data.get('seo_keywords_en', '').split(',') if s.strip()]
    
    # 更新Referer模式
    config['seo']['referer_mode'] = data.get('seo_referer_mode', 'dynamic')
    
    # 保存到配置文件
    with open('config.json', 'w') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    # 重置SEO查询模块实例，使新配置生效
    try:
        import seo_query_module
        seo_query_module.reset_seo_query_instance()
    except Exception:
        pass
    
    log.info("SEO配置已保存")
    return jsonify({"status": "ok"})


# ==================== KPI 仪表盘 ====================
KPI_DASHBOARD_FILE = "kpi_dashboard.json"
_kpi_lock = threading.Lock()


def _load_kpi_data():
    """加载 KPI 仪表盘数据"""
    try:
        if os.path.exists(KPI_DASHBOARD_FILE):
            with open(KPI_DASHBOARD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"加载 KPI 数据失败: {e}")
    return {"daily": {}}


def _save_kpi_data(data):
    """保存 KPI 仪表盘数据"""
    try:
        with open(KPI_DASHBOARD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"保存 KPI 数据失败: {e}")


def record_kpi_snapshot():
    """记录当前 KPI 快照到仪表盘数据（每次任务完成时调用）"""
    import datetime as _dt
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    with _kpi_lock:
        data = _load_kpi_data()
        daily = data.setdefault("daily", {})
        entry = daily.setdefault(today, {
            "tasks_total": 0, "tasks_success": 0, "tasks_fail": 0,
            "video_views": 0, "ad_clicks": 0, "unique_ips": 0
        })
        entry["tasks_total"] = stats.get("total", 0)
        entry["tasks_success"] = stats.get("success", 0)
        entry["tasks_fail"] = stats.get("fail", 0)
        entry["video_views"] = stats.get("video_view_count", 0)
        entry["ad_clicks"] = get_daily_ad_clicks()
        # 保留最近 30 天
        keys = sorted(daily.keys())
        for k in keys[:-30]:
            daily.pop(k, None)
        _save_kpi_data(data)


@app.route("/api/kpi_dashboard", methods=["GET"])
def api_kpi_dashboard():
    """KPI 仪表盘数据 API"""
    with _kpi_lock:
        data = _load_kpi_data()
    return jsonify(data)


@app.route("/kpi_dashboard", methods=["GET"])
def kpi_dashboard_page():
    """KPI 仪表盘页面"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KPI 仪表盘</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#f5f7fa; color:#333; }
        .header { background:#1a73e8; color:#fff; padding:20px 30px; }
        .header h1 { font-size:22px; }
        .container { max-width:1200px; margin:20px auto; padding:0 20px; }
        .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:24px; }
        .card { background:#fff; border-radius:10px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.08); text-align:center; }
        .card .value { font-size:32px; font-weight:700; color:#1a73e8; }
        .card .label { font-size:13px; color:#666; margin-top:6px; }
        .chart-container { background:#fff; border-radius:10px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.08); margin-bottom:20px; }
        .chart-container h3 { margin-bottom:12px; font-size:16px; }
        .nav { display:flex; gap:12px; margin-bottom:20px; }
        .nav a { color:#1a73e8; text-decoration:none; padding:8px 16px; border:1px solid #1a73e8; border-radius:6px; }
        .nav a:hover { background:#1a73e8; color:#fff; }
    </style>
</head>
<body>
    <div class="header">
        <h1>KPI 仪表盘</h1>
    </div>
    <div class="container">
        <div class="nav">
            <a href="/">← 返回主页</a>
            <a href="/config_audit_log">配置审计日志</a>
        </div>
        <div class="cards" id="cards"></div>
        <div class="chart-container">
            <h3>近 7 天任务趋势</h3>
            <canvas id="taskChart"></canvas>
        </div>
        <div class="chart-container">
            <h3>近 7 天视频播放 & 广告点击</h3>
            <canvas id="engagementChart"></canvas>
        </div>
    </div>
    <script>
        fetch('/api/kpi_dashboard')
            .then(r => r.json())
            .then(data => {
                const daily = data.daily || {};
                const dates = Object.keys(daily).sort().slice(-7);
                if (dates.length === 0) {
                    document.getElementById('cards').innerHTML = '<div class="card"><div class="value">—</div><div class="label">暂无数据</div></div>';
                    return;
                }
                const latest = daily[dates[dates.length - 1]];
                const cardsHtml = [
                    {v: latest.tasks_total || 0, l: '今日任务总数'},
                    {v: latest.tasks_success || 0, l: '成功'},
                    {v: latest.tasks_fail || 0, l: '失败'},
                    {v: latest.video_views || 0, l: '视频播放'},
                    {v: latest.ad_clicks || 0, l: '广告点击'},
                ].map(c => `<div class="card"><div class="value">${c.v}</div><div class="label">${c.l}</div></div>`).join('');
                document.getElementById('cards').innerHTML = cardsHtml;

                const taskCtx = document.getElementById('taskChart').getContext('2d');
                new Chart(taskCtx, {
                    type: 'bar',
                    data: {
                        labels: dates,
                        datasets: [
                            {label:'成功', data:dates.map(d=>daily[d].tasks_success||0), backgroundColor:'#34a853'},
                            {label:'失败', data:dates.map(d=>daily[d].tasks_fail||0), backgroundColor:'#ea4335'},
                        ]
                    },
                    options: {responsive:true, scales:{x:{stacked:true}, y:{stacked:true, beginAtZero:true}}}
                });

                const engCtx = document.getElementById('engagementChart').getContext('2d');
                new Chart(engCtx, {
                    type: 'line',
                    data: {
                        labels: dates,
                        datasets: [
                            {label:'视频播放', data:dates.map(d=>daily[d].video_views||0), borderColor:'#1a73e8', tension:0.3},
                            {label:'广告点击', data:dates.map(d=>daily[d].ad_clicks||0), borderColor:'#fbbc04', tension:0.3},
                        ]
                    },
                    options: {responsive:true, scales:{y:{beginAtZero:true}}}
                });
            });
    </script>
</body>
</html>
''')


# ==================== 配置审计日志 ====================
CONFIG_AUDIT_LOG_FILE = "config_audit_log.json"
_config_audit_lock = threading.Lock()


def _load_config_audit_log():
    """加载配置审计日志"""
    try:
        if os.path.exists(CONFIG_AUDIT_LOG_FILE):
            with open(CONFIG_AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"加载配置审计日志失败: {e}")
    return []


def _save_config_audit_log(data):
    """保存配置审计日志"""
    try:
        with open(CONFIG_AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"保存配置审计日志失败: {e}")


def record_config_audit(action, changed_keys=None, source="web_ui"):
    """记录配置变更审计日志"""
    import datetime as _dt
    entry = {
        "timestamp": _dt.datetime.utcnow().isoformat() + "Z",
        "timestamp_local": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "changed_keys": changed_keys or [],
        "source": source,
    }
    with _config_audit_lock:
        log_data = _load_config_audit_log()
        log_data.append(entry)
        # 保留最近 200 条记录
        log_data = log_data[-200:]
        _save_config_audit_log(log_data)
    log.info(f"[配置审计] {action}: {changed_keys}")


@app.route("/api/config_audit_log", methods=["GET"])
def api_config_audit_log():
    """配置审计日志 API"""
    with _config_audit_lock:
        log_data = _load_config_audit_log()
    return jsonify({"entries": log_data[-100:]})


@app.route("/config_audit_log", methods=["GET"])
def config_audit_log_page():
    """配置审计日志页面"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>配置审计日志</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#f5f7fa; color:#333; }
        .header { background:#34a853; color:#fff; padding:20px 30px; }
        .header h1 { font-size:22px; }
        .container { max-width:1200px; margin:20px auto; padding:0 20px; }
        .nav { display:flex; gap:12px; margin-bottom:20px; }
        .nav a { color:#1a73e8; text-decoration:none; padding:8px 16px; border:1px solid #1a73e8; border-radius:6px; }
        .nav a:hover { background:#1a73e8; color:#fff; }
        table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08); }
        th { background:#34a853; color:#fff; text-align:left; padding:12px 16px; font-size:13px; }
        td { padding:10px 16px; border-bottom:1px solid #eee; font-size:13px; }
        tr:hover { background:#f0f8f0; }
        .action-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; }
        .action-save { background:#e8f5e9; color:#2e7d32; }
        .action-reset { background:#fff3e0; color:#e65100; }
        .empty { text-align:center; padding:40px; color:#999; }
    </style>
</head>
<body>
    <div class="header">
        <h1>配置审计日志</h1>
    </div>
    <div class="container">
        <div class="nav">
            <a href="/">← 返回主页</a>
            <a href="/kpi_dashboard">KPI 仪表盘</a>
        </div>
        <div id="logTable"></div>
    </div>
    <script>
        fetch('/api/config_audit_log')
            .then(r => r.json())
            .then(data => {
                const entries = (data.entries || []).reverse();
                if (entries.length === 0) {
                    document.getElementById('logTable').innerHTML = '<div class="empty">暂无配置变更记录</div>';
                    return;
                }
                let html = '<table><thead><tr><th>时间</th><th>操作</th><th>变更字段</th><th>来源</th></tr></thead><tbody>';
                entries.forEach(e => {
                    const cls = e.action.includes('reset') ? 'action-reset' : 'action-save';
                    const keys = (e.changed_keys || []).join(', ') || '—';
                    html += `<tr><td>${e.timestamp_local || e.timestamp}</td><td><span class="action-badge ${cls}">${e.action}</span></td><td>${keys}</td><td>${e.source}</td></tr>`;
                });
                html += '</tbody></table>';
                document.getElementById('logTable').innerHTML = html;
            });
    </script>
</body>
</html>
''')


if __name__ == "__main__":
    try:
        with open('config.json', 'r') as f:
            loaded_config = json.load(f)
            # 确保 proxy_pool 存在（仅在完全缺失或为空时补全默认国家，尊重用户显式保存的配置）
            if 'proxy_pool' not in loaded_config or not loaded_config.get('proxy_pool'):
                log.info("配置文件中的 proxy_pool 不完整，补全国家并保留已配置的代理凭据")
                # 按国家代码索引用户已配置的代理
                loaded_by_country = {
                    p.get('country_code'): p
                    for p in loaded_config.get('proxy_pool', [])
                    if isinstance(p, dict)
                }
                merged_pool = []
                default_countries = set()
                # 以默认池(全部国家)为骨架，命中用户配置则合并保留其凭据
                for default_proxy in config.get('proxy_pool', []):
                    cc = default_proxy.get('country_code')
                    default_countries.add(cc)
                    if cc in loaded_by_country:
                        merged_proxy = copy.deepcopy(default_proxy)
                        merged_proxy.update(copy.deepcopy(loaded_by_country[cc]))
                        merged_pool.append(merged_proxy)
                    else:
                        merged_pool.append(copy.deepcopy(default_proxy))
                # 追加默认池中没有、但用户额外配置的代理国家
                for cc, p in loaded_by_country.items():
                    if cc not in default_countries:
                        merged_pool.append(copy.deepcopy(p))
                loaded_config['proxy_pool'] = merged_pool
            # 对 web_navigation 做深合并，保留默认值中的新字段（loop_count/loop_interval/min_stay）
            def _merge_web_navigation(default_wn, loaded_wn):
                if not isinstance(loaded_wn, dict):
                    return default_wn
                merged = {}
                for k, v in default_wn.items():
                    if isinstance(v, dict) and isinstance(loaded_wn.get(k), dict):
                        merged[k] = {**v, **loaded_wn[k]}
                    else:
                        merged[k] = loaded_wn[k] if k in loaded_wn else v
                for k, v in loaded_wn.items():
                    if k not in merged:
                        merged[k] = v
                return merged
            if 'web_navigation' in loaded_config and isinstance(config.get('web_navigation'), dict):
                loaded_config['web_navigation'] = _merge_web_navigation(config['web_navigation'], loaded_config['web_navigation'])
            config.clear()
            config.update(deep_merge_defaults(DEFAULT_CONFIG, loaded_config))
        ensure_config_defaults()
        # === 安全：环境变量覆盖敏感配置（优先级：.env > config.json） ===
        _env_overrides = {
            "ip_proxy_api": "IP_PROXY_API",
            "ip_proxy_user": "IP_PROXY_USER",
            "ip_proxy_pwd": "IP_PROXY_PWD",
        }
        for cfg_key, env_key in _env_overrides.items():
            env_val = os.environ.get(env_key)
            if env_val:  # 环境变量存在且非空时覆盖
                config[cfg_key] = env_val
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        log.info("配置已加载")
    except Exception as e:
        log.info(f"未找到配置文件或加载失败，使用默认配置: {e}")
    
    # 加载历史任务
    load_historical_tasks()
    log.info(f"已加载 {len(historical_tasks)} 条历史任务记录")
    
    # 加载指纹统计
    load_fingerprint_stats()
    log.info(f"已加载指纹统计数据: {len(fingerprint_stats['ua_usage'])}个UA, {len(fingerprint_stats['fingerprint_usage'])}个指纹")
    
    # Selenium 使用系统已安装的 Chrome + Selenium Manager 自动管理 chromedriver，无需额外安装步骤
    log.info("使用 Selenium + 本地 Chrome 驱动")
    
   # app.run(host="0.0.0.0", port=5001, debug=False)


    import os
    # 优先读取环境变量，无参数默认5001
    port = int(os.getenv("RUN_PORT",5001))
    host = os.getenv("RUN_HOST","0.0.0.0")
    app.run(host=host,port=port)
