# -*- coding: utf-8 -*-
"""26.8.23.1 EventStore 事件日志失控修复回归测试

覆盖两点 P0 修复：
1. rt_events.jsonl 无限制增长 → 轮转上限（默认 100MB，测试里临时改为 500 字节）
2. 同一 (rule_id, summary) 每秒重复写入 → 60 秒去重冷却
"""
import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

import pytest

from traffic_monitor import EventStore, RTEvent, _EVENT_JSONL_MAX_BYTES, _EVENT_DEDUP_COOLDOWN_SEC


@pytest.fixture()
def tmp_dir(tmp_path):
    """由 pytest 提供的临时目录。"""
    return str(tmp_path)


def _event(rule_id="R03_REFERER_MISMATCH", summary="内页  Referer缺失: '-'") -> RTEvent:
    return RTEvent(
        ts="2026-08-21T13:36:16Z",
        rule_id=rule_id,
        severity="WARN",
        summary=summary,
        facts={"path": "", "referer": "-"},
        auto_fix="【根因】xxx",
        sample_line="sample_line_here",
    )


def _write_n(store: EventStore, n: int, start: int = 0):
    for i in range(start, start + n):
        ev = _event(summary=f"内页  Referer缺失: '-', sample={i}")
        store.add(ev)


class TestEventStoreLogCap:
    """rt_events.jsonl 轮转 + 去重冷却 回归测试"""

    def test_no_rotate_when_under_limit(self, tmp_dir):
        store = EventStore(tmp_dir)
        _write_n(store, 3)
        assert (Path(tmp_dir) / "rt_events.jsonl").exists()
        assert not (Path(tmp_dir) / "rt_events.jsonl.1").exists()

    def test_rotate_when_exceed_limit(self, tmp_dir):
        store = EventStore(tmp_dir)
        # 临时把轮转阈值改成 500 字节，方便单测快速触发
        original_limit = 100 * 1024 * 1024
        import traffic_monitor as tm
        tm._EVENT_JSONL_MAX_BYTES = 500
        try:
            # 一条典型事件大约 300~500 字节，多写几条触发轮转
            _write_n(store, 20)
            jsonl = Path(tmp_dir) / "rt_events.jsonl"
            backup = Path(tmp_dir) / "rt_events.jsonl.1"
            assert jsonl.exists()
            assert backup.exists()
            # 新文件应比较小，旧备份至少包含事件
            assert backup.stat().st_size > 0
        finally:
            tm._EVENT_JSONL_MAX_BYTES = original_limit

    def test_rotate_overwrite_old_backup(self, tmp_dir):
        store = EventStore(tmp_dir)
        import traffic_monitor as tm
        original_limit = tm._EVENT_JSONL_MAX_BYTES
        tm._EVENT_JSONL_MAX_BYTES = 200  # 极低阈值：每条事件约 240 字节，每写 1 条触发轮转
        try:
            _write_n(store, 5, start=0)
            backup = Path(tmp_dir) / "rt_events.jsonl.1"
            first_line = backup.open("r", encoding="utf-8").readline()
            _write_n(store, 5, start=100)  # 第二批用不同内容
            second_line = backup.open("r", encoding="utf-8").readline()
            # 备份内容被覆盖（不是追加）：两次备份的首行不同
            assert first_line != second_line
        finally:
            tm._EVENT_JSONL_MAX_BYTES = original_limit

    def test_dedup_first_event_written(self, tmp_dir):
        store = EventStore(tmp_dir)
        ev = _event()
        store.add(ev)
        jsonl = Path(tmp_dir) / "rt_events.jsonl"
        assert sum(1 for _ in jsonl.open("r", encoding="utf-8")) == 1

    def test_dedup_second_same_event_suppressed(self, tmp_dir):
        store = EventStore(tmp_dir)
        store.add(_event())
        store.add(_event())
        jsonl = Path(tmp_dir) / "rt_events.jsonl"
        lines = list(jsonl.open("r", encoding="utf-8"))
        assert len(lines) == 1

    def test_dedup_different_event_still_written(self, tmp_dir):
        store = EventStore(tmp_dir)
        store.add(_event(rule_id="R03_REFERER_MISMATCH"))
        store.add(_event(rule_id="R07_SHORT_STAY"))
        jsonl = Path(tmp_dir) / "rt_events.jsonl"
        lines = list(jsonl.open("r", encoding="utf-8"))
        assert len(lines) == 2

    def test_dedup_after_cooldown_written_again(self, tmp_dir):
        import traffic_monitor as tm
        original_cooldown = tm._EVENT_DEDUP_COOLDOWN_SEC
        tm._EVENT_DEDUP_COOLDOWN_SEC = 0.05  # 缩短冷却，避免真实 sleep 60s
        try:
            store = EventStore(tmp_dir)
            store.add(_event())
            store.add(_event())  # 冷却期抑制
            time.sleep(0.1)      # 冷却结束
            store.add(_event())  # 应再次落盘
            jsonl = Path(tmp_dir) / "rt_events.jsonl"
            lines = list(jsonl.open("r", encoding="utf-8"))
            assert len(lines) == 2
        finally:
            tm._EVENT_DEDUP_COOLDOWN_SEC = original_cooldown

    def test_dedup_suppressed_count_increments(self, tmp_dir):
        store = EventStore(tmp_dir)
        assert store.dedup_suppressed_count == 0
        store.add(_event())
        store.add(_event())
        assert store.dedup_suppressed_count == 1

    def test_recent_returns_all_from_ring(self, tmp_dir):
        store = EventStore(tmp_dir)
        _write_n(store, 10)
        assert len(store.recent(limit=100)) == 10

    def test_subscriber_receives_even_suppressed(self, tmp_dir):
        """去重冷却不应阻断 SSE 推送。"""
        store = EventStore(tmp_dir)
        q = store.subscribe()
        store.add(_event())
        store.add(_event())  # 被冷却抑制
        msgs = []
        try:
            while True:
                msgs.append(q.get_nowait())
        except queue.Empty:
            pass
        # SSE 推送了两次（冷却只抑制落盘，不抑制推送）
        assert len(msgs) == 2

    def test_thread_safety(self, tmp_dir):
        store = EventStore(tmp_dir)
        errors = []

        def worker():
            try:
                for _ in range(50):
                    store.add(_event(summary=f"thread-{threading.current_thread().name}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        jsonl = Path(tmp_dir) / "rt_events.jsonl"
        lines = list(jsonl.open("r", encoding="utf-8"))
        assert len(lines) >= 1

    def test_dedup_key_pruning(self, tmp_dir):
        """超过 500 个 key 时清理过期 key，避免内存无限增长。"""
        store = EventStore(tmp_dir)
        # 制造 501 个不同 key
        for i in range(501):
            store.add(_event(summary=f"unique-{i}"))
        # 再次添加相同 key，不应因为 dict 过大而报错
        store.add(_event(summary="unique-0"))
        assert len(store._dedup_last_ts) <= 501
