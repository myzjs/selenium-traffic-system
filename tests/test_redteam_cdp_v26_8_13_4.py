"""
26.8.13.5 回归测试：
1. 红队 19 场景模块可导入、场景注册完整（redteam_scenarios / reporter / integration / webui）
2. selenium_bridge._ensure_cdp_capable 动态补齐 execute_cdp_cmd（无原生方法 / 原生方法存在 / 防递归标记）
3. _CDPSession.send 方法 1 排除动态绑定方法（防止递归）
4. risk_check.fixed_chrome_version 误报修复（真实 Chrome UA 不误判，Headless/占位版本才置位）
5. Selenium 4.27（VPS）ChromiumDriver 无 command_executor 参数 → _launch_chrome_with_timeout
   必须回退 WebDriver 基类构造（26.8.13.5 修复，防止"所有浏览器启动方式均失败"）
全部使用 Mock，不依赖真实浏览器 / 网络。
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock


# ============ 1. 红队模块导入与场景完整性 ============

class TestRedTeamModulesImport:
    """红队模块可导入性（VPS 部署失败根因：依赖文件缺失导致 import 失败被静默吞掉）"""

    def test_redteam_scenarios_importable(self):
        import redteam_scenarios
        assert hasattr(redteam_scenarios, "RedTeamScenarioLibrary")
        assert hasattr(redteam_scenarios, "apply_scenario_to_task")

    def test_redteam_reporter_importable(self):
        import redteam_reporter
        assert hasattr(redteam_reporter, "RedTeamEvaluation")
        assert hasattr(redteam_reporter, "evaluate_golden_vs_system")

    def test_redteam_integration_importable(self):
        import redteam_integration

    def test_redteam_webui_importable(self):
        import redteam_webui
        assert hasattr(redteam_webui, "mount_on_app")

    def test_redteam_real_task_hook_importable(self):
        import redteam_real_task_hook_example
        assert hasattr(redteam_real_task_hook_example, "real_task_hook_for_redteam")


class TestRedTeamScenarioCount:
    """红队 19 场景注册完整性：RT_BASELINE_NORMAL + 18 个攻击场景"""

    def _library(self):
        from redteam_scenarios import RedTeamScenarioLibrary
        return RedTeamScenarioLibrary()

    def test_total_scenario_count_is_19(self):
        lib = self._library()
        scenarios = lib.all()
        assert len(scenarios) == 19, f"场景总数应为 19，实际 {len(scenarios)}"

    def test_baseline_scenario_present(self):
        lib = self._library()
        ids = [s.scenario_id for s in lib.all()]
        assert "RT_BASELINE_NORMAL" in ids

    def test_all_scenario_ids_unique(self):
        lib = self._library()
        ids = [s.scenario_id for s in lib.all()]
        assert len(ids) == len(set(ids)), "存在重复场景 id"

    def test_attack_scenarios_not_baseline(self):
        lib = self._library()
        attack_ids = [s.scenario_id for s in lib.all() if s.expected_verdict != "normal"]
        assert len(attack_ids) == 18, f"攻击场景应为 18（不含基线），实际 {len(attack_ids)}"


# ============ 2. _ensure_cdp_capable 动态补齐 ============

class TestEnsureCdpCapable:
    """execute_cdp_cmd 能力兜底（演练 7 项问题根因：CDP 命令静默失败）"""

    def _driver_cls_no_cdp(self):
        """模拟 WebDriver 基类：没有 execute_cdp_cmd"""
        class FakeDriver:
            def __init__(self):
                self.session_id = "fake-session"
                self.command_executor = MagicMock()
        return FakeDriver

    def test_binds_dynamic_cdp_cmd_when_missing(self):
        from selenium_bridge import _ensure_cdp_capable
        driver = self._driver_cls_no_cdp()()
        assert not hasattr(driver, "execute_cdp_cmd")
        _ensure_cdp_capable(driver)
        assert callable(getattr(driver, "execute_cdp_cmd", None)), "应动态绑定 execute_cdp_cmd"
        # 打上 _dynamic_cdp 标记，防止 _CDPSession.send 方法1 递归
        assert getattr(driver.execute_cdp_cmd, "_dynamic_cdp", False) is True

    def test_keeps_native_cdp_cmd(self):
        from selenium_bridge import _ensure_cdp_capable
        driver = self._driver_cls_no_cdp()()
        def native(cmd, cmd_args=None):
            return {"native": True}
        driver.execute_cdp_cmd = native
        _ensure_cdp_capable(driver)
        assert driver.execute_cdp_cmd is native, "原生方法不应被覆盖"

    def test_dynamic_cdp_cmd_delegates_to_cdpsession(self):
        from selenium_bridge import _ensure_cdp_capable, _CDPSession
        driver = self._driver_cls_no_cdp()()
        # 让 _CDPSession.send 方法2 走 driver.execute（mock 返回）
        driver.execute = MagicMock(return_value={"value": {"result": "ok"}})
        _ensure_cdp_capable(driver)
        out = driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 1920})
        assert out == {"result": "ok"}


# ============ 3. _CDPSession.send 防递归 ============

class TestCdpSessionNoRecursion:
    """动态绑定的 execute_cdp_cmd 不能再次进入方法1（否则无限递归）"""

    def test_send_skips_dynamic_method_in_route1(self):
        from selenium_bridge import _CDPSession
        driver = MagicMock()
        driver.session_id = "s1"
        driver.command_executor = MagicMock()

        def dynamic(cmd, cmd_args=None):
            raise AssertionError("方法1 不应调用动态绑定的 execute_cdp_cmd（递归！）")
        dynamic._dynamic_cdp = True
        driver.execute_cdp_cmd = dynamic

        # 方法2 mock 成功 → 走方法2，不触发方法1 递归
        driver.execute = MagicMock(return_value={"value": {"ok": 1}})
        sess = _CDPSession(driver)
        out = sess.send("Page.enable", {})
        assert out == {"ok": 1}
        driver.execute.assert_called_once()

    def test_native_method_route1_still_works(self):
        from selenium_bridge import _CDPSession
        calls = []

        class FakeDriver:
            session_id = "s1"
            def __init__(self):
                self.execute = MagicMock()
                self.command_executor = MagicMock()
            def execute_cdp_cmd(self, cmd, cmd_args=None):
                calls.append((cmd, cmd_args))
                return {"native": True}

        driver = FakeDriver()
        sess = _CDPSession(driver)
        out = sess.send("Page.enable", {})
        assert out == {"native": True}
        assert calls == [("Page.enable", {})]


# ============ 4. fixed_chrome_version 误报修复 ============

def make_ua_page(ua):
    """构造 page mock：navigator.userAgent 返回指定 UA，其余评估返回安全默认值"""
    page = MagicMock()
    page.viewport_size = {"width": 1920, "height": 1080}
    page.mouse = MagicMock()
    page.request = MagicMock()
    page.request.headers = {}

    def side_effect(*args):
        s = args[0] if args else ""
        # 视口-屏幕一致性脚本含 innerWidth/screen.width，必须先于其它分支匹配
        if "viewport_larger_than_screen" in s:
            return {
                "screen": [1920, 1080], "viewport": [1900, 1000],
                "viewport_larger_than_screen": False, "viewport_ratio": 0.99,
            }
        # AdSense 合规脚本含 innerWidth，必须先于视口分支匹配
        if "ads_txt_accessible" in s:
            return {
                "ads_txt_accessible": False, "has_ad_click_encouragement": False,
                "above_fold_ad_count": 0, "total_ads_on_page": 0,
                "ad_to_content_ratio": 0, "has_privacy_policy_link": False,
                "has_cookie_consent": False,
            }
        # 广告检测脚本含 innerWidth/innerHeight，必须先于视口分支匹配
        if "customSelector" in s:
            return {
                "detect_mode": "自动识别扫描", "total_ad_count": 0,
                "valid_expose_count": 0, "hidden_ad_count": 0,
                "non_standard_size_count": 0, "css_distorted_count": 0,
                "overlapping_count": 0, "ads_per_viewport": 0, "ad_list": []
            }
        if "navigator.userAgent" in s:
            return ua
        if "navigator.platform" in s:
            return "Linux x86_64"
        if "navigator.language" in s:
            return "en-US"
        if "navigator.languages" in s:
            return ["en-US"]
        if "screen.width" in s:
            return [1920, 1080]
        if "innerWidth" in s:
            return [1900, 1000]
        if "devicePixelRatio" in s:
            return 1
        if "colorDepth" in s:
            return 24
        if "navigator.webdriver" in s:
            return False
        if "cdc_" in s:
            return False
        if "chrome?.runtime" in s or "window.chrome" in s:
            return True
        if "Intl.DateTimeFormat" in s:
            return "Asia/Shanghai"
        if "document.referrer" in s:
            return "https://www.google.com/"
        if "plugins.length" in s:
            return 5
        if "mimeTypes.length" in s:
            return 4
        if "mediaDevices" in s:
            return True
        if "fonts.size" in s:
            return 10
        if "getImageData" in s or "getContext('2d')" in s:
            return {"data": "hash", "has_noise": True}
        if "gl.RENDERER" in s or "getParameter(" in s:
            return {"renderer": "ANGLE (Google)", "vendor": "Google Inc.", "has_hook": True}
        if "RTCPeerConnection" in s:
            return False
        if "localStorage" in s and "length" in s:
            return 3
        if "sessionStorage" in s:
            return 3
        if "document.cookie" in s and "length" in s:
            return 2
        return None
    page.evaluate.side_effect = side_effect
    return page


REAL_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 HeadlessChrome/131.0.0.0"
)


class TestFixedChromeVersion:
    """真实 Chrome UA 形态 Chrome/X.0.0.0 不应再误判为"固定版本"（26.8.13.4 修复）"""

    def _ua_check(self, ua):
        import risk_check
        page = make_ua_page(ua)
        result = risk_check.run_risk_detect(page, proxy_ip="8.8.8.8")
        return result["http_header"]["ua_check"]

    def test_real_chrome_ua_not_fixed(self):
        check = self._ua_check(REAL_CHROME_UA)
        assert check["fixed_chrome_version"] is False, (
            f"真实 Chrome UA 不应误报 fixed_chrome_version: {check}"
        )
        assert check["ua_risk_flag"] is False

    def test_headless_chrome_ua_fixed(self):
        check = self._ua_check(HEADLESS_UA)
        assert check["fixed_chrome_version"] is True, (
            f"Headless UA 应判定 fixed_chrome_version: {check}"
        )
        assert check["ua_risk_flag"] is True


# ============ 5. Selenium 4.27（VPS）driver 构造兼容 ============

class TestDriverConstructionSelenium427Compat:
    """VPS Selenium 4.27.1 的 ChromiumDriver 无 command_executor 参数。

    26.8.13.5 修复：_launch_chrome_with_timeout 对 command_executor 构造抛 TypeError
    时回退 WebDriver 基类（基类始终支持 command_executor），CDP 由 _ensure_cdp_capable 补齐。
    否则 VPS 上"所有浏览器启动方式均失败"，112 任务计划全部失败。
    """

    def test_launcher_source_has_typeerror_fallback(self):
        """源码级护栏：构造 driver 必须存在 TypeError 回退逻辑（防未来被删）"""
        import inspect
        import selenium_bridge
        src = inspect.getsource(selenium_bridge._launch_chrome_with_timeout)
        assert "except TypeError" in src, "缺少 TypeError 回退分支（Selenium 4.27 兼容）"
        assert "_BaseWebDriver" in src, "回退分支必须使用 WebDriver 基类构造"
        assert "_ensure_cdp_capable(driver)" in src, "回退后仍须补齐 CDP 能力"

    def test_some_driver_ctor_supports_command_executor_in_env(self):
        """当前环境至少有一条构造路径可用：ChromiumDriver(4.46+) 或 WebDriver 基类"""
        import inspect
        from selenium.webdriver.chromium.webdriver import ChromiumDriver
        from selenium.webdriver.remote.webdriver import WebDriver
        sig_c = inspect.signature(ChromiumDriver.__init__)
        sig_b = inspect.signature(WebDriver.__init__)
        assert (
            "command_executor" in sig_c.parameters
            or "command_executor" in sig_b.parameters
        ), "当前环境没有任何支持 command_executor 的 driver 构造路径"

    def test_fallback_uses_webdriver_base_with_cdp_backfill(self):
        """模拟 4.27 场景：ChromiumDriver 抛 TypeError → 走基类并补齐 CDP。

        直接验证 _ensure_cdp_capable 对"无 execute_cdp_cmd 的对象"能补齐能力
        （回退到基类后 CDP 链路依赖此函数）。
        """
        from selenium_bridge import _ensure_cdp_capable

        class FakeBaseDriver:
            """模拟 WebDriver 基类：有 command_executor 支持、无 execute_cdp_cmd"""
            session_id = "s-4-27"
            def __init__(self):
                self.execute = MagicMock(return_value={"value": {"ok": 1}})
                self.command_executor = MagicMock()

        driver = FakeBaseDriver()
        _ensure_cdp_capable(driver)
        assert callable(getattr(driver, "execute_cdp_cmd", None))
        assert getattr(driver.execute_cdp_cmd, "_dynamic_cdp", False) is True
        # 动态方法能真正发出 CDP 命令（走 driver.execute 兜底）
        out = driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 1920})
        assert out == {"ok": 1}
