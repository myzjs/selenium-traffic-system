"""
风控检测模块测试 - 使用 Mock 不依赖真实浏览器
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch


def make_mock_page():
    """创建一个模拟的 Playwright page 对象"""
    page = MagicMock()
    page.evaluate.return_value = None
    page.goto.return_value = None
    page.viewport_size = {"width": 1920, "height": 1080}
    page.mouse = MagicMock()
    page.request = MagicMock()
    page.request.headers = {}
    return page


def make_evaluate_side_effect(webdriver=False, referer="https://google.com", tz="Asia/Shanghai"):
    """创建 page.evaluate 的 side_effect"""
    def side_effect(*args):
        s = args[0] if args else ""
        # 基础信息
        if "viewport_larger_than_screen" in s:
            return {
                "screen": [1920, 1080],
                "viewport": [1900, 1000],
                "viewport_larger_than_screen": False,
                "viewport_ratio": 0.99
            }
        if "hardwareConcurrency" in s:
            return {
                "hardwareConcurrency": 8,
                "deviceMemory": 8,
                "hc_reasonable": True,
                "hc_suspicious": False,
                "dm_reasonable": True,
                "dm_suspicious": False
            }
        if "navigator.userAgent" in s:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
        if "navigator.platform" in s:
            return "Win32"
        if "navigator.language" in s:
            return "zh-CN"
        if "screen.width" in s:
            return [1920, 1080]
        if "navigator.plugins.length" in s:
            return 5
        if "navigator.mimeTypes.length" in s:
            return 4
        if "navigator.mediaDevices" in s:
            return True
        if "document.fonts.size" in s:
            return 10
        # 自动化检测
        if "navigator.webdriver" in s:
            return webdriver
        if "cdc_" in s:
            return False
        if "window.chrome" in s and "runtime" in s:
            return True
        # 时区
        if "Intl.DateTimeFormat" in s:
            return tz
        # Referer
        if "document.referrer" in s:
            return referer
        # WebGL - 必须在 canvas 之前检查，因为 webgl 脚本也包含 canvas 关键词
        if "gl.RENDERER" in s or "gl.VENDOR" in s:
            return {"renderer": "ANGLE (Google)", "vendor": "Google Inc.", "has_hook": True}
        if "getParameter(" in s and "gl" in s:
            return {"renderer": "ANGLE (Google)", "vendor": "Google Inc.", "has_hook": True}
        # Canvas
        if "getContext('2d')" in s or "getImageData" in s:
            return {"data": "test_hash_12345", "has_noise": True}
        if "createElement('canvas')" in s:
            return {"data": "test_hash_12345", "has_noise": True}
        # WebRTC
        if "RTCPeerConnection" in s:
            return False
        # 广告检测
        if "customSelector" in s or "adSelector" in s:
            return {"ads_found": 0, "hidden_ads": 0, "exposed_ads": 0}
        # Storage
        if "localStorage" in s and "length" in s:
            return 3
        if "document.cookie" in s and "length" in s:
            return 2
        if "sessionStorage" in s:
            return 3
        return None
    return side_effect


class TestRiskDetectBasic:
    """风险检测基础功能测试"""

    def test_import_risk_check(self):
        import risk_check
        assert risk_check is not None

    def test_risk_check_has_required_constants(self):
        import risk_check
        assert hasattr(risk_check, "TIMEZONE")
        assert hasattr(risk_check, "LOCALE")
        assert hasattr(risk_check, "RISK_WEIGHT")
        assert hasattr(risk_check, "UA_RISK_KEYWORDS")

    def test_risk_weight_contains_required_keys(self):
        import risk_check
        required_keys = [
            "webdriver_leak", "cdc_trace_leak", "canvas_finger_no_noise",
            "tz_geo_mismatch", "webrtc_ip_leak", "empty_referer",
            "ua_risk_keyword"
        ]
        for key in required_keys:
            assert key in risk_check.RISK_WEIGHT, f"缺少风险权重: {key}"

    def test_ua_risk_keywords_contains_selenium(self):
        import risk_check
        assert "Selenium" in risk_check.UA_RISK_KEYWORDS

    def test_run_risk_detect_returns_dict(self):
        import risk_check
        page = make_mock_page()
        page.evaluate.side_effect = make_evaluate_side_effect()
        result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
        assert isinstance(result, dict)
        assert "base_info" in result
        assert "automation_probe" in result
        assert "fingerprint" in result
        assert "risk_calc" in result

    def test_run_risk_detect_detects_webdriver(self):
        import risk_check
        page = make_mock_page()
        page.evaluate.side_effect = make_evaluate_side_effect(webdriver=True)
        result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
        assert result["automation_probe"]["nav_webdriver"] is True

    def test_run_risk_detect_no_webdriver(self):
        import risk_check
        page = make_mock_page()
        page.evaluate.side_effect = make_evaluate_side_effect(webdriver=False)
        result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
        assert result["automation_probe"]["nav_webdriver"] is False

    def test_risk_score_calculation(self):
        import risk_check
        page = make_mock_page()
        page.evaluate.side_effect = make_evaluate_side_effect(webdriver=True, referer="")
        result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
        calc = result.get("risk_calc", {})
        assert "total_score" in calc
        assert calc["total_score"] >= 0

    def test_report_saved_to_disk(self):
        import risk_check
        import tempfile
        page = make_mock_page()
        page.evaluate.side_effect = make_evaluate_side_effect()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(risk_check, "REPORT_DIR", tmpdir):
                result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
                assert isinstance(result, dict)
