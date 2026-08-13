"""
26.8.13.6 回归测试：_finalize_ad_monitor 广告监控收尾结算（NameError 修复）

背景：worker 任务结束汇总前调用 _finalize_ad_monitor(ad_monitor) 但函数从未定义，
每次任务结束抛 NameError: name '_finalize_ad_monitor' is not defined，
导致广告曝光时长/有效曝光结算不完整（HilltopAds 收益链路受损）。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _finalize_ad_monitor, create_ad_monitor


def _mk_monitor(exposed50_since, prev_exposed50, durations=None, effective=None):
    m = create_ad_monitor()
    m["exposed50_since"] = dict(exposed50_since)
    m["prev_exposed50"] = set(prev_exposed50)
    if durations:
        m["exposure_duration_ms"] = dict(durations)
    if effective:
        m["effective_exposed"] = set(effective)
    return m


class TestFinalizeAdMonitor:
    """任务结束收尾结算逻辑"""

    def test_function_is_defined(self):
        """26.8.13.6 核心修复：函数必须存在（此前 NameError）"""
        assert callable(_finalize_ad_monitor)

    def test_empty_monitor_safe(self):
        """空 monitor 不抛异常"""
        out = _finalize_ad_monitor(None)
        assert out is None
        out = _finalize_ad_monitor({})
        assert out == {}

    def test_settles_last_exposure_segment(self):
        """仍在曝光态的广告位：最后一段 [起点, now] 补进累计时长"""
        now = time.time()
        m = _mk_monitor(exposed50_since={"ad1": now - 2.0}, prev_exposed50={"ad1"})
        out = _finalize_ad_monitor(m)
        # 2 秒曝光 → 约 2000ms
        assert out["exposure_duration_ms"]["ad1"] >= 1900
        assert "ad1" in out["effective_exposed"], "2000ms ≥ 1000ms 阈值应达标"

    def test_threshold_not_reached(self):
        """曝光不足 1000ms 不入 effective_exposed"""
        now = time.time()
        m = _mk_monitor(exposed50_since={"ad2": now - 0.3}, prev_exposed50={"ad2"})
        out = _finalize_ad_monitor(m)
        assert out["exposure_duration_ms"]["ad2"] <= 500
        assert "ad2" not in out["effective_exposed"]

    def test_accumulates_with_previous(self):
        """与既有累计时长累加，且跨阈值达标"""
        now = time.time()
        m = _mk_monitor(
            exposed50_since={"ad3": now - 1.5},
            prev_exposed50={"ad3"},
            durations={"ad3": 500},
        )
        out = _finalize_ad_monitor(m)
        assert out["exposure_duration_ms"]["ad3"] >= 1900  # 500 + 1500
        assert "ad3" in out["effective_exposed"]

    def test_clears_segment_state(self):
        """收尾后清理分段状态，防止重复结算"""
        now = time.time()
        m = _mk_monitor(exposed50_since={"ad4": now - 1.0}, prev_exposed50={"ad4"})
        out = _finalize_ad_monitor(m)
        assert out["exposed50_since"] == {}
        assert out["prev_exposed50"] == set()
        # 二次收尾不重复累加
        dur_before = out["exposure_duration_ms"].get("ad4", 0)
        _finalize_ad_monitor(out)
        assert out["exposure_duration_ms"].get("ad4", 0) == dur_before

    def test_no_active_exposure_no_change(self):
        """无在曝广告位：时长不变、不抛异常"""
        m = _mk_monitor(exposed50_since={}, prev_exposed50=set())
        out = _finalize_ad_monitor(m)
        assert out["exposure_duration_ms"] == {}
        assert out["effective_exposed"] == set()
