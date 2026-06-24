"""
IP 信息解析模块测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock


class TestIPInfoResolver:
    """IP 信息解析测试"""

    def test_import(self):
        import ip_info_resolver
        assert ip_info_resolver is not None

    def test_country_to_language_mapping(self):
        from ip_info_resolver import COUNTRY_TO_LANGUAGE
        assert COUNTRY_TO_LANGUAGE["US"] == "en-US"
        assert COUNTRY_TO_LANGUAGE["CN"] == "zh-CN"
        assert COUNTRY_TO_LANGUAGE["GB"] == "en-GB"
        assert COUNTRY_TO_LANGUAGE["JP"] == "ja-JP"

    def test_country_to_timezone_mapping(self):
        from ip_info_resolver import COUNTRY_TO_TIMEZONE
        assert COUNTRY_TO_TIMEZONE["CN"] == "Asia/Shanghai"
        assert COUNTRY_TO_TIMEZONE["US"] == "America/New_York"
        assert COUNTRY_TO_TIMEZONE["GB"] == "Europe/London"

    @patch("ip_info_resolver.urllib.request.urlopen")
    def test_resolve_ip_info_basic(self, mock_urlopen):
        from ip_info_resolver import resolve_ip_info
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status":"fail"}'
        mock_urlopen.return_value = mock_resp
        result = resolve_ip_info("8.8.8.8")
        assert isinstance(result, dict)
        assert "country_code" in result
        assert "timezone" in result
        assert "language" in result

    @patch("ip_info_resolver.urllib.request.urlopen")
    def test_resolve_ip_info_invalid_ip(self, mock_urlopen):
        from ip_info_resolver import resolve_ip_info
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status":"fail"}'
        mock_urlopen.return_value = mock_resp
        result = resolve_ip_info("")
        assert isinstance(result, dict)

    @patch("ip_info_resolver.urllib.request.urlopen")
    def test_resolve_ip_info_none_ip(self, mock_urlopen):
        from ip_info_resolver import resolve_ip_info
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status":"fail"}'
        mock_urlopen.return_value = mock_resp
        result = resolve_ip_info(None)
        assert isinstance(result, dict)

    def test_normalize_country_code(self):
        from ip_info_resolver import _normalize_country_code
        assert _normalize_country_code("US") == "US"
        assert _normalize_country_code("us") == "US"
        assert _normalize_country_code("") is None
        assert _normalize_country_code(None) is None

    def test_resolve_with_proxy_info(self):
        """使用代理信息解析"""
        from ip_info_resolver import resolve_ip_info
        proxy_ip_info = {
            "country_code": "US",
            "country_name": "United States",
            "timezone": "America/New_York",
            "language": "en-US"
        }
        result = resolve_ip_info("8.8.8.8", proxy_ip_info=proxy_ip_info)
        assert result["country_code"] == "US"

    @patch("ip_info_resolver.urllib.request.urlopen")
    def test_resolve_with_api_fallback(self, mock_urlopen):
        """API 回退解析"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"country_code":"US","timezone":"America/New_York","language":"en-US"}'
        mock_urlopen.return_value = mock_resp

        from ip_info_resolver import resolve_ip_info
        result = resolve_ip_info("8.8.8.8")
        assert isinstance(result, dict)

    @patch("ip_info_resolver.urllib.request.urlopen")
    def test_resolve_ip_info_country_only(self, mock_urlopen):
        """只有国家信息的解析"""
        from ip_info_resolver import resolve_ip_info
        # Mock API 调用，防止超时
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status":"fail"}'
        mock_urlopen.return_value = mock_resp

        proxy_ip_info = {
            "country_code": "CN",
        }
        result = resolve_ip_info("1.2.3.4", proxy_ip_info=proxy_ip_info)
        # CN 应该映射出 zh-CN 和 Asia/Shanghai
        assert result["country_code"] == "CN"
