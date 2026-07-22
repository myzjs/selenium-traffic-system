#!/usr/bin/env python3
"""测试 IPDeep 代理不同认证方式"""
import requests, base64, socket
from requests.auth import HTTPBasicAuth

api_url = "https://api.ipdeep.com/api/Pro/DynamicIp/GetIpByGenerateLink?id=5a4cN2JmMTI2Mjg1MDQ3MDY3MTgxMTM0"
api_user = "d8187332000-res-country-US-session-5461369000-sessiontime-5"
api_pwd = "zhan7263"

print("=== 1. 获取代理信息 ===")
resp = requests.get(api_url, auth=HTTPBasicAuth(api_user, api_pwd), timeout=15)
print(f"API响应: {resp.text}")
parts = resp.text.strip().split(":")
host, port, user, pwd = parts[0], parts[1], parts[2], parts[3]
print(f"代理: {host}:{port} user={user[:40]}... pwd={pwd}")

print("\n=== 2. 测试 http://user:pass@host:port ===")
try:
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    print(f"proxy_url: http://{user[:20]}:***@{host}:{port}")
    r = requests.get("http://ip-api.com/json", proxies={"http": proxy_url}, timeout=10)
    print(f"状态: {r.status_code}, 内容: {r.text[:200]}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== 3. 测试 HTTP CONNECT raw socket ===")
try:
    s = socket.create_connection((host, int(port)), timeout=10)
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    connect_req = f"CONNECT ip-api.com:80 HTTP/1.1\r\nHost: ip-api.com:80\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
    s.sendall(connect_req.encode())
    resp = s.recv(4096).decode("utf-8", errors="ignore")
    print(f"响应: {resp[:300]}")
    s.close()
except Exception as e:
    print(f"失败: {e}")

print("\n=== 4. 测试 http://host:port + Proxy-Authorization header ===")
try:
    proxy_url = f"http://{host}:{port}"
    auth_b64 = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    headers = {"Proxy-Authorization": f"Basic {auth_b64}"}
    r = requests.get("http://ip-api.com/json", proxies={"http": proxy_url}, headers=headers, timeout=10)
    print(f"状态: {r.status_code}, 内容: {r.text[:200]}")
except Exception as e:
    print(f"失败: {e}")

print("\n=== 5. 测试 HTTP CONNECT + GET through tunnel ===")
try:
    s = socket.create_connection((host, int(port)), timeout=10)
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    connect_req = f"CONNECT ip-api.com:80 HTTP/1.1\r\nHost: ip-api.com:80\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
    s.sendall(connect_req.encode())
    resp = s.recv(4096).decode("utf-8", errors="ignore")
    status_line = resp.split("\r\n")[0]
    print(f"CONNECT响应: {status_line}")
    if "200" in status_line:
        get_req = "GET /json HTTP/1.1\r\nHost: ip-api.com\r\nConnection: close\r\n\r\n"
        s.sendall(get_req.encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        print(f"GET响应: {data[:500].decode('utf-8', errors='ignore')}")
    s.close()
except Exception as e:
    print(f"失败: {e}")

print("\n=== 6. 测试 HTTPS via HTTP CONNECT ===")
try:
    s = socket.create_connection((host, int(port)), timeout=10)
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    connect_req = f"CONNECT ipinfo.io:443 HTTP/1.1\r\nHost: ipinfo.io:443\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
    s.sendall(connect_req.encode())
    resp = s.recv(4096).decode("utf-8", errors="ignore")
    status_line = resp.split("\r\n")[0]
    print(f"CONNECT ipinfo.io:443 响应: {status_line}")
    s.close()
except Exception as e:
    print(f"失败: {e}")
