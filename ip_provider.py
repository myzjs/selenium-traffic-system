"""
IP获取方式统一模块 - 2.0版（直连 IPDeep API）
功能：提供统一的IP获取接口，本机直连 IPDeep API 获取代理IP

架构（去除二层中转）：
  本机 → IPDeep API (Basic Auth) → 获得 host:port:user:pwd
  本机浏览器 → http://user:pwd@host:port → 目标网站

IPDeep API 返回格式（纯文本）：host:port:username:password
"""
import json
import logging
import os
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("ip_provider")

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    requests = None
    HTTPBasicAuth = None


class IPProvider:
    def __init__(self, provider_type: str = "proxy_api"):
        self.provider_type = provider_type
        self.proxy_pool = []
        self.current_proxy = None
        self._config = {}

    def configure_proxy_api(self, proxy_pool: list, config: Dict = None, **kwargs):
        """配置代理API参数（兼容旧调用签名，忽略 vps_* 参数）"""
        self.proxy_pool = proxy_pool
        if config:
            self._config = config
        self.provider_type = "proxy_api"

    def get_ip(self) -> Dict:
        if self.provider_type == "proxy_api":
            return self._get_ip_via_proxy()
        else:
            return {"success": False, "error": f"未知的IP获取方式: {self.provider_type}"}

    def _get_ip_via_proxy(self) -> Dict:
        if not self.proxy_pool:
            return {"success": False, "error": "代理池为空"}

        import random
        available_proxies = [p for p in self.proxy_pool if p.get("enabled", True)]
        if not available_proxies:
            return {"success": False, "error": "没有启用的代理"}

        selected_proxy = random.choice(available_proxies)
        self.current_proxy = selected_proxy

        api_url = selected_proxy.get("proxy_api_url")
        api_user = selected_proxy.get("proxy_user", "")
        api_pwd = selected_proxy.get("proxy_pwd", "")
        country_code = selected_proxy.get("country_code", "US")

        if not api_url:
            logger.warning("代理API URL为空，尝试使用配置文件中的默认API")
            api_url = self._config.get("ip_proxy_api", "")
            api_user = self._config.get("ip_proxy_user", "")
            api_pwd = self._config.get("ip_proxy_pwd", "")

            if not api_url:
                return {"success": False, "error": "代理API URL未配置"}

        return self._fetch_proxy_from_ipdeep(api_url, api_user, api_pwd, country_code)

    def _fetch_proxy_from_ipdeep(self, api_url: str, api_user: str, api_pwd: str,
                                   country_code: str = "US") -> Dict:
        """直连 IPDeep API 获取代理（去除 VPS 中转）

        IPDeep API 返回纯文本格式: host:port:username:password
        支持 Basic Auth 认证（api_user / api_pwd）。
        """
        if requests is None:
            logger.error("requests库未安装，无法调用代理API")
            return {"success": False, "error": "requests库未安装"}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"[IPDeep] 直连获取代理 (尝试 {attempt+1}/{max_retries}, 国家: {country_code})")
                logger.info(f"[IPDeep] API: {api_url[:80]}...")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "*/*"
                }

                # Basic Auth 认证
                auth = None
                if api_user and api_pwd:
                    auth = HTTPBasicAuth(api_user, api_pwd)
                    logger.info(f"[IPDeep] 使用 Basic Auth: user={api_user}")

                resp = requests.get(api_url, headers=headers, auth=auth, timeout=15)
                logger.info(f"[IPDeep] HTTP {resp.status_code}")

                if resp.status_code >= 400:
                    body_preview = (resp.text or "")[:300]
                    logger.warning(f"[IPDeep] 返回非2xx: HTTP {resp.status_code} (body={body_preview!r})")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return {"success": False, "error": f"IPDeep HTTP {resp.status_code}", "detail": body_preview}

                response_text = resp.text.strip()

                # 尝试 JSON 格式（某些 IPDeep 套餐返回 JSON）
                if response_text.startswith("{"):
                    try:
                        result = json.loads(response_text)
                        if not result.get("success", True):
                            msg = result.get("msg") or result.get("message") or result.get("error") or "IPDeep返回失败"
                            logger.warning(f"[IPDeep] 接口显式失败: {msg}")
                            return {"success": False, "error": msg}
                        # JSON 格式可能直接包含代理信息
                        if "data" in result:
                            data = result["data"]
                            proxy_host = data.get("ip") or data.get("host", "")
                            proxy_port = str(data.get("port", ""))
                            proxy_username = data.get("username", "")
                            proxy_password = data.get("password", "")
                        else:
                            proxy_host = result.get("ip") or result.get("host", "")
                            proxy_port = str(result.get("port", ""))
                            proxy_username = result.get("username", "")
                            proxy_password = result.get("password", "")
                    except json.JSONDecodeError:
                        pass  # 不是 JSON，按文本格式解析
                else:
                    # 纯文本格式: host:port:username:password
                    parts = response_text.split(":")
                    if len(parts) < 2:
                        logger.warning(f"[IPDeep] 返回格式不正确: {response_text[:200]}")
                        if attempt < max_retries - 1:
                            time.sleep(2)
                            continue
                        return {"success": False, "error": f"IPDeep返回格式不正确: {response_text[:100]}"}

                    proxy_host = parts[0]
                    proxy_port = parts[1]
                    proxy_username = ":".join(parts[2:-1]) if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
                    proxy_password = parts[-1] if len(parts) > 3 else (parts[3] if len(parts) > 3 else "")
                    # 处理 host:port:user:pwd 四段格式
                    if len(parts) == 4:
                        proxy_host = parts[0]
                        proxy_port = parts[1]
                        proxy_username = parts[2]
                        proxy_password = parts[3]
                    elif len(parts) == 3:
                        proxy_host = parts[0]
                        proxy_port = parts[1]
                        proxy_username = parts[2]
                        proxy_password = ""
                    elif len(parts) == 2:
                        proxy_host = parts[0]
                        proxy_port = parts[1]
                        proxy_username = ""
                        proxy_password = ""

                if not proxy_host or not proxy_port:
                    logger.warning(f"[IPDeep] 解析代理信息失败: host={proxy_host}, port={proxy_port}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return {"success": False, "error": "IPDeep返回的代理信息不完整"}

                logger.info(f"[IPDeep] ✅ 解析代理成功: {proxy_host}:{proxy_port} (user={proxy_username[:5] if proxy_username else '无'}...)")

                # 通过代理获取出口IP详情
                proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}" if proxy_username else f"http://{proxy_host}:{proxy_port}"
                ip_info = self._get_ip_details(proxy_url)

                # IP 去重检查
                exit_ip = ip_info.get("ip", "未知")
                if exit_ip != "未知" and check_ip_used_recently(exit_ip):
                    logger.warning(f"[IPDeep] IP {exit_ip} 在去重间隔内已使用过，重新获取...")
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue

                # 记录IP使用
                if exit_ip != "未知":
                    record_ip_use(exit_ip)

                result = {
                    "success": True,
                    "proxy_host": proxy_host,
                    "proxy_port": proxy_port,
                    "proxy_username": proxy_username,
                    "proxy_password": proxy_password,
                    "ip_info": ip_info,
                }
                logger.info(f"[IPDeep] ✅ 代理获取成功: {proxy_host}:{proxy_port}, 出口IP: {exit_ip}")
                return result

            except requests.exceptions.Timeout:
                logger.error(f"[IPDeep] ❌ 请求超时(15s) (尝试 {attempt+1})")
                if attempt < max_retries - 1:
                    time.sleep(2)
            except requests.exceptions.ConnectionError as e:
                logger.error(f"[IPDeep] ❌ 连接失败: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
            except Exception as e:
                logger.error(f"[IPDeep] ❌ 异常: {type(e).__name__}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)

        return {"success": False, "error": f"IPDeep API {max_retries}次尝试均失败"}

    def _get_ip_details(self, proxy_url: str) -> Dict:
        """通过代理获取出口IP详细信息"""
        ip_apis = [
            {
                "name": "ip-api.com",
                "url": "http://ip-api.com/json?fields=status,country,countryCode,regionName,city,timezone,isp,query",
                "parser": lambda data: {
                    "success": data.get("status") == "success",
                    "ip": data.get("query"),
                    "country": data.get("country"),
                    "country_code": data.get("countryCode"),
                    "region": data.get("regionName"),
                    "city": data.get("city"),
                    "timezone": data.get("timezone"),
                    "isp": data.get("isp"),
                    "language": _get_language_from_country(data.get("countryCode", "US"))
                }
            },
            {
                "name": "ipinfo.io",
                "url": "https://ipinfo.io/json",
                "parser": lambda data: {
                    "success": True,
                    "ip": data.get("ip"),
                    "country": data.get("country"),
                    "country_code": data.get("country"),
                    "region": data.get("region"),
                    "city": data.get("city"),
                    "timezone": data.get("timezone"),
                    "isp": data.get("org"),
                    "language": _get_language_from_country(data.get("country", "US"))
                }
            }
        ]

        proxies = {"http": proxy_url, "https": proxy_url}

        for api in ip_apis:
            try:
                resp = requests.get(api["url"], proxies=proxies, timeout=8)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                result = api["parser"](data)
                if result.get("success") and result.get("ip"):
                    logger.info(f"[IP详情] 通过 {api['name']} 获取: {result.get('ip')} ({result.get('country')})")
                    return result
            except Exception as e:
                logger.debug(f"[IP详情] {api['name']} 失败: {e}")
                continue

        logger.warning("[IP详情] 所有API失败，使用默认数据")
        return {
            "success": True,
            "ip": "未知",
            "country": "United States",
            "country_code": "US",
            "region": "Unknown",
            "city": "Unknown",
            "timezone": "America/New_York",
            "isp": "Unknown",
            "language": "en-US"
        }


def _get_language_from_country(country_code: str) -> str:
    """根据国家代码返回对应语言"""
    if not country_code:
        return "en-US"
    lang_map = {
        "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU",
        "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK", "JP": "ja-JP",
        "KR": "ko-KR", "DE": "de-DE", "FR": "fr-FR", "ES": "es-ES",
        "IT": "it-IT", "BR": "pt-BR", "RU": "ru-RU", "MX": "es-MX",
        "SA": "ar-SA", "AE": "ar-AE", "EG": "ar-EG"
    }
    return lang_map.get(country_code.upper(), "en-US")


# ============================================================
# 全局实例与便捷函数
# ============================================================

_global_ip_provider = IPProvider()


def get_ip_provider() -> IPProvider:
    return _global_ip_provider


def set_ip_provider_type(provider_type: str):
    global _global_ip_provider
    _global_ip_provider = IPProvider(provider_type)


def configure_ip_provider(config: Dict):
    provider = IPProvider("proxy_api")
    provider.configure_proxy_api(
        proxy_pool=config.get("proxy_pool", []),
        config=config
    )
    global _global_ip_provider
    _global_ip_provider = provider


def get_current_ip() -> Dict:
    return _global_ip_provider.get_ip()


# ============================================================
# 便捷函数：直接通过 API URL 获取代理（兼容 app.py 原有调用方式）
# ============================================================

def get_proxy_from_api_url(api_url: str, api_user: str = "", api_pwd: str = "",
                          country_code: str = "US",
                          use_cache: bool = True,
                          force_refresh: bool = False) -> Dict:
    """直连 IPDeep API 获取代理（便捷函数）

    与 app.py 中原有的 get_proxy_from_api_url 函数签名完全一致。

    :param api_url: IPDeep API URL
    :param api_user: 代理用户名（可选，用于 Basic Auth）
    :param api_pwd: 代理密码（可选）
    :param country_code: 国家代码（可选，默认US）
    :param use_cache: 是否使用本地缓存（默认True）
    :param force_refresh: 是否强制刷新（忽略缓存）
    :return: 代理信息字典 {success, proxy_host, proxy_port, proxy_username, proxy_password, ip_info}
    """
    provider = _global_ip_provider

    # 1. 检查缓存
    if use_cache and not force_refresh and api_url:
        with _proxy_cache_lock:
            cached = _proxy_cache.get(api_url)
            if cached and (time.time() - cached["timestamp"]) <= PROXY_CACHE_TTL:
                logger.debug(f"[代理缓存] 使用缓存代理 (API: {api_url[:50]}...)")
                return cached["data"]

    # 2. 直连 IPDeep API
    result = provider._fetch_proxy_from_ipdeep(
        api_url=api_url,
        api_user=api_user,
        api_pwd=api_pwd,
        country_code=country_code
    )

    # 3. 成功则写入缓存
    if result.get("success") and use_cache and api_url:
        with _proxy_cache_lock:
            _proxy_cache[api_url] = {
                "data": result,
                "timestamp": time.time()
            }
            logger.debug(f"[代理缓存] 已缓存代理 (API: {api_url[:50]}...)")

    return result


def invalidate_proxy_cache(api_url: str = None):
    """使代理缓存失效"""
    with _proxy_cache_lock:
        if api_url:
            if api_url in _proxy_cache:
                del _proxy_cache[api_url]
                logger.info(f"[代理缓存] 已清除指定缓存: {api_url[:50]}...")
        else:
            _proxy_cache.clear()
            logger.info("[代理缓存] 已清空所有代理缓存")


def check_ip_used_recently(ip: str) -> bool:
    """检查IP是否在去重间隔内使用过"""
    now = time.time()
    with _used_ips_lock:
        expired = [k for k, v in _used_ips.items() if (now - v) > IP_REUSE_INTERVAL]
        for k in expired:
            del _used_ips[k]
        return ip in _used_ips


def record_ip_use(ip: str):
    """记录IP使用时间"""
    with _used_ips_lock:
        _used_ips[ip] = time.time()
        logger.debug(f"[IP去重] 记录IP使用: {ip}")


def get_used_ips_count() -> int:
    """获取去重池中IP数量"""
    with _used_ips_lock:
        return len(_used_ips)


# ============================================================
# C段(/24)分散策略：避免同一子网集中访问触发 AdSense 风控
# ============================================================
_c_segment_usage = {}  # {"x.x.x": [timestamp, ...]}
_c_segment_lock = threading.Lock()
C_SEGMENT_MAX_PER_WINDOW = 3
C_SEGMENT_WINDOW_HOURS = 6


def get_c_segment(ip: str) -> str:
    """提取IP的C段（/24子网前缀）"""
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3])
    return ip


def check_c_segment_diversity(ip: str) -> bool:
    """检查IP的C段是否过度集中"""
    c_seg = get_c_segment(ip)
    now = time.time()
    window_sec = C_SEGMENT_WINDOW_HOURS * 3600

    with _c_segment_lock:
        if c_seg in _c_segment_usage:
            _c_segment_usage[c_seg] = [
                ts for ts in _c_segment_usage[c_seg] if (now - ts) < window_sec
            ]
        else:
            _c_segment_usage[c_seg] = []
        return len(_c_segment_usage[c_seg]) < C_SEGMENT_MAX_PER_WINDOW


def record_c_segment_use(ip: str):
    """记录IP的C段使用"""
    c_seg = get_c_segment(ip)
    with _c_segment_lock:
        if c_seg not in _c_segment_usage:
            _c_segment_usage[c_seg] = []
        _c_segment_usage[c_seg].append(time.time())
    logger.debug(f"[C段分散] 记录C段使用: {c_seg}.0/24 (IP: {ip})")


def get_c_segment_stats() -> Dict:
    """获取C段分散统计信息"""
    now = time.time()
    window_sec = C_SEGMENT_WINDOW_HOURS * 3600
    with _c_segment_lock:
        active = {
            seg: len([ts for ts in timestamps if (now - ts) < window_sec])
            for seg, timestamps in _c_segment_usage.items()
        }
    return {
        "total_segments": len(active),
        "segments": active,
        "max_per_window": C_SEGMENT_MAX_PER_WINDOW,
        "window_hours": C_SEGMENT_WINDOW_HOURS
    }


# ============================================================
# 从 app.py 配置同步：便捷初始化函数
# ============================================================

def init_from_config(config: Dict) -> IPProvider:
    """从配置字典初始化并返回全局 IPProvider"""
    global PROXY_CACHE_TTL, IP_REUSE_INTERVAL

    configure_ip_provider(config)

    if "proxy_cache_ttl" in config:
        PROXY_CACHE_TTL = int(config["proxy_cache_ttl"])
    if "ip_reuse_interval_hours" in config:
        IP_REUSE_INTERVAL = int(config["ip_reuse_interval_hours"]) * 3600

    logger.info(
        f"[IPProvider] 初始化完成: type=proxy_api(直连IPDeep), "
        f"cache_ttl={PROXY_CACHE_TTL}s, dedup_interval={IP_REUSE_INTERVAL//3600}h"
    )

    return _global_ip_provider


# ============================================================
# IP 缓存与去重
# ============================================================
_proxy_cache = {}  # {api_url: {"data": proxy_data, "timestamp": float}}
_proxy_cache_lock = threading.Lock()
_used_ips = {}  # {ip: timestamp}
_used_ips_lock = threading.Lock()
PROXY_CACHE_TTL = 600  # 代理缓存TTL（秒），默认10分钟
IP_REUSE_INTERVAL = 12 * 3600  # IP去重间隔（秒），默认12小时


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    print("=== 测试直连 IPDeep API ===")
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    provider = IPProvider("proxy_api")
    provider.configure_proxy_api(
        proxy_pool=config.get("proxy_pool", []),
        config=config
    )

    result = provider.get_ip()
    print("结果:", result)

    if result.get("success"):
        print(f"出口IP: {result.get('ip_info', {}).get('ip', 'unknown')}")
        print(f"代理: {result.get('proxy_host')}:{result.get('proxy_port')}")
"""
IP获取方式统一模块 - 1.1版（二层网络架构修正版）
功能：提供统一的IP获取接口，支持多种获取方式

支持的IP获取方式：
1. PROXY_API - 通过VPS代理服务对接IPDeep API接口获取出口IP（二层架构）
2. ADSL - 通过ADSL拨号获取IP

修正内容：
- 修复了直接调用IPDeep API的错误，改为通过VPS代理服务获取
- 使用HTTPBasicAuth认证访问VPS
- 正确处理VPS响应和IPDeep响应
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from typing import Dict, Optional

logger = logging.getLogger("ip_provider")

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    requests = None
    HTTPBasicAuth = None

IP_PROVIDER_TYPE = {
    "PROXY_API": "proxy_api",
    "ADSL": "adsl"
}


class IPProvider:
    def __init__(self, provider_type: str = "proxy_api"):
        self.provider_type = provider_type
        self.adsl_profile = "pppoe"
        self.adsl_username = ""
        self.adsl_password = ""
        self.adsl_interface = "ppp0"
        self.proxy_pool = []
        self.current_proxy = None
        self.vps_config = {
            "host": "",
            "port": 6666,
            "user": "",
            "pass": ""
        }
        self._config = {}
    
    def configure_proxy_api(self, proxy_pool: list, vps_host: str = "", 
                           vps_port: int = 6666, vps_user: str = "", vps_pass: str = "",
                           config: Dict = None):
        self.proxy_pool = proxy_pool
        self.vps_config = {
            "host": vps_host,
            "port": vps_port,
            "user": vps_user,
            "pass": vps_pass
        }
        if config:
            self._config = config
        self.provider_type = IP_PROVIDER_TYPE["PROXY_API"]
    
    def configure_adsl(self, profile: str = "pppoe", username: str = "", 
                      password: str = "", interface: str = "ppp0"):
        self.adsl_profile = profile
        self.adsl_username = username
        self.adsl_password = password
        self.adsl_interface = interface
        self.provider_type = IP_PROVIDER_TYPE["ADSL"]
    
    def get_ip(self) -> Dict:
        if self.provider_type == IP_PROVIDER_TYPE["PROXY_API"]:
            return self._get_ip_via_proxy()
        elif self.provider_type == IP_PROVIDER_TYPE["ADSL"]:
            return self._get_ip_via_adsl()
        else:
            return {"success": False, "error": f"未知的IP获取方式: {self.provider_type}"}
    
    def _get_ip_via_proxy(self) -> Dict:
        if not self.proxy_pool:
            return {"success": False, "error": "代理池为空"}
        
        import random
        available_proxies = [p for p in self.proxy_pool if p.get("enabled", True)]
        if not available_proxies:
            return {"success": False, "error": "没有启用的代理"}
        
        selected_proxy = random.choice(available_proxies)
        self.current_proxy = selected_proxy
        
        api_url = selected_proxy.get("proxy_api_url")
        api_user = selected_proxy.get("proxy_user", "")
        api_pwd = selected_proxy.get("proxy_pwd", "")
        country_code = selected_proxy.get("country_code", "US")
        
        if not api_url:
            logger.warning("代理API URL为空，尝试使用配置文件中的默认API")
            api_url = self._config.get("ip_proxy_api", "")
            api_user = self._config.get("ip_proxy_user", "")
            api_pwd = self._config.get("ip_proxy_pwd", "")
            
            if not api_url:
                return {"success": False, "error": "代理API URL未配置"}
        
        return self._fetch_proxy_from_vps(api_url, api_user, api_pwd, country_code)
    
    def _fetch_proxy_from_vps(self, api_url: str, api_user: str, api_pwd: str, 
                             country_code: str = "US") -> Dict:
        """通过VPS代理服务获取IPDeep代理（二层网络架构）
        - 通过VPS代理服务获取代理，不是直接调用IPDeep API
        - VPS端proxy_server_new.py会负责调用IPDeep并返回标准格式
        - 对非 2xx / 空 body / 非 JSON 响应统一返回结构化失败
        - 对 ipdeep 特有字段 {"code":..., "msg":"账号不存在"} 等也处理为 success=False
        """
        if requests is None:
            logger.error("requests库未安装，无法调用代理API")
            return {"success": False, "error": "requests库未安装"}
        
        try:
            # 构建VPS API请求URL，通过VPS代理服务获取代理（二层架构）
            vps_control_port = int(self.vps_config.get("port") or 6666)
            vps_host = self.vps_config.get("host", "")
            
            if not vps_host:
                # 从配置中获取VPS主机
                vps_host = self._config.get("vps_host", "")
            
            if not vps_host:
                logger.error("VPS主机未配置")
                return {"success": False, "error": "VPS主机未配置"}
            
            vps_url = f"http://{vps_host}:{vps_control_port}/api/get_proxy"
            encoded_api_url = urllib.parse.quote(api_url, safe='')
            full_url = f"{vps_url}?api_url={encoded_api_url}"
            
            logger.info(f"[VPS] 请求VPS获取代理 (国家: {country_code}, API: {api_url[:60]}...)")
            logger.info(f"[VPS] 完整请求URL: {full_url}")

            # 通过VPS API服务获取代理，使用HTTPBasicAuth认证
            vps_user = self.vps_config.get("user", "") or self._config.get("vps_user", "") or os.environ.get("VPS_USER", "admin")
            vps_pass = self.vps_config.get("pass", "") or self._config.get("vps_pass", "") or os.environ.get("VPS_PASS", "")
            
            logger.info(f"[VPS] VPS认证用户: {vps_user[:3] if vps_user else '(空)'}...")
            
            logger.info(f"[VPS] 开始requests.get调用 (timeout=20s)...")
            try:
                resp = requests.get(
                    full_url,
                    auth=HTTPBasicAuth(vps_user, vps_pass),
                    timeout=20
                )
                logger.info(f"[VPS] requests.get返回: HTTP {resp.status_code}")
            except requests.exceptions.Timeout:
                logger.error(f"[VPS] ❌ VPS请求超时(20s)，请检查VPS服务是否正常运行")
                return {
                    "success": False,
                    "error": "VPS请求超时(20s)",
                    "detail": "请检查VPS上的proxy_server_new.py服务是否正在运行"
                }
            except requests.exceptions.ConnectionError as e:
                logger.error(f"[VPS] ❌ VPS连接失败: {e}")
                return {
                    "success": False,
                    "error": f"VPS连接失败: {type(e).__name__}",
                    "detail": str(e)[:200]
                }
            except Exception as e:
                logger.error(f"[VPS] ❌ VPS请求异常: {type(e).__name__}: {e}")
                return {
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "detail": str(e)[:200]
                }
            
            # 不调用 raise_for_status 直接中断，先保留响应体以便写日志
            if resp.status_code >= 400:
                _body_preview = (resp.text or "")[:300]
                logger.warning(
                    f"[VPS] VPS代理返回非2xx: HTTP {resp.status_code} "
                    f"(body={_body_preview!r})"
                )
                return {
                    "success": False,
                    "error": f"VPS HTTP {resp.status_code}",
                    "detail": _body_preview,
                }

            # 解析 JSON
            try:
                result = resp.json()
            except ValueError:
                _body_preview = (resp.text or "")[:300]
                logger.warning(
                    f"[VPS] VPS代理响应不是合法JSON (body={_body_preview!r})"
                )
                return {
                    "success": False,
                    "error": "VPS响应非JSON",
                    "detail": _body_preview,
                }

            if not isinstance(result, dict):
                logger.warning(f"[VPS] VPS代理响应结构异常: {type(result).__name__}={result!r}")
                return {"success": False, "error": "VPS响应非dict", "detail": str(result)[:200]}

            # ipdeep 自身可能返回 {"code":-1,"msg":"账号不存在"} / {"success":False,...}
            if not result.get("success", False):
                _msg = (
                    result.get("msg")
                    or result.get("message")
                    or result.get("error")
                    or ""
                )
                logger.warning(
                    f"[VPS] VPS代理接口显式失败: success=False, msg={_msg or '未知'} "
                    f"(code={result.get('code')}, raw={str(result)[:200]})"
                )
                # 保留原始返回，外层能读到 error / msg 信息
                result.setdefault("error", _msg or "ipdeep返回失败")
                return result

            logger.info(f"[VPS] VPS响应结果: success=True")
            return result
        
        except Exception as e:
            logger.error(f"[VPS] 从VPS获取代理失败: {type(e).__name__}: {e}")
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
    
    def _get_ip_via_adsl(self) -> Dict:
        try:
            result = subprocess.run(
                ["ip", "addr", "show", self.adsl_interface],
                capture_output=True, text=True
            )
            
            if result.returncode != 0:
                return self._perform_adsl_connect()
            
            ip_address = self._extract_ip_from_output(result.stdout)
            if ip_address:
                return {
                    "success": True,
                    "provider": "adsl",
                    "ip_address": ip_address,
                    "interface": self.adsl_interface
                }
            else:
                return {"success": False, "error": "无法从ADSL接口提取IP"}
        
        except Exception as e:
            logger.error(f"[ADSL] 获取IP失败: {e}")
            return {"success": False, "error": str(e)}
    
    def _perform_adsl_connect(self) -> Dict:
        try:
            if self.adsl_username and self.adsl_password:
                # 命令注入防护：验证凭据只包含安全字符
                _safe_cred_re = re.compile(r'^[A-Za-z0-9._@\-]+$')
                if not _safe_cred_re.match(self.adsl_username):
                    logger.error("[ADSL] 用户名包含非法字符，拒绝连接")
                    return {"success": False, "error": "ADSL用户名包含非法字符"}
                if not _safe_cred_re.match(self.adsl_password):
                    logger.error("[ADSL] 密码包含非法字符，拒绝连接")
                    return {"success": False, "error": "ADSL密码包含非法字符"}
                config_content = f"""
user "{self.adsl_username}"
password "{self.adsl_password}"
plugin rp-pppoe.so
{self.adsl_interface}
"""
                with open("/etc/ppp/peers/adsl", "w") as f:
                    f.write(config_content)
                
                result = subprocess.run(
                    ["pon", "adsl"],
                    capture_output=True, text=True, timeout=30
                )
                
                if result.returncode == 0:
                    time.sleep(5)
                    result = subprocess.run(
                        ["ip", "addr", "show", self.adsl_interface],
                        capture_output=True, text=True
                    )
                    ip_address = self._extract_ip_from_output(result.stdout)
                    if ip_address:
                        return {
                            "success": True,
                            "provider": "adsl",
                            "ip_address": ip_address,
                            "interface": self.adsl_interface
                        }
            
            return {"success": False, "error": "ADSL拨号失败"}
        
        except Exception as e:
            logger.error(f"[ADSL] 拨号失败: {e}")
            return {"success": False, "error": str(e)}
    
    def _extract_ip_from_output(self, output: str) -> Optional[str]:
        for line in output.split('\n'):
            if 'inet ' in line and 'brd' in line:
                parts = line.strip().split()
                for part in parts:
                    if part.startswith('192.') or part.startswith('10.') or \
                       part.startswith('172.') or (':' not in part and '.' in part):
                        ip = part.split('/')[0]
                        if ip and not ip.startswith('127.'):
                            return ip
        return None
    
    def disconnect_adsl(self) -> bool:
        try:
            result = subprocess.run(
                ["poff", "adsl"],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"[ADSL] 断开失败: {e}")
            return False


_global_ip_provider = IPProvider()


def get_ip_provider() -> IPProvider:
    return _global_ip_provider


def set_ip_provider_type(provider_type: str):
    global _global_ip_provider
    _global_ip_provider = IPProvider(provider_type)


def configure_ip_provider(config: Dict):
    provider_type = config.get("ip_provider_type", "proxy_api")
    provider = IPProvider(provider_type)
    
    if provider_type == "proxy_api":
        # 优先使用 vps_new_port，其次是 vps_port，最后默认 6666
        vps_port = int(
            config.get("vps_new_port") 
            or config.get("vps_port") 
            or 6666
        )
        provider.configure_proxy_api(
            proxy_pool=config.get("proxy_pool", []),
            vps_host=config.get("vps_host", ""),
            vps_port=vps_port,
            vps_user=config.get("vps_user", ""),
            vps_pass=config.get("vps_pass", ""),
            config=config
        )
    elif provider_type == "adsl":
        provider.configure_adsl(
            profile=config.get("adsl_profile", "pppoe"),
            username=config.get("adsl_username", ""),
            password=config.get("adsl_password", ""),
            interface=config.get("adsl_interface", "ppp0")
        )
    
    global _global_ip_provider
    _global_ip_provider = provider


def get_current_ip() -> Dict:
    return _global_ip_provider.get_ip()


# ============================================================
# 便捷函数：直接通过 API URL 获取代理（兼容 app.py 原有调用方式）
# ============================================================

def get_proxy_from_api_url(api_url: str, api_user: str = "", api_pwd: str = "", 
                          country_code: str = "US", 
                          use_cache: bool = True,
                          force_refresh: bool = False) -> Dict:
    """通过 VPS 代理服务获取 IPDeep 代理（便捷函数）
    
    与 app.py 中原有的 get_proxy_from_api_url 函数签名完全一致，
    内部复用 IPProvider._fetch_proxy_from_vps 的实现，消除重复代码。
    
    :param api_url: IPDeep API URL
    :param api_user: 代理用户名（可选）
    :param api_pwd: 代理密码（可选）
    :param country_code: 国家代码（可选，默认US）
    :param use_cache: 是否使用本地缓存（默认True）
    :param force_refresh: 是否强制刷新（忽略缓存）
    :return: 代理信息字典 {success, proxy_host, proxy_port, ...}
    """
    provider = _global_ip_provider
    
    # 1. 检查缓存（如果启用）
    if use_cache and not force_refresh and api_url:
        with _proxy_cache_lock:
            cached = _proxy_cache.get(api_url)
            if cached and (time.time() - cached["timestamp"]) <= PROXY_CACHE_TTL:
                logger.debug(f"[代理缓存] 使用缓存代理 (API: {api_url[:50]}...)")
                return cached["data"]
    
    # 2. 构造临时代理配置并调用
    temp_proxy = {
        "proxy_api_url": api_url,
        "proxy_user": api_user,
        "proxy_pwd": api_pwd,
        "country_code": country_code,
        "enabled": True,
    }
    
    # 临时保存当前 proxy_pool
    original_pool = provider.proxy_pool
    try:
        provider.proxy_pool = [temp_proxy]
        provider.current_proxy = temp_proxy
        
        result = provider._fetch_proxy_from_vps(
            api_url=api_url,
            api_user=api_user,
            api_pwd=api_pwd,
            country_code=country_code
        )
        
        # 3. 如果成功，写入缓存
        if result.get("success") and use_cache and api_url:
            with _proxy_cache_lock:
                _proxy_cache[api_url] = {
                    "data": result,
                    "timestamp": time.time()
                }
                logger.debug(f"[代理缓存] 已缓存代理 (API: {api_url[:50]}...)")
        
        return result
    finally:
        # 恢复原始 proxy_pool
        provider.proxy_pool = original_pool


def invalidate_proxy_cache(api_url: str = None):
    """使代理缓存失效
    
    :param api_url: 指定的API URL，None则清空所有缓存
    """
    with _proxy_cache_lock:
        if api_url:
            if api_url in _proxy_cache:
                del _proxy_cache[api_url]
                logger.info(f"[代理缓存] 已清除指定缓存: {api_url[:50]}...")
        else:
            _proxy_cache.clear()
            logger.info("[代理缓存] 已清空所有代理缓存")


def check_ip_used_recently(ip: str) -> bool:
    """检查IP是否在去重间隔内使用过
    
    :param ip: 出口IP
    :return: 是否在间隔内使用过
    """
    now = time.time()
    
    # 先清理过期记录
    with _used_ips_lock:
        expired = [k for k, v in _used_ips.items() if (now - v) > IP_REUSE_INTERVAL]
        for k in expired:
            del _used_ips[k]
        
        return ip in _used_ips


def record_ip_use(ip: str):
    """记录IP使用时间
    
    :param ip: 出口IP
    """
    with _used_ips_lock:
        _used_ips[ip] = time.time()
        logger.debug(f"[IP去重] 记录IP使用: {ip}")


def get_used_ips_count() -> int:
    """获取去重池中IP数量"""
    with _used_ips_lock:
        return len(_used_ips)


# ============================================================
# C段(/24)分散策略：避免同一子网集中访问触发 AdSense 风控
# ============================================================
_c_segment_usage = {}  # {"x.x.x": [timestamp, ...]}
_c_segment_lock = threading.Lock()
C_SEGMENT_MAX_PER_WINDOW = 3  # 同一C段在窗口期内最多使用次数
C_SEGMENT_WINDOW_HOURS = 6   # C段统计窗口（小时）


def get_c_segment(ip: str) -> str:
    """提取IP的C段（/24子网前缀）"""
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3])
    return ip


def check_c_segment_diversity(ip: str) -> bool:
    """检查IP的C段是否过度集中
    
    :param ip: 出口IP
    :return: True=可用（C段未超限），False=应跳过（同C段使用过多）
    """
    c_seg = get_c_segment(ip)
    now = time.time()
    window_sec = C_SEGMENT_WINDOW_HOURS * 3600
    
    with _c_segment_lock:
        # 清理过期记录
        if c_seg in _c_segment_usage:
            _c_segment_usage[c_seg] = [
                ts for ts in _c_segment_usage[c_seg] if (now - ts) < window_sec
            ]
        else:
            _c_segment_usage[c_seg] = []
        
        return len(_c_segment_usage[c_seg]) < C_SEGMENT_MAX_PER_WINDOW


def record_c_segment_use(ip: str):
    """记录IP的C段使用"""
    c_seg = get_c_segment(ip)
    with _c_segment_lock:
        if c_seg not in _c_segment_usage:
            _c_segment_usage[c_seg] = []
        _c_segment_usage[c_seg].append(time.time())
    logger.debug(f"[C段分散] 记录C段使用: {c_seg}.0/24 (IP: {ip})")


def get_c_segment_stats() -> Dict:
    """获取C段分散统计信息"""
    now = time.time()
    window_sec = C_SEGMENT_WINDOW_HOURS * 3600
    with _c_segment_lock:
        active = {
            seg: len([ts for ts in timestamps if (now - ts) < window_sec])
            for seg, timestamps in _c_segment_usage.items()
        }
    return {
        "total_segments": len(active),
        "segments": active,
        "max_per_window": C_SEGMENT_MAX_PER_WINDOW,
        "window_hours": C_SEGMENT_WINDOW_HOURS
    }


# ============================================================
# 从 app.py 配置同步：便捷初始化函数
# ============================================================

def init_from_config(config: Dict) -> IPProvider:
    """从配置字典初始化并返回全局 IPProvider
    
    同时会更新缓存 TTL 和 IP 去重间隔（如果配置中有）。
    """
    global PROXY_CACHE_TTL, IP_REUSE_INTERVAL
    
    configure_ip_provider(config)
    
    # 更新缓存和去重参数（如果配置中有）
    if "proxy_cache_ttl" in config:
        PROXY_CACHE_TTL = int(config["proxy_cache_ttl"])
    if "ip_reuse_interval_hours" in config:
        IP_REUSE_INTERVAL = int(config["ip_reuse_interval_hours"]) * 3600
    
    logger.info(
        f"[IPProvider] 初始化完成: type={config.get('ip_provider_type', 'proxy_api')}, "
        f"vps={config.get('vps_host', '')}:{config.get('vps_new_port') or config.get('vps_port', 6666)}, "
        f"cache_ttl={PROXY_CACHE_TTL}s, dedup_interval={IP_REUSE_INTERVAL//3600}h"
    )
    
    return _global_ip_provider


# ============================================================
# 本机端 IP 缓存与去重机制（与 VPS 端 proxy_server.py 对应）
# ============================================================
import threading as _threading  # noqa: F811 (兼容下方原有代码)

_proxy_cache = {}  # {api_url: {"data": proxy_data, "timestamp": float}}
_proxy_cache_lock = _threading.Lock()
_used_ips = {}  # {ip: timestamp}
_used_ips_lock = _threading.Lock()
PROXY_CACHE_TTL = 600  # 代理缓存TTL（秒），默认10分钟
IP_REUSE_INTERVAL = 12 * 3600  # IP去重间隔（秒），默认12小时


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    print("=== 测试代理API方式（二层网络架构）===")
    provider = IPProvider("proxy_api")
    
    # 读取配置文件
    import os
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}
    
    provider.configure_proxy_api(
        proxy_pool=config.get("proxy_pool", []),
        vps_host=config.get("vps_host", "") or os.environ.get("VPS_HOST", ""),
        vps_port=int(config.get("vps_new_port") or config.get("vps_port", 6666)),
        vps_user=config.get("vps_user", "") or os.environ.get("VPS_USER", "admin"),
        vps_pass=config.get("vps_pass", "") or os.environ.get("VPS_PASS", ""),
        config=config
    )
    
    result = provider.get_ip()
    print("代理API方式:", result)
    
    if result.get("success"):
        print(f"获取到出口IP: {result.get('ip_info', {}).get('ip', 'unknown')}")
        print(f"代理配置: {result.get('proxy_host')}:{result.get('proxy_port')}")