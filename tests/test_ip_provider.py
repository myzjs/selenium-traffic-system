"""
IP代理获取模块测试 - 适配 2.0 直连 IPDeep API 架构
使用 Mock 不依赖真实网络
"""
from unittest.mock import patch, MagicMock
import sys
import os
import json

# 确保可以导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from ip_provider import IPProvider, configure_ip_provider, init_from_config
from ip_provider import get_proxy_from_api_url, check_ip_used_recently, record_ip_use
from ip_provider import invalidate_proxy_cache, get_used_ips_count


class TestIPProviderInit:
    """IPProvider 初始化测试"""

    def test_default_init(self):
        """默认初始化"""
        provider = IPProvider()
        assert provider.provider_type == "proxy_api"
        assert provider.proxy_pool == []
        assert provider.current_proxy is None

    def test_init_with_custom_type(self):
        """自定义类型初始化"""
        provider = IPProvider("custom_type")
        assert provider.provider_type == "custom_type"

    def test_configure_proxy_api(self):
        """配置代理 API 模式（2.0直连，无VPS中转）"""
        provider = IPProvider()
        pool = [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com"}]
        provider.configure_proxy_api(pool, config={"key": "value"})
        assert provider.provider_type == "proxy_api"
        assert len(provider.proxy_pool) == 1
        assert provider._config == {"key": "value"}

    def test_configure_proxy_api_kwargs_compatible(self):
        """兼容旧调用签名（vps_* 参数被 **kwargs 吸收）"""
        provider = IPProvider()
        pool = [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com"}]
        provider.configure_proxy_api(pool, vps_host="1.2.3.4", vps_port=6666)
        assert provider.provider_type == "proxy_api"
        assert len(provider.proxy_pool) == 1


class TestIPProviderGetIP:
    """IP 获取测试（直连 IPDeep API）"""

    def test_get_ip_empty_pool(self):
        """空代理池返回错误"""
        provider = IPProvider()
        result = provider.get_ip()
        assert result["success"] is False
        assert "代理池为空" in result["error"]

    def test_get_ip_no_enabled_proxy(self):
        """无启用的代理返回错误"""
        provider = IPProvider()
        provider.proxy_pool = [{"enabled": False, "country_code": "US"}]
        result = provider.get_ip()
        assert result["success"] is False
        assert "没有启用的代理" in result["error"]

    @patch("ip_provider.requests.get")
    def test_get_ip_ipdeep_text_format(self, mock_get):
        """IPDeep 返回纯文本格式 host:port:user:pwd"""
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.text = "1.2.3.4:8080:testuser:testpass"

        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {
            "status": "success",
            "query": "99.88.77.66",
            "country": "United States",
            "countryCode": "US",
            "regionName": "California",
            "city": "Los Angeles",
            "timezone": "America/Los_Angeles",
            "isp": "TestISP",
            "hosting": False,
            "proxy": False,
        }

        mock_get.side_effect = [mock_resp1, mock_resp2]

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US",
              "proxy_api_url": "http://gate.ipdeep.com:8082",
              "proxy_user": "user1", "proxy_pwd": "pass1"}]
        )
        result = provider.get_ip()
        assert result["success"] is True
        assert result["proxy_host"] == "1.2.3.4"
        assert result["proxy_port"] == "8080"
        assert result["proxy_username"] == "testuser"
        assert result["proxy_password"] == "testpass"
        assert result["ip_info"]["ip"] == "99.88.77.66"

    @patch("ip_provider.requests.get")
    def test_get_ip_ipdeep_json_format(self, mock_get):
        """IPDeep 返回 JSON 格式"""
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.text = json.dumps({
            "success": True,
            "data": {"ip": "5.6.7.8", "port": 3128, "username": "juser", "password": "jpass"}
        })

        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {
            "status": "success",
            "query": "44.33.22.11",
            "country": "United Kingdom",
            "countryCode": "GB",
            "regionName": "England",
            "city": "London",
            "timezone": "Europe/London",
            "isp": "TestISP UK",
            "hosting": False,
            "proxy": False,
        }

        mock_get.side_effect = [mock_resp1, mock_resp2]

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "GB",
              "proxy_api_url": "http://api.ipdeep.com/json",
              "proxy_user": "u", "proxy_pwd": "p"}]
        )
        result = provider.get_ip()
        assert result["success"] is True
        assert result["proxy_host"] == "5.6.7.8"
        assert result["proxy_port"] == "3128"
        assert result["proxy_username"] == "juser"
        assert result["proxy_password"] == "jpass"

    @patch("ip_provider.time.sleep")
    @patch("ip_provider.requests.get")
    def test_get_ip_http_error(self, mock_get, mock_sleep):
        """IPDeep 返回 HTTP 4xx/5xx（重试3次后失败）"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US",
              "proxy_api_url": "http://gate.ipdeep.com:8082",
              "proxy_user": "u", "proxy_pwd": "p"}]
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "IPDeep HTTP 500" in result["error"]
        assert mock_get.call_count == 3

    @patch("ip_provider.time.sleep")
    @patch("ip_provider.requests.get")
    def test_get_ip_bad_format(self, mock_get, mock_sleep):
        """IPDeep 返回格式不正确（无法解析）"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "invalid_response_no_colon"
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US",
              "proxy_api_url": "http://gate.ipdeep.com:8082",
              "proxy_user": "u", "proxy_pwd": "p"}]
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "格式不正确" in result["error"]

    @patch("ip_provider.requests.get")
    def test_get_ip_json_explicit_failure(self, mock_get):
        """IPDeep JSON 响应中 success=False"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"success": False, "msg": "账号不存在"})
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US",
              "proxy_api_url": "http://gate.ipdeep.com:8082",
              "proxy_user": "u", "proxy_pwd": "p"}]
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "账号不存在" in result["error"]

    def test_get_ip_unknown_type(self):
        """未知的 IP 获取方式"""
        provider = IPProvider("unknown_type")
        result = provider.get_ip()
        assert result["success"] is False
        assert "未知" in result["error"]

    @patch("ip_provider.time.sleep")
    @patch("ip_provider.requests.get")
    def test_get_ip_timeout(self, mock_get, mock_sleep):
        """IPDeep 请求超时（3次重试后失败）"""
        import requests as real_requests
        mock_get.side_effect = real_requests.exceptions.Timeout("Connection timed out")

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US",
              "proxy_api_url": "http://gate.ipdeep.com:8082",
              "proxy_user": "u", "proxy_pwd": "p"}]
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "3次尝试均失败" in result["error"]


class TestIPProviderConvenienceFunctions:
    """便捷函数测试"""

    @patch("ip_provider.requests.get")
    def test_get_proxy_from_api_url_success(self, mock_get):
        """get_proxy_from_api_url 直连成功"""
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.text = "10.0.0.1:9090:cacheuser:cachepass"

        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {
            "status": "success",
            "query": "77.66.55.44",
            "country": "Australia",
            "countryCode": "AU",
            "regionName": "NSW",
            "city": "Sydney",
            "timezone": "Australia/Sydney",
            "isp": "TestISP AU",
            "hosting": False,
            "proxy": False,
        }

        mock_get.side_effect = [mock_resp1, mock_resp2]

        result = get_proxy_from_api_url(
            "http://unique-test-api.example.com/proxy",
            api_user="testuser", api_pwd="testpass",
            country_code="AU", use_cache=False
        )
        assert result["success"] is True
        assert result["proxy_host"] == "10.0.0.1"
        assert result["proxy_port"] == "9090"

    @patch("ip_provider.requests.get")
    def test_get_proxy_from_api_url_with_cache(self, mock_get):
        """get_proxy_from_api_url 缓存机制（★ 缓存+IP去重联动）

        P1 修复后行为：缓存命中时若缓存 IP 已在 24h 去重池中（刚被占用），
        缓存必须失效并重新获取新 IP，防止同一 IP 被重复使用触发风控；
        仅当缓存 IP 可复用（不在去重间隔内）时才直接返回缓存、不发请求。
        """
        import time as _t
        import ip_provider as _ip

        def _mk_resp(text_or_json):
            r = MagicMock()
            r.status_code = 200
            if isinstance(text_or_json, str):
                r.text = text_or_json
            else:
                r.json.return_value = text_or_json
            return r

        mock_resp1 = _mk_resp("20.0.0.1:7070:cuser:cpass")
        mock_resp2 = _mk_resp({
            "status": "success",
            "query": "11.22.33.44",
            "country": "Canada",
            "countryCode": "CA",
            "regionName": "Ontario",
            "city": "Toronto",
            "timezone": "America/Toronto",
            "isp": "TestISP CA",
            "hosting": False,
            "proxy": False,
        })
        mock_resp3 = _mk_resp("21.0.0.1:7071:cuser:cpass")
        mock_resp4 = _mk_resp({
            "status": "success",
            "query": "22.33.44.55",
            "country": "Canada",
            "countryCode": "CA",
            "regionName": "Ontario",
            "city": "Toronto",
            "timezone": "America/Toronto",
            "isp": "TestISP CA",
            "hosting": False,
            "proxy": False,
        })
        mock_get.side_effect = [mock_resp1, mock_resp2, mock_resp3, mock_resp4]

        cache_url = "http://cache-test-api.example.com/proxy"
        result1 = get_proxy_from_api_url(cache_url, api_user="u", api_pwd="p", use_cache=True)
        assert result1["success"] is True

        # 第二次调用：缓存命中但 IP 11.22.33.44 已在去重池（刚被占用）
        # → 缓存失效，重新获取不同 IP
        result2 = get_proxy_from_api_url(cache_url, api_user="u", api_pwd="p", use_cache=True)
        assert result2["success"] is True
        assert result2["ip_info"]["ip"] != result1["ip_info"]["ip"]

        # 缓存 IP 已过 24h 去重期（可复用）→ 缓存命中直接返回，不发请求
        cached_ip = result2["ip_info"]["ip"]
        with _ip._used_ips_lock:
            _ip._used_ips[cached_ip] = _t.time() - 25 * 3600
        call_count_before = mock_get.call_count
        result3 = get_proxy_from_api_url(cache_url, api_user="u", api_pwd="p", use_cache=True)
        assert result3["success"] is True
        assert mock_get.call_count == call_count_before

        invalidate_proxy_cache(cache_url)

    def test_check_ip_used_recently_not_used(self):
        """检查未使用的 IP"""
        result = check_ip_used_recently("192.0.2.99")
        assert result is False

    def test_record_and_check_ip(self):
        """记录 IP 后检查"""
        record_ip_use("192.0.2.100")
        assert check_ip_used_recently("192.0.2.100") is True
        assert get_used_ips_count() >= 1

    def test_invalidate_proxy_cache(self):
        """使缓存失效"""
        invalidate_proxy_cache()
        assert True

    def test_invalidate_specific_cache(self):
        """使指定URL缓存失效"""
        invalidate_proxy_cache("http://some-url.com")
        assert True


class TestConfigureFunctions:
    """配置函数测试"""

    def test_configure_ip_provider_proxy_api(self):
        """配置 IP 提供者为代理 API 模式"""
        config = {
            "ip_provider_type": "proxy_api",
            "proxy_pool": [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com"}]
        }
        configure_ip_provider(config)
        from ip_provider import get_ip_provider
        provider = get_ip_provider()
        assert provider.provider_type == "proxy_api"
        assert len(provider.proxy_pool) == 1

    def test_configure_ip_provider_empty_pool(self):
        """配置空代理池"""
        config = {"ip_provider_type": "proxy_api", "proxy_pool": []}
        configure_ip_provider(config)
        from ip_provider import get_ip_provider
        provider = get_ip_provider()
        assert provider.provider_type == "proxy_api"
        assert provider.proxy_pool == []

    @patch("ip_provider.configure_ip_provider")
    def test_init_from_config(self, mock_configure):
        """从配置初始化"""
        config = {"ip_provider_type": "proxy_api"}
        init_from_config(config)
        mock_configure.assert_called_once_with(config)
