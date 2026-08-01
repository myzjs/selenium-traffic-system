"""深度诊断：检查freestoryweb.com在自动化浏览器中的实际HTML内容"""
import sys
sys.path.insert(0, '/root/selenium_traffic_system')
import time
from selenium_bridge import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-blink-features=AutomationControlled'])
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        viewport={'width': 1920, 'height': 1080}
    )
    page = ctx.new_page()
    
    print('=== goto freestoryweb.com ===')
    page.goto('https://freestoryweb.com/', timeout=30000, wait_until='domcontentloaded')
    try:
        page.wait_for_load_state('networkidle', timeout=8000)
    except:
        pass
    time.sleep(5)
    
    url = page.url
    title = page.title()
    html = page.content()
    html_len = len(html)
    
    print(f'URL: {url}')
    print(f'Title: {title}')
    print(f'HTML length: {html_len}')
    
    # 检查广告域名
    ad_domains = ['curoax', 'pufted', 'bony-teaching', 'untimely-hello', 'hilltopads', 'googleadservices', 'googlesyndication', 'adsbygoogle', 'propeller', 'mgid', 'taboola']
    print('\n=== 广告域名检测 ===')
    for domain in ad_domains:
        count = html.lower().count(domain.lower())
        if count > 0:
            print(f'  ✅ {domain}: 出现 {count} 次')
    
    # 检查所有script标签
    scripts_info = page.evaluate("""
        () => {
            const all = document.querySelectorAll('script');
            const with_src = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
            const inline_count = all.length - with_src.length;
            // 获取前5个inline script的前100字符
            const inline_previews = [];
            document.querySelectorAll('script:not([src])').forEach((s, i) => {
                if (i < 5) inline_previews.push(s.textContent.substring(0, 150));
            });
            return {total: all.length, with_src: with_src, inline_count, inline_previews};
        }
    """)
    print(f'\n=== Script标签统计 ===')
    print(f'  总数: {scripts_info["total"]}')
    print(f'  有src: {len(scripts_info["with_src"])}')
    print(f'  内联: {scripts_info["inline_count"]}')
    if scripts_info['with_src']:
        print(f'  外部脚本列表:')
        for s in scripts_info['with_src'][:10]:
            print(f'    - {s}')
    if scripts_info['inline_previews']:
        print(f'  内联脚本预览:')
        for i, preview in enumerate(scripts_info['inline_previews']):
            print(f'    [{i}] {preview[:120]}...')
    
    # 检查iframe
    iframes = page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(f => ({src: f.src, id: f.id, name: f.name}))")
    print(f'\n=== iframe统计: {len(iframes)} ===')
    for iframe in iframes[:5]:
        print(f'  - src={iframe["src"][:80]} id={iframe["id"]} name={iframe["name"]}')
    
    # 检查HTML中是否有 anti-hijack 或 anti-bot 脚本
    anti_keywords = ['anti-hijack', 'anti-bot', 'bot-detect', 'fingerprint', 'webdriver', 'navigator.webdriver', 'MutationObserver', 'digitalbook']
    print('\n=== 反bot/反劫持关键词 ===')
    for kw in anti_keywords:
        if kw.lower() in html.lower():
            # 找到上下文
            idx = html.lower().find(kw.lower())
            context = html[max(0,idx-30):idx+len(kw)+50]
            print(f'  ⚠️ 发现 "{kw}": ...{context}...')
    
    # dump HTML样本（前3000字符）
    print('\n=== HTML前3000字符样本 ===')
    print(html[:3000])
    print('\n=== HTML后1000字符样本 ===')
    print(html[-1000:])
    
    # 检查navigator.webdriver
    webdriver_val = page.evaluate("() => navigator.webdriver")
    print(f'\n=== 浏览器指纹 ===')
    print(f'  navigator.webdriver = {webdriver_val}')
    
    # 检查是否有 ad-block 检测
    adblock_check = page.evaluate("""
        () => {
            // 常见ad-block检测方式
            const testEl = document.createElement('div');
            testEl.innerHTML = '&nbsp;';
            testEl.className = 'adsbox ad-placement ad-banner';
            testEl.style.cssText = 'position:absolute;left:-9999px;height:1px;width:1px;';
            document.body.appendChild(testEl);
            const blocked = testEl.offsetHeight === 0;
            document.body.removeChild(testEl);
            return blocked;
        }
    """)
    print(f'  ad-block检测(元素高度=0): {adblock_check}')
    
    # 尝试访问/books/页面
    print('\n\n=== 访问 /books/ 页面 ===')
    page.goto('https://freestoryweb.com/books/', timeout=30000, wait_until='domcontentloaded')
    try:
        page.wait_for_load_state('networkidle', timeout=8000)
    except:
        pass
    time.sleep(5)
    
    html2 = page.content()
    print(f'URL: {page.url}')
    print(f'HTML length: {len(html2)}')
    
    scripts2 = page.evaluate("() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)")
    iframes2 = page.evaluate("() => Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src)")
    print(f'外部脚本: {len(scripts2)}')
    for s in scripts2[:10]:
        print(f'  - {s}')
    print(f'iframe: {len(iframes2)}')
    for f in iframes2[:5]:
        print(f'  - {f}')
    
    # 在books页面检查广告域名
    for domain in ad_domains:
        count = html2.lower().count(domain.lower())
        if count > 0:
            print(f'  ✅ {domain}: 出现 {count} 次')
    
    browser.close()
    print('\n=== 诊断完成 ===')
