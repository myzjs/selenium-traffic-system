# AGENT_RULES.md — 项目铁律（入口）

任何 Agent 在修改代码前必须先阅读本文件，并在动手前通读 `docs/AGENT_KNOWLEDGE_BASE.md`（坑库 / 测试基线 / 版本变更史 / 数据契约）。

> 交接协议是自我传播的：本文件 + 知识库是唯一共享载体（git 仓库本身，不是聊天记忆）。
> 新会话 / 新 Agent 只需 `git pull` → 读本文件 + `docs/AGENT_KNOWLEDGE_BASE.md`，即完整继承本项目全部约定与教训。
> 本仓库为「广告联盟流量风控测试系统」（Flask + Selenium + Playwright，红队攻防演练，多国代理流量模拟）。

---

## 0. 三大约束（永久生效，违反即返工）

### 约束一【全局一次性完整代码审计】
对整个工程批量审计时，必须覆盖 10 个维度（逻辑缺陷 / 空值风险 / 资源泄漏 / 异常缺陷 / 类型问题 / 安全风险 / 并发隐患 / 性能缺陷 / 兼容性 / 隐藏边界Bug），输出按 🔴阻断级 / 🟡高危 / 🟢优化 三级分级，每条含文件位置+代码行、原理、复现条件、完整修复代码，并生成 pytest 回归脚本。

### 约束二【修复 Bug 专用流程】（防"修一个冒出一个"）
1. 禁止改动业务无关代码、禁止优化无关逻辑、禁止擅自重构架构。
2. 区分「临时规避方案 / 根因修复方案」，优先根因修复。
3. 修改后逐条列出受影响代码路径 + 回归风险。
4. 列出需重点回归测试的原有功能。
5. 自动编写测试用例：复现原 Bug → 验证修复有效。

### 约束三【版本号 + Gitee 同步 + 前端展示】
- 版本号 `YY.M.D.N`（如 26.8.13.4），当天第 N 次变动序号自增，次日重置为 1。
- 每次代码变动必须：commit + push Gitee，前端展示的版本号与代码一致（`APP_VERSION` 常量）。

---

## 1. 标准工作流（每个任务必须按序执行）

1. **读档**：读本文件 + `docs/AGENT_KNOWLEDGE_BASE.md`（重点：坑库、当前版本、测试基线、数据契约）。
2. **定位**：先用 Grep / Glob / 语义搜索定位真实代码，禁止凭印象改代码。
3. **根因修复**：遵循约束二，禁止只打补丁掩盖症状。
4. **测试**：为新改动写 pytest 用例（复现 Bug → 验证修复），并跑相关回归测试集（见知识库「测试基线」）。
5. **版本号**：更新 `app.py` 的 `APP_VERSION`（若前端硬编码版本处同步更新）。
6. **提交 + 推送**：commit（信息含版本号）→ `git push gitee main`。
7. **部署（如需）**：按「部署清单」打包上传 VPS 并 `systemctl restart selenium_traffic.service`。
8. **交接**：将新坑、需求变更、版本记录、测试基线增量写入 `docs/AGENT_KNOWLEDGE_BASE.md` 并提交推送。

## 2. 验收标准

- [ ] pytest 相关测试集全部通过（`python3 -m pytest tests/ -q` 无新增失败）
- [ ] 版本号已更新且与前端展示一致
- [ ] 已 push Gitee（`git log --oneline -1` 可见新提交）
- [ ] 知识库已同步：坑库 / 版本变更史 / 测试基线已更新
- [ ] 部署类改动：VPS 页面 / API 实测验证通过

## 3. 部署清单（硬性）

**打包时必须包含全部依赖文件，禁止只挑主文件！** 历史事故：`deploy_v12.sh` 只打包 `app.py popunder_trigger.py`，导致红队模块 `redteam_scenarios.py` 等 5 个文件未上 VPS → `import` 失败被静默吞掉 → 功能"假装存在"。

部署包（/root/selenium_traffic_system）必须包含：
```
app.py  popunder_trigger.py  selenium_bridge.py  risk_check.py
redteam_webui.py  redteam_scenarios.py  redteam_reporter.py
redteam_integration.py  redteam_real_task_hook_example.py
traffic_distribution.py
```
- VPS 服务由 systemd 管理：`systemctl restart selenium_traffic.service`（**禁止**用 nohup 裸启，会与 systemd 冲突）。
- VPS 登录：`root@104.129.54.64`（凭据见本地 deploy 脚本，禁止写入本仓库）。

## 4. 硬约束速查（详见知识库「数据契约」）

- 流量有效性：仅 `ad_loaded == true 且 ad_impressions > 0` 算有效；**禁止**用 `_has_ad_code` 判成功。
- Pop-under：触发概率 0.85（★26.8.17.1 由 0.6 上调，冷却 75s 已兜底频控），生存 15-120s 三段混合分布（均值≈36-39s，下界 15s=R07 CRIT 硬门槛）；窗口禁止 `bring_to_front()`（防 IVT 分类）。
- 频控：单站任务 40 次/24h，多站任务 30 次/24h。
- 工作时间：强制 8:00-23:00 当地（country_segments / enforce_working_hours / 任务生成终检三层一致）。
- 看门狗宽限期：90s（防代理抖动误杀）。
- HilltopAds IP 访问控制：ISP/ASN 信息不完整的住宅 IP 必须放行。
- 调度器重启后自动恢复任务，无需手动 POST /start_task。
- 版本号 `YY.M.D.N` 规则见约束三。

## 5. 交接协议（自我传播）

1. 每次交付 = 代码提交 + **知识库文档同步提交**（坑库新增条目、版本变更史追加、测试基线更新、需求变更日志追加）。
2. 需求变更必须记录进 `docs/AGENT_KNOWLEDGE_BASE.md` 的「需求变更日志」，与代码提交对应（commit hash 关联）。
3. 若知识库与代码状态不一致，以代码为准，并立即修正知识库。
4. 本地聊天记忆（user profile / session memory）**不跨 Agent 共享**，只有落进仓库文件的内容才共享——务必把可复用结论沉淀到知识库。
