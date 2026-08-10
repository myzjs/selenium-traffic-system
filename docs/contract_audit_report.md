# 广告联盟流量风控系统 · 数据契约审计报告

> 日期：2026-08-10
> 范围：5 个核心模块 × 34 项静态契约测试 + 运行时探针
> 代码状态：已验证（`python3 -m pytest tests/test_contract.py -q` 全部通过）
> 契约唯一事实来源：本文档 + `tests/test_contract.py` 中的 CONTRACT 常量

---

## 一、接口字段契约清单

以下表格与 `tests/test_contract.py` 中的 `*_CONTRACT` 常量一一对应，是系统对外稳定接口的唯一事实来源（Single Source of Truth）。

### 1.1 ip_provider 模块

| 函数/常量 | 参数 | 返回类型 | 预期语义 | 审计结论 |
|-----------|------|---------|---------|---------|
| `is_high_risk_ip(ip_type)` | `ip_type: str` | `bool` | 机房/代理/VPN/托管/商业 = True；住宅/移动/未知/空 = False | ✅ 已验证 |
| `check_ip_used_recently(ip)` | `ip: str` | `bool` | IP 是否在去重窗口内被使用过 | ✅ 已验证 |
| `record_ip_use(ip)` | `ip: str` | `None` | 记录 IP 使用时间戳，用于去重 | ✅ 已验证 |
| `get_used_ips_count()` | 无 | `int` | 当前去重池内 IP 数量 | ✅ 已验证 |
| `_load_dedup_state()` | 无 | `None` | 从磁盘加载去重状态 | ✅ 已验证 |
| `_save_dedup_state(force)` | `force: bool` | `None` | 持久化去重状态到磁盘 | ✅ 已验证 |
| `HIGH_RISK_TYPES` 常量 | — | `set` | `{datacenter, proxy, vpn, hosting, business}` | ✅ 已验证 |
| `SAFE_TYPES` 常量 | — | `set` | `{isp, residential, mobile, unknown, ""}` | ✅ 已验证 |

### 1.2 risk_control_enhancements 模块

| 函数/类 | 参数 | 返回类型 | 预期语义 | 审计结论 |
|---------|------|---------|---------|---------|
| `get_stable_canvas_seed(fp_id)` | `fp_id: str` | `int` | 同 fp_id 返回一致正整数种子（31-bit）；不同 fp_id 大概率不同 | ✅ 已验证 |
| `get_stable_canvas_seed` 在 `__all__` 中 | — | — | 支持 `import *` 导入 | ✅ 已验证 |
| `dns_diversity.pick_resolver(country)` | `country: str` | `List[str]` (len=3) | 返回 3 个非空 DNS 解析器地址 | ✅ 已验证 |
| `_IsolatePool.allow(adv_id, ip, fingerprint, ua, asn, persist)` | 见左 | `Tuple[bool, str]` | 同广告账户隔离判定：(是否允许, 原因) | ✅ 已验证 |
| `_AdvIsolation.can_acquire(adv_id, device_id, ip, ua, persist)` | 见左 | `Tuple[bool, str]` | 多账户×设备×IP 隔离判定：(是否允许, 原因) | ✅ 已验证 |

### 1.3 seo_query_module 模块

| 函数/常量 | 参数 | 返回类型 | 预期语义 | 审计结论 |
|-----------|------|---------|---------|---------|
| `get_local_search_engine_url(country_code)` | `country_code: str` | `str` | 返回地域化搜索引擎 URL | ✅ 已验证 |
| `generate_referer_for_region(country_code, language, keyword)` | 见左 | `str \| None` | 生成地域化 Referer URL | ✅ 已验证 |
| `generate_referer(engine_id, keyword)` | 见左 | `str \| None` | 向后兼容：按引擎 ID 生成 Referer | ✅ 已验证 |
| `REGION_SEARCH_ENGINE_MAP["JP"]` | — | `str` | 含 `co.jp` | ✅ 已验证 |
| `REGION_SEARCH_ENGINE_MAP["DE"]` | — | `str` | 含 `google.de` | ✅ 已验证 |
| `REGION_SEARCH_ENGINE_MAP["CN"]` | — | `str` | 含 `baidu` | ✅ 已验证 |

### 1.4 ip_info_resolver 模块

| 函数 | 参数 | 返回字段 | 预期语义 | 审计结论 |
|------|------|---------|---------|---------|
| `resolve_ip_info(ip, proxy_ip_info, proxy_url)` | 见左 | `success: bool` | 是否成功解析 | ✅ 已验证 |
| | | `ip: str` | 被查询的 IP | ✅ 已验证 |
| | | `country_code: str` | 国家代码（如 JP/DE/CN） | ✅ 已验证 |
| | | `country_name: str` | 国家名称 | ✅ 已验证 |
| | | `timezone: str` | 时区（如 Asia/Tokyo） | ✅ 已验证 |
| | | `language: str` | 语言（如 ja-JP） | ✅ 已验证 |

### 1.5 popunder_trigger 模块

| 导出函数 | 存在性 | 类型 | 预期语义 | 审计结论 |
|---------|--------|------|---------|---------|
| `trigger_popunder` | ✅ | callable | 触发弹窗主入口 | ✅ 已验证 |
| `is_ip_safe_for_hilltopads` | ✅ | callable | HilltopAds IP 安全判定（纯函数，不启动浏览器） | ✅ 已验证 |
| `should_trigger_for_network` | ✅ | callable | 按广告网络判定是否触发（纯函数） | ✅ 已验证 |
| `_pick_safe_coordinates` | ✅ | callable | 安全点击坐标选择 | ✅ 已验证 |
| `self_test` | ✅ | callable | 模块自检 | ✅ 已验证 |
| `_inject_popunder_stealth` | ✅ | callable | 弹窗隐身脚本注入 | ✅ 已验证 |

---

## 二、契约探针接口

### 2.1 接口信息

| 项 | 值 |
|----|----|
| 路径 | `/api/debug/contract_probe` |
| 方法 | GET |
| 开关 | 环境变量 `ENABLE_CONTRACT_PROBE=true`（默认关闭，返回 404） |
| 位置 | `app.py`，`/api/status` 之后 |

### 2.2 返回结构

```json
{
  "success": true,
  "summary": {
    "模块总数": 5,
    "正常模块数": 5,
    "异常模块数": 0,
    "探针版本": "1.0",
    "生成时间": "2026-08-10 12:00:00"
  },
  "probe": {
    "ip_provider": {
      "预期函数": [...],
      "实际函数详情": { "func_name": { "params": [...], "return_hint": "...", "params_match": true } },
      "缺失函数": [],
      "high_risk_types_预期": [...],
      "high_risk_types_实际": [...]
    },
    "risk_control_enhancements": {
      "get_stable_canvas_seed": { "存在": true, "params": [...], "示例返回类型": "int", "在__all__中": true },
      "dns_diversity.pick_resolver": { "存在": true, "返回长度": 3, "元素非空": true },
      "isolate_pool.allow": { "存在": true, "二元组(bool,str)": true },
      "adv_isolation.can_acquire": { "存在": true, "二元组(bool,str)": true }
    },
    "seo_query_module": {
      "get_local_search_engine_url": { "地域样例": { "JP": {...}, "DE": {...}, "CN": {...} } },
      "generate_referer_for_region": { "返回类型": "str", "合法URL": true },
      "REGION_SEARCH_ENGINE_MAP": { "契约满足": true }
    },
    "ip_info_resolver": {
      "resolve_ip_info": {
        "预期字段": [...],
        "实际字段": [...],
        "缺失字段": [],
        "多余字段": [...]
      }
    },
    "popunder_trigger": {
      "预期导出函数": [...],
      "实际函数详情": {...},
      "缺失函数": []
    }
  }
}
```

### 2.3 安全契约

- **默认关闭**：未设置 `ENABLE_CONTRACT_PROBE=true` 时返回 404，不泄漏内部模块结构。
- **不落盘**：探针中对 `_IsolatePool` / `_AdvIsolation` 的实例化使用临时目录（`tempfile.TemporaryDirectory`），不污染生产状态。
- **不联网**：所有探针调用均为纯函数或本地 mock，不触发真实网络请求。

---

## 三、已验证项 / 待完善项 / 历史变更

### 3.1 已验证项（34 项）

| # | 模块 | 验证项 | 测试用例 |
|---|------|--------|---------|
| 1 | ip_provider | 6 个契约函数存在且可调用 | `test_contract_funcs_callable` |
| 2 | ip_provider | 5 种高危类型 → True | `test_high_risk_types_return_true` |
| 3 | ip_provider | 5 种安全类型 → False | `test_safe_types_return_false` |
| 4 | ip_provider | 记录后检查命中（去重语义） | `test_dedup_record_then_check` |
| 5 | ip_provider | `get_used_ips_count` 返回 int | `test_get_used_ips_count_returns_int` |
| 6 | ip_provider | 持久化函数存在 | `test_dedup_state_persist_functions_exist` |
| 7 | risk_control | `get_stable_canvas_seed` 在 `__all__` 中 | `test_import_star_contains_stable_seed` |
| 8 | risk_control | 同 fp_id 种子一致 | `test_stable_canvas_seed_same_fp_consistent` |
| 9 | risk_control | 不同 fp_id 种子不同 | `test_stable_canvas_seed_different_fp_different` |
| 10 | risk_control | 种子恒为正 | `test_stable_canvas_seed_positive` |
| 11 | risk_control | `pick_resolver` 返回 3 个非空字符串 | `test_dns_pick_resolver_returns_3_nonempty` |
| 12 | risk_control | `isolate_pool.allow` 返回 (bool, str) | `test_isolate_pool_allow_returns_pair` |
| 13 | risk_control | `adv_isolation.can_acquire` 返回 (bool, str) | `test_adv_isolation_can_acquire_returns_pair` |
| 14 | seo_query | JP 地域 URL 含 co.jp | `test_local_engine_url_jp` |
| 15 | seo_query | DE 地域 URL 含 google.de | `test_local_engine_url_de` |
| 16 | seo_query | CN 地域 URL 含 baidu | `test_local_engine_url_cn` |
| 17 | seo_query | `generate_referer_for_region` 返回合法 URL | `test_generate_referer_for_region_url` |
| 18 | seo_query | 不传 keyword 仍返回合法 URL | `test_generate_referer_for_region_no_keyword` |
| 19 | seo_query | `generate_referer` 向后兼容 | `test_generate_referer_backward_compat` |
| 20 | seo_query | `REGION_SEARCH_ENGINE_MAP` 常量契约 | `test_region_engine_map_contract_constants` |
| 21 | ip_info_resolver | 代理信息齐全时直接成功，6 字段齐备 | `test_resolve_ip_info_with_proxy_info_full` |
| 22 | ip_info_resolver | 无网络下仍返回合规 dict 结构 | `test_resolve_ip_info_never_hits_network` |
| 23 | ip_info_resolver | mock API 返回体补齐字段 | `test_resolve_ip_info_fills_from_mock_api` |
| 24 | popunder_trigger | 模块可独立导入 | `test_module_importable` |
| 25 | popunder_trigger | 6 个导出函数存在且可调用 | `test_exported_functions_exist` |
| 26 | popunder_trigger | 纯函数不启动浏览器即返回 bool | `test_pure_flag_functions_no_browser` |
| 27-34 | （参数化展开） | 各 parametrize 子用例 | 见测试文件 |

### 3.2 待完善项

| # | 模块 | 待完善内容 | 优先级 | 备注 |
|---|------|-----------|--------|------|
| 1 | ip_provider | `_start_periodic_save` 未纳入 CONTRACT 常量 | 🟢 低 | 测试中已断言存在，但清单未列 |
| 2 | risk_control | `_IsolatePool` / `_AdvIsolation` 构造函数参数契约 | 🟡 中 | 当前仅验证方法返回结构 |
| 3 | seo_query | `get_seo_query()` 单例模式契约 | 🟢 低 | 多次调用应返回同一实例 |
| 4 | ip_info_resolver | 异常路径（网络超时）返回结构契约 | 🟡 中 | 当前仅验证正常路径和空路径 |
| 5 | popunder_trigger | `self_test()` 返回体字段契约 | 🟡 中 | 当前仅验证存在性 |
| 6 | 全局 | 探针端点的自动化测试（test_contract 中增加） | 🟡 中 | 需 mock Flask app context |

### 3.3 历史变更

| 日期 | 版本 | 变更内容 | 影响模块 |
|------|------|---------|---------|
| 2026-08-10 | 1.0 | 初始建立契约审计体系：34 项静态测试 + 运行时探针 + 审计文档 + pre-commit 钩子 | 全部 5 模块 + app.py |

---

## 四、自动运行机制

### 4.1 pre-commit 钩子

配置文件：`.pre-commit-config.yaml`

- 钩子：`pytest` 运行 `tests/test_contract.py`
- 触发时机：每次 `git commit` 前
- 失败行为：阻止提交，需修复后重新提交

### 4.2 手动运行脚本

脚本：`scripts/run_contract_tests.sh`

```bash
#!/usr/bin/env bash
# 运行契约测试套件
# 用法：./scripts/run_contract_tests.sh
```

### 4.3 pytest 收集

`pytest.ini` 已配置：
- `testpaths = tests`
- `python_files = test_*.py`
- 契约测试文件 `tests/test_contract.py` 会被默认收集

---

## 五、验证命令速查

```bash
# 1. 运行契约测试
python3 -m pytest tests/test_contract.py -q

# 2. 验证 app.py 编译
python3 -m py_compile app.py

# 3. 启动探针（需设置环境变量）
ENABLE_CONTRACT_PROBE=true python3 app.py
# 然后访问：http://localhost:PORT/api/debug/contract_probe

# 4. 安装 pre-commit 钩子
pre-commit install

# 5. 手动触发 pre-commit 契约检查
pre-commit run pytest-contract --all-files
```
