"""
Pop-under 弹窗触发模块（HilltopAds 专属）
========================================
通过 CDP Input.dispatchMouseEvent 生成 isTrusted=true 的用户手势，
触发页面上 HilltopAds tag.min.js 的 window.open() 调用，
创建后台弹窗标签页并管理其生命周期以满足结算条件。

设计原则：
  1) CDP 层事件 isTrusted=true，绕过 HilltopAds anti-adblock 检测
  2) 弹窗坐标智能避让已知广告容器（防止误触 Google AdSense）
  3) 弹窗最小存活 15s + DOMContentLoaded 确认，满足结算阈值
  4) 仅 30-50% 会话触发弹窗，模拟自然拦截率
  5) 与 Google Ads 流程完全解耦——AdSense 扫描/点击不受影响
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("popunder_trigger")

# --------- 默认参数（可通过 config["hilltopads"] 覆盖） ----------
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,                        # 总开关
    "trigger_probability": 0.40,             # 40% 会话触发弹窗（不过度，模拟自然拦截率）
    "trigger_after_pct_min": 0.20,           # 在真人模拟进度 20% 后触发（等页面交互积累）
    "trigger_after_pct_max": 0.40,           # 最晚 40% 处触发
    "popunder_stay_min": 15,                 # 弹窗最小存活秒数（低于此不计费）
    "popunder_stay_max": 25,                 # 弹窗最大存活秒数
    "popunder_load_timeout_ms": 10000,       # 弹窗页面加载超时
    "cdp_move_steps": 5,                     # CDP 鼠标移动步数（轨迹自然度）
    "cdp_click_count": 1,                    # 点击次数（HilltopAds tag 只需 1 次交互）
    "ad_safe_margin_px": 60,                 # 避让 AdSense 广告容器的最小边距
    "max_wait_for_popup_s": 3.0,             # 等待弹窗创建的最长时间
    "cooldown_between_triggers_s": 90,       # 同一站点两次弹窗最小间隔
}


# --------- 安全区计算 ----------

def _get_ad_bounding_boxes(page: Any) -> List[Tuple[int, int, int, int]]:
    """返回页面上所有已知广告容器的边界框 [(x, y, width, height), ...]"""
    boxes = []
    try:
        rects = page.evaluate("""() => {
            const ads = document.querySelectorAll(
                'ins.adsbygoogle, iframe[src*="googleads"], iframe[src*="doubleclick"], '
                + '[id*="google_ads"], [class*="adsbygoogle"], '
                + 'div[data-ad], div[id*="adngin"], iframe[src*="adnxs"]'
            );
            return Array.from(ads).map(el => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }).filter(r => r.w > 10 && r.h > 10);
        }""")
        if rects:
            for r in rects:
                boxes.append((int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])))
    except Exception:
        pass
    return boxes


def _pick_safe_coordinates(
    page: Any,
    viewport: Dict[str, int],
    margin: int = 60,
) -> Tuple[int, int]:
    """选取一个不在任何广告容器内的安全坐标"""
    vw, vh = viewport.get("width", 1280), viewport.get("height", 720)
    ad_boxes = _get_ad_bounding_boxes(page)

    for _attempt in range(30):
        x = random.randint(80, vw - 80)
        y = random.randint(100, vh - 100)

        safe = True
        for ax, ay, aw, ah in ad_boxes:
            # 扩展广告边界 margin px
            if (ax - margin <= x <= ax + aw + margin and
                    ay - margin <= y <= ay + ah + margin):
                safe = False
                break
        if safe:
            return x, y

    # 兜底：选屏幕中间偏上（广告通常在下半部分）
    return random.randint(vw // 3, 2 * vw // 3), random.randint(80, vh // 3)


# --------- CDP 鼠标事件 ----------

def _cdp_mouse_move(
    cdp_session: Any,
    from_x: int, from_y: int,
    to_x: int, to_y: int,
    steps: int = 5,
) -> None:
    """通过 CDP 发送分步鼠标移动事件（带人类手抖噪声）"""
    for i in range(1, steps + 1):
        t = i / steps
        # 贝塞尔平滑 + 手抖噪声
        cur_x = int(from_x + (to_x - from_x) * t + random.gauss(0, 1.5))
        cur_y = int(from_y + (to_y - from_y) * t + random.gauss(0, 1.5))
        ts = int(time.time() * 1000)
        cdp_session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": cur_x, "y": cur_y,
            "modifiers": 0, "button": "none",
            "timestamp": ts,
        })
        time.sleep(random.uniform(0.01, 0.04))


def _cdp_click(
    cdp_session: Any, x: int, y: int,
) -> None:
    """通过 CDP 发送完整鼠标按下→释放序列 (isTrusted=true)"""
    ts = int(time.time() * 1000)
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": x, "y": y,
        "button": "left", "clickCount": 1,
        "timestamp": ts,
    })
    time.sleep(random.uniform(0.06, 0.20))
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": x, "y": y,
        "button": "left", "clickCount": 1,
        "timestamp": ts + int(random.uniform(80, 200)),
    })


# --------- 弹窗触发核心 ----------

# 全局冷却计时（进程内）
_LAST_POPUNDER_TS: float = 0.0


def trigger_popunder(
    page: Any,
    context: Any,
    *,
    stay_sec: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[Any], Dict[str, Any]]:
    """
    通过 CDP 层可信用户手势触发 HilltopAds Pop-under 弹窗。

    返回：
      (success, popunder_page, diagnostics)
        success:       弹窗是否成功触发
        popunder_page: 弹窗的 Playwright Page 对象（成功时），否则 None
        diagnostics:   {"triggered", "url", "stay_actual", "load_state", ...}
    """
    global _LAST_POPUNDER_TS
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    # 冷却检查
    now = time.time()
    cooldown = cfg.get("cooldown_between_triggers_s", 90)
    if now - _LAST_POPUNDER_TS < cooldown:
        _log.info(
            "Pop-under 冷却中（距上次 %d s < %d s），跳过",
            int(now - _LAST_POPUNDER_TS), cooldown,
        )
        return False, None, {"triggered": False, "reason": "cooldown"}

    # 随机概率
    prob = cfg.get("trigger_probability", 0.40)
    if random.random() > prob:
        _log.debug("Pop-under 随机跳过（概率 %.0f%%）", prob * 100)
        return False, None, {"triggered": False, "reason": "probability_skip"}

    stay = float(stay_sec or random.randint(
        cfg.get("popunder_stay_min", 15),
        cfg.get("popunder_stay_max", 25),
    ))
    margin = cfg.get("ad_safe_margin_px", 60)
    move_steps = cfg.get("cdp_move_steps", 5)

    try:
        # 1. 获取视口和当前鼠标位置
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        try:
            pos = page.evaluate("() => ({x: (window.__pw_last_x || 400), y: (window.__pw_last_y || 300)})")
            start_x, start_y = int(pos["x"]), int(pos["y"])
        except Exception:
            start_x, start_y = 400, 300

        # 2. 选取安全坐标（避让广告容器）
        safe_x, safe_y = _pick_safe_coordinates(page, viewport, margin)

        # 3. 记录弹窗前窗口数
        handles_before = len(context.pages)

        # 4. 建立 CDP 通道
        cdp = context.new_cdp_session(context.pages[0])

        # 5. 注入 bridged scroll handler（增加 HilltopAds tag 触发概率）
        page.evaluate("""
            if (!window.__ht_pop_primed) {
                window.__ht_pop_primed = false;
                document.addEventListener('scroll', function _htScroll() {
                    window.__ht_pop_primed = true;
                    document.removeEventListener('scroll', _htScroll);
                }, {once: false, passive: true});
            }
        """)

        # 6. CDP 鼠标移动 + 点击
        _cdp_mouse_move(cdp, start_x, start_y, safe_x, safe_y, steps=move_steps)
        time.sleep(random.uniform(0.05, 0.15))
        _cdp_click(cdp, safe_x, safe_y)

        # 7. 等待弹窗创建
        max_wait = cfg.get("max_wait_for_popup_s", 3.0)
        deadline = time.time() + max_wait
        popunder_page = None
        while time.time() < deadline:
            time.sleep(0.3)
            pages_now = context.pages
            if len(pages_now) > handles_before:
                popunder_page = pages_now[-1]
                break

        if popunder_page is None:
            _log.warning("Pop-under 弹窗未创建（%d s 内无新标签）", max_wait)
            return False, None, {"triggered": False, "reason": "no_new_tab"}

        # 8. 记录弹窗 URL
        try:
            pop_url = popunder_page.url or ""
        except Exception:
            pop_url = ""

        # 9. 等待落地页加载
        load_state = "unknown"
        try:
            popunder_page.wait_for_load_state(
                "domcontentloaded",
                timeout=cfg.get("popunder_load_timeout_ms", 10000),
            )
            load_state = "domcontentloaded"
            time.sleep(2)  # 额外等 2s 让图片/脚本加载
        except Exception:
            load_state = "timeout_or_error"

        # 10. 保持弹窗存活（模拟用户无视弹窗继续浏览原站）
        _log.info(
            "[Pop-under] 弹窗已创建: %s, 停留 %d s, 加载状态=%s",
            pop_url[:100] if pop_url else "(blank)", stay, load_state,
        )
        time.sleep(stay)

        # 11. 关闭弹窗
        try:
            popunder_page.close()
        except Exception:
            pass

        _LAST_POPUNDER_TS = time.time()
        return True, popunder_page, {
            "triggered": True,
            "url": pop_url[:200],
            "stay_actual": stay,
            "load_state": load_state,
            "click_coords": (safe_x, safe_y),
        }

    except Exception as e:
        _log.warning(
            "[Pop-under] 触发异常: %s: %s", type(e).__name__, e,
        )
        return False, None, {"triggered": False, "reason": f"exception:{type(e).__name__}"}


def should_trigger_for_network(detected_network: str) -> bool:
    """判断是否应该为此联盟触发 Pop-under 弹窗"""
    if not detected_network or detected_network == "无":
        return False
    net_lower = detected_network.lower()
    return any(kw in net_lower for kw in ("hilltopads", "hilltop", "evadav"))


# --------- 自检 ----------

def self_test() -> Dict[str, bool]:
    """模块自检（不依赖浏览器）：验证核心逻辑可用"""
    results: Dict[str, bool] = {}
    # 坐标选取
    from_vp = {"width": 1280, "height": 720}
    # 无需真实 page，直接测坐标逻辑
    x, y = _pick_safe_coordinates(None, from_vp, margin=60)
    results["coords_in_bounds"] = 80 <= x <= 1200 and 100 <= y <= 620
    # 网络判断
    results["detect_hilltop"] = should_trigger_for_network("HilltopAds")
    results["detect_evadav"] = should_trigger_for_network("EvaDav")
    results["detect_none"] = not should_trigger_for_network("无")
    results["detect_adsense"] = not should_trigger_for_network("AdSense")
    return results


if __name__ == "__main__":
    print("Pop-under Trigger 模块自检:")
    for k, v in self_test().items():
        status = "PASS" if v else "FAIL"
        print(f"  {k}: {status}")
