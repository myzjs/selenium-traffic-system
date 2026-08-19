# HilltopAds 风控审计报告 v2 — 改造后代码逐条复审

**审计日期**：2026-08-06（第2轮）  
**审计范围**：`popunder_trigger.py` (308行) + `app.py` 集成点（行 14730-14748, 14789-14791, 14833-14835）  
**改造内容**：CDP层弹窗触发 + 生命周期管理 + 坐标避让 + 冷却控制

---

## 开篇：改造前后对比

| 维度 | 改造前 | 改造后 | 改善 |
|------|--------|--------|:--:|
| 弹窗触发 | 零（无 window.open） | CDP `Input.dispatchMouseEvent` (isTrusted=true) | ✅ |
| 弹窗存活性 | 无 | 15-25s + DOMContentLoaded 确认 | ✅ |
| AdSense 误触 | 无保护 | `_pick_safe_coordinates` 60px 避让 | ✅ |
| 冷却控制 | 无 | 进程级 90s cooldown | ✅ |
| 概率控制 | 无 | 40% 触发率（模拟自然拦截） | ✅ |

**整体结论：从"完全无法产生 HilltopAds 收益"改善为"具备产生收益的基础能力，但仍有7个中高风险点需要修复"**

---

## 模块 1：Pop-under 弹窗专属结算行为审计

### 风险总评：🟡 中（从 🔴 严重降级）

### 逐项审计

#### 1-1：弹窗触发机制 ✅ 已解决

**代码**：`popunder_trigger.py:96-134` — CDP 层 5 步贝塞尔曲线鼠标移动 + 按下/释放序列

**评估**：`isTrusted=true` 的事件可以绕过 HilltopAds anti-adblock 检测。但有一个潜在问题：CDP session 通过 `context.new_cdp_session(context.pages[0])` 创建（第201行），如果 `context.pages[0]` 被 navigate 或 close，CDP 通道会断开。多轮循环中 L1 页面可能被 goto 重新导航。

#### 1-2：弹窗生命周期管理 ✅ 已解决

**代码**：`popunder_trigger.py:240-257` — `wait_for_load_state("domcontentloaded")` + 2s buffer + `time.sleep(stay)`  

**评估**：15s 最小停留符合 HilltopAds 基础结算阈值。但 `time.sleep(stay)` 期间主线程完全阻塞——意味着弹窗存活期间**原站浏览完全暂停**。真人用户会无视弹窗继续看原站。原站浏览暂停 15-25s 再恢复可能被 HilltopAds 的 Session Record 标记为异常。

#### 1-3：弹窗窗口指纹 🔴 缺失

**代码**：`popunder_trigger.py:257-263` — 弹窗关闭前没有任何反检测脚本注入

**问题**：HilltopAds 的 tag.min.js 会读取弹窗页面的 `window.opener`、`document.referrer`、`navigator.userAgent` 等属性。纯 `window.open()` 创建的弹窗这些值是正常的，但 Playwright 的 context page 这些值可能异常。没有注入任何 anti-fingerprinting 脚本。

#### 1-4：In-page Push 混跑 🔴 缺失

当前代码完全不处理 In-page Push 广告格式。如果站点同时开启了 Pop-under + In-page Push，需要额外处理 Push 的订阅/展示逻辑。**暂不阻塞**——因为 Pop-under 收益尚未验证，过早处理 Push 会增加测试变量。

### 量化阈值

| 指标 | 安全值 | 当前 | 状态 |
|------|--------|------|:--:|
| 弹窗触发机制 | isTrusted=true | ✅ CDP 实现 | 🟢 |
| 弹窗最小存活 | >10s | 15-25s | 🟢 |
| 弹窗触发率 | 30-50% | 40% | 🟢 |
| 弹窗指纹 | 完整 | ❌ 无注入 | 🔴 |
| 原站浏览 vs 弹窗同步 | 异步（真人无视弹窗） | 同步（time.sleep阻塞） | 🟡 |

### 整改方案

```python
# FIX 1: 弹窗反检测脚本注入（在 wait_for_load_state 之后、time.sleep 之前）
popunder_page.add_init_script("""
    if (!window.opener) {
        Object.defineProperty(window, 'opener', {value: window.parent || window, configurable: false});
    }
    if (document.cookie.length === 0) {
        document.cookie = 'ht_visitor=1; path=/; SameSite=Lax';
    }
""")

# FIX 2: 弹窗存活期间用 threading.Timer 管理，而不是阻塞原站浏览
# 伪代码：
# threading.Timer(stay_sec, lambda: popunder_page.close()).start()
# return popunder_page  # 让调用方继续原站浏览
```

---

## 模块 2：IP 与网络链路风控

### 风险总评：🔴 高 —— 改造未触及此模块

当前 IP 代理池、C段去重、ASN过滤等逻辑与改造前完全一致。HilltopAds 对机房 IP 的零容忍度未变。

**未修复的问题**：
- 代理池大概率仍是机房IP，过滤率 > 60%
- C段去重无跨天持久化（只在进程内去重）
- 多 VPS 从同一代理商拉 IP，C段重合率极高

---

## 模块 3：浏览器设备指纹

### 风险总评：🟡 中（主窗口 OK，弹窗空白）

主窗口的 Canvas/WebGL/Audio 噪声注入、navigator.webdriver 移除等与改造前一致，没有问题。但弹窗页面的指纹处理完全空白（见模块 1-3）。

---

## 模块 4：人机会话与时序行为

### 风险总评：🟡 中

#### 4-1：触发间隔 ✅ 已改善

**代码**：`popunder_trigger.py:164-170` — `cooldown_between_triggers_s = 90`

评估：90s 冷却合理，杜绝了"同站点连续弹窗"的高危特征。

#### 4-2：触发时机 ✅ 合理

**代码**：`app.py:14833` — L1 首页停留后触发（仅在 loop_idx==0 时）

评估：弹窗在积累了首页浏览+交互后触发，不是立即触发，符合真人行为模式。

#### 4-3：原站浏览被暂停 🔴 问题

**代码**：`popunder_trigger.py:257` — `time.sleep(stay)` 阻塞当前线程

这是当前实现最大的隐性缺陷：弹窗存活 15-25s 期间原站浏览完全冻结。`simulate_human_in_window` 的总时间被弹窗占用了一部分，但没有扣除——导致实际停留预算被弹窗吃掉了。HilltopAds 可以看到"用户在触发弹窗前有正常浏览→触发弹窗后原站突然静止 15s→弹窗关闭后又恢复浏览"这种痕迹。

#### 4-4：Referer 来源 ✅ 已处理

与改造前一致，`seo_query_module` 生成的搜索引擎 Referer 不受弹窗逻辑影响。

---

## 模块 5：广告可见性 Viewability

### 风险总评：🟡 中（从 🔴 降级）

#### 5-1：弹窗可见性 ✅ 已解决

**代码**：弹窗通过 `wait_for_load_state("domcontentloaded")` + 2s buffer 确保加载。

评估：弹窗页面完成基本加载后再计时 15-25s，满足 HilltopAds "弹窗必须加载后才开始计时"的要求。

#### 5-2：AdSense 坐标避让 ✅ 已解决

**代码**：`popunder_trigger.py:43-90` — `_get_ad_bounding_boxes` + `_pick_safe_coordinates`

评估：CDP 点击坐标自动避开已知广告容器（60px margin），30 次尝试后兜底到屏幕中上区域。这防止了 CDP 点击落在 Google 广告位上导致无效点击。

---

## 模块 6：流量质量与报表现象匹配

### 风险总评：改进到 🟡 中（从 🔴 严重）

改造后的预期变化：
- 弹窗现在有概率（40%）真实触发 → 应该能看到部分收益
- 但 60% 的不触发率 + 机房IP过滤率 > 60% → 实际有效结算可能在 15-20% 左右
- 如果继续使用机房代理IP，即使弹窗触发成功，也可能被 IVT 过滤

---

## 模块 7：网站合规

### 风险总评：🟡 中

弹窗触发率控制在 40% + 90s 冷却，不会触发"自动弹窗泛滥"规则。但每次触发弹窗的会话 100% 触发（40% 概率命中后必然触发一次），可能偏高于真人。

---

## 模块 8：多账号关联

### 风险总评：🔴 高 —— 改造未触及

多台 VPS 仍在共用相同的 `popunder_trigger.py` 参数和行为模式。HilltopAds 可通过弹窗的触发时机一致性、存活时长分布等数据关联各台 VPS。

---

## 模块 9：长期画像

### 风险总评：🔴 高 —— 取决于投产效果

如果改造后的有效结算率从 0% 提升到 15-20%，HilltopAds 的 90 天模型会逐渐调整评级。但如果机房 IP 导致过滤率居高不下，模型仍可能标记为无效流量源。

---

## 高危风险汇总（改造后仍存在）

| # | 模块 | 风险 | 优先级 |
|---|------|------|:--:|
| 1 | 1-3 | 弹窗页面无反转检测脚本注入（window.opener/Cookie） | P0 |
| 2 | 4-3 | time.sleep(stay) 阻塞原站浏览，制造"浏览中断"痕迹 | P0 |
| 3 | 2 | 机房代理IP，HilltopAds IVT 过滤率 > 60% | P0 |
| 4 | 8 | 多 VPS 共用行为模式，可被关联 | P1 |
| 5 | 1-1 | CDP session 可能因页面导航而断开 | P1 |
| 6 | 9 | 60% 不触发弹窗的会话对 HilltopAds 是"无广告库存消耗" | P2 |
| 7 | 3 | 弹窗窗口指纹空白 | P1 |

---

## 分优先级整改清单

### P0 紧急（上线前）

1. 弹窗 `time.sleep(stay)` 改为 `threading.Timer` + 异步管理，让原站浏览不受弹窗阻塞
2. 弹窗页面注入反检测脚本：`window.opener` 修复 + Cookie 基础值
3. 代理 IP 增加住宅/ISP 类型过滤，机房 IP 不给 HilltopAds 站点用

### P1（7天）

4. CDP session 使用 pages 列表中的活跃页面而非 pages[0]
5. 弹窗窗口指纹与主窗口一致性校验
6. 多 VPS 差异化配置（触发概率/存活时长微调）

### P2（长期）

7. 触发率从固定 40% 改为按天/时段动态调整
8. 弹窗存活时长分布向 Gamma 分布靠拢（减少正态性）

---

## HilltopAds 后台自查清单

1. Reports → Detailed：按天查看收益，改造后 D+2 应该有微量收益变化
2. Sites → Traffic Quality：查看 IVT% 是否下降
3. Settings → Anti-AdBlock：必须开启
4. 对比改造前后的"展示/收益"比率变化

---

## Nicolas 咨询模板

```
Subject: Pop-under traffic quality query for Site ID [YOUR_SITE_ID]

Hi Nicolas,

Could you help verify the following in your backend for my site [SITE_ID]:
1. What is the IVT filtering rate for the past 7 days?
2. Is there a difference between "raw impressions" shown in the dashboard
   and "billable impressions" after filtering?
3. What is the minimum pop-under open duration before billing qualifies?

Thank you.
```
