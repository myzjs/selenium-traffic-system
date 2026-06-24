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
import subprocess
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
            vps_user = self.vps_config.get("user", "") or self._config.get("vps_user", "admin")
            vps_pass = self.vps_config.get("pass", "") or self._config.get("vps_pass", "admin123")
            
            resp = requests.get(
                full_url,
                auth=HTTPBasicAuth(vps_user, vps_pass),
                timeout=20
            )
            
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
                       part.startswith('172.') or ':' not in part:
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
import threading as _threading

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
        vps_host=config.get("vps_host", "104.129.54.64"),
        vps_port=int(config.get("vps_new_port") or config.get("vps_port", 6666)),
        vps_user=config.get("vps_user", "admin"),
        vps_pass=config.get("vps_pass", "admin123"),
        config=config
    )
    
    result = provider.get_ip()
    print("代理API方式:", result)
    
    if result.get("success"):
        print(f"获取到出口IP: {result.get('ip_info', {}).get('ip', 'unknown')}")
        print(f"代理配置: {result.get('proxy_host')}:{result.get('proxy_port')}")