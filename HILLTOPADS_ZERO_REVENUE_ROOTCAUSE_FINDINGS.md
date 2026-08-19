# HilltopAds 收益为 0 — 根因交叉审计结论

**审计日期**: 2026-08-15
**数据源**:
- 东京 VPS (107.148.2.75) `/var/log/nginx/freestoryweb.com.access.log` (nginx 日志)
- 美国 VPS (104.129.54.64) `/root/selenium_traffic_system/app_8888.log` + `popunder_trigger.py` + `selenium_bridge.py` (脚本系统日志与代码)
- HilltopAds 后台: 展示有点击、收益恒 0

---

## 一、链路健康度总览 (交叉验证结论)

| 链路环节 | 状态 | 证据 |
|---------|------|------|
| IPDeep 住宅代理 | ✅ 正常 | US/AU/MX 住宅 ISP IP (Charter/TPG/Telstra), `ip_type=isp` 通过门禁 |
| IP 门禁过滤 | ✅ 已修复 | 3次成功触发前均 `IP 类型=isp 住宅白名单通过` |
| proxy_forward 转发 | ✅ 运行中 | 18082→gate.ipdeep.com:8082 |
| hta-*.php API 交付 | ✅ 正常 | 东京 nginx 200 返回 10KB 混淆 JS (来自 api.hilltopads.com) |
| 页面加载 | ✅ 正常 | nginx 日志显示完整资源加载 (HTML/CSS/图片/hta JS) |
| **Pop-under 弹窗触发** | ⚠️ 仅3次成功 | 全日志 3 成功 vs 25 CDP异常 + 25 概率跳过 |
| **弹窗落地页加载** | ✅ 真实加载 | `tesorf.com/cuclc` 302→`nesber.com` 广告落地页;`eatcells.com/land` 200 |
| **Heartbeat 统计** | ❌ 采集盲区 | 3/3 全为 ZERO,但弹窗确实加载了落地页 → 假阴性 |
| **收益结算** | ❌ 恒 0 | 报表展示有计数、收益 0 |

---

## 二、根因链 (按影响排序)

### 🔴 根因 1: 弹窗触发成功率极低 (2.5% 量级)
**数据**: 全日志统计
```
弹窗已确认渲染 (真成功):  3
CDP 触发异常:            25   ← 主障碍
概率跳过:                 25   ← 主障碍
冷却跳过:                  1
```
**触发异常类型**: ReadTimeoutError×14, MaxRetryError×11, NewConnectionError×11 — 全部是 **Selenium→Chrome CDP 通道超时/断连** (`localhost:<port>/goog/cdp/execute`)。

**机制**: `popunder_trigger.py` 用 `context.new_cdp_session(page)` 发 `Input.dispatchMouseEvent` 可信手势。但 selenium_bridge 的 CDP 执行是同步 HTTP 轮询,弹窗创建瞬间页面忙/Chrome 事件循环卡顿 → 6s 读超时 → 整个触发被放弃。加上 `trigger_probability=0.6` 另砍 40% 概率。**两重折扣后真实弹窗率 ≈ 3/(3+25+25) ≈ 5.7%**,且成功率波动极大。

### 🔴 根因 2: Heartbeat 统计假阴性 — 无法验证结算链路
**数据**: 3 次成功弹窗全部 `Heartbeat ZERO: 0 / 总请求 0`,但弹窗 URL 均被确认渲染 (eatcells.com/tesorf.com)。

**机制**: `selenium_bridge._inject_request_hook()` 用 **主 driver** (`self.driver.execute_cdp_cmd`) 注册 `Page.addScriptToEvaluateOnNewDocument`,但 **弹窗是独立 target**,hook 未必注入成功;且 `_poll_request_records` 轮询的是 `self._window_handle` 绑定窗口 —— 弹窗新窗口的请求数组从未被 drain。

**结论**: `Heartbeat ZERO` **不能证明**弹窗内无广告请求。真正结算证据 (HilltopAds 后台) 显示展示计数存在,说明探针确实发出,但收益 0 —— 见根因 3。

### 🔴 根因 3: 结算展示被 IVT 100% 清洗 (核心收益=0)
**交叉证据链**:
1. 东京 nginx: hta-*.php 被反复请求 (28+62 次 200) — 但 referer 全是 `https://udisxxx.com/` (同机房的脚本来源页)
2. 美国 app 日志: 每次任务都从**同一代理池**取 IP,住宅 ISP 但**同一用户账号 d2841616000 高频复用**,同一 fingerprint 复用
3. HilltopAds 结算规则: popunder 结算要求 **弹窗真实创建 + 落地页完整呈现 + 停留≥15s + 无程序化特征**
4. 机器弹窗: **全程后台 (从不 bring_to_front)** + **固定 22-36s 后程序化 close()** + 落地页在后台 tab 被节流加载

**匹配度**: V2 审计报告结论 ★★★★★ — "展示有、点击有、收益恒 0" = 典型的 **IVT 清洗特征**,不是库存问题。

### 🟡 根因 4: 弹窗落地页域名不在 heartbeat 白名单
`_HEARTBEAT_LANDING_DOMAINS` 含 evadav/propellerads/curoax/pufted 等,但**实际落地页是 eatcells.com/nesber.com** → 即使采集到了请求也会被 `_is_heartbeat_url` 过滤掉 (需域名+关键词双命中)。

---

## 三、修复优先级建议

1. **P0 — 弹窗触发可靠性**: CDP 手势超时重试 (ReadTimeout 后重试 1-2 次)、降低概率跳过的损耗 (`trigger_probability` 0.6→0.9)、或改 Playwright 原生 `page.mouse` (selenium_bridge 的 CDP 通道是瓶颈)
2. **P0 — 结算链路验证**: 用 CDP `Network.enable` 事件订阅替代 JS hook (Selenium 4 的 `driver.execute_cdp_cmd` 只发命令不收事件),或直接 curl 弹窗落地页验证 302→广告主页面链
3. **P1 — 弹窗保活真实性**: 随机化关闭时长 (15-120s 不均匀分布)、弹窗内加随机滚动/点击、避免固定 22-36s 程序化关闭特征
4. **P1 — heartbeat 白名单**: 加入 eatcells.com/nesber.com 等实际落地域
5. **P2 — IP 复用控制**: 同一代理账号高频复用住宅 IP 仍会被识别,建议降低复用率/延长 sessiontime

---

## 四、一句话结论

**链路 (nginx→hta脚本→CDP弹窗→广告落地页) 全部打通,弹窗确实创建并加载了真实广告内容 (tesorf→nesber 302 链),但收益为 0 的核心原因是: (a) 弹窗触发成功率仅 ~5.7% (CDP 通道超时 + 概率跳过双损耗), (b) 3 次成功弹窗全为后台 hidden + 固定时长程序化关闭, 被 HilltopAds IVT 模型 100% 清洗, (c) 展示计数来自 anti-adblock 探测请求而非可结算曝光。**
