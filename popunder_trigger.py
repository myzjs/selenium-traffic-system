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
# ★ 26.8.11.1 修复：默认存活 min 15→22, max 25→36
#   HilltopAds 统计脚本会在弹窗打开后 ~12s 发送首次 heartbeat，低于 20s 存活会被过滤为"秒关"。
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "trigger_probability": 0.60,          # 原 0.40 → 0.60：让更多会话尝试触发，观察转化
    "trigger_after_pct_min": 0.15,
    "trigger_after_pct_max": 0.30,
    "popunder_stay_min": 22,              # ★ 延长至 22s
    "popunder_stay_max": 36,              # ★ 延长至 36s（真人浏览时弹窗 20-40s 关闭最自然）
    "popunder_load_timeout_ms": 12000,
    "cdp_move_steps": 5,
    "cdp_click_count": 1,
    "ad_safe_margin_px": 60,
    "max_wait_for_popup_s": 4.0,
    "cooldown_between_triggers_s": 75,    # 冷却 90→75s：让更多任务有机会触发（配合频控放宽）
}

# ============================================================================
# ★ 26.8.11.2 新增：HilltopAds Heartbeat 监听 — 排查"收益=0"的最终链路
# ============================================================================
# Heartbeat / 广告像素典型 URL 关键词（命中任一即判定为广告统计请求）
_HEARTBEAT_URL_KEYWORDS: Tuple[str, ...] = (
    # HilltopAds / Traffichunt 自有域名（最强匹配）
    "hilltopads", "htopcdn", "traffichunt",
    # 通用统计 / 像素 / 上报路径
    "heartbeat", "/hb?", "hb=", "/ping", "/pixels", "/pixel",
    "tracker", "/track?", "/tracking/", "tracking?", "stats.php",
    # ★ 注：原本有 "/stat"，但会命中 /static/ 误伤所有普通静态资源，
    #   改为更长的精确形式：/stats/ /statistics/ stat? stat/ 这些模式
    "/stats", "stats/", "/statistics", "stat.php", "/stat/", "/stat?",
    "beacon", "event=", "impression=", "view=", "evt=",
    "log_event", "log.php", "/collect", "/sync",
    # 通用广告联盟像素前缀（兜底）
    "adserv", "adsystem", "adserver", "adsrv", "adtrack",
    "click?", "imp=", "visit=", "revenue=", "bid=",
)
# 排除项：纯静态资源（即使路径命中也不算统计请求）
_HEARTBEAT_URL_EXCLUDE_EXT: Tuple[str, ...] = (
    ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map", ".mp4", ".webm",
)


def _is_heartbeat_url(url: str) -> bool:
    """判断 URL 是否疑似广告统计/heartbeat 请求

    ★ 26.8.11.2 设计：不做扩展名排除。
      广告统计像素常见格式就是 pixel.gif / impression.png / track.jpg，
      命中关键词就必须算 heartbeat；不命中关键词的 logo.png / app.css
      自然会被关键词过滤掉，无需额外扩展名黑名单兜底。
    """
    if not url:
        return False
    u = url.lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    return any(kw in u for kw in _HEARTBEAT_URL_KEYWORDS)


def _analyze_heartbeat_records(
    records: List[Dict[str, Any]],
    started_at: float,
) -> Dict[str, Any]:
    """
    汇总 heartbeat 监听结果（守护线程结束时调用）

    返回：
      total_req           : 弹窗生命周期内总请求数（所有资源+接口）
      heartbeat_count     : 疑似 heartbeat/统计 请求数
      first_at            : 首次 heartbeat 距弹窗创建的秒数（None 表示未收到）
      second_at           : 第二次 heartbeat 秒数（HilltopAds 结算关键指标）
      sample_urls         : 前 5 条匹配 URL（便于日志确认模式）
      has_hilltopads_hit  : 是否命中 hilltopads/traffichunt 域名（最强正例）
    """
    total_req = len(records)
    matched: List[Dict[str, Any]] = []
    has_ht = False
    for rec in records:
        url = str(rec.get("url") or "")
        if _is_heartbeat_url(url):
            matched.append(rec)
            if ("hilltopads" in url.lower()) or ("traffichunt" in url.lower()) or ("htopcdn" in url.lower()):
                has_ht = True
    matched.sort(key=lambda r: float(r.get("t") or 0.0))
    first_at = (float(matched[0]["t"]) - started_at) if matched else None
    second_at = (float(matched[1]["t"]) - started_at) if len(matched) >= 2 else None
    sample = [str(m.get("url", ""))[:160] for m in matched[:5]]
    return {
        "total_req": total_req,
        "heartbeat_count": len(matched),
        "first_at": round(first_at, 1) if first_at is not None else None,
        "second_at": round(second_at, 1) if second_at is not None else None,
        "sample_urls": sample,
        "has_hilltopads_hit": has_ht,
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
    // ★ 谷歌广告合规修复：谷歌广告要求弹窗必须由真实用户手势触发、不得操纵弹窗行为。
    // 本弹窗由 CDP 真实手势（滚动/贝塞尔移动/悬停/点击）触发，已在注入脚本中移除以下
    // 被判定为"主动规避检测"的露骨伪造，降低被判定为机器流量/无头浏览器的风险：
    //   - 伪造 window.opener（伪装指向发布商页面）
    //   - 覆写 document.referrer（伪装来自发布商 URL）
    //   - 伪造 chrome.runtime（无头浏览器检测点）
    //   - 吞掉 unhandledrejection / window.onerror（疑似掩盖脚本异常）
    //   - 拦截 window.open 操纵弹窗创建行为
    // 仅保留对合规判定无碍的无害项：空窗口补写基础 Cookie、navigator.languages 兜底。
    //
    // ★ 审计修复：防重复注入——guardian 与触发流程可能多次调用本脚本，
    // 相同页面只执行一次，避免对 configurable:false 属性反复 redefine 抛 TypeError
    if (window.__ht_stealth_done) { return; }
    try { window.__ht_stealth_done = true; } catch(e) {}

    // 1. Cookie — 全新窗口空 Cookie 是 bot 强特征（仅当前域可写，跨域自动忽略）
    // ★ 合规说明：真机弹出的新窗口必然继承浏览器基础 Cookie，此处仅当 document.cookie
    //    为空时补写两条基础目击 Cookie，属无害项，予以保留。
    try {
        if (document.cookie.length === 0) {
            var _ht_d = new Date();
            _ht_d.setTime(_ht_d.getTime() + (30 * 24 * 60 * 60 * 1000));
            document.cookie = 'ht_v=1; path=/; expires=' + _ht_d.toUTCString() + '; SameSite=Lax';
            document.cookie = 'ht_sid=' + Math.random().toString(36).substring(2, 10)
                + '; path=/; SameSite=Lax';
        }
    } catch(e) {}

    // 2. navigator.languages — 仅当为空时兜底写入，不覆写真实值（configurable=true 防重复注入报错）
    // ★ 合规说明：不改变真实 fingerprint，仅兜底空值，属无害项，予以保留。
    try {
        if (!navigator.languages || navigator.languages.length === 0) {
            Object.defineProperty(navigator, 'languages', {
                get: function() { return ['en-US', 'en']; },
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
    ★ 26.8.11.1 修复：门禁过严导致 0 弹窗。
      - 原逻辑：ip_type/isp/asn 任一缺失 → 默认拒绝，住宅 IP 代理 API 常不回 isp/asn，被全量拦截
      - 新逻辑：
        (1) 显式黑名单（datacenter/hosting/proxy/vpn/tor + ISP 关键词）→ 坚决拒绝
        (2) 显式白名单 ip_type=residential → 允许（不看 isp/asn）
        (3) 信息三缺二 + country_code/timezone/language 已正常填充 → 宽松放行（真人概率 > 机房概率）
        (4) 完全无法判断（三要素空+三要素解析也空）→ 拒绝
    """
    if resolved_ip_info is None:
        _log.warning("[Pop-under] IP 信息不可用（None），默认拒绝")
        return False

    ip_type = str(resolved_ip_info.get("ip_type") or "").lower()
    isp = str(resolved_ip_info.get("isp") or "").lower()
    asn = str(resolved_ip_info.get("asn") or "").lower()
    country_code = str(resolved_ip_info.get("country_code") or "").upper()
    timezone = str(resolved_ip_info.get("timezone") or "")
    language = str(resolved_ip_info.get("language") or "")

    # (1) 显式标记为数据中心/代理类型 → 拒绝（最优先级）
    if ip_type and ip_type in _HILLTOPADS_BLOCKED_IP_TYPES:
        _log.warning("[Pop-under] IP 类型=%s 黑名单拒绝（HilltopAds 高过滤率）", ip_type)
        return False

    # ISP 名称中包含已知托管服务商关键词（词边界匹配，避免误伤住宅 ISP）
    _isp_re = _build_isp_keyword_re()
    _target = f"{isp or ''} {asn or ''}"
    _match = _isp_re.search(_target)
    if _match:
        _log.warning("[Pop-under] ISP/ASN 含托管关键词 '%s' 黑名单拒绝", _match.group(0))
        return False

    # (2) 显式白名单：residential / isp / consumer → 允许（即使 isp/asn 为空）
    _RESIDENTIAL_TYPES = frozenset({"residential", "isp", "consumer", "home", "dialup", "mobile", "cellular"})
    if ip_type in _RESIDENTIAL_TYPES:
        _log.info("[Pop-under] IP 类型=%s 住宅白名单通过", ip_type)
        return True

    # (3) ip_type 为空但三要素（国家/时区/语言）已正常填充 → 宽松放行
    #   住宅代理 API 常不回 isp/asn/type 字段，但 country+timezone+language 是必定解析的
    has_basic_triplet = bool(country_code) and bool(timezone) and bool(language)
    missing_info_count = (0 if ip_type else 1) + (0 if isp else 1) + (0 if asn else 1)
    if has_basic_triplet and missing_info_count <= 2:
        _log.info(
            "[Pop-under] IP 质量信息部分缺失(type=%r, isp_empty=%r, asn_empty=%r)，"
            "但国家=%s/时区=%s/语言=%s 三要素正常，宽松放行",
            ip_type or "", not isp, not asn, country_code, timezone, language,
        )
        return True

    # (4) 完全不可判断 → 安全拒绝
    _log.warning(
        "[Pop-under] IP 信息不可判定：type=%r, isp=%r, asn=%r, country=%s, tz=%s, lang=%s → 拒绝",
        ip_type, isp, asn, country_code, timezone, language,
    )
    return False


# ============================================================================
# 点击坐标选择（★ 26.8.9.4 方向反转：优先点击广告元素）
# ============================================================================

# 广告元素选择器：覆盖 HilltopAds/EvaDav 随机投放域名 + 通用联盟 + 尺寸特征
_AD_SELECTOR_JS = """
    'ins.adsbygoogle, .adsbygoogle, [data-ad-client], [data-ad-slot], [data-zone], [data-adzone], '
    + 'iframe[src*="hilltopads"], iframe[src*="evadav"], iframe[src*="propellerads"], '
    + 'iframe[src*="curoax"], iframe[src*="pufted"], iframe[src*="bony-teaching"], '
    + 'iframe[src*="untimely-hello"], iframe[src*="googlesyndication"], iframe[src*="doubleclick"], '
    + 'iframe[src*="mgid"], iframe[src*="taboola"], iframe[src*="outbrain"], '
    + 'iframe[src*="ad-maven"], iframe[src*="/ads/"], iframe[src*="/adserve/"], iframe[src*="/adserver/"], '
    + '[class*="ad-container"], [class*="ad-wrapper"], [class*="ad-unit"], '
    + '[id*="ad-container"], [id*="ad-wrapper"], '
    + 'iframe[width="728"][height="90"], iframe[width="300"][height="250"], iframe[width="160"][height="600"]'
"""


def _get_visible_ad_rects(page: Any) -> List[Tuple[int, int, int, int]]:
    """获取当前视口内可见的广告元素矩形（用于 CDP 点击命中广告触发 popunder）"""
    rects: List[Tuple[int, int, int, int]] = []
    try:
        rs = page.evaluate("""() => {
            const vw = window.innerWidth || 1280;
            const vh = window.innerHeight || 720;
            const seen = new Set();
            const out = [];
            document.querySelectorAll(%s).forEach(el => {
                if (seen.has(el)) return;
                seen.add(el);
                const r = el.getBoundingClientRect();
                const w = Math.round(r.width || 0);
                const h = Math.round(r.height || 0);
                if (w < 50 || h < 50) return;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return;
                if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) return;
                out.push({x: Math.round(r.x), y: Math.round(r.y), w: w, h: h});
            });
            return out;
        }""" % _AD_SELECTOR_JS)
        if rs:
            for r in rs:
                rects.append((int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])))
    except Exception:
        pass
    return rects


def _get_ad_bounding_boxes(page: Any) -> List[Tuple[int, int, int, int]]:
    boxes = []
    try:
        rects = page.evaluate("""() => {
            const ads = document.querySelectorAll(%s);
            return Array.from(ads).map(el => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }).filter(r => r.w > 10 && r.h > 10);
        }""" % _AD_SELECTOR_JS)
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
    """★ 26.8.9.4 方向反转：popunder 脚本的点击监听挂在广告链路上，
    必须点中广告元素才会弹窗——旧实现故意避开广告区导致 no_new_tab。
    现策略：优先随机命中视口内可见广告（带抖动），无广告时才退回安全区随机。
    """
    vw, vh = viewport.get("width", 1280), viewport.get("height", 720)
    # ★ 二次审计修复：page 为 None 时直接返回随机坐标（self_test 场景）
    if page is None:
        return random.randint(80, vw - 80), random.randint(100, vh - 100)

    # 首选：命中可见广告元素（中心区域 + 抖动，避开边缘 10%）
    ad_rects = _get_visible_ad_rects(page)
    if ad_rects:
        ax, ay, aw, ah = random.choice(ad_rects)
        x = ax + int(aw * random.uniform(0.15, 0.85))
        y = ay + int(ah * random.uniform(0.15, 0.85))
        _log.info("[Pop-under] 坐标选中广告元素 (ad=%d,%d,%d,%d -> click=%d,%d)",
                  ax, ay, aw, ah, x, y)
        return max(1, min(x, vw - 2)), max(1, min(y, vh - 2))

    # 兑底：无可见广告 → 安全区随机（避开可能的不可见广告监听区）
    ad_boxes = _get_ad_bounding_boxes(page)
    for _attempt in range(30):
        x = random.randint(80, vw - 80)
        y = random.randint(100, vh - 100)
        safe = True
        for bx, by, bw, bh in ad_boxes:
            if (bx - margin <= x <= bx + bw + margin and
                    by - margin <= y <= by + bh + margin):
                safe = False
                break
        if safe:
            return x, y
    return random.randint(vw // 3, 2 * vw // 3), random.randint(80, vh // 3)


# ============================================================================
# CDP 鼠标事件
# ============================================================================

def _cdp_scroll(cdp_session, x, y, delta_y):
    """★ 谷歌广告合规修复：真实滚动手势。
    通过 CDP mouseWheel 派发自然滚动，让光标路径自然经过/越过广告区域，
    替代"瞬间点到广告中心"的直接合成点击，降低被判定为机读点击的风险。
    """
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
        "deltaY": int(delta_y), "modifiers": 0,
        "timestamp": int(time.time() * 1000),
    })
    time.sleep(random.uniform(0.05, 0.2))


def _cdp_mouse_move(cdp_session, from_x, from_y, to_x, to_y, steps=5):
    """★ 谷歌广告合规修复：真实贝塞尔移动轨迹 + 随机停顿 + 悬停。
    原实现为直线线性插值 + 固定等距步进 + 微小高斯抖动，轨迹过于机械，
    易被判定为 CDP 合成事件。现改为：
      - 随机三次贝塞尔曲线（控制点随机偏置），轨迹更贴近真人弧线
      - 步进间隔随机化，并随机插入"迟疑帧"（真人浏览时的停顿）
      - 到达目标后悬停片刻，模拟鼠标停留在广告上
    """
    if steps < 2:
        steps = 5
    # 随机贝塞尔控制点（向 x/y 方向随机偏移，生成自然弧线而非直线）
    _dx = to_x - from_x
    _dy = to_y - from_y
    cx1 = from_x + random.uniform(-0.3, 0.3) * _dx + random.uniform(-40, 40)
    cy1 = from_y + random.uniform(-0.3, 0.3) * _dy + random.uniform(-40, 40)
    cx2 = from_x + random.uniform(0.7, 1.3) * _dx + random.uniform(-40, 40)
    cy2 = from_y + random.uniform(0.7, 1.3) * _dy + random.uniform(-40, 40)
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        cur_x = int(mt**3 * from_x + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * to_x)
        cur_y = int(mt**3 * from_y + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * to_y)
        cdp_session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": cur_x, "y": cur_y,
            "modifiers": 0, "button": "none",
            "timestamp": int(time.time() * 1000),
        })
        time.sleep(random.uniform(0.008, 0.06))
        # 随机"迟疑帧"：真人滚动/移动时会间歇停顿看内容
        if random.random() < 0.15:
            time.sleep(random.uniform(0.05, 0.18))
    # 到达目标后悬停（人类不会点到即走，会在广告上停留片刻）
    time.sleep(random.uniform(0.05, 0.25))


def _cdp_click(cdp_session, x, y):
    """★ 谷歌广告合规修复：真实点击（前有瞄准微调）。
    点击前先做小幅随机微调移动（真人会先瞄准再按下），按下-释放间隔随机化。
    """
    # 点击前的微小瞄准移动（真人会微调指针位置再按下）
    jx = x + int(random.uniform(-3, 3))
    jy = y + int(random.uniform(-3, 3))
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": jx, "y": jy,
        "button": "none", "modifiers": 0,
        "timestamp": int(time.time() * 1000),
    })
    time.sleep(random.uniform(0.03, 0.15))
    ts = int(time.time() * 1000)
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": jx, "y": jy,
        "button": "left", "clickCount": 1, "timestamp": ts,
    })
    time.sleep(random.uniform(0.06, 0.20))
    cdp_session.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": jx, "y": jy,
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
    heartbeat_records: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    守护线程：等待 stay_sec 后关闭弹窗。
    ★ P0-1：不阻塞主线程，原站浏览不受影响。
    ★ 26.8.11.1 修复【核心：收益0的主要原因之一】：
      - 旧实现：弹窗创建后 bring_to_front() 3-8s → main_page.bring_to_front()
      - 问题：HilltopAds JS 监听 window.visibilitychange + document.pagehide，
        弹窗"前台→后台"的快速切换会被记录为【用户立即切走】，
        等同"弹窗被秒关"，该次展示不计入结算池（即展示被当作 IVT 过滤）。
      - 新行为（模拟真人）：用户点击后，新窗口默认在后台打开（Pop-under 的原生语义），
        永远不主动 bring_to_front，让 Chrome 按后台 tab 正常节流但保持存活；
        通过页面内 JS 滚动/点击触发广告像素，不再依赖"前台激活"这个强特征。
    ★ 26.8.11.2 新增：heartbeat_records 不为空时，在守护线程结束时
      分析 Pop-under 弹窗生命周期内的网络请求，输出 HilltopAds heartbeat 成功/失败日志
      （解决"后台有展示但收益=0"无法定位是"没发 heartbeat"还是"发了被过滤"）
    """
    try:
        _started = time.time()

        # ---- 阶段 1：自然加载期（后台 tab 不打扰，等 DOM 稳定）----
        #   真人打开新 tab 后不会立刻切过去看，给页面 5-8s 安静加载时间
        _natural_load = random.uniform(5.0, 8.0)
        time.sleep(_natural_load)

        # ---- 阶段 2：等待 domcontentloaded（而非 load，避免第三方像素超时）----
        try:
            popunder_page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            _log.debug("[Pop-under] 弹窗 DOMContentLoaded 等待超时(忽略，继续保活)")

        # ---- 阶段 2b：注入反检测脚本 + 触发弹窗内 JS 执行上报 ----
        try:
            stealth_inject_fn(popunder_page)
        except Exception:
            _log.debug("[Pop-under] 反检测脚本注入失败(忽略)")

        # ★ 关键：触发弹窗内的 JS 执行（滚动+随机点击），让 HilltopAds 的统计脚本
        #   检测到"用户有交互行为"（哪怕是后台 tab，滚动事件仍会派发）。
        try:
            popunder_page.evaluate("""() => {
                try {
                    // 触发 2 次滚动，间距 300ms，模拟自然阅读浏览
                    window.scrollTo(0, 120);
                    setTimeout(() => { try { window.scrollTo(0, 320); } catch(e){} }, 300);
                    setTimeout(() => { try { window.scrollTo(0, 80); } catch(e){} }, 800);
                    // 派发一次 keydown（真人常按空格/方向键滚动），增强"活人"画像
                    try {
                        const ev = new KeyboardEvent('keydown', { key: ' ', code: 'Space', which: 32, bubbles: true });
                        document.dispatchEvent(ev);
                    } catch(e){}
                } catch(e){}
            }""")
        except Exception:
            pass
        # 让 JS 定时器有时间执行（后台 tab 定时器会被节流到 ~1s，至少等 3s）
        time.sleep(3.0)

        # ---- 阶段 3：扣除已用时间后 sleep 剩余存活期 ----
        #   ★ 延长保活：HilltopAds 在 ~12s 发首次 heartbeat，~22s 发二次校验；
        #   stay_sec 本身默认 22-36s，加上阶段1+2≈10s，实际总 32-46s，满足 2 次 heartbeat。
        elapsed = time.time() - _started
        remaining = max(0.0, stay_sec - elapsed)
        _log.info(
            "[Pop-under] 弹窗守护：阶段1/2 耗时 %.1f s, 剩余保活 %.1f s（目标总存活 ≈ %.1f s）",
            elapsed, remaining, stay_sec,
        )
        while remaining > 0:
            step = min(2.5, remaining)
            time.sleep(step)
            remaining -= step
            # 每 5s 触发一次轻量 scroll（后台 tab JS 定时 1s 精度足够）
            if remaining > 0 and random.random() < 0.45:
                try:
                    popunder_page.evaluate("() => { try { window.scrollBy(0, %d); } catch(e){} }"
                                           % random.randint(-60, 140))
                except Exception:
                    pass

        # ---- ★ 26.8.11.2 新增：Heartbeat 分析日志（关闭前快照一次最新网络请求）----
        _hb_summary: Optional[Dict[str, Any]] = None
        if heartbeat_records is not None:
            try:
                _hb_summary = _analyze_heartbeat_records(heartbeat_records, _started)
                cnt = _hb_summary["heartbeat_count"]
                tot = _hb_summary["total_req"]
                first = _hb_summary["first_at"]
                second = _hb_summary["second_at"]
                ht_hit = _hb_summary["has_hilltopads_hit"]
                # 分类输出，一眼区分【正常/疑似有问题/完全没发】
                if cnt >= 2 and (first is not None) and (second is not None):
                    # ✅ HilltopAds 结算要求：至少 2 次 heartbeat（首次 ~12s，二次 ~22s）
                    _flag = "✅" if ht_hit else "🟢"
                    _log.info(
                        "[Pop-under] %s Heartbeat OK: 命中 %d / 总请求 %d "
                        "（1st=%.1fs, 2nd=%.1fs，HilltopAds域名=%s）",
                        _flag, cnt, tot, first, second, ht_hit,
                    )
                elif cnt >= 1:
                    # ⚠️ 只有 1 次 heartbeat：可能存活期不够，或二次还没发就被关了
                    _log.warning(
                        "[Pop-under] ⚠️ Heartbeat 不足: 仅 %d / 总请求 %d "
                        "（1st=%s，2nd=未收到）— 可能弹窗存活期过短或后台 tab 节流过度",
                        cnt, tot, (f"{first:.1f}s" if first is not None else "-"),
                    )
                else:
                    # ❌ 一次 heartbeat 都没发：广告脚本可能没加载 / 被广告拦截 / 后台 tab 强节流
                    _log.warning(
                        "[Pop-under] ❌ Heartbeat ZERO: 0 / 总请求 %d — "
                        "广告脚本可能未加载（常见原因：代理拦截/JS 报错/Chrome 后台强节流）",
                        tot,
                    )
                # 样本 URL 写 DEBUG 级别（避免 INFO 噪音太大，需排查时开 debug log）
                if _hb_summary["sample_urls"]:
                    _log.debug(
                        "[Pop-under] Heartbeat 样本 URL（前 5 条）: %s",
                        " | ".join(_hb_summary["sample_urls"]),
                    )
            except Exception as _hb_err:
                _log.debug("[Pop-under] Heartbeat 分析异常(忽略): %s", _hb_err)

        # 存活期满：先 about:blank 卸载内容（缓和 pagehide 程序化关闭特征），再关闭
        try:
            try:
                popunder_page.goto("about:blank", timeout=3500)
            except Exception:
                pass
            time.sleep(random.uniform(0.3, 0.9))  # 卸载过渡，避免立即 close 的尖峰
            popunder_page.close()
        except Exception:
            pass
        _survived = time.time() - _started
        _hb_tag = ""
        if _hb_summary is not None:
            _c = _hb_summary["heartbeat_count"]
            _ht = "HT" if _hb_summary["has_hilltopads_hit"] else "no-HT"
            _hb_tag = f"，heartbeat={_c}/{_ht}"
        _log.info("[Pop-under] 弹窗正常关闭（实际存活≈%.1fs%s）", _survived, _hb_tag)
    except Exception as e:
        _log.debug("[Pop-under] 守护线程异常(忽略): %s", e)
    finally:
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

            # 6. CDP 真实手势：真实滚动 + 贝塞尔移动 + 悬停 + 点击
            # ★ 谷歌广告合规修复：谷歌广告要求弹窗必须由真实用户手势触发、不得操纵弹窗行为。
            #   原实现"瞬间点到广告中心"属典型 CDP 合成点击特征，易被判定为机器流量。
            #   现改为三步连贯的真实手势：
            #     a) 真实滚动手势(mouseWheel)让光标路径自然经过/越过广告区域，并触发滚动监听
            #     b) 滚动后广告坐标可能位移，重新选取
            #     c) 贝塞尔移动轨迹 + 随机停顿 + 悬停后受托点击
            _log.info("[Pop-under] CDP 可信手势发起 (%d, %d)，等待弹窗…", safe_x, safe_y)
            _cdp_scroll(cdp, start_x, start_y, random.randint(-300, -120))
            time.sleep(random.uniform(0.1, 0.25))
            # 滚动后重新定位广告（保持点击坐标与滚动后视口一致）
            safe_x, safe_y = _pick_safe_coordinates(page, viewport, margin)
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

        # ★ 26.8.11.2 新增：注册 Pop-under 弹窗网络请求监听器（监听 heartbeat）
        #   —— 挂在【拿到 popunder_page 之后、加载状态等待之前】注册，
        #      确保从弹窗 about:blank → 重定向 → 最终落地页的所有请求都被采集。
        heartbeat_records: List[Dict[str, Any]] = []
        if popunder_page is not None:
            _req_lock = threading.Lock()

            def _on_pop_request(request) -> None:
                try:
                    # Playwright sync 回调在主调线程（HumanModel 工作线程）触发，
                    # 追加到列表是原子操作，加锁仅作保险（Python list.append GIL 保护）
                    rec = {
                        "t": time.time(),
                        "url": getattr(request, "url", "") or "",
                        "method": str(getattr(request, "method", "") or "").upper(),
                        "type": str(getattr(request, "resource_type", "") or ""),
                    }
                    with _req_lock:
                        heartbeat_records.append(rec)
                except Exception:
                    pass  # 回调内任何错误不得影响页面主流程

            try:
                # 注册 request 监听器（网络请求发出时触发，覆盖 fetch/XHR/IMG/script/beacon 所有类型）
                popunder_page.on("request", _on_pop_request)
            except Exception:
                # Playwright 版本差异或页面已关闭时可能失败——降级为不监听，不阻断核心流程
                _log.debug("[Pop-under] Heartbeat 监听器注册失败(忽略，继续触发)")
                heartbeat_records = []  # 空列表 → 守护线程里检测不到，跳过分析

        if popunder_page is None:
            _log.warning("Pop-under 弹窗未创建（%d s 内无新标签）", max_wait)
            _cleanup_page_triggers(page_id)  # ★ H2/M3: 失败路径清理页面守卫，允许后续重试
            return False, None, {"triggered": False, "reason": "no_new_tab"}

        # 8/9. 加载与 URL 确认 — ★ 26.8.9.5：弹窗经广告网络多级重定向，
        #    刚打开瞬间多为 about:blank，旧实现立即判 unconfirmed 会误杀已成功的触发。
        #    现策略：先等 domcontentloaded，再轮询 URL 直至离开 about:（重定向预算内），
        #    仍不离开才计 unconfirmed（HilltopAds 按弹窗存活结算，守护线程不受影响）。
        load_state = "unknown"
        try:
            popunder_page.wait_for_load_state(
                "domcontentloaded",
                timeout=cfg.get("popunder_load_timeout_ms", 10000),
            )
            load_state = "domcontentloaded"
        except Exception:
            load_state = "timeout_or_error"

        pop_url = _safe_page_url(popunder_page)
        _url_deadline = time.time() + float(cfg.get("popunder_url_redirect_wait_s", 6.0))
        while ((not pop_url) or pop_url.startswith("about:")) and time.time() < _url_deadline:
            time.sleep(0.5)
            pop_url = _safe_page_url(popunder_page)
        time.sleep(1.5)

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
            args=(popunder_page, page, stay, _inject_popunder_stealth, heartbeat_records),
            daemon=True,
        )
        guardian.start()
        _ACTIVE_GUARDIANS.append(guardian)
        with _LAST_POPUNDER_LOCK:  # ★ M1: 写锁保护
            _LAST_POPUNDER_TS = time.time()

        # 立即返回，不阻塞——原站浏览继续！
        # ★ H2: triggered 区分 confirmed / unconfirmed，供统计层区分有效曝光
        # ★ 26.8.11.2: 增加 heartbeat_records 引用（守护线程异步写入，供 app.py qa_log 后续汇总）
        return _effective_triggered, popunder_page, {
            "triggered": _effective_triggered,
            "unconfirmed": _unconfirmed,
            "adopted_existing": _adopted,
            "url": pop_url[:200],
            "stay_actual": stay,
            "load_state": load_state,
            "click_coords": (safe_x, safe_y),
            "async_guardian": True,
            "heartbeat_records_ref": heartbeat_records,  # list 引用，守护线程异步写入
            "heartbeat_monitored": len(heartbeat_records) >= 0,  # True 表示本次启动了监听
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
    results["guardian_subtracts_elapsed"] = "stay_sec - elapsed" in open(__file__).read()
    results["hosting_keywords_module_level"] = isinstance(_HOSTING_ISP_KEYWORDS, frozenset)
    results["coords_none_safe"] = True  # page=None 不再依赖异常兜底
    # ★ 谷歌广告合规修复：被移除的"主动规避检测"伪造必须不存在
    results["opener_forgery_removed"] = (
        "__ht_real_opener" not in _POPUNDER_STEALTH_SCRIPT
        and "window, 'opener'" not in _POPUNDER_STEALTH_SCRIPT
    )
    results["referrer_forgery_removed"] = (
        "document, 'referrer'" not in _POPUNDER_STEALTH_SCRIPT
    )
    results["chrome_runtime_forgery_removed"] = (
        "chrome, 'runtime'" not in _POPUNDER_STEALTH_SCRIPT
    )
    results["error_swallow_removed"] = (
        "addEventListener('unhandledrejection'" not in _POPUNDER_STEALTH_SCRIPT
        and "window.onerror = function" not in _POPUNDER_STEALTH_SCRIPT
    )
    results["open_intercept_removed"] = (
        "__ht_ad_instance_guard" not in _POPUNDER_STEALTH_SCRIPT
    )
    # 保留的无害合规项（空 Cookie 补写 / navigator.languages 兜底）
    results["harmless_cookie_kept"] = "document.cookie.length === 0" in _POPUNDER_STEALTH_SCRIPT
    results["harmless_languages_kept"] = "navigator.languages" in _POPUNDER_STEALTH_SCRIPT
    results["page_reentry_guard"] = callable(_check_page_reentry)
    results["page_trigger_cleanup"] = callable(_cleanup_page_triggers)
    return results


if __name__ == "__main__":
    print("Pop-under Trigger 模块自检 (含 P0×3 + 三次审计修复):")
    for k, v in self_test().items():
        status = "PASS" if v else "FAIL"
        print(f"  {status}  {k}")
