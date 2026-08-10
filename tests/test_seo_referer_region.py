"""
P1-9 Referer 地域化 测试
验证：国家 → 本地搜索引擎域名 映射、多语言关键词池、地域一致 referer 生成、随机性、向后兼容。
"""
import sys
import os
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from seo_query_module import (
    SEOConfigQuery,
    REGION_SEARCH_ENGINE_MAP,
    EXTENDED_KEYWORD_POOLS,
)


@pytest.fixture
def query():
    return SEOConfigQuery(config_file="/tmp/nonexistent_seo_region_config.json")


class TestRegionSearchEngineMap:
    """国家/地区 → 本地搜索引擎域名 映射"""

    def test_us_google(self):
        assert "google.com/search" in REGION_SEARCH_ENGINE_MAP["US"]

    def test_jp_local_domain(self):
        assert "google.co.jp" in REGION_SEARCH_ENGINE_MAP["JP"]

    def test_de_local_domain(self):
        assert "google.de" in REGION_SEARCH_ENGINE_MAP["DE"]

    def test_fr_local_domain(self):
        assert "google.fr" in REGION_SEARCH_ENGINE_MAP["FR"]

    def test_gb_local_domain(self):
        assert "google.co.uk" in REGION_SEARCH_ENGINE_MAP["GB"]

    def test_cn_baidu(self):
        assert "baidu.com" in REGION_SEARCH_ENGINE_MAP["CN"]

    def test_kr_naver(self):
        assert "naver" in REGION_SEARCH_ENGINE_MAP["KR"]

    def test_ru_yandex(self):
        assert "yandex" in REGION_SEARCH_ENGINE_MAP["RU"]


class TestMultilingualKeywordPools:
    """多语言关键词池覆盖面（每语言 >= 30，非空）"""

    @pytest.mark.parametrize("lang", ["de", "fr", "ja", "ko", "es", "it"])
    def test_extended_language_pool_present_and_size(self, lang):
        assert len(EXTENDED_KEYWORD_POOLS[lang]) >= 30, f"{lang} 关键词池不足 30 条"

    @pytest.mark.parametrize(
        "lang, sample",
        [
            ("de", "romane online lesen kostenlos"),
            ("fr", "romans fantastiques à lire"),
            ("ja", "小説 無料 オンライン 読む"),
            ("ko", "웹소설 무료 읽기"),
            ("es", "leer novelas gratis en línea"),
            ("it", "leggere romanzi gratis online"),
        ],
    )
    def test_extended_language_keywords_match(self, lang, sample):
        assert sample in EXTENDED_KEYWORD_POOLS[lang]

    def test_japanese_pool_no_latin_garbage(self, query):
        # 日文池应包含日文假名/汉字，而非纯英文乱码
        ja_kws = query._get_multilingual_keywords("ja")
        assert ja_kws
        # 至少存在含日文汉字的词
        assert any(any('\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff' for ch in kw) for kw in ja_kws)


class TestGenerateRefererForRegion:
    """generate_referer_for_region 地域一致 referer"""

    def test_jp_referer_uses_cojp_and_japanese_keyword(self, query):
        referer = query.generate_referer_for_region("JP", "ja")
        assert referer is not None
        assert "google.co.jp" in referer
        # 查询词应来自日文池（URL 解码后含日文，而非英文）
        decoded = urllib.parse.unquote(referer.split("q=", 1)[1])
        assert any('\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff' for ch in decoded)

    def test_de_referer_uses_google_de_and_german_keyword(self, query):
        referer = query.generate_referer_for_region("DE", "de")
        assert referer is not None
        assert "google.de" in referer
        decoded = urllib.parse.unquote(referer.split("q=", 1)[1])
        # 关键词应来自德文池（真实德文词），而非英文
        assert decoded in EXTENDED_KEYWORD_POOLS["de"]

    def test_cn_referer_uses_baidu(self, query):
        referer = query.generate_referer_for_region("CN", "zh")
        assert referer is not None
        assert "baidu.com" in referer

    def test_fr_referer_uses_google_fr(self, query):
        referer = query.generate_referer_for_region("FR", "fr")
        assert referer is not None
        assert "google.fr" in referer

    def test_unknown_country_falls_back_to_us_google(self, query):
        referer = query.generate_referer_for_region("XX", "en")
        assert referer is not None
        assert "google.com" in referer

    def test_explicit_keyword_used(self, query):
        referer = query.generate_referer_for_region("JP", "ja", keyword="カスタム キーワード")
        assert referer is not None
        assert "google.co.jp" in referer
        assert urllib.parse.quote("カスタム キーワード") in referer

    def test_keyword_random_distribution(self, query):
        # 同一国家多次调用，关键词应呈随机分布（不应总是同一个）
        seen = set()
        for _ in range(200):
            referer = query.generate_referer_for_region("JP", "ja")
            seen.add(referer)
        assert len(seen) > 1, "同一国家多次生成 referer 关键词应具有随机性"

    def test_unknown_language_returns_none(self, query):
        assert query.generate_referer_for_region("US", "xx_none_xx") is None

    def test_url_encoded(self, query):
        referer = query.generate_referer_for_region("US", "en")
        assert referer is not None
        # URL 中不应出现未编码空格
        assert " " not in referer


class TestBackwardCompatibility:
    """不传国家时，旧版函数行为保持不变（向后兼容）"""

    def test_get_random_engine_for_region_unchanged(self, query):
        engine_id = query.get_random_engine_for_region("US")
        assert engine_id in ["google", "bing", "facebook", "twitter", "reddit", "instagram"]

    def test_generate_referer_old_signature(self, query):
        referer = query.generate_referer("baidu", "测试关键词")
        assert referer is not None
        assert "baidu.com" in referer

    def test_generate_referer_old_random(self, query):
        referer = query.generate_referer("google")
        assert referer is not None
        assert "google.com" in referer

    def test_get_random_keyword_for_engine_unchanged(self, query):
        kw = query.get_random_keyword_for_engine("baidu")
        assert kw is not None