"""
P2-13 / P2-15 验证测试：
  1. build_sensor_dynamic_script 对同 seed 输出稳定、含 levelchange 和 devicemotion 关键字
  2. 脚本字符串非空、包含 addEventListener 维护逻辑

注意：app.py import 会挂起（预先存在），本测试不 import app，只测 risk_control_enhancements。
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from risk_control_enhancements import build_sensor_dynamic_script


class TestBuildSensorDynamicScript:
    def test_output_non_empty(self):
        js = build_sensor_dynamic_script(42)
        assert isinstance(js, str)
        assert len(js) > 1000

    def test_same_seed_stable(self):
        a = build_sensor_dynamic_script(12345)
        b = build_sensor_dynamic_script(12345)
        assert a == b, "同 seed 输出必须完全一致"

    def test_different_seed_different(self):
        a = build_sensor_dynamic_script(1)
        b = build_sensor_dynamic_script(9999)
        assert a != b, "不同 seed 输出应不同"

    def test_contains_levelchange(self):
        js = build_sensor_dynamic_script(7)
        assert "levelchange" in js
        assert "chargingchange" in js

    def test_contains_devicemotion(self):
        js = build_sensor_dynamic_script(7)
        assert "devicemotion" in js
        assert "DeviceMotionEvent" in js

    def test_contains_add_event_listener_logic(self):
        js = build_sensor_dynamic_script(7)
        # 必须有 addEventListener 函数定义（维护列表）
        assert "addEventListener" in js
        assert "removeEventListener" in js
        # 必须有 _listeners 维护逻辑
        assert "_listeners" in js
        # 必须有 dispatchEvent 派发
        assert "dispatchEvent" in js

    def test_contains_battery_polyfill(self):
        js = build_sensor_dynamic_script(7)
        assert "navigator.getBattery" in js
        assert "Promise.resolve" in js

    def test_contains_motion_interval(self):
        js = build_sensor_dynamic_script(7)
        assert "setInterval" in js
        assert "accelerationIncludingGravity" in js

    def test_contains_random_walk(self):
        js = build_sensor_dynamic_script(7)
        # 随机游走关键词
        assert "_bx" in js
        assert "_by" in js
        assert "_bz" in js
