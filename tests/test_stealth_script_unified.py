# -*- coding: utf-8 -*-
"""
统一反检测脚本 build_stealth_script 的单元 + 浏览器集成测试。

验证点：
  - 同一 seed 输出稳定、不同 seed 输出不同、seed 正确嵌入脚本
  - 脚本含 webdriver=false / getImageData length=4 / toDataURL 噪声 / 平台 GPU 池
  - GPU 渲染器与 UA 平台匹配（Windows/macOS/Linux）
  - plugins 含 name/filename/description + item/namedItem/refresh，数量 3-5
  - navigator.webdriver 返回 false（脚本层 Navigator.prototype getter）
  - localStorage 键名随 seed 变化、不含 _app_ 前缀
说明：selenium_bridge 启动时会在 navigator 实例上定义 webdriver=undefined 且不可配置，
因此环境层 navigator.webdriver 可能被覆盖为 undefined；本测试直接调用
Navigator.prototype 上的 getter 验证脚本层返回 false，并断言环境层不泄漏 true。
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import risk_check  # noqa: E402

# 浏览器启动超时（秒）：真实浏览器不可用/需联网下载驱动时，快速跳过而非挂死
_LAUNCH_TIMEOUT = 25


# ================= 纯单元（不依赖浏览器） =================

def test_same_seed_stable():
    assert risk_check.build_stealth_script(12345) == risk_check.build_stealth_script(12345)


def test_different_seed_differs():
    assert risk_check.build_stealth_script(111) != risk_check.build_stealth_script(222)


def test_script_embeds_seed():
    assert "12345" in risk_check.build_stealth_script(12345)


def test_script_contains_anti_detection_marks():
    s = risk_check.build_stealth_script(1)
    # navigator.webdriver -> false（保留 Navigator.prototype 原生伪装）
    assert "return false;" in s
    assert "Navigator.prototype, 'webdriver'" in s
    # getImageData 形参 length=4（真机）
    assert "function getImageData(sx, sy, sw, sh)" in s
    # toDataURL 噪声 hook
    assert "HTMLCanvasElement.prototype.toDataURL =" in s
    # 平台 GPU 池（Windows/macOS/Linux）
    assert "windows:" in s and "macos:" in s and "linux:" in s
    # 同时覆盖标准/unmasked WebGL 常量（防 headless SwiftShader 泄漏）
    assert "p === 7937" in s and "p === 37446" in s
    # plugins PluginArray 风格（item/namedItem/refresh）
    assert "refresh" in s and "namedItem" in s
    # localStorage 键名随机生成（_v + hex），不含固定 _app_ 前缀逻辑
    assert "_v' + _hex(6)" in s


# ================= 浏览器集成（真实 Chrome 不可用时自动跳过） =================

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>stealth test</body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def local_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/"
    server.shutdown()


def _eval(seed, ua, url):
    """启动浏览器注入 build_stealth_script(seed)，返回页面探针数据。

    带 _LAUNCH_TIMEOUT 超时保护：真实浏览器不可用/驱动需联网下载时，
    快速失败（抛异常）供上层 pytest.skip，避免测试挂死。
    """
    from selenium_bridge import sync_playwright

    def _run():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1920, "height": 1080},
                )
                context.add_init_script(risk_check.build_stealth_script(seed))
                page = context.new_page()
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                return page.evaluate("""() => {
                    const wd = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
                    const plugins = Array.from(navigator.plugins).map(pl => ({
                        name: pl.name, filename: pl.filename, description: pl.description
                    }));
                    let renderer = '', vendor = '';
                    try {
                        const gl = document.createElement('canvas').getContext('webgl') ||
                                   document.createElement('canvas').getContext('experimental-webgl');
                        if (gl) { renderer = gl.getParameter(37446) || ''; vendor = gl.getParameter(37445) || ''; }
                    } catch(e) {}
                    return {
                        webdriver_instance: navigator.webdriver,
                        webdriver_getter: wd ? wd.get() : null,
                        plugin_count: navigator.plugins.length,
                        plugins: plugins,
                        has_item: typeof navigator.plugins.item,
                        has_namedItem: typeof navigator.plugins.namedItem,
                        has_refresh: typeof navigator.plugins.refresh,
                        renderer: renderer,
                        vendor: vendor,
                        ls_keys: Object.keys(localStorage).sort(),
                        ls_has_app: Object.keys(localStorage).some(k => k.indexOf('_app_') === 0),
                    };
                }""")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    _box = {}

    def _wrap():
        try:
            _box["data"] = _run()
        except Exception as e:  # noqa: BLE001
            _box["error"] = e

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    t.join(timeout=_LAUNCH_TIMEOUT)
    if t.is_alive():
        # 浏览器启动卡死（如需联网下载驱动被网络阻断）：放弃等待，上层跳到 skip
        raise RuntimeError("浏览器启动超时，跳过集成测试")
    if "error" in _box:
        raise _box["error"]
    return _box["data"]


WINDOWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
LINUX_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def test_runtime_webdriver(local_http):
    try:
        data = _eval(123, WINDOWS_UA, local_http)
    except Exception:
        pytest.skip("浏览器不可用，跳过集成测试")
    # 脚本层 Navigator.prototype getter 必须返回 false
    assert data["webdriver_getter"] is False
    # 环境层不得泄漏 true（selenium_bridge 可能在实例上覆盖为 undefined）
    assert data["webdriver_instance"] is not True


def test_runtime_plugins(local_http):
    try:
        data = _eval(123, WINDOWS_UA, local_http)
    except Exception:
        pytest.skip("浏览器不可用，跳过集成测试")
    assert 3 <= data["plugin_count"] <= 5
    assert data["has_item"] == "function"
    assert data["has_namedItem"] == "function"
    assert data["has_refresh"] == "function"
    for pl in data["plugins"]:
        assert pl["name"] and pl["filename"]
        assert "description" in pl


def test_runtime_gpu_matches_platform(local_http):
    try:
        win = _eval(7, WINDOWS_UA, local_http)
        lin = _eval(8, LINUX_UA, local_http)
        mac = _eval(9, MAC_UA, local_http)
    except Exception:
        pytest.skip("浏览器不可用，跳过集成测试")
    assert "Direct3D11" in win["renderer"], f"Windows 应匹配 Direct3D11, got: {win['renderer']}"
    assert "ANGLE" in lin["renderer"], f"Linux 应匹配 ANGLE/Mesa, got: {lin['renderer']}"
    assert "Apple" in mac["renderer"], f"macOS 应匹配 Apple, got: {mac['renderer']}"


def test_runtime_localstorage_varies_by_seed(local_http):
    try:
        a = _eval(101, WINDOWS_UA, local_http)
        b = _eval(202, WINDOWS_UA, local_http)
    except Exception:
        pytest.skip("浏览器不可用，跳过集成测试")
    assert not a["ls_has_app"] and not b["ls_has_app"], "localStorage 不应含 _app_ 前缀"
    # 两个 seed 下的 localStorage 键集合不同（随机键名生效）
    assert a["ls_keys"] != b["ls_keys"]