"""
红队 Golden Label 报告模块 — 反欺诈系统效果评估专用（正当安全测试）

用于：在每个红队任务结束时，输出一份结构化 JSONL 报告，包含：
  1. 注入的攻击场景、预期判定（golden label）
  2. 任务实际元数据（IP、国家、UA、停留、点击、Referer 等）
  3. 唯一 run_id（供反欺诈系统查询对应 request_id 做关联）

反欺诈系统评估指标：
  - TPR（召回率）= 反欺诈系统判定 fraud / 我们 golden label=fraud 的总数
  - FPR（误报率）= 反欺诈系统判定 fraud / 我们 golden label=normal 的总数
  - F1 = 2*Precision*Recall / (Precision+Recall)

与 risk_check.py / risk_control_enhancements.py 配合使用：
  红队报告 + 风险检查结果 = 完整效果评估闭环。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from redteam_scenarios import RedTeamAppliedContext, RedTeamScenario

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports", "redteam")
os.makedirs(REPORT_DIR, exist_ok=True)

# 每日滚动 JSONL（一天一份，便于和反欺诈系统按日对账）
def _daily_report_path() -> str:
    return os.path.join(REPORT_DIR, f"redteam_golden_{datetime.now().strftime('%Y%m%d')}.jsonl")


# ============================================================================
# 一、任务记录定义
# ============================================================================
@dataclass
class RedTeamTaskRecord:
    """单次红队任务的完整记录。"""
    run_id: str
    scenario_id: str
    dimension: str
    expected_verdict: str            # fraud | suspicious | normal
    severity: int
    injected_tags: List[str]

    # 会话实际元数据（任务完成后填充）
    ip: Optional[str] = None
    country_code: Optional[str] = None
    asn: Optional[str] = None
    user_agent: Optional[str] = None
    fingerprint_id: Optional[str] = None
    timezone_id: Optional[str] = None
    language: Optional[str] = None
    platform: Optional[str] = None
    referer: Optional[str] = None
    traffic_source: Optional[str] = None   # search | direct | social | referral
    session_start: Optional[str] = None
    session_end: Optional[str] = None
    total_stay_sec: Optional[float] = None
    pages_viewed: Optional[int] = None
    scroll_max_depth_pct: Optional[float] = None
    ad_impressions: Optional[int] = None
    ad_clicks: Optional[int] = None
    click_to_impression_ratio: Optional[float] = None
    page_stay_before_first_click_sec: Optional[float] = None

    # 自定义注入的异常特征（用于反欺诈对账）
    anomaly_features: Dict[str, Any] = field(default_factory=dict)

    # 最终状态
    success: Optional[bool] = None
    error: Optional[str] = None

    # ---- 评估辅助（由反欺诈系统回填，本模块仅提供占位字段）----
    system_verdict: Optional[str] = None   # 反欺诈系统判定
    system_score: Optional[float] = None   # 反欺诈系统打分
    system_detected_tags: List[str] = field(default_factory=list)
    evaluation_match: Optional[bool] = None

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# 二、Recorder：贯穿任务生命周期的记录器
# ============================================================================
class RedTeamRecorder:
    """
    使用流程：
        recorder = RedTeamRecorder(ctx)        # 任务开始前
        recorder.set_session_meta(ip=..., country=..., ua=..., ...)
        ... 执行任务 ...
        recorder.set_behavior(stay=60.3, pages=3, scroll=72, impressions=2, clicks=0, ...)
        recorder.finish(success=True)
    """

    def __init__(self, applied_ctx: RedTeamAppliedContext):
        self.ctx = applied_ctx
        self.record = RedTeamTaskRecord(
            run_id=applied_ctx.task_meta_tags.get("run_id", uuid.uuid4().hex[:12]),
            scenario_id=applied_ctx.scenario.scenario_id,
            dimension=applied_ctx.scenario.dimension,
            expected_verdict=applied_ctx.scenario.expected_verdict,
            severity=applied_ctx.scenario.severity,
            injected_tags=list(applied_ctx.scenario.injected_tags),
            anomaly_features=dict(applied_ctx.task_meta_tags),
        )

    # -------- session meta --------
    def set_session_meta(self, **kw: Any) -> None:
        for k, v in kw.items():
            if not hasattr(self.record, k):
                continue
            setattr(self.record, k, v)
        if "session_start" not in kw and not self.record.session_start:
            self.record.session_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -------- behavior / ad --------
    def set_behavior(self, **kw: Any) -> None:
        for k, v in kw.items():
            if hasattr(self.record, k):
                setattr(self.record, k, v)
        # 自动计算 CTR
        impr = self.record.ad_impressions
        clicks = self.record.ad_clicks
        if impr and clicks is not None:
            try:
                self.record.click_to_impression_ratio = round(clicks / impr, 4)
            except Exception:
                pass

    # -------- finish --------
    def finish(self, success: bool = True, error: Optional[str] = None) -> RedTeamTaskRecord:
        self.record.success = success
        self.record.error = error
        if not self.record.session_end:
            self.record.session_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 计算会话时长（自动，若未提供）
        if self.record.total_stay_sec is None and self.record.session_start and self.record.session_end:
            try:
                s = datetime.strptime(self.record.session_start, "%Y-%m-%d %H:%M:%S")
                e = datetime.strptime(self.record.session_end, "%Y-%m-%d %H:%M:%S")
                self.record.total_stay_sec = round((e - s).total_seconds(), 2)
            except Exception:
                pass
        self._persist()
        return self.record

    # -------- persist --------
    def _persist(self) -> None:
        path = _daily_report_path()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.record.to_json(), ensure_ascii=False) + "\n")
        except Exception as _e:
            print(f"[RedTeam][WARN] golden report persist fail: {_e}")


# ============================================================================
# 三、报告汇总分析（每日/每批次执行后，计算反欺诈系统效果指标）
# ============================================================================
@dataclass
class RedTeamEvaluation:
    """反欺诈系统效果评估结果。"""
    date_str: str
    total_tasks: int
    baseline_count: int
    fraud_count: int
    suspicious_count: int
    tpr: Optional[float] = None      # fraud 召回率
    fpr: Optional[float] = None      # 误报率
    precision: Optional[float] = None
    f1: Optional[float] = None
    dimension_recall: Dict[str, float] = field(default_factory=dict)
    scenario_recall: Dict[str, float] = field(default_factory=dict)
    tag_recall: Dict[str, float] = field(default_factory=dict)
    unmatched_records: List[Dict[str, Any]] = field(default_factory=list)


def evaluate_golden_vs_system(
    date_str: Optional[str] = None,
    *,
    system_verdict_field: str = "system_verdict",
    fraud_verdicts=("fraud", "suspicious", "blocked", "rejected"),
    normal_verdicts=("normal", "clean", "passed"),
) -> RedTeamEvaluation:
    """
    读取 day report，结合反欺诈系统回填的 system_verdict 字段，计算评估指标。

    使用前：先用反欺诈系统的 API/日志把每条记录的 system_verdict 回填。
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    path = os.path.join(REPORT_DIR, f"redteam_golden_{date_str}.jsonl")
    records: List[Dict[str, Any]] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue

    total = len(records)
    baseline = sum(1 for r in records if r.get("expected_verdict") == "normal")
    fraud = sum(1 for r in records if r.get("expected_verdict") == "fraud")
    susp = sum(1 for r in records if r.get("expected_verdict") == "suspicious")
    ev = RedTeamEvaluation(
        date_str=date_str, total_tasks=total,
        baseline_count=baseline, fraud_count=fraud, suspicious_count=susp,
    )

    # 先算有判定的样本
    judged = [r for r in records if r.get(system_verdict_field)]
    if judged:
        tp = sum(1 for r in judged
                 if r.get("expected_verdict") in ("fraud", "suspicious")
                 and r.get(system_verdict_field) in fraud_verdicts)
        fn = sum(1 for r in judged
                 if r.get("expected_verdict") in ("fraud", "suspicious")
                 and r.get(system_verdict_field) in normal_verdicts)
        fp = sum(1 for r in judged
                 if r.get("expected_verdict") == "normal"
                 and r.get(system_verdict_field) in fraud_verdicts)
        tn = sum(1 for r in judged
                 if r.get("expected_verdict") == "normal"
                 and r.get(system_verdict_field) in normal_verdicts)
        pos = tp + fn
        neg = fp + tn
        if pos > 0:
            ev.tpr = round(tp / pos, 4)
        if neg > 0:
            ev.fpr = round(fp / neg, 4)
        pred_pos = tp + fp
        if pred_pos > 0:
            ev.precision = round(tp / pred_pos, 4)
        if ev.tpr and ev.precision:
            ev.f1 = round(2 * ev.precision * ev.tpr / (ev.precision + ev.tpr), 4)

        # 各维度/场景/标签的召回率
        for dim_name in set(r.get("dimension", "") for r in judged):
            if not dim_name:
                continue
            dim_pos = [r for r in judged if r.get("dimension") == dim_name
                       and r.get("expected_verdict") in ("fraud", "suspicious")]
            if dim_pos:
                dim_hit = sum(1 for r in dim_pos if r.get(system_verdict_field) in fraud_verdicts)
                ev.dimension_recall[dim_name] = round(dim_hit / len(dim_pos), 4)
        for sid in set(r.get("scenario_id", "") for r in judged):
            if not sid:
                continue
            s_pos = [r for r in judged if r.get("scenario_id") == sid
                     and r.get("expected_verdict") in ("fraud", "suspicious")]
            if s_pos:
                s_hit = sum(1 for r in s_pos if r.get(system_verdict_field) in fraud_verdicts)
                ev.scenario_recall[sid] = round(s_hit / len(s_pos), 4)
        tag_count: Dict[str, List[bool]] = {}
        for r in judged:
            is_positive = r.get("expected_verdict") in ("fraud", "suspicious")
            detected = r.get(system_verdict_field) in fraud_verdicts
            for t in r.get("injected_tags", []):
                tag_count.setdefault(t, []).append(detected if is_positive else False)
        ev.tag_recall = {
            t: round(sum(hit_list) / len(hit_list), 4)
            for t, hit_list in tag_count.items() if len(hit_list) >= 3
        }

        # 未匹配（本该命中但系统漏判 / 误判）
        ev.unmatched_records = [
            r for r in judged
            if (r.get("expected_verdict") in ("fraud", "suspicious") and r.get(system_verdict_field) in normal_verdicts)
            or (r.get("expected_verdict") == "normal" and r.get(system_verdict_field) in fraud_verdicts)
        ]

    return ev


def print_evaluation_report(ev: RedTeamEvaluation) -> None:
    """打印人类可读的评估报告。"""
    print("=" * 60)
    print(f" 红队评估报告 [{ev.date_str}]")
    print("=" * 60)
    print(f" 任务总数     : {ev.total_tasks}")
    print(f"   · 基线(正常): {ev.baseline_count}")
    print(f"   · 欺诈样本  : {ev.fraud_count}")
    print(f"   · 可疑样本  : {ev.suspicious_count}")
    print()
    if ev.tpr is None:
        print(" [提示] 尚未回填 system_verdict，无法计算指标。")
        print("        用反欺诈系统 API/日志处理 report JSONL 后，再运行此函数。")
    else:
        print(f" TPR 召回率   : {ev.tpr:.2%} (越高越好 = 欺诈抓得多)")
        print(f" FPR 误报率   : {ev.fpr:.2%} (越低越好 = 真人不误判)")
        if ev.precision: print(f" Precision    : {ev.precision:.2%}")
        if ev.f1:        print(f" F1           : {ev.f1:.4f}")
        print()
        print(" 各维度召回率 Top:")
        for dim, rc in sorted(ev.dimension_recall.items(), key=lambda x: -x[1])[:5]:
            print(f"   · {dim:<38s} {rc:.0%}")
        print()
        if ev.unmatched_records:
            print(f" 漏判/误判样本数: {len(ev.unmatched_records)}（前 5 条）")
            for r in ev.unmatched_records[:5]:
                print(f"   · {r.get('run_id')} [{r.get('scenario_id')}] "
                      f"golden={r.get('expected_verdict')} system={r.get('system_verdict')}")
    print("=" * 60)


if __name__ == "__main__":
    # 自检：生成一条假任务，保存，然后读回
    from redteam_scenarios import RedTeamScenarioLibrary, apply_scenario_to_task
    rtl = RedTeamScenarioLibrary()
    scn = rtl.get("RT_FP_IP_MISMATCH")
    assert scn
    ctx = apply_scenario_to_task({}, {}, {}, scn)
    rec = RedTeamRecorder(ctx)
    rec.set_session_meta(
        ip="203.0.113.45", country_code="US", asn="AS701",
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/125 Safari/537.36",
        timezone_id="America/New_York", language="en-US", platform="Windows",
        referer="", traffic_source="direct",
    )
    rec.set_behavior(
        total_stay_sec=35.2, pages_viewed=2, scroll_max_depth_pct=55,
        ad_impressions=3, ad_clicks=0,
    )
    saved = rec.finish(success=True)
    print(f"[自检] 已保存 run_id={saved.run_id} scenario={saved.scenario_id} verdict={saved.expected_verdict}")

    ev = evaluate_golden_vs_system()
    print_evaluation_report(ev)
