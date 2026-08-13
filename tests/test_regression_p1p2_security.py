# -*- coding: utf-8 -*-
"""
P1/P2 安全与门禁回归测试（26.8.13.1）
=====================================
覆盖全面审计中测试覆盖薄弱的修复点，防止回归：

P1 IP 门禁收紧：
  - _validate_resolved_ip_info 三要素硬校验（国家/时区/语言/IP 匹配/国家匹配）
  - redial_adsl_and_get_ip fail-closed：ip_type 未知 / datacenter / proxy / vpn / hosting
    一律拒绝重拨，绝不放裸奔 IP 进入任务队列
  - 住宅（residential）/ 移动（mobile）IP 正常放行

P2 安全修复：
  - HTTP Basic Auth：无默认密码、未设密码不启用认证、401 + WWW-Authenticate、
    健康检查豁免
  - 配置接口密码脱敏（_masked_config_payload 深拷贝脱敏，不影响全局 config）
  - 硬编码清理：UA 重复率阈值从 config.ua_repeat_max_rate 读取（不再写死 0.2）
"""
import base64
import copy
import os
import time

import pytest

_UA_125 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
_UA_124 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_UA_123 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


# ========== P1：IP 门禁 fail-closed ==========

class TestIPGateFailClosed:
    """P1：IP 门禁 fail-closed —— 未知/高危 IP 类型必须被拒绝，不得进入任务队列"""

    _TEST_IP = "203.0.113.5"

    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        self.app = app_module
        self._orig_config = copy.deepcopy(dict(app_module.config))
        # 注入最小 ADSL 配置：快速失败（3 次尝试），避免真实拨号等待
        app_module.config["adsl_ip_redial_max_attempts"] = 3
        app_module.config["adsl_min_redial_interval"] = 1
        app_module.config["adsl_profile"] = "pppoe"
        app_module.config["adsl_required_country"] = "US"
        # 拨号/网络/睡眠全部打桩，测试只关注门禁判断逻辑
        monkeypatch.setattr(app_module.subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0, "stderr": b"", "stdout": b""})())
        monkeypatch.setattr(app_module, "get_direct_public_ip", lambda timeout=10: self._TEST_IP)
        monkeypatch.setattr(app_module, "_adsl_ip_seen_recently", lambda ip, w: (False, None))
        monkeypatch.setattr(app_module, "sync_process_timezone_to_ip", lambda resolved: None)
        monkeypatch.setattr(app_module, "_record_adsl_ip_use", lambda ip, resolved=None: None)
        yield
        app_module.config.clear()
        app_module.config.update(self._orig_config)

    def _resolved(self, ip_type="residential"):
        return {
            "success": True, "ip": self._TEST_IP, "country_code": "US",
            "country_name": "United States", "timezone": "America/New_York",
            "language": "en-US", "ip_type": ip_type, "isp": "Test ISP", "asn": "AS64500",
        }

    def _redial(self):
        return self.app.redial_adsl_and_get_ip(
            sleep_func=lambda *a, **k: True, status_obj={"status": ""}
        )

    # ---- 三要素硬校验 ----

    def test_validate_resolved_ip_info_ok(self):
        valid, reason = self.app._validate_resolved_ip_info(self._TEST_IP, self._resolved())
        assert valid and reason == "ok"

    def test_validate_resolved_ip_info_missing_triple(self):
        for key in ("country_code", "timezone", "language"):
            r = self._resolved()
            r[key] = None
            valid, reason = self.app._validate_resolved_ip_info(self._TEST_IP, r)
            assert not valid and "缺失" in reason

    def test_validate_resolved_ip_info_invalid_tz(self):
        r = self._resolved()
        r["timezone"] = "Mars/Olympus"
        valid, reason = self.app._validate_resolved_ip_info(self._TEST_IP, r)
        assert not valid and "IANA" in reason

    def test_validate_resolved_ip_info_invalid_lang(self):
        r = self._resolved()
        r["language"] = "en"
        valid, reason = self.app._validate_resolved_ip_info(self._TEST_IP, r)
        assert not valid and "BCP47" in reason

    def test_validate_resolved_ip_info_country_mismatch(self):
        r = self._resolved()
        r["country_code"] = "CN"
        valid, reason = self.app._validate_resolved_ip_info(self._TEST_IP, r)
        assert not valid and "国家不符" in reason

    # ---- fail-closed：未知/高危类型一律拒绝 ----

    def test_gate_rejects_unknown_ip_type_fail_closed(self, monkeypatch):
        # ★ 修复验证：ip_type 为空时即使三要素齐全也必须拒绝（fail-closed），
        #   防止"类型未知"的裸奔 IP 绕过风控进入任务队列
        monkeypatch.setattr(self.app, "resolve_ip_info",
                            lambda ip, **k: self._resolved(ip_type=None))
        with pytest.raises(RuntimeError, match="未获得"):
            self._redial()

    @pytest.mark.parametrize("bad_type", ["datacenter", "proxy", "vpn", "hosting"])
    def test_gate_rejects_high_risk_types(self, monkeypatch, bad_type):
        monkeypatch.setattr(self.app, "resolve_ip_info",
                            lambda ip, **k: self._resolved(ip_type=bad_type))
        with pytest.raises(RuntimeError, match="未获得"):
            self._redial()

    def test_gate_accepts_residential(self, monkeypatch):
        monkeypatch.setattr(self.app, "resolve_ip_info",
                            lambda ip, **k: self._resolved(ip_type="residential"))
        ip, resolved = self._redial()
        assert ip == self._TEST_IP
        assert resolved["ip_type"] == "residential"

    def test_gate_accepts_mobile(self, monkeypatch):
        monkeypatch.setattr(self.app, "resolve_ip_info",
                            lambda ip, **k: self._resolved(ip_type="mobile"))
        ip, resolved = self._redial()
        assert ip == self._TEST_IP
        assert resolved["ip_type"] == "mobile"


# ========== P2：HTTP Basic Auth 恢复 ==========

class TestBasicAuth:
    """P2：认证恢复 —— 无默认密码、401 + WWW-Authenticate、健康检查豁免"""

    @staticmethod
    def _client(monkeypatch, enabled, user="admin", pwd="secret"):
        import app as app_module
        monkeypatch.setattr(app_module, "_AUTH_ENABLED", enabled)
        monkeypatch.setattr(app_module, "AUTH_USER", user)
        monkeypatch.setattr(app_module, "AUTH_PASS", pwd)
        return app_module.app.test_client()

    def test_auth_disabled_by_default_no_default_password(self):
        # ★ 修复验证：未设置 APP_AUTH_PASS 时认证不启用（无"默认密码"安全假象）
        import app as app_module
        if not app_module.AUTH_PASS:
            assert app_module._AUTH_ENABLED is False
        assert app_module.AUTH_PASS == os.getenv("APP_AUTH_PASS", "")

    def test_disabled_allows_access(self, monkeypatch):
        client = self._client(monkeypatch, enabled=False)
        assert client.get("/get_config").status_code == 200

    def test_enabled_requires_credentials(self, monkeypatch):
        client = self._client(monkeypatch, enabled=True)
        resp = client.get("/get_config")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")

    def test_enabled_accepts_valid_credentials(self, monkeypatch):
        client = self._client(monkeypatch, enabled=True)
        token = base64.b64encode(b"admin:secret").decode()
        resp = client.get("/get_config", headers={"Authorization": f"Basic {token}"})
        assert resp.status_code == 200

    def test_enabled_rejects_wrong_password(self, monkeypatch):
        client = self._client(monkeypatch, enabled=True)
        token = base64.b64encode(b"admin:wrong").decode()
        resp = client.get("/get_config", headers={"Authorization": f"Basic {token}"})
        assert resp.status_code == 401

    def test_health_check_exempt_from_auth(self, monkeypatch):
        # 健康检查路径必须豁免认证（无路由时返回 404，但绝不能是 401）
        client = self._client(monkeypatch, enabled=True)
        resp = client.get("/health")
        assert resp.status_code != 401


# ========== P2：配置接口密码脱敏 ==========

class TestConfigMasking:
    """P2：密码脱敏 —— 接口/调试输出不得泄露明文凭据"""

    def test_masked_config_payload_masks_passwords(self):
        import app as app_module
        orig = copy.deepcopy(dict(app_module.config))
        app_module.config["ip_proxy_pwd"] = "TopSecret123"
        app_module.config["proxy_pool"] = [
            {"host": "h1", "username": "u1", "proxy_pwd": "P@ssw0rd!23"},
            {"host": "h2", "username": "u2", "proxy_pwd": "Abcd1234"},
        ]
        try:
            masked = app_module._masked_config_payload()
            # ★ 深拷贝：脱敏只发生在副本上，全局 config 明文不受影响
            assert app_module.config["ip_proxy_pwd"] == "TopSecret123"
            assert app_module.config["proxy_pool"][0]["proxy_pwd"] == "P@ssw0rd!23"
        finally:
            app_module.config.clear()
            app_module.config.update(orig)
        assert masked["ip_proxy_pwd"] == ""
        assert masked["proxy_pool"][0]["proxy_pwd"] == "P@***"
        assert masked["proxy_pool"][1]["proxy_pwd"] == "Ab***"

    def test_get_config_api_never_returns_plaintext_password(self):
        import app as app_module
        orig = copy.deepcopy(dict(app_module.config))
        app_module.config["ip_proxy_pwd"] = "TopSecret123"
        app_module.config["proxy_pool"] = [
            {"host": "h1", "username": "u1", "proxy_pwd": "P@ssw0rd!23"},
        ]
        try:
            resp = app_module.app.test_client().get("/get_config")
            data = resp.get_json() or {}
            cfg = data.get("config", {}) or {}
            assert cfg.get("ip_proxy_pwd") == ""
            assert "TopSecret123" not in str(data)
            for item in cfg.get("proxy_pool", []) or []:
                assert item.get("proxy_pwd", "") != "P@ssw0rd!23"
        finally:
            app_module.config.clear()
            app_module.config.update(orig)


# ========== P2：硬编码清理（UA 重复率阈值配置化） ==========

class TestHardcodedCleanup:
    """P2：硬编码清理 —— UA 重复率阈值必须从 config.ua_repeat_max_rate 读取"""

    def test_ua_repeat_rate_default_in_config(self):
        import app as app_module
        assert app_module.config.get("ua_repeat_max_rate") == 0.2

    def test_ua_repeat_rate_reads_config_when_pool_exhausted(self, monkeypatch):
        import app as app_module
        pool = [_UA_125, _UA_124]
        now = time.time()

        def _new_mgr():
            mgr = app_module.UAPoolManager()
            monkeypatch.setattr(mgr, "_get_ua_pool", lambda prefix: list(pool))
            monkeypatch.setattr(mgr, "_save_history", lambda: None)
            mgr.ua_history = {ua: now for ua in pool}   # 池子全部被使用过
            mgr.total_ua_used = 10
            mgr.reused_ua_count = 5                     # 当前重复率 0.5
            return mgr

        orig_cfg = copy.deepcopy(dict(app_module.config))
        try:
            # 阈值放宽到 1.0：0.5 < 1.0 → 走"复用"分支
            app_module.config["ua_repeat_max_rate"] = 1.0
            mgr = _new_mgr()
            ua = mgr._pick_from_pool_original("en", "chromium")
            assert ua in pool and mgr.reused_ua_count == 6

            # 阈值收紧到 0.0：0.5 >= 0.0 → 走"生成变体"分支，不产生复用
            app_module.config["ua_repeat_max_rate"] = 0.0
            mgr2 = _new_mgr()
            monkeypatch.setattr(mgr2, "_generate_ua_variant", lambda base: _UA_123)
            ua2 = mgr2._pick_from_pool_original("en", "chromium")
            assert ua2 == _UA_123
            assert mgr2.reused_ua_count == 5
        finally:
            app_module.config.clear()
            app_module.config.update(orig_cfg)
