"""
契约测试（Contract Test）— 可独立导入模块的稳定接口契约审计。

背景：`import app` 会卡在模块级初始化（预先存在），因此本文件【不 import app.py】，
只检测以下可独立导入的 Python 模块的稳定接口契约：
  - ip_provider
  - risk_control_enhancements
  - seo_query_module
  - ip_info_resolver
  - popunder_trigger

契约测试的两种方式：
  1. CONTRACT 常量清单：声明每个函数/类的入参签名与返回结构的关键字段；
  2. 运行时断言：真实调用函数，校验返回体的字段/语义。

约束：所有测试【不触发真实网络、不启动浏览器】。网络类调用一律用
unittest.mock 注入隔离，保证可在 CI 快速运行。

每条契约测试用中文 docstring 说明它保护了什么接口契约（防止后续改动破坏）。
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ======================================================================
# 0. 契约清单（CONTRACT 常量）—— 声明各模块对外稳定接口
# ======================================================================
IP_PROVIDER_CONTRACT = {
    "funcs": {
        "is_high_risk_ip": {"params": ["ip_type"], "returns": "bool"},
        "check_ip_used_recently": {"params": ["ip"], "returns": "bool"},
        "record_ip_use": {"params": ["ip"], "returns": None},
        "get_used_ips_count": {"params": [], "returns": "int"},
        "_load_dedup_state": {"params": [], "returns": None},
        "_save_dedup_state": {"params": ["force"], "returns": None},
    },
    "high_risk_types": {"datacenter", "proxy", "vpn", "hosting", "business"},
    "safe_types": {"isp", "residential", "mobile", "unknown", ""},
}

RISK_ENHANCE_CONTRACT = {
    "func_all": ["get_stable_canvas_seed"],  # 必须出现在 __all__（可 import *）
    "dns_pick_resolver": {"params": ["country"], "returns": "List[str] len=3"},
    "isolate_pool_allow": {
        "params": ["adv_id", "ip", "fingerprint", "ua", "asn"],
        "returns": "Tuple[bool, str]",
    },
    "adv_isolation_can_acquire": {
        "params": ["adv_id", "device_id", "ip", "ua"],
        "returns": "Tuple[bool, str]",
    },
}

SEO_QUERY_CONTRACT = {
    "REGION_SEARCH_ENGINE_MAP": {
        "JP": "co.jp",
        "DE": "google.de",
        "CN": "baidu",
    },
    "generate_referer_for_region": {
        "params": ["country_code", "language", "keyword"],
        "returns": "str(URL) | None",
    },
    "generate_referer": {
        "params": ["engine_id", "keyword"],
        "returns": "str(URL) | None",
    },
}

IP_RESOLVER_CONTRACT = {
    "resolve_ip_info": {
        "params": ["ip", "proxy_ip_info", "proxy_url"],
        "required_keys": {
            "success", "ip", "country_code", "country_name",
            "timezone", "language",
        },
    },
}

POPUNDER_CONTRACT = {
    "exported_funcs": [
        "trigger_popunder",
        "is_ip_safe_for_hilltopads",
        "should_trigger_for_network",
        "_pick_safe_coordinates",
        "self_test",
        "_inject_popunder_stealth",
    ],
}


# ======================================================================
# 1. ip_provider 契约
# ======================================================================
class TestIPProviderContract:
    """ip_provider 模块：高危IP判定 / IP去重 / 去重状态持久化接口契约"""

    def test_contract_funcs_callable(self):
        """保护：ip_provider 对外暴露的契约函数必须存在且可调用。"""
        import ip_provider
        expected = IP_PROVIDER_CONTRACT["funcs"]
        for name in expected:
            assert callable(getattr(ip_provider, name)), f"缺少可调用函数 {name}"

    @pytest.mark.parametrize(
        "ip_type",
        ["datacenter", "proxy", "vpn", "hosting", "business"],
    )
    def test_high_risk_types_return_true(self, ip_type):
        """保护：机房/代理/VPN/托管/商业 = 高危，is_high_risk_ip 必须返回 True。"""
        from ip_provider import is_high_risk_ip
        assert is_high_risk_ip(ip_type) is True

    @pytest.mark.parametrize(
        "ip_type",
        ["isp", "residential", "mobile", "unknown", ""],
    )
    def test_safe_types_return_false(self, ip_type):
        """保护：住宅/移动/未知/空 = 安全，is_high_risk_ip 必须返回 False。"""
        from ip_provider import is_high_risk_ip
        assert is_high_risk_ip(ip_type) is False

    def test_dedup_record_then_check(self, tmp_path):
        """保护：check_ip_used_recently / record_ip_use 的去重语义
        （记录后再次检查必须命中，满足 IP 复用去重）。"""
        import ip_provider
        with patch.object(ip_provider, "_STATE_DIR", str(tmp_path / ".risk_state")), \
             patch.object(ip_provider, "_STATE_FILE", str(tmp_path / "ip_dedup_state.json")):
            ip_provider._used_ips.clear()
            ip = "203.0.113.77"
            assert ip_provider.check_ip_used_recently(ip) is False
            ip_provider.record_ip_use(ip)
            assert ip_provider.check_ip_used_recently(ip) is True
            ip_provider._used_ips.clear()

    def test_get_used_ips_count_returns_int(self, tmp_path):
        """保护：get_used_ips_count 返回 int（供去重池容量统计）。"""
        import ip_provider
        with patch.object(ip_provider, "_STATE_DIR", str(tmp_path / ".risk_state")), \
             patch.object(ip_provider, "_STATE_FILE", str(tmp_path / "ip_dedup_state.json")):
            ip_provider._used_ips.clear()
            ip_provider._used_ips["198.51.100.1"] = __import__("time").time()
            n = ip_provider.get_used_ips_count()
            assert isinstance(n, int)
            ip_provider._used_ips.clear()

    def test_dedup_state_persist_functions_exist(self):
        """保护：去重状态落盘/加载函数存在且可调用（P1-10 持久化契约）。"""
        import ip_provider
        assert callable(ip_provider._load_dedup_state)
        assert callable(ip_provider._save_dedup_state)
        assert callable(ip_provider._start_periodic_save)


# ======================================================================
# 2. risk_control_enhancements 契约
# ======================================================================
class TestRiskControlEnhancementsContract:
    """risk_control_enhancements：指纹种子 / DNS分散 / 隔离池 / 广告隔离 契约"""

    def test_import_star_contains_stable_seed(self):
        """保护：模块 `import *` 必须包含 get_stable_canvas_seed（__all__ 契约）。"""
        import risk_control_enhancements as rce
        assert "get_stable_canvas_seed" in rce.__all__

    def test_stable_canvas_seed_same_fp_consistent(self):
        """保护：同 fp_id 必须返回一致的 int 种子（跨调用稳定）。"""
        from risk_control_enhancements import get_stable_canvas_seed
        a = get_stable_canvas_seed("fp-contract-1")
        b = get_stable_canvas_seed("fp-contract-1")
        assert a == b
        assert isinstance(a, int)

    def test_stable_canvas_seed_different_fp_different(self):
        """保护：不同 fp_id 大概率返回不同种子（31-bit 随机派生）。"""
        from risk_control_enhancements import get_stable_canvas_seed
        a = get_stable_canvas_seed("fp-contract-A")
        b = get_stable_canvas_seed("fp-contract-B")
        assert a != b

    def test_stable_canvas_seed_positive(self):
        """保护：返回值恒为正 int（避免退化 0 基线）。"""
        from risk_control_enhancements import get_stable_canvas_seed
        assert get_stable_canvas_seed("fp-contract-pos") > 0

    def test_dns_pick_resolver_returns_3_nonempty(self):
        """保护：dns_diversity.pick_resolver(country) 返回 list，长度 3，
        元素为非空字符串（P2-3 DNS 分散）。"""
        from risk_control_enhancements import dns_diversity
        for country in ("US", "JP", "DE", "XX"):
            res = dns_diversity.pick_resolver(country)
            assert isinstance(res, list)
            assert len(res) == 3
            assert all(isinstance(x, str) and x.strip() for x in res)

    def test_isolate_pool_allow_returns_pair(self, tmp_path):
        """保护：isolate_pool.allow(...) 返回 (bool, str) 二元组
        （P0-1 同广告账户隔离判定）。用全新实例 + 临时状态目录隔离。"""
        import risk_control_enhancements as rce
        with patch.object(rce, "STATE_DIR", tmp_path / ".risk_state"):
            pool = rce._IsolatePool()
            out = pool.allow(
                adv_id="adv-ct-1", ip="203.0.113.10",
                fingerprint="fp-ct-1", ua="ua-ct", asn="AS15169",
                persist=False,
            )
            assert isinstance(out, tuple) and len(out) == 2
            ok, reason = out
            assert isinstance(ok, bool)
            assert isinstance(reason, str)

    def test_adv_isolation_can_acquire_returns_pair(self, tmp_path):
        """保护：adv_isolation.can_acquire(...) 返回 (bool, str) 二元组
        （P0-4 多账户×设备×IP 隔离）。用全新实例 + 临时状态目录隔离。"""
        import risk_control_enhancements as rce
        with patch.object(rce, "STATE_DIR", tmp_path / ".risk_state"):
            iso = rce._AdvIsolation()
            out = iso.can_acquire(
                adv_id="adv-ct-1", device_id="dev-ct-1",
                ip="203.0.113.20", ua="ua-ct", persist=False,
            )
            assert isinstance(out, tuple) and len(out) == 2
            ok, reason = out
            assert isinstance(ok, bool)
            assert isinstance(reason, str)


# ======================================================================
# 3. seo_query_module 契约
# ======================================================================
class TestSEOQueryModuleContract:
    """seo_query_module：地域化搜索引擎 URL / 地域化 Referer / 向后兼容 Referer"""

    def test_local_engine_url_jp(self):
        """保护：get_local_search_engine_url('JP') 返回含 co.jp 的本地 Google URL。"""
        from seo_query_module import get_seo_query
        url = get_seo_query().get_local_search_engine_url("JP")
        assert "co.jp" in url
        assert url.startswith("http")

    def test_local_engine_url_de(self):
        """保护：get_local_search_engine_url('DE') 返回含 google.de 的 URL。"""
        from seo_query_module import get_seo_query
        url = get_seo_query().get_local_search_engine_url("DE")
        assert "google.de" in url

    def test_local_engine_url_cn(self):
        """保护：get_local_search_engine_url('CN') 返回含 baidu 的 URL。"""
        from seo_query_module import get_seo_query
        url = get_seo_query().get_local_search_engine_url("CN")
        assert "baidu" in url

    def test_generate_referer_for_region_url(self):
        """保护：generate_referer_for_region(country, language, keyword)
        返回合法 http/https URL（或 None）。"""
        from seo_query_module import get_seo_query
        q = get_seo_query()
        ref = q.generate_referer_for_region("JP", "ja", keyword="テスト")
        assert ref is None or (
            isinstance(ref, str) and ref.startswith(("http://", "https://"))
        )

    def test_generate_referer_for_region_no_keyword(self):
        """保护：不传 keyword 时从对应语言池随机采样，仍返回合法 URL。"""
        from seo_query_module import get_seo_query
        q = get_seo_query()
        ref = q.generate_referer_for_region("DE", "de")
        assert ref is None or ref.startswith(("http://", "https://"))

    def test_generate_referer_backward_compat(self):
        """保护：generate_referer(engine_id, keyword) 向后兼容，返回 str 或 None。"""
        from seo_query_module import get_seo_query
        q = get_seo_query()
        ref = q.generate_referer("google", "affiliate marketing")
        assert ref is None or ref.startswith(("http://", "https://"))
        # 未知引擎 → None
        assert q.generate_referer("no_such_engine", "kw") is None

    def test_region_engine_map_contract_constants(self):
        """保护：REGION_SEARCH_ENGINE_MAP 关键地域映射不被破坏（CONTRACT 清单）。"""
        from seo_query_module import REGION_SEARCH_ENGINE_MAP
        for cc, fragment in SEO_QUERY_CONTRACT["REGION_SEARCH_ENGINE_MAP"].items():
            assert fragment in REGION_SEARCH_ENGINE_MAP[cc]


# ======================================================================
# 4. ip_info_resolver 契约
# ======================================================================
class TestIPInfoResolverContract:
    """ip_info_resolver：resolve_ip_info 返回体字段契约（全程 mock，不联网）"""

    def test_resolve_ip_info_with_proxy_info_full(self):
        """保护：代理已返回三要素时，resolve_ip_info 不联网直接成功，
        返回体必须携带 success/ip/country_code/country_name/timezone/language。"""
        from ip_info_resolver import resolve_ip_info
        proxy_info = {
            "country": "JP",
            "timezone": "Asia/Tokyo",
            "language": "ja-JP",
        }
        result = resolve_ip_info("203.0.113.88", proxy_ip_info=proxy_info)
        assert isinstance(result, dict)
        for key in IP_RESOLVER_CONTRACT["resolve_ip_info"]["required_keys"]:
            assert key in result
        assert result["success"] is True
        assert result["country_code"] == "JP"
        assert result["timezone"] == "Asia/Tokyo"
        assert result["language"] == "ja-JP"

    @patch("ip_info_resolver._http_get_json", return_value=None)
    def test_resolve_ip_info_never_hits_network(self, mock_get):
        """保护：缺失字段时通过 mock 注入使网络层返回 None，
        验证 resolve_ip_info 在无网络下仍返回合规 dict 结构。"""
        from ip_info_resolver import resolve_ip_info
        result = resolve_ip_info("203.0.113.99", proxy_ip_info={})
        assert isinstance(result, dict)
        for key in IP_RESOLVER_CONTRACT["resolve_ip_info"]["required_keys"]:
            assert key in result
        # 无任何来源时 success 必须为 False（宁缺毋假）
        assert result["success"] is False

    @patch("ip_info_resolver._http_get_json")
    def test_resolve_ip_info_fills_from_mock_api(self, mock_get):
        """保护：代理缺字段时，用 mock 注入 ip-api 返回体补齐
        country/timezone/language，校验三要素齐备则 success=True。"""
        from ip_info_resolver import resolve_ip_info
        mock_get.return_value = {
            "status": "success",
            "countryCode": "DE",
            "country": "Germany",
            "timezone": "Europe/Berlin",
            "query": "203.0.113.99",
        }
        result = resolve_ip_info("203.0.113.99", proxy_ip_info={})
        assert result["success"] is True
        assert result["country_code"] == "DE"
        assert result["timezone"] == "Europe/Berlin"
        # language 由 COUNTRY_TO_LANGUAGE 映射兜底
        assert result["language"] == "de-DE"


# ======================================================================
# 5. popunder_trigger 契约
# ======================================================================
class TestPopunderTriggerContract:
    """popunder_trigger：模块可导入 + 关键函数存在（hasattr，不触发浏览器）"""

    def test_module_importable(self):
        """保护：popunder_trigger 模块可独立导入（不依赖浏览器环境）。"""
        import popunder_trigger
        assert popunder_trigger is not None

    def test_exported_functions_exist(self):
        """保护：导出的关键函数必须存在（hasattr 断言，不实际触发浏览器）。"""
        import popunder_trigger
        for name in POPUNDER_CONTRACT["exported_funcs"]:
            assert hasattr(popunder_trigger, name), f"缺少导出函数 {name}"
            assert callable(getattr(popunder_trigger, name))

    def test_pure_flag_functions_no_browser(self):
        """保护：should_trigger_for_network / is_ip_safe_for_hilltopads 纯函数
        不启动浏览器即返回 bool（供 CDP 触发前的门禁判断）。"""
        import popunder_trigger as pt
        assert pt.should_trigger_for_network("HilltopAds") is True
        assert pt.should_trigger_for_network("无") is False
        assert pt.is_ip_safe_for_hilltopads(
            {"ip_type": "residential", "isp": "Comcast"}
        ) is True
        assert pt.is_ip_safe_for_hilltopads(
            {"ip_type": "datacenter", "isp": "DigitalOcean"}
        ) is False