#!/usr/bin/env python3
"""测试修复后的代理认证扩展（移除--disable-extensions冲突后）"""
import os, sys, json, time

# 代理配置（IPDeep gate）
proxy_host = "gate.ipdeep.com"
proxy_port = 8082

# 从config.json读取凭证
with open('config.json', 'r') as f:
    cfg = json.load(f)
proxy_user = cfg.get('ip_proxy_user', '')
proxy_pwd = cfg.get('ip_proxy_pwd', '')

print(f"代理: {proxy_host}:{proxy_port}")
print(f"用户: {proxy_user[:8]}...")

# 生成扩展
ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.proxy_auth_ext')
os.makedirs(ext_dir, exist_ok=True)
manifest = {
    "version": "1.0.0",
    "manifest_version": 3,
    "name": "Proxy Auth Helper",
    "permissions": ["webRequest", "webRequestAuthProvider"],
    "host_permissions": ["<all_urls>"],
    "background": {"service_worker": "background.js"},
    "minimum_chrome_version": "108"
}
background_js = f"""const USERNAME = {repr(proxy_user)};
const PASSWORD = {repr(proxy_pwd)};
chrome.webRequest.onAuthRequired.addListener(
  (details) => ({{authCredentials: {{username: USERNAME, password: PASSWORD}}}}),
  {{urls: ["<all_urls>"]}},
  ["asyncBlocking"]
);
"""
with open(os.path.join(ext_dir, 'manifest.json'), 'w') as f:
    json.dump(manifest, f)
with open(os.path.join(ext_dir, 'background.js'), 'w') as f:
    f.write(background_js)
print(f"扩展目录: {ext_dir}")

# 使用selenium_bridge启动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from selenium_bridge import sync_playwright

print("\n=== 启动浏览器（有扩展，无--disable-extensions） ===")
p = sync_playwright()

# ★ 关键：不包含 --disable-extensions
args = [
    f"--proxy-server=http://{proxy_host}:{proxy_port}",
    f"--load-extension={ext_dir}",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--window-size=1366,768",
]

try:
    browser = p.chromium.launch(channel="chrome", headless=False, args=args)
    print("✅ 浏览器启动成功")
    context = browser.new_context()
    page = context.new_page()
    
    # 等待扩展service worker注册
    time.sleep(2)
    
    print("访问 https://freestoryweb.com/ ...")
    page.goto("https://freestoryweb.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    
    title = page.title()
    url = page.url
    html = page.content()
    
    print(f"\nURL: {url}")
    print(f"Title: {title}")
    print(f"HTML length: {len(html)}")
    
    # 检查广告域名
    ad_domains = ['curoax.com', 'pufted.com', 'bony-teaching.com', 'untimely-hello.com', 'hilltopads']
    found_ads = []
    for d in ad_domains:
        if d in html:
            found_ads.append(d)
    
    if found_ads:
        print(f"✅ 找到广告域名: {found_ads}")
    else:
        print(f"❌ 未找到广告域名")
        print(f"HTML[:500]: {html[:500]}")
    
    # 检查是否是Chrome错误页
    if 'chrome-error://' in url or 'ERR_' in html:
        print("❌ Chrome错误页 - 代理认证仍然失败!")
    elif len(html) < 100:
        print("❌ 页面内容过少 - 可能扩展未生效")
    elif len(html) > 10000 and not found_ads:
        print("⚠️ 页面加载成功但无广告（可能是IP/地区问题）")
    
    browser.close()
except Exception as e:
    print(f"❌ 异常: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
