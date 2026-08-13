# 📋 Trae 调度指令 - Selenium 流量生成系统

> 项目: https://github.com/myzjs/selenium-traffic-system
> 路径: /Users/mac/Documents/www-jb/626/selenium_traffic_system

---

## 一、你的任务

为现有的 **Selenium 流量生成系统** 编写 **Mock 化的单元测试**，让测试可以在没有真实浏览器和代理的情况下运行。

---

## 二、项目结构

```
selenium_traffic_system/
├── app.py                  (14.5K)  Flask Web 主入口 (不要动)
├── selenium_bridge.py      (1.7K)   Selenium/Playwright 桥接层
├── ip_provider.py          (526行)  IP代理获取模块
├── risk_check.py           (576行)  风控检测模块
├── seo_query_module.py     (595行)  SEO查询配置模块
├── ip_info_resolver.py     (253行)  IP信息解析
├── ip_region_module.py     (316行)  IP区域模块
├── test_full_workflow.py   (397行)  集成测试脚本 (不要动)
├── tests/                  ← 你在这里写测试
│   ├── __init__.py
│   └── conftest.py         (已有公共 fixtures)
├── pytest.ini              (已有配置)
├── requirements.txt
└── config.json
```

---

## 三、需要写的测试文件

| # | 源文件 | 测试文件 | 说明 |
|---|--------|----------|------|
| 1 | `ip_provider.py` | `tests/test_ip_provider.py` | IP代理获取，mock requests |
| 2 | `risk_check.py` | `tests/test_risk_check.py` | 风控检测，mock page.evaluate |
| 3 | `seo_query_module.py` | `tests/test_seo_query.py` | SEO配置校验 |
| 4 | `ip_info_resolver.py` | `tests/test_ip_info_resolver.py` | IP信息解析 |
| 5 | `ip_region_module.py` | `tests/test_ip_region.py` | IP区域模块 |
| 6 | `selenium_bridge.py` | `tests/test_selenium_bridge.py` | 桥接层基础功能 |

---

## 四、测试规范

### 4.1 使用 pytest + unittest.mock

```python
# tests/test_example.py
from unittest.mock import patch, MagicMock
import pytest


def test_normal_case():
    """正常流程测试"""
    result = my_function()
    assert result["success"] is True


@patch("ip_provider.requests.get")
def test_with_mock(mock_get):
    """Mock 外部依赖"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}
    mock_get.return_value = mock_response
    
    result = my_function()
    assert result["success"] is True


def test_error_case():
    """异常情况测试"""
    result = my_function_bad_input()
    assert result["success"] is False
    assert "error" in result
```

### 4.2 每个模块至少覆盖

- ✅ **正常流程** — 输入合法，输出正确
- ✅ **边界情况** — 空列表、None、0值、空字符串
- ✅ **异常处理** — 网络错误、超时、非法输入
- ✅ **Mock 外部依赖** — requests、浏览器、文件系统

### 4.3 已有的公共 Fixtures

```python
# tests/conftest.py 已提供:
- sample_config  → 加载真实 config.json
- mock_page      → 模拟 Playwright page 对象
- mock_response  → 模拟 requests.Response
```

---

## 五、运行测试

```bash
# 运行所有测试
cd /Users/mac/Documents/www-jb/626/selenium_traffic_system
python3 -m pytest tests/ -v

# 运行单个测试文件
python3 -m pytest tests/test_ip_provider.py -v

# 查看覆盖率
pip3 install pytest-cov
python3 -m pytest tests/ --cov=. --cov-report=term
```

---

## 六、注意事项

1. ⚠️ **不要修改** `app.py`、`test_full_workflow.py`
2. ⚠️ **不要修改** `pytest.ini`、`conftest.py`
3. ✅ 测试必须能 **离线运行**（不依赖真实网络/浏览器）
4. ✅ 使用 `unittest.mock` 模拟所有外部调用
5. ✅ 测试文件放在 `tests/` 目录下
6. ✅ 函数名以 `test_` 开头

---

## 七、完成后验证

```bash
cd /Users/mac/Documents/www-jb/626/selenium_traffic_system
python3 -m pytest tests/ -v --tb=short
```

期望输出: 所有测试通过，无 FAILED
