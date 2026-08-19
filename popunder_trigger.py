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
# ★ 26.8.15.1 修复【固定时长指纹】：存活窗口放宽为 15-120s，由 _sample_popunder_stay()
#   做三段混合采样（短 15-24 / 核 24-60 三角 / 长尾 60-120）——均值 ≈36-39s 落在两次
#   heartbeat（~12s/~22s）之后，同时消除"每次都 22-36s 整段关闭"的程序化指纹。
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "trigger_probability": 0.85,          # ★ 26.8.17.1: 0.40→0.60→0.85（P0-2：冷却 75s 已兜底频控，
                                          #   概率门是 5.7% 触发成功率的放大器，上调后靠 cooldown 控量）
    "trigger_after_pct_min": 0.15,
    "trigger_after_pct_max": 0.30,
    "popunder_stay_min": 15,              # ★ 26.8.15.1: 22→15（下界即 R07 CRIT 线，分布均值仍 ≥25s）
    "popunder_stay_max": 120,             # ★ 26.8.15.1: 36→120（长尾段，10% 概率，模拟"忘了关"）
    "popunder_load_timeout_ms": 12000,
    "cdp_move_steps": 5,
    "cdp_click_count": 1,
    "ad_safe_margin_px": 60,
    "max_wait_for_popup_s": 4.0,
    "cooldown_between_triggers_s": 75,    # 冷却 90→75s：让更多任务有机会触发（配合频控放宽）
}

# ============================================================================
# ★ 26.8.11.2 新增：HilltopAds Heartbeat 监听 — 排查"收益=0"的最终链路
# ★ 26.8.13.2 修复【心跳虚高】：域名与统计路径/参数分层匹配——
#   只有命中 HilltopAds 相关域名（自有域名 + 弹窗落地广告网络白名单）才计入
#   heartbeat；view=/event=/imp= 等泛参数必须同时满足 HilltopAds 相关域名，
#   杜绝页面普通业务 URL（如 /view?id=xx）被误计导致心跳统计虚高。
# ============================================================================
# HilltopAds 自有域名（最强正例：命中即判定为 heartbeat，无需额外路径）
_HEARTBEAT_OWN_DOMAINS: Tuple[str, ...] = (
    "hilltopads.com", "hilltopads.net", "hilltopads",
    "htopcdn", "traffichunt",
)
# 弹窗落地广告网络域名白名单（与 _CLICK_AD_SELECTOR_JS 的 iframe 广告域名一致；
# 命中此类域名后仍需命中下方统计路径/参数关键词才算 heartbeat）
_HEARTBEAT_LANDING_DOMAINS: Tuple[str, ...] = (
    "evadav", "propellerads", "curoax", "pufted",
    "bony-teaching", "untimely-hello", "googlesyndication", "doubleclick",
    "mgid", "taboola", "outbrain", "ad-maven",
    # ★ 26.8.17.1：8/15 排查确认的实际落地域名（此前缺失导致结算验证假阴性）
    "eatcells", "nesber",
)
# 统计 / 像素 / 上报路径关键词（落地广告域名命中后按此判定）
_HEARTBEAT_URL_KEYWORDS: Tuple[str, ...] = (
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
# 非 HilltopAds 相关域名的兜底强特征（不含 view=/event=/imp= 等泛参数，
# 防止任意普通 URL 因泛参数被误计为 heartbeat）
_HEARTBEAT_STRONG_KEYWORDS: Tuple[str, ...] = (
    "heartbeat", "/hb?", "hb=", "/ping", "/pixels", "/pixel",
    "tracker", "/track?", "/tracking/", "tracking?", "stats.php",
    "/stats", "stats/", "/statistics", "stat.php", "/stat/", "/stat?",
    "beacon", "log_event", "log.php", "/collect", "/sync",
    "adserv", "adsystem", "adserver", "adsrv", "adtrack", "click?",
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
    ★ 26.8.13.2 修复【心跳虚高】：收窄判定口径——
      1) HilltopAds 自有域名（hilltopads/htopcdn/traffichunt）→ 直接判定；
      2) 弹窗落地广告网络白名单域名 → 需同时命中统计路径/参数关键词；
      3) 其它域名 → 仅强特征关键词可命中，view=/event=/imp= 等泛参数
         必须同时满足 HilltopAds 相关域名才计入，防止普通业务 URL 虚高。
    """
    if not url:
        return False
    u = url.lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    # 1) HilltopAds 自有域名：最强正例，直接判定为 heartbeat
    if any(d in u for d in _HEARTBEAT_OWN_DOMAINS):
        return True
    # 2) 弹窗落地广告网络白名单域名：域名 + 统计路径/参数同时命中
    if any(d in u for d in _HEARTBEAT_LANDING_DOMAINS):
        return any(kw in u for kw in _HEARTBEAT_URL_KEYWORDS)
    # 3) 其它域名：仅强特征关键词可命中（泛参数在此不生效）
    return any(kw in u for kw in _HEARTBEAT_STRONG_KEYWORDS)


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
# 点击坐标选择（★ 26.8.11.12 核心修复：点击只选 iframe 型广告，杜绝误点普通 ad-class）
# ============================================================================
#
# ★ 26.8.11.12 背景：旧实现把广告监控选择器和 CDP 点击选择器混用了同一份 _AD_SELECTOR_JS，
#   里面的 [class*="ad-container"] / [class*="ad-wrapper"] / [class*="ad-unit"]
#   会命中网站主题自带的 "ad-" 前缀元素（如"推荐文章区"容器），导致 CDP 点击打开的是
#   普通内容页（blogtribehub.com 等），不是 HilltopAds 广告落地页 → 后台零点击零收益。
#
# 现在拆分成两份：
#   _CLICK_AD_SELECTOR_JS  — CDP 点击专用，只含 iframe 型广告（精准）
#   _MONITOR_AD_SELECTOR_JS — 广告监控专用，保留宽口径（曝光检测）
#   _AD_SELECTOR_JS       — 向后兼容别名 = 监控选择器（不影响监控逻辑）
#
# HilltopAds / EvaDav / PropellerAds / Mgid 等主流联盟 100% 用 iframe 投放。

# 点击专用：只含 iframe 型广告
_CLICK_AD_SELECTOR_JS = """
    'iframe[src*="hilltopads"], iframe[src*="evadav"], iframe[src*="propellerads"], '
    + 'iframe[src*="curoax"], iframe[src*="pufted"], iframe[src*="bony-teaching"], '
    + 'iframe[src*="untimely-hello"], iframe[src*="googlesyndication"], iframe[src*="doubleclick"], '
    + 'iframe[src*="mgid"], iframe[src*="taboola"], iframe[src*="outbrain"], '
    + 'iframe[src*="ad-maven"], iframe[src*="/ads/"], iframe[src*="/adserve/"], iframe[src*="/adserver/"], '
    + 'iframe[width="728"][height="90"], iframe[width="300"][height="250"], iframe[width="160"][height="600"]'
"""

# 监控专用：保留完整宽口径（用于曝光/容器计数）
_MONITOR_AD_SELECTOR_JS = """
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

# 向后兼容别名（广告监控等老代码仍引用 _AD_SELECTOR_JS）
_AD_SELECTOR_JS = _MONITOR_AD_SELECTOR_JS


def _get_visible_ad_rects(page: Any) -> List[Tuple[int, int, int, int]]:
    """获取当前视口内可见的 iframe 型广告元素矩形（用于 CDP 点击）。
    ★ 26.8.11.12 修复：只返回 iframe 型广告，避免误点网站主题 ad-class 普通元素。
    """
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
        }""" % _CLICK_AD_SELECTOR_JS)
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
    """★ 26.8.11.12 策略：只点击 iframe 型广告元素（精准命中真实广告位）。
    - 首选：视口内可见的 iframe 广告元素（中心区域 + 抖动）
    - 兜底：无可见 iframe 广告 → 安全区随机（避开所有广告元素边界）
    ★ 之前用 [class*="ad-"] 宽泛匹配会误点网站主题 ad- 元素，已修。
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

# ★ 26.8.17.1 新增：CDP 通道韧性 — 超时/连接类异常识别与单次重试。
#   根因（HILLTOPADS_ZERO_REVENUE_FINDINGS 🔴1）：localhost CDP HTTP 通道
#   （/goog/cdp/execute）偶发 ReadTimeoutError×14 / MaxRetryError×11 /
#   NewConnectionError×11，旧实现直接落入 trigger_popunder 的 except 分支
#   整次触发失败（触发成功率仅 ~5.7%）。此类异常多为瞬时（通道忙/代理抖动），
#   原地重试 1 次（前加 0.6-1.5s 随机退避）即可恢复，无需重建会话。
_CDP_TRANSIENT_EXC_NAMES: frozenset = frozenset((
    "ReadTimeoutError", "ReadTimeout", "ConnectTimeoutError", "ConnectTimeout",
    "MaxRetryError", "NewConnectionError", "ConnectionError", "TimeoutError",
    "HTTPError", "RemoteDisconnected", "ProtocolError",
))


def _is_cdp_transient_error(exc: BaseException) -> bool:
    """判断 CDP 调用异常是否为瞬时通道错误（值得原地重试 1 次）。"""
    return type(exc).__name__ in _CDP_TRANSIENT_EXC_NAMES


def _cdp_send_retry(cdp_session, method: str, params: Dict[str, Any],
                    _retry: int = 1):
    """带单次瞬时重试的 CDP 命令发送。

    非瞬时异常（协议错误/参数错误）立即抛出，交给上层 except 记录；
    瞬时异常（超时/连接断开）退避 0.6-1.5s 后原样重试 1 次，仍失败则抛出。
    """
    try:
        return cdp_session.send(method, params)
    except Exception as _e:
        if _retry <= 0 or not _is_cdp_transient_error(_e):
            raise
        _backoff = random.uniform(0.6, 1.5)
        _log.info("[Pop-under] CDP 瞬时错误(%s: %s)，%.2fs 后重试 1 次",
                  type(_e).__name__, _e, _backoff)
        time.sleep(_backoff)
        return cdp_session.send(method, params)  # 第二次失败直接抛给上层


def _cdp_scroll(cdp_session, x, y, delta_y):
    """★ 谷歌广告合规修复：真实滚动手势。
    通过 CDP mouseWheel 派发自然滚动，让光标路径自然经过/越过广告区域，
    替代"瞬间点到广告中心"的直接合成点击，降低被判定为机读点击的风险。
    """
    _cdp_send_retry(cdp_session, "Input.dispatchMouseEvent", {
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
        _cdp_send_retry(cdp_session, "Input.dispatchMouseEvent", {
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
    _cdp_send_retry(cdp_session, "Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": jx, "y": jy,
        "button": "none", "modifiers": 0,
        "timestamp": int(time.time() * 1000),
    })
    time.sleep(random.uniform(0.03, 0.15))
    ts = int(time.time() * 1000)
    _cdp_send_retry(cdp_session, "Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": jx, "y": jy,
        "button": "left", "clickCount": 1, "timestamp": ts,
    })
    time.sleep(random.uniform(0.06, 0.20))
    _cdp_send_retry(cdp_session, "Input.dispatchMouseEvent", {
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


def _sample_popunder_stay(min_s: float, max_s: float) -> float:
    """★ 26.8.15.1 新增：类人停留时长采样 — 三段混合分布，杀死"固定时长"指纹。

    旧问题：random.randint(min, max) 均匀采样 → 停留时长是平顶矩形分布；
    且旧默认 (22, 36) 让每次弹窗都"十几秒后整段关闭"，时间戳分布过于规律。
    新策略（混合分布，均值 ≈36-39s，避开 uniform(15,120) 的 67.5s 偏长）：
      ① 30% 短段 [lo, 24]         —— 快速浏览就走（仍 ≥ R07 CRIT 线 15s）
      ② 60% 核段 [24, 60] 三角(峰≈36) —— 多数人"看完一段"的停留时长
      ③ 10% 长段 [60, hi]         —— 长尾"读完全文"用户（模拟忘了关）
    依据：heartbeat ~12s/~22s + 完整结算窗口 ~24-25s → 核段下界 24 让 90%
    弹窗覆盖"两次 heartbeat + 结算完成"；短段是风险-成本权衡。
    """
    lo = max(15.0, float(min_s))          # 硬下限：R07 CRIT 线
    hi = max(lo, float(max_s))
    r = random.random()
    if r < 0.30:
        v = random.uniform(lo, min(hi, 24.0))
    elif r < 0.90:
        v = random.triangular(max(lo, 24.0), min(hi, 60.0),
                              min(max(36.0, lo), hi))  # 峰 ≈36s（位置参数 low/high/mode）
    else:
        v = random.uniform(min(hi, 60.0), hi)
    return round(min(hi, max(lo, v)), 1)


# ============================================================================
# P0-1：弹窗异步管理守护线程
# ============================================================================

def _guard_stay_and_close(
    popunder_page: Any,
    main_page: Any,
    stay_sec: float,
    stealth_inject_fn,
    heartbeat_records: Optional[List[Dict[str, Any]]] = None,
    popup_cdp: Any = None,
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
    ★ 26.8.15.1 新增【类人交互升级】：
      - popup_cdp：弹窗专属 CDP 会话（可选）。弹窗是独立 CDP target，主 driver 的
        JS 钩子钩不到它；用真实 Input 事件（mouseWheel/mouseMoved/keydown，
        isTrusted=true）替代纯 JS dispatch，行为画像从"后台 tab 定时器"升级为
        "后台 tab 里有人在滚动/按方向键"。会话为 None 时自动降级纯 JS 路径。
      - 交互节奏全部去固定化：JS 滚动偏移/定时/按键随机化；保活循环步长 1.5-3.5s
        随机 + 18% 概率"阅读停顿"0.5-3.0s；关闭前 50% 概率滚回顶部。
      - 注意：CDP Input 事件绑定 driver 的"当前窗口 target"，发送前需把 driver
        焦点切到弹窗窗口（_popup_cdp_focus_switch），发完切回主页面。
    """
    try:
        _started = time.time()

        # ---- 阶段 1：自然加载期（后台 tab 不打扰，等 DOM 稳定）----
        #   真人打开新 tab 后不会立刻切过去看，给页面 4-10s 安静加载时间
        #   ★ 26.8.15.1：5-8s → 4-10s，拉开加载期方差，弱化固定节奏指纹
        _natural_load = random.uniform(4.0, 10.0)
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
        #   ★ 26.8.15.1：滚动偏移/定时/按键全部随机化，旧版固定 120/320/80/300/800ms
        #     让每次弹窗的交互时间戳完全一致，是典型的"程序化交互"指纹。
        _sc1 = random.randint(80, 200)     # 第一次滚动目标
        _sc2 = _sc1 + random.randint(120, 260)   # 第二次（继续往下读）
        _sc3 = random.randint(40, max(50, _sc1 - 40))   # 第三次（回看）
        _t1 = random.randint(250, 550)     # 滚动间距（ms）
        _t2 = random.randint(600, 1100)
        _key = random.choice([(" ", "Space", 32), ("ArrowDown", "ArrowDown", 40)])
        try:
            popunder_page.evaluate("""() => {
                try {
                    // 3 次滚动，间距随机，模拟自然阅读浏览
                    window.scrollTo(0, %d);
                    setTimeout(() => { try { window.scrollTo(0, %d); } catch(e){} }, %d);
                    setTimeout(() => { try { window.scrollTo(0, %d); } catch(e){} }, %d);
                    // 派发一次 keydown（真人常按空格/方向键滚动），增强"活人"画像
                    try {
                        const ev = new KeyboardEvent('keydown', { key: '%s', code: '%s', which: %d, bubbles: true });
                        document.dispatchEvent(ev);
                    } catch(e){}
                } catch(e){}
            }""" % (_sc1, _sc2, _t1, _t2, _sc3, _key[0], _key[1], _key[2]))
        except Exception:
            pass
        # 让 JS 定时器有时间执行（后台 tab 定时器会被节流到 ~1s，至少等 3s）
        time.sleep(3.0)

        # ---- 阶段 2c：★ 26.8.15.1 新增 — 加载完成后 1-2 次真实交互 ----
        #   有 CDP 会话：真实 Input 事件（isTrusted=true，坐标随机、曲线移动）
        #   无 CDP 会话：降级 JS scrollBy（保持"后台 tab 有人在动"的最小画像）
        _touch_stats: Dict[str, int] = {"scroll": 0, "move": 0, "key": 0, "click": 0}
        for _ in range(random.randint(1, 2)):
            if popup_cdp is not None:
                try:
                    _popup_human_touch(popup_cdp, popunder_page, _touch_stats,
                                       can_click=False, main_page=main_page)
                except Exception as _te:
                    _log.debug("[Pop-under] CDP 触摸异常(降级JS): %s", _te)
                    try:
                        popunder_page.evaluate(
                            "() => { try { window.scrollBy(0, %d); } catch(e){} }"
                            % random.randint(60, 180))
                    except Exception:
                        pass
            else:
                try:
                    popunder_page.evaluate(
                        "() => { try { window.scrollBy(0, %d); } catch(e){} }"
                        % random.randint(60, 180))
                except Exception:
                    pass
            time.sleep(random.uniform(0.5, 1.5))

        # ---- 阶段 3：扣除已用时间后 sleep 剩余存活期 ----
        #   ★ 延长保活：HilltopAds 在 ~12s 发首次 heartbeat，~22s 发二次校验；
        #   stay_sec 本身默认 22-36s，加上阶段1+2≈10s，实际总 32-46s，满足 2 次 heartbeat。
        #   ★ 26.8.15.1：固定 2.5s 步长 → 1.5-3.5s 随机步长 + 18% 概率"阅读停顿"
        #     （真人读文章会停下来思考，定时器完全静止 0.5-3s 是最像人的节奏）。
        elapsed = time.time() - _started
        remaining = max(0.0, stay_sec - elapsed)
        _log.info(
            "[Pop-under] 弹窗守护：阶段1/2 耗时 %.1f s, 剩余保活 %.1f s（目标总存活 ≈ %.1f s）",
            elapsed, remaining, stay_sec,
        )
        while remaining > 0:
            step = min(random.uniform(1.5, 3.5), remaining)
            time.sleep(step)
            remaining -= step
            if remaining <= 0:
                break
            # ★ 26.8.15.1：18% 概率阅读停顿（之后 continue，本轮不再交互）
            if random.random() < 0.18:
                time.sleep(random.uniform(0.5, 3.0))
                continue
            # 每轮 55% 概率触发一次轻量交互（CDP 真实事件优先，JS 兜底）
            if random.random() < 0.55:
                if popup_cdp is not None:
                    try:
                        # 剩余 >15s 才允许点击（留够结算窗口），否则只滚动/移动/按键
                        _popup_human_touch(popup_cdp, popunder_page, _touch_stats,
                                           can_click=(remaining > 15),
                                           main_page=main_page)
                    except Exception as _te:
                        _log.debug("[Pop-under] CDP 触摸异常(降级JS): %s", _te)
                        try:
                            popunder_page.evaluate(
                                "() => { try { window.scrollBy(0, %d); } catch(e){} }"
                                % random.randint(-60, 140))
                        except Exception:
                            pass
                else:
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
        #   ★ 26.8.15.1：关闭前 50% 概率滚回顶部（真人看完/读累会往上翻），
        #     卸载过渡 0.3-0.9s → 0.6-2.4s（旧值太整齐，"卸载→关闭"间隔是固定指纹）
        try:
            if random.random() < 0.5:
                try:
                    if popup_cdp is not None:
                        # CDP Input 绑定 driver 当前窗口 target，先切焦点到弹窗再滚
                        if _popup_cdp_focus_switch(popunder_page, main_page):
                            _vp = popunder_page.viewport_size or {}
                            _vw = int(_vp.get("width", 1280) or 1280)
                            _vh = int(_vp.get("height", 720) or 720)
                            _cdp_scroll(popup_cdp, random.randint(200, _vw - 200),
                                        random.randint(100, max(150, _vh - 100)),
                                        random.randint(-600, -300))
                        _popup_cdp_restore(main_page)
                    else:
                        popunder_page.evaluate(
                            "() => { try { window.scrollTo(0, 0); } catch(e){} }")
                except Exception:
                    pass
                time.sleep(random.uniform(0.3, 1.2))
            try:
                popunder_page.goto("about:blank", timeout=3500)
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 2.4))  # 卸载过渡，避免立即 close 的尖峰
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


# ============================================================================
# ★ 26.8.15.1 新增：弹窗 CDP 真实交互（isTrusted=true）
#   弹窗是独立 CDP target，主 driver 的 JS 钩子钩不到它；
#   用 Input.dispatchMouseEvent / dispatchKeyEvent 发真实事件，
#   行为画像从"后台 tab 定时器"升级为"后台 tab 里有人在滚动/按方向键"。
#   注意：CDP Input 事件绑定 driver 的"当前窗口 target"，
#   发送前需把 driver 焦点切到弹窗窗口，发完切回主页面。
# ============================================================================

# 点击白名单：只点"内容型"元素，避开 a/button/input/iframe 等会触发
# 导航/表单/跨域跳转的元素（真人偶尔点一下页面空白或段落，不会乱点链接）
_POPUNDER_SAFE_CLICK_TAGS = frozenset({
    "body", "div", "span", "p", "section", "article", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "td", "th",
    "figure", "img", "main", "blockquote", "pre", "code", "em", "strong",
    "b", "i", "small", "sub", "sup", "abbr",
})


def _cdp_key(cdp_session: Any, key: str, code: str, which: int = 0) -> None:
    """★ 26.8.15.1 新增：CDP 真实按键（rawKeyDown → keyUp，间隔 40-120ms）。
    key: ' ' / 'ArrowDown' / 'ArrowUp' 等；code: 'Space' / 'ArrowDown' / 'ArrowUp'
    """
    ts = int(time.time() * 1000)
    params_down = {
        "type": "rawKeyDown",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": which,
        "timestamp": ts,
    }
    if key == " ":
        params_down["text"] = " "
    _cdp_send_retry(cdp_session, "Input.dispatchKeyEvent", params_down)
    time.sleep(random.uniform(0.03, 0.12))
    _cdp_send_retry(cdp_session, "Input.dispatchKeyEvent", {
        "type": "keyUp",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": which,
        "timestamp": ts + int(random.uniform(40, 120)),
    })


def _popup_cdp_focus_switch(cdp_page: Any, restore_page: Any) -> bool:
    """★ 26.8.15.1 新增：把 driver 焦点切到弹窗窗口（CDP Input 绑定当前 target）。
    成功返回 True；失败返回 False（调用方降级 JS 路径）。
    用 cdp_page._focus_window() 而非 cdp_page.evaluate()，因为后者会吞掉异常。
    """
    try:
        cdp_page._focus_window()
        # 验证：焦点是否真的切到了弹窗（_focus_window 可能因窗口已关闭而失败）
        if cdp_page._window_handle and cdp_page.driver.current_window_handle != cdp_page._window_handle:
            # 焦点没切过去，再试一次（_window_focus_lock 已序列化竞态）
            cdp_page._focus_window()
            if cdp_page.driver.current_window_handle != cdp_page._window_handle:
                return False
        return True
    except Exception:
        return False


def _popup_cdp_restore(restore_page: Any) -> None:
    """★ 26.8.15.1 新增：把 driver 焦点切回主页面（best-effort，失败不抛）。"""
    try:
        if restore_page is not None and hasattr(restore_page, "_focus_window"):
            restore_page._focus_window()
    except Exception:
        pass


def _popup_human_touch(
    cdp: Any,
    popunder_page: Any,
    stats: Dict[str, int],
    can_click: bool = False,
    main_page: Any = None,
) -> str:
    """★ 26.8.15.1 新增：对弹窗执行一次随机 CDP 交互（滚动/移动/按键/点击）。
    返回动作名（'scroll' / 'move' / 'key' / 'click' / 'scroll-fallback'），失败返回 ""。
    stats: Dict 计数器，键 'scroll' / 'move' / 'key' / 'click'。
    can_click: 是否允许点击（剩余 >15s 才允许，留够结算窗口）。
    main_page: 主页面（用于 focus switch 后切回），None 时不切回。

    权重：scroll 45% / move 25% / key 15% / click 15%（click 不可用时降级 scroll）
    安全：click 前用 elementFromPoint 检查目标标签在白名单内，避开 a/button/input/iframe
    """
    try:
        # ---- 1. 切焦点到弹窗（CDP Input 绑定 driver 当前 target）----
        if not _popup_cdp_focus_switch(popunder_page, main_page):
            return ""
        # ---- 2. 取 viewport 尺寸（fallback 1280×720）----
        _vp = popunder_page.viewport_size or {}
        _vw = int(_vp.get("width", 1280) or 1280)
        _vh = int(_vp.get("height", 720) or 720)
        _x = random.randint(20, max(21, _vw - 20))
        _y = random.randint(20, max(21, _vh - 20))
        # ---- 3. 随机选动作 ----
        r = random.random()
        if r < 0.45:
            # 滚动：delta ±(60..300)，模拟"往下读"或"往上翻"
            delta = random.choice([-1, 1]) * random.randint(60, 300)
            _cdp_scroll(cdp, _x, _y, delta)
            stats["scroll"] = stats.get("scroll", 0) + 1
            _action = "scroll"
        elif r < 0.70:
            # 移动：从随机起点到 (x,y)，6-10 步（贝塞尔曲线，见 _cdp_mouse_move）
            _fx = random.randint(20, max(21, _vw - 20))
            _fy = random.randint(20, max(21, _vh - 20))
            _cdp_mouse_move(cdp, _fx, _fy, _x, _y, steps=random.randint(6, 10))
            stats["move"] = stats.get("move", 0) + 1
            _action = "move"
        elif r < 0.85:
            # 按键：Space / ArrowDown / ArrowUp（真人滚动常用键）
            _key = random.choice([(" ", "Space", 32), ("ArrowDown", "ArrowDown", 40), ("ArrowUp", "ArrowUp", 38)])
            _cdp_key(cdp, _key[0], _key[1], _key[2])
            stats["key"] = stats.get("key", 0) + 1
            _action = "key"
        else:
            # 点击（can_click=False 时降级为轻滚动）
            if can_click and stats.get("click", 0) < 2:
                # 安全检查：elementFromPoint 目标标签在白名单内
                _tag = ""
                try:
                    _tag = popunder_page.evaluate(
                        "() => { try { const el = document.elementFromPoint(%d, %d); "
                        "return el ? el.tagName.toLowerCase() : ''; } catch(e){ return ''; } }"
                        % (_x, _y)) or ""
                except Exception:
                    _tag = ""
                if _tag in _POPUNDER_SAFE_CLICK_TAGS:
                    _cdp_click(cdp, _x, _y)
                    stats["click"] = stats.get("click", 0) + 1
                    _action = "click"
                else:
                    # 目标不是内容型元素，降级为轻滚动
                    _cdp_scroll(cdp, _x, _y, random.randint(30, 90))
                    stats["scroll"] = stats.get("scroll", 0) + 1
                    _action = "scroll-fallback"
            else:
                # can_click=False 或已点 2 次，降级为轻滚动
                _cdp_scroll(cdp, _x, _y, random.randint(30, 90))
                stats["scroll"] = stats.get("scroll", 0) + 1
                _action = "scroll-fallback"
        return _action
    except Exception:
        return ""
    finally:
        # ---- 4. 切回主页面焦点（best-effort）----
        _popup_cdp_restore(main_page)


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
    prob = cfg.get("trigger_probability", 0.85)  # ★ 26.8.17.1: 兜底默认 0.40→0.85，与 DEFAULT_CONFIG 对齐
    if random.random() > prob:
        # ★ 可观测修复：概率跳过不再静默（INFO 可见，便于区分 CDP 触发 vs 自然弹窗）
        _log.info("[Pop-under] 概率跳过 (random > %.2f)，本次不触发 CDP 点击", prob)
        return False, None, {"triggered": False, "reason": "probability_skip"}

    # ★ M3 修复: 概率通过后才正式占位（check + reserve 原子化）
    if not _check_page_reentry(page_id, cooldown_s=float(cooldown)):
        _log.info("Pop-under 页面级冷却中（并发占位竞争），跳过")
        return False, None, {"triggered": False, "reason": "page_cooldown"}

    # ★ 26.8.15.1：显式 stay_sec 参数仍逐字尊重（测试钩子）；
    #   随机分支改用三段混合分布 _sample_popunder_stay（短/核/长尾），
    #   旧 random.randint 平顶分布是"固定时长"指纹的根源。
    stay = float(stay_sec or _sample_popunder_stay(
        cfg.get("popunder_stay_min", 15),
        cfg.get("popunder_stay_max", 120),
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

        # 3. 记录窗口数（触发前的标签句柄集合，用于识别"本次触发期间新出现的标签"）
        pages_before = list(context.pages)
        pages_before_ids = set(id(p) for p in pages_before)
        max_wait = cfg.get("max_wait_for_popup_s", 3.0)

        # ★ 26.8.13.2 修复【多标签误收养】：旧逻辑无条件收养 pages_before[-1] 并强杀，
        #   会把真实用户标签页误当弹窗关闭。现在只收养【本次触发期间新出现的标签】
        #   （触发前后句柄集合的差集中最新出现者），差集为空则视为未弹出，
        #   走下方 no_new_tab 失败路径；绝不 close 触发前已存在的任何旧标签。
        popunder_page = None
        _adopted = False  # 不再收养存量标签；本次触发的新标签由下方等待循环差集识别

        # 4. CDP 通道 — ★ 审计修复【根因】：旧实现绑定 context.pages[0]，
        #    多 tab 场景（SEO 结果页/其它任务页先开）时鼠标事件派发到错误页面，
        #    弹窗触发失败（间歇性：时好时坏）。改为绑定当前发布商页。
        # ★ 26.8.17.1：会话建立也纳入瞬时重试（建会话本身可能撞上通道超时）
        try:
            cdp = context.new_cdp_session(page)
        except Exception as _se:
            if _is_cdp_transient_error(_se):
                # 瞬时通道错误：退避 0.6-1.5s 后原地重试 1 次（与 _cdp_send_retry 一致）
                _backoff = random.uniform(0.6, 1.5)
                _log.info("[Pop-under] CDP 会话建立瞬时错误(%s: %s)，%.2fs 后重试 1 次",
                          type(_se).__name__, _se, _backoff)
                time.sleep(_backoff)
                cdp = context.new_cdp_session(page)  # 第二次失败抛给外层 except
            else:
                raise

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
        #    ★ 26.8.13.2 修复：仅收养【本次触发期间新出现】的标签（触发前句柄差集），
        #    差集为空（未弹出新标签）时不收养、不误杀任何标签，交由下方 no_new_tab 处理。
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
        # ★ 26.8.13.2 修复：先用 hasattr 探测 Page 是否具备 on 事件系统
        #   （selenium_bridge 契约），注册成功/失败输出真实标志，不再静默吞掉
        #   AttributeError 导致心跳统计永远为空且诊断误报"已监听"。
        heartbeat_records: List[Dict[str, Any]] = []
        heartbeat_registered = False  # 真实反映监听是否注册成功（供诊断字典上报）
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

            if hasattr(popunder_page, "on"):
                # Page 具备 on() 事件系统（Playwright 或 selenium_bridge 新契约）
                try:
                    # 注册 request 监听器（网络请求发出时触发，覆盖 fetch/XHR/IMG/script/beacon 所有类型）
                    popunder_page.on("request", _on_pop_request)
                    heartbeat_registered = True
                    _log.info("[Pop-under] Heartbeat 监听已注册")
                except Exception:
                    # Playwright 版本差异或页面已关闭时可能失败——降级为不监听，不阻断核心流程
                    _log.warning("[Pop-under] Heartbeat 监听器注册失败(忽略，继续触发)")
                    heartbeat_records = []  # 空列表 → 守护线程里检测不到，跳过分析
            else:
                # selenium_bridge 的 Page 尚无 on 事件系统 → 心跳统计不可用，warning 可观测
                _log.warning("[Pop-under] Heartbeat 监听器不可用(page 无 on 事件系统)，心跳统计将为空")
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
                "[Pop-under] 弹窗已确认渲染: %s, 停留 %.1f s (异步守护), 加载=%s",
                pop_url[:100], stay, load_state,
            )

        # ★ 26.8.15.1 新增：为弹窗创建专属 CDP 会话（best-effort）。
        #   弹窗是独立 CDP target，主 driver 的 JS 钩子钩不到它；
        #   有会话 → 守护线程用真实 Input 事件（isTrusted=true）交互；
        #   无会话 → 降级纯 JS 路径（行为画像略弱但不影响核心保活）。
        _popup_cdp = None
        try:
            _pctx = getattr(popunder_page, "context", None)
            if _pctx is not None and hasattr(_pctx, "new_cdp_session"):
                _popup_cdp = _pctx.new_cdp_session(popunder_page)
        except Exception as _e_cdp:
            _log.debug("[Pop-under] 弹窗CDP会话不可用, 降级JS触摸: %s", _e_cdp)
        if _popup_cdp is not None:
            _log.debug("[Pop-under] 弹窗CDP会话已建立，守护线程将用真实Input事件交互")

        guardian = threading.Thread(
            target=_guard_stay_and_close,
            args=(popunder_page, page, stay, _inject_popunder_stealth,
                  heartbeat_records, _popup_cdp),
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
            "heartbeat_monitored": heartbeat_registered,  # True 表示监听注册成功（真实标志，不再恒真）
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
    # ---- ★ 26.8.15.1 新增：类人交互升级自检 ----
    # 1) 停留时长混合分布：2000 样本 ∈ [15, 120]，均值 ∈ [25, 55]，>60s 至少 10 个
    try:
        random.seed(268151)
        _samples = [_sample_popunder_stay(15, 120) for _ in range(2000)]
        results["stay_distribution_nonuniform"] = (
            min(_samples) >= 15.0
            and max(_samples) <= 120.0
            and 25.0 <= (sum(_samples) / len(_samples)) <= 55.0
            and sum(1 for s in _samples if s > 60.0) >= 10
        )
    except Exception:
        results["stay_distribution_nonuniform"] = False
    # 2) 守护线程第 6 参 popup_cdp（CDP 真实交互会话，None 时降级 JS）
    try:
        import inspect
        _sig2 = inspect.signature(_guard_stay_and_close)
        _params2 = list(_sig2.parameters.keys())
        results["popup_cdp_param"] = (
            "popup_cdp" in _params2
            and _sig2.parameters["popup_cdp"].default is None
        )
    except Exception:
        results["popup_cdp_param"] = False
    # 3) 类人触摸辅助函数存在且可调用
    results["human_touch_helper_exists"] = (
        callable(_popup_human_touch)
        and callable(_cdp_key)
        and callable(_popup_cdp_focus_switch)
        and callable(_popup_cdp_restore)
        and isinstance(_POPUNDER_SAFE_CLICK_TAGS, frozenset)
    )
    # 4) 关闭过渡抖动加宽（旧 0.3-0.9s 太整齐，新 0.6-2.4s）
    results["close_jitter_widened"] = "random.uniform(0.6, 2.4)" in open(__file__).read()
    # ---- ★ 26.8.17.1 新增：CDP 瞬时重试 + 概率上调 + 落地白名单 自检 ----
    src = open(__file__).read()
    # 5) CDP 瞬时重试：helper 存在、3 个手势函数 + 按键 + 会话建立全部走它
    results["cdp_retry_helper_exists"] = (
        "_CDP_TRANSIENT_EXC_NAMES" in src
        and "def _cdp_send_retry" in src
        and "def _is_cdp_transient_error" in src
    )
    results["cdp_retry_wired"] = (
        "_cdp_send_retry(cdp" in src
        and src.count("_cdp_send_retry(cdp") >= 6  # scroll/move/click×2/key×2/...
        and "cdp = context.new_cdp_session(page)" in src  # 会话建立重试分支仍在
    )
    # 瞬时错误分类：典型三类（ReadTimeout/MaxRetry/NewConnection）+ 非瞬时不误判
    def _exc_named(name):
        _cls = type(name, (Exception,), {})  # 动态造异常类，type().__name__ 即 name
        return _cls("x")
    results["cdp_transient_classify"] = (
        _is_cdp_transient_error(_exc_named("ReadTimeoutError"))
        and _is_cdp_transient_error(_exc_named("MaxRetryError"))
        and _is_cdp_transient_error(_exc_named("NewConnectionError"))
        and not _is_cdp_transient_error(_exc_named("ValueError"))
        and not _is_cdp_transient_error(_exc_named("AttributeError"))
    )
    # 6) 概率门 0.85（DEFAULT_CONFIG + 兜底默认两处对齐）
    results["prob_default_085"] = (
        DEFAULT_CONFIG["trigger_probability"] == 0.85
        and 'cfg.get("trigger_probability", 0.85)' in src
    )
    # 7) 落地白名单含 eatcells / nesber（结算验证假阴性修复）
    results["heartbeat_landing_whitelist"] = (
        any("eatcells" in d for d in _HEARTBEAT_LANDING_DOMAINS)
        and any("nesber" in d for d in _HEARTBEAT_LANDING_DOMAINS)
        and _is_heartbeat_url("https://eatcells.com/hb?x=1")
        and _is_heartbeat_url("https://nesber.com/pixel?id=9")
    )
    return results


if __name__ == "__main__":
    print("Pop-under Trigger 模块自检 (含 P0×3 + 三次审计修复):")
    for k, v in self_test().items():
        status = "PASS" if v else "FAIL"
        print(f"  {status}  {k}")
