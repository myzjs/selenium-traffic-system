import random
import re
import os
import time

# 本脚本所在目录（app.py 同路径）；演练报告统一存放到本目录下的 report/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = BASE_DIR
REPORT_DIR = os.path.join(BASE_DIR, "report")
os.makedirs(REPORT_DIR, exist_ok=True)

# ========== 全局配置（替代原config.py） ==========
# 指纹统一约束（IP/时区/语言/地区强一致）
TIMEZONE = "Asia/Shanghai"
LOCALE = "zh-CN"
GEO_LAT = 31.81
GEO_LON = 119.97  # 常州溧阳坐标

# 广告仿真参数（AdSense合规行为区间）
EXPOSE_MIN_SEC = 3
EXPOSE_MAX_SEC = 12
CLICK_PROBABILITY = 0.22  # 自然点击概率
PAGE_STAY_MIN = 15
PAGE_STAY_MAX = 90

# 反WebDriver开关
DISABLE_WEBDRIVER = True
CANVAS_FINGER_RANDOM = True  # 随机Canvas指纹绕过设备识别

# UA可疑风险关键词库（支持模糊匹配）—— 扩充版
UA_RISK_KEYWORDS = [
    "Selenium", "ChromeDriver", "undetected", "Headless", "cdc_",
    "Automation", "Robot", "bot", "Python", "Playwright", "Puppeteer",
    "PhantomJS", "SlimerJS", "Splinter", "Mechanize", "Scrapy",
    "HTTrack", "Wget", "curl", "Java/", "Go-http-client",
    "Axios", "node-fetch", "httpx", "reqwest",
    "HeadlessChrome", "Chrome Headless", "AutomationControlled",
    "pw-", "playwright", "puppeteer", "selenium"
]

# 标准广告尺寸（IAB规范）
STANDARD_AD_SIZES = [
    (300, 250), (728, 90), (160, 600), (320, 50), (320, 100),
    (336, 280), (970, 250), (970, 90), (300, 600), (300, 50),
    (250, 250), (200, 200), (468, 60), (120, 600), (120, 240),
    (300, 100), (728, 250), (970, 150),
]
AD_SIZE_TOLERANCE = 5  # 像素容差

# 高危风险权重（AdSense封号优先级）—— 扩充版
RISK_WEIGHT = {
    # === 自动化高危 ===
    "webdriver_leak": 45,
    "cdc_trace_leak": 40,
    "playwright_residual": 38,
    "tostring_leak": 35,
    "function_consistency_fail": 30,
    "automation_header": 25,

    # === 指纹类 ===
    "canvas_finger_no_noise": 35,
    "webgl_finger_no_noise": 30,
    "canvas_hook_detectable": 28,
    "webgl_hook_detectable": 25,
    "screen_resolution_mismatch": 20,
    "hardware_info_abnormal": 18,
    "battery_api_missing": 12,
    "font_fingerprint_uniform": 15,

    # === 时区/地理 ===
    "tz_geo_mismatch": 30,
    "lang_geo_mismatch": 22,
    "timezone_not_common": 15,

    # === 网络/IP ===
    "webrtc_ip_leak": 28,
    "empty_referer": 20,
    "proxy_header_leak": 25,
    "datacenter_ip_suspect": 20,
    "ip_frequency_high": 18,

    # === HTTP请求头 ===
    "ua_risk_keyword": 22,
    "ua_version_fixed": 18,
    "missing_standard_header": 15,
    "header_ua_sec_ch_ua_mismatch": 25,
    "header_ua_platform_mismatch": 22,
    "header_accept_abnormal": 18,
    "header_accept_lang_geo_mismatch": 20,
    "header_sec_fetch_missing": 15,
    "header_extra_suspicious": 20,
    "header_referer_inconsistent": 18,

    # === 插件/扩展 ===
    "plugin_fake_abnormal": 12,
    "plugin_toString_anomaly": 15,

    # === 存储 ===
    "storage_unrandomized": 10,

    # === 广告专属 ===
    "ad_no_valid_expose_single": 18,
    "ad_css_hidden_single": 30,
    "ad_non_standard_size": 15,
    "ad_css_distorted": 20,
    "ad_overlap_content": 25,
    "ad_too_many_on_page": 18,
    "ad_invalid_format": 16,

    # === 行为模式 ===
    "page_stay_too_short": 10,
    "scroll_pattern_abnormal": 18,
    "no_interaction_at_all": 25,
    "click_interval_too_fast": 20,
    "mouse_pattern_robotic": 22,

    # === AdSense 合规（站点级） ===
    "adsense_click_encouragement": 35,   # 诱导点击广告文案（严重违规）
    "adsense_above_fold_overload": 20,  # 首屏广告过多（>3个）
    "adsense_ad_ratio_high": 18,         # 广告/内容比例过高
    "adsense_no_privacy_policy": 15,     # 缺少隐私政策链接
    "adsense_no_cookie_consent": 5,      # 缺少 Cookie 同意横幅（目标站属性，非自动化特征，降权）
}


def run_risk_detect(page, proxy_ip, ad_selector=None, expected_timezone=None, expected_locale=None):
    """
    风控漏洞检测探针 v2.0
    全面覆盖谷歌广告联盟风控维度
    :param ad_selector: 可选，指定广告CSS选择器；不传则自动扫描全页广告
    :param expected_timezone: 可选，期望的浏览器时区（不传则用全局 TIMEZONE）
    :param expected_locale: 可选，期望的语言（不传则用全局 LOCALE）
    """
    report = {
        "base_info": {},
        "automation_probe": {},
        "automation_deep": {},
        "fingerprint": {},
        "device_consistency": {},
        "network_ip": {},
        "http_header_deep": {},
        "ad_risk": {},
        "behavior_pattern": {},
        "session_storage": {},
        "timezone_geo": {},
        "risk_calc": {}
    }

    # ====================== 1. 基础环境信息 ======================
    ua = page.evaluate("navigator.userAgent") or ""
    platform = page.evaluate("navigator.platform") or ""
    lang = page.evaluate("navigator.language") or ""
    languages = page.evaluate("navigator.languages") or []
    screen_size = page.evaluate("[screen.width, screen.height]") or [0, 0]
    viewport = page.evaluate("[window.innerWidth, window.innerHeight]") or [0, 0]
    device_pixel_ratio = page.evaluate("window.devicePixelRatio") or 1
    color_depth = page.evaluate("screen.colorDepth") or 0
    report["base_info"] = {
        "ua_full": ua,
        "ua_short": ua[:100],
        "platform": platform,
        "lang": lang,
        "languages": languages,
        "screen_size": screen_size,
        "viewport": viewport,
        "device_pixel_ratio": device_pixel_ratio,
        "color_depth": color_depth,
    }

    # ====================== 2. 自动化探针高危检测 ======================
    auto_probe = {}
    auto_probe["nav_webdriver"] = page.evaluate("""
        (() => {
            // 仅当自动化标志位为 true 时判定泄漏（真实浏览器返回 false，隐身返回 undefined）
            if (navigator.webdriver === true) return true;
            try {
                // 检测 prototype getter 是否被粗暴覆盖为非原生（劣质隐身）
                const desc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
                if (desc && desc.get && !Function.prototype.toString.call(desc.get).includes('native code')) return true;
            } catch(e) {}
            return false;
        })()
    """) or False

    auto_probe["cdc_trace"] = page.evaluate("""
        (() => {
            let hasCdc = false;
            for (let key in window) {
                if (key.startsWith('cdc_') || key.startsWith('$cdc_')) hasCdc = true;
            }
            // 也检查document
            for (let key in document) {
                if (key.startsWith('cdc_') || key.startsWith('$cdc_')) hasCdc = true;
            }
            return hasCdc;
        })()
    """) or False

    auto_probe["chrome_runtime_valid"] = page.evaluate("typeof window.chrome?.runtime === 'object'") or False
    plugins_len = page.evaluate("navigator.plugins.length") or 0
    auto_probe["plugins_empty"] = plugins_len == 0
    auto_probe["mime_empty"] = page.evaluate("navigator.mimeTypes.length === 0") or False

    # Playwright / Puppeteer 残留检测
    auto_probe["playwright_residuals"] = page.evaluate("""
        (() => {
            const markers = [
                '__playwright', '__pw_manual__', '__PW_inspect',
                '__selenium_unwrapped', '__webdriver_evaluate', '__driver_evaluate',
                '__webdriver_script_fn', '__fxdriver_evaluate', '__driver_unwrapped',
                '_Selenium_IDE_Recorder', 'callSelenium', '_selenium', '__webdriver',
                '__selenium_evaluate', 'domAutomationController', 'domAutomation'
            ];
            const found = [];
            for (const m of markers) {
                try { if (window[m] !== undefined) found.push(m); } catch(e) {}
            }
            return found;
        })()
    """) or []

    report["automation_probe"] = auto_probe

    # ====================== 2.1 自动化深度检测（toString泄漏/函数一致性） ======================
    auto_deep = {}

    # 检测被覆盖函数的 toString() 是否泄漏 "native code" 异常
    auto_deep["hook_toString_check"] = page.evaluate("""
        (() => {
            const results = {};
            // 检查关键原型函数是否被覆盖（非native code）
            const targets = {
                'CanvasRenderingContext2D.getImageData': CanvasRenderingContext2D.prototype.getImageData,
                'WebGLRenderingContext.getParameter': WebGLRenderingContext.prototype.getParameter,
                'HTMLCanvasElement.toDataURL': HTMLCanvasElement.prototype.toDataURL,
                'Navigator.prototype.toString': Navigator.prototype.toString,
                'permissions.query': navigator.permissions?.query,
            };
            for (const [name, fn] of Object.entries(targets)) {
                if (!fn) continue;
                try {
                    const str = Function.prototype.toString.call(fn);
                    // 正常native函数应包含 "native code"
                    // 被覆盖的函数会显示实际代码
                    results[name] = {
                        is_native: str.includes('native code'),
                        suspicious: !str.includes('native code') && !str.includes('[native code]'),
                        preview: str.substring(0, 80)
                    };
                } catch(e) {
                    results[name] = {error: e.message};
                }
            }
            return results;
        })()
    """) or {}

    # 检测 navigator 对象的 toString 是否返回异常值
    auto_deep["navigator_toString_check"] = page.evaluate("""
        (() => {
            try {
                const str = Object.prototype.toString.call(navigator);
                return {value: str, is_normal: str === '[object Navigator]'};
            } catch(e) { return {error: e.message}; }
        })()
    """) or {}

    # 检测 screen 对象的 toString
    auto_deep["screen_toString_check"] = page.evaluate("""
        (() => {
            try {
                const str = Object.prototype.toString.call(screen);
                return {value: str, is_normal: str === '[object Screen]'};
            } catch(e) { return {error: e.message}; }
        })()
    """) or {}

    # 检测 permissions.query 是否被覆盖（CDP特征）
    auto_deep["permissions_query_native"] = page.evaluate("""
        (() => {
            try {
                const str = Function.prototype.toString.call(navigator.permissions.query);
                return {
                    is_native: str.includes('native code'),
                    suspicious: !str.includes('native code')
                };
            } catch(e) { return {error: e.message}; }
        })()
    """) or {}

    # 检测 Notification.permission 一致性（CDP覆盖会暴露）
    auto_deep["notification_permission_consistent"] = page.evaluate("""
        (() => {
            try {
                return Notification.permission === 'default' || Notification.permission === 'granted' || Notification.permission === 'denied';
            } catch(e) { return false; }
        })()
    """) or False

    report["automation_deep"] = auto_deep

    # ====================== 3. 指纹检测 Canvas / WebGL ======================
    finger = {}
    canvas_data = page.evaluate("""
        (() => {
            const c = document.createElement('canvas');
            const ctx = c.getContext('2d');
            ctx.fillText('test_finger_2026', 12, 18);
            const raw = ctx.getImageData(0,0,20,20).data.toString();
            // 检测hook是否存在：对比prototype方法是否为native
            const hasNoiseHook = !CanvasRenderingContext2D.prototype.getImageData.toString().includes('native code');
            // 多次调用检测：同一canvas多次getImageData结果是否一致（有噪声hook则每次不同）
            ctx.fillText('test_finger_2026', 12, 18);
            const raw2 = ctx.getImageData(0,0,20,20).data.toString();
            return {data: raw, data2: raw2, has_noise_hook: hasNoiseHook, consistent: raw === raw2};
        })()
    """) or {"data": "", "data2": "", "has_noise_hook": False, "consistent": True}
    finger["canvas_raw_hash"] = canvas_data["data"][:150] if canvas_data.get("data") else ""
    finger["canvas_noise_injected"] = canvas_data.get("has_noise_hook", False)
    finger["canvas_hook_detectable"] = canvas_data.get("has_noise_hook", False)  # hook本身可被检测
    finger["canvas_consistency"] = canvas_data.get("consistent", True)

    webgl_info = page.evaluate("""
        (() => {
            const gl = document.createElement('canvas').getContext('webgl');
            if (!gl) return {renderer: null, vendor: null, has_hook: false};
            const hookCheck = gl.getParameter.toString().indexOf('native code') === -1;
            // 多次调用检测一致性
            const r1 = gl.getParameter(gl.RENDERER);
            const v1 = gl.getParameter(gl.VENDOR);
            return {
                renderer: r1, vendor: v1,
                has_hook: hookCheck,
                // 检查renderer字符串是否包含常见伪造特征
                renderer_suspicious: r1 && (r1.includes('SwiftShader') || r1 === 'Google Inc. (Intel)'),
            };
        })()
    """) or {"renderer": null, "vendor": null, "has_hook": False, "renderer_suspicious": False}
    finger["webgl_renderer"] = webgl_info["renderer"]
    finger["webgl_vendor"] = webgl_info["vendor"]
    finger["webgl_noise_injected"] = webgl_info["has_hook"]
    finger["webgl_hook_detectable"] = webgl_info["has_hook"]
    finger["webgl_renderer_suspicious"] = webgl_info.get("renderer_suspicious", False)
    finger["font_count"] = page.evaluate("document.fonts.size") or 0
    finger["media_devices_exist"] = page.evaluate("navigator.mediaDevices !== undefined") or False

    # 电池API检测（真实浏览器通常支持）
    finger["battery_api_exist"] = page.evaluate("""
        (() => {
            return 'getBattery' in navigator;
        })()
    """) or False

    report["fingerprint"] = finger

    # ====================== 3.1 设备指纹一致性检测 ======================
    device_cons = {}

    # 屏幕分辨率 vs 视口大小合理性
    device_cons["screen_vs_viewport"] = page.evaluate("""
        (() => {
            const sw = screen.width, sh = screen.height;
            const vw = window.innerWidth, vh = window.innerHeight;
            return {
                screen: [sw, sh],
                viewport: [vw, vh],
                // 视口不应大于屏幕
                viewport_larger_than_screen: vw > sw + 20 || vh > sh + 20,
                // viewport占比合理（通常70%-95%）
                viewport_ratio: sw > 0 ? Math.round((vw/sw)*100)/100 : 0,
            };
        })()
    """) or {}

    # 硬件信息合理性
    device_cons["hardware_info"] = page.evaluate("""
        (() => {
            const hc = navigator.hardwareConcurrency;
            const dm = navigator.deviceMemory;
            return {
                hardwareConcurrency: hc,
                deviceMemory: dm,
                // 合理性检查
                hc_reasonable: hc >= 1 && hc <= 128,
                hc_suspicious: hc === undefined || hc === 0 || hc > 128,
                dm_reasonable: dm === undefined || (dm >= 0.25 && dm <= 256),
                dm_suspicious: dm !== undefined && (dm === 0 || dm > 256),
            };
        })()
    """) or {}

    # 设备像素比合理性
    device_cons["device_pixel_ratio"] = {
        "value": device_pixel_ratio,
        "reasonable": 0.5 <= device_pixel_ratio <= 4.0,
    }

    # colorDepth 合理性
    device_cons["color_depth"] = {
        "value": color_depth,
        "reasonable": color_depth in (24, 30, 32, 48),
    }

    # 电池API详情（如支持）
    try:
        device_cons["battery_info"] = page.evaluate("""
            (() => {
                if (!('getBattery' in navigator)) return {supported: false};
                try {
                    return Promise.race([
                        navigator.getBattery().then(b => ({
                            supported: true,
                            level: b.level,
                            charging: b.charging,
                            suspicious_auto: b.level === 1 && b.charging === true
                        })),
                        new Promise(res => setTimeout(() => res({supported: false, timeout: true}), 3000))
                    ]);
                } catch(e) { return {supported: false, error: e.message}; }
            })()
        """) or {"supported": False}
    except Exception:
        device_cons["battery_info"] = {"supported": False}

    report["device_consistency"] = device_cons

    # ====================== 4. 时区 & 地理一致性 ======================
    tz_info = {}
    browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or ""
    _target_tz = expected_timezone or TIMEZONE
    tz_info["browser_tz"] = browser_tz
    tz_info["config_target_tz"] = _target_tz
    tz_info["tz_match"] = browser_tz == _target_tz

    # 检查时区是否为全球常见时区（非罕见时区可能触发审查）
    common_timezones = {
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "America/Toronto", "America/Vancouver", "America/Mexico_City",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Rome",
        "Europe/Amsterdam", "Europe/Brussels", "Europe/Zurich", "Europe/Stockholm",
        "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore",
        "Asia/Mumbai", "Asia/Kolkata", "Asia/Dubai", "Asia/Bangkok",
        "Australia/Sydney", "Australia/Melbourne",
        "Pacific/Auckland", "Africa/Johannesburg", "Africa/Cairo",
    }
    tz_info["is_common_timezone"] = browser_tz in common_timezones

    # 语言与UA平台一致性
    tz_info["lang_platform_match"] = True
    if "en" in lang.lower() and "linux" in platform.lower():
        tz_info["lang_platform_match"] = True  # 英语+Linux正常
    elif "zh" in lang.lower() and "win" in platform.lower():
        tz_info["lang_platform_match"] = True  # 中文+Windows正常

    report["timezone_geo"] = tz_info

    # ====================== 5. 网络 & WebRTC 泄漏检测 ======================
    net = {}
    try:
        net["webrtc_leak_ip"] = page.evaluate("""
            (() => {
                return new Promise(res => {
                    let leaked = false;
                    const timer = setTimeout(() => res(leaked), 5000);
                    try {
                        if (typeof RTCPeerConnection === 'undefined') { clearTimeout(timer); res(false); return; }
                        const pc = new RTCPeerConnection({iceServers: []});
                        pc.createDataChannel('test');
                        pc.createOffer().then(o => pc.setLocalDescription(o));
                        pc.onicecandidate = e => {
                            // 收集结束（null candidate）时判定
                            if (!e.candidate) { clearTimeout(timer); res(leaked); return; }
                            const c = e.candidate.candidate || '';
                            // 仅内网/私有 IP 暴露才算泄漏（公网 host candidate 属正常）
                            if (/(10\\.|192\\.168\\.|172\\.(1[6-9]|2\\d|3[01])\\.|0\\.0\\.0\\.0)/.test(c)) leaked = true;
                        };
                    }catch{clearTimeout(timer);res(false)}
                })
            })()
        """) or False
    except Exception:
        net["webrtc_leak_ip"] = False

    # WebRTC是否被完全禁用（过度禁用也是风险信号）
    net["webrtc_completely_disabled"] = page.evaluate("""
        (() => {
            return typeof RTCPeerConnection === 'undefined' && typeof webkitRTCPeerConnection === 'undefined';
        })()
    """) or False

    ref_str = page.evaluate("document.referrer") or ""
    net["referer_content"] = ref_str[:120]
    net["has_valid_referer"] = len(ref_str.strip()) > 0

    # 尝试获取页面出口IP
    net["proxy_ip"] = proxy_ip or "unknown"

    headers = page.request.headers if hasattr(page, "request") else {}
    net["missing_accept_lang"] = "Accept-Language" not in headers
    net["missing_sec_ch_ua"] = "Sec-Ch-Ua" not in headers
    report["network_ip"] = net

    # ====================== 6. HTTP请求头深度检测 ======================
    header_deep = {}

    # 6.1 请求头完整性检查
    required_headers = [
        "Accept", "Accept-Language", "Accept-Encoding",
        "Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform",
        "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Sec-Fetch-User",
        "Upgrade-Insecure-Requests", "User-Agent",
    ]
    missing_headers = []
    present_headers = []
    for h in required_headers:
        h_lower = h.lower()
        found = any(k.lower() == h_lower for k in headers.keys())
        if found:
            present_headers.append(h)
        else:
            missing_headers.append(h)
    header_deep["required_headers"] = required_headers
    header_deep["missing_headers"] = missing_headers
    header_deep["missing_count"] = len(missing_headers)
    header_deep["completeness_pct"] = round(len(present_headers) / len(required_headers) * 100, 1) if required_headers else 100

    # 6.2 UA 与 Sec-CH-UA 一致性
    ua_browser_version = ""
    ua_match = re.search(r'Chrome/(\d+)', ua)
    if ua_match:
        ua_browser_version = ua_match.group(1)

    sec_ch_ua = ""
    for k, v in headers.items():
        if k.lower() == "sec-ch-ua":
            sec_ch_ua = v
            break

    header_deep["ua_chrome_version"] = ua_browser_version
    header_deep["sec_ch_ua_header"] = sec_ch_ua[:120]

    # 检查Sec-CH-UA中的Chrome版本是否与UA一致
    sec_ch_version_match = re.search(r'Chromium";v="(\d+)"', sec_ch_ua) or re.search(r'Chrome";v="(\d+)"', sec_ch_ua)
    if sec_ch_version_match and ua_browser_version:
        header_deep["ua_sec_ch_ua_version_match"] = sec_ch_version_match.group(1) == ua_browser_version
    else:
        header_deep["ua_sec_ch_ua_version_match"] = True  # 无法检测时默认通过

    # 6.3 Sec-CH-UA-Platform 与 UA 平台一致性
    sec_ch_platform = ""
    for k, v in headers.items():
        if k.lower() == "sec-ch-ua-platform":
            sec_ch_platform = v.strip('"')
            break

    ua_platform = ""
    if "Windows" in ua:
        ua_platform = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        ua_platform = "macOS"
    elif "Linux" in ua and "Android" not in ua:
        ua_platform = "Linux"
    elif "Android" in ua:
        ua_platform = "Android"

    header_deep["sec_ch_ua_platform"] = sec_ch_platform
    header_deep["ua_declared_platform"] = ua_platform
    # macOS 在 Sec-CH-UA 中显示为 "macOS"
    platform_map = {"macOS": "macOS", "Windows": "Windows", "Linux": "Linux", "Android": "Android"}
    header_deep["platform_consistent"] = (
        platform_map.get(ua_platform, ua_platform) == sec_ch_platform
        if sec_ch_platform else True
    )

    # 6.4 Accept头合理性
    accept_header = ""
    for k, v in headers.items():
        if k.lower() == "accept":
            accept_header = v
            break
    header_deep["accept_header"] = accept_header[:120]
    # 正常Chrome的Accept应包含 text/html 和 application/xhtml+xml
    header_deep["accept_has_html"] = "text/html" in accept_header
    header_deep["accept_has_xhtml"] = "application/xhtml+xml" in accept_header
    header_deep["accept_abnormal"] = not header_deep["accept_has_html"]

    # 6.5 Accept-Language 与浏览器语言设置一致性
    accept_lang = ""
    for k, v in headers.items():
        if k.lower() == "accept-language":
            accept_lang = v
            break
    header_deep["accept_language_header"] = accept_lang[:80]
    header_deep["accept_lang_matches_navigator"] = (
        lang.split("-")[0].lower() in accept_lang.lower()
        if accept_lang and lang else True
    )

    # 6.6 Sec-Fetch-* 系列完整性
    sec_fetch_headers = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl.startswith("sec-fetch"):
            sec_fetch_headers[k] = v
    header_deep["sec_fetch_headers"] = sec_fetch_headers
    header_deep["sec_fetch_complete"] = len(sec_fetch_headers) >= 3

    # 6.7 可疑额外请求头检测
    suspicious_header_prefixes = ["x-selenium", "x-automation", "x-puppeteer", "x-playwright",
                                   "x-custom-automated", "x-webdriver", "x-scrapy"]
    found_suspicious_headers = []
    for k in headers.keys():
        kl = k.lower()
        for prefix in suspicious_header_prefixes:
            if kl.startswith(prefix):
                found_suspicious_headers.append(k)
    header_deep["suspicious_extra_headers"] = found_suspicious_headers

    # 6.8 UA风险关键词检测（扩充版）
    ua_risk = {}
    hit_risk_words = []
    for kw in UA_RISK_KEYWORDS:
        if re.search(re.escape(kw), ua, re.IGNORECASE):
            hit_risk_words.append(kw)
    # 也检查请求头中的其他字段
    for k, v in headers.items():
        for kw in UA_RISK_KEYWORDS:
            if kw.lower() in str(v).lower() and kw not in hit_risk_words:
                hit_risk_words.append(f"{kw}(in {k})")
    ua_risk["risk_keywords_hit"] = hit_risk_words
    ua_risk["ua_risk_flag"] = len(hit_risk_words) > 0
    ua_risk["fixed_chrome_version"] = re.search(r"Chrome\/120\.0\.0\.0", ua) is not None

    report["http_header_deep"] = header_deep
    report["http_header"] = {
        "ua_check": ua_risk,
        "lack_standard_header": net["missing_accept_lang"] or net["missing_sec_ch_ua"],
        "header_completeness": header_deep["completeness_pct"],
        "missing_header_count": header_deep["missing_count"],
    }

    # ====================== 7. 广告专属风控检测 ======================
    ad_result = page.evaluate("""
        (customSelector) => {
            const selectors = [
                'ins.adsbygoogle', '[data-ad-client]', '[data-ad-slot]',
                'iframe[src*="googleadservices.com"]', 'iframe[src*="doubleclick.net"]',
                'iframe[src*="googlesyndication"]',
                '[class*="adsbygoogle"]', '[class*="ad-unit"]', '[id*="ad-"]',
                '[class*="banner-ad"]', '[class*="sidebar-ad"]', '.google-auto-placed'
            ];

            let adElements = [];
            if (customSelector) {
                document.querySelectorAll(customSelector).forEach(el => adElements.push(el));
            } else {
                const set = new Set();
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => set.add(el));
                });
                adElements = [...set];
            }

            // 标准广告尺寸列表（IAB）
            const standardSizes = [
                [300,250],[728,90],[160,600],[320,50],[320,100],[336,280],
                [970,250],[970,90],[300,600],[300,50],[250,250],[200,200],
                [468,60],[120,600],[120,240],[300,100],[728,250],[970,150]
            ];
            const tolerance = 5;

            function isStandardSize(w, h) {
                return standardSizes.some(([sw, sh]) =>
                    Math.abs(w - sw) <= tolerance && Math.abs(h - sh) <= tolerance
                );
            }

            const adList = adElements.map((el, idx) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const viewH = window.innerHeight;
                const viewW = window.innerWidth;

                const overlapTop = Math.max(0, rect.top);
                const overlapBottom = Math.min(viewH, rect.bottom);
                const overlapLeft = Math.max(0, rect.left);
                const overlapRight = Math.min(viewW, rect.right);
                const overlapArea = Math.max(0, overlapBottom - overlapTop) * Math.max(0, overlapRight - overlapLeft);
                const totalArea = rect.width * rect.height;
                const visibleRate = totalArea > 0 ? overlapArea / totalArea : 0;

                const isHidden = style.display === 'none'
                    || style.visibility === 'hidden'
                    || parseFloat(style.opacity) === 0
                    || rect.width === 0
                    || rect.height === 0;

                // CSS变形检测
                const transform = style.transform || style.webkitTransform || 'none';
                const isDistorted = transform !== 'none' && transform !== '';
                const hasSkew = /skew/i.test(transform);
                const hasScale = /scale\\([^1]/i.test(transform);
                const hasRotate = /rotate\\([^0]/i.test(transform);

                // 广告尺寸合规性
                const w = Math.round(rect.width);
                const h = Math.round(rect.height);
                const isStdSize = isStandardSize(w, h);

                // 检查广告是否与内容重叠（简单检测：位置是否在主要内容区域）
                const isOverlappingContent = rect.top < 0 || (rect.top > viewH * 0.9);

                return {
                    index: idx,
                    tag: el.tagName,
                    className: el.className.substring(0, 60),
                    width: w,
                    height: h,
                    visible_rate: Math.round(visibleRate * 100) + '%',
                    is_valid_expose: visibleRate >= 0.5,
                    is_css_hidden: isHidden,
                    is_standard_size: isStdSize || w === 0 || h === 0,
                    is_css_distorted: isDistorted && (hasSkew || hasScale || hasRotate),
                    transform_value: transform.substring(0, 80),
                    is_overlapping: isOverlappingContent,
                };
            });

            const total = adList.length;
            const validExpose = adList.filter(a => a.is_valid_expose).length;
            const hiddenAds = adList.filter(a => a.is_css_hidden).length;
            const nonStandard = adList.filter(a => !a.is_standard_size).length;
            const distorted = adList.filter(a => a.is_css_distorted).length;
            const overlapping = adList.filter(a => a.is_overlapping).length;

            // 页面广告密度（每屏广告数量）
            const adsPerViewport = total > 0 ? (total / Math.max(1, Math.floor(document.body.scrollHeight / viewH))) : 0;

            return {
                detect_mode: customSelector ? '指定选择器' : '自动识别扫描',
                total_ad_count: total,
                valid_expose_count: validExpose,
                hidden_ad_count: hiddenAds,
                non_standard_size_count: nonStandard,
                css_distorted_count: distorted,
                overlapping_count: overlapping,
                ads_per_viewport: Math.round(adsPerViewport * 10) / 10,
                ad_list: adList
            };
        }
    """, ad_selector)

    if ad_result is None:
        ad_result = {
            "detect_mode": "自动识别扫描", "total_ad_count": 0,
            "valid_expose_count": 0, "hidden_ad_count": 0,
            "non_standard_size_count": 0, "css_distorted_count": 0,
            "overlapping_count": 0, "ads_per_viewport": 0, "ad_list": []
        }
    ad_result = {
        "detect_mode": ad_result.get("detect_mode", "自动识别扫描"),
        "total_ad_count": ad_result.get("total_ad_count") or 0,
        "valid_expose_count": ad_result.get("valid_expose_count") or 0,
        "hidden_ad_count": ad_result.get("hidden_ad_count") or 0,
        "non_standard_size_count": ad_result.get("non_standard_size_count") or 0,
        "css_distorted_count": ad_result.get("css_distorted_count") or 0,
        "overlapping_count": ad_result.get("overlapping_count") or 0,
        "ads_per_viewport": ad_result.get("ads_per_viewport") or 0,
        "ad_list": ad_result.get("ad_list", [])
    }
    report["ad_risk"] = ad_result

    # ====================== 7.5 AdSense 合规自检（站点级） ======================
    adsense_compliance = page.evaluate("""
        (() => {
            const result = {
                ads_txt_accessible: false,
                has_ad_click_encouragement: false,
                above_fold_ad_count: 0,
                total_ads_on_page: 0,
                ad_to_content_ratio: 0,
                has_privacy_policy_link: false,
                has_cookie_consent: false,
            };
            // 1. 检测页面是否有诱导点击广告的文案（违反 AdSense 政策）
            const bodyText = (document.body.innerText || '').toLowerCase();
            const clickBaitPhrases = [
                'click here', 'click the ad', '点击广告', '点这里',
                'please click', 'support us by clicking', 'click below'
            ];
            result.has_ad_click_encouragement = clickBaitPhrases.some(p => bodyText.includes(p));
            // 2. 首屏广告数量（AdSense 建议首屏不超过 3 个）
            const viewH = window.innerHeight;
            const allAds = document.querySelectorAll(
                'ins.adsbygoogle, [data-ad-client], iframe[src*="googlesyndication"], iframe[src*="doubleclick"]'
            );
            result.total_ads_on_page = allAds.length;
            allAds.forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.top < viewH && rect.bottom > 0 && rect.width > 0) {
                    result.above_fold_ad_count++;
                }
            });
            // 3. 广告/内容比例（广告面积占页面总面积）
            let adArea = 0;
            allAds.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) adArea += r.width * r.height;
            });
            const pageArea = Math.max(1, document.documentElement.scrollHeight * window.innerWidth);
            result.ad_to_content_ratio = Math.round((adArea / pageArea) * 100) / 100;
            // 4. 隐私政策链接（AdSense 要求必须有）
            const links = Array.from(document.querySelectorAll('a[href]'));
            result.has_privacy_policy_link = links.some(a => {
                const href = (a.href || '').toLowerCase();
                const text = (a.textContent || '').toLowerCase();
                return href.includes('privacy') || text.includes('privacy') ||
                       text.includes('隐私') || href.includes('隐私');
            });
            // 5. Cookie 同意横幅（GDPR 合规）
            result.has_cookie_consent = !!(
                document.querySelector('[class*="cookie"], [id*="cookie"], [class*="consent"], [id*="consent"]')
            );
            return result;
        })()
    """) or {}
    report["adsense_compliance"] = adsense_compliance

    # ====================== 8. 行为模式分析 ======================
    behavior = {}

    # 页面加载时间检测
    perf_data = page.evaluate("""
        (() => {
            try {
                const perf = performance.getEntriesByType('navigation')[0];
                if (!perf) return {available: false};
                return {
                    available: true,
                    domContentLoaded: Math.round(perf.domContentLoadedEventEnd),
                    loadComplete: Math.round(perf.loadEventEnd),
                    domInteractive: Math.round(perf.domInteractive),
                    responseEnd: Math.round(perf.responseEnd),
                    transferSize: perf.transferSize || 0,
                    // 页面加载时间是否异常短（<200ms才是真正异常，正常快速连接<2s是合理的）
                    load_too_fast: perf.loadEventEnd > 0 && perf.loadEventEnd < 200,
                    // 页面加载时间是否异常长（>15秒影响体验）
                    load_too_slow: perf.loadEventEnd > 15000,
                };
            } catch(e) { return {available: false, error: e.message}; }
        })()
    """) or {"available": False}
    behavior["page_load_perf"] = perf_data

    # 页面滚动行为分析
    scroll_info = page.evaluate("""
        (() => {
            const docH = document.documentElement.scrollHeight;
            const viewH = window.innerHeight;
            const scrollTop = window.scrollY || document.documentElement.scrollTop;
            const scrollRatio = docH > viewH ? scrollTop / (docH - viewH) : 0;
            return {
                document_height: docH,
                viewport_height: viewH,
                current_scroll_top: scrollTop,
                scroll_ratio: Math.round(scrollRatio * 100) / 100,
                // 页面是否有足够内容（高度>视口2倍表示有实质内容）
                has_substantial_content: docH > viewH * 2,
                // 是否完全未滚动（scrollTop=0 且页面高度>视口）
                never_scrolled: scrollTop === 0 && docH > viewH * 1.5,
            };
        })()
    """) or {}
    behavior["scroll_info"] = scroll_info

    # 用户交互事件统计（通过performance API间接检测）
    behavior["interaction_events"] = page.evaluate("""
        (() => {
            try {
                // 通过performance observer获取交互数据
                const entries = performance.getEntriesByType('navigation');
                const nav = entries[0];
                return {
                    has_navigation_timing: !!nav,
                    // 检测是否有用户交互标记
                    has_user_activation: typeof navigator.userActivation !== 'undefined',
                    user_activation_active: navigator.userActivation ? navigator.userActivation.isActive : false,
                };
            } catch(e) { return {error: e.message}; }
        })()
    """) or {}

    report["behavior_pattern"] = behavior

    # ====================== 9. 会话存储随机化校验 ======================
    storage = {}
    cookie_count = page.evaluate("document.cookie.split(';').length")
    storage["cookie_count"] = cookie_count if cookie_count is not None else 0
    ls_item_count = page.evaluate("Object.keys(localStorage).length")
    storage["ls_item_count"] = ls_item_count if ls_item_count is not None else 0
    storage["storage_unrandomized"] = storage["ls_item_count"] < 3

    # 检查cookie是否有合理的过期时间分布
    storage["cookie_analysis"] = page.evaluate("""
        (() => {
            const cookies = document.cookie.split(';').map(c => c.trim());
            const adCookies = cookies.filter(c => {
                const name = c.split('=')[0];
                return name.startsWith('_ga') || name.startsWith('_gid') || name.startsWith('NID')
                    || name.startsWith('IDE') || name.startsWith('DSID');
            });
            return {
                total_cookies: cookies.length,
                ad_related_cookies: adCookies.length,
                has_google_cookies: cookies.some(c => c.startsWith('_ga=') || c.startsWith('NID=')),
                has_analytics: cookies.some(c => c.startsWith('_ga=') || c.startsWith('_gid=')),
            };
        })()
    """) or {}

    report["session_storage"] = storage

    # ====================== 10. 风险加权计算 ======================
    score = 0
    risk_detail = []

    # --- 自动化高危项 ---
    if auto_probe.get("nav_webdriver"):
        score += RISK_WEIGHT["webdriver_leak"]
        risk_detail.append("🔴 WebDriver标识泄露（navigator.webdriver）")
    if auto_probe.get("cdc_trace"):
        score += RISK_WEIGHT["cdc_trace_leak"]
        risk_detail.append("🔴 cdc驱动残留特征")
    pw_residuals = auto_probe.get("playwright_residuals", [])
    if pw_residuals:
        score += RISK_WEIGHT["playwright_residual"]
        risk_detail.append(f"🔴 Playwright/Selenium残留: {pw_residuals}")

    # --- 自动化深度检测 ---
    hook_results = auto_deep.get("hook_toString_check", {})
    tostring_leaks = [name for name, info in hook_results.items()
                      if isinstance(info, dict) and info.get("suspicious")]
    if tostring_leaks:
        score += RISK_WEIGHT["tostring_leak"]
        risk_detail.append(f"🟠 函数toString泄漏（非native）: {tostring_leaks}")

    if auto_deep.get("permissions_query_native", {}).get("suspicious"):
        score += RISK_WEIGHT["function_consistency_fail"]
        risk_detail.append("🟠 permissions.query被覆盖（CDP特征）")

    # --- 指纹类 ---
    # 噪声是否存在以行为一致性判定（raw!==raw2 说明有扰动），而非 toString（会被原生伪装欺骗）
    canvas_noise_present = not canvas_data.get("consistent", True)
    canvas_hook_detectable = canvas_data.get("has_noise_hook", False)
    if not canvas_noise_present:
        score += RISK_WEIGHT["canvas_finger_no_noise"]
        risk_detail.append("🟠 Canvas指纹无噪声扰动")
    elif canvas_hook_detectable:
        score += RISK_WEIGHT["canvas_hook_detectable"]
        risk_detail.append("🟡 Canvas噪声hook可被检测（toString非native）")
    # WebGL 是否伪装以 renderer 是否为 headless/SwiftShader 判定，而非 toString
    if webgl_info.get("renderer_suspicious"):
        score += RISK_WEIGHT["webgl_finger_no_noise"]
        risk_detail.append("🟠 WebGL指纹未伪装（headless/SwiftShader渲染器）")
    elif webgl_info.get("has_hook"):
        score += RISK_WEIGHT["webgl_hook_detectable"]
        risk_detail.append("🟡 WebGL hook可被检测（getParameter toString非native）")

    # --- 设备一致性 ---
    dev_con = device_cons.get("screen_vs_viewport", {})
    if dev_con.get("viewport_larger_than_screen"):
        score += RISK_WEIGHT["screen_resolution_mismatch"]
        risk_detail.append("🟠 视口大于屏幕分辨率（异常）")

    hw_info = device_cons.get("hardware_info", {})
    if hw_info.get("hc_suspicious"):
        score += RISK_WEIGHT["hardware_info_abnormal"]
        risk_detail.append(f"🟡 hardwareConcurrency异常: {hw_info.get('hardwareConcurrency')}")

    if not finger.get("battery_api_exist"):
        score += RISK_WEIGHT["battery_api_missing"]
        risk_detail.append("🟢 电池API不可用（自动化浏览器常见）")

    # --- 时区/地理 ---
    if not tz_info.get("tz_match"):
        score += RISK_WEIGHT["tz_geo_mismatch"]
        risk_detail.append("🟠 浏览器时区与IP地域不匹配")
    if not tz_info.get("is_common_timezone"):
        score += RISK_WEIGHT["timezone_not_common"]
        risk_detail.append(f"🟡 使用罕见时区: {browser_tz}")

    # --- 网络/WebRTC ---
    if net.get("webrtc_leak_ip"):
        score += RISK_WEIGHT["webrtc_ip_leak"]
        risk_detail.append("🔴 WebRTC内网IP泄露")
    if net.get("webrtc_completely_disabled"):
        risk_detail.append("🟡 WebRTC被完全删除（过度禁用也可被检测）")
    if not net.get("has_valid_referer"):
        score += RISK_WEIGHT["empty_referer"]
        risk_detail.append("🟠 空Referer来路")

    # --- HTTP请求头 ---
    if ua_risk.get("ua_risk_flag"):
        score += RISK_WEIGHT["ua_risk_keyword"]
        risk_detail.append(f"🔴 UA包含自动化关键词：{hit_risk_words}")
    if ua_risk.get("fixed_chrome_version"):
        score += RISK_WEIGHT["ua_version_fixed"]
        risk_detail.append("🟠 UA使用固定Chrome120版本，多样性不足")

    # 请求头完整性
    if header_deep.get("missing_count", 0) > 3:
        score += RISK_WEIGHT["missing_standard_header"]
        risk_detail.append(f"🟠 缺失{header_deep['missing_count']}个标准请求头: {header_deep['missing_headers'][:5]}")

    # UA与Sec-CH-UA版本一致性
    if not header_deep.get("ua_sec_ch_ua_version_match", True):
        score += RISK_WEIGHT["header_ua_sec_ch_ua_mismatch"]
        risk_detail.append("🔴 UA中Chrome版本与Sec-CH-UA不一致")

    # 平台一致性
    if not header_deep.get("platform_consistent", True):
        score += RISK_WEIGHT["header_ua_platform_mismatch"]
        risk_detail.append(f"🔴 UA平台({header_deep.get('ua_declared_platform')})与Sec-CH-UA-Platform({header_deep.get('sec_ch_ua_platform')})不一致")

    # Accept头异常
    if header_deep.get("accept_abnormal"):
        score += RISK_WEIGHT["header_accept_abnormal"]
        risk_detail.append("🟠 Accept头缺少text/html")

    # Accept-Language与navigator.language一致性
    if not header_deep.get("accept_lang_matches_navigator", True):
        score += RISK_WEIGHT["header_accept_lang_geo_mismatch"]
        risk_detail.append("🟠 Accept-Language与navigator.language不一致")

    # Sec-Fetch缺失
    if not header_deep.get("sec_fetch_complete", True):
        score += RISK_WEIGHT["header_sec_fetch_missing"]
        risk_detail.append("🟡 Sec-Fetch-*系列请求头不完整")

    # 可疑额外请求头
    susp_headers = header_deep.get("suspicious_extra_headers", [])
    if susp_headers:
        score += RISK_WEIGHT["header_extra_suspicious"]
        risk_detail.append(f"🔴 发现可疑自动化请求头: {susp_headers}")

    # --- 插件 ---
    if auto_probe.get("plugins_empty"):
        score += RISK_WEIGHT["plugin_fake_abnormal"]
        risk_detail.append("🟠 浏览器插件列表为空")

    # --- 存储 ---
    if storage.get("storage_unrandomized"):
        score += RISK_WEIGHT["storage_unrandomized"]
        risk_detail.append("🟢 Cookie/LocalStorage未随机化")

    # --- 广告专属风险（多广告位加权） ---
    total_ad_count = ad_result.get("total_ad_count") or 0
    valid_expose_count = ad_result.get("valid_expose_count") or 0
    hidden_ad_count = ad_result.get("hidden_ad_count") or 0
    non_standard_count = ad_result.get("non_standard_size_count") or 0
    distorted_count = ad_result.get("css_distorted_count") or 0
    overlapping_count = ad_result.get("overlapping_count") or 0
    ads_per_viewport = ad_result.get("ads_per_viewport") or 0

    if total_ad_count > 0:
        invalid_count = total_ad_count - valid_expose_count
        if invalid_count > 0:
            score += invalid_count * RISK_WEIGHT["ad_no_valid_expose_single"]
            risk_detail.append(f"🟠 {invalid_count} 个广告位未达到50%有效曝光阈值")
        if hidden_ad_count > 0:
            score += hidden_ad_count * RISK_WEIGHT["ad_css_hidden_single"]
            risk_detail.append(f"🔴 {hidden_ad_count} 个广告位被CSS隐藏，作弊特征")
        if non_standard_count > 0:
            score += non_standard_count * RISK_WEIGHT["ad_non_standard_size"]
            risk_detail.append(f"🟠 {non_standard_count} 个广告位非IAB标准尺寸")
        if distorted_count > 0:
            score += distorted_count * RISK_WEIGHT["ad_css_distorted"]
            risk_detail.append(f"🔴 {distorted_count} 个广告位被CSS变形（skew/scale/rotate）")
        if overlapping_count > 0:
            score += overlapping_count * RISK_WEIGHT["ad_overlap_content"]
            risk_detail.append(f"🟠 {overlapping_count} 个广告位与内容区域异常重叠")
        if ads_per_viewport > 3:
            score += RISK_WEIGHT["ad_too_many_on_page"]
            risk_detail.append(f"🟠 广告密度过高: {ads_per_viewport}个/屏，疑似广告堆砌")

    # --- 行为模式风险 ---
    load_perf = behavior.get("page_load_perf", {})
    if load_perf.get("load_too_fast"):
        score += RISK_WEIGHT["page_stay_too_short"]
        risk_detail.append("🟠 页面加载时间<2秒（自动化特征）")

    scroll_info_data = behavior.get("scroll_info", {})
    if scroll_info_data.get("never_scrolled"):
        score += RISK_WEIGHT["scroll_pattern_abnormal"]
        risk_detail.append("🟠 页面有内容但完全未滚动（非真实用户行为）")

    interaction = behavior.get("interaction_events", {})
    if not interaction.get("has_user_activation", True):
        score += RISK_WEIGHT["no_interaction_at_all"]
        risk_detail.append("🟠 无任何用户交互标记（自动化特征）")

    # --- AdSense 合规风险（站点级） ---
    compliance = report.get("adsense_compliance", {})
    if compliance.get("has_ad_click_encouragement"):
        score += RISK_WEIGHT["adsense_click_encouragement"]
        risk_detail.append("🔴 页面含诱导点击广告文案（严重违反 AdSense 政策）")
    if (compliance.get("above_fold_ad_count") or 0) > 3:
        score += RISK_WEIGHT["adsense_above_fold_overload"]
        risk_detail.append(f"🟠 首屏广告过多: {compliance.get('above_fold_ad_count')}个（建议≤3）")
    if (compliance.get("ad_to_content_ratio") or 0) > 0.3:
        score += RISK_WEIGHT["adsense_ad_ratio_high"]
        risk_detail.append(f"🟠 广告/内容比例过高: {compliance.get('ad_to_content_ratio')}")
    if not compliance.get("has_privacy_policy_link", True):
        score += RISK_WEIGHT["adsense_no_privacy_policy"]
        risk_detail.append("🟡 缺少隐私政策链接（AdSense 合规要求）")
    if not compliance.get("has_cookie_consent", True):
        score += RISK_WEIGHT["adsense_no_cookie_consent"]
        risk_detail.append("🟡 缺少 Cookie 同意横幅（GDPR 合规）")

    # 风险等级
    if score >= 70:
        level = "🔴 高危（极易封号/无效流量标记）"
    elif score >= 35:
        level = "🟠 中危（存在明显机器人特征，复审概率高）"
    elif score >= 15:
        level = "🟡 低危（基础伪装合格，存在少量风险点）"
    else:
        level = "🟢 安全（风控伪装良好）"

    report["risk_calc"]["total_score"] = score
    report["risk_calc"]["risk_level"] = level
    report["risk_calc"]["risk_reason_list"] = risk_detail
    report["risk_calc"]["detection_dimensions"] = {
        "自动化检测": "✅" if not (auto_probe.get("nav_webdriver") or pw_residuals) else "❌",
        "指纹伪装": "✅" if (canvas_data.get("has_noise_hook") and webgl_info.get("has_hook") and finger.get("battery_api_exist")) else "⚠️",
        "HTTP请求头": "✅" if header_deep.get("completeness_pct", 0) >= 90 and header_deep.get("ua_sec_ch_ua_version_match") else "⚠️",
        "设备一致性": "✅" if not dev_con.get("viewport_larger_than_screen") and hw_info.get("hc_reasonable") else "⚠️",
        "时区/地理": "✅" if tz_info.get("tz_match") else "❌",
        "广告合规": "✅" if hidden_ad_count == 0 and distorted_count == 0 else "⚠️",
        "AdSense合规": "✅" if not compliance.get("has_ad_click_encouragement") and (compliance.get("above_fold_ad_count", 0) <= 3) else "⚠️",
        "行为模式": "✅" if not scroll_info_data.get("never_scrolled") else "⚠️",
        "网络/IP": "✅" if not net.get("webrtc_leak_ip") else "❌",
    }

    return report


# ============================================================
# 攻防演练运行器
# ============================================================

_STEALTH_INIT_SCRIPT = r"""
(function() {
    // ===== 中心 toString 原生伪装：防御 Function.prototype.toString.call(fn) 检测（CreepJS/AdSense 核心检测点）=====
    const _nativeMask = new Map();
    try {
        const _origToString = Function.prototype.toString;
        const _maskedToString = function toString() {
            if (_nativeMask.has(this)) return _nativeMask.get(this);
            return _origToString.call(this);
        };
        _nativeMask.set(_maskedToString, 'function toString() { [native code] }');
        Function.prototype.toString = _maskedToString;
    } catch(e) {}
    // 注册被hook函数为“原生样”，使其 toString 返回 native code 格式
    const _maskNative = function(fn, name) {
        try { _nativeMask.set(fn, 'function ' + name + '() { [native code] }'); } catch(e) {}
    };
    try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true}); } catch(e) {}
    // 清除 cdc_ / 自动化残留
    try {
        for (const k of Object.getOwnPropertyNames(window)) {
            if (k.indexOf('cdc_') === 0 || k.indexOf('$cdc_') === 0) { try { delete window[k]; } catch(e) {} }
        }
        delete window.__webdriver_evaluate; delete window.__selenium_unwrapped; delete window.__playwright;
        delete window.__pw_manual__; delete window.__PW_inspect;
        delete window.__driver_evaluate; delete window.__webdriver_script_fn;
        delete window.__fxdriver_evaluate; delete window.__driver_unwrapped;
        delete window._Selenium_IDE_Recorder; delete window.callSelenium;
    } catch(e) {}
    // Canvas 噪声 hook（使用toString保护）
    (function() {
        let _s = (Math.floor(Math.random()*2147483647)) >>> 0 || 1;
        const _rnd = function(){ _s = (_s*1103515245+12345)&0x7fffffff; return _s/0x7fffffff; };
        const _orig = CanvasRenderingContext2D.prototype.getImageData;
        const _hooked = function() {
            const d = _orig.apply(this, arguments);
            try { const a=d.data; for (let i=0;i<a.length;i+=4){ if(_rnd()<0.02){ const n=(_rnd()*3|0)-1; a[i]=Math.max(0,Math.min(255,a[i]+n)); a[i+1]=Math.max(0,Math.min(255,a[i+1]+n)); a[i+2]=Math.max(0,Math.min(255,a[i+2]+n)); } } } catch(e) {}
            return d;
        };
        // 注册到中心 toString 伪装（同时防御 fn.toString() 与 Function.prototype.toString.call(fn)）
        _maskNative(_hooked, 'getImageData');
        CanvasRenderingContext2D.prototype.getImageData = _hooked;
    })();
    // WebGL hook（同样保护toString）- GPU指纹从真实设备池随机选择
    (function() {
        // 真实 GPU 设备池（避免固定值被 AdSense 收录为指纹）
        const _gpuPool = [
            {vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (AMD)', renderer: 'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
            {vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)'},
            {vendor: 'Google Inc. (Apple)', renderer: 'ANGLE (Apple, Apple M2, OpenGL 4.1)'},
        ];
        const _gpu = _gpuPool[Math.floor(Math.random() * _gpuPool.length)];
        const patch = function(proto){
            if(!proto||!proto.getParameter) return;
            const o=proto.getParameter;
            const hooked = function(p){
                if(p===37445) return _gpu.vendor;
                if(p===37446) return _gpu.renderer;
                return o.call(this,p);
            };
            _maskNative(hooked, 'getParameter');
            proto.getParameter = hooked;
        };
        try { patch(WebGLRenderingContext.prototype); } catch(e) {}
        try { patch(WebGL2RenderingContext.prototype); } catch(e) {}
    })();
    // 屏幕尺寸伪造（headless 默认 800x600 小于视口，视口>屏幕是典型机器人指纹）
    try {
        const _sw = 1920, _sh = 1080;
        Object.defineProperty(screen, 'width', {get: () => _sw, configurable: true});
        Object.defineProperty(screen, 'height', {get: () => _sh, configurable: true});
        Object.defineProperty(screen, 'availWidth', {get: () => _sw, configurable: true});
        Object.defineProperty(screen, 'availHeight', {get: () => (_sh - 40), configurable: true});
        Object.defineProperty(screen, 'colorDepth', {get: () => 24, configurable: true});
    } catch(e) {}
    // plugins 伪造
    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', { get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}], configurable: true });
        }
    } catch(e) {}
    // mediaDevices 伪造枚举
    try {
        if (navigator.mediaDevices) {
            const _enumDev = function enumerateDevices() {
                return Promise.resolve([{deviceId:'default',kind:'audioinput',label:'',groupId:'g1'},{deviceId:'cam01',kind:'videoinput',label:'',groupId:'g2'}]);
            };
            _maskNative(_enumDev, 'enumerateDevices');
            navigator.mediaDevices.enumerateDevices = _enumDev;
        }
    } catch(e) {}
    // Battery API 注入（自动化浏览器默认禁用，必须mock）
    try {
        if (!('getBattery' in navigator)) {
            const _batLevel = 0.35 + Math.random() * 0.65; // 0.35~1.0
            const _batCharging = Math.random() > 0.5;
            const _batMock = {
                charging: _batCharging,
                chargingTime: _batCharging ? 0 : Infinity,
                dischargingTime: _batCharging ? Infinity : 3600 + Math.random() * 7200,
                level: Math.round(_batLevel * 100) / 100,
                onchargingchange: null,
                onchargingtimechange: null,
                ondischargingtimechange: null,
                onlevelchange: null,
                addEventListener: function(){},
                removeEventListener: function(){},
                dispatchEvent: function(){ return true; }
            };
            const _getBattery = function() { return Promise.resolve(_batMock); };
            _maskNative(_getBattery, 'getBattery');
            Object.defineProperty(navigator, 'getBattery', {get: () => _getBattery, configurable: true});
        }
    } catch(e) {}
    // localStorage 随机化（无条件注入，确保至少3项）
    try {
        if (!localStorage.getItem('_app_cid')) { localStorage.setItem('_app_cid', String(Math.floor(Math.random()*1e10))+'.'+String(Math.floor(Math.random()*1e10))); }
        if (!localStorage.getItem('_app_pref')) { localStorage.setItem('_app_pref', 'theme=light'); }
        if (!localStorage.getItem('_app_sid')) { localStorage.setItem('_app_sid', Math.random().toString(36).slice(2)); }
        // 额外cookie随机化标记（模拟真实用户cookie行为）
        if (document.cookie.indexOf('_sess_') === -1) {
            document.cookie = '_sess_=' + Math.random().toString(36).slice(2,10) + '; path=/; max-age=3600';
        }
    } catch(e) {}
    // permissions.query 保护（减少CDP检测面）
    try {
        const origQuery = navigator.permissions.query;
        const _hookedQuery = function query(params) {
            if (params && params.name === 'notifications') {
                return Promise.resolve({state: Notification.permission});
            }
            return origQuery.call(this, params);
        };
        _maskNative(_hookedQuery, 'query');
        navigator.permissions.query = _hookedQuery;
    } catch(e) {}
})();
"""


def _noop_log(msg):
    print(msg)


def _noop_progress(pct, stage):
    pass


def run_drill(target_url, headless=True, log_fn=None, progress_fn=None, with_stealth=True):
    """执行一次攻防演练：启动浏览器(默认带反检测注入)->打开目标站->风控探测->保存报告。

    返回: (report_dict, json_path, html_path)
    """
    import json
    import time
    from datetime import datetime

    log_fn = log_fn or _noop_log
    progress_fn = progress_fn or _noop_progress

    from selenium_bridge import sync_playwright

    progress_fn(5, "初始化演练环境")
    log_fn(f"🛡️ 攻防演练启动 | 目标={target_url} | 反检测注入={'开启' if with_stealth else '关闭(裸浏览器基线)'}")

    launch_args = [
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    report = None
    json_path = html_path = None
    browser = None
    try:
        with sync_playwright() as p:
            progress_fn(15, "启动浏览器")
            try:
                browser = p.chromium.launch(channel="chrome", headless=headless, args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=headless, args=launch_args)
            log_fn("✅ 浏览器已启动")

            progress_fn(30, "创建上下文")
            custom_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            context = browser.new_context(
                locale=LOCALE,
                timezone_id=TIMEZONE,
                user_agent=custom_ua,
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": f"{LOCALE},{LOCALE.split('-')[0]};q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="149", "Google Chrome";v="149"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Linux"',
                    "Referer": "https://www.google.com/",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            if with_stealth:
                progress_fn(40, "注入反检测脚本")
                context.add_init_script(_STEALTH_INIT_SCRIPT)
                log_fn("✅ 反检测注入脚本已加载")

            page = context.new_page()
            progress_fn(55, "打开目标页面")
            log_fn(f"🌐 正在打开目标页面: {target_url}")
            try:
                page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log_fn(f"⚠️ 页面加载异常(继续探测): {type(e).__name__}: {str(e)[:80]}")
            time.sleep(3)

            # 模拟真人浏览行为（滚动、鼠标移动），避免行为模式扣分
            progress_fn(60, "模拟真人浏览行为")
            try:
                import random as _rnd
                # 缓慢滚动页面（模拟阅读）
                for _i in range(3):
                    scroll_y = _rnd.randint(200, 500)
                    page.mouse.wheel(0, scroll_y)
                    time.sleep(_rnd.uniform(0.8, 1.5))
                # 鼠标随机移动
                page.mouse.move(_rnd.randint(200, 800), _rnd.randint(200, 600))
                time.sleep(0.5)
                log_fn("✅ 已模拟真人滚动/鼠标行为")
            except Exception as _e:
                log_fn(f"⚠️ 模拟行为异常(忽略): {_e}")

            progress_fn(70, "执行风控探测")
            log_fn("🔍 正在执行风控漏洞探测（v2.0 全维度）...")
            report = run_risk_detect(page, proxy_ip=None, ad_selector=None)
            report["meta"] = {
                "target_url": target_url,
                "with_stealth": with_stealth,
                "headless": headless,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "version": "2.0",
            }

            progress_fn(85, "生成演练报告")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            json_path = os.path.join(REPORT_DIR, f"drill_report_{ts}.json")
            html_path = os.path.join(REPORT_DIR, f"drill_report_{ts}.html")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(_render_report_html(report))

            calc = report.get("risk_calc", {})
            log_fn(f"📊 演练完成 | 风险分={calc.get('total_score')} | 等级={calc.get('risk_level')}")
            for reason in calc.get("risk_reason_list", []):
                log_fn(f"   • {reason}")
            log_fn(f"📄 报告已保存: {json_path}")
            progress_fn(100, "演练完成")
    except Exception as e:
        log_fn(f"❌ 演练异常: {type(e).__name__}: {str(e)[:160]}")
        progress_fn(100, "演练异常结束")
        raise
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
    return report, json_path, html_path


def _render_report_html(report):
    """把风控报告渲染成可读 HTML。"""
    import html as _html
    import json as _json

    calc = report.get("risk_calc", {})
    meta = report.get("meta", {})
    score = calc.get("total_score", 0)
    level = calc.get("risk_level", "")
    color = "#16a34a" if score < 15 else ("#f59e0b" if score < 35 else ("#f97316" if score < 70 else "#dc2626"))
    reasons = calc.get("risk_reason_list", [])
    reason_html = "".join(f"<li>{_html.escape(str(r))}</li>" for r in reasons) or "<li>无明显风险项</li>"

    # 检测维度仪表盘
    dimensions = calc.get("detection_dimensions", {})
    dim_html = "".join(
        f'<span style="display:inline-block;margin:4px 8px;padding:4px 10px;border-radius:6px;'
        f'background:{"#065f46" if v=="✅" else ("#92400e" if v=="⚠️" else "#991b1b")};font-size:13px;">'
        f'{_html.escape(k)} {v}</span>'
        for k, v in dimensions.items()
    )

    def _section(title, data):
        body = _html.escape(_json.dumps(data, ensure_ascii=False, indent=2))
        return f"<h3>{_html.escape(title)}</h3><pre>{body}</pre>"

    sections = "".join([
        _section("基础环境", report.get("base_info", {})),
        _section("自动化探针", report.get("automation_probe", {})),
        _section("自动化深度检测", report.get("automation_deep", {})),
        _section("指纹检测", report.get("fingerprint", {})),
        _section("设备一致性", report.get("device_consistency", {})),
        _section("时区/地理", report.get("timezone_geo", {})),
        _section("网络/WebRTC", report.get("network_ip", {})),
        _section("HTTP请求头深度", report.get("http_header_deep", {})),
        _section("广告风控", report.get("ad_risk", {})),
        _section("AdSense合规", report.get("adsense_compliance", {})),
        _section("行为模式", report.get("behavior_pattern", {})),
        _section("会话存储", report.get("session_storage", {})),
    ])
    version = meta.get("version", "1.0")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>攻防演练报告 v{version}</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;padding:24px;}}
.card{{background:#1e293b;border-radius:10px;padding:20px;margin-bottom:16px;}}
.score{{font-size:42px;font-weight:bold;color:{color};}}
h1{{color:#60a5fa;}} h3{{color:#93c5fd;border-bottom:1px solid #334155;padding-bottom:6px;}}
pre{{background:#0f172a;padding:12px;border-radius:6px;overflow:auto;font-size:12px;}}
ul{{line-height:1.8;}} .meta{{color:#94a3b8;font-size:13px;}}
.dim-bar{{margin:12px 0;}}
</style></head><body>
<h1>🛡️ 攻防演练报告 v{version}</h1>
<div class="card">
  <div class="meta">目标站: {_html.escape(str(meta.get('target_url','')))} ｜ 反检测注入: {'开启' if meta.get('with_stealth') else '关闭(裸基线)'} ｜ 时间: {_html.escape(str(meta.get('time','')))}</div>
  <div class="score">{score} 分</div>
  <div style="font-size:18px;margin-top:6px;">{_html.escape(str(level))}</div>
  <div class="dim-bar"><strong>检测维度：</strong>{dim_html}</div>
  <h3>风险命中项</h3><ul>{reason_html}</ul>
</div>
<div class="card">{sections}</div>
</body></html>"""

