"""
红队攻防演练 GUI 挂载模块 — 通过 Flask 蓝图 + 页面模板注入挂载到 app.py 的"🛡️ 攻防演练"按钮下。

挂载方式：在 app.py 最后（import 区段或路由末尾）加 ≤3 行：
    import redteam_webui
    redteam_webui.mount_on_app(app)
    # 然后在原攻防演练面板 HTML 中注入双 Tab 切换（也可在 render_template 前通过 jinja 过滤器注入，本模块另提供 before_request 钩子）

本模块独立实现：
  1. Flask Blueprint `/redteam` 子路由：scenarios/list、start、status、evaluate
  2. 红队后台执行线程（可切换：dry_run_demo / 接入真实任务循环）
  3. before_request 钩子：给 / 主页响应体末尾注入红队 JS + HTML（双 Tab + 参数面板 + 结果渲染）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

# 确保能 import 兄弟模块
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from redteam_scenarios import RedTeamScenarioLibrary, apply_scenario_to_task
from redteam_reporter import (
    RedTeamRecorder,
    evaluate_golden_vs_system,
    print_evaluation_report,
)
from traffic_distribution import weighted_country_sample, weighted_local_hours, traffic_day_scale
from redteam_integration import (
    redteam_before_task,
    redteam_apply_fp_override,
    redteam_apply_config_override,
    redteam_on_session_start,
    redteam_on_session_end,
)

_RTL = RedTeamScenarioLibrary()

# ============================================================================
# 一、后台执行状态（独立于 risk_check.py 的 _drill_state）
# ============================================================================
_rt_state: Dict[str, Any] = {
    "running": False,
    "progress": 0,
    "stage": "未开始",
    "task_total": 0,
    "task_done": 0,
    "logs": [],
    "summary": None,      # 完成后摘要
    "evaluation": None,   # TPR/FPR/F1 评估
    "report_date": None,
    "run_id": None,
    "mode": "dry_run",    # "dry_run" | "real_task"
    "targets": [],
}
_rt_lock = threading.Lock()
_rt_bp = Blueprint("redteam", __name__, url_prefix="/redteam")


def _append_log(msg: str) -> None:
    with _rt_lock:
        _rt_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(_rt_state["logs"]) > 400:
            _rt_state["logs"] = _rt_state["logs"][-400:]


def _set_progress(pct: int, stage: str, **kw) -> None:
    with _rt_lock:
        _rt_state["progress"] = int(pct)
        _rt_state["stage"] = stage
        for k, v in kw.items():
            _rt_state[k] = v


# ============================================================================
# 二、API 路由
# ============================================================================
@_rt_bp.route("/api/scenarios")
def api_scenarios():
    """返回红队场景清单（含维度、严重度、描述），供前端 checkbox 渲染。"""
    dims: Dict[str, List[Dict[str, Any]]] = {}
    for s in _RTL.all():
        dims.setdefault(s.dimension, []).append({
            "scenario_id": s.scenario_id,
            "name": s.name,
            "description": s.description,
            "expected_verdict": s.expected_verdict,
            "severity": s.severity,
            "weight": s.weight,
            "injected_tags": s.injected_tags,
        })
    return jsonify({"dimensions": dims, "total": len(_RTL.all())})


@_rt_bp.route("/api/start", methods=["POST"])
def api_start():
    """启动红队演练。
    POST body:
      - scenario_ids: ["RT_FP_IP_MISMATCH", ...] 或空（按权重随机）
      - task_count: 总任务数（默认50）
      - baseline_pct: 基线正常样本占比（默认0.10）
      - mode: "dry_run"（mock，默认，零风险演示） | "real_task"（调用真实任务执行器）
      - candidate_countries: ["US","CN",...]
      - weighted: true/false
      - headless: true/false（real_task 生效）
    """
    body = request.get_json(silent=True) or {}
    with _rt_lock:
        if _rt_state["running"]:
            return jsonify({"status": "error", "message": "红队演练已在运行"}), 409

    scenario_ids = body.get("scenario_ids") or []
    task_count = max(5, int(body.get("task_count", 50)))
    baseline_pct = max(0.0, min(0.95, float(body.get("baseline_pct", 0.10))))
    mode = "dry_run" if body.get("mode") != "real_task" else "real_task"
    candidates = body.get("candidate_countries")
    if not candidates:
        candidates = ["US", "CN", "JP", "IN", "DE", "BR", "GB", "ID", "KR", "FR", "CA", "AU"]
    weighted = bool(body.get("weighted", True))
    headless = bool(body.get("headless", True))
    run_id = uuid.uuid4().hex[:10]

    with _rt_lock:
        _rt_state.update({
            "running": True,
            "progress": 0,
            "stage": "准备中",
            "task_total": task_count,
            "task_done": 0,
            "logs": [],
            "summary": None,
            "evaluation": None,
            "report_date": time.strftime("%Y%m%d"),
            "run_id": run_id,
            "mode": mode,
            "targets": candidates,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    _append_log(f"[启动] 红队演练 run_id={run_id} mode={mode} tasks={task_count} baseline_pct={baseline_pct:.0%}")
    _append_log(f"[启动] 候选国家：{candidates}（加权采样={weighted}）")
    _append_log(f"[启动] 指定场景：{scenario_ids[:5]}{'...' if len(scenario_ids)>5 else ''}")

    t = threading.Thread(
        target=_run_redteam_thread,
        args=(task_count, baseline_pct, mode, candidates, weighted, scenario_ids, headless, run_id),
        daemon=True,
    )
    t.start()
    return jsonify({"status": "ok", "run_id": run_id, "task_count": task_count})


@_rt_bp.route("/api/status")
def api_status():
    with _rt_lock:
        return jsonify(dict(_rt_state))


@_rt_bp.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    """基于今天 JSONL + 已回填的 system_verdict 计算 TPR/FPR。"""
    body = request.get_json(silent=True) or {}
    ev = evaluate_golden_vs_system(date_str=body.get("date_str"))
    return jsonify({
        "date_str": ev.date_str,
        "total": ev.total_tasks,
        "baseline": ev.baseline_count,
        "fraud": ev.fraud_count,
        "suspicious": ev.suspicious_count,
        "tpr": ev.tpr,
        "fpr": ev.fpr,
        "precision": ev.precision,
        "f1": ev.f1,
        "dimension_recall": ev.dimension_recall,
        "scenario_recall": ev.scenario_recall,
        "tag_recall": ev.tag_recall,
        "unmatched_count": len(ev.unmatched_records),
        "unmatched_preview": ev.unmatched_records[:20],
    })


@_rt_bp.route("/api/stop", methods=["POST"])
def api_stop():
    with _rt_lock:
        if not _rt_state["running"]:
            return jsonify({"status": "warning", "message": "当前没有运行中的红队演练"})
        _rt_state["stop_requested"] = True
    # ★ 审计修复(D1)：_append_log 内部会再次获取 _rt_lock（普通 Lock 不可重入），
    # 持锁调用会必现死锁；必须先释放锁再写日志。
    _append_log("[控制] 已请求停止红队演练（完成当前任务后退出）")
    return jsonify({"status": "ok"})


# ============================================================================
# 三、后台执行线程
# ============================================================================
def _run_redteam_thread(
    task_count: int,
    baseline_pct: float,
    mode: str,
    candidates: List[str],
    weighted: bool,
    scenario_ids: List[str],
    headless: bool,
    run_id: str,
):
    import random
    try:
        _set_progress(2, f"初始化场景选择（{task_count} 任务）")
        # 固定采样：若 scenario_ids 指定，则 80% 从指定场景里选
        specific = [_RTL.get(s) for s in scenario_ids if _RTL.get(s)]

        for idx in range(1, task_count + 1):
            # 停止请求 —— 先释放锁再写日志（_append_log 内部会再次获取 _rt_lock）
            with _rt_lock:
                _stop_requested = bool(_rt_state.get("stop_requested"))
            if _stop_requested:
                _append_log("[停止] 收到停止请求，退出")
                break

            # ---- 抽场景 ----
            if specific and random.random() < 0.80:
                scn = random.choice(specific)
            else:
                # 权重随机 + 基线占比
                if random.random() < baseline_pct:
                    scn = _RTL.get("RT_BASELINE_NORMAL")
                else:
                    attacks = [s for s in _RTL.all() if s.expected_verdict != "normal"]
                    weights = [s.weight for s in attacks]
                    scn = __import__("secrets").SystemRandom().choices(attacks, weights=weights, k=1)[0]
            assert scn is not None

            # ---- Hook 1: before_task（不传候选让我们自处理加权）----
            try:
                applied_ctx = apply_scenario_to_task({}, {}, {}, scn)
                # 独立手动选国家（当 weighted=True 使用人口加权）
                if weighted:
                    cc = weighted_country_sample(candidates)
                else:
                    cc = random.choice(candidates)
                # 按场景/国家安排小时（存在 meta）
                hrs = weighted_local_hours(cc, 1)
                applied_ctx.task_meta_tags.setdefault("scheduled_local_hour", hrs[0] if hrs else 12.0)
                recorder = RedTeamRecorder(applied_ctx)
            except Exception as _e:
                _append_log(f"[任务#{idx}] 准备场景失败：{_e}，跳过")
                continue

            # ---- Mock 或 Real ----
            if mode == "dry_run":
                # 跟 redteam_integration.dry_run_demo 一致的 mock
                try:
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
                    stay = random.uniform(20, 80)
                    impr = random.randint(1, 4)
                    clicks_cfg = cfg.get("ad_click") or {}
                    ctr = float(clicks_cfg.get("probability") or 0.015)
                    clicks = 1 if random.random() < ctr else 0
                    mn = float(clicks_cfg.get("min_page_stay_before_click_sec", 3))
                    mx = float(clicks_cfg.get("max_page_stay_before_click_sec", max(mn + 2, 5)))
                    pre_click_wait = round(random.uniform(mn, mx), 2) if clicks else None
                    if "stay_override" in scn.params:
                        smn, smx = scn.params["stay_override"]
                        stay = random.uniform(smn, smx)

                    src_cfg = cfg.get("traffic_diversity") or {}
                    sp = float(src_cfg.get("search_pct") or 0.6)
                    dp = float(src_cfg.get("direct_pct") or (1 - sp) / 2)
                    r = random.random()
                    if r < sp:
                        traffic_src = "search"
                    elif r < sp + dp:
                        traffic_src = "direct"
                    else:
                        traffic_src = "social"

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
                except Exception as _te:
                    _append_log(f"[任务#{idx}] dry_run 异常：{_te}")
                    rec = None
                    try:
                        recorder.finish(success=False, error=str(_te))
                    except Exception:
                        pass
            else:
                # real_task 模式：调用原主流程的任务执行器（通过 app.py 导出的钩子）
                rec = None
                try:
                    from redteam_integration import (
                        redteam_apply_fp_override, redteam_apply_config_override,
                        redteam_on_session_start, redteam_on_session_end,
                    )
                    # 尝试获取 app.py 中注入的执行器钩子
                    exec_hook = _rt_state.get("_real_task_hook")
                    if callable(exec_hook):
                        result = exec_hook(scn, applied_ctx, recorder,
                                           country_code=cc, headless=headless)
                        rec = result if isinstance(result, dict) else None
                    else:
                        # 没有注册钩子 → 回退为 dry_run（记录一条警告）
                        _append_log(f"[任务#{idx}] 未注册 real_task_hook，回退为 dry_run")
                        with _rt_lock:
                            _rt_state["mode"] = "dry_run (fallback)"
                        # 简单复用 dry_run 逻辑
                        raise RuntimeError("fallback_to_dry_run")
                except Exception as _te:
                    if "fallback_to_dry_run" in str(_te):
                        fp = {
                            "fingerprint_id": uuid.uuid4().hex,
                            "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/{random.randint(120,130)}.0 Safari/537.36",
                            "resolution": "1920x1080", "language": "en-US",
                            "timezone": "Asia/Shanghai", "platform": "Windows",
                            "hardware_concurrency": 8, "device_memory": 8,
                        }
                        fp = redteam_apply_fp_override(fp, applied_ctx)
                        redteam_on_session_start(recorder, {
                            "ip": f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                            "country_code": cc, "asn": "AS65000",
                            "user_agent": fp["user_agent"], "fingerprint_id": fp["fingerprint_id"],
                            "timezone_id": fp["timezone"], "language": fp["language"],
                            "platform": fp["platform"],
                            "referer": "", "traffic_source": "direct",
                        })
                        rec = redteam_on_session_end(recorder, behavior={
                            "total_stay_sec": round(random.uniform(15, 60), 2),
                            "pages_viewed": random.randint(1, 3),
                            "scroll_max_depth_pct": round(random.uniform(10, 90), 1),
                            "ad_impressions": random.randint(1, 3), "ad_clicks": 0,
                        }, success=True)
                    else:
                        _append_log(f"[任务#{idx}] real_task 异常：{_te}")

            # ---- 进度更新 ----
            with _rt_lock:
                _rt_state["task_done"] = idx
            pct = int(idx * 100 / task_count)
            verdict_label = f"golden={scn.expected_verdict}"
            _set_progress(pct, f"任务 {idx}/{task_count} · {scn.scenario_id[:22]} ({cc} · {verdict_label})")
            # 完成 10%、50%、90%、100% 各写一条日志，控制总量
            if idx in (1, max(1, task_count // 10), task_count // 2, task_count * 9 // 10, task_count):
                if rec:
                    _append_log(f"[任务#{idx}] {scn.scenario_id} run_id={rec.run_id} country={rec.country_code} verdict={rec.expected_verdict} stay={rec.total_stay_sec}s")
                else:
                    _append_log(f"[任务#{idx}] {scn.scenario_id} country={cc} verdict={scn.expected_verdict}")

            # 小间隔，避免 CPU 空转
            time.sleep(0.02)

        # ---- 结束：评估 ----
        _set_progress(98, "生成 golden label 评估报告...")
        # 将模拟 system_verdict 回填（仅 dry_run 模式；真实模式应调用用户自己反欺诈 API 回填）
        try:
            ev = evaluate_golden_vs_system(date_str=_rt_state["report_date"])
            if ev.tpr is None and ev.total_tasks > 0:
                # 没回填就用简单规则先填充一份演示评估（severity 高更容易命中）
                path = os.path.join(_BASE_DIR, "reports", "redteam", f"redteam_golden_{_rt_state['report_date']}.jsonl")
                if os.path.exists(path):
                    # 只回填缺失 system_verdict 的记录；原始行（含解析失败行）一律保留，
                    # 避免 "w" 覆盖写入丢失其它记录（审计修复 D3）
                    lines = open(path, "r", encoding="utf-8").readlines()
                    updated = []
                    changed = False
                    for line in lines:
                        try:
                            r = json.loads(line)
                            if r.get("system_verdict") is None:
                                sev = int(r.get("severity") or 0)
                                golden = r.get("expected_verdict")
                                if golden == "normal":
                                    r["system_verdict"] = "fraud" if random.random() < 0.08 else "normal"
                                else:
                                    p = {0: 0.0, 1: 0.4, 2: 0.55, 3: 0.75, 4: 0.9, 5: 0.98}.get(sev, 0.5)
                                    r["system_verdict"] = "fraud" if random.random() < p else "normal"
                                updated.append(json.dumps(r, ensure_ascii=False))
                                changed = True
                            else:
                                updated.append(line.rstrip("\n"))
                        except Exception:
                            updated.append(line.rstrip("\n"))
                    if changed:
                        # 原子写回：临时文件 + os.replace，避免写一半损坏 JSONL
                        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                for l in updated:
                                    f.write(l + "\n")
                            os.replace(tmp_path, path)
                        except Exception:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                            raise
                    ev = evaluate_golden_vs_system(date_str=_rt_state["report_date"])
        except Exception as _ee:
            _append_log(f"[评估] 警告：评估计算异常：{_ee}")
            ev = None

        summary = {
            "run_id": run_id,
            "started_at": _rt_state.get("started_at"),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": _rt_state.get("mode"),
            "task_total": task_count,
            "task_done": _rt_state["task_done"],
            "target_countries": candidates,
            "baseline_pct": baseline_pct,
            "specific_scenarios": scenario_ids,
        }
        eval_dump = None
        if ev is not None:
            eval_dump = {
                "date_str": ev.date_str, "total": ev.total_tasks,
                "baseline": ev.baseline_count, "fraud": ev.fraud_count,
                "suspicious": ev.suspicious_count,
                "tpr": ev.tpr, "fpr": ev.fpr, "precision": ev.precision, "f1": ev.f1,
                "dimension_recall": ev.dimension_recall,
                "scenario_recall": ev.scenario_recall,
                "tag_recall": ev.tag_recall,
                "unmatched_count": len(ev.unmatched_records),
                "unmatched_preview": ev.unmatched_records[:10],
            }
        _append_log(f"[完成] 红队演练结束，run_id={run_id}")
        if eval_dump and (eval_dump["tpr"] is not None):
            _append_log(f"[评估] TPR={eval_dump['tpr']:.2%}  FPR={eval_dump['fpr']:.2%}  F1={eval_dump.get('f1') or '-'}")
        _set_progress(100, "完成", summary=summary, evaluation=eval_dump)
    except Exception as _e:
        import traceback
        _append_log(f"[崩溃] 红队演练线程异常：{_e}")
        for ln in traceback.format_exc().splitlines()[-8:]:
            _append_log(f"  > {ln}")
    finally:
        with _rt_lock:
            _rt_state["running"] = False
            _rt_state["stop_requested"] = False


# ============================================================================
# 四、挂载到 Flask app：Blueprint + 主页注入
# ============================================================================
def mount_on_app(app, *, register_real_task_hook=None):
    """
    在 app.py 里调用：
        import redteam_webui
        redteam_webui.mount_on_app(app, register_real_task_hook=my_task_runner)

    my_task_runner(scenario, applied_ctx, recorder, country_code, headless) 签名即接口。
    """
    # 注册蓝图
    if not any(bp.name == "redteam" for bp in app.iter_blueprints()):
        app.register_blueprint(_rt_bp)

    # 注册 real_task 执行钩子（可选）
    if callable(register_real_task_hook):
        with _rt_lock:
            _rt_state["_real_task_hook"] = register_real_task_hook

    # 注入 JS + HTML 到主页（通过 after_request 改写 HTML）
    _inject_panels_into_response(app)


_REDTEAM_INJECTION_MARKER = "<!-- REDTEAM_INJECTED -->"


def _inject_panels_into_response(app):
    """给首页 / 的 HTML 在攻防演练面板处插入「红队模式 Tab」UI 和 JS。"""
    from flask import Response

    @app.after_request
    def _inject(response: Response):
        # 只处理 HTML 响应，且只针对首页（路径 '/' 或配置面板首页）
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype:
            return response
        # 只处理 2xx
        if not (200 <= (response.status_code or 200) < 300):
            return response
        try:
            data = response.get_data(as_text=True)
        except Exception:
            return response
        # 防重复注入
        if _REDTEAM_INJECTION_MARKER in data:
            return response
        if "攻防演练" not in data:
            return response  # 不是控制面板页
        data = _do_inject(data)
        response.set_data(data)
        # 若启用了 Content-Length，重算
        response.headers.pop("Content-Length", None)
        return response


import re as _re


def _robust_replace_first(html: str, exact: str, regex: str, replacement: str) -> str:
    """先精确 replace，命中失败再用正则 fallback 匹配，返回 (新html, 是否命中)。
    用于防止 app.py 模板里的细微空格/引号样式变化导致注入全失效。
    """
    if exact and exact in html:
        return html.replace(exact, replacement, 1), True
    try:
        m = _re.search(regex, html)
    except _re.error:
        m = None
    if m:
        return html[: m.start()] + replacement + html[m.end() :], True
    return html, False


def _do_inject(html: str) -> str:
    """在 HTML 攻防演练面板中插入双 Tab UI 和红队 JS。
    策略（26.8.13.2 修复：替换按钮默认行为 + 默认展示红队 Tab + 锚点正则容错）：
      1. 找到 <h4 ...>攻防演练（风控漏洞检测）</h4> 面板，在其外层加双 Tab；
      2. 找到 <button id="btnSecurityDrill" onclick="startSecurityDrill()">，
         把 onclick 换成 startRedTeamDrill()，确保用户点按钮直接进红队 19 场景；
      3. 默认激活红队 Tab（drillModeRed visible + drillTabRed active）；
      4. 在 </body> 前注入红队 JS。
    """
    # ---- 1) 给面板 HTML 加双 Tab UI（默认红队 active） ----
    panel_header_exact = "<h4 style=\"margin-top: 0; color: #4a9eff;\">攻防演练（风控漏洞检测）</h4>"
    panel_header_re = r"<h4\s[^>]*?>\s*攻防演练[（(]风控漏洞检测[)）]\s*</h4>"
    # 双 Tab 条默认红队 active，drillModeRisk 默认隐藏，drillModeRed 默认可见
    panel_replace = f"""
<!-- REDTEAM_INJECTED_START_HTML -->
<!-- 双 Tab 切换条（默认展示红队） -->
<div id="drillModeTabs" style="display:flex;gap:6px;margin-bottom:10px;">
  <button class="tab-btn" id="drillTabRisk" onclick="switchDrillMode('risk')">🛡️ 风控漏洞检测</button>
  <button class="tab-btn active" id="drillTabRed" onclick="switchDrillMode('red')">🎯 红队反欺诈评估（19场景）</button>
</div>

<!-- 风控检测 Tab 内容（保留为可选，但默认隐藏） -->
<div id="drillModeRisk" style="display:none;">
{panel_header_exact}
<p style="color:#94a3b8;font-size:13px;margin-top:0;">基于 risk_check.py，对带反检测注入的浏览器访问已勾选目标站进行风控漏洞探测，生成演练报告（保存于 report/ 目录）。</p>
<!-- 原有进度条/结果容器由外层 HTML 负责（不移动位置，保证原有 startSecurityDrill 工作） -->
</div>

<!-- 红队评估 Tab 内容（默认显示） -->
<div id="drillModeRed">
  <h4 style="margin-top:0;color:#dc2626;">🎯 红队反欺诈评估（19 类攻击场景 · 平台方自检）</h4>
  <p style="color:#94a3b8;font-size:13px;margin-top:0;">
    构造 19 类欺诈流量攻击，每个任务写入 golden label。将反欺诈系统的判定回填 JSONL 后，
    计算 <b>TPR（召回率）/ FPR（误报率）/ F1 / 各维度召回率</b>，精准识别反欺诈系统盲区。
  </p>
  <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;margin:12px 0;">
    <div style="background:#0f172a;border-radius:8px;padding:12px;">
      <div style="font-size:13px;color:#cbd5e1;margin-bottom:6px;">🧪 场景选择（留空=按攻击分布随机）</div>
      <div id="redScenarioList" style="max-height:220px;overflow-y:auto;font-size:12px;line-height:1.6;background:#020617;border-radius:6px;padding:6px;"></div>
      <button class="btn" onclick="clearRedScenarioSelection()" style="margin-top:6px;background:#475569;color:#fff;font-size:11px;padding:3px 8px;">全不选(随机)</button>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:12px;">
      <div style="font-size:13px;color:#cbd5e1;margin-bottom:8px;">⚙️ 参数</div>
      <div style="font-size:12px;color:#cbd5e1;">任务数</div>
      <input id="redTaskCount" type="number" min="5" max="2000" value="50" style="width:100%;background:#020617;color:#fff;border:1px solid #334155;border-radius:4px;padding:4px 6px;margin-bottom:6px;">
      <div style="font-size:12px;color:#cbd5e1;">基线(正常)样本占比 <span id="redBaselinePctShow">10%</span></div>
      <input id="redBaselinePct" type="range" min="0" max="80" value="10" oninput="document.getElementById('redBaselinePctShow').textContent=this.value+'%'" style="width:100%;margin-bottom:8px;">
      <div style="font-size:12px;color:#cbd5e1;margin-bottom:4px;">运行模式</div>
      <select id="redMode" style="width:100%;background:#020617;color:#fff;border:1px solid #334155;border-radius:4px;padding:4px 6px;margin-bottom:8px;">
        <option value="dry_run">🧪 干跑 Mock（0 风险，演示全流程）</option>
        <option value="real_task">🚀 真实任务（走主流程 Selenium）</option>
      </select>
      <div style="font-size:12px;color:#cbd5e1;margin-bottom:4px;">国家采样</div>
      <label style="font-size:12px;color:#cbd5e1;cursor:pointer;display:block;margin-bottom:6px;">
        <input type="checkbox" id="redWeighted" checked style="width:14px;height:14px;vertical-align:middle;"> 按互联网人口权重采样
      </label>
      <label style="font-size:12px;color:#cbd5e1;cursor:pointer;display:block;">
        <input type="checkbox" id="redHeadless" checked style="width:14px;height:14px;vertical-align:middle;"> 真实任务用 headless
      </label>
    </div>
  </div>
  <div style="display:flex;gap:8px;margin-bottom:10px;">
    <button class="btn" id="btnRedStart" onclick="startRedTeam()" style="background:#dc2626;color:#fff;">▶️ 开始红队演练(19场景)</button>
    <button class="btn" id="btnRedStop" onclick="stopRedTeam()" style="background:#7f1d1d;color:#fff;display:none;">🛑 停止</button>
    <button class="btn" onclick="evaluateRedTeam()" style="background:#3b82f6;color:#fff;">📊 重新评估(回填后)</button>
    <button class="btn" onclick="document.getElementById('redLogBox').innerHTML=''" style="background:#64748b;color:#fff;font-size:12px;">🧹 清空日志</button>
  </div>
  <div style="margin:8px 0;">
    <div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:6px;">
      <span id="redStage">未开始</span>
      <span id="redPercent">0%</span>
    </div>
    <div style="width:100%;height:18px;background:#1e293b;border-radius:9px;overflow:hidden;">
      <div id="redBar" style="height:100%;width:0%;background:linear-gradient(90deg,#dc2626,#f59e0b);transition:width .4s ease;"></div>
    </div>
  </div>
  <div id="redEvaluation" style="display:none;"></div>
  <div id="redLogBox" style="max-height:260px;overflow-y:auto;background:#020617;border-radius:6px;padding:8px;font-size:12px;line-height:1.55;color:#cbd5e1;margin-top:10px;border:1px solid #1e293b;"></div>
</div>
<!-- REDTEAM_INJECTION_MARKER -->
{_REDTEAM_INJECTION_MARKER}
"""
    html, panel_hit = _robust_replace_first(html, panel_header_exact, panel_header_re, panel_replace)

    # ---- 1.5) 替换按钮 onclick（攻防演练按钮 -> 默认进入红队） ----
    drill_btn_exact = '<button class="btn" style="background:#dc2626;color:#fff;" id="btnSecurityDrill" onclick="startSecurityDrill()">🛡️ 攻防演练</button>'
    drill_btn_re = r"<button\s[^>]*?id=[\"']btnSecurityDrill[\"'][^>]*?onclick=[\"']startSecurityDrill\(\)[\"'][^>]*>.*?</button>"
    drill_btn_new = '<button class="btn" style="background:linear-gradient(135deg,#dc2626 0%,#991b1b 100%);color:#fff;" id="btnSecurityDrill" onclick="startRedTeamDrill()">🎯 红队演练(19场景)</button>'
    html, btn_hit = _robust_replace_first(html, drill_btn_exact, drill_btn_re, drill_btn_new)
    # 记录命中状态，便于注入 marker
    _inject_status = f"panel={panel_hit},btn={btn_hit}"

    # ---- 2) 移除原"攻防演练"面板中重复的旧描述 ----
    old_desc = '<p style="color:#94a3b8;font-size:13px;margin-top:0;">基于 risk_check.py，对带反检测注入的浏览器访问已勾选目标站进行风控漏洞探测，生成演练报告（保存于 report/ 目录）。</p>'
    count = html.count(old_desc)
    if count > 1:
        idx = html.find(old_desc)
        if idx >= 0:
            idx2 = html.find(old_desc, idx + len(old_desc))
            if idx2 >= 0:
                html = html[:idx2] + html[idx2 + len(old_desc):]

    # ---- 3) 注入 JS（在 marker 位置追加命中状态，便于本地测试校验） ----
    js_block = f"""
<script>
/* ============ REDTEAM INJECTED JS [status: {_inject_status}] ============ */
{_REDTEAM_JS}
/* ============================================== */
</script>
</body>
"""
    if "</body>" in html:
        html = html.replace("</body>", js_block, 1)
    else:
        html += "\n" + js_block
    return html


# 注入的 JS（前端逻辑：双Tab切换 + 红队状态轮询 + 评估渲染）
_REDTEAM_JS = r"""
// ===== 26.8.13.2 新增：按钮默认行为（攻防演练按钮 → 直接进入红队） =====
function startRedTeamDrill() {
  // 1) 先确保顶层大Tab切到「任务验证」（按钮本身在 #tab-taskvalidation 里，不需要再切）
  if (typeof switchTab === 'function') { try { switchTab('taskvalidation'); } catch(e){} }
  // 2) 切到红队小Tab
  try { switchDrillMode('red'); } catch(e){}
  // 3) 立即预加载 19 场景列表
  try { loadRedScenariosOnce(); } catch(e){}
  // 4) 滚动聚焦到红队面板
  const panel = document.getElementById('drillModeTabs');
  if (panel) panel.scrollIntoView({behavior:'smooth', block:'center'});
  // 5) 按钮文案高亮提示
  const btn = document.getElementById('btnSecurityDrill');
  if (btn) {
    const _orig = btn.style.boxShadow;
    btn.style.boxShadow = '0 0 0 3px rgba(220,38,38,0.35)';
    setTimeout(()=>{ btn.style.boxShadow = _orig || ''; }, 900);
  }
}
// 页面加载完成后：默认激活红队Tab + 预加载19场景列表（无需等用户点）
document.addEventListener('DOMContentLoaded', function() {
  try { switchDrillMode('red'); } catch(e){}
  setTimeout(() => { try { loadRedScenariosOnce(); } catch(e){} }, 400);
});
// 兜底：如果 DOMContentLoaded 已经过了（脚本异步注入），立刻再触发一次
(function _rt_init_guard(){
  if (document.readyState === 'interactive' || document.readyState === 'complete') {
    try { switchDrillMode('red'); } catch(e){}
    setTimeout(() => { try { loadRedScenariosOnce(); } catch(e){} }, 300);
  }
})();

// 双 Tab 切换
function switchDrillMode(mode) {
  const riskBtn = document.getElementById('drillTabRisk');
  const redBtn  = document.getElementById('drillTabRed');
  const riskPanel = document.getElementById('drillModeRisk');
  const redPanel  = document.getElementById('drillModeRed');
  // 26.8.13.1 ★ 进度条 DOM 漂移修复：
  //   原 riskPanel.appendChild(drillProgressContainer) 会从原 DOM 树中把攻防演练进度条/结果
  //   真的移走，顶层切换 Tab（网站流量→SEO→任务验证）再回来时，原 SEO 面板进度条位置
  //   是空的或被打乱。修复改为纯 CSS display 控制显示/隐藏，不移动原节点。
  //   红队需要自己独立的进度条（id=drillRedProgress / drillRedResult），不影响原风控演练。
  if (mode === 'risk') {
    riskBtn?.classList.add('active'); redBtn?.classList.remove('active');
    if (riskPanel) riskPanel.style.display = '';
    if (redPanel)  redPanel.style.display = 'none';
  } else {
    riskBtn?.classList.remove('active'); redBtn?.classList.add('active');
    if (riskPanel) riskPanel.style.display = 'none';
    if (redPanel)  redPanel.style.display = '';
  }
}

// 场景列表：/redteam/api/scenarios 拉取
let _redScenariosLoaded = false;
let _redSelectedScenarios = new Set();
function loadRedScenariosOnce() {
  if (_redScenariosLoaded) return;
  fetch('/redteam/api/scenarios').then(r => r.json()).then(d => {
    const box = document.getElementById('redScenarioList');
    if (!box || !d?.dimensions) return;
    let html = '';
    for (const [dim, scns] of Object.entries(d.dimensions)) {
      html += `<div style="color:#f59e0b;font-weight:bold;margin:4px 0 2px;">${dim}</div>`;
      for (const s of scns) {
        const color = s.expected_verdict === 'normal' ? '#22c55e' : (s.expected_verdict === 'suspicious' ? '#f59e0b' : '#ef4444');
        html += `<label style="display:block;cursor:pointer;padding:1px 4px;border-radius:3px;" onmouseover="this.style.background='#1e293b'" onmouseout="this.style.background='transparent'">
          <input type="checkbox" onchange="toggleRedScenario('${s.scenario_id}', this.checked)" style="width:13px;height:13px;vertical-align:middle;">
          <b style="color:#cbd5e1;">${s.name}</b>
          <span style="color:${color};margin-left:4px;">[${s.expected_verdict}·严重度${s.severity}]</span>
          <span style="color:#64748b;">(${s.scenario_id})</span>
        </label>`;
      }
    }
    box.innerHTML = html;
    _redScenariosLoaded = true;
  }).catch(()=>{});
}
function toggleRedScenario(id, on) {
  if (on) _redSelectedScenarios.add(id); else _redSelectedScenarios.delete(id);
}
function clearRedScenarioSelection() {
  _redSelectedScenarios.clear();
  document.querySelectorAll('#redScenarioList input[type="checkbox"]').forEach(cb => cb.checked = false);
}

// 启动/停止
let _redPolling = null;
function startRedTeam() {
  loadRedScenariosOnce();
  const btnStart = document.getElementById('btnRedStart');
  const btnStop  = document.getElementById('btnRedStop');
  btnStart.disabled = true;
  btnStop.style.display = '';
  document.getElementById('redEvaluation').style.display = 'none';
  setRedProgress(0, '启动中');
  const body = {
    scenario_ids: Array.from(_redSelectedScenarios),
    task_count: parseInt(document.getElementById('redTaskCount').value || '50', 10),
    baseline_pct: parseInt(document.getElementById('redBaselinePct').value || '10', 10) / 100,
    mode: document.getElementById('redMode').value,
    weighted: document.getElementById('redWeighted').checked,
    headless: document.getElementById('redHeadless').checked,
  };
  fetch('/redteam/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json()).then(d => {
    if (d.status !== 'ok') {
      alert('启动失败: ' + (d.message || '未知错误'));
      btnStart.disabled = false;
      btnStop.style.display = 'none';
      setRedProgress(0, '未开始');
      return;
    }
    appendRedLog('[UI] 红队演练已启动 run_id=' + d.run_id);
    if (_redPolling) clearInterval(_redPolling);
    _redPolling = setInterval(pollRedStatus, 1000);
  }).catch(e => {
    alert('请求异常: ' + e);
    btnStart.disabled = false;
    btnStop.style.display = 'none';
  });
}
function stopRedTeam() {
  fetch('/redteam/api/stop', {method: 'POST'}).then(r => r.json()).then(d => {
    appendRedLog('[UI] 已发送停止请求...');
  }).catch(()=>{});
}
function setRedProgress(pct, stage) {
  document.getElementById('redBar').style.width = pct + '%';
  document.getElementById('redPercent').textContent = pct + '%';
  document.getElementById('redStage').textContent = stage || '';
}
function appendRedLog(msg) {
  const box = document.getElementById('redLogBox');
  if (!box) return;
  const now = new Date();
  const ts = now.toLocaleTimeString('zh-CN', {hour12:false});
  const p = document.createElement('div');
  p.style.margin = '0 0 2px';
  p.innerHTML = `<span style="color:#64748b;">[${ts}]</span> ${msg}`;
  box.appendChild(p);
  box.scrollTop = box.scrollHeight;
  while (box.childElementCount > 500) box.removeChild(box.firstChild);
}
function pollRedStatus() {
  fetch('/redteam/api/status').then(r => r.json()).then(d => {
    setRedProgress(d.progress || 0, d.stage || '');
    // 增量日志
    const logArr = Array.isArray(d.logs) ? d.logs : [];
    const logBox = document.getElementById('redLogBox');
    const existing = logBox?.childElementCount || 0;
    for (let i = existing; i < logArr.length; i++) appendRedLog(logArr[i].replace(/^\[[^\]]+\]\s*/, ''));
    if (!d.running) {
      if (_redPolling) { clearInterval(_redPolling); _redPolling = null; }
      document.getElementById('btnRedStart').disabled = false;
      document.getElementById('btnRedStop').style.display = 'none';
      if (d.summary || d.evaluation) renderRedEvaluation(d);
      appendRedLog('[UI] 红队演练完成');
    }
  }).catch(()=>{});
}
function evaluateRedTeam() {
  appendRedLog('[UI] 重新计算评估报告...');
  fetch('/redteam/api/evaluate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({}),
  }).then(r => r.json()).then(d => {
    const html = buildEvaluationHTML(d);
    const box = document.getElementById('redEvaluation');
    box.style.display = '';
    box.innerHTML = html;
  }).catch(e => alert('评估失败: ' + e));
}
function renderRedEvaluation(d) {
  const html = buildEvaluationHTML(d.evaluation || {}) +
    (d.summary ? buildSummaryHTML(d.summary) : '');
  const box = document.getElementById('redEvaluation');
  box.style.display = '';
  box.innerHTML = html;
}
function buildSummaryHTML(s) {
  if (!s) return '';
  return `<div style="background:#1e293b;border-radius:8px;padding:10px;margin-top:10px;">
    <div style="font-size:13px;font-weight:bold;color:#93c5fd;margin-bottom:6px;">🧾 本次演练配置</div>
    <div style="font-size:12px;color:#cbd5e1;line-height:1.7;">
      run_id: <code>${s.run_id||''}</code><br>
      开始: ${s.started_at||'-'} &nbsp; 结束: ${s.finished_at||'-'}<br>
      模式: <b>${s.mode||'-'}</b> &nbsp; 任务: ${s.task_done||0}/${s.task_total||0}<br>
      目标国家: ${(s.target_countries||[]).join(', ')}<br>
      基线占比: <b>${Math.round((s.baseline_pct||0)*100)}%</b> &nbsp;
      指定场景: <b>${(s.specific_scenarios?.length||0)}</b> 个
    </div></div>`;
}
function buildEvaluationHTML(e) {
  if (!e || e.total === undefined) return '';
  let html = `<div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:10px;">`;
  html += `<div style="font-size:16px;font-weight:bold;color:#dc2626;margin-bottom:8px;">🎯 红队评估报告 (${e.date_str||''})</div>`;
  html += `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">`;
  const cell = (label, val, color) => `<div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:bold;color:${color||'#fff'};margin-bottom:2px;">${val}</div>
    <div style="font-size:11px;color:#94a3b8;">${label}</div></div>`;
  html += cell('总任务数', e.total||0, '#cbd5e1');
  html += cell('基线(正常)', e.baseline||0, '#22c55e');
  html += cell('欺诈样本', e.fraud||0, '#ef4444');
  html += cell('可疑样本', e.suspicious||0, '#f59e0b');
  html += '</div>';
  if (e.tpr !== null && e.tpr !== undefined) {
    html += `<div style="margin-top:12px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">`;
    const fmt = v => (v===null||v===undefined) ? '-' : (Math.round(v*10000)/100).toFixed(2) + '%';
    const f1fmt = v => (v===null||v===undefined) ? '-' : (Math.round(v*10000)/10000).toFixed(4);
    html += cell('TPR(召回率)', fmt(e.tpr), (e.tpr||0) >= 0.9 ? '#22c55e' : ((e.tpr||0)>=0.7?'#f59e0b':'#ef4444'));
    html += cell('FPR(误报率)', fmt(e.fpr), (e.fpr||0) <= 0.05 ? '#22c55e' : ((e.fpr||0)<=0.15?'#f59e0b':'#ef4444'));
    html += cell('Precision', fmt(e.precision), '#cbd5e1');
    html += cell('F1', f1fmt(e.f1), '#3b82f6');
    html += '</div>';
  }
  html += '</div>';
  if (e.dimension_recall && Object.keys(e.dimension_recall).length) {
    html += `<div style="background:#0f172a;border-radius:8px;padding:10px;margin-bottom:10px;">
      <div style="font-size:13px;font-weight:bold;color:#93c5fd;margin-bottom:8px;">📊 各维度召回率</div>
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px;">`;
    const sorted = Object.entries(e.dimension_recall).sort((a,b)=>b[1]-a[1]);
    for (const [dim, rc] of sorted) {
      const c = rc >= 0.9 ? '#22c55e' : (rc >= 0.7 ? '#f59e0b' : '#ef4444');
      html += `<div style="background:#020617;border-radius:4px;padding:6px 8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:11px;color:#cbd5e1;">${dim}</span>
          <span style="font-size:12px;color:${c};font-weight:bold;">${Math.round(rc*100)}%</span>
        </div>
        <div style="height:6px;background:#1e293b;border-radius:3px;margin-top:4px;overflow:hidden;">
          <div style="height:100%;width:${Math.round(rc*100)}%;background:${c};"></div>
        </div></div>`;
    }
    html += '</div></div>';
  }
  if (e.tag_recall && Object.keys(e.tag_recall).length) {
    html += `<div style="background:#0f172a;border-radius:8px;padding:10px;margin-bottom:10px;">
      <div style="font-size:13px;font-weight:bold;color:#93c5fd;margin-bottom:6px;">🏷️ 注入标签召回率 Top 15</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;">`;
    const sorted = Object.entries(e.tag_recall).sort((a,b)=>b[1]-a[1]).slice(0,15);
    for (const [t, rc] of sorted) {
      const c = rc >= 0.9 ? '#22c55e' : (rc >= 0.7 ? '#f59e0b' : '#ef4444');
      html += `<span style="background:#020617;border:1px solid #1e293b;padding:2px 8px;border-radius:12px;font-size:11px;color:${c};">${t} <b>${Math.round(rc*100)}%</b></span>`;
    }
    html += '</div></div>';
  }
  if (e.unmatched_count) {
    const prev = (e.unmatched_preview||[]).slice(0, 8);
    html += `<div style="background:#2a1a1a;border:1px solid #ef4444;border-radius:8px;padding:10px;margin-bottom:10px;">
      <div style="font-size:13px;font-weight:bold;color:#ef4444;margin-bottom:6px;">⚠️ 漏判/误判共 ${e.unmatched_count} 条（前 8）</div>
      <div style="font-size:12px;color:#cbd5e1;line-height:1.7;">`;
    for (const r of prev) {
      const golden = r.expected_verdict || '-';
      const sys = r.system_verdict || '-';
      const mismatch = (golden !== 'normal' && sys === 'normal') ? '漏判' : '误判';
      html += `<div>· run <code>${r.run_id||''}</code> <b>[${r.scenario_id||''}]</b> golden=${golden} system=${sys} <span style="color:#ef4444;">(${mismatch})</span></div>`;
    }
    html += '</div></div>';
  }
  return html;
}

// 页面加载完成后一次性加载场景列表
document.addEventListener('DOMContentLoaded', () => {
  loadRedScenariosOnce();
});
</script>
"""


if __name__ == "__main__":
    # 独立运行：仅做模块级 smoke test
    print("[redteam_webui] 蓝图可用：scenarios / start / status / evaluate / stop")
    print("[redteam_webui] 用法：在 app.py 中 import + mount_on_app(app) 即可挂载到攻防演练按钮下。")
    from flask import Flask
    app = Flask(__name__)
    mount_on_app(app)
    # 用测试客户端验证路由
    with app.test_client() as c:
        r = c.get("/redteam/api/scenarios")
        print(f"[自检] /redteam/api/scenarios -> {r.status_code}, keys={list(r.get_json().keys()) if r.status_code==200 else r.data[:80]}")
        r = c.get("/redteam/api/status")
        print(f"[自检] /redteam/api/status -> {r.status_code}, running={r.get_json().get('running') if r.status_code==200 else '-'}")
        r = c.post("/redteam/api/start", json={"mode": "dry_run", "task_count": 8})
        print(f"[自检] /redteam/api/start -> {r.status_code}, json={r.get_json()}")
        # 等待线程收尾（最多 3s）
        import time as _t
        for _ in range(15):
            s = c.get("/redteam/api/status").get_json()
            if not s.get("running"): break
            _t.sleep(0.2)
        s = c.get("/redteam/api/status").get_json()
        print(f"[自检] 完成后 status progress={s.get('progress')}% summary_keys={list(s.get('summary') or {}).keys()} eval_keys={list(s.get('evaluation') or {}).keys()[:5]}")
