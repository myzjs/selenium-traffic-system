# -*- coding: utf-8 -*-
"""
test_audit_findings_v26_8_13_2.py —— 26.8.13.2 深度审计缺陷验证测试

覆盖本轮深度审计发现并修复的全部 🔴 阻断级问题：
  A1 UAPoolManager 锁应为可重入 RLock（自死锁）
  A2 日志 HTML 白名单过滤（存储型 XSS）
  A4 video_ad 配置键缺失/None 直接索引崩溃
  A5 _vp_coord 小视口 random.randint 下界>上界 ValueError
  A7 daily_plan 旧格式缺失键 KeyError
  B2 剩余时间不足 random.uniform 下界>上界 ValueError
  B3 /save_config 非法类型写入 config 污染
  C1 redteam 蓝图挂载
  D1/D2 redteam 持锁写日志死锁
  D3 golden JSONL "w" 覆盖写丢失数据
  D4 Playwright page.request 无 .headers 属性
  E1 IP 去重冲突最后一次尝试仍 fall-through 放行
  E2 Page.close() 未停止请求采集轮询线程
  E3 NAT/特殊用途 IP 段误判为美欧
"""
import json
import os
import re
import sys
import tempfile
import threading

import pytest

# 确保项目根目录在path中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# 辅助：创建Flask测试客户端
# ============================================================
@pytest.fixture(scope="module")
def app_client():
    """导入app并创建测试客户端（不启动服务器）"""
    os.environ.setdefault("RUN_PORT", "15999")
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client, app_module


def _read_project_file(name):
    with open(os.path.join(_PROJ, name), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# A1: UAPoolManager._bucket_lock 应为 RLock（可重入）
# ============================================================
class TestA1_UABucketRLock:
    """get_ua 持锁期间调用 _save_buckets（内部再次加锁），普通 Lock 会自死锁。"""

    def test_bucket_lock_is_rlock_assignment(self):
        content = _read_project_file("app.py")
        assert "self._bucket_lock = threading.RLock()" in content, \
            "_bucket_lock 必须赋值为 threading.RLock()（可重入）"
        # 排除普通 Lock 赋值
        bad = re.findall(r"self\._bucket_lock\s*=\s*threading\.Lock\(\)", content)
        assert not bad, "存在 self._bucket_lock = threading.Lock() 的死锁隐患"

    def test_lock_bucket_has_noop_fallback(self):
        content = _read_project_file("app.py")
        assert "_NoopLock" in content, "_lock_bucket 应提供 NoopLock 兜底（锁不可用时不崩溃）"
        assert "return self._bucket_lock if self._bucket_lock is not None else _NoopLock()" in content


# ============================================================
# A2: _sanitize_log_html 白名单过滤
# ============================================================
class TestA2_SanitizeLogHtml:
    """日志消息中任意原始 HTML 直接以 safe 渲染会执行注入脚本。"""

    def test_script_tag_escaped(self):
        from app import _sanitize_log_html
        out = _sanitize_log_html('<script>alert(1)</script>')
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_img_onerror_escaped(self):
        from app import _sanitize_log_html
        out = _sanitize_log_html('<img src=x onerror=alert(1)>')
        assert "<img" not in out
        assert "&lt;img" in out

    def test_whitelist_tag_preserved(self):
        from app import _sanitize_log_html
        out = _sanitize_log_html('<b>bold</b> 与 <i>italic</i>')
        assert "<b>bold</b>" in out
        assert "<i>italic</i>" in out

    def test_span_color_preserved(self):
        from app import _sanitize_log_html
        out = _sanitize_log_html('<span style="color:#ff0000">red</span>')
        assert '<span style="color:#ff0000">red</span>' in out

    def test_span_non_color_attrs_stripped(self):
        from app import _sanitize_log_html
        out = _sanitize_log_html('<span style="color:red" onclick="x()">t</span>')
        # 非 #hex 样式或携带事件属性的 span 不应保留危险属性
        assert "onclick" not in out

    def test_non_string_input_safe(self):
        from app import _sanitize_log_html
        # 非字符串输入按 str() 兜底，不崩溃且不出现原始标签
        out = _sanitize_log_html(None)
        assert isinstance(out, str)
        assert "<script>" not in out


# ============================================================
# A4: video_ad 配置键缺失/None
# ============================================================
class TestA4_VideoAdConfigDefaults:
    """config 缺 video_ad 键或为 None 时直接 config["video_ad"] 会 KeyError/AttributeError。"""

    def test_video_ad_uses_get_with_default(self):
        content = _read_project_file("app.py")
        assert re.search(r'config\.get\("video_ad", \{\}\) or \{\}', content), \
            "必须使用 config.get('video_ad', {}) or {} 兜底"
        assert "_video_ad_cfg = config.get(\"video_ad\", {}) or {}" in content


# ============================================================
# A5: _vp_coord 小视口下界>上界
# ============================================================
class TestA5_VpCoordSmallViewport:
    """random.randint(100, width-100) 在视口≤200px 时下界>上界抛 ValueError。"""

    def test_vp_coord_guard_code_exists(self):
        content = _read_project_file("app.py")
        assert "_m = min(100, max(1, _size // 3))" in content
        assert "random.randint(_m, max(_m, _size - _m))" in content

    def test_vp_coord_logic_no_crash_small_viewports(self):
        import random

        def _vp_coord(size, default):
            try:
                _size = int(size or default)
            except Exception:
                _size = default
            if _size <= 0:
                return 0
            _m = min(100, max(1, _size // 3))
            return random.randint(_m, max(_m, _size - _m))

        for size in (0, 1, 2, 5, 10, 50, 100, 150, 199, 200, 500, 1920):
            v = _vp_coord(size, 1920)
            assert isinstance(v, int) and v >= 0, f"size={size} 返回值非法: {v}"


# ============================================================
# A7: daily_plan 旧格式缺失键
# ============================================================
class TestA7_DailyPlanMissingKeys:
    """断点恢复的旧格式 daily_plan 可能缺失键，直接索引会 KeyError 中断任务。"""

    def test_total_tasks_get_with_default(self):
        content = _read_project_file("app.py")
        assert 'daily_plan.get("total_tasks", 0) or 0' in content
        assert 'daily_plan.get("model_used", "unknown") or "unknown"' in content
        assert 'daily_plan.get("site_age", 0) or 0' in content
        assert 'daily_plan.get("tasks", []) or []' in content


# ============================================================
# B2: 剩余时间不足 random.uniform 下界>上界
# ============================================================
class TestB2_RemainingTimeGuard:
    """available_time < _stay_min 时 random.uniform(min, available_time) 抛 ValueError。"""

    def test_guard_code_exists(self):
        content = _read_project_file("app.py")
        assert "remaining_time = max(0.0, task_deadline - time.time())" in content
        assert "remaining_time / max(1, chapter_loop_count - _r)" in content
        assert "if available_time < _stay_min:" in content
        assert "round_time = available_time" in content

    def test_remaining_time_logic_no_crash(self):
        import random
        # 模拟 B2 修复后的时间分配逻辑
        task_deadline = 100.0
        remaining_time = max(0.0, task_deadline - 60.0)  # 剩 40s
        chapter_loop_count = 5
        stays = []
        for _r in range(chapter_loop_count):
            max_round_time = 30.0
            available_time = min(max_round_time, remaining_time / max(1, chapter_loop_count - _r))
            _stay_min = 15.0
            if available_time < _stay_min:
                round_time = available_time
            else:
                round_time = random.uniform(_stay_min, available_time)
            stays.append(round_time)
            remaining_time -= round_time
        assert all(s >= 0 for s in stays)


# ============================================================
# B3: /save_config 非法类型校验
# ============================================================
class TestB3_ValidateConfigTypes:
    """对象型/布尔型键错误类型、NaN/Infinity 写入 config 会导致运行期随机崩溃。"""

    def test_dict_key_wrong_type_rejected(self):
        from app import _validate_config_types
        bad = _validate_config_types({"total_stay": "not-a-dict"})
        assert any("total_stay" in b for b in bad)

    def test_dict_key_correct_type_accepted(self):
        from app import _validate_config_types
        assert _validate_config_types({"total_stay": {"min": 1, "max": 5}}) == []

    def test_bool_key_wrong_value_rejected(self):
        from app import _validate_config_types
        bad = _validate_config_types({"webrtc_leak_check_enabled": "yes"})
        assert any("webrtc_leak_check_enabled" in b for b in bad)

    def test_bool_key_string_true_accepted(self):
        from app import _validate_config_types
        assert _validate_config_types({"webrtc_leak_check_enabled": "true"}) == []
        assert _validate_config_types({"webrtc_leak_check_enabled": True}) == []

    def test_nan_rejected(self):
        from app import _validate_config_types
        bad = _validate_config_types({"some_float": float("nan")})
        assert any("NaN" in b for b in bad)

    def test_infinity_rejected(self):
        from app import _validate_config_types
        bad = _validate_config_types({"some_float": float("inf")})
        assert any("Infinity" in b for b in bad)

    def test_save_config_invalid_type_returns_400(self, app_client):
        client, app_module = app_client
        resp = client.post('/save_config', json={"total_stay": "not-a-dict"})
        assert resp.status_code == 400, \
            f"非法类型应返回400，实际 {resp.status_code}"


# ============================================================
# C1: redteam 蓝图挂载
# ============================================================
class TestC1_RedteamMount:
    """app.py 需挂载 /redteam 蓝图，否则红队评估 Tab 与 API 404。"""

    def test_redteam_blueprint_registered(self, app_client):
        client, app_module = app_client
        names = [bp.name for bp in app_module.app.iter_blueprints()]
        assert "redteam" in names, "redteam 蓝图未注册"

    def test_redteam_scenarios_api_ok(self, app_client):
        client, app_module = app_client
        resp = client.get('/redteam/api/scenarios')
        assert resp.status_code == 200
        data = resp.get_json()
        assert "dimensions" in data


# ============================================================
# D1/D2: redteam 持锁写日志死锁
# ============================================================
class TestD1_D2_ApiStop:
    """_append_log 内部重新获取 _rt_lock，持锁调用必现死锁（普通 Lock 不可重入）。"""

    def test_stop_when_not_running_no_deadlock(self, app_client):
        client, app_module = app_client
        from redteam_webui import _rt_state, _rt_lock
        with _rt_lock:
            _rt_state["running"] = False
            _rt_state["stop_requested"] = False
        resp = client.post("/redteam/api/stop")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "warning"

    def test_stop_when_running_sets_flag(self, app_client):
        client, app_module = app_client
        from redteam_webui import _rt_state, _rt_lock
        with _rt_lock:
            _rt_state["running"] = True
            _rt_state["stop_requested"] = False
        try:
            resp = client.post("/redteam/api/stop")
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["status"] == "ok"
            with _rt_lock:
                assert _rt_state["stop_requested"] is True
        finally:
            with _rt_lock:
                _rt_state["running"] = False
                _rt_state["stop_requested"] = False


# ============================================================
# D3: golden JSONL 回填保留解析失败行
# ============================================================
class _FakeEval:
    """模拟 evaluate_golden_vs_system 返回值。"""

    def __init__(self, tpr, total=3):
        self.date_str = "2026-08-13"
        self.total_tasks = total
        self.baseline_count = 1
        self.fraud_count = 2
        self.suspicious_count = 0
        self.tpr = tpr
        self.fpr = 0.1
        self.precision = 0.8
        self.f1 = 0.7
        self.dimension_recall = 0.5
        self.scenario_recall = 0.5
        self.tag_recall = 0.5
        self.unmatched_records = []


class TestD3_BackfillPreservesBrokenLines:
    """回填 system_verdict 不得用 "w" 覆盖 JSONL，否则丢失解析失败行与其他记录。"""

    def test_backfill_preserves_broken_and_filled_lines(self, monkeypatch, tmp_path):
        import redteam_webui
        # 构造 golden JSONL：行1=缺 system_verdict；行2=无效 JSON；行3=已有 verdict
        rdir = tmp_path / "reports" / "redteam"
        rdir.mkdir(parents=True)
        path = rdir / "redteam_golden_2026-08-13.jsonl"
        lines = [
            json.dumps({"expected_verdict": "normal", "severity": 3, "country_code": "US"}),
            "this is a broken non-json line",
            json.dumps({"expected_verdict": "fraud", "severity": 5,
                        "system_verdict": "fraud", "country_code": "CN"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        fake_state = {
            "running": True, "progress": 0, "stage": "测试", "task_total": 0,
            "task_done": 0, "logs": [], "summary": None, "evaluation": None,
            "report_date": "2026-08-13", "run_id": "t-d3", "mode": "dry_run",
            "targets": [], "stop_requested": False, "started_at": "2026-08-13 10:00:00",
        }
        calls = {"n": 0}

        def fake_eval(**kwargs):
            calls["n"] += 1
            return _FakeEval(None if calls["n"] == 1 else 0.5)

        monkeypatch.setattr(redteam_webui, "_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(redteam_webui, "_rt_state", fake_state)
        monkeypatch.setattr(redteam_webui, "evaluate_golden_vs_system", fake_eval)

        redteam_webui._run_redteam_thread(
            task_count=0, baseline_pct=0.0, mode="dry_run",
            candidates=[], weighted=False, scenario_ids=[],
            headless=False, run_id="t-d3",
        )

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == 3, f"回填后行数应为3（不允许覆盖丢失行），实际 {len(after)}"
        # 解析失败行必须原样保留
        assert after[1] == "this is a broken non-json line"
        # 行1 已被回填 system_verdict
        r0 = json.loads(after[0])
        assert r0.get("system_verdict") in ("normal", "fraud")
        # 行3 已有 verdict 不被改动
        r2 = json.loads(after[2])
        assert r2.get("system_verdict") == "fraud"

    def test_backfill_uses_atomic_replace(self):
        content = _read_project_file("redteam_webui.py")
        # 不允许直接 "w" 覆盖 golden JSONL
        assert re.search(r'open\([^\n]*redteam_golden[^\n]*["\']w["\']', content) is None, \
            "回填逻辑不得以 'w' 覆盖 golden JSONL"
        assert "tempfile.mkstemp" in content and "os.replace" in content, \
            "回填写回必须使用临时文件 + os.replace 原子写入"


# ============================================================
# D4: Playwright page.request 无 .headers 属性
# ============================================================
def _make_mock_page():
    """模拟 Playwright page（含 D4 需要的 on/reload）"""
    from unittest.mock import MagicMock
    page = MagicMock()
    page.evaluate.return_value = None
    page.goto.return_value = None
    page.viewport_size = {"width": 1920, "height": 1080}
    page.mouse = MagicMock()
    page.request = MagicMock()
    page.request.headers = {}
    page.handlers = {}
    page.on.side_effect = lambda ev, h: page.handlers.__setitem__(ev, h)
    page.reload.return_value = None
    return page


def _make_evaluate_side_effect(webdriver=False, referer="https://google.com"):
    def side_effect(*args):
        s = args[0] if args else ""
        if "viewport_larger_than_screen" in s:
            return {"screen": [1920, 1080], "viewport": [1900, 1000],
                    "viewport_larger_than_screen": False, "viewport_ratio": 0.99}
        if "hardwareConcurrency" in s:
            return {"hardwareConcurrency": 8, "deviceMemory": 8,
                    "hc_reasonable": True, "hc_suspicious": False,
                    "dm_reasonable": True, "dm_suspicious": False}
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
        if "navigator.webdriver" in s:
            return webdriver
        if "cdc_" in s:
            return False
        if "window.chrome" in s and "runtime" in s:
            return True
        if "Intl.DateTimeFormat" in s:
            return "Asia/Shanghai"
        if "document.referrer" in s:
            return referer
        if "gl.RENDERER" in s or "gl.VENDOR" in s:
            return {"renderer": "ANGLE (Google)", "vendor": "Google Inc.", "has_hook": True}
        if "getParameter(" in s and "gl" in s:
            return {"renderer": "ANGLE (Google)", "vendor": "Google Inc.", "has_hook": True}
        if "getContext('2d')" in s or "getImageData" in s:
            return {"data": "test_hash_12345", "has_noise": True}
        if "createElement('canvas')" in s:
            return {"data": "test_hash_12345", "has_noise": True}
        if "RTCPeerConnection" in s:
            return False
        if "customSelector" in s or "adSelector" in s:
            return {"ads_found": 0, "hidden_ads": 0, "exposed_ads": 0}
        if "localStorage" in s and "length" in s:
            return 3
        if "document.cookie" in s and "length" in s:
            return 2
        if "sessionStorage" in s:
            return 3
        return None
    return side_effect


class TestD4_RequestHeaderCapture:
    """Playwright Python 的 page.request 是 APIRequestContext，无 .headers。"""

    def _run(self, page, tmp_path):
        import risk_check
        from unittest.mock import patch
        with patch.object(risk_check, "REPORT_DIR", str(tmp_path)):
            return risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")

    def test_captured_headers_used(self, tmp_path):
        from unittest.mock import MagicMock
        page = _make_mock_page()
        page.evaluate.side_effect = _make_evaluate_side_effect()
        # 模拟 reload 触发导航：页面收到 document 请求并携带真实请求头
        def _reload_trigger(*a, **k):
            req = MagicMock()
            req.resource_type = "document"
            req.headers = {"Accept-Language": "en-US", "Sec-Ch-Ua": '"Not A;Brand"'}
            page.handlers["request"](req)
        page.reload.side_effect = _reload_trigger

        result = self._run(page, tmp_path)
        net = result["network_ip"]
        assert net["missing_accept_lang"] is False, "捕获到 Accept-Language 不应判缺失"
        assert net["missing_sec_ch_ua"] is False, "捕获到 Sec-Ch-Ua 不应判缺失"

    def test_fallback_page_request_headers(self, tmp_path):
        page = _make_mock_page()
        page.evaluate.side_effect = _make_evaluate_side_effect()
        # 不触发 request handler，仅兜底 page.request.headers
        page.request.headers = {"Accept-Language": "en-US"}
        result = self._run(page, tmp_path)
        net = result["network_ip"]
        assert net["missing_accept_lang"] is False
        assert net["missing_sec_ch_ua"] is True, "无 Sec-Ch-Ua 时应判缺失"


# ============================================================
# E1: IP 去重冲突最后一次尝试必须返回失败
# ============================================================
class TestE1_DedupConflict:
    """最后一次尝试仍撞去重冲突时，不允许 fall-through 放行重复 IP。"""

    def test_last_attempt_dedup_conflict_returns_failure(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        import ip_provider as ip_provider_module
        from ip_provider import IPProvider, acquire_ip_use
        ip = "203.0.113.77"  # TEST-NET-3 保留地址，避免与真实使用记录冲突
        # 先占用该 IP，使后续获取必然撞去重
        assert acquire_ip_use(ip) is True
        provider = IPProvider("proxy_api")
        # mock 网络层：IPDeep 返回可解析文本；IP 详情固定返回同一重复 IP
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "1.2.3.4:8080:user:pass"
        monkeypatch.setattr(ip_provider_module.requests, "get", lambda *a, **k: resp)
        monkeypatch.setattr(provider, "_get_ip_details",
                            lambda *a, **k: {"ip": ip, "ip_type": "isp_trust_unknown",
                                             "country_code": "US"})
        provider.configure_proxy_api([{
            "enabled": True, "proxy_api_url": "http://x",
            "proxy_user": "u", "proxy_pwd": "p", "country_code": "US",
        }])
        result = provider.get_ip()
        assert result["success"] is False, \
            "所有尝试均去重冲突时必须返回 success=False（禁止放行重复IP）"
        assert "去重" in result.get("error", ""), \
            f"错误信息应含去重说明: {result.get('error')}"


# ============================================================
# E2: Page.close() 停止请求采集轮询线程
# ============================================================
class TestE2_PageCloseStopsPolling:
    """close() 必须先停止采集轮询线程，否则线程泄漏/空转。"""

    def test_close_stops_collection_thread(self):
        from unittest.mock import MagicMock
        from selenium_bridge import Page
        p = Page.__new__(Page)
        p._collecting = True
        p._collect_thread = MagicMock()
        p._collect_thread.is_alive.return_value = True
        p._collect_stop = threading.Event()
        p._window_handle = None
        p.driver = MagicMock()
        p.driver.window_handles = ["h1"]
        p._context = None
        p.close()
        assert p._collecting is False, "close() 后 _collecting 应为 False"
        assert p._collect_thread is None, "close() 后采集线程应置 None"

    def test_close_idempotent_when_not_collecting(self):
        from unittest.mock import MagicMock
        from selenium_bridge import Page
        p = Page.__new__(Page)
        p._collecting = False
        p._collect_thread = None
        p._collect_stop = threading.Event()
        p._window_handle = None
        p.driver = MagicMock()
        p.driver.window_handles = ["h1"]
        p._context = None
        p.close()  # 不应抛异常


# ============================================================
# E3: NAT/特殊用途 IP 段移除
# ============================================================
class TestE3_SpecialPurposeIps:
    """176/8（中东）、39/8、100/8（CGNAT）、172/8（NAT）不得再被误判为美欧。"""

    def test_removed_ranges_not_us_eu(self):
        from ip_region_module import get_ip_recognizer, REGION_OTHER
        rec = get_ip_recognizer()
        for ip in ("176.1.1.1", "39.1.1.1", "100.64.0.1", "172.20.1.1", "172.16.0.1"):
            assert rec.recognize_region(ip) == REGION_OTHER, \
                f"{ip} 不应被识别为美欧"

    def test_china_still_recognized(self):
        from ip_region_module import get_ip_recognizer, REGION_CHINA
        rec = get_ip_recognizer()
        assert rec.recognize_region("223.5.5.5") == REGION_CHINA  # 阿里公共DNS，中国段
        assert rec.recognize_region("119.29.29.29") == REGION_CHINA  # DNSPod，中国段

    def test_source_ranges_no_longer_contain_removed_blocks(self):
        content = _read_project_file("ip_region_module.py")
        us_eu = content.split("us_eu_ip_ranges", 1)[1].split("def recognize_region", 1)[0]
        for bad in ('"176.', '"39.', '"100.', '"172.'):
            assert bad not in us_eu, f"us_eu_ip_ranges 仍包含已移除的 {bad} 段"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
