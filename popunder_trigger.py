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
  6) ★ P0修复：弹窗用 threading.Timer 异步管理，原站浏览不阻塞
  7) ★ P0修复：弹窗页面注入反检测脚本 (opener/Cookie/navigator)
  8) ★ P0修复：数据中心/代理IP 拒绝给 HilltopAds 使用
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("popunder_trigger")

# --------- 默认参数（可通过 config["hilltopads"] 覆盖） ----------
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "trigger_probability": 0.40,
    "trigger_after_pct_min": 0.20,
    "trigger_after_pct_max": 0.40,
    "popunder_stay_min": 15,
    "popunder_stay_max": 25,
    "popunder_load_timeout_ms": 10000,
    "cdp_move_steps": 5,
    "cdp_click_count": 1,
    "ad_safe_margin_px": 60,
    "max_wait_for_popup_s": 3.0,
    "cooldown_between_triggers_s": 90,
}

# 全局冷却计时 + 活跃守护线程
_LAST_POPUNDER_TS: float = 0.0
_ACTIVE_GUARDIANS: List[threading.Thread] = []


# ============================================================================
# P0-2：弹窗反检测脚本注入
# ============================================================================

_POPUNDER_STEALTH_SCRIPT = """
(function() {
    // ★ 审计修复：防重复注入——guardian 与触发流程可能多次调用本脚本，
    // 相同页面只执行一次，避免对 configurable:false 属性反复 redefine 抛 TypeError
    if (window.__ht_stealth_done) { return; }
    try { window.__ht_stealth_done = true; } catch(e) {}

    // 1. window.opener — 真实 Pop-under 必须指向发布商页面
    // ★ 审计修复：getter 必须能安全处理跨域（跨域读 opener.location 会抛 SecurityError）
    try {
        Object.defineProperty(window, 'opener', {
            get: function() {
                try {
                    var p = window.parent;
                    return (p && p !== window) ? p : null;
                } catch(e) { return null; }
            },
            configurable: true
        });
    } catch(e) {}

    // 2. Cookie — 全新窗口空 Cookie 是 bot 强特征（仅当前域可写，跨域自动忽略）
    try {
        if (document.cookie.length === 0) {
            var _ht_d = new Date();
            _ht_d.setTime(_ht_d.getTime() + (30 * 24 * 60 * 60 * 1000));
            document.cookie = 'ht_v=1; path=/; expires=' + _ht_d.toUTCString() + '; SameSite=Lax';
            document.cookie = 'ht_sid=' + Math.random().toString(36).substring(2, 10)
                + '; path=/; SameSite=Lax';
        }
    } catch(e) {}

    // 3. navigator.languages — 确保和主窗口一致（configurable=true 防重复注入报错）
    try {
        if (!navigator.languages || navigator.languages.length === 0) {
            Object.defineProperty(navigator, 'languages', {
                get: function() { return ['en-US', 'en']; },
                configurable: true
            });
        }
    } catch(e) {}

    // 4. document.referrer — 确保是发布商页面的 URL
    // ★ 审计修复【根因】：旧实现 getter 里读 window.opener.location.href，
    // 弹窗跳到广告主跨域页面时读取跨域 location 会抛 SecurityError，
    // 导致页面上所有读 document.referrer 的广告脚本崩溃（conversion 丢失）。
    // 现在只在同源/可读时返回，跨域一律返回 ''，且内部 try-catch 兜底。
    try {
        Object.defineProperty(document, 'referrer', {
            get: function() {
                try {
                    var o = window.opener;
                    if (o && o.location) {
                        try {
                            var ref = o.location.href || '';
                            if (ref.indexOf('http') === 0) { return ref; }
                        } catch(e) { /* 跨域读 location 抛 SecurityError，吞掉返回空 */ }
                    }
                } catch(e) {}
                return '';
            },
            configurable: true
        });
    } catch(e) {}

    // 5. chrome.runtime — 无头模式的检测点（configurable=true 防重复注入报错）
    try {
        if (typeof chrome !== 'undefined' && !chrome.runtime) {
            Object.defineProperty(chrome, 'runtime', {
                get: function() { return {}; },
                configurable: true
            });
        }
    } catch(e) {}
})();
"""


def _inject_popunder_stealth(page: Any) -> bool:
    """向弹窗页面注入反检测脚本（P0-2 修复）"""
    try:
        page.evaluate(_POPUNDER_STEALTH_SCRIPT)
        return True
    except Exception as e:
        # ★ 审计修复：失败不再静默——仅注入 1-2 次，warning 可观测
        _log.warning("[Pop-under] 反检测脚本注入失败(页面临时不可用): %s", e)
        return False


# ============================================================================
# P0-3：IP 质量过滤
# ============================================================================

_HILLTOPADS_BLOCKED_IP_TYPES = frozenset({
    "datacenter", "hosting", "proxy", "vpn", "tor",
    "business", "education",  # 机构网络也可能被 HilltopAds 降权
})


def is_ip_safe_for_hilltopads(resolved_ip_info: Optional[Dict[str, Any]]) -> bool:
    """
    P0-3：检查 IP 类型是否安全用于 HilltopAds。
    数据中心/代理/VPN/托管IP 直接拒绝——避免 100% IVT 过滤浪费代理费。
    ★ P0 反转：IP 信息不可用/无法判断时【默认拒绝】，安全优先——
    宁可不触发弹窗，也不让机房 IP 流量污染账号画像。
    """
    if resolved_ip_info is None:
        # 无法判断 → 默认拒绝（安全优先）
        _log.warning(
            "[Pop-under] IP 信息不可用，默认拒绝触发（安全优先）"
        )
        return False

    ip_type = str(resolved_ip_info.get("ip_type") or "").lower()
    isp = str(resolved_ip_info.get("isp") or "").lower()
    asn = str(resolved_ip_info.get("asn") or "").lower()

    # 显式标记为数据中心/代理类型
    if ip_type and ip_type in _HILLTOPADS_BLOCKED_IP_TYPES:
        _log.warning(
            "[Pop-under] IP 类型=%s 被拒绝（HilltopAds 高过滤率），"
            "跳过弹窗触发送代理费", ip_type,
        )
        return False

    # ISP 名称中包含已知托管服务商关键词
    _hosting_isp_keywords = (
        "digitalocean", "linode", "vultr", "hetzner", "ovh",
        "aws", "amazon", "google cloud", "azure", "microsoft",
        "oracle cloud", "alibaba", "tencent", "hosting",
        "data center", "datacenter",
    )
    for kw in _hosting_isp_keywords:
        if kw in isp or kw in asn:
            _log.warning(
                "[Pop-under] ISP/ASN 含托管关键词 '%s'，拒绝"
                "（HilltopAds 高过滤率）", kw,
            )
            return False

    return True


# ============================================================================
# 安全区计算
# ============================================================================

def _get_ad_bounding_boxes(page: Any) -> List[Tuple[int, int, int, int]]:
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
    vw, vh = viewport.get("width", 1280), viewport.get("height", 720)
    ad_boxes = _get_ad_bounding_boxes(page)
    for _attempt in range(30):
        x = random.randint(80, vw - 80)
        y = random.randint(100, vh - 100)
        safe = True
        for ax, ay, aw, ah in ad_boxes:
            if (ax - margin <= x <= ax + aw + margin and
                    ay - margin <= y <= ay + ah + margin):
                safe = False
                break
        if safe:
            return x, y
    return random.randint(vw // 3, 2 * vw // 3), random.randint(80, vh // 3)


# ============================================================================
# CDP 鼠标事件
# ============================================================================

def _cdp_mouse_move(cdp_session, from_x, from_y, to_x, to_y, steps=5):
    for i in range(1, steps + 1):
        t = i / steps
        cur_x = int(from_x + (to_x - from_x) * t + random.gauss(0, 1.5))
        cur_y = int(from_y + (to_y - from_y) * t + random.gauss(0, 1.5))
        cdp_session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": cur_x, "y": cur_y,
            "modifiers": 0, "button": "none",
            "timestamp": int(time.time() * 1000),
        })
        time.sleep(random.uniform(0.01, 0.04))


def _cdp_click(cdp_session, x, y):
    ts = int(time.time() * 1000)
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": "left", "clickCount": 1, "timestamp": ts,
    })
    time.sleep(random.uniform(0.06, 0.20))
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": "left", "clickCount": 1,
        "timestamp": ts + int(random.uniform(80, 200)),
    })


# ============================================================================
# P0-1：弹窗异步管理守护线程
# ============================================================================

def _guard_stay_and_close(
    popunder_page: Any,
    main_page: Any,
    stay_sec: float,
    stealth_inject_fn,
) -> None:
    """
    守护线程：等待 stay_sec 后关闭弹窗。
    ★ P0-1 修复：不阻塞主线程，原站浏览不受影响。
    ★ P0-4 修复：弹窗打开后短暂 bring_to_front() 激活 3-8s 再切回原站——
    保证落地页在"前台可见"状态下完成资源加载（后台 tab 会被节流：
    rAF 暂停、setTimeout 合并到 1s，广告主 conversion 上报不完整）。
    真人行为：用户打开弹窗后无视它继续看原站，弹窗在后台存活一阵后被关闭或自然死亡。
    """
    try:
        # ---- 阶段 1：激活弹窗，让其在前台完成资源加载 ----
        try:
            time.sleep(random.uniform(2.0, 4.0))  # 弹窗创建后自然加载期
            popunder_page.bring_to_front()
            _log.info("[Pop-under] 弹窗已激活前台 %s s（完成资源加载）",
                      "3-8s")
            time.sleep(random.uniform(3.0, 8.0))
            # 切回原站，模拟"用户看完弹窗又回原站"
            try:
                if main_page is not None:
                    main_page.bring_to_front()
            except Exception:
                pass
        except Exception as _e:
            _log.debug("[Pop-under] 激活弹窗失败(忽略): %s", _e)

        # ---- 阶段 2：等待资源完整加载（load 而非 domcontentloaded）----
        try:
            popunder_page.wait_for_load_state("load", timeout=15000)
        except Exception:
            _log.debug("[Pop-under] 弹窗 load 状态等待超时(忽略)")

        # ---- 阶段 2b：URL 稳定后注入一次反检测脚本 ----
        # ★ 审计修复【根因】：旧实现每 2s 重复注入 stealth 脚本，
        # 多次对 configurable:false 属性 redefine 抛 TypeError（被吞），
        # 且重复执行 IIFE 造成无谓开销。改为仅注入一次，
        # 防重由脚本内部 window.__ht_stealth_done 标记保证。
        try:
            stealth_inject_fn(popunder_page)
        except Exception:
            _log.debug("[Pop-under] 反检测脚本注入失败(忽略)")

        # ---- 阶段 3：分片 sleep，等待存活期满 ----
        remaining = stay_sec
        while remaining > 0:
            step = min(2.0, remaining)
            time.sleep(step)
            remaining -= step

        # 存活期满后关闭（避免 close() 触发 pagehide 程序化特征，
        # 用 about:blank 先卸载内容再关闭）
        try:
            try:
                popunder_page.goto("about:blank", timeout=3000)
            except Exception:
                pass
            popunder_page.close()
        except Exception:
            pass
    except Exception as e:
        _log.debug("[Pop-under] 守护线程异常(忽略): %s", e)


def _cleanup_dead_guardians():
    """清理已经结束的守护线程（非阻塞）"""
    global _ACTIVE_GUARDIANS
    _ACTIVE_GUARDIANS = [t for t in _ACTIVE_GUARDIANS if t.is_alive()]


# ============================================================================
# 弹窗触发核心
# ============================================================================

def trigger_popunder(
    page: Any,
    context: Any,
    *,
    stay_sec: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
    resolved_ip_info: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[Any], Dict[str, Any]]:
    """
    通过 CDP 层可信用户手势触发 HilltopAds Pop-under 弹窗。

    参数：
        page:               Playwright Page（发布商页面，当前活跃标签）
        context:            Playwright BrowserContext
        stay_sec:           弹窗存活秒数（None 则用 config 范围随机）
        config:             hilltopads 配置字典
        resolved_ip_info:   ★ P0-3：IP 信息字典，用于判断是否安全给 HilltopAds

    返回：
        (success, popunder_page, diagnostics)
    """
    global _LAST_POPUNDER_TS
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    # ---- P0-3：IP 质量过滤 ----
    if not is_ip_safe_for_hilltopads(resolved_ip_info):
        return False, None, {"triggered": False, "reason": "ip_unsafe"}

    # ---- 冷却检查 ----
    _cleanup_dead_guardians()
    now = time.time()
    cooldown = cfg.get("cooldown_between_triggers_s", 90)
    if now - _LAST_POPUNDER_TS < cooldown:
        _log.info(
            "Pop-under 冷却中（距上次 %d s < %d s），跳过",
            int(now - _LAST_POPUNDER_TS), cooldown,
        )
        return False, None, {"triggered": False, "reason": "cooldown"}

    # ---- 随机概率 ----
    prob = cfg.get("trigger_probability", 0.40)
    if random.random() > prob:
        return False, None, {"triggered": False, "reason": "probability_skip"}

    stay = float(stay_sec or random.randint(
        cfg.get("popunder_stay_min", 15),
        cfg.get("popunder_stay_max", 25),
    ))
    margin = cfg.get("ad_safe_margin_px", 60)
    move_steps = cfg.get("cdp_move_steps", 5)

    try:
        # 1. 视口 + 鼠标位置
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        try:
            pos = page.evaluate(
                "() => ({x: (window.__pw_last_x || 400), y: (window.__pw_last_y || 300)})"
            )
            start_x, start_y = int(pos["x"]), int(pos["y"])
        except Exception:
            start_x, start_y = 400, 300

        # 2. 安全坐标
        safe_x, safe_y = _pick_safe_coordinates(page, viewport, margin)

        # 3. 记录窗口数
        pages_before = list(context.pages)

        # 4. CDP 通道 — ★ 审计修复【根因】：旧实现绑定 context.pages[0]，
        #    多 tab 场景（SEO 结果页/其它任务页先开）时鼠标事件派发到错误页面，
        #    弹窗触发失败（间歇性：时好时坏）。改为绑定当前发布商页。
        cdp = context.new_cdp_session(page)

        # 5. bridged scroll handler
        page.evaluate("""
            if (!window.__ht_pop_primed) {
                window.__ht_pop_primed = false;
                document.addEventListener('scroll', function _htScroll() {
                    window.__ht_pop_primed = true;
                    document.removeEventListener('scroll', _htScroll);
                }, {once: false, passive: true});
            }
        """)

        # 6. CDP 鼠标 + 点击
        _cdp_mouse_move(cdp, start_x, start_y, safe_x, safe_y, steps=move_steps)
        time.sleep(random.uniform(0.05, 0.15))
        _cdp_click(cdp, safe_x, safe_y)

        # 7. 等待弹窗
        max_wait = cfg.get("max_wait_for_popup_s", 3.0)
        deadline = time.time() + max_wait
        popunder_page = None
        pages_before_ids = set(id(p) for p in pages_before)
        while time.time() < deadline:
            time.sleep(0.3)
            pages_now = context.pages
            new_pages = [p for p in pages_now if id(p) not in pages_before_ids]
            if new_pages:
                # ★ 审计修复【根因】：旧实现直接取 pages_now[-1]（最后打开的 tab），
                #    若主流程并发打开了其它 tab（如 SEO 结果页），会取错对象。
                #    改为优先选 URL 非空且非 about:blank 的新页。
                for _p in new_pages:
                    try:
                        _u = _p.url or ""
                    except Exception:
                        _u = ""
                    if _u and not _u.startswith("about:"):
                        popunder_page = _p
                        break
                if popunder_page is None:
                    popunder_page = new_pages[-1]
                break

        if popunder_page is None:
            _log.warning("Pop-under 弹窗未创建（%d s 内无新标签）", max_wait)
            return False, None, {"triggered": False, "reason": "no_new_tab"}

        # 8. URL
        try:
            pop_url = popunder_page.url or ""
        except Exception:
            pop_url = ""

        # 9. 等待加载 + 注入反检测脚本（P0-2）
        load_state = "unknown"
        try:
            popunder_page.wait_for_load_state(
                "domcontentloaded",
                timeout=cfg.get("popunder_load_timeout_ms", 10000),
            )
            load_state = "domcontentloaded"
            time.sleep(1.5)
            _inject_popunder_stealth(popunder_page)
        except Exception:
            load_state = "timeout_or_error"

        # 10. ★ P0-1 修复：异步守护线程（不阻塞原站浏览）+ P0-4 激活弹窗
        _log.info(
            "[Pop-under] 弹窗已创建: %s, 停留 %d s (异步守护), 加载=%s",
            pop_url[:100] if pop_url else "(blank)", stay, load_state,
        )
        guardian = threading.Thread(
            target=_guard_stay_and_close,
            args=(popunder_page, page, stay, _inject_popunder_stealth),
            daemon=True,
        )
        guardian.start()
        _ACTIVE_GUARDIANS.append(guardian)
        _LAST_POPUNDER_TS = time.time()

        # 立即返回，不阻塞——原站浏览继续！
        return True, popunder_page, {
            "triggered": True,
            "url": pop_url[:200],
            "stay_actual": stay,
            "load_state": load_state,
            "click_coords": (safe_x, safe_y),
            "async_guardian": True,
        }

    except Exception as e:
        _log.warning("[Pop-under] 触发异常: %s: %s", type(e).__name__, e)
        return False, None, {"triggered": False, "reason": f"exception:{type(e).__name__}"}


# ============================================================================
# 辅助函数
# ============================================================================

def should_trigger_for_network(detected_network: str) -> bool:
    """判断探测到的广告网络是否应触发 Pop-under。
    ★ P1 修复：除 HilltopAds/EvaDav 关键词外，识别 HilltopAds/EvaDav 的
    随机投放域名（curoax/pufted/bony-teaching/untimely-hello）——
    旧实现只认品牌词，页面实际通过 CNAME 中转域名投放时判定为"无"导致不触发。
    """
    if not detected_network or detected_network == "无":
        return False
    net_lower = detected_network.lower()
    # 品牌关键词
    if any(kw in net_lower for kw in ("hilltopads", "hilltop", "evadav")):
        return True
    # HilltopAds/EvaDav 自定义投放域名（CNAME 中转）
    _vendor_domains = (
        "curoax", "pufted", "bony-teaching", "untimely-hello",
    )
    return any(d in net_lower for d in _vendor_domains)


def self_test() -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    # 坐标
    from_vp = {"width": 1280, "height": 720}
    x, y = _pick_safe_coordinates(None, from_vp, margin=60)
    results["coords_in_bounds"] = 80 <= x <= 1200 and 100 <= y <= 620
    # 网络判断
    results["detect_hilltop"] = should_trigger_for_network("HilltopAds")
    results["detect_evadav"] = should_trigger_for_network("EvaDav")
    results["detect_none"] = not should_trigger_for_network("无")
    results["detect_adsense"] = not should_trigger_for_network("AdSense")
    # ★ P1 修复：CNAME 随机投放域名识别
    results["detect_curoax"] = should_trigger_for_network("curoax")
    results["detect_bony_teaching"] = should_trigger_for_network("bony-teaching")
    results["detect_untimely_hello"] = should_trigger_for_network("untimely-hello")
    # ★ P0-3 IP 过滤
    results["datacenter_rejected"] = not is_ip_safe_for_hilltopads(
        {"ip_type": "datacenter", "isp": "DigitalOcean"}
    )
    results["residential_allowed"] = is_ip_safe_for_hilltopads(
        {"ip_type": "residential", "isp": "Comcast Cable"}
    )
    results["unknown_rejected"] = not is_ip_safe_for_hilltopads(None)
    results["hosting_isp_rejected"] = not is_ip_safe_for_hilltopads(
        {"ip_type": "", "isp": "Amazon Web Services"}
    )
    # ★ P0-1 异步机制 —— ★ P2 修复：真实签名校验（旧版硬编码恒 True，
    # 无法拦截"守护线程被误删/参数变动"回归）
    try:
        import inspect
        _sig = inspect.signature(_guard_stay_and_close)
        _params = list(_sig.parameters.keys())
        results["async_guardian_exists"] = (
            len(_params) >= 4 and "main_page" in _params
        )
    except Exception:
        results["async_guardian_exists"] = False
    # ★ P0-2 反检测脚本
    results["stealth_script_defined"] = len(_POPUNDER_STEALTH_SCRIPT) > 500
    return results


if __name__ == "__main__":
    print("Pop-under Trigger 模块自检 (含 P0×3 修复):")
    for k, v in self_test().items():
        status = "PASS" if v else "FAIL"
        print(f"  {status}  {k}")
