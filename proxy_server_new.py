#!/usr/bin/env python3
"""
VPS双层代理服务器 - 完全重写版本
- 纯 socket 实现，避免 BaseHTTPRequestHandler 的问题
- 同时提供 API 和代理转发
- 架构: 本地 → VPS代理 → IPDeep → 目标网站
"""

import socket
import threading
import time
import base64
import requests
import urllib.parse
import logging
import json
import os
from io import BytesIO

# 配置
USER = "admin"
PASS = "admin123"
TIMEOUT = 60
PORT = 6666
SOCKS5_PORT = 1666
REQUIRE_PROXY_AUTH = True  # 6666 HTTP控制面/兼容HTTP代理保留认证
REQUIRE_SOCKS5_AUTH = False  # 1666浏览器数据面不认证：Chrome/Playwright SOCKS5认证兼容性差

# 全局状态
_current_ipdeep_proxy = None
_last_update = 0
PROXY_CACHE_TTL = 600  # 阶段2新服务：缓存10分钟，避免每个连接都刷新IPDeep
_proxy_refresh_lock = threading.Lock()
_DEFAULT_IPDEEP_API_URL = ""

# IP去重机制：记录IP和使用时间，12小时内不重复
_used_ips = {}  # {ip: timestamp}
IP_REUSE_INTERVAL = 12 * 3600  # 12小时 = 43200秒

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('proxy_server_new.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def check_auth(auth_header, request_type="unknown"):
    """
    检查Basic认证
    :param auth_header: Authorization 或 Proxy-Authorization 头的值
    :param request_type: 请求类型，用于日志（API/HTTP/HTTPS）
    :return: 认证是否成功
    """
    logger.debug(f"[{request_type}] 收到认证头: {auth_header[:60] if auth_header else 'None'}...")
    
    if not auth_header:
        logger.warning(f"[{request_type}] 认证失败：没有提供认证头")
        return False
    
    try:
        # 分割认证类型和值
        parts = auth_header.split(' ', 1)
        if len(parts) != 2:
            logger.warning(f"[{request_type}] 认证失败：认证头格式错误")
            return False
        
        auth_type, auth_value = parts
        auth_type = auth_type.lower()
        
        logger.debug(f"[{request_type}] 认证类型: {auth_type}")
        
        if auth_type != 'basic':
            logger.warning(f"[{request_type}] 认证失败：不支持的认证类型 '{auth_type}'")
            return False
        
        # 清理值（去除首尾空格）
        auth_value = auth_value.strip()
        logger.debug(f"[{request_type}] Base64 值长度: {len(auth_value)}")
        
        # 解码
        decoded_bytes = base64.b64decode(auth_value)
        decoded_str = decoded_bytes.decode('utf-8', errors='replace')
        logger.debug(f"[{request_type}] 解码后字符串: '{decoded_str}'")
        
        # 分割用户名和密码
        credentials = decoded_str.split(':', 1)
        if len(credentials) != 2:
            logger.warning(f"[{request_type}] 认证失败：凭证格式错误")
            return False
        
        username, password = credentials
        
        # 验证
        auth_success = username == USER and password == PASS
        
        if auth_success:
            logger.info(f"[{request_type}] ✅ 认证成功！用户: {username}")
        else:
            logger.warning(f"[{request_type}] ❌ 认证失败！用户: {username}, 期望用户: {USER}")
        
        return auth_success
        
    except base64.binascii.Error as e:
        logger.error(f"[{request_type}] Base64 解码失败: {e}")
        return False
    except UnicodeDecodeError as e:
        logger.error(f"[{request_type}] UTF-8 解码失败: {e}")
        return False
    except Exception as e:
        logger.error(f"[{request_type}] 认证处理异常: {e}", exc_info=True)
        return False


def get_or_refresh_ipdeep_proxy(api_url):
    """从IPDeep API获取代理信息，并按目标站访问速度筛选节点。"""
    global _current_ipdeep_proxy, _last_update, _used_ips

    now = time.time()

    expired_ips = [ip for ip, ts in _used_ips.items() if (now - ts) > IP_REUSE_INTERVAL]
    for ip in expired_ips:
        del _used_ips[ip]

    max_retries = 3
    speed_threshold = 8.0
    proxy_data = None
    best_slow_candidate = None

    for attempt in range(max_retries):
        logger.info(f"从IPDeep API获取新代理... (尝试 {attempt+1}/{max_retries})")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "*/*"
            }

            resp = requests.get(api_url, headers=headers, timeout=15)
            resp.raise_for_status()

            response_text = resp.text.strip()
            parts = response_text.split(":")
            if len(parts) < 4:
                raise Exception(f"IPDeep返回格式不正确: {response_text}")

            proxy_host = parts[0]
            proxy_port = parts[1]
            proxy_username = ":".join(parts[2:-1])
            proxy_password = parts[-1]

            logger.info(f"解析代理成功: {proxy_host}:{proxy_port}")

            proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_host}:{proxy_port}"
            ip_info = get_ip_details_proxy(proxy_url)
            exit_ip = ip_info.get("ip", "未知")

            if exit_ip in _used_ips:
                ip_age = now - _used_ips[exit_ip]
                if ip_age < IP_REUSE_INTERVAL:
                    logger.warning(f"IP {exit_ip} 在 {int(ip_age/3600)} 小时内用过，继续获取新IP...")
                    time.sleep(1)
                    continue

            target_fast, target_detail, target_elapsed, target_reachable = validate_target_site_proxy(
                proxy_url,
                timeout=speed_threshold,
                speed_threshold=speed_threshold,
            )

            candidate = {
                "success": True,
                "proxy_host": proxy_host,
                "proxy_port": proxy_port,
                "proxy_username": proxy_username,
                "proxy_password": proxy_password,
                "ip_info": ip_info,
                "target_test_time": target_elapsed,
                "target_test_detail": target_detail,
            }

            if target_fast:
                logger.info(f"✓ 目标站快节点通过: {target_detail}")
                proxy_data = candidate
                _used_ips[exit_ip] = now
                logger.info(f"✓ 新IP确认: {exit_ip} (已记录，12小时内不重复)")
                break

            if target_reachable:
                logger.warning(f"节点可用但超过 {speed_threshold:.0f}s 阈值，暂存为慢节点候选: {target_detail}")
                if best_slow_candidate is None or target_elapsed < best_slow_candidate[0]:
                    best_slow_candidate = (target_elapsed, candidate, exit_ip)
            else:
                logger.warning(f"节点目标站不可达，丢弃该节点: {target_detail}")

            time.sleep(1)

        except Exception as e:
            logger.error(f"获取代理失败: {e}")
            time.sleep(2)

    if proxy_data is None and best_slow_candidate is not None:
        best_elapsed, proxy_data, best_exit_ip = best_slow_candidate
        _used_ips[best_exit_ip] = now
        logger.warning(
            f"连续 {max_retries} 次未找到 <= {speed_threshold:.0f}s 的快节点，"
            f"使用本轮最快可用节点: ip={best_exit_ip}, time={best_elapsed:.2f}s"
        )

    if proxy_data is None:
        logger.error("所有重试都失败了，且没有可用慢节点候选")
        return {"success": False, "error": "多次尝试后仍无法获取可用IPDeep节点"}

    _current_ipdeep_proxy = proxy_data
    _last_update = now
    logger.info(f"代理缓存更新成功: target_time={proxy_data.get('target_test_time')}s")
    return proxy_data


def get_ip_details_proxy(proxy_url):
    """获取代理IP的详细信息"""
    logger.info(f"获取IP详情，代理: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
    
    # 多个备用API
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
                "language": get_language_from_country(data.get("countryCode", "US"))
            }
        },
        {
            "name": "ipinfo.io",
            "url": "http://ipinfo.io/json",
            "parser": lambda data: {
                "success": True,
                "ip": data.get("ip"),
                "country": data.get("country"),
                "country_code": data.get("country"),
                "region": data.get("region"),
                "city": data.get("city"),
                "timezone": data.get("timezone"),
                "isp": data.get("org"),
                "language": get_language_from_country(data.get("country", "US"))
            }
        }
    ]
    
    proxies = {"http": proxy_url, "https": proxy_url}
    
    for api in ip_apis:
        try:
            logger.info(f"尝试使用 {api['name']} ...")
            resp = requests.get(api["url"], proxies=proxies, timeout=6)
            logger.info(f"{api['name']} 响应状态码: {resp.status_code}")
            
            if resp.status_code != 200:
                logger.warning(f"{api['name']} 返回非200状态码: {resp.status_code}")
                continue
                
            data = resp.json()
            logger.debug(f"{api['name']} 响应数据: {data}")
            
            result = api["parser"](data)
            if result.get("success") and result.get("ip"):
                logger.info(f"✓ 通过 {api['name']} 获取IP成功: {result.get('ip')}")
                return result
        except Exception as e:
            logger.warning(f"API {api['name']} 失败: {e}", exc_info=True)
            continue
    
    # 所有API都失败时，返回默认数据
    logger.warning("所有IP查询API都失败，使用默认数据")
    return {
        "success": True,
        "ip": "1.1.1.1",
        "country": "United States",
        "country_code": "US",
        "region": "California",
        "city": "Los Angeles",
        "timezone": "America/Los_Angeles",
        "isp": "Cloudflare, Inc.",
        "language": "en-US"
    }


def validate_target_site_proxy(proxy_url, target_url="https://freestoryweb.com", timeout=8.0, speed_threshold=8.0):
    """缓存节点前验证目标站首页速度；返回(是否快节点, 明细, 耗时, 是否可达)。"""
    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.time()
    try:
        resp = requests.get(
            target_url,
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            stream=True,
        )
        elapsed = time.time() - start
        status = resp.status_code
        resp.close()
        reachable = 200 <= status < 400
        fast = reachable and elapsed <= speed_threshold
        logger.info(
            f"目标站测速: {target_url} status={status} time={elapsed:.2f}s "
            f"reachable={reachable} fast={fast} threshold={speed_threshold:.0f}s"
        )
        return fast, f"status={status}, time={elapsed:.2f}s", elapsed, reachable
    except Exception as e:
        elapsed = time.time() - start
        logger.warning(f"目标站测速失败: {target_url} time={elapsed:.2f}s error={e}")
        return False, f"error={e}, time={elapsed:.2f}s", elapsed, False


def get_language_from_country(country_code):
    """根据国家代码返回对应的语言"""
    if not country_code:
        return "en-US"
    country_code = country_code.upper()
    lang_map = {
        "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU",
        "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK", "JP": "ja-JP",
        "KR": "ko-KR", "DE": "de-DE", "FR": "fr-FR", "ES": "es-ES",
        "IT": "it-IT", "BR": "pt-BR", "RU": "ru-RU", "MX": "es-MX",
        "SA": "ar-SA", "AE": "ar-AE", "EG": "ar-EG"
    }
    return lang_map.get(country_code, "en-US")


def read_http_request(client_socket):
    """从 socket 读取完整的 HTTP 请求"""
    logger.debug("[read_http_request] 开始读取 HTTP 请求...")
    request_data = BytesIO()
    
    # 先读取请求头
    header_buffer = BytesIO()
    total_bytes_read = 0
    chunks_read = 0
    
    while True:
        chunk = client_socket.recv(4096)
        if not chunk:
            logger.warning(f"[read_http_request] 连接关闭！已读 {total_bytes_read} 字节，{chunks_read} 个块")
            return None
        
        chunks_read += 1
        total_bytes_read += len(chunk)
        header_buffer.write(chunk)
        header_bytes = header_buffer.getvalue()
        
        logger.debug(f"[read_http_request] 第 {chunks_read} 块，已读 {len(chunk)} 字节，累计 {total_bytes_read} 字节")
        
        # 调试：输出当前已读的原始内容前 200 字节
        preview = header_bytes[:200]
        logger.debug(f"[read_http_request] 当前已读内容预览: {repr(preview)}")
        
        # 检查是否读到了完整的请求头
        double_newline_pos = header_bytes.find(b'\r\n\r\n')
        if double_newline_pos != -1:
            logger.info(f"[read_http_request] ✅ 找到了完整请求头！位置: {double_newline_pos}")
            
            # 找到请求头结束的位置
            header_end = double_newline_pos
            request_data.write(header_bytes[:header_end + 4])
            
            # 输出完整请求头文本
            header_text = header_bytes[:header_end].decode('utf-8', errors='ignore')
            logger.info(f"[read_http_request] 完整请求头:\n{header_text}")
            
            # 解析内容长度
            content_length = 0
            
            for line in header_text.split('\r\n'):
                if line.lower().startswith('content-length:'):
                    try:
                        content_length = int(line.split(':', 1)[1].strip())
                        logger.debug(f"[read_http_request] 找到 Content-Length: {content_length}")
                        break
                    except:
                        pass
            
            # 如果有内容，继续读取
            if content_length > 0:
                remaining = content_length - (len(header_bytes) - header_end - 4)
                logger.debug(f"[read_http_request] 需要继续读取内容: {remaining} 字节")
                while remaining > 0:
                    chunk = client_socket.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    request_data.write(chunk)
                    remaining -= len(chunk)
            
            break
        elif total_bytes_read > 16384:
            # 安全防护，避免无限等待
            logger.warning(f"[read_http_request] 读取超过 16KB 仍未找到请求头结束，放弃")
            return None
    
    final_request = request_data.getvalue()
    logger.debug(f"[read_http_request] 读取完成，总大小: {len(final_request)} 字节")
    return final_request


def parse_http_request(request_bytes):
    """解析 HTTP 请求"""
    try:
        request_text = request_bytes.decode('utf-8', errors='ignore')
        lines = request_text.split('\r\n')
        
        if not lines:
            logger.warning("解析请求失败：没有内容")
            return None
        
        # 解析请求行
        request_line = lines[0]
        parts = request_line.split(' ', 2)
        if len(parts) < 3:
            logger.warning(f"解析请求失败：请求行格式错误: {request_line}")
            return None
        
        method, path, version = parts
        logger.debug(f"请求行: {method} {path} {version}")
        
        # 解析请求头
        headers = {}
        auth_headers_found = []
        for line in lines[1:]:
            if not line:
                break
            if ':' in line:
                key, value = line.split(':', 1)
                key_stripped = key.strip()
                value_stripped = value.strip()
                headers[key_stripped] = value_stripped
                
                # 记录认证相关的头
                if 'authorization' in key_stripped.lower():
                    auth_headers_found.append(f"{key_stripped}: {value_stripped[:60]}...")
        
        if auth_headers_found:
            logger.debug(f"找到认证相关头: {', '.join(auth_headers_found)}")
        
        # 获取消息体
        body_start = request_bytes.find(b'\r\n\r\n')
        body = request_bytes[body_start + 4:] if body_start != -1 else b''
        
        logger.debug(f"解析完成，共 {len(headers)} 个请求头")
        
        return {
            "method": method,
            "path": path,
            "version": version,
            "headers": headers,
            "body": body
        }
    except Exception as e:
        logger.error(f"解析HTTP请求失败: {e}")
        return None


def handle_api_request(client_socket, request):
    """处理 API 请求"""
    path = request["path"]
    headers = request["headers"]
    
    try:
        # /health 接口不需要认证
        if path == "/health":
            logger.info("健康检查请求 - 无需认证")
            import json
            response_json = {
                "status": "ok",
                "proxy_cached": _current_ipdeep_proxy is not None
            }
            response_body = json.dumps(response_json).encode()
            
            response_str = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "\r\n"
            )
            response = response_str.encode() + response_body
            client_socket.sendall(response)
            return
        
        # 其他 API 需要认证
        auth_header = headers.get("Authorization", "")
        if not check_auth(auth_header, request_type="API"):
            logger.warning("API认证失败")
            response = (
                b"HTTP/1.1 401 Unauthorized\r\n"
                b"Content-Type: application/json\r\n"
                b'WWW-Authenticate: Basic realm="Proxy"\r\n'
                b"\r\n"
                b'{"success": false, "error": "Unauthorized"}'
            )
            client_socket.sendall(response)
            return
        
        if path.startswith("/api/get_proxy") or path.startswith("/api/get-proxy"):
            # 解析查询参数
            if "?" in path:
                query_params = path.split("?", 1)[1]
                params = urllib.parse.parse_qs(query_params)
                api_url_encoded = params.get("api_url", [""])[0]
            else:
                api_url_encoded = ""
            
            if not api_url_encoded:
                response = (
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Content-Type: application/json\r\n"
                    b"\r\n"
                    b'{"success": false, "error": "Missing api_url parameter"}'
                )
                client_socket.sendall(response)
                return
            
            api_url = urllib.parse.unquote(api_url_encoded)
            logger.info(f"IPDeep API: {api_url}")
            
            # 获取代理信息
            proxy_data = get_or_refresh_ipdeep_proxy(api_url)
            
            if not proxy_data.get("success"):
                response = (
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Type: application/json\r\n"
                    b"\r\n"
                    b'{"success": false, "error": "Failed to get proxy"}'
                )
                client_socket.sendall(response)
                return
            
            # 获取本机 IP
            server_ip = client_socket.getsockname()[0]
            ip_info = proxy_data.get('ip_info', {})
            
            logger.info(f"返回代理: {server_ip}:{PORT}")
            logger.info(f"出口IP: {ip_info.get('ip')}")
            
            # 返回 JSON 响应
            response_json = {
                "success": True,
                "proxy_host": server_ip,
                "proxy_port": str(PORT),
                "proxy_username": USER,
                "proxy_password": PASS,
                "ip_info": ip_info
            }
            
            import json
            response_body = json.dumps(response_json).encode()
            
            # 先构建字符串，再一起编码
            response_str = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "\r\n"
            )
            response = response_str.encode() + response_body
            client_socket.sendall(response)
            return
        
        # 未知 API
        response = (
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Not Found"
        )
        client_socket.sendall(response)
        
    except Exception as e:
        logger.error(f"API处理失败: {e}", exc_info=True)
        response = (
            b"HTTP/1.1 500 Internal Server Error\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"success": false, "error": "Server error"}'
        )
        client_socket.sendall(response)


def handle_http_proxy(client_socket, request):
    """处理 HTTP 代理请求"""
    method = request["method"]
    path = request["path"]
    headers = request["headers"]
    body = request["body"]
    
    # 检查认证
    if REQUIRE_PROXY_AUTH:
        auth_header = headers.get("Proxy-Authorization", "")
        if not check_auth(auth_header, request_type="HTTP"):
            logger.warning("HTTP代理认证失败")
            response = (
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="Proxy"\r\n'
                b"\r\n"
            )
            client_socket.sendall(response)
            return
    else:
        logger.info("HTTP代理认证已禁用（调试模式）")
    
    if not _current_ipdeep_proxy:
        logger.error("没有缓存的IPDeep代理")
        response = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"\r\n"
        )
        client_socket.sendall(response)
        return
    
    try:
        proxy = _current_ipdeep_proxy
        proxy_url = f"http://{proxy['proxy_username']}:{proxy['proxy_password']}@{proxy['proxy_host']}:{proxy['proxy_port']}"
        
        # 构建请求头
        request_headers = {}
        for key, value in headers.items():
            if key.lower() not in ["proxy-authorization", "proxy-connection"]:
                request_headers[key] = value
        
        # 通过 IPDeep 代理转发
        resp = requests.request(
            method=method,
            url=path if path.startswith("http") else f"http://{headers.get('Host')}{path}",
            headers=request_headers,
            data=body,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True
        )
        
        # 构建响应
        response = f"HTTP/1.1 {resp.status_code} {resp.reason}\r\n".encode()
        for key, value in resp.headers.items():
            if key.lower() not in ["transfer-encoding", "connection"]:
                response += f"{key}: {value}\r\n".encode()
        response += b"\r\n"
        client_socket.sendall(response)
        
        # 发送响应体
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                client_socket.sendall(chunk)
                
    except Exception as e:
        logger.error(f"HTTP代理转发失败: {e}", exc_info=True)
        response = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"\r\n"
        )
        client_socket.sendall(response)


def handle_https_connect(client_socket, request):
    """处理 HTTPS CONNECT 请求"""
    path = request["path"]
    headers = request["headers"]
    
    # 检查认证
    if REQUIRE_PROXY_AUTH:
        auth_header = headers.get("Proxy-Authorization", "")
        if not check_auth(auth_header, request_type="HTTPS"):
            logger.warning("HTTPS代理认证失败")
            response = (
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="Proxy"\r\n'
                b"\r\n"
            )
            client_socket.sendall(response)
            return
    else:
        logger.info("HTTPS代理认证已禁用（调试模式）")
    
    if not _current_ipdeep_proxy:
        logger.error("没有缓存的IPDeep代理")
        response = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"\r\n"
        )
        client_socket.sendall(response)
        return
    
    try:
        logger.info("[HTTPS] 步骤1: 告诉客户端连接已建立")
        # 先告诉客户端连接已建立
        client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        
        # 解析目标地址
        target_host, target_port = path.split(":")
        target_port = int(target_port)
        
        # 安全限制：只允许常用端口
        ALLOWED_PORTS = {80, 443, 8080, 8443}
        if target_port not in ALLOWED_PORTS:
            logger.warning(f"[HTTPS] 拒绝访问非允许端口: {target_port}")
            response = (
                b"HTTP/1.1 403 Forbidden\r\n"
                b"\r\n"
            )
            client_socket.sendall(response)
            return
        
        logger.info(f"[HTTPS] 步骤2: 目标地址 {target_host}:{target_port}")
        
        # 连接到 IPDeep 代理
        proxy = _current_ipdeep_proxy
        logger.info(f"[HTTPS] 步骤3: 连接 IPDeep 代理 {proxy['proxy_host']}:{proxy['proxy_port']}")
        proxy_sock = socket.create_connection(
            (proxy['proxy_host'], int(proxy['proxy_port'])),
            timeout=TIMEOUT
        )
        logger.info("[HTTPS] 步骤3: IPDeep代理连接成功")
        
        # 发送 CONNECT 请求到 IPDeep 代理
        logger.info("[HTTPS] 步骤4: 发送 CONNECT 请求到 IPDeep")
        connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        connect_req += f"Host: {target_host}:{target_port}\r\n"
        
        if proxy.get('proxy_username') and proxy.get('proxy_password'):
            proxy_auth = base64.b64encode(
                f"{proxy['proxy_username']}:{proxy['proxy_password']}".encode()
            ).decode()
            connect_req += f"Proxy-Authorization: Basic {proxy_auth}\r\n"
        
        connect_req += "\r\n"
        proxy_sock.sendall(connect_req.encode())
        logger.info("[HTTPS] 步骤4: CONNECT 请求已发送")
        
        # 读取 IPDeep 的响应
        logger.info("[HTTPS] 步骤5: 等待 IPDeep 响应...")
        resp_buffer = BytesIO()
        while True:
            chunk = proxy_sock.recv(4096)
            if not chunk:
                logger.warning("[HTTPS] 步骤5: 收到空数据，连接关闭")
                break
            resp_buffer.write(chunk)
            if b'\r\n\r\n' in resp_buffer.getvalue():
                logger.info("[HTTPS] 步骤5: 收到完整响应头")
                break
        
        resp_data = resp_buffer.getvalue()
        resp_text = resp_data.decode('utf-8', errors='ignore')
        logger.info(f"[HTTPS] 步骤5: IPDeep 响应: {resp_text[:100]}")
        
        if not (resp_text.startswith('HTTP/1.1 200') or resp_text.startswith('HTTP/1.0 200')):
            logger.error(f"IPDeep代理连接失败: {resp_text[:100]}")
            proxy_sock.close()
            return
        
        logger.info("✓ HTTPS隧道建立成功，开始转发数据...")
        
        # 双向转发数据
        def forward(src, dst, direction):
            try:
                total_bytes = 0
                while True:
                    data = src.recv(8192)
                    if not data:
                        logger.debug(f"{direction} 转发结束: 连接关闭")
                        break
                    
                    total_bytes += len(data)
                    if total_bytes % 16384 == 0:  # 每16KB记录一次
                        logger.debug(f"{direction} 已转发 {total_bytes} 字节")
                    
                    dst.sendall(data)
                    
            except Exception as e:
                logger.warning(f"{direction} 转发中断: {e}")
            finally:
                logger.info(f"{direction} 关闭连接，总计转发 {total_bytes} 字节")
                try:
                    src.close()
                except:
                    pass
                try:
                    dst.close()
                except:
                    pass
        
        # 启动转发线程
        t1 = threading.Thread(target=forward, args=(client_socket, proxy_sock, "客户端→IPDeep"))
        t2 = threading.Thread(target=forward, args=(proxy_sock, client_socket, "IPDeep→客户端"))
        t1.start()
        t2.start()
        
        # 等待线程完成
        t1.join()
        t2.join()
        
    except Exception as e:
        logger.error(f"HTTPS CONNECT失败: {e}", exc_info=True)
        try:
            response = (
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"\r\n"
            )
            client_socket.sendall(response)
        except:
            pass


def _load_default_api_url():
    """从常见路径读取 ip_proxy_api，供1666 SOCKS5在未调用/api/get_proxy时自动预加载。"""
    global _DEFAULT_IPDEEP_API_URL
    if _DEFAULT_IPDEEP_API_URL:
        return _DEFAULT_IPDEEP_API_URL
    for cfg_path in ("/root/config.json", os.path.join(os.getcwd(), "config.json")):
        try:
            if not os.path.exists(cfg_path):
                continue
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            api_url = str(data.get("ip_proxy_api") or "").strip()
            if api_url:
                _DEFAULT_IPDEEP_API_URL = api_url
                logger.info(f"默认 IPDeep API URL 已加载: {api_url[:80]}...")
                return _DEFAULT_IPDEEP_API_URL
        except Exception as e:
            logger.warning(f"读取默认配置失败 {cfg_path}: {e}")
    return ""


def _ensure_ipdeep_proxy(api_url=None, reason="连接请求", force_refresh=False):
    """阶段2新服务：确保已有 IPDeep 上游。保留1.68的HTTP获取逻辑，只增加缓存与按需刷新。"""
    global _current_ipdeep_proxy, _last_update
    now = time.time()
    if not force_refresh and _current_ipdeep_proxy is not None and (now - _last_update) <= PROXY_CACHE_TTL:
        return True
    if not api_url:
        api_url = _load_default_api_url()
    if not api_url:
        logger.warning(f"[{reason}] 当前没有可用IPDeep代理，且未提供api_url，无法自动刷新")
        return _current_ipdeep_proxy is not None
    with _proxy_refresh_lock:
        now = time.time()
        if not force_refresh and _current_ipdeep_proxy is not None and (now - _last_update) <= PROXY_CACHE_TTL:
            return True
        logger.info(f"[{reason}] 刷新IPDeep代理...")
        proxy_data = get_or_refresh_ipdeep_proxy(api_url)
        if proxy_data and proxy_data.get("success"):
            _current_ipdeep_proxy = proxy_data
            _last_update = time.time()
            return True
        logger.error(f"[{reason}] 刷新IPDeep代理失败: {proxy_data}")
        return _current_ipdeep_proxy is not None


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("连接提前关闭")
        data += chunk
    return data


def _read_socks5_addr(sock):
    atyp = _recv_exact(sock, 1)[0]
    if atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, _recv_exact(sock, 4))
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        host = _recv_exact(sock, length).decode("utf-8", errors="ignore")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    else:
        raise ValueError(f"不支持的SOCKS5地址类型: {atyp}")
    port = int.from_bytes(_recv_exact(sock, 2), "big")
    return host, port


def _connect_upstream_http_connect(target_host, target_port):
    """SOCKS5数据面复用1.68的IPDeep HTTP CONNECT上游，保持阶段2新服务最小改动。"""
    if not _current_ipdeep_proxy:
        raise RuntimeError("没有可用的IPDeep代理")
    proxy = _current_ipdeep_proxy
    upstream = socket.create_connection((proxy['proxy_host'], int(proxy['proxy_port'])), timeout=TIMEOUT)
    connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
    connect_req += f"Host: {target_host}:{target_port}\r\n"
    if proxy.get('proxy_username') and proxy.get('proxy_password'):
        proxy_auth = base64.b64encode(
            f"{proxy['proxy_username']}:{proxy['proxy_password']}".encode()
        ).decode()
        connect_req += f"Proxy-Authorization: Basic {proxy_auth}\r\n"
    connect_req += "\r\n"
    upstream.sendall(connect_req.encode())

    resp_buffer = BytesIO()
    while True:
        chunk = upstream.recv(4096)
        if not chunk:
            raise ConnectionError("IPDeep CONNECT 响应为空")
        resp_buffer.write(chunk)
        if b"\r\n\r\n" in resp_buffer.getvalue():
            break
        if resp_buffer.tell() > 16384:
            raise ConnectionError("IPDeep CONNECT 响应头过大")
    resp_text = resp_buffer.getvalue().decode("utf-8", errors="ignore")
    status_line = resp_text.split("\r\n", 1)[0]
    if not (status_line.startswith("HTTP/1.1 200") or status_line.startswith("HTTP/1.0 200")):
        upstream.close()
        raise ConnectionError(f"IPDeep HTTP CONNECT 失败: {status_line}")
    return upstream


def _pipe_bidirectional(left, right, label):
    def forward(src, dst, direction):
        total = 0
        try:
            while True:
                data = src.recv(8192)
                if not data:
                    break
                total += len(data)
                dst.sendall(data)
        except Exception as e:
            logger.debug(f"{label} {direction} 转发结束: {e}")
        finally:
            logger.debug(f"{label} {direction} 共转发 {total} 字节")
            try:
                src.close()
            except Exception:
                pass
            try:
                dst.close()
            except Exception:
                pass

    t1 = threading.Thread(target=forward, args=(left, right, "客户端→上游"), daemon=True)
    t2 = threading.Thread(target=forward, args=(right, left, "上游→客户端"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def handle_socks5_client(client_socket, client_address):
    """阶段2新服务新增：SOCKS5数据面。8888 HTTP旧链路不受影响。"""
    logger.info(f"SOCKS5 新连接: {client_address}")
    client_socket.settimeout(TIMEOUT)
    try:
        ver = _recv_exact(client_socket, 1)[0]
        if ver != 5:
            raise ValueError("不是SOCKS5请求")
        nmethods = _recv_exact(client_socket, 1)[0]
        methods = _recv_exact(client_socket, nmethods)
        if REQUIRE_SOCKS5_AUTH:
            raise PermissionError("阶段2新服务不启用SOCKS5认证")
        if 0 not in methods:
            client_socket.sendall(b"\x05\xff")
            return
        client_socket.sendall(b"\x05\x00")

        ver, cmd, _rsv = _recv_exact(client_socket, 3)
        if ver != 5 or cmd != 1:
            client_socket.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        target_host, target_port = _read_socks5_addr(client_socket)
        logger.info(f"SOCKS5 CONNECT: {target_host}:{target_port}")

        _ensure_ipdeep_proxy(reason=f"SOCKS5 {target_host}:{target_port}")
        if not _current_ipdeep_proxy:
            raise RuntimeError("没有可用的IPDeep代理；自动刷新失败")
        try:
            upstream = _connect_upstream_http_connect(target_host, target_port)
        except Exception as first_error:
            logger.warning(
                f"SOCKS5 上游连接失败，强制刷新IPDeep节点后重试一次: "
                f"{target_host}:{target_port} error={first_error}"
            )
            _ensure_ipdeep_proxy(reason=f"SOCKS5重试 {target_host}:{target_port}", force_refresh=True)
            if not _current_ipdeep_proxy:
                raise RuntimeError("强制刷新后仍没有可用的IPDeep代理")
            upstream = _connect_upstream_http_connect(target_host, target_port)
        client_socket.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        _pipe_bidirectional(client_socket, upstream, f"SOCKS5 {target_host}:{target_port}")
    except Exception as e:
        logger.error(f"SOCKS5 连接处理失败: {e}", exc_info=True)
        try:
            client_socket.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass
        try:
            client_socket.close()
        except Exception:
            pass


def start_socks5_server():
    """启动1666 SOCKS5数据面；阶段2新服务不影响8888 HTTP旧链路。"""
    logger.info(f"正在启动SOCKS5数据面: 0.0.0.0:{SOCKS5_PORT}")
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("0.0.0.0", SOCKS5_PORT))
        server_socket.listen(128)
        logger.info(f"✓ SOCKS5数据面成功监听: 0.0.0.0:{SOCKS5_PORT}")
    except Exception as e:
        logger.error(f"SOCKS5服务器启动失败: {e}", exc_info=True)
        return
    while True:
        client_socket, client_address = server_socket.accept()
        threading.Thread(
            target=handle_socks5_client,
            args=(client_socket, client_address),
            daemon=True
        ).start()


def handle_client(client_socket, client_address):
    """处理客户端连接"""
    logger.info(f"新连接: {client_address}")
    client_socket.settimeout(TIMEOUT)
    
    try:
        # 读取请求
        request_bytes = read_http_request(client_socket)
        if not request_bytes:
            logger.warning("收到空请求")
            client_socket.close()
            return
        
        # 解析请求
        request = parse_http_request(request_bytes)
        if not request:
            logger.warning("无法解析请求")
            client_socket.close()
            return
        
        method = request["method"]
        path = request["path"]
        logger.info(f"请求: {method} {path}")
        
        # 判断请求类型
        if method == "CONNECT":
            # HTTPS 代理
            handle_https_connect(client_socket, request)
        elif path == "/health" or path.startswith("/api/"):
            # API 请求
            handle_api_request(client_socket, request)
        else:
            # HTTP 代理
            handle_http_proxy(client_socket, request)
            
    except Exception as e:
        logger.error(f"处理客户端连接失败: {e}", exc_info=True)
    finally:
        try:
            client_socket.close()
        except:
            pass


def start_server():
    """启动服务器"""
    logger.info("正在启动服务器...")
    
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        logger.info(f"尝试绑定: 0.0.0.0:{PORT}")
        server_socket.bind(('0.0.0.0', PORT))
        server_socket.listen(128)
        
        # 获取实际监听的地址
        sockname = server_socket.getsockname()
        logger.info(f"✓ 服务器成功监听: {sockname[0]}:{sockname[1]}")
        
        logger.info("=" * 60)
        logger.info("VPS双层代理服务器启动成功")
        logger.info(f"端口: {PORT}")
        logger.info(f"认证: {USER}/{PASS}")
        logger.info("架构: 本地 → VPS代理 → IPDeep → 目标网站")
        logger.info("=" * 60)
        logger.info(f"请确保防火墙是否开放: 已允许外部访问: python3 -c \"import socket; s=socket.create_connection(('127.0.0.1', {PORT})); print('OK')\" 测试本地可连接")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"服务器启动失败: {e}", exc_info=True)
        return
    logger.info("=" * 60)
    
    try:
        while True:
            client_socket, client_address = server_socket.accept()
            client_thread = threading.Thread(
                target=handle_client,
                args=(client_socket, client_address)
            )
            client_thread.daemon = True
            client_thread.start()
            
    except KeyboardInterrupt:
        logger.info("服务器关闭")
        server_socket.close()


if __name__ == "__main__":
    def _warmup_proxy():
        try:
            _ensure_ipdeep_proxy(reason="启动预热")
        except Exception as e:
            logger.warning(f"启动预热代理失败（不影响端口监听）: {e}")

    socks_thread = threading.Thread(target=start_socks5_server, daemon=True)
    socks_thread.start()
    threading.Thread(target=_warmup_proxy, daemon=True).start()
    start_server()
