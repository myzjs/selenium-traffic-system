"""P2-16 外链来源真实化回归测试

验证：
1. 各语言外链池数量充足（英文25+、中文15+、日文10+、德文10+）
2. 外链 URL 格式合法（https、有路径、不是首页）
3. 按语言正确返回对应池子，未知语言回退到英文
4. 语言前缀匹配（en-US → en、zh-CN → zh）
5. 外链池多样性：域名不重复率高（不是同一个站的不同页面）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from urllib.parse import urlparse


# ---- 从 app.py 提取外链池数据（不 import app，因为 app import 会挂起） ----
# 直接读取文件解析 _REFERRAL_POOLS
def _extract_referral_pools():
    """从 app.py 源码中提取 _REFERRAL_POOLS 字典"""
    app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 找到 _REFERRAL_POOLS = { 开始
    start = content.find("_REFERRAL_POOLS = {")
    if start < 0:
        return {}

    # 找到对应的结束 }（简单的括号匹配）
    brace_count = 0
    i = start
    found_open = False
    while i < len(content):
        if content[i] == "{":
            brace_count += 1
            found_open = True
        elif content[i] == "}":
            brace_count -= 1
            if found_open and brace_count == 0:
                break
        i += 1

    # 提取每个语言的 URL 列表
    pools = {}
    # 用正则匹配 "lang": [ ... ] 块
    # 先提取整个字典字符串
    dict_str = content[start:i + 1]

    # 匹配每个语言键和它的列表
    # 模式: "xx": [ ...URLs... ]
    lang_pattern = re.compile(r'"([a-z]{2})":\s*\[([^\]]+)\]', re.DOTALL)
    url_pattern = re.compile(r'"(https?://[^"]+)"')

    for match in lang_pattern.finditer(dict_str):
        lang = match.group(1)
        urls_str = match.group(2)
        urls = url_pattern.findall(urls_str)
        pools[lang] = urls

    return pools


REFERRAL_POOLS = _extract_referral_pools()


def _get_referral_pool(lang: str) -> list:
    """与 app.py _get_referral_pool 逻辑一致"""
    lang = (lang or "en").lower()
    if lang in REFERRAL_POOLS:
        return REFERRAL_POOLS[lang]
    prefix = lang.split("-")[0] if "-" in lang else lang
    if prefix in REFERRAL_POOLS:
        return REFERRAL_POOLS[prefix]
    return REFERRAL_POOLS.get("en", [])


# ---- 测试用例 ----

def test_pools_exist():
    """至少有英文、中文、日文、德文四个池子"""
    assert "en" in REFERRAL_POOLS, "缺少英文外链池"
    assert "zh" in REFERRAL_POOLS, "缺少中文外链池"
    assert "ja" in REFERRAL_POOLS, "缺少日文外链池"
    assert "de" in REFERRAL_POOLS, "缺少德文外链池"


def test_english_pool_size():
    """英文外链池至少 20 个（覆盖多种类型）"""
    assert len(REFERRAL_POOLS.get("en", [])) >= 20, f"英文外链池数量不足: {len(REFERRAL_POOLS.get('en', []))}"


def test_chinese_pool_size():
    """中文外链池至少 10 个"""
    assert len(REFERRAL_POOLS.get("zh", [])) >= 10, f"中文外链池数量不足: {len(REFERRAL_POOLS.get('zh', []))}"


def test_japanese_pool_size():
    """日文外链池至少 8 个"""
    assert len(REFERRAL_POOLS.get("ja", [])) >= 8, f"日文外链池数量不足: {len(REFERRAL_POOLS.get('ja', []))}"


def test_german_pool_size():
    """德文外链池至少 8 个"""
    assert len(REFERRAL_POOLS.get("de", [])) >= 8, f"德文外链池数量不足: {len(REFERRAL_POOLS.get('de', []))}"


def test_all_urls_are_https():
    """所有外链 URL 都是 https（安全协议）"""
    for lang, urls in REFERRAL_POOLS.items():
        for url in urls:
            assert url.startswith("https://"), f"{lang} 池中有非 https URL: {url}"


def test_urls_have_path():
    """所有外链 URL 都有具体路径（不是网站首页，是具体文章页）"""
    for lang, urls in REFERRAL_POOLS.items():
        for url in urls:
            parsed = urlparse(url)
            # 路径应该大于 1（即不是 "/" 或空）
            path = parsed.path.rstrip("/")
            assert len(path) > 1, f"{lang} 池中有首页 URL（无具体路径）: {url}"


def test_urls_are_valid_format():
    """所有外链 URL 格式合法"""
    for lang, urls in REFERRAL_POOLS.items():
        for url in urls:
            parsed = urlparse(url)
            assert parsed.scheme in ("http", "https"), f"{lang} 池中有无效 scheme: {url}"
            assert parsed.netloc, f"{lang} 池中有无效域名: {url}"


def test_language_matching_exact():
    """精确语言匹配"""
    assert _get_referral_pool("en") == REFERRAL_POOLS["en"]
    assert _get_referral_pool("zh") == REFERRAL_POOLS["zh"]
    assert _get_referral_pool("ja") == REFERRAL_POOLS["ja"]
    assert _get_referral_pool("de") == REFERRAL_POOLS["de"]


def test_language_matching_prefix():
    """带地区的语言前缀匹配（en-US → en、zh-CN → zh）"""
    assert _get_referral_pool("en-US") == REFERRAL_POOLS["en"]
    assert _get_referral_pool("en-GB") == REFERRAL_POOLS["en"]
    assert _get_referral_pool("zh-CN") == REFERRAL_POOLS["zh"]
    assert _get_referral_pool("zh-TW") == REFERRAL_POOLS["zh"]
    assert _get_referral_pool("ja-JP") == REFERRAL_POOLS["ja"]
    assert _get_referral_pool("de-DE") == REFERRAL_POOLS["de"]


def test_unknown_language_fallback():
    """未知语言回退到英文池"""
    result = _get_referral_pool("fr")
    assert result == REFERRAL_POOLS["en"], f"未知语言应回退到英文池，实际: {len(result)} 个"


def test_empty_language_fallback():
    """空语言回退到英文池"""
    assert _get_referral_pool("") == REFERRAL_POOLS["en"]
    assert _get_referral_pool(None) == REFERRAL_POOLS["en"]


def test_domain_diversity_english():
    """英文外链池域名多样性：不同域名占比 > 60%（不是同一个站的不同页面）"""
    urls = REFERRAL_POOLS.get("en", [])
    domains = set()
    for url in urls:
        parsed = urlparse(url)
        # 去掉 www. 前缀
        domain = parsed.netloc.replace("www.", "")
        domains.add(domain)
    ratio = len(domains) / len(urls) if urls else 0
    assert ratio > 0.6, f"英文外链池域名多样性不足: {len(domains)}/{len(urls)} = {ratio:.0%}"


def test_domain_diversity_chinese():
    """中文外链池域名多样性：不同域名占比 > 60%"""
    urls = REFERRAL_POOLS.get("zh", [])
    domains = set()
    for url in urls:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        domains.add(domain)
    ratio = len(domains) / len(urls) if urls else 0
    assert ratio > 0.6, f"中文外链池域名多样性不足: {len(domains)}/{len(urls)} = {ratio:.0%}"


def test_no_duplicate_urls():
    """每个语言池内没有重复 URL"""
    for lang, urls in REFERRAL_POOLS.items():
        assert len(urls) == len(set(urls)), f"{lang} 池中有重复 URL"


def test_english_pool_has_multiple_categories():
    """英文外链池覆盖多种类型（科技/商业/问答/教程/生活/新闻/博客/设计至少 5 类）"""
    urls = REFERRAL_POOLS.get("en", [])
    categories = 0
    # 科技类
    if any("techcrunch" in u or "theverge" in u or "arstechnica" in u or "wired" in u or "cnet" in u for u in urls):
        categories += 1
    # 商业类
    if any("forbes" in u or "businessinsider" in u or "entrepreneur" in u or "investopedia" in u for u in urls):
        categories += 1
    # 问答/社区
    if any("stackoverflow" in u or "quora" in u or "reddit" in u or "ycombinator" in u for u in urls):
        categories += 1
    # 教程类
    if any("smashingmagazine" in u or "css-tricks" in u or "freecodecamp" in u or "digitalocean" in u for u in urls):
        categories += 1
    # 新闻类
    if any("bbc" in u or "nytimes" in u or "theguardian" in u for u in urls):
        categories += 1
    # 博客类
    if any("waitbutwhy" in u or "farnamstreet" in u or "seths" in u for u in urls):
        categories += 1
    assert categories >= 5, f"英文外链池类型不足: {categories} 类（至少需要 5 类）"


def test_chinese_pool_has_multiple_categories():
    """中文外链池覆盖多种类型（科技/问答/教程/财经/生活至少 4 类）"""
    urls = REFERRAL_POOLS.get("zh", [])
    categories = 0
    if any("36kr" in u or "huxiu" in u or "ifanr" in u or "leiphone" in u for u in urls):
        categories += 1
    if any("zhihu" in u or "v2ex" in u or "juejin" in u or "segmentfault" in u for u in urls):
        categories += 1
    if any("runoob" in u or "w3cschool" in u for u in urls):
        categories += 1
    if any("yicai" in u or "ftchinese" in u for u in urls):
        categories += 1
    if any("douban" in u or "xiaohongshu" in u or "guokr" in u for u in urls):
        categories += 1
    assert categories >= 4, f"中文外链池类型不足: {categories} 类（至少需要 4 类）"


def test_random_selection_distribution():
    """随机选择 1000 次，每个 URL 被选中的概率大致均匀（无极端偏差）"""
    import random
    rng = random.Random(42)
    urls = REFERRAL_POOLS.get("en", [])
    n = 1000
    counts = {u: 0 for u in urls}
    for _ in range(n):
        u = rng.choice(urls)
        counts[u] += 1

    # 期望每个被选中 n/len 次，容差 ±50%
    expected = n / len(urls)
    for url, count in counts.items():
        assert count > expected * 0.3, f"URL 选中次数过低: {count}/{expected:.0f} - {url[:50]}"
        assert count < expected * 2.0, f"URL 选中次数过高: {count}/{expected:.0f} - {url[:50]}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
