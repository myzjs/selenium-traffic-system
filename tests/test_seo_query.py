"""
SEO 查询模块测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
import json


class TestSEOConfigValidator:
    """SEO 配置校验器测试"""

    def test_import(self):
        import seo_query_module
        assert seo_query_module is not None

    def test_validator_init(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.errors == []
        assert v.warnings == []

    def test_reset(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        v.add_error("e")
        v.reset()
        assert v.errors == []

    def test_validate_empty_engines(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_search_engines([]) is False
        assert len(v.errors) > 0

    def test_validate_none_engines(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_search_engines(None) is False

    def test_validate_valid_engines(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        engines = [
            {"id": "google", "name": "G", "url": "https://g.com", "language": "en"},
            {"id": "baidu", "name": "B", "url": "https://b.com", "language": "zh"},
        ]
        assert v.validate_search_engines(engines) is True

    def test_validate_duplicate_ids(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        engines = [
            {"id": "google", "name": "G", "url": "https://g.com", "language": "en"},
            {"id": "google", "name": "G2", "url": "https://g2.com", "language": "en"},
        ]
        assert v.validate_search_engines(engines) is False

    def test_validate_missing_fields(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_search_engines([{"id": "g"}]) is False

    def test_validate_region_map_empty(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_region_engine_map({}, []) is False

    def test_validate_region_map_valid(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        engines = [{"id": "g", "name": "G", "url": "https://g.com", "language": "en"}]
        assert v.validate_region_engine_map({"US": ["g"]}, engines) is True

    def test_validate_region_map_missing_engine(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        engines = [{"id": "g", "name": "G", "url": "https://g.com", "language": "en"}]
        # 缺失引擎只产生警告，不阻止通过
        result = v.validate_region_engine_map({"US": ["g", "x"]}, engines)
        assert result is True  # 缺失引擎是警告不是错误
        assert len(v.warnings) > 0

    def test_validate_keyword_pools_empty(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_keyword_pools({}) is False

    def test_validate_keyword_pools_valid(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        assert v.validate_keyword_pools({"zh": ["a"], "en": ["b"]}) is True

    def test_validate_keyword_pools_empty_list(self):
        from seo_query_module import SEOConfigValidator
        v = SEOConfigValidator()
        # 空关键词列表是警告不是错误
        result = v.validate_keyword_pools({"zh": []})
        assert result is True
        assert len(v.warnings) > 0


class TestSEOConfigQuery:
    """SEOConfigQuery 测试"""

    def test_init_with_defaults(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        assert q.config is not None
        assert "search_engines" in q.config

    def test_get_search_engines(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        engines = q.get_search_engines()
        assert isinstance(engines, list)
        assert len(engines) > 0

    def test_get_engine_by_id_found(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        engine = q.get_engine_by_id("google")
        assert engine is not None
        assert engine["id"] == "google"

    def test_get_engine_by_id_not_found(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        assert q.get_engine_by_id("nonexistent") is None

    def test_get_region_engine_map(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        region_map = q.get_region_engine_map()
        assert isinstance(region_map, dict)

    def test_get_engines_by_region_found(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        engines = q.get_engine_ids_for_region("美国")
        assert isinstance(engines, list)

    def test_get_engines_by_region_not_found(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        engines = q.get_engine_ids_for_region("火星")
        assert engines == []

    def test_get_random_keywords(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        keywords = q.get_keywords_by_language("en")
        assert isinstance(keywords, list)

    def test_get_random_keywords_invalid_lang(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        assert q.get_keywords_by_language("invalid") == []

    def test_build_search_url(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        url = q.get_engine_url("google")
        assert url is not None

    def test_build_search_url_unknown_engine(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        assert q.get_engine_url("nonexistent") is None

    def test_get_all_config(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        config = q.get_all_config()
        assert isinstance(config, dict)

    def test_reload_config(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        q.reload_config()  # 确保不抛异常
        assert True

    def test_get_keyword_pools(self):
        from seo_query_module import SEOConfigQuery
        q = SEOConfigQuery(config_file="/tmp/nonexistent_config.json")
        pools = q.get_keyword_pools()
        assert isinstance(pools, dict)


class TestModuleFunctions:
    """模块级函数测试"""

    def test_get_seo_query(self):
        from seo_query_module import get_seo_query
        q = get_seo_query(config_file="/tmp/nonexistent_config.json")
        assert q is not None
        assert hasattr(q, "get_search_engines")

    def test_reset_seo_query_instance(self):
        from seo_query_module import reset_seo_query_instance
        reset_seo_query_instance()  # 确保不抛异常
        assert True
