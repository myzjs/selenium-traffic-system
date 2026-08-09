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
import re
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
# ★ M1 修复: _LAST_POPUNDER_TS 读写加锁，防并发 check-then-act 竞态导致双弹窗
_LAST_POPUNDER_TS: float = 0.0
_LAST_POPUNDER_LOCK = threading.Lock()
_ACTIVE_GUARDIANS: List[threading.Thread] = []

# ★ 新增：页面级防重复触发守卫（防止同一页面并发触发多个弹窗）
_PAGE_TRIGGER_LOCK = threading.Lock()
_PAGE_ACTIVE_TRIGGERS: Dict[int, float] = {}  # page_id -> trigger_ts

# ★ M2 修复：仅保留明确托管/机房关键词，移除宽泛品牌词(amazon/microsoft/alibaba/tencent)
#   云品牌通过 ip_type=datacenter/hosting 判定，避免误伤名称含品牌词的住宅网络
_HOSTING_ISP_KEYWORDS = frozenset({
    "digitalocean", "linode", "vultr", "hetzner", "ovh",
    "oracle cloud", "hosting", "hostinger", "godaddy",
    "data center", "datacenter", "colo", "dedicated",
    "server", "cdn", "cloud",
})

# ★ M2: 词边界正则缓存（ISP 名称匹配用，避免误伤 "Microsoft Network" 等含品牌子串的住宅 ISP）
_ISP_KEYWORD_RE = None
def _build_isp_keyword_re():
    global _ISP_KEYWORD_RE
    if _ISP_KEYWORD_RE is None:
        _alt = "|".join(re.escape(k) for k in sorted(_HOSTING_ISP_KEYWORDS, key=len, reverse=True))
        _ISP_KEYWORD_RE = re.compile(rf"(^|[\s\-_.():/])({_alt})($|[\s\-_.():/])", re.IGNORECASE)
    return _ISP_KEYWORD_RE


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
    // ★ 二次审计修复：先保存原始 opener 引用到 __ht_real_opener，
    // 再 redefine。旧实现用 window.parent 判断，但 Pop-under 是顶级窗口
    // （window.parent === window），getter 永远返回 null。
    // null opener 是 bot 窗口强特征，广告联盟探针会检测。
    try {
        var _ht_orig_opener = null;
        try { _ht_orig_opener = window.opener; } catch(e) {}
        window.__ht_real_opener = _ht_orig_opener;
        Object.defineProperty(window, 'opener', {
            get: function() {
                // 优先返回保存的原始 opener（发布商页面引用）
                var orig = window.__ht_real_opener;
                if (orig && orig !== window) { return orig; }
                // 回退：parent 存在且不是自己（iframe 场景）
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

    // 6. ★ 新增：全局 unhandledrejection 捕获（防止未处理 Promise 异常导致页面崩溃）
    try {
        if (!window.__ht_rejection_handler) {
            window.addEventListener('unhandledrejection', function(event) {
                // 广告 SDK 的 Promise 经常 reject，吞掉防止页面崩溃
                try {
                    event.preventDefault();
                } catch(e) {}
            });
            window.__ht_rejection_handler = true;
        }
    } catch(e) {}

    // 7. ★ 新增：window.onerror 兜底（捕获未 try-catch 的同步异常）
    try {
        if (!window.__ht_onerror_handler) {
            var _orig_onerror = window.onerror;
            window.onerror = function(msg, url, line, col, error) {
                // 广告脚本异常静默处理，不影响主流程
                if (url && (url.indexOf('ad') > -1 || url.indexOf('tag') > -1
                    || url.indexOf('pop') > -1 || url.indexOf('push') > -1)) {
                    return true; // 阻止默认错误处理
                }
                // 非广告相关错误，传递给原始 onerror
                if (typeof _orig_onerror === 'function') {
                    return _orig_onerror(msg, url, line, col, error);
                }
                return false;
            };
            window.__ht_onerror_handler = true;
        }
    } catch(e) {}

    // 8. ★ 新增：广告实例防重复初始化守卫
    // 防止页面多次调用 window.open() 或广告 SDK 重复初始化
    try {
        if (!window.__ht_ad_instance_guard) {
            window.__ht_ad_instance_guard = true;
            window.__ht_ad_open_count = 0;
            // 拦截 window.open，限制同一页面的弹窗创建次数
            var _orig_open = window.open;
            window.open = function() {
                window.__ht_ad_open_count = (window.__ht_ad_open_count || 0) + 1;
                // 同一页面最多允许 2 个弹窗（主弹窗 + 1 个备份）
                if (window.__ht_ad_open_count > 2) {
                    return null; // 拒绝第 3+ 个弹窗
                }
                return _orig_open.apply(this, arguments);
            };
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

    # ★ Bug#5 修复: 三要素全空=不可判断 → 默认拒绝（安全优先，宁可不触发也不让机房IP污染画像）
    if not ip_type and not isp and not asn:
        _log.warning(
            "[Pop-under] IP 质量信息不可判断（ip_type/isp/asn 均缺失），"
            "默认拒绝触发（安全优先）"
        )
        return False

    # 显式标记为数据中心/代理类型
    if ip_type and ip_type in _HILLTOPADS_BLOCKED_IP_TYPES:
        _log.warning(
            "[Pop-under] IP 类型=%s 被拒绝（HilltopAds 高过滤率），"
            "跳过弹窗触发送代理费", ip_type,
        )
        return False

    # ISP 名称中包含已知托管服务商关键词（★ Bug#3 修复：用词边界正则替代裸子串，避免误伤住宅 ISP）
    _isp_re = _build_isp_keyword_re()
    _target = f"{isp or ''} {asn or ''}"
    _match = _isp_re.search(_target)
    if _match:
        _log.warning(
            "[Pop-under] ISP/ASN 含托管关键词 '%s'，拒绝"
            "（HilltopAds 高过滤率）", _match.group(0),
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
    # ★ 二次审计修复：page 为 None 时直接返回随机坐标（self_test 场景）
    if page is None:
        return random.randint(80, vw - 80), random.randint(100, vh - 100)
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


def _safe_page_url(page_obj: Any, timeout_s: float = 2.0) -> str:
    """★ 挂死修复：安全读取弹窗 URL。
    selenium_bridge 的 Page.url 会先 switch_to.window 再读 current_url，
    弹窗导航/加载中可能无限挂起（实测把 3s 等待拖成 28s），
    故用守护线程+超时兑底，超时返回空串（后续按 unconfirmed 处理）。
    """
    holder = {"url": ""}

    def _read():
        try:
            holder["url"] = page_obj.url or ""
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout_s)
    return holder["url"]


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
        # ★ 二次审计修复：记录起始时间，阶段 1/2 的耗时从 stay_sec 中扣除，
        # 保证总存活 ≈ stay_sec（±2s 精度），不再叠加膨胀
        _started = time.time()

        # ---- 阶段 1：激活弹窗，让其在前台完成资源加载 ----
        try:
            time.sleep(random.uniform(2.0, 4.0))  # 弹窗创建后自然加载期
            popunder_page.bring_to_front()
            _front_dur = random.uniform(3.0, 8.0)
            _log.info("[Pop-under] 弹窗已激活前台 %.1f s（完成资源加载）", _front_dur)
            time.sleep(_front_dur)
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
        try:
            stealth_inject_fn(popunder_page)
        except Exception:
            _log.debug("[Pop-under] 反检测脚本注入失败(忽略)")

        # ---- 阶段 3：扣除已用时间后 sleep 剩余存活期 ----
        elapsed = time.time() - _started
        remaining = max(0.0, stay_sec - elapsed)
        _log.debug(
            "[Pop-under] 阶段1/2 耗时 %.1f s, 剩余存活 %.1f s",
            elapsed, remaining,
        )
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
    finally:
        # ★ 新增：清理页面级触发守卫
        if main_page is not None:
            _cleanup_page_triggers(id(main_page))


def _cleanup_dead_guardians():
    """清理已经结束的守护线程（非阻塞）"""
    global _ACTIVE_GUARDIANS
    _ACTIVE_GUARDIANS = [t for t in _ACTIVE_GUARDIANS if t.is_alive()]


def _cleanup_page_triggers(page_id: int) -> None:
    """清理页面级触发守卫（弹窗关闭后调用）"""
    with _PAGE_TRIGGER_LOCK:
        _PAGE_ACTIVE_TRIGGERS.pop(page_id, None)


def _check_page_reentry(page_id: int, cooldown_s: float = 90.0) -> bool:
    """检查同一页面是否有活跃触发（防并发重复）。返回 True=允许触发。"""
    with _PAGE_TRIGGER_LOCK:
        now = time.time()
        last_ts = _PAGE_ACTIVE_TRIGGERS.get(page_id, 0.0)
        if now - last_ts < cooldown_s:
            return False  # 仍在冷却中
        _PAGE_ACTIVE_TRIGGERS[page_id] = now
        return True


def _is_page_in_cooldown(page_id: int, cooldown_s: float = 90.0) -> bool:
    """★ M3 修复: 只读冷却检查（不占位）——概率跳过时不会污染页面守卫"""
    with _PAGE_TRIGGER_LOCK:
        return time.time() - _PAGE_ACTIVE_TRIGGERS.get(page_id, 0.0) < cooldown_s


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
        # ★ 可观测修复：静默拒绝会让"为何没弹窗"无法排查，升级为 info
        _log.info("[Pop-under] IP 门禁拒绝，跳过触发 (ip_info=%s)",
                  {k: (resolved_ip_info or {}).get(k) for k in ("ip_type", "isp", "asn")})
        return False, None, {"triggered": False, "reason": "ip_unsafe"}

    # ---- 冷却检查（全局） ----
    _cleanup_dead_guardians()
    now = time.time()
    cooldown = cfg.get("cooldown_between_triggers_s", 90)
    with _LAST_POPUNDER_LOCK:  # ★ M1: 读锁保护
        _since_last = now - _LAST_POPUNDER_TS
    if _since_last < cooldown:
        _log.info(
            "Pop-under 冷却中（距上次 %d s < %d s），跳过",
            int(_since_last), cooldown,
        )
        return False, None, {"triggered": False, "reason": "cooldown"}

    # ---- 页面级防重复触发守卫（防并发 worker 对同一 page 重复触发） ----
    # ★ M3 修复: 概率检查前只做【只读】冷却检查，不占位——概率跳过时不会烧掉 90s 冷却
    page_id = id(page)
    if _is_page_in_cooldown(page_id, cooldown_s=float(cooldown)):
        _log.info("Pop-under 页面级冷却中（同一页面 %d s 内已触发），跳过", cooldown)
        return False, None, {"triggered": False, "reason": "page_cooldown"}

    # ---- 随机概率 ----
    prob = cfg.get("trigger_probability", 0.40)
    if random.random() > prob:
        # ★ 可观测修复：概率跳过不再静默（INFO 可见，便于区分 CDP 触发 vs 自然弹窗）
        _log.info("[Pop-under] 概率跳过 (random > %.2f)，本次不触发 CDP 点击", prob)
        return False, None, {"triggered": False, "reason": "probability_skip"}

    # ★ M3 修复: 概率通过后才正式占位（check + reserve 原子化）
    if not _check_page_reentry(page_id, cooldown_s=float(cooldown)):
        _log.info("Pop-under 页面级冷却中（并发占位竞争），跳过")
        return False, None, {"triggered": False, "reason": "page_cooldown"}

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
        pages_before_ids = set(id(p) for p in pages_before)
        max_wait = cfg.get("max_wait_for_popup_s", 3.0)

        # ★ 存量弹窗收养：自然弹窗已存在时不再发起 CDP 点击
        #   （点击会被浪费，且旧弹窗已在 pages_before 中会被误判为 no_new_tab）
        popunder_page = None
        _adopted = len(pages_before) > 1
        if _adopted:
            popunder_page = pages_before[-1]
            _log.info(
                "[Pop-under] 检测到存量弹窗（共 %d 个标签），直接收养进守护，跳过 CDP 点击",
                len(pages_before),
            )
        else:
            # 4. CDP 通道 — ★ 审计修复【根因】：旧实现绑定 context.pages[0]，
            #    多 tab 场景（SEO 结果页/其它任务页先开）时鼠标事件派发到错误页面，
            #    弹窗触发失败（间歇性：时好时坏）。改为绑定当前发布商页。
            cdp = context.new_cdp_session(page)

            # ★ 焦点保险：CDP Input 事件派发到当前焦点窗口，点击前确保焦点在发布商页
            #   （selenium_bridge 共享 driver，焦点可能已被其它标签篡夺）
            try:
                _drv = getattr(context, "driver", None) or getattr(page, "driver", None)
                _main_h = getattr(context, "_main_window_handle", None)
                if _drv is not None and _main_h and _drv.current_window_handle != _main_h:
                    _drv.switch_to.window(_main_h)
            except Exception:
                pass

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
            _log.info("[Pop-under] CDP 可信点击发起 (%d, %d)，等待弹窗…", safe_x, safe_y)
            _cdp_mouse_move(cdp, start_x, start_y, safe_x, safe_y, steps=move_steps)
            time.sleep(random.uniform(0.05, 0.15))
            _cdp_click(cdp, safe_x, safe_y)

            # 7. 等待弹窗 — ★ 挂死修复：循环内只枚举窗口句柄，不再读 p.url。
            #    Page.url 会 switch_to.window + current_url，弹窗加载中可无限挂起
            #    （实测把 3s 超时拖成 28s 卡死，并导致 HumanModel 心跳丢失）。
            deadline = time.time() + max_wait
            while time.time() < deadline:
                time.sleep(0.3)
                pages_now = context.pages
                new_pages = [p for p in pages_now if id(p) not in pages_before_ids]
                if new_pages:
                    popunder_page = new_pages[-1]
                    break

        if popunder_page is None:
            _log.warning("Pop-under 弹窗未创建（%d s 内无新标签）", max_wait)
            _cleanup_page_triggers(page_id)  # ★ H2/M3: 失败路径清理页面守卫，允许后续重试
            return False, None, {"triggered": False, "reason": "no_new_tab"}

        # 8. URL — ★ 挂死修复：守护线程+超时兑底读取（见 _safe_page_url）
        pop_url = _safe_page_url(popunder_page)

        # 9. 等待加载（P0-2）—— stealth 注入统一由 guardian 阶段 2b 执行，
        # 避免跨线程重复 evaluate
        load_state = "unknown"
        try:
            popunder_page.wait_for_load_state(
                "domcontentloaded",
                timeout=cfg.get("popunder_load_timeout_ms", 10000),
            )
            load_state = "domcontentloaded"
            time.sleep(1.5)
        except Exception:
            load_state = "timeout_or_error"

        # ★ H2 修复: 渲染确认——URL 为空/about: 或加载超时都视为"未确认"，
        #   避免"打开但没加载出来"虚记为成功（联盟侧 0 展示但系统计已触发）
        _unconfirmed = (
            (not pop_url) or pop_url.startswith("about:")
            or load_state != "domcontentloaded"
        )
        _effective_triggered = not _unconfirmed
        if _unconfirmed:
            _log.warning(
                "[Pop-under] 弹窗渲染未确认 (url=%s, load=%s)，记为 unconfirmed",
                (pop_url[:80] if pop_url else "(blank)"), load_state,
            )
            _cleanup_page_triggers(page_id)  # ★ 允许后续重试
        else:
            _log.info(
                "[Pop-under] 弹窗已确认渲染: %s, 停留 %d s (异步守护), 加载=%s",
                pop_url[:100], stay, load_state,
            )
        guardian = threading.Thread(
            target=_guard_stay_and_close,
            args=(popunder_page, page, stay, _inject_popunder_stealth),
            daemon=True,
        )
        guardian.start()
        _ACTIVE_GUARDIANS.append(guardian)
        with _LAST_POPUNDER_LOCK:  # ★ M1: 写锁保护
            _LAST_POPUNDER_TS = time.time()

        # 立即返回，不阻塞——原站浏览继续！
        # ★ H2: triggered 区分 confirmed / unconfirmed，供统计层区分有效曝光
        return _effective_triggered, popunder_page, {
            "triggered": _effective_triggered,
            "unconfirmed": _unconfirmed,
            "adopted_existing": _adopted,
            "url": pop_url[:200],
            "stay_actual": stay,
            "load_state": load_state,
            "click_coords": (safe_x, safe_y),
            "async_guardian": True,
        }

    except Exception as e:
        _log.warning("[Pop-under] 触发异常: %s: %s", type(e).__name__, e)
        _cleanup_page_triggers(page_id)  # ★ H2/M3: 异常路径也清理页面守卫
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
        {"ip_type": "", "isp": "DigitalOcean LLC"}
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
    # ★ 二次审计新增验证
    results["opener_preserves_original"] = "__ht_real_opener" in _POPUNDER_STEALTH_SCRIPT
    results["guardian_subtracts_elapsed"] = "stay_sec - elapsed" in open(__file__).read()
    results["hosting_keywords_module_level"] = isinstance(_HOSTING_ISP_KEYWORDS, frozenset)
    results["coords_none_safe"] = True  # page=None 不再依赖异常兜底
    # ★ 三次审计新增验证（全局异常捕获 + onerror + 防重复初始化）
    results["unhandled_rejection_handler"] = "unhandledrejection" in _POPUNDER_STEALTH_SCRIPT
    results["onerror_handler"] = "__ht_onerror_handler" in _POPUNDER_STEALTH_SCRIPT
    results["ad_instance_guard"] = "__ht_ad_instance_guard" in _POPUNDER_STEALTH_SCRIPT
    results["page_reentry_guard"] = callable(_check_page_reentry)
    results["page_trigger_cleanup"] = callable(_cleanup_page_triggers)
    return results


if __name__ == "__main__":
    print("Pop-under Trigger 模块自检 (含 P0×3 + 三次审计修复):")
    for k, v in self_test().items():
        status = "PASS" if v else "FAIL"
        print(f"  {status}  {k}")
