"""
pytest 公共 fixtures 和配置
"""
import json
import os
import pytest

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def sample_config():
    """加载真实 config.json 供测试使用"""
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@pytest.fixture
def mock_page(mocker):
    """创建一个模拟的 Playwright page 对象"""
    page = mocker.MagicMock()
    page.evaluate.return_value = None
    page.goto.return_value = None
    page.viewport_size = {"width": 1920, "height": 1080}
    page.mouse = mocker.MagicMock()
    page.request = mocker.MagicMock()
    page.request.headers = {}
    return page


@pytest.fixture
def mock_response():
    """创建一个模拟的 requests.Response 对象"""
    response = mocker.MagicMock()
    response.status_code = 200
    response.json.return_value = {"success": True, "data": {}}
    response.text = '{"success": true}'
    return response
