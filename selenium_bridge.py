"""
Selenium Bridge - 提供与 Playwright sync API 兼容的接口层。
使得原 Playwright 代码可以最小改动地迁移到 Selenium。
"""

import time
import json
import os
import re
import random
import threading
import logging
import urllib.parse
from typing import Optional, Any, Callable, List, Dict, Union

logger = logging.getLogger("selenium_bridge")

from selenium import webdriver
from selenium.webdriver import ChromeOptions, ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    InvalidSelectorException,
)

# ========== 导出兼容的异常类 ==========
class PlaywrightTimeoutError(TimeoutException):
    """兼容 Playwright 的 TimeoutError"""
    pass

TimeoutError = PlaywrightTimeoutError  # 别名


# ========== 全局 driver 注册表 + 停止支持 ==========
# 解决"点击停止后浏览器仍然打开"的问题：
# stop_task 路由只设标志位、不持有 driver 句柄，无法主动关闭浏览器；
# 这里维护一个全局活跃 driver 列表，并提供 force_quit_all() 供外部停止时调用，
# 同时在关闭时强杀残留的 chrome/chromedriver 进程作为兜底。
import subprocess as _subprocess
import signal as _signal

_active_drivers = []
_active_drivers_lock = threading.Lock()

# ★ 窗口切换/句柄枚举互斥锁：多个守护线程（popunder 触发/心跳采集）与主线程
#   并发调用 switch_to.window / window_handles 存在竞态（窗口焦点被互相篡改、
#   句柄枚举期间切换导致 IndexError），统一用该锁串行化窗口级操作。
_window_focus_lock = threading.Lock()

# 停止检查回调：由 app.py 注入，返回 True 表示任务应当停止。
# 用于让 goto/wait 等阻塞循环及时中断，避免"点停止后仍卡在加载等待里"。
_stop_check_callback = None


def set_stop_check(callback):
    """注册停止检查回调（app.py 调用，传入返回 bool 的函数）。"""
    global _stop_check_callback
    _stop_check_callback = callback


def _should_stop() -> bool:
    """查询是否应当停止当前操作。"""
    cb = _stop_check_callback
    if cb is None:
        return False
    try:
        return bool(cb())
    except Exception:
        return False


def _register_driver(driver):
    with _active_drivers_lock:
        _active_drivers.append(driver)


def _unregister_driver(driver):
    with _active_drivers_lock:
        try:
            _active_drivers.remove(driver)
        except ValueError:
            pass


# ★ 26.8.9.6：chromedriver HTTP 通道单条命令读超时（秒）。
# 超时后抛异常由上层 try/except 捕获，保险绳检查点得以恢复控制。
_CHROMEDRIVER_HTTP_TIMEOUT_S = 60.0


def _launch_chrome_with_timeout(chrome_options):
    """★ 26.8.9.6 卡死根治：启动 Chrome 并给 chromedriver HTTP 通道注入读超时。

    Selenium 4.46 的 webdriver.Chrome() 默认构造的 RemoteConnection
    timeout=None，当 chrome/chromedriver 在某条命令上挂死（窗口切换、
    current_url 读取、CDP 调用等）时，urllib3 会无限阻塞在 socket 读上
    → 主线程冻结（实测冻结 14 分钟+，单任务跑 1085s），看门狗即使强杀
    进程也救不了已卡在系统调用里的主线程。
    此处手动构造 Service + ChromiumRemoteConnection（带 ClientConfig.timeout），
    把单条命令限制在 _CHROMEDRIVER_HTTP_TIMEOUT_S 内，超时抛异常后
    上层检查点/_check_rope 即可正常截断任务。
    """
    from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
    from selenium.webdriver.remote.client_config import ClientConfig
    # ★ 26.8.13.3 根因修复：必须用 ChromiumDriver 构造 driver！
    #   execute_cdp_cmd 是 ChromiumDriver/Chrome 子类专属方法，WebDriver 基类没有。
    #   之前用基类导致 _apply_context_config 中 UA/时区/屏幕/请求头/cdc 清理
    #   等全部 CDP 调用抛 AttributeError 被 logger.debug 静默吞掉，
    #   → 演练 7 项风控特征全部暴露（Headless UA/伦敦时区/800x600 屏幕/空 Referer/cdc 残留）。
    try:
        from selenium.webdriver.chromium.webdriver import ChromiumDriver as _RemoteWebDriver
    except ImportError:
        # 极老环境无 chromium.webdriver，退回基类（下方 _ensure_cdp_capable 会动态补齐）
        from selenium.webdriver.remote.webdriver import WebDriver as _RemoteWebDriver
    from selenium.webdriver.common.driver_finder import DriverFinder

    service = Service()
    finder = DriverFinder(service, chrome_options)
    if finder.get_browser_path():
        chrome_options.binary_location = finder.get_browser_path()
        chrome_options.browser_version = None
    service.path = service.env_path() or finder.get_driver_path()
    service.start()

    client_config = ClientConfig(
        remote_server_addr=service.service_url,
        keep_alive=True,
        timeout=_CHROMEDRIVER_HTTP_TIMEOUT_S,
    )
    executor = ChromiumRemoteConnection(
        remote_server_addr=service.service_url,
        browser_name="chrome",
        vendor_prefix="goog",
        keep_alive=True,
        ignore_proxy=chrome_options._ignore_local_proxy,
        client_config=client_config,
    )
    # ★ 26.8.13.5 兼容 Selenium 4.27（VPS 4.27.1）：ChromiumDriver.__init__ 无 command_executor
    #   参数（4.27 签名: browser_name/vendor_prefix/options/service/keep_alive），
    #   本地 Selenium 4.46 的 ChromiumDriver 则支持 command_executor。
    #   先按 4.46 签名构造；TypeError 时回退 WebDriver 基类（基类始终支持 command_executor），
    #   CDP 能力由下方 _ensure_cdp_capable 动态补齐，两条路径 CDP 链路一致。
    try:
        driver = _RemoteWebDriver(command_executor=executor, options=chrome_options)
    except TypeError:
        from selenium.webdriver.remote.webdriver import WebDriver as _BaseWebDriver
        driver = _BaseWebDriver(command_executor=executor, options=chrome_options)
    driver.service = service  # _kill_driver_processes / force_quit_all 依赖此属性
    # ★ 26.8.13.3 兜底：若最终拿到的 driver 没有 execute_cdp_cmd（基类/特殊环境），
    #   动态绑定基于 _CDPSession.send 三级兼容实现的同名方法，确保 CDP 链路永不缺席
    _ensure_cdp_capable(driver)
    return driver


def _ensure_cdp_capable(driver):
    """★ 26.8.13.3 根因修复：确保 driver 具备 execute_cdp_cmd 能力。

    背景：_launch_chrome_with_timeout 之前用 WebDriver 基类构造 driver，
    基类没有 execute_cdp_cmd 方法 → 所有 CDP 调用（UA/时区/屏幕/请求头/
    cdc 清理）抛 AttributeError 被 logger.debug 静默吞掉 → 演练 7 项风控
    特征全部暴露。此处若 driver 原生缺 execute_cdp_cmd，动态绑定一个基于
    _CDPSession.send 三级兼容实现（官方 API → driver.execute → 手工注入
    sessionId）的同名方法。
    """
    _native = getattr(driver, "execute_cdp_cmd", None)
    if _native is not None and callable(_native) and not getattr(_native, "_dynamic_cdp", False):
        return driver  # 原生能力就绪

    def _execute_cdp_cmd(cmd, cmd_args=None):
        sess = _CDPSession(driver)
        return sess.send(cmd, cmd_args or {})

    _execute_cdp_cmd._dynamic_cdp = True  # 标记动态补齐，防 _CDPSession.send 方法1 递归
    driver.execute_cdp_cmd = _execute_cdp_cmd
    logger.debug("已为 driver(%s) 动态补齐 execute_cdp_cmd", type(driver).__name__)
    return driver


def _kill_driver_processes(driver):
    """强杀某个 driver 关联的 chrome 与 chromedriver 进程（兜底）。

    ★ 26.8.9.6 增强：service.process 引用失效时，按 --port 扫描 /proc
    命令行兜底找出 chromedriver，确保看门狗强杀真实生效。
    """
    pids = []
    port = None
    try:
        # chromedriver 进程pid
        svc = getattr(driver, "service", None)
        if svc is not None:
            proc = getattr(svc, "process", None)
            if proc is not None and proc.pid:
                pids.append(proc.pid)
            try:
                port = urllib.parse.urlparse(getattr(svc, "service_url", "") or "").port
            except Exception:
                port = None
    except Exception:
        pass
    try:
        # chrome 浏览器主进程pid（Selenium 4 在 caps 中暴露）
        caps = getattr(driver, "caps", {}) or {}
        chrome_info = caps.get("goog:chromeOptions", {}) or {}
        browser_pid = chrome_info.get("browserPid")
        if browser_pid:
            pids.append(int(browser_pid))
    except Exception:
        pass
    # ★ 兜底：按端口扫 /proc 命令行，找出引用丢失的 chromedriver
    if port:
        try:
            needle = f"--port={port}".encode()
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                        cmd = f.read()
                    if b"chromedriver" in cmd and needle in cmd:
                        pids.append(int(pid_dir))
                except Exception:
                    continue
        except Exception:
            pass
    killed = set()
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGKILL)
            killed.add(pid)
        except Exception:
            pass
    return killed


def force_quit_all():
    """强制关闭所有活跃浏览器（供 app.py 的停止任务调用）。

    先尝试优雅 quit（带超时），无论成功与否都强杀残留进程，
    确保"点击停止后浏览器一定关闭"。
    """
    with _active_drivers_lock:
        drivers = list(_active_drivers)
        _active_drivers.clear()

    for driver in drivers:
        # 1) 在子线程里优雅 quit，避免 chromedriver 命令串行队列把主线程卡死
        def _graceful(d=driver):
            try:
                d.quit()
            except Exception:
                pass
        t = threading.Thread(target=_graceful, daemon=True)
        t.start()
        t.join(timeout=5)
        # 2) 无论 quit 是否完成，强杀进程兜底
        _kill_driver_processes(driver)

    return len(drivers)


# ========== 工具函数 ==========
def _js_quote(s: str) -> str:
    """将字符串安全嵌入JS代码"""
    return json.dumps(s)


def _find_chromedriver() -> str:
    """自动查找ChromeDriver路径
    
    重要：强制使用 Selenium Manager 自动管理版本
    系统中的 chromedriver (v108) 与 Chrome (v149) 版本不兼容
    返回 None 让 Selenium 自动下载匹配版本的 chromedriver
    """
    # 清除旧的环境变量，确保使用Selenium Manager
    import os
    if 'CHROMEDRIVER_PATH' in os.environ:
        del os.environ['CHROMEDRIVER_PATH']
    return None


def _find_chrome_binary() -> Optional[str]:
    """查找 Chrome 浏览器路径"""
    import platform
    system = platform.system()

    candidates = []
    if system == "Darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Windows":
        candidates = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


# ========== Response 对象 ==========
class Response:
    """模拟 Playwright 的 Response 对象"""
    def __init__(self, status: int = 200, url: str = "", ok: bool = True):
        self.status = status
        self.url = url
        self.ok = ok

    def __bool__(self):
        return self.ok


# ========== ElementHandle 兼容类 ==========
class ElementHandle:
    """包装 Selenium WebElement，提供 Playwright 兼容的API"""

    def __init__(self, element, page):
        self._element = element
        self._page = page

    def _safe_call(self, method_name, *args, **kwargs):
        """安全调用元素方法，处理StaleElement异常"""
        max_retries = 3
        for i in range(max_retries):
            try:
                method = getattr(self._element, method_name)
                return method(*args, **kwargs)
            except StaleElementReferenceException:
                if i < max_retries - 1:
                    time.sleep(0.2)
                else:
                    raise
            except Exception:
                raise

    def click(self, force=False, timeout=30000, **kwargs):
        """点击元素"""
        try:
            self._element.click()
        except Exception:
            # JS点击兜底
            try:
                self._page.driver.execute_script("arguments[0].click();", self._element)
            except Exception:
                raise

    def text_content(self) -> str:
        """获取元素textContent"""
        try:
            return self._safe_call("text") or ""
        except Exception:
            return ""

    def inner_text(self) -> str:
        """获取元素innerText"""
        try:
            return self._page.driver.execute_script(
                "return arguments[0].innerText || '';", self._element
            ) or ""
        except Exception:
            return self.text_content()

    def get_attribute(self, name: str) -> Optional[str]:
        """获取元素属性"""
        try:
            return self._safe_call("get_attribute", name)
        except Exception:
            return None

    def bounding_box(self) -> Optional[Dict]:
        """获取元素边界框 {x, y, width, height}"""
        try:
            rect = self._page.driver.execute_script("""
                var r = arguments[0].getBoundingClientRect();
                return {x: r.x, y: r.y, width: r.width, height: r.height};
            """, self._element)
            return rect
        except Exception:
            return None

    def scroll_into_view_if_needed(self, timeout=10000, **kwargs):
        """滚动元素到视图中"""
        try:
            self._page.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                self._element
            )
            time.sleep(0.3)
        except Exception:
            try:
                self._element.location_once_scrolled_into_view
            except Exception:
                pass

    def is_visible(self) -> bool:
        """检查元素是否可见"""
        try:
            return self._element.is_displayed()
        except Exception:
            return False

    def hover(self):
        """鼠标悬停"""
        try:
            ActionChains(self._page.driver).move_to_element(self._element).perform()
        except Exception:
            pass

    def fill(self, value: str):
        """填充输入框"""
        try:
            self._element.clear()
            self._element.send_keys(value)
        except Exception:
            self._page.driver.execute_script(
                "arguments[0].value = arguments[1];", self._element, value
            )

    @property
    def tag_name(self):
        try:
            return self._safe_call("tag_name")
        except Exception:
            return ""


# ========== Frame 兼容类 ==========
class Frame:
    """模拟 Playwright 的 Frame 对象"""

    def __init__(self, driver, frame_reference=None):
        self.driver = driver
        self._frame_ref = frame_reference

    def _switch_to(self):
        if self._frame_ref is not None:
            try:
                if isinstance(self._frame_ref, int):
                    self.driver.switch_to.frame(self._frame_ref)
                elif isinstance(self._frame_ref, str):
                    self.driver.switch_to.frame(self._frame_ref)
                else:
                    self.driver.switch_to.frame(self._frame_ref)
            except Exception:
                self.driver.switch_to.default_content()
                try:
                    iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                    if isinstance(self._frame_ref, int) and self._frame_ref < len(iframes):
                        self.driver.switch_to.frame(iframes[self._frame_ref])
                except Exception:
                    pass
        else:
            self.driver.switch_to.default_content()

    def _switch_back(self):
        self.driver.switch_to.default_content()

    def evaluate(self, script: str, arg=None) -> Any:
        """在frame中执行JS"""
        prev_frame = self.driver.execute_script("return window.frameElement;")
        try:
            self._switch_to()
            result = self.driver.execute_script(self._wrap_script(script, arg))
            return result
        finally:
            self._switch_back()

    def _wrap_script(self, script: str, arg=None) -> str:
        import re as _re_ws
        s = script.strip()
        # ★ 带参箭头函数：(keywords) => {...} → 用 arguments[0] 传参调用
        if _re_ws.match(r"^\(([^()]*)\)\s*=>", s) or _re_ws.match(r"^[A-Za-z_$][\w$]*\s*=>", s):
            if arg is not None:
                return f"return ({s})(arguments[0]);"
            return f"return ({s})();"
        if s.startswith("() =>") or s.startswith("function"):
            if arg is not None:
                return f"return ({script})(arguments[0]);"
            return f"return ({script})();"
        if arg is not None:
            return f"return (function() {{ {script} }})(arguments[0]);"
        return f"return (function() {{ {script} }})();" if not s.startswith("return") else script

    def wait_for_load_state(self, state: str = "load", timeout: int = 30000):
        """等待加载状态"""
        try:
            self._switch_to()
            if state == "domcontentloaded":
                WebDriverWait(self.driver, timeout / 1000).until(
                    lambda d: d.execute_script("return document.readyState !== 'loading';")
                )
            elif state == "load":
                WebDriverWait(self.driver, timeout / 1000).until(
                    lambda d: d.execute_script("return document.readyState === 'complete';")
                )
            elif state == "networkidle":
                end_time = time.time() + timeout / 1000
                while time.time() < end_time:
                    time.sleep(0.5)
        finally:
            self._switch_back()

    def query_selector(self, selector: str) -> Optional[ElementHandle]:
        """查询单个元素"""
        try:
            self._switch_to()
            el = self.driver.find_element(By.CSS_SELECTOR, selector)
            return ElementHandle(el, self._get_page())
        except NoSuchElementException:
            return None
        finally:
            self._switch_back()

    def query_selector_all(self, selector: str) -> List[ElementHandle]:
        """查询多个元素"""
        try:
            self._switch_to()
            els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            return [ElementHandle(el, self._get_page()) for el in els]
        finally:
            self._switch_back()

    def _get_page(self):
        """获取关联的Page对象"""
        return Page._get_current_page() or Page(self.driver)

    @property
    def url(self) -> str:
        try:
            self._switch_to()
            return self.driver.current_url
        finally:
            self._switch_back()


# ========== Mouse 兼容类 ==========
class Mouse:
    """模拟 Playwright page.mouse（通过 CDP Input.dispatchMouseEvent 派发真实鼠标事件）。

    CDP 方式相比 Selenium ActionChains 的优势：
    1. 坐标基于视口左上角，不受 body 元素边界限制，不会抛 MoveTargetOutOfBounds；
    2. 派发的是浏览器内核级真实事件（含 mousemove 轨迹），反检测/真人行为生效；
    3. 不依赖元素定位，性能更好。
    """

    def __init__(self, driver):
        self.driver = driver
        self._x = 0
        self._y = 0
        self._vw = None
        self._vh = None

    def _viewport(self):
        """获取视口尺寸（带缓存），用于坐标clamp。"""
        try:
            size = self.driver.execute_script(
                "return [window.innerWidth||1920, window.innerHeight||1080];"
            )
            if size and len(size) == 2:
                self._vw, self._vh = int(size[0]), int(size[1])
        except Exception:
            pass
        return (self._vw or 1920), (self._vh or 1080)

    def _clamp(self, x, y):
        """将坐标限制在视口可见范围内，避免CDP派发到不可见区域。"""
        vw, vh = self._viewport()
        x = max(1, min(int(x), vw - 2))
        y = max(1, min(int(y), vh - 2))
        return x, y

    def _dispatch(self, event_type, x, y, button="none", clicks=0):
        """通过CDP派发鼠标事件。"""
        params = {
            "type": event_type,
            "x": x,
            "y": y,
            "button": button,
            "buttons": 1 if button == "left" else 0,
        }
        if clicks:
            params["clickCount"] = clicks
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", params)

    def move(self, x: float, y: float, steps: int = 1, **kwargs):
        """移动鼠标到指定坐标（CDP真实mousemove，支持分步轨迹）。"""
        tx, ty = self._clamp(x, y)
        try:
            steps = max(1, int(steps))
            start_x, start_y = self._x, self._y
            for i in range(1, steps + 1):
                t = i / steps
                cx = int(start_x + (tx - start_x) * t)
                cy = int(start_y + (ty - start_y) * t)
                self._dispatch("mouseMoved", cx, cy)
                self._x, self._y = cx, cy
                if steps > 1:
                    time.sleep(0.008)
        except Exception:
            # CDP失败时降级到ActionChains（不静默丢弃，至少尝试真实移动）
            try:
                body = self.driver.find_element(By.TAG_NAME, "body")
                ActionChains(self.driver).move_to_element_with_offset(body, 1, 1).move_by_offset(tx - 1, ty - 1).perform()
                self._x, self._y = tx, ty
            except Exception:
                pass

    def click(self, x: float, y: float, button: str = "left", click_count: int = 1, **kwargs):
        """在指定坐标点击（CDP真实press+release）。"""
        tx, ty = self._clamp(x, y)
        try:
            self.move(tx, ty)
            self._dispatch("mousePressed", tx, ty, button=button, clicks=click_count)
            time.sleep(0.03)
            self._dispatch("mouseReleased", tx, ty, button=button, clicks=click_count)
            self._x, self._y = tx, ty
        except Exception:
            # 降级：JS点击命中坐标处元素
            try:
                self.driver.execute_script(
                    "var el=document.elementFromPoint(arguments[0], arguments[1]); if(el){el.click();}",
                    tx, ty
                )
            except Exception:
                pass

    def wheel(self, delta_x: float, delta_y: float, **kwargs):
        """滚轮事件（CDP真实wheel）。"""
        try:
            self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": self._x or 100,
                "y": self._y or 100,
                "deltaX": int(delta_x),
                "deltaY": int(delta_y),
            })
        except Exception:
            try:
                self.driver.execute_script(
                    "window.scrollBy(arguments[0], arguments[1]);", int(delta_x), int(delta_y)
                )
            except Exception:
                pass


# ========== Keyboard 兼容类 ==========
class Keyboard:
    """模拟 Playwright page.keyboard（CDP Input.dispatchKeyEvent 派发真实按键）。"""

    def __init__(self, driver):
        self.driver = driver
        # ★ 修复: Ctrl+A/Meta+A 全选支持 —— 修饰键按下状态位（CDP modifiers）
        self._modifiers = 0

    # Playwright键名 -> (windowsVirtualKeyCode, key, code)
    _CDP_KEY_MAP = {
        "PageDown": (34, "PageDown", "PageDown"),
        "PageUp": (33, "PageUp", "PageUp"),
        "ArrowDown": (40, "ArrowDown", "ArrowDown"),
        "ArrowUp": (38, "ArrowUp", "ArrowUp"),
        "ArrowLeft": (37, "ArrowLeft", "ArrowLeft"),
        "ArrowRight": (39, "ArrowRight", "ArrowRight"),
        "End": (35, "End", "End"),
        "Home": (36, "Home", "Home"),
        "Space": (32, " ", "Space"),
        "Enter": (13, "Enter", "Enter"),
        "Tab": (9, "Tab", "Tab"),
        "Escape": (27, "Escape", "Escape"),
        "Backspace": (8, "Backspace", "Backspace"),
    }

    # 修饰键: vk, key, code, CDP modifiers位
    _MOD_KEY_MAP = {
        "Control": (17, "Control", "ControlLeft", 2),
        "Meta": (91, "Meta", "MetaLeft", 4),
        "Shift": (16, "Shift", "ShiftLeft", 8),
        "Alt": (18, "Alt", "AltLeft", 1),
    }

    _SELENIUM_KEY_MAP = {
        "PageDown": Keys.PAGE_DOWN, "PageUp": Keys.PAGE_UP,
        "ArrowDown": Keys.ARROW_DOWN, "ArrowUp": Keys.ARROW_UP,
        "ArrowLeft": Keys.ARROW_LEFT, "ArrowRight": Keys.ARROW_RIGHT,
        "End": Keys.END, "Home": Keys.HOME, "Space": Keys.SPACE,
        "Enter": Keys.ENTER, "Tab": Keys.TAB, "Escape": Keys.ESCAPE,
        "Backspace": Keys.BACKSPACE,
    }

    def down(self, key: str, **kwargs):
        """按住键（不释放）—— Playwright Keyboard.down 兼容，支持修饰键组合 Ctrl+A。"""
        _mod = self._MOD_KEY_MAP.get(key)
        if _mod:
            vk, key_name, code, mod_bit = _mod
            self._modifiers |= mod_bit
            try:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "windowsVirtualKeyCode": vk,
                    "key": key_name, "code": code,
                    "modifiers": self._modifiers,
                })
                return
            except Exception:
                pass  # 降级到 ActionChains
        try:
            mapped = self._SELENIUM_KEY_MAP.get(key, key)
            ActionChains(self.driver).key_down(mapped).perform()
        except Exception:
            pass

    def up(self, key: str, **kwargs):
        """释放键 —— Playwright Keyboard.up 兼容。"""
        _mod = self._MOD_KEY_MAP.get(key)
        if _mod:
            vk, key_name, code, mod_bit = _mod
            self._modifiers &= ~mod_bit
            try:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "windowsVirtualKeyCode": vk,
                    "key": key_name, "code": code,
                    "modifiers": self._modifiers,
                })
                return
            except Exception:
                pass
        try:
            mapped = self._SELENIUM_KEY_MAP.get(key, key)
            ActionChains(self.driver).key_up(mapped).perform()
        except Exception:
            pass

    def press(self, key: str, **kwargs):
        """按键（CDP keyDown+keyUp，携带当前修饰键状态；失败降级到ActionChains）。"""
        cdp_key = self._CDP_KEY_MAP.get(key)
        if cdp_key:
            vk, key_name, code = cdp_key
            try:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "windowsVirtualKeyCode": vk,
                    "key": key_name, "code": code,
                    "modifiers": self._modifiers,
                })
                time.sleep(0.02)
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "windowsVirtualKeyCode": vk,
                    "key": key_name, "code": code,
                    "modifiers": self._modifiers,
                })
                return
            except Exception:
                pass
        # 单字符键：CDP 派发（携带修饰键，保证 Ctrl+A 语义）
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            try:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "text": key, "key": key,
                    "code": f"Key{key.upper()}", "modifiers": self._modifiers,
                })
                time.sleep(0.02)
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": key,
                    "code": f"Key{key.upper()}", "modifiers": self._modifiers,
                })
                return
            except Exception:
                pass
        # 降级到ActionChains
        try:
            mapped_key = self._SELENIUM_KEY_MAP.get(key, key)
            ActionChains(self.driver).send_keys(mapped_key).perform()
        except Exception:
            pass

    def _cdp_char(self, ch: str):
        """通过 CDP Input.insertText 输入单个字符（真实输入事件）。"""
        try:
            self.driver.execute_cdp_cmd("Input.insertText", {"text": ch})
            return True
        except Exception:
            return False

    def _cdp_backspace(self):
        """派发一次退格键（用于错字修正）。"""
        try:
            self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace",
            })
            time.sleep(random.uniform(0.02, 0.05))
            self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace",
            })
            return True
        except Exception:
            return False

    def type(self, text: str, delay: float = None, input_type: str = "text", **kwargs):
        """真人逐字符输入：随机间隔、错字修正、分段停顿；间隔随输入内容类型自适应。

        delay 若指定（毫秒），作为基础间隔；否则按 input_type 选用不同节奏：
          - password: 输入更慢更谨慎（120-360ms），几乎不连打，错字率更低
          - search/text: 普通文本（80-280ms）
          - numeric: 数字串（100-260ms）
        """
        if text is None:
            return
        text = str(text)
        _typo_chars = "abcdefghijklmnopqrstuvwxyz"
        # 按内容类型确定间隔区间与错字概率
        _it = (input_type or "text").lower()
        if _it == "password":
            lo, hi, typo_p, seg_p = 0.12, 0.36, 0.04, 0.25
        elif _it == "numeric":
            lo, hi, typo_p, seg_p = 0.10, 0.26, 0.05, 0.10
        else:
            lo, hi, typo_p, seg_p = 0.08, 0.28, 0.10, 0.15
        try:
            for idx, ch in enumerate(text):
                # 概率性先打一个错字再退格修正（密码框错字率低）
                if ch.strip() and random.random() < typo_p:
                    wrong = random.choice(_typo_chars)
                    if self._cdp_char(wrong):
                        time.sleep(random.uniform(0.08, 0.20))
                        self._cdp_backspace()
                        time.sleep(random.uniform(0.05, 0.15))
                # 输入真实字符（CDP 失败降级 send_keys）
                if not self._cdp_char(ch):
                    try:
                        ActionChains(self.driver).send_keys(ch).perform()
                    except Exception:
                        pass
                # 字符间随机间隔
                if delay is not None:
                    base = float(delay) / 1000.0
                    time.sleep(max(0.0, random.uniform(base * 0.6, base * 1.4)))
                else:
                    time.sleep(random.uniform(lo, hi))
                # 长文本分段停顿：遇到空格/标点后偶尔短暂停顿思考
                if ch in " ,.，。、" and random.random() < seg_p:
                    time.sleep(random.uniform(0.3, 0.9))
        except Exception:
            pass

    # Playwright 别名
    def insert_text(self, text: str, **kwargs):
        self.type(text, **kwargs)


# ========== Locator 兼容类 ==========
class Locator:
    """模拟 Playwright Locator"""

    def __init__(self, page, selector: str):
        self._page = page
        self._selector = selector

    def is_visible(self, timeout: int = 5000) -> bool:
        try:
            el = self._page.driver.find_element(By.CSS_SELECTOR, self._selector)
            return el.is_displayed()
        except Exception:
            return False

    def click(self, timeout: int = 30000, **kwargs):
        try:
            el = WebDriverWait(self._page.driver, timeout / 1000).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, self._selector))
            )
            el.click()
        except Exception:
            try:
                el = self._page.driver.find_element(By.CSS_SELECTOR, self._selector)
                self._page.driver.execute_script("arguments[0].click();", el)
            except Exception:
                pass

    def hover(self, timeout: int = 5000):
        try:
            el = WebDriverWait(self._page.driver, timeout / 1000).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self._selector))
            )
            ActionChains(self._page.driver).move_to_element(el).perform()
        except Exception:
            pass


# ========== Request/Route 兼容类 ==========
class Request:
    """模拟 Playwright Request"""
    def __init__(self, url: str = "", headers: dict = None):
        self.url = url
        self.headers = headers or {}
        self.method = "GET"
        self.resource_type = "document"


class Route:
    """模拟 Playwright Route - Selenium下通过CDP/js预处理，此为兼容层。

    ★ 26.8.13.3 修复：abort/continue_/fulfill 从空操作改为记录路由决策，
    由 Page 的请求采集管道读取决策并汇总日志（物理拦截能力受 Selenium
    限制为近似实现：fetch/XHR 级阻断由 JS hook 负责，CDP 无法改写已发出请求）。
    """

    def __init__(self, request: Request = None):
        self.request = request or Request()
        # ★ 路由决策状态：abort / fulfill / continue 三选一，供采集管道读取
        self._aborted = False
        self._fulfilled = None  # (status, body, content_type)
        self._continued = False

    def continue_(self, headers: dict = None, **kwargs):
        """继续请求（放行）。Selenium 无法改写已发出的请求头，headers 仅记录。"""
        self._continued = True
        if headers:
            try:
                self.request.headers.update(headers)
            except Exception:
                pass

    def abort(self, error_code: str = "failed", **kwargs):
        """中止请求（记录拦截决策并记日志，物理拦截由 JS hook 阻断表兜底）"""
        self._aborted = True
        logger.info(f"Route.abort 拦截请求: {self.request.url} (error_code={error_code})")

    def fulfill(self, status: int = 200, body: str = "", **kwargs):
        """用本地响应替代请求（记录决策；物理替代受 Selenium 能力限制为近似实现）"""
        content_type = kwargs.get("content_type") or kwargs.get("contentType") or "text/html"
        self._fulfilled = (int(status), body, content_type)
        logger.debug(f"Route.fulfill 本地响应替代: {self.request.url} status={status}")


# ========== Page 兼容类 ==========
import threading as _threading

class Page:
    """模拟 Playwright Page 对象，核心适配层"""

    _thread_local = _threading.local()  # 线程局部变量，避免多页面并发时互相覆盖

    @classmethod
    def _get_current_page(cls):
        return getattr(cls._thread_local, 'page', None)

    @classmethod
    def _set_current_page(cls, page):
        cls._thread_local.page = page

    # 保持旧接口兼容
    @property
    def _current_page(self):
        return self._get_current_page()

    @_current_page.setter
    def _current_page(self, value):
        self._set_current_page(value)

    def __init__(self, driver, context=None):
        self.driver = driver
        self._context = context
        self._default_nav_timeout = 120000
        self._default_timeout = 60000
        self._init_scripts = []
        self._request_handlers = []
        self._network_interception_enabled = False
        self._cdp_listener = None
        self._request_id_map = {}
        # ★ 事件系统（Playwright 兼容）：on/once/off/emit
        self._event_handlers: Dict[str, List] = {}
        self._event_lock = threading.Lock()
        # ★ 网络请求采集管道：JS hook 写入 window.__sb_requests，
        #   轮询线程 drain 后存入 _request_records，并派发 request 事件 / route handlers
        self._request_records: List[Dict] = []
        self._request_records_lock = threading.Lock()
        self._collecting = False
        self._collect_stop = threading.Event()
        self._collect_thread = None
        # ★ 窗口句柄绑定：None=主页面（沿用焦点语义）；非空=弹窗等副标签，
        #   操作前自动切到绑定窗口，避免共享driver焦点被其它标签篡改
        self._window_handle = None
        self.mouse = Mouse(driver)
        self.keyboard = Keyboard(driver)
        Page._set_current_page(self)

    def _focus_window(self):
        """★ 若绑定了窗口句柄且当前焦点不在其上，先切换焦点（失败不阻断）。
        用模块级 _window_focus_lock 串行化窗口切换，避免多个守护线程并发
        switch_to.window 互相篡改焦点（弹窗触发线程/心跳采集线程/主线程三方竞态）。
        """
        if not self._window_handle:
            return
        try:
            with _window_focus_lock:
                if self.driver.current_window_handle != self._window_handle:
                    self.driver.switch_to.window(self._window_handle)
        except Exception:
            pass

    # ================= 事件系统（Playwright 兼容） =================
    # ★ 26.8.13.3 新增：on/once/off/emit。重点支持 "request" 事件——
    #   上层 popunder_trigger 的 popunder_page.on("request", _on_pop_request)
    #   依赖它采集弹窗页网络请求（url / method / resource_type）。
    def on(self, event: str, handler: Callable):
        """注册事件监听器。event=="request" 时自动启动网络请求采集管道；
        CDP 采集不可用时仅记 warning 不抛异常（保证上层注册不被异常打断）。
        """
        if event == "request":
            self._start_request_collection()
        with self._event_lock:
            self._event_handlers.setdefault(event, []).append(handler)
        return None

    def once(self, event: str, handler: Callable):
        """注册一次性监听器（触发一次后自动移除）"""
        def _wrapper(*args, **kwargs):
            try:
                handler(*args, **kwargs)
            finally:
                self.off(event, _wrapper)
        if event == "request":
            self._start_request_collection()
        with self._event_lock:
            self._event_handlers.setdefault(event, []).append(_wrapper)
        return None

    def off(self, event: str, handler: Callable = None):
        """移除监听器；handler 为 None 时移除该事件全部监听器"""
        with self._event_lock:
            hs = self._event_handlers.get(event)
            if not hs:
                return
            if handler is None:
                self._event_handlers[event] = []
            else:
                self._event_handlers[event] = [h for h in hs if h is not handler and h != handler]

    def emit(self, event: str, *args, **kwargs):
        """触发事件：快照 handler 列表后在锁外调用，避免回调内再注册/注销导致死锁"""
        with self._event_lock:
            hs = list(self._event_handlers.get(event, []))
        for h in hs:
            try:
                h(*args, **kwargs)
            except Exception as e:
                logger.debug(f"Page.emit({event}) 回调异常(忽略): {e}")

    def drain_request_records(self) -> List[Dict]:
        """取走并清空已采集的网络请求记录（供上层做心跳/流量分析）。
        每条记录含 url / method / resource_type / timestamp 字段。
        """
        with self._request_records_lock:
            records = self._request_records
            self._request_records = []
        return records

    @property
    def context(self):
        """公开context属性，兼容Playwright API（page.context.options）"""
        return self._context

    @property
    def url(self) -> str:
        try:
            self._focus_window()
            return self.driver.current_url or ""
        except Exception:
            return ""

    @property
    def request(self) -> Request:
        """返回当前页面的请求信息（从浏览器上下文动态读取实际headers）"""
        headers = {}
        if self._context and hasattr(self._context, '_options'):
            headers = dict(self._context._options.get("extra_http_headers") or {})
        # 补全 User-Agent
        if "User-Agent" not in headers and "user-agent" not in headers:
            try:
                ua = self.driver.execute_script("return navigator.userAgent")
                if ua:
                    headers["User-Agent"] = ua
            except Exception:
                pass
        return Request(
            url=self.url,
            headers=headers
        )

    @property
    def viewport_size(self) -> Dict:
        # ★ 修复：窗口外框（get_window_size）≠ 视口，改用 JS 读取真实视口，
        #   JS 失败（about:blank/布局未完成/驱动异常）时回退原逻辑。
        try:
            self._focus_window()
            vp = self.driver.execute_script(
                "var de = document.documentElement;"
                "return {w: de.clientWidth || window.innerWidth || 0,"
                "        h: de.clientHeight || window.innerHeight || 0};"
            )
            if isinstance(vp, dict) and vp.get("w") and vp.get("h"):
                return {"width": int(vp["w"]), "height": int(vp["h"])}
        except Exception:
            pass
        try:
            size = self.driver.get_window_size()
            return {"width": size.get("width", 1920), "height": size.get("height", 1080)}
        except Exception:
            return {"width": 1920, "height": 1080}

    def title(self) -> str:
        try:
            return self.driver.title or ""
        except Exception:
            return ""

    def content(self) -> str:
        try:
            return self.driver.page_source or ""
        except Exception:
            return ""

    def close(self, timeout: int = 10000, **kwargs):
        """关闭页面标签"""
        # 审计修复(E2)：关闭页面前先停止网络请求采集轮询线程，避免线程泄漏/空转
        try:
            self._stop_request_collection()
        except Exception:
            pass
        try:
            if self._window_handle:
                # ★ 绑定句柄的弹窗页：先切到自身再关闭，防止误关主窗口
                _switched = False
                try:
                    self.driver.switch_to.window(self._window_handle)
                    _switched = True
                except Exception:
                    pass  # 句柄已失效（窗口已自然死亡），跳过关闭
                if not _switched:
                    return
                try:
                    self.driver.close()
                except Exception:
                    return
                # 焦点归还主站（或第一个剩余窗口）
                try:
                    _main_h = getattr(self._context, "_main_window_handle", None)
                    _hs = self.driver.window_handles
                    if _main_h and _main_h in _hs:
                        self.driver.switch_to.window(_main_h)
                    elif _hs:
                        self.driver.switch_to.window(_hs[0])
                except Exception:
                    pass
                return
            # 主页面沿用旧逻辑：关闭当前窗口
            if len(self.driver.window_handles) > 1:
                self.driver.close()
                # 切换回最后一个窗口
                self.driver.switch_to.window(self.driver.window_handles[-1])
        except Exception:
            pass

    def set_default_navigation_timeout(self, timeout: int):
        self._default_nav_timeout = timeout
        try:
            self.driver.set_page_load_timeout(timeout / 1000)
        except Exception:
            pass

    def set_default_timeout(self, timeout: int):
        self._default_timeout = timeout
        try:
            self.driver.implicitly_wait(timeout / 1000)
        except Exception:
            pass

    def goto(self, url: str, timeout: int = None, wait_until: str = "load",
             referer: str = None, **kwargs) -> Optional[Response]:
        """导航到URL"""
        timeout = timeout or self._default_nav_timeout
        status = 200
        ok = True

        try:
            self._focus_window()
            # 设置referer（通过CDP）
            if referer:
                try:
                    self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                        "headers": {"Referer": referer}
                    })
                except Exception:
                    pass

            # 使用eager策略快速导航
            old_timeout = self.driver.timeouts.page_load if hasattr(self.driver, 'timeouts') else None
            try:
                self.driver.set_page_load_timeout(timeout / 1000)
            except Exception:
                pass

            try:
                self.driver.get(url)
            except TimeoutException:
                # 超时不代表页面没加载，通过JS停止加载
                try:
                    self.driver.execute_script("window.stop();")
                except Exception:
                    pass

            # 根据wait_until等待
            if wait_until == "commit":
                # 最快模式：只等待URL变化
                time.sleep(0.5)
            elif wait_until in ("domcontentloaded", "load"):
                self._wait_for_load_state(wait_until, min(timeout, 30000))
            elif wait_until == "networkidle":
                self._wait_for_load_state("domcontentloaded", min(timeout, 30000))
                time.sleep(1.0)

            # 获取HTTP状态码（通过JS performance API）
            try:
                nav_entries = self.driver.execute_script(
                    "return performance.getEntriesByType('navigation')[0] || {};"
                )
                if nav_entries and isinstance(nav_entries, dict):
                    status = int(nav_entries.get("responseStatus", 200) or 200)
                    ok = 200 <= status < 400
            except Exception:
                pass

            # 导航后注入 localStorage 随机化
            try:
                self.driver.execute_script(
                    "localStorage.setItem('_v_'+Date.now(),'1');"
                    "localStorage.setItem('_s_'+Math.random().toString(36).substr(2),'1');"
                    "localStorage.setItem('_p_'+Math.random().toString(36).substr(2,8),'en');"
                )
            except Exception:
                pass

            return Response(status=status, url=url, ok=ok)

        except Exception as e:
            # 即使导航抛出异常，页面可能已经加载了部分内容
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
            return Response(status=0, url=url, ok=False)

    def _wait_for_load_state(self, state: str, timeout: int = 30000):
        """等待页面加载状态（每0.2s检查停止标志，可被停止任务中断）"""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            if _should_stop():
                try:
                    self.driver.execute_script("window.stop();")
                except Exception:
                    pass
                return
            try:
                ready = self.driver.execute_script("return document.readyState;")
                if state == "domcontentloaded" and ready in ("interactive", "complete"):
                    return
                if state == "load" and ready == "complete":
                    return
                if state == "networkidle":
                    # 粗略模拟networkidle
                    if ready == "complete":
                        time.sleep(0.5)
                        return
            except Exception:
                pass
            time.sleep(0.2)

    def wait_for_load_state(self, state: str = "load", timeout: int = 30000):
        """等待加载状态"""
        self._focus_window()
        self._wait_for_load_state(state, timeout)

    def wait_for_selector(self, selector: str, timeout: int = 30000, **kwargs) -> Optional[ElementHandle]:
        """等待选择器出现"""
        try:
            el = WebDriverWait(self.driver, timeout / 1000).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return ElementHandle(el, self)
        except TimeoutException:
            return None

    def wait_for_timeout(self, ms: int):
        """可中断的等待：分片sleep，期间检查停止标志。"""
        deadline = time.time() + ms / 1000
        while time.time() < deadline:
            if _should_stop():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.time())))

    def evaluate(self, script: str, arg=None) -> Any:
        """执行JavaScript"""
        try:
            self._focus_window()
            js = self._prepare_script(script, arg)
            if arg is not None and not isinstance(arg, (str, int, float, bool)):
                # ElementHandle传参
                if hasattr(arg, '_element'):
                    return self.driver.execute_script(js, arg._element)
                return self.driver.execute_script(js, arg)
            elif arg is not None:
                return self.driver.execute_script(js, arg)
            else:
                return self.driver.execute_script(js)
        except Exception:
            return None

    def _prepare_script(self, script: str, arg=None) -> str:
        """准备JS脚本"""
        import re as _re_ps
        s = script.strip()
        # ★ 带参箭头函数：(keywords) => {...} 或 x => {...}
        # 必须用 arguments[0] 传参调用，否则函数只被定义不被执行，返回 undefined
        if _re_ps.match(r"^\(([^()]*)\)\s*=>", s) or _re_ps.match(r"^[A-Za-z_$][\w$]*\s*=>", s):
            if arg is not None:
                return f"return ({s})(arguments[0]);"
            return f"return ({s})();"
        # 箭头函数或function
        if s.startswith("() =>") or s.startswith("(())") or s.startswith("function"):
            return f"return ({script})();"
        # 已经是表达式
        if s.startswith("return "):
            return script
        # 多行脚本
        if ";" in s or "\n" in s or s.startswith("("):
            # 已调用的 IIFE（以 () 结尾）或括号表达式 => 直接 return
            # 裸箭头函数（含 => 但未调用）=> 原样返回由上层处理
            # 注：用 strip 后的 s 拼接，避免 return 后紧跟换行触发 JS 自动分号插入(ASI)
            if s.startswith("(") and (s.endswith("()") or "=>" not in s[:100]):
                return f"return {s};"
            return script
        # 单表达式
        return f"return {s};"

    def query_selector(self, selector: str) -> Optional[ElementHandle]:
        """查询单个元素"""
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, selector)
            return ElementHandle(el, self)
        except NoSuchElementException:
            return None
        except InvalidSelectorException:
            # 尝试XPath
            try:
                el = self.driver.find_element(By.XPATH, selector)
                return ElementHandle(el, self)
            except Exception:
                return None

    def query_selector_all(self, selector: str) -> List[ElementHandle]:
        """查询多个元素"""
        try:
            els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            return [ElementHandle(el, self) for el in els]
        except InvalidSelectorException:
            try:
                els = self.driver.find_elements(By.XPATH, selector)
                return [ElementHandle(el, self) for el in els]
            except Exception:
                return []
        except Exception:
            return []

    def click(self, selector: str, timeout: int = 30000, **kwargs):
        """点击选择器匹配的元素"""
        try:
            el = WebDriverWait(self.driver, timeout / 1000).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            el.click()
        except Exception:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, selector)
                self.driver.execute_script("arguments[0].click();", el)
            except Exception:
                pass

    def fill(self, selector: str, value: str, timeout: int = 30000):
        """填充输入框"""
        try:
            el = WebDriverWait(self.driver, timeout / 1000).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            el.clear()
            el.send_keys(value)
        except Exception:
            pass

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    def go_back(self, timeout: int = 30000, wait_until: str = "domcontentloaded", **kwargs):
        """浏览器后退"""
        try:
            self.driver.back()
            self._wait_for_load_state(wait_until, timeout)
        except Exception:
            pass

    def reload(self, timeout: int = 30000, wait_until: str = "domcontentloaded", **kwargs):
        """刷新页面"""
        try:
            self.driver.refresh()
            self._wait_for_load_state(wait_until, timeout)
        except Exception:
            pass

    @property
    def frames(self) -> List[Frame]:
        """获取所有frames"""
        frames = [Frame(self.driver, None)]  # 主frame
        try:
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            for i, iframe in enumerate(iframes):
                frames.append(Frame(self.driver, i))
        except Exception:
            pass
        return frames

    def frame(self, url: str = None, name: str = None) -> Optional[Frame]:
        """获取指定frame"""
        try:
            if name:
                return Frame(self.driver, name)
            if url:
                iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                for i, iframe in enumerate(iframes):
                    src = iframe.get_attribute("src") or ""
                    if url in src:
                        return Frame(self.driver, i)
        except Exception:
            pass
        return None

    def bring_to_front(self):
        """将窗口置前"""
        try:
            if self._window_handle:
                # ★ 绑定句柄的窗口（如弹窗）：切到自身并OS级前置
                self.driver.switch_to.window(self._window_handle)
                try:
                    self.driver.execute_cdp_cmd("Page.bringToFront", {})
                except Exception:
                    pass
            else:
                # 主页面：焦点可能已被弹窗篡夺，切回context记录的主窗口
                _main_h = getattr(self._context, "_main_window_handle", None)
                if _main_h:
                    try:
                        self.driver.switch_to.window(_main_h)
                    except Exception:
                        pass
        except Exception:
            pass

    def add_init_script(self, script: str):
        """添加初始化脚本（通过CDP Page.addScriptToEvaluateOnNewDocument实现）"""
        self._init_scripts.append(script)
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": script
            })
        except Exception:
            pass

    def evaluate_on_new_document(self, script: str):
        """同 add_init_script"""
        self.add_init_script(script)

    def route(self, url_pattern: str, handler: Callable):
        """注册请求拦截（通过CDP Network域实现）"""
        self._request_handlers.append((url_pattern, handler))
        self._enable_network_interception()

    def unroute(self, url_pattern: str = None):
        """移除请求拦截"""
        if url_pattern:
            self._request_handlers = [
                (p, h) for p, h in self._request_handlers if p != url_pattern
            ]
        else:
            self._request_handlers = []

    def _enable_network_interception(self):
        """启用CDP网络请求拦截"""
        if self._network_interception_enabled:
            return
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self._network_interception_enabled = True
            # Selenium中请求拦截通过CDP事件实现比较复杂，
            # 这里使用selenium-wire或mitmproxy风格会太重。
            # 采用折中方案：通过Chrome扩展或请求头设置来处理。
            # 对于主要场景（设置Referer/Origin/UA），通过CDP的
            # Network.setExtraHTTPHeaders和requestWillBeSent监听。
            # 简化实现：直接在所有新请求中附加额外headers
        except Exception:
            pass

    # ================= 网络请求采集（on("request") 数据源） =================
    # ★ 26.8.13.3 新增：Selenium 无官方 CDP 事件订阅 API，采用组合方案：
    #   1) CDP Network.enable（尽力而为，失败仅记 warning 不阻断）
    #   2) JS hook（fetch/XHR/sendBeacon/Image）写入 window.__sb_requests 数组
    #   3) 守护线程轮询 drain 数组 → 存入 _request_records → 派发 route handlers
    #      与 request 事件（心跳监听/流量分析）
    def _start_request_collection(self):
        """启动网络请求采集管道（幂等）：on("request") 注册时自动调用。"""
        if self._collecting:
            return
        self._collecting = True
        self._collect_stop.clear()
        # 1) CDP Network.enable（失败不抛异常，仅记 warning；JS hook 仍可采集）
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self._network_interception_enabled = True
        except Exception as e:
            logger.warning(f"Page 网络采集 CDP Network.enable 失败，仅依赖 JS hook: {e}")
        # 2) JS hook：立即注入当前文档 + 注册到后续新文档
        self._inject_request_hook()
        # 3) 轮询守护线程
        self._collect_thread = threading.Thread(
            target=self._poll_request_records, name="sb-req-collect", daemon=True
        )
        self._collect_thread.start()

    def _stop_request_collection(self):
        """停止网络请求采集（幂等）：通知轮询线程退出（Page.close() 时调用）。"""
        if not self._collecting:
            return
        self._collecting = False
        self._collect_stop.set()
        t = self._collect_thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        self._collect_thread = None

    def _inject_request_hook(self):
        """注入 JS hook：把 fetch/XHR/sendBeacon/Image 请求写入 window.__sb_requests。
        数组元素形如 {u: url, m: method, t: resourceType, n: 时间戳}，
        轮询线程 drain 时翻译成 Request 记录。
        """
        hook_js = r"""
        (function() {
            try {
                if (window.__sb_req_hooked) { return; }
                window.__sb_req_hooked = true;
                if (!window.__sb_requests) { window.__sb_requests = []; }
                function _sb_push(u, m, t) {
                    try {
                        if (!u || String(u).indexOf('about:') === 0) { return; }
                        window.__sb_requests.push({u: String(u), m: String(m || 'GET'), t: String(t || 'other'), n: Date.now()});
                        if (window.__sb_requests.length > 2000) {
                            window.__sb_requests.splice(0, window.__sb_requests.length - 2000);
                        }
                    } catch(e) {}
                }
                // fetch
                if (window.fetch) {
                    var _f = window.fetch;
                    window.fetch = function(resource, init) {
                        var u = (typeof resource === 'string') ? resource : (resource && resource.url);
                        var m = (init && init.method) || (resource && resource.method) || 'GET';
                        _sb_push(u, m, 'fetch');
                        return _f.apply(this, arguments);
                    };
                }
                // XMLHttpRequest
                var _oOpen = XMLHttpRequest.prototype.open;
                var _oSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this._sb = {m: method, u: url};
                    return _oOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                    if (this._sb) { _sb_push(this._sb.u, this._sb.m, 'xhr'); }
                    return _oSend.apply(this, arguments);
                };
                // sendBeacon（心跳/埋点常用）
                if (navigator.sendBeacon) {
                    var _sb = navigator.sendBeacon.bind(navigator);
                    navigator.sendBeacon = function(url, data) {
                        _sb_push(url, 'POST', 'beacon');
                        return _sb(url, data);
                    };
                }
                // Image（new Image().src 埋点）：劫持原型 setter，稳健且覆盖所有实例
                var _srcDesc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
                if (_srcDesc && _srcDesc.set) {
                    Object.defineProperty(HTMLImageElement.prototype, 'src', {
                        set: function(v) { _sb_push(v, 'GET', 'img'); _srcDesc.set.call(this, v); },
                        get: function() { return _srcDesc.get.call(this); }
                    });
                }
            } catch(e) {}
        })();
        """
        # 注册到后续所有新文档（弹窗 about:blank → 重定向 → 落地页全程生效）
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": hook_js
            })
        except Exception as e:
            logger.debug(f"Page 请求 hook 注册新文档失败: {e}")
        # 立即注入当前文档
        try:
            self.driver.execute_script(hook_js)
        except Exception:
            pass

    def _poll_request_records(self):
        """轮询守护线程：每 0.5s 从绑定窗口的 JS 数组 drain 请求记录。
        页面不可用/驱动异常时静默跳过；stop 事件或全局停止标志触发后退出。
        """
        while not self._collect_stop.is_set():
            if _should_stop():
                break
            items = []
            try:
                if self._window_handle:
                    # 绑定窗口已关闭 → 等下一轮（避免 drain 到其它窗口的数据）
                    if self._window_handle not in list(self.driver.window_handles):
                        time.sleep(0.5)
                        continue
                    self._focus_window()
                items = self.driver.execute_script(
                    "var a = window.__sb_requests || []; window.__sb_requests = []; return a;"
                ) or []
            except Exception:
                items = []
            if items:
                self._handle_collected_items(items)
            time.sleep(0.5)

    def _handle_collected_items(self, items):
        """翻译 JS 数组元素为 Request 记录：入库 + 派发 route handlers + request 事件"""
        records = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                url = str(it.get("u") or "")
                if not url or url.startswith("about:"):
                    continue
                records.append({
                    "url": url,
                    "method": str(it.get("m") or "GET"),
                    "resource_type": str(it.get("t") or "other"),
                    "timestamp": float(it.get("n") or time.time()),
                })
            except Exception:
                continue
        if not records:
            return
        with self._request_records_lock:
            self._request_records.extend(records)
        for rec in records:
            try:
                req = Request(url=rec["url"], headers={})
                req.method = rec["method"]
                req.resource_type = rec["resource_type"]
                # B：先执行 route handlers（可 abort/fulfill/continue），再派发 request 事件
                self._dispatch_route_handlers(req)
                self.emit("request", req)
            except Exception:
                continue

    def _dispatch_route_handlers(self, req: Request):
        """按注册顺序执行全部 route handlers（页面级 + context 级），保持
        "每次请求都执行 handler 列表" 的语义；handler 异常捕获，不阻断采集。
        注意：context.route() 会同时写入 context._route_handlers 与主页面
        _request_handlers，合并时按 (pattern, handler) 去重，避免同一 handler 双跑。
        """
        merged: List[tuple] = []
        seen = set()
        for pattern, handler in list(self._request_handlers):
            key = (pattern, id(handler))
            if key in seen:
                continue
            seen.add(key)
            merged.append((pattern, handler))
        ctx = self._context
        if ctx is not None:
            for pattern, handler in list(getattr(ctx, "_route_handlers", []) or []):
                key = (pattern, id(handler))
                if key in seen:
                    continue
                seen.add(key)
                merged.append((pattern, handler))
        if not merged:
            return
        route = Route(request=req)
        for pattern, handler in merged:
            if not self._route_pattern_match(pattern, req.url):
                continue
            try:
                handler(route, req)
            except Exception as e:
                logger.debug(f"Route handler 异常(忽略): {e}")
        # 汇总决策日志（abort / fulfill / continue 语义由状态机记录）
        if route._aborted:
            logger.info(f"[Route] 请求已被拦截: {req.url}")
        elif route._fulfilled is not None:
            logger.info(f"[Route] 本地响应替代: {req.url} status={route._fulfilled[0]}")

    @staticmethod
    def _route_pattern_match(pattern: str, url: str) -> bool:
        """近似匹配 Playwright glob 模式（**、**/*、**/*.mp4 等）。
        fnmatch 把 ** 视为 *，可覆盖本项目实际用到的全部模式；匹配失败返回 False 放行。
        """
        if not pattern:
            return True
        try:
            p = str(pattern)
            if p in ("**", "**/*", "/*", "*"):
                return True
            import fnmatch as _fnmatch
            if _fnmatch.fnmatch(url, p) or _fnmatch.fnmatch(url.lower(), p.lower()):
                return True
            return False
        except Exception:
            return False

    def _setup_request_interception(self):
        """配置请求拦截（在页面首次加载前调用）"""
        # Selenium CDP请求拦截比较复杂，这里用JS fetch/xhr hook作为替代
        if not self._request_handlers:
            return

        # 构建拦截JS脚本，hook fetch和XMLHttpRequest
        block_domains = set()
        extra_headers_map = {}

        for pattern, handler in self._request_handlers:
            # 分析handler闭包中的拦截逻辑
            # 对于常见的block模式和header修改模式，通过JS实现
            pass

        # 注入请求拦截JS（hook fetch/xhr）
        intercept_js = """
        (function() {
            // Hook fetch to add headers and block requests
            const _originalFetch = window.fetch;
            window.fetch = function(resource, init) {
                let url = typeof resource === 'string' ? resource : resource.url;
                init = init || {};
                init.headers = init.headers || {};

                // Block noisy domains
                const blockedDomains = ['googleapis.com', 'safebrowsing', 'gvt1.com',
                    'gstatic.com/generate_204', 'httpbin.org', 'api.ipify.org',
                    'icanhazip.com', 'ifconfig.me', 'checkip.amazonaws.com', 'ident.me'];
                for (const d of blockedDomains) {
                    if (url.toLowerCase().includes(d)) {
                        return new Promise((resolve, reject) => {
                            reject(new Error('Blocked'));
                        });
                    }
                }

                return _originalFetch.call(this, resource, init);
            };

            // Hook XMLHttpRequest
            const _originalOpen = XMLHttpRequest.prototype.open;
            const _originalSend = XMLHttpRequest.prototype.send;
            const _originalSetHeader = XMLHttpRequest.prototype.setRequestHeader;

            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                this._url = url;
                const blockedDomains = ['googleapis.com', 'safebrowsing', 'gvt1.com',
                    'gstatic.com/generate_204', 'httpbin.org', 'api.ipify.org',
                    'icanhazip.com', 'ifconfig.me', 'checkip.amazonaws.com', 'ident.me'];
                for (const d of blockedDomains) {
                    if (url.toLowerCase().includes(d)) {
                        this._blocked = true;
                        return;
                    }
                }
                return _originalOpen.call(this, method, url, ...rest);
            };

            XMLHttpRequest.prototype.send = function(...args) {
                if (this._blocked) {
                    this.dispatchEvent(new Event('error'));
                    return;
                }
                return _originalSend.call(this, ...args);
            };
        })();
        """
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": intercept_js
            })
        except Exception:
            pass

    def _apply_init_scripts(self):
        """应用所有初始化脚本到当前页面"""
        for script in self._init_scripts:
            try:
                self.driver.execute_script(script)
            except Exception:
                pass


# ========== CDP Session 兼容类 ==========
class _CDPSession:
    """模拟 Playwright CDPSession：.send(method, params) -> driver.execute_cdp_cmd"""

    def __init__(self, driver, page=None, session_id=None):
        self.driver = driver
        self.page = page
        # ★ 26.8.13 修复：支持调用方显式传入 session_id；
        # Selenium 3 手工注入兜底时优先使用它（driver.session_id 可能不存在）
        self.session_id = session_id or getattr(driver, "session_id", None)

    def send(self, method: str, params: Dict = None) -> Any:
        """发送 CDP 命令（Selenium 3/4/5 通吃的兼容实现）
        - 优先使用 Selenium 4+ 官方 API: driver.execute_cdp_cmd(method, params)
        - 若失败（TypeError/KeyError 等），走 driver.execute 兜底（自动注入 sessionId）
        - 再不行，走 _commands 手工注入兜底（补 sessionId）

        ★ 26.8.11.11 修复【根因】：旧兜底直接调 _cmd_exec.execute() 没带 sessionId，
          URL 模板 /session/$sessionId/goog/cdp/execute 在替换时抛 KeyError: 'sessionId'，
          导致 Pop-under CDP 点击 100% 失败 → HilltopAds 零点击零收益。
        """
        _params = params or {}
        driver = self.driver

        # 方法 1：Selenium 4.0+ 原生 execute_cdp_cmd（最稳，优先）
        _native = getattr(driver, "execute_cdp_cmd", None)
        if _native is not None and callable(_native) and not getattr(_native, "_dynamic_cdp", False):
            try:
                return driver.execute_cdp_cmd(method, _params)
            except (TypeError, KeyError):
                # 个别版本签名不匹配 / 内部异常，降级走 driver.execute
                pass

        # 方法 2：走 driver.execute（Selenium 4 WebDriver.execute 会自动注入 sessionId）
        #   — 这是最接近官方实现的兜底，URL 模板替换有保证
        if hasattr(driver, "execute") and callable(getattr(driver, "execute", None)):
            try:
                resp = driver.execute("executeCdpCommand", {"cmd": method, "params": _params})
                if isinstance(resp, dict) and "value" in resp:
                    return resp["value"]
                return resp
            except Exception:
                # 还不行再退到手工注入
                pass

        # 方法 3：Selenium 3.x → 通过 command_executor 手工注入 executeCdpCommand 端点
        #   ★ 关键修复：params 里必须带 sessionId，否则 URL 模板 $sessionId 替换抛 KeyError
        _cmd_exec = getattr(driver, "command_executor", None)
        if _cmd_exec is not None and hasattr(_cmd_exec, "_commands"):
            if "executeCdpCommand" not in _cmd_exec._commands:
                try:
                    _cmd_exec._commands["executeCdpCommand"] = (
                        "POST",
                        "/session/$sessionId/goog/cdp/execute",
                    )
                except Exception:
                    pass
            # ★ 补 sessionId（从 driver.session_id 取），修复 KeyError: 'sessionId'
            _exec_params = {"cmd": method, "params": _params}
            _sid = getattr(driver, "session_id", None)
            if _sid and "sessionId" not in _exec_params:
                _exec_params["sessionId"] = _sid
            try:
                result = _cmd_exec.execute("executeCdpCommand", _exec_params)
            except TypeError:
                # 极少数老版本 execute 签名是 (command, name, params)
                try:
                    result = _cmd_exec.execute("executeCdpCommand", method, _params)
                except Exception:
                    result = None
            if isinstance(result, dict) and "value" in result:
                return result["value"]
            return result

        raise RuntimeError(
            f"当前 Selenium 驱动不支持发送 CDP 命令（driver={type(driver).__name__}，"
            f"has_execute_cdp_cmd={hasattr(driver, 'execute_cdp_cmd')}）"
        )

    def close(self):
        pass


# ========== BrowserContext 兼容类 ==========
class BrowserContext:
    """模拟 Playwright BrowserContext"""

    def __init__(self, driver, options: Dict = None):
        self.driver = driver
        self._options = options or {}
        self._init_scripts = []
        self._route_handlers = []
        self._pages = []
        self._cookies = []
        self._closed = False
        # ★ Browser 反向引用（Browser.new_context 中设置），close() 时从 contexts 移除
        self._browser = None

        # 创建默认page
        self._main_page = Page(driver, context=self)
        self._pages.append(self._main_page)

        # ★ 窗口动态跟踪：记录主窗口句柄 + 弹窗句柄→Page映射
        try:
            self._main_window_handle = driver.current_window_handle
        except Exception:
            self._main_window_handle = None
        self._handle_pages = {}

        # 应用init scripts和request interception到driver
        self._apply_context_config()

    @property
    def options(self):
        """公开options属性，兼容Playwright API（context.options.get('user_agent')）"""
        return self._options

    @staticmethod
    def _build_ua_metadata(user_agent: str, platform_hint: str = ""):
        """根据 UA 字符串构建 userAgentMetadata（Client Hints），与 UA 一致，供 Sec-Ch-Ua 使用。"""
        import re as _re
        try:
            m = _re.search(r"Chrome/(\d+)", user_agent or "")
            major = m.group(1) if m else "120"
            full_m = _re.search(r"Chrome/([\d.]+)", user_agent or "")
            full_ver = full_m.group(1) if full_m else (major + ".0.0.0")
            ua_l = (user_agent or "").lower()
            if "windows" in ua_l:
                platform = "Windows"; platform_version = "15.0.0"
            elif "mac os x" in ua_l or "macintosh" in ua_l:
                platform = "macOS"; platform_version = "13.0.0"
            elif "android" in ua_l:
                platform = "Android"; platform_version = "13.0.0"
            elif "linux" in ua_l:
                platform = "Linux"; platform_version = "6.0.0"
            else:
                platform = platform_hint or "Windows"; platform_version = "15.0.0"
            mobile = "mobile" in ua_l or "android" in ua_l
            arch = "arm" if ("arm" in ua_l or platform == "Android") else "x86"
            brands = [
                {"brand": "Not)A;Brand", "version": "99"},
                {"brand": "Google Chrome", "version": major},
                {"brand": "Chromium", "version": major},
            ]
            full_version_list = [
                {"brand": "Not)A;Brand", "version": "99.0.0.0"},
                {"brand": "Google Chrome", "version": full_ver},
                {"brand": "Chromium", "version": full_ver},
            ]
            return {
                "brands": brands,
                "fullVersionList": full_version_list,
                "fullVersion": full_ver,
                "platform": platform,
                "platformVersion": platform_version,
                "architecture": arch,
                "model": "",
                "mobile": mobile,
                "bitness": "64",
                "wow64": False,
            }
        except Exception:
            return None

    def _apply_context_config(self):
        """应用context配置（UA、语言、时区、headers等CDP命令）"""
        opts = self._options
        user_agent = opts.get("user_agent", "")
        accept_language = ""
        extra_headers = opts.get("extra_http_headers") or {}
        for k, v in extra_headers.items():
            if k.lower() == "accept-language":
                accept_language = v

        try:
            cdp_kwargs = {}
            if user_agent:
                cdp_kwargs["userAgent"] = user_agent
            if accept_language:
                cdp_kwargs["acceptLanguage"] = accept_language
            # 构建 userAgentMetadata（Client Hints / Sec-Ch-Ua），与 UA 保持一致，避免缺失或冲突
            if user_agent:
                meta = self._build_ua_metadata(user_agent, opts.get("platform", ""))
                if meta:
                    cdp_kwargs["userAgentMetadata"] = meta
            if cdp_kwargs:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride", cdp_kwargs)
        except Exception as e:
            logger.debug(f"CDP Network.setUserAgentOverride 失败: {e}")

        # 设置时区
        timezone = opts.get("timezone_id", "")
        if timezone:
            try:
                self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
                    "timezoneId": timezone
                })
            except Exception as e:
                logger.debug(f"CDP Emulation.setTimezoneOverride 失败: {e}")

        # 设置语言locale
        locale = opts.get("locale", "")
        if locale:
            try:
                self.driver.execute_cdp_cmd("Emulation.setLocaleOverride", {
                    "locale": locale
                })
            except Exception as e:
                logger.debug(f"CDP Emulation.setLocaleOverride 失败: {e}")

        # 设置视口
        viewport = opts.get("viewport", {})
        if viewport:
            w = viewport.get("width", 1920)
            h = viewport.get("height", 1080)
            try:
                self.driver.set_window_size(w, h)
                # ★ 26.8.13.3 修复：setDeviceMetricsOverride 必须带 screenWidth/screenHeight，
                #   否则 screen.width/height 仍是真实屏幕(如 800x600)，视口 1920x1080 会被
                #   判定为"视口大于屏幕分辨率"（vw > sw + 20）。
                self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": w, "height": h,
                    "screenWidth": w, "screenHeight": h,
                    "deviceScaleFactor": opts.get("device_scale_factor", 1),
                    "mobile": opts.get("is_mobile", False)
                })
            except Exception as e:
                logger.debug(f"CDP Emulation.setDeviceMetricsOverride 失败: {e}")

        # 设置额外HTTP头
        if extra_headers:
            try:
                self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                    "headers": extra_headers
                })
            except Exception as e:
                logger.debug(f"CDP Network.setExtraHTTPHeaders 失败: {e}")

        # 设置权限（全部拒绝）
        try:
            self.driver.execute_cdp_cmd("Browser.setPermission", {
                "permission": {"name": "notifications"},
                "setting": "denied"
            })
        except Exception as e:
            logger.debug(f"CDP Browser.setPermission 失败: {e}")

        # 加载storage_state（cookies和localStorage）
        storage_state_path = opts.get("storage_state")
        if storage_state_path and os.path.isfile(storage_state_path):
            self._load_storage_state(storage_state_path)

    def _load_storage_state(self, path: str):
        """加载storage state (cookies + localStorage)"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            # 设置cookies
            cookies = state.get("cookies", [])
            if cookies:
                selenium_cookies = []
                for c in cookies:
                    sc = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    }
                    if c.get("expires") and c["expires"] > 0:
                        sc["expiry"] = int(c["expires"])
                    if c.get("httpOnly"):
                        sc["httpOnly"] = c["httpOnly"]
                    if c.get("secure"):
                        sc["secure"] = c["secure"]
                    if c.get("sameSite"):
                        sc["sameSite"] = c["sameSite"]
                    selenium_cookies.append(sc)
                # 需要先导航到对应域名才能设置cookie
                for c in selenium_cookies:
                    try:
                        self.driver.execute_cdp_cmd("Network.setCookie", c)
                    except Exception:
                        pass

            # localStorage通过JS注入
            origins = state.get("origins", [])
            for origin_data in origins:
                origin = origin_data.get("origin", "")
                local_storage = origin_data.get("localStorage", [])
                session_storage = origin_data.get("sessionStorage", [])
                if origin and (local_storage or session_storage):
                    # 先导航到origin（about:blank然后注入）
                    storage_js = ""
                    for item in local_storage:
                        key = json.dumps(item.get("name", ""))
                        val = json.dumps(item.get("value", ""))
                        storage_js += f"try {{ localStorage.setItem({key}, {val}); }} catch(e) {{}}\n"
                    for item in session_storage:
                        key = json.dumps(item.get("name", ""))
                        val = json.dumps(item.get("value", ""))
                        storage_js += f"try {{ sessionStorage.setItem({key}, {val}); }} catch(e) {{}}\n"
                    if storage_js:
                        try:
                            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                                "source": storage_js
                            })
                        except Exception:
                            pass
        except Exception as e:
            pass

    def add_init_script(self, script: str):
        """添加初始化脚本（CDP Page.addScriptToEvaluateOnNewDocument）"""
        self._init_scripts.append(script)
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": script
            })
        except Exception:
            pass

    def new_page(self) -> Page:
        """创建新页面"""
        # Selenium中driver启动后已经有一个页面，直接使用
        page = Page(self.driver, context=self)

        # 应用所有init scripts到当前页面（在导航之前已通过CDP注册，
        # 但也在当前about:blank执行一次）
        for script in self._init_scripts:
            try:
                self.driver.execute_script(script)
            except Exception:
                pass

        return page

    def route(self, url_pattern: str, handler: Callable):
        """请求路由拦截（Selenium下通过CDP Network.setExtraHTTPHeaders + JS Fetch/XMLHttpRequest hook）"""
        self._route_handlers.append((url_pattern, handler))
        # 页面级别的请求拦截配置
        self._main_page.route(url_pattern, handler)

        # ---- 额外实际效果：尝试从 handler 里提取 Referer 等header规则并注入 ----
        # 分析 handler 源码，提取常见的 header 设置模式
        try:
            import inspect
            src = inspect.getsource(handler)
            # 模式1: route.continue_(headers={...}) 里的硬编码 headers
            headers_js_lines = []
            # 提取常见的 Referer 值
            referer_m = re.search(r'"Referer"\s*:\s*"([^"]+)"', src)
            ua_m = re.search(r'"User-Agent"\s*:\s*"([^"]+)"', src)
            if referer_m or ua_m:
                extra_cdp = {}
                if referer_m:
                    extra_cdp["Referer"] = referer_m.group(1)
                if ua_m:
                    extra_cdp["User-Agent"] = ua_m.group(1)
                if extra_cdp:
                    try:
                        self.driver.execute_cdp_cmd(
                            "Network.setExtraHTTPHeaders", {"headers": extra_cdp}
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    def unroute(self, url_pattern: str = None):
        self._main_page.unroute(url_pattern)

    def add_cookies(self, cookies: List[Dict]):
        """添加cookies"""
        for c in cookies:
            try:
                self.driver.execute_cdp_cmd("Network.setCookie", c)
            except Exception:
                pass

    def cookies(self, urls: List[str] = None) -> List[Dict]:
        """获取cookies"""
        try:
            result = self.driver.execute_cdp_cmd("Network.getAllCookies", {})
            return result.get("cookies", [])
        except Exception:
            try:
                return self.driver.get_cookies()
            except Exception:
                return []

    def storage_state(self, path: str = None) -> Dict:
        """导出storage state"""
        state = {"cookies": [], "origins": []}

        # 获取cookies
        try:
            cookies = self.driver.execute_cdp_cmd("Network.getAllCookies", {})
            state["cookies"] = cookies.get("cookies", [])
        except Exception:
            try:
                state["cookies"] = self.driver.get_cookies()
            except Exception:
                pass

        # 获取localStorage
        try:
            current_url = self.driver.current_url or ""
            if current_url.startswith("http"):
                origin = f"{urllib.parse.urlparse(current_url).scheme}://{urllib.parse.urlparse(current_url).netloc}"
                ls_data = self.driver.execute_script("""
                    var items = {};
                    for (var i = 0; i < localStorage.length; i++) {
                        var key = localStorage.key(i);
                        items[key] = localStorage.getItem(key);
                    }
                    return items;
                """) or {}
                ls_list = [{"name": k, "value": v} for k, v in ls_data.items()]
                # 获取 sessionStorage（同源）
                ss_data = self.driver.execute_script("""
                    var items = {};
                    try {
                        for (var i = 0; i < sessionStorage.length; i++) {
                            var key = sessionStorage.key(i);
                            items[key] = sessionStorage.getItem(key);
                        }
                    } catch(e) {}
                    return items;
                """) or {}
                ss_list = [{"name": k, "value": v} for k, v in ss_data.items()]
                if ls_list or ss_list:
                    origin_entry = {"origin": origin, "localStorage": ls_list}
                    if ss_list:
                        origin_entry["sessionStorage"] = ss_list
                    state["origins"] = [origin_entry]
        except Exception:
            pass

        # 保存到文件
        if path:
            os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

        return state

    def add_extension(self, path: str):
        """添加扩展（Selenium中需在启动时加载，此处仅记录）"""
        # Selenium的扩展加载必须在ChromeOptions中设置，启动后无法动态添加
        # 这里记录路径，在Browser启动时统一处理
        pass

    def close(self, timeout: int = 15000):
        """关闭context（在Selenium中关闭driver）"""
        pass  # driver在Browser级别关闭

    def new_cdp_session(self, page=None):
        """创建 CDP 会话（Playwright BrowserContext.new_cdp_session 兼容）。

        Selenium 下所有页面共享同一 chromedriver 的 CDP 通道，
        因此返回一个把 .send(method, params) 映射到 driver.execute_cdp_cmd 的包装对象。
        popunder_trigger 依赖它派发 isTrusted 的 Input.dispatchMouseEvent/KeyEvent。
        """
        return _CDPSession(self.driver, page)

    @property
    def pages(self) -> List[Page]:
        """★ 动态返回当前所有标签页（含弹窗）。
        旧实现只返回静态 _pages（永远只有主页面），导致 popunder_trigger
        和广告落地页检测永远看不到新开的标签。现按 driver.window_handles
        实时构建：主窗口复用 _main_page（保持焦点语义），其余窗口创建
        绑定句柄的 Page 并按句柄缓存（保证对象身份稳定，支持 id() 去重）。
        """
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            return list(self._pages)
        result: List[Page] = []
        for h in handles:
            if self._main_window_handle and h == self._main_window_handle:
                result.append(self._main_page)
                continue
            p = self._handle_pages.get(h)
            if p is None:
                p = Page(self.driver, context=self)
                p._window_handle = h
                self._handle_pages[h] = p
            result.append(p)
        # 清理已关闭窗口的缓存
        for h in list(self._handle_pages.keys()):
            if h not in handles:
                self._handle_pages.pop(h, None)
        return result


# ========== Browser 兼容类 ==========
class Browser:
    """模拟 Playwright Browser"""

    def __init__(self, driver):
        self.driver = driver
        self._contexts = []
        self._closed = False

    def new_context(self, **kwargs) -> BrowserContext:
        """创建新的浏览器上下文"""
        context = BrowserContext(self.driver, kwargs)
        context._browser = self  # ★ 反向引用，供 context.close() 从 contexts 移除
        self._contexts.append(context)
        return context

    def close(self, timeout: int = 20000):
        """关闭浏览器（quit driver + 强杀残留进程兜底）。"""
        if self._closed:
            return
        self._closed = True
        # 在子线程里优雅 quit，避免被 chromedriver 串行命令队列卡死
        def _graceful():
            try:
                self.driver.quit()
            except Exception:
                try:
                    self.driver.close()
                except Exception:
                    pass
        t = threading.Thread(target=_graceful, daemon=True)
        t.start()
        t.join(timeout=max(2, timeout / 1000))
        # 无论 quit 是否在超时内完成，都强杀残留进程并注销，确保浏览器一定关闭
        _kill_driver_processes(self.driver)
        _unregister_driver(self.driver)

    @property
    def contexts(self) -> List[BrowserContext]:
        return self._contexts

    def is_connected(self) -> bool:
        try:
            self.driver.current_url
            return True
        except Exception:
            return False


# ========== Chromium 启动器 ==========
class Chromium:
    """模拟 p.chromium"""

    def launch(self, headless: bool = False, args: List[str] = None,
               channel: str = None, **kwargs) -> Browser:
        """启动Chrome浏览器"""
        args = args or []
        chrome_options = ChromeOptions()

        # 检测Chrome二进制路径
        chrome_binary = None
        if channel == "chrome":
            chrome_binary = _find_chrome_binary()

        if chrome_binary:
            chrome_options.binary_location = chrome_binary

        # 无头模式
        if headless:
            chrome_options.add_argument("--headless=new")

        # 添加启动参数
        extensions_to_load = []
        extensions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extensions')

        # 收集host-rules用于block噪音域名（IP检测、Chrome后台服务）
        host_rules_map = [
            "MAP api.ipify.org 0.0.0.0",
            "MAP icanhazip.com 0.0.0.0",
            "MAP ifconfig.me 0.0.0.0",
            "MAP checkip.amazonaws.com 0.0.0.0",
            "MAP ident.me 0.0.0.0",
            "MAP httpbin.org 0.0.0.0",
            "MAP safebrowsing.googleapis.com 0.0.0.0",
            "MAP safebrowsinghttpgateway.googleapis.com 0.0.0.0",
            "MAP clients2.google.com 0.0.0.0",
            "MAP gvt1.com 0.0.0.0",
            "MAP gstatic.com/generate_204 0.0.0.0",
            "MAP accounts.google.com 0.0.0.0",
        ]
        # 注意：不block googleapis.com整体（会影响Chrome功能），只block特定子域名
        args.append(f"--host-rules={','.join(host_rules_map)}")

        # ★ 检测是否有--load-extension参数（如代理认证扩展）
        has_load_extension = any(a.startswith('--load-extension') for a in args)

        for arg in args:
            if arg.startswith("--disable-extensions"):
                # 如果有--load-extension或.crx扩展要加载，跳过--disable-extensions
                if has_load_extension or os.path.exists(extensions_dir):
                    continue  # 不添加--disable-extensions
                else:
                    chrome_options.add_argument(arg)
            else:
                chrome_options.add_argument(arg)

        # 加载扩展（extensions目录中的.crx文件）
        if os.path.exists(extensions_dir):
            ext_paths = []
            for f in os.listdir(extensions_dir):
                if f.endswith('.crx'):
                    ext_paths.append(os.path.join(extensions_dir, f))
            if ext_paths:
                has_load_extension = True

        # 关键反检测配置
        # ★ excludeSwitches 必须一次性设置，多次调用会覆盖！
        _exclude_switches = ["enable-automation"]
        if has_load_extension:
            _exclude_switches.append("disable-extensions")
        chrome_options.add_experimental_option("excludeSwitches", _exclude_switches)
        chrome_options.add_experimental_option("useAutomationExtension", False)

        # 设置页面加载策略为none，由我们手动控制等待
        chrome_options.page_load_strategy = "none"

        # 禁用自动化控制特征
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        # 防止 headless 模式下页面被后台节流（定时器/网络请求被降速）
        chrome_options.add_argument("--disable-backgrounding-occluded-windows")
        chrome_options.add_argument("--disable-renderer-backgrounding")
        chrome_options.add_argument("--disable-background-timer-throttling")
        # 禁用信息栏和默认浏览器检查（避免额外UI干扰）
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")

        # 终极修复：Selenium 4.45+ 会自动调用 Selenium Manager
        # 已移除系统中的旧版 chromedriver v108，Selenium Manager 将自动下载 v149
        # ★ 26.8.9.6：改用带 HTTP 读超时的启动器，杜绝 chromedriver 通道无限阻塞
        try:
            driver = _launch_chrome_with_timeout(chrome_options)
        except Exception as e:
            error_msg = str(e)
            # 如果是因为找不到驱动，尝试手动指定 Chrome 二进制路径后重试
            if 'Unable to obtain driver' in error_msg or 'chromedriver' in error_msg.lower():
                if chrome_binary:
                    chrome_options.binary_location = chrome_binary
                    try:
                        driver = _launch_chrome_with_timeout(chrome_options)
                    except Exception as e2:
                        raise RuntimeError(f"Chrome启动失败: {e}; 指定binary后仍失败: {e2}")
                else:
                    raise
            else:
                raise

        # 执行CDP反检测配置
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                // 基础反检测
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                // 删除cdc_特征（★ 26.8.13.3：改为动态遍历全部 cdc_/$cdc_ 前缀键，
                //   旧代码只删 3 个固定键，新 chromedriver 的 _Object/_Proxy/_JSON/_Window 等键会残留）
                (function() {
                    for (const k of Object.getOwnPropertyNames(window)) {
                        if (k.indexOf('cdc_') === 0 || k.indexOf('$cdc_') === 0) {
                            try { delete window[k]; } catch(e) {}
                        }
                    }
                })();
                // WebRTC IP泄露保护（保留API但阻止内网IP泄露）
                (function() {
                    const _origRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
                    if (!_origRTC) return;
                    const _patchedRTC = function(config) {
                        if (config && config.iceServers) { config.iceServers = []; }
                        const pc = new _origRTC(config);
                        pc.addEventListener('icecandidate', function(e) {
                            if (e.candidate && e.candidate.candidate) {
                                const c = e.candidate.candidate;
                                if (/((10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.))/.test(c)) { return; }
                            }
                        });
                        return pc;
                    };
                    _patchedRTC.prototype = _origRTC.prototype;
                    Object.defineProperty(_patchedRTC, 'toString', {
                        value: function() { return 'function RTCPeerConnection() { [native code] }'; }
                    });
                    window.RTCPeerConnection = _patchedRTC;
                    if (window.webkitRTCPeerConnection) { window.webkitRTCPeerConnection = _patchedRTC; }
                })();
                // Cookie/LocalStorage 随机化
                try {
                    if (localStorage.length < 3) {
                        localStorage.setItem('_visitor_' + Date.now(), 'true');
                        localStorage.setItem('_session_' + Math.random().toString(36).substr(2), '1');
                        localStorage.setItem('_pref_' + Math.random().toString(36).substr(2, 8), 'en');
                    }
                } catch(e) {}
                """
            })
        except Exception as e:
            logger.debug(f"CDP Page.addScriptToEvaluateOnNewDocument 失败: {e}")

        # 移除webdriver标识 + 设置默认请求头（后续 new_context 会用实际指纹覆盖）
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            # 设置通用默认请求头（new_context 创建时会通过 _apply_context_config 覆盖为实际值）
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                "headers": {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Upgrade-Insecure-Requests": "1",
                }
            })
        except Exception as e:
            logger.debug(f"CDP Network.setExtraHTTPHeaders 失败: {e}")

        # 注册到全局活跃列表，供 force_quit_all() 停止时强制关闭
        _register_driver(driver)

        return Browser(driver)


# ========== SyncPlaywright 入口 ==========
class SyncPlaywright:
    """模拟 sync_playwright() 返回的对象"""

    def __init__(self):
        self.chromium = Chromium()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Selenium没有全局资源需要清理，Browser会在close时quit driver
        pass

    def stop(self):
        pass


def sync_playwright() -> SyncPlaywright:
    """启动Selenium Playwright兼容层（替代Playwright的sync_playwright）"""
    return SyncPlaywright()


# ========== Stealth 兼容类 ==========
class Stealth:
    """兼容 playwright_stealth.Stealth（不实际使用，保留接口兼容）"""

    def use_sync(self, context):
        """Selenium下已通过CDP和启动参数实现反检测，此方法为空操作"""
        pass


# ========== 辅助：selenium-stealth 集成 ==========
# 已移除 apply_stealth() 死代码：该函数从未被调用，且其内置 WebGL 值(Intel Iris)
# 与主流程 add_init_script 注入的真实GPU字符串冲突。反检测统一由 BrowserContext
# 的 CDP 注入 + 启动参数(--disable-blink-features=AutomationControlled / excludeSwitches)实现。
