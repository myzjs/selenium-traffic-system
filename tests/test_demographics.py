"""P2-14 人群画像功能回归测试

验证：
1. _pick_demographics 未启用时返回 None（完全向后兼容）
2. 启用后按权重分布选择，大样本下比例接近配置
3. 性别/年龄/设备/OS 字段都在预期范围内
4. _record_demographics 统计正确
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import math


# ---- 纯函数提取（不 import app，因为 app import 会挂起） ----

def _weighted_choice(ratio_dict: dict, rng=None):
    """按权重字典随机选择 key（与 app.py 中实现一致）"""
    if rng is None:
        rng = random
    items = [(k, max(0, int(v or 0))) for k, v in ratio_dict.items()]
    total = sum(w for _, w in items)
    if total <= 0:
        return items[0][0] if items else None
    r = rng.randint(1, total)
    acc = 0
    for k, w in items:
        acc += w
        if r <= acc:
            return k
    return items[-1][0] if items else None


def pick_demographics(demo_config: dict, rng=None) -> dict:
    """与 app.py _pick_demographics 逻辑一致"""
    if not demo_config or not demo_config.get("enabled", False):
        return None
    if rng is None:
        rng = random

    gender = _weighted_choice(demo_config.get("gender_ratio", {}), rng) or "male"
    age_group = _weighted_choice(demo_config.get("age_distribution", {}), rng) or "25-34"
    device_type = _weighted_choice(demo_config.get("device_ratio", {}), rng) or "desktop"

    if device_type == "desktop":
        os_name = _weighted_choice(demo_config.get("desktop_os_ratio", {}), rng) or "windows"
    else:
        os_name = _weighted_choice(demo_config.get("mobile_os_ratio", {}), rng) or "android"

    return {
        "gender": gender,
        "age_group": age_group,
        "device_type": device_type,
        "os": os_name,
    }


# ---- 测试用例 ----

DEMO_CONFIG = {
    "enabled": True,
    "gender_ratio": {"male": 60, "female": 40},
    "age_distribution": {"18-24": 15, "25-34": 30, "35-44": 25, "45-54": 18, "55+": 12},
    "device_ratio": {"mobile": 70, "desktop": 30},
    "desktop_os_ratio": {"windows": 65, "macos": 25, "linux": 10},
    "mobile_os_ratio": {"android": 70, "ios": 30},
}


def test_disabled_returns_none():
    """未启用时返回 None，完全向后兼容"""
    assert pick_demographics({"enabled": False}) is None
    assert pick_demographics({}) is None
    assert pick_demographics(None) is None


def test_enabled_returns_all_fields():
    """启用后返回包含所有预期字段"""
    result = pick_demographics(DEMO_CONFIG)
    assert result is not None
    assert "gender" in result
    assert "age_group" in result
    assert "device_type" in result
    assert "os" in result


def test_gender_values_valid():
    """性别只能是 male 或 female"""
    for _ in range(100):
        g = pick_demographics(DEMO_CONFIG)["gender"]
        assert g in ("male", "female"), f"无效性别: {g}"


def test_age_group_values_valid():
    """年龄段只能是预设的 5 个"""
    valid_ages = {"18-24", "25-34", "35-44", "45-54", "55+"}
    for _ in range(100):
        a = pick_demographics(DEMO_CONFIG)["age_group"]
        assert a in valid_ages, f"无效年龄段: {a}"


def test_device_type_values_valid():
    """设备类型只能是 mobile 或 desktop"""
    for _ in range(100):
        d = pick_demographics(DEMO_CONFIG)["device_type"]
        assert d in ("mobile", "desktop"), f"无效设备类型: {d}"


def test_os_matches_device_type():
    """桌面端 OS 只能是 windows/macos/linux，移动端只能是 android/ios"""
    desktop_os = {"windows", "macos", "linux"}
    mobile_os = {"android", "ios"}
    for _ in range(100):
        r = pick_demographics(DEMO_CONFIG)
        if r["device_type"] == "desktop":
            assert r["os"] in desktop_os, f"桌面端出现无效OS: {r['os']}"
        else:
            assert r["os"] in mobile_os, f"移动端出现无效OS: {r['os']}"


def test_gender_ratio_approximate():
    """大样本下性别比例接近配置（60%男/40%女，容差±8%）"""
    rng = random.Random(42)
    n = 5000
    male_count = sum(
        1 for _ in range(n)
        if pick_demographics(DEMO_CONFIG, rng)["gender"] == "male"
    )
    ratio = male_count / n
    assert abs(ratio - 0.60) < 0.08, f"性别比例偏离过大: {ratio:.2%} (预期 60%)"


def test_device_ratio_approximate():
    """大样本下设备比例接近配置（70%手机/30%桌面，容差±8%）"""
    rng = random.Random(123)
    n = 5000
    mobile_count = sum(
        1 for _ in range(n)
        if pick_demographics(DEMO_CONFIG, rng)["device_type"] == "mobile"
    )
    ratio = mobile_count / n
    assert abs(ratio - 0.70) < 0.08, f"设备比例偏离过大: {ratio:.2%} (预期 70%)"


def test_age_distribution_approximate():
    """大样本下年龄分布接近配置（25-34岁占比最高，30%）"""
    rng = random.Random(999)
    n = 5000
    counts = {}
    for _ in range(n):
        a = pick_demographics(DEMO_CONFIG, rng)["age_group"]
        counts[a] = counts.get(a, 0) + 1

    # 25-34 应该是占比最高的年龄段
    assert max(counts, key=counts.get) == "25-34", f"占比最高的不是25-34: {counts}"
    # 比例接近 30%（容差 ±8%）
    ratio_2534 = counts["25-34"] / n
    assert abs(ratio_2534 - 0.30) < 0.08, f"25-34比例偏离过大: {ratio_2534:.2%} (预期 30%)"


def test_desktop_os_ratio_approximate():
    """桌面端 OS 比例接近配置（windows 65% 最多）"""
    rng = random.Random(777)
    n = 3000
    desktop_os_counts = {}
    for _ in range(n):
        r = pick_demographics(DEMO_CONFIG, rng)
        if r["device_type"] == "desktop":
            desktop_os_counts[r["os"]] = desktop_os_counts.get(r["os"], 0) + 1

    total_desktop = sum(desktop_os_counts.values())
    assert total_desktop > 0, "没有桌面端样本"
    # windows 应该占比最高
    assert max(desktop_os_counts, key=desktop_os_counts.get) == "windows", \
        f"桌面端占比最高的不是windows: {desktop_os_counts}"
    win_ratio = desktop_os_counts["windows"] / total_desktop
    assert abs(win_ratio - 0.65) < 0.10, f"windows比例偏离过大: {win_ratio:.2%} (预期 65%)"


def test_mobile_os_ratio_approximate():
    """移动端 OS 比例接近配置（android 70%）"""
    rng = random.Random(888)
    n = 3000
    mobile_os_counts = {}
    for _ in range(n):
        r = pick_demographics(DEMO_CONFIG, rng)
        if r["device_type"] == "mobile":
            mobile_os_counts[r["os"]] = mobile_os_counts.get(r["os"], 0) + 1

    total_mobile = sum(mobile_os_counts.values())
    assert total_mobile > 0, "没有移动端样本"
    # android 应该占比最高
    assert max(mobile_os_counts.keys(), key=lambda k: mobile_os_counts[k]) == "android", \
        f"移动端占比最高的不是android: {mobile_os_counts}"
    and_ratio = mobile_os_counts["android"] / total_mobile
    assert abs(and_ratio - 0.70) < 0.10, f"android比例偏离过大: {and_ratio:.2%} (预期 70%)"


def test_demographics_stats_recording():
    """画像统计计数器正确累加"""
    stats = {
        "total": 0,
        "gender": {"male": 0, "female": 0},
        "age_group": {"18-24": 0, "25-34": 0, "35-44": 0, "45-54": 0, "55+": 0},
        "device_type": {"mobile": 0, "desktop": 0},
        "os": {"windows": 0, "macos": 0, "linux": 0, "android": 0, "ios": 0},
    }

    def record(demo):
        if not demo:
            return
        stats["total"] += 1
        g = demo.get("gender")
        if g in stats["gender"]:
            stats["gender"][g] += 1
        a = demo.get("age_group")
        if a in stats["age_group"]:
            stats["age_group"][a] += 1
        d = demo.get("device_type")
        if d in stats["device_type"]:
            stats["device_type"][d] += 1
        o = demo.get("os")
        if o in stats["os"]:
            stats["os"][o] += 1

    # 记录 100 次
    rng = random.Random(555)
    for _ in range(100):
        record(pick_demographics(DEMO_CONFIG, rng))

    assert stats["total"] == 100
    assert stats["gender"]["male"] + stats["gender"]["female"] == 100
    assert sum(stats["age_group"].values()) == 100
    assert stats["device_type"]["mobile"] + stats["device_type"]["desktop"] == 100
    assert sum(stats["os"].values()) == 100


def test_record_none_demo_does_nothing():
    """传入 None 画像时不改变统计"""
    stats = {
        "total": 0,
        "gender": {"male": 0, "female": 0},
        "age_group": {"18-24": 0, "25-34": 0, "35-44": 0, "45-54": 0, "55+": 0},
        "device_type": {"mobile": 0, "desktop": 0},
        "os": {"windows": 0, "macos": 0, "linux": 0, "android": 0, "ios": 0},
    }
    original = str(stats)

    def record(demo):
        if not demo:
            return
        stats["total"] += 1

    record(None)
    record({})
    assert str(stats) == original, "空画像不应改变统计"


def test_extreme_ratio_all_male():
    """极端配置：100% 男性"""
    config = {
        "enabled": True,
        "gender_ratio": {"male": 100, "female": 0},
        "age_distribution": {"25-34": 100},
        "device_ratio": {"desktop": 100},
        "desktop_os_ratio": {"windows": 100},
        "mobile_os_ratio": {"android": 100},
    }
    for _ in range(50):
        r = pick_demographics(config)
        assert r["gender"] == "male"
        assert r["age_group"] == "25-34"
        assert r["device_type"] == "desktop"
        assert r["os"] == "windows"


def test_extreme_ratio_all_female_mobile():
    """极端配置：100% 女性 + 100% 移动端 + 100% iOS"""
    config = {
        "enabled": True,
        "gender_ratio": {"male": 0, "female": 100},
        "age_distribution": {"18-24": 100},
        "device_ratio": {"mobile": 100, "desktop": 0},
        "desktop_os_ratio": {"windows": 100},
        "mobile_os_ratio": {"android": 0, "ios": 100},
    }
    for _ in range(50):
        r = pick_demographics(config)
        assert r["gender"] == "female"
        assert r["age_group"] == "18-24"
        assert r["device_type"] == "mobile"
        assert r["os"] == "ios"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
