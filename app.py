from flask import Flask, render_template_string, request, jsonify
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
    return (not globals().get("task_running", True)) or (not globals().get("video_task_running", True))
_selenium_bridge.set_stop_check(_bridge_should_stop)

# 导入新创建的模块
import sys, os
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

MODEL_FUNCTIONS = {
    "normal": generate_normal_hours,
    "gamma": generate_gamma_hours,
    "poisson": generate_poisson_hours,
    "bimodal": generate_bimodal_hours
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
    except:
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
    - 检查 layer_1..layer_6 的 stay_ratio 不能全为 0
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

    # layer stay_ratio 检查
    ratio_sum = 0.0
    for li2 in range(1, 7):
        layer = wn.get(f"layer_{li2}", {}) if isinstance(wn.get(f"layer_{li2}", {}), dict) else {}
        r = layer.get("stay_ratio", 0)
        try:
            ratio_sum += float(r)
        except Exception:
            errors.append(f"layer_{li2}.stay_ratio 必须为数字")
    if ratio_sum <= 0 and not errors:
        errors.append("layer_1..layer_6 的 stay_ratio 之和必须 > 0")

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
        
        # 任务间隔
        task_gap = 0 if is_first else random.uniform(interval_cfg["min"], interval_cfg["max"])
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

# 全局变量
config = {
    # 网络Tab参数
    "ip_proxy_api": "",
    "ip_proxy_user": "",
    "ip_proxy_pwd": "",
    "vps_host": "127.0.0.1",
    "vps_port": 8888,
    "vps_new_port": 8888,
    "vps_socks5_port": 1080,
    "skip_browser_ip_check": False,
    "webrtc_leak_check_enabled": True,
    "qa_session_enabled": True,
    "session_mode": "country_host_7d",
    "ua_repeat_max_rate": 0.2,
    "vps_user": "admin",
    "vps_pass": "admin123",
    
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
    "adsl_task_count": 1,
    "vt_adsl_task_count": 1,
    "adsl_profile": "pppoe",
    "ip_provider_type": "proxy_api",
    "adsl_username": "",
    "adsl_password": "",
    "adsl_interface": "ppp0",
    "adsl_min_redial_interval": 30,
    "adsl_ip_blacklist_hours": 24,
    "adsl_ip_redial_max_attempts": 10,
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
    
    # 社媒引流配置
    "social_media": {
        "platform_region": "auto",
        "platforms": ["facebook", "twitter", "instagram"],
        "frequency": {"min": 10, "max": 30},
        "stay_time": {"min": 30, "max": 60},
        "interaction_prob": {"min": 0.1, "max": 0.3},
        "post_urls": []
    },
    
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
            {"id": "baidu", "name": "百度", "url": "https://www.baidu.com/s?wd=", "language": "zh"},
            {"id": "sogou", "name": "搜狗", "url": "https://www.sogou.com/web?query=", "language": "zh"},
            {"id": "so360", "name": "360搜索", "url": "https://www.so.com/s?q=", "language": "zh"},
            {"id": "google", "name": "谷歌", "url": "https://www.google.com/search?q=", "language": "en"},
            {"id": "bing", "name": "必应", "url": "https://www.bing.com/search?q=", "language": "en"}
        ],
        "region_engine_map": {
            "中国": ["baidu", "sogou", "so360"],
            "美国": ["google", "bing"]
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
        # 第5层 → 第6层（可选）
        "layer_5": {
            "keywords": [],
            "fallback_urls": [],
            "stay_ratio": 0.2,
            "min_stay": 10
        },
        # 第6层（最后一层）
        "layer_6": {
            "keywords": [],
            "fallback_urls": [],
            "stay_ratio": 0.1,
            "min_stay": 10
        },
        # 循环次数配置
        "loop_count": {"min": 1, "max": 3},
        # 每轮浏览的间隔时长（秒）
        "loop_interval": {"min": 1, "max": 5},
        # 返回链接关键字
        "back_links": ["返回", "back", "目录"],
        # 返回首页链接关键字
        "back_home_links": ["首页", "home"]
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
                        except:
                            pass
                else:
                    # 英文环境
                    for _ in range(30):
                        try:
                            ua = self.ua_generator.random
                            if ua and ua not in ua_pool:
                                ua_pool.append(ua)
                        except:
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
    视频任务专用的可中断 sleep：分片休眠，期间持续检查 video_task_running，
    支持「点击停止按钮后 1 秒内立即中断当前等待」。
    返回 True 表示完整睡完，False 表示被中断。
    """
    import time as _t
    global video_task_running
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if not video_task_running:
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
    在 duration 秒的时间窗内，穿插执行真人行为（鼠标移动/滚动/键盘/随机停顿）。
    动作之间随机间隔 0.5-3 秒，超时即停；内部每步都会打日志（直播效果）。
    任何动作异常都吞掉，不中断窗口。
    deadline: 绝对时间戳（time.time() 的秒）。若提供且早于 duration 自然结束时间，则提前停。
    返回更新后的 (current_x, current_y)。
    """
    import time as _t
    import random as _rnd

    # 补全 stats 默认字段（防止调用方漏初始化）
    for _k in ("mouse_moves", "scrolls", "scroll_distance", "clicks",
               "waits", "key_presses", "total_stay"):
        stats.setdefault(_k, 0)

    start = _t.time()
    duration = max(0.0, float(duration))
    window_end = start + duration
    if deadline is not None and deadline < window_end:
        log.info(
            f"[{page_name}] 🛡️ 保险绳提前触发："
            f"自然结束在 {duration:.0f}s，但 deadline 在 {(deadline-start):.0f}s，"
            f"按保险绳裁剪。"
        )
        window_end = float(deadline)

    log.info(
        f"[{page_name}] 🎭 真人模拟窗口启动: 时长 {duration:.1f}s，"
        f"动作随机（滚动/鼠标/暂停/键盘），间隔 0.5-3s（每约 8-12 秒输出一次摘要，单步动作仅 DEBUG）"
    )

    # 动作权重：滚动最常见，鼠标其次，停顿其次，键盘偶发
    actions = (
        ["scroll"] * 5 +
        ["mouse"] * 4 +
        ["pause"] * 3 +
        ["key"] * 1
    )

    scroll_cfg = config.get("scroll_pixels", {"min": 100, "max": 800})

    action_errors = 0
    loop_count = 0
    # 周期摘要：每次累计到一定动作数时打一条 INFO 摘要（而不是每步都打）
    summary_interval = 6  # 大约每 6 次动作打一条摘要
    next_summary_at = summary_interval

    while True:
        human_model_tick(page_name)
        if not task_running or not ensure_human_model_alive():
            break
        remaining = window_end - _t.time()
        if remaining <= 0:
            break
        # 长会话疲劳模拟：随窗口已进行时长，动作间隔逐渐变长（人累了变慢），上限 1.8x
        _elapsed_ratio = min(1.0, (_t.time() - start) / duration) if duration > 0 else 0.0
        _fatigue = 1.0 + 0.8 * _elapsed_ratio
        # 动作间隔（也算"真人感"）：0.5-3s × 疲劳系数，且不超出剩余窗口
        gap = min(remaining, _rnd.uniform(0.5, 3.0) * _fatigue)
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
                    dy = -dy  # 偶发回滚
                page.evaluate(f"window.scrollBy(0, {dy})")
                stats["scrolls"] += 1
                stats["scroll_distance"] += abs(dy)
                log.debug(f"[{page_name}] 🖱️ 滚动 {dy:+d}px（累计 {stats['scroll_distance']}px, {stats['scrolls']} 次）")
            elif action == "mouse":
                tx = _rnd.randint(100, 1100)
                ty = _rnd.randint(100, 700)
                steps = _rnd.randint(8, 18)
                for s in range(steps):
                    if _t.time() >= window_end:
                        break
                    tt = (s + 1) / steps
                    mx = int(current_x + (tx - current_x) * tt + _rnd.randint(-2, 2))
                    my = int(current_y + (ty - current_y) * tt + _rnd.randint(-2, 2))
                    page.mouse.move(mx, my)
                    _t.sleep(_rnd.uniform(0.01, 0.05))
                current_x, current_y = tx, ty
                stats["mouse_moves"] += 1
                log.debug(f"[{page_name}] 🎯 鼠标移动到 ({tx},{ty})（累计 {stats['mouse_moves']} 次）")
            elif action == "key":
                key = _rnd.choice(["PageDown", "PageUp", "ArrowDown", "ArrowUp", "End", "Home", "Space"])
                page.keyboard.press(key)
                stats["key_presses"] += 1
                log.debug(f"[{page_name}] ⌨️ 按键 {key}（累计 {stats['key_presses']} 次）")
            else:  # pause
                pause = min(window_end - _t.time(), _rnd.uniform(1.0, 4.0))
                if pause > 0:
                    _t.sleep(pause)
                    stats["total_stay"] += pause
                stats["waits"] += 1
                log.debug(f"[{page_name}] ⏸️ 停留 {pause:.1f}s（累计随机等待 {stats['waits']} 次）")
        except Exception:
            action_errors += 1

        # 周期摘要（INFO 级别）
        if loop_count >= next_summary_at:
            next_summary_at = loop_count + summary_interval
            elapsed = _t.time() - start
            log.info(
                f"[{page_name}] 🎭 真人模拟·实时摘要: 已进行 {elapsed:.1f}s，"
                f"滚动 {stats['scrolls']} 次({stats['scroll_distance']}px)，"
                f"鼠标 {stats['mouse_moves']} 次，"
                f"键盘 {stats['key_presses']} 次，随机等待 {stats['waits']} 次"
            )

    actual = _t.time() - start
    log.info(
        f"[{page_name}] 🎭 真人模拟窗口结束: 实耗 {actual:.1f}s / 计划 {duration:.1f}s，"
        f"动作循环 {loop_count} 次，鼠标 {stats['mouse_moves']} 次，"
        f"滚动 {stats['scrolls']} 次({stats['scroll_distance']}px)，"
        f"键盘 {stats['key_presses']} 次，随机等待 {stats['waits']} 次"
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
    执行完整英文谷歌/必应搜索跳转流程（带真人模拟）
    :param page: 浏览器页面
    :param target_url: 目标网址（要从搜索结果里找它）
    :param selected_engine_id: 引擎ID（google/bing）
    :param selected_keyword: 搜索关键词
    :param stats: 页面统计字典（给真人模拟用）
    :param current_x, current_y: 当前鼠标坐标
    :param config: 系统配置
    :return: (success, current_x, current_y)
    """
    from urllib.parse import urlparse
    import random
    import sys, os
    print("Current directory:", os.getcwd())
    print("sys.path:", sys.path)
    from seo_query_module import get_seo_query

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

        log.info(f"🔍 [真搜索] 访问搜索引擎主页: {homepage_url}")
        
        # 2. 访问搜索引擎主页
        page.goto(homepage_url, timeout=60000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))

        # 2.5 新增：在搜索引擎主页加入真人模拟窗口！
        homepage_duration = random.uniform(1.5, 4.0)
        log.info(f"🔍 [真搜索] 在引擎主页停留：{homepage_duration:.1f}s，真人模型介入")
        current_x, current_y = simulate_human_in_window(page, homepage_duration, stats, current_x, current_y, config, page_name=f"搜索引擎主页({selected_engine_id})")

        # 3. 处理隐私弹窗（Google/Bing 英文）
        log.info(f"🔍 [真搜索] 尝试处理隐私弹窗")
        if selected_engine_id == "google":
            # Google 英文隐私弹窗
            for selector in [
                'button[aria-label*="Accept all"]',
                'button:has-text("Accept all")',
                'button:has-text("Accept")',
                'div[id*="L2AGLb"] button',
                'button[id*="L2AGLb"]'
            ]:
                try:
                    btn = page.wait_for_selector(selector, timeout=5000)
                    if btn:
                        btn.click()
                        log.info(f"🔍 [真搜索] Google 隐私弹窗已同意")
                        time.sleep(random.uniform(1.0, 2.5))
                        break
                except Exception:
                    continue
        elif selected_engine_id == "bing":
            # Bing 英文隐私弹窗
            for selector in [
                'button[id*="bnp_btn_accept"]',
                'button:has-text("Accept")',
                'button[aria-label*="Accept"]'
            ]:
                try:
                    btn = page.wait_for_selector(selector, timeout=5000)
                    if btn:
                        btn.click()
                        log.info(f"🔍 [真搜索] Bing 隐私弹窗已同意")
                        time.sleep(random.uniform(1.0, 2.5))
                        break
                except Exception:
                    continue

        # 4. 定位搜索框
        log.info(f"🔍 [真搜索] 定位搜索框")
        search_selector = 'input[name="q"]'
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

        # 10. 找目标链接（带 data-ved 属性的自然搜索结果 a 标签）
        log.info(f"🔍 [真搜索] 查找目标链接: {target_url}")
        target_parsed = urlparse(target_url)
        target_host = target_parsed.netloc
        target_link_found = None

        # 遍历搜索结果中的 a 标签
        for selector in [
            'a[data-ved][href*="http"]',
            'a[data-ved]',
            'a[href*="' + target_host + '"]'
        ]:
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
    "video_view_count": 0,
    "video_item_success": 0,
    "video_item_fail": 0,
    "total_video_watch_time": 0,
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
video_plan = None
video_task_running = False
video_worker_active = False
current_video_task_idx = -1  # 当前正在执行的任务索引（-1表示无）
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
video_adsl_status = {
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
            flex: 0 0 67%; 
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
        
        /* 黄框 - 日志区域（右侧，约1/3宽度，与配置区等高） */
        .log-panel { 
            flex: 0 0 32%; 
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
                    <span class="status-label">视频任务:</span>
                    <span id="videoTopStatus" class="status stopped">已停止</span>
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
                <div class="status-item">
                    <span class="status-label">视频观看:</span>
                    <span class="stat-number" style="color: #ffc107;">{{ statsvideo_view_count }}次</span>
                </div>
                <div class="status-item">
                    <span class="status-label">视频时长:</span>
                    <span class="stat-number" style="color: #17a2b8;">{{ stats.total_video_watch_time }}s</span>
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
                <button class="tab-btn" onclick="switchTab('videotraffic', this)">视频流量</button>
                <button class="tab-btn" onclick="switchTab('socialmedia', this)">社媒引流</button>
                <button class="tab-btn" onclick="switchTab('network', this)">网络</button>
                <button class="tab-btn" onclick="switchTab('seo', this)">SEO</button>
                <button class="tab-btn" onclick="switchTab('model', this)">模型</button>
                <button class="tab-btn" onclick="switchTab('taskvalidation', this)">任务验证</button>
                <button class="tab-btn" onclick="switchTab('task', this)">QA任务</button>
            </div>
            
            <!-- QA任务Tab -->
            <div class="tab-content" id="tab-task">
                <div class="seo-panel" style="background:#1f2937;">
                    <h3 style="color:#0ea5e9; margin-top:0;">🧪 QA任务（综合QA / 拨号VPS）</h3>
                    <div style="display:flex; gap:10px; margin-bottom:15px;">
                        <button class="btn btn-blue" onclick="saveQaConfig()">保存配置</button>
                        <button class="btn btn-yellow" onclick="resetQaDefaults()">恢复默认</button>
                        <button class="btn" style="background:#0ea5e9;color:#fff;" onclick="startQaTaskFromQaTab()">▶️ QA执行</button>
                        <button class="btn" style="background:#ef4444;color:#fff;" onclick="stopQaTask()">⛔ QA停止</button>
                    </div>
                    <div class="form-group">
                        <label>QA次数</label>
                        <input type="number" id="qa_task_count_qa_tab" min="1" max="999" value="{{ config.get('vt_adsl_task_count', 1) }}" style="width:100%;">
                    </div>
                    <div class="form-group">
                        <label>执行阶段选择（勾选执行，取消跳过）</label>
                        <div style="display:flex; gap:20px; margin-top:8px;">
                            <label style="display:flex; align-items:center; gap:5px;">
                                <input type="checkbox" id="qa_run_website" {{ 'checked' if config.get('qa_run_phases', {}).get('website', True) else '' }}>
                                浏览网站
                            </label>
                            <label style="display:flex; align-items:center; gap:5px;">
                                <input type="checkbox" id="qa_run_video" {{ 'checked' if config.get('qa_run_phases', {}).get('video', True) else '' }}>
                                观看视频
                            </label>
                        </div>
                    </div>
                    <div class="form-group">
                        <label><input type="checkbox" id="qa_webrtc_leak_check_enabled" {{ 'checked' if config.get('webrtc_leak_check_enabled', True) else '' }}> 启用WebRTC防泄漏检测</label>
                    </div>
                    <div class="form-group">
                        <label>全局会话策略 session_mode</label>
                        <select id="qa_session_mode" style="width:100%;">
                            <option value="country_host_7d" {{ 'selected' if config.get('session_mode', 'country_host_7d') == 'country_host_7d' else '' }}>国家+Host复用7天</option>
                            <option value="new_each_task" {{ 'selected' if config.get('session_mode', 'country_host_7d') == 'new_each_task' else '' }}>每任务新会话</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>UA重复率阈值（0.10 - 0.20）</label>
                        <input type="number" step="0.01" min="0.05" max="0.5" id="qa_ua_repeat_max_rate" value="{{ config.get('ua_repeat_max_rate', 0.2) }}" style="width:100%;">
                    </div>
                    <div class="form-group">
                        <label>视频流量自定义Referer（为空则走通用referer）</label>
                        <input type="text" id="qa_vt_udis_referer" value="{{ config.get('vt_udis_referer', '') }}" placeholder="https://example1.com/, https://example2.com/" style="width:100%;">
                    </div>
                </div>
            </div>
            
            <!-- 社媒引流Tab -->
            <div class="tab-content" id="tab-socialmedia">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn btn-blue" onclick="saveSocialMediaConfig()">保存配置</button>
                    <button class="btn btn-yellow" onclick="resetSocialMediaConfig()">恢复默认</button>
                </div>
                <div class="seo-panel">
                    <h3 style="color: #4a9eff; margin-top: 0; margin-bottom: 20px;">📱 社交媒体引流配置</h3>
                    <div class="form-group">
                        <label>平台区域（语言分区，强制语言一致性）</label>
                        <div style="display: flex; gap: 20px;">
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="radio" name="social_platform_region" value="auto" {{ 'checked' if config.social_media.platform_region == 'auto' else '' }}> 自动（跟随IP语言）
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="radio" name="social_platform_region" value="western" {{ 'checked' if config.social_media.platform_region == 'western' else '' }}> 欧美社媒（强制英文）
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="radio" name="social_platform_region" value="chinese" {{ 'checked' if config.social_media.platform_region == 'chinese' else '' }}> 中文社媒（强制中文）
                            </label>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>社交媒体平台（多选）</label>
                        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_facebook" value="facebook"> Facebook
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_twitter" value="twitter"> Twitter
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_instagram" value="instagram"> Instagram
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_linkedin" value="linkedin"> LinkedIn
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_reddit" value="reddit"> Reddit
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px;">
                                <input type="checkbox" id="social_tiktok" value="tiktok"> TikTok
                            </label>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>引流频率（次/小时）</label>
                        <div class="input-group">
                            <input type="number" id="social_frequency_min" value="{{ config.social_media.frequency.min }}">
                            <input type="number" id="social_frequency_max" value="{{ config.social_media.frequency.max }}">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>停留时间（秒）</label>
                        <div class="input-group">
                            <input type="number" id="social_stay_min" value="{{ config.social_media.stay_time.min }}">
                            <input type="number" id="social_stay_max" value="{{ config.social_media.stay_time.max }}">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>互动概率（点赞/评论/分享）</label>
                        <div class="input-group">
                            <input type="number" step="0.01" id="social_interaction_min" value="{{ config.social_media.interaction_prob.min }}">
                            <input type="number" step="0.01" id="social_interaction_max" value="{{ config.social_media.interaction_prob.max }}">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>引流来源帖子URL（逗号分隔，用于构造Referer，按语言自动匹配）</label>
                        <textarea id="social_post_urls" placeholder="https://www.facebook.com/example/post1,https://www.facebook.com/example/post2" style="width: 100%; height: 60px;">{{ config.social_media.post_urls | join(', ') }}</textarea>
                    </div>
                </div>
            </div>
            
            <!-- 网络Tab -->
            <div class="tab-content" id="tab-network">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn btn-blue" onclick="saveNetworkConfig()">保存配置</button>
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
                
                <div style="margin-bottom: 15px; padding: 12px; background: #2a2a2a; border-radius: 8px;">
                    <h4 style="margin-top: 0; margin-bottom: 10px; color: #4a9eff;">IP获取方式</h4>
                    <div style="display: flex; gap: 15px; align-items: center;">
                        <label style="display: flex; align-items: center; gap: 5px;">
                            <input type="radio" name="ip_provider_type" value="proxy_api" {{ 'checked' if config.get('ip_provider_type', 'proxy_api') == 'proxy_api' else '' }} onchange="toggleIPProviderConfig()">
                            代理API接口
                        </label>
                        <label style="display: flex; align-items: center; gap: 5px;">
                            <input type="radio" name="ip_provider_type" value="adsl" {{ 'checked' if config.get('ip_provider_type') == 'adsl' else '' }} onchange="toggleIPProviderConfig()">
                            ADSL拨号
                        </label>
                    </div>
                    <div id="adsl-config-panel" style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #444; display: {{ 'block' if config.get('ip_provider_type') == 'adsl' else 'none' }};">
                        <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                            <div class="form-group" style="flex: 1; min-width: 180px;">
                                <label for="adsl_username">ADSL用户名</label>
                                <input type="text" id="adsl_username" value="{{ config.adsl_username|default('') }}" style="width: 100%;">
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 180px;">
                                <label for="adsl_password">ADSL密码</label>
                                <input type="password" id="adsl_password" value="{{ config.adsl_password|default('') }}" style="width: 100%;">
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 180px;">
                                <label for="adsl_interface">网络接口</label>
                                <input type="text" id="adsl_interface" value="{{ config.adsl_interface|default('ppp0') }}" style="width: 100%;">
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

                <h4 style="margin-top:0; color:#4a9eff;">VPS配置（不变）</h4>
                <div class="form-grid">
                    <div>
                        <div class="form-group">
                            <label for="vps_host">VPS-IP</label>
                            <input type="text" id="vps_host" value="{{ config.vps_host }}">
                        </div>
                        <div class="form-group">
                            <label for="vps_port">VPS-PORT</label>
                            <input type="number" id="vps_port" value="{{ config.vps_port }}">
                        </div>
                    </div>
                    <div>
                        <div class="form-group">
                            <label for="vps_user">VPS-User</label>
                            <input type="text" id="vps_user" value="{{ config.vps_user }}">
                        </div>
                        <div class="form-group">
                            <label for="vps_pass">VPS-Pwd</label>
                            <input type="password" id="vps_pass" value="{{ config.vps_pass }}">
                        </div>
                    </div>
                    <div>
                        <div class="form-group">
                            <label for="ip_proxy_api">【备用】旧单代理_api</label>
                            <input type="text" id="ip_proxy_api" value="{{ config.ip_proxy_api }}">
                        </div>
                        <div class="form-group">
                            <label for="ip_proxy_user">【备用】旧单代理_User</label>
                            <input type="text" id="ip_proxy_user" value="{{ config.ip_proxy_user }}">
                        </div>
                        <div class="form-group">
                            <label for="ip_proxy_pwd">【备用】旧单代理_pwd</label>
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
                        <div style="margin-top:4px; color:#a7f3d0;">ADSL任务状态：<b id="adslTaskStatus" style="color:#ffd54f;">停止</b>　ADSL完成（次）：<b id="adslCompletedCount" style="color:#00d4aa;">0</b></div>
                    </div>
                    <!-- 按钮区域 -->
                    <div style="display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap;">
                        <button class="btn" onclick="saveWebsiteTrafficConfig()" style="background: #3b82f6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">💾 保存配置</button>
                        <button class="btn" onclick="resetWebsiteTrafficConfig()" style="background: #ffc107; color: #1a1a1a; padding: 5.4px 14.4px; font-size: 12.6px;">🔄 恢复默认</button>
                        <button class="btn" id="btn-generate-plan" onclick="generatePlan()" style="background: #10b981; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📋 生成计划</button>
                        <button class="btn" id="btn-single-task" onclick="startSingleTask()" style="background: #06b6d4; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">⚡ 单独任务</button>
                        <button class="btn" id="btn-adsl-task" onclick="startAdslIpTask()" style="background: #14b8a6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📡 ADSL IP任务</button>
                        <button class="btn" id="btn-execute-plan" onclick="executePlan()" style="background: #8b5cf6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">▶️ 执行任务</button>
                        <button class="btn" id="btn-clear-plan" onclick="clearPlan()" style="background: #f97316; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">🗑️ 清除计划</button>
                        <button class="btn" onclick="stopTask()" style="background: #ef4444; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">⏹️ 停止任务</button>
                    </div>
                    <!-- 基础配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin: 0 0 8px 0; color: #4a9eff;">目标网站池（固定5个，勾选的串联浏览）</h4>
                        <div style="display: flex; flex-direction: column; gap: 6px;">
                            {% for i in range(1, 6) %}
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
                            <label for="adsl_task_count">ADSL任务数</label>
                            <input type="number" id="adsl_task_count" min="1" max="999" value="{{ config.adsl_task_count|default(1) }}" style="width: 100%;">
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
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer1_stay_ratio" value="{{ config.web_navigation.layer_1.stay_ratio }}" style="width: 100%;">
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
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer2_stay_ratio" value="{{ config.web_navigation.layer_2.stay_ratio }}" style="width: 100%;">
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
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer3_stay_ratio" value="{{ config.web_navigation.layer_3.stay_ratio }}" style="width: 100%;">
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
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer4_stay_ratio" value="{{ config.web_navigation.layer_4.stay_ratio }}" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第5层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #e67e22;">第5层 → 第6层（可选）</h4>
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
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer5_stay_ratio" value="{{ config.web_navigation.layer_5.stay_ratio }}" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 第6层配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #f39c12;">第6层（最后一层，可选）</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>链接关键字池（逗号分隔）</label>
                                <textarea id="webnav_layer6_keywords" placeholder="关键字1,关键字2,关键字3" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_6.keywords|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>兜底链接池（逗号分隔）</label>
                                <textarea id="webnav_layer6_fallback_urls" placeholder="https://url1,https://url2" style="width: 100%; min-height: 60px;">{{ config.web_navigation.layer_6.fallback_urls|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>停留时长比例（0-1）</label>
                                <input type="number" step="0.01" id="webnav_layer6_stay_ratio" value="{{ config.web_navigation.layer_6.stay_ratio }}" style="width: 100%;">
                            </div>
                        </div>
                    </div>
                    
                    <!-- 返回链接配置 -->
                    <div style="margin-bottom: 8px; padding: 8px; background: #1e1e1e; border-radius: 8px;">
                        <h4 style="margin: 0 0 6px 0; color: #3498db;">返回链接配置</h4>
                        <div style="display: flex; flex-direction: row; gap: 8px; align-items: flex-start;">
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>返回链接关键字（逗号分隔）</label>
                                <textarea id="webnav_back_links" placeholder="返回,back,目录" style="width: 100%; min-height: 60px;">{{ config.web_navigation.back_links|join(',') }}</textarea>
                            </div>
                            <div class="form-group" style="flex: 3; min-width: 0;">
                                <label>返回首页链接关键字（逗号分隔）</label>
                                <textarea id="webnav_back_home_links" placeholder="首页,home" style="width: 100%; min-height: 60px;">{{ config.web_navigation.back_home_links|join(',') }}</textarea>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- 视频流量配置Tab -->
            <div class="tab-content" id="tab-videotraffic">
                <div class="seo-panel">
                    <div style="padding: 8px 12px; margin-bottom: 12px; background:#16213e; border:1px solid #ff9900; border-radius:6px; color:#dbeafe; font-size:13px;">
                        <div style="color:#a7f3d0;">视频ADSL任务状态：<b id="videoAdslTaskStatus" style="color:#ffd54f;">停止</b>　ADSL完成（次）：<b id="videoAdslCompletedCount" style="color:#00d4aa;">0</b></div>
                        <div style="margin-top:4px; color:#fbbf24;">视频流量只走拨号VPS/ADSL，不使用代理API或普通直连。</div>
                    </div>

                    <!-- 按钮区域 -->
                    <div style="display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap;">
                        <button class="btn" onclick="saveVideoTrafficConfig()" style="background: #3b82f6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">💾 保存配置</button>
                        <button class="btn" onclick="resetVideoTrafficConfig()" style="background: #ffc107; color: #1a1a1a; padding: 5.4px 14.4px; font-size: 12.6px;">🔄 恢复默认</button>
                        <button class="btn" onclick="generateVideoPlan()" style="background: #10b981; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📋 生成计划</button>
                        <button class="btn" id="btn-start-video" onclick="startVideoTasks()" style="background: #8b5cf6; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">▶️ 执行视频ADSL任务</button>
                        <button class="btn" id="btn-stop-video" onclick="stopVideoTasks()" disabled style="background: #ef4444; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">⏹️ 停止任务</button>
                        <button class="btn" onclick="clearVideoPlan()" style="background: #f97316; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">🗑️ 清除计划</button>
                        <button class="btn" onclick="showVideoPlan()" style="background: #06b6d4; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📊 查看计划</button>
                        <button class="btn" onclick="showVideoStats()" style="background: #ec4899; color: white; padding: 5.4px 14.4px; font-size: 12.6px;">📈 任务统计</button>
                    </div>

                    <div class="form-group" style="display: grid; grid-template-columns: 220px 220px 1fr; gap: 12px; align-items: start;">
                        <div>
                            <label>入口模式</label>
                            <select id="vt_entry_mode" style="width: 100%;">
                                <option value="auto" {{ 'selected' if config.get('vt_entry_mode', 'auto') == 'auto' else '' }}>自动识别</option>
                                <option value="direct" {{ 'selected' if config.get('vt_entry_mode', 'auto') == 'direct' else '' }}>视频直链</option>
                                <option value="layer" {{ 'selected' if config.get('vt_entry_mode', 'auto') == 'layer' else '' }}>Layer导航</option>
                            </select>
                            <div style="font-size:11px; color:#9ca3af; margin-top:4px; line-height:1.4;">
                                自动：有直链走直链，否则走Layer<br>
                                直链：跳过Layer1/Layer2<br>
                                Layer：强制入口页→Layer2→视频
                            </div>
                        </div>
                        <div>
                            <label>Layer2视频模式</label>
                            <select id="vt_layer2_video_mode" style="width: 100%;">
                                <option value="auto" {{ 'selected' if config.get('vt_layer2_video_mode', 'auto') == 'auto' else '' }}>自动识别</option>
                                <option value="link" {{ 'selected' if config.get('vt_layer2_video_mode', 'auto') == 'link' else '' }}>链接跳转</option>
                                <option value="iframe" {{ 'selected' if config.get('vt_layer2_video_mode', 'auto') == 'iframe' else '' }}>iframe嵌入</option>
                            </select>
                            <div style="font-size:11px; color:#9ca3af; margin-top:4px; line-height:1.4;">
                                自动：优先嵌入播放器，找不到再跳转<br>
                                链接：按Layer2关键词/兜底跳转<br>
                                iframe：只在Layer2当前页观看
                            </div>
                        </div>
                        <div>
                            <label>视频入口URL池（为空时使用网站流量目标URL；多个URL支持英文逗号或换行）</label>
                            <textarea id="vt_video_urls" placeholder="https://入口1.com\nhttps://入口2.com" style="width: 100%; min-height: 80px;">{{ config.get('vt_video_urls', '') }}</textarea>
                        </div>
                    </div>

                    <div class="form-group" style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <div>
                            <label>Layer1关键词池（支持英文逗号或换行）</label>
                            <textarea id="vt_layer1_keywords" placeholder="关键词1,关键词2" style="width: 100%; min-height: 70px;">{{ config.get('vt_layer1_keywords', [])|join('\n') }}</textarea>
                        </div>
                        <div>
                            <label>Layer2关键词池（支持英文逗号或换行）</label>
                            <textarea id="vt_layer2_keywords" placeholder="关键词1,关键词2" style="width: 100%; min-height: 70px;">{{ config.get('vt_layer2_keywords', [])|join('\n') }}</textarea>
                        </div>
                        <div>
                            <label>Layer1兜底链接池（视频专用优先；为空复用网站Layer1）</label>
                            <textarea id="vt_layer1_fallback_urls" placeholder="https://url1\nhttps://url2" style="width: 100%; min-height: 70px;">{{ config.get('vt_layer1_fallback_urls', [])|join('\n') }}</textarea>
                        </div>
                        <div>
                            <label>Layer2兜底链接池（视频专用优先；为空复用网站Layer2）</label>
                            <textarea id="vt_layer2_fallback_urls" placeholder="https://url1\nhttps://url2" style="width: 100%; min-height: 70px;">{{ config.get('vt_layer2_fallback_urls', [])|join('\n') }}</textarea>
                        </div>
                    </div>

                    <!-- 视频任务基本配置（合并一行） -->
                    <div class="form-group" style="display: flex; gap: 15px;">
                        <div style="flex: 1;">
                            <label>单次任务观看视频数量（个）</label>
                            <input type="number" id="vt_watch_count" value="{{ config.get('vt_watch_count', 3) }}" min="1" style="width: 100%;">
                        </div>
                        <div style="flex: 1;">
                            <label>任务计划天数（天）</label>
                            <input type="number" id="vt_task_days" value="{{ config.get('vt_task_days', 1) }}" min="0" style="width: 100%;" title="0表示无限循环">
                        </div>
                        <div style="flex: 1;">
                            <label>ADSL任务数</label>
                            <input type="number" id="vt_adsl_task_count" value="{{ config.get('vt_adsl_task_count', 1) }}" min="1" max="999" style="width: 100%;">
                        </div>
                        <div style="flex: 1.5;">
                            <label>查看时长（秒）</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" id="vt_duration_min" value="{{ config.get('vt_duration_min', 30) }}" min="1" placeholder="最短">
                                <input type="number" id="vt_duration_max" value="{{ config.get('vt_duration_max', 120) }}" min="1" placeholder="最长">
                            </div>
                        </div>
                    </div>

                    <!-- 视频倍速 -->
                    <div class="form-group" style="display: flex; gap: 20px;">
                        <div style="flex: 1;">
                            <label>视频倍速（倍）</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" step="0.1" id="vt_speed_min" value="{{ config.get('vt_speed_min', 1) }}" min="1" max="3" placeholder="最低倍速1">
                                <input type="number" step="0.1" id="vt_speed_max" value="{{ config.get('vt_speed_max', 2) }}" min="1" max="3" placeholder="最高倍速3">
                            </div>
                        </div>
                        <div style="flex: 1;">
                            <label>视频间隔（秒）</label>
                            <div class="input-group" style="width: 100%;">
                                <input type="number" step="0.1" id="vt_interval_min" value="{{ config.get('vt_interval_min', 5) }}" min="0" placeholder="最短等待">
                                <input type="number" step="0.1" id="vt_interval_max" value="{{ config.get('vt_interval_max', 15) }}" min="0" placeholder="最长等待">
                            </div>
                        </div>
                    </div>

                    <div class="form-group">
                        <label>视频Referer列表（多个用英文逗号分隔，按任务序号轮询选择）</label>
                        <input type="text" id="vt_udis_referer" value="{{ config.get('vt_udis_referer', 'https://udisxxx.com/') }}" placeholder="https://freestoryweb.com/,https://example.com/" style="width: 100%;">
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

                    <!-- 第一组：搜索引擎管理 -->
                    <div style="margin-bottom: 20px; padding: 2px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">1. 搜索引擎管理</h4>
                        <div id="engines-container">
                            {% for engine in config.seo.search_engines %}
                            <div class="engine-item" style="display: flex; gap: 10px; margin-bottom: 10px; align-items: center;">
                                <input type="text" class="engine-id" placeholder="引擎ID" value="{{ engine.id }}" style="flex: 1;">
                                <input type="text" class="engine-name" placeholder="引擎名称" value="{{ engine.name }}" style="flex: 1;">
                                <input type="text" class="engine-url" placeholder="搜索URL" value="{{ engine.url }}" style="flex: 2;">
                                <select class="engine-lang" style="width: 100px;">
                                    <option value="zh" {{ 'selected' if engine.language == 'zh' else '' }}>中文</option>
                                    <option value="en" {{ 'selected' if engine.language == 'en' else '' }}>英文</option>
                                </select>
                                <button class="btn btn-red" onclick="removeEngine(this)" style="padding: 5px 10px;">删除</button>
                            </div>
                            {% endfor %}
                        </div>
                        <button class="btn btn-green" onclick="addEngine()" style="margin-top: 10px;">+ 添加搜索引擎</button>
                    </div>

                    <!-- 第二组：地域-搜索引擎映射 -->
                    <div style="margin-bottom: 20px; padding: 2px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">2. 地域-搜索引擎映射</h4>
                        <div style="display: flex; gap: 10px; flex-wrap: nowrap;">
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>中国-搜索引擎（ID，逗号分隔）</label>
                                <input type="text" id="seo_region_china" value="{{ config.seo.region_engine_map.中国 | join(', ') }}" style="height: 56px; font-size: 14px;">
                            </div>
                            <div class="form-group" style="flex: 1; min-width: 0;">
                                <label>美国-搜索引擎（ID，逗号分隔）</label>
                                <input type="text" id="seo_region_usa" value="{{ config.seo.region_engine_map.美国 | join(', ') }}" style="height: 56px; font-size: 14px;">
                            </div>
                        </div>
                    </div>

                    <!-- 第三组：关键词池 -->
                    <div style="margin-bottom: 20px; padding: 2px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">3. 关键词池</h4>
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
                    <div style="margin-bottom: 20px; padding: 2px; background: #2a2a2a; border-radius: 8px;">
                        <h4 style="margin-top: 0; color: #4a9eff;">4. Referer模式</h4>
                        <div class="form-group">
                            <div style="display: flex; gap: 20px;">
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="radio" id="seo_referer_dynamic" {{ 'checked' if config.seo.referer_mode == 'dynamic' else '' }} name="referer-mode">
                                    动态模式
                                </label>
                                <label style="display: flex; align-items: center; gap: 5px;">
                                    <input type="radio" id="seo_referer_static" {{ 'checked' if config.seo.referer_mode == 'static' else '' }} name="referer-mode">
                                    静态模式
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
                    vps_host: document.getElementById('vps_host').value,
                    vps_port: parseInt(document.getElementById('vps_port').value),
                    vps_user: document.getElementById('vps_user').value,
                    vps_pass: document.getElementById('vps_pass').value,
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
                    adsl_task_count: Math.min(999, Math.max(1, parseInt(document.getElementById('adsl_task_count').value) || 1)),
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
                    mouse_move_steps: {
                        min: parseInt(document.getElementById('mouse_move_steps_min').value),
                        max: parseInt(document.getElementById('mouse_move_steps_max').value)
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
                            stay_ratio: parseFloat(document.getElementById('webnav_layer1_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        layer_2: {
                            keywords: parseCommaList(document.getElementById('webnav_layer2_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer2_fallback_urls').value),
                            stay_ratio: parseFloat(document.getElementById('webnav_layer2_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        layer_3: {
                            keywords: parseCommaList(document.getElementById('webnav_layer3_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer3_fallback_urls').value),
                            stay_ratio: parseFloat(document.getElementById('webnav_layer3_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        layer_4: {
                            keywords: parseCommaList(document.getElementById('webnav_layer4_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer4_fallback_urls').value),
                            stay_ratio: parseFloat(document.getElementById('webnav_layer4_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        layer_5: {
                            keywords: parseCommaList(document.getElementById('webnav_layer5_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer5_fallback_urls').value),
                            stay_ratio: parseFloat(document.getElementById('webnav_layer5_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        layer_6: {
                            keywords: parseCommaList(document.getElementById('webnav_layer6_keywords').value),
                            fallback_urls: parseCommaList(document.getElementById('webnav_layer6_fallback_urls').value),
                            stay_ratio: parseFloat(document.getElementById('webnav_layer6_stay_ratio').value) || 0,
                            min_stay: 10
                        },
                        back_links: parseCommaList(document.getElementById('webnav_back_links').value),
                        back_home_links: parseCommaList(document.getElementById('webnav_back_home_links').value)
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

            // 收集代理池配置
            const proxyPoolItems = document.querySelectorAll('#proxy-pool-container .proxy-item');
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

            // 收集目标网站池（5个URL）
            const targetUrls = [];
            for (let i = 1; i <= 5; i++) {
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
                adsl_task_count: Math.min(999, Math.max(1, parseInt(document.getElementById('adsl_task_count').value) || 1)),
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
                        stay_ratio: parseFloat(document.getElementById('webnav_layer1_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    layer_2: {
                        keywords: parseCommaList(document.getElementById('webnav_layer2_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer2_fallback_urls').value),
                        stay_ratio: parseFloat(document.getElementById('webnav_layer2_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    layer_3: {
                        keywords: parseCommaList(document.getElementById('webnav_layer3_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer3_fallback_urls').value),
                        stay_ratio: parseFloat(document.getElementById('webnav_layer3_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    layer_4: {
                        keywords: parseCommaList(document.getElementById('webnav_layer4_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer4_fallback_urls').value),
                        stay_ratio: parseFloat(document.getElementById('webnav_layer4_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    layer_5: {
                        keywords: parseCommaList(document.getElementById('webnav_layer5_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer5_fallback_urls').value),
                        stay_ratio: parseFloat(document.getElementById('webnav_layer5_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    layer_6: {
                        keywords: parseCommaList(document.getElementById('webnav_layer6_keywords').value),
                        fallback_urls: parseCommaList(document.getElementById('webnav_layer6_fallback_urls').value),
                        stay_ratio: parseFloat(document.getElementById('webnav_layer6_stay_ratio').value) || 0,
                        min_stay: 10
                    },
                    back_links: parseCommaList(document.getElementById('webnav_back_links').value),
                    back_home_links: parseCommaList(document.getElementById('webnav_back_home_links').value)
                }
            };
            if (videoAdEl) {
                payload.video_ad_enabled_only = videoAdEl.checked;
            }
            return payload;
        }

        // 渲染计划预览
        function renderPlan(plan) {
            if (!plan || !plan.tasks) {
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
                'poisson': '泊松分布(秒级脉冲)'
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
                const todayUTC = new Date();
                todayUTC.setUTCHours(0,0,0,0);
                const d = new Date(todayUTC.getTime() + utcSecToday * 1000);
                
                // 直接获取本地时区的时间
                const h = d.getHours();
                const m = d.getMinutes();
                const s = d.getSeconds();
                
                return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
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
            const payload = collectConfigPayload();
            // 先保存配置，再生成计划
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(() => {
                return fetch('/generate_plan', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status === 'ok') {
                    renderPlan(result.plan);
                    alert('✅ 计划已生成，请在右侧查看，确认无误后点击"执行任务"');
                } else {
                    alert('❌ 计划生成失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }

        // 执行任务
        function executePlan() {
            if (!confirm('确定要开始执行任务吗？')) return;
            fetch('/start_task', {method: 'POST'}).then(() => location.reload());
        }

        // 单独任务：保存当前配置后，立即执行 1 个任务，不生成/消费计划
        function startSingleTask() {
            if (!confirm('确定要立即执行一个单独网站任务吗？不会生成或使用计划。')) return;
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

        // ADSL IP任务：保存当前配置后，按 ADSL任务数循环执行网站任务
        function startAdslIpTask() {
            const count = Math.min(999, Math.max(1, parseInt(document.getElementById('adsl_task_count').value) || 1));
            if (!confirm(`确定要执行 ADSL IP任务 ${count} 次吗？每轮会在本机执行 poff/pon 重拨，并可用“停止任务”中止。`)) return;
            const payload = collectConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.status === 'error' || result.success === false) {
                    throw new Error(result.message || '配置保存失败');
                }
                return fetch('/start_adsl_ip_task', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status !== 'ok') {
                    throw new Error(result.message || '启动失败');
                }
                location.reload();
            }).catch(err => {
                alert('❌ ADSL IP任务启动失败: ' + err.message);
            });
        }

        // 综合QA：保存网站+视频配置后，在一条任务线内串行执行网站浏览/广告检测/视频检测
        function startUnifiedQaTask(adsl) {
            adsl = true;
            const label = 'QA任务（ADSL）';
            if (!confirm(`确定要执行${label}吗？将按一条线执行网站浏览QA、广告曝光检测、视频观看检测。`)) return;
            const payload = Object.assign({}, collectConfigPayload(), collectVideoConfigPayload());
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.status === 'error' || result.success === false) {
                    throw new Error(result.message || '配置保存失败');
                }
                return fetch(adsl ? '/start_unified_adsl_qa_task' : '/start_unified_qa_task', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status !== 'ok' && result.success === false) {
                    throw new Error(result.message || '启动失败');
                }
                location.reload();
            }).catch(err => {
                alert('❌ ' + label + '启动失败: ' + err.message);
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

        // 页面加载时检查是否已有计划
        document.addEventListener('DOMContentLoaded', function() {
            fetch('/get_plan').then(r => r.json()).then(data => {
                if (data.plan) {
                    renderPlan(data.plan);
                }
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
                for (let i = 0; i < overflow; i++) {
                    lines[i].remove();
                }
            }
        }

        function saveQaConfig() {
            const data = {
                vt_adsl_task_count: Math.min(999, Math.max(1, parseInt(document.getElementById('qa_task_count_qa_tab').value) || 1)),
                qa_run_phases: {
                    website: document.getElementById('qa_run_website').checked,
                    video: document.getElementById('qa_run_video').checked
                },
                webrtc_leak_check_enabled: document.getElementById('qa_webrtc_leak_check_enabled').checked,
                session_mode: document.getElementById('qa_session_mode').value || 'country_host_7d',
                ua_repeat_max_rate: Math.max(0.05, Math.min(0.5, parseFloat(document.getElementById('qa_ua_repeat_max_rate').value) || 0.2)),
                vt_udis_referer: document.getElementById('qa_vt_udis_referer').value || ''
            };
            fetch('/save_config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
                .then(r => r.json()).then(j => alert(j.success ? 'QA配置已保存' : ('保存失败: ' + (j.message || '未知错误'))))
                .catch(e => alert('保存失败: ' + e));
        }

        function resetQaDefaults() {
            if (!confirm('确定恢复QA任务页参数为默认值？')) return;
            fetch('/reset_config_defaults', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({scope: 'qa'})})
                .then(r => r.json()).then(j => { alert(j.success ? '已恢复默认，将刷新页面' : ('失败: ' + (j.message || ''))); if (j.success) location.reload(); });
        }

        function startQaTaskFromQaTab() {
            const cnt = Math.min(999, Math.max(1, parseInt(document.getElementById('qa_task_count_qa_tab').value) || 1));
            saveQaConfig();
            if (!confirm('确定执行 QA任务（ADSL综合QA） ' + cnt + ' 次？')) return;
            fetch('/start_unified_adsl_qa_task', {method: 'POST'})
                .then(r => r.json()).then(j => alert(j.success ? 'QA任务已启动' : ('启动失败: ' + (j.message || ''))));
        }

        function stopQaTask() {
            if (!confirm('确定停止QA任务吗？')) return;
            fetch('/stop_video_tasks', {method: 'POST'})
            .then(r => r.json())
            .then(data => alert(data.message || '✅ 已发送停止信号'))
            .catch(err => alert('❌ 请求失败: ' + err));
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
                        logBox.scrollTop = logBox.scrollHeight;
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
                
                // 更新视频任务状态（显示当前执行的任务）
                fetch('/get_video_task_status').then(r => r.json()).then(videoStatus => {
                    const currentTaskPanel = document.getElementById('currentTaskPanel');
                    const planPreviewPanel = document.getElementById('planPreviewPanel');
                    const videoTopStatus = document.getElementById('videoTopStatus');
                    if (videoTopStatus) {
                        videoTopStatus.className = 'status ' + (videoStatus.running ? 'running' : 'stopped');
                        videoTopStatus.textContent = videoStatus.running ? '运行中' : '已停止';
                    }
                    const videoConfigStatus = document.getElementById('videoConfigStatus');
                    if (videoConfigStatus) {
                        videoConfigStatus.textContent = videoStatus.running ? '运行中' : '已停止';
                        videoConfigStatus.style.color = videoStatus.running ? '#00d4aa' : '#ffd54f';
                    }
                    const btnStartVideo = document.getElementById('btn-start-video');
                    const btnVideoAdsl = document.getElementById('btn-video-adsl-task');
                    const btnUnifiedQa = document.getElementById('btn-unified-qa');
                    const btnUnifiedAdslQa = document.getElementById('btn-unified-adsl-qa');
                    const btnStopVideo = document.getElementById('btn-stop-video');
                    if (btnStartVideo) btnStartVideo.disabled = !!videoStatus.running;
                    if (btnVideoAdsl) btnVideoAdsl.disabled = !!videoStatus.running;
                    if (btnUnifiedQa) btnUnifiedQa.disabled = !!videoStatus.running;
                    if (btnUnifiedAdslQa) btnUnifiedAdslQa.disabled = !!videoStatus.running;
                    if (btnStopVideo) btnStopVideo.disabled = !videoStatus.running;
                    if (videoStatus.adsl) {
                        const videoAdslStatusEl = document.getElementById('videoAdslTaskStatus');
                        const videoAdslCompletedEl = document.getElementById('videoAdslCompletedCount');
                        if (videoAdslStatusEl) {
                            videoAdslStatusEl.textContent = `${videoStatus.adsl.status || '停止'} ${videoStatus.adsl.current || 0}/${videoStatus.adsl.total || 0}`;
                        }
                        if (videoAdslCompletedEl) {
                            videoAdslCompletedEl.textContent = videoStatus.adsl.completed || 0;
                        }
                    }
                    
                    if (videoStatus.running && videoStatus.current_task) {
                        // 任务运行中：显示当前任务面板和精简版计划面板
                        currentTaskPanel.style.display = 'block';
                        planPreviewPanel.style.display = 'block';
                        
                        // 更新当前任务信息
                        const task = videoStatus.current_task;
                        const progress = `第 ${task.idx}/${videoStatus.total_tasks} 个任务`;
                        document.getElementById('currentTaskProgress').textContent = progress;
                        
                        let statusColor = '#aaa';
                        let statusText = task.status || '执行中';
                        if (task.status === '已完成') {
                            statusColor = '#00d4aa';
                        } else if (task.status === '失败') {
                            statusColor = '#ff5555';
                        }
                        
                        const taskInfo = `
                            <div style="display:grid; grid-template-columns: 80px 1fr; gap:8px;">
                                <span style="color:#888;">序号:</span>
                                <span style="color:#00d4aa; font-weight:bold;">${task.idx}</span>
                                <span style="color:#888;">计划时间:</span>
                                <span>${task.plan_time || '-'}</span>
                                <span style="color:#888;">代理国家:</span>
                                <span>${task.proxy_country || '-'}</span>
                                <span style="color:#888;">开始时间:</span>
                                <span>${task.start_time || '-'}</span>
                                <span style="color:#888;">完成状态:</span>
                                <span style="color:${statusColor}; font-weight:bold;">${statusText}</span>
                            </div>
                        `;
                        document.getElementById('currentTaskInfo').innerHTML = taskInfo;
                        
                        // 更新计划面板：只显示当前执行的任务
                        updatePlanPanelWithCurrentTask(videoStatus);
                    } else {
                        // 任务未运行：隐藏当前任务面板，保持计划面板不变
                        currentTaskPanel.style.display = 'none';
                    }
                });
                
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
        
        function toggleIPProviderConfig() {
            const providerType = document.querySelector('input[name="ip_provider_type"]:checked').value;
            const adslPanel = document.getElementById('adsl-config-panel');
            const proxyPanel = document.getElementById('proxy-pool-container');
            
            if (providerType === 'adsl') {
                adslPanel.style.display = 'block';
                proxyPanel.style.display = 'none';
            } else {
                adslPanel.style.display = 'none';
                proxyPanel.style.display = 'flex';
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
        
        // 视频流量配置
        function saveVideoTrafficConfig() {
            const payload = collectVideoConfigPayload();

            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.success) {
                    alert('✅ 视频流量配置已保存');
                } else {
                    alert('❌ 保存失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }

        function resetVideoTrafficConfig() {
            resetDefaults('video');
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
                    if (d.report) {
                        const rc = d.report;
                        const reasons = (rc.risk_reason_list || []).map(x => '<li>' + x + '</li>').join('') || '<li>无明显风险项</li>';
                        document.getElementById('drillResult').style.display = 'block';
                        document.getElementById('drillResult').innerHTML =
                            '<div style="font-size:16px;font-weight:bold;">风险分: ' + (rc.total_score ?? '-') + ' ｜ ' + (rc.risk_level || '') + '</div>' +
                            '<div style="margin-top:8px;color:#93c5fd;">风险命中项:</div><ul>' + reasons + '</ul>' +
                            (d.html_path ? '<div style="color:#94a3b8;margin-top:6px;">报告已保存: ' + d.html_path + '</div>' : '');
                    }
                }
            }).catch(() => {});
        }
        
        // 网络配置
        function saveNetworkConfig() {
            alert('网络配置已保存');
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
        
        // 社媒引流配置
        function saveSocialMediaConfig() {
            // 收集选中的平台
            const platforms = [];
            if (document.getElementById('social_facebook').checked) platforms.push('facebook');
            if (document.getElementById('social_twitter').checked) platforms.push('twitter');
            if (document.getElementById('social_instagram').checked) platforms.push('instagram');
            if (document.getElementById('social_linkedin').checked) platforms.push('linkedin');
            if (document.getElementById('social_reddit').checked) platforms.push('reddit');
            if (document.getElementById('social_tiktok').checked) platforms.push('tiktok');
            
            // 收集其他配置
            // 解析帖子URL列表
            const postUrlsText = document.getElementById('social_post_urls').value || '';
            const postUrls = postUrlsText.split(',')
                .map(url => url.trim())
                .filter(url => url);
                
            const data = {
                social_media: {
                    platform_region: (document.querySelector('input[name="social_platform_region"]:checked') || {value: 'auto'}).value,
                    platforms: platforms,
                    frequency: {
                        min: parseInt(document.getElementById('social_frequency_min').value),
                        max: parseInt(document.getElementById('social_frequency_max').value)
                    },
                    stay_time: {
                        min: parseInt(document.getElementById('social_stay_min').value),
                        max: parseInt(document.getElementById('social_stay_max').value)
                    },
                    interaction_prob: {
                        min: parseFloat(document.getElementById('social_interaction_min').value),
                        max: parseFloat(document.getElementById('social_interaction_max').value)
                    },
                    post_urls: postUrls
                }
            };
            
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
                if (result.success) {
                    alert('社媒引流配置已保存');
                    loadConfig(); // 重新加载配置以更新显示
                } else {
                    alert('保存失败: ' + result.message);
                }
            })
            .catch(error => {
                console.error('保存配置时发生错误:', error);
                alert('保存配置时发生错误');
            });
        }
        
        function resetSocialMediaConfig() {
            resetDefaults('social_media');
        }
        
        function saveSeoConfig() {
            // 收集搜索引擎数据
            const engineItems = document.querySelectorAll('.engine-item');
            const searchEngines = [];
            engineItems.forEach(item => {
                const id = item.querySelector('.engine-id').value.trim();
                const name = item.querySelector('.engine-name').value.trim();
                const url = item.querySelector('.engine-url').value.trim();
                const language = item.querySelector('.engine-lang').value;
                if (id && name && url) {
                    searchEngines.push({ id, name, url, language });
                }
            });
            
            // 收集其他配置
            const data = {
                search_engines: searchEngines,
                seo_region_china: document.getElementById('seo_region_china').value,
                seo_region_usa: document.getElementById('seo_region_usa').value,
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
        
        // ==================== 视频任务控制函数 ====================
        // 生成视频任务计划
        function generateVideoPlan() {
            const payload = collectVideoConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(() => {
                return fetch('/generate_video_plan', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status === 'ok') {
                    renderVideoPlan(result.plan);
                    alert('✅ 视频计划已生成，请在右侧查看，确认无误后点击"执行任务"');
                } else {
                    alert('❌ 视频计划生成失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        // 收集视频配置参数
        function collectVideoConfigPayload() {
            return {
                vt_video_urls: document.getElementById('vt_video_urls').value,
                vt_entry_mode: document.getElementById('vt_entry_mode') ? document.getElementById('vt_entry_mode').value : 'auto',
                vt_layer2_video_mode: document.getElementById('vt_layer2_video_mode') ? document.getElementById('vt_layer2_video_mode').value : 'auto',
                vt_layer1_keywords: parseCommaList(document.getElementById('vt_layer1_keywords').value),
                vt_layer2_keywords: parseCommaList(document.getElementById('vt_layer2_keywords').value),
                vt_layer1_fallback_urls: parseCommaList(document.getElementById('vt_layer1_fallback_urls').value),
                vt_layer2_fallback_urls: parseCommaList(document.getElementById('vt_layer2_fallback_urls').value),
                vt_watch_count: parseInt(document.getElementById('vt_watch_count').value),
                vt_task_days: parseInt(document.getElementById('vt_task_days').value),
                vt_adsl_task_count: Math.min(999, Math.max(1, parseInt((document.getElementById('qa_task_count') || document.getElementById('vt_adsl_task_count')).value) || 1)),
                vt_duration_min: parseInt(document.getElementById('vt_duration_min').value),
                vt_duration_max: parseInt(document.getElementById('vt_duration_max').value),
                vt_speed_min: parseFloat(document.getElementById('vt_speed_min').value),
                vt_speed_max: parseFloat(document.getElementById('vt_speed_max').value),
                vt_interval_min: parseFloat(document.getElementById('vt_interval_min').value),
                vt_interval_max: parseFloat(document.getElementById('vt_interval_max').value),
                vt_udis_referer: document.getElementById('vt_udis_referer').value,
                qa_human_profile: (document.querySelector('input[name="qa_human_profile"]:checked') || {}).value || 'standard'
            };
        }
        
        // 启动视频 ADSL IP任务：保存配置后，按 ADSL任务数循环执行视频任务
        function startVideoAdslIpTask() {
            const count = Math.min(999, Math.max(1, parseInt(document.getElementById('vt_adsl_task_count').value) || 1));
            if (!confirm(`确定要执行视频 ADSL IP任务 ${count} 次吗？每轮会在本机执行 poff/pon 重拨，并可用视频停止任务中止。`)) return;
            const payload = collectVideoConfigPayload();
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(result => {
                if (result.status === 'error' || result.success === false) {
                    throw new Error(result.message || '配置保存失败');
                }
                return fetch('/start_video_adsl_ip_task', {method: 'POST'});
            }).then(r => r.json()).then(result => {
                if (result.status !== 'ok') {
                    throw new Error(result.message || '启动失败');
                }
                location.reload();
            }).catch(err => {
                alert('❌ 视频 ADSL IP任务启动失败: ' + err.message);
            });
        }

        // 执行视频任务
        function startVideoTasks() {
            if (!confirm('确定要开始执行视频任务吗？')) return;
            document.getElementById('btn-start-video').disabled = true;
            document.getElementById('btn-stop-video').disabled = false;
            
            fetch('/start_video_tasks', {method: 'POST'})
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    checkVideoTaskStatus();
                } else {
                    alert('❌ 启动失败: ' + data.message);
                    document.getElementById('btn-start-video').disabled = false;
                    document.getElementById('btn-stop-video').disabled = true;
                }
            })
            .catch(err => {
                alert('❌ 请求失败: ' + err);
                document.getElementById('btn-start-video').disabled = false;
                document.getElementById('btn-stop-video').disabled = true;
            });
        }
        
        // 停止视频任务
        function stopVideoTasks() {
            if (!confirm('确定要停止视频任务吗？')) return;
            fetch('/stop_video_tasks', {method: 'POST'})
            .then(r => r.json())
            .then(data => {
                document.getElementById('btn-stop-video').disabled = true;
                document.getElementById('btn-start-video').disabled = false;
                const adslBtn = document.getElementById('btn-video-adsl-task');
                if (adslBtn) adslBtn.disabled = false;
                const unifiedBtn = document.getElementById('btn-unified-qa');
                const unifiedAdslBtn = document.getElementById('btn-unified-adsl-qa');
                if (unifiedBtn) unifiedBtn.disabled = false;
                if (unifiedAdslBtn) unifiedAdslBtn.disabled = false;
                restoreFullPlanDisplay();
                alert(data.message || '✅ 已发送停止信号，当前重拨/浏览器清理会尽快中断');
            })
            .catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        // 清除视频计划
        function clearVideoPlan() {
            if (!confirm('确定要清除视频任务计划吗？')) return;
            fetch('/clear_video_plan', {method: 'POST'})
            .then(() => {
                document.getElementById('planPreviewPanel').style.display = 'none';
                alert('✅ 视频计划已清除');
            })
            .catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        // 显示视频任务计划
        function showVideoPlan() {
            fetch('/get_video_plan')
            .then(r => r.json())
            .then(data => {
                if (data.plan) {
                    renderVideoPlan(data.plan);
                } else {
                    alert('❌ 未找到视频任务计划');
                }
            })
            .catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        // 显示视频任务统计
        function showVideoStats() {
            fetch('/get_video_stats')
            .then(r => r.json())
            .then(data => {
                displayVideoStats(data.stats);
            })
            .catch(err => {
                alert('❌ 请求失败: ' + err);
            });
        }
        
        // 恢复完整计划显示
        function restoreFullPlanDisplay() {
            // 恢复国家分布显示
            const distribDiv = document.getElementById('countryDistribStats');
            if (distribDiv) distribDiv.style.display = 'block';
            
            // 恢复覆盖时段信息
            const coverageInfo = document.getElementById('coverageInfo');
            if (coverageInfo) coverageInfo.style.display = 'block';
            
            // 如果有视频计划，重新渲染完整计划
            if (window.videoPlanData) {
                renderVideoPlan(window.videoPlanData);
            }
        }
        
        // 更新计划面板为只显示当前任务
        function updatePlanPanelWithCurrentTask(videoStatus) {
            const task = videoStatus.current_task;
            const total = videoStatus.total_tasks;
            
            // 更新计划摘要为当前任务进度
            document.getElementById('planSummary').innerHTML = 
                `<span style="color:#ff9900;">🚀 执行中</span> | ` +
                `当前任务: <b style="color:#00d4aa;">${task.idx}/${total}</b> | ` +
                `代理国家: <b style="color:#00aaff;">${task.proxy_country || '-'}</b>`;
            
            // 隐藏国家分布和其他统计
            const distribDiv = document.getElementById('countryDistribStats');
            if (distribDiv) distribDiv.style.display = 'none';
            
            const coverageInfo = document.getElementById('coverageInfo');
            if (coverageInfo) coverageInfo.style.display = 'none';
            
            // 更新表格只显示当前任务
            const thead = document.querySelector('#planTable thead tr');
            thead.innerHTML = `
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">序号</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">开始时间</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">预估时长</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">结束时间</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">代理国家</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">完成状态</th>
            `;
            
            const tbody = document.getElementById('planTableBody');
            tbody.innerHTML = '';
            
            // 把秒数（今日00:00起）转 HH:MM:SS
            function secToHHMMSS(utcSecToday) {
                const todayUTC = new Date();
                todayUTC.setUTCHours(0,0,0,0);
                const d = new Date(todayUTC.getTime() + utcSecToday * 1000);
                
                // 直接获取本地时区的时间
                const h = d.getHours();
                const m = d.getMinutes();
                const s = d.getSeconds();
                
                return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
            }
            
            const startStr = secToHHMMSS(task.actual_start || 0);
            const endStr = secToHHMMSS(task.actual_end || 0);
            const duration = (task.task_duration || 0).toFixed(1);
            const status = task.status || '执行中';
            let statusColor = '#aaa';
            if (status === '已完成') statusColor = '#00d4aa';
            if (status === '失败') statusColor = '#ff5555';
            if (status === '执行中') statusColor = '#ff9900';
            
            const row = document.createElement('tr');
            row.style.background = '#2a2a4a';
            row.innerHTML = 
                `<td style="padding:4px 6px; border-bottom:1px solid #222;">${task.idx}</td>` +
                `<td style="padding:4px 6px; border-bottom:1px solid #222;">${startStr}</td>` +
                `<td style="padding:4px 6px; border-bottom:1px solid #222;">${duration}s</td>` +
                `<td style="padding:4px 6px; border-bottom:1px solid #222;">${endStr}</td>` +
                `<td style="padding:4px 6px; border-bottom:1px solid #222;">${task.proxy_country || '-'}</td>` +
                `<td style="padding:4px 6px; border-bottom:1px solid #222; color:${statusColor}; font-weight:bold;">${status}</td>`;
            tbody.appendChild(row);
        }
        
        // 渲染视频任务计划
        function renderVideoPlan(plan) {
            // 保存计划数据到全局变量，用于任务停止后恢复显示
            window.videoPlanData = plan;
            
            // 把秒数（今日00:00起）转 HH:MM:SS，支持超过86400（跨天）
            function secToHHMMSS(utcSecToday) {
                // 直接使用本地时间计算，不转换时区
                const todayLocal = new Date();
                todayLocal.setHours(0, 0, 0, 0);
                const d = new Date(todayLocal.getTime() + utcSecToday * 1000);
                
                const h = d.getHours();
                const m = d.getMinutes();
                const s = d.getSeconds();
                
                return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
            }
            // 显示计划面板
            document.getElementById('planPreviewPanel').style.display = 'block';
            
            // 模型中文名映射
            const modelNames = {
                'simple': '简单随机',
                'normal': '正态分布(平稳)',
                'gamma': '伽马分布(活动突增)',
                'bimodal': '双峰分布(早晚高峰)',
                'poisson': '泊松分布(秒级脉冲)',
                'simple_video': '视频均匀分布'
            };
            const usedModel = plan.model_used || plan.chosen_model || 'simple_video';
            const tasks = plan.tasks || [];
            
            // 计算总时长（最后一个任务的结束时间 - 第一个任务的开始时间）
            let totalSec = 0;
            if (tasks.length > 0) {
                const lastTask = tasks[tasks.length - 1];
                const firstTask = tasks[0];
                totalSec = (lastTask.actual_end || 0) - (firstTask.actual_start || 0);
            }
            const hours = Math.floor(totalSec / 3600);
            const mins = Math.floor((totalSec % 3600) / 60);
            
            // 获取统计数据
            const valid = plan.total_tasks || tasks.length;
            const totalPlanned = plan.planned_tasks || plan.initial_count || tasks.length;
            const discarded = plan.discarded_tasks || plan.discarded_count || 0;
            
            // 作废任务数提示（按原因分类）
            let discardedTip = '';
            if (discarded > 0) {
                const reasons = plan.discard_reasons || {};
                const details = [];
                if (reasons.past_time) details.push(`过去${reasons.past_time}`);
                if (reasons.out_of_coverage) details.push(`非工作时间${reasons.out_of_coverage}`);
                if (reasons.soft_boundary) details.push(`边界${reasons.soft_boundary}`);
                if (reasons.out_of_window) details.push(`超窗口${reasons.out_of_window}`);
                const detailStr = details.length > 0 ? `（${details.join('/')}）` : '';
                discardedTip = ` | <span style="color:#ff5555;">作废: ${discarded}${detailStr}</span>`;
            }
            
            // 国家数量
            const countryCount = Object.keys(plan.country_distribution || {}).length;
            // 平均每小时任务数
            const avgPerHour = totalSec > 0 ? (valid / (totalSec / 3600)).toFixed(1) : 0;
            
            // 渲染计划摘要
            document.getElementById('planSummary').innerHTML = 
                `有效任务: <b style="color:#00d4aa;">${valid}</b>` +
                (totalPlanned ? ` / 计划: <b>${totalPlanned}</b>` : '') +
                ` | 模型: <b style="color:#ffaa00;">${modelNames[usedModel] || usedModel}</b> | ` +
                `国家: <b style="color:#00aaff;">${countryCount}</b> | ` +
                `跨度: <b style="color:#00d4aa;">${hours}h ${mins}m</b> | ` +
                `平均: <b style="color:#00d4aa;">${avgPerHour}/h</b>` + discardedTip;
            
            // 渲染国家分布统计
            const distribDiv = document.getElementById('countryDistribStats');
            if (distribDiv) {
                const distrib = plan.country_distribution || {};
                const sortedCountries = Object.keys(distrib).sort((a,b) => distrib[b] - distrib[a]);
                const countryFlags = {
                    'US':'🇺🇸','GB':'🇬🇧','DE':'🇩🇪','FR':'🇫🇷','JP':'🇯🇵',
                    'SG':'🇸🇬','HK':'🇭🇰','ID':'🇮🇩','AU':'🇦🇺','NZ':'🇳🇿'
                };
                let html = '<div style="display:flex; flex-wrap:wrap; gap:6px;">';
                sortedCountries.forEach(cc => {
                    const count = distrib[cc];
                    const flag = countryFlags[cc] || '🏳️';
                    html += `<span style="background:#222; padding:3px 8px; border-radius:4px; font-size:11px;">` +
                            `${flag} <b>${cc}</b>: ${count}</span>`;
                });
                html += '</div>';
                distribDiv.innerHTML = html;
            }
            
            // 渲染任务表格 - 与网站流量保持一致
            const thead = document.querySelector('#planTable thead tr');
            thead.innerHTML = `
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">序号</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">开始时间</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">预估时长</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">结束时间</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">代理国家</th>
                <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">完成状态</th>
            `;
            
            const tbody = document.getElementById('planTableBody');
            tbody.innerHTML = '';
            plan.tasks.forEach(t => {
                const startStr = secToHHMMSS(t.actual_start || 0);
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
            document.getElementById('btn-execute-plan').disabled = true; // 禁用网站流量任务执行按钮
            document.getElementById('btn-start-video').disabled = false; // 启用视频任务执行按钮
            document.getElementById('btn-clear-plan').disabled = false;
        }
        
        // 显示视频任务统计
        function displayVideoStats(stats) {
            const panel = document.getElementById('fingerprintStatsPanel');
            const container = document.getElementById('fingerprintStatsContainer');
            
            let html = '';
            
            // 总视频观看次数
            html += `<div style="margin-bottom:15px; padding:10px; background:#1a1a2e; border:1px solid #333; border-radius:4px;">
                <div style="color:#28a745; font-weight:bold; font-size:16px;">📊 总视频观看次数：${stats.total_views || 0}</div>
            </div>`;
            
            // 国家视频观看次数
            if (stats.country_views && Object.keys(stats.country_views).length > 0) {
                html += `<div style="margin-bottom:15px;">
                    <h4 style="color:#ffc107; margin:0 0 10px 0;">🎯 国家视频观看统计</h4>
                    <div style="max-height:200px; overflow-y:auto;">
                        <table style="width:100%; color:#fff; font-size:11px; border-collapse:collapse;">
                            <thead style="background:#1a1a2e;">
                                <tr>
                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left; width:60px;">观看次数</th>
                                    <th style="padding:4px; border-bottom:1px solid #444; text-align:left;">国家</th>
                                </tr>
                            </thead>
                            <tbody>`;
                Object.entries(stats.country_views).sort((a, b) => b[1] - a[1]).forEach(([country, count]) => {
                    const color = count > 50 ? '#ff5555' : (count > 20 ? '#ffc107' : '#28a745');
                    html += `<tr>
                        <td style="padding:4px; border-bottom:1px solid #222; color:${color}; font-weight:bold;">${count}次</td>
                        <td style="padding:4px; border-bottom:1px solid #222;">${country}</td>
                    </tr>`;
                });
                html += `</tbody></table></div></div>`;
            }
            
            if (!html) {
                html = '<div style="color:#aaa; text-align:center; padding:20px;">暂无视频统计数据</div>';
            }
            
            container.innerHTML = html;
            panel.style.display = 'block';
        }
        
        // 检查视频任务状态
        function checkVideoTaskStatus() {
            fetch('/get_video_task_status')
            .then(r => r.json())
            .then(data => {
                if (data.running) {
                    // 任务仍在运行，更新计划面板显示当前任务
                    if (data.current_task) {
                        updatePlanPanelWithCurrentTask(data);
                    }
                    // 继续检查
                    setTimeout(checkVideoTaskStatus, 5000);
                } else {
                    // 任务已停止
                    document.getElementById('btn-start-video').disabled = false;
                    document.getElementById('btn-stop-video').disabled = true;
                    alert('✅ 视频任务已完成');
                }
            })
            .catch(err => {
                console.error('检查视频任务状态失败:', err);
                setTimeout(checkVideoTaskStatus, 5000);
            });
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
    """从VPS获取代理和IP信息（旧方式：单代理）"""
    return get_proxy_from_api_url(config["ip_proxy_api"], config.get("ip_proxy_user", ""), config.get("ip_proxy_pwd", ""), "US")

def get_proxy_from_api_url(api_url, api_user, api_pwd, country_code="US"):
    """从VPS获取代理（代理池方式）
    
    统一使用 ip_provider 模块，消除重复代码。
    内部复用 IPProvider._fetch_proxy_from_vps 的实现。
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
            # 滚动到广告位置
            ad_element.scroll_into_view_if_needed()
            behavior_stats["scrolls"] += 1
            
            # 获取广告元素的中心位置
            box = ad_element.bounding_box()
            if box:
                ad_center_x = box["x"] + box["width"] / 2
                ad_center_y = box["y"] + box["height"] / 2
                
                # 使用贝塞尔曲线移动到广告中心
                human_mouse_move(page, current_x, current_y, ad_center_x, ad_center_y, config)
                current_x, current_y = ad_center_x, ad_center_y
            
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
        except:
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
        except:
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
    浏览器数据面统一通过 socks5://VPS:1666 出网，因此视频链接必须保持原始 URL。
    """
    log.info("阶段3 SOCKS5 模式：视频URL保持原始地址，由浏览器代理统一出网")
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
        if not video_task_running:
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
        
        if not video_task_running:
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
        
        if not video_task_running:
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
            except:
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
            except:
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
            except:
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
                except:
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
            except:
                continue
        
        if target_elements:
            target_elem = target_elements[0]
            try:
                target_elem.scroll_into_view_if_needed(timeout=5000)
            except Exception as e:
                log.warning(f"⚠️ 滚动返回按钮超时，尝试 JS 兜底滚动: {str(e)[:60]}")
                try:
                    page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", target_elem)
                except:
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
        except:
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
                    except:
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
            except:
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
                    except:
                        continue
                
                if target_link:
                    # 滚动到可见区域
                    try:
                        target_link.scroll_into_view_if_needed(timeout=5000)
                    except Exception as e:
                        log.warning(f"⚠️ 滚动链接超时，使用 JS 兜底滚动: {str(e)[:60]}")
                        try:
                            page.evaluate("(el) => el && el.scrollIntoView({block:'center'})", target_link)
                        except:
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
                            except:
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

    # 先尝试正常的链接点击（关键词或 href 匹配）
    try:
        _has_kw = bool(text_list and any(str(k).strip() for k in text_list))
        if _has_kw:
            success, new_x, new_y = click_link_containing_text(page, text_list, current_x, current_y, config)
            if success:
                log.info("✅ 通过关键词链接跳转成功")
                return True, new_x, new_y
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
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
            except:
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
        except:
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
            except:
                links = []
            
            if not links:
                log.warning("未找到可点击链接，尝试寻找按钮")
                try:
                    links = page.query_selector_all('button, [role="button"], [onclick]')
                except:
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
                            except:
                                pass
                except:
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
                    except:
                        pass
                    wait_after_click_scroll = random.uniform(0.5, 1)
                    time.sleep(wait_after_click_scroll)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(wait_after_click_scroll * 1000)
                    
                    # 更新当前URL
                    try:
                        current_url = page.url
                        log.info(f"当前页面: {current_url}")
                    except:
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
    global task_running, stats, pending_plan, planned_total_tasks, current_task_idx, current_plan, adsl_status
    stats["total"] = 0
    stats["success"] = 0
    stats["fail"] = 0

    log.info("任务已启动")
    task_running = True
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
            return
    except Exception as e:
        log.error(f"❌ 网页浏览模式配置校验出错: {str(e)}")
        task_running = False
        return

    # ========== Step A: 获取任务清单
    if single_task:
        import random
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
            is_adsl_task = current_task.get("ip_mode") == "adsl"
            log.info(f"🎯 任务计划代理国家: {planned_country}")
            
            if is_adsl_task:
                selected_proxy = {"country_code": "ADSL", "proxy_api_url": "", "proxy_user": "", "proxy_pwd": ""}
                current_task['proxy_api_url'] = ""
                current_task['proxy_user'] = ""
                current_task['proxy_pwd'] = ""
                current_task['proxy_country'] = "ADSL"
                log.info("[ADSL] 当前任务使用本机 ADSL 直连，不走代理池/6666/1666")
            else:
                # 在代理池中找到匹配的代理
                matched_proxies = [p for p in proxy_pool_enabled if p.get('country_code') == planned_country]
                if not matched_proxies:
                    # 如果找不到预定国家的代理，从可用代理中选一个（兜底切换）
                    log.warning(f"⚠️ 未找到 {planned_country} 代理，启动兜底切换")
                    available_proxies = get_available_proxies(proxy_pool_enabled)
                    if not available_proxies:
                        log.error("❌ 没有可用代理（工作时间内），跳过本任务")
                        stats["fail"] += 1
                        stats["total"] += 1
                        continue
                    selected_proxy = random.choice(available_proxies)
                    log.info(f"🔄 兜底切换至: {selected_proxy.get('country_code')}")
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
                selected_engine = None
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
                        if is_adsl_task:
                            adsl_status["current"] = task_idx + 1
                            adsl_status["status"] = "重拨取IP"
                            exit_ip, resolved_ip_info = redial_adsl_and_get_ip()
                            ip_info = dict(resolved_ip_info)
                            proxy_info = {
                                "success": True,
                                "proxy_host": "DIRECT",
                                "proxy_port": "DIRECT",
                                "ip_info": ip_info,
                                "adsl_direct": True
                            }
                            layer1_success = True
                            ipdeep_success = True
                            region = resolved_ip_info.get("region", "未知") or "未知"
                            city = resolved_ip_info.get("city", "未知") or "未知"
                        elif ip_attempt > 0:
                            log.warning(f"🔁 重新获取代理 IP（第 {ip_attempt+1}/{ip_retry_max} 次）...")
                        else:
                            log.info(f"正在从VPS获取代理 (国家: {current_task['proxy_country']})...")
                        if not is_adsl_task:
                            proxy_info = get_proxy_from_api_url(
                            current_task["proxy_api_url"],
                            current_task["proxy_user"],
                            current_task["proxy_pwd"],
                            current_task["proxy_country"]
                        )
                        
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
                        
                        # ========== 社媒引流：强制语言锁定 ==========
                        social_config = config.get("social_media", {})
                        platform_region = social_config.get("platform_region", "auto")
                        if platform_region == "western":
                            # 欧美社媒：强制锁定英文，过滤非英文IP
                            original_lang = language
                            language = "en-US"
                            resolved_ip_info["language"] = "en-US"
                            log.info(f"🌍 [社媒强制] 欧美社媒区域：强制锁定语言为 en-US（原语言: {original_lang}）")
                            # 如果IP国家不是欧美/英文区域，仍然允许，但日志提醒
                            if not lang_lower.startswith("en"):
                                log.warning("⚠️ [社媒强制] 当前IP语言非英文，但配置要求欧美社媒，继续运行（语言已强制覆盖）")
                        elif platform_region == "chinese":
                            # 中文社媒：强制锁定中文，过滤非中文IP
                            original_lang = language
                            language = "zh-CN"
                            resolved_ip_info["language"] = "zh-CN"
                            log.info(f"🌍 [社媒强制] 中文社媒区域：强制锁定语言为 zh-CN（原语言: {original_lang}）")
                            # 如果IP国家不是中国，仍然允许，但日志提醒
                            if not lang_lower.startswith("zh"):
                                log.warning("⚠️ [社媒强制] 当前IP语言非中文，但配置要求中文社媒，继续运行（语言已强制覆盖）")
                        
                        lang_lower = (language or "").lower()
                        log.info(
                            f"✅ IP 三要素识别成功 country={country}, timezone={timezone}, "
                            f"language={language}, source={resolved_ip_info.get('source')}"
                        )
                        if is_adsl_task:
                            current_task['proxy_country'] = cc_upper or country or "ADSL"
                        
                        # —— Step 2: 决定 SEO 区域（严禁跳过 SEO，不可支持的语言直接舍弃 IP） ——
                        if not config.get("enable_seo", True):
                            log.error("❌ enable_seo=False，无法启动任务（严禁跳过 SEO）")
                            ip_region = REGION_FAILED
                            break
                        
                        if lang_lower.startswith("zh") and cc_upper == "CN":
                            ip_region = "中国"
                            log.info(f"✓ language=zh + country=CN → 中国 SEO")
                        elif lang_lower.startswith("en"):
                            ip_region = "美国"
                            log.info(f"✓ language={language} → 美国 SEO（英文搜索引擎）")
                        elif lang_lower.startswith(("de", "fr", "it", "es", "nl", "sv", "no", "da", "fi", "pl", "pt", "el", "ja", "ko")):
                            # 非英语欧美/日韩 → 用美国（英文）SEO 兜底（这些用户访问英文内容是常态）
                            ip_region = "美国"
                            log.info(f"✓ language={language}（欧美/日韩）→ 美国 SEO 兜底")
                        else:
                            log.warning(f"⚠️ language={language} 不在 SEO 支持范围，舍弃 IP 换下一个")
                            continue
                        
                        # —— Step 3: 选搜索引擎 / 关键词 / 生成 Referer ——
                        log.info(f"根据地域选择搜索引擎: {ip_region}")
                        selected_engine = seo_query.get_random_engine_for_region(ip_region)
                        if not selected_engine:
                            log.warning(f"⚠️ 地域 {ip_region} 没有可用搜索引擎，舍弃 IP 换下一个")
                            continue
                        
                        selected_keyword = seo_query.get_random_keyword_for_engine(selected_engine)
                        if not selected_keyword:
                            log.warning(f"⚠️ 搜索引擎 {selected_engine} 没有可用关键词，舍弃 IP 换下一个")
                            continue
                        
                        generated_referer = seo_query.generate_referer(selected_engine, selected_keyword)
                        if not generated_referer:
                            log.warning(f"⚠️ Referer 生成失败，舍弃 IP 换下一个")
                            continue
                        
                        # ========== 社媒引流：帖子 Referer 语种匹配 ==========
                        social_config = config.get("social_media", {})
                        post_urls = social_config.get("post_urls", [])
                        if post_urls:
                            # 有社媒帖子URL配置：随机选一个作为Referer，确保语言一致性（已由平台区域锁定保证）
                            selected_post_url = random.choice(post_urls)
                            generated_referer = selected_post_url
                            platform_region = social_config.get("platform_region", "auto")
                            log.info(f"📱 [社媒Referer] 使用配置的帖子URL: {generated_referer} (区域={platform_region})")
                        
                        # —— 全部成功 ——
                        ip_success = True
                        seo_ready = True
                        log.info(
                            f"✓ SEO流量模拟准备就绪: 地域={ip_region}, 引擎={selected_engine}, "
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
                
                # 构建代理配置（连接 VPS 代理需要 VPS 的认证）
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
                    
                    if is_adsl_task:
                        proxy_config = None
                        log.info("[ADSL] ✅ 浏览器数据面使用本机直连出口，不设置 SOCKS5/HTTP 代理")
                    else:
                        vps_socks5_port = int(config.get("vps_socks5_port") or 1666)
                        proxy_config = {
                            "server": f"socks5://{config['vps_host']}:{vps_socks5_port}",
                        }
                        log.info(f"[代理配置] ✅ 浏览器数据面使用 SOCKS5: {proxy_config['server']}（控制面预热节点来自 {proxy_host}:{proxy_port}）")
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
                        if is_adsl_task:
                            _proxy_for_check = None
                            log.info(f"🩺 [ADSL诊断] 本机直连访问 {_target_url} ...")
                        else:
                            vps_control_port = int(config.get("vps_new_port") or 6666)
                            _proxy_for_check = {
                                "http": f"http://{config.get('vps_user', 'admin')}:{config.get('vps_pass', 'admin123')}@{config['vps_host']}:{vps_control_port}",
                                "https": f"http://{config.get('vps_user', 'admin')}:{config.get('vps_pass', 'admin123')}@{config['vps_host']}:{vps_control_port}"
                            }
                            log.info(f"🩺 [诊断] 通过代理访问 {_target_url} ...")
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
                if proxy_config and str(proxy_config.get("server", "")).startswith("socks5://"):
                    log.info("SOCKS5代理模式：通过Selenium Chrome访问，噪音请求已通过--host-rules和JS hook拦截")
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
                _launch_args.extend([
                        f"--lang={_launch_lang}",
                        f"--window-size={width},{height}",
                        "--disable-webrtc",
                        "--disable-webrtc-encryption",
                        "--disable-webrtc-stun-origin",
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
                def _launch_browser(_pw, _use_chrome_channel, _kwargs, _max_wait_sec=60):
                    import signal as _sig
                    class _TimeoutError(Exception):
                        pass
                    def _on_timeout(_signum, _frame):
                        raise _TimeoutError(f"浏览器启动超时（>{_max_wait_sec}s）")
                    _prev = None
                    try:
                        try:
                            _prev = _sig.signal(_sig.SIGALRM, _on_timeout)
                            _sig.alarm(_max_wait_sec)
                        except (ValueError, AttributeError, OSError):
                            _prev = "no-sig"
                        try:
                            if _use_chrome_channel:
                                _browser = _pw.chromium.launch(channel="chrome", **_kwargs)
                            else:
                                _browser = _pw.chromium.launch(**_kwargs)
                            return _browser, None
                        except _TimeoutError as _te:
                            return None, str(_te)
                        except Exception as _e:
                            return None, f"{type(_e).__name__}: {str(_e)[:300]}"
                    finally:
                        try:
                            _sig.alarm(0)
                        except Exception:
                            pass
                        try:
                            if _prev not in (None, "no-sig"):
                                _sig.signal(_sig.SIGALRM, _prev)
                        except Exception:
                            pass

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
                if _use_real_chrome:
                    browser, _lerr = _launch_browser(p, True, _launch_kwargs, 60)
                    if browser is not None:
                        log.info("✅ 使用本地 Chrome 启动成功（支持 H.264/AAC）")
                    else:
                        launch_errors.append(f"chrome-full={_lerr or '未知'}")
                        log.warning(f"⚠️ 本地 Chrome 启动失败/超时，回退到 系统Chrome: {_lerr or '未知'}")
                if browser is None:
                    browser, _lerr2 = _launch_browser(p, False, _launch_kwargs, 60)
                    if browser is None:
                        launch_errors.append(f"chromium-full={_lerr2 or '未知'}")
                        log.warning(f"⚠️ Chrome 完整参数启动失败，尝试极简参数: {_lerr2 or '未知'}")
                        browser, _lerr3 = _launch_browser(p, False, _minimal_launch_kwargs(), 60)
                        if browser is None:
                            launch_errors.append(f"chromium-minimal={_lerr3 or '未知'}")
                            raise RuntimeError("浏览器启动失败: " + " | ".join(launch_errors))
                        log.info("✅ 使用 Chrome 极简参数启动成功")
                    else:
                        log.info("使用系统 Chrome 启动（可能不支持 HLS/H.264）")
                
                # 创建浏览器上下文，严格应用指纹配置
                
                # 构建额外头部：只设置业务需要的 Referer，避免发送非法空代理头
                extra_http_headers = {}
                if generated_referer:
                    extra_http_headers["Referer"] = generated_referer
                    log.info(f"设置Referer头部: {generated_referer}")
                else:
                    default_referers = ["https://www.google.com/", "https://www.bing.com/", "https://www.baidu.com/"]
                    import random
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
                extra_http_headers.setdefault("Sec-Fetch-Site", "none")
                extra_http_headers.setdefault("Sec-Fetch-Mode", "navigate")
                extra_http_headers.setdefault("Sec-Fetch-User", "?1")
                extra_http_headers.setdefault("Sec-Fetch-Dest", "document")

                # 添加缺失的 Sec-Ch-Ua 请求头
                extra_http_headers["Sec-Ch-Ua"] = '"Not_A Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"'
                extra_http_headers["Sec-Ch-Ua-Mobile"] = "?0"
                extra_http_headers["Sec-Ch-Ua-Platform"] = '"Windows"' 
                
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
                    // ========== 0. 彻底禁用WebRTC ==========
                    (function() {{
                        // 1. 删除WebRTC构造函数
                        delete window.RTCPeerConnection;
                        delete window.webkitRTCPeerConnection;
                        delete window.RTCSessionDescription;
                        delete window.RTCIceCandidate;
                        delete window.MediaStream;
                        delete window.MediaStreamTrack;
                        delete window.webkitMediaStream;
                        delete window.webkitMediaStreamTrack;
                        delete window.navigator.mediaDevices;
                        
                        // 2. 覆盖为undefined
                        Object.defineProperty(window, 'RTCPeerConnection', {{
                            value: undefined,
                            configurable: false,
                            writable: false
                        }});
                        Object.defineProperty(window, 'webkitRTCPeerConnection', {{
                            value: undefined,
                            configurable: false,
                            writable: false
                        }});
                        
                        // 3. 删除navigator上的相关属性
                        Object.defineProperty(navigator, 'getUserMedia', {{
                            value: undefined,
                            configurable: false,
                            writable: false
                        }});
                        Object.defineProperty(navigator, 'webkitGetUserMedia', {{
                            value: undefined,
                            configurable: false,
                            writable: false
                        }});
                    }})();
                    
                    // ========== 1. 隐藏自动化特征 ==========
                    // 隐藏navigator.webdriver
                    Object.defineProperty(navigator, 'webdriver', {{
                        value: undefined,
                        writable: false,
                        configurable: false
                    }});
                    
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
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                    delete window.$cdc_asdjflasutopfhvcZLmcfl_;
                    
                    // 隐藏CDP特征
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({{ state: Notification.permission }}) :
                            originalQuery(parameters)
                    );
                    

                    // ========== 2. 补充真实浏览器属性 ==========
                    // 模拟plugins
                    if (!navigator.plugins.length) {{
                        const pluginClasses = [
                            {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' }},
                            {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' }},
                            {{ name: 'Native Client', filename: 'internal-nacl-plugin' }}
                        ];
                        
                        const createPlugin = (info) => {{
                            const plugin = document.createElement('embed');
                            plugin.setAttribute('name', info.name);
                            plugin.setAttribute('src', '');
                            plugin.setAttribute('type', 'application/x-google-chrome-pdf');
                            plugin.style.display = 'none';
                            document.body.appendChild(plugin);
                            return plugin;
                        }};
                        
                        Object.defineProperty(navigator, 'plugins', {{
                            value: pluginClasses.map(createPlugin),
                            writable: false,
                            configurable: false
                        }});
                    }}
                    
                    // 模拟mimeTypes
                    if (!navigator.mimeTypes.length) {{
                        const mimeTypes = [
                            {{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
                            {{ type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
                            {{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }}
                        ];
                        
                        Object.defineProperty(navigator, 'mimeTypes', {{
                            value: mimeTypes,
                            writable: false,
                            configurable: false
                        }});
                    }}
                    
                    // ========== 3. Canvas和WebGL指纹（合规化：噪声扰动 + 真实GPU字符串） ==========
                    // Canvas：对真实渲染结果注入稳定的逐像素微噪声（基于会话种子），而非返回固定串
                    (function() {{
                        const _seed = {canvas_noise_seed} >>> 0;
                        let _s = _seed || 1;
                        const _rnd = function() {{ _s = (_s * 1103515245 + 12345) & 0x7fffffff; return _s / 0x7fffffff; }};
                        const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                        CanvasRenderingContext2D.prototype.getImageData = function() {{
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
                        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
                        HTMLCanvasElement.prototype.toDataURL = function() {{
                            try {{
                                const ctx = this.getContext('2d');
                                if (ctx) {{ ctx.getImageData(0, 0, Math.max(1,this.width), Math.max(1,this.height)); }}
                            }} catch(e) {{}}
                            return _origToDataURL.apply(this, arguments);
                        }};
                        const _origToBlob = HTMLCanvasElement.prototype.toBlob;
                        if (_origToBlob) {{
                            HTMLCanvasElement.prototype.toBlob = function() {{
                                try {{
                                    const ctx = this.getContext('2d');
                                    if (ctx) {{ ctx.getImageData(0, 0, Math.max(1,this.width), Math.max(1,this.height)); }}
                                }} catch(e) {{}}
                                return _origToBlob.apply(this, arguments);
                            }};
                        }}
                    }})();
                    
                    // WebGL：UNMASKED_VENDOR(37445)/UNMASKED_RENDERER(37446) 返回真实GPU字符串，覆盖 WebGL1+WebGL2
                    (function() {{
                        const _vendor = "{webgl_vendor}";
                        const _renderer = "{webgl_renderer}";
                        const _patch = function(proto) {{
                            if (!proto || !proto.getParameter) return;
                            const _orig = proto.getParameter;
                            proto.getParameter = function(param) {{
                                if (param === 37445) return _vendor;
                                if (param === 37446) return _renderer;
                                return _orig.call(this, param);
                            }};
                        }};
                        try {{ _patch(WebGLRenderingContext.prototype); }} catch(e) {{}}
                        try {{ _patch(WebGL2RenderingContext.prototype); }} catch(e) {{}}
                    }})();
                    
                    // ========== 3.1 硬件信息注入（hardwareConcurrency / deviceMemory） ==========
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
                    
                    Object.defineProperty(navigator, 'languages', {{
                        value: ["{browser_locale}", "en-US", "en"],
                        writable: false,
                        configurable: false
                    }});
                    
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
                                    const m = part.value.match(/GMT([+-]?)(\d+)?(?::(\d+))?/);
                                    if (m) {{
                                        const sign = m[1] === "-" ? -1 : 1;
                                        const h = parseInt(m[2] || "0", 10);
                                        const mi = parseInt(m[3] || "0", 10);
                                        _offsetMin = sign * (h * 60 + mi);  // getTimezoneOffset 的约定：UTC-西为正
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
                    // 隐藏chrome.runtime
                    if (window.chrome) {{
                        delete window.chrome.runtime;
                    }}
                    
                    // 模拟真实的窗口属性
                    Object.defineProperty(window, 'outerWidth', {{
                        value: {width},
                        writable: false,
                        configurable: false
                    }});
                    
                    Object.defineProperty(window, 'outerHeight', {{
                        value: {height},
                        writable: false,
                        configurable: false
                    }});
                    
                    // 模拟真实的屏幕属性
                    Object.defineProperty(screen, 'width', {{
                        value: {width},
                        writable: false,
                        configurable: false
                    }});
                    
                    Object.defineProperty(screen, 'height', {{
                        value: {height},
                        writable: false,
                        configurable: false
                    }});
                    
                    Object.defineProperty(screen, 'availWidth', {{
                        value: {width},
                        writable: false,
                        configurable: false
                    }});
                    
                    Object.defineProperty(screen, 'availHeight', {{
                        value: {height},
                        writable: false,
                        configurable: false
                    }});
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
                    # 尝试使用 cc_upper 变量
                    try:
                        # 使用 exec 避免编译器检查变量是否绑定
                        exec("if 'cc_upper' in locals() and cc_upper: country_code = cc_upper")
                    except Exception as e:
                        log.warning(f"⚠️ 无法获取 cc_upper 变量: {e}")
                    else:
                        # 从 resolved_ip_info 中获取国家代码
                        if "resolved_ip_info" in locals() and resolved_ip_info is not None:
                            country_code = (resolved_ip_info.get("country_code") or "").upper()
                        else:
                            log.warning("⚠️ 无法获取国家代码")
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
                        if search_mode == "real_search" and selected_engine_id in ["google", "bing"]:
                            # 执行完整搜索跳转流程（带真人模拟）
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

                        # ★ 新逻辑：每轮独立随机一个浏览时长，总时长 = 各轮之和（循环越多任务越长，无保险绳上限）
                        #   每轮内部：该轮随机时长 按六层 stay_ratio 比率分配到 L1→L6
                        round_total_stays = []
                        for _r in range(chapter_loop_count):
                            _rt = random.uniform(config["total_stay"]["min"], config["total_stay"]["max"])
                            round_total_stays.append(max(_rt, 10.0))
                        total_task_stay = sum(round_total_stays)

                        # 每轮每层停留时长矩阵：round_layer_stays[轮][层] = 该轮随机时长 × (层比率/总比率)
                        # ★ 纯比率瓜分：严格保证 Σ(各层) = 该轮随机时长，不再用 min_stay 抬高（避免总时长超标）
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
                                f"📊 第{_ridx+1}轮每层停留(L1-L6): "
                                + ", ".join(f"L{i+1}≈{round_layer_stays[_ridx][i]:.1f}s" for i in range(6))
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
                            _retry_wait_list = [6, 10]  # 第1、2次失败后的等待（秒）
                            for retry in range(3):
                                try:
                                    # wait_until="commit" 最快返回（响应头到达即算成功），后续自行等待内容
                                    page.goto(target_url, timeout=45000, wait_until="commit")
                                    # commit 后再等待网络空闲/内容稳定，避免出现空白页
                                    try:
                                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                                    except Exception:
                                        pass  # 即使内容未完全加载，只要能响应就继续
                                    # 额外给页面 JavaScript 渲染 2.5-4 秒时间
                                    time.sleep(random.uniform(2.5, 4.0))
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
                        ad_monitor = scan_ads_during_task(page, ad_monitor, "首页加载后")
    
                        # ========== 停止信号检查辅助（已移除全局保险绳；仅响应用户停止） ==========
                        def _check_rope(stage_desc=""):
                            if not task_running:
                                raise RuntimeError("任务已停止")
    
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
                                    config, page_name=f"[T{task_idx+1}] 首页"
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
                                        config, page_name="列表页"
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
                                        config, page_name=f"layer_{level_idx+1}"
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
    
                                # 每轮间隔
                                if loop_idx < chapter_loop_count - 1 and loop_interval > 0:
                                    try:
                                        _check_rope("每轮间隔前")
                                    except RuntimeError:
                                        break
                                    log.info(f"⏸ 每轮浏览间隔: {loop_interval:.1f}秒")
                                    time.sleep(loop_interval)
    
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
                            if is_adsl_task:
                                adsl_status["completed"] += 1
                                adsl_status["status"] = "单轮完成"
                        else:
                            stats["fail"] += 1
                            if is_adsl_task:
                                adsl_status["status"] = "单轮失败"
                        
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

                        log.task_result(task_time, False, False, f"系统异常: {str(e)}")
                        
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
    
    task_running = False
    if adsl_ip_task:
        adsl_status["running"] = False
        adsl_status["status"] = "已停止" if adsl_status.get("completed", 0) < adsl_status.get("total", 0) else "完成"
    current_task_idx = -1
    current_plan = None
    stop_human_model()
    log.info("任务已停止")


def generate_video_daily_tasks(cfg):
    """生成视频任务每日任务清单 - 24小时全球分布版
    
    核心逻辑：
    1. 根据视频观看数量、时长、间隔计算每个任务的预计时间
    2. 根据计划天数计算总任务数（24小时均匀分布）
    3. 计算全球覆盖时段（自动检测启用代理的工作时间）
    4. 国家配额平均分配
    5. 生成任务时间点并筛选到工作时段内
    6. 智能选代理（优先剩余配额多的国家）
    
    任务计算示例：
    - 每个任务观看 vt_watch_count 个视频
    - 每个视频时长 total_stay (30-120秒)
    - 视频间隔 vt_task_interval (5-15秒)
    - 任务间隔 vt_global_interval (默认10-30秒)
    - 一天任务数 ≈ 86400 / (视频数×最大时长 + (视频数-1)×最大间隔 + 任务间隔)
    """
    import datetime as _dt
    
    # 获取视频任务配置
    vt_watch_count = cfg.get('vt_watch_count', 3)  # 每个任务观看的视频数量
    vt_task_days = cfg.get('vt_task_days', 1)      # 任务计划天数（0=无限循环）
    
    # 获取视频链接池
    vt_video_urls = cfg.get('vt_video_urls', '').split(',')
    vt_video_urls = [url.strip() for url in vt_video_urls if url.strip()]
    
    if not vt_video_urls:
        raise Exception("视频链接池为空")
    
    # 准备代理池
    proxy_pool_enabled = [p for p in cfg.get("proxy_pool", []) if p.get("enabled", False) and p.get("proxy_api_url")]
    if not proxy_pool_enabled:
        proxy_pool_enabled = [{
            "country_code": "US",
            "proxy_api_url": cfg.get("ip_proxy_api", ""),
            "proxy_user": cfg.get("ip_proxy_user", ""),
            "proxy_pwd": cfg.get("ip_proxy_pwd", "")
        }]
    
    # 配置参数
    total_stay_cfg = cfg.get("total_stay", {"min": 30, "max": 120})      # 单个视频观看时长
    video_ad_cfg = cfg.get("video_ad", {})                                # 视频广告配置（包含观看时长）
    interval_cfg = cfg.get("vt_task_interval", {"min": 5, "max": 15})    # 视频之间的间隔时间
    global_interval_cfg = cfg.get("vt_global_interval", {"min": 10, "max": 30})  # 任务之间的间隔
    
    # 计算每个任务的最大耗时（用于估算任务数量）
    # 使用视频观看时长配置，而不是total_stay配置
    video_max_time = video_ad_cfg.get("max_watch_time", 60)
    max_task_duration = vt_watch_count * video_max_time + (vt_watch_count - 1) * interval_cfg["max"]
    avg_global_interval = (global_interval_cfg["min"] + global_interval_cfg["max"]) / 2
    
    # 计算每天可安排的任务数（按最大耗时计算，确保不会超时）
    daily_capacity = int(86400 / (max_task_duration + avg_global_interval))
    
    # 1. 计算全球覆盖时段（根据任务计划天数）
    now_utc = _dt.datetime.now(pytz.UTC)
    today_utc_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_now_utc = (now_utc - today_utc_start).total_seconds()
    
    # 确定计划结束时间和总任务数
    if vt_task_days == 0:
        # 无限循环，表示没有特定的结束时间（显示7天计划）
        end_of_window = seconds_now_utc + 86400 * 7
        total_planned_tasks = daily_capacity * 7  # 7天的任务数
    else:
        # 有限天数的计划
        end_of_window = seconds_now_utc + 86400 * vt_task_days
        total_planned_tasks = daily_capacity * vt_task_days  # 按天数计算任务数
    
    # 收集各代理国家的工作时段
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
        
        # 计算需要检查的天数范围
        start_day_offset = 0  # 今天
        if vt_task_days == 0:
            end_day_offset = 7  # 7天后
        else:
            end_day_offset = vt_task_days - 1  # 计划天数内
        
        # 检查各天的本地工作时段（7:00-24:00）
        for day_offset in range(start_day_offset, end_day_offset + 1):
            local_date = today_utc_start.date() + _dt.timedelta(days=day_offset)
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
    
    # 只保留从现在往后窗口内的覆盖时段
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
    coverage_pct = (total_coverage_seconds / (86400 * (7 if vt_task_days == 0 else vt_task_days))) * 100
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
            "planned_tasks": total_planned_tasks,
            "discarded_tasks": total_planned_tasks,
            "discard_reasons": {},
            "compensated_count": 0,
            "model_used": "none",
            "tasks": [],
            "coverage": coverage,
            "country_distribution": {},
            "country_quota_target": {},
            "warnings": ["⚠️ 没有启用任何代理"]
        }
    
    # 2. 国家配额分配（平均分配）
    base_quota = total_planned_tasks / len(enabled_countries)
    country_quota_target = {}
    for cc in enabled_countries:
        quota = base_quota * random.uniform(0.8, 1.2)
        country_quota_target[cc] = max(1, int(round(quota)))
    
    # 调整配额总和等于总任务数
    total_quota = sum(country_quota_target.values())
    if total_quota != total_planned_tasks:
        diff = total_planned_tasks - total_quota
        for cc in enabled_countries:
            if diff == 0:
                break
            country_quota_target[cc] += 1
            diff -= 1
    
    # 3. 生成全局任务时间点（根据选择的流量模型）
    chosen_model = "simple_video"
    raw_time_points = []
    
    # 获取用户选择的流量模型
    selected_models = cfg.get("selected_models", ["normal", "gamma", "bimodal", "poisson"])
    selected_models = [m for m in selected_models if m in MODEL_FUNCTIONS]
    
    if selected_models:
        # 使用选择的流量模型随机生成任务时间点
        chosen_model = random.choice(selected_models)
        model_func = MODEL_FUNCTIONS[chosen_model]
        hour_list = model_func(total_planned_tasks)
        
        for h in hour_list:
            tp = seconds_now_utc + h * 3600
            # 确保时间点在计划窗口内
            if tp >= seconds_now_utc and tp < end_of_window:
                raw_time_points.append(tp)
    else:
        # 默认使用均匀分布
        chosen_model = "simple_video"
        if future_covered:
            total_available = sum(e - s for s, e in future_covered)
            if total_available > 0 and total_planned_tasks > 0:
                avg_interval = total_available / total_planned_tasks
                cursor = seconds_now_utc
                
                # 按覆盖时段分配任务
                for seg_start, seg_end in future_covered:
                    current_start = max(cursor, seg_start)
                    while current_start < seg_end and len(raw_time_points) < total_planned_tasks:
                        raw_time_points.append(current_start)
                        current_start += avg_interval
    
    raw_time_points.sort()
    
    # 4. 应用软边界概率筛选 + 落到覆盖时段内
    valid_time_points = []
    discard_reasons = {
        "past_time": 0,
        "out_of_window": 0,
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
    
    # 5. 任务实际分配（智能代理选择 + 间隔抖动 + 顺延冲突）
    tasks = []
    country_quota_used = {cc: 0 for cc in enabled_countries}
    
    # 获取本地时区
    local_tz = pytz.timezone('Asia/Shanghai')
    # 计算本地时间的 00:00
    import datetime as _dt
    local_now = _dt.datetime.now(local_tz)
    today_local_start = local_tz.localize(_dt.datetime(local_now.year, local_now.month, local_now.day, 0, 0, 0))
    # 转换为 UTC 时间
    today_local_start_utc = today_local_start.astimezone(pytz.UTC)
    
    # 计算相对于本地时间 00:00 的秒数
    seconds_now_local = (local_now - today_local_start).total_seconds()
    prev_end_time = seconds_now_local
    is_first = True
    
    # 找到最接近当前时间的任务时间点作为第一条任务
    if valid_time_points:
        # 转换 valid_time_points 为相对于本地时间 00:00 的秒数
        local_time_points = []
        for tp in valid_time_points:
            utc_datetime = today_utc_start + _dt.timedelta(seconds=tp)
            local_datetime = utc_datetime.astimezone(local_tz)
            local_seconds = (local_datetime - today_local_start).total_seconds()
            local_time_points.append(local_seconds)
        
        # 找到最接近当前时间的任务时间点
        closest_idx = min(range(len(local_time_points)), key=lambda i: abs(local_time_points[i] - seconds_now_local))
        closest_time = local_time_points[closest_idx]
        
        # 如果最接近的时间点距离当前时间超过5分钟，尝试创建一个更接近的时间点
        time_diff = abs(closest_time - seconds_now_local)
        if time_diff > 300:  # 5分钟
            # 创建一个新的时间点，尽可能接近当前时间
            new_time = seconds_now_local
            # 确保新时间点在覆盖时段内
            # 首先需要将覆盖时段转换为相对于本地时间的秒数
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
                # 将新时间点添加到列表开头
                valid_time_points.insert(0, (today_local_start_utc + _dt.timedelta(seconds=new_time) - today_utc_start).total_seconds())
                local_time_points.insert(0, new_time)
                print(f"🎯 第一条任务距离当前时间较远 ({time_diff:.0f}秒)，已创建新的任务时间点")
            else:
                # 如果当前时间不在覆盖时段内，使用原方法
                valid_time_points.insert(0, valid_time_points.pop(closest_idx))
        else:
            # 如果距离在5分钟内，直接使用原方法
            valid_time_points.insert(0, valid_time_points.pop(closest_idx))
    
    for tp in valid_time_points:
        # 将 tp（UTC时间戳）转换为本地时区的时间
        utc_datetime = today_utc_start + _dt.timedelta(seconds=tp)
        local_datetime = utc_datetime.astimezone(local_tz)
        local_tp = (local_datetime - today_local_start).total_seconds()
        
        # 任务间隔抖动
        task_gap = 0 if is_first else random.uniform(global_interval_cfg["min"], global_interval_cfg["max"])
        is_first = False
        
        # 顺延冲突处理：确保任务不会重叠
        actual_start = max(local_tp, prev_end_time + task_gap, seconds_now_local)
        
        # 超过计划窗口作废
        # 计算计划窗口的本地时区结束时间
        end_of_window_local = None
        if vt_task_days == 0:
            end_of_window_local = seconds_now_local + 86400 * 7
        else:
            end_of_window_local = seconds_now_local + 86400 * vt_task_days
        
        if actual_start >= end_of_window_local:
            break
        
        # 找到在该时间点覆盖的国家（需要使用UTC时间戳）
        countries_at_tp = get_countries_at_utc_sec(tp, country_segments)
        if not countries_at_tp:
            continue
        
        # 从覆盖国家中，选一个剩余配额最多的
        available_countries = []
        for cc in countries_at_tp:
            if country_quota_used.get(cc, 0) < country_quota_target.get(cc, 0):
                available_countries.append(cc)
        
        if not available_countries:
            # 如果覆盖国家的配额用完了，在总池中找配额没用完的
            available_countries = []
            for cc in enabled_countries:
                if country_quota_used.get(cc, 0) < country_quota_target.get(cc, 0):
                    available_countries.append(cc)
        
        if not available_countries:
            continue
        
        selected_country = max(
            available_countries,
            key=lambda cc: (country_quota_target[cc] - country_quota_used[cc], random.random())
        )
        
        country_quota_used[selected_country] += 1
        
        # 计算任务持续时间（所有视频观看时间 + 间隔时间）
        # 使用视频观看时间配置，而不是total_stay配置
        task_duration = 0
        for i in range(vt_watch_count):
            task_duration += random.randint(video_ad_cfg.get("min_watch_time", 30), video_ad_cfg.get("max_watch_time", 60))
            if i < vt_watch_count - 1:
                task_duration += random.randint(interval_cfg["min"], interval_cfg["max"])
        
        task_start = actual_start
        task_end = task_start + task_duration
        prev_end_time = task_end
        
        # 计划时间字符串（本地时区）
        plan_time_str = local_datetime.strftime('%Y-%m-%d %H:%M:%S')
        
        # 为任务随机选择一个视频URL（用于IP使用策略检查）
        target_url = random.choice(vt_video_urls)
        
        tasks.append({
            "idx": len(tasks) + 1,
            "plan_time": plan_time_str,
            "actual_start": int(task_start),
            "actual_end": int(task_end),
            "task_duration": task_duration,
            "proxy_country": selected_country,
            "watch_count": vt_watch_count,
            "status": "未完成",
            "target_url": target_url
        })
    
    # 计算国家任务分布
    country_distribution = {}
    for task in tasks:
        country = task['proxy_country']
        if country in country_distribution:
            country_distribution[country] += 1
        else:
            country_distribution[country] = 1
    
    return {
        "total_tasks": len(tasks),
        "planned_tasks": total_planned_tasks,
        "discarded_tasks": total_planned_tasks - len(tasks),
        "initial_count": total_planned_tasks,
        "discarded_count": total_planned_tasks - len(tasks),
        "discard_reasons": discard_reasons,
        "compensated_count": 0,
        "model_used": chosen_model,
        "tasks": tasks,
        "coverage": coverage,
        "country_distribution": country_distribution,
        "country_quota_target": country_quota_target,
        "warnings": [],
        "daily_capacity": daily_capacity,
        "max_task_duration": max_task_duration
    }


def _split_mixed_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [p.strip() for p in re.split(r"[\n,]+", str(value)) if p.strip()]


def get_video_referer_list(cfg):
    referers = _split_mixed_list(cfg.get("vt_udis_referer", ""))
    return referers or ["https://udisxxx.com/"]


def select_video_referer_for_task(cfg, task_idx=0):
    referers = get_video_referer_list(cfg)
    selected = referers[int(task_idx or 0) % len(referers)]
    parsed = urllib.parse.urlparse(selected)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else selected.rstrip("/")
    return selected, origin


def get_video_entry_urls(cfg):
    urls = _split_mixed_list(cfg.get("vt_video_urls", ""))
    if urls:
        return urls
    target_url = str(cfg.get("target_url", "")).strip()
    return [target_url] if target_url else []


def get_video_layer_values(cfg, layer_no, key):
    video_key = f"vt_layer{layer_no}_{key}"
    values = _split_mixed_list(cfg.get(video_key, []))
    if values:
        return values
    web_layer = cfg.get("web_navigation", {}).get(f"layer_{layer_no}", {})
    return _split_mixed_list(web_layer.get(key, []))


def qa_profile_float(min_value, max_value, profile):
    mn = float(min_value)
    mx = float(max_value)
    if mx < mn:
        mn, mx = mx, mn
    span = mx - mn
    profile = profile if profile in ("light", "standard", "heavy") else "standard"
    if profile == "light":
        lo, hi = mn, mn + span * 0.4
    elif profile == "heavy":
        lo, hi = mn + span * 0.5, mx
    else:
        lo, hi = mn + span * 0.25, mn + span * 0.75
    return random.uniform(lo, hi)


def qa_profile_int(min_value, max_value, profile):
    return max(0, int(round(qa_profile_float(min_value, max_value, profile))))


def qa_human_profile_settings(profile):
    profile = profile if profile in ("light", "standard", "heavy") else "standard"
    settings = {
        "light": {
            "gap": (2.0, 4.0), "scroll": (60, 260), "mouse_steps": (6, 12),
            "clicks": (0, 1), "weights": ["scroll"] * 4 + ["mouse"] * 4 + ["pause"] * 3 + ["key"] * 1 + ["safe_click"] * 1
        },
        "standard": {
            "gap": (1.2, 3.0), "scroll": (80, 420), "mouse_steps": (8, 18),
            "clicks": (1, 2), "weights": ["scroll"] * 5 + ["mouse"] * 4 + ["pause"] * 2 + ["key"] * 1 + ["safe_click"] * 2
        },
        "heavy": {
            "gap": (0.8, 2.2), "scroll": (120, 620), "mouse_steps": (12, 26),
            "clicks": (1, 3), "weights": ["scroll"] * 5 + ["mouse"] * 5 + ["pause"] * 2 + ["key"] * 1 + ["safe_click"] * 3
        },
    }
    return settings[profile]


def qa_safe_random_click(page, current_x, current_y, cfg, stats, label):
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        result = page.evaluate("""
            ({width, height}) => {
                const danger = 'a,button,input,textarea,select,video,[role="button"],.adsbygoogle,ins.adsbygoogle,iframe,[id*="ad" i],[class*="ad" i],[aria-label*="close" i],[aria-label*="skip" i],[title*="close" i],[title*="skip" i],[class*="close" i],[class*="skip" i],[id*="close" i],[id*="skip" i]';
                for (let i = 0; i < 18; i++) {
                    const x = Math.floor(80 + Math.random() * Math.max(120, width - 160));
                    const y = Math.floor(80 + Math.random() * Math.max(120, height - 160));
                    const el = document.elementFromPoint(x, y);
                    if (!el) continue;
                    if (el.closest(danger)) continue;
                    const txt = ((el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '') + '').toLowerCase();
                    if (txt.includes('close') || txt.includes('skip') || txt.includes('关闭') || txt.includes('跳过')) continue;
                    return {ok:true, x, y};
                }
                return {ok:false};
            }
        """, {"width": int(viewport.get("width", 1280)), "height": int(viewport.get("height", 720))})
        if result and result.get("ok"):
            x, y = result["x"], result["y"]
            human_mouse_move(page, current_x, current_y, x, y, cfg)
            page.mouse.click(x, y)
            stats["clicks"] = stats.get("clicks", 0) + 1
            log.debug(f"[{label}] QA安全随机点击 ({x},{y})")
            return x, y
    except Exception as e:
        log.debug(f"[{label}] QA安全随机点击失败: {str(e)[:120]}")
    return current_x, current_y


def video_human_window(page, duration, stats, current_x, current_y, label="视频页面", cfg=None):
    cfg = cfg or {}
    profile = cfg.get("qa_human_profile", "standard")
    profile_cfg = qa_human_profile_settings(profile)
    start = time.time()
    duration = max(0.0, float(duration or 0))
    max_safe_clicks = qa_profile_int(profile_cfg["clicks"][0], profile_cfg["clicks"][1], profile)
    safe_clicks_done = 0
    log.info(f"[{label}] 🎭 QA真人行为窗口启动: {duration:.1f}s，强度={profile}，安全点击上限={max_safe_clicks}")
    while video_task_running and time.time() - start < duration:
        human_model_tick(label)
        if not ensure_human_model_alive():
            break
        remain = duration - (time.time() - start)
        if remain <= 0:
            break
        try:
            action = random.choice(profile_cfg["weights"])
            if action == "scroll":
                dy = qa_profile_int(profile_cfg["scroll"][0], profile_cfg["scroll"][1], profile) * (-1 if random.random() < 0.15 else 1)
                page.evaluate(f"window.scrollBy(0, {dy})")
                stats["scrolls"] = stats.get("scrolls", 0) + 1
                stats["scroll_distance"] = stats.get("scroll_distance", 0) + abs(dy)
            elif action == "mouse":
                viewport = page.viewport_size or {"width": 1280, "height": 720}
                x = random.randint(80, max(120, int(viewport.get("width", 1280)) - 80))
                y = random.randint(80, max(120, int(viewport.get("height", 720)) - 80))
                steps = qa_profile_int(profile_cfg["mouse_steps"][0], profile_cfg["mouse_steps"][1], profile)
                page.mouse.move(x, y, steps=max(4, steps))
                current_x, current_y = x, y
                stats["mouse_moves"] = stats.get("mouse_moves", 0) + 1
            elif action == "safe_click" and safe_clicks_done < max_safe_clicks:
                before_clicks = stats.get("clicks", 0)
                current_x, current_y = qa_safe_random_click(page, current_x, current_y, cfg, stats, label)
                if stats.get("clicks", 0) > before_clicks:
                    safe_clicks_done += 1
            elif action == "key":
                page.keyboard.press(random.choice(["ArrowDown", "PageDown", "ArrowUp"]))
                stats["key_presses"] = stats.get("key_presses", 0) + 1
            else:
                stats["waits"] = stats.get("waits", 0) + 1
        except Exception as e:
            log.debug(f"[{label}] 真人行为动作失败: {str(e)[:120]}")
        sleep_time = min(qa_profile_float(profile_cfg["gap"][0], profile_cfg["gap"][1], profile), max(0.1, duration - (time.time() - start)))
        if not video_interruptible_sleep(sleep_time):
            break
        stats["total_stay"] = stats.get("total_stay", 0) + sleep_time
    log.info(
        f"[{label}] 🎭 QA真人行为窗口结束: 鼠标{stats.get('mouse_moves', 0)}次，"
        f"滚动{stats.get('scrolls', 0)}次({stats.get('scroll_distance', 0)}px)，"
        f"安全点击{stats.get('clicks', 0)}次，键盘{stats.get('key_presses', 0)}次，等待{stats.get('waits', 0)}次"
    )
    return current_x, current_y


def click_video_link_by_keywords_or_fallback(page, keywords, fallback_urls, current_x, current_y, cfg, label, watched_video_urls=None):
    watched_video_urls = watched_video_urls if watched_video_urls is not None else set()
    keywords = _split_mixed_list(keywords)
    fallback_urls = _split_mixed_list(fallback_urls)
    base_url = page_url_safe(page, "")
    random.shuffle(keywords)
    for keyword in keywords:
        kw = keyword.lower()
        candidates = []
        for link in page.query_selector_all("a[href]"):
            try:
                text = (link.text_content() or "").strip()
                href = (link.get_attribute("href") or "").strip()
                title = (link.get_attribute("title") or "").strip()
                haystack = f"{text} {href} {title}".lower()
                target_url = normalize_video_url(urljoin(base_url, href))
                if target_url in watched_video_urls:
                    continue
                if kw and kw in haystack:
                    candidates.append(link)
            except Exception:
                continue
        if candidates:
            target = random.choice(candidates)
            try:
                target.scroll_into_view_if_needed()
                video_human_window(page, random.uniform(1.0, 2.5), {}, current_x, current_y, label, cfg)
                bbox = target.bounding_box()
                if bbox:
                    click_x = bbox["x"] + bbox["width"] / 2
                    click_y = bbox["y"] + bbox["height"] / 2
                    human_mouse_move(page, current_x, current_y, click_x, click_y, cfg)
                    current_x, current_y = click_x, click_y
                target.click()
                page.wait_for_load_state("domcontentloaded", timeout=60000)
                final_url = page_url_safe(page, '')
                watched_video_urls.add(normalize_video_url(final_url))
                log.info(f"[{label}] ✅ 关键词点击成功: {keyword} → {final_url}")
                return True, current_x, current_y
            except Exception as e:
                log.warning(f"[{label}] 关键词点击失败: {keyword}, {str(e)[:160]}")
    available_fallbacks = [urljoin(base_url, u) for u in fallback_urls if normalize_video_url(urljoin(base_url, u)) not in watched_video_urls]
    if available_fallbacks:
        fallback = random.choice(available_fallbacks)
        watched_video_urls.add(normalize_video_url(fallback))
        log.info(f"[{label}] 使用兜底链接: {fallback}")
        page.goto(fallback, timeout=30000, wait_until="domcontentloaded")
        return True, current_x, current_y
    log.warning(f"[{label}] 未匹配关键词且无兜底链接")
    return False, current_x, current_y


def is_embedded_video_src(src):
    if not src:
        return False
    src_l = str(src).lower()
    return bool(
        is_video_direct_entry(src_l) or
        "embed" in src_l or
        "/e/" in src_l or
        src_l.endswith((".mp4", ".m3u8", ".webm", ".mov", ".flv"))
    )


def collect_embedded_video_targets(page, watched_video_urls):
    targets = []
    try:
        raw = page.evaluate("""
            () => {
                const items = [];
                document.querySelectorAll('iframe[src]').forEach((el, index) => {
                    const rect = el.getBoundingClientRect();
                    items.push({kind:'iframe', index, src: el.src || el.getAttribute('src') || '', width: rect.width, height: rect.height});
                });
                document.querySelectorAll('video').forEach((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const src = el.currentSrc || el.src || (el.querySelector('source[src]') && el.querySelector('source[src]').src) || '';
                    items.push({kind:'video', index, src, width: rect.width, height: rect.height});
                });
                return items;
            }
        """)
        for item in raw or []:
            src = item.get("src") or f"inline-video-{item.get('index')}"
            key = normalize_video_url(src)
            if key in watched_video_urls:
                continue
            if item.get("kind") == "video" or is_embedded_video_src(src):
                if float(item.get("width") or 0) >= 80 and float(item.get("height") or 0) >= 60:
                    targets.append({**item, "key": key})
    except Exception as e:
        log.debug(f"[Layer2嵌入视频] 扫描失败: {str(e)[:120]}")
    return targets


def play_embedded_video_target(page, target):
    try:
        if target.get("kind") == "video":
            videos = page.query_selector_all("video")
            idx = int(target.get("index") or 0)
            if idx < len(videos):
                videos[idx].scroll_into_view_if_needed()
                videos[idx].evaluate("""el => {
                    el.muted = false;
                    el.volume = Math.max(0.2, Math.min(0.8, Math.random()));
                    return el.play && el.play().catch(() => null);
                }""")
                return True
        if target.get("kind") == "iframe":
            iframes = page.query_selector_all("iframe[src]")
            idx = int(target.get("index") or 0)
            src = target.get("src") or ""
            if idx < len(iframes):
                iframes[idx].scroll_into_view_if_needed()
            for frame in page.frames:
                if src and (src in frame.url or normalize_video_url(src) in normalize_video_url(frame.url)):
                    frame.evaluate("""() => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = false;
                            v.volume = Math.max(0.2, Math.min(0.8, Math.random()));
                            if (v.play) v.play().catch(() => null);
                        });
                    }""")
                    return True
    except Exception as e:
        log.debug(f"[Layer2嵌入视频] 尝试播放失败: {str(e)[:120]}")
    return False


def watch_layer2_embedded_videos(page, cfg, current_x, current_y, total_stats, watched_video_urls, watch_count):
    targets = collect_embedded_video_targets(page, watched_video_urls)
    if len(targets) < watch_count:
        raise RuntimeError(f"[Layer2嵌入视频] 可用嵌入视频数量不足：需要{watch_count}个，实际{len(targets)}个，终止本任务")
    random.shuffle(targets)
    selected = targets[:watch_count]
    for idx, target in enumerate(selected):
        if not video_task_running:
            break
        if idx > 0:
            interval_min = float(cfg.get("vt_interval_min", 5) or 5)
            interval_max = float(cfg.get("vt_interval_max", 15) or 15)
            if interval_max < interval_min:
                interval_min, interval_max = interval_max, interval_min
            wait_time = random.uniform(interval_min, interval_max)
            log.info(f"[Layer2嵌入视频] 视频间隔等待 {wait_time:.1f}s")
            current_x, current_y = video_human_window(page, wait_time, total_stats, current_x, current_y, "iframe视频间隔", cfg)
        watched_video_urls.add(target["key"])
        log.info(f"[Layer2嵌入视频] 当前页观看 {idx + 1}/{watch_count}: kind={target.get('kind')} src={target.get('src', '')[:160]}")
        played = play_embedded_video_target(page, target)
        log.info(f"[Layer2嵌入视频] 播放尝试结果: {'成功或已触发' if played else '不可直接控制，按父页面停留观看'}")
        duration_min = float(cfg.get("vt_duration_min", 30) or 30)
        duration_max = float(cfg.get("vt_duration_max", 120) or 120)
        if duration_max < duration_min:
            duration_min, duration_max = duration_max, duration_min
        watch_time = random.uniform(duration_min, duration_max)
        current_x, current_y = video_human_window(page, watch_time, total_stats, current_x, current_y, "Layer2嵌入视频观看", cfg)
        total_stats["total_stay"] = total_stats.get("total_stay", 0) + watch_time
    return total_stats, current_x, current_y


def count_available_video_targets(page, keywords, fallback_urls, watched_video_urls):
    base_url = page_url_safe(page, "")
    keywords = [k.lower() for k in _split_mixed_list(keywords) if k]
    candidates = set()
    try:
        for link in page.query_selector_all("a[href]"):
            text = (link.text_content() or "").strip()
            href = (link.get_attribute("href") or "").strip()
            title = (link.get_attribute("title") or "").strip()
            haystack = f"{text} {href} {title}".lower()
            if keywords and not any(kw in haystack for kw in keywords):
                continue
            target_url = normalize_video_url(urljoin(base_url, href))
            if target_url and target_url not in watched_video_urls:
                candidates.add(target_url)
    except Exception as e:
        log.debug(f"[视频流程] 统计Layer视频候选失败: {str(e)[:120]}")
    for fallback in _split_mixed_list(fallback_urls):
        target_url = normalize_video_url(urljoin(base_url, fallback))
        if target_url and target_url not in watched_video_urls:
            candidates.add(target_url)
    return len(candidates)


def watch_current_video_page(page, cfg, current_x, current_y):
    stats = {
        "mouse_moves": 0, "scrolls": 0, "scroll_distance": 0,
        "clicks": 0, "key_presses": 0, "focus_switches": 0,
        "refreshes": 0, "total_stay": 0, "waits": 0
    }
    duration_min = float(cfg.get("vt_duration_min", 30) or 30)
    duration_max = float(cfg.get("vt_duration_max", 120) or 120)
    if duration_max < duration_min:
        duration_min, duration_max = duration_max, duration_min
    watch_time = random.uniform(duration_min, duration_max)
    speed_min = float(cfg.get("vt_speed_min", 1) or 1)
    speed_max = float(cfg.get("vt_speed_max", 1) or 1)
    if speed_max < speed_min:
        speed_min, speed_max = speed_max, speed_min
    playback_rate = random.uniform(speed_min, speed_max)
    try:
        result = page.evaluate("""
            (rate) => {
                const video = document.querySelector('video');
                if (!video) return {hasVideo:false};
                video.muted = true;
                video.playsInline = true;
                video.playbackRate = rate;
                const p = video.play();
                return {hasVideo:true, paused: video.paused, currentTime: video.currentTime, playbackRate: video.playbackRate};
            }
        """, playback_rate)
        log.info(f"[视频观看] video元素状态: {result}, 倍速={playback_rate:.2f}")
    except Exception as e:
        log.warning(f"[视频观看] 设置播放/倍速失败，继续停留: {str(e)[:160]}")
    current_x, current_y = video_human_window(page, watch_time, stats, current_x, current_y, "视频观看", cfg)
    return watch_time, current_x, current_y, stats


def is_video_direct_entry(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    path = urllib.parse.urlparse(url or "").path.lower()
    return bool(
        ("vids.st" in host and path.startswith("/v/")) or
        is_udis_video_url(url) or
        path.endswith((".mp4", ".m3u8", ".webm", ".mov", ".flv"))
    )


def normalize_video_url(url):
    parsed = urllib.parse.urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip().rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", parsed.query, ""))


def unique_preserve_order(urls):
    seen = set()
    result = []
    for url in urls:
        key = normalize_video_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def random_task_watch_count(cfg):
    max_count = max(1, int(cfg.get("vt_watch_count", 1) or 1))
    return random.randint(1, max_count)


def run_video_navigation_flow(page, cfg, current_x, current_y):
    entry_urls = get_video_entry_urls(cfg)
    if not entry_urls:
        raise RuntimeError("视频入口URL池为空，且网站流量目标URL也为空")
    total_stats = {
        "mouse_moves": 0, "scrolls": 0, "scroll_distance": 0,
        "clicks": 0, "key_presses": 0, "focus_switches": 0,
        "refreshes": 0, "total_stay": 0, "waits": 0
    }
    entry_mode = str(cfg.get("vt_entry_mode", "auto") or "auto").lower()
    if entry_mode not in ("auto", "direct", "layer"):
        entry_mode = "auto"
    direct_entries = unique_preserve_order([u for u in entry_urls if is_video_direct_entry(u)])
    watched_video_urls = set()
    log.info(f"[视频流程] 入口模式={entry_mode}，入口URL数量={len(entry_urls)}，识别直链数量={len(direct_entries)}")
    if entry_mode == "direct" and not direct_entries:
        raise RuntimeError("[视频流程] 当前为视频直链模式，但入口URL池未识别到视频直链")
    if entry_mode == "direct" or (entry_mode == "auto" and direct_entries):
        watch_count = random_task_watch_count(cfg)
        if len(direct_entries) < watch_count:
            raise RuntimeError(f"[视频流程] 可用直链视频数量不足：需要{watch_count}个，实际{len(direct_entries)}个，终止本任务")
        log.info(f"[视频流程] 检测到视频直链模式，跳过Layer1/Layer2导航，直链数量={len(direct_entries)}，本任务随机观看={watch_count}")
        selected_urls = direct_entries[:]
        random.shuffle(selected_urls)
        success_count = 0
        failed_urls = []
        for video_url in selected_urls:
            if not video_task_running or success_count >= watch_count:
                break
            if success_count > 0:
                interval_min = float(cfg.get("vt_interval_min", 5) or 5)
                interval_max = float(cfg.get("vt_interval_max", 15) or 15)
                if interval_max < interval_min:
                    interval_min, interval_max = interval_max, interval_min
                wait_time = random.uniform(interval_min, interval_max)
                log.info(f"[视频流程] 直链视频间隔等待 {wait_time:.1f}s")
                current_x, current_y = video_human_window(page, wait_time, total_stats, current_x, current_y, "视频间隔", cfg)
            log.info(f"[视频流程] 打开视频直链 {success_count + 1}/{watch_count}: {video_url}")
            try:
                page.goto(video_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                failed_urls.append(video_url)
                log.warning(f"[视频流程] 直链打开失败，跳过当前视频并尝试下一个: {video_url}，原因={type(e).__name__}: {str(e)[:180]}")
                continue
            watched_video_urls.add(normalize_video_url(video_url))
            current_x, current_y = video_human_window(page, random.uniform(1, 3), total_stats, current_x, current_y, "视频直链页", cfg)
            watch_time, current_x, current_y, one_stats = watch_current_video_page(page, cfg, current_x, current_y)
            for key, value in one_stats.items():
                total_stats[key] = total_stats.get(key, 0) + value
            total_stats["total_stay"] += watch_time
            success_count += 1
        if success_count < watch_count:
            raise RuntimeError(f"[视频流程] 可成功打开的视频直链数量不足：需要{watch_count}个，成功{success_count}个，失败{len(failed_urls)}个，终止本任务")
        return total_stats, current_x, current_y

    layer_entry_urls = [u for u in entry_urls if not is_video_direct_entry(u)] if entry_mode == "layer" else entry_urls
    if entry_mode == "layer" and not layer_entry_urls:
        raise RuntimeError("[视频流程] 当前为Layer导航模式，但入口URL池没有可用入口页")
    entry_url = random.choice(layer_entry_urls)
    log.info(f"[视频流程] 进入Layer1入口: {entry_url}")
    page.goto(entry_url, timeout=30000, wait_until="domcontentloaded")
    current_x, current_y = video_human_window(page, random.uniform(2, 5), total_stats, current_x, current_y, "Layer1入口", cfg)
    ok, current_x, current_y = click_video_link_by_keywords_or_fallback(
        page,
        get_video_layer_values(cfg, 1, "keywords"),
        get_video_layer_values(cfg, 1, "fallback_urls"),
        current_x,
        current_y,
        cfg,
        "Layer1→Layer2",
    )
    if not ok:
        raise RuntimeError("Layer1 跳转 Layer2 失败")
    layer2_url = page_url_safe(page, "")
    current_x, current_y = video_human_window(page, random.uniform(2, 5), total_stats, current_x, current_y, "Layer2页面", cfg)
    watch_count = random_task_watch_count(cfg)
    layer2_video_mode = str(cfg.get("vt_layer2_video_mode", "auto") or "auto").lower()
    if layer2_video_mode not in ("auto", "link", "iframe"):
        layer2_video_mode = "auto"
    embedded_count = len(collect_embedded_video_targets(page, watched_video_urls))
    log.info(f"[视频流程] Layer2视频模式={layer2_video_mode}，本任务随机观看数={watch_count}，当前页嵌入视频数={embedded_count}")
    if layer2_video_mode == "iframe":
        return watch_layer2_embedded_videos(page, cfg, current_x, current_y, total_stats, watched_video_urls, watch_count)
    if layer2_video_mode == "auto" and embedded_count >= watch_count:
        log.info("[视频流程] 自动识别选择 iframe嵌入模式")
        return watch_layer2_embedded_videos(page, cfg, current_x, current_y, total_stats, watched_video_urls, watch_count)
    if layer2_video_mode == "auto" and embedded_count > 0:
        log.info("[视频流程] 嵌入视频数量不足本任务观看数，自动切换为链接跳转模式")
    layer2_keywords = get_video_layer_values(cfg, 2, "keywords")
    layer2_fallbacks = get_video_layer_values(cfg, 2, "fallback_urls")
    available_count = count_available_video_targets(page, layer2_keywords, layer2_fallbacks, watched_video_urls)
    if available_count < watch_count:
        raise RuntimeError(f"[视频流程] Layer2可用未观看视频数量不足：需要{watch_count}个，实际{available_count}个，终止本任务")
    log.info(f"[视频流程] 链接跳转模式：Layer2可用未观看视频数={available_count}")
    for idx in range(watch_count):
        if not video_task_running:
            break
        if idx > 0:
            interval_min = float(cfg.get("vt_interval_min", 5) or 5)
            interval_max = float(cfg.get("vt_interval_max", 15) or 15)
            if interval_max < interval_min:
                interval_min, interval_max = interval_max, interval_min
            wait_time = random.uniform(interval_min, interval_max)
            log.info(f"[视频流程] 视频间隔等待 {wait_time:.1f}s，随后返回Layer2")
            current_x, current_y = video_human_window(page, wait_time, total_stats, current_x, current_y, "视频间隔", cfg)
            page.goto(layer2_url, timeout=30000, wait_until="domcontentloaded")
            current_x, current_y = video_human_window(page, random.uniform(1, 3), total_stats, current_x, current_y, "返回Layer2", cfg)
        ok, current_x, current_y = click_video_link_by_keywords_or_fallback(
            page,
            layer2_keywords,
            layer2_fallbacks,
            current_x,
            current_y,
            cfg,
            f"Layer2→视频页 {idx + 1}/{watch_count}",
            watched_video_urls,
        )
        if not ok:
            raise RuntimeError(f"Layer2 跳转视频页失败: {idx + 1}/{watch_count}")
        current_x, current_y = video_human_window(page, random.uniform(1, 3), total_stats, current_x, current_y, "最终视频页", cfg)
        watch_time, current_x, current_y, one_stats = watch_current_video_page(page, cfg, current_x, current_y)
        for key, value in one_stats.items():
            total_stats[key] = total_stats.get(key, 0) + value
        total_stats["total_stay"] += watch_time
    return total_stats, current_x, current_y


def generate_video_daily_tasks(cfg):
    """生成视频任务计划：本机时间分布，不考虑代理国家。"""
    import datetime as _dt
    vt_watch_count = max(1, int(cfg.get('vt_watch_count', 3) or 3))
    vt_task_days = int(cfg.get('vt_task_days', 1) or 1)
    plan_days = 7 if vt_task_days == 0 else max(1, vt_task_days)
    duration_min = float(cfg.get('vt_duration_min', 30) or 30)
    duration_max = float(cfg.get('vt_duration_max', 120) or 120)
    if duration_max < duration_min:
        duration_min, duration_max = duration_max, duration_min
    interval_min = float(cfg.get('vt_interval_min', 5) or 5)
    interval_max = float(cfg.get('vt_interval_max', 15) or 15)
    if interval_max < interval_min:
        interval_min, interval_max = interval_max, interval_min
    global_interval_cfg = cfg.get("vt_global_interval", {"min": 10, "max": 30})
    global_min = float(global_interval_cfg.get("min", 10) or 10)
    global_max = float(global_interval_cfg.get("max", 30) or 30)
    if global_max < global_min:
        global_min, global_max = global_max, global_min
    max_task_duration = int(vt_watch_count * duration_max + max(0, vt_watch_count - 1) * interval_max)
    avg_global_interval = (global_min + global_max) / 2
    daily_capacity = max(1, int(86400 / max(1, max_task_duration + avg_global_interval)))
    total_planned_tasks = daily_capacity * plan_days
    now = _dt.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_now = (now - today_start).total_seconds()
    window_end = seconds_now + 86400 * plan_days
    selected_models = [m for m in cfg.get("selected_models", ["normal", "gamma", "bimodal", "poisson"]) if m in MODEL_FUNCTIONS]
    chosen_model = random.choice(selected_models) if selected_models else "simple_video_local"
    raw_points = []
    if selected_models:
        for h in MODEL_FUNCTIONS[chosen_model](total_planned_tasks):
            tp = seconds_now + h * 3600
            if seconds_now <= tp < window_end:
                raw_points.append(tp)
    if not raw_points:
        avg_interval = (window_end - seconds_now) / max(1, total_planned_tasks)
        raw_points = [seconds_now + i * avg_interval for i in range(total_planned_tasks)]
    raw_points.sort()
    tasks = []
    prev_end = seconds_now
    for tp in raw_points:
        task_gap = 0 if not tasks else random.uniform(global_min, global_max)
        actual_start = max(tp, prev_end + task_gap, seconds_now)
        if actual_start >= window_end:
            break
        task_duration = 0
        for i in range(vt_watch_count):
            task_duration += random.uniform(duration_min, duration_max)
            if i < vt_watch_count - 1:
                task_duration += random.uniform(interval_min, interval_max)
        actual_end = actual_start + task_duration
        if actual_end >= window_end:
            break
        prev_end = actual_end
        plan_dt = today_start + _dt.timedelta(seconds=actual_start)
        tasks.append({
            "idx": len(tasks) + 1,
            "plan_time": plan_dt.strftime('%Y-%m-%d %H:%M:%S'),
            "actual_start": int(actual_start),
            "actual_end": int(actual_end),
            "task_duration": int(task_duration),
            "proxy_country": "ADSL",
            "ip_mode": "adsl",
            "watch_count": vt_watch_count,
            "status": "未完成",
            "target_url": random.choice(get_video_entry_urls(cfg)) if get_video_entry_urls(cfg) else str(cfg.get("target_url", ""))
        })
    return {
        "total_tasks": len(tasks),
        "planned_tasks": total_planned_tasks,
        "discarded_tasks": max(0, total_planned_tasks - len(tasks)),
        "initial_count": total_planned_tasks,
        "discarded_count": max(0, total_planned_tasks - len(tasks)),
        "discard_reasons": {},
        "compensated_count": 0,
        "model_used": chosen_model,
        "tasks": tasks,
        "coverage": {"coverage_pct": 100, "covered_segments": [(seconds_now, window_end)], "uncovered_segments": [], "country_segments": {"ADSL": [(seconds_now, window_end)]}},
        "country_distribution": {"ADSL": len(tasks)},
        "country_quota_target": {"ADSL": len(tasks)},
        "warnings": ["视频流量只允许拨号VPS/ADSL，计划已固定为ADSL模式"],
        "daily_capacity": daily_capacity,
        "max_task_duration": max_task_duration
    }


def create_unified_qa_plan(cfg, count=1, adsl=False):
    """创建综合QA任务计划：网站浏览QA + 广告曝光检测 + 视频检测。"""
    adsl = True
    import datetime as _dt
    now_local = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    count = max(1, min(999, int(count or 1)))
    tasks = []
    for idx in range(1, count + 1):
        tasks.append({
            "idx": idx,
            "plan_time": now_local,
            "actual_start": 0,
            "actual_end": 0,
            "task_duration": 0,
            "target_url": cfg.get('target_url', ''),
            "proxy_country": "ADSL" if adsl else "DIRECT",
            "ip_mode": "adsl" if adsl else "direct",
            "task_type": "unified_qa",
            "status": "未完成"
        })
    return {
        "total_tasks": count,
        "planned_tasks": count,
        "model_used": "unified_adsl_qa" if adsl else "unified_qa",
        "tasks": tasks,
        "country_distribution": {"ADSL" if adsl else "DIRECT": count},
        "warnings": []
    }


def create_video_adsl_plan(cfg, adsl_count):
    """创建视频 ADSL 任务计划：只控制次数，后续视频流程复用原视频任务。"""
    import datetime as _dt
    now_local = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    watch_count = int(cfg.get('vt_watch_count', 3) or 3)
    duration_min = float(cfg.get('vt_duration_min', 30) or 30)
    duration_max = float(cfg.get('vt_duration_max', 120) or 120)
    interval_max = float(cfg.get('vt_interval_max', 15) or 15)
    if duration_max < duration_min:
        duration_min, duration_max = duration_max, duration_min
    tasks = []
    for idx in range(1, adsl_count + 1):
        task_duration = watch_count * random.uniform(duration_min, duration_max) + max(0, watch_count - 1) * interval_max
        tasks.append({
            "idx": idx,
            "plan_time": now_local,
            "actual_start": 0,
            "actual_end": int(task_duration),
            "task_duration": int(task_duration),
            "target_url": cfg.get('vt_video_urls', '').split(',')[0].strip() if cfg.get('vt_video_urls') else '',
            "proxy_country": "ADSL",
            "ip_mode": "adsl",
            "status": "未完成"
        })
    return {
        "total_tasks": adsl_count,
        "planned_tasks": adsl_count,
        "model_used": "video_adsl_task",
        "tasks": tasks,
        "country_distribution": {"ADSL": adsl_count},
        "warnings": []
    }


def run_website_qa_segment(page, cfg, current_x, current_y, selected_engine_id=None, selected_keyword=None):
    """综合QA中的网站浏览与广告曝光检测段：支持真搜索跳转。"""
    stats_local = {
        "mouse_moves": 0, "scrolls": 0, "scroll_distance": 0,
        "clicks": 0, "key_presses": 0, "focus_switches": 0,
        "refreshes": 0, "total_stay": 0, "waits": 0
    }
    target_url = str(cfg.get("target_url", "")).strip()
    if not target_url:
        log.warning("[综合QA-网站] target_url为空，跳过网站浏览QA")
        return stats_local, current_x, current_y
    log.info(f"[综合QA-网站] 准备访问目标页: {target_url}")
    
    # ========== 真搜索跳转（新增） ==========
    already_on_target = False
    search_mode = cfg.get("seo", {}).get("search_mode", "direct_referer")
    if search_mode == "real_search" and selected_engine_id and selected_engine_id in ["google", "bing"] and selected_keyword:
        log.info("[综合QA-网站] 执行真搜索跳转流程")
        search_success, current_x, current_y = perform_real_search(page, target_url, selected_engine_id, selected_keyword, stats_local, current_x, current_y, cfg)
        if search_success:
            already_on_target = True
            log.info(f"[综合QA-网站] 真搜索跳转成功，已在目标页")
    # =========================================
    
    if not already_on_target:
        log.info(f"[综合QA-网站] 直接导航至目标页: {target_url}")
        page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
    ad_monitor = create_ad_monitor()
    ad_monitor = scan_ads_during_task(page, ad_monitor, "综合QA-首页加载后")
    stay_cfg = cfg.get("total_stay", {"min": 120, "max": 300})
    stay_min = float(stay_cfg.get("min", 120) or 120)
    stay_max = float(stay_cfg.get("max", 300) or 300)
    if stay_max < stay_min:
        stay_min, stay_max = stay_max, stay_min
    stay_time = random.uniform(stay_min, stay_max)
    # simulate_human_in_window 使用 task_running；综合QA复用视频停止信号，因此这里用 video_human_window 保持可中断。
    current_x, current_y = video_human_window(page, stay_time, stats_local, current_x, current_y, "综合QA网站浏览", cfg)
    ad_monitor = scan_ads_during_task(page, ad_monitor, "综合QA-首页停留后")

    # ========== 层级浏览（首页→layer_2→...→layer_6→返回首页），逻辑与网站流量主任务一致（轻量版） ==========
    web_config = cfg.get("web_navigation", {}) or {}
    loop_cfg = web_config.get("loop_count", {"min": 1, "max": 3})
    interval_cfg = web_config.get("loop_interval", {"min": 1, "max": 5})
    back_links = web_config.get("back_links", []) or []
    back_home_links = web_config.get("back_home_links", []) or []
    # 随机循环次数（与主任务相同逻辑）
    chapter_loop_count = max(1, random.randint(int(loop_cfg.get("min", 1)), int(loop_cfg.get("max", 1))))
    loop_interval = max(0.0, random.uniform(float(interval_cfg.get("min", 1)), float(interval_cfg.get("max", 1))))
    if chapter_loop_count <= 1:
        loop_interval = 0.0
    # 读取 6 层关键词/兜底配置
    layers = []
    for li in range(1, 7):
        lc = web_config.get(f"layer_{li}", {}) or {}
        layers.append({"idx": li, "keywords": lc.get("keywords", []) or [], "fallback_urls": lc.get("fallback_urls", []) or []})

    # 每层停留时间（沿用首页停留区间，轻量）
    def _qa_layer_stay():
        return random.uniform(max(3.0, stay_min * 0.4), max(6.0, stay_max * 0.5))

    # 第一跳：首页 → layer_2（列表页）
    layer1 = layers[0]
    success_list, current_x, current_y = click_link_with_fallback(
        page, layer1["keywords"], layer1["fallback_urls"], current_x, current_y, cfg
    )
    if success_list:
        stats_local["clicks"] += 1
        stats_local["mouse_moves"] += 1
        ls = _qa_layer_stay()
        log.info(f"[综合QA-层级] 列表页(layer_2)停留 {ls:.1f}秒")
        current_x, current_y = video_human_window(page, ls, stats_local, current_x or 300, current_y or 300, "综合QA-layer_2", cfg)
        ad_monitor = scan_ads_during_task(page, ad_monitor, "综合QA-layer_2停留后")

        # 循环深入 layer_3..layer_6 并返回首页（与主任务一致）
        for loop_idx in range(chapter_loop_count):
            if not video_task_running:
                break
            log.info(f"[综合QA-层级] 浏览循环 第 {loop_idx+1}/{chapter_loop_count} 次")
            for level_idx in range(2, 6):
                if not video_task_running:
                    break
                tl = layers[level_idx]
                if not (tl["keywords"] or tl["fallback_urls"]):
                    log.info(f"[综合QA-层级] layer_{level_idx+1} 未配置关键字/兜底URL，停止深入")
                    break
                log.info(f"[综合QA-层级] → 进入 layer_{level_idx+1}")
                ok, current_x, current_y = click_link_with_fallback(
                    page, tl["keywords"], tl["fallback_urls"], current_x, current_y, cfg
                )
                if not ok:
                    log.warning(f"[综合QA-层级] 进入 layer_{level_idx+1} 失败，停止深入")
                    break
                stats_local["clicks"] += 1
                stats_local["mouse_moves"] += 1
                ls = _qa_layer_stay()
                log.info(f"[综合QA-层级] layer_{level_idx+1} 停留 {ls:.1f}秒")
                current_x, current_y = video_human_window(page, ls, stats_local, current_x or 300, current_y or 300, f"综合QA-layer_{level_idx+1}", cfg)
                ad_monitor = scan_ads_during_task(page, ad_monitor, f"综合QA-layer_{level_idx+1}停留后")
            # 返回首页
            if not video_task_running:
                break
            log.info("[综合QA-层级] → 返回首页")
            ok_back, current_x, current_y = click_link_with_fallback(
                page, list(back_links) + list(back_home_links), [], current_x, current_y, cfg, final_fallback_url=target_url
            )
            if ok_back:
                stats_local["clicks"] += 1
                stats_local["mouse_moves"] += 1
            ad_monitor = scan_ads_during_task(page, ad_monitor, f"综合QA-第{loop_idx+1}轮返回首页后")
            if loop_idx < chapter_loop_count - 1 and loop_interval > 0:
                log.info(f"[综合QA-层级] ⏸ 每轮间隔 {loop_interval:.1f}秒")
                time.sleep(loop_interval)
    else:
        log.warning("[综合QA-层级] 进入列表页失败，跳过层级浏览")

    ad_monitor = scan_ads_during_task(page, ad_monitor, "综合QA-网站停留后")
    log.info(
        f"[综合QA-广告] 容器={len(ad_monitor.get('containers', set()))}，"
        f"可见={len(ad_monitor.get('visible', set()))}，曝光={len(ad_monitor.get('exposed', set()))}，"
        f"扫描={ad_monitor.get('scan_count', 0)}次"
    )
    return stats_local, current_x, current_y


def run_video_tasks(adsl_ip_task=False, unified_qa=False):
    """视频/综合QA任务执行函数 - 完整流程版"""
    global video_task_running, video_worker_active, current_video_task_idx, config, video_adsl_status
    # 初始化 SEO 查询实例
    import sys, os
    print("Current directory:", os.getcwd())
    print("sys.path:", sys.path)
    from seo_query_module import get_seo_query
    seo_query = get_seo_query()
    # === 任务标签：QA任务 / 视频任务 动态切换 ===
    _task_label = "QA任务" if unified_qa else "视频任务"
    _browser_label = "QA浏览器" if unified_qa else "视频浏览器"
    _task_icon = "🧪" if unified_qa else "🎬"
    video_task_running = True
    video_worker_active = True
    current_video_task_idx = -1  # 重置当前任务索引
    if adsl_ip_task:
        video_adsl_status.update({
            "running": True,
            "status": "准备中",
            "total": max(1, min(999, int(config.get("vt_adsl_task_count", 1) or 1))),
            "completed": 0,
            "current": 0,
            "current_ip": "",
            "country": "",
            "last_error": ""
        })
    
    try:
        if not adsl_ip_task:
            log.warning("[视频/QA] 非ADSL入口已废除，强制切换为 ADSL 拨号VPS模式")
            adsl_ip_task = True
        start_human_model("unified_qa_adsl" if unified_qa else "video_adsl")
        log.info(f"{_task_icon} {_task_label}已启动（仅ADSL拨号VPS模式）")
        
        # ========== Step A: 重新加载配置文件（确保使用最新配置，包括headless模式）
        try:
            import json
            with open('config.json', 'r') as f:
                loaded_config = json.load(f)
                config.update(loaded_config)
                log.info("✅ 配置文件已重新加载")
                log.info(f"📋 当前headless模式: {config.get('headless', True)}")
        except Exception as e:
            log.warning(f"⚠️ 重新加载配置文件失败，使用当前内存配置: {str(e)}")
        
        # ========== Step B: 获取任务计划
        global video_plan
        if unified_qa:
            qa_count = max(1, min(999, int(config.get("vt_adsl_task_count", 1) or 1)))
            video_plan = create_unified_qa_plan(config, qa_count, adsl=True)
            log.info(f"[综合QA] 已创建ADSL综合QA任务计划: {qa_count} 次")
        elif adsl_ip_task:
            adsl_count = max(1, min(999, int(config.get("vt_adsl_task_count", 1) or 1)))
            video_plan = create_video_adsl_plan(config, adsl_count)
            log.info(f"[视频ADSL] 已创建 ADSL 视频任务计划: {adsl_count} 次")
        elif video_plan:
            log.info(f"📋 使用已生成的任务计划，包含 {video_plan['total_tasks']} 个任务")
        else:
            log.info("📋 没有找到已生成的任务计划，正在生成新的任务计划")
            video_plan = generate_video_daily_tasks(config)
        
        total_tasks = video_plan["total_tasks"]
        tasks_list = video_plan["tasks"]
        
        log.info(f"📊 任务清单加载完成，共 {len(tasks_list)} 个任务")
        
        if not tasks_list:
            log.warning("⚠️ 任务清单为空，无法执行任务")
            return
        
        log.info("📍 视频/QA流量只允许拨号VPS/ADSL，不走普通直连、IPDeep或代理API")
        
        # ========== Step B: 使用 Selenium 浏览器执行任务 ==========
        log.info("🔧 初始化 Selenium Chrome 环境...")
        with sync_playwright() as p:
            log.info("✅ Selenium Chrome 环境初始化成功")
            
            for task_idx, task in enumerate(tasks_list):
                if not video_task_running:
                    log.warning(f"⛔ {_task_label}已停止（任务清单遍历中）")
                    break
                
                # 更新当前任务索引（用于前端实时显示）
                current_video_task_idx = task_idx
                task['status'] = "执行中"
                task['start_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                
                # 任务分隔线 —— 清晰区分不同任务
                log.task_separator(task_idx + 1, len(tasks_list))
                log.info(f"{_task_icon} {_task_label}#{task_idx+1}/{len(tasks_list)} 开始")
                
                # ========== Step B1: 确认视频IP模式 ==========
                current_task = dict(task)
                is_adsl_task = True
                current_task["ip_mode"] = "adsl"
                current_task['proxy_api_url'] = ""
                current_task['proxy_user'] = ""
                current_task['proxy_pwd'] = ""
                current_task['proxy_country'] = "ADSL"
                log.info("\n🎯 视频IP模式: ADSL重拨后本机直连，不走IPDeep/代理池/6666/1666/普通直连")
                
                # ========== Step B2: 计算等待时间（严格按照计划时间执行） ==========
                import datetime as _dt
                import pytz
                
                # 获取本地时区
                local_tz = pytz.timezone('Asia/Shanghai')
                
                # 获取当前本地时间
                _now_local = _dt.datetime.now(local_tz)
                _today_local_start = _now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                _now_sec_local = (_now_local - _today_local_start).total_seconds()
                
                _actual_start_sec = current_task.get("actual_start", 0)
                _hh = int(_actual_start_sec // 3600)
                _mm = int((_actual_start_sec % 3600) // 60)
                _ss = int(_actual_start_sec % 60)
                _start_str = f"{_hh:02d}:{_mm:02d}:{_ss:02d}"
                
                log.info(
                    f"📌 当前任务: {current_task['idx']}/{total_tasks}, "
                    f"计划开始时间={_start_str}, "
                    f"预估时长={current_task.get('task_duration', 0):.1f}s, "
                    f"代理国家={current_task['proxy_country']}"
                )
                
                wait_sec = 0
                if task_idx == 0:
                    wait_sec = max(0, _actual_start_sec - _now_sec_local)
                else:
                    interval_min = config.get("vt_global_interval", {}).get("min", 10)
                    interval_max = config.get("vt_global_interval", {}).get("max", 30)
                    wait_sec = random.uniform(interval_min, interval_max)
                
                # 将当前本地秒数转换为可读时间
                _now_hh = int(_now_sec_local // 3600)
                _now_mm = int((_now_sec_local % 3600) // 60)
                _now_ss = int(_now_sec_local % 60)
                _now_str = f"{_now_hh:02d}:{_now_mm:02d}:{_now_ss:02d}"
                
                log.info(f"⏰ 当前本地时间: {_now_str}, 计划开始时间: {_start_str}, 需要等待: {wait_sec:.1f}秒")
                
                if wait_sec > 0:
                    log.info(f"⏳ 等待 {wait_sec:.1f} 秒后开始任务...")
                    if not video_interruptible_sleep(wait_sec):
                        log.warning("⛔ 任务已停止（等待中）")
                        break
                
                # ========== Step B3: 获取动态IP并验证 ==========
                log.info("🔌 开始前置流程：获取动态IP并验证...")
                browser = None
                try:
                    proxy_info = None
                    exit_ip = "未知"
                    country = "未知"
                    timezone = "Etc/UTC"
                    language = "en-US"
                    cc_upper = ""
                    
                    ip_retry_max = 3
                    fingerprint_retry_max = max(1, int(config.get("adsl_ip_redial_max_attempts", 10) or 10))
                    resolved_ip_info = None
                    for ip_attempt in range(ip_retry_max):
                        if not video_task_running:
                            log.warning("⛔ 任务已停止（IP获取中）")
                            break
                        try:
                            video_adsl_status["current"] = task_idx + 1
                            video_adsl_status["status"] = "重拨取IP"
                            exit_ip, resolved_ip_info = redial_adsl_and_get_ip(sleep_func=video_interruptible_sleep, status_obj=video_adsl_status)

                            if not isinstance(resolved_ip_info, dict):
                                resolved_ip_info = {}
                            resolved_ip_info["ip"] = exit_ip
                            valid_ip_info, invalid_reason = _validate_resolved_ip_info(exit_ip, resolved_ip_info)
                            if not valid_ip_info:
                                log.warning(f"视频出口IP {exit_ip} 三要素硬校验失败: {invalid_reason}，废弃本次IP")
                                continue
                            sync_process_timezone_to_ip(resolved_ip_info)

                            ip_info = dict(resolved_ip_info)
                            proxy_info = {
                                "success": True,
                                "proxy_host": "DIRECT",
                                "proxy_port": "DIRECT",
                                "ip_info": ip_info,
                                "direct": True,
                                "adsl_direct": bool(is_adsl_task),
                            }
                            country = resolved_ip_info.get("country_name") or resolved_ip_info.get("country_code") or "DIRECT"
                            timezone = resolved_ip_info.get("timezone") or "Etc/UTC"
                            language = resolved_ip_info.get("language") or "en-US"
                            cc_upper = (resolved_ip_info.get("country_code") or "DIRECT").upper()
                            current_task['proxy_country'] = cc_upper or country or ("ADSL" if is_adsl_task else "DIRECT")
                            if is_adsl_task:
                                video_adsl_status["country"] = current_task['proxy_country']
                                video_adsl_status["current_ip"] = exit_ip
                            log.info(f"✅ 视频出口IP就绪: {exit_ip}, 国家={country}, 模式={'ADSL直连' if is_adsl_task else '本机直连'}")
                            break
                        except Exception as e:
                            if not video_task_running:
                                log.warning(f"⛔ 任务已停止（IP获取中）: {type(e).__name__}: {e}")
                                break
                            log.error(f"视频直连IP获取异常: {type(e).__name__}: {e}")
                            continue

                    if not video_task_running:
                        task['status'] = "已停止"
                        task['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                        log.warning(f"⛔ {_task_label}已停止，跳出当前任务")
                        break

                    if not proxy_info or not proxy_info.get("success") or not resolved_ip_info:
                        log.error("❌ 视频直连IP获取失败，跳过本任务")
                        task['status'] = "失败"
                        task['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                        continue
                    
                    # ========== Step B4: 生成与IP匹配的指纹 ==========
                    ua_repeat_max_rate = float(config.get("ua_repeat_max_rate", 0.2) or 0.2)
                    fingerprint = None
                    for fp_attempt in range(fingerprint_retry_max):
                        log.info(f"🔧 生成与IP匹配的指纹...（第 {fp_attempt + 1}/{fingerprint_retry_max} 次）")
                        log.debug(f"用于生成指纹的IP信息: {resolved_ip_info}")
                        fingerprint = generate_fingerprint(resolved_ip_info)
                        user_agent = fingerprint["user_agent"]
                        current_repeat_rate = 0
                        if fingerprint_stats["ua_usage"]:
                            total_used = sum(fingerprint_stats["ua_usage"].values())
                            if total_used > 0:
                                current_repeat_rate = fingerprint_stats["ua_usage"].get(user_agent, 0) / total_used
                        log.info(f"📈 当前UA重复率: {current_repeat_rate:.2%}, 阈值={ua_repeat_max_rate:.2%}, 当前UA使用次数: {fingerprint_stats['ua_usage'].get(user_agent, 0)}")
                        if current_repeat_rate <= ua_repeat_max_rate:
                            break
                        log.warning(f"⚠️ UA重复率超过阈值，废弃本次指纹/IP并重试")
                        fingerprint = None
                        if fp_attempt < fingerprint_retry_max - 1:
                            video_adsl_status["status"] = "UA重复率超阈值，重拨重试"
                            exit_ip, resolved_ip_info = redial_adsl_and_get_ip(sleep_func=video_interruptible_sleep, status_obj=video_adsl_status)
                    if not fingerprint:
                        raise RuntimeError(f"UA重复率连续超过阈值 {ua_repeat_max_rate:.2%}，本条{_task_label}失败")
                    log.debug(f"🔍 生成的完整指纹信息: {fingerprint}")
                    fingerprint_id = fingerprint["fingerprint_id"]
                    user_agent = fingerprint["user_agent"]
                    resolution = fingerprint["resolution"]
                    canvas = fingerprint["canvas"]
                    webgl = fingerprint["webgl"]
                    webrtc = "disabled"
                    log.info(f"✅ 指纹生成成功: ID={fingerprint_id[:8]}...")
                    
                    # 记录指纹使用统计
                    record_fingerprint_usage(fingerprint_id, user_agent, current_task['proxy_country'])

                    # ========== SEO 准备步骤（新增） ==========
                    selected_engine_id = None
                    selected_keyword = None
                    generated_referer = None
                    ip_region = None
                    try:
                        if not config.get("enable_seo", True):
                            log.warning("⚠️ enable_seo=False，但QA任务仍可继续（SEO跳过）")
                        else:
                            # Step 1: 根据语言/国家判断 IP 区域
                            lang_lower = (language or "").lower()
                            if lang_lower.startswith("zh") and cc_upper == "CN":
                                ip_region = "中国"
                            elif lang_lower.startswith("en"):
                                ip_region = "美国"
                            elif lang_lower.startswith(("de","fr","it","es","nl","sv","no","da","fi","pl","pt","el","ja","ko")):
                                ip_region = "美国"
                            else:
                                log.warning(f"⚠️ 语言{language}无对应SEO区域，尝试美国")
                                ip_region = "美国"

                            if ip_region:
                                log.info(f"✅ SEO 区域判定: {ip_region}")
                                # Step 2: 获取搜索引擎
                                selected_engine_id = seo_query.get_random_engine_for_region(ip_region)
                                if selected_engine_id:
                                    # Step3: 获取关键词
                                    selected_keyword = seo_query.get_random_keyword_for_engine(selected_engine_id)
                                    if selected_keyword:
                                        # Step4: 生成 Referer
                                        generated_referer = seo_query.generate_referer(selected_engine_id, selected_keyword)
                                        log.info(f"✅ SEO 准备完成：引擎={selected_engine_id}, 关键词={selected_keyword}")
                    except Exception as seo_e:
                        log.warning(f"⚠️ SEO 准备过程异常：{str(seo_e)}，不影响任务继续（SEO跳过）")
                    # =========================================
                    
                    # ========== Step B6: 构建代理配置（关键修正） ==========
                    # 重要：浏览器必须通过VPS代理服务器访问，这样才能确保使用获取到的动态出口IP
                    # VPS代理服务器会转发流量到正确的出口IP
                    
                    # ========== Step B7: 启动浏览器（带防检测配置） ==========
                    log.info("🔍 启动浏览器...")
                    _headless_mode = bool(config.get("headless", True))
                    ensure_xvfb_for_headed_mode(_headless_mode)
                    _use_real_chrome = bool(config.get("use_real_chrome", True))
                    log.info(f"浏览器模式: {'无头' if _headless_mode else '有界面'}，内核: {'本地Chrome' if _use_real_chrome else '系统Chrome'}")
                    log.debug(f"代理信息详细: proxy_host={proxy_info.get('proxy_host')}, proxy_port={proxy_info.get('proxy_port')}")
                    log.info(f"🌐 浏览器出口IP: {exit_ip}（通过代理访问: {proxy_info['proxy_host']}:{proxy_info['proxy_port']}）")
                    
                    proxy_config = None
                    log.info(f"[视频直连] ✅ 浏览器数据面使用本机出口，不设置 SOCKS5/HTTP 代理（模式={'ADSL' if is_adsl_task else 'DIRECT'}）")
                    log.debug(f"最终代理配置: {proxy_config}")
                    
                    # 检查参数类型
                    if proxy_config is not None:
                        assert isinstance(proxy_config, dict), "proxy_config must be a dict"
                    assert isinstance(_headless_mode, bool), "headless must be a bool"
                    
                    _launch_args = []
                    if proxy_config is not None:
                        _launch_args.extend([
                            f"--proxy-server={proxy_config['server']}",
                            "--proxy-bypass-list=*.google.com;*.googleapis.com;*.gstatic.com;*.gvt1.com;accounts.google.com;clients2.google.com;safebrowsing.googleapis.com;safebrowsinghttpgateway.googleapis.com;httpbin.org;api.ipify.org;icanhazip.com;ifconfig.me;checkip.amazonaws.com;ident.me",
                        ])
                    _launch_args.extend([
                            "--autoplay-policy=no-user-gesture-required",
                            "--mute-audio",
                            "--disable-features=AutoplayIgnoreWebAudio,MediaRouter,Translate,TranslateUI,LanguageDetection,OptimizationHints,VizDisplayCompositor",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-infobars",
                            "--disable-blink-features=AutomationControlled",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-extensions",
                            "--disable-background-networking",
                            "--safebrowsing-disable-auto-update",
                            "--disable-domain-reliability",
                            "--disable-popup-blocking",
                            "--disable-translate",
                            "--translate-ranker-model-url=0.0.0.0",
                            "--translate-security-origin=0.0.0.0",
                            "--disable-browser-side-navigation",
                            "--disable-gpu",
                            "--start-maximized",
                            f"--lang={fingerprint['language']},{fingerprint['language'].split('-')[0]};q=0.9",
                            "--disable-bundled-ppapi-flash",
                            "--disable-component-update",
                            "--disable-component-extensions-with-background-pages",
                    ])
                    _launch_kwargs = {
                        "headless": _headless_mode,
                        "args": _launch_args
                    }
                    
                    # ✅ 启动浏览器（改为主线程，SIGALRM 超时保护，避免 Task was destroyed）
                    def _vt_launch(_pw, _use_chrome_channel, _kwargs, _max_wait_sec=60):
                        import signal as _vsig
                        class _VTimeout(Exception):
                            pass
                        def _on_timeout(_signum, _frame):
                            raise _VTimeout(f"浏览器启动超时（>{_max_wait_sec}s）")
                        _vprev = None
                        try:
                            try:
                                _vprev = _vsig.signal(_vsig.SIGALRM, _on_timeout)
                                _vsig.alarm(_max_wait_sec)
                            except (ValueError, AttributeError, OSError):
                                _vprev = "no-sig"
                            try:
                                if _use_chrome_channel:
                                    _browser = _pw.chromium.launch(channel="chrome", **_kwargs)
                                else:
                                    _browser = _pw.chromium.launch(**_kwargs)
                                return _browser, None
                            except _VTimeout as _te:
                                return None, str(_te)
                            except Exception as _e:
                                return None, f"{type(_e).__name__}: {str(_e)[:200]}"
                        finally:
                            try:
                                _vsig.alarm(0)
                            except Exception:
                                pass
                            try:
                                if _vprev not in (None, "no-sig"):
                                    _vsig.signal(_vsig.SIGALRM, _vprev)
                            except Exception:
                                pass

                    def _vt_minimal_launch_kwargs():
                        return {
                            "headless": True,
                            "args": [
                                "--no-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-gpu",
                                "--disable-blink-features=AutomationControlled",
                                "--autoplay-policy=no-user-gesture-required",
                                "--mute-audio",
                                f"--lang={fingerprint['language']},{fingerprint['language'].split('-')[0]};q=0.9",
                                f"--window-size={width},{height}",
                            ]
                        }

                    browser = None
                    launch_errors = []
                    if _use_real_chrome:
                        browser, _vt_err = _vt_launch(p, True, _launch_kwargs, 60)
                        if browser is not None:
                            log.info("✅ 使用本地 Chrome 启动成功（支持 H.264/AAC）")
                        else:
                            launch_errors.append(f"chrome-full={_vt_err or '超时'}")
                            log.warning(f"⚠️ 本地 Chrome 启动失败/超时，回退: {_vt_err or '超时'}")
                    if browser is None:
                        browser, _vt_err2 = _vt_launch(p, False, _launch_kwargs, 60)
                        if browser is None:
                            launch_errors.append(f"chromium-full={_vt_err2 or '未知错误'}")
                            log.warning(f"⚠️ Chrome 完整参数启动失败，尝试极简参数: {_vt_err2 or '未知错误'}")
                            browser, _vt_err3 = _vt_launch(p, False, _vt_minimal_launch_kwargs(), 60)
                            if browser is None:
                                launch_errors.append(f"chromium-minimal={_vt_err3 or '未知错误'}")
                                raise RuntimeError(f"{_browser_label}启动失败: " + " | ".join(launch_errors))
                            log.info("✅ 使用 Chrome 极简参数启动成功")
                        else:
                            log.info("使用系统 Chrome 启动")
                    
                    # ========== Step B8: 创建浏览器上下文（应用指纹） ==========
                    log.info("🎨 创建浏览器上下文并应用指纹...")
                    width, height = map(int, resolution.split("x"))
                    
                    # 获取视频Referer配置：支持逗号分隔列表，按任务序号轮询选择（QA可观测，不随机伪装）
                    udis_referer, udis_referer_origin = select_video_referer_for_task(config, task_idx)
                    config["current_video_referer"] = udis_referer
                    config["current_video_referer_origin"] = udis_referer_origin
                    log.info(f"[视频Referer] 列表数量={len(get_video_referer_list(config))}，当前任务#{task_idx + 1}使用: Referer={udis_referer}, Origin={udis_referer_origin}")
                    
                    # 添加 Accept-Language 请求头，确保与指纹语言一致
                    lang_prefix = fingerprint["language"].split("-")[0]
                    accept_language = f"{fingerprint['language']},{lang_prefix};q=0.9,en-US;q=0.8,en;q=0.7"
                    extra_http_headers = {
                        "Referer": udis_referer,
                        "Accept-Language": accept_language
                    }
                    
                    context = browser.new_context(
                        user_agent=user_agent,
                        viewport={"width": width, "height": height},
                        locale=fingerprint["language"],
                        timezone_id=fingerprint["timezone"],
                        permissions=[],
                        geolocation=None,
                        device_scale_factor=1,
                        is_mobile=False,
                        has_touch=False,
                        color_scheme="light",
                        extra_http_headers=extra_http_headers
                    )
                    
                    # ========== Step B9: 添加请求拦截器（处理视频反盗链） ==========
                    log.info("🛡️ 配置请求拦截器...")
                    
                    # QA请求拦截：仅按本任务配置补齐业务需要的 Referer/Origin，不随机UA、不使用爬虫UA
                    def handle_video_request(route, request):
                        url = request.url
                        if is_udis_video_url(url):
                            custom_headers = {
                                "Referer": udis_referer,
                                "Origin": udis_referer_origin,
                                "Accept": "video/mp4,video/webm,video/ogg,video/*;q=0.9,application/octet-stream;q=0.8,audio/*;q=0.7,*/*;q=0.5",
                                "Accept-Language": fingerprint["language"],
                                "Accept-Encoding": "identity;q=1, *;q=0",
                                "Connection": "keep-alive"
                            }
                            log.debug(f"🎬 视频请求headers: Referer={udis_referer}, Origin={udis_referer_origin}, url={url}")
                            route.continue_(headers={**request.headers, **custom_headers})
                        else:
                            route.continue_()
                    
                    # 拦截所有视频格式
                    context.route("**/*.mp4", handle_video_request)
                    context.route("**/*.m3u8", handle_video_request)
                    context.route("**/*.ts", handle_video_request)
                    context.route("**/*.flv", handle_video_request)
                    context.route("**/*.mov", handle_video_request)
                    context.route("**/*.webm", handle_video_request)
                    context.route("**/*.ogg", handle_video_request)
                    
                    # ========== Step B11: 注入防检测脚本（隐藏自动化特征） ==========
                    log.info("🎭 注入防检测脚本...")
                    context.add_init_script(r"""
                        // ========== 0. 彻底禁用WebRTC（防止IP泄漏） ==========
                        (function() {
                            // 1. 删除构造函数
                            delete window.RTCPeerConnection;
                            delete window.webkitRTCPeerConnection;
                            
                            // 2. 覆盖为undefined
                            Object.defineProperty(window, 'RTCPeerConnection', {
                                value: undefined,
                                configurable: false,
                                writable: false
                            });
                            Object.defineProperty(window, 'webkitRTCPeerConnection', {
                                value: undefined,
                                configurable: false,
                                writable: false
                            });
                            
                            // 3. 删除navigator上的相关属性
                            Object.defineProperty(navigator, 'getUserMedia', {
                                value: undefined,
                                configurable: false,
                                writable: false
                            });
                            Object.defineProperty(navigator, 'webkitGetUserMedia', {
                                value: undefined,
                                configurable: false,
                                writable: false
                            });
                        })();
                        
                        // ========== 1. 隐藏自动化特征 ==========
                        // 隐藏navigator.webdriver
                        Object.defineProperty(navigator, 'webdriver', {
                            value: undefined,
                            writable: false,
                            configurable: false
                        });
                        
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
                        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                        delete window.$cdc_asdjflasutopfhvcZLmcfl_;
                        
                        // 隐藏CDP特征
                        const originalQuery = window.navigator.permissions.query;
                        window.navigator.permissions.query = (parameters) => (
                            parameters.name === 'notifications' ?
                                Promise.resolve({ state: Notification.permission }) :
                                originalQuery(parameters)
                        );
                        
                        // ========== 2. Cookie和LocalStorage初始化 ==========
                    (function() {
                        var cookies = [
                            {name: "NID", value: "511=" + Math.random().toString(36).substring(2, 20), domain: ".google.com", path: "/"},
                            {name: "PHPSESSID", value: Math.random().toString(36).substring(2, 26), path: "/"},
                            {name: "_ga", value: "GA1.2." + Math.floor(Math.random() * 1000000000) + "." + Math.floor(Date.now() / 1000), path: "/"},
                            {name: "_gid", value: "GA1.2." + Math.floor(Math.random() * 1000000000) + "." + Math.floor(Date.now() / 1000), path: "/"},
                            {name: "session", value: Math.random().toString(36).substring(2, 40), path: "/"}];
                        cookies.forEach(function(c) {
                            try {
                                document.cookie = c.name + "=" + c.value + "; path=" + c.path + "; expires=" + new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toUTCString();
                            } catch(e) {}
                        });
                        var localStorageData = {
                            "visitedSites": JSON.stringify(["https://www.google.com", "https://www.youtube.com"]),
                            "preferences": JSON.stringify({"theme": "light", "language": "zh-CN"}),
                            "lastVisit": Date.now().toString(),
                            "visitCount": Math.floor(Math.random() * 100).toString()
                        };
                        Object.keys(localStorageData).forEach(function(key) {
                            try {
                                localStorage.setItem(key, localStorageData[key]);
                            } catch(e) {}
                        });
                    })();

                    // ========== 2. 补充真实浏览器属性 ==========
                        // 模拟plugins
                        if (!navigator.plugins.length) {
                            const pluginClasses = [
                                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                                { name: 'Native Client', filename: 'internal-nacl-plugin' }
                            ];
                            
                            const createPlugin = (info) => {
                                const plugin = document.createElement('embed');
                                plugin.setAttribute('name', info.name);
                                plugin.setAttribute('src', '');
                                plugin.setAttribute('type', 'application/x-google-chrome-pdf');
                                plugin.style.display = 'none';
                                document.body.appendChild(plugin);
                                return plugin;
                            };
                            
                            Object.defineProperty(navigator, 'plugins', {
                                value: pluginClasses.map(createPlugin),
                                writable: false,
                                configurable: false
                            });
                        }
                        
                        // 模拟mimeTypes
                        if (!navigator.mimeTypes.length) {
                            const mimeTypes = [
                                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                                { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                                { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }
                            ];
                            
                            Object.defineProperty(navigator, 'mimeTypes', {
                                value: mimeTypes,
                                writable: false,
                                configurable: false
                            });
                        }
                        
                        // ========== 3. Canvas和WebGL指纹伪装（合规化：噪声+真实GPU字符串） ==========
                        (function() {
                            let _s = (__CANVAS_SEED__) >>> 0 || 1;
                            const _rnd = function() { _s = (_s * 1103515245 + 12345) & 0x7fffffff; return _s / 0x7fffffff; };
                            const _origGID = CanvasRenderingContext2D.prototype.getImageData;
                            CanvasRenderingContext2D.prototype.getImageData = function() {
                                const data = _origGID.apply(this, arguments);
                                try {
                                    const d = data.data;
                                    for (let i = 0; i < d.length; i += 4) {
                                        if (_rnd() < 0.02) {
                                            const n = (_rnd() * 3 | 0) - 1;
                                            d[i]=Math.max(0,Math.min(255,d[i]+n));
                                            d[i+1]=Math.max(0,Math.min(255,d[i+1]+n));
                                            d[i+2]=Math.max(0,Math.min(255,d[i+2]+n));
                                        }
                                    }
                                } catch(e) {}
                                return data;
                            };
                            const _origTDU = HTMLCanvasElement.prototype.toDataURL;
                            HTMLCanvasElement.prototype.toDataURL = function() {
                                try { const c=this.getContext('2d'); if(c){c.getImageData(0,0,Math.max(1,this.width),Math.max(1,this.height));} } catch(e){}
                                return _origTDU.apply(this, arguments);
                            };
                        })();
                        (function() {
                            const _patch = function(proto) {
                                if (!proto || !proto.getParameter) return;
                                const _orig = proto.getParameter;
                                proto.getParameter = function(param) {
                                    if (param === 37445) return '__WEBGL_VENDOR__';
                                    if (param === 37446) return '__WEBGL_RENDERER__';
                                    return _orig.call(this, param);
                                };
                            };
                            try { _patch(WebGLRenderingContext.prototype); } catch(e) {}
                            try { _patch(WebGL2RenderingContext.prototype); } catch(e) {}
                        })();
                        try { Object.defineProperty(navigator,'hardwareConcurrency',{get:function(){return __HW_CONC__;},configurable:true}); } catch(e){}
                        try { Object.defineProperty(navigator,'deviceMemory',{get:function(){return __DEV_MEM__;},configurable:true}); } catch(e){}
                        
                        // ========== 4. 语言设置 ==========
                        Object.defineProperty(navigator, 'language', {
                            value: '__LANGUAGE__',
                            writable: false,
                            configurable: false
                        });
                        
                        Object.defineProperty(navigator, 'languages', {
                            value: ['__LANGUAGE__', '__PREFIX__', 'en-US', 'en'],
                            writable: false,
                            configurable: false
                        });
                        
                        // ========== 5. 其他指纹伪装 ==========
                        // 隐藏chrome.runtime
                        if (window.chrome) {
                            delete window.chrome.runtime;
                        }
                        
                        // 覆盖Object.prototype.toString，隐藏真实构造器
                        const originalToString = Object.prototype.toString;
                        Object.prototype.toString = function() {
                            if (this === window.navigator) return '[object Navigator]';
                            if (this === window.screen) return '[object Screen]';
                            return originalToString.call(this);
                        };
                    """.replace('__CANVAS_SEED__', str(int(fingerprint.get('canvas_noise_seed', 12345)))).replace('__WEBGL_VENDOR__', fingerprint.get('webgl_vendor', 'Google Inc. (Intel)')).replace('__WEBGL_RENDERER__', fingerprint.get('webgl_renderer', 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)')).replace('__HW_CONC__', str(int(fingerprint.get('hardware_concurrency', 8)))).replace('__DEV_MEM__', str(int(fingerprint.get('device_memory', 8)))).replace('__CANVAS__', canvas).replace('__WEBGL__', webgl).replace('__LANGUAGE__', fingerprint['language']).replace('__PREFIX__', fingerprint['language'].split('-')[0]))
                    log.info("✅ 防检测脚本注入完成")
                    
                    # ========== Step B11: 创建页面并进行一致性检查 ==========
                    log.info("🧪 创建页面并进行一致性检查...")
                    page = context.new_page()

                    # 检查语言一致性（安全读取）
                    actual_language = page_eval(page, "() => navigator.language || 'en-US'", default=fingerprint['language'])
                    if not isinstance(actual_language, str) or not actual_language:
                        actual_language = fingerprint['language']
                    log.info(f"🌐 语言检查: 预期={fingerprint['language']}, 实际={actual_language}")

                    # ❌ 已按需求删除"浏览器出口IP泄漏一致性检测"(check_ip_leak_robust)。
                    # 出口IP 由 ADSL 本机直连 / SOCKS5 链路保证，浏览器再比对 ipify 冗余且常超时。
                    _webrtc_local = page_eval(page, """() => {
                        try {
                            if (typeof window.RTCPeerConnection === 'undefined' &&
                                typeof window.webkitRTCPeerConnection === 'undefined') {
                                return 'disabled';
                            }
                            return 'enabled';
                        } catch(e) { return 'disabled'; }
                    }""", default="disabled")
                    actual_webrtc_status = _webrtc_local if isinstance(_webrtc_local, str) else "disabled"
                    leak_status, real_ip = "skip", exit_ip
                    leak_ok = True

                    # 检查时区一致性（安全读取）
                    actual_timezone = page_eval(
                        page,
                        "() => Intl.DateTimeFormat().resolvedOptions().timeZone",
                        default=fingerprint.get("timezone", "UTC"),
                    )
                    if not isinstance(actual_timezone, str) or not actual_timezone:
                        actual_timezone = fingerprint.get("timezone", "UTC")
                    log.info(f"⏰ 时区检查: 预期={fingerprint['timezone']}, 实际={actual_timezone}")

                    # 检查 webdriver 是否被正确隐藏（安全读取，缺省视为隐藏）
                    webdriver_hidden = page_eval(page, "() => navigator.webdriver === undefined", default=True)
                    if not isinstance(webdriver_hidden, bool):
                        webdriver_hidden = True
                    log.info(f"🕵️ webdriver隐藏检查: {'✅ 已隐藏' if webdriver_hidden else '❌ 未隐藏'}")

                    # 检查 plugins 是否正确模拟（安全读取）
                    plugins_ok = page_eval(page, "() => (navigator.plugins && navigator.plugins.length > 0) || true", default=True)
                    if not isinstance(plugins_ok, bool):
                        plugins_ok = True
                    log.info(f"🧩 plugins模拟检查: {'✅ 已模拟' if plugins_ok else '❌ 未模拟'}")

                    # 最终一致性检查：pass/skip/unreachable 都算通过；时区/语言严格匹配；webdriver 必须隐藏
                    # skip = 配置明确跳过检测，unreachable = 检测站无法访问，不算IP泄漏
                    leak_ok = leak_status in ("pass", "skip", "unreachable")
                    consistency = (
                        leak_ok and
                        actual_timezone == fingerprint["timezone"] and
                        _bcp47_prefix_equal(actual_language, fingerprint["language"]) and
                        webdriver_hidden
                    )

                    if consistency:
                        log.info("✅ 一致性检查全部通过")
                    else:
                        details = []
                        if not leak_ok:
                            details.append(f"IP泄漏: 预期={exit_ip}, 实际={real_ip} (leak_status={leak_status})")
                        if actual_timezone != fingerprint["timezone"]:
                            details.append(f"时区不匹配: 预期={fingerprint['timezone']}, 实际={actual_timezone}")
                        if not _bcp47_prefix_equal(actual_language, fingerprint["language"]):
                            details.append(f"语言不匹配: 预期={fingerprint['language']}, 实际={actual_language}")
                        if not webdriver_hidden:
                            details.append("webdriver未隐藏")
                        task['status'] = "失败"
                        task['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                        log.error(f"❌ 指纹/IP一致性检查失败，当前{_task_label}退出: " + "; ".join(details))
                        continue
                    
                    log.info("✅ 浏览器初始化完成，前置流程全部通过")
                    
                    # ========== Step B5: 执行任务主体 ==========
                    task['start_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    
                    # 初始化行为统计
                    current_x, current_y = 100, 100
                    behavior_stats = {
                        "mouse_moves": 0, "scrolls": 0, "scroll_distance": 0,
                        "clicks": 0, "key_presses": 0, "focus_switches": 0,
                        "refreshes": 0, "total_stay": 0
                    }
                    
                    task_config = dict(config)
                    log.info(f"[QA会话] 全局session策略: {get_global_session_mode()}，视频/QA本轮使用 {'country_host_7d' if get_global_session_mode() == 'country_host_7d' else 'new_each_task'}")
                    # 根据用户勾选决定执行哪些阶段
                    run_website = bool(config.get('qa_run_phases', {}).get('website', True))
                    run_video = bool(config.get('qa_run_phases', {}).get('video', True))
                    
                    if unified_qa and run_website:
                        log.web_round_separator(1, 3 if run_video else 1)
                        log.info("[综合QA] 阶段：网站浏览QA + 广告曝光检测开始")
                        website_behavior, current_x, current_y = run_website_qa_segment(page, task_config, current_x, current_y, selected_engine_id, selected_keyword)
                        for key in behavior_stats:
                            behavior_stats[key] += website_behavior.get(key, 0)
                        if not video_task_running:
                            raise RuntimeError("综合QA已停止（网站/广告阶段后）")
                        if run_video:
                            log.info("[综合QA] 网站/广告QA完成，进入视频QA")
                            log.video_round_separator(2, 3)
                    
                    if not unified_qa or run_video:
                        if not unified_qa:
                            log.video_round_separator(1, 1)
                        log.info(f"[视频流程] 开始入口→Layer1→Layer2→视频页导航观看流程，本任务Referer={config.get('current_video_referer', '')}")
                        video_behavior, current_x, current_y = run_video_navigation_flow(page, task_config, current_x, current_y)
                        for key in behavior_stats:
                            behavior_stats[key] += video_behavior.get(key, 0)
                    
                    if unified_qa and run_website and run_video:
                        log.info("[综合QA] 阶段3/3：视频QA完成")
                    elif unified_qa and run_video:
                        log.info("[综合QA] 视频QA完成")
                    elif unified_qa and run_website:
                        log.info("[综合QA] 仅网站浏览QA，已完成（未勾选视频，跳过视频QA）")
                    
                    # 记录行为统计
                    log.behavior_module(
                        behavior_stats["mouse_moves"], behavior_stats["scrolls"],
                        behavior_stats["scroll_distance"], behavior_stats["clicks"],
                        behavior_stats["total_stay"], behavior_stats["focus_switches"],
                        behavior_stats["refreshes"], 0, behavior_stats["total_stay"],
                        behavior_stats["key_presses"]
                    )
                    
                    # 记录视频观看统计
                    increment_video_view_count(task['proxy_country'])
                    increment_video_item_success()
                    
                    task['status'] = "已完成"
                    if is_adsl_task:
                        video_adsl_status["completed"] += 1
                        video_adsl_status["status"] = "单轮完成"
                    log.info(f"✅ {_task_label}执行成功")
                    
                except Exception as e:
                    if not video_task_running:
                        log.warning(f"⛔ {_task_label}已停止，当前任务不计失败: {str(e)[:160]}")
                        task['status'] = "已停止"
                        if is_adsl_task:
                            video_adsl_status["status"] = "已停止"
                            video_adsl_status["last_error"] = "用户停止任务"
                    else:
                        log.error(f"❌ {_task_label}执行失败: {str(e)}")
                        task['status'] = "失败"
                        increment_video_item_fail()
                        if is_adsl_task:
                            video_adsl_status["status"] = "单轮失败"
                            video_adsl_status["last_error"] = str(e)[:200]
                finally:
                    # video_task: 带 timeout 保护的 browser close
                    if browser:
                        try:
                            import threading as _vth
                            _vresult = {"ok": False}

                            def _vclose():
                                try:
                                    browser.close(timeout=15000)
                                    _vresult["ok"] = True
                                except Exception:
                                    try:
                                        browser.close()
                                        _vresult["ok"] = True
                                    except Exception:
                                        pass

                            _vt = _vth.Thread(target=_vclose, daemon=True)
                            _vt.start()
                            _vt.join(18)  # 最多等 18 秒
                            if not _vresult["ok"]:
                                log.warning(f"⚠️ {_browser_label}关闭超时，已跳过强制杀进程，等待系统自然回收")
                            try:
                                import random as _vrandom_cleanup
                                _vwait = _vrandom_cleanup.uniform(5, 10)
                                log.debug(f"🧹 {_browser_label}关闭流程完成，等待 {_vwait:.1f}s 后进入下一任务")
                                video_interruptible_sleep(_vwait)
                            except Exception:
                                pass
                        except Exception:
                            pass

                    task['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        log.info(f"{_task_icon} {_task_label}全部执行完成")

        # 将完成的任务计划保存到历史记录
        if video_plan:
            add_to_historical_tasks(video_plan)
            log.info(f"{_task_label}计划已保存到历史记录")

    except Exception as e:
        log.error(f"{_task_label}执行失败: {str(e)}")
    finally:
        video_task_running = False
        video_worker_active = False
        if adsl_ip_task:
            video_adsl_status["running"] = False
            video_adsl_status["status"] = "完成" if video_adsl_status.get("completed", 0) >= video_adsl_status.get("total", 0) else "已停止"
        stop_human_model()


def increment_video_view_count(country):
    """增加视频观看计数"""
    global stats
    stats['video_view_count'] += 1
    if country not in stats['country_video_views']:
        stats['country_video_views'][country] = 0
    stats['country_video_views'][country] += 1


def increment_video_item_success():
    global stats
    stats["video_item_success"] = stats.get("video_item_success", 0) + 1


def increment_video_item_fail():
    global stats
    stats["video_item_fail"] = stats.get("video_item_fail", 0) + 1


def get_total_video_views():
    """获取总视频观看次数"""
    global stats
    return stats['video_view_count']


def get_country_video_views():
    """获取国家视频观看次数"""
    global stats
    return stats['country_video_views']


def get_current_ip_context():
    ip = video_adsl_status.get("current_ip") or adsl_status.get("current_ip") or ""
    country = video_adsl_status.get("country") or adsl_status.get("country") or ""
    language = ""
    timezone_name = os.environ.get("TZ") or ""
    local_time = ""
    if ip:
        try:
            resolved = resolve_ip_info(ip)
            if isinstance(resolved, dict):
                country = country or resolved.get("country_code") or resolved.get("country_name") or ""
                language = resolved.get("language") or ""
                timezone_name = resolved.get("timezone") or timezone_name
        except Exception as e:
            log.debug(f"[全局状态] 当前IP信息解析失败: {str(e)[:120]}")
    try:
        tz = pytz.timezone(timezone_name) if timezone_name else None
        local_time = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S') if tz else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return {
        "ip": ip,
        "country": country,
        "language": language,
        "timezone": timezone_name,
        "local_time": local_time
    }

@app.route('/')
def index():
    ensure_config_defaults()
    return render_template_string(HTML_TEMPLATE, config=config, logs=log.messages[-500:], 
                                  statstotal=stats['total'], statssuccess=stats['success'], 
                                  statsfail=stats['fail'], statsvideo_view_count=stats['video_view_count'],
                                  stats=stats, runningtask=task_running,
                                  planned_total=planned_total_tasks)


# ==================== 视频任务API接口 ====================
@app.route('/generate_video_plan', methods=['POST'])
def generate_video_plan():
    try:
        # 生成视频任务计划（类似generate_plan，但专门为视频流量优化）
        plan = generate_video_daily_tasks(config)
        global video_plan
        video_plan = plan
        return jsonify({"status": "ok", "plan": plan})
    except Exception as e:
        log.error(f"生成视频任务计划失败: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/get_video_plan', methods=['GET'])
def get_video_plan():
    global video_plan
    return jsonify({"plan": video_plan})


@app.route('/clear_video_plan', methods=['POST'])
def clear_video_plan():
    global video_plan
    video_plan = None
    return jsonify({"success": True, "message": "视频计划已清除"})


@app.route('/start_unified_adsl_qa_task', methods=['POST'])
def start_unified_adsl_qa_task():
    try:
        global video_task_running, video_worker_active
        if video_worker_active:
            return jsonify({"status": "error", "success": False, "message": "已有视频/综合QA任务正在运行或停止中"}), 409
        video_task_running = True
        video_worker_active = True
        from threading import Thread
        thread = Thread(target=run_video_tasks, kwargs={"adsl_ip_task": True, "unified_qa": True})
        thread.start()
        log.info("✅ ADSL综合QA任务线程已启动")
        return jsonify({"status": "ok", "success": True, "message": "ADSL综合QA任务已启动"})
    except Exception as e:
        video_worker_active = False
        video_task_running = False
        log.error(f"启动ADSL综合QA任务失败: {str(e)}")
        return jsonify({"status": "error", "success": False, "message": str(e)}), 500


@app.route('/start_unified_qa_task', methods=['POST'])
def start_unified_qa_task():
    try:
        global video_task_running, video_worker_active
        if video_worker_active:
            return jsonify({"status": "error", "success": False, "message": "已有视频/综合QA任务正在运行或停止中"}), 409
        video_task_running = True
        video_worker_active = True
        from threading import Thread
        thread = Thread(target=run_video_tasks, kwargs={"adsl_ip_task": True, "unified_qa": True})
        thread.start()
        log.info("✅ ADSL综合QA任务线程已启动（原综合QA入口已转为ADSL）")
        return jsonify({"status": "ok", "success": True, "message": "ADSL综合QA任务已启动"})
    except Exception as e:
        video_worker_active = False
        video_task_running = False
        log.error(f"启动综合QA任务失败: {str(e)}")
        return jsonify({"status": "error", "success": False, "message": str(e)}), 500


@app.route('/start_video_adsl_ip_task', methods=['POST'])
def start_video_adsl_ip_task():
    try:
        global video_task_running, video_worker_active
        if video_worker_active:
            return jsonify({"status": "error", "message": "已有视频任务正在运行或停止中"}), 409
        video_task_running = True
        video_worker_active = True
        from threading import Thread
        thread = Thread(target=run_video_tasks, kwargs={"adsl_ip_task": True})
        thread.start()
        log.info("✅ 视频 ADSL IP任务线程已启动")
        return jsonify({"status": "ok", "success": True, "message": "视频 ADSL IP任务已启动"})
    except Exception as e:
        log.error(f"启动视频 ADSL IP任务失败: {str(e)}")
        return jsonify({"status": "error", "success": False, "message": str(e)}), 500

@app.route('/start_video_tasks', methods=['POST'])
def start_video_tasks():
    try:
        log.info("🔴 收到启动视频任务请求")
        # 启动视频任务执行线程
        global video_task_running, video_worker_active
        if video_worker_active:
            return jsonify({"success": False, "message": "已有视频任务正在运行或停止中"}), 409
        video_task_running = True
        video_worker_active = True
        from threading import Thread
        thread = Thread(target=run_video_tasks, kwargs={"adsl_ip_task": True})
        thread.start()
        log.info("✅ 视频ADSL任务线程已启动")
        return jsonify({"success": True, "message": "视频ADSL任务已启动"})
    except Exception as e:
        log.error(f"启动视频任务失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/stop_video_tasks', methods=['POST'])
def stop_video_tasks():
    global video_task_running, video_worker_active, current_video_task_idx, video_adsl_status, video_plan
    video_task_running = False
    stop_human_model()
    if video_plan and current_video_task_idx >= 0 and current_video_task_idx < len(video_plan.get('tasks', [])):
        cur = video_plan['tasks'][current_video_task_idx]
        if cur.get('status') == '执行中':
            cur['status'] = '已停止'
            cur['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    current_video_task_idx = -1
    video_adsl_status["running"] = False
    video_adsl_status["status"] = "停止中"
    video_adsl_status["last_error"] = "用户点击停止任务"
    _is_qa = bool(video_plan and str(video_plan.get("model_used", "")).startswith("unified_qa"))
    _stop_label = "QA任务" if _is_qa else "视频任务"
    log.warning(f"⛔ 已收到{_stop_label}停止请求，正在中断当前等待/重拨/浏览器流程")
    # 强制关闭所有活跃浏览器（确保点击停止后浏览器一定关闭）
    try:
        import selenium_bridge
        closed = selenium_bridge.force_quit_all()
        if closed:
            log.info(f"🛑 停止{_stop_label}：已强制关闭 {closed} 个浏览器实例")
    except Exception as e:
        log.warning(f"强制关闭浏览器异常: {e}")
    return jsonify({"success": True, "status": "ok", "message": f"✅ 已发送停止信号（{_stop_label}），当前重拨/浏览器清理会尽快中断"})


@app.route('/get_video_task_status', methods=['GET'])
def get_video_task_status():
    global video_task_running, video_worker_active, current_video_task_idx, video_plan, video_adsl_status
    current_task = None
    if video_plan and current_video_task_idx >= 0 and current_video_task_idx < len(video_plan.get('tasks', [])):
        current_task = video_plan['tasks'][current_video_task_idx]
    return jsonify({
        "running": video_worker_active,
        "current_task_idx": current_video_task_idx,
        "current_task": current_task,
        "total_tasks": video_plan['total_tasks'] if video_plan else 0,
        "adsl": video_adsl_status
    })


@app.route('/get_global_task_status', methods=['GET'])
def get_global_task_status():
    current_website_task = None
    if current_plan and current_task_idx >= 0 and current_task_idx < len(current_plan.get('tasks', [])):
        current_website_task = current_plan['tasks'][current_task_idx]
    current_video_task = None
    if video_plan and current_video_task_idx >= 0 and current_video_task_idx < len(video_plan.get('tasks', [])):
        current_video_task = video_plan['tasks'][current_video_task_idx]
    qa_running = bool(video_worker_active and video_plan and str(video_plan.get("model_used", "")).startswith("unified_qa"))
    with human_model_lock:
        human_model = dict(human_model_state)
    return jsonify({
        "website": {
            "running": bool(task_running or qa_running),
            "current_task_idx": current_task_idx,
            "current_task": current_website_task,
            "total_tasks": current_plan['total_tasks'] if current_plan else 0
        },
        "video": {
            "running": bool(video_worker_active or qa_running),
            "current_task_idx": current_video_task_idx,
            "current_task": current_video_task,
            "total_tasks": video_plan['total_tasks'] if video_plan else 0,
            "adsl": video_adsl_status
        },
        "qa": {
            "running": qa_running,
            "session_mode": get_global_session_mode(),
            "adsl": video_adsl_status if qa_running else {}
        },
        "ip": get_current_ip_context(),
        "human_model": human_model,
        "stats": stats
    })


@app.route('/get_website_task_status', methods=['GET'])
def get_website_task_status():
    """获取网站流量任务状态"""
    global task_running, current_task_idx, current_plan
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
        stats = {
            "total_views": get_total_video_views(),
            "country_views": get_country_video_views(),
            "video_item_success": stats.get("video_item_success", 0),
            "video_item_fail": stats.get("video_item_fail", 0)
        }
        return jsonify({"stats": stats})
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
    
    # ⭐ 修复：识别"仅同步视频广告启用开关"的特殊字段，避免覆盖整个 video_ad 子配置
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
    
    log.info("配置已保存")
    return jsonify({"success": True, "status": "ok"})


@app.route('/reset_config_defaults', methods=['POST'])
def reset_config_defaults():
    global config, pending_plan, video_plan, planned_total_tasks
    data = request.get_json(silent=True) or {}
    if task_running or video_worker_active:
        return jsonify({"success": False, "status": "error", "message": "任务运行中，请先停止任务再恢复默认"}), 409
    config.clear()
    config.update(copy.deepcopy(DEFAULT_CONFIG))
    ensure_config_defaults()
    pending_plan = None
    video_plan = None
    planned_total_tasks = 0
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
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

@app.route('/start_task', methods=['POST'])
def start_task():
    global task_running
    if not task_running:
        threading.Thread(target=worker_task, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route('/start_single_task', methods=['POST'])
def start_single_task():
    global task_running
    if task_running:
        return jsonify({"status": "error", "message": "已有任务正在运行"}), 409
    threading.Thread(target=worker_task, kwargs={"single_task": True}, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route('/start_adsl_ip_task', methods=['POST'])
def start_adsl_ip_task():
    global task_running
    if task_running:
        return jsonify({"status": "error", "message": "已有任务正在运行"}), 409
    threading.Thread(target=worker_task, kwargs={"single_task": True, "adsl_ip_task": True}, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route('/stop_task', methods=['POST'])
def stop_task():
    global task_running, adsl_status
    task_running = False
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
        def _log_fn(msg):
            log.info(f"[攻防演练] {msg}")
        def _progress_fn(pct, stage):
            _drill_state["progress"] = int(pct)
            _drill_state["stage"] = stage
        report, json_path, html_path = risk_check.run_drill(
            target_url, headless=headless, log_fn=_log_fn, progress_fn=_progress_fn, with_stealth=True
        )
        _drill_state["report"] = (report or {}).get("risk_calc", {})
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
        "json_path": _drill_state["json_path"],
        "html_path": _drill_state["html_path"],
    })


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
    return ''.join([f"<p>{msg}</p>" for msg in messages])

@app.route('/api/status')
def api_status():
    return jsonify({
        "running": task_running,
        "total": stats["total"],
        "success": stats["success"],
        "fail": stats["fail"],
        "video_view_count": stats["video_view_count"],
        "total_video_watch_time": stats["total_video_watch_time"],
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
    
    # 更新搜索引擎列表
    config['seo']['search_engines'] = data.get('search_engines', [])
    
    # 更新地域-搜索引擎映射
    config['seo']['region_engine_map']['中国'] = [s.strip() for s in data.get('seo_region_china', '').split(',') if s.strip()]
    config['seo']['region_engine_map']['美国'] = [s.strip() for s in data.get('seo_region_usa', '').split(',') if s.strip()]
    
    # 更新关键词池
    config['seo']['keyword_pools']['zh'] = [s.strip() for s in data.get('seo_keywords_zh', '').split(',') if s.strip()]
    config['seo']['keyword_pools']['en'] = [s.strip() for s in data.get('seo_keywords_en', '').split(',') if s.strip()]
    
    # 更新Referer模式
    config['seo']['referer_mode'] = data.get('seo_referer_mode', 'dynamic')
    
    # 保存到配置文件
    with open('config.json', 'w') as f:
        json.dump(config, f, indent=4)
    
    log.info("SEO配置已保存")
    return jsonify({"status": "ok"})



if __name__ == "__main__":
    try:
        with open('config.json', 'r') as f:
            loaded_config = json.load(f)
            # 确保 proxy_pool 存在且有所有国家
            if 'proxy_pool' not in loaded_config or len(loaded_config['proxy_pool']) < 10:
                log.info("配置文件中的 proxy_pool 不完整，使用默认配置")
                loaded_config['proxy_pool'] = config['proxy_pool']
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
    app.run(host="0.0.0.0",port=port)
