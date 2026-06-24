"""
IP代理获取模块测试 - 使用 Mock 不依赖真实网络
"""
from unittest.mock import patch, MagicMock, PropertyMock
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

    def test_init_with_adsl(self):
        """ADSL 模式初始化"""
        provider = IPProvider("adsl")
        assert provider.provider_type == "adsl"

    def test_configure_proxy_api(self):
        """配置代理 API 模式"""
        provider = IPProvider()
        pool = [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com"}]
        provider.configure_proxy_api(pool, vps_host="1.2.3.4", vps_port=6666)
        assert provider.provider_type == "proxy_api"
        assert len(provider.proxy_pool) == 1
        assert provider.vps_config["host"] == "1.2.3.4"

    def test_configure_adsl(self):
        """配置 ADSL 模式"""
        provider = IPProvider()
        provider.configure_adsl(profile="pppoe", username="user", password="pass")
        assert provider.provider_type == "adsl"
        assert provider.adsl_username == "user"


class TestIPProviderGetIP:
    """IP 获取测试"""

    def test_get_ip_empty_pool(self):
        """空代理池返回错误"""
        provider = IPProvider()
        result = provider.get_ip()
        assert result["success"] is False
        assert "代理池为空" in result["error"]

    @patch("ip_provider.requests.get")
    def test_get_ip_via_proxy_success(self, mock_get):
        """通过 VPS 获取代理成功"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "proxy_host": "1.2.3.4",
            "proxy_port": 3128,
            "ip_info": {"ip": "8.8.8.8", "country": "US", "city": "Mountain View"}
        }
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com/api"}],
            vps_host="1.2.3.4"
        )
        result = provider.get_ip()
        assert result["success"] is True
        assert result["proxy_host"] == "1.2.3.4"

    @patch("ip_provider.requests.get")
    def test_get_ip_via_proxy_vps_error(self, mock_get):
        """VPS 返回非 2xx 状态码"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com/api"}],
            vps_host="1.2.3.4"
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "VPS HTTP 500" in result["error"]

    @patch("ip_provider.requests.get")
    def test_get_ip_via_proxy_non_json(self, mock_get):
        """VPS 返回非 JSON 响应"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = "not json"
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com/api"}],
            vps_host="1.2.3.4"
        )
        result = provider.get_ip()
        assert result["success"] is False
        assert "非JSON" in result["error"]

    @patch("ip_provider.requests.get")
    def test_get_ip_via_proxy_ipdeep_failed(self, mock_get):
        """IPDeep 返回失败状态"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": False, "msg": "账号不存在"}
        mock_get.return_value = mock_resp

        provider = IPProvider()
        provider.configure_proxy_api(
            [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com/api"}],
            vps_host="1.2.3.4"
        )
        result = provider.get_ip()
        assert result["success"] is False

    def test_get_ip_unknown_type(self):
        """未知的 IP 获取方式"""
        provider = IPProvider("unknown_type")
        result = provider.get_ip()
        assert result["success"] is False
        assert "未知" in result["error"]

    @patch("ip_provider.subprocess.run")
    def test_get_ip_adsl_success(self, mock_run):
        """ADSL 获取 IP 成功"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "    inet 192.168.1.100/24 brd 192.168.1.255 scope global ppp0"
        mock_run.return_value = mock_result

        provider = IPProvider("adsl")
        provider.configure_adsl(interface="ppp0")
        result = provider.get_ip()
        assert result["success"] is True
        assert result["ip_address"] == "192.168.1.100"


class TestIPProviderConvenienceFunctions:
    """便捷函数测试"""

    @patch("ip_provider.requests.get")
    def test_get_proxy_from_api_url(self, mock_get):
        """get_proxy_from_api_url 成功"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "proxy_host": "5.6.7.8",
            "proxy_port": 8080
        }
        mock_get.return_value = mock_resp

        # 需要先配置 VPS 主机
        from ip_provider import get_ip_provider
        provider = get_ip_provider()
        provider.vps_config["host"] = "1.2.3.4"

        result = get_proxy_from_api_url("http://test.com/api", use_cache=False)
        assert result["success"] is True

    def test_check_ip_used_recently_not_used(self):
        """检查未使用的 IP"""
        result = check_ip_used_recently("9.9.9.9")
        assert result is False

    def test_record_and_check_ip(self):
        """记录 IP 后检查"""
        record_ip_use("1.2.3.4")
        assert check_ip_used_recently("1.2.3.4") is True
        assert get_used_ips_count() >= 1

    def test_invalidate_proxy_cache(self):
        """使缓存失效"""
        invalidate_proxy_cache()
        # 只是确保不抛异常
        assert True


class TestConfigureFunctions:
    """配置函数测试"""

    def test_configure_ip_provider_proxy_api(self):
        """配置 IP 提供者为代理 API 模式"""
        config = {
            "ip_provider_type": "proxy_api",
            "vps_host": "1.2.3.4",
            "vps_new_port": 6666,
            "proxy_pool": [{"enabled": True, "country_code": "US", "proxy_api_url": "http://test.com"}]
        }
        configure_ip_provider(config)
        # 验证全局 provider 已配置
        from ip_provider import get_ip_provider
        provider = get_ip_provider()
        assert provider.provider_type == "proxy_api"

    def test_configure_ip_provider_adsl(self):
        """配置 IP 提供者为 ADSL 模式"""
        config = {
            "ip_provider_type": "adsl",
            "adsl_username": "user",
            "adsl_password": "pass"
        }
        configure_ip_provider(config)
        from ip_provider import get_ip_provider
        provider = get_ip_provider()
        assert provider.provider_type == "adsl"

    @patch("ip_provider.configure_ip_provider")
    def test_init_from_config(self, mock_configure):
        """从配置初始化"""
        config = {"ip_provider_type": "proxy_api"}
        init_from_config(config)
        mock_configure.assert_called_once_with(config)
