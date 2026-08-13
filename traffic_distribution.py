"""
流量分布精细化模块 — 平台方红队测试专用（正当安全测试）

问题：原框架按国家均匀分配流量，真实世界应加权（美国/印度人口多流量多、瑞士/新加坡人口少流量少），
且真实流量受工作日/周末/节假日季节效应影响。

提供：
  1. COUNTRY_POPULATION_WEIGHTS — 各国互联网人口权重（相对值，用于代理池按权重采样）
  2. WEEKDAY_WEEKEND_ADJUSTMENT — 工作日/周末整体流量差异（周末通常高10-20%）
  3. DAILY_PATTERN_BY_COUNTRY — 各国典型日常时段曲线（工作9-18波峰 vs 夜间低谷）
  4. 便捷采样器 weighted_country_sample()、is_heavy_traffic_day()

用法：
    from traffic_distribution import weighted_country_sample, is_heavy_traffic_day
    if is_heavy_traffic_day(datetime.now(), "US"):
        scale = 1.15
    cc = weighted_country_sample(["US", "DE", "JP", "IN", "BR", "ID"])
"""
from __future__ import annotations

import random
import secrets
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

_sec = secrets.SystemRandom()

# ============================================================================
# 一、各国互联网人口权重（相对值，2024 数据归一化。仅用于红队测试时模拟真实流量分布）
#    来源：ITU / InternetWorldStats 互联网用户数，做归一化以形成相对采样权重
# ============================================================================
COUNTRY_POPULATION_WEIGHTS: Dict[str, float] = {
    # G20 主流市场（合计覆盖全球约 80% 互联网用户）
    "CN": 16.0,   # 中国
    "IN": 13.5,   # 印度
    "US": 9.0,    # 美国
    "ID": 5.5,    # 印尼
    "BR": 4.0,    # 巴西
    "JP": 3.2,    # 日本
    "MX": 2.5,    # 墨西哥
    "PH": 2.2,    # 菲律宾
    "VN": 2.1,    # 越南
    "RU": 2.0,    # 俄罗斯
    "DE": 1.8,    # 德国
    "TR": 1.7,    # 土耳其
    "GB": 1.5,    # 英国
    "FR": 1.5,    # 法国
    "IT": 1.4,    # 意大利
    "KR": 1.3,    # 韩国
    "ES": 1.2,    # 西班牙
    "CA": 1.0,    # 加拿大
    "AU": 0.9,    # 澳大利亚
    "AR": 0.9,    # 阿根廷
    "ZA": 0.9,    # 南非
    "SA": 0.8,    # 沙特
    "TH": 0.8,    # 泰国
    "EG": 0.8,    # 埃及
    "PL": 0.7,    # 波兰
    "NG": 0.7,    # 尼日利亚
    "CO": 0.6,    # 哥伦比亚
    "MY": 0.6,    # 马来西亚
    "PE": 0.5,    # 秘鲁
    "NL": 0.5,    # 荷兰
    "SE": 0.4,    # 瑞典
    "BE": 0.4,    # 比利时
    "CH": 0.3,    # 瑞士
    "SG": 0.3,    # 新加坡
    "HK": 0.3,    # 香港
    "NZ": 0.2,    # 新西兰
    "IE": 0.2,    # 爱尔兰
    "NO": 0.2,    # 挪威
    "DK": 0.2,    # 丹麦
    "FI": 0.2,    # 芬兰
    "PT": 0.2,    # 葡萄牙
    "GR": 0.2,    # 希腊
    "CZ": 0.2,    # 捷克
    "HU": 0.2,    # 匈牙利
    "RO": 0.2,    # 罗马尼亚
    "UA": 0.2,    # 乌克兰
    "CL": 0.2,    # 智利
    "KE": 0.2,    # 肯尼亚
}

# 兜底权重（国家不在映射表时使用）
DEFAULT_COUNTRY_WEIGHT = 0.3


def weighted_country_sample(
    candidates: Iterable[str],
    *,
    weights: Optional[Dict[str, float]] = None,
    rng=None,
) -> str:
    """
    按人口权重从候选国家里采样一个（放回）。
    candidates 中的国家不在 COUNTRY_POPULATION_WEIGHTS 时使用兜底权重。
    """
    rng = rng or _sec
    pool = list(candidates)
    if not pool:
        raise ValueError("candidates 不能是空列表")
    w_map = weights or COUNTRY_POPULATION_WEIGHTS
    w_list = [w_map.get(cc.upper(), DEFAULT_COUNTRY_WEIGHT) for cc in pool]
    chosen = rng.choices(pool, weights=w_list, k=1)[0]
    return chosen


# ============================================================================
# 二、工作日 / 周末 / 季节性 流量强度乘数
# ============================================================================

# 典型周末强度倍数（全球平均周末比工作日多 ~15% 流量）
WEEKEND_SCALE_DEFAULT = 1.15
WEEKDAY_SCALE_DEFAULT = 1.00

# ============================================================================
# 二点五、各国标准时区偏移（用于 local_hour → UTC 小时换算）
#  注：这里取"标准时间"（不考虑夏令时 DST），对流量规划足够精确。
#  正数 = UTC+X（东半球），负数 = UTC-X（西半球），None = 未知 → 按 UTC±0
# ============================================================================
COUNTRY_TZ_STANDARD_OFFSET_HOUR: Dict[str, float] = {
    # 东亚
    "CN": 8.0, "JP": 9.0, "KR": 9.0, "HK": 8.0, "TW": 8.0, "SG": 8.0, "MY": 8.0,
    "TH": 7.0, "VN": 7.0, "ID": 7.0, "PH": 8.0,
    # 南亚 / 东南亚
    "IN": 5.5,  # UTC+5:30
    "PK": 5.0, "BD": 6.0, "LK": 5.5, "NP": 5.75,
    # 中东 / 俄罗斯
    "RU": 3.0,  # 莫斯科 UTC+3
    "TR": 3.0, "AE": 4.0, "SA": 3.0, "IR": 3.5, "IL": 2.0,
    # 欧洲（标准时间，不考虑夏令时）
    "GB": 0.0, "IE": 0.0, "PT": 0.0, "IS": 0.0,
    "DE": 1.0, "FR": 1.0, "ES": 1.0, "IT": 1.0, "NL": 1.0, "BE": 1.0,
    "SE": 1.0, "NO": 1.0, "DK": 1.0, "FI": 2.0, "PL": 1.0, "CZ": 1.0,
    "AT": 1.0, "CH": 1.0, "GR": 2.0, "RO": 2.0, "HU": 1.0,
    # 美洲（标准时间）
    "US": -5.0,  # ET / 东部时间 UTC-5
    "CA": -5.0,  # 多伦多/蒙特利尔 ET
    "MX": -6.0,  # CST 中部
    "BR": -3.0,  # 巴西利亚 UTC-3
    "AR": -3.0, "CL": -4.0, "CO": -5.0, "PE": -5.0,
    # 澳新
    "AU": 10.0, "NZ": 12.0,
    # 非洲
    "ZA": 2.0, "EG": 2.0, "NG": 1.0, "KE": 3.0, "MA": 1.0,
}
DEFAULT_TZ_OFFSET_HOUR = 0.0


def local_hour_to_utc_hour(country_code: str, local_hour: float) -> float:
    """将某国本地小时（0~24）转换为 UTC 小时（0~24，超范围 mod 24）。

    例：本地 20:00（CN, UTC+8）→ UTC = 12:00
       本地 20:00（US, UTC-5）→ UTC = 次日 01:00 → 返回 1.0
    未知国家 → 返回原值（UTC±0 不换算，向下兼容）。
    """
    off = COUNTRY_TZ_STANDARD_OFFSET_HOUR.get(country_code.upper())
    if off is None:
        return local_hour % 24.0
    utc_h = (local_hour - off) % 24.0
    return round(utc_h, 4)


def utc_hour_to_local_hour(country_code: str, utc_hour: float) -> float:
    """反向转换，用于日志/展示。"""
    off = COUNTRY_TZ_STANDARD_OFFSET_HOUR.get(country_code.upper())
    if off is None:
        return utc_hour % 24.0
    return round(((utc_hour + off) % 24.0), 4)

# 各国主要节假日（仅示例，仅用于红队测试时调参；真实使用时可按需要扩充）
# 格式：set of "MM-DD" 字符串
COUNTRY_HOLIDAYS: Dict[str, set] = {
    "US": {"01-01", "07-04", "11-27", "12-25"},   # 元旦/独立日/感恩节(浮动近似)/圣诞
    "CN": {"01-01", "02-01", "02-02", "02-03", "02-04", "02-05", "02-06",
           "02-07", "02-08", "05-01", "10-01", "10-02", "10-03", "10-04",
           "10-05", "10-06", "10-07"},              # 元旦/春节一周/五一/国庆一周
    "JP": {"01-01", "01-02", "01-03", "02-11",
           "04-29", "05-03", "05-04", "05-05",
           "08-11", "09-23", "11-03", "11-23", "12-23"},
    "GB": {"01-01", "04-07", "05-06", "12-25", "12-26"},  # 元旦/复活节(近似)/五一/圣诞/节礼日
    "DE": {"01-01", "04-07", "05-01", "10-03", "12-25", "12-26"},
    "FR": {"01-01", "05-01", "05-08", "07-14", "08-15", "11-01", "11-11", "12-25"},
    "IN": {"01-26", "08-15", "10-02", "10-24", "11-01", "12-25"},  # 共和国日/独立日/甘地诞辰等
    "BR": {"01-01", "04-21", "05-01", "09-07", "10-12", "11-02", "11-15", "12-25"},
    "ID": {"01-01", "03-31", "04-01", "05-01", "08-17", "12-25"},
    "KR": {"01-01", "03-01", "05-05", "06-06", "08-15", "10-03", "10-09", "12-25"},
}

# 节假日流量倍率：公共假日当天通常比工作日高 20-40%
HOLIDAY_SCALE_DEFAULT = 1.30


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_holiday(d: date, country_code: str) -> bool:
    hset = COUNTRY_HOLIDAYS.get(country_code.upper())
    if not hset:
        return False
    return d.strftime("%m-%d") in hset


def traffic_day_scale(
    d: Optional[date] = None,
    country_code: str = "US",
    *,
    weekend_scale: float = WEEKEND_SCALE_DEFAULT,
    weekday_scale: float = WEEKDAY_SCALE_DEFAULT,
    holiday_scale: float = HOLIDAY_SCALE_DEFAULT,
) -> float:
    """
    返回指定日期/国家的流量强度乘数。
    假日最高，周末次之，工作日基准 = 1。
    """
    d = d or datetime.now().date()
    if is_holiday(d, country_code):
        return holiday_scale
    if is_weekend(d):
        return weekend_scale
    return weekday_scale


def is_heavy_traffic_day(
    d: Optional[date] = None,
    country_code: str = "US",
) -> bool:
    """便捷：当天/当国是否为流量高峰日（周末或假日）。"""
    return traffic_day_scale(d, country_code) > WEEKDAY_SCALE_DEFAULT


# ============================================================================
# 三、各国典型日内流量曲线（简化版：用 3 个波峰时段 + 午夜低谷表达）
#    用于 generate_daily_tasks 中按更真实的 24h 分布安排任务
# ============================================================================
# {country_code: [(hour_start, hour_end, weight_multiplier), ...]}
# 均为当地时间，小时段为左闭右开，倍率 = 相对平均值。
COUNTRY_DAILY_PATTERNS: Dict[str, List] = {
    "US": [
        (0, 5, 0.20),   # 凌晨低谷
        (5, 9, 0.70),   # 早晨
        (9, 12, 1.20),  # 上午波峰(通勤后+办公)
        (12, 14, 1.40), # 中午波峰(午休浏览)
        (14, 17, 1.20), # 下午
        (17, 19, 1.10), # 下班时段
        (19, 23, 1.60), # 晚间黄金档(家庭/娱乐浏览最高峰)
        (23, 24, 0.35), # 深夜衰减
    ],
    "CN": [
        (0, 6, 0.15),
        (6, 9, 0.75),
        (9, 12, 1.10),
        (12, 14, 1.55),  # 午休刷手机
        (14, 18, 1.05),
        (18, 20, 0.95),  # 通勤路上
        (20, 23, 1.80),  # 晚间大高峰(国内短视频/小说站典型峰值)
        (23, 24, 0.50),
    ],
    "JP": [
        (0, 5, 0.15),
        (5, 9, 0.80),
        (9, 12, 1.20),
        (12, 14, 1.35),
        (14, 18, 1.15),
        (18, 20, 1.05),
        (20, 23, 1.55),
        (23, 24, 0.35),
    ],
    "GB": [
        (0, 5, 0.20), (5, 9, 0.75), (9, 12, 1.25),
        (12, 14, 1.35), (14, 17, 1.20), (17, 19, 1.10),
        (19, 23, 1.55), (23, 24, 0.35),
    ],
    "DE": [
        (0, 5, 0.18), (5, 9, 0.70), (9, 12, 1.25),
        (12, 14, 1.30), (14, 17, 1.20), (17, 19, 1.15),
        (19, 23, 1.50), (23, 24, 0.35),
    ],
    "IN": [
        (0, 5, 0.20), (5, 9, 0.70), (9, 12, 1.15),
        (12, 14, 1.25), (14, 17, 1.10), (17, 19, 1.10),
        (19, 23, 1.70), (23, 24, 0.40),
    ],
    "BR": [
        (0, 5, 0.18), (5, 9, 0.65), (9, 12, 1.15),
        (12, 14, 1.30), (14, 17, 1.15), (17, 19, 1.15),
        (19, 23, 1.65), (23, 24, 0.35),
    ],
    "ID": [
        (0, 5, 0.20), (5, 9, 0.70), (9, 12, 1.15),
        (12, 14, 1.30), (14, 17, 1.15), (17, 19, 1.10),
        (19, 23, 1.60), (23, 24, 0.35),
    ],
}

DEFAULT_DAILY_PATTERN = [
    (0, 5, 0.20), (5, 9, 0.70), (9, 12, 1.15),
    (12, 14, 1.30), (14, 17, 1.15), (17, 19, 1.10),
    (19, 23, 1.55), (23, 24, 0.35),
]


def hourly_weight(country_code: str, local_hour: int) -> float:
    """返回某国当地某小时的流量相对权重（>=0）。"""
    pattern = COUNTRY_DAILY_PATTERNS.get(country_code.upper(), DEFAULT_DAILY_PATTERN)
    h = max(0, min(23, int(local_hour)))
    for start, end, w in pattern:
        if start <= h < end:
            return w
    # fallback
    return 1.0


def weighted_local_hours(
    country_code: str,
    n: int = 24,
    *,
    min_hour: Optional[float] = None,
    max_hour: Optional[float] = None,
    rng=None,
) -> List[float]:
    """
    按该国 24 小时加权曲线采样 n 个小时槽位（浮点，单位：小时，范围 0-24）。
    n 越大越接近真实分布。

    可选参数 min_hour / max_hour（闭区间 [min_hour, max_hour)）：
      用于 enforce_working_hours 风格的硬截断（例 8.0~23.0，禁止深夜任务）。
      超限样本会重试最多 max_retry=4 次；仍失败则在 [min_hour, max_hour) 内均匀取一点。
      None 表示不做边界限制（默认，全 0-24 小时）。
    """
    rng = rng or _sec
    pattern = COUNTRY_DAILY_PATTERNS.get(country_code.upper(), DEFAULT_DAILY_PATTERN)
    slots, weights = [], []
    for start, end, w in pattern:
        slots.append((start, end))
        weights.append(w)
    results: List[float] = []
    for _ in range(n):
        v = None
        for _try in range(4):
            (s, e) = rng.choices(slots, weights=weights, k=1)[0]
            cand = round(rng.uniform(s, e), 4)
            if min_hour is not None and cand < min_hour:
                continue
            if max_hour is not None and cand >= max_hour:
                continue
            v = cand
            break
        if v is None:
            # 4 次都落在边界外（极小概率，例该国曲线偏深夜+强制 8~23），兜底均匀
            lo = min_hour if min_hour is not None else 0.0
            hi = max_hour if max_hour is not None else 24.0
            v = round(rng.uniform(lo, hi), 4)
        results.append(v)
    return results


if __name__ == "__main__":
    print("=== 各国人口权重 Top10 ===")
    top = sorted(COUNTRY_POPULATION_WEIGHTS.items(), key=lambda x: -x[1])[:10]
    for cc, w in top:
        print(f"  {cc}: {w}")

    print("\n=== 100 次加权采样（从 US/DE/JP/IN/BR/ID 中）===")
    from collections import Counter
    c = Counter(weighted_country_sample(["US", "DE", "JP", "IN", "BR", "ID"]) for _ in range(100))
    for cc, n in c.most_common():
        print(f"  {cc}: {n}")

    print("\n=== 今天日期效果 ===")
    today = date.today()
    for cc in ["US", "CN", "JP", "IN", "DE"]:
        scale = traffic_day_scale(today, cc)
        tag = "假日" if is_holiday(today, cc) else ("周末" if is_weekend(today) else "工作日")
        print(f"  {cc} {today} ({tag}) scale={scale}")

    print("\n=== CN 24h 权重曲线（整点）===")
    for h in range(24):
        w = hourly_weight("CN", h)
        bar = "#" * int(w * 20)
        print(f"  {h:02d}:00  {w:.2f}  {bar}")

    print("\n=== CN 按曲线采样 20 个任务小时槽 ===")
    hrs = sorted(weighted_local_hours("CN", 20))
    for h in hrs:
        clock = f"{int(h):02d}:{int((h-int(h))*60):02d}"
        print(f"  {clock}", end="")
    print()
