#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极限风控测试套件 — 针对 selenium_traffic_system 的 P0/P1 边界场景
运行方式: python3 extreme_risk_test.py   (在 VPS 上 app.py 同目录执行)
仅做纯函数级测试，不需要真实浏览器；Watchdog 测试用 FakePage 模拟。
"""
import time
import random
import threading
import sys
import os
import json

PASS = 0
FAIL = 0
RESULTS = []

def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        mark = "PASS"
    else:
        FAIL += 1
        mark = "FAIL"
    RESULTS.append((mark, name, detail))
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))

def expect_throws(fn, exc_type, name):
    try:
        fn()
        report(name, False, "未抛出异常")
    except exc_type:
        report(name, True)
    except Exception as e:
        report(name, False, f"抛出错误类型 {type(e).__name__}: {e}")

# ========== 导入 app.py（不触发 Flask run） ==========
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A

print("=" * 70)
print(f"极限风控测试开始 | app 版本: {A.APP_VERSION}")
print("=" * 70)

# =========================================================
# Group A: 保险绳看门狗 (P0 致命)
# =========================================================
print("\n──── Group A: 保险绳看门狗竞态 ────")

class FakePage:
    """模拟 Playwright Page 最小接口，记录 close 调用"""
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.context = type("Ctx", (), {"options": {"user_agent": "Mozilla/5.0 Chrome/120"}})()
        self._url = "https://fake.example.com"
    def close(self):
        self.closed = True
        self.close_calls += 1
    def url(self):
        return self._url
    def evaluate(self, *a, **k):
        return None
    def query_selector(self, *a, **k):
        return None
    def query_selector_all(self, *a, **k):
        return []
    def goto(self, *a, **k):
        return None
    def title(self):
        return ""
    def mouse(self, *a, **k):
        return self
    def keyboard(self, *a, **k):
        return self

# A1: 正常完成竞态 — deadline 前 cancel，不得触发 page.close
def test_a1():
    fp = FakePage()
    deadline = time.time() + 5
    A._rope_watchdog_event.clear()
    A._start_rope_watchdog(deadline, fp, label="test-a1")
    A._cancel_rope_watchdog()
    time.sleep(0.2)
    ok = (fp.close_calls == 0) and (A._rope_watchdog_fired is False)
    report("A1 正常完成竞态(watchdog被cancel)", ok,
           f"close_calls={fp.close_calls} fired={A._rope_watchdog_fired}")
test_a1()

# A2: deadline 触发 — 强制 page.close 中断冻结调用
def test_a2():
    fp = FakePage()
    A._rope_watchdog_event.clear()
    A._start_rope_watchdog(time.time() + 0.5, fp, label="test-a2")
    time.sleep(1.2)  # 等 watchdog 触发
    ok = (fp.close_calls >= 1)
    report("A2 deadline到达强制close page", ok,
           f"close_calls={fp.close_calls} fired={A._rope_watchdog_fired}")
test_a2()

# A3: deadline 已过期时启动 watchdog — 应立即返回不误关 page
def test_a3():
    fp = FakePage()
    A._rope_watchdog_event.clear()
    A._start_rope_watchdog(time.time() - 10, fp, label="test-a3")
    time.sleep(0.3)
    ok = (fp.close_calls == 0)
    report("A3 deadline已过期启动watchdog不误关page", ok,
           f"close_calls={fp.close_calls}")
test_a3()

# A4: 多次连续 start 不 cancel — 不应崩溃
def test_a4():
    fp = FakePage()
    A._rope_watchdog_event.clear()
    for i in range(5):
        A._start_rope_watchdog(time.time() + 10, fp, label=f"test-a4-{i}")
    time.sleep(0.3)
    ok = (fp.close_calls == 0)
    report("A4 连续5次start watchdog无崩溃", ok, f"close_calls={fp.close_calls}")
    A._cancel_rope_watchdog()
test_a4()

# A5: task_running 全局标志在 watchdog 触发后被置 False
def test_a5():
    fp = FakePage()
    A._rope_watchdog_event.clear()
    old = getattr(A, 'task_running', True)
    A.task_running = True
    A._start_rope_watchdog(time.time() + 0.5, fp, label="test-a5")
    time.sleep(1.2)
    ok = (getattr(A, 'task_running', None) is False)
    report("A5 watchdog触发后task_running置False", ok,
           f"task_running={getattr(A,'task_running',None)}")
    A.task_running = old
test_a5()

# =========================================================
# Group B: 函数级边界 (P1)
# =========================================================
print("\n──── Group B: 函数级边界 ────")

# B1: simulate_human_in_window duration=0 — 应立即返回
def test_b1():
    stats = {}
    cfg = {"scroll_pixels": {"min": 100, "max": 800}}
    t0 = time.time()
    x, y = A.simulate_human_in_window(FakePage(), 0, stats, 10, 10, cfg, page_name="B1")
    dt = time.time() - t0
    ok = (dt < 2.0) and (x == 10 and y == 10)
    report("B1 simulate_human duration=0 立即返回", ok, f"耗时={dt:.2f}s")
test_b1()

# B2: simulate_human_in_window deadline 已过期 — 应立即返回
def test_b2():
    stats = {}
    cfg = {"scroll_pixels": {"min": 100, "max": 800}}
    t0 = time.time()
    x, y = A.simulate_human_in_window(FakePage(), 60, stats, 10, 10, cfg,
                                      page_name="B2", deadline=time.time() - 5)
    dt = time.time() - t0
    ok = (dt < 2.0)
    report("B2 simulate_human deadline已过期立即返回", ok, f"耗时={dt:.2f}s")
test_b2()

# B3: simulate_rtt_jitter base_ms=0 / jitter_ms=0 — 不崩溃且延迟有限
def test_b3():
    t0 = time.time()
    try:
        rtt = A.simulate_rtt_jitter(base_ms=0, jitter_ms=0)
        dt = time.time() - t0
        ok = (dt < 2.0) and (5 <= rtt <= 500)
        report("B3 simulate_rtt_jitter base=0/jitter=0", ok,
               f"rtt={rtt:.1f}ms 耗时={dt:.2f}s")
    except Exception as e:
        report("B3 simulate_rtt_jitter base=0/jitter=0", False, f"异常: {e}")
test_b3()

# B4: daily_ad_click_limit {min:0, max:0} — 返回 False(不限)
def test_b4():
    old_cfg = A.config.get("daily_ad_click_limit")
    A.config["daily_ad_click_limit"] = {"min": 0, "max": 0}
    try:
        ok = (A.daily_ad_click_limit_reached() is False)
        report("B4 daily_ad_click_limit 0/0=不限", ok)
    finally:
        if old_cfg is not None:
            A.config["daily_ad_click_limit"] = old_cfg
        else:
            A.config.pop("daily_ad_click_limit", None)
test_b4()

# B5: click_link_with_fallback task_deadline=None — 不崩溃
def test_b5():
    fp = FakePage()
    ok = True
    try:
        res = A.click_link_with_fallback(fp, [], [], 10, 10, {}, task_deadline=None)
        ok = isinstance(res, tuple) and len(res) == 3
        report("B5 click_link_with_fallback deadline=None 不崩溃", ok, f"返回={res}")
    except Exception as e:
        report("B5 click_link_with_fallback deadline=None 不崩溃", False, f"异常: {e}")
test_b5()

# B6: click_link_with_fallback deadline 已过期 — 应立即返回 False
def test_b6():
    fp = FakePage()
    try:
        res = A.click_link_with_fallback(fp, ["home"], [], 10, 10, {},
                                         task_deadline=time.time() - 5)
        ok = (res[0] is False)
        report("B6 click_link_with_fallback deadline已过期返回False", ok, f"返回={res[0]}")
    except Exception as e:
        report("B6 click_link_with_fallback deadline已过期返回False", False, f"异常: {e}")
test_b6()

# B7: click_link_containing_text deadline 已过期 — 应立即返回
def test_b7():
    fp = FakePage()
    try:
        res = A.click_link_containing_text(fp, ["home"], 10, 10, {},
                                           task_deadline=time.time() - 5)
        ok = (res[0] is False)
        report("B7 click_link_containing_text deadline已过期返回False", ok, f"返回={res[0]}")
    except Exception as e:
        report("B7 click_link_containing_text deadline已过期返回False", False, f"异常: {e}")
test_b7()

# =========================================================
# Group C: 反检测 & CAPTCHA 关键词覆盖 (P1)
# =========================================================
print("\n──── Group C: 反检测 & CAPTCHA 覆盖 ────")

# C1: CAPTCHA 关键词在多种语言的标题下是否命中
def test_c1():
    keywords = ["captcha", "recaptcha", "unusual traffic", "not a robot",
                "验证", "人机验证", "安全检测", "robot", "blocked",
                "sorry", "access denied", "403",
                "アクセス", "拒否", "ロボット", "再試行", "bot",
                "접근", "차단", "사람", "로봇",
                "доступ", "запрещ", "робот", "капч", "проверк"]
    titles = {
        "Google CAPTCHA": "Recaptcha - unusual traffic",
        "中文验证": "人机验证 - 安全检测",
        "日语验证": "アクセスが拒否されました",
        "日文robot": "ロボットによるアクセスです",
        "韩语验证": "사람이 아닙니다",
        "韩文阻止": "접근이 차단되었습니다",
        "俄语验证": "Доступ запрещен",
        "俄文capcha": "Подтвердите что вы не робот",
    }
    miss = [k for k, t in titles.items() if not any(w in t.lower() for w in keywords)]
    report("C1 CAPTCHA关键词覆盖(中英日韩俄)", not miss, f"未覆盖: {miss or '无'}")
test_c1()

# =========================================================
# Group D: 死锁风险 — 心跳监督 & interruptible_sleep (P0)
# =========================================================
print("\n──── Group D: 死锁/卡死防护 ────")

# D1: interruptible_sleep 在 task_running=False 时被中断
def test_d1():
    old = A.task_running
    A.task_running = False
    t0 = time.time()
    res = A.interruptible_sleep(10)
    dt = time.time() - t0
    A.task_running = old
    ok = (res is False) and (dt < 1.0)
    report("D1 interruptible_sleep 停止时立即中断", ok, f"耗时={dt:.2f}s")
test_d1()

# D2: interruptible_sleep 正常完成返回 True
def test_d2():
    old = A.task_running
    A.task_running = True
    t0 = time.time()
    res = A.interruptible_sleep(0.1)
    dt = time.time() - t0
    A.task_running = old
    ok = (res is True) and (dt < 1.0)
    report("D2 interruptible_sleep 正常完成", ok, f"耗时={dt:.2f}s")
test_d2()

# =========================================================
# Group E: 全局自杀看门狗 (P0)
# =========================================================
print("\n──── Group E: 全局自杀看门狗 ────")

# E1: 启动后取消 — 不触发 os._exit
def test_e1():
    # 直接测 Timer 逻辑（不调 os._exit）
    fired = []
    def fake_suicide():
        fired.append(1)
    t = threading.Timer(interval=0.5, function=fake_suicide)
    t.daemon = True
    t.start()
    t.cancel()
    time.sleep(0.8)
    ok = (len(fired) == 0)
    report("E1 全局watchdog cancel后不触发", ok, f"fired={fired}")
test_e1()

# =========================================================
# Group G: 本轮 v3.6.11 修复回归 (H1/M2/M3)
# =========================================================
print("\n──── Group G: v3.6.11 修复回归 (IP过滤/关键词边界/守卫泄漏) ────")

# G1: H1 — is_ip_safe_for_hilltopads 真正读取 ip_type/isp/asn
def test_g1():
    # 机房 IP: ip_type=datacenter 应被拒绝
    r1 = A._rce is not None and False  # 占位（popunder_trigger 通过 app 导入方式不同）
    # 直接从 popunder_trigger 模块测
    import importlib
    try:
        pt = importlib.import_module("popunder_trigger")
        ok1 = pt.is_ip_safe_for_hilltopads({"ip_type": "datacenter", "isp": "", "asn": ""}) is False
        ok2 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "DigitalOcean LLC", "asn": ""}) is False
        ok3 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "Comcast Cable", "asn": ""}) is True
        ok4 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "Microsoft Network", "asn": ""}) is True
        # "Tencent Cloud" 含托管特征词 cloud → 应拒绝（品牌名不再单独判定，但 cloud 托管特征仍拦）
        ok5 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "Tencent Cloud", "asn": ""}) is False
        # "Comcast Cloud Backup" 含 cloud 子串 → 住宅ISP误伤风险验证：词边界仍会命中 cloud...
        # 用纯住宅名验证：不含任何托管词
        ok6 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "Verizon Fios", "asn": ""}) is True
        # Bug#5 兜底: 三要素全空=不可判断 → 拒绝
        ok7 = pt.is_ip_safe_for_hilltopads({"ip_type": "", "isp": "", "asn": ""}) is False
        # Bug#5 兜底: resolved_ip_info=None → 拒绝
        ok8 = pt.is_ip_safe_for_hilltopads(None) is False
        report("G1 is_ip_safe IP质量过滤(H1+M2+Bug#5)", ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8,
               f"datacenter={ok1} digitalocean={ok2} comcast={ok3} msnetwork={ok4} tencent={ok5} verizon={ok6} empty={ok7} none={ok8}")
    except Exception as e:
        report("G1 is_ip_safe IP质量过滤(H1+M2)", False, f"导入/调用异常: {e}")
test_g1()

# G2: M3 — 概率跳过不占位，失败路径清理守卫
def test_g2():
    import importlib
    pt = importlib.import_module("popunder_trigger")
    pid = id(object())  # 随机对象 id 模拟 page_id
    # 只读检查不占位
    c1 = pt._is_page_in_cooldown(pid, 90.0)  # 应为 False（无记录）
    # 占位
    ok = pt._check_page_reentry(pid, 90.0)  # 应为 True
    c2 = pt._is_page_in_cooldown(pid, 90.0)  # 现在应为 True（已占位）
    # 清理
    pt._cleanup_page_triggers(pid)
    c3 = pt._is_page_in_cooldown(pid, 90.0)  # 清理后应为 False
    report("G2 页面守卫check/canary/cleanup(M3)", (c1 is False) and ok and c2 and (c3 is False),
           f"before={c1} reserve={ok} after={c2} cleaned={c3}")
test_g2()

# G3: M1 — _LAST_POPUNDER_TS 锁存在且可读写
def test_g3():
    import importlib
    pt = importlib.import_module("popunder_trigger")
    ok = hasattr(pt, "_LAST_POPUNDER_LOCK") and hasattr(pt, "_LAST_POPUNDER_TS")
    report("G3 _LAST_POPUNDER_TS 锁保护存在(M1)", ok)
test_g3()

# G4: H1 — resolve_ip_info 返回结构含 isp/asn/ip_type 键
def test_g4():
    import importlib
    ipr = importlib.import_module("ip_info_resolver")
    r = ipr.resolve_ip_info("8.8.8.8", proxy_ip_info={"country": "US", "timezone": "America/New_York", "language": "en-US"})
    ok = all(k in r for k in ("isp", "asn", "ip_type"))
    report("G4 resolve_ip_info 透传 isp/asn/ip_type 键(H1)", ok, f"keys={list(r.keys())}")
test_g4()

print("\n" + "=" * 70)
print(f"测试完成: PASS={PASS} FAIL={FAIL}")
print("=" * 70)
if FAIL > 0:
    print("\n失败项:")
    for mark, name, detail in RESULTS:
        if mark == "FAIL":
            print(f"  [FAIL] {name} -- {detail}")
    sys.exit(1)
else:
    sys.exit(0)
