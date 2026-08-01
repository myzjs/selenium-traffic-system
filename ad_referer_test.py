"""验证Referer头是否导致广告代码消失"""
import sys
sys.path.insert(0, '/root/selenium_traffic_system')
import time
from selenium_bridge import sync_playwright

def test_with_referer(referer_url, label):
    print(f'\n{"="*60}')
    print(f'测试: {label} (Referer: {referer_url})')
    print(f'{"="*60}')
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080}
        )
        page = ctx.new_page()
        
        # 先访问Referer页
        if referer_url:
            try:
                page.goto(referer_url, timeout=15000, wait_until='domcontentloaded')
                time.sleep(2)
            except:
                pass
        
        # 然后导航到目标站
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
        
        browser.close()
        return found_ads

# 测试1: 无Referer（直接访问）
test_with_referer(None, '无Referer直接访问')

# 测试2: Google Referer
test_with_referer('https://www.google.com/search?q=free+stories+online', 'Google搜索Referer')

# 测试3: Reddit Referer  
test_with_referer('https://www.reddit.com/', 'Reddit Referer')

print('\n\n=== 结论 ===')
print('如果无Referer有广告但带Referer无广告 → 网站根据Referer决定是否展示广告')
