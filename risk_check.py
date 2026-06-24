import random
import re
import os

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

# 分布式代理池（没有代理可以留空）
PROXY_POOL = [
    "http://user:pass@ip1:port",
    "http://user:pass@ip2:port"
]

# UA可疑风险关键词库（支持模糊匹配）
UA_RISK_KEYWORDS = [
    "Selenium", "ChromeDriver", "undetected", "Headless", "cdc_",
    "Automation", "Robot", "bot", "Python", "Playwright", "Puppeteer"
]

# 高危风险权重（AdSense封号优先级）
RISK_WEIGHT = {
    "webdriver_leak": 45,
    "cdc_trace_leak": 40,
    "canvas_finger_no_noise": 35,
    "webgl_finger_no_noise": 30,
    "tz_geo_mismatch": 30,
    "webrtc_ip_leak": 28,
    "empty_referer": 20,
    "ua_risk_keyword": 22,
    "ua_version_fixed": 18,
    "missing_standard_header": 15,
    "plugin_fake_abnormal": 12,
    "storage_unrandomized": 10,
    "ad_no_valid_expose_single": 18,  # 单条无效曝光
    "ad_css_hidden_single": 30        # 单条隐藏广告作弊
}


def run_risk_detect(page, proxy_ip, ad_selector=None):
    """
    风控漏洞检测探针
    :param ad_selector: 可选，指定广告CSS选择器；不传则自动扫描全页广告
    """
    report = {
        "base_info": {},
        "automation_probe": {},
        "fingerprint": {},
        "network_ip": {},
        "ad_risk": {},
        "session_storage": {},
        "timezone_geo": {},
        "http_header": {},
        "risk_calc": {}
    }

    # ====================== 1. 基础环境信息 ======================
    ua = page.evaluate("navigator.userAgent") or ""
    platform = page.evaluate("navigator.platform") or ""
    lang = page.evaluate("navigator.language") or ""
    screen = page.evaluate("[screen.width, screen.height]") or [0, 0]
    report["base_info"]["ua_full"] = ua
    report["base_info"]["ua_short"] = ua[:100]
    report["base_info"]["platform"] = platform
    report["base_info"]["lang"] = lang
    report["base_info"]["screen_size"] = screen

    # ====================== 2. 自动化探针高危检测 ======================
    auto_probe = {}
    auto_probe["nav_webdriver"] = page.evaluate("navigator.webdriver !== undefined") or False
    auto_probe["cdc_trace"] = page.evaluate("""
        let hasCdc = false;
        for (let key in window) {
            if (key.startsWith('cdc_')) hasCdc = true;
        }
        return hasCdc;
    """) or False
    auto_probe["chrome_runtime_valid"] = page.evaluate("typeof window.chrome?.runtime === 'object'") or False
    plugins_len = page.evaluate("navigator.plugins.length") or 0
    auto_probe["plugins_empty"] = plugins_len == 0
    auto_probe["mime_empty"] = page.evaluate("navigator.mimeTypes.length === 0") or False
    report["automation_probe"] = auto_probe

    # ====================== 3. 指纹检测 Canvas / WebGL ======================
    finger = {}
    canvas_data = page.evaluate("""
        const c = document.createElement('canvas');
        const ctx = c.getContext('2d');
        ctx.fillText('test_finger_2026', 12, 18);
        const raw = ctx.getImageData(0,0,20,20).data.toString();
        const hasNoiseHook = !CanvasRenderingContext2D.prototype.getImageData.toString().includes('native code');
        return {data: raw, has_noise: hasNoiseHook};
    """) or {"data": "", "has_noise": False}
    finger["canvas_raw_hash"] = canvas_data["data"][:150] if canvas_data.get("data") else ""
    finger["canvas_noise_injected"] = canvas_data.get("has_noise", False)

    webgl_info = page.evaluate("""
        const gl = document.createElement('canvas').getContext('webgl');
        if (!gl) return {renderer: null, vendor: null, has_hook: false};
        const hookCheck = gl.getParameter.toString().indexOf('native code') === -1;
        return {
            renderer: gl.getParameter(gl.RENDERER),
            vendor: gl.getParameter(gl.VENDOR),
            has_hook: hookCheck
        }
    """) or {"renderer": null, "vendor": null, "has_hook": False}
    finger["webgl_renderer"] = webgl_info["renderer"]
    finger["webgl_vendor"] = webgl_info["vendor"]
    finger["webgl_noise_injected"] = webgl_info["has_hook"]
    finger["font_count"] = page.evaluate("document.fonts.size") or 0
    finger["media_devices_exist"] = page.evaluate("navigator.mediaDevices !== undefined") or False
    report["fingerprint"] = finger

    # ====================== 4. 时区 & 地理一致性 ======================
    tz_info = {}
    browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or ""
    tz_info["browser_tz"] = browser_tz
    tz_info["config_target_tz"] = TIMEZONE
    tz_info["tz_match"] = browser_tz == TIMEZONE
    report["timezone_geo"] = tz_info

    # ====================== 5. 网络 & WebRTC 泄漏检测 ======================
    net = {}
    net["webrtc_leak_ip"] = page.evaluate("""
        return new Promise(res => {
            try {
                const pc = new RTCPeerConnection({iceServers: []});
                pc.createDataChannel('test');
                pc.createOffer().then(o => pc.setLocalDescription(o));
                pc.onicecandidate = e => {
                    if (e.candidate) res(true);
                    else res(false);
                }
            }catch{res(false)}
        })
    """) or False
    ref_str = page.evaluate("document.referrer") or ""
    net["referer_content"] = ref_str[:120]
    net["has_valid_referer"] = len(ref_str.strip()) > 0
    headers = page.request.headers if hasattr(page, "request") else {}
    net["missing_accept_lang"] = "Accept-Language" not in headers
    net["missing_sec_ch_ua"] = "Sec-Ch-Ua" not in headers
    report["network_ip"] = net

    # ====================== 6. UA 风险检测 ======================
    ua_risk = {}
    hit_risk_words = []
    for kw in UA_RISK_KEYWORDS:
        if re.search(kw, ua, re.IGNORECASE):
            hit_risk_words.append(kw)
    ua_risk["risk_keywords_hit"] = hit_risk_words
    ua_risk["ua_risk_flag"] = len(hit_risk_words) > 0
    ua_risk["fixed_chrome_version"] = re.search(r"Chrome\/120\.0\.0\.0", ua) is not None
    report["http_header"]["ua_check"] = ua_risk
    report["http_header"]["lack_standard_header"] = net["missing_accept_lang"] or net["missing_sec_ch_ua"]

    # ====================== 7. 广告专属风控检测（新增自动识别核心） ======================
    ad_result = page.evaluate("""
        (customSelector) => {
            // 广告识别规则集（按精准度排序，减少误判）
            const selectors = [
                // 1. AdSense 精准专属特征
                'ins.adsbygoogle',
                '[data-ad-client]',
                '[data-ad-slot]',
                // 2. 广告iframe域名特征
                'iframe[src*="googleadservices.com"]',
                'iframe[src*="doubleclick.net"]',
                'iframe[src*="googlesyndication"]',
                // 3. 通用广告类名/ID特征
                '[class*="adsbygoogle"]',
                '[class*="ad-unit"]',
                '[id*="ad-"]',
                '[class*="banner-ad"]',
                '[class*="sidebar-ad"]',
                '.google-auto-placed'
            ];

            let adElements = [];
            // 如果传了自定义选择器，优先用自定义
            if (customSelector) {
                document.querySelectorAll(customSelector).forEach(el => adElements.push(el));
            } else {
                // 自动扫描全页，去重
                const set = new Set();
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => set.add(el));
                });
                adElements = [...set];
            }

            // 逐个检测每个广告位
            const adList = adElements.map((el, idx) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const viewH = window.innerHeight;
                const viewW = window.innerWidth;

                // 计算视口可见比例
                const overlapTop = Math.max(0, rect.top);
                const overlapBottom = Math.min(viewH, rect.bottom);
                const overlapLeft = Math.max(0, rect.left);
                const overlapRight = Math.min(viewW, rect.right);
                const overlapArea = Math.max(0, overlapBottom - overlapTop) * Math.max(0, overlapRight - overlapLeft);
                const totalArea = rect.width * rect.height;
                const visibleRate = totalArea > 0 ? overlapArea / totalArea : 0;

                // 隐藏作弊检测
                const isHidden = style.display === 'none' 
                    || style.visibility === 'hidden' 
                    || parseFloat(style.opacity) === 0
                    || rect.width === 0 
                    || rect.height === 0;

                return {
                    index: idx,
                    tag: el.tagName,
                    className: el.className.substring(0, 60),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    visible_rate: Math.round(visibleRate * 100) + '%',
                    is_valid_expose: visibleRate >= 0.5,
                    is_css_hidden: isHidden
                };
            });

            // 汇总统计
            const total = adList.length;
            const validExpose = adList.filter(a => a.is_valid_expose).length;
            const hiddenAds = adList.filter(a => a.is_css_hidden).length;

            return {
                detect_mode: customSelector ? '指定选择器' : '自动识别扫描',
                total_ad_count: total,
                valid_expose_count: validExpose,
                hidden_ad_count: hiddenAds,
                ad_list: adList
            };
        }
    """, ad_selector)  # 把Python参数传入JS

    # 确保 ad_result 有默认值，避免 NoneType 错误
    if ad_result is None:
        ad_result = {
            "detect_mode": "自动识别扫描",
            "total_ad_count": 0,
            "valid_expose_count": 0,
            "hidden_ad_count": 0,
            "ad_list": []
        }
    # 确保所有必要属性都有默认值
    ad_result = {
        "detect_mode": ad_result.get("detect_mode", "自动识别扫描"),
        "total_ad_count": ad_result.get("total_ad_count") or 0,
        "valid_expose_count": ad_result.get("valid_expose_count") or 0,
        "hidden_ad_count": ad_result.get("hidden_ad_count") or 0,
        "ad_list": ad_result.get("ad_list", [])
    }
    
    report["ad_risk"] = ad_result

    # ====================== 8. 会话存储随机化校验 ======================
    storage = {}
    cookie_count = page.evaluate("document.cookie.split(';').length")
    storage["cookie_count"] = cookie_count if cookie_count is not None else 0
    ls_item_count = page.evaluate("Object.keys(localStorage).length")
    storage["ls_item_count"] = ls_item_count if ls_item_count is not None else 0
    storage["storage_unrandomized"] = storage["ls_item_count"] < 3
    report["session_storage"] = storage

    # ====================== 9. 风险加权计算 ======================
    score = 0
    risk_detail = []

    # 自动化高危项
    if auto_probe.get("nav_webdriver"):
        score += RISK_WEIGHT["webdriver_leak"]
        risk_detail.append("webdriver标识泄露")
    if auto_probe.get("cdc_trace"):
        score += RISK_WEIGHT["cdc_trace_leak"]
        risk_detail.append("cdc驱动残留特征")
    if not canvas_data.get("has_noise"):
        score += RISK_WEIGHT["canvas_finger_no_noise"]
        risk_detail.append("Canvas指纹无噪声扰动")
    if not webgl_info.get("has_hook"):
        score += RISK_WEIGHT["webgl_finger_no_noise"]
        risk_detail.append("WebGL指纹未伪装")
    if not tz_info.get("tz_match"):
        score += RISK_WEIGHT["tz_geo_mismatch"]
        risk_detail.append("浏览器时区与IP地域不匹配")
    if net.get("webrtc_leak_ip"):
        score += RISK_WEIGHT["webrtc_ip_leak"]
        risk_detail.append("WebRTC内网IP泄露")
    if not net.get("has_valid_referer"):
        score += RISK_WEIGHT["empty_referer"]
        risk_detail.append("空Referer来路")
    if ua_risk.get("ua_risk_flag"):
        score += RISK_WEIGHT["ua_risk_keyword"]
        risk_detail.append(f"UA包含自动化关键词：{hit_risk_words}")
    if ua_risk.get("fixed_chrome_version"):
        score += RISK_WEIGHT["ua_version_fixed"]
        risk_detail.append("UA使用固定Chrome120版本，池多样性不足")
    if report["http_header"].get("lack_standard_header"):
        score += RISK_WEIGHT["missing_standard_header"]
        risk_detail.append("缺失Accept-Language/Sec-Ch-Ua请求头")
    if auto_probe.get("plugins_empty"):
        score += RISK_WEIGHT["plugin_fake_abnormal"]
        risk_detail.append("浏览器插件列表为空，伪造特征")
    if storage.get("storage_unrandomized"):
        score += RISK_WEIGHT["storage_unrandomized"]
        risk_detail.append("Cookie/LocalStorage未随机化")

    # 广告专属风险（多广告位加权）
    total_ad_count = ad_result.get("total_ad_count") or 0
    valid_expose_count = ad_result.get("valid_expose_count") or 0
    hidden_ad_count = ad_result.get("hidden_ad_count") or 0
    
    if total_ad_count > 0:
        invalid_count = total_ad_count - valid_expose_count
        if invalid_count > 0:
            score += invalid_count * RISK_WEIGHT["ad_no_valid_expose_single"]
            risk_detail.append(f"{invalid_count} 个广告位未达到50%有效曝光阈值")
        if hidden_ad_count > 0:
            score += hidden_ad_count * RISK_WEIGHT["ad_css_hidden_single"]
            risk_detail.append(f"{hidden_ad_count} 个广告位被CSS隐藏，作弊特征")

    # 风险等级
    if score >= 70:
        level = "🔴 高危（极易封号/无效流量标记）"
    elif score >= 35:
        level = "🟠 中危（存在明显机器人特征，复审概率高）"
    else:
        level = "🟢 低危（基础伪装合格，少量优化点）"

    report["risk_calc"]["total_score"] = score
    report["risk_calc"]["risk_level"] = level
    report["risk_calc"]["risk_reason_list"] = risk_detail

    return report


# ============================================================
# 攻防演练运行器：启动带反检测注入的浏览器 -> 打开目标站 -> 风控探测 -> 保存报告
# ============================================================

# 与主流程一致的关键反检测注入（覆盖 risk_check 探测的各维度）
_STEALTH_INIT_SCRIPT = r"""
(function() {
    try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true}); } catch(e) {}
    // 清除 cdc_ / 自动化残留
    try {
        for (const k of Object.getOwnPropertyNames(window)) {
            if (k.indexOf('cdc_') === 0 || k.indexOf('$cdc_') === 0) { try { delete window[k]; } catch(e) {} }
        }
        delete window.__webdriver_evaluate; delete window.__selenium_unwrapped; delete window.__playwright;
    } catch(e) {}
    // Canvas 噪声 hook
    (function() {
        let _s = (Math.floor(Math.random()*2147483647)) >>> 0 || 1;
        const _rnd = function(){ _s = (_s*1103515245+12345)&0x7fffffff; return _s/0x7fffffff; };
        const _orig = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function() {
            const d = _orig.apply(this, arguments);
            try { const a=d.data; for (let i=0;i<a.length;i+=4){ if(_rnd()<0.02){ const n=(_rnd()*3|0)-1; a[i]=Math.max(0,Math.min(255,a[i]+n)); a[i+1]=Math.max(0,Math.min(255,a[i+1]+n)); a[i+2]=Math.max(0,Math.min(255,a[i+2]+n)); } } } catch(e) {}
            return d;
        };
    })();
    // WebGL hook
    (function() {
        const patch = function(proto){ if(!proto||!proto.getParameter) return; const o=proto.getParameter; proto.getParameter=function(p){ if(p===37445) return 'Google Inc. (Intel)'; if(p===37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)'; return o.call(this,p); }; };
        try { patch(WebGLRenderingContext.prototype); } catch(e) {}
        try { patch(WebGL2RenderingContext.prototype); } catch(e) {}
    })();
    // plugins 伪造（避免空列表）
    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', { get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}], configurable: true });
        }
    } catch(e) {}
    // mediaDevices 伪造枚举
    try {
        if (navigator.mediaDevices) {
            navigator.mediaDevices.enumerateDevices = function() {
                return Promise.resolve([{deviceId:'default',kind:'audioinput',label:'',groupId:'g1'},{deviceId:'cam01',kind:'videoinput',label:'',groupId:'g2'}]);
            };
        }
    } catch(e) {}
    // localStorage 随机化种子（避免存储未随机化判定）
    try {
        if (!localStorage.getItem('_app_cid')) { localStorage.setItem('_app_cid', String(Math.floor(Math.random()*1e10))+'.'+String(Math.floor(Math.random()*1e10))); }
        if (!localStorage.getItem('_app_pref')) { localStorage.setItem('_app_pref', 'theme=light'); }
        if (!localStorage.getItem('_app_sid')) { localStorage.setItem('_app_sid', Math.random().toString(36).slice(2)); }
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

    # 延迟导入，避免循环依赖
    from selenium_bridge import sync_playwright

    progress_fn(5, "初始化演练环境")
    log_fn(f"🛡️ 攻防演练启动 | 目标={target_url} | 反检测注入={'开启' if with_stealth else '关闭(裸浏览器基线)'}")

    launch_args = [
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-webrtc", "--disable-webrtc-encryption",
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
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="149", "Google Chrome";v="149"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Linux"',
                    "Referer": "https://www.google.com/",
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

            progress_fn(70, "执行风控探测")
            log_fn("🔍 正在执行风控漏洞探测...")
            report = run_risk_detect(page, proxy_ip=None, ad_selector=None)
            report["meta"] = {
                "target_url": target_url,
                "with_stealth": with_stealth,
                "headless": headless,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    color = "#16a34a" if score < 35 else ("#f59e0b" if score < 70 else "#dc2626")
    reasons = calc.get("risk_reason_list", [])
    reason_html = "".join(f"<li>{_html.escape(str(r))}</li>" for r in reasons) or "<li>无明显风险项</li>"

    def _section(title, data):
        body = _html.escape(_json.dumps(data, ensure_ascii=False, indent=2))
        return f"<h3>{_html.escape(title)}</h3><pre>{body}</pre>"

    sections = "".join([
        _section("基础环境", report.get("base_info", {})),
        _section("自动化探针", report.get("automation_probe", {})),
        _section("指纹检测", report.get("fingerprint", {})),
        _section("时区/地理", report.get("timezone_geo", {})),
        _section("网络/WebRTC", report.get("network_ip", {})),
        _section("HTTP头/UA", report.get("http_header", {})),
        _section("广告风控", report.get("ad_risk", {})),
        _section("会话存储", report.get("session_storage", {})),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>攻防演练报告</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;padding:24px;}}
.card{{background:#1e293b;border-radius:10px;padding:20px;margin-bottom:16px;}}
.score{{font-size:42px;font-weight:bold;color:{color};}}
h1{{color:#60a5fa;}} h3{{color:#93c5fd;border-bottom:1px solid #334155;padding-bottom:6px;}}
pre{{background:#0f172a;padding:12px;border-radius:6px;overflow:auto;font-size:12px;}}
ul{{line-height:1.8;}} .meta{{color:#94a3b8;font-size:13px;}}
</style></head><body>
<h1>🛡️ 攻防演练报告</h1>
<div class="card">
  <div class="meta">目标站: {_html.escape(str(meta.get('target_url','')))} ｜ 反检测注入: {'开启' if meta.get('with_stealth') else '关闭(裸基线)'} ｜ 时间: {_html.escape(str(meta.get('time','')))}</div>
  <div class="score">{score} 分</div>
  <div style="font-size:18px;margin-top:6px;">{_html.escape(str(level))}</div>
  <h3>风险命中项</h3><ul>{reason_html}</ul>
</div>
<div class="card">{sections}</div>
</body></html>"""
