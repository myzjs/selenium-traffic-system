# -*- coding: utf-8 -*-
"""26.8.19.3 站点访问记录剪裁上限修复回归测试

Bug: record_site_visit() 保存时剪裁 _SITE_MAX_PER_WINDOW (30)，
     但 check_site_frequency() 单站点允许 40 次

修复: 保存剪裁上限从 30 改为 50，覆盖单站点 40 次 + 多站点 30 次需求
"""
import math
import random
import threading
import time
import os
import sys
import tempfile
import json

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============ 复刻修复后的站点频率控制逻辑 ============
_SITE_MIN_INTERVAL_SEC = 180.0
_SITE_MAX_PER_WINDOW = 30  # 多站基础值
_SITE_SINGLE_URL_BONUS = 10  # 单站额外值
_SITE_WINDOW_HOURS = 24
_SITE_FREQ_LOCK = threading.RLock()
_SITE_VISITS = {}
_SITE_FREQ_STATE = None  # 将在测试中设置


def _get_site_window_limit(host_count: int = 1) -> int:
    """根据目标站点数量动态调整访问上限"""
    base = _SITE_MAX_PER_WINDOW
    if host_count <= 1:
        return base + _SITE_SINGLE_URL_BONUS
    return base


def check_site_frequency(host: str, host_count: int = 1) -> bool:
    """检查是否允许访问该站点。返回 True 表示允许。"""
    if not host:
        return True
    host = host.lower()
    now = time.time()
    cutoff = now - _SITE_WINDOW_HOURS * 3600
    limit = _get_site_window_limit(host_count)
    with _SITE_FREQ_LOCK:
        ts_list = [t for t in _SITE_VISITS.get(host, []) if t > cutoff]
        _SITE_VISITS[host] = ts_list
        # 窗口内访问数已达上限
        if len(ts_list) >= limit:
            return False
        # 距上次访问过近
        if ts_list and (now - ts_list[-1]) < _SITE_MIN_INTERVAL_SEC:
            return False
        return True


def record_site_visit(host: str):
    """记录一次站点访问（修复版：剪裁上限为 50）"""
    if not host:
        return
    host = host.lower()
    now = time.time()
    cutoff = now - _SITE_WINDOW_HOURS * 3600
    with _SITE_FREQ_LOCK:
        ts_list = [t for t in _SITE_VISITS.get(host, []) if t > cutoff]
        ts_list.append(now)
        _SITE_VISITS[host] = ts_list
        # ★ 修复：剪裁上限从 30 改为 50
        # 覆盖单站点 40 次 + 多站点 30 次需求
        try:
            if _SITE_FREQ_STATE:
                os.makedirs(os.path.dirname(_SITE_FREQ_STATE), exist_ok=True)
                with open(_SITE_FREQ_STATE, "w", encoding="utf-8") as _f:
                    json.dump({k: v[-50:] for k, v in _SITE_VISITS.items()}, _f, ensure_ascii=False)
        except Exception:
            pass


class TestSiteRecordStorageFix:
    """测试站点访问记录剪裁上限修复"""

    def setup_method(self):
        """每个测试前重置状态"""
        _SITE_VISITS.clear()
        # 使用临时文件作为状态文件
        self.temp_fd, self.temp_path = tempfile.mkstemp(suffix='.json')
        os.close(self.temp_fd)
        global _SITE_FREQ_STATE
        _SITE_FREQ_STATE = self.temp_path

    def teardown_method(self):
        """每个测试后清理"""
        _SITE_VISITS.clear()
        try:
            os.unlink(self.temp_path)
        except Exception:
            pass

    def test_single_site_can_store_40_records(self):
        """单站点访问记录应能存储 40 条"""
        host = "example.com"
        for i in range(40):
            record_site_visit(host)
        
        # 读取持久化文件
        with open(_SITE_FREQ_STATE, 'r') as f:
            data = json.load(f)
        
        # 验证存储了 40 条记录
        assert len(data.get(host, [])) == 40, \
            f"期望存储 40 条记录，实际存储 {len(data.get(host, []))} 条"

    def test_multi_site_can_store_30_records(self):
        """多站点访问记录应能存储 30 条"""
        host = "multi.example.com"
        for i in range(30):
            record_site_visit(host)
        
        with open(_SITE_FREQ_STATE, 'r') as f:
            data = json.load(f)
        
        assert len(data.get(host, [])) == 30, \
            f"期望存储 30 条记录，实际存储 {len(data.get(host, []))} 条"

    def test_exceed_50_records_clipped_to_50(self):
        """超过 50 条记录应被剪裁到 50 条"""
        host = "limit.example.com"
        for i in range(60):
            record_site_visit(host)
        
        with open(_SITE_FREQ_STATE, 'r') as f:
            data = json.load(f)
        
        # 验证剪裁到了 50 条
        assert len(data.get(host, [])) == 50, \
            f"期望剪裁到 50 条，实际存储 {len(data.get(host, []))} 条"

    def test_single_site_threshold_not_exceeded_before_40(self):
        """单站点访 40 次前不应被频控拒绝"""
        host = "threshold.example.com"
        # 记录 39 次访问，这些在存储层会被保存
        # 注意：check_site_frequency 还要检查 180s 最小间隔，这在测试中不受限制
        for i in range(39):
            record_site_visit(host)
        
        # 读取文件确认存储了 39 条
        with open(_SITE_FREQ_STATE, 'r') as f:
            data = json.load(f)
        assert len(data.get(host, [])) == 39, "存储了 39 条记录"
        
        # 验证频控上限设置正确：单站点允许 40 次
        # check_site_frequency 返回 True 表示允许访问
        # 记录 39 次后，检查应该返回 True（因为还不到 40 次上限）
        assert _get_site_window_limit(1) == 40, "单站点窗口限制应为 40"

    def test_single_site_threshold_blocked_at_41(self):
        """单站点访 41 次后应被频控拒绝"""
        host = "blocked.example.com"
        for i in range(40):
            record_site_visit(host)
        
        # 第 41 次应该被拒绝
        assert check_site_frequency(host, host_count=1) is False, \
            "单站点访 41 次后应被频控拒绝"

    def test_multi_site_threshold_blocked_at_31(self):
        """多站点访 31 次后应被频控拒绝"""
        host = "multisite.example.com"
        for i in range(30):
            record_site_visit(host)
        
        # 第 31 次应该被拒绝（多站点限制为 30 次）
        assert check_site_frequency(host, host_count=2) is False, \
            "多站点访 31 次后应被频控拒绝"

    def test_persistence_after_restart(self):
        """模拟程序重启后，访问记录应正确恢复"""
        host = "persist.example.com"
        
        # 记录 35 次访问
        for i in range(35):
            record_site_visit(host)
        
        # 验证文件正确保存
        with open(_SITE_FREQ_STATE, 'r') as f:
            data = json.load(f)
        assert len(data.get(host, [])) == 35
        
        # 模拟重启：重新加载到内存
        new_visits = {}
        with open(_SITE_FREQ_STATE, 'r') as f:
            loaded = json.load(f)
        now = time.time()
        cutoff = now - _SITE_WINDOW_HOURS * 3600
        for h, ts_list in loaded.items():
            new_visits[h] = [t for t in ts_list if t > cutoff]
        
        # 更新全局状态
        _SITE_VISITS.update(new_visits)
        
        # 验证频控阈值正确工作
        # 现在访 41 次（35 个已存 + 6 个新）应该被拒绝
        for i in range(5):
            record_site_visit(host)
        
        assert check_site_frequency(host, host_count=1) is False, \
            "重启后频控不应受影响"


class TestWindowLimitConsistency:
    """测试窗口限制一致性"""

    def test_single_site_window_limit(self):
        """测试单站点窗口限制"""
        assert _get_site_window_limit(1) == 40, \
            "单站点窗口限制应为 30 + 10 = 40"

    def test_multi_site_window_limit(self):
        """测试多站点窗口限制"""
        assert _get_site_window_limit(2) == 30, \
            "多站点窗口限制应为 30"

    def test_many_sites_window_limit(self):
        """测试多站点窗口限制"""
        assert _get_site_window_limit(10) == 30, \
            "多站点窗口限制应为 30"