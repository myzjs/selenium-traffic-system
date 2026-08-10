"""
Geo查询走代理 + IP质量字段 测试（I2 / P0-3）
- Geo查询尽量复用已配置代理（ip_provider._enrich_ip_geo / ip_info_resolver._http_get_json）
- resolve_ip_info 结果包含 ip_type 字段
全部使用 mock，不依赖真实网络。
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import ip_provider
import ip_info_resolver
from ip_provider import IPProvider
from ip_info_resolver import resolve_ip_info, _http_get_json


class TestGeoViaProxy:
    """Geo查询应尽量走代理，避免向Geo服务商暴露本机真IP"""

    @patch("ip_provider.requests.get")
    def test_enrich_ip_geo_uses_proxy(self, mock_get):
        """传入 proxy_url 时 requests.get 应携带 proxies"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success", "country": "United States", "countryCode": "US",
            "regionName": "California", "city": "LA", "timezone": "America/Los_Angeles",
            "isp": "TestISP",
        }
        mock_get.return_value = mock_resp
        provider = IPProvider()
        result = provider._enrich_ip_geo("1.2.3.4", proxy_url="http://u:p@proxy:8080")
        assert result is not None
        assert result["country_code"] == "US"
        _, kwargs = mock_get.call_args
        assert kwargs.get("proxies") == {"http": "http://u:p@proxy:8080", "https": "http://u:p@proxy:8080"}

    @patch("ip_provider.requests.get")
    def test_enrich_ip_geo_without_proxy_logs_warning(self, mock_get):
        """未传 proxy_url 时仍可工作（直连），但记录 warning"""
        th = ip_provider.logger
        with patch.object(th, "warning") as mock_warn:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "status": "success", "country": "UK", "countryCode": "GB",
                "regionName": "England", "city": "London", "timezone": "Europe/London", "isp": "X"
            }
            mock_get.return_value = mock_resp
            provider = IPProvider()
            result = provider._enrich_ip_geo("1.2.3.4")
            assert result is not None
            assert any("直连" in str(a) for a in mock_warn.call_args_list)

    @patch("ip_info_resolver.urllib.request.build_opener")
    def test_http_get_json_uses_proxy(self, mock_build_opener):
        """传入 proxy_url 时 build_opener + ProxyHandler 应被使用"""
        mock_opener = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status": "success"}'
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener
        result = _http_get_json("https://ip-api.com/json/1.2.3.4", proxy_url="http://proxy:8080")
        assert result == {"status": "success"}
        mock_build_opener.assert_called_once()


class TestResolveIpType:
    """resolve_ip_info 结果应包含 ip_type 字段（P0-3）"""

    def test_result_has_ip_type_key(self):
        """无论成功失败，ip_type 字段都存在"""
        with patch.object(ip_info_resolver, "_query_ip_api", return_value={}), \
             patch.object(ip_info_resolver, "_query_ipapi_co", return_value={}), \
             patch.object(ip_info_resolver, "_query_ipinfo_io", return_value={}):
            result = resolve_ip_info("1.2.3.4")
            assert "ip_type" in result

    def test_proxy_ip_type_passthrough(self):
        """代理返回的 type 透传到 ip_type"""
        proxy_info = {"country": "US", "timezone": "America/New_York", "language": "en-US", "type": "hosting"}
        result = resolve_ip_info("1.2.3.4", proxy_ip_info=proxy_info)
        assert result["ip_type"] == "hosting"

    def test_ip_api_hosting_maps_to_ip_type(self):
        """ip-api 的 hosting/proxy 布尔标记换算为 ip_type"""
        def fake_ip_api(ip, proxy_url=None):
            return {"country_code": "US", "country_name": "United States",
                    "timezone": "America/New_York", "language": "en-US", "ip_type": "hosting"}
        with patch.object(ip_info_resolver, "_query_ip_api", side_effect=fake_ip_api), \
             patch.object(ip_info_resolver, "_query_ipapi_co", return_value={}):
            result = resolve_ip_info("1.2.3.4")
            assert result["ip_type"] == "hosting"
            # 上层据此可拒绝该IP
            from ip_provider import is_high_risk_ip
            assert is_high_risk_ip(result["ip_type"]) is True