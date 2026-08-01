"""测试：仅用扩展处理代理认证"""
import sys, os, json, time, random
sys.path.insert(0, '/root/selenium_traffic_system')
from selenium_bridge import sync_playwright

proxy_host = 'gate.ipdeep.com'
proxy_port = 8082
proxy_user = f'd2841616000-res-country-AU-session-{random.randint(1000000,9999999)}-sessiontime-5'
proxy_pass = 'zhan7263'

# 生成扩展 - 仅处理认证
ext_dir = '/root/selenium_traffic_system/.proxy_auth_ext'
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
background_js = 'const config = {\n'
background_js += f'  username: {repr(proxy_user)},\n'
background_js += f'  password: {repr(proxy_pass)}\n'
background_js += '};\n'
background_js += 'chrome.webRequest.onAuthRequired.addListener(\n'
background_js += '  (details) => ({authCredentials: {username: config.username, password: config.password}}),\n'
background_js += '  {urls: ["<all_urls>"]},\n'
background_js += '  ["asyncBlocking"]\n'
background_js += ');\n'

with open(os.path.join(ext_dir, 'manifest.json'), 'w') as f:
    json.dump(manifest, f)
with open(os.path.join(ext_dir, 'background.js'), 'w') as f:
    f.write(background_js)

print(f'扩展已生成: {ext_dir}')
print(f'background.js内容:\n{background_js}')

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=[
            '--no-sandbox','--disable-dev-shm-usage','--disable-gpu',
            '--disable-blink-features=AutomationControlled',
            f'--proxy-server=http://{proxy_host}:{proxy_port}',
            f'--load-extension={ext_dir}',
        ]
    )
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        viewport={'width': 1920, 'height': 1080}
    )
    page = ctx.new_page()
    
    print('\n=== 通过代理+认证扩展访问 ===')
    page.goto('https://freestoryweb.com/', timeout=30000, wait_until='domcontentloaded')
    try:
        page.wait_for_load_state('networkidle', timeout=8000)
    except:
        pass
    time.sleep(5)
    
    url = page.url
    title = page.title()
    html = page.content()
    
    print(f'URL: {url}')
    print(f'Title: {title}')
    print(f'HTML length: {len(html)}')
    
    ad_domains = ['curoax', 'pufted', 'bony-teaching', 'untimely-hello']
    found_ads = False
    for domain in ad_domains:
        count = html.lower().count(domain.lower())
        if count > 0:
            print(f'  ✅ {domain}: {count}次')
            found_ads = True
    if not found_ads:
        print(f'  ❌ 无广告')
        print(f'  HTML前500: {html[:500]}')
    
    scripts = page.evaluate("() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)")
    print(f'外部脚本数: {len(scripts)}')
    
    browser.close()
    print('\n✅ 代理认证扩展成功！' if found_ads else '\n❌ 仍失败')
