#!/usr/bin/env python3
"""
Selenium流量系统 - 完整流程集成测试脚本
需要真实浏览器和代理才能运行。
"""
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("full_workflow_test")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
else:
    CONFIG = {}

REPORT_DIR = os.path.join(os.path.dirname(__file__), "test_reports")
os.makedirs(REPORT_DIR, exist_ok=True)


def run_ip_provider():
    """阶段1：通过中转服务器获取IPDeep代理（带重试）"""
    logger.info("=" * 60)
    logger.info("阶段1：通过中转服务器获取IPDeep代理")
    logger.info("=" * 60)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            import ip_provider
            provider = ip_provider.init_from_config(CONFIG)
            logger.info(f"VPS配置: {CONFIG.get('vps_host')}:{CONFIG.get('vps_new_port')}")
            logger.info(f"代理池数量: {len(CONFIG.get('proxy_pool', []))}")
            logger.info(f"开始获取代理IP... (尝试 {attempt+1}/{max_retries})")
            result = provider.get_ip()

            if result.get("success"):
                ip_info = result.get("ip_info", {})
                logger.info(f"出口IP: {ip_info.get('ip', '未知')}")
                logger.info(f"代理: {result.get('proxy_host')}:{result.get('proxy_port')}")
                logger.info(f"国家: {ip_info.get('country', '未知')}")
                logger.info(f"城市: {ip_info.get('city', '未知')}")
                if ip_info.get("ip"):
                    if ip_provider.check_ip_used_recently(ip_info["ip"]):
                        logger.warning("该IP在去重间隔内已使用")
                    else:
                        logger.info("IP未在近期使用，可正常使用")
                        ip_provider.record_ip_use(ip_info["ip"])
                return {"success": True, "proxy_host": result.get("proxy_host"), "proxy_port": result.get("proxy_port"), "ip_info": ip_info, "result": result}
            else:
                logger.warning(f"获取代理失败 (尝试 {attempt+1}/{max_retries}): {result.get('error', '未知错误')}")
                if attempt < max_retries - 1:
                    time.sleep(5)
        except Exception as e:
            logger.warning(f"IP获取异常 (尝试 {attempt+1}/{max_retries}): {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)

    return {"success": False, "error": f"重试{max_retries}次后仍失败"}


def run_risk_detection(page, proxy_info):
    """阶段2：风险检测"""
    logger.info("\n" + "=" * 60)
    logger.info("阶段2：风险检测")
    logger.info("=" * 60)

    try:
        import risk_check
        logger.info("执行风控漏洞探测...")
        report = risk_check.run_risk_detect(page, proxy_ip=proxy_info.get("ip_info", {}).get("ip"))
        calc = report.get("risk_calc", {})
        score = calc.get("total_score", 0)
        level = calc.get("risk_level", "")
        logger.info(f"风险评分: {score} 分")
        logger.info(f"风险等级: {level}")
        if calc.get("risk_reason_list"):
            logger.info("风险项列表:")
            for reason in calc.get("risk_reason_list")[:5]:
                logger.info(f"  • {reason}")

        checks = [
            ("WebDriver检测", not report["automation_probe"].get("nav_webdriver")),
            ("CDC残留", not report["automation_probe"].get("cdc_trace")),
            ("Canvas噪声", report["fingerprint"].get("canvas_noise_injected")),
            ("时区匹配", report["timezone_geo"].get("tz_match")),
            ("有效Referer", report["network_ip"].get("has_valid_referer")),
            ("UA安全", not report["http_header"]["ua_check"].get("ua_risk_flag"))
        ]
        logger.info("\n关键指标检查:")
        all_pass = True
        for check_name, passed in checks:
            status = "通过" if passed else "失败"
            logger.info(f"  {check_name}: {status}")
            if not passed:
                all_pass = False

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(REPORT_DIR, f"risk_report_{ts}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"风险报告已保存: {report_path}")
        return {"success": all_pass, "score": score, "level": level, "report_path": report_path, "risk_reasons": calc.get("risk_reason_list", [])}
    except Exception as e:
        logger.error(f"风险检测异常: {type(e).__name__}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def run_human_behavior(page, config):
    """阶段3：真人模拟行为"""
    logger.info("\n" + "=" * 60)
    logger.info("阶段3：真人模拟行为")
    logger.info("=" * 60)

    try:
        def human_delay(min_sec=0.5, max_sec=2.0):
            time.sleep(random.uniform(min_sec, max_sec))

        def simulate_mouse_movement(p):
            logger.info("  模拟鼠标移动...")
            viewport = p.viewport_size
            for _ in range(random.randint(3, 7)):
                x = random.randint(50, viewport["width"] - 50)
                y = random.randint(50, viewport["height"] - 50)
                p.mouse.move(x, y, steps=random.randint(5, 15))
                human_delay(0.1, 0.3)

        def simulate_scroll(p):
            logger.info("  模拟页面滚动...")
            for _ in range(random.randint(2, 5)):
                p.mouse.wheel(0, random.randint(100, 300))
                human_delay(0.3, 0.8)

        simulate_mouse_movement(page)
        human_delay(0.5, 1.0)
        logger.info("  模拟阅读行为...")
        human_delay(1.0, 2.5)
        simulate_mouse_movement(page)
        human_delay(0.5, 1.5)
        simulate_scroll(page)
        logger.info("真人模拟行为完成")
        return {"success": True}
    except Exception as e:
        logger.error(f"真人模拟行为异常: {type(e).__name__}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def run_search_engine_navigation(page, search_query, target_domain):
    """阶段4：搜索引擎导航"""
    logger.info("\n" + "=" * 60)
    logger.info("阶段4：搜索引擎导航")
    logger.info("=" * 60)

    try:
        logger.info(f"搜索关键词: {search_query}")
        search_url = f"https://www.google.com/search?q={search_query}"
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        logger.info("搜索页面加载完成")
        time.sleep(2)
        logger.info(f"查找目标网站: {target_domain}")
        links = page.query_selector_all("a")
        target_link = None
        for link in links:
            href = link.get_attribute("href") or ""
            if target_domain in href:
                target_link = link
                break
        if target_link:
            logger.info(f"找到目标链接: {target_link.get_attribute('href')[:80]}...")
            target_link.scroll_into_view_if_needed()
            time.sleep(random.uniform(0.3, 0.8))
            target_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            logger.info(f"目标页面加载完成: {page.url}")
            return {"success": True, "target_url": page.url}
        else:
            logger.warning(f"未找到目标网站链接: {target_domain}")
            return {"success": False, "error": "未找到目标链接"}
    except Exception as e:
        logger.error(f"搜索引擎导航异常: {type(e).__name__}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def run_browsing_rules(page, config):
    """阶段5：按规则浏览网页"""
    logger.info("\n" + "=" * 60)
    logger.info("阶段5：按规则浏览网页")
    logger.info("=" * 60)

    try:
        browse_rules = config.get("browse_rules", {})
        stay_time = random.uniform(browse_rules.get("stay_min", 15), browse_rules.get("stay_max", 60))
        logger.info(f"页面停留时间: {int(stay_time)}秒")
        logger.info(f"滚动深度: {int(browse_rules.get('scroll_depth', 0.8) * 100)}%")

        if browse_rules.get("click_links", True):
            logger.info("随机点击页面链接...")
            links = page.query_selector_all("a[href^='http']")
            if links:
                random_link = random.choice(links)
                href = random_link.get_attribute("href") or ""
                if "javascript" not in href.lower() and len(href) > 10:
                    random_link.scroll_into_view_if_needed()
                    time.sleep(random.uniform(0.3, 0.7))
                    random_link.click()
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    logger.info(f"  点击链接: {href[:60]}...")

        start_time = time.time()
        while time.time() - start_time < stay_time:
            if random.random() < 0.1:
                page.mouse.wheel(0, random.randint(-50, 100))
            time.sleep(0.5)
        logger.info("规则浏览完成")
        return {"success": True, "stay_time": stay_time}
    except Exception as e:
        logger.error(f"规则浏览异常: {type(e).__name__}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def main():
    """集成测试主入口"""
    logger.info("开始完整流程测试")
    logger.info("=" * 60)

    results = {}
    ip_result = run_ip_provider()
    results["ip_provider"] = ip_result

    if not ip_result.get("success"):
        logger.error("IP获取失败，终止测试")
        return 1

    from selenium_bridge import sync_playwright
    browser = None
    try:
        with sync_playwright() as p:
            logger.info("\n启动浏览器...")
            launch_args = [
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-webrtc",
                "--disable-web-security",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--enforce-webrtc-ip-permission-check=false",
            ]
            proxy_host = ip_result.get("proxy_host")
            proxy_port = ip_result.get("proxy_port")
            if proxy_host and proxy_port:
                proxy_server = f"socks5://{proxy_host}:{proxy_port}"
                launch_args.append(f"--proxy-server={proxy_server}")
                logger.info(f"使用代理: {proxy_server}")

            browser = p.chromium.launch(headless=True, args=launch_args)
            context = browser.new_context(
                locale=CONFIG.get("locale", "zh-CN"),
                timezone_id=CONFIG.get("timezone", "Asia/Shanghai"),
                user_agent=CONFIG.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": "https://www.google.com/",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
                },
            )
            import risk_check
            context.add_init_script(risk_check._STEALTH_INIT_SCRIPT)
            page = context.new_page()
            page.goto("https://httpbin.org/html", timeout=30000)

            risk_result = run_risk_detection(page, ip_result)
            results["risk_detection"] = risk_result

            human_result = run_human_behavior(page, CONFIG)
            results["human_behavior"] = human_result

            search_result = run_search_engine_navigation(page, "python selenium tutorial", "w3schools.com")
            results["search_navigation"] = search_result

            if search_result.get("success"):
                browse_result = run_browsing_rules(page, CONFIG)
                results["browsing_rules"] = browse_result

            browser.close()
    except Exception as e:
        logger.error(f"测试异常: {type(e).__name__}: {e}", exc_info=True)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        return 1

    logger.info("\n" + "=" * 60)
    logger.info("测试报告汇总")
    logger.info("=" * 60)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORT_DIR, f"full_test_report_{ts}.json")
    summary = {"test_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "results": results, "summary": {}}

    passed = 0
    total = 0
    for phase, result in results.items():
        total += 1
        if result.get("success"):
            passed += 1
        logger.info(f"  {phase}: {'通过' if result.get('success') else '失败'}")

    summary["summary"] = {"total_phases": total, "passed_phases": passed, "failed_phases": total - passed, "overall_status": "PASS" if passed == total else "FAIL"}
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info(f"\n完整测试报告已保存: {report_path}")
    logger.info(f"测试结果: {passed}/{total} 通过")
    if passed == total:
        logger.info("所有测试通过！")
        return 0
    else:
        logger.error("部分测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
