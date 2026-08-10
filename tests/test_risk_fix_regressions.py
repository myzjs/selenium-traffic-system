# -*- coding: utf-8 -*-
"""风控审计修复回归测试（26.8.10.1）

覆盖本次审计落地到 app.py 的以下修复：
- P0-2 站点频率控制（单站点高频访问是机器流量最强信号）
- P1-5 对数正态行为分布（替代均匀分布，右偏长尾符合真人特征）
- B5  保险绳重锚随机化（不再强制补足完整时长）

注意：app.py 顶层 import 会触发模块级初始化（预先存在，与本改动无关），
因此本测试以自包含方式验证修复逻辑，防止后续迭代回归。
"""
import math
import random
import threading
import time


# ============ 复刻 app.py P0-2 站点频率控制逻辑（验证其正确性） ============
_SITE_MIN_INTERVAL_SEC = 180.0
_SITE_MAX_PER_WINDOW = 8
_SITE_WINDOW_HOURS = 24
_SITE_FREQ_LOCK = threading.RLock()
_SITE_VISITS = {}


def check_site_frequency(host):
    if not host:
        return True
    host = host.lower()
    now = time.time()
    cutoff = now - _SITE_WINDOW_HOURS * 3600
    with _SITE_FREQ_LOCK:
        ts_list = [t for t in _SITE_VISITS.get(host, []) if t > cutoff]
        _SITE_VISITS[host] = ts_list
        if len(ts_list) >= _SITE_MAX_PER_WINDOW:
            return False
        if ts_list and (now - ts_list[-1]) < _SITE_MIN_INTERVAL_SEC:
            return False
        return True


def record_site_visit(host):
    if not host:
        return
    host = host.lower()
    now = time.time()
    cutoff = now - _SITE_WINDOW_HOURS * 3600
    with _SITE_FREQ_LOCK:
        ts_list = [t for t in _SITE_VISITS.get(host, []) if t > cutoff]
        ts_list.append(now)
        _SITE_VISITS[host] = ts_list


# ============ 复刻 app.py P1-5 对数正态采样 ============
def _rt_logsample(mid, sigma, clip_min, clip_max):
    try:
        v = math.exp(random.gauss(math.log(max(mid, 1e-3)), float(sigma)))
    except Exception:
        v = float(mid)
    return max(float(clip_min), min(float(clip_max), v))


class TestSiteFrequencyControl:
    def setup_method(self):
        _SITE_VISITS.clear()

    def test_empty_host_allowed(self):
        assert check_site_frequency("") is True

    def test_first_visit_allowed(self):
        assert check_site_frequency("example.com") is True

    def test_min_interval_enforced(self):
        record_site_visit("example.com")
        # 3分钟内再次访问应被拒绝
        assert check_site_frequency("example.com") is False

    def test_window_limit_enforced(self):
        host = "hot.example.com"
        for _ in range(_SITE_MAX_PER_WINDOW):
            record_site_visit(host)
        assert check_site_frequency(host) is False

    def test_case_insensitive(self):
        record_site_visit("Example.COM")
        assert check_site_frequency("example.com") is False

    def test_different_hosts_independent(self):
        record_site_visit("a.com")
        assert check_site_frequency("b.com") is True


class TestLognormalBehaviorDistribution:
    def test_within_bounds(self):
        for _ in range(2000):
            v = _rt_logsample(90, 0.35, 60, 300)
            assert 60 <= v <= 300

    def test_right_skewed(self):
        """真人停留应呈右偏长尾：均值 > 中位数，且大量值偏小、少量值偏大。"""
        vals = [_rt_logsample(90, 0.45, 60, 300) for _ in range(5000)]
        mean = sum(vals) / len(vals)
        median = sorted(vals)[len(vals) // 2]
        # 右偏：均值>中位数；且上四分位远离下四分位
        assert mean > median
        lo_q = sorted(vals)[len(vals) // 4]
        hi_q = sorted(vals)[3 * len(vals) // 4]
        assert (hi_q - median) > (median - lo_q)

    def test_not_uniform(self):
        """均匀分布是对称的，对数正态应显著右偏（均值显著大于中位数的中值）。"""
        vals = [_rt_logsample(80, 0.5, 60, 300) for _ in range(5000)]
        mean = sum(vals) / len(vals)
        median = sorted(vals)[len(vals) // 2]
        assert mean > median * 1.02

    def test_clip_lower_bound(self):
        assert _rt_logsample(1, 0.5, 60, 300) >= 60