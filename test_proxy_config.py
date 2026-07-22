#!/usr/bin/env python3
"""
测试代理配置和连接的脚本
"""

import requests
import json


def test_ipdeep_api():
    """测试IPDeep API"""
    print('=== 测试IPDeep API ===')
    try:
        response = requests.get('https://api.ipdeep.com/api/')
        print(f'Status code: {response.status_code}')
        print(f'Response: {response.text}')
        return True
    except Exception as e:
        print(f'Error: {e}')
        return False


def test_http_proxy():
    """测试HTTP代理 (6666端口)"""
    proxy_host = '104.129.54.64'
    print(f'\n=== 测试HTTP代理 ({proxy_host}:6666) ===')
    
    try:
        proxies = {
            'http': f'http://admin:admin123@{proxy_host}:6666',
            'https': f'http://admin:admin123@{proxy_host}:6666'
        }
        
        print('测试直接访问httpbin.org/ip:')
        response = requests.get('https://httpbin.org/ip', proxies=proxies, timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Response: {response.text}')
        
        print('测试访问目标网站:')
        response = requests.get('https://freestoryweb.com', proxies=proxies, timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Content length: {len(response.text)}')
        
        return True
    except Exception as e:
        print(f'Error: {e}')
        return False


def test_socks5_proxy():
    """测试SOCKS5代理 (1666端口)"""
    proxy_host = '104.129.54.64'
    print(f'\n=== 测试SOCKS5代理 ({proxy_host}:1666) ===')
    
    try:
        # 尝试使用requests的socks代理格式
        proxies = {
            'http': f'socks5://admin:admin123@{proxy_host}:1666',
            'https': f'socks5://admin:admin123@{proxy_host}:1666'
        }
        
        print('测试直接访问httpbin.org/ip:')
        response = requests.get('https://httpbin.org/ip', proxies=proxies, timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Response: {response.text}')
        
        print('测试访问目标网站:')
        response = requests.get('https://freestoryweb.com', proxies=proxies, timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Content length: {len(response.text)}')
        
        return True
    except Exception as e:
        print(f'Error: {e}')
        return False


def test_direct_connection():
    """测试直接连接"""
    print('\n=== 测试直接连接 ===')
    
    try:
        print('测试直接访问httpbin.org/ip:')
        response = requests.get('https://httpbin.org/ip', timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Response: {response.text}')
        
        print('测试直接访问目标网站:')
        response = requests.get('https://freestoryweb.com', timeout=10)
        print(f'Status code: {response.status_code}')
        print(f'Content length: {len(response.text)}')
        
        return True
    except Exception as e:
        print(f'Error: {e}')
        return False


def main():
    """主测试函数"""
    print('开始代理配置测试...')
    
    test_results = {
        'ipdeep_api': test_ipdeep_api(),
        'http_proxy': test_http_proxy(),
        'socks5_proxy': test_socks5_proxy(),
        'direct_connection': test_direct_connection()
    }
    
    print('\n=== 测试结果 ===')
    for test_name, passed in test_results.items():
        status = '✅' if passed else '❌'
        print(f'{status} {test_name}: {"成功" if passed else "失败"}')


if __name__ == '__main__':
    main()
