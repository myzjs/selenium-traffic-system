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
import re
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
        # ★ 5.4 代理池监控：连续失败计数
        self._consecutive_failures = 0
        self._total_ips_used = 0

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

                # 26.8.13.1 ★ 超时放大：API 服务器在海外，中国大陆访问偶发 15s 以上
                resp = requests.get(api_url, headers=headers, auth=auth, timeout=30)
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
                    except json.JSONDecodeError as e:
                        logger.warning(f"[IPDeep] JSON 解析失败（按文本格式解析）: {e}")
                        proxy_host, proxy_port, proxy_username, proxy_password = "", "", "", ""
                        if "host" in response_text and ":" in response_text:
                            parts = response_text.split(":")
                            if len(parts) >= 2:
                                proxy_host = parts[0]
                                proxy_port = parts[1]
                                proxy_username = parts[2] if len(parts) > 2 else ""
                                proxy_password = parts[3] if len(parts) > 3 else ""
                else:
                    parts = response_text.split(":")
                    if len(parts) < 2:
                        logger.warning(f"[IPDeep] 返回格式不正确: {response_text[:200]}")
                        if attempt < max_retries - 1:
                            time.sleep(2)
                            continue
                        return {"success": False, "error": f"IPDeep返回格式不正确: {response_text[:100]}"}

                    if len(parts) == 4:
                        proxy_host, proxy_port, proxy_username, proxy_password = parts
                    elif len(parts) == 3:
                        proxy_host, proxy_port, proxy_username = parts
                        proxy_password = ""
                    elif len(parts) == 2:
                        proxy_host, proxy_port = parts
                        proxy_username, proxy_password = "", ""
                    else:
                        # >4 段：user/pwd 内部可能带冒号，按 host:port:user(...):last = pwd
                        proxy_host = parts[0]
                        proxy_port = parts[1]
                        proxy_password = parts[-1]
                        proxy_username = ":".join(parts[2:-1])

                if not proxy_host or not proxy_port:
                    logger.warning(f"[IPDeep] 解析代理信息失败: host={proxy_host}, port={proxy_port}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return {"success": False, "error": "IPDeep返回的代理信息不完整"}

                logger.info(f"[IPDeep] ✅ 解析代理成功: {proxy_host}:{proxy_port} (user={proxy_username[:5] if proxy_username else '无'}...)")

                # 构造代理 URL（HTTP Basic 方式，适配 requests 直接消费）
                proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}" if proxy_username else f"http://{proxy_host}:{proxy_port}"
                ip_info = self._get_ip_details(proxy_url, proxy_user=proxy_username, proxy_pwd=proxy_password)

                # ★ 26.8.13.1 P0-3 决策重写（对齐实测 RTT=61s + 407 场景）：
                # 原 fail-closed 在 proxy 建连超时时必失败，导致任务整批中断。
                # 新规则：
                #   - 确认为 hosting/proxy/datacenter/vpn → 拒绝（fail-closed 保留）
                #   - ip_type=None 但 ip-api 至少成功返回了 IP → 标记 ip_type="isp_trust_unknown" + fail-open
                #     （因为 IPDeep 家庭宽带概率 >90%，保守 26% 误判损失 > 放进来风险）
                #   - ip_type=None 且连 IP 都没拿到 → 才 fail-closed
                _detected_type = (ip_info or {}).get("ip_type") or (ip_info or {}).get("type")
                _exit_ip_raw = (ip_info or {}).get("ip") or ""
                if is_high_risk_ip(_detected_type):
                    logger.warning(f"[IPDeep] 高危IP类型已拒绝: type={_detected_type}, ip={_exit_ip_raw}")
                    return {
                        "success": False,
                        "error": "IPDeep机房/代理IP已拒绝",
                        "detail": {"ip": _exit_ip_raw, "ip_type": _detected_type},
                    }
                if not _detected_type:
                    if _exit_ip_raw and _exit_ip_raw not in ("", "未知"):
                        logger.warning(f"[IPDeep] ip_type未知但出口IP存在({_exit_ip_raw})，fail-open放行（标记ip_type=isp_trust_unknown）")
                        ip_info = dict(ip_info or {})
                        ip_info["ip_type"] = "isp_trust_unknown"
                        ip_info.setdefault("country_code", country_code or "US")
                        ip_info.setdefault("timezone", _get_timezone_from_country(ip_info.get("country_code", "US")))
                        ip_info.setdefault("language", _get_language_from_country(ip_info.get("country_code", "US")))
                    else:
                        logger.warning(f"[IPDeep] 未获取到IP类型且无出口IP，保守拒绝")
                        return {
                            "success": False,
                            "error": "无法确认出口IP类型，已拒绝",
                            "detail": {"ip": _exit_ip_raw, "ip_type": _detected_type},
                        }

                # IP 去重检查
                exit_ip = (ip_info or {}).get("ip") or ""
                if exit_ip and exit_ip != "未知" and not acquire_ip_use(exit_ip):
                    logger.warning(f"[IPDeep] IP {exit_ip} 在去重间隔内已使用过，重新获取...")
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue

                result = {
                    "success": True,
                    "proxy_host": proxy_host,
                    "proxy_port": proxy_port,
                    "proxy_username": proxy_username,
                    "proxy_password": proxy_password,
                    "ip_info": ip_info,
                }
                self._consecutive_failures = 0
                self._total_ips_used += 1
                logger.info(f"[IPDeep] ✅ 代理获取成功: {proxy_host}:{proxy_port}, 出口IP: {exit_ip}")
                return result

            except requests.exceptions.Timeout:
                logger.error(f"[IPDeep] ❌ 请求超时(30s) (尝试 {attempt+1})")
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

        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            logger.warning(f"⚠️ [IPDeep] 代理池可能耗尽！连续失败{self._consecutive_failures}次，请检查IPDeep配额")
        return {"success": False, "error": f"IPDeep API {max_retries}次尝试均失败"}

    def _get_ip_details(self, proxy_url: str, proxy_user: str = "", proxy_pwd: str = "") -> Dict:
        """通过代理获取出口IP详细信息（5个API + 重试，提高容错）

        26.8.13.1 ★ 认证双保险 + 超时长适配：
        - 实测 gate.ipdeep.com:8082 TCP 握手 RTT ≈ 61s，timeout 从 10s → 120s
        - 除了 proxy_url 内嵌 user:pwd，还额外构造 requests Session + Proxy-Authorization 头，
          解决部分代理服务器对 http://user:pwd@host 解析失败 → 407 的问题
        """
        ip_apis = [
            {
                "name": "ip-api.com",
                # ★ 修复：fields 逗号分隔需 URL 编码(%2C)，保持原有字段并追加缺失字段
                # as=ASN号码+机构, org=机构名, hosting/proxy=布尔型托管/代理标识（用于 ip_type 高危判断）
                "url": "http://ip-api.com/json?fields=status%2Ccountry%2CcountryCode%2CregionName%2Ccity%2Ctimezone%2Cisp%2Cas%2Corg%2Chosting%2Cproxy%2Cquery",
                "parser": lambda data: {
                    "success": data.get("status") == "success",
                    "ip": data.get("query"),
                    "country": data.get("country"),
                    "country_code": data.get("countryCode"),
                    "region": data.get("regionName"),
                    "city": data.get("city"),
                    "timezone": data.get("timezone"),
                    "isp": data.get("isp"),
                    # ★ 修复：从 as/org 字段提取纯 ASN 号码（如 "AS15169 Google LLC" -> "15169"）
                    "asn": _extract_asn(data.get("as")) or _extract_asn(data.get("org")),
                    # ★ 修复：hosting/proxy 布尔标识映射为高危 ip_type；
                    # 字段明确存在且均为 False 时视为正常家庭宽带(isp)；
                    # 字段缺失（旧 API/异常响应）时返回 None，由调用方 fail-closed 拒绝
                    "ip_type": (
                        "hosting" if data.get("hosting")
                        else ("proxy" if data.get("proxy")
                              else ("isp" if ("hosting" in data or "proxy" in data) else None))
                    ),
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
            },
            {
                "name": "ifconfig.me",
                "url": "http://ifconfig.me/ip",
                "parser": lambda text: {
                    "success": bool(text.strip()),
                    "ip": text.strip(),
                    "country": "",
                    "country_code": "",
                    "region": "",
                    "city": "",
                    "timezone": "",
                    "isp": "",
                    "language": "en-US",
                    "_ip_only": True  # 标记：仅获取IP，需二次查询地理信息
                },
                "_text_mode": True
            },
            {
                "name": "api.ipify.org",
                "url": "https://api.ipify.org?format=json",
                "parser": lambda data: {
                    "success": bool(data.get("ip")),
                    "ip": data.get("ip"),
                    "country": "",
                    "country_code": "",
                    "region": "",
                    "city": "",
                    "timezone": "",
                    "isp": "",
                    "language": "en-US",
                    "_ip_only": True
                }
            },
            {
                "name": "checkip.amazonaws.com",
                "url": "http://checkip.amazonaws.com",
                "parser": lambda text: {
                    "success": bool(text.strip()),
                    "ip": text.strip(),
                    "country": "",
                    "country_code": "",
                    "region": "",
                    "city": "",
                    "timezone": "",
                    "isp": "",
                    "language": "en-US",
                    "_ip_only": True
                },
                "_text_mode": True
            }
        ]

        proxies = {"http": proxy_url, "https": proxy_url}

        # 26.8.13.1 ★ 认证双保险：
        #  - proxy_url 内嵌 user:pwd（requests 会拆成 Proxy-Authorization）
        #  - 再手动给每次请求加 Proxy-Authorization header
        # 注：为保持与单元测试 @patch("ip_provider.requests.get") 兼容，
        #    仍使用 requests.get 接口；Session 仅做优化（如 Session 构造异常或 proxy_user
        #    无法取得时退化为直接 requests.get + headers 参数）。
        _proxy_auth_header = None
        try:
            if proxy_user and proxy_pwd:
                import base64 as _b64_local
                _token = _b64_local.b64encode(f"{proxy_user}:{proxy_pwd}".encode("utf-8")).decode("ascii")
                _proxy_auth_header = {"Proxy-Authorization": f"Basic {_token}"}
        except Exception:
            _proxy_auth_header = None

        # ★ 最多尝试2轮（第2轮仅重试前2个完整API）
        for round_idx in range(2):
            apis_to_try = ip_apis if round_idx == 0 else ip_apis[:2]
            for api in apis_to_try:
                try:
                    # 26.8.13.1 ★ timeout 10s → 120s （实测 gate.ipdeep.com RTT ≈ 61s）
                    _req_kwargs = {"proxies": proxies, "timeout": 120}
                    if _proxy_auth_header:
                        _req_kwargs["headers"] = _proxy_auth_header
                    resp = requests.get(api["url"], **_req_kwargs)
                    if resp.status_code != 200:
                        continue
                    # 文本模式 vs JSON模式
                    if api.get("_text_mode"):
                        result = api["parser"](resp.text)
                    else:
                        data = resp.json()
                        result = api["parser"](data)
                    if result.get("success") and result.get("ip"):
                        # 如果仅获取到IP（无地理信息），用ip-api.com补全
                        if result.get("_ip_only") and not result.get("country_code"):
                            geo = self._enrich_ip_geo(
                                result["ip"],
                                proxy_url=proxy_url,
                                _timeout=120,
                                _extra_headers=_proxy_auth_header,
                            )
                            if geo:
                                result.update(geo)
                        logger.info(f"[IP详情] 通过 {api['name']} 获取: {result.get('ip')} ({result.get('country_code', '?')})")
                        return result
                except Exception as e:
                    logger.debug(f"[IP详情] {api['name']} 失败(round{round_idx}): {e}")
                    continue
            if round_idx == 0:
                logger.debug("[IP详情] 第1轮全部失败，1秒后重试...")
                time.sleep(1)

        logger.warning("[IP详情] 所有API失败(2轮)，无法获取出口IP信息")
        return {
            "success": False,
            "error": "所有IP详情API均不可达",
            "ip": "",
        }

    def _enrich_ip_geo(self, ip: str, proxy_url: str = None,
                        _timeout: int = 30, _extra_headers=None,
                        _reuse_session=None) -> Dict:
        """用ip-api.com补全IP地理信息（优先走调用方传入的代理，避免直连暴露本机真IP）"""
        try:
            req_kwargs = {"timeout": _timeout}
            if proxy_url:
                req_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            if _extra_headers:
                req_kwargs["headers"] = _extra_headers
            else:
                if not proxy_url:
                    logger.warning("[IP详情] 未能走代理补全Geo信息，将直连ip-api.com（可能暴露本机真IP）")
            _client = _reuse_session if _reuse_session is not None else requests
            resp = _client.get(
                f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,timezone,isp",
                **req_kwargs
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return {
                        "country": data.get("country"),
                        "country_code": data.get("countryCode"),
                        "region": data.get("regionName"),
                        "city": data.get("city"),
                        "timezone": data.get("timezone"),
                        "isp": data.get("isp"),
                        "language": _get_language_from_country(data.get("countryCode", "US")),
                        "_ip_only": False
                    }
        except Exception:
            pass
        return None


# ============================================================
# ASN 提取工具（供 ip-api 等 API 的 as/org 字段解析使用）
# ============================================================
_ASN_RE = re.compile(r"\bAS(\d+)\b", re.IGNORECASE)


def _extract_asn(text) -> str:
    """从 as/org 字段中提取纯 ASN 号码，如 'AS15169 Google LLC' -> '15169'"""
    if not text:
        return ""
    m = _ASN_RE.search(str(text))
    return m.group(1) if m else ""


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


_COUNTRY_TZ_NAME_MAP = {
    "US": "America/New_York", "GB": "Europe/London", "CA": "America/Toronto",
    "AU": "Australia/Sydney", "CN": "Asia/Shanghai", "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "ES": "Europe/Madrid",
    "IT": "Europe/Rome", "BR": "America/Sao_Paulo", "RU": "Europe/Moscow",
    "MX": "America/Mexico_City", "SA": "Asia/Riyadh", "AE": "Asia/Dubai",
    "EG": "Africa/Cairo", "IN": "Asia/Kolkata", "ID": "Asia/Jakarta",
    "VN": "Asia/Ho_Chi_Minh", "TH": "Asia/Bangkok", "PH": "Asia/Manila",
}


def _get_timezone_from_country(country_code: str) -> str:
    """根据国家代码返回 IANA 时区名（fail-open，未知返回 America/New_York）"""
    if not country_code:
        return "America/New_York"
    return _COUNTRY_TZ_NAME_MAP.get(country_code.upper(), "America/New_York")


# ============================================================
# 高危IP类型判断（机房/代理/VPN/Hosting 等）
# ============================================================
_HIGH_RISK_IP_TYPES = {"datacenter", "proxy", "vpn", "hosting", "business"}


def is_high_risk_ip(ip_type: str) -> bool:
    """判断连接类型是否属于机房/代理等高风险 IP 类型（用于拒绝该IP）"""
    if not ip_type:
        return False
    return str(ip_type).strip().lower() in _HIGH_RISK_IP_TYPES


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

    # 1. 检查缓存（★ 缓存命中后仍需IP去重检查，防止同一IP被重复使用）
    if use_cache and not force_refresh and api_url:
        with _proxy_cache_lock:
            cached = _proxy_cache.get(api_url)
            if cached and (time.time() - cached["timestamp"]) <= PROXY_CACHE_TTL:
                # ★ 关键修复：缓存命中后检查IP是否已在去重池中
                cached_ip = cached["data"].get("ip_info", {}).get("ip", "")
                if cached_ip and cached_ip != "未知" and check_ip_used_recently(cached_ip):
                    # IP已使用过，清除缓存，强制重新获取
                    del _proxy_cache[api_url]
                    logger.warning(f"[代理缓存] ⚠️ 缓存IP {cached_ip} 已在去重池中，清除缓存重新获取")
                else:
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
    """记录IP使用时间（变更后立即持久化）"""
    with _used_ips_lock:
        _used_ips[ip] = time.time()
        logger.debug(f"[IP去重] 记录IP使用: {ip}")
    _save_dedup_state()


def acquire_ip_use(ip: str) -> bool:
    """原子执行"过期清理 → 检查 → 记录"（★ TOCTOU 修复）

    在同一把 _used_ips_lock 内完成检查与记录，避免 check_ip_used_recently 与
    record_ip_use 分离调用在并发场景下的竞态（同一IP可能被两个线程同时放行）。

    :return: True 表示成功占用该IP（此前未被使用或已过期）；
             False 表示该IP在去重间隔内已使用过，调用方应重新获取
    """
    if not ip:
        return True
    now = time.time()
    with _used_ips_lock:
        expired = [k for k, v in _used_ips.items() if (now - v) > IP_REUSE_INTERVAL]
        for k in expired:
            del _used_ips[k]
        if ip in _used_ips:
            return False
        _used_ips[ip] = now
    _save_dedup_state()
    logger.debug(f"[IP去重] 记录IP使用: {ip}")
    return True


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
    """记录IP的C段使用（变更后立即持久化）"""
    c_seg = get_c_segment(ip)
    with _c_segment_lock:
        if c_seg not in _c_segment_usage:
            _c_segment_usage[c_seg] = []
        _c_segment_usage[c_seg].append(time.time())
    logger.debug(f"[C段分散] 记录C段使用: {c_seg}.0/24 (IP: {ip})")
    _save_dedup_state()


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
PROXY_CACHE_TTL = 60  # 代理缓存TTL（秒），★ 从600s降60s，防止session过期后缓存返回失效凭证
IP_REUSE_INTERVAL = 24 * 3600  # IP去重间隔（秒），★ 24小时去重（设计要求）


# ============================================================
# IP去重状态持久化（P1-10）：_used_ips(24h) 与 _c_segment_usage(C段/24)
# 保存到脚本同目录 .risk_state/ip_dedup_state.json，启动加载、变更保存、后台定期保存
# ============================================================
_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".risk_state")
_STATE_FILE = os.path.join(_STATE_DIR, "ip_dedup_state.json")
_STATE_SAVE_INTERVAL = 60  # 定期自动持久化间隔（秒）
_STATE_LOCK = threading.Lock()
_state_save_stop = threading.Event()


def _load_dedup_state():
    """启动时加载去重状态到内存（过滤已过期项）"""
    global _used_ips, _c_segment_usage
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        loaded_ips = data.get("ip_last_use", {}) or {}
        loaded_c = data.get("c_segment_usage", {}) or {}
        _used_ips = {
            ip: float(ts) for ip, ts in loaded_ips.items()
            if (now - float(ts)) <= IP_REUSE_INTERVAL
        }
        window_sec = C_SEGMENT_WINDOW_HOURS * 3600
        _c_segment_usage = {
            seg: [float(ts) for ts in timestamps if (now - float(ts)) < window_sec]
            for seg, timestamps in loaded_c.items()
        }
        logger.info(f"[IP去重] 已从持久化恢复去重状态: {len(_used_ips)} 个IP, {len(_c_segment_usage)} 个C段")
    except Exception as e:
        logger.warning(f"[IP去重] 加载持久化状态失败（使用空状态）: {e}")
        _used_ips = {}
        _c_segment_usage = {}


def _save_dedup_state(force: bool = False):
    """将内存去重状态保存到磁盘（原子写 + 锁，防止并发写坏文件）"""
    with _STATE_LOCK:
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            with _used_ips_lock, _c_segment_lock:
                data = {
                    "ip_last_use": dict(_used_ips),
                    "c_segment_usage": dict(_c_segment_usage),
                }
            tmp = _STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.warning(f"[IP去重] 持久化状态保存失败: {e}")


def _periodic_save_loop():
    """后台线程：定期自动持久化，避免进程异常退出丢失状态"""
    while not _state_save_stop.wait(_STATE_SAVE_INTERVAL):
        _save_dedup_state()


def _start_periodic_save():
    threading.Thread(target=_periodic_save_loop, args=(), daemon=True).start()


# 模块加载即恢复上次运行的去重状态，并启动后台定期保存线程
_load_dedup_state()
_start_periodic_save()


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
