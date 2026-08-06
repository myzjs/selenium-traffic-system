"""
# 本地自测（不需要 app.py 跑起来）：
# 1) 启动 guardian:   python3 _dwell_monitor_guardian.py --no-auto-pause --consume-history --poll 0.05 >/dev/null 2>&1 &
# 2) 运行本脚本:      python3 _dwell_monitor_guardian_test.py
# 3) 观察 guardian 控制台输出是否打出 WARNING + CRITICAL + 跳出率>55%的 CRITICAL
"""
import os, time, random, pathlib

_logpath = pathlib.Path(__file__).with_name("app.log")

LOG_TEMPLATES = [
    # 正常 OK 任务（占比 60%）
    ("OK", """2026-08-06 11:22:33,142 INFO  ⏱️ [停留-01] enter_site_time锚点: 11:22:33, total_stay配置: min=120s / max=300s
2026-08-06 11:22:33,891 INFO  ⏱️ [停留-02] Session保险绳(对数正态): 188s | task_deadline = 11:25:41
2026-08-06 11:22:50,402 INFO  🔍 [真搜索] 已成功跳转至目标页
2026-08-06 11:23:01,700 INFO  [第1轮] layer_2 停留: 42.1秒
2026-08-06 11:25:45,012 INFO  ✅ P2-5[停留审计] 浏览网站时长=191.9s ≥ 60s 达标\n"""),
    # 跳出型 OK（占比 15%）
    ("BOUNCE_OK", """2026-08-06 11:26:01,112 INFO  ⏱️ [停留-01] enter_site_time锚点: 11:26:01
2026-08-06 11:26:02,018 INFO  ⏱️ [停留-02] Session保险绳(对数正态): 134s | 建议值 60s 达标
2026-08-06 11:26:02,400 INFO  🚪 本次任务为跳出型(概率28%)：仅停留首页后离开
2026-08-06 11:26:02,621 INFO  ⏱️ [停留-03] 跳出型任务停留=92.7s ≥ 建议值60s（仍属于高跳出率，长期会降低质量分）
2026-08-06 11:27:38,223 INFO  ✅ P2-5[停留审计] 浏览网站时长=97.1s ≥ 60s 达标\n"""),
    # WARN: 58.7s < 60s（占比 10%）
    ("WARN", """2026-08-06 11:28:00,210 INFO  ⏱️ [停留-01] enter_site_time锚点: 11:28:00
2026-08-06 11:28:01,010 INFO  ⏱️ [停留-02] Session保险绳(对数正态): 59s | median=180s
2026-08-06 11:28:59,771 INFO  ⚠️ P2-5[停留审计] 浏览网站时长=58.7s < 建议阈值 60s，广告有填充但可能没达到ActiveView(≥50%面积/≥1s) 计数，收益有较大折损。\n"""),
    # CRIT: 38.2s < 45s（占比 8%）→ guardian 应报 CRITICAL
    ("CRIT", """2026-08-06 11:29:31,400 INFO  ⏱️ [停留-01] enter_site_time锚点: 11:29:31
2026-08-06 11:29:32,120 INFO  ⏱️ [停留-02/红线] Session保险绳=41s < 红线45s，广告脚本大概率没完成 init→request→拍卖→渲染，必然 0 收益！
2026-08-06 11:30:11,320 INFO  🚫 P2-5[停留审计] 浏览网站时长=39.8s < 红线45s，广告脚本大概率尚未完成 init+request+render，本任务基本确定 0 收益。\n"""),
    # CRIT: 跳出型 + 停留33s（占比 7%）
    ("BOUNCE_CRIT", """2026-08-06 11:31:00,110 INFO  ⏱️ [停留-01] enter_site_time锚点: 11:31:00
2026-08-06 11:31:01,330 INFO  🚪 本次任务为跳出型(概率33%)：仅停留首页后离开
2026-08-06 11:31:01,880 ERROR ⏱️ [停留-03/红线] 跳出型任务停留=33.1s < 红线45s，广告脚本不可能完成 init+request+render，必然 0 收益。
2026-08-06 11:31:36,000 INFO  🚫 P2-5[停留审计-异常] 浏览网站时长=35.8s < 红线45s，广告未完成渲染\n"""),
]

# 合成一个触发：最近 20 个任务里跳出率 > 55%
def simulate():
    rnd = random.Random(42)
    lines = []
    # 先丢 25 条历史 OK/BOUNCE_OK 热身
    for _ in range(25):
        r = rnd.random()
        key = "OK" if r < 0.7 else "BOUNCE_OK"
        lines.append(LOG_TEMPLATES[[t[0] for t in LOG_TEMPLATES].index(key)][1])
    # 再来 10 条 CRIT/BOUNCE_CRIT 混合 → 滑窗跳出率会冲上去
    for _ in range(10):
        key = rnd.choice(["BOUNCE_CRIT", "CRIT", "WARN", "BOUNCE_CRIT"])
        lines.append(LOG_TEMPLATES[[t[0] for t in LOG_TEMPLATES].index(key)][1])
    # 最后再单独塞 1 条 CRIT 触发 自动暂停
    lines.append(LOG_TEMPLATES[[t[0] for t in LOG_TEMPLATES].index("CRIT")][1])

    with open(_logpath, "a", encoding="utf-8") as f:
        for block in lines:
            # 写一条停 20ms，模拟真实日志到来
            f.write(block)
            f.flush()
            time.sleep(0.02)
    print(f"✅ 写入完成：{_logpath}（累计写入块数={len(lines)}）")

if __name__ == "__main__":
    simulate()
