# -*- coding: utf-8 -*-
"""
sandbox_test.py —— 沙盒集成测试
在VPS上通过HTTP API验证全部业务流程，不消耗任何代理IP。

测试范围：
  A. 服务健康检查
  B. 配置读取/保存
  C. 计划生成/清除
  D. 任务启停控制
  E. 风控检测（隐身浏览器 + 沙盒页面）
  F. 生产准入测试API
  G. 日志与状态接口
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime

import requests

BASE_URL = "http://127.0.0.1:5001"
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_reports")
os.makedirs(REPORT_DIR, exist_ok=True)

# 测试结果收集
results = []
_start_time = time.time()


def record(category, name, passed, detail="", elapsed=0):
    results.append({
        "category": category,
        "name": name,
        "passed": passed,
        "detail": detail,
        "elapsed_s": round(elapsed, 2),
    })
    status = "✅" if passed else "❌"
    print(f"  {status} [{category}] {name} ({elapsed:.1f}s) {detail[:80] if detail else ''}")


def api_get(path, timeout=15):
    return requests.get(f"{BASE_URL}{path}", timeout=timeout)


def api_post(path, data=None, timeout=15):
    return requests.post(f"{BASE_URL}{path}", json=data or {}, timeout=timeout)


# ============================================================
# A. 服务健康检查
# ============================================================
def test_health():
    print("\n" + "=" * 60)
    print("A. 服务健康检查")
    print("=" * 60)

    t0 = time.time()
    try:
        r = api_get("/", timeout=5)
        record("健康", "首页响应", r.status_code == 200, f"HTTP {r.status_code}", time.time() - t0)
    except Exception as e:
        record("健康", "首页响应", False, str(e), time.time() - t0)
        return False

    t0 = time.time()
    try:
        r = api_get("/get_global_task_status", timeout=5)
        data = r.json()
        ok = r.status_code == 200 and "stats" in data
        running = data.get("human_model", {}).get("running", False)
        record("健康", "全局任务状态", ok, f"running={running}", time.time() - t0)
    except Exception as e:
        record("健康", "全局任务状态", False, str(e), time.time() - t0)

    return True


# ============================================================
# B. 配置读取/保存
# ============================================================
def test_config():
    print("\n" + "=" * 60)
    print("B. 配置读取/保存")
    print("=" * 60)

    # 读取配置
    t0 = time.time()
    cfg = None
    try:
        r = api_get("/get_config", timeout=5)
        data = r.json()
        cfg = data.get("config", {})
        ok = r.status_code == 200 and isinstance(cfg, dict) and "proxy_pool" in cfg
        record("配置", "读取配置", ok, f"proxy_pool={len(cfg.get('proxy_pool', []))}个", time.time() - t0)
    except Exception as e:
        record("配置", "读取配置", False, str(e), time.time() - t0)
        return

    # 保存配置（原样保存，不修改任何值）
    t0 = time.time()
    try:
        r = api_post("/save_config", {"config": cfg}, timeout=10)
        data = r.json()
        ok = r.status_code == 200 and data.get("status") == "ok"
        record("配置", "保存配置(原样)", ok, f"status={data.get('status')}", time.time() - t0)
    except Exception as e:
        record("配置", "保存配置(原样)", False, str(e), time.time() - t0)


# ============================================================
# C. 计划生成/清除
# ============================================================
def test_plan():
    print("\n" + "=" * 60)
    print("C. 计划生成/清除")
    print("=" * 60)

    # 生成计划
    t0 = time.time()
    try:
        r = api_post("/generate_plan", {}, timeout=30)
        data = r.json()
        plan = data.get("plan", {})
        # plan 是 dict，包含 country_distribution / daily_summaries 等
        ok = r.status_code == 200 and isinstance(plan, dict) and len(plan) > 0
        daily = plan.get("daily_summaries", [])
        total_tasks = daily[0].get("generated_tasks", 0) if daily else 0
        record("计划", "生成计划", ok and total_tasks > 0,
               f"生成{total_tasks}条任务", time.time() - t0)
    except Exception as e:
        record("计划", "生成计划", False, str(e), time.time() - t0)
        return

    # 验证计划结构
    t0 = time.time()
    try:
        has_dist = "country_distribution" in plan
        has_summary = "daily_summaries" in plan
        has_quota = "country_quota_target" in plan
        struct_ok = has_dist and has_summary and has_quota
        countries = list(plan.get("country_distribution", {}).keys())
        record("计划", "计划结构校验", struct_ok,
               f"国家={countries}", time.time() - t0)
    except Exception as e:
        record("计划", "计划结构校验", False, str(e), time.time() - t0)

    # 获取计划（确认已存储）
    t0 = time.time()
    try:
        r = api_get("/get_plan", timeout=5)
        data = r.json()
        stored_plan = data.get("plan")
        ok = stored_plan is not None
        record("计划", "获取已存计划", ok, f"plan存在={ok}", time.time() - t0)
    except Exception as e:
        record("计划", "获取已存计划", False, str(e), time.time() - t0)

    # 清除计划
    t0 = time.time()
    try:
        r = api_post("/clear_plan", {}, timeout=10)
        data = r.json()
        ok = r.status_code == 200 and data.get("status") == "ok"
        record("计划", "清除计划", ok, f"status={data.get('status')}", time.time() - t0)
    except Exception as e:
        record("计划", "清除计划", False, str(e), time.time() - t0)

    # 确认计划已清除
    t0 = time.time()
    try:
        r = api_get("/get_plan", timeout=5)
        data = r.json()
        ok = data.get("plan") is None
        record("计划", "确认计划已清除", ok, f"plan={data.get('plan')}", time.time() - t0)
    except Exception as e:
        record("计划", "确认计划已清除", False, str(e), time.time() - t0)


# ============================================================
# D. 任务启停控制
# ============================================================
def test_task_control():
    print("\n" + "=" * 60)
    print("D. 任务启停控制")
    print("=" * 60)

    # 确认当前无任务运行
    t0 = time.time()
    try:
        r = api_get("/get_global_task_status", timeout=5)
        data = r.json()
        running = data.get("human_model", {}).get("running", False)
        record("任务", "初始状态(未运行)", not running, f"running={running}", time.time() - t0)
    except Exception as e:
        record("任务", "初始状态(未运行)", False, str(e), time.time() - t0)
        return

    # 停止任务（安全操作，即使没有运行中的任务）
    t0 = time.time()
    try:
        r = api_post("/stop_task", {}, timeout=10)
        data = r.json()
        ok = r.status_code == 200 and data.get("status") == "ok"
        record("任务", "停止任务(安全)", ok, f"status={data.get('status')}", time.time() - t0)
    except Exception as e:
        record("任务", "停止任务(安全)", False, str(e), time.time() - t0)

    # 获取历史任务记录
    t0 = time.time()
    try:
        r = api_get("/get_historical_tasks", timeout=5)
        data = r.json()
        ok = r.status_code == 200
        record("任务", "历史任务记录", ok, f"HTTP {r.status_code}", time.time() - t0)
    except Exception as e:
        record("任务", "历史任务记录", False, str(e), time.time() - t0)


# ============================================================
# E. 风控检测（沙盒模式 - 使用隐身浏览器访问公开页面）
# ============================================================
def test_risk_detection_sandbox():
    print("\n" + "=" * 60)
    print("E. 风控检测（沙盒模式）")
    print("=" * 60)

    # 风险评分检测（使用隐身浏览器 + httpbin）
    t0 = time.time()
    try:
        import risk_check
        from selenium_bridge import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = browser.new_context(
                locale=risk_check.LOCALE,
                timezone_id=risk_check.TIMEZONE,
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": f"{risk_check.LOCALE},{risk_check.LOCALE.split('-')[0]};q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
            context.add_init_script(risk_check._STEALTH_INIT_SCRIPT)
            page = context.new_page()
            page.goto("https://httpbin.org/html", timeout=30000, wait_until="domcontentloaded")
            time.sleep(1)

            # 模拟真人行为（滚动+鼠标）
            import random as _rnd
            for _ in range(3):
                page.mouse.wheel(0, _rnd.randint(200, 500))
                time.sleep(_rnd.uniform(0.5, 1.0))
            page.mouse.move(_rnd.randint(200, 800), _rnd.randint(200, 600))
            time.sleep(0.5)

            report = risk_check.run_risk_detect(page, proxy_ip="127.0.0.1")
            score = report.get("risk_calc", {}).get("total_score", 999)
            # 沙盒模式（无代理IP、VPS数据中心IP、访问httpbin），阈值 < 40
            # 生产环境使用IPDeep代理+真实目标站时风险分=5
            ok = score < 40
            record("风控", "风险评分(沙盒)", ok,
                   f"score={score} (沙盒阈值<40, 生产<15)", time.time() - t0)
            browser.close()
    except Exception as e:
        record("风控", "风险评分(沙盒)", False, str(e)[:100], time.time() - t0)

    # WebRTC 泄露检测
    t0 = time.time()
    try:
        import risk_check
        from selenium_bridge import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            )
            context.add_init_script(risk_check._STEALTH_INIT_SCRIPT)
            page = context.new_page()
            page.goto("https://httpbin.org/html", timeout=20000, wait_until="domcontentloaded")
            # 检测 WebRTC 泄露
            webrtc_leak = page.evaluate("""
                () => {
                    return new Promise((resolve) => {
                        try {
                            const pc = new RTCPeerConnection({iceServers:[]});
                            pc.createDataChannel('');
                            pc.createOffer().then(o => pc.setLocalDescription(o));
                            const ips = [];
                            pc.onicecandidate = (e) => {
                                if (!e.candidate) { resolve(ips); return; }
                                const m = e.candidate.candidate.match(/(\d+\.\d+\.\d+\.\d+)/);
                                if (m && !m[1].startsWith('0.')) ips.push(m[1]);
                            };
                            setTimeout(() => resolve(ips), 3000);
                        } catch(e) { resolve([]); }
                    });
                }
            """)
            leak_count = len(webrtc_leak) if webrtc_leak else 0
            record("风控", "WebRTC泄露检测", leak_count == 0,
                   f"泄露={leak_count}处", time.time() - t0)
            browser.close()
    except Exception as e:
        record("风控", "WebRTC泄露检测", False, str(e)[:100], time.time() - t0)


# ============================================================
# F. 生产准入测试API
# ============================================================
def test_production_api():
    print("\n" + "=" * 60)
    print("F. 生产准入测试API")
    print("=" * 60)

    # 启动 L1 代码检查（最轻量的层）
    t0 = time.time()
    try:
        r = api_post("/start_production_test", {"layers": "code"}, timeout=10)
        data = r.json()
        ok = r.status_code == 200 and data.get("success", False)
        record("准入API", "启动L1代码检查", ok, str(data.get("message", ""))[:60], time.time() - t0)
    except Exception as e:
        record("准入API", "启动L1代码检查", False, str(e), time.time() - t0)
        return

    # 轮询等待完成
    t0 = time.time()
    try:
        final_status = None
        for _ in range(60):  # 最多等60秒
            time.sleep(2)
            r = api_get("/get_production_test_status", timeout=10)
            status = r.json()
            if not status.get("running", True):
                final_status = status
                break

        if final_status:
            layers = final_status.get("layers", {})
            code_layer = layers.get("code", {})
            code_passed = code_layer.get("passed", False)
            record("准入API", "L1代码检查完成", code_passed,
                   f"passed={code_passed}", time.time() - t0)
        else:
            record("准入API", "L1代码检查完成", False, "超时未完成", time.time() - t0)
    except Exception as e:
        record("准入API", "L1代码检查完成", False, str(e), time.time() - t0)


# ============================================================
# G. 日志与辅助接口
# ============================================================
def test_auxiliary_apis():
    print("\n" + "=" * 60)
    print("G. 日志与辅助接口")
    print("=" * 60)

    # 获取日志（返回HTML格式）
    t0 = time.time()
    try:
        r = api_get("/get_logs", timeout=5)
        ok = r.status_code == 200 and len(r.text) > 0
        record("辅助", "获取日志", ok, f"长度={len(r.text)}字符", time.time() - t0)
    except Exception as e:
        record("辅助", "获取日志", False, str(e), time.time() - t0)

    # 指纹统计
    t0 = time.time()
    try:
        r = api_get("/get_fingerprint_stats", timeout=5)
        data = r.json()
        ok = r.status_code == 200 and "fingerprint_stats" in data
        record("辅助", "指纹统计", ok, f"UA数={len(data.get('ua_stats', {}))}", time.time() - t0)
    except Exception as e:
        record("辅助", "指纹统计", False, str(e), time.time() - t0)

    # 全球任务状态
    t0 = time.time()
    try:
        r = api_get("/get_global_task_status", timeout=5)
        data = r.json()
        ok = r.status_code == 200 and "stats" in data
        stats = data.get("stats", {})
        record("辅助", "全球任务状态", ok,
               f"total={stats.get('total',0)}, success={stats.get('success',0)}", time.time() - t0)
    except Exception as e:
        record("辅助", "全球任务状态", False, str(e), time.time() - t0)


# ============================================================
# 主入口
# ============================================================
def main():
    print("=" * 60)
    print(f"  沙盒集成测试  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  目标: {BASE_URL}")
    print("=" * 60)

    # 检查服务是否可达
    try:
        requests.get(f"{BASE_URL}/", timeout=5)
    except Exception as e:
        print(f"\n❌ 无法连接到服务 {BASE_URL}: {e}")
        print("   请确认 selenium_traffic 服务正在运行")
        sys.exit(1)

    test_health()
    test_config()
    test_plan()
    test_task_control()
    test_risk_detection_sandbox()
    test_production_api()
    test_auxiliary_apis()

    # 汇总
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    elapsed_total = time.time() - _start_time

    print("\n" + "=" * 60)
    print(f"  沙盒测试完成: {passed}/{total} 通过, {failed} 失败")
    print(f"  总耗时: {elapsed_total:.1f}s")
    print("=" * 60)

    # 保存报告
    report = {
        "type": "sandbox_test",
        "timestamp": datetime.now().isoformat(),
        "target": BASE_URL,
        "summary": {"total": total, "passed": passed, "failed": failed},
        "elapsed_s": round(elapsed_total, 1),
        "results": results,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORT_DIR, f"sandbox_test_{ts}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
