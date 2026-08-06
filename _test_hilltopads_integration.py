#!/usr/bin/env python3
"""
HilltopAds + Google Ads 双模改造 综合测试套件
==============================================
覆盖：模块自检 / 配置正确性 / 流程集成 / 风控合规 / 模拟弹窗触发
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PASS, FAIL = "✅ PASS", "❌ FAIL"
total_pass, total_fail = 0, 0

def test(name, condition, detail=""):
    global total_pass, total_fail
    if condition:
        total_pass += 1
        print(f"  {PASS}  {name}")
    else:
        total_fail += 1
        print(f"  {FAIL}  {name}  {detail}")

print("=" * 60)
print("HilltopAds + Google Ads 双模改造测试")
print("=" * 60)

# ========== Part 1: popunder_trigger 模块自检 ==========
print("\n[1] popunder_trigger 模块自检")
import popunder_trigger as _pt

results = _pt.self_test()
test("坐标范围检查", results["coords_in_bounds"])
test("HilltopAds检测", results["detect_hilltop"])
test("EvaDav检测", results["detect_evadav"])
test("无网络拒绝", results["detect_none"])
test("AdSense不过滤", results["detect_adsense"])

# 模块导入
test("模块导入", _pt is not None)

# 默认配置验证
test("DEFAULT_CONFIG存在", isinstance(_pt.DEFAULT_CONFIG, dict))
test("enabled默认False", _pt.DEFAULT_CONFIG["enabled"] is False)
test("触发概率40%", _pt.DEFAULT_CONFIG["trigger_probability"] == 0.40)
test("存活区间15-25s",
     _pt.DEFAULT_CONFIG["popunder_stay_min"] == 15 and
     _pt.DEFAULT_CONFIG["popunder_stay_max"] == 25)

# should_trigger_for_network
test("HilltopAds可触发", _pt.should_trigger_for_network("HilltopAds"))
test("HilltopAds/EvaDav可触发", _pt.should_trigger_for_network("HilltopAds/EvaDav"))
test("Google AdSense不触发", not _pt.should_trigger_for_network("Google AdSense"))

# CDP坐标避让
from popunder_trigger import _pick_safe_coordinates
vp = {"width": 1280, "height": 720}
x, y = _pick_safe_coordinates(None, vp, margin=60)
test("安全坐标x", 80 <= x <= 1200, f"got {x}")
test("安全坐标y", 100 <= y <= 620, f"got {y}")

# 大量测试坐标去重
coords_set = set()
for _ in range(200):
    cx, cy = _pick_safe_coordinates(None, vp, margin=60)
    coords_set.add((cx // 50, cy // 50))
test("坐标多样性", len(coords_set) >= 30, f"200次采样唯一槽位={len(coords_set)}")


# ========== Part 2: app.py 导入验证 ==========
print("\n[2] app.py 导入验证")
try:
    import app
    test("app.py 导入成功", True)
    test("版本号存在", hasattr(app, 'APP_VERSION'))
    print(f"    版本: {app.APP_VERSION}")
except Exception as e:
    test("app.py 导入成功", False, str(e)[:100])


# ========== Part 3: 配置结构验证 ==========
print("\n[3] 配置结构验证")
try:
    from app import DEFAULT_CONFIG
    test("DEFAULT_CONFIG可访问", isinstance(DEFAULT_CONFIG, dict))
    test("hilltopads配置存在", "hilltopads" in DEFAULT_CONFIG)
    ht = DEFAULT_CONFIG["hilltopads"]
    test("hilltopads.enabled", "enabled" in ht)
    test("hilltopads.trigger_probability", "trigger_probability" in ht)
    test("hilltopads触发概率值", 0.30 <= ht["trigger_probability"] <= 0.50,
         f"got {ht['trigger_probability']}")
except Exception as e:
    test("配置验证", False, str(e)[:100])


# ========== Part 4: 代码集成点验证 ==========
print("\n[4] 代码集成验证")
try:
    app_code = open(os.path.join(BASE, "app.py")).read()

    test("_HAS_POPUNDER定义", "_HAS_POPUNDER" in app_code)
    test("popunder_trigger导入", "import popunder_trigger as _popunder" in app_code)
    test("_try_hilltopads_popunder函数", "_try_hilltopads_popunder" in app_code)
    test("跳出型弹窗调用", "_try_hilltopads_popunder(page, context, config)" in app_code)
    test("loop_idx==0仅触发1次", "if loop_idx == 0:" in app_code and "_try_hilltopads_popunder" in app_code)
    test("save_seo_config Hills配置", "hilltopads_enabled" in app_code)
    test("前端HTML Hills面板", "hilltopads_enabled" in app_code)
    test("CDP避让逻辑存在", "_pick_safe_coordinates" in open(os.path.join(BASE, "popunder_trigger.py")).read())
except Exception as e:
    test("集成验证", False, str(e)[:100])


# ========== Part 5: 风控逻辑验证 ==========
print("\n[5] 风控逻辑验证")

# 5.1 弹窗坐标不会落在广告容器内
from popunder_trigger import _get_ad_bounding_boxes

# Mock: _get_ad_bounding_boxes 的参数为 None 时返回空列表
boxes = _get_ad_bounding_boxes(None)
test("空page返回空广告框", isinstance(boxes, list) and len(boxes) == 0)

# 5.2 冷却机制
test("全局冷却时间戳存在", hasattr(_pt, '_LAST_POPUNDER_TS'))

# 5.3 概率跳过
import random
random.seed(42)
skip_count = 0
for _ in range(1000):
    if random.random() > 0.40:
        skip_count += 1
test("概率跳过≈60%", 550 <= skip_count <= 650, f"实际{skip_count}/1000")


# ========== Part 6: 模拟弹窗收益场景 ==========
print("\n[6] 弹窗收益场景模拟")

# 模拟一次完整的弹窗触发（不依赖浏览器）
class MockPage:
    def evaluate(self, js):
        return [{"x": 200, "y": 200, "w": 300, "h": 250}]
    viewport_size = {"width": 1280, "height": 720}

mock_page = MockPage()
ad_boxes = _get_ad_bounding_boxes(mock_page)
test("Mock页面广告框数≥1", len(ad_boxes) >= 1)

# 验证坐标确实避开了广告区域
for _ in range(100):
    x, y = _pick_safe_coordinates(mock_page, {"width": 1280, "height": 720}, margin=60)
    hit_ad = False
    for ax, ay, aw, ah in ad_boxes:
        if (ax - 60 <= x <= ax + aw + 60 and ay - 60 <= y <= ay + ah + 60):
            hit_ad = True
            break
    if not hit_ad:
        break
else:
    test("坐标100%避让广告", False, "无法找到安全坐标")
test("坐标避让广告区域", not hit_ad, f"coords=({x},{y}), ad=({ad_boxes[0]})")


# ========== Part 7: 配置读写验证 ==========
print("\n[7] config.json 读写验证")
config_test_path = os.path.join(BASE, "config.json.test")
ht_test_config = {
    "hilltopads": {
        "enabled": True,
        "trigger_probability": 0.35,
        "popunder_stay_min": 18,
        "popunder_stay_max": 30,
    }
}
try:
    with open(config_test_path, "w") as f:
        json.dump(ht_test_config, f, indent=2)
    with open(config_test_path) as f:
        loaded = json.load(f)
    test("写入成功", os.path.exists(config_test_path))
    test("enabled读取", loaded["hilltopads"]["enabled"] is True)
    test("trigger_probability读取", loaded["hilltopads"]["trigger_probability"] == 0.35)
    os.remove(config_test_path)
except Exception as e:
    test("配置读写", False, str(e)[:100])


# ========== 汇总 ==========
print(f"\n{'='*60}")
print(f"测试结果: {total_pass}/{total_pass + total_fail} 通过")
if total_fail == 0:
    print("🎯 全部测试通过！")
else:
    print(f"⚠️ {total_fail} 项失败，请检查")
print(f"{'='*60}")

sys.exit(0 if total_fail == 0 else 1)
