"""
回归测试：版本 26.8.15.1 Pop-under 弹窗"类人交互"升级（IVT 规避）
====================================================================
目标：让已触发的弹窗不再被 HilltopAds 判定为"程序化后台保活"而 IVT 过滤，
从而把"展示有计数"转化为可结算收益。

覆盖 4 类回归：
  1) 停留时长混合分布：三段（短 uniform / 主峰 triangular / 长尾 uniform）
     杀死"固定 22-36s"指纹，均值 36-39s 仍覆盖两次 heartbeat（~12s / ~22s）。
  2) 守护线程签名 + 5 位置参数向后兼容（新增第 6 参 popup_cdp 默认 None）。
  3) _popup_human_touch CDP 真实交互：滚动/移动/按键/点击 + 焦点切回 + 异常吞掉。
  4) 守护线程 e2e（mock 页面）：加载→保活→关闭全链路，CDP 会话被使用、弹窗被关闭、
     焦点切回主页面。
  5) DEFAULT_CONFIG 15/120/0.60 + 源码字面量 "stay_sec - elapsed" 仍在（自检测耦合）。
"""
from __future__ import annotations

import os
import sys
import random
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 路径处理：让测试能导入 popunder_trigger
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ===========================================================================
# Mock 构建
# ===========================================================================

def _make_cdp() -> MagicMock:
    """mock CDP 会话：send() 记录所有调用。"""
    m = MagicMock()
    m.calls: List[Any] = []
    m.send = lambda method, params=None: m.calls.append((method, params))
    return m


def _make_popup_page() -> MagicMock:
    """mock 弹窗 Page：focus 成功 + viewport 尺寸 + evaluate 不抛。"""
    p = MagicMock()
    p._window_handle = "pop_handle"
    p.viewport_size = {"width": 1280, "height": 720}
    p.driver.current_window_handle = "pop_handle"  # focus 后等于弹窗句柄
    p.evaluate.return_value = "div"  # elementFromPoint → 内容型标签（允许点击）
    p.wait_for_load_state.return_value = None
    p.goto.return_value = None
    p.close.return_value = None
    return p


def _make_main_page() -> MagicMock:
    """mock 主页面 Page：focus 成功，driver 共享弹窗页面的 driver。
    _focus_window 真正把 driver.current_window_handle 切到 main_handle。"""
    m = MagicMock()
    m._window_handle = "main_handle"

    def _do_focus():
        if m.driver is not None:
            m.driver.current_window_handle = "main_handle"

    m._focus_window = _do_focus
    return m


def _make_guardian_env():
    """构建守护线程 e2e 所需对象，返回 (popup_cdp, pop_page, main_page)。
    主页面与弹窗共享 driver：focus 到弹窗 → driver.current=pop_handle；
    restore 回主页面 → driver.current=main_handle。"""
    pop = _make_popup_page()
    main = _make_main_page()
    main.driver = pop.driver  # 共享同一 driver（CDP Input 绑定当前 target）
    cdp = _make_cdp()
    return cdp, pop, main


# ===========================================================================
# 1) 停留时长混合分布
# ===========================================================================
class TestStayDistribution:
    """_sample_popunder_stay 三段混合分布：边界/均值/长尾 + 固定种子可复现。"""

    def test_bounds_and_mean(self):
        from popunder_trigger import _sample_popunder_stay
        random.seed(20260815)
        samples = [_sample_popunder_stay(15, 120) for _ in range(4000)]
        assert min(samples) >= 15.0, "短段下界必须≥15s（R07 CRIT 线）"
        assert max(samples) <= 120.0, "长尾上界必须≤120s"
        mean = sum(samples) / len(samples)
        assert 25.0 <= mean <= 55.0, f"均值 {mean:.1f}s 偏离 36-39s 目标区间"

    def test_long_tail_present(self):
        from popunder_trigger import _sample_popunder_stay
        random.seed(20260815)
        samples = [_sample_popunder_stay(15, 120) for _ in range(4000)]
        over_60 = sum(1 for s in samples if s > 60.0)
        assert over_60 >= 20, "长尾段（>60s 读完全文用户）缺失，分布退化为固定短时长"

    def test_deterministic_seed(self):
        from popunder_trigger import _sample_popunder_stay
        random.seed(777)
        a = [_sample_popunder_stay(15, 120) for _ in range(50)]
        random.seed(777)
        b = [_sample_popunder_stay(15, 120) for _ in range(50)]
        assert a == b, "固定种子下分布必须可复现（便于回归断言）"

    def test_min_max_clamp(self):
        """min>max 或 min 越界时夹紧，不抛异常、不产生负值。"""
        from popunder_trigger import _sample_popunder_stay
        random.seed(1)
        for lo, hi in [(15, 120), (40, 30), (5, 120), (0, 15), (15, 15)]:
            for _ in range(50):
                v = _sample_popunder_stay(lo, hi)
                assert 0.0 < v <= max(max(lo, 15.0), hi) + 1e-6


# ===========================================================================
# 2) 守护线程签名 + 向后兼容
# ===========================================================================
class TestGuardianSignature:
    """_guard_stay_and_close 第 6 参 popup_cdp（默认 None），旧 5 位置调用仍可用。"""

    def test_signature_has_popup_cdp(self):
        import inspect
        from popunder_trigger import _guard_stay_and_close
        sig = inspect.signature(_guard_stay_and_close)
        params = list(sig.parameters.keys())
        assert "popup_cdp" in params, "守护线程缺少 popup_cdp 参数"
        assert sig.parameters["popup_cdp"].default is None, "popup_cdp 默认必须为 None（降级 JS）"
        # 参数顺序：popunder_page, main_page, stay_sec, stealth_inject_fn, heartbeat_records, popup_cdp
        assert params[0] == "popunder_page"
        assert params[1] == "main_page"
        assert params[2] == "stay_sec"

    def test_5_positional_backcompat(self):
        """旧 5 位置调用（无 popup_cdp）仍应完整跑通 → 降级 JS 路径，不抛异常。"""
        from popunder_trigger import _guard_stay_and_close
        cdp, pop, main = _make_guardian_env()
        # 不传第 6 参 → popup_cdp=None → JS 降级路径
        with patch("popunder_trigger.time.sleep", return_value=None):
            _guard_stay_and_close(pop, main, 1.0, lambda p: None, [])
        assert pop.wait_for_load_state.called, "加载等待必须发生"
        assert pop.close.called, "弹窗必须被关闭"
        assert pop.evaluate.called, "JS 降级路径应调用 evaluate"
        # cdp 未传 → CDP send 不应被调用
        assert len(cdp.calls) == 0, "popup_cdp=None 时不应发 CDP 事件"


# ===========================================================================
# 3) _popup_human_touch CDP 真实交互
# ===========================================================================
class _RNGSpy:
    """劫持 random.random：首次调用返回指定 r（决定动作分支），之后透传原实现。
    env 构建（MagicMock）会消耗随机数，靠"首次 r"精确控制分支比靠种子稳定。"""
    def __init__(self, first_r: float):
        self._orig = random.random
        self._first_r = first_r
        self._used = False
        random.random = self

    def __call__(self):
        if not self._used:
            self._used = True
            return self._first_r
        return self._orig()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        random.random = self._orig
        return False


class TestHumanTouch:
    """_popup_human_touch：滚动/移动/按键/点击 + 焦点切回 + 异常吞掉。
    用 _RNGSpy 精确控制首个 r（决定分支），比靠种子稳定。"""

    def test_scroll_action(self):
        from popunder_trigger import _popup_human_touch
        cdp, pop, main = _make_guardian_env()
        stats: Dict[str, int] = {}
        with _RNGSpy(0.20):  # r<0.45 → scroll
            action = _popup_human_touch(cdp, pop, stats, can_click=False, main_page=main)
        assert action == "scroll", f"r=0.20 应触发 scroll，实际 {action}"
        assert stats.get("scroll", 0) == 1
        methods = [c[0] for c in cdp.calls]
        assert "Input.dispatchMouseEvent" in methods, "滚动应发 CDP mouseWheel"
        assert main.driver.current_window_handle == "main_handle", "触摸后焦点必须切回主页面"

    def test_move_action(self):
        from popunder_trigger import _popup_human_touch
        cdp, pop, main = _make_guardian_env()
        stats: Dict[str, int] = {}
        with _RNGSpy(0.55):  # 0.45≤r<0.70 → move
            action = _popup_human_touch(cdp, pop, stats, can_click=False, main_page=main)
        assert action == "move", f"r=0.55 应触发 move，实际 {action}"
        assert stats.get("move", 0) == 1

    def test_key_action(self):
        from popunder_trigger import _popup_human_touch
        cdp, pop, main = _make_guardian_env()
        stats: Dict[str, int] = {}
        with _RNGSpy(0.78):  # 0.70≤r<0.85 → key
            action = _popup_human_touch(cdp, pop, stats, can_click=False, main_page=main)
        assert action == "key", f"r=0.78 应触发 key，实际 {action}"
        assert stats.get("key", 0) == 1
        methods = [c[0] for c in cdp.calls]
        assert "Input.dispatchKeyEvent" in methods, "按键应发 CDP keyDown/keyUp"

    def test_all_actions_covered(self):
        """多轮采样覆盖 4 类动作（scroll/move/key/click），计数器与焦点一致。"""
        from popunder_trigger import _popup_human_touch
        seen = set()
        for r in (0.1, 0.3, 0.5, 0.6, 0.75, 0.8, 0.9, 0.95):
            cdp, pop, main = _make_guardian_env()
            pop.evaluate.return_value = "div"
            stats: Dict[str, int] = {}
            with _RNGSpy(r):
                action = _popup_human_touch(cdp, pop, stats, can_click=True, main_page=main)
            seen.add(action)
            assert action in ("scroll", "move", "key", "click", "scroll-fallback", "")
            assert main.driver.current_window_handle == "main_handle"
            if action == "scroll":
                assert stats.get("scroll", 0) == 1
            elif action in ("move", "key", "click"):
                assert stats.get(action, 0) == 1
        assert {"scroll", "move", "key", "click"} <= seen, f"8 轮应覆盖 4 类动作，实际 {seen}"

    def test_click_action_whitelist(self):
        from popunder_trigger import _popup_human_touch
        cdp, pop, main = _make_guardian_env()
        pop.evaluate.return_value = "div"  # 白名单内容型标签
        stats: Dict[str, int] = {}
        with _RNGSpy(0.92):  # r≥0.85 → click 分支
            action = _popup_human_touch(cdp, pop, stats, can_click=True, main_page=main)
        assert action == "click", f"r=0.92+div 应触发点击，实际 {action}"
        assert stats.get("click", 0) == 1
        methods = [c[0] for c in cdp.calls]
        assert "Input.dispatchMouseEvent" in methods

    def test_click_degrades_when_no_click(self):
        """can_click=False 时，点击分支降级为轻滚动（scroll-fallback）。"""
        from popunder_trigger import _popup_human_touch
        cdp, pop, main = _make_guardian_env()
        stats: Dict[str, int] = {}
        with _RNGSpy(0.92):
            action = _popup_human_touch(cdp, pop, stats, can_click=False, main_page=main)
        assert action == "scroll-fallback", f"can_click=False 应降级滚动，实际 {action}"
        assert stats.get("scroll", 0) == 1

    def test_click_blocked_by_tag_blacklist(self):
        """elementFromPoint 命中 a/button/input → 不在白名单 → 降级滚动（不点链接）。"""
        from popunder_trigger import _popup_human_touch
        for tag in ("a", "button", "input", "iframe"):
            cdp, pop, main = _make_guardian_env()
            pop.evaluate.return_value = tag  # 非内容型 → 降级
            stats: Dict[str, int] = {}
            with _RNGSpy(0.92):
                action = _popup_human_touch(cdp, pop, stats, can_click=True, main_page=main)
            assert action == "scroll-fallback", f"tag={tag} 应降级滚动，实际 {action}"
            assert stats.get("click", 0) == 0, f"tag={tag} 不应计入点击"

    def test_focus_switch_failure_returns_empty(self):
        """焦点切换失败（窗口已关）→ 返回 '' 且不发 CDP 事件（降级 JS）。"""
        from popunder_trigger import _popup_human_touch
        cdp = _make_cdp()
        pop = _make_popup_page()
        main = _make_main_page()
        main.driver = pop.driver
        pop._window_handle = "pop_dead"
        pop.driver.current_window_handle = "other"
        pop._focus_window = MagicMock(return_value=None)  # 切不过去
        with _RNGSpy(0.20):
            action = _popup_human_touch(cdp, pop, {}, can_click=True, main_page=main)
        assert action == "", "焦点切不过去时应返回空串降级"
        assert len(cdp.calls) == 0, "焦点失败时不应发 CDP 事件"

    def test_exception_swallowed(self):
        """evaluate 抛异常（elementFromPoint 失败）→ 不向外抛，返回动作名或空串。"""
        from popunder_trigger import _popup_human_touch
        cdp = _make_cdp()
        pop = _make_popup_page()
        main = _make_main_page()
        main.driver = pop.driver
        pop.evaluate = MagicMock(side_effect=RuntimeError("boom"))
        stats: Dict[str, int] = {}
        with _RNGSpy(0.92):  # 点击分支：evaluate 抛 → tag='' → 降级滚动
            action = _popup_human_touch(cdp, pop, stats, can_click=True, main_page=main)
        assert action in ("scroll-fallback", ""), f"异常应被吞掉，实际 {action!r}"
        assert main.driver.current_window_handle == "main_handle", "异常后焦点仍应切回"


# ===========================================================================
# 4) 守护线程 e2e（mock 页面，加速 sleep）
# ===========================================================================
class TestGuardianE2E:
    """守护线程全链路：加载→保活→关闭，CDP 会话被使用、弹窗被关闭、焦点切回。"""

    def test_guardian_cdp_path_closes_popup(self):
        from popunder_trigger import _guard_stay_and_close
        cdp, pop, main = _make_guardian_env()
        with patch("popunder_trigger.time.sleep", return_value=None):
            _guard_stay_and_close(pop, main, 1.0, lambda p: None, [], cdp)
        # CDP 会话被使用（阶段2c / 阶段3 触摸）
        assert len(cdp.calls) > 0, "传了 popup_cdp 却没发任何 CDP 事件"
        methods = {c[0] for c in cdp.calls}
        assert "Input.dispatchMouseEvent" in methods, "应有鼠标类 CDP 事件"
        # 弹窗被关闭
        assert pop.close.called, "弹窗必须被关闭"
        assert pop.goto.called, "关闭前应 about:blank 卸载"
        # 焦点最终切回主页面
        assert main.driver.current_window_handle == "main_handle", "守护结束后焦点应回到主页面"

    def test_guardian_js_fallback_path(self):
        """popup_cdp=None → 纯 JS 路径，evaluate 被调用，弹窗被关闭。"""
        from popunder_trigger import _guard_stay_and_close
        cdp, pop, main = _make_guardian_env()
        with patch("popunder_trigger.time.sleep", return_value=None):
            _guard_stay_and_close(pop, main, 1.0, lambda p: None, [])
        assert pop.evaluate.called, "JS 降级路径应调用 evaluate"
        assert pop.close.called, "弹窗必须被关闭"
        assert len(cdp.calls) == 0, "无 CDP 会话时不应发 CDP 事件"

    def test_guardian_heartbeat_analysis(self):
        """heartbeat_records 非空时触发分析日志（不抛异常、正常关闭）。"""
        from popunder_trigger import _guard_stay_and_close
        cdp, pop, main = _make_guardian_env()
        hb = [
            {"t": time.time() + 12.0, "url": "https://curoax.example/hb", "method": "GET", "type": "fetch"},
            {"t": time.time() + 22.0, "url": "https://curoax.example/hb2", "method": "GET", "type": "fetch"},
        ]
        with patch("popunder_trigger.time.sleep", return_value=None):
            _guard_stay_and_close(pop, main, 1.0, lambda p: None, hb, cdp)
        assert pop.close.called, "带 heartbeat 分析也应正常关闭"


# ===========================================================================
# 5) DEFAULT_CONFIG + 源码字面量
# ===========================================================================
class TestConfigAndSource:
    """DEFAULT_CONFIG 15/120/0.60 + self_test 耦合的字面量仍在。"""

    def test_default_config_values(self):
        from popunder_trigger import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["popunder_stay_min"] >= 15, "stay_min 必须≥15s（R07 CRIT 线）"
        assert DEFAULT_CONFIG["popunder_stay_max"] >= 120, "stay_max 必须≥120s（长尾加宽）"
        assert DEFAULT_CONFIG["trigger_probability"] >= 0.6, "触发概率默认≥0.6"

    def test_source_literal_stay_elapsed(self):
        """self_test() 靠 open(__file__).read() 找 'stay_sec - elapsed'，改动须同步。"""
        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()
        assert "stay_sec - elapsed" in src, "源码字面量缺失 → self_test guardian_subtracts_elapsed 回归"

    def test_source_literal_close_jitter(self):
        """关闭过渡抖动加宽字面量（self_test close_jitter_widened 耦合）。"""
        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()
        assert "random.uniform(0.6, 2.4)" in src, "关闭过渡抖动字面量缺失"

    def test_self_test_all_pass(self):
        """模块自检全绿（含 4 项新增：分布/签名/触摸辅助/关闭抖动）。"""
        from popunder_trigger import self_test
        results = self_test()
        fails = {k: v for k, v in results.items() if not v}
        assert not fails, f"self_test 有失败项: {fails}"
        # 新增 4 项必须存在
        for key in ("stay_distribution_nonuniform", "popup_cdp_param",
                    "human_touch_helper_exists", "close_jitter_widened"):
            assert key in results, f"self_test 缺少新增项 {key}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
