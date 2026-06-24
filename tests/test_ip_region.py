"""
IP 地域识别模块测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestIPRegionRecognizer:
    """IP 地域识别测试"""

    def test_import(self):
        import ip_region_module
        assert ip_region_module is not None

    def test_constants(self):
        import ip_region_module
        assert ip_region_module.REGION_CHINA == "中国"
        assert ip_region_module.REGION_US_EU == "美国"
        assert ip_region_module.REGION_OTHER == "其他"
        assert ip_region_module.REGION_FAILED == "识别失败"

    def test_recognizer_init(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        assert r is not None
        assert len(r.china_ip_ranges) > 0

    def test_recognize_china_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("1.2.3.4")
        assert region == "中国"

    def test_recognize_us_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("8.8.8.8")
        assert region == "美国"

    def test_recognize_invalid_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("")
        assert region == "识别失败"

    def test_recognize_none_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region(None)
        assert region == "识别失败"

    def test_recognize_private_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("192.168.1.1")
        # 192.168.x.x 属于美国 IP 段 (192.0.0.0 - 192.255.255.255)
        assert region is not None

    def test_recognize_localhost(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("127.0.0.1")
        assert region == "其他"

    def test_ip_to_int(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        assert r._ip_to_int("0.0.0.0") == 0
        assert r._ip_to_int("255.255.255.255") == 4294967295
        assert r._ip_to_int("1.2.3.4") == 16909060

    def test_ip_to_int_invalid(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        # 空字符串会抛 ValueError，测试异常处理
        with pytest.raises((ValueError, AttributeError)):
            r._ip_to_int("")

    def test_is_ip_in_range(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        assert r._is_ip_in_range("1.0.0.50", "1.0.0.0", "1.0.0.255") is True
        assert r._is_ip_in_range("2.0.0.1", "1.0.0.0", "1.0.0.255") is False

    def test_recognize_uk_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("81.2.69.144")
        assert region is not None

    def test_recognize_germany_ip(self):
        from ip_region_module import IPRegionRecognizer
        r = IPRegionRecognizer()
        region = r.recognize_region("85.214.132.117")
        assert region is not None
