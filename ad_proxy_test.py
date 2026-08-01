"""测试通过代理访问时广告是否存在 - 简化版"""
import sys
sys.path.insert(0, '/root/selenium_traffic_system')
import time
import random
from selenium_bridge import sync_playwright

# 使用与app相同的代理配置
proxy_host = 'gate.ipdeep.com'
proxy_port = 8082
proxy_user = f'd2841616000-res-country-AU-session-{random.randint(1000000,9999999)}-sessiontime-5'
proxy_pass = 'zhan7263'
proxy_server = f'http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}'

print(f'代理: {proxy_server[:50]}...')

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=[
            '--no-sandbox','--disable-dev-shm-usage','--disable-gpu',
            '--disable-blink-features=AutomationControlled',
            f'--proxy-server={proxy_server}',
        ]
    )
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        viewport={'width': 1920, 'height': 1080}
    )
    page = ctx.new_page()
    
    print('\n=== 通过AU代理访问 freestoryweb.com ===')
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
    
    # 广告域名
    ad_domains = ['curoax', 'pufted', 'bony-teaching', 'untimely-hello']
    found_ads = False
    for domain in ad_domains:
        count = html.lower().count(domain.lower())
        if count > 0:
            print(f'  ✅ {domain}: {count}次')
            found_ads = True
    if not found_ads:
        print(f'  ❌ 无任何广告域名!')
    
    # 外部脚本
    scripts = page.evaluate("() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)")
    print(f'外部脚本数: {len(scripts)}')
    ad_scripts = [s for s in scripts if any(d in s for d in ad_domains)]
    print(f'广告脚本数: {len(ad_scripts)}')
    
    # iframe
    iframes = page.evaluate("() => Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src)")
    ad_iframes = [f for f in iframes if any(d in f for d in ad_domains)]
    print(f'iframe数: {len(iframes)}, 广告iframe: {len(ad_iframes)}')
    
    if not found_ads:
        print('\n=== HTML前2000字符 ===')
        print(html[:2000])
    
    browser.close()
    
    print('\n\n=== 结论 ===')
    if found_ads:
        print('✅ 通过代理访问时广告存在 → 问题不在代理IP')
    else:
        print('❌ 通过代理访问时广告消失 → 代理IP被网站/广告网络屏蔽!')
