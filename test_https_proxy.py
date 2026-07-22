#!/usr/bin/env python3
"""测试 HTTPS 通过 HTTP 代理（复现 407 问题）"""
import requests, base64, socket, ssl
from requests.auth import HTTPBasicAuth

api_url = "https://api.ipdeep.com/api/Pro/DynamicIp/GetIpByGenerateLink?id=5a4cN2JmMTI2Mjg1MDQ3MDY3MTgxMTM0"
api_user = "d8187332000-res-country-US-session-5461369000-sessiontime-5"
api_pwd = "zhan7263"

print("=== 获取代理 ===")
resp = requests.get(api_url, auth=HTTPBasicAuth(api_user, api_pwd), timeout=15)
parts = resp.text.strip().split(":")
host, port, user, pwd = parts[0], parts[1], parts[2], parts[3]
print(f"代理: {host}:{port}")
print(f"用户名: {user}")
print(f"密码: {pwd}")

proxy_url = f"http://{user}:{pwd}@{host}:{port}"

print("\n=== TEST A: requests HTTPS (ipinfo.io) ===")
try:
    r = requests.get("https://ipinfo.io/json", proxies={"https": proxy_url}, timeout=10)
    print(f"状态: {r.status_code}, 内容: {r.text[:200]}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== TEST B: requests HTTP (ip-api.com) ===")
try:
    r = requests.get("http://ip-api.com/json", proxies={"http": proxy_url}, timeout=10)
    print(f"状态: {r.status_code}, 内容: {r.text[:200]}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== TEST C: requests HTTPS with explicit header ===")
try:
    auth_b64 = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    headers = {"Proxy-Authorization": f"Basic {auth_b64}"}
    r = requests.get("https://ipinfo.io/json", proxies={"https": f"http://{host}:{port}"}, headers=headers, timeout=10)
    print(f"状态: {r.status_code}, 内容: {r.text[:200]}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== TEST D: Raw CONNECT + TLS to ipinfo.io ===")
try:
    s = socket.create_connection((host, int(port)), timeout=10)
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    connect_req = f"CONNECT ipinfo.io:443 HTTP/1.1\r\nHost: ipinfo.io:443\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
    s.sendall(connect_req.encode())
    resp_data = s.recv(4096).decode("utf-8", errors="ignore")
    status_line = resp_data.split("\r\n")[0]
    print(f"CONNECT: {status_line}")
    if "200" in status_line:
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname="ipinfo.io")
        get_req = "GET /json HTTP/1.1\r\nHost: ipinfo.io\r\nConnection: close\r\n\r\n"
        ss.send(get_req.encode())
        data = b""
        while True:
            chunk = ss.recv(4096)
            if not chunk:
                break
            data += chunk
        print(f"响应: {data[:300].decode('utf-8', errors='ignore')}")
    s.close()
except Exception as e:
    print(f"失败: {e}")

print("\n=== TEST E: requests HTTPS (freestoryweb.com) ===")
try:
    r = requests.get("https://freestoryweb.com", proxies={"https": proxy_url}, timeout=10, allow_redirects=False)
    print(f"状态: {r.status_code}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== TEST F: Check urllib3 version and proxy behavior ===")
import urllib3
print(f"urllib3 版本: {urllib3.__version__}")
print(f"requests 版本: {requests.__version__}")

# Check if ProxyManager sends auth on CONNECT
try:
    from urllib3.util.proxy import connection_requires_proxy_tunnel
    print("connection_requires_proxy_tunnel: available")
except ImportError:
    print("urllib3.util.proxy not available")
