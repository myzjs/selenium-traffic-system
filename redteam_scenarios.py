"""
红队攻击场景库 — 用于检验反欺诈系统各维度检测能力（正当安全测试专用）

平台方自检工具：模拟已知的广告欺诈模式，用来验证自己的反欺诈系统是否能正确识别。
每个场景都故意制造"已知异常"，输出 golden label（该任务应被判定为欺诈）。

设计原则：
  1. 场景 = { 场景ID, 维度, 预期判定(欺诈/正常), 变异参数, apply(ctx) }
  2. apply() 最小侵入修改 fingerprint/config/task_meta，不改动 app.py 主流程
  3. 每个场景可叠加（组合攻击），用于检验多维度联合检测
  4. 红队报告 = 场景ID + golden label + 注入的异常特征，便于和反欺诈系统结果比对

使用方式：
    from redteam_scenarios import RedTeamScenarioLibrary, apply_scenario_to_task
    rtl = RedTeamScenarioLibrary()
    # 单场景执行
    task = rtl.select_scenario_by_id("RT_FP_IP_MISMATCH")
    ctx = apply_scenario_to_task(base_fingerprint, base_config, base_task_meta, task)
    # 或随机按权重抽取（模拟真实世界攻击分布）
    task = rtl.sample_attack_scenario()
"""
from __future__ import annotations

import json
import os
import random
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, ".risk_state")
os.makedirs(STATE_DIR, exist_ok=True)

_sec = secrets.SystemRandom()

# ============================================================================
# 一、场景分类（红队目标 = 反欺诈系统的检测维度）
# ============================================================================
DIMENSION_FP_IP_CONSISTENCY = "fingerprint_ip_consistency"      # 指纹-IP一致性
DIMENSION_FP_DIVERSITY = "fingerprint_diversity"                 # 指纹重复/瀑布流异常
DIMENSION_BEHAVIOR_STAT = "behavior_statistical"                 # 行为统计异常（停留/滚动/点击）
DIMENSION_CTR_MODEL = "ctr_distribution"                         # CTR/点击模式异常
DIMENSION_IP_CLUSTER = "ip_cluster_and_reuse"                    # IP聚类/复用
DIMENSION_TIME_PATTERN = "temporal_pattern"                      # 时间分布异常
DIMENSION_REFERRER = "referrer_and_traffic_source"               # 来源/Referer异常
DIMENSION_MULTI_DIM = "multi_dimension_correlation"              # 多维度关联异常
DIMENSION_STEALTH_EVASION = "stealth_evasion"                    # 隐蔽规避（对抗型）
DIMENSION_HUMAN_BASELINE = "human_baseline"                      # 真人基线（用于对照）

# ============================================================================
# 二、场景定义
# ============================================================================
@dataclass
class RedTeamScenario:
    scenario_id: str
    dimension: str
    name: str
    description: str
    expected_verdict: str            # "fraud" | "suspicious" | "normal"
    severity: int                    # 1-5，越大越明显/越容易被检测
    weight: float                    # 随机抽取时的权重
    injected_tags: List[str] = field(default_factory=list)  # golden label 标记
    # 场景特有参数
    params: Dict[str, Any] = field(default_factory=dict)


class RedTeamScenarioLibrary:
    """所有红队攻击场景的注册中心。"""

    def __init__(self):
        self._scenarios: Dict[str, RedTeamScenario] = {}
        self._register_all()

    # ------------------------------------------------------------------ utils
    def add(self, s: RedTeamScenario) -> None:
        self._scenarios[s.scenario_id] = s

    def all(self) -> List[RedTeamScenario]:
        return list(self._scenarios.values())

    def get(self, scenario_id: str) -> Optional[RedTeamScenario]:
        return self._scenarios.get(scenario_id)

    def by_dimension(self, dimension: str) -> List[RedTeamScenario]:
        return [s for s in self._scenarios.values() if s.dimension == dimension]

    def sample_attack_scenario(self, baseline_pct: float = 0.05) -> RedTeamScenario:
        """按权重抽取一个场景。baseline_pct 控制真人基线(对照)样本占比。"""
        if random.random() < baseline_pct:
            return self.get("RT_BASELINE_NORMAL")  # type: ignore[return-value]
        attacks = [s for s in self._scenarios.values() if s.expected_verdict != "normal"]
        weights = [s.weight for s in attacks]
        return _sec.choices(attacks, weights=weights, k=1)[0]

    # ---------------------------------------------------------------- register
    def _register_all(self):
        # ===== 真人基线对照（用于评估误报率） =====
        self.add(RedTeamScenario(
            scenario_id="RT_BASELINE_NORMAL",
            dimension=DIMENSION_HUMAN_BASELINE,
            name="真人基线样本",
            description="无任何异常注入，完全按标准模型生成流量。用于评估反欺诈系统误报率（FPR）。",
            expected_verdict="normal",
            severity=0,
            weight=5.0,
            injected_tags=["baseline", "normal_sample"],
        ))

        # ===== DIM 1：指纹-IP一致性 =====
        self.add(RedTeamScenario(
            scenario_id="RT_FP_IP_MISMATCH",
            dimension=DIMENSION_FP_IP_CONSISTENCY,
            name="IP-指纹时空错配",
            description="IP 在美国，但时区=Asia/Shanghai、语言=zh-CN、经纬度=上海。典型的代理+本机环境未改干净。",
            expected_verdict="fraud",
            severity=4,
            weight=4.0,
            injected_tags=["fp_ip_mismatch", "tz_mismatch", "lang_mismatch", "geo_mismatch"],
            params={"swap_tz_to": "Asia/Shanghai", "swap_lang_to": "zh-CN", "swap_geo_to": (31.23, 121.47)},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_FP_SAME_30MIN",
            dimension=DIMENSION_FP_DIVERSITY,
            name="指纹30分钟复用",
            description="30 分钟窗口内同一个指纹(UA+Canvas+WebGL)从 5 个不同国家 IP 发起请求。典型指纹池用完的症状。",
            expected_verdict="fraud",
            severity=4,
            weight=3.5,
            injected_tags=["fp_reuse", "cross_country_fp", "fingerprint_waterfall"],
            params={"reuse_countries": ["US", "DE", "JP", "IN", "BR"], "window_min": 30},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_FP_UA_TOO_OLD",
            dimension=DIMENSION_FP_DIVERSITY,
            name="UA版本异常老旧",
            description="UA 固定 Chrome/50 (2016年)，但硬件 concurrency=16、device_memory=32GB。版本-硬件代际错配。",
            expected_verdict="suspicious",
            severity=3,
            weight=2.0,
            injected_tags=["ua_ancient", "ua_hw_mismatch", "generational_mismatch"],
            params={
                "old_ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36",
                "hw_override": {"hardware_concurrency": 16, "device_memory": 32},
            },
        ))

        # ===== DIM 2：行为统计异常 =====
        self.add(RedTeamScenario(
            scenario_id="RT_BHV_STAY_TOO_SHORT",
            dimension=DIMENSION_BEHAVIOR_STAT,
            name="停留时长极端偏低",
            description="所有页面停留 < 3 秒，无滚动无鼠标移动。典型机器点击器 symptom。",
            expected_verdict="fraud",
            severity=5,
            weight=3.0,
            injected_tags=["stay_too_short", "no_scroll", "no_mouse_move", "bounce_100pct"],
            params={"stay_override": (1.0, 3.0), "disable_scroll": True, "disable_mouse": True},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_BHV_CLICK_INSTANT",
            dimension=DIMENSION_CTR_MODEL,
            name="页面加载立刻点击广告",
            description="进入页面 < 1 秒即点击广告（真人至少需要几秒阅读反应）。违反 8 秒 rule。",
            expected_verdict="fraud",
            severity=5,
            weight=3.0,
            injected_tags=["instant_ad_click", "click_before_read", "violate_8s_rule"],
            params={"pre_click_wait_max": 1.0, "force_click_prob": 1.0},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_BHV_CTR_ABNORMAL",
            dimension=DIMENSION_CTR_MODEL,
            name="点击率严重偏高 CTR>20%",
            description="自然 CTR 通常 0.1%-2%。本场景强制 CTR=25%，且每次点击都落在同一坐标。",
            expected_verdict="fraud",
            severity=4,
            weight=2.5,
            injected_tags=["ctr_abnormal_high", "fixed_click_position", "ctr_25pct"],
            params={"ctr_override": 0.25, "fixed_click_offset": (5, 5)},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_BHV_PERFECT_UNIFORM",
            dimension=DIMENSION_BEHAVIOR_STAT,
            name="行为过度均匀(机器特征)",
            description="每次鼠标移动 200ms、每次滚动 400px、每次停留 15s — 所有参数均匀分布(缺乏方差)。",
            expected_verdict="suspicious",
            severity=3,
            weight=2.5,
            injected_tags=["uniform_distribution", "no_variance", "math_trap"],
            params={"disable_lognormal": True, "fixed_mouse_ms": 200, "fixed_scroll_px": 400, "fixed_stay_s": 15},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_BHV_NO_RANDOMNESS",
            dimension=DIMENSION_BEHAVIOR_STAT,
            name="鼠标轨迹无生理微颤",
            description="贝塞尔曲线的速度=常数、无 8-12Hz 微颤叠加、无犹豫停留。反ML特征。",
            expected_verdict="suspicious",
            severity=2,
            weight=2.0,
            injected_tags=["no_physiological_tremor", "constant_velocity", "smooth_trap"],
            params={"disable_tremor": True, "velocity_profile": "constant"},
        ))

        # ===== DIM 3：IP 聚类 / 复用 =====
        self.add(RedTeamScenario(
            scenario_id="RT_IP_SAME_C_SEGMENT",
            dimension=DIMENSION_IP_CLUSTER,
            name="C段高频访问",
            description="同一 /24 子网在 1 小时内产生 50+ 次访问。典型代理出口共享/小鸡池。",
            expected_verdict="fraud",
            severity=4,
            weight=3.0,
            injected_tags=["same_c_segment_burst", "subnet_cluster", "proxy_pool"],
            params={"c_segment_burst_threshold": 50, "burst_window_min": 60},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_IP_REUSE_1HR",
            dimension=DIMENSION_IP_CLUSTER,
            name="同IP 1小时重复30次",
            description="同一个住宅代理IP在1小时内被30个会话反复使用（正常住宅 IP 1小时约 1-3 会话）。",
            expected_verdict="fraud",
            severity=5,
            weight=3.0,
            injected_tags=["ip_high_reuse", "short_window_reuse", "violate_24h_rule"],
            params={"ip_reuse_count": 30, "reuse_window_min": 60},
        ))

        # ===== DIM 4：时间分布异常 =====
        self.add(RedTeamScenario(
            scenario_id="RT_TIME_3AM_BURST",
            dimension=DIMENSION_TIME_PATTERN,
            name="凌晨时段集中访问",
            description="所有会话都发生在目标用户国家当地时间 03:00-05:00（正常人极少活跃）。",
            expected_verdict="suspicious",
            severity=3,
            weight=2.5,
            injected_tags=["3am_burst", "anti_working_hours", "temporal_anomaly"],
            params={"force_hour_range": (3, 5)},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_TIME_UNIFORM_24H",
            dimension=DIMENSION_TIME_PATTERN,
            name="24小时均匀分布(无人睡觉)",
            description="全天每小时访问数完全均匀（±2%）。真人分布一定有明显白天波峰、夜间波谷。",
            expected_verdict="suspicious",
            severity=3,
            weight=2.0,
            injected_tags=["uniform_24h", "no_daily_pattern", "no_sleep_pattern"],
            params={"uniform_24h": True, "variance_pct": 2},
        ))

        # ===== DIM 5：来源/Referer =====
        self.add(RedTeamScenario(
            scenario_id="RT_REF_100PCT_DIRECT",
            dimension=DIMENSION_REFERRER,
            name="100%直接访问",
            description="全部会话 Referer 为空，无搜索无外链无社媒。新站不可能出现。",
            expected_verdict="fraud",
            severity=4,
            weight=3.0,
            injected_tags=["100pct_direct", "no_referer", "empty_referrer_only"],
            params={"force_direct_pct": 1.0, "disable_search": True, "disable_social": True},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_REF_100PCT_SEARCH",
            dimension=DIMENSION_REFERRER,
            name="100% 搜索来源",
            description="全部访问都来自 Google 搜索同一个关键词，且点击排名相同位置。",
            expected_verdict="suspicious",
            severity=3,
            weight=2.5,
            injected_tags=["100pct_search", "single_keyword", "same_srp_position"],
            params={"search_pct_override": 1.0, "keyword_fixed": "freestory novels", "srp_position_fixed": 3},
        ))

        # ===== DIM 6：多维度关联异常（最容易漏检） =====
        self.add(RedTeamScenario(
            scenario_id="RT_MULTI_COMBO_A",
            dimension=DIMENSION_MULTI_DIM,
            name="经典低水平组合攻击：短停留+立刻点击+空Referer",
            description="停留<3s + 立刻点击 + 空Referer 组合出现。反欺诈系统应多特征联合判定。",
            expected_verdict="fraud",
            severity=5,
            weight=4.0,
            injected_tags=["combo_attack", "short_stay", "instant_click", "empty_referer"],
            params={
                "stay_override": (1.0, 2.0),
                "pre_click_wait_max": 0.5,
                "force_click_prob": 1.0,
                "force_direct_pct": 1.0,
            },
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_MULTI_COMBO_B",
            dimension=DIMENSION_MULTI_DIM,
            name="高级组合：指纹复用+IP聚类+短停留（模拟中型代理池刷量）",
            description="同一指纹跨国家复用 + C段IP聚集 + 短停留，多维度联合出现，需综合检测器识别。",
            expected_verdict="fraud",
            severity=5,
            weight=3.5,
            injected_tags=["combo_attack", "fp_reuse", "same_c_segment", "short_stay"],
            params={
                "reuse_countries": ["US", "GB", "CA"],
                "window_min": 30,
                "c_segment_burst_threshold": 20,
                "burst_window_min": 60,
                "stay_override": (2.0, 6.0),
            },
        ))

        # ===== DIM 7：隐蔽规避型（高级攻击，模拟"看起来像真人"的刷量者） =====
        self.add(RedTeamScenario(
            scenario_id="RT_STEALTH_SLOW_BURN",
            dimension=DIMENSION_STEALTH_EVASION,
            name="慢烧型规避：每个维度都正常，但CTR稳定偏高1.5%",
            description="所有单维检测都正常（IP/指纹/行为/时间/来源全部过阈值），只有 CTR 长期稳定在正常值的 3 倍。需 CTR 基线长期检测才能抓到。",
            expected_verdict="fraud",
            severity=2,
            weight=1.5,
            injected_tags=["stealth", "slow_burn", "ctr_drift", "single_dim_anomaly_only"],
            params={"ctr_override": 0.045, "all_other_clean": True},
        ))
        self.add(RedTeamScenario(
            scenario_id="RT_STEALTH_BEHAVIOR_CLOSE",
            dimension=DIMENSION_STEALTH_EVASION,
            name="行为极其一致：脚本化刷量者(按台词重复)",
            description="每个会话滚动深度完全相同(83%)、停留时间 ±0.5s、点击广告位置偏差 < 5px — 单看正常，但跨会话聚类为同一行为脚本。",
            expected_verdict="suspicious",
            severity=2,
            weight=1.5,
            injected_tags=["stealth", "behavior_scripted", "cluster_same_behavior", "exact_repeat"],
            params={"scroll_depth_fixed_pct": 83, "stay_std_dev_s": 0.5, "click_pos_std_px": 5},
        ))


# ============================================================================
# 三、场景应用器：把 Scenario 的异常注入到 指纹 / 配置 / 任务元数据
# ============================================================================
@dataclass
class RedTeamAppliedContext:
    """场景应用后的上下文，供 app.py 执行任务时使用。"""
    scenario: RedTeamScenario
    fingerprint_overrides: Dict[str, Any] = field(default_factory=dict)
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    task_meta_tags: Dict[str, Any] = field(default_factory=dict)  # golden label

    def golden_label_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario.scenario_id,
            "dimension": self.scenario.dimension,
            "expected_verdict": self.scenario.expected_verdict,
            "severity": self.scenario.severity,
            "injected_tags": self.scenario.injected_tags,
            **self.task_meta_tags,
        }


def apply_scenario_to_task(
    base_fingerprint: Optional[Dict[str, Any]],
    base_config: Optional[Dict[str, Any]],
    base_task_meta: Optional[Dict[str, Any]],
    scenario: RedTeamScenario,
    *,
    rng=None,
) -> RedTeamAppliedContext:
    """把场景的异常特征注入到上下文。最小侵入，不修改 base 参数本身。"""
    rng = rng or _sec
    fp_over: Dict[str, Any] = {}
    cfg_over: Dict[str, Any] = {}
    meta: Dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "run_id": uuid.uuid4().hex[:12],
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    p = scenario.params

    sid = scenario.scenario_id

    # ---- DIM 1：指纹-IP一致性 ----
    if sid == "RT_FP_IP_MISMATCH":
        fp_over["timezone"] = p.get("swap_tz_to", "Asia/Shanghai")
        fp_over["language"] = p.get("swap_lang_to", "zh-CN")
        lat, lon = p.get("swap_geo_to", (31.23, 121.47))
        meta["force_geolocation"] = {"latitude": lat, "longitude": lon}

    # ---- DIM 1b：指纹复用 ----
    elif sid == "RT_FP_SAME_30MIN":
        # 固定 fingerprint 共享 key（app.py 侧按 scenario_id 命中时复用）
        meta["shared_fp_pool_key"] = f"rt30min_{scenario.scenario_id}"
        fp_over["fingerprint_id"] = f"REUSE-{uuid.uuid4().hex[:8]}"
        meta["reuse_window_countries"] = p["reuse_countries"]

    elif sid == "RT_FP_UA_TOO_OLD":
        fp_over["user_agent"] = p["old_ua"]
        fp_over["hardware_concurrency"] = p["hw_override"]["hardware_concurrency"]
        fp_over["device_memory"] = p["hw_override"]["device_memory"]

    # ---- DIM 2：行为异常 ----
    elif sid == "RT_BHV_STAY_TOO_SHORT":
        mn, mx = p["stay_override"]
        cfg_over["total_stay"] = {"min": mn, "max": mx}
        cfg_over["scroll_pixels"] = {"min": 0, "max": 0}
        cfg_over["scroll_count"] = {"min": 0, "max": 0}
        cfg_over["mouse_move_count"] = {"min": 0, "max": 0}
        cfg_over["random_click_count"] = {"min": 0, "max": 0}

    elif sid == "RT_BHV_CLICK_INSTANT":
        cfg_over["ad_click"] = {
            "enabled": True,
            "min_page_stay_before_click_sec": 0,  # 违反 8s rule
            "max_page_stay_before_click_sec": p["pre_click_wait_max"],
            "force_click_probability": p["force_click_prob"],
            "probability": p["force_click_prob"],
        }

    elif sid == "RT_BHV_CTR_ABNORMAL":
        cfg_over["ad_click"] = {
            "enabled": True,
            "probability": p["ctr_override"],
            "min_page_stay_before_click_sec": 3,
            "fixed_click_offset": p["fixed_click_offset"],
        }

    elif sid == "RT_BHV_PERFECT_UNIFORM":
        meta["disable_random_distributions"] = True
        cfg_over["scroll_pixels"] = {"min": p["fixed_scroll_px"], "max": p["fixed_scroll_px"]}
        cfg_over["scroll_count"] = {"min": 4, "max": 4}
        cfg_over["mouse_move_count"] = {"min": 6, "max": 6}
        cfg_over["mouse_move_steps"] = {"min": 100, "max": 100}
        cfg_over["mouse_move_wait"] = {"min": p["fixed_mouse_ms"] / 1000.0, "max": p["fixed_mouse_ms"] / 1000.0}
        cfg_over["total_stay"] = {"min": p["fixed_stay_s"], "max": p["fixed_stay_s"]}

    elif sid == "RT_BHV_NO_RANDOMNESS":
        meta["disable_mouse_tremor"] = True
        meta["disable_bezier_randomness"] = True
        meta["mouse_velocity_profile"] = p["velocity_profile"]

    # ---- DIM 3：IP 聚类 / 复用 ----
    elif sid == "RT_IP_SAME_C_SEGMENT":
        meta["ip_cluster_burst"] = True
        meta["c_segment_burst_threshold"] = p["c_segment_burst_threshold"]
        meta["burst_window_min"] = p["burst_window_min"]

    elif sid == "RT_IP_REUSE_1HR":
        meta["ip_reuse_burst"] = True
        meta["ip_reuse_count"] = p["ip_reuse_count"]
        meta["reuse_window_min"] = p["reuse_window_min"]

    # ---- DIM 4：时间分布 ----
    elif sid == "RT_TIME_3AM_BURST":
        meta["force_schedule_hour_range"] = p["force_hour_range"]

    elif sid == "RT_TIME_UNIFORM_24H":
        meta["force_uniform_hours"] = True
        meta["variance_pct"] = p["variance_pct"]

    # ---- DIM 5：Referer ----
    elif sid == "RT_REF_100PCT_DIRECT":
        cfg_over["traffic_diversity"] = {
            "enabled": True,
            "search_pct": 0.0,
            "direct_pct": 1.0,
            "social_pct": 0.0,
        }

    elif sid == "RT_REF_100PCT_SEARCH":
        cfg_over["traffic_diversity"] = {
            "enabled": True,
            "search_pct": p["search_pct_override"],
            "direct_pct": 0.0,
            "social_pct": 0.0,
        }
        cfg_over["seo"] = {
            "search_mode": "real_search",
            "keyword_pool": [p["keyword_fixed"]],
            "srp_position_fixed": p.get("srp_position_fixed"),
        }

    # ---- DIM 6：多维度组合 ----
    elif sid == "RT_MULTI_COMBO_A":
        mn, mx = p["stay_override"]
        cfg_over["total_stay"] = {"min": mn, "max": mx}
        cfg_over["ad_click"] = {
            "enabled": True,
            "min_page_stay_before_click_sec": 0,
            "max_page_stay_before_click_sec": p["pre_click_wait_max"],
            "probability": p["force_click_prob"],
            "force_click_probability": p["force_click_prob"],
        }
        cfg_over["traffic_diversity"] = {
            "enabled": True, "search_pct": 0.0, "direct_pct": p["force_direct_pct"], "social_pct": 0.0,
        }

    elif sid == "RT_MULTI_COMBO_B":
        meta["shared_fp_pool_key"] = f"rtcomboB_{scenario.scenario_id}"
        meta["ip_cluster_burst"] = True
        meta["c_segment_burst_threshold"] = p["c_segment_burst_threshold"]
        mn, mx = p["stay_override"]
        cfg_over["total_stay"] = {"min": mn, "max": mx}

    # ---- DIM 7：隐蔽规避 ----
    elif sid == "RT_STEALTH_SLOW_BURN":
        cfg_over["ad_click"] = {
            "enabled": True,
            "probability": p["ctr_override"],
            "min_page_stay_before_click_sec": 10,
        }
        meta["all_other_clean"] = True

    elif sid == "RT_STEALTH_BEHAVIOR_CLOSE":
        meta["scroll_depth_fixed_pct"] = p["scroll_depth_fixed_pct"]
        meta["stay_std_dev_s"] = p["stay_std_dev_s"]
        meta["click_pos_std_px"] = p["click_pos_std_px"]

    # ---- BASELINE：不做任何注入 ----
    elif sid == "RT_BASELINE_NORMAL":
        meta["baseline"] = True

    return RedTeamAppliedContext(
        scenario=scenario,
        fingerprint_overrides=fp_over,
        config_overrides=cfg_over,
        task_meta_tags=meta,
    )


# ============================================================================
# 四、便捷辅助函数
# ============================================================================
def merge_dicts_shallow(base: Optional[Dict], overrides: Dict) -> Dict:
    """浅合并（app.py 中应用 fingerprint/config override 用）。"""
    result = dict(base or {})
    result.update(overrides)
    return result


def print_scenario_summary(rtl: RedTeamScenarioLibrary) -> None:
    """打印场景清单摘要（调试用）。"""
    dims: Dict[str, int] = {}
    for s in rtl.all():
        dims[s.dimension] = dims.get(s.dimension, 0) + 1
    print(f"[RedTeam] 共注册 {len(rtl.all())} 个场景，分布：")
    for d, c in sorted(dims.items()):
        print(f"  · {d}: {c}")


if __name__ == "__main__":
    rtl = RedTeamScenarioLibrary()
    print_scenario_summary(rtl)
    print()
    print("[RedTeam] 随机抽取 3 个场景演示：")
    for i in range(3):
        s = rtl.sample_attack_scenario(baseline_pct=0.0)
        ctx = apply_scenario_to_task({}, {}, {}, s)
        print(f"  #{i+1} [{s.scenario_id}] {s.name} -> verdict={s.expected_verdict}")
        if ctx.fingerprint_overrides:
            print(f"      FP overrides: {list(ctx.fingerprint_overrides.keys())}")
        if ctx.config_overrides:
            print(f"      Config overrides: {list(ctx.config_overrides.keys())}")
        print(f"      Golden tags: {s.injected_tags}")
