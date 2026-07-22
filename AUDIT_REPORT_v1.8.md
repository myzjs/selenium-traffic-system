# selenium_traffic_system v1.8 — 全面审计报告

> 审计日期：2026-06-29  
> 审计范围：代码安全、业务逻辑、测试覆盖率、框架对照差距分析  
> 备份位置：outputs/selenium_traffic_system_v1.8.tar.gz (1.9MB)

---

## 一、备份信息

| 项目 | 详情 |
|------|------|
| 备份文件 | `selenium_traffic_system_v1.8.tar.gz` |
| 大小 | 1.9 MB |
| 内容 | 完整源代码 + .git 仓库 + 测试 + 配置文件 |
| 排除项 | `__pycache__/`、`*.pyc`、`test_reports/`（历史报告）、`report/`（演练报告）、`feedback/`、`*.bak` |

---

## 二、严重问题报告（CRITICAL）

### C-1. 明文凭据硬编码在源码和配置文件中
| 项目 | 详情 |
|------|------|
| **严重等级** | CRITICAL |
| **类别** | 安全 |
| **涉及文件** | `config.json:15,841-869`、`proxy_server_new.py:21-22`、`ip_provider.py:145,518`、`app.py:10388-10389` |
| **描述** | 仓库中包含真实密钥：VPS密码 (`admin123`)、IP代理密码 (`zhan7263`)、IPDeep API账号ID（嵌入在URL中）、VPS公网IP (`104.129.54.64`)。`config.json` 被提交到了 git 仓库中。 |
| **影响** | 任何人获得仓库访问权限即可接管VPS、盗用代理配额、冒充IPDeep账户。 |
| **建议** | 立即轮换所有凭据；将 `config.json` 加入 `.gitignore`；使用 `.env` 文件管理密钥；用 `git filter-repo` 清除历史记录。 |

### C-2. Flask管理界面无认证，密码明文渲染到HTML
| 项目 | 详情 |
|------|------|
| **严重等级** | CRITICAL |
| **类别** | 安全 |
| **涉及文件** | `app.py:3318-3391, 14152-14866` |
| **描述** | Flask绑定 `0.0.0.0`，无任何认证机制。所有路由（`/start_video_tasks`、`/save_config`等）无CSRF保护。配置中的VPS密码、代理密码直接嵌入HTML的 `<input value="...">` 字段。 |
| **影响** | 局域网内（或端口暴露时全网）可直接读取所有凭据、启动/停止任务、POST恶意配置。 |
| **建议** | 添加HTTP Basic Auth；密码字段mask显示；绑定 `127.0.0.1` 或使用反向代理；启用CSRF Token。 |

### C-3. `exec()` 用于普通变量查找
| 项目 | 详情 |
|------|------|
| **严重等级** | CRITICAL |
| **类别** | 安全隐患 |
| **涉及文件** | `app.py:11131` |
| **描述** | `exec("if 'cc_upper' in locals() and cc_upper: country_code = cc_upper")` — 用 `exec()` 来规避变量作用域警告，但引入了任意代码执行风险。 |
| **影响** | 若变量来源被污染，可导致任意代码执行；静态分析工具完全失明。 |
| **建议** | 替换为 `if 'cc_upper' in locals(): ...` 等普通Python写法。 |

### C-4. `_used_ips` 字典在迭代中被修改（RuntimeError）
| 项目 | 详情 |
|------|------|
| **严重等级** | CRITICAL |
| **类别** | Bug |
| **涉及文件** | `proxy_server_new.py:126-128` |
| **描述** | 遍历 `_used_ips.items()` 时并发线程也在修改同一字典，无锁保护。 |
| **影响** | 高并发下抛出 `RuntimeError: dictionary changed size during iteration`，IP去重机制失效。 |
| **建议** | 添加 `_used_ips_lock` 锁保护所有读写操作；遍历时使用 `list(_used_ips.items())` 快照。 |

### C-5. ADSL配置文件写入存在命令注入风险
| 项目 | 详情 |
|------|------|
| **严重等级** | CRITICAL |
| **类别** | 安全隐患 |
| **涉及文件** | `ip_provider.py:235-242` |
| **描述** | ADSL用户名/密码通过f-string写入 `/etc/ppp/peers/adsl`，无任何转义。通过Web UI注入特殊字符可逃逸引号并注入pppd指令。 |
| **影响** | 若Flask以root运行，攻击者可通过构造密码获得root shell。 |
| **建议** | 验证用户名为 `^[A-Za-z0-9._@-]+$`；使用 `chap-secrets` 替代内联密码；Web进程不应以root运行。 |

---

## 三、高危问题报告（HIGH）

| # | 标题 | 文件 | 描述 |
|---|------|------|------|
| H-1 | Flask缺少Secret Key和HTTPS | `app.py:14866` | 未设 `app.secret_key`，HTTP明文传输，cookie可伪造 |
| H-2 | 硬编码macOS chromedriver路径 | `selenium_bridge.py:1681` | `/Users/mac/.wdm/.../chromedriver` 仅在此机器有效 |
| H-3 | `Page._current_page` 全局变量被覆盖 | `selenium_bridge.py:724,738,403` | 多页面时Frame查找解析到错误的Page |
| H-4 | IP-API响应驱动系统命令 | `app.py:9396,9442-9443` | `timedatectl` 的timezone参数来自外部API |
| H-5 | 不安全HTTP调用 | `ip_info_resolver.py:121`、`ip_provider.py:136` | VPS控制通道和ip-api.com使用明文HTTP |
| H-6 | HTTPS CONNECT握手失败时socket泄漏 | `proxy_server_new.py:767-770` | 客户端已收到200但隧道不可用 |
| H-7 | `_used_ips` 无锁写入 | `proxy_server_new.py:162-189` | 多个handler线程竞态写入 |
| H-8 | IPDeep账号ID暴露在代码中 | `config.json:839-870` | `GetIpByGenerateLink?id=...` 即API密钥 |

---

## 四、中/低级别问题汇总

### 中等级别 (MEDIUM) — 10项
1. **裸 `except:`** — `app.py:183`、`utils.py:14`、`proxy_server_new.py` 多处吞掉所有异常
2. **运行时反射解析handler源码** — `selenium_bridge.py:1438-1460` 用 `inspect.getsource` + 正则提取header
3. **全局proxy_pool被临时替换** — `ip_provider.py:383-406` 多线程竞态
4. **`random` 模块跨线程共享** — 可能导致关联随机序列
5. **死代码** — `_js_quote`、`PROXY_POOL` 常量未被使用
6. **测试fixture未使用** — `test_risk_check.py` 重复实现了 `mock_page`
7. **`pytest-mock` 未列入依赖** — `conftest.py` 引用但 `requirements.txt` 中缺失

### 低级别 (LOW) — 15项
- 重复`import os`
- 模块级 `print()` 语句
- 变量名遮蔽
- `config.json` 中 layer_1~layer_6 存在500行重复配置
- 多余本地 `import datetime`
- Keyboard.type 静默吞错
- 风险报告路径无净化

---

## 五、测试覆盖率总结

| 模块 | 有测试 | 缺失项 |
|------|--------|--------|
| `ip_provider.py` | ✅ | 缺少线程安全测试、`_extract_ip_from_output` 边界测试 |
| `risk_check.py` | ✅ | 缺少 `run_drill` 测试、ad_selector 注入测试 |
| `seo_query_module.py` | ✅ | 覆盖率良好 |
| `ip_info_resolver.py` | ✅ | 缺少 API 超时路径测试 |
| `ip_region_module.py` | ✅ | 覆盖率良好 |
| `app.py` | ❌ | **14,800+ 行完全未测** |
| `selenium_bridge.py` | ❌ | 1,790 行未测 |
| `proxy_server_new.py` | ❌ | Auth、CONNECT隧道、SOCKS5均未测 |
| `utils.py` | ❌ | `clean_logs` 未测 |

**测试基础设施问题**：`conftest.py` 预定义了 `mock_page`、`mock_response` 等fixture但部分测试文件未使用；`pytest-mock` 未被声明为依赖。

---

## 六、框架对照差距分析（对应思维导图）

### 统计总览（排除"不适用"项后共21项有效评估）

| 状态 | 数量 | 占比 |
|------|------|------|
| ✅ 已有 | 4 | 19.0% |
| ⚠️ 部分 | 10 | 47.6% |
| ❌ 缺失 | 7 | 33.3% |
| ➖ 不适用 | 3 | — |

### 逐项对照

**分支2：研究与规划**
| 子项 | 状态 | 说明 |
|------|------|------|
| 2.1 目标/KPI设定 | ❌ 缺失 | 无业务目标定义，仅有技术参数 |
| 2.2 市场调研 | ❌ 缺失 | 无竞品分析、趋势数据采集 |
| 2.3 内容差距分析 | ⚠️ 部分 | 有关键词池但无搜索量/竞争度分析 |
| 2.4 内容Brief | ❌ 缺失 | 系统不涉及内容策划 |

**分支3：内容策略与规划**
| 子项 | 状态 | 说明 |
|------|------|------|
| 3.1 受众细分 | ⚠️ 部分 | 按国家/时区分组，但非用户画像维度 |
| 3.2 内容支柱 | ➖ 不适用 | 流量工具不生产内容 |
| 3.3 内容日历 | ✅ 已有 | 多天计划生成+暂停恢复+停止控制 |
| 3.4 SEO优化 | ⚠️ 部分 | 搜索跳转完整但缺排名监控和SEO审计 |

**分支4：内容生产与优化**
| 子项 | 状态 | 说明 |
|------|------|------|
| 4.1 AI辅助生成 | ➖ 不适用 | — |
| 4.2 人机协作 | ➖ 不适用 | — |
| 4.3 多模态创作 | ➖ 不适用 | — |
| 4.4 A/B测试 | ❌ 缺失 | 无对照组/实验组框架 |

**分支5：分发与推广**
| 子项 | 状态 | 说明 |
|------|------|------|
| 5.1 渠道选择 | ✅ 已有 | 13个搜索引擎+6个社媒平台 |
| 5.2 付费媒体 | ❌ 缺失 | 无Google Ads/Facebook Ads集成 |
| 5.3 社区互动 | ⚠️ 部分 | 配置了互动概率但无实际互动执行代码 |
| 5.4 跨渠道协同 | ⚠️ 部分 | 网站+视频任务可并行但无归因/调度 |

**分支6：绩效监控与迭代**
| 子项 | 状态 | 说明 |
|------|------|------|
| 6.1 KPI追踪 | ⚠️ 部分 | 有stats计数但无持久化仪表盘 |
| 6.2 数据驱动优化 | ⚠️ 部分 | 有补偿机制但无自动闭环调优 |
| 6.3 竞品基准 | ❌ 缺失 | 无SimilarWeb等数据集成 |
| 6.4 持续学习 | ❌ 缺失 | UA管理为硬编码规则非自适应 |

**分支7：伦理与治理**
| 子项 | 状态 | 说明 |
|------|------|------|
| 7.1 偏见检测 | ❌ 缺失 | 无流量分布异常告警 |
| 7.2 透明度 | ❌ 缺失 | 无操作审计日志、无变更追踪 |
| 7.3 隐私合规 | ⚠️ 部分 | 有WebRTC泄露检测但缺GDPR框架 |
| 7.4 人工监督 | ✅ 已有 | 完整启停控制+计划预览+实时日志 |

### Top 5 优先填补项

1. **绩效数据仪表盘**（6.1 + 6.2）— 运行数据散落各处，缺乏可视化
2. **数据驱动闭环优化**（6.2 + 4.4）— 风控报告不会自动反馈到配置
3. **目标/KPI定义与追踪**（2.1）— 缺少"北极星指标"
4. **SEO能力深化**（3.4 + 2.3）— 关键词仅硬编码，缺搜索量/排名数据
5. **操作审计与透明度**（7.2 + 7.3）— 配置变更无记录

---

## 七、优先级行动建议

### 立即（本周）
1. 轮换所有凭据，`config.json` 加入 `.gitignore`
2. Flask添加认证，密码字段mask处理
3. 替换 `exec()` 为普通Python写法
4. 修复 `_used_ips` 的锁问题

### 短期（1-2周）
5. Web UI增加KPI仪表盘（Chart.js）
6. 增加配置变更审计日志
7. 历史数据自动过期清理
8. VPS控制通道切换到HTTPS

### 中期（3-4周）
9. 构建反馈闭环模块（风控评分→自动调参）
10. SEO模块集成SERP API
11. 增加A/B测试框架
12. `app.py` 添加核心函数单元测试
