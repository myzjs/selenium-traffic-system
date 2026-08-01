#!/usr/bin/env python3
"""
Selenium流量系统 - 完整流程测试脚本（带Mock数据）
"""
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("full_workflow_test")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CONFIG = json.load(open(CONFIG_PATH, "r", encoding="utf-8")) if os.path.exists(CONFIG_PATH) else {}
REPORT_DIR = os.path.join(os.path.dirname(__file__), "test_reports")
os.makedirs(REPORT_DIR, exist_ok=True)


def test_ip_provider_with_mock():
    logger.info("=" * 60)
    logger.info("测试阶段1：直连 IPDeep API 获取代理（Mock）")
    logger.info("=" * 60)
    
    # 模拟 IPDeep 直连返回（格式: host:port:user:pwd）
    mock_result = {
        "success": True,
        "proxy_host": "104.129.54.64",
        "proxy_port": "8082",
        "proxy_username": "ipdeep_user01",
        "proxy_password": "ipdeep_pass01",
        "ip_info": {"ip": "192.168.1.100", "country": "United States", "city": "New York"},
        "country_code": "US"
    }
    
    logger.info("IPDeep 直连获取代理成功！")
    logger.info(f"出口IP: {mock_result['ip_info']['ip']}")
    logger.info(f"代理: {mock_result['proxy_host']}:{mock_result['proxy_port']} (HTTP认证代理)")
    logger.info(f"认证: {mock_result['proxy_username']}:***")
    return mock_result


def test_risk_detection(page, proxy_info):
    logger.info("\n测试阶段2：风险检测")
    logger.info("=" * 60)
    
    import risk_check
    report = risk_check.run_risk_detect(page, proxy_ip=proxy_info.get("ip_info", {}).get("ip"))
    calc = report.get("risk_calc", {})
    
    logger.info(f"风险评分: {calc.get('total_score', 0)} 分")
    logger.info(f"风险等级: {calc.get('risk_level', '')}")
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(REPORT_DIR, f"risk_report_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    return {"success": True, "score": calc.get("total_score", 0)}


def simulate_human_behavior(page, config):
    logger.info("\n测试阶段3：真人模拟行为")
    logger.info("=" * 60)
    
    def delay(min_sec=0.5, max_sec=2.0):
        time.sleep(random.uniform(min_sec, max_sec))
    
    logger.info("模拟鼠标移动...")
    viewport = page.viewport_size
    for _ in range(random.randint(3, 7)):
        page.mouse.move(random.randint(50, viewport["width"] - 50), 
                        random.randint(50, viewport["height"] - 50), 
                        steps=random.randint(5, 15))
        delay(0.1, 0.3)
    
    logger.info("模拟页面滚动...")
    for _ in range(random.randint(2, 5)):
        page.mouse.wheel(0, random.randint(100, 300))
        delay(0.3, 0.8)
    
    logger.info("真人模拟行为完成")
    return {"success": True}


def test_search_engine_navigation(page, search_query, target_domain):
    logger.info("\n测试阶段4：搜索引擎导航")
    logger.info("=" * 60)
    
    search_url = f"https://www.google.com/search?q={search_query}"
    page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
    logger.info("搜索页面加载完成")
    time.sleep(2)
    
    links = page.query_selector_all("a")
    target_link = None
    for link in links:
        href = link.get_attribute("href") or ""
        if target_domain in href:
            target_link = link
            break
    
    if target_link:
        target_link.scroll_into_view_if_needed()
        time.sleep(random.uniform(0.3, 0.8))
        target_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        logger.info(f"目标页面加载完成: {page.url}")
        return {"success": True, "target_url": page.url}
    return {"success": False, "error": "未找到目标链接"}


def test_browsing_rules(page, config):
    logger.info("\n测试阶段5：按规则浏览网页")
    logger.info("=" * 60)
    
    browse_rules = config.get("browse_rules", {})
    stay_time = random.uniform(browse_rules.get("stay_min", 15), browse_rules.get("stay_max", 60))
    logger.info(f"页面停留时间: {int(stay_time)}秒")
    
    links = page.query_selector_all("a[href^='http']")
    if links and browse_rules.get("click_links", True):
        random_link = random.choice(links)
        href = random_link.get_attribute("href") or ""
        if "javascript" not in href.lower() and len(href) > 10:
            random_link.scroll_into_view_if_needed()
            time.sleep(random.uniform(0.3, 0.7))
            random_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
    
    start_time = time.time()
    while time.time() - start_time < stay_time:
        if random.random() < 0.1:
            page.mouse.wheel(0, random.randint(-50, 100))
        time.sleep(0.5)
    
    logger.info("规则浏览完成")
    return {"success": True}


def main():
    logger.info("开始完整流程测试（Mock模式）")
    
    results = {}
    ip_result = test_ip_provider_with_mock()
    results["ip_provider"] = {"success": True, **ip_result}
    
    from selenium_bridge import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        
        import risk_check
        context.add_init_script(risk_check._STEALTH_INIT_SCRIPT)
        
        page = context.new_page()
        page.goto("https://httpbin.org/html", timeout=30000)
        
        results["risk_detection"] = test_risk_detection(page, ip_result)
        results["human_behavior"] = simulate_human_behavior(page, CONFIG)
        results["search_navigation"] = test_search_engine_navigation(page, "python tutorial", "w3schools.com")
        
        if results["search_navigation"].get("success"):
            results["browsing_rules"] = test_browsing_rules(page, CONFIG)
        
        browser.close()
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(REPORT_DIR, f"full_test_report_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    passed = sum(1 for r in results.values() if r.get("success"))
    logger.info(f"\n测试结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())