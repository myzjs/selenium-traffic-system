"""
真实任务钩子示例 — 把红队场景"翻译"成 app.py 里的 worker_task 单任务执行。
直接复制到 app.py 中 mount_on_app(..., register_real_task_hook=real_task_hook_for_redteam) 即可。

注意：这是示例代码，供你参考实现；生产使用时请根据 app.py 最新内部实现调整。
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from typing import Any, Dict, Optional

# 如果在 app.py 里使用，下面这些函数是全局可直接用的：
#   worker_task(single_task=True, adsl_ip_task=False)
#   config (全局配置 dict)
#   task_running / stats / current_plan 等全局状态
#
# 此处为了让文件能独立 import 不报错，用占位实现：
try:
    from app import (
        config as _GLOBAL_CONFIG,
        task_running,
        _safe_stats_inc,
        validate_web_navigation_config,
        worker_task,
    )
    _HAS_APP = True
except Exception:
    _HAS_APP = False
    _GLOBAL_CONFIG: Dict[str, Any] = {}


def real_task_hook_for_redteam(
    scenario,            # redteam_scenarios.RedTeamScenario
    applied_ctx,         # redteam_scenarios.RedTeamAppliedContext
    recorder,            # redteam_reporter.RedTeamRecorder
    country_code: str,
    headless: bool = True,
):
    """
    红队真实任务模式的"转接线"：
      1. 把 applied_ctx.fingerprint_overrides 写入临时 fingerprint.json 覆盖层
      2. 把 applied_ctx.config_overrides 写入临时 config 覆盖层（不影响原配置）
      3. 强制代理池只选 country_code 国家
      4. 跑 app.worker_task(single_task=True)
      5. 结束后从 stats/current_plan 抽取行为统计（停留/滚动/点击/page数）
      6. 调 recorder.finish() 写 golden label

    返回一个 dict 行为记录，或 None（recorder 已经自己 finish 过）。
    """
    # 如果你 app.py 的全局没加载，回退为 dry_run
    if not _HAS_APP:
        return None

    import redteam_integration as _rti

    # ---- 阶段 A：记录会话级 meta（IP/UA/指纹等），先给 recorder 一个快照占位 ----
    # 真实 meta 要等 worker_task 结束后从结果里读，先写空值。

    # ---- 阶段 B：构造临时覆盖配置 ----
    # redteam_integration 已经给了浅/深合并工具
    from redteam_scenarios import merge_dicts_shallow
    from redteam_integration import _deep_merge  # 非公开，但好用

    task_cfg = _deep_merge(dict(_GLOBAL_CONFIG), applied_ctx.config_overrides or {})

    # 强制代理池使用 scenario 指定的国家：
    proxy_pool = task_cfg.get("proxy_pool") or []
    matching_proxies = [p for p in proxy_pool
                        if p.get("enabled") and p.get("country_code", "").upper() == country_code.upper()]
    if matching_proxies:
        # 仅保留匹配国家的代理启用，其它禁用
        for p in proxy_pool:
            p["enabled"] = p in matching_proxies
    else:
        # 没有匹配的代理，回退：使用单代理 API（IPDEEP）+ 指定国家参数
        # 依赖 ip_provider 实现；这里保留默认值，worker_task 自己会 fallback
        task_cfg["ip_proxy_country_hint"] = country_code

    # 浏览器 headless 模式
    task_cfg["browser_headless"] = headless

    # 任务数强制 = 1（红队每个真实任务只跑 1 次真实会话，循环由红队线程自己控制）
    task_cfg["adsl_task_count"] = 1

    # ---- 阶段 C：指纹覆盖 ----
    # 生成指纹时要用上 applied_ctx 的 override。最安全的做法：
    # 1) 在任务开始前用"一次性注入"变量，让 generate_fingerprint 在创建 context 时读取
    # 2) 最稳妥：写一个一次性 json 覆盖文件（app.py 已支持）
    fp_override_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".risk_state", f"rt_fp_override_{recorder.record.run_id}.json"
    )
    os.makedirs(os.path.dirname(fp_override_path), exist_ok=True)
    try:
        with open(fp_override_path, "w", encoding="utf-8") as f:
            json.dump(applied_ctx.fingerprint_overrides or {}, f, ensure_ascii=False)
        task_cfg["_redteam_fp_override_file"] = fp_override_path
    except Exception:
        pass

    # ---- 阶段 D：同步跑 worker_task（单任务）----
    # 注意：worker_task 里有大量对全局变量的修改（task_running、stats、current_plan 等），
    # 且原 worker_task 本身是被放到 Thread 里的。我们红队线程本身是独立线程，
    # 为避免递归线程+全局冲突，这里直接调用 worker_task(single_task=True) 同步执行即可。
    start_t = time.time()

    # 先把全局 config 临时替换（保存快照）
    import app as _app_module
    original_cfg_snapshot = json.loads(json.dumps(_app_module.config))  # 深拷贝
    try:
        _app_module.config = task_cfg
        # 全局 task_running 先复位为 True，避免 worker_task 内部一开始就退出
        _app_module.task_running = True
        _app_module._single_task_mode = True

        # 调用 worker_task（同步执行，会持续 ~30–180s）
        try:
            worker_task(single_task=True)
        except Exception as _e:
            # worker_task 内部已经 try/except 过，这里只兜底
            import traceback
            print(f"[RedTeam][真实任务] worker_task 异常：{_e}\n{traceback.format_exc()[:400]}")
    finally:
        # 恢复原始 config 快照
        _app_module.config = original_cfg_snapshot

    elapsed = round(time.time() - start_t, 2)

    # ---- 阶段 E：从 stats / current_plan 提取行为数据 ----
    try:
        plan_tasks = (getattr(_app_module, "current_plan", None) or {}).get("tasks") or []
        # 取最后一个完成的 task（单任务模式只有 1 个）
        last_task = plan_tasks[-1] if plan_tasks else None
        pages = 1
        stay = elapsed
        scroll_pct = None
        impr = 0
        clicks = 0
        exit_ip = None
        ua = None
        fp_id = None
        tz = None
        lang = None
        plat = None
        if isinstance(last_task, dict):
            stay = float(last_task.get("browse_duration") or elapsed)
            pages = int(last_task.get("pages_viewed") or pages)
            scroll_pct = last_task.get("scroll_max_depth_pct")
            impr = int(last_task.get("ad_impressions") or 0)
            clicks = int(last_task.get("ad_clicks") or 0)
            exit_ip = last_task.get("exit_ip")
            ua = last_task.get("user_agent")
            fp_id = last_task.get("fingerprint_id")
            tz = last_task.get("timezone")
            lang = last_task.get("language")
            plat = last_task.get("platform")

        behavior = {
            "total_stay_sec": round(stay, 2),
            "pages_viewed": pages,
            "scroll_max_depth_pct": scroll_pct,
            "ad_impressions": impr,
            "ad_clicks": clicks,
        }
        meta = {
            "ip": exit_ip,
            "country_code": country_code,
            "user_agent": ua,
            "fingerprint_id": fp_id,
            "timezone_id": tz,
            "language": lang,
            "platform": plat,
        }
    except Exception as _e2:
        print(f"[RedTeam][真实任务] 行为提取异常：{_e2}，用默认值")
        behavior = {"total_stay_sec": elapsed, "pages_viewed": 1, "scroll_max_depth_pct": None,
                    "ad_impressions": 0, "ad_clicks": 0}
        meta = {"country_code": country_code}

    # ---- 阶段 F：清理一次性 fp 覆盖文件 ----
    try:
        if os.path.exists(fp_override_path):
            os.remove(fp_override_path)
    except Exception:
        pass

    # ---- 阶段 G：落盘 golden label ----
    # meta 可能有 None 值，RedTeamRecorder 会忽略未知字段，所以直接传入
    _rti.redteam_on_session_start(recorder, {k: v for k, v in meta.items() if v is not None})
    return _rti.redteam_on_session_end(recorder, behavior=behavior, success=True)
