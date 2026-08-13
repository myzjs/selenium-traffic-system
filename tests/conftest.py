"""
pytest 公共 fixtures 和配置
"""
import json
import os
import shutil
from unittest.mock import MagicMock

import pytest

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate_ip_dedup_state():
    """每个测试前隔离 IP 去重池与代理缓存（测试隔离，不改业务逻辑）

    背景：ip_provider 模块加载时从 .risk_state/ip_dedup_state.json 恢复 24h
    去重状态，且 acquire_ip_use 会立即持久化。若上一轮测试写入的 mock IP
    （如 99.88.77.66）残留，会触发 "已在去重间隔内使用过 → 重试 → mock 耗尽"
    导致契约测试偶发失败。本 fixture 在测试前备份磁盘状态并清空内存状态，
    测试后恢复磁盘原状，保证测试间互不污染、也不污染真实运行状态。
    """
    import ip_provider

    state_file = getattr(ip_provider, "_STATE_FILE", None)
    backup_file = None
    if state_file and os.path.exists(state_file):
        backup_file = state_file + ".testbak"
        try:
            shutil.copy2(state_file, backup_file)
        except OSError:
            backup_file = None
    with ip_provider._used_ips_lock:
        ip_provider._used_ips.clear()
    with ip_provider._proxy_cache_lock:
        ip_provider._proxy_cache.clear()
    yield
    # 恢复磁盘持久化状态，丢弃测试期间写入的 mock IP
    if state_file:
        try:
            if backup_file and os.path.exists(backup_file):
                shutil.copy2(backup_file, state_file)
                os.remove(backup_file)
            elif os.path.exists(state_file):
                os.remove(state_file)
        except OSError:
            pass


@pytest.fixture
def sample_config():
    """加载真实 config.json 供测试使用"""
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@pytest.fixture
def mock_page():
    """创建一个模拟的 Playwright page 对象（无需 pytest-mock）"""
    page = MagicMock()
    page.evaluate.return_value = None
    page.goto.return_value = None
    page.viewport_size = {"width": 1920, "height": 1080}
    page.mouse = MagicMock()
    page.request = MagicMock()
    page.request.headers = {}
    return page


@pytest.fixture
def mock_response():
    """创建一个模拟的 requests.Response 对象（无需 pytest-mock）"""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"success": True, "data": {}}
    response.text = '{"success": true}'
    return response
