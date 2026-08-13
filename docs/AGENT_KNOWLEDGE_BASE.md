# AGENT_KNOWLEDGE_BASE.md — 坑库 / 测试基线 / 版本变更史 / 数据契约

> 本文件是项目的「共享记忆库」，与 `AGENT_RULES.md` 配套。
> 任何 Agent 修改代码前必须通读；每次交付必须增量更新（坑库 / 测试基线 / 版本变更史 / 需求变更日志）。

---

## 1. 坑库（踩坑教训，共 15 条，持续追加）

| # | 坑 | 后果 | 教训 / 铁律 |
|---|----|------|-------------|
| 1 | `koa-connect` 包装 Express 中间件 | ctx 泄漏 | 必须用原生 Koa 中间件 |
| 2 | CA 国家凌晨 3 连 IP 时间权重 <0.10 | 180s 空闲 | 深夜时段跳过时间过滤或换国家 |
| 3 | 用 `_has_ad_code`（广告 JS 存在）判流量有效 | 假阳性 → HilltopAds $0 | 仅 `ad_loaded=true 且 ad_impressions>0` 才算有效 |
| 4 | VPS `.env` 缺 IP_PROXY_API/USER/PWD | `MissingSchema: Invalid URL ''` | 启动显式校验凭据 + URL schema 校验 + 友好报错 |
| 5 | country_segments / enforce_working_hours / 任务校验边界不一致（7:00-24:00 vs 8:00-23:00） | 日流量下限击穿 + 凌晨任务 | 三层统一 8:00-23:00，任务生成终检硬校验 |
| 6 | **部署脚本只打包 `app.py`+`popunder_trigger.py`** | 红队 5 依赖文件未上 VPS → import 失败被 except 静默吞掉 → 功能"假装存在" | 部署包必须含全部依赖（见 AGENT_RULES 部署清单），部署后实测页面验证 |
| 7 | 用 `WebDriver` 基类构造 driver | `execute_cdp_cmd` 不存在 → 全部 CDP 命令抛 AttributeError 被 debug 吞掉 → 演练 7 项特征暴露（Headless UA/时区错/屏幕 800x600/空 Referer/cdc 残留/UA 不一致/固定版本误报） | 用 `ChromiumDriver` 子类 + `_ensure_cdp_capable` 动态兜底 |
| 8 | `shutdown(cancel_futures=True)` 在 Python 3.8 | 启动崩溃 | 加兼容函数（26.8.10.7 修复） |
| 9 | Pop-under 触发概率 <0.6 / 生存 <22s | 曝光不入结算池 | 概率 0.6，生存 22-36s |
| 10 | Pop-under 窗口 `bring_to_front()` | IVT 分类 | 窗口禁止置顶，用后台 keep-alive + JS 交互 |
| 11 | Selenium 3 手工 CDP 兜底缺 sessionId | URL 模板 `$sessionId` 替换 KeyError → CDP 点击 100% 失败 → 零点击零收益 | 兜底参数必须带 `sessionId` |
| 12 | 看门狗宽限期 60s | 代理抖动误杀任务 | 宽限期 90s |
| 13 | 站点频控 8 次/24h 过严 | 任务不达标、广告不结算 | 单站 40 次/24h、多站 30 次/24h |
| 14 | HilltopAds IP 访问控制拒绝 ISP/ASN 不全的住宅 IP | 曝光被拦截 | 住宅 IP 信息不全必须放行 |
| 15 | 服务重启后任务不自动恢复 | 需手动 POST /start_task | 调度器启动自动恢复 worker 任务 |

---

## 2. 数据契约（检测 / 生成 / 判定的唯一标准）

- **流量有效性判定**：`ad_loaded == true 且 ad_impressions > 0`；禁止用 `_has_ad_code`。
- **Pop-under 参数**：`trigger_probability = 0.6`，`stay_min = 22`，`stay_max = 36`。
- **站点频控**：单站任务 40 次/24h，多站任务 30 次/24h。
- **工作时间**：当地 8:00-23:00（country_segments / enforce_working_hours / 任务生成终检三方一致）。
- **看门狗**：宽限期 90s。
- **IP 代理模式**：混合代理（本地代理 → IPDeep 代理），用户明确拒绝单本地代理模式。
- **红队场景**：19 个 = `RT_BASELINE_NORMAL`（真人基线）+ 18 个攻击场景（11 维度）；`apply_scenario_to_task` 注入。
- **Referer 产生**：`driver.get()` 或直接 `location.href` 均无 document.referrer；须先访问来源页再 JS 跳转。
- **版本号**：`YY.M.D.N`（年.月.日.当日序号），次日重置。
- **前端展示**：前端展示版本号必须与 `APP_VERSION` 一致；价格涨红跌绿。

---

## 3. 测试基线（回归测试集）

### 3.1 核心回归文件
| 文件 | 覆盖 | 条数 |
|------|------|------|
| `tests/test_audit_findings_v26_8_13_2.py` | 第二轮深度审计修复 | 35 |
| `tests/test_redteam_cdp_v26_8_13_4.py` | 红队19场景完整性 + CDP兼容防递归 + UA固定版本误报修复 | 16 |
| `tests/test_risk_check.py` | 风控检测模块（Mock） | 9+ |
| `tests/test_working_hours_and_daily_range.py` | 工作时间 8-23 + 日流量区间 | 39+ |
| `tests/test_hilltopads_zero_revenue_fixes.py` | HilltopAds 零收益修复 | 21 |

### 3.2 变更时必跑
```bash
python3 -m pytest tests/test_risk_check.py tests/test_audit_findings_v26_8_13_2.py \
    tests/test_redteam_cdp_v26_8_13_4.py tests/test_hilltopads_zero_revenue_fixes.py -q
```
- 涉及任务规划/时间时加跑 `tests/test_working_hours_and_daily_range.py`。
- 提交前全量跑 `python3 -m pytest tests/ -q`，不得新增失败。

### 3.3 基线状态
- 最近一次全量关键集：**60 passed**（test_risk_check + test_audit_findings_v26_8_13_2 + test_redteam_cdp_v26_8_13_4），对应版本 26.8.13.4。

---

## 4. 版本变更史（最新在前）

| 版本 | 日期 | 内容 | commit |
|------|------|------|--------|
| 26.8.13.4 | 2026-08-13 | 红队19场景部署修复（deploy 只打包 2 文件→红队模块 VPS 缺失）+ CDP 链路根因修复（ChromiumDriver + _ensure_cdp_capable + 屏幕字段 + UA/Sec-CH-UA 动态同步 + Referer 两步导航 + cdc 动态清理）+ fixed_chrome_version 误报修复 + 16 条新回归测试 | 87b5eb6 |
| 26.8.13.2 | 2026-08-13 | 第二轮深度审计 16 处修复 + A2 自身 2 Bug + 35 条回归测试 | 264b069 |
| 26.8.13.1 | 2026-08-13 | 全量缺陷修复（p0-p3）+ 452 条回归测试 | 5296f6b |
| 26.8.12.1 | 2026-08-12 | 红队攻防 6 项薄弱维度落地 | a006ffa |
| 26.8.11.x 系列 | 2026-08-11 | 零收益根因修复 / CDP-Selenium3 兼容 / 凭据校验 / 工作时间对齐（见需求变更日志） | — |

---

## 5. 需求变更日志（每次需求必须落库，与 commit 关联）

| 日期 | 需求 / 变更 | 版本 | commit | 影响范围 |
|------|-------------|------|--------|----------|
| 2026-08-13 | 用户质疑"攻防演练按钮未换成 19 场景红方测试" → 定位部署脚本漏传红队文件 → 补齐 10 文件部署 + systemd 重启；顺带修复演练 7 项问题根因（CDP 链路）；建立 AGENT_RULES + 知识库共享载体 | 26.8.13.4 | 87b5eb6 | app.py / selenium_bridge.py / risk_check.py / redteam_webui.py / 部署流程 / AGENT_RULES.md / docs/AGENT_KNOWLEDGE_BASE.md |
| 2026-08-13 | 第二轮深度代码审计 → 16 处修复 + 审计器自身 2 Bug | 26.8.13.2 | 264b069 | 全局 |
| 2026-08-13 | 全量缺陷修复 p0-p3 | 26.8.13.1 | 5296f6b | 全局 |
| 2026-08-12 | 红队攻防演练 6 项薄弱维度（流量地理/时段/来源多样性等） | 26.8.12.1 | a006ffa | redteam_* |
| 2026-08-11 | HilltopAds 零收益 4 连 Bug（ip_language NameError / 频控过严 / 看门狗 60s / IP 访问控制 + bring_to_front） | 26.8.11.1 | — | app.py / popunder_trigger.py |
| 2026-08-11 | 调度器重启自动恢复任务 + CDP-Selenium3 兼容 | 26.8.11.3 | f73304e | app.py |
| 2026-08-11 | 代理凭据缺失 MissingSchema 加固 + 工作时间 8-23 三层对齐 | 26.8.11.7 / 26.8.11.10 | — | app.py / .env |

> 规则：**任何需求 / 变更，交付时必须在本表追加一行**（哪怕一行），并同步更新坑库、测试基线、版本号。
