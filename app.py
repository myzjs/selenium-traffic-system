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

# ========== 应用版本号 ==========
APP_VERSION = "3.5.9"

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
from local_proxy_relay import start_relay as _start_proxy_relay, stop_relay as _stop_proxy_relay

# ★ 风控增强模块（P0 / P1 / P2 统一整改落地）— 最小侵入式接入
try:
    import risk_control_enhancements as _rce
    _HAS_RCE = True
except Exception as _e:
    _rce = None  # type: ignore[assignment]
    _HAS_RCE = False
    print(f"[WARN] risk_control_enhancements 模块不可用（不影响主流程）: {_e}", file=sys.stderr)

# ★ HilltopAds Pop-under 弹窗触发模块
try:
    import popunder_trigger as _popunder
    _HAS_POPUNDER = True
except Exception as _e:
    _popunder = None
    _HAS_POPUNDER = False

# ★ P0-1: TLS/JA3指纹伪装 - 使用curl_cffi模拟Chrome TLS指纹（替代原生requests的Python TLS特征）
try:
    from curl_cffi import requests as _tls_requests
    _TLS_IMPERSONATE = "chrome"  # 模拟Chrome 120+ TLS/JA3指纹
    _HAS_CURL_CFFI = True
except ImportError:
    _tls_requests = requests  # 回退到原生requests
    _TLS_IMPERSONATE = None
    _HAS_CURL_CFFI = False

def tls_safe_get(url, **kwargs):
    """TLS指纹安全的GET请求（JA3指纹=Chrome，避免Python-requests被识别）"""
    if _HAS_CURL_CFFI:
        kwargs.setdefault("impersonate", _TLS_IMPERSONATE)
        kwargs.setdefault("timeout", 30)
        return _tls_requests.get(url, **kwargs)
    return requests.get(url, **kwargs)

def tls_safe_post(url, **kwargs):
    """TLS指纹安全的POST请求"""
    if _HAS_CURL_CFFI:
        kwargs.setdefault("impersonate", _TLS_IMPERSONATE)
        kwargs.setdefault("timeout", 30)
        return _tls_requests.post(url, **kwargs)
    return requests.post(url, **kwargs)

_xvfb_process = None
_xvfb_lock = threading.Lock()

# ★ 审计修复#12：进程退出时清理Xvfb子进程，防止泄漏
import atexit as _atexit
def _cleanup_xvfb():
    global _xvfb_process
    if _xvfb_process and _xvfb_process.poll() is None:
        try:
            _xvfb_process.terminate()
            _xvfb_process.wait(timeout=3)
        except Exception:
            try:
                _xvfb_process.kill()
            except Exception:
                pass
_atexit.register(_cleanup_xvfb)

# ========== 时区和工作时间判断函数 ==========
COUNTRY_TIMEZONE_MAP = {
    # 原有国家
    "US": "America/New_York",      # 美国 - 纽约
    "CA": "America/Toronto",       # 加拿大 - 多伦多
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
    "CN": "Asia/Shanghai",         # 中国 - 上海
    "IE": "Europe/Dublin",         # 爱尔兰 - 都柏林
    "IN": "Asia/Kolkata",          # 印度 - 加尔各答
    "MY": "Asia/Kuala_Lumpur",     # 马来西亚
    "PH": "Asia/Manila",           # 菲律宾
    "ZA": "Africa/Johannesburg",   # 南非
    "KR": "Asia/Seoul",            # 韩国
    "BR": "America/Sao_Paulo",     # 巴西
    "MX": "America/Mexico_City",   # 墨西哥
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

# ========== 网络RTT延迟抖动仿真 ==========
def simulate_rtt_jitter(base_ms=50, jitter_ms=30):
    """模拟真实网络RTT延迟抖动（正态分布，模拟真实网络波动）。
    用于在关键网络操作前插入随机延迟，避免机器式零延迟特征。
    base_ms: 基础延迟（毫秒），jitter_ms: 抖动幅度（毫秒）
    """
    import math
    # 对数正态分布模拟RTT：中位数=base_ms，偶尔出现高延迟尖峰
    rtt = max(5, min(500, math.exp(random.gauss(math.log(max(1, base_ms)), 0.4))))
    rtt += random.uniform(0, jitter_ms)  # 额外抖动
    time.sleep(rtt / 1000.0)
    return rtt


# ========== 城市→经纬度映射（用于geolocation注入） ==========
# 全球主要城市坐标（±0.05°随机抖动，模拟GPS精度误差）
CITY_COORDINATES = {
    # 北美
    "new york": (40.7128, -74.0060), "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298), "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740), "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936), "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970), "miami": (25.7617, -80.1918),
    "atlanta": (33.7490, -84.3880), "boston": (42.3601, -71.0589),
    "seattle": (47.6062, -122.3321), "denver": (39.7392, -104.9903),
    "toronto": (43.6532, -79.3832), "vancouver": (49.2827, -123.1207),
    "montreal": (45.5017, -73.5673), "calgary": (51.0447, -114.0719),
    "ottawa": (45.4215, -75.6972), "edmonton": (53.5461, -113.4938),
    # 欧洲
    "london": (51.5074, -0.1278), "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050), "madrid": (40.4168, -3.7038),
    "rome": (41.9028, 12.4964), "amsterdam": (52.3676, 4.9041),
    "brussels": (50.8503, 4.3517), "vienna": (48.2082, 16.3738),
    "dublin": (53.3498, -6.2603), "stockholm": (59.3293, 18.0686),
    "oslo": (59.9139, 10.7522), "copenhagen": (55.6761, 12.5683),
    "helsinki": (60.1699, 24.9384), "warsaw": (52.2297, 21.0122),
    "prague": (50.0755, 14.4378), "budapest": (47.4979, 19.0402),
    "lisbon": (38.7223, -9.1393), "athens": (37.9838, 23.7275),
    "moscow": (55.7558, 37.6173), "zurich": (47.3769, 8.5417),
    # 亚太
    "tokyo": (35.6762, 139.6503), "seoul": (37.5665, 126.9780),
    "shanghai": (31.2304, 121.4737), "beijing": (39.9042, 116.4074),
    "hong kong": (22.3193, 114.1694), "taipei": (25.0330, 121.5654),
    "singapore": (1.3521, 103.8198), "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631), "auckland": (-36.8485, 174.7633),
    "mumbai": (19.0760, 72.8777), "delhi": (28.6139, 77.2090),
    "bangkok": (13.7563, 100.5018), "jakarta": (-6.2088, 106.8456),
    "kuala lumpur": (3.1390, 101.6869), "manila": (14.5995, 120.9842),
    # 其他
    "sao paulo": (-23.5505, -46.6333), "mexico city": (19.4326, -99.1332),
    "buenos aires": (-34.6037, -58.3816), "johannesburg": (-26.2041, 28.0473),
    "cairo": (30.0444, 31.2357), "lagos": (6.5244, 3.3792),
    "dubai": (25.2048, 55.2708), "istanbul": (41.0082, 28.9784),
}


def get_geolocation_for_ip(ip_info):
    """根据IP信息生成带抖动的地理坐标（模拟GPS精度误差±0.02-0.08°）。
    返回 {"latitude": float, "longitude": float, "accuracy": int} 或 None。
    """
    if not ip_info:
        return None
    city = (ip_info.get("city") or "").strip().lower()
    region = (ip_info.get("region") or "").strip().lower()
    # 尝试城市匹配
    coords = CITY_COORDINATES.get(city)
    if not coords and region:
        coords = CITY_COORDINATES.get(region)
    if not coords:
        # 回退：根据国家代码给一个粗略坐标
        cc = (ip_info.get("country_code") or "").upper()
        _country_centers = {
            "US": (39.8283, -98.5795), "CA": (56.1304, -106.3468),
            "GB": (55.3781, -3.4360), "DE": (51.1657, 10.4515),
            "FR": (46.2276, 2.2137), "JP": (36.2048, 138.2529),
            "AU": (-25.2744, 133.7751), "SG": (1.3521, 103.8198),
            "HK": (22.3193, 114.1694), "KR": (35.9078, 127.7669),
            "IN": (20.5937, 78.9629), "BR": (-14.2350, -51.9253),
        }
        coords = _country_centers.get(cc)
    if not coords:
        return None
    # 添加随机抖动（±0.02-0.08°，模拟GPS精度误差约2-8km）
    jitter_lat = random.uniform(-0.08, 0.08)
    jitter_lng = random.uniform(-0.08, 0.08)
    accuracy = random.randint(50, 500)  # GPS精度50-500米
    return {
        "latitude": round(coords[0] + jitter_lat, 6),
        "longitude": round(coords[1] + jitter_lng, 6),
        "accuracy": accuracy
    }

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
    
    # ★ 3.4 周末衰减因子：周六日流量乘以0.6-0.8（真实网站周末流量下降）
    import datetime as _dt_wk
    _weekday = _dt_wk.datetime.now().weekday()  # 0=Mon, 5=Sat, 6=Sun
    if _weekday >= 5:  # 周末
        _wk_cfg = cfg.get("weekend_factor", {"min": 0.6, "max": 0.8})
        _wk_factor = random.uniform(float(_wk_cfg.get("min", 0.6)), float(_wk_cfg.get("max", 0.8)))
        total_tasks_planned = max(1, int(total_tasks_planned * _wk_factor))
        log.info(f"📅 周末衰减: 系数{_wk_factor:.2f}，任务数调整为{total_tasks_planned}")

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
    
    # 2. ★ 3.5 地域分散策略：按config权重分配国家配额（同一目标站3-5国混合访问）
    _geo_cfg = cfg.get("geo_dispersion", {})
    _geo_weights = _geo_cfg.get("weights", {})
    country_quota_target = {}
    if _geo_cfg.get("enabled", False) and _geo_weights:
        # 使用配置权重
        _total_weight = sum(_geo_weights.get(cc, 0.1) for cc in enabled_countries)
        for cc in enabled_countries:
            _w = _geo_weights.get(cc, 0.1)
            quota = total_tasks_planned * (_w / _total_weight) * random.uniform(0.85, 1.15)
            country_quota_target[cc] = max(1, int(round(quota)))
        log.info(f"🌍 地域分散策略启用: 权重={_geo_weights}, 配额={country_quota_target}")
    else:
        # 兜底：基础平均 + ±20% 抖动
        base_quota = total_tasks_planned / len(enabled_countries)
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
        # 非自动模式：★ 3.1 时段权重曲线（按目标国时区的双峰分布）
        chosen_model = "hourly_weighted"
        _hourly_weights = cfg.get("hourly_weights", [0.2,0.1,0.1,0.1,0.2,0.3,0.5,0.8,1.0,1.0,0.9,0.8,0.7,0.7,0.8,0.9,1.0,1.0,0.9,0.8,0.7,0.5,0.4,0.3])
        # 按权重采样小时，然后在小时内随机分钟
        _hours_pool = list(range(24))
        _weights_sum = sum(_hourly_weights)
        _weights_norm = [w / _weights_sum for w in _hourly_weights]
        for i in range(total_tasks_planned):
            # 按权重随机选择小时
            _h = random.choices(_hours_pool, weights=_weights_norm, k=1)[0]
            _m = random.randint(0, 59)
            _s = random.randint(0, 59)
            # 转换为UTC秒数（假设目标国时区，简化处理：直接用UTC）
            tp = seconds_now_utc + (_h * 3600 + _m * 60 + _s)
            # 确保在24h窗口内
            if tp < end_of_window:
                raw_time_points.append(tp)
        # 补充：如果权重采样导致时间点不足，用均匀分布补齐
        if len(raw_time_points) < total_tasks_planned:
            est_task_len = (total_stay_cfg["min"] + total_stay_cfg["max"]) / 2
            avg_gap = (interval_cfg["min"] + interval_cfg["max"]) / 2
            cursor = seconds_now_utc
            while len(raw_time_points) < total_tasks_planned:
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
        
        # 任务间隔（增强随机性：非线性抖动 + 偶尔分心暂停 + 高熵随机源）
        if is_first:
            task_gap = 0
        else:
            base_gap = _secure_rng.uniform(interval_cfg["min"], interval_cfg["max"])
            # 10% 概率出现"分心暂停"（模拟用户去倒水、看手机等）
            if _secure_rng.random() < 0.10:
                base_gap += _secure_rng.uniform(30, 120)
            # 5% 概率出现"短暂快速操作"（模拟用户快速连续浏览）
            elif _secure_rng.random() < 0.05:
                base_gap = max(3, base_gap * 0.3)
            # 添加 ±15% 高斯微抖动
            task_gap = max(2, base_gap * (1 + _secure_rng.gauss(0, 0.15)))
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
            # ★ 第一天最低保底：即使晚间生成计划，也保证至少20%日任务量或12个任务
            _min_first_day = max(12, int(round(full_day_tasks * 0.20)))
            if planned_for_day < _min_first_day and remain_ratio > 0:
                planned_for_day = _min_first_day
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
    """★ P0-4: 多段三次贝塞尔曲线 + 生理微颤 + 速度钟形曲线（对抗ML轨迹分类器）"""
    import math as _m
    _sec = globals().get('_secure_rng') or random
    
    dist = _m.hypot(end_x - start_x, end_y - start_y)
    if dist < 3:
        page.mouse.move(end_x, end_y)
        return
    
    # 根据距离决定分段数（短距离2段，长距离3-4段）
    n_segments = max(2, min(4, int(dist / 150) + 1))
    
    # 生成中间路径点（每段终点）
    waypoints = [(start_x, start_y)]
    for i in range(1, n_segments):
        ratio = i / n_segments
        # 基础线性插值 + 垂直偏移（制造S形/弧形轨迹）
        bx = start_x + (end_x - start_x) * ratio
        by = start_y + (end_y - start_y) * ratio
        # 垂直方向偏移（最大偏移量随距离增大）
        perp_offset = _sec.gauss(0, dist * 0.08)
        angle = _m.atan2(end_y - start_y, end_x - start_x) + _m.pi / 2
        bx += perp_offset * _m.cos(angle)
        by += perp_offset * _m.sin(angle)
        waypoints.append((bx, by))
    waypoints.append((end_x, end_y))
    
    # 总步数基于距离（Fitts定律）
    total_steps = max(12, min(int(config.get("mouse_move_steps", {}).get("max", 250)), int(dist / 3)))
    steps_per_seg = total_steps // n_segments
    
    # 速度钟形曲线参数（先加速后减速，峰值在30%-40%处）
    peak_ratio = _sec.uniform(0.25, 0.40)
    
    # 生理微颤参数（8-12Hz人手自然震颤）
    tremor_freq = _sec.uniform(8.0, 12.0)  # Hz
    tremor_amp_x = _sec.uniform(0.3, 1.2)  # px
    tremor_amp_y = _sec.uniform(0.3, 1.2)  # px
    tremor_phase_x = _sec.uniform(0, 2 * _m.pi)
    tremor_phase_y = _sec.uniform(0, 2 * _m.pi)
    
    t_start = time.time()
    pause_prob = get_random_value(config.get("bezier_pause_prob", {"min": 0.05, "max": 0.2}))
    
    for seg_idx in range(n_segments):
        p0 = waypoints[seg_idx]
        p3 = waypoints[seg_idx + 1]
        # 三次贝塞尔控制点（制造每段内的自然弯曲）
        seg_dist = _m.hypot(p3[0] - p0[0], p3[1] - p0[1])
        ctrl_spread = seg_dist * _sec.uniform(0.2, 0.45)
        seg_angle = _m.atan2(p3[1] - p0[1], p3[0] - p0[0])
        p1 = (
            p0[0] + ctrl_spread * _m.cos(seg_angle + _sec.gauss(0, 0.3)),
            p0[1] + ctrl_spread * _m.sin(seg_angle + _sec.gauss(0, 0.3))
        )
        p2 = (
            p3[0] - ctrl_spread * _m.cos(seg_angle + _sec.gauss(0, 0.3)),
            p3[1] - ctrl_spread * _m.sin(seg_angle + _sec.gauss(0, 0.3))
        )
        
        for s in range(steps_per_seg):
            # 全局进度（0→1）
            global_t = (seg_idx * steps_per_seg + s) / total_steps
            # 局部进度（段内0→1）
            local_t = s / max(1, steps_per_seg - 1)
            
            # 速度钟形曲线映射（非匀速）
            if global_t < peak_ratio:
                eased = (global_t / peak_ratio) ** 0.6  # 加速段
            else:
                eased = 1.0 - ((1.0 - global_t) / (1.0 - peak_ratio)) ** 1.8  # 减速段（更慢）
            eased = max(0.0, min(1.0, eased))
            # 将eased映射回local_t
            local_eased = eased * n_segments - seg_idx
            local_eased = max(0.0, min(1.0, local_eased))
            
            # 三次贝塞尔插值
            t = local_eased
            mt = 1 - t
            bx = mt**3 * p0[0] + 3*mt**2*t * p1[0] + 3*mt*t**2 * p2[0] + t**3 * p3[0]
            by = mt**3 * p0[1] + 3*mt**2*t * p1[1] + 3*mt*t**2 * p2[1] + t**3 * p3[1]
            
            # ★ 生理微颤（8-12Hz正弦叠加，幅度随速度变化）
            elapsed = time.time() - t_start
            speed_factor = _m.sin(global_t * _m.pi)  # 中间快两端慢
            tremor_x = tremor_amp_x * speed_factor * _m.sin(2 * _m.pi * tremor_freq * elapsed + tremor_phase_x)
            tremor_y = tremor_amp_y * speed_factor * _m.sin(2 * _m.pi * tremor_freq * elapsed + tremor_phase_y)
            
            mx = int(bx + tremor_x + _sec.gauss(0, 0.3))
            my = int(by + tremor_y + _sec.gauss(0, 0.3))
            page.mouse.move(mx, my)
            
            # 步间等待（速度钟形：中间快、两端慢）
            base_pause = config.get("mouse_move_pause", {}).get("min", 0.008)
            speed_mod = 1.5 - speed_factor  # 两端慢
            time.sleep(max(0.004, base_pause * speed_mod * _sec.uniform(0.7, 1.3)))
            
            # 随机微停顿（模拟人类视觉校准）
            if _sec.random() < pause_prob * 0.3:
                time.sleep(_sec.uniform(0.05, 0.2))
    
    # 终点精确到位
    page.mouse.move(int(end_x), int(end_y))

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


# ★ P2-9: 跨会话行为画像记忆（每个站点+国家维护独立的行为模式，跨任务复用）
BEHAVIOR_PROFILE_DIR = "behavior_profiles"

def _bp_path(site_url, country):
    """Per-site行为画像文件路径"""
    import os
    site = _qa_safe_name(_qa_site_host(site_url))
    cc = _qa_safe_name((country or "US").upper())
    os.makedirs(os.path.join(os.getcwd(), BEHAVIOR_PROFILE_DIR), exist_ok=True)
    return os.path.join(os.getcwd(), BEHAVIOR_PROFILE_DIR, f"{site}_{cc}.json")

def load_behavior_profile(site_url, country):
    """加载跨会话行为画像（滚动偏好/点击热区/停留时长分布）"""
    path = _bp_path(site_url, country)
    profile = _qa_load_json(path, None)
    if profile is None:
        # 初始化默认画像
        profile = {
            "created_at": time.time(),
            "visit_count": 0,
            "avg_stay_sec": 0,
            "preferred_scroll_depth": 0.5,  # 0-1，页面滚动深度偏好
            "click_heatzone": "content_center",  # 点击热区偏好
            "preferred_content_types": [],  # 偏好的内容类型
            "scroll_speed_preference": "medium",  # slow/medium/fast
            "last_visit_at": 0,
            "total_pages_viewed": 0,
        }
    return profile

def save_behavior_profile(site_url, country, stats, profile):
    """任务结束后更新行为画像（指数移动平均，越新权重越大）"""
    import os
    path = _bp_path(site_url, country)
    alpha = 0.3  # EMA系数（新数据占30%权重）
    
    profile["visit_count"] = profile.get("visit_count", 0) + 1
    profile["last_visit_at"] = time.time()
    profile["total_pages_viewed"] = profile.get("total_pages_viewed", 0) + stats.get("pages_viewed", 1)
    
    # 更新平均停留时长（EMA）
    cur_stay = stats.get("total_stay", 0)
    if cur_stay > 0:
        old_stay = profile.get("avg_stay_sec", 0)
        profile["avg_stay_sec"] = old_stay * (1 - alpha) + cur_stay * alpha if old_stay > 0 else cur_stay
    
    # 更新滚动深度偏好
    scroll_dist = stats.get("scroll_distance", 0)
    if scroll_dist > 0:
        # 估算滚动深度（假设页面高度~3000px）
        est_depth = min(1.0, scroll_dist / 3000.0)
        old_depth = profile.get("preferred_scroll_depth", 0.5)
        profile["preferred_scroll_depth"] = old_depth * (1 - alpha) + est_depth * alpha
    
    # 更新点击热区偏好
    clicks = stats.get("clicks", 0)
    if clicks > 0:
        zones = ["content_center", "sidebar", "navigation", "footer"]
        # 简化：根据点击次数判断活跃度
        if clicks > 5:
            profile["click_heatzone"] = "content_center"
    
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def apply_behavior_profile_to_config(profile, config):
    """将行为画像应用到当前任务配置（微调行为参数，制造跨会话一致性）
    ★ 返回深拷贝，不修改原始config，防止全局配置污染"""
    import copy as _copy_bp
    if not profile or profile.get("visit_count", 0) < 2:
        return _copy_bp.deepcopy(config)  # 数据不足时返回副本
    
    config = _copy_bp.deepcopy(config)  # ★ 在副本上操作
    # 根据历史停留时长微调本次停留范围
    avg_stay = profile.get("avg_stay_sec", 0)
    if avg_stay > 30:
        # 老用户停留更久（模拟熟悉网站的浏览习惯）
        stay_cfg = config.get("total_stay", {"min": 120, "max": 300})
        config["total_stay"] = {
            "min": max(stay_cfg.get("min", 120), int(avg_stay * 0.8)),
            "max": max(stay_cfg.get("max", 300), int(avg_stay * 1.3))
        }
    
    # 根据滚动深度偏好微调滚动配置
    depth = profile.get("preferred_scroll_depth", 0.5)
    if depth > 0.7:
        config["scroll_pixels"] = {"min": 300, "max": 1200}  # 深度阅读者滚动更多
    elif depth < 0.3:
        config["scroll_pixels"] = {"min": 100, "max": 500}  # 浅层浏览者滚动少
    
    return config


app = Flask(__name__)
# ★ 5.3 日志轮转：RotatingFileHandler（maxBytes=10MB, backupCount=5，总占用≤50MB）
from logging.handlers import RotatingFileHandler as _RFH
_log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
_file_handler = _RFH('app.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, logging.StreamHandler()])

# ★ 5.2 Chromium僵尸进程清理（启动时执行一次 + 每30分钟定时清理）
def _cleanup_zombie_chromium():
    """清理运行超过10分钟的僵尸chromium进程"""
    import subprocess as _sp
    import os as _os_z
    try:
        # 查找运行超过10分钟的chromium进程（排除当前进程的父进程）
        _my_pid = _os_z.getpid()
        result = _sp.run(['ps', '-eo', 'pid,ppid,etime,comm'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n')[1:]:
                parts = line.split()
                if len(parts) >= 4 and 'chromium' in parts[3].lower():
                    pid, ppid, etime = int(parts[0]), int(parts[1]), parts[2]
                    # 跳过当前进程树
                    if pid == _my_pid or ppid == _my_pid:
                        continue
                    # 检查运行时间是否超过10分钟（格式: MM:SS 或 HH:MM:SS）
                    try:
                        if ':' in etime:
                            time_parts = etime.split(':')
                            minutes = int(time_parts[-2]) if len(time_parts) >= 2 else 0
                            if len(time_parts) == 3:
                                minutes += int(time_parts[0]) * 60
                            if minutes >= 10:
                                _sp.run(['kill', '-9', str(pid)], timeout=2)
                                log.info(f"🧹 清理僵尸chromium进程: PID={pid}, 运行时间={etime}")
                    except Exception:
                        pass
    except Exception as e:
        log.debug(f"僵尸进程清理异常: {e}")

# 启动时清理一次
_cleanup_zombie_chromium()
# 每30分钟定时清理
import threading as _th_z
def _schedule_cleanup():
    _cleanup_zombie_chromium()
    _th_z.Timer(1800, _schedule_cleanup).start()
_th_z.Timer(1800, _schedule_cleanup).start()

# ========== Flask 安全配置 ==========
import secrets
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ★ 高熵随机源（PDF风控要求：关键风控参数必须使用密码学安全随机数，而非伪随机）
# secrets.SystemRandom() 基于 os.urandom()，提供不可预测的随机数
_secure_rng = secrets.SystemRandom()

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
    "ad_click_prob": {"min": 0.01, "max": 0.03},
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

    # ★ HilltopAds Pop-under 弹窗触发配置
    "hilltopads": {
        "enabled": False,                      # 总开关：是否触发 Pop-under 弹窗
        "trigger_probability": 0.40,           # 40% 会话触发（模拟自然拦截率）
        "trigger_after_pct_min": 0.20,         # 模拟进度 20% 后触发（积累页面交互）
        "trigger_after_pct_max": 0.40,         # 最晚 40% 处触发
        "popunder_stay_min": 15,               # 弹窗最小存活秒数
        "popunder_stay_max": 25,               # 弹窗最大存活秒数
    },
    
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
            {"id": "tiktok", "name": "TikTok", "url": "https://www.tiktok.com/", "language": "en", "type": "social"},
            {"id": "goodreads", "name": "Goodreads", "url": "https://www.goodreads.com/", "language": "en", "type": "social"},
            {"id": "wattpad", "name": "Wattpad", "url": "https://www.wattpad.com/", "language": "en", "type": "social"},
            {"id": "quora", "name": "Quora", "url": "https://www.quora.com/", "language": "en", "type": "social"}
        ],
        "region_engine_map": {
            "US": ["google", "bing", "facebook", "twitter", "reddit", "instagram", "goodreads", "wattpad", "quora"],
            "GB": ["google", "bing", "facebook", "twitter", "reddit", "goodreads", "quora"],
            "AU": ["google", "bing", "facebook", "reddit", "instagram", "goodreads"],
            "DE": ["google", "bing", "facebook", "instagram"],
            "FR": ["google", "bing", "facebook", "instagram"],
            "JP": ["google", "bing", "twitter", "instagram", "tiktok"],
            "CN": ["baidu", "sogou", "tiktok"]
        },
        "keyword_pools": {
            "zh": ["免费小说在线阅读", "修真小说推荐", "武侠小说全本", "重生小说排行榜", "网络小说免费阅读", "修仙小说推荐", "穿越小说完本", "玄幻小说在线阅读", "都市重生小说", "仙侠小说免费阅读"],
            "en": ["free novels online read", "wuxia novels english translation", "cultivation novels free", "rebirth story novel", "xianxia novels online", "read free fiction online", "web novel free reading", "fantasy novel chapters free", "reincarnation novel english", "martial arts novel online", "transmigration story free", "best free novels to read", "novel reading website free", "chinese novel english translation", "immortal cultivation novel", "reborn novel free online", "free story books online", "read novels free no signup", "web fiction free chapters", "cultivation xianxia wuxia novel"]
        },
        "referer_mode": "dynamic",
        "search_mode": "real_search"
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
    # ★ 保护用户已保存的proxy_pool（列表类型不应被默认10空条目覆盖）
    _saved_proxy_pool = config.get('proxy_pool')
    fixed = deep_merge_defaults(DEFAULT_CONFIG, config)
    if _saved_proxy_pool and isinstance(_saved_proxy_pool, list) and len(_saved_proxy_pool) > 0:
        fixed['proxy_pool'] = _saved_proxy_pool
    config.clear()
    config.update(fixed)


task_running = False
_single_task_mode = False  # 单独任务模式标志：不影响网站任务状态显示
pending_plan = None
current_task_idx = -1  # 当前正在执行的任务索引（-1表示无）
current_plan = None    # 当前执行的计划
_last_executed_plan = None  # 保留最后一次执行的计划（供预览查看）

# ★ 断点恢复：计划进度持久化文件
PLAN_PROGRESS_FILE = "plan_progress.json"

def _save_plan_progress(plan, tasks_list):
    """保存当前计划进度到文件，支持停止后断点恢复"""
    try:
        import json as _json
        progress = {
            "plan": plan,
            "tasks_status": [t.get("status", "待执行") for t in tasks_list] if tasks_list else [],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(PLAN_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            _json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"保存计划进度失败: {e}")

def _load_plan_progress():
    """加载上次未完成的计划进度，返回(plan, tasks_status)或(None, None)"""
    try:
        import json as _json
        if not os.path.exists(PLAN_PROGRESS_FILE):
            return None, None
        with open(PLAN_PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        # 只恢复当天的计划（跨天不恢复）
        saved_at = data.get("saved_at", "")
        today = time.strftime("%Y-%m-%d")
        if not saved_at.startswith(today):
            log.info(f"📋 计划进度文件已过期（保存于{saved_at}），不恢复")
            return None, None
        return data.get("plan"), data.get("tasks_status", [])
    except Exception as e:
        log.warning(f"加载计划进度失败: {e}")
        return None, None

def _clear_plan_progress():
    """清除计划进度文件（计划全部完成时调用）"""
    try:
        if os.path.exists(PLAN_PROGRESS_FILE):
            os.remove(PLAN_PROGRESS_FILE)
    except Exception:
        pass

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
        self.max_session_duration = 600  # 10分钟（PDF风控要求≥10分钟，动态IP有效时长5-10分钟超时销毁）
        self.max_daily_visits = 4       # 24小时内不超过4次访问
        self.daily_window = 86400       # 24小时精确去重
        
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
    """UA 池管理器（P2-1：按国家+24小时段缓存7天，避免"同一国家每任务换UA"造成指纹瀑布异常）

    策略：
    - bucket_key = {country_code.upper()}|{YYYY-MM-DD_HH}（小时分段，模拟真实UA在短时间内不会抖动）
    - 同一 bucket 在 7 天内始终命中同一个 UA（主 UA），重复率受 ua_repeat_max_rate 约束；
      当命中次数 / 该 bucket 总使用次数 超过重复率上限，才在同语言池内重新挑一个。
    - 7 天窗口：超过 7 天的 bucket 记录自动丢弃（重启/进程存活期间均生效）。
    - 跨进程持久化：UA_BUCKET_FILE 落盘，下次启动加载，保证 VPS 重启后仍延续同一"国家-时间段 UA"。
    """

    UA_HISTORY_FILE = "ua_usage_history.json"
    UA_BUCKET_FILE = "ua_country_hour_buckets.json"
    WINDOW_HOURS = 24
    # P2-1 新增：bucket 级缓存有效期（天），与 country_host_7d 会话策略一致
    BUCKET_MAX_DAYS = 7

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
        # P2-1: 国家+小时段的 UA bucket 缓存，结构：
        # { "<CC>|<YYYY-MM-DD_HH>": {"ua": str, "created_at": ts, "hits": int, "total": int} }
        self.ua_buckets = {}
        self._bucket_lock = None
        try:
            import threading
            self._bucket_lock = threading.Lock()
        except Exception:
            self._bucket_lock = None

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

        # P2-1：加载 UA bucket 缓存（跨进程延续）
        if os.path.exists(self.UA_BUCKET_FILE):
            try:
                with open(self.UA_BUCKET_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                now = time.time()
                cutoff = now - self.BUCKET_MAX_DAYS * 86400
                cleaned = {}
                for k, v in raw.items():
                    try:
                        if not isinstance(v, dict):
                            continue
                        if float(v.get("created_at", 0) or 0) < cutoff:
                            continue
                        if not v.get("ua"):
                            continue
                        cleaned[k] = v
                    except Exception:
                        continue
                self.ua_buckets = cleaned
                self._safe_log("info", f"UA bucket 缓存加载成功，当前 {len(self.ua_buckets)} 个（国家+小时段）")
            except Exception as e:
                self._safe_log("warning", f"加载 UA bucket 缓存失败: {e}")
                self.ua_buckets = {}

    def _bucket_key(self, country_code, for_date=None):
        """生成 bucket key: {CC}|{YYYY-MM-DD_HH}"""
        import datetime
        cc = ((country_code or "xx").split("-")[0] or "xx").upper()[:8]
        now = for_date or datetime.datetime.now()
        try:
            hh = now.strftime("%H")
            d = now.strftime("%Y-%m-%d")
        except Exception:
            d = datetime.datetime.now().strftime("%Y-%m-%d")
            hh = datetime.datetime.now().strftime("%H")
        return f"{cc}|{d}_{hh}"

    def _save_buckets(self):
        import json
        import os
        try:
            # 持久化前清理过期记录（> BUCKET_MAX_DAYS 天）
            now = time.time()
            cutoff = now - self.BUCKET_MAX_DAYS * 86400
            cleaned = {}
            with self._lock_bucket():
                for k, v in self.ua_buckets.items():
                    try:
                        if float(v.get("created_at", 0) or 0) >= cutoff:
                            cleaned[k] = v
                    except Exception:
                        continue
                self.ua_buckets = cleaned
                payload = json.dumps(self.ua_buckets, ensure_ascii=False, indent=2)
            with open(self.UA_BUCKET_FILE, "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception as e:
            self._safe_log("debug", f"保存 UA bucket 缓存失败: {e}")

    def _lock_bucket(self):
        class _NoopLock:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
        return self._bucket_lock if self._bucket_lock is not None else _NoopLock()

    def _pick_from_pool_original(self, lang_prefix, browser_family):
        """沿用原有的"UA池 + 去重 + 重复率管控"逻辑，作为 bucket 缺失时的 UA 抽取器。

        注意：与原始 get_ua 完全等价，但不保存历史（由调用方统一在 bucket 层记录）。
        同时把全局重复率阈值从硬编码 0.2 改为读取 config.ua_repeat_max_rate（默认 0.2）。
        """
        import time
        import random
        self._clean_old_records()
        ua_pool = self._get_ua_pool(lang_prefix)
        if browser_family == "chromium":
            chromium_pool = [ua for ua in ua_pool if ("Chrome/" in ua or "Edg/" in ua or "Chromium/" in ua) and "Firefox/" not in ua and "Version/" not in ua]
            if chromium_pool:
                ua_pool = chromium_pool
            else:
                self._safe_log("warning", "Chromium UA 池为空，回退使用完整 UA 池")
        if self.total_ua_used > 0:
            current_repeat_rate = self.reused_ua_count / self.total_ua_used
        else:
            current_repeat_rate = 0
        try:
            max_rate = max(0.0, min(1.0, float(globals().get("config", {}).get("ua_repeat_max_rate", 0.2) or 0.2)))
        except Exception:
            max_rate = 0.2
        self._safe_log("debug", f"当前 UA 池大小: {len(ua_pool)}, 已使用 UA: {len(self.ua_history)}, 总任务: {self.total_ua_used}, 复用数: {self.reused_ua_count}, 重复率: {current_repeat_rate:.2%}, 阈值: {max_rate:.0%}")
        unused_uas = [ua for ua in ua_pool if ua not in self.ua_history]
        is_reused = False
        if unused_uas:
            selected_ua = random.choice(unused_uas)
            is_reused = False
            self._safe_log("debug", f"选择了新的 UA: {selected_ua[:60]}...")
        else:
            if current_repeat_rate < max_rate:
                sorted_uas = sorted(self.ua_history.items(), key=lambda x: x[1])
                selected_ua = sorted_uas[0][0]
                is_reused = True
                self._safe_log("debug", f"复用了 UA: {selected_ua[:60]}...")
            else:
                self._safe_log("warning", f"需要生成新的 UA 变体（当前重复率: {current_repeat_rate:.2%}，阈值: {max_rate:.0%}）")
                selected_ua = self._generate_ua_variant(random.choice(ua_pool))
                attempts = 0
                while selected_ua in self.ua_history and attempts < 20:
                    selected_ua = self._generate_ua_variant(random.choice(ua_pool))
                    attempts += 1
                is_reused = selected_ua in self.ua_history
        if not self._is_valid_ua(selected_ua):
            self._safe_log("warning", f"⚠️ 选中的 UA 格式非法，已回退: {selected_ua[:60]}")
            _valid_candidates = [ua for ua in ua_pool if self._is_valid_ua(ua)]
            if _valid_candidates:
                selected_ua = random.choice(_valid_candidates)
                is_reused = selected_ua in self.ua_history
        self.ua_history[selected_ua] = time.time()
        self.total_ua_used += 1
        if is_reused:
            self.reused_ua_count += 1
        self._save_history()
        new_repeat_rate = self.reused_ua_count / self.total_ua_used if self.total_ua_used > 0 else 0
        if new_repeat_rate >= max_rate:
            self._safe_log("warning", f"⚠️ 当前 UA 重复率: {new_repeat_rate:.2%}（超过阈值 {max_rate:.0%}），总任务: {self.total_ua_used}，复用: {self.reused_ua_count}")
        elif new_repeat_rate >= max(max_rate - 0.05, 0.0):
            self._safe_log("info", f"UA 重复率: {new_repeat_rate:.2%}（接近阈值 {max_rate:.0%}）")
        return selected_ua

    def get_ua(self, lang_prefix, browser_family="chromium", country_code=None):
        """P2-1 升级版入口：优先按 {国家+小时段} 命中缓存 UA，否则回退原池逻辑并回填缓存。

        参数:
            country_code: 可选，传 fingerprint.country_code（如 US/JP/CN），不传则退化为原全局去重。
        """
        import time
        if not country_code:
            # 无国家信息 → 退化为原行为
            return self._pick_from_pool_original(lang_prefix, browser_family)
        bucket_key = self._bucket_key(country_code)
        now = time.time()
        with self._lock_bucket():
            cached = self.ua_buckets.get(bucket_key)
            need_new = True
            if cached and isinstance(cached, dict):
                try:
                    if float(cached.get("created_at", 0) or 0) >= now - self.BUCKET_MAX_DAYS * 86400 and cached.get("ua"):
                        need_new = False
                except Exception:
                    need_new = True
            if not need_new:
                # 命中 bucket，在阈值内优先复用
                ua = cached["ua"]
                total = int(cached.get("total", 0) or 0) + 1
                hits = int(cached.get("hits", 0) or 0) + 1
                cached["total"] = total
                cached["hits"] = hits
                # 在 ua_history 里也记一次（保证重复率数据一致）
                is_reused = ua in self.ua_history
                self.ua_history[ua] = now
                self.total_ua_used += 1
                if is_reused:
                    self.reused_ua_count += 1
                # 当该 bucket 重复率（hits/total）超过 ua_repeat_max_rate，则重抽一次降低同 bucket 命中
                try:
                    max_rate = max(0.0, min(1.0, float(globals().get("config", {}).get("ua_repeat_max_rate", 0.2) or 0.2)))
                except Exception:
                    max_rate = 0.2
                if max_rate > 0 and total > 4 and (hits / total) > max_rate + 0.1:
                    # 重复率过高，标记需要重新抽取并记录（不要直接删除，保留到下一个小时段自然过期）
                    cached["over_rate"] = True
                    self._safe_log(
                        "info",
                        f"[UA bucket] {bucket_key} 命中重复率 {hits/total:.2%} > 阈值+0.1，本次强制重抽（保留缓存避免抖动）"
                    )
                else:
                    # 正常命中：落盘并返回
                    try:
                        self._save_history()
                        # 间隔落盘 bucket（每 12 次命中或 5% 概率，避免频繁IO）
                        if total % 12 == 0 or random.random() < 0.05:
                            self._save_buckets()
                    except Exception:
                        pass
                    return ua
        # bucket 缺失/过期/超过重复率 → 走原 UA 池逻辑并回填 bucket
        ua = self._pick_from_pool_original(lang_prefix, browser_family)
        try:
            with self._lock_bucket():
                self.ua_buckets[bucket_key] = {
                    "ua": ua,
                    "created_at": now,
                    "hits": 1,
                    "total": 1,
                    "lang_prefix": lang_prefix,
                    "country": (country_code or "").upper()[:8],
                }
            # 异步/低概率落盘（避免高频 IO）
            try:
                import random as _r
                if _r.random() < 0.3:
                    self._save_buckets()
            except Exception:
                pass
        except Exception as e:
            self._safe_log("debug", f"回填 UA bucket 失败: {e}")
        return ua
    
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
    """是否已达今日单日点击上限（config.daily_ad_click_limit={min,max}，每天随机选取上限值，0=不限）"""
    try:
        cfg = config.get("daily_ad_click_limit", {"min": 0, "max": 0})
        if isinstance(cfg, dict):
            _min = int(cfg.get("min", 0) or 0)
            _max = int(cfg.get("max", 0) or 0)
        else:
            # 兼容旧格式（单个整数）
            _min = _max = int(cfg or 0)
        if _max <= 0:
            return False  # 0=不限
        # 每天随机确定当日上限（缓存在fingerprint_stats中，同一天不变）
        import datetime as _dt
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        daily_limits = fingerprint_stats.setdefault("daily_ad_click_limits", {})
        if today not in daily_limits:
            daily_limits[today] = random.randint(_min, _max) if _min < _max else _max
            save_fingerprint_stats()
        limit = daily_limits[today]
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
    """心跳监督线程：定期检查 last_heartbeat，超时则清除 running 标志位。"""
    while True:
        try:
            human_model_stop_event.wait(timeout=8)
        except Exception:
            pass
        if human_model_stop_event.is_set():
            return
        try:
            with human_model_lock:
                hb = human_model_state.get("last_heartbeat", 0)
                running = human_model_state.get("running", False)
                src = human_model_state.get("last_source", "")
            if running and time.time() - hb > 30:
                log.warning(
                    f"[HumanModel] 心跳丢失超过30s(last_source={src})，"
                    f"标记监督退出(任务侧检测后会重启)"
                )
                with human_model_lock:
                    human_model_state["running"] = False
                    human_model_state["last_error"] = f"heartbeat_timeout_from_{src}"
            else:
                continue
        except Exception as _he:
            log.debug(f"[HumanModel] supervisor 异常忽略: {_he}")
        # 本轮超时处理完后再次 wait
        human_model_stop_event.clear()


def start_human_model(task_type):
    global human_model_thread
    with human_model_lock:
        human_model_state.update({
            "running": True,
            "task_type": task_type,
            "last_heartbeat": time.time(),
            "last_source": "start",
            "last_error": "",
        })
    human_model_stop_event.clear()
    need_start = False
    with human_model_lock:
        if human_model_thread is None or not human_model_thread.is_alive():
            need_start = True
    if need_start:
        t = threading.Thread(target=_human_model_supervisor_loop, daemon=True)
        t.start()
        with human_model_lock:
            human_model_thread = t
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
    """保证真人模型心跳存在：心跳丢失>20s就自动重启 supervisor + 重设 running。

    返回 True（调用方不用再判断），但日志会记录每次重启事件。
    """
    global human_model_thread
    try:
        with human_model_lock:
            s = human_model_state
            running = s.get("running", False)
            if not running:
                return True
            hb = s.get("last_heartbeat", 0)
        if time.time() - hb > 20:
            log.warning("[HumanModel] 心跳丢失 20s，重启 supervisor 并重设 running=True")
            human_model_stop_event.set()
            time.sleep(0.6)
            human_model_stop_event.clear()
            with human_model_lock:
                human_model_state["last_heartbeat"] = time.time()
                human_model_state["running"] = True
                human_model_state["last_source"] = "ensure_alive_reboot"
                t = threading.Thread(target=_human_model_supervisor_loop, daemon=True)
                t.start()
                human_model_thread = t
    except Exception as _e:
        log.debug(f"[HumanModel] ensure_alive 异常忽略: {_e}")
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
    # ★ 高熵随机源用于关键风控参数（动作选择、间隔计算）
    _sec = globals().get('_secure_rng') or _rnd

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
    mouse_count_cfg = config.get("mouse_move_count", {"min": 2, "max": 20})

    # ★ QA真人模型强度：调节动作频率和停顿
    _profile = config.get("qa_human_profile", "standard")
    if _profile == "light":
        _action_gap_range = (1.5, 4.0)  # 动作间隔更长
        _scroll_max = max(3, int(scroll_count_cfg.get("min", 2)))  # 滚动次数取下限
        _mouse_max = max(2, int(mouse_count_cfg.get("min", 2)))  # 鼠标次数取下限
    elif _profile == "heavy":
        _action_gap_range = (0.3, 1.2)  # 动作间隔更短
        _scroll_max = int(scroll_count_cfg.get("max", 10))  # 滚动次数取上限
        _mouse_max = int(mouse_count_cfg.get("max", 20))  # 鼠标次数取上限
    else:  # standard
        _action_gap_range = (0.5, 2.0)
        _scroll_max = _rnd.randint(int(scroll_count_cfg.get("min", 2)), int(scroll_count_cfg.get("max", 10)))
        _mouse_max = _rnd.randint(int(mouse_count_cfg.get("min", 2)), int(mouse_count_cfg.get("max", 20)))

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

    # 动作权重：滚动和鼠标为主，点击权重从配置读取（确保真人行为包含点击）
    _click_weight = max(2, int(click_count_cfg.get("min", 3)) // 2)  # 至少2，配置min=3→权重2
    _key_weight = max(1, _click_weight // 2)
    # ★ 移动端手势检测：如果UA包含移动设备关键词，添加触摸手势动作
    _is_mobile_context = any(kw in (page.context.options.get('user_agent', '') or '') for kw in ("Android", "Mobile", "iPhone", "iPad"))
    _touch_actions = ["touch_swipe", "touch_tap"] if _is_mobile_context else []
    if _is_mobile_context:
        log.info(f"[真人模拟] 检测到移动端UA，启用触摸手势模拟（滑动/点击）")
    actions = (
        ["scroll"] * 4 +
        ["mouse"] * 4 +
        ["click"] * _click_weight +
        ["key"] * _key_weight +
        ["text_select"] * 2 +  # ★ 文字选中/复制/粘贴复合行为（PDF风控要求）
        _touch_actions * 3  # ★ 移动端手势：滑动/点击（仅移动设备）
    )

    action_errors = 0
    loop_count = 0
    summary_interval = 6
    next_summary_at = summary_interval
    bezier_prob = _rnd.uniform(bezier_pause_cfg.get("min", 0.05), bezier_pause_cfg.get("max", 0.2))
    # ★ 动作计数器：用于限制滚动/鼠标次数（从配置读取上限）
    _scroll_done = 0
    _mouse_done = 0
    # ★ 疲劳模拟参数（PDF风控要求：长会话后期放缓操作速度/增加停顿，模拟人类疲劳）
    # 疲劳系数：0.0（初始精力充沛）→ 1.0（极度疲劳），根据已运行时长/总时长比例计算
    _fatigue_duration_threshold = 120.0  # 超过120秒后开始显现疲劳
    _fatigue_max_multiplier = 2.5  # 最大停顿倍数（疲劳时停顿时间延长2.5倍）
    _fatigue_speed_min = 0.4  # 最小速度系数（疲劳时鼠标/滚动速度降低到40%）
    _fatigue_gap_multiplier = 1.0  # ★ 初始值（循环内会动态更新）
    _fatigue_speed_factor = 1.0  # ★ 初始值（循环内会动态更新）

    while True:
        human_model_tick(page_name)
        if not task_running or not ensure_human_model_alive():
            break
        remaining = window_end - _t.time()
        if remaining <= 0:
            break
        
        # 动作间隔：从配置读取（受qa_human_profile调节 + 疲劳系数延长）
        gap = min(remaining, _rnd.uniform(_action_gap_range[0], _action_gap_range[1]) * _fatigue_gap_multiplier)
        _t.sleep(gap)
        stats["total_stay"] += gap
        if _t.time() >= window_end or not task_running:
            break

        loop_count += 1
        action = _sec.choice(actions)  # ★ 高熵随机选择动作（防止伪随机被预测）
        # ★ 疲劳系数计算：超过阈值后线性增长，0.0→1.0
        _elapsed = _t.time() - start
        _fatigue = max(0.0, min(1.0, (_elapsed - _fatigue_duration_threshold) / max(1.0, duration - _fatigue_duration_threshold))) if duration > _fatigue_duration_threshold else 0.0
        # 疲劳影响：动作间隔延长、鼠标/滚动速度降低
        _fatigue_gap_multiplier = 1.0 + _fatigue * (_fatigue_max_multiplier - 1.0)  # 1.0→2.5
        _fatigue_speed_factor = 1.0 - _fatigue * (1.0 - _fatigue_speed_min)  # 1.0→0.4
        try:
            if action == "scroll":
                # ★ 滚动次数限制：达到配置上限后跳过
                if _scroll_done >= _scroll_max:
                    continue
                # ★ 2.1 惯性滚动模型：初始速度v0从配置scroll_pixels读取
                _v0 = _rnd.uniform(float(scroll_cfg.get("min", 200)), float(scroll_cfg.get("max", 1000)))  # 初始速度 px/s
                _direction = -1 if _rnd.random() < 0.15 else 1
                _inertia_js = """
                    (v0, direction) => {
                        return new Promise(resolve => {
                            let v = v0 * direction;
                            let totalDist = 0;
                            const decay = 0.88;
                            const minV = 20;
                            function frame() {
                                if (Math.abs(v) < minV) { resolve(totalDist); return; }
                                window.scrollBy(0, v * 0.016);
                                totalDist += Math.abs(v * 0.016);
                                v *= decay;
                                requestAnimationFrame(frame);
                            }
                            requestAnimationFrame(frame);
                        });
                    }
                """
                try:
                    _scrolled = page.evaluate(_inertia_js, [_v0, _direction])
                    _scrolled = int(abs(_scrolled or 0))
                except Exception:
                    _scrolled = _rnd.randint(int(scroll_cfg.get("min", 100)), int(scroll_cfg.get("max", 800)))
                    page.evaluate(f"window.scrollBy(0, {_scrolled * _direction})")
                stats["scrolls"] += 1
                _scroll_done += 1
                stats["scroll_distance"] += _scrolled
                # 滚动后等待（从配置读取）
                _sw = min(window_end - _t.time(), _rnd.uniform(scroll_wait_cfg.get("min", 0.5), scroll_wait_cfg.get("max", 2.0)))
                if _sw > 0:
                    _t.sleep(_sw)
                    stats["total_stay"] += _sw
            elif action == "mouse":
                # ★ 鼠标移动次数限制：达到配置上限后跳过
                if _mouse_done >= _mouse_max:
                    continue
                # ★ 2.4 注意力热区模型：鼠标目标不再完全随机，按热区权重采样
                _hotzone = _rnd.random()
                if _hotzone < 0.40:  # 内容区域中心偏上(40%)
                    tx = _rnd.randint(200, 900)
                    ty = _rnd.randint(150, 450)
                elif _hotzone < 0.60:  # 导航栏区域(20%)
                    tx = _rnd.randint(100, 1000)
                    ty = _rnd.randint(20, 80)
                elif _hotzone < 0.75:  # 侧边栏(15%)
                    tx = _rnd.randint(900, 1150)
                    ty = _rnd.randint(100, 600)
                elif _hotzone < 0.85:  # 底部(10%)
                    tx = _rnd.randint(100, 1000)
                    ty = _rnd.randint(600, 800)
                else:  # 随机(15%)
                    tx = _rnd.randint(50, 1150)
                    ty = _rnd.randint(50, 750)
                # ★ 2.2 步数从配置mouse_move_steps读取（Fitts定律作为微调因子）
                import math as _math_f
                _dist_f = _math_f.hypot(tx - current_x, ty - current_y)
                _fitts_factor = max(0.5, min(1.5, _math_f.log2(_dist_f / 100 + 1)))  # 距离微调±50%
                _cfg_steps = _rnd.randint(int(mouse_steps_cfg.get("min", 50)), int(mouse_steps_cfg.get("max", 250)))
                steps = max(8, int(_cfg_steps * _fitts_factor))
                steps = min(steps, int(mouse_steps_cfg.get("max", 250)))  # 上限从配置读取
                # 生成随机控制点，制造自然弯曲
                ctrl_x = _rnd.uniform(min(current_x, tx), max(current_x, tx))
                ctrl_y = _rnd.uniform(min(current_y, ty) - 80, max(current_y, ty) + 80)
                for s in range(steps):
                    if _t.time() >= window_end:
                        break
                    tt = (s + 1) / steps
                    # ★ ease-in-out 缓动：先加速后减速（符合Fitts定律）
                    if tt < 0.5:
                        eased_tt = 4 * tt * tt * tt
                    else:
                        eased_tt = 1 - ((-2 * tt + 2) ** 3) / 2
                    # 二次贝塞尔曲线
                    bx = (1-eased_tt)**2 * current_x + 2*(1-eased_tt)*eased_tt * ctrl_x + eased_tt**2 * tx
                    by = (1-eased_tt)**2 * current_y + 2*(1-eased_tt)*eased_tt * ctrl_y + eased_tt**2 * ty
                    # 微小抗动（±1-2px，模拟人手微颤）
                    mx = int(bx + _rnd.randint(-2, 2))
                    my = int(by + _rnd.randint(-2, 2))
                    page.mouse.move(mx, my)
                    # 每步等待从配置读取（受疲劳系数影响：疲劳时步间等待延长）
                    _step_wait = _rnd.uniform(mouse_pause_cfg.get("min", 0.01), mouse_pause_cfg.get("max", 0.1)) / max(0.1, _fatigue_speed_factor)
                    _t.sleep(_step_wait)
                    # 贝塞尔暂停概率
                    if _rnd.random() < bezier_prob:
                        _t.sleep(_rnd.uniform(0.1, 0.4))
                current_x, current_y = tx, ty
                stats["mouse_moves"] += 1
                _mouse_done += 1
                # ★ 2.4 阅读停顿：每3-5次鼠标移动后插入1-4s停顿
                if stats["mouse_moves"] % _rnd.randint(3, 5) == 0:
                    _read_pause = _rnd.uniform(1.0, 4.0)
                    _t.sleep(min(_read_pause, max(0, window_end - _t.time())))
                    stats["total_stay"] += _read_pause
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
            elif action == "text_select":
                # ★ 文字选中/复制/粘贴复合行为（模拟真人阅读时选中文字、复制、偶尔粘贴）
                try:
                    _select_js = """
                    () => {
                        // 查找页面中的可见文本节点（p, span, div, li, td, h1-h6, a）
                        const textEls = document.querySelectorAll('p, span, li, td, h1, h2, h3, h4, h5, h6, a, blockquote');
                        const candidates = [];
                        for (const el of textEls) {
                            const text = (el.textContent || '').trim();
                            if (text.length >= 20 && text.length <= 500) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 50 && rect.height > 10 && rect.top > 0 && rect.top < window.innerHeight) {
                                    candidates.push({el, text, rect});
                                }
                            }
                        }
                        if (candidates.length === 0) return null;
                        const chosen = candidates[Math.floor(Math.random() * candidates.length)];
                        const text = chosen.text;
                        // 随机选取文字的一个子串（模拟拖拽选中）
                        const start = Math.floor(Math.random() * Math.max(1, text.length - 10));
                        const selLen = Math.min(text.length - start, Math.floor(Math.random() * 40) + 5);
                        // 创建Range并选中
                        const range = document.createRange();
                        const sel = window.getSelection();
                        sel.removeAllRanges();
                        try {
                            const walker = document.createTreeWalker(chosen.el, NodeFilter.SHOW_TEXT);
                            let node, charCount = 0, startNode = null, startOffset = 0, endNode = null, endOffset = 0;
                            while ((node = walker.nextNode())) {
                                const nodeLen = node.textContent.length;
                                if (!startNode && charCount + nodeLen > start) {
                                    startNode = node;
                                    startOffset = start - charCount;
                                }
                                if (charCount + nodeLen >= start + selLen) {
                                    endNode = node;
                                    endOffset = start + selLen - charCount;
                                    break;
                                }
                                charCount += nodeLen;
                            }
                            if (startNode && endNode) {
                                range.setStart(startNode, startOffset);
                                range.setEnd(endNode, Math.min(endOffset, endNode.textContent.length));
                                sel.addRange(range);
                                return {success: true, selected: text.substring(start, start + selLen)};
                            }
                        } catch(e) {}
                        return null;
                    }
                    """
                    _sel_result = page.evaluate(_select_js)
                    if _sel_result and _sel_result.get("success"):
                        stats.setdefault("text_selections", 0)
                        stats["text_selections"] += 1
                        # 模拟真人选中后短暂停顿（阅读选中内容）
                        _t.sleep(_rnd.uniform(0.3, 1.2))
                        # 复制 (Ctrl+C / Cmd+C)
                        page.keyboard.press("Control+c")
                        _t.sleep(_rnd.uniform(0.2, 0.8))
                        # 30%概率执行粘贴（模拟真人复制后粘贴到搜索框等）
                        if _rnd.random() < 0.3:
                            # 尝试找到页面搜索框并粘贴
                            _paste_js = """
                            () => {
                                const inputs = document.querySelectorAll('input[type="text"], input[type="search"], input:not([type]), textarea');
                                for (const inp of inputs) {
                                    const rect = inp.getBoundingClientRect();
                                    if (rect.width > 80 && rect.height > 15 && rect.top > 0 && rect.top < window.innerHeight) {
                                        inp.focus();
                                        return true;
                                    }
                                }
                                return false;
                            }
                            """
                            _found_input = page.evaluate(_paste_js)
                            if _found_input:
                                page.keyboard.press("Control+v")
                                _t.sleep(_rnd.uniform(0.5, 1.5))
                                # 粘贴后清空（避免影响页面状态）
                                page.keyboard.press("Control+a")
                                page.keyboard.press("Backspace")
                except Exception:
                    pass  # 文字选中失败不影响任务流程
            elif action == "touch_swipe":
                # ★ 移动端触摸手势：滑屏（模拟手指上下滑动浏览）
                try:
                    _sx = _rnd.randint(100, 500)
                    _sy = _rnd.randint(200, 600)
                    _ex = _sx + _rnd.randint(-50, 50)  # 水平微偏
                    _ey = _sy + _rnd.choice([-1, 1]) * _rnd.randint(100, 300)  # 上下滑动100-300px
                    # Playwright touch: 使用 page.touchscreen 或 mouse 模拟
                    page.mouse.move(_sx, _sy)
                    page.mouse.down()
                    # 分步滑动（模拟手指滑动轨迹）
                    _steps = _rnd.randint(5, 15)
                    for _i in range(_steps):
                        _t_val = (_i + 1) / _steps
                        _cx = _sx + (_ex - _sx) * _t_val + _rnd.uniform(-2, 2)
                        _cy = _sy + (_ey - _sy) * _t_val + _rnd.uniform(-2, 2)
                        page.mouse.move(_cx, _cy)
                        _t.sleep(_rnd.uniform(0.01, 0.03))
                    page.mouse.up()
                    stats.setdefault("touch_gestures", 0)
                    stats["touch_gestures"] += 1
                    _t.sleep(_rnd.uniform(0.3, 0.8))  # 滑动后等待
                except Exception:
                    pass
            elif action == "touch_tap":
                # ★ 移动端触摸手势：轻触点击（模拟手指点击）
                try:
                    _tx = _rnd.randint(100, 600)
                    _ty = _rnd.randint(100, 800)
                    page.mouse.move(_tx, _ty)
                    _t.sleep(_rnd.uniform(0.05, 0.15))  # 短暂停顿
                    page.mouse.click(_tx, _ty)
                    stats.setdefault("touch_gestures", 0)
                    stats["touch_gestures"] += 1
                    _t.sleep(_rnd.uniform(0.3, 1.0))
                except Exception:
                    pass
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
                // Google AdSense
                'ins.adsbygoogle',
                '.adsbygoogle',
                '[id*="google_ads_iframe"]',
                '[id^="google_ads_iframe"]',
                'iframe[id*="google_ads"]',
                'iframe[name*="google_ads"]',
                'iframe[src*="googlesyndication"]',
                'iframe[src*="doubleclick"]',
                '[data-ad-client]',
                '[data-ad-slot]',
                // HilltopAds
                'iframe[src*="hilltopads"]',
                'script[src*="hilltopads"]',
                '[id*="hilltopads"]',
                '[class*="hilltopads"]',
                // PropellerAds / AdMaven / EvaDav / other networks
                'iframe[src*="propellerads"]',
                'iframe[src*="ad-maven"]',
                'iframe[src*="evadav"]',
                'iframe[src*="mgid"]',
                'iframe[src*="taboola"]',
                'iframe[src*="outbrain"]',
                'script[src*="propellerads"]',
                'script[src*="evadav"]',
                'script[src*="mgid"]',
                'script[src*="taboola"]',
                'script[src*="outbrain"]',
                // HilltopAds/EvaDav 投放域名（随机域名，通过白名单确认）
                'script[src*="curoax"]', 'iframe[src*="curoax"]',
                'script[src*="pufted"]', 'iframe[src*="pufted"]',
                'iframe[src*="bony-teaching"]', 'script[src*="bony-teaching"]',
                'script[src*="untimely-hello"]', 'iframe[src*="untimely-hello"]',
                // Ezoic / Mediavine / AdThrive / Raptive
                'script[src*="ezoic"]',
                'script[src*="ezoicnet"]',
                '[id*="ezoic"]',
                'script[src*="mediavine"]',
                '[class*="mediavine"]',
                'script[src*="adthrive"]',
                'script[src*="raptive"]',
                // Monumetric / Broadstreet
                'script[src*="monumetric"]',
                'script[src*="broadstreet"]',
                // Infolinks / Adsterra
                'script[src*="infolinks"]',
                'script[src*="adsterra"]',
                // BuySellAds / Carbon
                'script[src*="buysellads"]',
                'script[src*="carbonads"]',
                // GAM / Google Publisher Tag
                'script[src*="securepubads"]',
                'script[src*="googletagservices"]',
                // 通用广告容器（覆盖大多数联盟）
                'iframe[src*="/ads/"], iframe[src*="/adserve/"], iframe[src*="/adserver/"]',
                'iframe[src*="banner"]',
                '[class*="nativeads"]',
                '[class*="ad-container"]',
                '[class*="ad-wrapper"]',
                '[class*="ad-unit"]',
                '[id*="ad-container"]',
                '[id*="ad-wrapper"]',
                '[id*="ad-unit"]',
                'iframe[width="728"][height="90"]',
                'iframe[width="300"][height="250"]',
                'iframe[width="160"][height="600"]',
                '[data-zone]',
                '[data-adzone]',
                '[data-ad-id]',
                '[data-adunit]'
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
            // ★ 通用跨域iframe检测器：捕获HilltopAds/EvaDav等随机投放域名的广告iframe
            // 原理：广告iframe通常是跨域的、有合理尺寸的、且不属于已知非广告嵌入
            const _nonAdDomains = ['youtube.com','youtu.be','vimeo.com','dailymotion.com','twitter.com','x.com','facebook.com','instagram.com','tiktok.com','spotify.com','soundcloud.com','maps.google','recaptcha','hcaptcha','disqus.com','paypal.com','stripe.com'];
            const _pageHost = window.location.hostname;
            document.querySelectorAll('iframe').forEach((el) => {
                if (seen.has(el)) return;
                const _src = el.getAttribute('src') || '';
                if (!_src || _src === 'about:blank' || _src.startsWith('javascript:')) return;
                // 跳过同源iframe
                try { if (new URL(_src, window.location.href).hostname === _pageHost) return; } catch(e) {}
                // 跳过已知非广告嵌入
                const _srcLower = _src.toLowerCase();
                if (_nonAdDomains.some(d => _srcLower.includes(d))) return;
                const r = el.getBoundingClientRect();
                const w = Math.round(r.width || 0);
                const h = Math.round(r.height || 0);
                // 广告iframe通常宽度>=160且高度>=90
                if (w < 160 || h < 90) return;
                // 排除超大iframe（可能是页面布局框架）
                if (w > vw * 0.95 && h > vh * 0.95) return;
                seen.add(el);
                const style = window.getComputedStyle(el);
                const id = el.id || '';
                const cls = typeof el.className === 'string' ? el.className : '';
                const key = id || `ADFRAME:${cls}:${Math.round(r.left)}:${Math.round(r.top)}:${w}x${h}`;
                const visible = style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
                const inViewport = visible && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
                let visibleRatio = 0;
                if (inViewport && w > 0 && h > 0) {
                    const visW = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
                    const visH = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                    visibleRatio = (visW * visH) / (w * h);
                }
                const exposed = inViewport && w >= 20 && h >= 20;
                const exposed50 = exposed && visibleRatio >= 0.5;
                items.push({key, tag: 'IFRAME', id, cls, src: _src, width: w, height: h, visible, inViewport, exposed, exposed50, visibleRatio: Math.round(visibleRatio*100)/100, loaded: true});
            });
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
        # ★ Popunder观察（只读，合规）：统计浏览器额外打开的窗口/标签数，
        # 用于确认 HilltopAds 等 Popunder 广告位是否真正触发（弹窗打开才计展示）
        try:
            _extra_wins = max(0, len(page.driver.window_handles) - 1)
            ad_monitor["popunder_max_windows"] = max(int(ad_monitor.get("popunder_max_windows", 0) or 0), _extra_wins)
        except Exception:
            _extra_wins = -1
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
            f"加载={loaded_count} 刷新={ad_monitor['refresh_count']} 弹窗窗口数={_extra_wins}"
        )
    except Exception as e:
        ad_monitor["scan_count"] = ad_monitor.get("scan_count", 0) + 1
        log.warning(f"[广告监控][{stage}] 扫描失败: {str(e)[:120]}")
    return ad_monitor


# ★ 广告点击风控：任务级冷却时间 + 广告去重
_ad_click_last_ts = 0  # 上次广告点击时间戳（全局，跨任务冷却）
_ad_click_cooldown = 180  # 冷却时间（秒），同一IP/会话两次点击间隔至少3分钟
_ad_clicked_positions = set()  # 当前任务已点击的广告坐标key（任务结束后清空）

def reset_ad_click_tracking():
    """每个新任务开始时调用，清空任务级去重记录"""
    global _ad_clicked_positions
    _ad_clicked_positions = set()

def try_click_visible_ad(page, config, current_x, current_y, stage="页面"):
    """★ 广告点击核心函数：按配置概率随机点击可见广告。
    在每次 scan_ads_during_task 之后调用，实现“检测到广告→概率点击”闭环。
    返回 (clicked: bool, current_x, current_y)
    """
    global _ad_click_last_ts, _ad_clicked_positions
    try:
        # 1. 每日上限检查
        if daily_ad_click_limit_reached():
            return False, current_x, current_y

        # 1.5 ★ 风控冷却：两次广告点击间隔至少3分钟（模拟真人不会连续点击广告）
        _now = time.time()
        if _now - _ad_click_last_ts < _ad_click_cooldown:
            return False, current_x, current_y

        # 1.6 ★ P1-5: 广告交互前停留≥8秒（模拟阅读行为，防止页面刚加载就点广告）
        _ad_min_page_stay = config.get("ad_min_page_stay", 8.0)  # 最少停留秒数
        try:
            _page_load_ts = page.evaluate("performance.timing.navigationStart || performance.timeOrigin") or 0
            if _page_load_ts > 0:
                _page_stay_sec = (_now * 1000 - _page_load_ts) / 1000.0  # 转换为秒
                if _page_stay_sec < _ad_min_page_stay:
                    log.debug(f"[广告点击][风控] 页面停留仅{_page_stay_sec:.1f}s < {_ad_min_page_stay}s，延迟点击")
                    return False, current_x, current_y
        except Exception:
            pass  # 获取navigationStart失败不阻断流程

        # 2. 概率掷骰
        ad_click_prob_cfg = config.get("ad_click_prob", {"min": 0.005, "max": 0.05})
        prob = random.uniform(float(ad_click_prob_cfg.get("min", 0.005)), float(ad_click_prob_cfg.get("max", 0.05)))
        if random.random() >= prob:
            return False, current_x, current_y

        # 3. 通过JS查找当前视口内可见的广告元素及其坐标
        ad_positions = page.evaluate("""
        () => {
            const selectors = [
                'ins.adsbygoogle', '.adsbygoogle',
                '[id*="google_ads_iframe"]', 'iframe[src*="googlesyndication"]',
                'iframe[src*="doubleclick"]', '[data-ad-client]', '[data-ad-slot]',
                'iframe[src*="hilltopads"]', 'iframe[src*="propellerads"]',
                'iframe[src*="evadav"]', 'iframe[src*="mgid"]',
                'iframe[src*="taboola"]', 'iframe[src*="outbrain"]',
                'iframe[src*="ad-maven"]', 'iframe[src*="/ads/"]',
                'iframe[src*="/adserve/"]', 'iframe[src*="/adserver/"]',
                // ★ HilltopAds/EvaDav 随机投放域名（必须同时覆盖 script 和 iframe）
                'iframe[src*="curoax"]', 'iframe[src*="pufted"]',
                'iframe[src*="bony-teaching"]', 'iframe[src*="untimely-hello"]',
                '[class*="ad-container"]', '[class*="ad-wrapper"]', '[class*="ad-unit"]',
                '[id*="ad-container"]', '[id*="ad-wrapper"]',
                'iframe[width="728"][height="90"]', 'iframe[width="300"][height="250"]',
                'iframe[width="160"][height="600"]', '[data-zone]', '[data-adzone]'
            ];
            const vw = window.innerWidth || 1920;
            const vh = window.innerHeight || 1080;
            const seen = new Set();
            const results = [];
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                    if (seen.has(el)) return;
                    seen.add(el);
                    const r = el.getBoundingClientRect();
                    const w = Math.round(r.width || 0);
                    const h = Math.round(r.height || 0);
                    if (w < 50 || h < 50) return;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    // 必须在视口内
                    if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) return;
                    results.push({
                        x: Math.round(r.left + w * (0.3 + Math.random() * 0.4)),
                        y: Math.round(r.top + h * (0.3 + Math.random() * 0.4)),
                        w: w, h: h
                    });
                });
            }
            // 跨域iframe兆底检测
            if (results.length === 0) {
                const _nonAd = ['youtube','vimeo','dailymotion','twitter','facebook','instagram','recaptcha','hcaptcha','disqus','paypal'];
                document.querySelectorAll('iframe').forEach(el => {
                    if (seen.has(el)) return;
                    const src = (el.getAttribute('src') || '').toLowerCase();
                    if (!src || src === 'about:blank') return;
                    if (_nonAd.some(d => src.includes(d))) return;
                    try { if (new URL(src, location.href).hostname === location.hostname) return; } catch(e){}
                    const r = el.getBoundingClientRect();
                    const w = Math.round(r.width || 0);
                    const h = Math.round(r.height || 0);
                    if (w < 160 || h < 90) return;
                    if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) return;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    seen.add(el);
                    results.push({
                        x: Math.round(r.left + w * (0.3 + Math.random() * 0.4)),
                        y: Math.round(r.top + h * (0.3 + Math.random() * 0.4)),
                        w: w, h: h
                    });
                });
            }
            return results;
        }
        """)

        if not ad_positions or len(ad_positions) == 0:
            log.debug(f"[广告点击][{stage}] 概率命中但无可见广告元素")
            return False, current_x, current_y

        # 4. ★ 风控去重：过滤已点击过的广告位置（同一任务内不重复点击同一广告位）
        _unclicked = [a for a in ad_positions if f"{a['x']//50}:{a['y']//50}" not in _ad_clicked_positions]
        if not _unclicked:
            log.debug(f"[广告点击][{stage}] 所有可见广告均已点击过，跳过")
            return False, current_x, current_y

        # 5. 随机选择一个广告
        target_ad = random.choice(_unclicked)
        ad_x = target_ad["x"]
        ad_y = target_ad["y"]
        # 记录已点击位置（50px粒度去重）
        _ad_clicked_positions.add(f"{ad_x//50}:{ad_y//50}")
        log.info(f"🎯 [广告点击][{stage}] 概率命中(prob={prob:.4f})，发现{len(ad_positions)}个可见广告，准备点击({ad_x},{ad_y})")

        # 5. 贝塞尔曲线移动鼠标到广告位置
        try:
            human_mouse_move(page, current_x, current_y, ad_x, ad_y, config)
            current_x, current_y = ad_x, ad_y
        except Exception:
            page.mouse.move(ad_x, ad_y)
            current_x, current_y = ad_x, ad_y

        # 6. ★ P1-5增强: 广告前阅读模拟（真人在点击广告前会先阅读周围内容）
        # 先在广告附近区域滚动/停留，模拟“看到广告”的过程
        _pre_read_time = random.uniform(2.0, 5.0)  # 阅读周围内容 2-5秒
        time.sleep(_pre_read_time)
        # 微观犹豫（真人看到广告后的停顿）
        time.sleep(random.uniform(0.5, 1.5))

        # 7. 记录点击前标签页数
        _context = page.context
        _pages_before = len(_context.pages)

        # P0-3 广告素材语义相似度检查
        if _HAS_RCE:
            try:
                _page_title = ''
                try:
                    _page_title = page.title() or ''
                except Exception:
                    pass
                _h1_text = ''
                try:
                    _h1_text = page.evaluate("() => { const h1 = document.querySelector('h1'); return h1 ? h1.textContent.trim().substring(0,200) : ''; }") or ''
                except Exception:
                    pass
                _landing_text = (_page_title + ' ' + _h1_text).strip()
                if _landing_text:
                    _ok_s, _s, _why = _rce.semantic_sim.allow(
                        creative_text=(f"ad_{stage}" if stage else "ad"),
                        landing_text=_landing_text,
                    )
                    if not _ok_s:
                        log.warning(f"⛔ P0-3 语义不匹配(score={_s:.3f}<0.62)，跳过此广告: {_why[:100]}")
                        return False, current_x, current_y
            except Exception as _rce_e:
                log.debug(f"P0-3 语义检查异常(忽略): {_rce_e}")

        # 8. 点击广告
        try:
            page.mouse.click(ad_x, ad_y)
        except Exception:
            pass

        _today_clicks = record_ad_click(1)
        _ad_click_last_ts = time.time()  # ★ 更新冷却时间戳
        log.info(f"🖱️ [广告点击] 已执行点击（今日累计 {_today_clicks} 次，冷却{_ad_click_cooldown}s）")

        # 9. 点击后等待
        ad_click_wait_cfg = config.get("ad_click_wait", {"min": 2, "max": 20})
        _click_wait = random.uniform(float(ad_click_wait_cfg.get("min", 2)), float(ad_click_wait_cfg.get("max", 20)))
        time.sleep(_click_wait)

        # 10. 检测新标签页（广告落地页）
        try:
            _pages_after = _context.pages
            if len(_pages_after) > _pages_before:
                _landing_page = _pages_after[-1]
                _lp_url = ""
                try:
                    _lp_url = _landing_page.url or ""
                except Exception:
                    pass
                log.info(f"🛬 [广告点击] 落地页已打开: {_lp_url[:100]}")
                # 落地页停留
                _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                _lp_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
                time.sleep(_lp_load)
                import math as _math_lp
                _lp_stay = max(15, min(90, _math_lp.exp(random.gauss(_math_lp.log(25), 0.5))))
                # 落地页滚动
                for _i in range(random.randint(1, 3)):
                    try:
                        _landing_page.evaluate(f"window.scrollBy(0, {random.randint(int(config.get('scroll_pixels', {}).get('min', 200)), int(config.get('scroll_pixels', {}).get('max', 1000)))}")
                        time.sleep(random.uniform(1.5, 4.0))
                    except Exception:
                        break
                _elapsed = _lp_load + 7.5
                _remaining = max(0, _lp_stay - _elapsed)
                if _remaining > 0:
                    time.sleep(_remaining)
                log.info(f"🛬 [广告点击] 落地页浏览完成（停留≈{_lp_stay:.1f}s），关闭")
                try:
                    _landing_page.close()
                except Exception:
                    pass
        except Exception as _lp_err:
            log.debug(f"[广告点击] 落地页处理异常: {str(_lp_err)[:80]}")

        return True, current_x, current_y

    except Exception as e:
        log.debug(f"[广告点击][{stage}] 异常(忽略): {type(e).__name__}: {str(e)[:80]}")
        return False, current_x, current_y


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
            log.warning(f"[真搜索] 找不到引擎配置，搜索跳转失败")
            return False, current_x, current_y
        
        # ★ 社媒平台不走真搜索流程（无搜索框/结果页），直接返回让Referer流程处理
        _engine_type = selected_engine.get("type", "search")
        if _engine_type == "social":
            log.info(f"🔍 [真搜索] 引擎 {selected_engine_id} 为社媒平台(type=social)，跳过真搜索，由Referer流程处理")
            return False, current_x, current_y
        
        engine_url = selected_engine.get("url")
        homepage_url = seo_query.get_engine_homepage(engine_url)
        if not homepage_url:
            log.warning(f"[真搜索] 提取主页失败，搜索跳转失败")
            return False, current_x, current_y

        # 获取该引擎的选择器（无则用通用兜底）
        selectors = ENGINE_SELECTORS.get(selected_engine_id, {
            "search_box": 'input[type="text"], input[name="q"], input[name="wd"], input[name="query"]',
            "result_links": ['h3 a[href*="http"]', '.result a', 'a[href*="' + urlparse(target_url).netloc + '"]'],
            "privacy_buttons": [],
        })

        log.info(f"🔍 [真搜索] 访问搜索引擎主页: {homepage_url}")
        
        # 2. 访问搜索引擎主页
        simulate_rtt_jitter(base_ms=80, jitter_ms=40)  # ★ RTT仿真：模拟真实网络延迟
        try:
            _hard_timeout_goto(page, homepage_url, timeout=60, wait_until="domcontentloaded")
        except Exception:
            pass
        _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)

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
            log.warning(f"[真搜索] 定位搜索框失败: {str(e)[:100]}，搜索跳转失败")
            return False, current_x, current_y

        # 5. 移动鼠标到搜索框、点击
        if not search_box:
            log.warning("[真搜索] 搜索框元素为None，搜索跳转失败")
            return False, current_x, current_y
        try:
            search_box.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass  # 滚动失败不阻断流程
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

        # 6. 清空搜索框（如果有默认值）—— 根据平台选择正确修饰键（Mac用Meta，Windows/Linux用Control）
        _select_all_mod = "Meta" if ("Mac OS X" in user_agent or "Macintosh" in user_agent) else "Control"
        page.keyboard.down(_select_all_mod)
        page.keyboard.press("a")
        page.keyboard.up(_select_all_mod)
        time.sleep(random.uniform(0.15, 0.4))
        page.keyboard.press("Backspace")
        time.sleep(random.uniform(0.2, 0.5))

        # 7. 模拟真人分段输入关键词（删改1-2次模拟思考）
        # ★ 2.3 键盘bigram延迟表：常见字母组合打字更快（如th/he/in），罕见组合更慢（如qz/xj）
        _BIGRAM_FAST = {"th", "he", "in", "er", "an", "re", "on", "at", "en", "nd", "ti", "es", "or", "te", "of", "ed", "is", "it", "al", "ar", "st", "to", "nt", "ng", "se", "ha", "as", "ou", "io", "le", "ve", "co", "me", "de", "hi", "ri", "ro", "ic", "ne", "ea", "ra", "ce", "li", "ch", "ll", "be", "ma", "si", "om", "ur"}
        _BIGRAM_SLOW = {"qz", "xj", "zk", "jx", "qy", "zw", "vx", "jk", "xz", "wq", "qx", "jv", "kx", "zq"}
        def _bigram_factor(prev_c, curr_c):
            bg = (prev_c + curr_c).lower()
            if bg in _BIGRAM_FAST:
                return random.uniform(0.55, 0.75)  # 常见组合：更快
            if bg in _BIGRAM_SLOW:
                return random.uniform(1.5, 1.9)  # 罕见组合：更慢
            return random.uniform(0.85, 1.15)  # 普通组合
        log.info(f"🔍 [真搜索] 模拟真人输入关键词(bigram延迟)")
        words = selected_keyword.split(" ")
        i = 0
        _prev_char = ""
        while i < len(words):
            chunk = " ".join(words[i:i+2])
            # 使用原生按字符 + bigram延迟
            for c in chunk:
                page.keyboard.type(c)
                _base_delay = random.uniform(0.06, 0.18)
                _delay = _base_delay * _bigram_factor(_prev_char, c) if _prev_char else _base_delay
                time.sleep(_delay)
                _prev_char = c
            # ★ 5%概率词间"思考停顿"（0.5-2s）
            if random.random() < 0.05:
                time.sleep(random.uniform(0.5, 2.0))
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
        # 等待搜索结果页（networkidle → _safe_page_wait）
        _safe_page_wait(page, min_wait=1.5, max_wait=3.5, ad_wait=False)

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
            # 滚动到可见（加超时保护，避免元素已脱离DOM导致卡死）
            try:
                target_link_found.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
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
            
            # 11. 等待跳转到目标页（networkidle → _safe_page_wait，目标站启用ad_wait）
            _safe_page_wait(page, min_wait=2.0, max_wait=4.5, ad_wait=True)
            current_url = page.url
            log.info(f"🔍 [真搜索] 当前URL: {current_url[:100]}")
            if target_host in current_url:
                log.info(f"✅ [真搜索] 成功跳转到目标页！")
                return True, current_x, current_y
            else:
                log.warning(f"[真搜索] 跳转后URL不匹配，搜索跳转失败")
                return False, current_x, current_y
        else:
            log.warning(f"[真搜索] 搜索结果页没找到目标链接，搜索跳转失败")
            return False, current_x, current_y

    except Exception as e:
        log.warning(f"[真搜索] 流程异常: {str(e)[:180]}，搜索跳转失败")
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
            display: none;
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
        .log-task-separator { color: #ff0000; font-style: italic; font-weight: 900; font-size: 27px; display: block; text-align: center; }
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
            <div class="system-name">Selenium流量系统 <span style="font-size:12px;color:#aaa;font-weight:normal;">v{{ APP_VERSION }}</span>{% if VPS_HOST %} <span style="font-size:12px;color:#7fd4ff;font-weight:normal;margin-left:6px;">VPS: {{ VPS_HOST }}</span>{% endif %}</div>
            
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
                    {% for idx in range(config.proxy_pool|length) %}
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
                        <div class="form-group">
                            <label>每日广告点击上限（0=不限，建议5~10，每天随机取值）</label>
                            <div class="input-group">
                                <input type="number" id="daily_ad_click_limit_min" value="{{ config.get('daily_ad_click_limit', {}).get('min', 0) if config.get('daily_ad_click_limit') is mapping else 0 }}">
                                <input type="number" id="daily_ad_click_limit_max" value="{{ config.get('daily_ad_click_limit', {}).get('max', 0) if config.get('daily_ad_click_limit') is mapping else 0 }}">
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
                    <!-- 🛡️ Dwell Monitor Guardian：实时守护面板（增强版：状态灯 + 告警 + 健康度指标 + 恢复按钮） -->
                    <div id="dm-panel" style="margin-bottom: 12px; padding: 12px; background: linear-gradient(90deg,#1f2937,#111827); border: 1px solid #374151; border-radius: 8px;">
                        <!-- 第1行：标题 + 控制按钮 + 状态灯 -->
                        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px;">
                            <span style="color:#e5e7eb; font-weight: 600; font-size: 13px;">🛡️ 停留/跳出率 实时守护</span>
                            <span id="dm-status-light" style="display: inline-block; width: 12px; height: 12px; border-radius: 50%; background: #6b7280; box-shadow: 0 0 6px #6b7280;" title="未启动"></span>
                            <span id="dm-status-text" style="color:#9ca3af; font-size: 12px;">未启动</span>
                            <button class="btn" id="btn-dm-start" onclick="toggleDwellMonitor(true)" style="background:#059669; color:white; padding:5px 12px; font-size:12px;">▶️ 启动守护</button>
                            <button class="btn" id="btn-dm-stop" onclick="toggleDwellMonitor(false)" style="background:#7f1d1d; color:white; padding:5px 12px; font-size:12px;">⏹ 停止守护</button>
                            <button class="btn" id="btn-dm-resume" onclick="resumeTaskFromMonitor()" style="background:#2563eb; color:white; padding:5px 12px; font-size:12px; display:none;">🔄 恢复任务</button>
                            <label style="color:#9ca3af; font-size: 11px; display:inline-flex; align-items:center; gap:4px; margin-left: auto;">
                                <input type="checkbox" id="dm-no-pause" style="width:auto;">仅报警不暂停
                            </label>
                        </div>
                        <!-- 第2行：健康度指标 -->
                        <div id="dm-metrics" style="display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #d1d5db; margin-bottom: 6px;">
                            <span>📊 平均停留: <b id="dm-avg-dwell" style="color:#60a5fa;">--</b>s</span>
                            <span>📈 跳出率: <b id="dm-bounce-rate" style="color:#fbbf24;">--</b>%</span>
                            <span>🔴 CRIT: <b id="dm-crit-count" style="color:#f87171;">0</b></span>
                            <span>🟡 WARN: <b id="dm-warn-count" style="color:#fbbf24;">0</b></span>
                            <span>🟢 OK: <b id="dm-ok-count" style="color:#34d399;">0</b></span>
                            <span id="dm-consec-crit" style="display:none;">⚡ 连续CRIT: <b style="color:#ef4444;">0</b></span>
                        </div>
                        <!-- 第3行：最近3条告警 -->
                        <div id="dm-alerts-box" style="font-size: 11px; color: #9ca3af; max-height: 72px; overflow-y: auto; background: #0f172a; border-radius: 4px; padding: 4px 8px;">
                            <span style="color:#6b7280;">等待监控启动...</span>
                        </div>
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

                    <!-- ★ HilltopAds Pop-under 弹窗配置 -->
                    <div style="margin-bottom: 20px; padding: 12px; background: #1a1a2e; border-radius: 8px; border: 2px solid #e94560;">
                        <h4 style="margin-top: 0; color: #e94560;">🪟 HilltopAds Pop-under 弹窗触发</h4>
                        <p style="color:#94a3b8;font-size:12px;margin:0 0 12px 0;">通过CDP层真实用户手势触发页面HilltopAds脚本创建后台弹窗，管理弹窗生命周期满足结算条件。与Google AdSense流程共存，互不干扰。</p>
                        <div style="display: flex; flex-wrap: wrap; gap: 12px; align-items: center;">
                            <label style="display: flex; align-items: center; gap: 6px; color: #e2e8f0; font-size: 13px;">
                                <input type="checkbox" id="hilltopads_enabled" {{ 'checked' if config.hilltopads.get('enabled', False) else '' }}>
                                启用 Pop-under 弹窗触发
                            </label>
                            <div class="form-group" style="flex: 0 0 auto; min-width: 0;">
                                <label style="font-size:11px; color:#94a3b8;">触发概率</label>
                                <input type="text" id="hilltopads_trigger_prob" value="{{ (config.hilltopads.get('trigger_probability', 0.40) * 100)|int }}%" style="width: 60px; font-size:12px;">
                            </div>
                            <div class="form-group" style="flex: 0 0 auto; min-width: 0;">
                                <label style="font-size:11px; color:#94a3b8;">最小存活(s)</label>
                                <input type="number" id="hilltopads_stay_min" value="{{ config.hilltopads.get('popunder_stay_min', 15) }}" style="width: 55px; font-size:12px;">
                            </div>
                            <div class="form-group" style="flex: 0 0 auto; min-width: 0;">
                                <label style="font-size:11px; color:#94a3b8;">最大存活(s)</label>
                                <input type="number" id="hilltopads_stay_max" value="{{ config.hilltopads.get('popunder_stay_max', 25) }}" style="width: 55px; font-size:12px;">
                            </div>
                        </div>
                        <p style="color:#94a3b8;font-size:11px;margin:8px 0 0 0;">
                            弹窗触发仅对配置了 HilltopAds 广告代码的站点生效。坐标自动避让 AdSense 广告容器。冷却 90s，同任务仅触发1次。
                        </p>
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
                        <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#cbd5e1;margin-bottom:6px;">
                            <span id="prodTestStage">准备中...</span>
                            <div style="display:flex;align-items:center;gap:10px;">
                                <span id="prodTestPercent">0%</span>
                                <button id="btnForceStopProd" class="btn" style="background:#dc2626;color:#fff;font-size:11px;padding:3px 10px;display:none;" onclick="forceStopProductionTest()">🛑 强制停止</button>
                            </div>
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
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">开始时间(北京)</th>
                                    <th style="padding:6px; text-align:left; border-bottom:1px solid #444;">当地时间</th>
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
                            <button id="btn-toggle-plan-preview" class="btn" onclick="togglePlanPreview()" style="background: #555; color: #fff; padding: 4px 10px; font-size: 12px; border-radius: 4px; cursor: pointer; border: 1px solid #00d4aa;">📋 计划预览</button>
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
                    daily_ad_click_limit: {min: parseInt((document.getElementById('daily_ad_click_limit_min') || {value:'0'}).value) || 0, max: parseInt((document.getElementById('daily_ad_click_limit_max') || {value:'0'}).value) || 0},
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

                // ★ 模型Tab完整字段（修复：之前缺失导致保存无效）
                ad_stay_time: {
                    min: parseFloat(document.getElementById('ad_stay_time_min').value) || 3,
                    max: parseFloat(document.getElementById('ad_stay_time_max').value) || 40
                },
                page_load_wait: {
                    min: parseFloat(document.getElementById('page_load_wait_min').value) || 1,
                    max: parseFloat(document.getElementById('page_load_wait_max').value) || 8
                },
                scroll_pixels: {
                    min: parseInt(document.getElementById('scroll_pixels_min').value) || 200,
                    max: parseInt(document.getElementById('scroll_pixels_max').value) || 1000
                },
                scroll_wait: {
                    min: parseFloat(document.getElementById('scroll_wait_min').value) || 0.5,
                    max: parseFloat(document.getElementById('scroll_wait_max').value) || 5
                },
                ad_click_prob: {
                    min: parseFloat(document.getElementById('ad_click_prob_min').value) || 0.005,
                    max: parseFloat(document.getElementById('ad_click_prob_max').value) || 0.05
                },
                ad_click_wait: {
                    min: parseFloat(document.getElementById('ad_click_wait_min').value) || 2,
                    max: parseFloat(document.getElementById('ad_click_wait_max').value) || 20
                },
                daily_ad_click_limit: {
                    min: parseInt((document.getElementById('daily_ad_click_limit_min') || {value:'0'}).value) || 0,
                    max: parseInt((document.getElementById('daily_ad_click_limit_max') || {value:'0'}).value) || 0
                },
                random_click_count: {
                    min: parseInt(document.getElementById('random_click_count_min').value) || 3,
                    max: parseInt(document.getElementById('random_click_count_max').value) || 10
                },
                random_click_wait: {
                    min: parseFloat(document.getElementById('random_click_wait_min').value) || 0.5,
                    max: parseFloat(document.getElementById('random_click_wait_max').value) || 3
                },
                mouse_move_count: {
                    min: parseInt(document.getElementById('mouse_move_count_min').value) || 2,
                    max: parseInt(document.getElementById('mouse_move_count_max').value) || 20
                },
                mouse_move_steps: {
                    min: parseInt(document.getElementById('mouse_move_steps_min').value) || 50,
                    max: parseInt(document.getElementById('mouse_move_steps_max').value) || 250
                },
                mouse_move_wait: {
                    min: parseFloat(document.getElementById('mouse_move_wait_min').value) || 0.1,
                    max: parseFloat(document.getElementById('mouse_move_wait_max').value) || 1
                },
                scroll_count: {
                    min: parseInt(document.getElementById('scroll_count_min').value) || 2,
                    max: parseInt(document.getElementById('scroll_count_max').value) || 10
                },
                bezier_pause_prob: {
                    min: parseFloat(document.getElementById('bezier_pause_prob_min').value) || 0.05,
                    max: parseFloat(document.getElementById('bezier_pause_prob_max').value) || 0.2
                },
                mouse_move_pause: {
                    min: parseFloat(document.getElementById('mouse_move_pause_min').value) || 0.01,
                    max: parseFloat(document.getElementById('mouse_move_pause_max').value) || 0.1
                },
                task_interval: {
                    min: parseFloat(document.getElementById('task_interval_min').value) || 10,
                    max: parseFloat(document.getElementById('task_interval_max').value) || 60
                },

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
                    groupRow.innerHTML = `<td colspan="7" style="padding:5px 6px; background:#111827; color:#fbbf24; font-weight:bold; border-bottom:1px solid #374151;">📅 ${taskDate}</td>`;
                    tbody.appendChild(groupRow);
                }
                const startStr = t.plan_time || secToHHMMSS(t.actual_start || 0);
                const endStr = secToHHMMSS(t.actual_end || 0);
                const duration = (t.task_duration || 0).toFixed(1);
                const status = t.status || '未完成';
                let statusColor = '#aaa';
                if (status === '已完成') statusColor = '#00d4aa';
                if (status === '失败') statusColor = '#ff5555';
                
                // 计算目标国当地时间
                const tzMap = {'US':'America/New_York','GB':'Europe/London','DE':'Europe/Berlin','FR':'Europe/Paris','JP':'Asia/Tokyo','SG':'Asia/Singapore','HK':'Asia/Hong_Kong','ID':'Asia/Jakarta','AU':'Australia/Sydney','NZ':'Pacific/Auckland','CA':'America/New_York'};
                let localTimeStr = '-';
                if (t.actual_start_epoch && t.proxy_country) {
                    try {
                        const tz = tzMap[t.proxy_country] || 'America/New_York';
                        localTimeStr = new Intl.DateTimeFormat('en-GB', {timeZone: tz, hour:'2-digit', minute:'2-digit', hour12:false}).format(new Date(t.actual_start_epoch * 1000));
                    } catch(e) { localTimeStr = '-'; }
                }
                
                const row = document.createElement('tr');
                row.innerHTML = 
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${t.idx}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222;">${startStr}</td>` +
                    `<td style="padding:4px 6px; border-bottom:1px solid #222; color:#00d4aa;">${localTimeStr}</td>` +
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
            // ★ 即时视觉反馈：确认按钮点击生效
            const _btn = document.getElementById('btn-generate-plan');
            if (_btn) { _btn.textContent = '⏳ 生成中...'; _btn.disabled = true; }
            console.log('✅ 生成计划按钮被点击');
            let payload;
            try {
                payload = collectConfigPayload();
            } catch(e) {
                console.error('❌ collectConfigPayload 异常:', e);
                alert('❌ 配置收集失败: ' + e.message);
                if (_btn) { _btn.textContent = '📋 生成计划'; _btn.disabled = false; }
                return;
            }
            console.log('✅ 收集到的配置:', payload);
            // 先保存配置，再生成计划
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => {
                if (!r.ok) throw new Error('save_config HTTP ' + r.status);
                return r.json();
            }).then(() => {
                console.log('✅ 配置保存成功');
                return fetch('/generate_plan', {method: 'POST'});
            }).then(r => {
                if (!r.ok) throw new Error('generate_plan HTTP ' + r.status);
                console.log('✅ 获取计划响应:', r.status);
                return r.json();
            }).then(result => {
                console.log('✅ 解析的结果:', result);
                if (result.status === 'ok') {
                    console.log('✅ 开始渲染计划');
                    renderPlan(result.plan);
                    // 确保显示计划预览界面
                    document.getElementById('planPreviewPanel').style.display = 'block';
                    // 同步切换按钮状态为"关闭预览"
                    const _toggleBtn = document.getElementById('btn-toggle-plan-preview');
                    if (_toggleBtn) { _toggleBtn.style.background = '#00d4aa'; _toggleBtn.style.color = '#1a1a1a'; _toggleBtn.textContent = '📋 关闭预览'; }
                    // 立即刷新日志窗口，确保“待执行计划预览”置顶展示（alert 会阻塞轮询，必须先刷新再弹窗）
                    refreshLogBox().then(() => {
                        alert('✅ 计划已生成，请在右侧查看，确认无误后点击“执行计划”');
                    });
                } else {
                    alert('❌ 计划生成失败: ' + (result.message || '未知错误'));
                }
            }).catch(err => {
                console.error('❌ 请求错误:', err);
                alert('❌ 请求失败: ' + err.message);
            }).finally(() => {
                if (_btn) { _btn.textContent = '📋 生成计划'; _btn.disabled = false; }
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
            // 执行计划后隐藏计划预览面板
            document.getElementById('planPreviewPanel').style.display = 'none';
            fetch('/start_task', {method: 'POST'}).then(() => location.reload());
        }

        // 切换计划预览面板显示/隐藏
        function togglePlanPreview() {
            const panel = document.getElementById('planPreviewPanel');
            const btn = document.getElementById('btn-toggle-plan-preview');
            if (panel.style.display === 'none' || panel.style.display === '') {
                panel.style.display = 'block';
                if (btn) { btn.style.background = '#00d4aa'; btn.style.color = '#1a1a1a'; btn.textContent = '📋 关闭预览'; }
                // 打开时检查表格是否为空，若空则重新加载计划数据
                const tbody = document.getElementById('planTableBody');
                if (tbody && tbody.children.length === 0) {
                    fetch('/get_plan').then(r => r.json()).then(data => {
                        if (data.plan) { renderPlan(data.plan); }
                    });
                }
            } else {
                panel.style.display = 'none';
                if (btn) { btn.style.background = '#555'; btn.style.color = '#fff'; btn.textContent = '📋 计划预览'; }
            }
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
                // 重置切换按钮状态
                const _tb = document.getElementById('btn-toggle-plan-preview');
                if (_tb) { _tb.style.background = '#555'; _tb.style.color = '#fff'; _tb.textContent = '📋 计划预览'; }
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
            // 1. 加载计划预览数据（填充表格，但不自动显示面板）
            fetch('/get_plan').then(r => r.json()).then(data => {
                if (data.plan) {
                    renderPlan(data.plan);
                    // 页面加载时不自动展示计划预览（仅生成计划时才自动展示）
                    // 用户可通过“📋 计划预览”按钮手动查看
                    document.getElementById('planPreviewPanel').style.display = 'none';
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

        // ========== 🛡️ Dwell Monitor Guardian：增强版（状态灯 + 健康指标 + 告警展示 + 恢复任务） ==========
        let _dm_poll_timer = null;

        function _dmRenderStatus(payload) {
            const running = !!payload.running;
            const s = payload.snapshot || {};
            const alerts = payload.alerts || [];

            // ---- 状态灯 ----
            const light = document.getElementById('dm-status-light');
            const statusText = document.getElementById('dm-status-text');
            const resumeBtn = document.getElementById('btn-dm-resume');
            if (light && statusText) {
                if (!running) {
                    light.style.background = '#6b7280';
                    light.style.boxShadow = '0 0 6px #6b7280';
                    light.title = '未启动';
                    statusText.textContent = '未启动';
                    statusText.style.color = '#9ca3af';
                    if (resumeBtn) resumeBtn.style.display = 'none';
                } else if ((s.crit || 0) > 0) {
                    light.style.background = '#ef4444';
                    light.style.boxShadow = '0 0 10px #ef4444';
                    light.title = '异常：存在 CRITICAL 告警';
                    statusText.textContent = '异常 (CRIT=' + (s.crit||0) + ')';
                    statusText.style.color = '#ef4444';
                    if (resumeBtn) resumeBtn.style.display = 'inline-block';
                } else if ((s.warn || 0) > 0) {
                    light.style.background = '#fbbf24';
                    light.style.boxShadow = '0 0 10px #fbbf24';
                    light.title = '警告：存在 WARNING 告警';
                    statusText.textContent = '警告 (WARN=' + (s.warn||0) + ')';
                    statusText.style.color = '#fbbf24';
                    if (resumeBtn) resumeBtn.style.display = 'inline-block';
                } else {
                    light.style.background = '#10b981';
                    light.style.boxShadow = '0 0 10px #10b981';
                    light.title = '正常运行';
                    statusText.textContent = '正常 (pid=' + (payload.pid || '?') + ')';
                    statusText.style.color = '#10b981';
                    if (resumeBtn) resumeBtn.style.display = 'none';
                }
            }

            // ---- 健康度指标 ----
            const avgDwell = document.getElementById('dm-avg-dwell');
            const bounceRate = document.getElementById('dm-bounce-rate');
            const critCount = document.getElementById('dm-crit-count');
            const warnCount = document.getElementById('dm-warn-count');
            const okCount = document.getElementById('dm-ok-count');
            const consecCrit = document.getElementById('dm-consec-crit');
            if (avgDwell) avgDwell.textContent = (s.avg_dwell_last120 || 0).toFixed(0);
            if (bounceRate) bounceRate.textContent = (s.win_bounce_pct || 0).toFixed(1);
            if (critCount) { critCount.textContent = s.crit || 0; critCount.style.color = (s.crit||0) > 0 ? '#f87171' : '#d1d5db'; }
            if (warnCount) { warnCount.textContent = s.warn || 0; warnCount.style.color = (s.warn||0) > 0 ? '#fbbf24' : '#d1d5db'; }
            if (okCount) okCount.textContent = s.ok || 0;
            if (consecCrit) {
                const cc = payload.consecutive_crit || 0;
                if (cc > 0) {
                    consecCrit.style.display = 'inline';
                    consecCrit.querySelector('b').textContent = cc;
                } else {
                    consecCrit.style.display = 'none';
                }
            }

            // ---- 最近3条告警 ----
            const alertsBox = document.getElementById('dm-alerts-box');
            if (alertsBox) {
                if (!running) {
                    alertsBox.innerHTML = '<span style="color:#6b7280;">等待监控启动...</span>';
                } else if (alerts.length === 0) {
                    alertsBox.innerHTML = '<span style="color:#34d399;">✅ 无告警记录</span>';
                } else {
                    const recent = alerts.slice(-3).reverse();
                    alertsBox.innerHTML = recent.map(function(a) {
                        const sevColor = a.severity === 'CRITICAL' ? '#ef4444' : (a.severity === 'DEGRADE' ? '#c084fc' : (a.severity === 'WARNING' ? '#fbbf24' : '#9ca3af'));
                        const sevBadge = '[' + (a.severity || '?').substring(0,4) + ']';
                        return '<div style="margin-bottom:2px;"><span style="color:' + sevColor + '; font-weight:600;">' + sevBadge + '</span> <span style="color:#9ca3af;">' + (a.ts||'').substring(11,19) + '</span> ' + (a.title||'').substring(0,60) + '</div>';
                    }).join('');
                }
            }
        }

        async function refreshDwellMonitorStatus() {
            try {
                const r = await fetch('/dwell_monitor/status');
                const j = await r.json();
                _dmRenderStatus(j);
            } catch (_) {
                const statusText = document.getElementById('dm-status-text');
                if (statusText) { statusText.textContent = '获取状态失败'; statusText.style.color = '#f87171'; }
            }
        }

        async function toggleDwellMonitor(startFlag) {
            const noPause = document.getElementById('dm-no-pause') ? document.getElementById('dm-no-pause').checked : false;
            try {
                const url = startFlag ? '/dwell_monitor/start' : '/dwell_monitor/stop';
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({no_auto_pause: noPause})
                });
                const data = await resp.json();
                if (!data.success) {
                    alert('操作失败: ' + (data.message || '未知错误'));
                    return;
                }
                if (startFlag) {
                    if (_dm_poll_timer) clearInterval(_dm_poll_timer);
                    _dm_poll_timer = setInterval(refreshDwellMonitorStatus, 3000);
                    setTimeout(refreshDwellMonitorStatus, 300);
                } else {
                    if (_dm_poll_timer) { clearInterval(_dm_poll_timer); _dm_poll_timer = null; }
                    setTimeout(refreshDwellMonitorStatus, 300);
                }
                alert(startFlag ? '已启动停留/跳出率监控守护' : '已停止停留/跳出率监控守护');
            } catch (e) {
                alert('操作异常: ' + e);
            }
        }

        async function resumeTaskFromMonitor() {
            // 先尝试 /start_task 恢复任务（无专用 /resume_task 路由时使用 start_task）
            try {
                const resp = await fetch('/start_task', { method: 'POST' });
                const data = await resp.json();
                if (data.status === 'ok') {
                    alert('任务已恢复运行');
                    location.reload();
                } else if (data.status === 'error' && data.message && data.message.indexOf('已有任务') >= 0) {
                    alert('任务已在运行中，无需恢复');
                } else {
                    alert('恢复任务: ' + JSON.stringify(data));
                }
            } catch (e) {
                alert('恢复任务失败: ' + e);
            }
        }

        // 页面加载时检查并尝试拉状态
        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(refreshDwellMonitorStatus, 600);
        });

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

        // 刷新日志窗口（供定时轮询和手动刷新复用）
        let _lastLogHTML = '';  // 缓存上次日志内容，避免无变化时重绘闪烁
        function refreshLogBox() {
            const logModeEl = document.querySelector('input[name="log_mode"]:checked');
            const logMode = logModeEl ? logModeEl.value : 'test';
            return fetch('/get_logs?mode=' + encodeURIComponent(logMode) + '&limit=500').then(r => {
                if (!r.ok) return null;  // HTTP错误时不更新
                return r.text();
            }).then(html => {
                if (html === null) return;  // 请求失败，保留当前内容
                const logBox = document.getElementById('logBox');
                const autoScrollCheckbox = document.getElementById('autoScroll');
                const filterSelect = document.getElementById('logFilter');
                const isAutoScroll = autoScrollCheckbox ? autoScrollCheckbox.checked : true;
                
                if (logBox) {
                    // ★ 防闪烁：内容未变化时跳过DOM更新
                    if (html === _lastLogHTML) {
                        return;  // 日志无变化，不重绘
                    }
                    // ★ 防闪烁：返回空内容时不清空已有日志
                    if (!html && logBox.innerHTML) {
                        return;
                    }
                    _lastLogHTML = html;
                    
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
            }).catch(() => {});  // 网络异常静默忽略，保留当前日志
        }

        // 更新日志
        setInterval(() => {
            // 更新日志
            refreshLogBox();
            
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
                        // 任务运行中：仅显示任务计划状态面板，计划预览默认隐藏（可通过切换按钮查看）
                        planStatusPanel.style.display = 'block';
                        // planPreviewPanel 不再自动显示，用户可通过"📋 计划预览"按钮手动切换
                        
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
                                <div><span style="color:#888;">计划:</span> <span style="color:#00aaff; font-weight:bold;">${task.idx}/${websiteStatus.total_tasks}</span> <span style="color:#888;">时间:</span> <span>${task.plan_time || '-'} (北京时间)</span></div>
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
                if (statusItems.length >= 4) {
                    statusItems[1].querySelector('.stat-number').textContent = status.total;
                    statusItems[2].querySelector('.stat-number').textContent = status.success;
                    statusItems[3].querySelector('.stat-number').textContent = status.fail;
                }
            });
        }, 2000);  // ★ 2秒轮询（原1秒太频繁导致闪烁）
        
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
            document.getElementById('btnForceStopProd').style.display = 'inline-block';
            setProdTestProgress(0, '启动中...');
            _prodTestLastProgress = -1;
            _prodTestStuckCount = 0;
            fetch('/start_production_test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({layers: layers, headless: true})
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    alert('启动失败: ' + (d.message || '未知错误'));
                    enableProdTestButtons();
                    document.getElementById('prodTestProgress').style.display = 'none';
                    document.getElementById('btnForceStopProd').style.display = 'none';
                    return;
                }
                _prodTestPolling = setInterval(pollProductionTest, 1000);
            }).catch(e => {
                alert('请求异常: ' + e);
                enableProdTestButtons();
                document.getElementById('prodTestProgress').style.display = 'none';
                document.getElementById('btnForceStopProd').style.display = 'none';
            });
        }
        function forceStopProductionTest() {
            if (!confirm('确定要强制停止当前测试吗？')) return;
            fetch('/force_stop_production_test', {method:'POST'}).then(r => r.json()).then(d => {
                if (_prodTestPolling) { clearInterval(_prodTestPolling); _prodTestPolling = null; }
                enableProdTestButtons();
                document.getElementById('btnForceStopProd').style.display = 'none';
                setProdTestProgress(0, '已强制停止');
            });
        }
        function enableProdTestButtons() {
            document.querySelectorAll('#tab-taskvalidation button[onclick^="startProductionTest"]').forEach(b => b.disabled = false);
        }
        var _prodTestLastProgress = -1;
        var _prodTestStuckCount = 0;
        function setProdTestProgress(pct, stage) {
            document.getElementById('prodTestBar').style.width = pct + '%';
            document.getElementById('prodTestPercent').textContent = pct + '%';
            document.getElementById('prodTestStage').textContent = stage || '';
        }
        function pollProductionTest() {
            fetch('/get_production_test_status').then(r => r.json()).then(d => {
                setProdTestProgress(d.progress || 0, d.stage || '');
                // ★ 卡死检测：如果进度60秒未变化，显示警告
                if (d.running) {
                    if (d.progress === _prodTestLastProgress) {
                        _prodTestStuckCount++;
                        if (_prodTestStuckCount > 60) {
                            document.getElementById('prodTestStage').textContent = (d.stage || '') + ' ⚠️ 可能卡死，点击强制停止';
                            document.getElementById('prodTestStage').style.color = '#f59e0b';
                        }
                    } else {
                        _prodTestStuckCount = 0;
                        document.getElementById('prodTestStage').style.color = '#cbd5e1';
                    }
                    _prodTestLastProgress = d.progress;
                }
                if (!d.running) {
                    clearInterval(_prodTestPolling);
                    _prodTestPolling = null;
                    enableProdTestButtons();
                    document.getElementById('btnForceStopProd').style.display = 'none';
                    document.getElementById('prodTestStage').style.color = '#cbd5e1';
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
                
                // ★ 修复：先判断是否完成，完成时直接设100%，避免进度条卡在中间值
                if (!data.is_running) {
                    clearInterval(_keywordPolling);
                    _keywordPolling = null;
                    document.getElementById('btnKeywordExplore').disabled = false;
                    
                    if (data.result) {
                        const _ar = data.result.ad_hit_rate;
                        const _arTxt = _ar !== undefined ? '，广告命中率 ' + _ar + '%' : '';
                        setKeywordProgress(100, '探索完成！共 ' + data.result.total_keywords + ' 个关键词，' + (data.result.total_fallback_links || 0) + ' 个兜底链接' + _arTxt);
                        renderKeywordResult(data.result);
                    } else if (data.error) {
                        setKeywordProgress(100, '探索失败');
                        document.getElementById('keywordStage').textContent = '探索失败: ' + data.error;
                        document.getElementById('keywordBar').style.background = '#ef4444';
                    } else {
                        setKeywordProgress(100, data.progress || '已完成');
                    }
                    return;
                }
                
                // 运行中：更新进度
                if (data.current_layer !== undefined && data.max_layer !== undefined && data.max_layer > 0) {
                    const pct = Math.min(95, Math.round((data.current_layer / data.max_layer) * 100));
                    setKeywordProgress(pct, data.progress || `正在探索第 ${data.current_layer} 层`);
                } else {
                    setKeywordProgress(0, data.progress || '准备中...');
                }
            }).catch(() => {});
        }
        
        function renderKeywordResult(result) {
            let html = '';
            
            // 1. 结果总览
            html += '<div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:12px;">';
            html += '<div style="font-size:16px;font-weight:bold;color:#f59e0b;margin-bottom:8px;">';
            html += ' 关键词探索完成</div>';
            html += '<div style="display:flex;gap:20px;font-size:14px;color:#cbd5e1;flex-wrap:wrap;">';
            html += '<span>关键词(锚文本): <strong style="color:#22c55e;">' + result.total_keywords + '</strong></span>';
            html += '<span>兜底链接: <strong style="color:#3b82f6;">' + (result.total_fallback_links || 0) + '</strong></span>';
            html += '<span>层级: <strong style="color:#a78bfa;">' + result.layers_crawled + '</strong></span>';
            html += '</div>';
            // ★ 广告统计展示
            if (result.ad_pages !== undefined) {
                const hitRate = result.ad_hit_rate || 0;
                const hitColor = hitRate >= 50 ? '#22c55e' : (hitRate >= 20 ? '#f59e0b' : '#ef4444');
                html += '<div style="display:flex;gap:20px;font-size:13px;color:#94a3b8;margin-top:8px;padding-top:8px;border-top:1px solid #334155;">';
                html += '<span>🎯 含广告页: <strong style="color:#22c55e;">' + result.ad_pages + '</strong></span>';
                html += '<span>❌ 无广告页: <strong style="color:#ef4444;">' + result.no_ad_pages + '</strong></span>';
                html += '<span>广告命中率: <strong style="color:' + hitColor + ';">' + hitRate + '%</strong></span>';
                html += '</div>';
                if (hitRate < 30) {
                    html += '<div style="font-size:12px;color:#f59e0b;margin-top:6px;">⚠️ 广告命中率较低，建议检查兜底链接是否指向含广告的页面（如章节阅读页、书籍详情页）</div>';
                }
            }
            html += '</div>';
            
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
            
            // ★ 必须注入的默认关键词（每层都要有）
            // "chapter" 通过 includes() 匹配可覆盖 chapter1~chapter3000 所有章节链接
            const MUST_HAVE_KWS = ['chapter', 'home'];
            const MUST_HAVE_FBS = ['https://freestoryweb.com/'];

            for (let i = 1; i <= 5; i++) {
                const layerKey = 'layer_' + i;
                const data = layerData[layerKey];  // 可能为undefined（爬取层数<5时）
                
                // ★ 修复：DOM id是 webnav_layer1_keywords（无下划线），不是 webnav_layer_1_keywords
                const kwTextarea = document.getElementById('webnav_layer' + i + '_keywords');
                const fbTextarea = document.getElementById('webnav_layer' + i + '_fallback_urls');
                
                if (kwTextarea) {
                    // 如果该层有爬取数据则用之，否则用合并池，最后至少用MUST_HAVE_KWS
                    let kws = (data && data.keywords && data.keywords.length > 0) ? [...data.keywords]
                            : (mergedKws.length > 0 ? [...mergedKws] : []);
                    // ★ 每层最多保存50个关键词（避免1935个章节标题塞满配置）
                    if (kws.length > 50) {
                        kws = kws.sort((a, b) => a.length - b.length).slice(0, 50);
                    }
                    // ★ 强制注入 chapter + home（确保每层都能匹配章节链接和首页）
                    for (const mk of MUST_HAVE_KWS) {
                        if (!kws.some(k => k.toLowerCase() === mk)) kws.unshift(mk);
                    }
                    kwTextarea.value = kws.join(',');
                }
                
                if (fbTextarea) {
                    // 如果该层有爬取数据则用之，否则用合并池
                    let fbs = (data && data.fallback_urls && data.fallback_urls.length > 0) ? [...data.fallback_urls]
                            : (mergedFbs.length > 0 ? [...mergedFbs] : []);
                    // ★ 每层最多保存20个兜底链接
                    if (fbs.length > 20) fbs = fbs.slice(0, 20);
                    // ★ 强制注入首页兜底链接
                    for (const mf of MUST_HAVE_FBS) {
                        if (!fbs.includes(mf)) fbs.unshift(mf);
                    }
                    fbTextarea.value = fbs.join(',');
                }
            }
            
            // ★ 自动保存到后端（无需用户手动切换Tab点保存）
            const webNavPayload = {};
            for (let i = 1; i <= 5; i++) {
                const kwEl = document.getElementById('webnav_layer' + i + '_keywords');
                const fbEl = document.getElementById('webnav_layer' + i + '_fallback_urls');
                webNavPayload['layer_' + i] = {
                    keywords: kwEl ? kwEl.value.split(',').map(s => s.trim()).filter(s => s) : [],
                    fallback_urls: fbEl ? fbEl.value.split(',').map(s => s.trim()).filter(s => s) : []
                };
            }
            fetch('/save_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ web_navigation: webNavPayload })
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    console.log('[关键词探索] 已自动保存关键词和兜底链接到配置');
                }
            }).catch(e => console.warn('自动保存配置失败:', e));
                    
            if (!silent) {
                alert('✅ 已将关键词和兜底链接自动填写到各层配置并保存！\n\n可切换到"网站流量"Tab查看。');
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
                seo_referer_mode: document.getElementById('seo_referer_dynamic').checked ? 'dynamic' : 'static',
                // ★ HilltopAds Pop-under 配置
                hilltopads_enabled: document.getElementById('hilltopads_enabled').checked,
                hilltopads_trigger_prob: parseInt(document.getElementById('hilltopads_trigger_prob').value) / 100 || 0.4,
                hilltopads_stay_min: parseInt(document.getElementById('hilltopads_stay_min').value) || 15,
                hilltopads_stay_max: parseInt(document.getElementById('hilltopads_stay_max').value) || 25,
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


import threading as _watchdog_threading


def _safe_page_wait(page, min_wait=2.5, max_wait=5.5, ad_wait=False, deadline=None):
    """替代 wait_until='networkidle'（AdSense页面永远达不到，会卡死）。

    策略：domcontentloaded + 温和随机等待；若目标站包含广告，则额外等广告元素最多 4s。
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=45000)
    except Exception as _e:
        log.debug(f"[_safe_page_wait] domcontentloaded 等待放弃: {type(_e).__name__}")
    t = random.uniform(min_wait, max_wait)
    if deadline:
        t = min(t, max(0.5, deadline - time.time()))
    if t > 0:
        time.sleep(t)
    if ad_wait:
        try:
            page.wait_for_selector(
                "ins.adsbygoogle, [data-ad-client], .ad-container, [class*='adsbygoogle'], [id*='ad-wrapper']",
                timeout=4000,
            )
        except Exception:
            pass


def _hard_timeout_goto(page, url, timeout=60, **kwargs):
    """page.goto 的硬超时封装（避免低质量代理在 SSL connect 阶段卡死）。

    Playwright/Selenium 的 timeout 参数只约束"完成度"；代理 TCP/SSL 握手失败时
    底层可能不触发 timeout，这里用线程 join 做真正的兜底 kill。
    """
    result = [None]
    exc = [None]

    def _run():
        try:
            # 把软超时也传进去（但硬超时 = timeout + 8 秒兜底）
            if "timeout" not in kwargs:
                kwargs["timeout"] = timeout * 1000
            result[0] = page.goto(url, **kwargs)
        except Exception as e:
            exc[0] = e

    worker = _watchdog_threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=timeout + 8)
    if worker.is_alive():
        log.error(f"🚫 [_hard_timeout_goto] 硬超时({timeout + 8}s)：{str(url)[:120]}，尝试关闭页面兜底")
        try:
            page.close()
        except Exception:
            pass
        raise TimeoutError(f"page.goto 硬超时: {str(url)[:120]}")
    if exc[0]:
        raise exc[0]
    return result[0]


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
        """, default="false")
        try:
            if isinstance(has_cloudflare_challenge, bool):
                return has_cloudflare_challenge
            if isinstance(has_cloudflare_challenge, str):
                _s = has_cloudflare_challenge.strip().lower()
                return _s == "true" or _s == "1"
            return bool(has_cloudflare_challenge)
        except Exception:
            return False
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
            
        # 等待验证过程完成（networkidle → _safe_page_wait）
        _safe_page_wait(page, min_wait=3.0, max_wait=6.0, ad_wait=False)
        
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
    """QA指纹与IP一致性校验（阻断式：不一致时返回False，调用方必须拒绝该IP）"""
    country_code = (ip_info or {}).get("country_code") or "未知"
    ip_timezone = (ip_info or {}).get("timezone") or ""
    ip_language = (ip_info or {}).get("language") or ""
    expected_tz = get_timezone_for_country(country_code) if country_code != "未知" else None
    expected_lang = qa_country_language_default(country_code)
    actual_lang = fingerprint.get("language")
    actual_tz = fingerprint.get("timezone")
    ua = fingerprint.get("user_agent", "")
    platform = fingerprint.get("platform", "")

    # ★ 严格一致性判定
    tz_ok = bool(actual_tz) and bool(ip_timezone) and actual_tz == ip_timezone
    lang_ok = bool(actual_lang) and bool(ip_language) and actual_lang == ip_language
    # 时区与国家的交叉校验：指纹时区必须属于该国家的合理时区
    tz_country_ok = True
    if expected_tz and actual_tz:
        # 允许同国家多时区（如US有4个），但绝不允许跨洲
        from ip_info_resolver import COUNTRY_TO_TIMEZONE as _CC_TZ
        _valid_tz = _CC_TZ.get(country_code, expected_tz)
        tz_country_ok = (actual_tz == _valid_tz) or (actual_tz == expected_tz)
        # 额外：如果IP返回的时区与指纹时区一致，也通过
        if not tz_country_ok and ip_timezone == actual_tz:
            tz_country_ok = True

    checks = {
        "country": country_code,
        "timezone_expected": expected_tz,
        "timezone_actual": actual_tz,
        "timezone_ip": ip_timezone,
        "timezone_ok": tz_ok,
        "tz_country_ok": tz_country_ok,
        "language_expected": expected_lang,
        "language_actual": actual_lang,
        "language_ip": ip_language,
        "language_ok": lang_ok,
        "language_prefix_ok": (actual_lang or "").split("-")[0] == expected_lang.split("-")[0],
        "ua_family_ok": ("Chrome/" in ua or "Edg/" in ua or "Chromium/" in ua) and "Firefox/" not in ua and "Version/" not in ua,
        "platform_ok": qa_infer_platform_from_ua(ua) == platform,
        "resolution": fingerprint.get("resolution"),
    }

    # ★ 阻断判定：时区或语言与IP信息不一致 → 拒绝
    all_ok = tz_ok and lang_ok and tz_country_ok
    if not all_ok:
        log.error(
            f"🚫 [QA一致性] 指纹与IP不一致！拒绝该IP！"
            f" country={country_code}, "
            f"tz: 指纹={actual_tz} vs IP={ip_timezone} (ok={tz_ok}), "
            f"lang: 指纹={actual_lang} vs IP={ip_language} (ok={lang_ok}), "
            f"tz_country_ok={tz_country_ok}"
        )
    else:
        log.info(
            f"[QA一致性] ✅ country={country_code} lang={actual_lang}/{expected_lang} "
            f"tz={actual_tz}/{expected_tz} ua_chromium={checks['ua_family_ok']} "
            f"platform={platform} platform_ok={checks['platform_ok']} resolution={checks['resolution']}"
        )
    checks["all_consistent"] = all_ok
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
        "Europe/Dublin": "en-IE", "Europe/Madrid": "es-ES", "Europe/Rome": "it-IT",
        "Europe/Moscow": "ru-RU", "Europe/Amsterdam": "nl-NL",
        "America/New_York": "en-US", "America/Chicago": "en-US", "America/Denver": "en-US", "America/Los_Angeles": "en-US",
        "America/Toronto": "en-CA", "America/Vancouver": "en-CA",
        "America/Sao_Paulo": "pt-BR", "America/Mexico_City": "es-MX",
        "Asia/Shanghai": "zh-CN", "Asia/Tokyo": "ja-JP", "Asia/Seoul": "ko-KR",
        "Asia/Singapore": "en-SG", "Asia/Hong_Kong": "zh-HK", "Asia/Taipei": "zh-TW",
        "Asia/Jakarta": "id-ID", "Asia/Kolkata": "en-IN", "Asia/Bangkok": "th-TH",
        "Australia/Sydney": "en-AU", "Australia/Melbourne": "en-AU",
        "Pacific/Auckland": "en-NZ",
        "Africa/Johannesburg": "en-ZA", "Africa/Lagos": "en-NG", "Africa/Cairo": "ar-EG"
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
    
    # 根据语言前缀选择 User-Agent（使用 UA 池管理器 P2-1：国家+小时段缓存7天）
    lang_prefix = ip_language.split("-")[0]
    # P2-1：country_code 优先从 ip_info.country_code 取（如 US/JP/CN/...），
    #       若缺失则从 ip_language / ip_timezone 推导，保证 UA 按 {国家+小时段} 命中同一个 UA。
    _country_for_ua = (ip_info or {}).get("country_code")
    if not _country_for_ua:
        try:
            # 从 BCP47 语言推导（en-US → US, zh-CN → CN, ja-JP → JP）
            _split = (ip_language or "").split("-")
            if len(_split) >= 2 and len(_split[-1]) == 2:
                _country_for_ua = _split[-1].upper()
        except Exception:
            _country_for_ua = None
    if not _country_for_ua:
        # 再兜底：从 timezone 反向到国家（使用 COUNTRY_TIMEZONE_MAP 的反向映射）
        try:
            _rev = {}
            for _cc, _tz in COUNTRY_TIMEZONE_MAP.items():
                if isinstance(_tz, str):
                    _rev.setdefault(_tz, _cc.upper())
            if ip_timezone and ip_timezone in _rev:
                _country_for_ua = _rev[ip_timezone]
        except Exception:
            _country_for_ua = None
    user_agent = ua_pool_manager.get_ua(lang_prefix, browser_family="chromium", country_code=_country_for_ua)
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
    
    # P2-2 指纹独立种子（不再共享 random 状态）
    if _HAS_RCE:
        _fp_id = ip_info.get('country_code', 'XX') if isinstance(ip_info, dict) else 'XX'
        if 'user_agent' in dir() and user_agent:
            _fp_id = f"{_fp_id}|{user_agent[:32]}"
        canvas_noise_seed = _rce.fingerprint_seed.get(_fp_id)
    else:
        canvas_noise_seed = random.randint(1, 2**31 - 1)
    
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
        "canvas_noise_seed": canvas_noise_seed,
        "fonts": fonts_shuffled,
        "platform": platform,
        "hardware_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16, 32]),
        "battery_level": round(random.uniform(0.35, 1.0), 2),
        "orientation_type": "landscape-primary" if random.random() < 0.85 else "portrait-primary",
        "orientation_angle": 0
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
    
    # 4. 模拟页面滚动（观看过程中可能会滚动页面）★ 从配置读取参数
    log.info("📜 模拟页面滚动")
    _scroll_cfg = config.get("scroll_pixels", {"min": 200, "max": 1000})
    _scroll_wait_cfg = config.get("scroll_wait", {"min": 0.5, "max": 5})
    _scroll_count_cfg = config.get("scroll_count", {"min": 2, "max": 10})
    scroll_count = min(random.randint(0, max(2, int(_scroll_count_cfg.get("min", 2)))), 3)
    for _ in range(scroll_count):
        scroll_amount = random.randint(-int(_scroll_cfg.get("max", 1000)) // 2, int(_scroll_cfg.get("max", 1000)) // 2)
        page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        behavior_stats["scrolls"] += 1
        behavior_stats["scroll_distance"] += abs(scroll_amount)
        wait_time = random.uniform(float(_scroll_wait_cfg.get("min", 0.5)), float(_scroll_wait_cfg.get("max", 5)))
        time.sleep(wait_time)
        behavior_stats["waits"] += 1
        behavior_stats["total_stay"] += int(wait_time * 1000)  # 转换为毫秒
    
    # 5. 模拟鼠标移动到随机位置（使用贝塞尔曲线）
    log.info("🖱️ 随机移动鼠标")
    mouse_move_count = random.randint(int(config.get("mouse_move_count", {}).get("min", 2)), int(config.get("mouse_move_count", {}).get("max", 20)))
    for _ in range(mouse_move_count):
        target_x = random.randint(100, 1800)
        target_y = random.randint(100, 900)
        
        # 使用贝塞尔曲线移动鼠标
        human_mouse_move(page, current_x, current_y, target_x, target_y, config)
        
        current_x, current_y = target_x, target_y
        behavior_stats["mouse_moves"] += 1
        
        _mouse_wait_cfg = config.get("mouse_move_wait", {"min": 0.1, "max": 1.0})
        move_wait = random.uniform(float(_mouse_wait_cfg.get("min", 0.1)), float(_mouse_wait_cfg.get("max", 1.0)))
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
            # ============================================================
            # ★ P0-1 重写：广告点击前摇（真人浏览路径，避免"直奔广告"）
            # 顺序：1. 在正文段落停留阅读 → 2. 滚动到广告露出上边缘 → 3. 鼠标路过广告外侧 →
            #       4. 回扫到广告 → 5. 凝视微抖动 → 6. ActiveView可见性校验 → 7. click
            # ============================================================
            # 1) 先在正文其他内容区停留（模拟先看正文，广告是顺带看到的）
            _anchors_raw = page_eval(page, """() => {
              try {
                const arr = Array.from(document.querySelectorAll(
                  'article p, main p, .content p, .entry-content p, section p, [class*="post"] p'
                ));
                return arr.slice(0, 10).map(p => {
                  const r = p.getBoundingClientRect();
                  return r.width > 100 && r.height > 15
                    ? {x: r.x + r.width*0.5, y: r.y + r.height*0.5}
                    : null;
                }).filter(Boolean);
              } catch(e){ return []; }
            }""", default=[])
            read_anchors = []
            try:
                if isinstance(_anchors_raw, list):
                    read_anchors = [a for a in _anchors_raw if isinstance(a, dict) and "x" in a and "y" in a]
            except Exception:
                read_anchors = []
            _used_anchor_count = 0
            if len(read_anchors) >= 2:
                _k = min(4, len(read_anchors))
                _sample_n = random.randint(2, _k)
                import random as _r_a
                for rp in _r_a.sample(read_anchors, k=_sample_n):
                    tx = int(rp.get('x', 500)); ty = int(rp.get('y', 400))
                    human_mouse_move(page, current_x, current_y, tx, ty, config)
                    current_x, current_y = tx, ty
                    _rp = random.uniform(2.5, 7.0)
                    time.sleep(_rp)
                    behavior_stats["total_stay"] += int(_rp * 1000)
                    _used_anchor_count += 1
            else:
                # 兜底：在 (200,300)~(1400,900) 随机停留 2~4 处
                for _ in range(random.randint(2, 4)):
                    tx, ty = random.randint(200, 1400), random.randint(300, 900)
                    human_mouse_move(page, current_x, current_y, tx, ty, config)
                    current_x, current_y = tx, ty
                    time.sleep(random.uniform(2.0, 5.5))

            ad_box_pre = None
            try:
                ad_box_pre = ad_element.bounding_box()
            except Exception:
                ad_box_pre = None
            # 2) 滚动到广告上方 80~150px（只露出广告上边缘，像真人自然滚动那样慢慢进入视野）
            if ad_box_pre:
                try:
                    target_top = max(0, int(ad_box_pre["y"]) - random.randint(80, 150))
                    page.evaluate(f"window.scrollTo(0, {target_top})")
                    time.sleep(random.uniform(2.5, 9.0))
                    # 再慢慢滚 1~2 次 30~80px，让广告自然入视
                    for _ in range(random.randint(1, 2)):
                        pxs = random.randint(30, 80)
                        page.evaluate(f"window.scrollBy(0, {pxs})")
                        time.sleep(random.uniform(0.8, 2.5))
                except Exception:
                    pass

            # 再次获取广告 box（滚动后位置改变）
            box = None
            try:
                box = ad_element.bounding_box()
            except Exception:
                box = None
            ad_center_x = current_x
            ad_center_y = current_y
            if box:
                ad_center_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                ad_center_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                # 3) 鼠标先从阅读点移动到广告"外侧"（路过效果），再回扫到广告
                side = random.choice(["left", "right", "bottom"])
                if side == "left":
                    pass_x = box["x"] - random.randint(60, 120)
                    pass_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                elif side == "right":
                    pass_x = box["x"] + box["width"] + random.randint(60, 120)
                    pass_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                else:
                    pass_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                    pass_y = box["y"] + box["height"] + random.randint(60, 120)
                human_mouse_move(page, current_x, current_y, int(pass_x), int(pass_y), config)
                current_x, current_y = int(pass_x), int(pass_y)
                time.sleep(random.uniform(0.35, 1.1))  # 路过的短暂停顿
                # 4) 回扫到广告中心
                human_mouse_move(page, current_x, current_y, int(ad_center_x), int(ad_center_y), config)
                current_x, current_y = int(ad_center_x), int(ad_center_y)

            # 5) 凝视阶段：1.5~3.5s 内 ±4px 微抖动（模拟眼球带动鼠标）
            _gaze_start = time.time()
            _gaze_dur = random.uniform(1.5, 3.5)
            while time.time() - _gaze_start < _gaze_dur:
                try:
                    page.mouse.move(
                        int(ad_center_x + random.randint(-4, 4)),
                        int(ad_center_y + random.randint(-4, 4)),
                        steps=random.randint(3, 8),
                    )
                except Exception:
                    pass
                time.sleep(random.uniform(0.15, 0.4))
            behavior_stats["total_stay"] += int(_gaze_dur * 1000)

            # ★ ad_stay_time 不再让鼠标钉死在广告上（100%机器人特征）
            # 改为：鼠标离开广告区到下方正文继续阅读，期间累计 ad_stay（表示用户把广告留在视野里阅读正文）
            ad_stay_time = get_random_value(config["ad_stay_time"])
            _ad_stay_end = time.time() + ad_stay_time
            _away_targets = [
                (int(ad_center_x) + random.randint(-80, -20), int(ad_center_y) + random.randint(80, 160)),
                (int(ad_center_x) + random.randint(20, 80), int(ad_center_y) + random.randint(140, 220)),
            ]
            away_x, away_y = random.choice(_away_targets)
            human_mouse_move(page, current_x, current_y, away_x, away_y, config)
            current_x, current_y = away_x, away_y
            while time.time() < _ad_stay_end:
                _chunk = min(random.uniform(1.5, 3.5), _ad_stay_end - time.time())
                if _chunk > 0:
                    time.sleep(_chunk)
                # 偶尔微移
                if random.random() < 0.3:
                    try:
                        page.mouse.move(
                            current_x + random.randint(-12, 12),
                            current_y + random.randint(-12, 12),
                            steps=random.randint(2, 6),
                        )
                    except Exception:
                        pass
            behavior_stats["ad_stay"] = int(ad_stay_time * 1000)
            behavior_stats["total_stay"] += int(ad_stay_time * 1000)

            # 模拟点击广告的概率
            ad_click_prob = get_random_value(config["ad_click_prob"])
            if random.random() < ad_click_prob:
                # 单日点击上限校验（跨任务/跨会话持久化）
                if daily_ad_click_limit_reached():
                    _dl = fingerprint_stats.get('daily_ad_click_limits', {}).get(_today_key(), '?')
                    log.warning(
                        f"🚫 今日广告点击已达上限(当日上限={_dl}，已点击={get_daily_ad_clicks()})，本次跳过点击")
                    raise StopIteration

                # ★ P1-3：ActiveView 可见性校验（≥50% 面积 + ≥1s 可见），不达标跳过点击
                _sel_js = config.get("ad_selector", ".ad-container, [class*='ad'], [id*='ad']")
                _visible_ms = 0
                try:
                    _visible_ms_raw = page_eval(page, f"""() => {{
                      return new Promise(function(resolve) {{
                        try {{
                          const sel = {json.dumps(_sel_js)};
                          const el = document.querySelector(sel) || (document.querySelectorAll(sel) && document.querySelectorAll(sel)[0]);
                          if (!el) return resolve(0);
                          let seen = 0; let i = 0; const MAX = 12;
                          const iv = setInterval(() => {{
                            try {{
                              const r = el.getBoundingClientRect();
                              const vx = Math.max(0, Math.min(r.width, window.innerWidth - r.x));
                              const vy = Math.max(0, Math.min(r.height, window.innerHeight - r.y));
                              const ratio = (vx*vy) / Math.max(1, r.width*r.height);
                              if (ratio >= 0.5) seen += 100; else seen = 0;
                              if (seen >= 1000 || ++i > MAX) {{ clearInterval(iv); resolve(seen); return; }}
                            }} catch(_) {{ clearInterval(iv); resolve(seen); return; }}
                          }}, 100);
                          setTimeout(() => {{ clearInterval(iv); resolve(seen); }}, 1400);
                        }} catch(_) {{ resolve(0); }}
                      }});
                    }}""", default="0")
                    try:
                        _visible_ms = int(_visible_ms_raw)
                    except Exception:
                        _visible_ms = 0
                except Exception:
                    _visible_ms = 0
                if _visible_ms < 800:
                    log.warning(
                        f"🚫 广告可见性不足(可见{_visible_ms}ms，未达ActiveView ≥50%且≥1s)，跳过点击")
                    raise StopIteration

                # 记录点击前的页面数量，用于检测广告落地页新标签（Playwright API）
                _context = page.context
                _pages_before = len(_context.pages)

                # 鼠标回到广告中心（微偏移），然后 click
                try:
                    final_cx = int(ad_center_x + random.randint(-6, 6))
                    final_cy = int(ad_center_y + random.randint(-6, 6))
                    page.mouse.move(final_cx, final_cy, steps=random.randint(6, 14))
                    time.sleep(random.uniform(0.2, 0.6))
                    page.mouse.click(final_cx, final_cy)
                    current_x, current_y = final_cx, final_cy
                except Exception:
                    try:
                        ad_element.click(force=True)
                    except Exception:
                        pass
                behavior_stats["clicks"] += 1
                _today_clicks = record_ad_click(1)
                log.info(f"🖱️ 广告点击已记录（今日累计 {_today_clicks} 次，可见{_visible_ms}ms）")
                
                ad_click_wait = get_random_value(config["ad_click_wait"])
                time.sleep(ad_click_wait)
                behavior_stats["waits"] += 1
                behavior_stats["total_stay"] += int(ad_click_wait * 1000)
                # 标记：本任务流程发生过"广告点击→新标签打开落地页→关闭返回原站"
                did_return_after_ad = False
                # ========== 广告点击后落地页真人行为（Playwright：检测新标签页→停留→滚动→关闭） ==========
                try:
                    _pages_after = _context.pages
                    _opened_new = len(_pages_after) > _pages_before
                    if _opened_new:
                        _landing_page = _pages_after[-1]  # 最新打开的标签页
                        _lp_url = ""
                        try:
                            _lp_url = _landing_page.url or ""
                        except Exception:
                            pass
                        log.info(f"🛬 广告落地页已打开: {_lp_url[:100]}，开始真人浏览")
                        # 等待落地页加载
                        _lp_load = get_random_value(config.get("page_load_wait", {"min": 2, "max": 5}))
                        time.sleep(_lp_load)
                        # ★ 落地页停留时间：对数正态（中位数25s，最低15s）
                        import math as _math_lp
                        _lp_stay = max(15, min(90, _math_lp.exp(random.gauss(_math_lp.log(25), 0.5))))
                        # 落地页滚动浏览（1~3 次）
                        _lp_scrolls = random.randint(1, max(1, min(3, int(config.get("scroll_count", {}).get("min", 2)))))
                        for _i in range(_lp_scrolls):
                            try:
                                _lp_sp = config.get("scroll_pixels", {"min": 200, "max": 1000})
                                _dist = random.randint(int(_lp_sp.get("min", 200)), int(_lp_sp.get("max", 1000)))
                                _landing_page.evaluate(f"window.scrollBy(0, {_dist})")
                                behavior_stats["scrolls"] += 1
                                behavior_stats["scroll_distance"] += _dist
                                _lp_sw = config.get("scroll_wait", {"min": 0.5, "max": 5})
                                time.sleep(random.uniform(float(_lp_sw.get("min", 0.5)), float(_lp_sw.get("max", 5))))
                            except Exception:
                                break
                        # 落地页剩余停留时间
                        _elapsed = _lp_load + _lp_scrolls * 2.5
                        _remaining_stay = max(0, _lp_stay - _elapsed)
                        if _remaining_stay > 0:
                            time.sleep(_remaining_stay)
                        behavior_stats["total_stay"] += int(_lp_stay * 1000)
                        # ★ 关闭前鼠标移动到关闭按钮区域（模拟真人关闭标签页）
                        try:
                            _vw = _landing_page.viewport_size or {"width": 1920, "height": 1080}
                            _landing_page.mouse.move(random.randint(_vw["width"] - 80, _vw["width"] - 20), random.randint(5, 25))
                            time.sleep(random.uniform(0.3, 0.8))
                        except Exception:
                            pass
                        log.info(f"🛬 落地页浏览完成（停留≈{_lp_stay:.1f}s，滚动{_lp_scrolls}次），关闭并返回原站")
                        # 关闭落地页标签
                        try:
                            _landing_page.close()
                        except Exception:
                            pass
                        # ★ 关闭落地页后，激活标记位，随后会执行"原站续读 15~40s"（P2-3）
                        did_return_after_ad = True
                except Exception as _lp_err:
                    log.debug(f"落地页行为处理异常（忽略）: {type(_lp_err).__name__}: {str(_lp_err)[:80]}")
        except Exception:
            pass
    # 未命中任何广告点击分支：复位标记（避免外层误触发原站续读）
    try:
        did_return_after_ad = bool(did_return_after_ad) if "did_return_after_ad" in locals() else False
    except Exception:
        did_return_after_ad = False

    # ========== ★ P2-3：落地页关闭后"原站续读 15~40s"（降低跳出率，Google Ads 会跟踪 bounce rate） ==========
    # 真实用户：点完广告若内容不吸引，会关掉新标签回到原站继续看下文/相关推荐/底部评论；
    # 机器人：点完广告直接 close 当前页（100%跳出，被 Ads Invalid Traffic 直接判定）
    # 逻辑：仅在"发生过广告点击后原站仍然有效"时执行，通过 did_return_after_ad 标志位判断。
    try:
        if did_return_after_ad:
            _after_stay = random.uniform(15, 40)
            _t0_after = time.time()
            log.info(f"🪂 P2-3：广告落地页已关闭，回到原站续读 {_after_stay:.1f}s（降低跳出率）")
            # 1) 回扫到正文中部（模拟"回到刚才的位置继续看"）
            try:
                _mid_y_raw = page_eval(page, """() => {
                  try {
                    const ps = Array.from(document.querySelectorAll(
                      'article p, main p, .entry-content p, [class*="content"] p, section p'
                    ));
                    if (ps && ps.length) {
                      const idx = Math.max(0, Math.floor(ps.length * 0.5) - 1);
                      const r = ps[idx].getBoundingClientRect();
                      return Math.max(0, window.scrollY + r.top - 120);
                    }
                    const h = (document.body && document.body.scrollHeight) ? document.body.scrollHeight : 0;
                    return h > 0 ? Math.max(0, Math.floor(h * 0.45)) : 0;
                  } catch(e){ return 0; }
                }""", default="0")
                try:
                    _mid_y = 0
                    if isinstance(_mid_y_raw, (int, float)):
                        _mid_y = int(_mid_y_raw)
                    elif isinstance(_mid_y_raw, str):
                        _mid_y = int(_mid_y_raw) if _mid_y_raw.strip() else 0
                except Exception:
                    _mid_y = 0
                if _mid_y > 0:
                    page.evaluate(f"window.scrollTo(0, {_mid_y})")
            except Exception:
                pass
            # 2) 在正文 2~4 个位置停留阅读
            try:
                _read_spots = max(2, min(4, int(config.get("scroll_count", {}).get("min", 2))))
            except Exception:
                _read_spots = 2
            _chunk = _after_stay / max(1, _read_spots + 2)
            for _sp in range(_read_spots):
                try:
                    tx = max(50, min(1800, int(current_x) + random.randint(-120, 160)))
                    ty = max(80, min(950, int(current_y) + random.randint(-120, 160)))
                    human_mouse_move(page, current_x, current_y, tx, ty, config)
                    current_x, current_y = tx, ty
                except Exception:
                    pass
                try:
                    px = random.randint(60, 140)
                    page.evaluate(f"window.scrollBy(0, {px})")
                except Exception:
                    pass
                time.sleep(max(0.5, _chunk + random.uniform(-0.6, 0.9)))
                behavior_stats["total_stay"] += int(max(0.5, _chunk) * 1000)
            # 3) 剩余时间自然阅读 + 偶尔向上滚动一下（回看一段）
            _remain = max(0, _after_stay - (time.time() - _t0_after))
            if random.random() < 0.4:
                try:
                    page.evaluate(f"window.scrollBy(0, -{random.randint(40, 100)})")
                except Exception:
                    pass
            if _remain > 0:
                time.sleep(_remain)
                behavior_stats["total_stay"] += int(_remain * 1000)
            log.info(f"🪂 原站续读完成（≈{_after_stay:.1f}s）")
    except Exception as _after_read_err:
        log.debug(f"原站续读阶段异常（忽略）: {type(_after_read_err).__name__}: {str(_after_read_err)[:80]}")
    
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
        try:
            _hard_timeout_goto(page, "https://vids.st", timeout=60, wait_until="domcontentloaded")
        except Exception:
            pass
        _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)

        # 查找导航菜单或视频列表
        # 查找视频链接或相关导航
        if page.query_selector('a[href*="/videos"]'):
            page.click('a[href*="/videos"]')
            log.info("✅ 点击视频导航菜单")
            _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)

            # 在视频列表中查找目标视频
            # 这里只是一个示例，实际需要根据网站结构调整
            video_links = page.query_selector_all('a[href*="/v/"]')
            if video_links:
                log.info(f"✅ 在视频列表中找到 {len(video_links)} 个视频")
                # 可以尝试点击与目标视频相关的链接
                # 或者直接跳转到目标视频页面
                try:
                    _hard_timeout_goto(page, video_url, timeout=60, wait_until="domcontentloaded")
                except Exception:
                    pass
                _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
                log.info("✅ 成功导航到视频页面")
                return True
            else:
                log.warning("⚠️ 在视频列表中未找到视频")
                # 直接访问视频页面
                try:
                    _hard_timeout_goto(page, video_url, timeout=60, wait_until="domcontentloaded")
                except Exception:
                    pass
                _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
                return True
        else:
            log.warning("⚠️ 未找到视频导航菜单，直接访问视频页面")
            try:
                _hard_timeout_goto(page, video_url, timeout=60, wait_until="domcontentloaded")
            except Exception:
                pass
            _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
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
    
    # 等待页面加载（networkidle → _safe_page_wait）
    _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
    
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
                
                # 等待登录完成（networkidle → _safe_page_wait）
                _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)

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
                try:
                    _hard_timeout_goto(page, video_url, timeout=60, wait_until="domcontentloaded")
                except Exception:
                    pass
                _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
        else:
            log.info(f"正在访问视频页面...")
            try:
                _hard_timeout_goto(page, video_url, timeout=60, wait_until="domcontentloaded")
            except Exception:
                pass
            _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=False)
        
        # 设置视频请求的Referer
        final_referer = referer_url

        # 兜底：若 select_video_referer_for_task 函数未定义，使用 config 默认值（避免 NameError）
        def _safe_select_video_referer(cfg, idx):
            try:
                _ref_list = cfg.get("video_referers") or []
                if isinstance(_ref_list, list) and _ref_list:
                    return list(_ref_list)
                if isinstance(cfg.get("seo"), dict):
                    _kw = cfg["seo"].get("keywords") or []
                    if isinstance(_kw, list) and _kw:
                        try:
                            from urllib.parse import quote as _q
                            return [f"https://www.google.com/search?q={_q(str(_kw[0]))}"]
                        except Exception:
                            return [f"https://www.google.com/search?q={_kw[0]}"]
                return ["https://www.google.com/"]
            except Exception:
                return ["https://www.google.com/"]

        # 对于udis视频，使用当前任务已选择的Referer；若没有则按任务序号0取列表首项
        if is_udis_video_url(original_video_url):
            final_referer = config.get('current_video_referer') or _safe_select_video_referer(config, 0)[0]
            log.info(f"🎯 udis视频，使用当前任务Referer: {final_referer}")
        else:
            # 非udis视频，如果没有传入referer，使用当前任务Referer或列表首项
            if not final_referer:
                final_referer = config.get('current_video_referer') or _safe_select_video_referer(config, 0)[0]
            log.info(f"📋 使用Referer: {final_referer}")
        
        # ==================== 第一步：Cloudflare 验证绕过 ====================
        log.info("🛡️ 开始 Cloudflare 验证绕过...")
        try:
            # 访问目标页面，等待验证加载（使用更宽松的超时设置 + 硬超时兜底）
            try:
                response = _hard_timeout_goto(
                    page, video_url, timeout=180,
                    wait_until="domcontentloaded", referer=final_referer,
                )
            except Exception:
                response = None
            _safe_page_wait(page, min_wait=3.0, max_wait=6.0, ad_wait=False)

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
                try:
                    response = _hard_timeout_goto(
                        page, video_url, timeout=180,
                        wait_until="domcontentloaded", referer=final_referer,
                    )
                except Exception:
                    response = None
                _safe_page_wait(page, min_wait=3.0, max_wait=6.0, ad_wait=False)

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
        _sp_cfg2 = config.get("scroll_pixels", {"min": 200, "max": 1000})
        scroll_amount = random.randint(0, int(_sp_cfg2.get("max", 1000)))
        page.evaluate(f"""
            window.scrollTo({{
                top: {scroll_amount},
                behavior: 'smooth'
            }});
        """)
        behavior_stats["scrolls"] += 1
        behavior_stats["scroll_distance"] += scroll_amount
        _sw_cfg2 = config.get("scroll_wait", {"min": 0.5, "max": 5})
        scroll_wait = random.randint(int(float(_sw_cfg2.get("min", 0.5)) * 1000), int(float(_sw_cfg2.get("max", 5)) * 1000))
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
                    _rcw = config.get("random_click_wait", {"min": 0.5, "max": 2.0})
                    click_wait = random.randint(int(float(_rcw.get("min", 0.5)) * 1000), int(float(_rcw.get("max", 2.0)) * 1000))
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
            _has_video_raw = page_eval(
                page,
                "() => { const v = document.querySelector('video'); return !!v ? '1' : '0'; }",
                default="0",
            )
            has_video = False
            if isinstance(_has_video_raw, bool):
                has_video = _has_video_raw
            elif isinstance(_has_video_raw, str):
                has_video = _has_video_raw.strip() in ("1", "true", "True")
            else:
                try:
                    has_video = bool(_has_video_raw)
                except Exception:
                    has_video = False
            log.info(f"页面上有video元素吗: {'是' if has_video else '否'}")
            
            # 如果没有video元素，尝试查找iframe中的视频
            if not has_video:
                _has_iframe_raw = page_eval(
                    page,
                    "() => { const v = document.querySelector('iframe'); return !!v ? '1' : '0'; }",
                    default="0",
                )
                has_iframe = False
                if isinstance(_has_iframe_raw, bool):
                    has_iframe = _has_iframe_raw
                elif isinstance(_has_iframe_raw, str):
                    has_iframe = _has_iframe_raw.strip() in ("1", "true", "True")
                else:
                    try:
                        has_iframe = bool(_has_iframe_raw)
                    except Exception:
                        has_iframe = False
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
        
        mouse_move_wait = config.get("mouse_move_wait", {"min": 0.1, "max": 1.0})
        scroll_pixels = config.get("scroll_pixels", {"min": 200, "max": 1000})
        scroll_wait = config.get("scroll_wait", {"min": 0.5, "max": 5})
        mouse_steps_cfg = config.get("mouse_move_steps", {"min": 50, "max": 250})
        click_count_cfg = config.get("random_click_count", {"min": 0, "max": 3})
        click_wait_cfg = config.get("random_click_wait", {"min": 0.5, "max": 2.0})
        
        log.info(f"📋 行为模拟参数: 鼠标移动最多{max_mouse_moves}次, 滚动最多{max_scrolls}次, 点击最多{max_clicks}次")
        
        # 阶段1: 模拟鼠标移动（简化版）
        _mmc_cfg = config.get("mouse_move_count", {"min": 2, "max": 20})
        mouse_move_count = min(random.randint(int(_mmc_cfg.get("min", 2)), int(_mmc_cfg.get("max", 20))), max_mouse_moves)
        log.info(f"🖱️ 阶段1: 鼠标移动 {mouse_move_count} 次")
        for _ in range(mouse_move_count):
            if elapsed >= watch_time:
                break
                
            target_x = random.randint(100, page.viewport_size.get('width', 1920) - 100)
            target_y = random.randint(100, page.viewport_size.get('height', 1080) - 100)
            
            # 使用简化的线性移动（避免贝塞尔曲线计算过多）
            page.mouse.move(target_x, target_y, steps=random.randint(int(mouse_steps_cfg.get("min", 50)), int(mouse_steps_cfg.get("max", 250))))
            current_x, current_y = target_x, target_y
            behavior_stats["mouse_moves"] += 1
            
            move_wait = random.uniform(float(mouse_move_wait.get("min", 0.1)), float(mouse_move_wait.get("max", 1.0)))
            sleep_time = min(move_wait, watch_time - elapsed)
            if not video_interruptible_sleep(sleep_time):
                log.warning("⛔ 任务已停止（鼠标移动等待中）")
                return 0, current_x, current_y, behavior_stats
            elapsed += sleep_time
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
        
        # 阶段2: 模拟页面滚动
        _sc_cfg2 = config.get("scroll_count", {"min": 2, "max": 10})
        scroll_count = min(random.randint(max(1, int(_sc_cfg2.get("min", 2))), max(1, int(_sc_cfg2.get("max", 10)))), max_scrolls)
        log.info(f"📜 阶段2: 页面滚动 {scroll_count} 次")
        for _ in range(scroll_count):
            if elapsed >= watch_time:
                break
                
            scroll_amount = random.randint(int(scroll_pixels.get("min", 200)), int(scroll_pixels.get("max", 1000)))
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            behavior_stats["scrolls"] += 1
            behavior_stats["scroll_distance"] += scroll_amount
            
            scroll_wait_time = random.uniform(float(scroll_wait.get("min", 0.5)), float(scroll_wait.get("max", 5)))
            sleep_time = min(scroll_wait_time, watch_time - elapsed)
            if not video_interruptible_sleep(sleep_time):
                log.warning("⛔ 任务已停止（滚动等待中）")
                return 0, current_x, current_y, behavior_stats
            elapsed += sleep_time
            behavior_stats["waits"] += 1
            behavior_stats["total_stay"] += int(sleep_time * 1000)
        
        # 阶段3: 随机点击页面
        click_count = min(random.randint(max(1, int(click_count_cfg.get("min", 0))), max(1, int(click_count_cfg.get("max", 3)))), max_clicks)
        log.info(f"👆 阶段3: 随机点击 {click_count} 次")
        for _ in range(click_count):
            if elapsed >= watch_time:
                break
                
            try:
                target_x = random.randint(100, page.viewport_size.get('width', 1920) - 100)
                target_y = random.randint(100, page.viewport_size.get('height', 1080) - 100)
                
                page.mouse.move(target_x, target_y, steps=random.randint(int(mouse_steps_cfg.get("min", 50)), int(mouse_steps_cfg.get("max", 250))))
                page.mouse.click(target_x, target_y)
                behavior_stats["clicks"] += 1
                
                click_wait_time = random.uniform(float(click_wait_cfg.get("min", 0.5)), float(click_wait_cfg.get("max", 2.0)))
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
                    _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                    wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
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
                    _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                    wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
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
                    _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                    wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
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
                    _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                    wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
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
    # ★ 性能优化：关键词超过50个时随机取50个（避免1935个关键词传入JS导致慢）
    if len(_normalized_user) > 50:
        log.info(f"📌 关键词共{len(_normalized_user)}个，随机取50个进行匹配")
        _normalized_user = random.sample(_normalized_user, 50)
    _merged = list(_normalized_user)
    for t in _normalized_user:
        _seen.add(t)
    for t in _extra_defaults:
        if t not in _seen:
            _merged.append(t)
            _seen.add(t)
    log.info(f"🔍 尝试在页面上找到并点击链接（用户关键词={len(text_list or [])}个, 扩展后共 {len(_merged)} 个）...")

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

        # ★ 使用 JS evaluate 在浏览器内完成链接匹配（比Python逐一遍历CDP快10倍+）
        for attempt in range(2):
            try:
                _js_result = page.evaluate("""
                    (keywords) => {
                        const links = document.querySelectorAll('a[href]');
                        const candidates = [];
                        const seenHref = new Set();
                        const baseUrl = window.location.href;
                        for (const link of links) {
                            const href = (link.getAttribute('href') || '').trim();
                            if (!href || href.startsWith('mailto:') || href.startsWith('tel:') ||
                                href.startsWith('javascript:') || href.startsWith('data:') || href === '#') continue;
                            const text = (link.textContent || '').toLowerCase();
                            const hrefLow = href.toLowerCase();
                            // 规范化相对路径
                            let normalized = href;
                            try { normalized = new URL(href, baseUrl).href; } catch(e) {}
                            if (seenHref.has(normalized)) continue;
                            for (const kw of keywords) {
                                if (!kw) continue;
                                if (text.includes(kw) || hrefLow.includes(kw) || normalized.toLowerCase().includes(kw)) {
                                    // 排除自链接
                                    if (normalized.replace(/\/#?$/, '') === baseUrl.replace(/\/#?$/, '')) continue;
                                    seenHref.add(normalized);
                                    candidates.push({href: normalized, text: text.substring(0, 60), match: kw});
                                    break;
                                }
                            }
                        }
                        return candidates;
                    }
                """, _merged) or []
                
                if _js_result:
                    # 从所有命中链接中随机选一个
                    _chosen = random.choice(_js_result)
                    target_href = _chosen['href']
                    target_text_found = _chosen['text']
                    log.info(
                        f"✅ 命中 {len(_js_result)} 个关键词链接(JS)，随机选中: "
                        f"{str(target_text_found)[:40]} | match={_chosen['match']} | {target_href}"
                    )
                    break
            except Exception as _js_err:
                log.debug(f"JS链接匹配异常(attempt={attempt}): {str(_js_err)[:80]}")
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
                            
                            # ★ 点击链接（用 JS click 避免 Playwright 阻塞等待导航）
                            try:
                                page.evaluate("(el) => el.click()", target_link)
                            except Exception:
                                target_link.click(no_wait_after=True, timeout=5000)
                            log.info(f"✅ 点击包含 {text_list} 的链接成功！")
                            
                            # 等待页面加载（带硬超时+保险绳检查）
                            _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                            wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
                            _click_deadline = time.time() + 15  # 最多等15s
                            while time.time() < _click_deadline:
                                if not task_running:
                                    log.warning("⛔ 点击后等待中任务被停止")
                                    return False, current_x, current_y
                                time.sleep(min(0.5, max(0, _click_deadline - time.time())))
                            try:
                                page.wait_for_load_state('domcontentloaded', timeout=5000)
                            except Exception:
                                log.warning("等待页面load状态超时，但继续执行...")
                            return True, current_x, current_y
                    except Exception as e:
                        log.debug(f"点击链接失败（带移动）: {str(e)}")
                
                # 兜底方案：直接导航到规范化的完整URL（带硬超时保护）
                log.info(f"🚀 使用兜底方案：直接导航到 {target_href}")
                try:
                    if not task_running:
                        return False, current_x, current_y
                    page.goto(target_href, wait_until="domcontentloaded", timeout=15000)
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

    # ★ 修复：Playwright page对象非线程安全，不能用ThreadPoolExecutor！
    # 直接调用（JS evaluate本身很快，<1秒），不再套线程池超时
    _has_kw = bool(text_list and any(str(k).strip() for k in text_list))
    if _has_kw:
        try:
            success, new_x, new_y = click_link_containing_text(page, text_list, current_x, current_y, config)
            if success:
                log.info("✅ 通过关键词链接跳转成功")
                return True, new_x, new_y
        except Exception as e:
            log.warning(f"⚠️ click_link_containing_text 异常: {str(e)[:80]}")

    # ★ 新增：通用链接点击回退——在当前页面点击任意内容链接（排除导航/功能链接）
    try:
        _generic_result = page.evaluate("""
            () => {
                const exclude = /login|logout|admin|register|signup|cart|checkout|account|privacy|terms|dmca|refund|contact|about|faq|mailto|javascript/i;
                const links = document.querySelectorAll('a[href]');
                const candidates = [];
                for (const a of links) {
                    const href = (a.getAttribute('href') || '').trim();
                    if (!href || href === '#' || href.startsWith('javascript:') || href.startsWith('mailto:')) continue;
                    if (exclude.test(href)) continue;
                    const text = (a.textContent || '').trim();
                    if (text.length < 2 && !a.querySelector('img')) continue;
                    // 排除自链接
                    try {
                        const full = new URL(href, window.location.href).href;
                        if (full.replace(/\/#?$/, '') === window.location.href.replace(/\/#?$/, '')) continue;
                    } catch(e) {}
                    candidates.push(href);
                }
                if (candidates.length === 0) return null;
                return candidates[Math.floor(Math.random() * candidates.length)];
            }
        """)
        if _generic_result:
            # 解析相对路径为绝对URL
            from urllib.parse import urljoin as _uj
            _target = _uj(page.url, _generic_result)
            log.info(f"🔗 通用回退：随机点击页面链接 → {_target[:80]}")
            try:
                page.goto(_target, wait_until="domcontentloaded", timeout=12000)
                time.sleep(random.uniform(1.5, 3))
                log.info(f"✅ 通用回退跳转成功")
                return True, current_x or 300, current_y or 300
            except Exception as _ge:
                log.warning(f"⚠️ 通用回退跳转失败: {str(_ge)[:80]}")
    except Exception as _e2:
        log.debug(f"通用链接回退异常: {str(_e2)[:60]}")

    # 如果失败（或关键词为空），尝试使用 fallback_urls
    # ★ 打乱兜底链接池顺序，实现 chapter1~N 分散访问（避免每次只点第一个）
    _fbs = list(fallback_urls or [])
    # ★ 根因修复：过滤无广告页面，避免跳转到/privacy-policy/等无广告页浪费任务
    _NO_AD_PATHS_FB = ['/about', '/contact', '/privacy', '/refund', '/dmca', '/faq', '/terms', '/tos', '/cookie', '/sitemap', '/login', '/register', '/account']
    _fbs_before = len(_fbs)
    _fbs = [u for u in _fbs if u and u.strip() and not any(p in u.lower() for p in _NO_AD_PATHS_FB)]
    if len(_fbs) < _fbs_before:
        log.info(f"🚫 [无广告页过滤] 从兜底链接池移除了 {_fbs_before - len(_fbs)} 个无广告页面URL")
    random.shuffle(_fbs)
    if final_fallback_url:
        _fbs.append(final_fallback_url)  # 终极兜底始终最后尝试

    # ★ 性能优化：最多尝试3个兜底URL，避免19个全试导致超时（每个20s×19=380s/层）
    _max_fb_try = 3
    if len(_fbs) > _max_fb_try:
        log.info(f"📌 兜底URL共{len(_fbs)}个，随机取{_max_fb_try}个尝试（避免超时）")
        _fbs = _fbs[:_max_fb_try]

    if _fbs:
        log.warning(f"⚠️ 未找到关键词链接，尝试使用 {len(_fbs)} 个兜底URL...")
        _consecutive_fail = 0
        for url in _fbs:
            try:
                log.info(f"🚀 尝试兜底URL: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=10000)
                time.sleep(random.uniform(1.5, 3))
                log.info(f"✅ 兜底URL跳转成功：{url}")
                return True, current_x or 300, current_y or 300
            except Exception as e:
                log.warning(f"⚠️ 兜底URL跳转失败：{str(e)[:120]}")
                _consecutive_fail += 1
                # ★ 连续2次失败立即放弃，不再浪费时间
                if _consecutive_fail >= 2:
                    log.warning(f"⚠️ 兜底URL连续{_consecutive_fail}次失败，放弃剩余尝试")
                    break
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
        _fbs2 = list(fallback_urls)
        random.shuffle(_fbs2)
        # ★ 性能优化：最多尝试3个
        if len(_fbs2) > 3:
            _fbs2 = _fbs2[:3]
        log.warning(f"⚠️ 未找到关键词链接，尝试使用 {len(_fbs2)} 个兜底URL...")
        _cf2 = 0
        for url in _fbs2:
            try:
                log.info(f"🚀 尝试兜底URL：{url}")
                page.goto(url, wait_until="domcontentloaded", timeout=10000)
                time.sleep(random.uniform(1.5, 3))
                log.info(f"✅ 兜底URL跳转成功：{url}")
                return True, current_x or 300, current_y or 300
            except Exception as e:
                log.warning(f"⚠️ 兜底URL跳转失败：{url}，错误：{str(e)[:80]}")
                _cf2 += 1
                if _cf2 >= 2:
                    log.warning(f"⚠️ 兜底URL连续{_cf2}次失败，放弃")
                    break
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
            _sw_vf = config.get("scroll_wait", {"min": 0.5, "max": 5})
            wait_scroll = random.uniform(float(_sw_vf.get("min", 0.5)), float(_sw_vf.get("max", 5)))
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
            try:
                frame.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception:
                pass
            _plw = config.get("page_load_wait", {"min": 1, "max": 8})
            wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
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
        
        # 滚动页面 ★ 从配置读取参数
        log.info("滚动首页...")
        _sp_cfg = config.get("scroll_pixels", {"min": 200, "max": 1000})
        _sw_cfg = config.get("scroll_wait", {"min": 0.5, "max": 5})
        try:
            scroll_amount = random.randint(int(_sp_cfg.get("min", 200)), int(_sp_cfg.get("max", 1000)))
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            behavior_stats["scrolls"] += 1
            behavior_stats["scroll_distance"] += scroll_amount
        except Exception:
            pass
        wait_after_scroll = random.uniform(float(_sw_cfg.get("min", 0.5)), float(_sw_cfg.get("max", 5)))
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
                    _plw = config.get("page_load_wait", {"min": 1, "max": 8})
                    wait_load = random.uniform(float(_plw.get("min", 1)), float(_plw.get("max", 8)))
                    time.sleep(wait_load)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(wait_load * 1000)
                    
                    # 停留并滚动
                    page_wait = random.uniform(2, 4)
                    log.info(f"页面停留: {page_wait:.1f}秒")
                    time.sleep(page_wait)
                    behavior_stats["waits"] += 1
                    behavior_stats["total_stay"] += int(page_wait * 1000)
                    
                    # 滚动新页面 ★ 从配置读取
                    try:
                        _sp_cfg3 = config.get("scroll_pixels", {"min": 200, "max": 1000})
                        scroll_amount = random.randint(int(_sp_cfg3.get("min", 200)), int(_sp_cfg3.get("max", 1000)))
                        page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                        behavior_stats["scrolls"] += 1
                        behavior_stats["scroll_distance"] += scroll_amount
                    except Exception:
                        pass
                    _sw_nh = config.get("scroll_wait", {"min": 0.5, "max": 5})
                    wait_after_click_scroll = random.uniform(float(_sw_nh.get("min", 0.5)), float(_sw_nh.get("max", 5)))
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


def _generate_proxy_auth_extension(proxy_host, proxy_port, username, password):
    """动态生成Chrome代理认证扩展，解决Chrome 150+不支持--proxy-server内嵌凭证的问题。
    扩展仅处理代理认证(onAuthRequired)，代理服务器由--proxy-server参数指定。
    返回扩展目录路径，供--load-extension使用。"""
    import json as _json
    ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.proxy_auth_ext')
    os.makedirs(ext_dir, exist_ok=True)
    manifest = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Proxy Auth Helper",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108"
    }
    # ★ 仅处理代理认证，不设置proxy（由--proxy-server命令行参数控制）
    background_js = f"""const USERNAME = {repr(username)};
const PASSWORD = {repr(password)};
chrome.webRequest.onAuthRequired.addListener(
  (details) => ({{authCredentials: {{username: USERNAME, password: PASSWORD}}}}),
  {{urls: ["<all_urls>"]}},
  ["asyncBlocking"]
);
"""
    with open(os.path.join(ext_dir, 'manifest.json'), 'w') as f:
        _json.dump(manifest, f)
    with open(os.path.join(ext_dir, 'background.js'), 'w') as f:
        f.write(background_js)
    log.info(f"[代理认证] 生成MV3认证扩展: {ext_dir} (user={username[:6]}...)")
    return ext_dir

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
        # IP 类型检测（住宅/数据中心/移动/代理）；数据中心/代理IP对广告风控高危，自动拒绝
        _ip_type = resolved.get("ip_type")
        if _ip_type:
            if _ip_type in ("datacenter", "proxy", "vpn", "hosting"):
                log.error(f"🚫 [风控铁律] IP {public_ip} 类型={_ip_type}（数据中心/代理/VPN/托管，广告风控高危），自动拒绝并重新获取")
                continue  # ★ 阻断式：高危IP直接拒绝，重新获取
            else:
                log.info(f"[ADSL] IP {public_ip} 类型={_ip_type}（住宅/移动，安全）")
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


def _format_plan_log_block(plan, title, show_tasks=True, max_tasks=10):
    """将任务计划格式化为日志窗口展示块。

    由于日志是“最新置顶”且每条日志为一个 <p>，多条 log.info 会被倒序展示，
    因此这里将多行内容合并为单条日志（内部用 <br> 分行），保证计划预览正序阅读。
    """
    total = plan.get("total_tasks", 0)
    days = plan.get("plan_days", 1)
    model = plan.get("model_used", "")
    site_age = plan.get("site_age", "")
    dist = plan.get("country_distribution", {})
    dist_str = "、".join(f"{k}={v}" for k, v in dist.items()) if dist else "无"
    lines = [
        f"{title}",
        f"总任务数: <b>{total}</b> | 计划天数: {days} | 流量模型: {model} | 站点年龄: {site_age}",
        f"国家分布: {dist_str}",
    ]
    if show_tasks:
        import datetime as _dt_fmt
        tasks = plan.get("tasks", [])
        for t in tasks[:max_tasks]:
            # 计算目标国本地时间
            _cc = t.get('proxy_country', '')
            _epoch = t.get('actual_start_epoch', 0)
            _local_str = ''
            if _epoch and _cc:
                try:
                    _tz_name = get_timezone_for_country(_cc)
                    _tz_obj = pytz.timezone(_tz_name)
                    _dt_obj = _dt_fmt.datetime.fromtimestamp(_epoch, tz=_tz_obj)
                    _local_str = f" 当地{_dt_obj.strftime('%H:%M')}"
                except Exception:
                    pass
            lines.append(
                f"&nbsp;&nbsp;#{t.get('idx','')} {t.get('plan_time','')} "
                f"[{_cc}]{_local_str} 停留{t.get('task_duration',0)}s"
            )
        if len(tasks) > max_tasks:
            lines.append(f"&nbsp;&nbsp;... 还有 {len(tasks) - max_tasks} 条任务")
    return "<br>".join(lines)


def worker_task(single_task=False, adsl_ip_task=False):
    global task_running, _single_task_mode, stats, pending_plan, planned_total_tasks, current_task_idx, current_plan, adsl_status, _last_executed_plan, config
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
                    "actual_start_epoch": int(time.time()),  # ★ 单独任务立即执行，不需要等待
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
        # ★ 断点恢复：检查是否有当天未完成的计划
        _resumed_plan, _resumed_status = _load_plan_progress()
        if _resumed_plan and _resumed_status:
            _done_count = sum(1 for s in _resumed_status if s == "已完成")
            _total_count = len(_resumed_status)
            if _done_count > 0 and _done_count < _total_count:
                log.info(f"📋 检测到当天未完成的计划，断点恢复！已完成 {_done_count}/{_total_count}，从第 {_done_count+1} 个任务继续")
                daily_plan = _resumed_plan
                current_plan = _resumed_plan
                # 恢复每个任务的状态
                _tasks = daily_plan.get("tasks", [])
                for _i, _s in enumerate(_resumed_status):
                    if _i < len(_tasks):
                        _tasks[_i]["status"] = _s
            else:
                log.info(f"📋 上次计划已全部完成({_done_count}/{_total_count})，生成新计划")
                _clear_plan_progress()
                daily_plan = None
        else:
            daily_plan = None
        
        if daily_plan is None:
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
    # 在日志最顶部展示当前任务计划的执行日志（执行头部，后续实时执行日志会逐条置顶）
    log.info(_format_plan_log_block(
        daily_plan, "▶️ <b>开始执行当前任务计划</b>", show_tasks=False
    ))
    
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
    
    # ★ 保存config基线快照（行为画像修改后每个任务迭代开始时恢复，防止跨任务污染）
    import copy as _copy_cfg_snap
    _config_baseline_snapshot = _copy_cfg_snap.deepcopy(config)

    # ========== ★ P2-5(1)：单任务 watchdog suicide Timer（30 分钟硬上限，杜绝卡死） ==========
    # 背景：偶发 Playwright / Proxy / 广告请求会在某个 task 内卡死（page.goto/page.evaluate 超时后仍不释放），
    # 后续所有任务都被阻塞，相当于整个调度器"停摆"。这里每个 task 外层挂一个 1800s 的 watchdog Timer，
    # 到点仍未执行完就直接 os._exit(24)，由 systemd/supervisor/外层调度器拉起，避免整天 0 任务。
    _task_global_watchdog = [None]  # 用 list 方便内层闭包修改
    _task_suicide_code = 24

    def _start_task_global_watchdog(task_label, seconds=1800):
        """每个 task 外层开启 suicide watchdog。"""
        try:
            import threading as _tw
            _tid = [None]

            def _suicide_fn():
                try:
                    log.critical(
                        f"💀 P2-5 watchdog: 单任务[{task_label}]执行超过 {seconds}s，"
                        f"认定为死锁/卡死，立即 os._exit({_task_suicide_code})，"
                        f"请用 systemd/supervisor 自动拉起并查看上一个任务日志"
                    )
                except Exception:
                    pass
                # 直接 _exit 而不是 sys.exit，避免 finally/atexit 阻塞
                os._exit(_task_suicide_code)

            _t = _tw.Timer(interval=seconds, function=_suicide_fn)
            _t.daemon = True
            _tid[0] = _t
            _t.start()
            _task_global_watchdog[0] = _tid
        except Exception as _e:
            log.debug(f"watchdog 启动失败（不影响任务）: {type(_e).__name__}")

    def _cancel_task_global_watchdog():
        """task 正常结束后取消 suicide watchdog。"""
        try:
            if _task_global_watchdog and _task_global_watchdog[0]:
                _t = _task_global_watchdog[0][0]
                if _t and _t.is_alive():
                    _t.cancel()
                    _task_global_watchdog[0] = None
        except Exception:
            _task_global_watchdog[0] = None

    # ========== ★ P2-5(2)：单任务浏览网站时长全局审计（低于阈值=没给广告注入时间） ==========
    # Google AdSense / Ads 脚本：首次进入目标站后，
    #   1) ad client 初始化 (5~12s)
    #   2) 发起 ad request → 竞拍 → 渲染 (10~25s)
    #   3) ActiveView 计数要 ≥1s 可见
    # 若浏览时长 < 45s，基本等同于"广告刚填完就走"=没曝光没收益。
    # 这里把阈值硬性钉到 60s，并对 < 60s 的任务发出警告+写入日志。
    # （真正补时长的地方在 simulate_human_behavior：P0-2 的 safe_page_wait 与各层最小停留）
    _BROWSE_DURATION_WARN_S = 60.0  # 建议：90s 更稳，60s 是下限红线
    _BROWSE_DURATION_CRITICAL_S = 45.0  # 低于这个值基本 0 收益

    with sync_playwright() as p:
        for task_idx, task in enumerate(tasks_list):
            # ★ 每个任务迭代开始时恢复config到基线状态（防止上一个任务的行为画像修改残留）
            config.update(_copy_cfg_snap.deepcopy(_config_baseline_snapshot))
            
            # ★ 断点恢复：跳过已完成的任务
            if task.get("status") == "已完成":
                log.info(f"⏭️ 跳过已完成的任务 #{task_idx+1}（断点恢复）")
                stats["success"] += 1
                continue

            # ========== ★ P2-5：为本任务开启 suicide watchdog（30 min 硬上限） ==========
            _start_task_global_watchdog(
                f"{task_idx+1}/{total_tasks if 'total_tasks' in dir() else len(tasks_list)}@{task.get('proxy_country','??')}",
                seconds=1800,
            )
            enter_site_time = None  # 进入网站（首页加载完成）时间锚点（放这里防止单分支未定义）

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
            # 附加当前进程时区下的计划时间，避免因进程TZ随IP切换导致"看似未按计划执行"的误解
            _local_plan_hint = ""
            _plan_epoch = current_task.get("actual_start_epoch")
            if _plan_epoch:
                try:
                    import datetime as _dt_hint
                    _local_plan_hint = f" = {_dt_hint.datetime.fromtimestamp(int(_plan_epoch)).strftime('%Y-%m-%d %H:%M:%S')}当前时区"
                except Exception:
                    _local_plan_hint = ""
            log.info(
                f"📌 当前任务: {current_task['idx']}/{total_tasks}, "
                f"计划开始时间={_start_str}(北京时间){_local_plan_hint}, "
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
            elif current_task.get("plan_time"):
                # 旧格式计划无 epoch 字段：按北京时间解析 plan_time 推导 epoch，
                # 严禁回退到 20-40 秒间隔（会导致多天计划被连续提前跑完）
                try:
                    _naive_pt = _dt.datetime.strptime(str(current_task["plan_time"]), "%Y-%m-%d %H:%M:%S")
                    _pt_epoch = int(pytz.timezone("Asia/Shanghai").localize(_naive_pt).timestamp())
                    wait_sec = max(0, _pt_epoch - _now_epoch)
                except Exception:
                    wait_sec = random.uniform(
                        config.get("task_interval", {}).get("min", 20),
                        config.get("task_interval", {}).get("max", 40))
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

                # ⏱️ 前置流程计时起点：从取IP/代理设置开始
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

                        # ===== ★ IP会话频率控制：24h内单IP最多4次，会话间隔≥5分钟 =====
                        if exit_ip and exit_ip != "未知":
                            if not ip_session_manager.is_ip_available(exit_ip):
                                log.warning(
                                    f"⚠️ IP {exit_ip} 24h内使用次数已达上限或会话间隔不足，舍弃"
                                )
                                continue

                        # ===== ★ 任务使用后立即清除代理缓存，防止下一个任务命中相同缓存 =====
                        # 注：IP去重已在 _fetch_proxy_from_ipdeep 内部完成（check + record）
                        # 此处仅确保缓存不会导致下一个任务复用相同代理凭证
                        _ip_provider.invalidate_proxy_cache(current_task.get("proxy_api_url"))

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
                                # ★ 严禁回退到 Etc/UTC！该时区与任何真实用户不匹配，极易被风控标记。
                                # 尝试用国家代码映射表兜底
                                from ip_info_resolver import COUNTRY_TO_TIMEZONE as _CC_TZ_MAP
                                _fallback_tz = _CC_TZ_MAP.get(resolved_ip_info.get("country_code", ""))
                                if _fallback_tz:
                                    resolved_ip_info["timezone"] = _fallback_tz
                                    log.info(f"🗂️ 时区兜底映射: {resolved_ip_info.get('country_code')} → {_fallback_tz}")
                                else:
                                    log.warning(
                                        f"⚠️ IP {exit_ip} 无法确定时区（国家={resolved_ip_info.get('country_code')}），"
                                        f"舍弃换下一个 IP（严禁使用 Etc/UTC）"
                                    )
                                    continue
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

                        # ========== ★ P0-1/P0-4/P1-1 风控钩子：IP/账户/时段准入 ==========
                        if _HAS_RCE and exit_ip and exit_ip != "未知":
                            # adv_id 占位：后续若接入多账户配置，可从 task/account 取
                            _adv_id = current_task.get("adv_account_id") or ""
                            # 构造一个稳定 device_id (基于 fingerprint_id 或任务+UA的hash)
                            # ⚠️ 此时指纹浏览器尚未启动，fingerprint_id/user_agent/selected_ua 均不存在，
                            # 若仅用 国别|语言 会导致同国所有任务指纹相同、被 FP 30 天互斥全部拒绝，
                            # 因此追加随机后缀，保证每次任务指纹唯一
                            _fp = current_task.get("fingerprint_id") or (
                                f"{cc_upper}|{language}|"
                                f"{selected_ua[:64] if 'selected_ua' in dir() else ''}|"
                                f"{uuid.uuid4().hex[:12]}"
                            )
                            # P0-1 隔离池（7 天 C 段 + ASN + 指纹互斥）
                            _ok1, _why1 = _rce.isolate_pool.allow(
                                adv_id=_adv_id or "default",
                                ip=exit_ip,
                                fingerprint=_fp,
                                ua=current_task.get("user_agent") or "",
                                asn=resolved_ip_info.get("asn") or "",
                            )
                            if not _ok1:
                                log.warning(f"⛔ P0-1隔离拒绝，换IP：{_why1}")
                                continue
                            # P0-4 3 层账户×设备×IP 互斥
                            _ok4, _why4 = _rce.adv_isolation.can_acquire(
                                adv_id=_adv_id or "default",
                                device_id=_fp,
                                ip=exit_ip,
                                ua=current_task.get("user_agent") or "",
                            )
                            if not _ok4:
                                log.warning(f"⛔ P0-4账户隔离拒绝，换IP：{_why4}")
                                continue
                            # P1-1 时段分布过滤（当地凌晨拒绝 / 工作时段加权通过）
                            _tz = resolved_ip_info.get("timezone") or timezone
                            _ok_tz, _w, _hr = _rce.tz_schedule.allow_now(_tz)
                            if not _ok_tz:
                                log.warning(
                                    f"⏳ P1-1 时段过滤：当地 {_hr}:00 权重={_w:.2f} < 阈值，"
                                    f"挂起此 IP 并延后 60s 再试"
                                )
                                time.sleep(60)
                                continue
                            # P1-5 Copula 采样：提前为本次任务抽取 bounce/pv/engagement 目标值
                            # ⚠️ 提取目标站 host（_host 函数不存在，直接用 urlparse，避免 NameError 导致任务失败）
                            try:
                                _host_val = urllib.parse.urlparse(target_url).netloc if ('target_url' in dir() and target_url) else ""
                            except Exception:
                                _host_val = ""
                            _b = _rce.copula.sample_behavior(
                                _host_val,
                                country=cc_upper,
                            )
                            current_task.setdefault("_rce_behavior_plan", _b)
                            log.info(
                                f"🎲 P1-5 行为采样 bounce_prob={_b['bounce_prob']:.2f} "
                                f"pages={_b['pages']} engagement={_b['engagement_sec']:.0f}s"
                            )
                            # P2-1 曝光 CV 限流检查
                            _ok_cv, _cv = _rce.exposure_cv.allow(_host_val)
                            if not _ok_cv:
                                log.warning(
                                    f"📉 P2-1 曝光模式异常 CV={_cv:.2f}，本轮注入率降低 30%"
                                )
                                # 70% 概率跳过（软限流）
                                if random.random() < 0.3:
                                    continue
                        # ======================================================================
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
                        
                        # ★ 3.6 关键词长尾策略：优先使用keyword_explore已验证词
                        selected_keyword = None
                        _kw_strategy = config.get("keyword_strategy", {})
                        if _kw_strategy.get("explore_first", True):
                            # 从data/keyword_explore/最新文件中随机抽取
                            import glob as _glob_kw
                            import os as _os_kw
                            _kw_dir = _os_kw.path.join(_os_kw.path.dirname(_os_kw.path.abspath(__file__)), "data", "keyword_explore")
                            _kw_files = sorted(_glob_kw.glob(_os_kw.path.join(_kw_dir, "keywords_*.txt")), reverse=True)
                            if _kw_files:
                                try:
                                    with open(_kw_files[0], 'r', encoding='utf-8') as _kf:
                                        # 过滤注释行（# 开头）和标记行（## 开头），只保留真实关键词
                                        _explored_kws = [
                                            l.strip() for l in _kf.readlines()
                                            if l.strip() and not l.strip().startswith('#')
                                            and len(l.strip().split()) >= 3
                                        ]
                                    if _explored_kws:
                                        selected_keyword = random.choice(_explored_kws)
                                        log.info(f"🔑 使用keyword_explore长尾词: {selected_keyword[:50]}")
                                except Exception:
                                    pass
                        # 兜底：从config关键词池选择
                        if not selected_keyword:
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
                
                # ★ 同步进程/系统时区到 IP 时区（与 ADSL 模式一致，确保日志时间戳 = IP 当地时间）
                if ip_success and resolved_ip_info:
                    try:
                        sync_process_timezone_to_ip(resolved_ip_info)
                    except Exception as _tz_sync_err:
                        log.warning(f"⚠️ 进程时区同步失败（浏览器时区已通过 timezone_id 正确设置，不影响反检测）: {str(_tz_sync_err)[:100]}")
                
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
                    qa_checks = qa_log_fingerprint_ip_consistency(resolved_ip_info, fingerprint)
                    # ★ 阻断式校验：指纹与IP不一致时拒绝该IP，重新获取
                    if not qa_checks.get("all_consistent", False):
                        log.error(f"🚫 指纹与IP一致性校验失败，舍弃该IP，重新获取")
                        fingerprint = None
                        fingerprint_success = False
                        continue
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
                    battery_level = float(fingerprint.get("battery_level", 0.85))
                    orientation_type = fingerprint.get("orientation_type", "landscape-primary")
                    orientation_angle = int(fingerprint.get("orientation_angle", 0))
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
                    # ★ Chrome 150+ 不支持 --proxy-server 内嵌凭证，使用本地代理转发器
                    # 本地转发器: 127.0.0.1:18082 (无需认证) → IPDeep代理 (自动添加认证头)
                    if proxy_username and proxy_password:
                        _relay_addr = _start_proxy_relay(proxy_host, int(proxy_port), proxy_username, proxy_password)
                        proxy_server = _relay_addr  # http://127.0.0.1:18082
                        log.info(f"[代理配置] ✅ 启动本地代理转发器: {_relay_addr} → {proxy_host}:{proxy_port}")
                    else:
                        proxy_server = f"http://{proxy_host}:{proxy_port}"
                        log.info(f"[代理配置] ✅ 直连代理(无认证): {proxy_host}:{proxy_port}")
                    proxy_config = {
                        "server": proxy_server,
                        "username": proxy_username,
                        "password": proxy_password,
                    }
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

                # ========== Step C-1: 目标网站健康检测 ==========
                # ★ 风控修复：已移除 requests.get() 直连目标站！
                # 原因：该请求无Referer、无Cookie、TLS指纹与浏览器不同，
                # 目标站服务器日志会记录一次"裸访问"，AdSense/广告联盟后台可关联此IP为机器人。
                # 所有对目标站的访问必须且只能通过浏览器+Referer来源页进入。
                log.info("🔒 [风控] 已禁用直连诊断，所有目标站访问将通过浏览器Referer链路")

                # 启动浏览器，强制关闭WebRTC，添加反检测参数
                log.info("正在启动浏览器...")
                _headless_mode = bool(config.get("headless", False))
                # ★ 有头模式反检测保障：无图形界面服务器（无 DISPLAY）上必须通过 Xvfb 虚拟显示器运行有头模式，
                #   因为 headless 模式会被谷歌风控识别。若 Xvfb 不可用则任务直接失败，绝不降级为无头模式。
                if not _headless_mode and not os.environ.get("DISPLAY"):
                    try:
                        ensure_xvfb_for_headed_mode(_headless_mode)
                    except Exception as _xvfb_err:
                        log.error(f"❌ 有界面模式不可用（无 DISPLAY 且 Xvfb 启动失败: {str(_xvfb_err)[:120]}）。"
                                  f"有头模式为反检测硬性要求，任务终止。请安装 xvfb: apt-get install -y xvfb")
                        raise RuntimeError(f"Xvfb 不可用，无法运行有头模式: {str(_xvfb_err)[:120]}")
                    if not os.environ.get("DISPLAY"):
                        log.error("❌ Xvfb 启动后仍无 DISPLAY，有头模式不可用，任务终止。")
                        raise RuntimeError("Xvfb 启动后仍无 DISPLAY")
                    log.info(f"✅ Xvfb 虚拟显示器已就绪 DISPLAY={os.environ.get('DISPLAY')}，有头模式可正常运行")
                _use_real_chrome = bool(config.get("use_real_chrome", True))
                if proxy_config and str(proxy_config.get("server", "")).startswith("http://"):
                    log.info("IPDeep HTTP代理模式：通过Selenium Chrome访问，噪音请求已通过--host-rules和JS hook拦截")
                log.info(f"浏览器模式: {'无头(headless=True)' if _headless_mode else '有界面(headless=False, Xvfb虚拟显示)'}，浏览器内核: {'本地 Chrome（带 H.264/AAC）' if _use_real_chrome else '系统Chrome（无专有 codec）'}")
                
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
                        # ★ 代理绕过列表：仅包含 Chrome 内部后台服务，严禁包含 *.google.com！
                        # 之前包含 *.google.com;*.googleapis.com;*.gstatic.com 导致：
                        #   - Google搜索请求绕过代理直连 → Google看到VPS真实IP（致命）
                        #   - Google Fonts/广告资源绕过代理 → IP不一致
                        # 仅保留 Chrome 安全浏览和组件更新等内部服务
                        "--proxy-bypass-list=safebrowsing.googleapis.com;safebrowsinghttpgateway.googleapis.com;clients2.google.com;update.googleapis.com;edgedl.me.gvt1.com",
                    ])
                    # ★ 本地代理转发器已处理认证，无需Chrome扩展
                # WebRTC防护：仅强制走代理，不完全禁用（完全禁用是强检测信号）
                # init_script 已通过包装 RTCPeerConnection + 过滤 ICE candidate 实现 IP 泄露防护
                if config.get("webrtc_leak_check_enabled", True):
                    _launch_args.extend([
                        # ★ 严禁 --disable-webrtc！真实浏览器都有 WebRTC，完全禁用会被风控检测。
                        # 仅用 policy 强制 UDP 走代理，保留 API 可用性
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--enforce-webrtc-ip-handling-policy",
                    ])
                    log.info("WebRTC防泄漏已启用（仅强制代理，保留API可用性）")
                else:
                    log.warning("WebRTC防泄漏已禁用（可能导致IP泄漏风险）")
                
                # ★ 7.1 DNS-over-HTTPS：防止系统级DNS查询泄露真实地理位置
                _launch_args.extend([
                    "--dns-over-https-templates=https://dns.google/dns-query",
                    "--dns-over-https-mode=secure",
                ])
                
                # P2-3 DNS 解析分散
                if _HAS_RCE:
                    _dns_pool = _rce.dns_diversity.pick_resolver(country or 'US')
                    log.info(f"🌐 P2-3 DNS解析器: {_dns_pool}")
                
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
                        "--metrics-recording-only",
                        "--disable-component-update",
                        "--disable-component-extensions-with-background-pages",
                        "--disable-sync",
                        "--disable-default-apps",
                        "--disable-hang-monitor",
                        "--disable-prompt-on-repost",
                        "--disable-client-side-phishing-detection"
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
                
                # ★ 风控修复：严禁在 extra_http_headers 中设置全局 Referer！
                # 原因：真实浏览器只在顶层导航请求发送 Referer，子资源(img/css/js/xhr)不发。
                # 全局设置 Referer 会导致所有请求都带 Referer，这是机器人特征，广告联盟可检测。
                # Referer 将通过自然导航链路（先访问来源页→再跳转目标站）正确传递。
                extra_http_headers = {}
                log.info(f"🔒 [风控] Referer将通过自然导航链路传递，来源={generated_referer or '搜索引擎'}")
                
                # 添加 Accept-Language 请求头，确保与指纹语言一致（避免自引用：en-US 不再重复出现在 q=0.9 位）
                lang_prefix = fingerprint["language"].split("-")[0]
                _al_primary = fingerprint['language']  # e.g. "en-US"
                _al_fallback = f",{_al_primary};q=0.9" if _al_primary != f"{lang_prefix}" else ""
                # 构建合理回退链：主语言 → 语言前缀(如果不同) → en-US/en(如果主语言非英语)
                _al_parts = [_al_primary]
                if lang_prefix != _al_primary:
                    _al_parts.append(f"{lang_prefix};q=0.9")
                if not _al_primary.startswith("en"):
                    _al_parts.append("en-US;q=0.8")
                    _al_parts.append("en;q=0.7")
                else:
                    # 英语变体（en-GB/en-AU等）追加 en-US 作为次级回退
                    if _al_primary != "en-US":
                        _al_parts.append("en-US;q=0.8")
                    _al_parts.append("en;q=0.7")
                accept_language = ",".join(_al_parts)
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
                # ★ Sec-Ch-Ua-Mobile: 根据UA动态判断（Android/Mobile为?1，桌面为?0）
                _is_mobile_ua = any(kw in user_agent for kw in ("Android", "Mobile", "iPhone", "iPad"))
                extra_http_headers["Sec-Ch-Ua-Mobile"] = "?1" if _is_mobile_ua else "?0"
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

                # ★ 专家1修复: 上下文参数必须与UA类型严格一致（风控交叉验证致命点）
                _ctx_is_mobile = any(kw in selected_ua for kw in ("Android", "Mobile", "iPhone", "iPad"))
                _ctx_dsf = random.choice([2, 3]) if _ctx_is_mobile else random.choice([1, 2])
                context_kwargs = dict(
                    user_agent=selected_ua,
                    viewport={"width": width, "height": height},
                    locale=browser_locale,
                    timezone_id=browser_timezone,
                    permissions=["geolocation"],
                    geolocation=get_geolocation_for_ip(resolved_ip_info),
                    device_scale_factor=_ctx_dsf,
                    is_mobile=_ctx_is_mobile,
                    has_touch=_ctx_is_mobile,
                    color_scheme="light",
                    extra_http_headers=extra_http_headers if extra_http_headers else None
                )
                if qa_storage_state_path:
                    context_kwargs["storage_state"] = qa_storage_state_path
                context = browser.new_context(**context_kwargs)
                # ★ 地理坐标注入日志
                _geo = context_kwargs.get("geolocation")
                if _geo:
                    log.info(f" 地理坐标注入: lat={_geo['latitude']}, lng={_geo['longitude']}, accuracy={_geo['accuracy']}m")
                
                log.info(f"✅ 浏览器上下文配置完成 - 语言: {browser_locale}, 时区: {browser_timezone}, 分辨率: {resolution}")
                
                # P1-3 电池+运动传感器仿真
                if _HAS_RCE:
                    try:
                        _dev_id = current_task.get('fingerprint_id') or f"{country}|{selected_ua}"
                        _bat = _rce.battery.get_level(_dev_id)
                        _accel = _rce.motion.make_accel(128)
                        log.info(f"🔋 P1-3 电池: {_bat['level_pct']}% charging={_bat['charging']}")
                    except Exception as _rce_e:
                        log.debug(f"P1-3 电池/传感器异常(忽略): {_rce_e}")
                
                # ========== 覆盖Canvas和WebGL指纹，添加完整反检测 ==========
                context.add_init_script(rf"""
                    // ========== -1. Meta Referrer 注入（广告合规，提升收益；WordPress 用户请同时在主题 header.php 中加入 <meta name="referrer" ... />） ==========
                    // 必须在所有 init_script 的第一块执行，保证 AdSense 脚本执行前已经设置好 Referrer Policy
                    (function() {{
                        try {{
                            const _m = document.createElement('meta');
                            _m.setAttribute('name', 'referrer');
                            _m.setAttribute('content', 'no-referrer-when-downgrade');
                            const _insert = function() {{
                                try {{
                                    const _h = document.head || document.getElementsByTagName('head')[0];
                                    if (_h && !document.querySelector('meta[name="referrer"]')) _h.insertBefore(_m, _h.firstChild);
                                }} catch(_) {{}}
                            }};
                            _insert();
                            // Head 可能还没解析完，挂 DOMContentLoaded 再次兜底
                            try {{ document.addEventListener('DOMContentLoaded', _insert, {{ once: true }}); }} catch(_) {{}}
                        }} catch(_) {{}}
                    }})();

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
                    // 隐藏navigator.webdriver —— 使用 Navigator.prototype 层覆盖，
                    // 同时对顶层窗口 + 所有 iframe 生效；返回值必须是 false（真浏览器非自动化时为false），
                    // 且 configurable=true 允许广告联盟脚本重新定义（writable:false/configurable:false 反而会被严格检测识破）
                    (function() {{
                        try {{
                            Object.defineProperty(Navigator.prototype, 'webdriver', {{
                                get: function() {{ return false; }},
                                configurable: true,
                                enumerable: true
                            }});
                        }} catch(e) {{
                            try {{
                                Object.defineProperty(navigator, 'webdriver', {{
                                    get: function() {{ return false; }},
                                    configurable: true,
                                    enumerable: true
                                }});
                            }} catch(_) {{}}
                        }}
                    }})();
                    
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
                    // ★ 已移除假 plugins / mimeTypes 注入（Chrome 120+ 默认隐私模式返回空数组，
                    //   合规、真实且被广告风控接受；造假反而与 Chrome 版本 + Headless 模式冲突，
                    //   导致 fingerprint 一致性校验失败）。
                    // （如需本地非隐私模式验证 plugins 长度，直接在真实 Chrome 打开控制台即可）

                    // ★ navigator.language / languages：可重定义（configurable=true），
                    //   允许广告联盟脚本覆盖，避免被风控严格检测识破。
                    (function() {{
                        try {{
                            const _lang = "{browser_locale}";
                            const _langs = ["{browser_locale}"];
                            // 兼容 zh-CN → en-US 的宽松比对
                            const _primary = _lang.split('-')[0] || _lang;
                            if (_lang.indexOf('-') > 0 && _primary !== _lang) _langs.push(_primary);
                            if (_langs.indexOf('en') === -1) _langs.push('en');
                            Object.defineProperty(Navigator.prototype, 'language', {{
                                get: function() {{ return _lang; }},
                                configurable: true,
                                enumerable: true
                            }});
                            Object.defineProperty(Navigator.prototype, 'languages', {{
                                get: function() {{ return _langs.slice(); }},
                                configurable: true,
                                enumerable: true
                            }});
                        }} catch(e) {{
                            try {{
                                Object.defineProperty(navigator, 'language', {{
                                    get: function() {{ return "{browser_locale}"; }},
                                    configurable: true
                                }});
                            }} catch(_) {{}}
                        }}
                    }})();
                    
                    // ========== 3. Canvas和WebGL指纹（合规化：噪声扰动 + 真实GPU字符串 + toString保护） ==========
                    // Canvas：对真实渲染结果注入稳定的逐像素微噪声（基于会话种子），而非返回固定串
                    (function() {{
                        const _seed = {canvas_noise_seed} >>> 0;
                        // ★ 升级: xorshift128 PRNG（替代LCG，防止ML分类器识别线性同余序列）
                        let _x = _seed || 1, _y = (_seed * 2654435761) >>> 0 || 362436069, _z = (_seed * 2246822519) >>> 0 || 521288629, _w = (_seed * 3266489917) >>> 0 || 88675123;
                        const _rnd = function() {{
                            const t = _x ^ (_x << 11); _x = _y; _y = _z; _z = _w;
                            _w = (_w ^ (_w >>> 19)) ^ (t ^ (t >>> 8));
                            return (_w >>> 0) / 4294967296;
                        }};
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
                    
                    // ========== 3.1 硬件信息注入（hardwareConcurrency / deviceMemory / connection / platform / vendor / maxTouchPoints） ==========
                    try {{
                        Object.defineProperty(navigator, 'hardwareConcurrency', {{
                            get: function() {{ return {hardware_concurrency}; }}, configurable: true
                        }});
                    }} catch(e) {{}}
                    // ★ navigator.platform: 必须与UA一致（风控会交叉验证）
                    try {{
                        const _ua = navigator.userAgent;
                        let _platform = 'Win32';
                        if (_ua.includes('Mac OS X') || _ua.includes('Macintosh')) _platform = 'MacIntel';
                        else if (_ua.includes('Linux') && !_ua.includes('Android')) _platform = 'Linux x86_64';
                        else if (_ua.includes('Android')) _platform = 'Linux armv8l';
                        else if (_ua.includes('iPhone') || _ua.includes('iPad')) _platform = 'iPhone';
                        Object.defineProperty(navigator, 'platform', {{
                            get: function() {{ return _platform; }}, configurable: true
                        }});
                    }} catch(e) {{}}
                    // ★ navigator.vendor: Chrome固定为"Google Inc."
                    try {{
                        Object.defineProperty(navigator, 'vendor', {{
                            get: function() {{ return 'Google Inc.'; }}, configurable: true
                        }});
                    }} catch(e) {{}}
                    // ★ navigator.maxTouchPoints: 桌面=0，移动端=5（与UA一致）
                    try {{
                        const _ua_tp = navigator.userAgent;
                        const _is_touch = _ua_tp.includes('Android') || _ua_tp.includes('iPhone') || _ua_tp.includes('iPad') || _ua_tp.includes('Mobile');
                        Object.defineProperty(navigator, 'maxTouchPoints', {{
                            get: function() {{ return _is_touch ? 5 : 0; }}, configurable: true
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
                    // （已在"2. 补充真实浏览器属性"块中以 Navigator.prototype + configurable:true 覆盖，
                    //   这里的旧 writable:false/configurable:false 版本会与上一个定义冲突，直接删除）

                    // ========== P1-2：全局 Function.prototype.toString 保护 ==========
                    // 任何经过 hook 的函数 .toString() 都必须展示 native code，否则被风控脚本一眼识破
                    // 注意：Python f-string 中不允许反斜杠；这里改用 / 字面量包裹（正斜杠在 f-string 中可直接写）
                    (function() {{
                        try {{
                            const _orig = Function.prototype.toString;
                            // 正则字面量花括号已在Python rf-string中用双写方式转义
                            const _NATIVE_RE = /^\\s*function[^()]*\\([^)]*\\)\\s*{{\\s*\\[native code\\]\\s*}}\\s*$/i;
                            const _HOOK_FLAG = "__pw_native_tostring__";
                            Function.prototype.toString = function() {{
                                try {{
                                    if (this && typeof this === 'function') {{
                                        try {{
                                            if (this[_HOOK_FLAG]) return "function " + (this.name || "") + "() {{ [native code] }}";
                                        }} catch(_) {{}}
                                        try {{
                                            const s = _orig.call(this);
                                            if (typeof s === 'string' && _NATIVE_RE.test(s)) return s;
                                        }} catch(_) {{}}
                                        try {{
                                            const fnName = this.name || "";
                                            if (typeof _ORIGINAL_FN_SET !== 'undefined' && _ORIGINAL_FN_SET && _ORIGINAL_FN_SET.has && _ORIGINAL_FN_SET.has(this)) {{
                                                return "function " + fnName + "() {{ [native code] }}";
                                            }}
                                        }} catch(_) {{}}
                                    }}
                                }} catch(_) {{}}
                                return _orig.apply(this, arguments);
                            }};
                            // 保护自身 toString
                            Object.defineProperty(Function.prototype.toString, 'toString', {{
                                value: function() {{ return 'function toString() {{ [native code] }}'; }},
                                configurable: true
                            }});
                        }} catch(e) {{}}
                    }})();
                    // 用全局 WeakSet 收集"已经被标记为原生"的函数对象（支持 iframe 跨域不访问）
                    (function() {{
                        try {{ window._ORIGINAL_FN_SET = new WeakSet(); }} catch(_) {{}}
                    }})();

                    // ========== P1-2：iframe MutationObserver + 反检测脚本注入 ==========
                    // AdSense 广告在 iframe 里渲染，父窗口 init_script 默认不会注入到跨域 iframe；
                    // 策略：1) 用 MutationObserver 监听新 iframe 创建；2) 对同源 iframe 通过 contentWindow 再次写入保护；
                    //        3) 对跨域 iframe 也不做任何破坏性尝试，只保证父窗口已通过 window.top/frameElement 伪装。
                    (function() {{
                        try {{
                            const _applyIframeProtections = function(win) {{
                                if (!win) return;
                                try {{
                                    // 同源才会成功，跨域抛错直接跳过（符合合规要求）
                                    if (win === window) return;
                                    const d = win.document;
                                    if (!d) return;
                                    // 同源 iframe 内再注入一次 referrer meta
                                    try {{
                                        if (!d.querySelector('meta[name="referrer"]')) {{
                                            const m = d.createElement('meta');
                                            m.name = 'referrer'; m.content = 'no-referrer-when-downgrade';
                                            const h = d.head || d.getElementsByTagName('head')[0];
                                            if (h) h.insertBefore(m, h.firstChild);
                                        }}
                                    }} catch(_) {{}}
                                }} catch(_) {{}}
                            }};
                            // 对已存在的 iframe 扫一遍
                            try {{ Array.prototype.forEach.call(document.querySelectorAll('iframe'), function(f){{ try {{ _applyIframeProtections(f.contentWindow); }} catch(_){{}} }}); }} catch(_) {{}}
                            // 监听后续新增 iframe
                            try {{
                                const _mo = new MutationObserver(function(mutations) {{
                                    for (let i = 0; i < mutations.length; i++) {{
                                        const m = mutations[i];
                                        if (!m || !m.addedNodes || !m.addedNodes.length) continue;
                                        m.addedNodes.forEach(function(n) {{
                                            try {{
                                                if (!n) return;
                                                if (n.nodeType === 1 && n.tagName === 'IFRAME') {{
                                                    setTimeout(function(){{ _applyIframeProtections(n.contentWindow); }}, 0);
                                                    return;
                                                }}
                                                if (n.querySelectorAll) {{
                                                    Array.prototype.forEach.call(n.querySelectorAll('iframe'), function(f){{
                                                        setTimeout(function(){{ _applyIframeProtections(f.contentWindow); }}, 0);
                                                    }});
                                                }}
                                            }} catch(_) {{}}
                                        }});
                                    }}
                                }});
                                _mo.observe(document.documentElement, {{ childList: true, subtree: true }});
                            }} catch(_) {{}}
                        }} catch(e) {{}}
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
                    // ★ Battery API保护（headless可能缺失，补充真实值 + toString保护）
                    try {{
                        if (navigator.getBattery) {{
                            const _origGetBattery = navigator.getBattery.bind(navigator);
                            const _hookedGetBattery = function() {{
                                return Promise.resolve({{
                                    charging: true,
                                    chargingTime: 0,
                                    dischargingTime: Infinity,
                                    level: {battery_level},
                                    onchargingchange: null,
                                    onchargingtimechange: null,
                                    ondischargingtimechange: null,
                                    onlevelchange: null,
                                    addEventListener: function() {{}},
                                    removeEventListener: function() {{}}
                                }});
                            }};
                            Object.defineProperty(_hookedGetBattery, 'toString', {{
                                value: function() {{ return 'function getBattery() {{ [native code] }}'; }},
                                configurable: true
                            }});
                            navigator.getBattery = _hookedGetBattery;
                        }}
                    }} catch(e) {{}}
                    
                    // ★ Screen Orientation保护（补充真实值）
                    try {{
                        if (screen.orientation) {{
                            Object.defineProperty(screen.orientation, 'type', {{
                                get: function() {{ return '{orientation_type}'; }},
                                configurable: true
                            }});
                            Object.defineProperty(screen.orientation, 'angle', {{
                                get: function() {{ return {orientation_angle}; }},
                                configurable: true
                            }});
                        }}
                    }} catch(e) {{}}
                    
                    // ★ SpeechSynthesis保护（headless可能缺失voices）
                    try {{
                        if (window.speechSynthesis) {{
                            const _origGetVoices = window.speechSynthesis.getVoices.bind(window.speechSynthesis);
                            window.speechSynthesis.getVoices = function() {{
                                const voices = _origGetVoices();
                                if (!voices || voices.length === 0) {{
                                    return [
                                        {{ name: 'Google US English', lang: 'en-US', voiceURI: 'Google US English', localService: false, default: true }},
                                        {{ name: 'Google UK English Male', lang: 'en-GB', voiceURI: 'Google UK English Male', localService: false, default: false }}
                                    ].map(v => {{
                                        const voice = Object.create(SpeechSynthesisVoice.prototype);
                                        Object.defineProperties(voice, {{
                                            name: {{ value: v.name }},
                                            lang: {{ value: v.lang }},
                                            voiceURI: {{ value: v.voiceURI }},
                                            localService: {{ value: v.localService }},
                                            default: {{ value: v.default }}
                                        }});
                                        return voice;
                                    }});
                                }}
                                return voices;
                            }};
                        }}
                    }} catch(e) {{}}
                    
                    // ★ MediaDevices保护（headless可能缺失，补充空列表）
                    try {{
                        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
                            const _origEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
                            navigator.mediaDevices.enumerateDevices = function() {{
                                return _origEnumerate().then(devices => {{
                                    // 过滤掉可能暴露自动化的设备
                                    return devices.filter(d => d.kind === 'audiooutput' || d.kind === 'videoinput');
                                }});
                            }};
                        }}
                    }} catch(e) {{}}
                    
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
                    
                    // ========== ★ 4.1 AudioContext指纹噪声（CreepJS检测点） ==========
                    (function() {{
                        try {{
                            const _seed = {canvas_noise_seed} >>> 0;
                            let _x = _seed || 1, _y = (_seed * 2654435761) >>> 0 || 362436069, _z = (_seed * 2246822519) >>> 0 || 521288629, _w = (_seed * 3266489917) >>> 0 || 88675123;
                            const _rnd = function() {{ const t = _x ^ (_x << 11); _x = _y; _y = _z; _z = _w; _w = (_w ^ (_w >>> 19)) ^ (t ^ (t >>> 8)); return (_w >>> 0) / 4294967296; }};
                            // Hook OfflineAudioContext.startRendering
                            if (window.OfflineAudioContext) {{
                                const _origStart = OfflineAudioContext.prototype.startRendering;
                                OfflineAudioContext.prototype.startRendering = function() {{
                                    return _origStart.call(this).then(function(buffer) {{
                                        // 在渲染结果中注入微量噪声
                                        try {{
                                            for (let ch = 0; ch < buffer.numberOfChannels; ch++) {{
                                                const data = buffer.getChannelData(ch);
                                                for (let i = 0; i < data.length; i += 100) {{
                                                    data[i] += (_rnd() - 0.5) * 0.0001;
                                                }}
                                            }}
                                        }} catch(e) {{}}
                                        return buffer;
                                    }});
                                }};
                            }}
                            // ★ P0-3增强: AnalyserNode.getFloatFrequencyData噪声（CreepJS/Pixelscan检测点）
                            if (window.AnalyserNode) {{
                                const _origGetFloat = AnalyserNode.prototype.getFloatFrequencyData;
                                AnalyserNode.prototype.getFloatFrequencyData = function(array) {{
                                    _origGetFloat.call(this, array);
                                    try {{
                                        for (let i = 0; i < array.length; i += 10) {{
                                            array[i] += (_rnd() - 0.5) * 0.01;
                                        }}
                                    }} catch(e) {{}}
                                }};
                                const _origGetByte = AnalyserNode.prototype.getByteFrequencyData;
                                AnalyserNode.prototype.getByteFrequencyData = function(array) {{
                                    _origGetByte.call(this, array);
                                    try {{
                                        for (let i = 0; i < array.length; i += 20) {{
                                            array[i] = Math.max(0, Math.min(255, array[i] + ((_rnd() * 2 | 0) - 1)));
                                        }}
                                    }} catch(e) {{}}
                                }};
                            }}
                            // ★ DynamicsCompressorNode.threshold保护（另一个音频指纹检测点）
                            if (window.DynamicsCompressorNode) {{
                                const _origDynamics = window.DynamicsCompressorNode;
                                // 不覆盖构造函数，仅保护getFloatFrequencyData即可
                            }}
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== ★ 4.2 ClientRects微噪声（防止DOM布局测量指纹） ==========
                    (function() {{
                        try {{
                            const _seed = {canvas_noise_seed} >>> 0;
                            let _x = _seed || 1, _y = (_seed * 2654435761) >>> 0 || 362436069, _z = (_seed * 2246822519) >>> 0 || 521288629, _w = (_seed * 3266489917) >>> 0 || 88675123;
                            const _rnd = function() {{ const t = _x ^ (_x << 11); _x = _y; _y = _z; _z = _w; _w = (_w ^ (_w >>> 19)) ^ (t ^ (t >>> 8)); return (_w >>> 0) / 4294967296; }};
                            const _origGetBCR = Element.prototype.getBoundingClientRect;
                            Element.prototype.getBoundingClientRect = function() {{
                                const rect = _origGetBCR.call(this);
                                // 添加±0.1-0.3px微噪声（极小，不影响布局）
                                const _noise = (_rnd() - 0.5) * 0.4;
                                return new DOMRect(rect.x + _noise, rect.y + _noise, rect.width + _noise, rect.height + _noise);
                            }};
                            Object.defineProperty(Element.prototype.getBoundingClientRect, 'toString', {{
                                value: function() {{ return 'function getBoundingClientRect() {{ [native code] }}'; }},
                                configurable: true
                            }});
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== ★ P2-7: WebGL时间侧信道防护（渲染耗时随机化） ==========
                    (function() {{
                        try {{
                            const _seed = {canvas_noise_seed} >>> 0;
                            let _x = _seed || 1, _y = (_seed * 2654435761) >>> 0 || 362436069, _z = (_seed * 2246822519) >>> 0 || 521288629, _w = (_seed * 3266489917) >>> 0 || 88675123;
                            const _rnd = function() {{ const t = _x ^ (_x << 11); _x = _y; _y = _z; _z = _w; _w = (_w ^ (_w >>> 19)) ^ (t ^ (t >>> 8)); return (_w >>> 0) / 4294967296; }};
                            // Hook performance.now() 添加微噪声（防止通过渲染时间差异指纹化GPU）
                            const _origPerfNow = performance.now.bind(performance);
                            let _perfNoiseAccum = 0;
                            performance.now = function() {{
                                const real = _origPerfNow();
                                // 每次调用添加±0.01-0.05ms的累积漂移（模拟真实系统时钟抖动）
                                _perfNoiseAccum += (_rnd() - 0.5) * 0.04;
                                return real + _perfNoiseAccum;
                            }};
                            Object.defineProperty(performance.now, 'toString', {{
                                value: function() {{ return 'function now() {{ [native code] }}'; }},
                                configurable: true
                            }});
                            // WebGL readPixels时间侧信道：添加随机延迟
                            const _patchReadPixels = function(proto) {{
                                if (!proto || !proto.readPixels) return;
                                const _orig = proto.readPixels;
                                proto.readPixels = function() {{
                                    // 在readPixels前插入微量随机工作（干扰时间测量）
                                    const _junk = new Float32Array(16);
                                    for (let i = 0; i < 16; i++) _junk[i] = Math.sin(i * _rnd());
                                    return _orig.apply(this, arguments);
                                }};
                            }};
                            try {{ _patchReadPixels(WebGLRenderingContext.prototype); }} catch(e) {{}}
                            try {{ _patchReadPixels(WebGL2RenderingContext.prototype); }} catch(e) {{}}
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== ★ P2-8: 事件时序对齐（requestAnimationFrame同步，防止事件时间戳异常） ==========
                    (function() {{
                        try {{
                            // 确保 Date.now() 和 performance.now() 的时间戳与 rAF 帧对齐
                            // 真实浏览器中，事件时间戳始终是帧时间的整数倍（16.67ms @ 60Hz）
                            const _origRAF = window.requestAnimationFrame;
                            let _lastFrameTime = 0;
                            window.requestAnimationFrame = function(callback) {{
                                return _origRAF.call(window, function(timestamp) {{
                                    _lastFrameTime = timestamp;
                                    callback(timestamp);
                                }});
                            }};
                            Object.defineProperty(window.requestAnimationFrame, 'toString', {{
                                value: function() {{ return 'function requestAnimationFrame() {{ [native code] }}'; }},
                                configurable: true
                            }});
                            // 保护 Event.timeStamp：确保事件时间戳与帧时间对齐
                            const _origAddEventListener = EventTarget.prototype.addEventListener;
                            EventTarget.prototype.addEventListener = function(type, listener, options) {{
                                const wrappedListener = function(event) {{
                                    // 将事件时间戳对齐到最近的帧边界（16.67ms倍数）
                                    try {{
                                        if (event && event.timeStamp && _lastFrameTime > 0) {{
                                            const frameInterval = 1000 / 60;  // 60Hz
                                            const aligned = Math.round(event.timeStamp / frameInterval) * frameInterval;
                                            Object.defineProperty(event, 'timeStamp', {{ value: aligned, configurable: true }});
                                        }}
                                    }} catch(e) {{}}
                                    if (typeof listener === 'function') return listener.call(this, event);
                                    if (listener && listener.handleEvent) return listener.handleEvent(event);
                                }};
                                return _origAddEventListener.call(this, type, wrappedListener, options);
                            }};
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== ★ 4.4 document.hasFocus() 保护（AdSense Active View检测） ==========
                    (function() {{
                        try {{
                            const _origHasFocus = document.hasFocus;
                            document.hasFocus = function() {{ return true; }};
                            Object.defineProperty(document.hasFocus, 'toString', {{
                                value: function() {{ return 'function hasFocus() {{ [native code] }}'; }},
                                configurable: true
                            }});
                        }} catch(e) {{}}
                    }})();
                    
                    // ========== ★ 6.2 iframe顶层窗口检测应对（AdSense检测嵌套） ==========
                    (function() {{
                        try {{
                            // 确保 window.top === window.self（防止被检测为嵌套iframe）
                            if (window.top !== window.self) {{
                                Object.defineProperty(window, 'top', {{
                                    get: function() {{ return window.self; }},
                                    configurable: true
                                }});
                            }}
                            // frameElement 返回 null
                            Object.defineProperty(window, 'frameElement', {{
                                get: function() {{ return null; }},
                                configurable: true
                            }});
                        }} catch(e) {{}}
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
                        # ★ 使用当前浏览器实际UA（而非硬编码），避免UA版本不一致被检测
                        custom_headers = {
                            "User-Agent": selected_ua,
                            "Referer": "https://udisxxx.com/"
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
                        # ★ 仅拦截 Chrome 后台噪音请求，严禁拦截 gstatic.com/googleapis.com 全域名！
                        # gstatic.com 承载 Google Fonts 和部分广告资源，拦截后页面字体/广告异常
                        noisy_patterns = (
                            "gstatic.com/generate_204",  # 仅拦截连通性检测
                            "googleapis.com/generate_204",
                            "safebrowsing",
                            "httpbin.org", "api.ipify.org", "icanhazip.com", "ifconfig.me",
                            "checkip.amazonaws.com", "ident.me"
                        )
                        if any(p in req_url for p in noisy_patterns):
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
                log.warning("🩺 代理链路浏览器探测已跳过（阶段2）：后续通过搜索/社媒Referer进入目标站")
                
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
                    reset_ad_click_tracking()  # ★ 新任务开始，清空广告点击去重记录
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
                    
                    # ★ P2-9: 加载跨会话行为画像（微调本次任务参数，制造用户级一致性）
                    _behavior_profile = None
                    try:
                        _bp_site = target_url or ""
                        _bp_country = (country or "US").upper()
                        _behavior_profile = load_behavior_profile(_bp_site, _bp_country)
                        if _behavior_profile and _behavior_profile.get("visit_count", 0) >= 2:
                            config = apply_behavior_profile_to_config(_behavior_profile, config)
                            log.info(f"🧠 [行为画像] 已加载历史画像(访问{_behavior_profile['visit_count']}次)，微调本次行为参数")
                    except Exception as _bp_err:
                        log.debug(f"[行为画像] 加载失败(忽略): {str(_bp_err)[:60]}")
                    
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
                        
                        # ★ P0-2: 流量来源多样化（防止100%搜索流量被审计识别）
                        # 真实网站流量分布: ~60%搜索 + ~20%直接 + ~10%社媒 + ~10%外链
                        _traffic_diversity_cfg = config.get("traffic_diversity", {})
                        _td_enabled = _traffic_diversity_cfg.get("enabled", True)
                        _traffic_source = "search"  # 默认搜索引擎
                        if _td_enabled:
                            _td_roll = random.random()
                            _pct_search = _traffic_diversity_cfg.get("search_pct", 0.60)
                            _pct_direct = _traffic_diversity_cfg.get("direct_pct", 0.20)
                            _pct_social = _traffic_diversity_cfg.get("social_pct", 0.10)
                            # _pct_referral = 1 - search - direct - social
                            if _td_roll < _pct_search:
                                _traffic_source = "search"
                            elif _td_roll < _pct_search + _pct_direct:
                                _traffic_source = "direct"
                            elif _td_roll < _pct_search + _pct_direct + _pct_social:
                                _traffic_source = "social"
                            else:
                                _traffic_source = "referral"
                            log.info(f"🌐 [流量多样化] 本次访问来源: {_traffic_source} (search={_pct_search:.0%}/direct={_pct_direct:.0%}/social={_pct_social:.0%}/referral=剩余)")
                        
                        if _traffic_source == "direct":
                            # ★ 直接访问：用户输入URL/书签（无Referer）
                            log.info(f"🌐 [流量多样化] 直接访问模式（模拟书签/地址栏输入）")
                            simulate_rtt_jitter(base_ms=60, jitter_ms=30)
                            try:
                                page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
                                already_on_target = True
                                enter_site_time = time.time()
                            except Exception as _direct_err:
                                log.warning(f"⚠️ 直接访问失败: {str(_direct_err)[:80]}，回退到搜索模式")
                                _traffic_source = "search"
                        elif _traffic_source == "social":
                            # ★ 社交媒体跳转：先访问社媒平台，再点击链接进入目标站
                            _social_platforms = [
                                {"name": "Facebook", "url": "https://www.facebook.com/"},
                                {"name": "Twitter/X", "url": "https://x.com/home"},
                                {"name": "Reddit", "url": "https://www.reddit.com/"},
                                {"name": "Pinterest", "url": "https://www.pinterest.com/"},
                                {"name": "Instagram", "url": "https://www.instagram.com/"},
                            ]
                            _social = random.choice(_social_platforms)
                            log.info(f"🌐 [流量多样化] 社媒跳转: {_social['name']} → {target_url}")
                            simulate_rtt_jitter(base_ms=100, jitter_ms=50)
                            try:
                                page.goto(_social["url"], timeout=30000, wait_until="domcontentloaded")
                                # 在社媒页面停留（模拟浏览动态）
                                _social_stay = random.uniform(3.0, 8.0)
                                current_x, current_y = simulate_human_in_window(
                                    page, _social_stay, page_behavior_stats, current_x, current_y,
                                    config, page_name=f"社媒({_social['name']})"
                                )
                                # 通过JS导航到目标站（模拟点击链接，保留Referer）
                                page.evaluate(f"window.location.href = '{target_url}'")
                                time.sleep(random.uniform(2.0, 5.0))
                                already_on_target = True
                                enter_site_time = time.time()
                                log.info(f"✅ [流量多样化] 社媒跳转成功: {_social['name']} → 目标站")
                            except Exception as _social_err:
                                log.warning(f"⚠️ 社媒跳转失败: {str(_social_err)[:80]}，回退到搜索模式")
                                _traffic_source = "search"
                        elif _traffic_source == "referral":
                            # ★ 外链跳转：从相关网站点击链接进入（模拟博客/论坛推荐）
                            _referral_sites = [
                                "https://news.ycombinator.com/",
                                "https://www.quora.com/",
                                "https://medium.com/",
                                "https://stackoverflow.com/",
                            ]
                            _ref_site = random.choice(_referral_sites)
                            log.info(f"🌐 [流量多样化] 外链跳转: {_ref_site} → {target_url}")
                            simulate_rtt_jitter(base_ms=90, jitter_ms=40)
                            try:
                                page.goto(_ref_site, timeout=30000, wait_until="domcontentloaded")
                                _ref_stay = random.uniform(2.0, 6.0)
                                current_x, current_y = simulate_human_in_window(
                                    page, _ref_stay, page_behavior_stats, current_x, current_y,
                                    config, page_name=f"外链站({_ref_site.split('//')[1][:20]})"
                                )
                                page.evaluate(f"window.location.href = '{target_url}'")
                                time.sleep(random.uniform(2.0, 4.0))
                                already_on_target = True
                                enter_site_time = time.time()
                                log.info(f"✅ [流量多样化] 外链跳转成功")
                            except Exception as _ref_err:
                                log.warning(f"⚠️ 外链跳转失败: {str(_ref_err)[:80]}，回退到搜索模式")
                                _traffic_source = "search"
                        
                        # P0-2 Referer风控检查
                        if _HAS_RCE:
                            try:
                                _kw = selected_keyword if 'selected_keyword' in dir() and selected_keyword else config.get('seo',{}).get('default_kw','')
                                _ref_result = _rce.referer_guard.check_and_make(
                                    search_url=generated_referer if (_traffic_source == 'search' and 'generated_referer' in dir()) else '',
                                    landing_url=target_url,
                                    kw=_kw,
                                )
                                if _ref_result.get('rewritten'):
                                    log.info(f"🔗 P0-2 Referer已改写: {_ref_result.get('reason')} → {_ref_result.get('referer')[:80]}...")
                            except Exception as _rce_e:
                                log.debug(f"P0-2 Referer检查异常(忽略): {_rce_e}")
                        
                        # 搜索引擎模式（默认/回退）
                        if _traffic_source == "search" and search_mode == "real_search":
                            # 执行完整搜索跳转流程（带真人模拟，支持所有搜索引擎）
                            search_success, current_x, current_y = perform_real_search(page, target_url, selected_engine_id, selected_keyword, page_behavior_stats, current_x, current_y, config)
                            if search_success:
                                log.info(f"🔍 [真搜索] 已成功跳转至目标页，跳过直接导航")
                                already_on_target = True
                                # [调整] 把 enter_site_time 设置为现在（真搜索跳转完成即进入网站）
                                enter_site_time = time.time()
                            else:
                                log.warning(f"🔍 [真搜索] 未成功跳转，将由Referer来源页导航进入目标站")
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

                        # 读取 5 层配置（停留比例 + 最小停留 + 关键字 + 兜底 URL）
                        layers = []
                        total_ratio = 0.0
                        for li in range(1, 6):
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

                        # P2-4 跳转漏斗 + CPL 仿真
                        if _HAS_RCE:
                            try:
                                _target_url = current_task.get('target_url') or target_url
                                _funnel_path = _rce.funnel.build_3layer(_target_url, layers=3)
                                _cpl_stays = _rce.cpl_simulator.simulate(_funnel_path)
                                log.info(f"📐 P2-4 漏斗: {len(_funnel_path)}层, CPL停留: {_cpl_stays}")
                            except Exception as _rce_e:
                                log.debug(f"P2-4 漏斗异常(忽略): {_rce_e}")

                        # ★ 修正逻辑：配置时长优先，保险绳保底
                        #   每轮独立随机，但总时长不超过对数正态保险绳
                        if enter_site_time is None:
                            enter_site_time = time.time()
                        # ★ 对数正态采样（中位数180s，95%分位≈540s，硬上限600s）
                        _sd_cfg = config.get("session_duration", {})
                        _sd_median = float(_sd_cfg.get("median_sec", 180))
                        _sd_sigma = float(_sd_cfg.get("sigma", 0.7))
                        _sd_cap = float(_sd_cfg.get("hard_cap_sec", 600))
                        import math as _math
                        _sd_mu = _math.log(_sd_median)
                        # ★ 修复：保险绳下限必须≥配置的浏览时长最小值，避免deadline提前截断浏览
                        _cfg_stay_min_for_rope = float(config.get("total_stay", {}).get("min", 80))
                        _cfg_stay_max_for_rope = float(config.get("total_stay", {}).get("max", 300))
                        _rope_floor = max(60, _cfg_stay_min_for_rope)  # 不低于配置min，且绝对不低于60s
                        _session_secs = min(_sd_cap, max(_rope_floor, _math.exp(random.gauss(_sd_mu, _sd_sigma))))
                        task_deadline = enter_site_time + _session_secs
                        # ========== ★ 停留日志：进入网站锚点 + Session保险绳参数（用于排查广告收益低） ==========
                        log.info(
                            f"⏱️ [停留-01] enter_site_time锚点: {time.strftime('%H:%M:%S', time.localtime(enter_site_time))}, "
                            f"total_stay配置: min={_cfg_stay_min_for_rope:.0f}s / max={_cfg_stay_max_for_rope:.0f}s"
                        )
                        log.info(
                            f"⏱️ [停留-02] Session保险绳(对数正态): {_session_secs:.0f}s | "
                            f"参数: median={_sd_median:.0f}s, σ={_sd_sigma}, hard_cap={_sd_cap:.0f}s, floor={_rope_floor:.0f}s | "
                            f"task_deadline = {time.strftime('%H:%M:%S', time.localtime(task_deadline))}"
                        )
                        if _session_secs < _BROWSE_DURATION_CRITICAL_S:
                            log.error(
                                f"⏱️ [停留-02/红线] Session保险绳={_session_secs:.0f}s < 红线{_BROWSE_DURATION_CRITICAL_S:.0f}s，"
                                f"广告脚本大概率没完成 init→request→拍卖→渲染，必然 0 收益！建议把 config.total_stay.min 至少调到 {int(_BROWSE_DURATION_WARN_S)+10}s"
                            )
                        elif _session_secs < _BROWSE_DURATION_WARN_S:
                            log.warning(
                                f"⏱️ [停留-02/警告] Session保险绳={_session_secs:.0f}s < 建议值{_BROWSE_DURATION_WARN_S:.0f}s，"
                                f"广告可能有填充但 ActiveView 计数被折损。建议把 config.total_stay.min 至少调到 {int(_BROWSE_DURATION_WARN_S)+10}s"
                            )
                        
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
                            f"任务总浏览时长≈{total_task_stay:.1f}秒（=各轮之和，不含前置/取IP/间隔）"
                        )
                        for _ridx in range(chapter_loop_count):
                            log.info(
                                f"📊 第{_ridx+1}轮每层停留(L1-L{len(layers)}): "
                                + ", ".join(f"L{i+1}≈{round_layer_stays[_ridx][i]:.1f}s" for i in range(len(round_layer_stays[_ridx])))
                            )
    
                        # ========== 风控核心：Referer来源页自然导航链路 ==========
                        # ★ 铁律：严禁直接访问目标网站！必须先访问来源页（搜索引擎/社媒），
                        #   模拟真人浏览行为后，通过自然跳转进入目标站。
                        #   这确保：HTTP Referer正确 + document.referrer正确 + 浏览器历史正确
                        
                        # 统一设置更宽松的导航超时，避免网络抖动时过早失败
                        try:
                            page.set_default_navigation_timeout(120000)
                            page.set_default_timeout(60000)
                        except Exception:
                            pass
    
                        # ---------- 辅助：检测"页面是否真的有内容" ----------
                        _detect = page_has_meaningful_content
    
                        home_load_success = False
                        _home_page_reason = "未执行"

                        # ========== 处理 already_on_target（真搜索模式已成功跳转） ==========
                        if already_on_target:
                            log.info("🔍 [真搜索] 已在目标页，跳过导航，直接检测当前页面")
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
                            # ========== 第 0 步：强制访问Referer来源页（搜索引擎/社媒） ==========
                            # ★ 风控铁律：没有Referer来源页就不允许访问目标站！
                            _referer_url = generated_referer
                            if not _referer_url:
                                # 兆底：如果SEO模块未生成referer，使用搜索引擎首页
                                _referer_url = random.choice([
                                    "https://www.google.com/", "https://www.bing.com/",
                                    "https://search.yahoo.com/", "https://duckduckgo.com/"
                                ])
                                log.warning(f"⚠️ [风控] SEO未生成Referer，使用兆底搜索引擎: {_referer_url}")
                            
                            log.info(f"🔗 [风控铁律] 第0步：访问Referer来源页: {_referer_url[:80]}")
                            _referer_visited = False
                            try:
                                simulate_rtt_jitter(base_ms=60, jitter_ms=50)  # ★ RTT仿真
                                try:
                                    _hard_timeout_goto(page, _referer_url, timeout=30, wait_until="domcontentloaded")
                                except Exception:
                                    pass
                                _safe_page_wait(page, min_wait=1.0, max_wait=2.0, ad_wait=False)
                                _referer_visited = True
                                log.info(f"✅ [风控] Referer来源页已加载: {page.url[:60]}")
                            except Exception as e:
                                log.warning(f"⚠️ [风控] Referer来源页加载异常({str(e)[:60]})，尝试继续")
                                _referer_visited = True  # 即使超时也认为已访问（页面可能部分加载）
                            
                            # ★ 模拟真人在来源页的浏览行为（5-15秒停留+滚动+鼠标+键盘）
                            if _referer_visited:
                                _dwell_time = random.uniform(5.0, 15.0)
                                log.info(f"👤 [风控] 模拟真人在来源页浏览 {_dwell_time:.1f}秒...")
                                # 滚动浏览来源页内容
                                try:
                                    for _scroll_i in range(random.randint(1, 3)):
                                        _scroll_y = random.randint(int(config.get("scroll_pixels", {}).get("min", 100)), int(config.get("scroll_pixels", {}).get("max", 400)))
                                        page.mouse.wheel(0, _scroll_y)
                                        time.sleep(random.uniform(0.8, 2.0))
                                    # 随机鼠标移动（模拟阅读/浏览）
                                    page.mouse.move(random.randint(200, 800), random.randint(150, 500))
                                    time.sleep(random.uniform(0.5, 1.5))
                                    # ★ 风控增强：模拟键盘交互（AdSense行为分析检测keydown事件）
                                    # 真实用户在社媒/搜索页会有Tab导航、箭头键滚动、空格翻页等行为
                                    _kb_actions = random.choice(['tab_nav', 'arrow_scroll', 'space_page', 'none'])
                                    if _kb_actions == 'tab_nav':
                                        for _ in range(random.randint(1, 3)):
                                            page.keyboard.press('Tab')
                                            time.sleep(random.uniform(0.3, 0.8))
                                    elif _kb_actions == 'arrow_scroll':
                                        for _ in range(random.randint(1, 2)):
                                            page.keyboard.press('ArrowDown')
                                            time.sleep(random.uniform(0.5, 1.0))
                                    elif _kb_actions == 'space_page':
                                        page.keyboard.press('Space')
                                        time.sleep(random.uniform(0.8, 1.5))
                                except Exception:
                                    pass
                                # 剩余停留时间
                                _elapsed = 3.0  # 上面滚动大约用了3秒
                                if _dwell_time > _elapsed:
                                    time.sleep(_dwell_time - _elapsed)
                                log.info(f"✅ [风控] 来源页浏览完成，准备自然跳转目标站")
                            
                            # ========== 第 1 步：从来源页自然跳转到目标站 ==========
                            log.info("第 1 步：从Referer来源页自然跳转目标站（layer_1）")
                            
                            _retry_wait_list = [6, 10]
                            for retry in range(3):
                                try:
                                    def optimized_page_goto(page, url, max_retries=2, referer=None):
                                        # ★ 严禁拦截任何资源类型！
                                        # 图片(.png/.jpg/.webp/.gif)是广告素材的核心载体，
                                        # 拦截后 AdSense 广告将显示空白，无法形成有效曝光。
                                        # 仅拦截大体积视频文件以提升加载速度。
                                        try:
                                            page.route("**/*.mp4", lambda route: route.abort())
                                            page.route("**/*.webm", lambda route: route.abort())
                                        except Exception:
                                            pass
                                        for attempt in range(max_retries):
                                            try:
                                                # ★ 风控核心：使用 window.location.href 自然跳转
                                                # 这确保 document.referrer = 来源页URL（真实浏览器行为）
                                                # 而 page.goto(url, referer=xxx) 只设置HTTP头，不设置document.referrer
                                                page.evaluate("(url) => { window.location.href = url; }", url)
                                                # 等待页面加载（networkidle → _safe_page_wait，根治卡死）
                                                try:
                                                    page.wait_for_load_state("domcontentloaded", timeout=25000)
                                                except Exception:
                                                    pass
                                                _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=True)
                                                # Cloudflare/WAF挑战检测
                                                try:
                                                    if is_cloudflare_challenge(page):
                                                        log.info("🔐 检测到Cloudflare验证挑战，等待自动通过...")
                                                        time.sleep(random.uniform(5.0, 8.0))
                                                        _safe_page_wait(page, min_wait=2.0, max_wait=4.0, ad_wait=True)
                                                except Exception:
                                                    pass
                                                # ★ 根因修复：验证导航是否成功（window.location.href可能被Referer页SW/CSP阻止）
                                                # 如果当前URL不是目标站，使用CDP Referer + page.goto强制跳转（不丢失Referer头）
                                                try:
                                                    from urllib.parse import urlparse as _urlparse_nav
                                                    _nav_target = _urlparse_nav(url).hostname or ''
                                                    _nav_current = _urlparse_nav(page.url or '').hostname or ''
                                                    if _nav_target and _nav_current and _nav_target not in _nav_current and _nav_current not in _nav_target:
                                                        log.warning(f"⚠️ [导航失败] window.location.href被{_nav_current}的SW/CSP阻止！使用CDP Referer+goto回退")
                                                        # ★ 通过selenium_bridge的referer参数设置HTTP Referer头（CDP Network.setExtraHTTPHeaders）
                                                        # 这确保目标站收到的HTTP请求包含正确的Referer头
                                                        _referer_for_goto = referer or ''
                                                        try:
                                                            _hard_timeout_goto(
                                                                page, url, timeout=30,
                                                                wait_until="domcontentloaded",
                                                                referer=_referer_for_goto,
                                                            )
                                                        except Exception:
                                                            pass
                                                        _safe_page_wait(page, min_wait=1.5, max_wait=3.0, ad_wait=True)
                                                        # ★ 注入document.referrer覆写（让页面JS/广告脚本也能读到正确的referrer）
                                                        if _referer_for_goto:
                                                            try:
                                                                page.evaluate("""(ref) => {
                                                                    try {
                                                                        Object.defineProperty(document, 'referrer', {get: () => ref, configurable: true});
                                                                    } catch(e) {}
                                                                }""", _referer_for_goto)
                                                            except Exception:
                                                                pass
                                                except Exception:
                                                    pass
                                                return True
                                            except Exception as e:
                                                log.warning(f"第{attempt+1}次访问失败: {e}")
                                                if attempt < max_retries - 1:
                                                    time.sleep(2)
                                        return False
                                    
                                    # ★ 深层URL策略：70%概率从深层页面开始
                                    _actual_target = target_url
                                    _web_nav_cfg = config.get("web_navigation", {})
                                    _layer1_fallbacks = _web_nav_cfg.get("layer_1", {}).get("fallback_urls", [])
                                    _NO_AD_PATHS = ['/about', '/contact', '/privacy', '/refund', '/dmca', '/faq', '/terms', '/tos', '/cookie', '/sitemap', '/login', '/register', '/account']
                                    _deep_urls = [
                                        u for u in _layer1_fallbacks
                                        if u and u.strip()
                                        and u.strip().rstrip('/') != target_url.rstrip('/')
                                        and not any(p in u.lower() for p in _NO_AD_PATHS)
                                    ]
                                    if _deep_urls and random.random() < 0.70:
                                        _actual_target = random.choice(_deep_urls)
                                        log.info(f"📄 深层URL策略：跳转内容页 {_actual_target[:60]}...")
                                    else:
                                        log.info(f"📄 从首页开始浏览: {target_url}")
                                    
                                    # ★ 自然跳转（从来源页通过JS导航，document.referrer自动正确）
                                    simulate_rtt_jitter(base_ms=100, jitter_ms=60)  # ★ RTT仿真：模拟真实网络延迟
                                    if not optimized_page_goto(page, _actual_target, referer=_referer_url):
                                        log.error(f"页面访问多次失败，任务终止")
                                        return False
                                        
                                    # 给页面 JavaScript 渲染 1.5-3 秒时间
                                    time.sleep(random.uniform(1.5, 3.0))
                                    _ok, _bl, _u = _detect(page)
                                    # ★ 根因修复：验证检测到的URL是否是目标站（防止Referer页面被误判为目标页加载成功）
                                    if _ok:
                                        try:
                                            from urllib.parse import urlparse as _urlparse2
                                            _target_host2 = _urlparse2(_actual_target).hostname or ''
                                            _detected_host2 = _urlparse2(str(_u or '')).hostname or ''
                                            if _target_host2 and _detected_host2 and _target_host2 not in _detected_host2 and _detected_host2 not in _target_host2:
                                                log.warning(f"⚠️ [URL域名验证] 页面未跳转到目标站！检测URL={_detected_host2}，目标={_target_host2}，标记为失败")
                                                _ok = False
                                                _home_page_reason = f"URL域名不匹配（当前={_detected_host2}，目标={_target_host2}）"
                                        except Exception:
                                            pass
                                    if _ok:
                                        home_load_success = True
                                        _home_page_reason = f"goto成功，body≈{_bl}字符"
                                        log.info(f"✅ 首页访问成功，页面已响应（URL={str(_u)[:80]}，body≈{_bl}字符）")
                                        # ★ 即时广告检测：页面加载后立即检查是否含广告代码（提前诊断）
                                        try:
                                            _early_ad = page.evaluate("""
                                                () => {
                                                    // Google AdSense / GAM
                                                    if (document.querySelector('ins.adsbygoogle,script[src*="adsbygoogle"],[data-ad-client]')) return 'AdSense';
                                                    if (document.querySelector('script[src*="googlesyndication"],script[src*="pagead2"],iframe[src*="googlesyndication"]')) return 'AdSense/GAM';
                                                    if (document.querySelector('script[src*="securepubads"],script[src*="googletagservices"]')) return 'GAM';
                                                    // HilltopAds
                                                    if (document.querySelector('script[src*="hilltopads"],iframe[src*="hilltopads"],[id*="hilltopads"]')) return 'HilltopAds';
                                                    // EvaDav
                                                    if (document.querySelector('script[src*="evadav"],iframe[src*="evadav"]')) return 'EvaDav';
                                                    // HilltopAds/EvaDav 投放域名
                                                    if (document.querySelector('script[src*="curoax"],iframe[src*="curoax"],script[src*="pufted"],iframe[src*="pufted"],iframe[src*="bony-teaching"],script[src*="bony-teaching"],script[src*="untimely-hello"],iframe[src*="untimely-hello"]')) return 'HilltopAds/EvaDav';
                                                    // NativeAds
                                                    if (document.querySelector('[class*="nativeads"]')) return 'NativeAds';
                                                    // PropellerAds
                                                    if (document.querySelector('script[src*="propellerads"],iframe[src*="propellerads"]')) return 'PropellerAds';
                                                    // MGID
                                                    if (document.querySelector('script[src*="mgid"],iframe[src*="mgid"]')) return 'MGID';
                                                    // Taboola / Outbrain
                                                    if (document.querySelector('script[src*="taboola"],iframe[src*="taboola"]')) return 'Taboola';
                                                    if (document.querySelector('script[src*="outbrain"],iframe[src*="outbrain"]')) return 'Outbrain';
                                                    // Ezoic / Mediavine / AdThrive / Raptive
                                                    if (document.querySelector('script[src*="ezoic"],script[src*="ezoicnet"],[id*="ezoic"]')) return 'Ezoic';
                                                    if (document.querySelector('script[src*="mediavine"],script[data-cfasync*="mediavine"],[class*="mediavine"]')) return 'Mediavine';
                                                    if (document.querySelector('script[src*="adthrive"],script[src*="raptive"]')) return 'AdThrive/Raptive';
                                                    // Monumetric / Bloomreach
                                                    if (document.querySelector('script[src*="monumetric"],script[src*="broadstreet"]')) return 'Monumetric';
                                                    // BuySellAds / Carbon
                                                    if (document.querySelector('script[src*="buysellads"],script[src*="carbonads"]')) return 'BuySellAds';
                                                    // Infolinks / Adsterra
                                                    if (document.querySelector('script[src*="infolinks"],script[src*="adsterra"]')) return 'Infolinks/Adsterra';
                                                    // 通用广告特征
                                                    if (document.querySelector('iframe[width="728"][height="90"],iframe[width="300"][height="250"],iframe[width="160"][height="600"]')) return 'Banner';
                                                    if (document.querySelector('[data-zone],[data-adzone],[data-ad-id],[data-adunit]')) return 'Generic';
                                                    if (document.querySelector('iframe[src*="/ads/"],iframe[src*="/adserve/"],iframe[src*="/adserver/"]')) return 'Generic';
                                                    if (document.querySelector('[class*="ad-container"],[class*="ad-wrapper"],[class*="ad-unit"],[id*="ad-container"],[id*="ad-wrapper"]')) return 'Generic';
                                                    return false;
                                                }
                                            """)
                                            if _early_ad:
                                                log.info(f"🎯 页面含广告代码 [{_early_ad}]，本次访问将产生有效曝光")
                                            else:
                                                # ★ 延迟二次检测：滚动页面后再检测（捕获懒加载广告）
                                                try:
                                                    page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
                                                    time.sleep(random.uniform(2.0, 3.5))
                                                    _early_ad_retry = page.evaluate("""
                                                        () => {
                                                            if (document.querySelector('ins.adsbygoogle,script[src*="adsbygoogle"],[data-ad-client],script[src*="googlesyndication"],script[src*="securepubads"]')) return 'AdSense/GAM';
                                                            if (document.querySelector('script[src*="hilltopads"],iframe[src*="hilltopads"]')) return 'HilltopAds';
                                                            if (document.querySelector('script[src*="evadav"],iframe[src*="evadav"]')) return 'EvaDav';
                                                            if (document.querySelector('script[src*="propellerads"],iframe[src*="propellerads"]')) return 'PropellerAds';
                                                            if (document.querySelector('script[src*="mgid"],iframe[src*="mgid"]')) return 'MGID';
                                                            if (document.querySelector('script[src*="taboola"],script[src*="outbrain"]')) return 'Taboola/Outbrain';
                                                            if (document.querySelector('script[src*="ezoic"],script[src*="mediavine"],script[src*="adthrive"]')) return 'Ezoic/Mediavine/AdThrive';
                                                            if (document.querySelector('[data-zone],[data-adzone],[data-ad-id]')) return 'Generic';
                                                            if (document.querySelector('iframe[width="728"][height="90"],iframe[width="300"][height="250"]')) return 'Banner';
                                                            if (document.querySelector('[class*="ad-container"],[class*="ad-wrapper"],[id*="ad-container"]')) return 'Generic';
                                                            return false;
                                                        }
                                                    """)
                                                    if _early_ad_retry:
                                                        _early_ad = _early_ad_retry
                                                        log.info(f"🎯 滚动后检测到广告代码 [{_early_ad}]（懒加载广告）")
                                                except Exception:
                                                    pass
                                                if not _early_ad:
                                                    # ★ 审计修复：DOM CSS选择器检测失败时，回退到原始HTML文本检测
                                                    try:
                                                        _page_html = page.evaluate("() => document.documentElement.outerHTML") or ''
                                                        if _page_html:
                                                            _html_ad = _html_has_ad_code(_page_html)
                                                            if _html_ad:
                                                                _early_ad = _html_ad
                                                                log.info(f"🎯 [HTML回退检测] 页面含广告代码 [{_html_ad}]（DOM选择器未匹配，原始HTML检测到）")
                                                    except Exception:
                                                        pass
                                                if not _early_ad:
                                                    # ★ 诊断：输出页面所有外部script/iframe来源
                                                    try:
                                                        _diag_sources = page.evaluate("""
                                                            () => {
                                                                const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src).filter(s => s && !s.includes('chrome-extension'));
                                                                const iframes = Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src).filter(s => s && s !== 'about:blank');
                                                                return {scripts: scripts.slice(0, 15), iframes: iframes.slice(0, 10)};
                                                            }
                                                        """)
                                                        _diag_s = _diag_sources.get('scripts', []) if _diag_sources else []
                                                        _diag_f = _diag_sources.get('iframes', []) if _diag_sources else []
                                                        log.info(f"⚠️ 页面无广告代码，本次访问不会产生广告展示（建议将兖底链接配置为含广告的页面）")
                                                        if _diag_s or _diag_f:
                                                            log.info(f"🔍 [广告诊断] 页面外部脚本({len(_diag_s)}): {_diag_s[:8]}")
                                                            if _diag_f:
                                                                log.info(f"🔍 [广告诊断] 页面iframe({len(_diag_f)}): {_diag_f[:5]}")
                                                        else:
                                                            try:
                                                                _html_len = page.evaluate("() => document.documentElement.outerHTML.length") or 0
                                                                _page_title = page.evaluate("() => document.title || ''") or ''
                                                                log.info(f"🔍 [广告诊断] 页面无任何外部脚本/iframe | HTML原始长度={_html_len} | title='{_page_title[:60]}' | 可能原因: SPA未渲染/CF拦截/广告被屏蔽")
                                                            except Exception:
                                                                log.info(f"🔍 [广告诊断] 页面无任何外部脚本/iframe（可能为纯SPA或广告被屏蔽）")
                                                    except Exception:
                                                        log.info(f"⚠️ 页面无广告代码，本次访问不会产生广告展示（建议将兖底链接配置为含广告的页面）")
                                        except Exception:
                                            pass
                                        break
                                    # ★ 根因修复：URL域名不匹配时，不允许“二次检测”误判为成功
                                    # 防止Referer页面（Reddit/Instagram等）被误认为目标站加载成功
                                    if _home_page_reason and 'URL域名不匹配' in _home_page_reason:
                                        log.warning(f"⚠️ [导航失败] 当前页面非目标站（{_home_page_reason}），重试...")
                                        if retry < 2:
                                            _wait = _retry_wait_list[retry]
                                            time.sleep(_wait)
                                        continue
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

                        # ========== ★ 广告主动加载阶段：全页滚动触发懒加载广告 + 等待广告脚本自然执行 ==========
                        # 合规原则：绝不人为干预广告脚本执行（重执行/注入/修改DOM = IVT无效流量）
                        # 仅通过自然用户行为（滚动/停留/等待）让广告脚本自行完成加载
                        if home_load_success:
                            try:
                                # ★ 合规策略：给广告脚本充足的自然执行时间
                                # 广告脚本通常在DOMContentLoaded后异步加载，需要3-8秒完成广告请求+渲染
                                # 这里不做任何DOM操作，仅等待广告脚本自然完成
                                log.info("⏳ [广告等待] 给广告脚本 5-8 秒自然执行时间（不干预DOM）...")
                                time.sleep(random.uniform(5.0, 8.0))
                                
                                _page_height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 800
                                _viewport_h = page.evaluate("() => window.innerHeight") or 700
                                _ad_scroll_step = max(300, int(_viewport_h * 0.7))
                                _ad_scroll_pos = 0
                                # 分段滚动全页（合规：真人也会滚动浏览页面内容）
                                while _ad_scroll_pos < _page_height:
                                    _ad_scroll_pos += _ad_scroll_step
                                    page.evaluate(f"window.scrollTo(0, {_ad_scroll_pos})")
                                    time.sleep(random.uniform(1.5, 2.5))
                                # 滚回顶部（广告通常在页面中上部）
                                page.evaluate("window.scrollTo(0, 0)")
                                time.sleep(random.uniform(2.0, 3.0))
                                # 重新扫描广告（只读检测，不修改DOM）
                                ad_monitor = scan_ads_during_task(page, ad_monitor, "广告主动加载-全页滚动后")
                                _ad_containers_after_scroll = len(ad_monitor.get('containers', set()))
                                if _ad_containers_after_scroll > 0:
                                    log.info(f"✅ [广告主动加载] 全页滚动后检测到 {_ad_containers_after_scroll} 个广告容器")
                                else:
                                    log.info(f"⚠️ [广告主动加载] 全页滚动后仍未检测到广告DOM容器（广告脚本可能未执行或被阻断）")
                                # ★ 广告点击：首页全页滚动后尝试点击
                                _ad_clicked, current_x, current_y = try_click_visible_ad(page, config, current_x, current_y, stage="首页-广告加载后")
                            except Exception as _ad_load_e:
                                log.warning(f"⚠️ [广告主动加载] 滚动触发异常: {str(_ad_load_e)[:80]}")

                        # ========== 任务总时长保险绳（防止任务无限延长） ==========
                        # ★ 对数正态采样（与前置计算一致，不重复随机）
                        if 'task_deadline' not in dir() or task_deadline is None:
                            _sd_cfg2 = config.get("session_duration", {})
                            _sd_cap2 = float(_sd_cfg2.get("hard_cap_sec", 600))
                            task_deadline = enter_site_time + _session_secs if '_session_secs' in dir() else enter_site_time + _sd_cap2
                        else:
                            # ★ 修复：deadline 锚点可能设在前置流程（来源页浏览/搜索导航）之前，
                            # 前置流程耗时几十秒会把预算吃光，导致进站时 deadline 已过期、
                            # 浏览循环被保险绳立即截断 → 0点击/0停留 → 广告从未被真实浏览 → 广告收入为0。
                            # 若剩余预算 < 配置的最小浏览时长，则重新锚定到首页加载完成时刻、重发完整预算
                            _cfg_stay_min_rope2 = float(config.get("total_stay", {}).get("min", 80))
                            _sd_cfg3 = config.get("session_duration", {})
                            _sd_cap3 = float(_sd_cfg3.get("hard_cap_sec", 600))
                            _rope_left = task_deadline - time.time()
                            if _rope_left < _cfg_stay_min_rope2:
                                _new_budget = min(_sd_cap3, _session_secs if '_session_secs' in dir() else max(_cfg_stay_min_rope2, 120))
                                log.warning(
                                    f"⏱️ [保险绳重锚] 旧deadline已过期/不足（剩余={max(0, _rope_left):.1f}s < 最小浏览{_cfg_stay_min_rope2:.0f}s，"
                                    f"被前置流程消耗），重新锚定到首页加载完成时刻，新预算={_new_budget:.0f}s"
                                )
                                task_deadline = time.time() + _new_budget
                        
                        def _check_rope(stage_desc=""):
                            if not task_running:
                                raise RuntimeError("任务已停止")
                            if time.time() >= task_deadline:
                                raise RuntimeError(f"任务超时（已运行 {time.time() - enter_site_time:.1f}秒）")

                        # ★ HilltopAds Pop-under 弹窗触发辅助
                        def _try_hilltopads_popunder(_page, _context, _cfg):
                            """在积累页面交互后，通过 CDP 层可信手势触发 Pop-under 弹窗"""
                            if not _HAS_POPUNDER:
                                return False, None
                            _ht_cfg = _cfg.get("hilltopads", {})
                            if not _ht_cfg.get("enabled", False):
                                return False, None
                            try:
                                # ★ P0-3：传入 IP 信息，机房/代理IP 直接拒绝，避免浪费代理费
                                _ip_info = resolved_ip_info if 'resolved_ip_info' in dir() else None
                                _ok, _pop, _diag = _popunder.trigger_popunder(
                                    _page, _context, config=_ht_cfg,
                                    resolved_ip_info=_ip_info,
                                )
                                if _ok:
                                    log.info(
                                        f"[HilltopAds] Pop-under 弹窗触发成功: "
                                        f"url={_diag.get('url','')[:80]} "
                                        f"stay={_diag.get('stay_actual',0)}s"
                                    )
                                elif _diag.get("reason") not in ("probability_skip", "cooldown"):
                                    log.debug(
                                        f"[HilltopAds] Pop-under 跳过: {_diag.get('reason','')}"
                                    )
                                return _ok, _diag
                            except Exception as _ht_e:
                                log.debug(f"[HilltopAds] 弹窗触发异常(忽略): {_ht_e}")
                                return False, None

                        current_x, current_y = 100, 100

                        # ========== ★ 跳出率模拟 + 网页浏览模式循环 ==========
                        _bounce_cfg = config.get("bounce_rate", {"min": 0.20, "max": 0.35})
                        _bounce_prob = random.uniform(float(_bounce_cfg.get("min", 0.20)), float(_bounce_cfg.get("max", 0.35)))
                        _is_bounce = random.random() < _bounce_prob
                        if _is_bounce:
                            log.info(f"🚪 本次任务为跳出型(概率{_bounce_prob:.0%})：仅停留首页后离开")
                            # ★ 修复：跳出型停留时间必须尊重配置的浏览时长下限
                            # 跳出 = 只浏览首页就离开，但停留时间仍需≥配置最小值的70%
                            _cfg_stay_min = float(config.get("total_stay", {}).get("min", 80))
                            _cfg_stay_max = float(config.get("total_stay", {}).get("max", 220))
                            _bounce_floor = max(30, _cfg_stay_min)  # ★ 跳出停留不低于配置最小值
                            _bounce_ceil = min(_cfg_stay_max, (_cfg_stay_min + _cfg_stay_max) / 2)  # 上限取配置中位数
                            if _bounce_ceil <= _bounce_floor:
                                _bounce_ceil = _bounce_floor + 20
                            _bounce_stay = random.uniform(_bounce_floor, _bounce_ceil)
                            log.info(f"🚪 跳出停留: {_bounce_stay:.1f}s (范围{_bounce_floor:.0f}~{_bounce_ceil:.0f}s, 配置min={_cfg_stay_min:.0f}s)")
                            # ========== ★ 停留日志：跳出型分支 ==========
                            if _bounce_stay < _BROWSE_DURATION_CRITICAL_S:
                                log.error(
                                    f"⏱️ [停留-03/红线] 跳出型任务停留={_bounce_stay:.1f}s < 红线{_BROWSE_DURATION_CRITICAL_S:.0f}s，"
                                    f"广告脚本不可能完成 init+request+render，必然 0 收益。请把 config.bounce_rate.max 调低至≤0.15，"
                                    f"并把 config.total_stay.min 至少调到 {int(_BROWSE_DURATION_WARN_S)+10}s"
                                )
                            elif _bounce_stay < _BROWSE_DURATION_WARN_S:
                                log.warning(
                                    f"⏱️ [停留-03/警告] 跳出型任务停留={_bounce_stay:.1f}s < 建议值{_BROWSE_DURATION_WARN_S:.0f}s，"
                                    f"广告填充但 ActiveView 可能不计数，收益折损。建议把 bounce_rate.max 调低≤0.20"
                                )
                            else:
                                log.info(
                                    f"⏱️ [停留-03] 跳出型任务停留={_bounce_stay:.1f}s ≥ 建议值{_BROWSE_DURATION_WARN_S:.0f}s（仍属于高跳出率，长期会降低质量分）"
                                )
                            current_x, current_y = simulate_human_in_window(
                                page, _bounce_stay, page_behavior_stats, current_x, current_y,
                                config, page_name=f"[T{task_idx+1}] 首页(跳出)", deadline=task_deadline
                            )
                            ad_monitor = scan_ads_during_task(page, ad_monitor, "跳出型任务首页停留后")
                            # ★ HilltopAds Pop-under：首页浏览后触发弹窗
                            _try_hilltopads_popunder(page, context, config)
                            log.info(f"🚪 跳出型任务完成：首页停留{_bounce_stay:.0f}s后离开")
                        if not _is_bounce:
                            log.info(f"🔄 网页浏览模式循环次数: {chapter_loop_count}次（每轮走完 L1→L{len(layers)}，任务总时长=各轮之和）")
                        if not _is_bounce:
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
                                # ========== ★ 停留日志：L1-Ln 每层停留预算与红线对比 ==========
                                _t_before_layers = time.time()
                                if home_stay < 20:
                                    log.warning(
                                        f"⏱️ [停留-04/L{1}] 第{loop_idx+1}轮 L1首页停留预算={home_stay:.1f}s < 20s，"
                                        f"内容+广告未进入视野就下翻，易被判定跳转垃圾流量（≥20s/页才被GA4记为有效页面）"
                                    )
                                current_x, current_y = simulate_human_in_window(
                                    page, home_stay, page_behavior_stats, current_x or 100, current_y or 100,
                                    config, page_name=f"[T{task_idx+1}] 首页", deadline=task_deadline
                                )
                                log.info(
                                    f"⏱️ [停留-04/L{1}] 第{loop_idx+1}轮 L1首页实际运行: simulate_human_in_window 返回，"
                                    f"deadline剩余={max(0, task_deadline - time.time()):.1f}s"
                                )
                                ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮首页停留后")
                                # ★ HilltopAds Pop-under：仅第一轮 L1 首页后触发（避免重复弹窗）
                                if loop_idx == 0:
                                    _try_hilltopads_popunder(page, context, config)

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
                                    if list_stay < 20:
                                        log.warning(
                                            f"⏱️ [停留-04/L2] 第{loop_idx+1}轮 L2列表页停留预算={list_stay:.1f}s < 20s，"
                                            f"GA4会把<15s页记为bounce page session，降低广告质量分"
                                        )
                                    current_x, current_y = simulate_human_in_window(
                                        page, list_stay, page_behavior_stats,
                                        current_x or 300, current_y or 300,
                                        config, page_name="列表页", deadline=task_deadline
                                    )
                                    log.info(
                                        f"⏱️ [停留-04/L2] 第{loop_idx+1}轮 L2列表页实际运行返回，"
                                        f"deadline剩余={max(0, task_deadline - time.time()):.1f}s"
                                    )
                                    ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮列表页停留后")
                                    # ★ 广告点击：列表页检测到广告后按概率点击
                                    _ad_clicked, current_x, current_y = try_click_visible_ad(page, config, current_x, current_y, stage=f"列表页L2-第{loop_idx+1}轮")
                                else:
                                    log.warning(f"⚠️ 第{loop_idx+1}轮进入列表页失败，本轮跳过深层")

                                # 尝试更深层浏览：layer_3 → layer_4 → layer_5
                                _broken = False
                                for level_idx in range(2, len(layers)):
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
                                    if stay < 25:
                                        log.warning(
                                            f"⏱️ [停留-04/L{level_idx+1}] 第{loop_idx+1}轮 内容页(L{level_idx+1})停留预算={stay:.1f}s < 25s，"
                                            f"内容页正常阅读需30~90s，过短会被GA4/Ads模型判定为诱导跳转或爬虫"
                                        )
                                    _ts_before_deep = time.time()
                                    current_x, current_y = simulate_human_in_window(
                                        page, stay, page_behavior_stats,
                                        current_x or 300, current_y or 300,
                                        config, page_name=f"layer_{level_idx+1}", deadline=task_deadline
                                    )
                                    log.info(
                                        f"⏱️ [停留-04/L{level_idx+1}] 第{loop_idx+1}轮 L{level_idx+1}内容页实际返回，"
                                        f"预算={stay:.1f}s，真实耗时≈{time.time()-_ts_before_deep:.1f}s，"
                                        f"deadline剩余={max(0, task_deadline - time.time()):.1f}s"
                                    )
                                    ad_monitor = scan_ads_during_task(page, ad_monitor, f"第{loop_idx+1}轮layer_{level_idx+1}停留后")
                                    # ★ 广告点击：深层页面检测到广告后按概率点击
                                    _ad_clicked, current_x, current_y = try_click_visible_ad(page, config, current_x, current_y, stage=f"layer{level_idx+1}-第{loop_idx+1}轮")

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
                                    _ts_before_interval = time.time()
                                    current_x, current_y = simulate_human_in_window(
                                        page, loop_interval, page_behavior_stats,
                                        current_x or 100, current_y or 100,
                                        config, page_name=f"[轮间间隔{loop_idx+1}]", deadline=task_deadline
                                    )
                                    log.info(
                                        f"⏱️ [停留-05/轮间] 第{loop_idx+1}轮→{loop_idx+2}轮间隔返回，"
                                        f"间隔预算={loop_interval:.1f}s，真实耗时≈{time.time()-_ts_before_interval:.1f}s，"
                                        f"deadline剩余={max(0, task_deadline - time.time()):.1f}s"
                                    )
    
                            # ========== 网页浏览模式结束 → 全程真人行为统计 ==========
                            _sum_stats_stay_ms = page_behavior_stats.get("total_stay", 0)
                            _sum_stats_stay_s = float(_sum_stats_stay_ms) / 1000.0 if isinstance(_sum_stats_stay_ms, (int, float)) else 0.0
                            _actual_elapsed_s = float(time.time() - _t_before_layers) if '_t_before_layers' in dir() else 0.0
                            log.info(
                                f"⏱️ [停留-06/全程] 全流程L1→Ln实际运行≈{_actual_elapsed_s:.1f}s，"
                                f"behavior_stats.total_stay(行为累加)={_sum_stats_stay_s:.1f}s，"
                                f"deadline剩余={max(0, task_deadline - time.time()):.1f}s"
                            )
                            if _actual_elapsed_s > 0 and _actual_elapsed_s < _BROWSE_DURATION_CRITICAL_S:
                                log.error(
                                    f"⏱️ [停留-06/全程红线] 全流程运行仅{_actual_elapsed_s:.1f}s < 红线{_BROWSE_DURATION_CRITICAL_S:.0f}s，"
                                    f"请检查：① _check_rope 是否提前抛RuntimeError ② 每层 stay_ratio 总和是否太小 ③ total_stay.min 是否过短"
                                )
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
                        
                        # ★ 诊断：检查页面是否包含广告代码（支持所有主流联盟，不仅限AdSense）
                        _has_ad_code = False
                        _detected_network = '无'
                        try:
                            _ad_check_result = page.evaluate("""
                                () => {
                                    // Google AdSense / GAM
                                    if (document.querySelector('script[src*="adsbygoogle"],ins.adsbygoogle,[data-ad-client]')) return 'AdSense';
                                    if (document.querySelector('script[src*="googlesyndication"],script[src*="pagead2"],iframe[src*="googlesyndication"]')) return 'AdSense/GAM';
                                    if (document.querySelector('script[src*="securepubads"],script[src*="googletagservices"]')) return 'GAM';
                                    // HilltopAds
                                    if (document.querySelector('script[src*="hilltopads"],iframe[src*="hilltopads"],[id*="hilltopads"]')) return 'HilltopAds';
                                    // EvaDav
                                    if (document.querySelector('script[src*="evadav"],iframe[src*="evadav"]')) return 'EvaDav';
                                    // HilltopAds/EvaDav 投放域名（随机域名）
                                    if (document.querySelector('script[src*="curoax"],iframe[src*="curoax"],script[src*="pufted"],iframe[src*="pufted"],iframe[src*="bony-teaching"],script[src*="bony-teaching"],script[src*="untimely-hello"],iframe[src*="untimely-hello"]')) return 'HilltopAds/EvaDav';
                                    // NativeAds 容器
                                    if (document.querySelector('[class*="nativeads"]')) return 'NativeAds';
                                    // 标准广告尺寸 iframe
                                    if (document.querySelector('iframe[width="728"][height="90"],iframe[width="300"][height="250"],iframe[width="160"][height="600"]')) return 'Banner';
                                    // PropellerAds
                                    if (document.querySelector('script[src*="propellerads"],iframe[src*="propellerads"]')) return 'PropellerAds';
                                    // MGID
                                    if (document.querySelector('script[src*="mgid"],iframe[src*="mgid"]')) return 'MGID';
                                    // Taboola / Outbrain
                                    if (document.querySelector('script[src*="taboola"],iframe[src*="taboola"]')) return 'Taboola';
                                    if (document.querySelector('script[src*="outbrain"],iframe[src*="outbrain"]')) return 'Outbrain';
                                    // Ezoic / Mediavine / AdThrive / Raptive
                                    if (document.querySelector('script[src*="ezoic"],script[src*="ezoicnet"],[id*="ezoic"]')) return 'Ezoic';
                                    if (document.querySelector('script[src*="mediavine"],[class*="mediavine"]')) return 'Mediavine';
                                    if (document.querySelector('script[src*="adthrive"],script[src*="raptive"]')) return 'AdThrive/Raptive';
                                    // Monumetric / Broadstreet
                                    if (document.querySelector('script[src*="monumetric"],script[src*="broadstreet"]')) return 'Monumetric';
                                    // Infolinks / Adsterra
                                    if (document.querySelector('script[src*="infolinks"],script[src*="adsterra"]')) return 'Infolinks/Adsterra';
                                    // BuySellAds / Carbon
                                    if (document.querySelector('script[src*="buysellads"],script[src*="carbonads"]')) return 'BuySellAds';
                                    // 通用广告iframe/script（兜底检测）
                                    if (document.querySelector('iframe[src*="/ads/"],iframe[src*="adserve"],iframe[src*="/adserver/"]')) return 'Generic';
                                    if (document.querySelector('[data-zone],[data-adzone],[data-ad-id],[data-adunit]')) return 'Generic';
                                    if (document.querySelector('[class*="ad-container"],[class*="ad-wrapper"],[class*="ad-unit"],[id*="ad-container"],[id*="ad-wrapper"]')) return 'Generic';
                                    return false;
                                }
                            """)
                            if _ad_check_result:
                                _has_ad_code = True
                                _detected_network = _ad_check_result
                        except Exception:
                            pass
                        # ★ 审计修复：DOM CSS选择器检测失败时，回退到原始HTML文本检测
                        if not _has_ad_code:
                            try:
                                _final_page_html = page.evaluate("() => document.documentElement.outerHTML") or ''
                                if _final_page_html:
                                    _html_ad_final = _html_has_ad_code(_final_page_html)
                                    if _html_ad_final:
                                        _has_ad_code = True
                                        _detected_network = _html_ad_final
                                        log.info(f"🎯 [HTML回退检测] 最终检测发现广告代码 [{_html_ad_final}]（DOM选择器未匹配，原始HTML检测到）")
                            except Exception:
                                pass
                        
                        # ========== ★ 广告恢复机制：HTML检测到广告代码但DOM未渲染时，通过自然滚动等待广告加载 ==========
                        # 合规原则：仅通过自然用户行为（滚动浏览）等待广告脚本自行完成加载
                        # 绝不注入/重执行/修改广告脚本（那会被联盟反作弊系统标记为IVT无效流量）
                        if _has_ad_code and not ad_found:
                            log.warning(f"⚠️ [广告恢复] 页面含{_detected_network}广告代码但DOM未渲染广告容器，通过自然滚动等待广告加载...")
                            try:
                                # 合规策略：模拟真人滚动浏览页面（广告通常在内容区域中间/底部/顶部）
                                page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
                                time.sleep(random.uniform(3.0, 5.0))
                                page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.75)")
                                time.sleep(random.uniform(2.0, 4.0))
                                page.evaluate("window.scrollTo(0, 0)")
                                time.sleep(random.uniform(3.0, 5.0))
                                # 只读扫描广告DOM（不修改任何元素）
                                ad_monitor = scan_ads_during_task(page, ad_monitor, "广告恢复-滚动触发后")
                                ad_found = len(ad_monitor.get('containers', set())) > 0
                                ad_in_viewport = len(ad_monitor.get('visible', set())) > 0
                                ad_loaded = ad_found
                                ad_impressions = len(ad_monitor.get('exposed', set()))
                                ad_refreshes = int(ad_monitor.get('refresh_count', 0) or 0)
                                _eff_exposed = len(ad_monitor.get('effective_exposed', set()))
                                _dur_map = ad_monitor.get('exposure_duration_ms', {}) or {}
                                _total_dur = sum(_dur_map.values())
                                _max_dur = max(_dur_map.values()) if _dur_map else 0
                                if ad_found:
                                    log.info(f"✅ [广告恢复] 成功！滚动触发后检测到 {len(ad_monitor.get('containers', set()))} 个广告容器，曝光={ad_impressions}")
                                else:
                                    log.warning(f"⚠️ [广告恢复] 失败：滚动触发后仍未检测到广告DOM容器。可能原因：广告脚本未执行/广告服务器无填充/代理IP被广告服务器屏蔽")
                            except Exception as _recovery_e:
                                log.warning(f"⚠️ [广告恢复] 异常: {str(_recovery_e)[:80]}")
                        
                        log.info(
                            f"[广告监控汇总] 扫描={ad_monitor.get('scan_count', 0)} "
                            f"容器去重={len(ad_monitor.get('containers', set()))} "
                            f"曾进入视口={len(ad_monitor.get('visible', set()))} "
                            f"曾曝光={ad_impressions} 刷新={ad_refreshes} "
                            f"有效曝光达标(≥50%可见且累计≥{int(config.get('ad_effective_exposure_ms', 1000) or 1000)}ms)={_eff_exposed} "
                            f"累计曝光时长={_total_dur}ms 单广告位最长={_max_dur}ms "
                            f"| Popunder弹窗触发={'是(峰值'+str(int(ad_monitor.get('popunder_max_windows',0)) or 0)+'个窗口)' if int(ad_monitor.get('popunder_max_windows',0) or 0)>0 else '否'} "
                            f"| 页面含广告代码={'是' if _has_ad_code else '否'} 联盟={_detected_network}"
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
                        # ★ 有效流量判定：广告监控检测到曝光 OR 页面确实含有广告代码（联盟会自行计数）
                        traffic_valid = bool(ad_loaded and ad_impressions > 0) or _has_ad_code
                        valid_traffic = traffic_valid
                        success = bool(load_success and consistency)
                        if success and not traffic_valid:
                            if not _has_ad_code:
                                log.info("ℹ️ 任务流程已完成，目标页无广告代码（流量标记为无效，非系统问题）")
                            else:
                                log.warning(f"⚠️ 任务流程已完成，页面含{_detected_network}广告代码但广告未实际渲染（广告脚本未执行/广告服务器无填充/代理IP被屏蔽，流量标记为无效）")
                        
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
                        
                        # ★ 记录IP会话（用于24h频率控制）
                        if exit_ip and exit_ip != "未知":
                            ip_session_manager.record_ip_session(exit_ip)
                        
                        # ⏱️ 时间统计：前置流程时长（取IP→搜索/社媒跳转→进入网站）、浏览网站时长（进入网站→任务结束）
                        _task_end_time = time.time()
                        if enter_site_time is not None:
                            _pre_dur = max(0.0, enter_site_time - dial_start_time)
                            _browse_dur = max(0.0, _task_end_time - enter_site_time)
                        else:
                            # 未成功进入网站（首页一直失败）：前置=全程，浏览=0
                            _pre_dur = max(0.0, _task_end_time - dial_start_time)
                            _browse_dur = 0.0
                        log.info(
                            f"<span style='color:#ff3333;font-weight:bold'>前置流程时长（取IP→搜索/社媒跳转→进入网站）: {_pre_dur:.1f}秒</span>"
                        )
                        log.info(
                            f"<span style='color:#ff3333;font-weight:bold'>浏览网站时长（进入网站→任务结束）: {_browse_dur:.1f}秒</span>"
                        )
                        # P1-2 Profile持久化+回访入队
                        if _HAS_RCE:
                            try:
                                _fp_id = current_task.get('fingerprint_id') or f"{country}|{selected_ua[:32] if 'selected_ua' in dir() else 'unknown'}"
                                _target_host = ''
                                try: _target_host = current_task.get('target_url','') or target_url
                                except: pass
                                _scroll_d = behavior_stats.get('total_scroll_depth',0) if 'behavior_stats' in dir() else 0.0
                                _rce.profile_store.record_visit(
                                    fp_id=_fp_id,
                                    host=_target_host,
                                    dwell_sec=_browse_dur if '_browse_dur' in dir() else 60,
                                    scroll_depth=min(1.0, _scroll_d / 5000) if _scroll_d else 0.3,
                                    clicks=ad_impressions if 'ad_impressions' in dir() else 0,
                                )
                                log.info(f"👤 P1-2 Profile已更新: fp={_fp_id[:20]}...")
                            except Exception as _rce_e:
                                log.debug(f"P1-2 Profile异常(忽略): {_rce_e}")
                        # ========== ★ P2-5(2)：浏览网站时长全局审计 ==========
                        # 低于 CRITICAL：直接判定"没给广告注入时间"，流量必然 0 收益
                        # 低于 WARN 但 ≥CRITICAL：有注入概率但偏低，提示管理员把 total_stay 调大
                        try:
                            if enter_site_time is None:
                                log.error(
                                    f"🚫 P2-5[停留审计] 本任务 enter_site_time=None（未进入目标站），"
                                    f"广告不可能被初始化/渲染/点击，必然 0 收益"
                                )
                            elif _browse_dur < _BROWSE_DURATION_CRITICAL_S:
                                log.error(
                                    f"🚫 P2-5[停留审计] 浏览网站时长={_browse_dur:.1f}s < "
                                    f"红线 {_BROWSE_DURATION_CRITICAL_S:.0f}s，广告脚本大概率"
                                    f"尚未完成 init→request→拍卖→渲染，本任务基本确定 0 收益。"
                                    f"建议把 config.total_stay.min 提高到至少 {int(_BROWSE_DURATION_WARN_S)+10}s"
                                )
                            elif _browse_dur < _BROWSE_DURATION_WARN_S:
                                log.warning(
                                    f"⚠️ P2-5[停留审计] 浏览网站时长={_browse_dur:.1f}s < "
                                    f"建议阈值 {_BROWSE_DURATION_WARN_S:.0f}s，广告有填充但可能没达到"
                                    f"ActiveView(≥50%面积/≥1s) 计数，收益有较大折损。建议把 "
                                    f"config.total_stay.min 提高到至少 {int(_BROWSE_DURATION_WARN_S)+10}s"
                                )
                            else:
                                log.info(
                                    f"✅ P2-5[停留审计] 浏览网站时长={_browse_dur:.1f}s ≥ "
                                    f"{_BROWSE_DURATION_WARN_S:.0f}s 达标，广告有充足时间 "
                                    f"init+request+render+ActiveView 计数"
                                )
                        except Exception as _audit_err:
                            log.debug(f"停留审计日志异常(忽略): {type(_audit_err).__name__}")

                        # P2-5 ICR 无效点击率监控
                        if _HAS_RCE:
                            try:
                                _is_bounce_val = 1.0 if ('_is_bounce' in dir() and _is_bounce) else 0.0
                                _rce.icr_monitor.record(time.time(), _browse_dur if '_browse_dur' in dir() else 0, _is_bounce_val)
                                _warn_icr, _snap_icr, _why_icr = _rce.icr_monitor.should_warn()
                                if _warn_icr:
                                    log.error(f"🚫 P2-5 ICR告警: {_why_icr} | 快照={_snap_icr}")
                            except Exception as _rce_e:
                                log.debug(f"P2-5 ICR异常(忽略): {_rce_e}")

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
                                f"<span style='color:#ff3333;font-weight:bold'>前置流程时长（取IP→搜索/社媒跳转→进入网站）: {_pre_dur:.1f}秒</span>"
                            )
                            log.info(
                                f"<span style='color:#ff3333;font-weight:bold'>浏览网站时长（进入网站→任务结束）: {_browse_dur:.1f}秒</span>"
                            )
                            # ========== ★ P2-5(2)：异常路径也要审计停留时长 ==========
                            try:
                                if _est is None:
                                    log.error(
                                        f"🚫 P2-5[停留审计-异常] enter_site_time=None（未进入目标站），"
                                        f"广告 0 收益"
                                    )
                                elif _browse_dur < _BROWSE_DURATION_CRITICAL_S:
                                    log.error(
                                        f"🚫 P2-5[停留审计-异常] 浏览网站时长={_browse_dur:.1f}s < "
                                        f"红线 {_BROWSE_DURATION_CRITICAL_S:.0f}s，广告未完成渲染"
                                    )
                                elif _browse_dur < _BROWSE_DURATION_WARN_S:
                                    log.warning(
                                        f"⚠️ P2-5[停留审计-异常] 浏览网站时长={_browse_dur:.1f}s < "
                                        f"建议阈值 {_BROWSE_DURATION_WARN_S:.0f}s"
                                    )
                                else:
                                    log.info(
                                        f"✅ P2-5[停留审计-异常] 浏览网站时长={_browse_dur:.1f}s ≥ "
                                        f"{_BROWSE_DURATION_WARN_S:.0f}s 达标"
                                    )
                            except Exception:
                                pass

                        # P2-5 ICR 无效点击率监控（异常路径）
                        if _HAS_RCE:
                            try:
                                _is_bounce_val = 1.0 if ('_is_bounce' in dir() and _is_bounce) else 0.0
                                _bw_dur = _browse_dur if '_browse_dur' in dir() else 0
                                _rce.icr_monitor.record(time.time(), _bw_dur, _is_bounce_val)
                                _warn_icr, _snap_icr, _why_icr = _rce.icr_monitor.should_warn()
                                if _warn_icr:
                                    log.error(f"🚫 P2-5 ICR告警(异常路径): {_why_icr} | 快照={_snap_icr}")
                            except Exception as _rce_e:
                                log.debug(f"P2-5 ICR异常(忽略): {_rce_e}")

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
                        # ★ P2-9: 保存跨会话行为画像
                        try:
                            if _behavior_profile is not None and 'page_behavior_stats' in locals():
                                _bp_save_url = target_url or config.get("target_url", "")
                                _bp_save_country = (country or "US").upper()
                                save_behavior_profile(_bp_save_url, _bp_save_country, page_behavior_stats, _behavior_profile)
                                log.debug(f"🧠 [行为画像] 已更新(visit_count={_behavior_profile.get('visit_count', 0)})")
                        except Exception as _bp_save_err:
                            log.debug(f"[行为画像] 保存失败(忽略): {str(_bp_save_err)[:60]}")
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
    
                        # ★ 停止本地代理转发器
                        try:
                            _stop_proxy_relay()
                            log.debug("🧹 已停止本地代理转发器")
                        except Exception:
                            pass

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
                        # ========== ★ P2-5：本任务正常/异常结束后，取消 suicide watchdog ==========
                        _cancel_task_global_watchdog()
            except Exception as outer_e:
                log.error(f"外层任务异常: {str(outer_e)}")
                import traceback
                log.debug(f"外层异常详情: {traceback.format_exc()}")
                # 外层异常也要取消 watchdog，避免误自杀
                try:
                    _cancel_task_global_watchdog()
                except Exception:
                    pass
            

    
    # 将任务计划添加到历史记录
    add_to_historical_tasks(daily_plan)
    record_kpi_snapshot()
    
    # ★ 断点恢复：判断是全部完成还是中途停止
    _all_done = all(t.get("status") == "已完成" for t in tasks_list) if tasks_list else True
    if _all_done:
        _clear_plan_progress()
        log.info("📋 计划全部完成，已清除进度文件")
    else:
        _save_plan_progress(daily_plan, tasks_list)
        _done = sum(1 for t in tasks_list if t.get("status") == "已完成")
        log.info(f"📋 计划未完成({_done}/{len(tasks_list)})，进度已保存，下次执行将从断点恢复")
    
    task_running = False
    _single_task_mode = False
    if adsl_ip_task:
        adsl_status["running"] = False
        adsl_status["status"] = "已停止" if adsl_status.get("completed", 0) < adsl_status.get("total", 0) else "完成"
    current_task_idx = -1
    _last_executed_plan = current_plan  # 保留计划数据供预览查看
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
    from flask import make_response
    resp = make_response(render_template_string(HTML_TEMPLATE, config=config, logs=list(reversed(log.messages[-500:])), 
                                  statstotal=stats['total'], statssuccess=stats['success'], 
                                  statsfail=stats['fail'],
                                  stats=stats, runningtask=task_running,
                                  planned_total=planned_total_tasks, APP_VERSION=APP_VERSION,
                                  VPS_HOST=os.environ.get("VPS_HOST", "")))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp




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
        # 兜底：直接从 stats 中读，避免引用未定义函数（UI 不崩）
        # （get_total_video_views / get_country_video_views 若未来上线，再替换这里）
        _cvv = stats.get("country_video_views", {}) or {}
        if not isinstance(_cvv, dict):
            _cvv = {}
        _total_views = 0
        try:
            _total_views = sum(int(v) for v in _cvv.values() if isinstance(v, (int, float)))
        except Exception:
            _total_views = 0
        video_stats = {
            "total_views": _total_views,
            "country_views": dict(_cvv),
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
    data = request.get_json(silent=True) or {}  # ★ 审计修复#4：防止None崩溃
    
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
    # ★ 审计修复#14：过滤双下划线开头的危险键，防止配置注入
    config.update({k: v for k, v in data.items() if k not in [
        'site_creation_date', 'plan_days', 'adsl_task_count', 'vt_adsl_task_count',
        'session_mode', 'ua_repeat_max_rate', 'selected_models',
        'daily_traffic_range', 'proxy_pool', 'video_ad_enabled_only',
        'web_navigation'
    ] and not k.startswith('__')})
    
    # ★ 审计修复#5：指定encoding防止非UTF-8环境崩溃
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
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
        # 在日志窗口展示待执行计划预览（待执行状态，供用户确认后执行）
        log.info(_format_plan_log_block(
            pending_plan, "📋 <b>待执行计划预览</b>（确认无误后点击“执行计划”开始执行）"
        ))
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
    global pending_plan, current_plan, _last_executed_plan
    # 优先返回待执行计划，其次当前执行中的计划，最后保留的历史计划（供预览查看）
    plan_data = pending_plan if pending_plan is not None else (current_plan if current_plan is not None else _last_executed_plan)
    # 内存变量全为空时，从磁盘文件加载（服务重启后内存丢失）
    if plan_data is None:
        try:
            import json as _json
            if os.path.exists(PLAN_PROGRESS_FILE):
                with open(PLAN_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                    _disk_data = _json.load(f)
                plan_data = _disk_data.get("plan")
                if plan_data:
                    _last_executed_plan = plan_data  # 缓存到内存
        except Exception:
            pass
    return jsonify({
        "plan": plan_data
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
_task_start_lock = threading.Lock()  # ★ 审计修复#2：防止并发启动竞态

@app.route('/start_task', methods=['POST'])
def start_task():
    global task_running
    with _task_start_lock:
        if task_running:
            return jsonify({"status": "error", "message": "已有任务正在运行"}), 409
        task_running = True
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
        headless = bool(body.get("headless", False))  # ★ 默认有头模式（headless会被风控检测）
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
_prodtest_last_update = time.time()  # ★ 看门狗：记录最后一次进度更新时间
_PRODTEST_STUCK_TIMEOUT = 180  # ★ 超过180秒无进度更新则判定卡死


def _run_production_test_thread(layers, headless, target_url):
    global _prodtest_state, _prodtest_last_update
    try:
        import production_test

        def _log_fn(msg):
            log.info(f"[生产准入] {msg}")
            # ★ 审计修复#7：加锁保护并发读写
            with _prodtest_lock:
                _prodtest_state["logs"].append(str(msg))
                if len(_prodtest_state["logs"]) > 300:
                    _prodtest_state["logs"] = _prodtest_state["logs"][-300:]

        def _progress_fn(pct, stage):
            global _prodtest_last_update
            _prodtest_state["progress"] = int(pct)
            _prodtest_state["stage"] = stage
            _prodtest_last_update = time.time()  # ★ 看门狗更新

        result = production_test.run_production_test(
            layers=layers, progress=_progress_fn, log=_log_fn,
            config=config, target_url=target_url, headless=headless,
        )
        # 累积合并：将新层结果合入已有层（单层按钮不覆盖其他层）
        new_layers = result.get("layers", {})
        _prodtest_state["layers"].update(new_layers)
        # 从所有累积层重建准入检查单
        _prodtest_state["gate"] = production_test.build_gate_report(_prodtest_state["layers"])
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
    global _prodtest_state, _prodtest_last_update
    with _prodtest_lock:
        if _prodtest_state["running"]:
            # ★ 看门狗容错：如果running但已超时，允许重新启动
            if time.time() - _prodtest_last_update > _PRODTEST_STUCK_TIMEOUT:
                log.warning("[生产准入] 检测到卡死状态，允许重新启动")
                _prodtest_state["running"] = False
            else:
                return jsonify({"status": "error", "success": False, "message": "已有生产准入测试正在运行"}), 409
        try:
            body = request.get_json(silent=True) or {}
        except Exception:
            body = {}
        layers = body.get("layers") or "all"
        headless = bool(body.get("headless", False))  # ★ 默认有头模式（headless会被风控检测）
        # 从配置取第一个已勾选目标站（L3对抗验证需要真实目标）
        target_url = ""
        _urls_cfg = config.get("target_urls")
        if isinstance(_urls_cfg, list) and _urls_cfg:
            target_url = next((item.get("url", "").strip() for item in _urls_cfg
                               if item.get("enabled") and item.get("url", "").strip()), "")
        if not target_url:
            target_url = config.get("target_url", "") or ""
        # 全量测试重置所有状态；单层测试保留已有层结果（累积合并）
        if layers == "all":
            _prodtest_state.update({"running": True, "progress": 0, "stage": "启动中",
                                    "layers": {}, "gate": None, "report_path": None, "logs": []})
        else:
            _prodtest_state.update({"running": True, "progress": 0, "stage": "启动中", "logs": []})
        _prodtest_last_update = time.time()  # ★ 重置看门狗时间戳
    from threading import Thread
    Thread(target=_run_production_test_thread, args=(layers, headless, target_url), daemon=True).start()
    log.info(f"✅ 生产准入测试线程已启动，层级: {layers}")
    return jsonify({"status": "ok", "success": True, "message": "生产准入测试已启动", "layers": layers})


@app.route('/get_production_test_status')
def get_production_test_status():
    global _prodtest_state, _prodtest_last_update
    # ★ 看门狗：如果running但超过180秒无进度更新，强制重置为卡死状态
    if _prodtest_state["running"] and (time.time() - _prodtest_last_update > _PRODTEST_STUCK_TIMEOUT):
        log.warning(f"[生产准入] ⚠️ 看门狗触发：超过{_PRODTEST_STUCK_TIMEOUT}秒无进度更新，强制重置")
        _prodtest_state["running"] = False
        _prodtest_state["stage"] = "⚠️ 超时卡死，已自动重置"
        _prodtest_state["progress"] = 0
    return jsonify({
        "running": _prodtest_state["running"],
        "progress": _prodtest_state["progress"],
        "stage": _prodtest_state["stage"],
        "layers": _prodtest_state["layers"],
        "gate": _prodtest_state["gate"],
        "report_path": _prodtest_state["report_path"],
        "logs": _prodtest_state["logs"][-60:],
    })


@app.route('/force_stop_production_test', methods=['POST'])
def force_stop_production_test():
    """★ 强制停止卡死的生产准入测试"""
    global _prodtest_state, _prodtest_last_update
    _prodtest_state["running"] = False
    _prodtest_state["stage"] = "已强制停止"
    _prodtest_state["progress"] = 0
    _prodtest_last_update = time.time()
    log.info("[生产准入] 🛑 用户强制停止测试")
    return jsonify({"status": "ok", "message": "已强制停止"})


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
        # ★ 过滤过长的锚文本（>60字符通常是章节标题，不适合作为搜索关键词）
        if len(text) > 60:
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


# -------- 广告代码检测（HTML级别，用于关键词探索过滤无广告页面） --------
# 主流广告联盟的 HTML 特征签名
_AD_HTML_SIGNATURES = [
    # HilltopAds
    'hilltopads.com', 'hilltopads.net',
    # Google AdSense
    'adsbygoogle', 'googlesyndication', 'pagead2.googlesyndication', 'data-ad-client', 'data-ad-slot',
    # PropellerAds
    'propellerads.com', 'propellerclick.com',
    # MGID
    'mgid.com',
    # Taboola / Outbrain
    'taboola.com', 'outbrain.com',
    # AdMaven
    'ad-maven.com',
    # EvaDav
    'evadav.com',
    # HilltopAds/EvaDav 投放域名（随机域名，通过站点白名单确认）
    'curoax.com', 'pufted.com', 'bony-teaching.com', 'untimely-hello.com',
    # NativeAds 容器
    'nativeads',
    # 通用广告服务器路径
    '/adserve/', '/adserver/', '/ads/',
    # 通用广告属性
    'data-zone', 'data-adzone',
]

def _html_has_ad_code(html):
    """检查 HTML 源码中是否包含广告联盟代码。
    返回检测到的联盟名称(str)或 None。
    """
    if not html:
        return None
    html_lower = html.lower()
    # 按优先级检测
    checks = [
        ('HilltopAds', ['hilltopads.com', 'hilltopads.net']),
        ('AdSense', ['adsbygoogle', 'googlesyndication', 'pagead2.googlesyndication', 'data-ad-client']),
        ('GAM', ['securepubads.g.doubleclick', 'googletagservices']),
        ('EvaDav', ['evadav.com']),
        ('HilltopAds/EvaDav', ['curoax.com', 'pufted.com', 'bony-teaching.com', 'untimely-hello.com']),
        ('NativeAds', ['nativeads']),
        ('PropellerAds', ['propellerads.com', 'propellerclick.com']),
        ('MGID', ['mgid.com']),
        ('Taboola', ['taboola.com']),
        ('Outbrain', ['outbrain.com']),
        ('AdMaven', ['ad-maven.com']),
        ('Ezoic', ['ezoic.com', 'ezoicnet.com']),
        ('Mediavine', ['mediavine.com']),
        ('AdThrive/Raptive', ['adthrive.com', 'raptive.com']),
        ('Monumetric', ['monumetric.com', 'broadstreetads.com']),
        ('Infolinks/Adsterra', ['infolinks.com', 'adsterra.com']),
        ('BuySellAds', ['buysellads.com', 'carbonads']),
        ('Generic', ['/adserve/', '/adserver/', 'data-zone', 'data-adzone', 'data-ad-id', 'data-adunit']),
    ]
    for network, sigs in checks:
        for sig in sigs:
            if sig in html_lower:
                return network
    return None


def _fetch_page(url, session=None, timeout=10):
    """获取单个页面 HTML"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        if session:
            r = session.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.get(url, headers=headers, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f'[关键词探索] _fetch_page异常: {url} | {type(e).__name__}: {str(e)[:100]}')
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
    mgr['max_layer'] = max_layer  # ★ 修复：使用实际传入的max_layer，而非硬编码6
    mgr['result'] = None
    mgr['error'] = None

    session = requests.Session()
    session.verify = False

    visited = set()
    layer_anchor_texts = {}   # {layer_num: set(anchor_text)} 每层的关键词（可点击链接文本）
    layer_fallback_links = {} # {layer_num: set(url)} 每层的兜底链接
    layer_all_outgoing = {}   # {layer_num: set(url)} 每层全量出站链接（回退用）
    all_urls = set()          # 所有层级的去重URL汇总
    # ★ 广告统计累加器
    total_ad_pages = 0
    total_no_ad_pages = 0

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

            # 存储每个页面的 (anchor_texts, outgoing_links, has_ad_network)
            page_data = []  # [(url, anchor_texts_set, outgoing_links_set, ad_network_or_None), ...]

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
                        # ★ 检测页面是否含广告代码
                        ad_network = _html_has_ad_code(html)

                        page_data.append((url, anchor_texts, outgoing_links, ad_network))
                        _ad_tag = f'🎯广告:{ad_network}' if ad_network else '❌无广告'
                        log.info(f'[关键词探索] ✅ {url} | 锚文本: {len(anchor_texts)} | 出站链接: {len(outgoing_links)} | {_ad_tag}')

                    except Exception as e:
                        log.warning(f'[关键词探索] 页面异常 {url}: {e}')

            # ---- 构建本层关键词池和兜底链接 ----
            # 本层关键词 = 所有页面的锚文本并集（可点击链接文本）
            layer_kw_pool = set()
            for _, ats, _, _ in page_data:
                layer_kw_pool.update(ats)
            # ★ 每层最多保留100个关键词（优先短关键词，匹配率更高）
            if len(layer_kw_pool) > 100:
                _sorted_kws = sorted(layer_kw_pool, key=lambda x: len(x))
                layer_kw_pool = set(_sorted_kws[:100])
                log.info(f'[关键词探索] 第 {layer} 层关键词过多，截取前100个短关键词')

            # ★ 本层兜底链接 = 只纳入含广告页面的出站链接（过滤无广告页面）
            layer_fb = set()
            layer_all_fb = set()  # 全量出站链接（回退用）
            _ad_pages = 0
            _no_ad_pages = 0
            for url, _, out_links, ad_net in page_data:
                layer_all_fb.update(out_links)
                if ad_net:
                    _ad_pages += 1
                    layer_fb.update(out_links)
                else:
                    _no_ad_pages += 1
            # 兜底链接去除已经作为关键词来源的页面URL
            layer_fb -= {u for u, _, _, _ in page_data}
            layer_all_fb -= {u for u, _, _, _ in page_data}

            layer_anchor_texts[layer] = layer_kw_pool
            layer_fallback_links[layer] = layer_fb
            layer_all_outgoing[layer] = layer_all_fb
            total_ad_pages += _ad_pages
            total_no_ad_pages += _no_ad_pages
            log.info(f'[关键词探索] 第 {layer} 层完成 | 关键词: {len(layer_kw_pool)} | 兜底链接: {len(layer_fb)} | 含广告页: {_ad_pages} | 无广告页: {_no_ad_pages}')

            # 下一层的URL = 本层所有页面的 outgoing 链接（去重后未访问的）
            next_layer_urls = set()
            for _, _, out_links, _ in page_data:
                for link in out_links:
                    if link not in visited:
                        next_layer_urls.add(link)
                        all_urls.add(link)
            current_urls = next_layer_urls

        # ★ 回退逻辑：如果所有页面都检测不到广告代码（说明该站广告是JS动态注入），
        # 则使用全量出站链接作为兜底链接，否则兜底链接为0毫无意义
        if total_ad_pages == 0 and total_no_ad_pages > 0:
            log.warning(f'[关键词探索] ⚠️ 所有 {total_no_ad_pages} 个页面均未在HTML中检测到广告代码')
            log.warning(f'[关键词探索] ⚠️ 该站可能使用JS动态加载广告，回退为使用全量出站链接作为兜底链接')
            for layer_num in layer_all_outgoing:
                layer_fallback_links[layer_num] = layer_all_outgoing[layer_num]

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
            f.write(f'# 总兜底链接数: {total_fallback}（仅含广告页面的链接）\n')
            f.write(f'# 广告统计: 含广告页 {total_ad_pages} 个, 无广告页 {total_no_ad_pages} 个, 广告命中率 {total_ad_pages/max(1, total_ad_pages+total_no_ad_pages)*100:.0f}%\n\n')

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
            'merged_fallback_urls': sorted(all_merged_fbs),
            # ★ 广告统计
            'ad_pages': total_ad_pages,
            'no_ad_pages': total_no_ad_pages,
            'ad_hit_rate': round(total_ad_pages / max(1, total_ad_pages + total_no_ad_pages) * 100)
        }
        mgr['progress'] = f'探索完成！共 {total_keywords} 个关键词，{total_fallback} 个兜底链接（广告命中率 {round(total_ad_pages / max(1, total_ad_pages + total_no_ad_pages) * 100)}%）'
        log.info(f'[关键词探索] ✅ 完成！关键词: {total_keywords} | 兜底链接: {total_fallback} | 含广告页: {total_ad_pages} | 无广告页: {total_no_ad_pages} | 文件: {filename}')

    except Exception as e:
        mgr['error'] = str(e)
        mgr['progress'] = f'探索失败: {str(e)}'
        log.error(f'[关键词探索] 异常: {str(e)}')
    finally:
        mgr['is_running'] = False


# -------- 路由 --------
_keyword_explore_lock = threading.Lock()  # ★ 审计修复#8：防止并发启动竞态

@app.route('/api/keyword_explore', methods=['POST'])
def start_keyword_explore():
    with _keyword_explore_lock:
        if keyword_explore_manager['is_running']:
            return jsonify({'success': False, 'message': '关键词探索正在进行中，请等待完成'})
        keyword_explore_manager['is_running'] = True

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
    # ★ 审计修复#1：HTML转义防止XSS注入
    import html as _html_escape_mod
    return ''.join([f"<p>{_html_escape_mod.escape(msg)}</p>" for msg in reversed(messages)])

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

# ========== 🛡️ Dwell Monitor Guardian：Flask 控制接口（前端按钮：启动/停止/状态） ==========
# 设计：用 subprocess.Popen 启动独立的 _dwell_monitor_guardian.py 守护进程，与 app.py 完全解耦，
#       即便 app.py reload/重启，守护进程仍可单独存活或被明确 kill。
import subprocess as _sp_dm
import signal as _sig_dm
_DWELL_MONITOR_PROC = {"proc": None, "start_ts": None}

def _dm_snapshot_read_safe() -> dict:
    """最佳方案是 guardian 写 JSON 到 logs/monitor_status.json，这里读它；若文件不存在则返回空快照。"""
    import json as _json_dm
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "monitor_status.json")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return _json_dm.load(f)
    except Exception:
        pass
    return {}

@app.route('/dwell_monitor/status', methods=['GET'])
def dwell_monitor_status():
    proc = _DWELL_MONITOR_PROC["proc"]
    running = bool(proc and proc.poll() is None)
    pid = proc.pid if running else (proc.pid if proc else None)
    full_status = _dm_snapshot_read_safe()
    snapshot = full_status.get("snapshot", {})
    alerts = full_status.get("alerts", [])
    return jsonify({
        "success": True,
        "running": running,
        "pid": pid,
        "start_ts": _DWELL_MONITOR_PROC["start_ts"],
        "snapshot": snapshot,
        "alerts": alerts[-20:],  # 最近20条告警
        "alert_count": len(alerts),
        "alert_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "monitor_alerts.log"),
        "consecutive_crit": full_status.get("consecutive_crit", 0),
    })

@app.route('/dwell_monitor/alerts', methods=['GET'])
def dwell_monitor_alerts():
    """返回告警历史（最近100条），支持 ?limit=N 参数"""
    import json as _json_dm
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 200))  # 限制在 1-200 之间
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "monitor_alerts.log")
    alerts = []
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            alerts.append(_json_dm.loads(line))
                        except Exception:
                            pass
    except Exception:
        pass
    # 按时间倒序，返回最近 limit 条
    alerts = alerts[-limit:]
    return jsonify({
        "success": True,
        "alerts": alerts,
        "total": len(alerts),
    })

@app.route('/dwell_monitor/start', methods=['POST'])
def dwell_monitor_start():
    global _DWELL_MONITOR_PROC
    data = request.get_json(silent=True) or {}
    no_auto_pause = bool(data.get("no_auto_pause", False))
    # 若已运行 → 直接返回成功
    proc = _DWELL_MONITOR_PROC["proc"]
    if proc and proc.poll() is None:
        return jsonify({"success": True, "message": "Dwell Monitor 已在运行", "pid": proc.pid})
    # 否则 spawn 一个新的 Python 子进程（stdout/stderr 吞掉，防止阻塞 app）
    base_dir = os.path.dirname(os.path.abspath(__file__))
    guardian_script = os.path.join(base_dir, "_dwell_monitor_guardian.py")
    if not os.path.exists(guardian_script):
        return jsonify({"success": False, "message": f"监控脚本不存在: {guardian_script}"}), 404
    env = os.environ.copy()
    cmd = [
        sys.executable, guardian_script,
        "--host=127.0.0.1",
        f"--port={config.get('server_port', 5000) if hasattr(config, 'get') else 5000}",
        f"--log={os.path.join(base_dir, 'app.log')}",
        "--poll=0.15",
    ]
    if no_auto_pause:
        cmd.append("--no-auto-pause")
    try:
        new_proc = _sp_dm.Popen(
            cmd,
            stdout=_sp_dm.DEVNULL,
            stderr=_sp_dm.STDOUT,
            cwd=base_dir,
            env=env,
            start_new_session=True,  # 独立会话，app 退出后 guardian 不会被连带 kill（用户要手动停）
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"启动失败: {type(e).__name__}: {e}"}), 500
    _DWELL_MONITOR_PROC["proc"] = new_proc
    _DWELL_MONITOR_PROC["start_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"success": True, "pid": new_proc.pid, "cmd": cmd})

@app.route('/dwell_monitor/stop', methods=['POST'])
def dwell_monitor_stop():
    global _DWELL_MONITOR_PROC
    proc = _DWELL_MONITOR_PROC["proc"]
    if not proc:
        return jsonify({"success": True, "message": "Dwell Monitor 未启动"})
    if proc.poll() is not None:
        _DWELL_MONITOR_PROC["proc"] = None
        return jsonify({"success": True, "message": "Dwell Monitor 已退出(僵尸句柄清理)"})
    try:
        try:
            # start_new_session=True 的情况下 kill pgid 最干净
            os.killpg(os.getpgid(proc.pid), _sig_dm.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except _sp_dm.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), _sig_dm.SIGKILL)
            except Exception:
                proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
    finally:
        _DWELL_MONITOR_PROC["proc"] = None
    return jsonify({"success": True, "message": "Dwell Monitor 已停止"})

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
    data = request.get_json(silent=True) or {}  # ★ 审计修复#3：防止None崩溃
    
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

    # ★ 更新 HilltopAds Pop-under 配置
    if 'hilltopads_enabled' in data:
        config.setdefault('hilltopads', {})
        config['hilltopads']['enabled'] = bool(data.get('hilltopads_enabled', False))
        config['hilltopads']['trigger_probability'] = float(data.get('hilltopads_trigger_prob', 0.40))
        config['hilltopads']['popunder_stay_min'] = int(data.get('hilltopads_stay_min', 15))
        config['hilltopads']['popunder_stay_max'] = int(data.get('hilltopads_stay_max', 25))

    # 保存到配置文件
    # ★ 审计修复#5：指定encoding防止非UTF-8环境崩溃
    with open('config.json', 'w', encoding='utf-8') as f:
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
            # ★ 修复：deep_merge_defaults对列表类型会取默认值（10空条目），覆盖用户保存的proxy_pool
            # 先保存用户已加载的proxy_pool，合并后恢复
            _user_proxy_pool = loaded_config.get('proxy_pool')
            config.clear()
            config.update(deep_merge_defaults(DEFAULT_CONFIG, loaded_config))
            # 恢复用户保存的proxy_pool（非空时优先使用用户配置）
            if _user_proxy_pool and isinstance(_user_proxy_pool, list) and len(_user_proxy_pool) > 0:
                config['proxy_pool'] = _user_proxy_pool
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
