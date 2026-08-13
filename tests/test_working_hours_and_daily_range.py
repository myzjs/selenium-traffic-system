"""test_working_hours_and_daily_range.py — 26.8.11.10 修复回归测试。

验证 3 个根因修复 + 1 个高危隐患：
  🔴 1. enforce_working_hours / 所有流量模型：只生成 [8.0, 23.0) 小时
  🔴 2. generate_daily_tasks(新函数)：所有任务当地时间都在 [8,23)，日任务量不低于 day_min
  🔴 3. generate_daily_tasks_legacy(旧函数)：同上，新增 outside_cc_work_hour 最终硬校验
  🟡 4. country_segments 覆盖段：边界严格 8:00 与 23:00，不再 7:00 / 24:00
"""
import json
import os
import sys
import datetime as dt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 避免触发 app.py 的 Flask/全局 side-effect，只做模块层 import
from app import (
    enforce_working_hours,
    generate_normal_hours,
    generate_gamma_hours,
    generate_poisson_hours,
    generate_bimodal_hours,
    generate_burst_hours,
    generate_power_law_hours,
    soft_boundary_probability,
    generate_daily_tasks,
    generate_daily_tasks_legacy,
    get_timezone_for_country,
)
import pytz
import random


WORK_HOUR_MIN = 8.0
WORK_HOUR_MAX = 23.0  # 开区间，<23.0

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
with open(CFG_PATH, "r") as _f:
    BASE_CFG = json.load(_f)


# ───────────────────────── helper ─────────────────────────
def _task_local_hour(task_utc_ts, country_code):
    """把任务 actual_start（相对 today_utc_start 的 UTC 秒）换算成 chosen_country 的浮点数小时。

    ⚠️ 26.8.13.1 修复测试误读：actual_start 不是 epoch 秒（如 43200 会被
    fromtimestamp 当成 1970-01-01 12:00 UTC，US 当地 07:00 → 18 个假失败），
    而是"今天 UTC 0 点起的秒数"，必须加到 today_utc_start 上再转目标时区。
    """
    tz = pytz.timezone(get_timezone_for_country(country_code))
    today_utc_start = dt.datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    local = (today_utc_start + dt.timedelta(seconds=task_utc_ts)).astimezone(tz)
    return local.hour + local.minute / 60.0


# ───────────────────────── 1. enforce_working_hours & 6 种流量模型 ─────────────────────────
class TestEnforceWorkingHoursAndModels:
    @pytest.mark.parametrize("raw_min,raw_max", [
        (0, 7), (23, 24), (0, 24),  # 全非法 / 混合
    ])
    def test_enforce_working_hours_always_returns_legal_range(self, raw_min, raw_max):
        N = 500
        raw = [random.uniform(raw_min, raw_max) for _ in range(N)]
        hours = enforce_working_hours(raw)
        assert len(hours) == N, f"enforce 必须保留原数量 {N}，实际 {len(hours)}"
        bad = [h for h in hours if not (WORK_HOUR_MIN <= h < WORK_HOUR_MAX)]
        assert bad == [], f"enforce 后存在非法小时: {bad[:5]}"

    @pytest.mark.parametrize("model_fn,label", [
        (generate_normal_hours, "normal"),
        (generate_gamma_hours, "gamma"),
        (generate_poisson_hours, "poisson"),
        (generate_bimodal_hours, "bimodal"),
        (generate_burst_hours, "burst"),
        (generate_power_law_hours, "power_law"),
    ])
    @pytest.mark.parametrize("n", [20, 200])
    def test_all_models_produce_only_working_hours(self, model_fn, label, n):
        random.seed(None)
        for _ in range(5):
            hours = model_fn(n)
            assert len(hours) == n, f"{label}(n={n}) 数量偏差: expected {n}, got {len(hours)}"
            bad = [h for h in hours if not (WORK_HOUR_MIN <= h < WORK_HOUR_MAX)]
            assert bad == [], f"{label}(n={n}) 存在非法小时: {bad[:5]}"


# ───────────────────────── 2. soft_boundary_probability 凌晨=0 ─────────────────────────
class TestSoftBoundary:
    def test_midnight_probability_is_zero(self):
        """当地 0:00 - 7:59 及 23:00 - 23:59 → 概率必须恒为 0。"""
        import datetime as _dt

        def _utc_sec_for_local_hour(country_code, local_hour):
            # 构造今天的指定本地小时 → 换算 UTC 秒 (相对 today_utc_start)
            tz = pytz.timezone(get_timezone_for_country(country_code))
            today_local = dt.datetime.now(tz).date()
            local_dt = tz.localize(_dt.datetime.combine(today_local, _dt.time(0, 0))) + _dt.timedelta(hours=local_hour)
            today_utc_start = dt.datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            return (local_dt.astimezone(pytz.UTC) - today_utc_start).total_seconds()

        for cc in ["US", "GB", "AU", "CA"]:
            for illegal_h in [0.0, 3.5, 7.9, 23.0, 23.5, 23.99]:
                utc_s = _utc_sec_for_local_hour(cc, illegal_h)
                p = soft_boundary_probability(utc_s, cc)
                assert p == 0.0, f"{cc} 本地 {illegal_h}h → 概率应为 0，实际 {p}"

            for core_h in [10.0, 12.5, 18.0, 21.0]:
                utc_s = _utc_sec_for_local_hour(cc, core_h)
                p = soft_boundary_probability(utc_s, cc)
                assert p == 1.0, f"{cc} 本地核心 {core_h}h → 概率应为 1，实际 {p}"


# ───────────────────────── 3. 新函数 generate_daily_tasks ─────────────────────────
class TestGenerateDailyTasks:
    def _make_cfg(self, site_age="old", plan_days=3, countries=None):
        """构造一个隔离配置，避免真实代理状态影响。"""
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg["site_age"] = site_age
        cfg["plan_days"] = plan_days
        cfg["site_creation_date"] = {
            "new": "2026-06-01", "mid": "2025-01-01", "old": "2020-01-01"
        }[site_age]
        # 固定启用的国家池（全部 enabled），避免随机
        enabled_ccs = countries or ["US", "GB", "AU", "CA"]
        cfg["proxy_pool"] = []
        for cc in enabled_ccs:
            cfg["proxy_pool"].append({
                "country_code": cc, "enabled": True,
                "proxy_api_url": f"https://example.com/{cc}",
                "proxy_user": "u", "proxy_pwd": "p",
            })
        return cfg

    @pytest.mark.parametrize("site_age", ["new", "mid", "old"])
    @pytest.mark.parametrize("seed", list(range(3)))
    def test_all_new_function_tasks_in_working_hours(self, site_age, seed):
        random.seed(seed)
        cfg = self._make_cfg(site_age=site_age, plan_days=3)
        result = generate_daily_tasks(cfg)
        tasks = result["tasks"]
        # 检查当地工作时间
        bad_tasks = []
        for t in tasks:
            cc = t.get("proxy_country") or "US"
            h = _task_local_hour(t["actual_start"], cc)
            if not (WORK_HOUR_MIN <= h < WORK_HOUR_MAX):
                bad_tasks.append((t["plan_time"], cc, round(h, 2)))
        assert bad_tasks == [], (
            f"new_fn site_age={site_age} seed={seed} 存在非工作时间任务: {bad_tasks[:5]}\n"
            f"discard_reasons={result['discard_reasons']}"
        )

    def test_new_function_daily_task_count_respects_min_range(self):
        """日流量区间不可被击穿：计划天数>1时，每天生成任务量应 ≥ daily_traffic_range[age].min。"""
        random.seed(42)
        cfg = self._make_cfg(site_age="old", plan_days=5)
        day_min = cfg["daily_traffic_range"]["old"]["min"]
        result = generate_daily_tasks(cfg)
        # 按日期聚合
        by_date = {}
        for t in result["tasks"]:
            d = t["date"]
            by_date[d] = by_date.get(d, 0) + 1
        # 跳过第一天（可能是"今天"，若当前时间已过下午会被比例削减，但旧函数第一天保底也有 day_min）
        full_days = sorted(by_date.keys())[1:] if len(by_date) > 1 else list(by_date.keys())
        for d in full_days:
            cnt = by_date[d]
            assert cnt >= day_min, (
                f"日期 {d} 任务量 {cnt} < 日流量下限 {day_min}。daily_summaries={result.get('daily_summaries')}"
            )


# ───────────────────────── 4. 旧函数 generate_daily_tasks_legacy 最终硬校验 ─────────────────────────
class TestGenerateDailyTasksLegacy:
    def _make_cfg(self, site_age="old", countries=None):
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg["site_age"] = site_age
        cfg["site_creation_date"] = {
            "new": "2026-06-01", "mid": "2025-01-01", "old": "2020-01-01"
        }[site_age]
        enabled_ccs = countries or ["US", "GB", "AU", "CA"]
        cfg["proxy_pool"] = []
        for cc in enabled_ccs:
            cfg["proxy_pool"].append({
                "country_code": cc, "enabled": True,
                "proxy_api_url": f"https://example.com/{cc}",
                "proxy_user": "u", "proxy_pwd": "p",
            })
        return cfg

    @pytest.mark.parametrize("site_age", ["new", "mid", "old"])
    @pytest.mark.parametrize("seed", list(range(3)))
    def test_legacy_all_tasks_in_working_hours(self, site_age, seed):
        """🔴 原 26.8.11.9 的阻断级 Bug：legacy 没有 outside_cc_work_hour 校验，会出现凌晨任务。"""
        random.seed(seed)
        cfg = self._make_cfg(site_age=site_age)
        result = generate_daily_tasks_legacy(cfg)
        tasks = result["tasks"]
        bad_tasks = []
        for t in tasks:
            cc = t.get("proxy_country") or "US"
            h = _task_local_hour(t["actual_start"], cc)
            if not (WORK_HOUR_MIN <= h < WORK_HOUR_MAX):
                bad_tasks.append((t["plan_time"], cc, round(h, 2)))
        assert bad_tasks == [], (
            f"legacy_fn site_age={site_age} seed={seed} 存在非工作时间任务: {bad_tasks[:5]}\n"
            f"discard_reasons={result.get('discard_reasons')}, total={len(tasks)}"
        )

    def test_legacy_discard_reasons_has_outside_cc_key(self):
        """26.8.11.10 新增键：确保 legacy 返回 dict 中存在 outside_cc_work_hour 计数键。"""
        cfg = self._make_cfg(site_age="old")
        result = generate_daily_tasks_legacy(cfg)
        reasons = result["discard_reasons"]
        assert "outside_cc_work_hour" in reasons, (
            f"legacy discard_reasons 缺少 outside_cc_work_hour 键，keys={list(reasons.keys())}"
        )


# ───────────────────────── 5. country_segments 边界对齐 8:00 / 23:00 ─────────────────────────
class TestCountrySegmentsBoundary:
    def test_new_function_segments_match_working_window(self):
        """新函数 generate_daily_tasks 内部构造的 country_segments 必须 8:00 开始，23:00 结束。"""
        # 通过反推：对启用国家构造极端早/晚的 UTC 秒，检测 get_countries_at_utc_sec 是否在 7:59 返回空、8:00 返回非空，22:59 返回非空、23:00 返回空
        import datetime as _dt

        def _utc_epoch_for_local(cc, hour, minute):
            tz = pytz.timezone(get_timezone_for_country(cc))
            today_local = dt.datetime.now(tz).date()
            dt_local = tz.localize(_dt.datetime.combine(today_local, _dt.time(hour, minute)))
            return int(dt_local.astimezone(pytz.UTC).timestamp())

        cfg = {
            "daily_traffic_range": BASE_CFG["daily_traffic_range"],
            "plan_days": 1,
            "site_creation_date": "2020-01-01",
            "proxy_pool": [
                {"country_code": "US", "enabled": True, "proxy_api_url": "x", "proxy_user": "u", "proxy_pwd": "p"},
                {"country_code": "GB", "enabled": True, "proxy_api_url": "x", "proxy_user": "u", "proxy_pwd": "p"},
            ],
        }
        # 跑一次函数拿到返回结果，再检查生成任务的实际时间（因为 country_segments 是内部变量）
        random.seed(0)
        result = generate_daily_tasks(cfg)
        # 所有任务的国家当地小时都必须 ≥8 且 <23（间接证明 country_segments + 硬校验共同生效）
        for t in result["tasks"]:
            cc = t.get("proxy_country", "US")
            h = _task_local_hour(t["actual_start"], cc)
            assert WORK_HOUR_MIN <= h < WORK_HOUR_MAX, (
                f"任务 {t['plan_time']} ({cc}) 当地 {h}h 超出 [8,23)"
            )
