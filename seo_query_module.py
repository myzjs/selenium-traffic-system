"""
SEO配置查询模块 - 2.0版
功能：独立的SEO配置查询工具，与原有代码完全解耦
提供：配置读取、通用查询方法、配置校验功能
新特性：搜索引擎动态管理、按语种共享关键词池
作者：真人流量模拟系统
"""
import json
import os
import logging
import random
import urllib.parse
from typing import List, Dict, Optional, Any, Tuple

# ==================== 模块配置 ====================
CONFIG_FILE = "config.json"  # 使用主配置文件

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[SEO查询] %(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("seo_query")

# 默认的SEO配置结构（用于校验和兖底）
DEFAULT_SEO_CONFIG = {
    "search_engines": [
        {"id": "google", "name": "谷歌", "url": "https://www.google.com/search?q=", "language": "en", "type": "search"},
        {"id": "bing", "name": "必应", "url": "https://www.bing.com/search?q=", "language": "en", "type": "search"},
        {"id": "baidu", "name": "百度", "url": "https://www.baidu.com/s?wd=", "language": "zh", "type": "search"},
        {"id": "sogou", "name": "搜狗", "url": "https://www.sogou.com/web?query=", "language": "zh", "type": "search"},
        {"id": "facebook", "name": "Facebook", "url": "https://www.facebook.com/", "language": "en", "type": "social"},
        {"id": "twitter", "name": "Twitter/X", "url": "https://x.com/", "language": "en", "type": "social"},
        {"id": "reddit", "name": "Reddit", "url": "https://www.reddit.com/", "language": "en", "type": "social"},
        {"id": "instagram", "name": "Instagram", "url": "https://www.instagram.com/", "language": "en", "type": "social"},
        {"id": "linkedin", "name": "LinkedIn", "url": "https://www.linkedin.com/", "language": "en", "type": "social"},
        {"id": "tiktok", "name": "TikTok", "url": "https://www.tiktok.com/", "language": "en", "type": "social"}
    ],
    "region_engine_map": {
        "US": ["google", "bing", "facebook", "twitter", "reddit", "instagram"],
        "GB": ["google", "bing", "facebook", "twitter", "reddit"],
        "AU": ["google", "bing", "facebook", "reddit", "instagram"],
        "DE": ["google", "bing", "facebook", "instagram"],
        "FR": ["google", "bing", "facebook", "instagram"],
        "JP": ["google", "bing", "twitter", "instagram", "tiktok"],
        "CN": ["baidu", "sogou", "tiktok"]
    },
    "keyword_pools": {
        "zh": ["广告联盟", "SEO优化", "网站推广", "网络营销", "数字营销"],
        "en": ["affiliate marketing", "SEO optimization", "website promotion", "digital marketing", "online marketing"]
    },
    "referer_mode": "dynamic"
}


class SEOConfigValidator:
    """SEO配置校验器"""
    
    def __init__(self):
        self.errors = []
        self.warnings = []
    
    def reset(self):
        """重置校验结果"""
        self.errors = []
        self.warnings = []
    
    def add_error(self, message: str):
        """添加错误信息"""
        self.errors.append(message)
        logger.error(f"配置校验错误: {message}")
    
    def add_warning(self, message: str):
        """添加警告信息"""
        self.warnings.append(message)
        logger.warning(f"配置校验警告: {message}")
    
    def validate_search_engines(self, search_engines: List) -> bool:
        """
        校验搜索引擎列表
        
        :param search_engines: 搜索引擎列表
        :return: 是否校验通过
        """
        is_valid = True
        
        if not search_engines or not isinstance(search_engines, list):
            self.add_error("搜索引擎列表为空或格式错误")
            return False
        
        engine_ids = set()
        for idx, engine in enumerate(search_engines):
            required_fields = ["id", "name", "url", "language"]
            for field in required_fields:
                if field not in engine or not engine[field]:
                    self.add_error(f"搜索引擎 #{idx} 缺少必要字段 '{field}'")
                    is_valid = False
            
            if "id" in engine:
                if engine["id"] in engine_ids:
                    self.add_error(f"搜索引擎ID重复: {engine['id']}")
                    is_valid = False
                engine_ids.add(engine["id"])
            
            if "language" in engine and engine["language"] not in ["zh", "en"]:
                self.add_warning(f"搜索引擎语言 '{engine['language']}' 不标准（应为 'zh' 或 'en'）")
        
        return is_valid
    
    def validate_region_engine_map(self, region_engine_map: Dict, search_engines: List) -> bool:
        """
        校验地域-搜索引擎映射表
        
        :param region_engine_map: 地域-引擎映射表
        :param search_engines: 搜索引擎列表
        :return: 是否校验通过
        """
        is_valid = True
        
        if not region_engine_map or not isinstance(region_engine_map, dict):
            self.add_error("地域-搜索引擎映射表为空或格式错误")
            return False
        
        valid_engine_ids = {engine["id"] for engine in search_engines}
        
        for region, engine_ids in region_engine_map.items():
            if not engine_ids:
                self.add_warning(f"地域 '{region}' 的搜索引擎列表为空")
                continue
            
            for engine_id in engine_ids:
                if engine_id not in valid_engine_ids:
                    self.add_warning(f"地域 '{region}' 映射的引擎ID '{engine_id}' 未在搜索引擎列表中定义")
        
        return is_valid
    
    def validate_keyword_pools(self, keyword_pools: Dict) -> bool:
        """
        校验关键词池
        
        :param keyword_pools: 关键词池
        :return: 是否校验通过
        """
        is_valid = True
        
        if not keyword_pools or not isinstance(keyword_pools, dict):
            self.add_error("关键词池为空或格式错误")
            return False
        
        for lang, keywords in keyword_pools.items():
            if not keywords:
                self.add_warning(f"语言 '{lang}' 的关键词池为空")
        
        return is_valid
    
    def validate_referer_config(self, referer_mode: str) -> bool:
        """
        校验Referer配置
        
        :param referer_mode: Referer模式
        :return: 是否校验通过
        """
        is_valid = True
        
        if referer_mode not in ["dynamic", "static"]:
            self.add_error(f"Referer模式 '{referer_mode}' 无效，应为 'dynamic' 或 'static'")
            is_valid = False
        
        return is_valid
    
    def validate(self, seo_config: Dict) -> bool:
        """
        完整校验配置
        
        :param seo_config: SEO配置
        :return: 是否校验通过
        """
        self.reset()
        
        search_engines = seo_config.get("search_engines", [])
        region_engine_map = seo_config.get("region_engine_map", {})
        keyword_pools = seo_config.get("keyword_pools", {})
        referer_mode = seo_config.get("referer_mode", "dynamic")
        
        # 分步校验
        self.validate_search_engines(search_engines)
        self.validate_region_engine_map(region_engine_map, search_engines)
        self.validate_keyword_pools(keyword_pools)
        self.validate_referer_config(referer_mode)
        
        # 返回结果
        return len(self.errors) == 0
    
    def get_report(self) -> Dict[str, Any]:
        """
        获取校验报告
        
        :return: 校验报告字典
        """
        return {
            "has_errors": len(self.errors) > 0,
            "has_warnings": len(self.warnings) > 0,
            "errors": self.errors,
            "warnings": self.warnings,
            "is_valid": len(self.errors) == 0
        }


class SEOConfigQuery:
    """SEO配置查询器 - 提供配置查询功能（2.0版）"""
    
    def __init__(self, config_file: str = CONFIG_FILE):
        """
        初始化SEO配置查询器
        
        :param config_file: 配置文件路径
        """
        self.config_file = config_file
        self.config = self._load_config()
        self.validator = SEOConfigValidator()
        self._engine_cache = self._build_engine_cache()
        
        # 初始化时自动校验
        self._validate_config()
    
    def _load_config(self) -> Dict:
        """
        加载配置文件
        
        :return: 配置字典
        """
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    full_config = json.load(f)
                seo_config = full_config.get("seo", DEFAULT_SEO_CONFIG.copy())
                logger.info(f"成功加载SEO配置文件: {self.config_file}")
                return seo_config
            except Exception as e:
                logger.error(f"加载SEO配置失败，使用默认配置: {e}")
                return DEFAULT_SEO_CONFIG.copy()
        else:
            logger.warning(f"配置文件不存在，使用默认配置: {self.config_file}")
            return DEFAULT_SEO_CONFIG.copy()
    
    def _build_engine_cache(self) -> Dict:
        """
        构建搜索引擎缓存（按ID索引）
        
        :return: 缓存字典
        """
        cache = {}
        for engine in self.config.get("search_engines", []):
            if "id" in engine:
                cache[engine["id"]] = engine
        return cache
    
    def _validate_config(self) -> None:
        """校验配置并输出日志"""
        is_valid = self.validator.validate(self.config)
        report = self.validator.get_report()
        
        if report["has_errors"]:
            logger.error("SEO配置校验发现错误")
        elif report["has_warnings"]:
            logger.warning("SEO配置校验发现警告")
        else:
            logger.info("SEO配置校验通过")
    
    def reload_config(self) -> None:
        """重新加载配置文件"""
        logger.info("重新加载SEO配置")
        self.config = self._load_config()
        self._engine_cache = self._build_engine_cache()
        self._validate_config()
    
    # ==================== 配置读取方法 ====================
    
    def get_all_config(self) -> Dict:
        """
        获取完整的SEO配置
        
        :return: 完整的配置字典
        """
        return self.config.copy()
    
    def get_search_engines(self) -> List[Dict]:
        """
        获取所有搜索引擎列表
        
        :return: 搜索引擎列表
        """
        return self.config.get("search_engines", []).copy()
    
    def get_engine_by_id(self, engine_id: str) -> Optional[Dict]:
        """
        根据ID获取搜索引擎信息
        
        :param engine_id: 搜索引擎ID
        :return: 搜索引擎字典，未找到返回None
        """
        return self._engine_cache.get(engine_id)
    
    def get_region_engine_map(self) -> Dict:
        """
        获取地域-搜索引擎映射表
        
        :return: 映射表字典
        """
        return self.config.get("region_engine_map", {}).copy()
    
    def get_keyword_pools(self) -> Dict:
        """
        获取SEO关键词池
        
        :return: 关键词池字典
        """
        return self.config.get("keyword_pools", {}).copy()
    
    def get_referer_mode(self) -> str:
        """
        获取Referer模式
        
        :return: 模式名称 'dynamic' 或 'static'
        """
        return self.config.get("referer_mode", "dynamic")
    
    # ==================== 通用查询方法 ====================
    
    def get_engine_ids_for_region(self, region_name: str) -> List[str]:
        """
        根据地域获取对应的搜索引擎ID列表
        
        :param region_name: 地域名称（如"中国"、"美国"）
        :return: 搜索引擎ID列表
        """
        region_engine_map = self.get_region_engine_map()
        engine_ids = region_engine_map.get(region_name, [])
        logger.debug(f"地域 '{region_name}' 对应引擎ID: {engine_ids}")
        
        if not engine_ids:
            logger.warning(f"未找到地域 '{region_name}' 对应的搜索引擎配置")
        
        return engine_ids.copy()
    
    def get_engine_url(self, engine_id: str) -> Optional[str]:
        """
        根据引擎ID获取引擎搜索URL
        
        :param engine_id: 引擎ID
        :return: 搜索URL，未找到则返回None
        """
        engine = self.get_engine_by_id(engine_id)
        url = engine.get("url") if engine else None
        
        if not url:
            logger.warning(f"未找到引擎ID '{engine_id}' 的URL配置")
        
        return url
    
    def get_engine_language(self, engine_id: str) -> Optional[str]:
        """
        根据引擎ID获取引擎语言
        
        :param engine_id: 引擎ID
        :return: 语言代码 'zh' 或 'en'，未找到则返回None
        """
        engine = self.get_engine_by_id(engine_id)
        lang = engine.get("language") if engine else None
        
        if not lang:
            logger.warning(f"未找到引擎ID '{engine_id}' 的语言配置")
        
        return lang
    
    def get_keywords_by_language(self, language: str) -> List[str]:
        """
        根据语言获取关键词列表
        
        :param language: 语言代码 'zh' 或 'en'
        :return: 关键词列表
        """
        keyword_pools = self.get_keyword_pools()
        keywords = keyword_pools.get(language, [])
        logger.debug(f"语言 '{language}' 的关键词数量: {len(keywords)}")
        return keywords.copy()
    
    def get_random_engine_for_region(self, region_name: str) -> Optional[str]:
        """
        根据地域随机选择一个搜索引擎ID
        
        :param region_name: 地域名称
        :return: 引擎ID，没有可用引擎则返回None
        """
        engine_ids = self.get_engine_ids_for_region(region_name)
        
        if not engine_ids:
            logger.error(f"地域 '{region_name}' 没有可用的搜索引擎")
            return None
        
        selected_engine_id = random.choice(engine_ids)
        engine_info = self.get_engine_by_id(selected_engine_id)
        logger.info(f"地域 '{region_name}' 随机选择引擎: {engine_info.get('name', selected_engine_id)} (ID={selected_engine_id})")
        return selected_engine_id
    
    def get_random_keyword_for_engine(self, engine_id: str) -> Optional[str]:
        """
        根据引擎随机选择一个关键词（根据引擎语言）
        
        :param engine_id: 引擎ID
        :return: 关键词，没有可用关键词则返回None
        """
        language = self.get_engine_language(engine_id)
        if not language:
            logger.error(f"无法获取引擎ID '{engine_id}' 的语言配置")
            return None
        
        keywords = self.get_keywords_by_language(language)
        
        if not keywords:
            logger.error(f"语言 '{language}' 没有可用的关键词")
            return None
        
        selected_keyword = random.choice(keywords)
        engine_info = self.get_engine_by_id(engine_id)
        logger.info(f"引擎 '{engine_info.get('name', engine_id)}' (语言={language}) 随机选择关键词: {selected_keyword}")
        return selected_keyword
    
    def generate_referer(self, engine_id: str, keyword: str = None) -> Optional[str]:
        """
        生成Referer URL
        
        :param engine_id: 引擎ID
        :param keyword: 关键词（可选，用于动态模式）
        :return: Referer URL，生成失败则返回None
        """
        referer_mode = self.get_referer_mode()
        engine = self.get_engine_by_id(engine_id)
        engine_url = engine.get("url") if engine else None
        engine_type = engine.get("type", "search") if engine else "search"
        
        if not engine_url:
            logger.error(f"无法生成Referer：引擎ID '{engine_id}' 的URL为空")
            return None
        
        # 社媒平台：直接使用平台URL作为Referer（不拼接关键词）
        if engine_type == "social":
            logger.info(f"社媒平台Referer: {engine_url}")
            return engine_url
        
        # 搜索引擎：动态拼接关键词
        if referer_mode == "dynamic":
            if not keyword:
                keyword = self.get_random_keyword_for_engine(engine_id)
                if not keyword:
                    logger.error(f"无法生成Referer：引擎ID '{engine_id}' 没有关键词")
                    return None
            
            # URL编码关键词
            encoded_keyword = urllib.parse.quote(keyword)
            
            # 拼接URL
            referer = f"{engine_url}{encoded_keyword}"
            logger.info(f"动态生成Referer: {referer}")
            return referer
        else:
            # 静态模式：使用搜索引擎主页
            homepage = self.get_engine_homepage(engine_url)
            logger.info(f"静态Referer: {homepage}")
            return homepage or engine_url
    
    def get_engine_homepage(self, engine_url: str) -> Optional[str]:
        """
        从搜索引擎搜索URL中提取主页
        
        :param engine_url: 搜索URL，比如 https://www.google.com/search?q=
        :return: 主页，比如 https://www.google.com
        """
        try:
            parsed = urllib.parse.urlparse(engine_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception as e:
            logger.error(f"提取搜索引擎主页失败: {str(e)}")
            return None
    
    # ==================== 校验相关方法 ====================
    
    def get_validation_report(self) -> Dict[str, Any]:
        """
        获取当前配置的校验报告
        
        :return: 校验报告
        """
        self.validator.validate(self.config)
        return self.validator.get_report()
    
    def is_config_valid(self) -> bool:
        """
        检查配置是否有效
        
        :return: 是否有效
        """
        report = self.get_validation_report()
        return report["is_valid"]
    
    # ==================== 便捷查询方法 ====================
    
    def get_all_regions(self) -> List[str]:
        """
        获取所有配置的地域名称
        
        :return: 地域名称列表
        """
        region_engine_map = self.get_region_engine_map()
        return list(region_engine_map.keys())
    
    def get_all_engine_ids(self) -> List[str]:
        """
        获取所有引擎ID
        
        :return: 引擎ID列表
        """
        return list(self._engine_cache.keys())


# 全局SEO配置查询器实例（单例模式）
_seo_query_instance = None


def get_seo_query(config_file: str = CONFIG_FILE) -> SEOConfigQuery:
    """
    获取SEO配置查询器实例（单例模式）
    
    :param config_file: 配置文件路径
    :return: SEO配置查询器实例
    """
    global _seo_query_instance
    if _seo_query_instance is None:
        _seo_query_instance = SEOConfigQuery(config_file)
    return _seo_query_instance


def reset_seo_query_instance() -> None:
    """重置SEO配置查询器实例"""
    global _seo_query_instance
    _seo_query_instance = None


# ==================== 使用示例 ====================
if __name__ == "__main__":
    print("=" * 60)
    print("SEO配置查询模块 2.0版 - 使用示例")
    print("=" * 60)
    
    # 获取查询器实例
    query = get_seo_query()
    
    # 1. 获取配置校验报告
    print("\n1. 配置校验报告:")
    report = query.get_validation_report()
    print(f"   配置有效: {report['is_valid']}")
    if report['has_errors']:
        print(f"   错误: {report['errors']}")
    if report['has_warnings']:
        print(f"   警告: {report['warnings']}")
    
    # 2. 获取搜索引擎列表
    print("\n2. 搜索引擎列表:")
    engines = query.get_search_engines()
    for engine in engines:
        print(f"   - {engine['name']} (ID={engine['id']}, 语言={engine['language']})")
        print(f"     URL: {engine['url']}")
    
    # 3. 根据地域获取引擎
    print("\n3. 根据地域查询引擎:")
    test_regions = ["中国", "美国"]
    for region in test_regions:
        engine_ids = query.get_engine_ids_for_region(region)
        engine_names = [query.get_engine_by_id(eid).get('name', eid) for eid in engine_ids if query.get_engine_by_id(eid)]
        print(f"   地域 '{region}': {engine_names}")
    
    # 4. 随机选择引擎
    print("\n4. 随机选择引擎:")
    for region in test_regions:
        engine_id = query.get_random_engine_for_region(region)
        if engine_id:
            engine_info = query.get_engine_by_id(engine_id)
            print(f"   地域 '{region}' -> 选中引擎: {engine_info.get('name', engine_id)}")
    
    # 5. 获取关键词池
    print("\n5. 关键词池:")
    keyword_pools = query.get_keyword_pools()
    for lang, keywords in keyword_pools.items():
        print(f"   语言 '{lang}': {keywords}")
    
    # 6. 随机选择关键词
    print("\n6. 随机选择关键词:")
    test_engine_ids = ["baidu", "google"]
    for engine_id in test_engine_ids:
        keyword = query.get_random_keyword_for_engine(engine_id)
        engine_info = query.get_engine_by_id(engine_id)
        print(f"   引擎 '{engine_info.get('name', engine_id)}' -> 选中关键词: {keyword}")
    
    # 7. 生成Referer
    print("\n7. 生成Referer:")
    for engine_id in test_engine_ids:
        referer = query.generate_referer(engine_id)
        engine_info = query.get_engine_by_id(engine_id)
        print(f"   引擎 '{engine_info.get('name', engine_id)}' -> Referer: {referer}")
    
    print("\n" + "=" * 60)
    print("示例完成！")
    print("=" * 60)
