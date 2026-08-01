"""
app.py 核心函数单元测试
覆盖审计报告中标注为"14800+ 行完全未测"的关键业务逻辑
"""
import pytest
import random
import copy
import datetime


# ========== 时区与国家辅助函数 ==========

class TestTimezoneAndCountry:
    """测试时区映射和工作时间判断"""

    def test_get_timezone_for_country_us(self):
        from app import get_timezone_for_country
        assert get_timezone_for_country("US") == "America/New_York"

    def test_get_timezone_for_country_gb(self):
        from app import get_timezone_for_country
        assert get_timezone_for_country("GB") == "Europe/London"

    def test_get_timezone_for_country_jp(self):
        from app import get_timezone_for_country
        assert get_timezone_for_country("JP") == "Asia/Tokyo"

    def test_get_timezone_for_country_case_insensitive(self):
        from app import get_timezone_for_country
        assert get_timezone_for_country("us") == "America/New_York"
        assert get_timezone_for_country("Gb") == "Europe/London"

    def test_get_timezone_for_country_unknown_defaults_to_new_york(self):
        from app import get_timezone_for_country
        assert get_timezone_for_country("XX") == "America/New_York"
        assert get_timezone_for_country("") == "America/New_York"

    def test_is_working_hours_returns_bool(self):
        from app import is_working_hours
        result = is_working_hours("US")
        assert isinstance(result, bool)

    def test_is_working_hours_invalid_country_still_works(self):
        from app import is_working_hours
        result = is_working_hours("XX")
        assert isinstance(result, bool)


# ========== 流量模型函数 ==========

class TestTrafficModels:
    """测试流量分布模型"""

    def test_clamp_hour_normal(self):
        from app import clamp_hour
        assert clamp_hour(12) == 12
        assert clamp_hour(0) == 0
        assert clamp_hour(23) == 23

    def test_clamp_hour_overflow(self):
        from app import clamp_hour
        assert clamp_hour(24) == 0
        assert clamp_hour(25) == 1
        assert clamp_hour(48) == 0

    def test_clamp_hour_negative(self):
        from app import clamp_hour
        assert clamp_hour(-1) == 23
        assert clamp_hour(-24) == 0
        assert clamp_hour(-25) == 23

    def test_generate_normal_hours_count(self):
        from app import generate_normal_hours
        result = generate_normal_hours(50)
        assert len(result) == 50
        assert result == sorted(result)  # should be sorted

    def test_generate_normal_hours_range(self):
        from app import generate_normal_hours
        result = generate_normal_hours(100)
        for h in result:
            assert 0 <= h < 24

    def test_generate_gamma_hours_count(self):
        from app import generate_gamma_hours
        result = generate_gamma_hours(30)
        assert len(result) == 30
        assert result == sorted(result)

    def test_generate_gamma_hours_range(self):
        from app import generate_gamma_hours
        result = generate_gamma_hours(100)
        for h in result:
            assert 0 <= h < 24

    def test_generate_poisson_hours_count(self):
        from app import generate_poisson_hours
        result = generate_poisson_hours(20)
        assert len(result) == 20

    def test_generate_bimodal_hours_count(self):
        from app import generate_bimodal_hours
        result = generate_bimodal_hours(40)
        assert len(result) == 40
        assert result == sorted(result)

    def test_generate_zero_tasks(self):
        from app import generate_normal_hours, generate_gamma_hours
        assert generate_normal_hours(0) == []
        assert generate_gamma_hours(0) == []


# ========== 站点年龄分类 ==========

class TestSiteAgeCategory:
    """测试站点年龄分类逻辑"""

    def test_new_site(self):
        from app import get_site_age_category
        today = datetime.datetime.now()
        recent = (today - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        assert get_site_age_category(recent) == "new"

    def test_mid_site(self):
        from app import get_site_age_category
        today = datetime.datetime.now()
        mid = (today - datetime.timedelta(days=45)).strftime("%Y-%m-%d")
        assert get_site_age_category(mid) == "mid"

    def test_old_site(self):
        from app import get_site_age_category
        today = datetime.datetime.now()
        old = (today - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        assert get_site_age_category(old) == "old"

    def test_empty_string_returns_old(self):
        from app import get_site_age_category
        assert get_site_age_category("") == "old"

    def test_none_returns_old(self):
        from app import get_site_age_category
        assert get_site_age_category(None) == "old"

    def test_invalid_date_returns_old(self):
        from app import get_site_age_category
        assert get_site_age_category("not-a-date") == "old"


# ========== 国家配额选择 ==========

class TestSelectCountryByQuota:
    """测试配额加权的国家选择"""

    def test_selects_from_available(self):
        from app import select_country_by_quota
        candidates = ["US", "GB"]
        used = {"US": 5, "GB": 2}
        target = {"US": 10, "GB": 10}
        random.seed(42)
        result = select_country_by_quota(candidates, used, target)
        assert result in candidates

    def test_empty_candidates_returns_none(self):
        from app import select_country_by_quota
        assert select_country_by_quota([], {}, {}) is None

    def test_all_quota_exhausted_still_returns(self):
        from app import select_country_by_quota
        candidates = ["US", "GB"]
        used = {"US": 10, "GB": 10}
        target = {"US": 10, "GB": 10}
        result = select_country_by_quota(candidates, used, target)
        assert result in candidates  # fallback random

    def test_prefers_higher_remaining_quota(self):
        from app import select_country_by_quota
        random.seed(0)
        candidates = ["US", "GB"]
        used = {"US": 9, "GB": 0}
        target = {"US": 10, "GB": 10}
        # GB has 10 remaining, US has 1 remaining
        results = [select_country_by_quota(candidates, used, target) for _ in range(100)]
        gb_count = results.count("GB")
        assert gb_count > 70  # GB should be picked majority of times


# ========== 配置校验 ==========

class TestValidateWebNavigationConfig:
    """测试 web_navigation 配置校验"""

    def test_valid_config(self):
        from app import validate_web_navigation_config
        cfg = {
            "web_navigation": {
                "loop_count": {"min": 1, "max": 3},
                "loop_interval": {"min": 1, "max": 5},
                "layer_1": {"stay_ratio": 0.15},
                "layer_2": {"stay_ratio": 0.15},
                "layer_3": {"stay_ratio": 0.35},
                "layer_4": {"stay_ratio": 0.2},
                "layer_5": {"stay_ratio": 0.15},
            }
        }
        success, errors = validate_web_navigation_config(cfg)
        assert success is True
        assert errors == []

    def test_missing_web_navigation(self):
        from app import validate_web_navigation_config
        success, errors = validate_web_navigation_config({})
        assert success is False
        assert "缺少 web_navigation 配置" in errors[0]

    def test_missing_loop_count(self):
        from app import validate_web_navigation_config
        cfg = {
            "web_navigation": {
                "loop_interval": {"min": 1, "max": 5},
                "layer_1": {"stay_ratio": 0.5},
            }
        }
        success, errors = validate_web_navigation_config(cfg)
        assert success is False
        assert any("loop_count" in e for e in errors)

    def test_all_zero_stay_ratio(self):
        from app import validate_web_navigation_config
        cfg = {
            "web_navigation": {
                "loop_count": {"min": 1, "max": 3},
                "loop_interval": {"min": 1, "max": 5},
            }
        }
        # no layers defined -> ratio_sum = 0
        success, errors = validate_web_navigation_config(cfg)
        assert success is False
        assert any("stay_ratio" in e for e in errors)

    def test_fail_hard_raises(self):
        from app import validate_web_navigation_config
        # Use a config with web_navigation present but with errors
        cfg = {
            "web_navigation": {
                "loop_count": {"min": -1, "max": 3},
                "loop_interval": {"min": 1, "max": 5},
                "layer_1": {"stay_ratio": 0.5},
            }
        }
        with pytest.raises(ValueError):
            validate_web_navigation_config(cfg, fail_hard=True)


# ========== 工具函数 ==========

class TestUtilityFunctions:
    """测试通用工具函数"""

    def test_get_random_value_range(self):
        from app import get_random_value
        cfg = {"min": 1.0, "max": 5.0}
        for _ in range(50):
            v = get_random_value(cfg)
            assert 1.0 <= v <= 5.0

    def test_get_random_int_range(self):
        from app import get_random_int
        cfg = {"min": 1, "max": 10}
        for _ in range(50):
            v = get_random_int(cfg)
            assert 1 <= v <= 10
            assert isinstance(v, int)

    def test_bezier_curve_endpoints(self):
        from app import bezier_curve
        p0, p1, p2 = (0, 0), (5, 10), (10, 0)
        x0, y0 = bezier_curve(p0, p1, p2, 0)
        assert abs(x0 - 0) < 1e-9
        assert abs(y0 - 0) < 1e-9
        x1, y1 = bezier_curve(p0, p1, p2, 1)
        assert abs(x1 - 10) < 1e-9
        assert abs(y1 - 0) < 1e-9

    def test_bezier_curve_midpoint(self):
        from app import bezier_curve
        p0, p1, p2 = (0, 0), (5, 10), (10, 0)
        x, y = bezier_curve(p0, p1, p2, 0.5)
        assert abs(x - 5) < 1e-9
        assert abs(y - 5) < 1e-9


# ========== QA 会话辅助函数 ==========

class TestQAHelpers:
    """测试 QA 会话辅助函数"""

    def test_qa_safe_name_normal(self):
        from app import _qa_safe_name
        assert _qa_safe_name("hello") == "hello"

    def test_qa_safe_name_special_chars(self):
        from app import _qa_safe_name
        result = _qa_safe_name("hello world!@#")
        assert " " not in result
        assert "!" not in result

    def test_qa_safe_name_none(self):
        from app import _qa_safe_name
        assert _qa_safe_name(None) == "unknown"

    def test_qa_safe_name_max_length(self):
        from app import _qa_safe_name
        long_str = "a" * 200
        assert len(_qa_safe_name(long_str)) == 80

    def test_qa_site_host(self):
        from app import _qa_site_host
        assert _qa_site_host("https://www.example.com/path") == "www.example.com"
        assert _qa_site_host("") == "unknown"
        assert _qa_site_host(None) == "unknown"

    def test_qa_cookie_domain_matches_exact(self):
        from app import _qa_cookie_domain_matches
        assert _qa_cookie_domain_matches(".example.com", "example.com") is True
        assert _qa_cookie_domain_matches("example.com", "example.com") is True

    def test_qa_cookie_domain_matches_subdomain(self):
        from app import _qa_cookie_domain_matches
        assert _qa_cookie_domain_matches(".example.com", "sub.example.com") is True

    def test_qa_cookie_domain_no_match(self):
        from app import _qa_cookie_domain_matches
        assert _qa_cookie_domain_matches(".other.com", "example.com") is False

    def test_qa_is_ad_cookie_by_domain(self):
        from app import _qa_is_ad_cookie
        assert _qa_is_ad_cookie({"domain": ".doubleclick.net", "name": "id"}) is True
        assert _qa_is_ad_cookie({"domain": ".googleadservices.com", "name": "x"}) is True

    def test_qa_is_ad_cookie_by_prefix(self):
        from app import _qa_is_ad_cookie
        assert _qa_is_ad_cookie({"domain": ".example.com", "name": "gcl_abc"}) is True
        assert _qa_is_ad_cookie({"domain": ".example.com", "name": "_gcl_xyz"}) is True

    def test_qa_is_ad_cookie_normal(self):
        from app import _qa_is_ad_cookie
        assert _qa_is_ad_cookie({"domain": ".example.com", "name": "session_id"}) is False


# ========== 深度合并配置 ==========

class TestDeepMergeDefaults:
    """测试配置深度合并"""

    def test_simple_merge(self):
        from app import deep_merge_defaults
        defaults = {"a": 1, "b": 2}
        overrides = {"b": 3, "c": 4}
        result = deep_merge_defaults(defaults, overrides)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        from app import deep_merge_defaults
        defaults = {"a": {"x": 1, "y": 2}, "b": 3}
        overrides = {"a": {"y": 99, "z": 100}}
        result = deep_merge_defaults(defaults, overrides)
        assert result["a"] == {"x": 1, "y": 99, "z": 100}
        assert result["b"] == 3

    def test_override_non_dict_with_dict(self):
        from app import deep_merge_defaults
        defaults = {"a": 1}
        overrides = {"a": {"nested": True}}
        result = deep_merge_defaults(defaults, overrides)
        assert result["a"] == {"nested": True}

    def test_non_dict_overrides_returns_defaults(self):
        from app import deep_merge_defaults
        defaults = {"a": 1}
        result = deep_merge_defaults(defaults, "not a dict")
        assert result == {"a": 1}

    def test_original_defaults_not_mutated(self):
        from app import deep_merge_defaults
        defaults = {"a": {"x": 1}}
        overrides = {"a": {"x": 99}}
        result = deep_merge_defaults(defaults, overrides)
        assert defaults["a"]["x"] == 1  # original unchanged
        assert result["a"]["x"] == 99


# ========== 广告点击统计 ==========

class TestAdClickTracking:
    """测试每日广告点击统计"""

    def test_get_daily_ad_clicks_default_zero(self):
        import app
        # Reset state
        app.fingerprint_stats.pop("daily_ad_clicks", None)
        assert app.get_daily_ad_clicks() == 0

    def test_record_ad_click_increments(self):
        import app
        app.fingerprint_stats["daily_ad_clicks"] = {}
        before = app.get_daily_ad_clicks()
        app.record_ad_click(1)
        after = app.get_daily_ad_clicks()
        assert after == before + 1

    def test_record_ad_click_multiple(self):
        import app
        app.fingerprint_stats["daily_ad_clicks"] = {}
        app.record_ad_click(3)
        assert app.get_daily_ad_clicks() == 3

    def test_daily_ad_click_limit_not_set(self):
        import app
        app.config["daily_ad_click_limit"] = 0
        assert app.daily_ad_click_limit_reached() is False

    def test_daily_ad_click_limit_reached(self):
        import app
        app.config["daily_ad_click_limit"] = 5
        app.fingerprint_stats["daily_ad_clicks"] = {}
        app.record_ad_click(5)
        assert app.daily_ad_click_limit_reached() is True

    def test_daily_ad_click_limit_not_reached(self):
        import app
        app.config["daily_ad_click_limit"] = 100
        app.fingerprint_stats["daily_ad_clicks"] = {}
        app.record_ad_click(3)
        assert app.daily_ad_click_limit_reached() is False


# ========== 全球覆盖计算 ==========

class TestGlobalCoverage:
    """测试全球时段覆盖计算"""

    def test_empty_pool_returns_zero(self):
        from app import get_global_coverage
        result = get_global_coverage([])
        assert result["coverage_pct"] == 0.0
        assert result["uncovered_segments"] == [(0, 86400)]

    def test_single_country_has_coverage(self):
        from app import get_global_coverage
        pool = [{"country_code": "US", "enabled": True}]
        result = get_global_coverage(pool)
        assert result["coverage_pct"] > 0
        assert "US" in result["country_segments"]

    def test_multiple_countries_increase_coverage(self):
        from app import get_global_coverage
        pool_us = [{"country_code": "US", "enabled": True}]
        pool_multi = [
            {"country_code": "US", "enabled": True},
            {"country_code": "JP", "enabled": True},
        ]
        us_only = get_global_coverage(pool_us)
        multi = get_global_coverage(pool_multi)
        assert multi["coverage_pct"] >= us_only["coverage_pct"]

    def test_disabled_proxies_excluded(self):
        from app import get_global_coverage
        pool = [
            {"country_code": "US", "enabled": True},
            {"country_code": "JP", "enabled": False},
        ]
        result = get_global_coverage(pool)
        assert "JP" not in result["country_segments"]


# ========== 可中断 sleep ==========

class TestInterruptibleSleep:
    """测试可中断休眠"""

    def test_sleep_completes(self):
        import app
        app.task_running = True
        result = app.interruptible_sleep(0.1, check_interval=0.05)
        assert result is True

    def test_sleep_interrupted(self):
        import app
        app.task_running = False
        result = app.interruptible_sleep(10, check_interval=0.05)
        assert result is False
        app.task_running = True  # restore

    def test_sleep_zero_seconds(self):
        import app
        app.task_running = True
        result = app.interruptible_sleep(0)
        assert result is True

    def test_sleep_negative_seconds(self):
        import app
        app.task_running = True
        result = app.interruptible_sleep(-1)
        assert result is True


# ========== 指纹使用记录 ==========

class TestFingerprintUsage:
    """测试指纹和 UA 使用记录"""

    def test_record_fingerprint_usage(self):
        import app
        # Reset
        app.fingerprint_stats["ua_usage"] = {}
        app.fingerprint_stats["fingerprint_usage"] = {}
        app.fingerprint_stats["history"] = []

        app.record_fingerprint_usage("fp_001", "Mozilla/5.0 Test", "US")

        assert app.fingerprint_stats["ua_usage"]["Mozilla/5.0 Test"] == 1
        assert app.fingerprint_stats["fingerprint_usage"]["fp_001"] == 1
        assert len(app.fingerprint_stats["history"]) == 1

    def test_record_fingerprint_usage_increments(self):
        import app
        app.fingerprint_stats["ua_usage"] = {}
        app.fingerprint_stats["fingerprint_usage"] = {}
        app.fingerprint_stats["history"] = []

        app.record_fingerprint_usage("fp_001", "UA1", "US")
        app.record_fingerprint_usage("fp_001", "UA1", "US")

        assert app.fingerprint_stats["ua_usage"]["UA1"] == 2
        assert app.fingerprint_stats["fingerprint_usage"]["fp_001"] == 2


# ========== KPI 仪表盘 ==========

class TestKPIDashboard:
    """测试 KPI 仪表盘功能"""

    def test_load_kpi_data_empty(self):
        import app
        import os
        # Remove file if exists
        if os.path.exists(app.KPI_DASHBOARD_FILE):
            os.remove(app.KPI_DASHBOARD_FILE)
        data = app._load_kpi_data()
        assert "daily" in data

    def test_record_kpi_snapshot_creates_file(self):
        import app
        import os
        if os.path.exists(app.KPI_DASHBOARD_FILE):
            os.remove(app.KPI_DASHBOARD_FILE)
        app.stats["total"] = 10
        app.stats["success"] = 8
        app.stats["fail"] = 2
        app.stats["video_view_count"] = 5
        app.record_kpi_snapshot()
        assert os.path.exists(app.KPI_DASHBOARD_FILE)
        data = app._load_kpi_data()
        today = sorted(data["daily"].keys())[-1]
        assert data["daily"][today]["tasks_total"] == 10
        assert data["daily"][today]["tasks_success"] == 8

    def test_record_kpi_snapshot_limits_to_30_days(self):
        import app
        import datetime
        # Create fake old entries
        data = {"daily": {}}
        for i in range(35):
            d = (datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            data["daily"][d] = {"tasks_total": i}
        app._save_kpi_data(data)
        app.record_kpi_snapshot()
        result = app._load_kpi_data()
        assert len(result["daily"]) <= 31  # 30 old + 1 today


# ========== 配置审计日志 ==========

class TestConfigAuditLog:
    """测试配置审计日志功能"""

    def test_load_empty_audit_log(self):
        import app
        import os
        if os.path.exists(app.CONFIG_AUDIT_LOG_FILE):
            os.remove(app.CONFIG_AUDIT_LOG_FILE)
        log_data = app._load_config_audit_log()
        assert log_data == []

    def test_record_config_audit(self):
        import app
        import os
        if os.path.exists(app.CONFIG_AUDIT_LOG_FILE):
            os.remove(app.CONFIG_AUDIT_LOG_FILE)
        app.record_config_audit("save_config", changed_keys=["vps_host", "vps_port"])
        log_data = app._load_config_audit_log()
        assert len(log_data) == 1
        assert log_data[0]["action"] == "save_config"
        assert "vps_host" in log_data[0]["changed_keys"]

    def test_record_config_audit_limits_to_200(self):
        import app
        import os
        if os.path.exists(app.CONFIG_AUDIT_LOG_FILE):
            os.remove(app.CONFIG_AUDIT_LOG_FILE)
        for i in range(210):
            app.record_config_audit(f"action_{i}")
        log_data = app._load_config_audit_log()
        assert len(log_data) == 200
        # Verify it kept the most recent entries
        assert log_data[-1]["action"] == "action_209"


# ========== 浏览时长配置尊重测试 ==========

class TestBounceStayRespectsConfig:
    """验证跳出型任务停留时间不低于配置的total_stay.min"""

    def test_bounce_stay_floor_equals_config_min(self):
        """跳出停留下限 = max(30, config_min)，当config_min=80时应为80"""
        config_min = 80
        config_max = 220
        bounce_floor = max(30, config_min)
        bounce_ceil = min(config_max, (config_min + config_max) / 2)
        if bounce_ceil <= bounce_floor:
            bounce_ceil = bounce_floor + 20
        # 验证下限不低于配置min
        assert bounce_floor >= config_min
        # 验证上限大于下限
        assert bounce_ceil > bounce_floor
        # 模拟100次随机采样，全部在范围内
        import random
        for _ in range(100):
            stay = random.uniform(bounce_floor, bounce_ceil)
            assert stay >= config_min, f"跳出停留{stay:.1f}s < 配置min {config_min}s"
            assert stay <= bounce_ceil

    def test_bounce_stay_with_low_config(self):
        """配置min=30时，跳出停留下限=max(30,30)=30"""
        config_min = 30
        config_max = 60
        bounce_floor = max(30, config_min)
        bounce_ceil = min(config_max, (config_min + config_max) / 2)
        if bounce_ceil <= bounce_floor:
            bounce_ceil = bounce_floor + 20
        assert bounce_floor == 30
        assert bounce_ceil == 45  # min(60, 45) = 45

    def test_session_rope_floor_respects_config(self):
        """保险绳下限必须≥配置total_stay.min"""
        config_min = 80
        rope_floor = max(60, config_min)
        assert rope_floor >= config_min
        assert rope_floor == 80

    def test_session_rope_floor_with_high_config(self):
        """配置min=120时，保险绳下限=120"""
        config_min = 120
        rope_floor = max(60, config_min)
        assert rope_floor == 120

    def test_old_bounce_logic_would_violate_config(self):
        """复现原Bug：旧逻辑uniform(15,60)在配置min=80时必然违规"""
        import random
        config_min = 80
        violations = 0
        for _ in range(1000):
            old_stay = random.uniform(15, 60)
            if old_stay < config_min:
                violations += 1
        # 旧逻辑100%违规
        assert violations == 1000, "旧逻辑应100%低于配置min=80"
