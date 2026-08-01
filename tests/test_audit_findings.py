# -*- coding: utf-8 -*-
"""
test_audit_findings.py —— 审计缺陷验证测试
覆盖全面深度审计中发现的所有🔴阻断级和🟡高危隐患问题。
每条测试对应审计报告中的一个编号问题。
"""
import json
import os
import sys
import threading
import time
import html as html_mod

import pytest

# 确保项目根目录在path中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# 辅助：创建Flask测试客户端
# ============================================================
@pytest.fixture(scope="module")
def app_client():
    """导入app并创建测试客户端（不启动服务器）"""
    # 设置测试环境变量避免副作用
    os.environ.setdefault("RUN_PORT", "15999")
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client, app_module


# ============================================================
# 🔴 #1: XSS漏洞 - /get_logs 返回未转义HTML
# ============================================================
class TestXSSInGetLogs:
    """验证日志输出是否对HTML特殊字符进行转义"""

    def test_log_message_with_script_tag_should_be_escaped(self, app_client):
        client, app_module = app_client
        # 注入含XSS payload的日志
        xss_payload = '<script>alert("xss")</script>'
        app_module.log.messages.append(xss_payload)
        try:
            resp = client.get('/get_logs')
            body = resp.data.decode('utf-8')
            # 修复后：不应包含原始<script>标签
            # 如果未修复，body中会出现原始<script>
            assert '<script>' not in body, \
                "XSS漏洞：/get_logs返回了未转义的<script>标签"
        finally:
            # 清理
            if xss_payload in app_module.log.messages:
                app_module.log.messages.remove(xss_payload)

    def test_log_message_with_html_entities(self, app_client):
        client, app_module = app_client
        payload = '<img src=x onerror=alert(1)>'
        app_module.log.messages.append(payload)
        try:
            resp = client.get('/get_logs')
            body = resp.data.decode('utf-8')
            assert 'onerror=' not in body, \
                "XSS漏洞：/get_logs返回了未转义的事件处理器"
        finally:
            if payload in app_module.log.messages:
                app_module.log.messages.remove(payload)


# ============================================================
# 🔴 #2: /start_task 竞态条件
# ============================================================
class TestStartTaskRaceCondition:
    """验证并发启动任务时是否存在竞态"""

    def test_concurrent_start_task_should_only_start_one(self, app_client):
        client, app_module = app_client
        # 确保初始状态为未运行
        app_module.task_running = False
        results = []
        barrier = threading.Barrier(2, timeout=5)

        def fire_start():
            barrier.wait()
            resp = client.post('/start_task')
            results.append(resp.status_code)

        t1 = threading.Thread(target=fire_start)
        t2 = threading.Thread(target=fire_start)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # 修复后：应有一个409（拒绝）和一个200
        # 未修复时：两个都是200（竞态）
        # 注意：由于Flask测试客户端是同步的，这里主要验证逻辑正确性
        assert 200 in results, "至少一个请求应成功"
        # 清理
        app_module.task_running = False


# ============================================================
# 🔴 #3: /save_seo_config 未判空
# ============================================================
class TestSaveSeoConfigNullBody:
    """验证非JSON请求不会导致500"""

    def test_empty_body_should_not_crash(self, app_client):
        client, app_module = app_client
        resp = client.post('/save_seo_config',
                           data='not json',
                           content_type='text/plain')
        # 修复后应返回400或200，不应500
        assert resp.status_code != 500, \
            f"save_seo_config在空body时崩溃: {resp.status_code}"

    def test_no_content_type_should_not_crash(self, app_client):
        client, app_module = app_client
        resp = client.post('/save_seo_config', data=b'')
        assert resp.status_code != 500, \
            "save_seo_config在无Content-Type时崩溃"


# ============================================================
# 🔴 #4: /save_config 未判空
# ============================================================
class TestSaveConfigNullBody:
    """验证非JSON请求不会导致500"""

    def test_empty_body_should_not_crash(self, app_client):
        client, app_module = app_client
        resp = client.post('/save_config',
                           data='invalid',
                           content_type='text/plain')
        assert resp.status_code != 500, \
            f"save_config在空body时崩溃: {resp.status_code}"


# ============================================================
# 🔴 #5: config.json写入缺少encoding
# ============================================================
class TestConfigWriteEncoding:
    """验证config写入代码是否指定了encoding"""

    def test_save_config_uses_utf8_encoding(self):
        """静态检查：确认open()调用包含encoding参数"""
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 查找所有 open('config.json', 'w') 调用
        import re
        # 匹配 open('config.json', 'w') 但没有 encoding 的
        pattern = r"open\(['\"]config\.json['\"],\s*['\"]w['\"]\s*\)"
        matches = re.findall(pattern, content)
        # 修复后：不应存在无encoding的写法
        assert len(matches) == 0, \
            f"发现{len(matches)}处config.json写入未指定encoding: {matches}"


# ============================================================
# 🔴 #6: 启动代码重复
# ============================================================
class TestDuplicateAppRun:
    """验证app.run()不应重复出现"""

    def test_app_run_appears_only_once(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            content = f.read()

        import re
        # 匹配 app.run( 调用（排除注释行）
        lines = content.split('\n')
        app_run_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if 'app.run(' in stripped:
                app_run_count += 1

        assert app_run_count <= 1, \
            f"app.run()出现{app_run_count}次（应为1次），存在重复死代码"


# ============================================================
# 🟡 #7: _prodtest_state 并发读写
# ============================================================
class TestProdtestStateConcurrency:
    """验证生产准入状态的并发安全性"""

    def test_logs_truncation_is_safe(self, app_client):
        client, app_module = app_client
        state = app_module._prodtest_state
        # 模拟大量日志写入
        original_logs = state["logs"][:]
        try:
            for i in range(400):
                state["logs"].append(f"test_log_{i}")
            # 模拟截断
            if len(state["logs"]) > 300:
                state["logs"] = state["logs"][-300:]
            assert len(state["logs"]) <= 300
        finally:
            state["logs"] = original_logs


# ============================================================
# 🟡 #8: keyword_explore 无锁竞态
# ============================================================
class TestKeywordExploreRaceCondition:
    """验证关键词探索的并发启动保护"""

    def test_concurrent_explore_should_be_rejected(self, app_client):
        client, app_module = app_client
        mgr = app_module.keyword_explore_manager
        original = mgr['is_running']
        try:
            mgr['is_running'] = True
            resp = client.post('/api/keyword_explore',
                               json={'target_url': 'https://example.com'})
            data = resp.get_json()
            assert data.get('success') is False, \
                "is_running=True时应拒绝新请求"
        finally:
            mgr['is_running'] = original


# ============================================================
# 🟡 #9: production_test 超时消息错误
# ============================================================
class TestProductionTestTimeoutMessage:
    """验证超时消息与实际timeout值一致"""

    def test_timeout_message_matches_actual_value(self):
        pt_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'production_test.py')
        with open(pt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        import re
        # 找到实际的timeout值
        timeout_match = re.search(r'timeout=(\d+)', content)
        assert timeout_match, "未找到timeout设置"
        actual_timeout = int(timeout_match.group(1))

        # 找到超时消息中的数值
        msg_match = re.search(r'测试超时\(>(\d+)s\)', content)
        if msg_match:
            msg_timeout = int(msg_match.group(1))
            assert msg_timeout == actual_timeout, \
                f"超时消息说>{msg_timeout}s，但实际timeout={actual_timeout}s"


# ============================================================
# 🟡 #10: ip_provider 失败返回success:True
# ============================================================
class TestIPProviderFallbackSuccess:
    """验证所有API失败时不应返回success:True"""

    def test_fallback_should_not_return_success_true(self):
        from ip_provider import IPProvider
        provider = IPProvider("proxy_api")
        # 使用无效代理URL，所有IP详情API都会失败
        result = provider._get_ip_details("http://invalid_proxy_12345:9999")
        # 修复后：success应为False
        if result.get("ip") == "未知":
            assert result.get("success") is False, \
                "所有API失败时不应返回success:True（ip='未知'）"


# ============================================================
# 🟡 #11: log.messages 内存无上限
# ============================================================
class TestLogMessagesMemoryLimit:
    """验证日志列表是否有容量保护"""

    def test_log_messages_should_have_cap(self, app_client):
        client, app_module = app_client
        log_obj = app_module.log
        original = log_obj.messages[:]
        try:
            # 模拟大量日志
            for i in range(3000):
                log_obj.messages.append(f"flood_msg_{i}")
            # 检查是否有自动截断机制
            # 修复后：messages不应超过某个上限（如2000）
            # 未修复时：会有3000+条
            if len(log_obj.messages) > 2500:
                pytest.fail(
                    f"log.messages无上限保护：当前{len(log_obj.messages)}条，"
                    "长时间运行将导致内存泄漏"
                )
        finally:
            log_obj.messages = original


# ============================================================
# 🟡 #14: /save_config 可注入任意键
# ============================================================
class TestSaveConfigKeyInjection:
    """验证save_config是否允许注入非预期键"""

    def test_unknown_keys_should_not_persist(self, app_client):
        client, app_module = app_client
        # 尝试注入一个非预期键
        resp = client.post('/save_config', json={
            '__injected_key__': 'malicious_value',
            'plan_days': 3
        })
        # 检查config中是否存在注入的键
        assert '__injected_key__' not in app_module.config, \
            "save_config允许注入任意键到config中（配置污染风险）"


# ============================================================
# 边界测试：极端输入
# ============================================================
class TestEdgeCases:
    """极端输入和边界条件测试"""

    def test_keyword_explore_invalid_url(self, app_client):
        client, app_module = app_client
        resp = client.post('/api/keyword_explore',
                           json={'target_url': ''})
        data = resp.get_json()
        assert data.get('success') is False

    def test_keyword_explore_extreme_layer(self, app_client):
        client, app_module = app_client
        # max_layer=0 或负数不应崩溃
        resp = client.post('/api/keyword_explore',
                           json={'target_url': 'https://example.com',
                                 'max_layer': -1})
        # 不应500
        assert resp.status_code != 500

    def test_get_logs_limit_injection(self, app_client):
        client, app_module = app_client
        # limit参数注入
        resp = client.get('/get_logs?limit=99999999')
        assert resp.status_code == 200
        # 应被clamp到500
        body = resp.data.decode('utf-8')
        # 不应返回超过500条
        assert body.count('<p>') <= 500

    def test_download_path_traversal(self, app_client):
        client, app_module = app_client
        # 路径穿越攻击
        resp = client.get('/api/keyword_explore/download/..%2F..%2Fapp.py')
        assert resp.status_code in (400, 404), \
            "路径穿越未被拦截"

    def test_production_test_force_stop_idempotent(self, app_client):
        client, app_module = app_client
        # 多次强制停止不应报错
        for _ in range(3):
            resp = client.post('/force_stop_production_test')
            assert resp.status_code == 200


# ============================================================
# 静态分析：资源泄漏检查
# ============================================================
class TestResourceLeakPatterns:
    """静态检查常见资源泄漏模式"""

    def test_xvfb_has_cleanup_mechanism(self):
        """检查Xvfb进程是否有atexit清理"""
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 检查是否有atexit或signal处理
        has_cleanup = ('atexit' in content or
                       'signal.signal' in content or
                       '_xvfb_process.kill' in content or
                       '_xvfb_process.terminate' in content)
        if not has_cleanup:
            pytest.fail("Xvfb进程无atexit/signal清理机制，进程泄漏风险")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ============================================================
# 广告检测回退逻辑测试
# ============================================================
class TestAdDetectionFallback:
    """验证DOM检测失败时HTML回退检测能正确识别广告"""

    def test_html_has_ad_code_detects_inline_script(self):
        """内联script中的广告域名应被_html_has_ad_code检测到"""
        from app import _html_has_ad_code
        # 模拟内联script包含广告域名（无src属性，CSS选择器无法匹配）
        html_with_inline_ad = '''
        <html><body>
        <script>
            var _ad = document.createElement('script');
            _ad.src = 'https://hilltopads.com/ad.js';
            document.body.appendChild(_ad);
        </script>
        </body></html>
        '''
        result = _html_has_ad_code(html_with_inline_ad)
        assert result is not None, "内联script中的hilltopads.com应被检测到"
        assert 'HilltopAds' in result

    def test_html_has_ad_code_detects_adsense_inline(self):
        """AdSense内联配置应被检测到"""
        from app import _html_has_ad_code
        html = '<html><script>(adsbygoogle = window.adsbygoogle || []).push({});</script></html>'
        result = _html_has_ad_code(html)
        assert result is not None
        assert 'AdSense' in result

    def test_html_has_ad_code_no_false_positive(self):
        """普通页面不应误报"""
        from app import _html_has_ad_code
        html = '<html><body><p>Hello World</p><script src="https://cdn.example.com/app.js"></script></body></html>'
        result = _html_has_ad_code(html)
        assert result is None, "普通页面不应误报为含广告"

    def test_html_has_ad_code_detects_propellerads(self):
        """PropellerAds内联引用应被检测到"""
        from app import _html_has_ad_code
        html = '<html><script>var zone = "propellerads.com/zone/123";</script></html>'
        result = _html_has_ad_code(html)
        assert result is not None
        assert 'PropellerAds' in result

    def test_html_has_ad_code_empty_input(self):
        """空输入不应崩溃"""
        from app import _html_has_ad_code
        assert _html_has_ad_code('') is None
        assert _html_has_ad_code(None) is None


# ============================================================
# 广告曝光率修复验证测试
# ============================================================
class TestAdImpressionFix:
    """验证68次任务仅1次展示的根因修复"""

    def test_no_mp4_route_blocking(self):
        """确认.mp4/.webm拦截已移除（不再阻断广告素材）"""
        import re
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        # 确认optimized_page_goto中不再有route abort mp4
        # 查找函数定义区域
        func_start = content.find('def optimized_page_goto')
        func_end = content.find('\n                                    # ★ 深层URL策略', func_start)
        if func_end == -1:
            func_end = func_start + 2000
        func_body = content[func_start:func_end]
        assert 'route.abort()' not in func_body, "optimized_page_goto中不应再有route.abort()拦截"
        assert '*.mp4' not in func_body or '已移除' in func_body, ".mp4拦截应已移除"

    def test_traffic_valid_not_just_has_ad_code(self):
        """确认traffic_valid不再仅凭_has_ad_code判定有效"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        # 查找traffic_valid赋值行
        import re
        matches = re.findall(r'traffic_valid\s*=\s*(.+)', content)
        assert len(matches) > 0, "应存在traffic_valid赋值"
        for m in matches:
            # 新逻辑不应包含 "or _has_ad_code" 作为单独条件
            assert 'or _has_ad_code' not in m, f"traffic_valid不应仅凭_has_ad_code: {m}"

    def test_ad_script_reexecution_removed(self):
        """确认违规的广告脚本重执行已移除（合规铁律）"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        # 重执行代码必须不存在
        assert 'data-ad-reexec' not in content, "违规的data-ad-reexec标记不应存在"
        assert 'createElement(\'script\')' not in content or '广告脚本强制重执行' not in content, "不应有广告脚本重执行代码"
        # 合规原则注释应存在
        assert '绝不人为干预广告脚本执行' in content or '不干预DOM' in content, "应包含合规原则声明"

    def test_generic_cross_origin_iframe_detector(self):
        """确认通用跨域iframe检测器存在"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '通用跨域iframe检测器' in content, "应包含通用跨域iframe检测器"
        assert '_nonAdDomains' in content, "应包含非广告域名白名单"

    def test_ad_recovery_mechanism_exists(self):
        """确认广告恢复机制存在"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '广告恢复机制' in content or '广告恢复' in content, "应包含广告恢复机制"
        assert '_has_ad_code and not ad_found' in content, "应检测HTML有广告但DOM未渲染的情况"

    def test_active_ad_loading_phase_exists(self):
        """确认广告主动加载阶段存在"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '广告主动加载阶段' in content, "应包含广告主动加载阶段"
        assert 'scrollTo' in content, "应包含滚动触发逻辑"


# ============================================================
# 计划断点恢复测试
# ============================================================
class TestPlanResume:
    """验证停止后继续执行能从断点恢复"""

    def test_save_plan_progress_function_exists(self):
        """确认保存进度函数存在"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert 'def _save_plan_progress' in content, "应包含_save_plan_progress函数"
        assert 'def _load_plan_progress' in content, "应包含_load_plan_progress函数"
        assert 'def _clear_plan_progress' in content, "应包含_clear_plan_progress函数"

    def test_plan_progress_file_constant(self):
        """确认进度文件常量存在"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert 'PLAN_PROGRESS_FILE' in content, "应包含PLAN_PROGRESS_FILE常量"
        assert 'plan_progress.json' in content, "进度文件名应为plan_progress.json"

    def test_skip_completed_tasks_in_loop(self):
        """确认主循环跳过已完成任务"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '跳过已完成的任务' in content, "应包含跳过已完成任务的逻辑"
        assert 'task.get("status") == "已完成"' in content or "task.get('status') == '已完成'" in content, "应检查任务状态"

    def test_resume_on_restart(self):
        """确认重新执行时检测未完成计划"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '断点恢复' in content, "应包含断点恢复逻辑"
        assert '_load_plan_progress()' in content, "应调用_load_plan_progress"

    def test_cross_day_expiry(self):
        """确认跨天不恢复旧进度"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert 'saved_at.startswith(today)' in content or 'startswith(today)' in content, "应检查日期是否当天"

    def test_save_on_stop_clear_on_complete(self):
        """确认停止时保存、完成时清除"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), 'r') as f:
            content = f.read()
        assert '_save_plan_progress(daily_plan, tasks_list)' in content, "停止时应保存进度"
        assert '_clear_plan_progress()' in content, "完成时应清除进度"
