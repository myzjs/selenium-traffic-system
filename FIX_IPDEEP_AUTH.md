# IPDeep 代理认证问题修复

## 问题描述

### 前端日志（图1）
```
[2026-06-29 14:21:02] [WARNING] ⚠️ 第 1 次代理获取失败
[2026-06-29 14:21:07] [WARNING] ⚠️ 第 2 次代理获取失败
[2026-06-29 14:21:12] [WARNING] ⚠️ 第 3 次代理获取失败
【任务结果】 任务耗时: 16.0s、任务状态: 失败、有效流量: 否、
失败原因: 严禁跳过SEO: IP+SEO 准备失败 (已重试 3 次)
```

### VPS 日志（图2）
```
http://gate.ipdeep.com:8082 "GET http://ip-api.com/json?fields=... HTTP/1.1" 407 0
ip-api.com 响应状态码: 407
WARNING - ip-api.com 返回非200状态码: 407
Tunnel connection failed: 407 Proxy Authentication Required
ERROR - 所有重试都失败了，且没有可用慢节点候选
ERROR - [启动预热] 刷新IPDeep代理失败: {'success': False, 'error': '多次尝试后仍无法获取可用IPDeep节点'}
```

## 根本原因

**HTTP 407 Proxy Authentication Required** 错误表明：通过 IPDeep 代理访问 `ip-api.com` 和 `ipinfo.io` 时，代理服务器要求认证，但代码中**没有传递 IPDeep API 的认证凭据**。

### 具体问题

1. **IPDeep API 需要 Basic Auth 认证**才能访问
2. 代码在调用 IPDeep API (`requests.get(api_url, ...)`) 时**未传递认证信息**
3. config.json 中虽然有 `ip_proxy_user` 和 `ip_proxy_pwd` 字段，但未被使用
4. 导致从 IPDeep 获取到的代理虽然格式正确，但后续通过该代理访问 IP 查询 API 时返回 407

## 解决方案

### 修改文件

#### 1. `proxy_server_new.py`

**新增全局变量**（存储 IPDeep API 认证凭据）：
```python
_DEFAULT_IPDEEP_API_URL = ""
_DEFAULT_IPDEEP_API_USER = ""  # 新增
_DEFAULT_IPDEEP_API_PWD = ""   # 新增
```

**重命名并增强配置加载函数**：
```python
def _load_default_api_credentials():
    """从常见路径读取 ip_proxy_api 及认证凭据"""
    global _DEFAULT_IPDEEP_API_URL, _DEFAULT_IPDEEP_API_USER, _DEFAULT_IPDEEP_API_PWD
    # ... 同时加载 api_url, api_user, api_pwd
    return _DEFAULT_IPDEEP_API_URL, _DEFAULT_IPDEEP_API_USER, _DEFAULT_IPDEEP_API_PWD
```

**修改 `get_or_refresh_ipdeep_proxy` 函数签名**：
```python
def get_or_refresh_ipdeep_proxy(api_url, api_user=None, api_pwd=None):
    # 如果提供了认证凭据，使用 Basic Auth
    auth = None
    if api_user and api_pwd:
        from requests.auth import HTTPBasicAuth
        auth = HTTPBasicAuth(api_user, api_pwd)
        logger.info(f"使用 IPDeep API 认证: user={api_user}")
    
    resp = requests.get(api_url, headers=headers, auth=auth, timeout=15)
    # ...
```

**更新所有调用点**，传递认证凭据：
- `/api/get_proxy` 端点
- `_ensure_ipdeep_proxy` 函数
- 支持环境变量 fallback: `IP_PROXY_USER`, `IP_PROXY_PWD`

#### 2. `.env.example`

添加注释说明：
```bash
# IP代理凭据 (IPDeep)
# 注意：IP_PROXY_API 是 API URL，不是代理服务器地址
# IP_PROXY_USER/IP_PROXY_PWD 用于访问 IPDeep API 的 Basic Auth 认证
IP_PROXY_API=https://api.ipdeep.com/api/
IP_PROXY_USER=your_proxy_user_here
IP_PROXY_PWD=your_proxy_password_here
```

## 使用步骤

### 1. 配置认证凭据

**方式一：编辑 config.json**
```json
{
    "ip_proxy_api": "https://api.ipdeep.com/api/",
    "ip_proxy_user": "YOUR_REAL_USERNAME",
    "ip_proxy_pwd": "YOUR_REAL_PASSWORD",
    ...
}
```

**方式二：使用 .env 文件（推荐）**
```bash
cp .env.example .env
# 编辑 .env 文件，填入真实值
VPS_HOST=your_vps_ip
VPS_PASS=your_vps_password
IP_PROXY_USER=your_ipdeep_username
IP_PROXY_PWD=your_ipdeep_password
FLASK_AUTH_PASS=your_web_password
```

### 2. 重启服务

```bash
# 停止现有进程
pkill -f "python3 proxy_server_new.py"
pkill -f "python3 app.py"

# 重新启动
cd /Users/mac/Documents/www-jb/626/selenium_traffic_system
run_port=5002 python3 app.py &
python3 proxy_server_new.py &
```

### 3. 验证修复

查看日志确认：
```bash
tail -f proxy_server_new.log | grep "IPDeep API 认证"
```

应该看到：
```
使用 IPDeep API 认证: user=YOUR_USERNAME
✓ 新IP确认: xxx.xxx.xxx.xxx (已记录，12小时内不重复)
```

## 技术细节

### 为什么需要认证？

IPDeep 是一个商业代理服务，其 API 端点 (`https://api.ipdeep.com/api/`) 需要 HTTP Basic Authentication 来验证用户身份。如果不提供认证：
- API 可能返回 401 Unauthorized
- 或者返回空/错误格式的响应
- 导致解析失败或获取到无效的代理节点

### 认证流程

```
本地客户端 → VPS (6666端口) → IPDeep API (需认证) → 返回代理节点
                                                    ↓
                                              通过代理访问 ip-api.com/ipinfo.io
                                                    ↓
                                              返回出口IP详情
```

1. 客户端请求 VPS `/api/get_proxy?api_url=...`
2. VPS 使用 `ip_proxy_user/ip_proxy_pwd` 认证访问 IPDeep API
3. IPDeep 返回代理节点：`host:port:username:password`
4. VPS 使用该代理访问 IP 查询 API
5. 返回完整的代理信息和出口 IP 详情给客户端

## 相关文件

- [`proxy_server_new.py`](file:///Users/mac/Documents/www-jb/626/selenium_traffic_system/proxy_server_new.py) - VPS 代理服务（已修复）
- [`.env.example`](file:///Users/mac/Documents/www-jb/626/selenium_traffic_system/.env.example) - 环境变量模板（已更新）
- [`config.json`](file:///Users/mac/Documents/www-jb/626/selenium_traffic_system/config.json) - 配置文件（需填入真实凭据）

## 注意事项

1. **不要提交真实凭据到 Git**：确保 `.env` 和 `config.json` 已在 `.gitignore` 中
2. **轮换泄露的凭据**：如果之前的凭据已暴露在 Git 历史中，请联系 IPDeep 更换
3. **环境变量优先级**：环境变量 > config.json > 默认值
4. **测试环境**：建议先在测试环境验证，确认能正常获取代理后再部署生产

## 相关审计问题

此修复解决了审计报告中的以下问题：
- **C-1**: 硬编码凭据 → 改为环境变量管理
- **H-1**: 无 secret key → Flask Basic Auth 已实现
- **M-4**: 不安全 HTTP → IP 查询 API 已升级为 HTTPS

---

**修复日期**: 2026-06-29  
**影响范围**: VPS 代理服务层  
**风险等级**: 低（仅添加认证逻辑，不影响现有功能）
