"""
红队框架接入层 — 把 redteam_scenarios / redteam_reporter / traffic_distribution
三个新增模块以"最小侵入"方式接入到现有 app.py / 任务主流程。

用法（两种模式）：
  模式 A：在 app.py 顶部本模块 import 一次，然后在"生成任务前"、"生成指纹后"、
         "创建浏览器 context 前"、"任务结束后"四个关键节点调用本模块提供的 hook。
         —— 不改 app.py 核心代码，仅在关键节点插入 ≤ 5 行。
  模式 B：独立运行本脚本进行"干跑演示"——执行一批红队场景任务（用 mock 浏览器），
         验证场景注入 + golden label 报告 + 评估指标计算全流程打通。

关键 Hook：
  1. redteam_before_task(config, candidates_countries)
       → 返回 (scenario, applied_ctx, recorder) 供主流程贯穿使用
  2. redteam_apply_fp_override(fingerprint_dict, applied_ctx)
       → 合并 scenario 注入的 fingerprint 覆盖
  3. redteam_apply_config_override(config_dict, applied_ctx)
       → 合并 scenario 注入的 config 覆盖
  4. redteam_on_session_start(recorder, session_meta_dict)
       → 记录 IP/UA/指纹/时区/语言/Referer 等
  5. redteam_on_session_end(recorder, behavior_dict, *, success, error)
       → 写 golden label JSONL
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# 允许从任何 cwd import
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from redteam_scenarios import (
    RedTeamAppliedContext,
    RedTeamScenario,
    RedTeamScenarioLibrary,
    apply_scenario_to_task,
    merge_dicts_shallow,
)
from redteam_reporter import (
    RedTeamRecorder,
    evaluate_golden_vs_system,
    print_evaluation_report,
)
from traffic_distribution import (
    weighted_country_sample,
    traffic_day_scale,
    weighted_local_hours,
)

_RTL = RedTeamScenarioLibrary()


# ============================================================================
# Hook 函数（供 app.py 调用；全部函数都带 try/except 确保不影响主流程）
# ============================================================================
def redteam_before_task(
    base_config: Optional[Dict[str, Any]],
    candidate_countries,
    *,
    scenario_id: Optional[str] = None,
    baseline_pct: float = 0.05,
    force_country_weighted: bool = True,
) -> Tuple[Optional[RedTeamScenario], RedTeamAppliedContext, RedTeamRecorder, str]:
    """
    主流程"生成任务前"调用。返回：
      scenario, applied_ctx, recorder, selected_country

    若 scenario_id 传入则精确命中该场景；否则按权重抽取（含一定比例基线正常样本）。
    """
    try:
        # --- 先按人口权重选国家 ---
        cc_list = list(candidate_countries)
        if force_country_weighted and cc_list:
            try:
                selected_cc = weighted_country_sample(cc_list)
            except Exception:
                import random as _r
                selected_cc = _r.choice(cc_list)
        else:
            import random as _r
            selected_cc = _r.choice(cc_list) if cc_list else "US"

        # --- 抽红队场景 ---
        if scenario_id:
            scenario = _RTL.get(scenario_id) or _RTL.get("RT_BASELINE_NORMAL")
        else:
            scenario = _RTL.sample_attack_scenario(baseline_pct=baseline_pct)
        assert scenario is not None

        applied_ctx = apply_scenario_to_task({}, dict(base_config or {}), {}, scenario)
        recorder = RedTeamRecorder(applied_ctx)

        # 若该场景强制时间段，写入 task_meta（供调度器读取）
        meta = applied_ctx.task_meta_tags
        if "force_schedule_hour_range" in meta:
            import random as _r
            h1, h2 = meta["force_schedule_hour_range"]
            meta["scheduled_local_hour"] = round(_r.uniform(h1, h2), 2)
        elif meta.get("force_uniform_hours"):
            import random as _r
            meta["scheduled_local_hour"] = round(_r.uniform(0.0, 24.0), 2)
        else:
            # 按该国典型曲线采样 1 个小时槽
            hrs = weighted_local_hours(selected_cc, 1)
            if hrs:
                meta["scheduled_local_hour"] = hrs[0]

        return scenario, applied_ctx, recorder, selected_cc
    except Exception as _e:
        # 任何红队模块错误都不能影响主流程 —— 回退到 baseline 空场景
        print(f"[RedTeam][WARN] before_task fallback to baseline: {_e}")
        scenario = _RTL.get("RT_BASELINE_NORMAL")
        assert scenario is not None
        applied_ctx = apply_scenario_to_task({}, {}, {}, scenario)
        recorder = RedTeamRecorder(applied_ctx)
        cc = (list(candidate_countries) if hasattr(candidate_countries, "__iter__") and not isinstance(candidate_countries, str) else ["US"])[0] if candidate_countries else "US"
        return scenario, applied_ctx, recorder, cc


def redteam_apply_fp_override(
    fingerprint_dict: Optional[Dict[str, Any]],
    applied_ctx: RedTeamAppliedContext,
) -> Dict[str, Any]:
    """把红队场景的 fingerprint overrides 合并到 generate_fingerprint() 的输出。"""
    if not applied_ctx or not applied_ctx.fingerprint_overrides:
        return fingerprint_dict or {}
    try:
        return merge_dicts_shallow(fingerprint_dict, applied_ctx.fingerprint_overrides)
    except Exception as _e:
        print(f"[RedTeam][WARN] apply_fp_override skip: {_e}")
        return fingerprint_dict or {}


def redteam_apply_config_override(
    config_dict: Optional[Dict[str, Any]],
    applied_ctx: RedTeamAppliedContext,
) -> Dict[str, Any]:
    """把红队场景的 config overrides（停留/点击/流量多样化等）合并到任务 config。

    策略：深合并，字典不覆盖而递归合并；列表/标量直接覆盖。
    """
    if not applied_ctx or not applied_ctx.config_overrides:
        return config_dict or {}
    try:
        return _deep_merge(config_dict or {}, applied_ctx.config_overrides)
    except Exception as _e:
        print(f"[RedTeam][WARN] apply_config_override skip: {_e}")
        return config_dict or {}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def redteam_on_session_start(
    recorder: RedTeamRecorder,
    session_meta: Dict[str, Any],
) -> None:
    """context 创建完成、获得 IP/UA/指纹后调用。"""
    try:
        recorder.set_session_meta(**session_meta)
    except Exception as _e:
        print(f"[RedTeam][WARN] on_session_start skip: {_e}")


def redteam_on_session_end(
    recorder: RedTeamRecorder,
    behavior: Optional[Dict[str, Any]] = None,
    *,
    success: bool = True,
    error: Optional[str] = None,
):
    """任务结束时调用 — 落盘 golden label JSONL。返回保存的任务记录。"""
    try:
        if behavior:
            recorder.set_behavior(**behavior)
        return recorder.finish(success=success, error=error)
    except Exception as _e:
        print(f"[RedTeam][WARN] on_session_end persist fail: {_e}")
        return None


# ============================================================================
# 独立运行模式（干跑演示）：用 mock 会话跑一批场景，验证全流程
# ============================================================================
def dry_run_demo(task_count: int = 50):
    import random
    print(f"[RedTeamDemo] 干跑 {task_count} 个红队任务（mock 浏览器/会话）...")
    print()

    candidate_ccs = ["US", "CN", "JP", "IN", "DE", "BR", "GB", "ID", "KR", "FR", "CA", "AU"]
    records = []
    for idx in range(task_count):
        # ---- Hook 1: before_task ----
        scenario, applied_ctx, recorder, cc = redteam_before_task(
            base_config={
                "traffic_diversity": {"enabled": True},
                "ad_click": {"enabled": True, "probability": 0.015, "min_page_stay_before_click_sec": 8},
            },
            candidate_countries=candidate_ccs,
            baseline_pct=0.10,  # 10% 基线样本（用于测 FPR）
        )
        # ---- 生成基础指纹 ----
        fp = {
            "fingerprint_id": uuid.uuid4().hex,
            "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/{random.randint(120,130)}.0 Safari/537.36",
            "resolution": "1920x1080",
            "language": "en-US",
            "timezone": "America/New_York" if cc == "US" else "Asia/Shanghai",
            "platform": "Windows",
            "hardware_concurrency": random.choice([4, 8, 12, 16]),
            "device_memory": random.choice([4, 8, 16, 32]),
        }
        fp = redteam_apply_fp_override(fp, applied_ctx)
        cfg = redteam_apply_config_override({}, applied_ctx)

        # ---- mock 执行 ----
        # 读取场景参数，决定 mock 行为
        meta = applied_ctx.task_meta_tags
        stay = random.uniform(20, 80)
        impr = random.randint(1, 4)
        clicks = 0
        pre_click_wait = None
        clicks_cfg = cfg.get("ad_click", {}) or {}
        ctr = float(clicks_cfg.get("probability") or 0.015)
        if random.random() < ctr:
            clicks = 1
            mn = float(clicks_cfg.get("min_page_stay_before_click_sec", 3))
            mx = float(clicks_cfg.get("max_page_stay_before_click_sec", max(mn + 2, 5)))
            pre_click_wait = round(random.uniform(mn, mx), 2)

        # mock: 异常场景注入的"异常特征"影响 mock 数值
        if "stay_override" in scenario.params:
            smn, smx = scenario.params["stay_override"]
            stay = random.uniform(smn, smx)

        # ---- Hook 3/4: 记录 ----
        traffic_source = (cfg.get("traffic_diversity") or {}).get("search_pct", 0.6)
        traffic_src = "search" if random.random() < traffic_source else ("direct" if random.random() < 0.5 else "social")
        redteam_on_session_start(
            recorder,
            {
                "ip": f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                "country_code": cc,
                "asn": f"AS{random.randint(1,70000)}",
                "user_agent": fp["user_agent"],
                "fingerprint_id": fp["fingerprint_id"],
                "timezone_id": fp["timezone"],
                "language": fp["language"],
                "platform": fp["platform"],
                "referer": "" if traffic_src == "direct" else "https://www.google.com/",
                "traffic_source": traffic_src,
            },
        )
        rec = redteam_on_session_end(
            recorder,
            behavior={
                "total_stay_sec": round(stay, 2),
                "pages_viewed": random.randint(1, 5),
                "scroll_max_depth_pct": round(random.uniform(15, 95), 1),
                "ad_impressions": impr,
                "ad_clicks": clicks,
                "page_stay_before_first_click_sec": pre_click_wait,
            },
            success=True,
        )
        records.append(rec)
        if idx % 10 == 0 or idx == task_count - 1:
            print(f"  任务 {idx+1:03d}/{task_count} | run_id={rec.run_id} | {rec.scenario_id[:28]:<28s} | golden={rec.expected_verdict} | country={rec.country_code} | stay={rec.total_stay_sec}s")
        # 按场景内的日程微调停顿
        time.sleep(0.01)

    print()
    # ---- 统计：各场景执行次数 ----
    from collections import Counter
    scn_cnt = Counter(r.scenario_id for r in records)
    vrd_cnt = Counter(r.expected_verdict for r in records)
    cc_cnt = Counter(r.country_code for r in records)
    print(f"[RedTeamDemo] 完成 {len(records)} 个任务:")
    print(f"  · 判定分布: 基线(正常)={vrd_cnt.get('normal',0)} 欺诈={vrd_cnt.get('fraud',0)} 可疑={vrd_cnt.get('suspicious',0)}")
    print(f"  · 国家分布 Top: {cc_cnt.most_common(5)}")
    print(f"  · 场景覆盖: {len(scn_cnt)} / {len(_RTL.all())} 个场景命中")

    # ---- 演示评估（用简单策略模拟反欺诈系统判定回填） ----
    # 回填策略：欺诈场景 severity>=4 → 90% 系统抓中；severity<=2 → 50%抓中；基线 10%误报
    path = records[0] and os.path.join(BASE_DIR, "reports", "redteam", f"redteam_golden_{time.strftime('%Y%m%d')}.jsonl")
    if path and os.path.exists(path):
        import json
        lines = open(path, "r", encoding="utf-8").readlines()
        # 用"预期 severity 高则系统更可能识别"的简单规则做模拟回填（仅演示用）
        # 实际应调用您自己反欺诈系统的 API 回填 system_verdict
        updated = []
        for line in lines:
            try:
                r = json.loads(line)
                sev = int(r.get("severity") or 0)
                golden = r.get("expected_verdict")
                if golden == "normal":
                    r["system_verdict"] = "fraud" if random.random() < 0.10 else "normal"
                else:
                    p = {0: 0.0, 1: 0.4, 2: 0.55, 3: 0.75, 4: 0.9, 5: 0.98}.get(sev, 0.5)
                    r["system_verdict"] = "fraud" if random.random() < p else "normal"
                updated.append(r)
            except Exception:
                updated.append(None)
        with open(path, "w", encoding="utf-8") as f:
            for r in updated:
                if r:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("\n[RedTeamDemo] 已用模拟反欺诈策略回填 system_verdict，评估结果：")
        ev = evaluate_golden_vs_system()
        print_evaluation_report(ev)


if __name__ == "__main__":
    # 允许命令行参数： --dry N  或  --eval
    args = sys.argv[1:]
    if "--eval" in args:
        ev = evaluate_golden_vs_system()
        print_evaluation_report(ev)
    else:
        n = 30
        for i, a in enumerate(args):
            if a == "--dry" and i + 1 < len(args):
                try:
                    n = int(args[i + 1])
                except Exception:
                    pass
        dry_run_demo(n)
