"""
IP去重持久化 + 两套去重统一 测试（P1-10）
- 去重状态可持久化/恢复（_used_ips 与 _c_segment_usage）
- proxy_server_new 与 ip_provider 共用同一套去重（check_ip_used_recently / record_ip_use）
- 高危IP类型被拒绝（P0-3）
- Geo失败不伪造IP/国家（I4）
全部使用 mock，不依赖真实网络。
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import ip_provider
from ip_provider import (
    IPProvider,
    check_ip_used_recently,
    record_ip_use,
    get_used_ips_count,
    record_c_segment_use,
    get_c_segment_stats,
    is_high_risk_ip,
)


class TestDedupPersistence:
    """去重状态持久化：保存到文件后可恢复"""

    def test_record_and_reload(self, tmp_path):
        """记录IP后再加载，应能恢复去重状态"""
        state_dir = tmp_path / ".risk_state"
        state_file = state_dir / "ip_dedup_state.json"
        with patch.object(ip_provider, "_STATE_DIR", str(state_dir)), \
             patch.object(ip_provider, "_STATE_FILE", str(state_file)):
            # 记录一个IP与一个C段
            record_ip_use("203.0.113.10")
            record_c_segment_use("203.0.113.20")
            assert state_file.exists(), "记录后应已写入持久化文件"

            # 清空内存，模拟进程重启
            ip_provider._used_ips.clear()
            ip_provider._c_segment_usage.clear()
            assert check_ip_used_recently("203.0.113.10") is False

            # 重新加载
            ip_provider._load_dedup_state()
            assert check_ip_used_recently("203.0.113.10") is True
            stats = get_c_segment_stats()
            assert stats["total_segments"] >= 1

    def test_persist_to_default_location(self, tmp_path):
        """变更后自动保存到默认位置（通过 monkeypatch 目录）"""
        state_dir = tmp_path / ".risk_state"
        with patch.object(ip_provider, "_STATE_DIR", str(state_dir)), \
             patch.object(ip_provider, "_STATE_FILE", str(state_dir / "ip_dedup_state.json")):
            record_ip_use("198.51.100.5")
            assert (state_dir / "ip_dedup_state.json").exists()

    def test_load_corrupted_file_returns_empty(self, tmp_path):
        """损坏的持久化文件不影响启动（回退空状态）"""
        state_dir = tmp_path / ".risk_state"
        state_dir.mkdir(exist_ok=True)
        state_file = state_dir / "ip_dedup_state.json"
        state_file.write_text("{ not valid json ", encoding="utf-8")
        with patch.object(ip_provider, "_STATE_FILE", str(state_file)):
            ip_provider._used_ips.clear()
            ip_provider._load_dedup_state()
            assert ip_provider._used_ips == {}


class TestUnifiedDedup:
    """proxy_server_new 与 ip_provider 共用同一套去重"""

    def test_shared_reference(self):
        """两套去重应引用同一函数（同一口径）"""
        import proxy_server_new
        assert proxy_server_new.check_ip_used_recently is ip_provider.check_ip_used_recently
        assert proxy_server_new.record_ip_use is ip_provider.record_ip_use

    def test_cross_module_dedup(self):
        """在 ip_provider 记录后，proxy_server_new 侧也能识别为已用"""
        import proxy_server_new
        record_ip_use("9.9.9.99")
        assert proxy_server_new.check_ip_used_recently("9.9.9.99") is True


class TestHighRiskRejection:
    """高危IP类型（机房/代理/VPN/Hosting）应被拒绝（P0-3）"""

    def test_is_high_risk_ip(self):
        assert is_high_risk_ip("datacenter") is True
        assert is_high_risk_ip("proxy") is True
        assert is_high_risk_ip("vpn") is True
        assert is_high_risk_ip("hosting") is True
        assert is_high_risk_ip("business") is True
        assert is_high_risk_ip("Datacenter") is True  # 大小写不敏感
        assert is_high_risk_ip("isp") is False
        assert is_high_risk_ip("") is False
        assert is_high_risk_ip(None) is False

    @patch("ip_provider.requests.get")
    def test_fetch_rejects_datacenter(self, mock_get):
        """IPDeep 出口IP为 datacenter 类型时拒绝该IP"""
        provider = IPProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "1.2.3.4:8080:user:pass"
        mock_get.return_value = mock_resp
        # 让 _get_ip_details 返回高危类型
        provider._get_ip_details = MagicMock(return_value={
            "success": True, "ip": "5.6.7.8", "country_code": "US", "ip_type": "datacenter"
        })
        result = provider._fetch_proxy_from_ipdeep("http://api", "u", "p")
        assert result["success"] is False
        assert "机房/代理IP已拒绝" in result["error"]
        assert result["detail"]["ip_type"] == "datacenter"

    @patch("ip_provider.requests.get")
    def test_fetch_rejects_proxy_via_type_key(self, mock_get):
        """兼容 ip_info 使用 type 字段时同样拒绝"""
        provider = IPProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "1.2.3.4:8080:user:pass"
        mock_get.return_value = mock_resp
        provider._get_ip_details = MagicMock(return_value={
            "success": True, "ip": "5.6.7.8", "type": "proxy"
        })
        result = provider._fetch_proxy_from_ipdeep("http://api", "u", "p")
        assert result["success"] is False
        assert "机房/代理IP已拒绝" in result["error"]

    @patch("ip_provider.requests.get")
    def test_fetch_allows_normal_ip(self, mock_get):
        """正常IP类型（isp）不被拒绝"""
        provider = IPProvider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "1.2.3.4:8080:user:pass"
        mock_get.return_value = mock_resp
        provider._get_ip_details = MagicMock(return_value={
            "success": True, "ip": "5.6.7.8", "country_code": "US", "ip_type": "isp"
        })
        result = provider._fetch_proxy_from_ipdeep("http://api", "u", "p")
        assert result["success"] is True


class TestGeoFailureNoFake:
    """Geo查询失败时不应伪造国家/时区（I4）"""

    @patch("ip_provider.requests.get")
    @patch("ip_provider.time.sleep")
    def test_ip_provider_geo_failure_no_fake_us(self, mock_sleep, mock_get):
        """ip_provider 所有Geo API失败时不伪造US"""
        mock_get.side_effect = Exception("network down")
        provider = IPProvider()
        result = provider._get_ip_details("http://p:1@h:8080")
        assert result["success"] is False
        assert result["ip"] == ""
        assert result.get("country_code") is None
        assert result.get("country_code") != "US"
        assert "timezone" not in result or not result["timezone"]

    @patch("proxy_server_new.requests.get")
    def test_proxy_server_geo_failure_no_fake(self, mock_get):
        """proxy_server_new 所有Geo API失败时不伪造1.1.1.1/US"""
        mock_get.side_effect = Exception("network down")
        import proxy_server_new
        result = proxy_server_new.get_ip_details_proxy("http://p:1@h:8080")
        assert result["success"] is False
        assert result["ip"] == ""
        assert result.get("country_code") != "US"
        assert "1.1.1.1" not in result.get("ip", "")