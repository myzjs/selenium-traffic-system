"""
IP 信息精准解析模块 - 1.0 版
功能：根据出口 IP 精准识别 country / timezone / language 三要素
策略：
  1. 优先使用代理已返回的字段
  2. 任一字段缺失时，调免费 IP 信息 API 补齐（ip-api.com → ipapi.co → ipinfo.io）
  3. 通过 country → language / timezone 的权威映射表兜底补齐
  4. 三者全部成功才返回 success=True，调用方据此决定是否舍弃当前 IP
"""
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Optional

logger = logging.getLogger("ip_info_resolver")

# ============================================================
# 国家 → 主要官方语言 映射（ISO 3166-1 alpha-2 国家代码 → BCP-47 语言）
# 只列出 SEO 区域所需的主流国家
# ============================================================
COUNTRY_TO_LANGUAGE = {
    # 中文
    "CN": "zh-CN", "HK": "zh-HK", "TW": "zh-TW", "MO": "zh-HK",
    # 英语为官方语言
    "US": "en-US", "GB": "en-GB", "UK": "en-GB",
    "CA": "en-CA", "AU": "en-AU", "NZ": "en-NZ",
    "IE": "en-IE", "SG": "en-SG", "MY": "en-MY",
    "PH": "en-PH", "IN": "en-IN", "ZA": "en-ZA",
    "NG": "en-NG", "KE": "en-KE", "GH": "en-GH",
    "JM": "en-JM", "PK": "en-PK", "BD": "en-BD",
    # 欧洲非英语
    "DE": "de-DE", "FR": "fr-FR", "IT": "it-IT", "ES": "es-ES",
    "NL": "nl-NL", "BE": "fr-BE", "SE": "sv-SE", "NO": "nb-NO",
    "DK": "da-DK", "FI": "fi-FI", "CH": "de-CH", "AT": "de-AT",
    "PL": "pl-PL", "PT": "pt-PT", "GR": "el-GR", "RU": "ru-RU",
    # 亚洲非英语
    "JP": "ja-JP", "KR": "ko-KR", "TH": "th-TH", "VN": "vi-VN",
    "ID": "id-ID", "TR": "tr-TR",
    # 拉美
    "BR": "pt-BR", "MX": "es-MX", "AR": "es-AR", "CL": "es-CL",
}

# ============================================================
# 国家 → 代表时区 映射（每国选最主要的时区，避免 None）
# ============================================================
COUNTRY_TO_TIMEZONE = {
    "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei", "MO": "Asia/Macau",
    "US": "America/New_York", "GB": "Europe/London", "UK": "Europe/London",
    "CA": "America/Toronto", "AU": "Australia/Sydney", "NZ": "Pacific/Auckland",
    "IE": "Europe/Dublin", "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur",
    "PH": "Asia/Manila", "IN": "Asia/Kolkata", "ZA": "Africa/Johannesburg",
    "NG": "Africa/Lagos", "KE": "Africa/Nairobi", "GH": "Africa/Accra",
    "JM": "America/Jamaica", "PK": "Asia/Karachi", "BD": "Asia/Dhaka",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "IT": "Europe/Rome", "ES": "Europe/Madrid",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
    "CH": "Europe/Zurich", "AT": "Europe/Vienna", "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon", "GR": "Europe/Athens", "RU": "Europe/Moscow",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "ID": "Asia/Jakarta", "TR": "Europe/Istanbul",
    "BR": "America/Sao_Paulo", "MX": "America/Mexico_City", "AR": "America/Argentina/Buenos_Aires",
    "CL": "America/Santiago",
}


def _normalize_country_code(country_raw: str) -> Optional[str]:
    """把任意形式的国家名/代码标准化为 ISO alpha-2 大写代码"""
    if not country_raw:
        return None
    s = country_raw.strip()
    if not s:
        return None
    # 已经是 2 位代码
    if len(s) == 2 and s.isalpha():
        return s.upper()
    # 常见全称 → 代码
    name_map = {
        "china": "CN", "people's republic of china": "CN", "prc": "CN", "中国": "CN",
        "united states": "US", "united states of america": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
        "united kingdom": "GB", "great britain": "GB", "england": "GB",
        "hong kong": "HK", "taiwan": "TW", "macau": "MO", "macao": "MO",
        "canada": "CA", "australia": "AU", "new zealand": "NZ", "ireland": "IE",
        "singapore": "SG", "malaysia": "MY", "philippines": "PH", "india": "IN",
        "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "ghana": "GH",
        "jamaica": "JM", "pakistan": "PK", "bangladesh": "BD",
        "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
        "netherlands": "NL", "belgium": "BE", "sweden": "SE", "norway": "NO",
        "denmark": "DK", "finland": "FI", "switzerland": "CH", "austria": "AT",
        "poland": "PL", "portugal": "PT", "greece": "GR", "russia": "RU",
        "japan": "JP", "korea": "KR", "south korea": "KR", "republic of korea": "KR",
        "thailand": "TH", "vietnam": "VN", "viet nam": "VN", "indonesia": "ID", "turkey": "TR",
        "brazil": "BR", "mexico": "MX", "argentina": "AR", "chile": "CL",
    }
    return name_map.get(s.lower())


def _http_get_json(url: str, timeout: float = 15.0) -> Optional[Dict]:
    """简单 HTTP GET JSON（用 stdlib，避免新增依赖）"""
    try:
        req = urllib.request.Request(
            url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.debug(f"HTTP 状态码错误 {resp.status} for {url}")
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as e:
        logger.debug(f"HTTP 请求失败 {url}: {e}")
        return None


def _query_ip_api(ip: str) -> Dict:
    """ip-api.com 免费版：45 次/分钟，无需 key"""
    data = _http_get_json(
        f"https://ip-api.com/json/{ip}?fields=status,countryCode,country,timezone,query"
    )
    if data and data.get("status") == "success":
        return {
            "country_code": data.get("countryCode"),
            "country_name": data.get("country"),
            "timezone": data.get("timezone"),
        }
    return {}


def _query_ipapi_co(ip: str) -> Dict:
    """ipapi.co 免费版：1000 次/天，无需 key"""
    data = _http_get_json(f"https://ipapi.co/{ip}/json/")
    if data and not data.get("error"):
        return {
            "country_code": data.get("country") or data.get("country_code"),
            "country_name": data.get("country_name"),
            "timezone": data.get("timezone"),
            "language": (data.get("languages") or "").split(",")[0] or None,
        }
    return {}


def _query_ipinfo_io(ip: str) -> Dict:
    """ipinfo.io 免费版：50000 次/月，无需 key"""
    data = _http_get_json(f"https://ipinfo.io/{ip}/json")
    if data and "country" in data:
        return {
            "country_code": data.get("country"),
            "country_name": None,
            "timezone": data.get("timezone"),
        }
    return {}


def resolve_ip_info(ip: str, proxy_ip_info: Optional[Dict] = None) -> Dict:
    """
    主入口：获取 country/timezone/language 三要素
    
    :param ip: 出口 IP
    :param proxy_ip_info: 代理返回的 ip_info 字典（可选），如果代理本身就返回了三要素则不调外部 API
    :return: {success: bool, ip, country_code, country_name, timezone, language, source}
    """
    result = {
        "success": False,
        "ip": ip,
        "country_code": None,
        "country_name": None,
        "timezone": None,
        "language": None,
        "source": [],
    }
    
    # ---- Step 1: 先用代理返回的字段 ----
    if proxy_ip_info:
        c_raw = proxy_ip_info.get("country") or proxy_ip_info.get("country_code") or ""
        cc = _normalize_country_code(c_raw)
        if cc:
            result["country_code"] = cc
            result["country_name"] = c_raw if len(c_raw) > 2 else None
            result["source"].append("proxy:country")
        
        tz = proxy_ip_info.get("timezone")
        if tz and tz != "未知":
            result["timezone"] = tz
            result["source"].append("proxy:timezone")
        
        lang = proxy_ip_info.get("language")
        if lang and lang != "未知" and "-" in str(lang):
            result["language"] = lang
            result["source"].append("proxy:language")
    
    # ---- Step 2: 缺失字段时调外部免费 API ----
    if not (result["country_code"] and result["timezone"]):
        for api_name, api_func in [
            ("ip-api", _query_ip_api),
            ("ipapi.co", _query_ipapi_co),
            ("ipinfo.io", _query_ipinfo_io),
        ]:
            api_data = api_func(ip)
            if not api_data:
                continue
            if not result["country_code"] and api_data.get("country_code"):
                cc = _normalize_country_code(api_data["country_code"])
                if cc:
                    result["country_code"] = cc
                    result["country_name"] = api_data.get("country_name") or result["country_name"]
                    result["source"].append(f"{api_name}:country")
            if not result["timezone"] and api_data.get("timezone"):
                result["timezone"] = api_data["timezone"]
                result["source"].append(f"{api_name}:timezone")
            if not result["language"] and api_data.get("language"):
                result["language"] = api_data["language"]
                result["source"].append(f"{api_name}:language")
            # 三者齐了就早停
            if result["country_code"] and result["timezone"] and result["language"]:
                break
    
    # ---- Step 3: 根据 country_code 映射兜底 language / timezone ----
    if result["country_code"]:
        if not result["language"]:
            lang = COUNTRY_TO_LANGUAGE.get(result["country_code"])
            if lang:
                result["language"] = lang
                result["source"].append("map:language")
        if not result["timezone"]:
            tz = COUNTRY_TO_TIMEZONE.get(result["country_code"])
            if tz:
                result["timezone"] = tz
                result["source"].append("map:timezone")
    
    # ---- Step 4: 三者必须齐全才 success ----
    if result["country_code"] and result["timezone"] and result["language"]:
        result["success"] = True
        if not result["country_name"]:
            result["country_name"] = result["country_code"]
    
    return result


# ==================== 自测 ====================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    test_ips = ["8.8.8.8", "118.103.66.83", "203.116.20.1"]  # 美国 / 中国 / 新加坡
    if len(sys.argv) > 1:
        test_ips = sys.argv[1:]
    for ip in test_ips:
        info = resolve_ip_info(ip)
        print(f"\n=== {ip} ===")
        for k, v in info.items():
            print(f"  {k}: {v}")
