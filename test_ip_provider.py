#!/usr/bin/env python3
"""
测试IP获取功能 - 验证二层网络架构
"""
import json
import logging
import sys
import os

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_ip_provider")

def load_config():
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def test_proxy_api():
    """测试通过VPS代理服务获取IPDeep代理"""
    logger.info("=== 测试代理API方式（二层网络架构）===")
    
    config = load_config()
    logger.info(f"配置文件加载成功: vps_host={config.get('vps_host')}, vps_new_port={config.get('vps_new_port')}")
    
    try:
        import ip_provider
        
        # 初始化IPProvider
        provider = ip_provider.init_from_config(config)
        
        # 测试获取IP
        logger.info("开始获取代理IP...")
        result = provider.get_ip()
        
        logger.info(f"获取结果: {result}")
        
        if result.get("success"):
            ip_info = result.get("ip_info", {})
            logger.info(f"✓ 成功获取代理")
            logger.info(f"  - 出口IP: {ip_info.get('ip', '未知')}")
            logger.info(f"  - 代理主机: {result.get('proxy_host')}")
            logger.info(f"  - 代理端口: {result.get('proxy_port')}")
            logger.info(f"  - 国家代码: {result.get('country_code')}")
            logger.info(f"  - 国家: {ip_info.get('country', '未知')}")
            logger.info(f"  - 区域: {ip_info.get('region', '未知')}")
            logger.info(f"  - 城市: {ip_info.get('city', '未知')}")
            return True
        else:
            logger.error(f"✗ 获取代理失败: {result.get('error', '未知错误')}")
            return False
            
    except Exception as e:
        logger.error(f"✗ 测试失败: {type(e).__name__}: {e}", exc_info=True)
        return False

def test_get_proxy_from_api_url():
    """测试便捷函数 get_proxy_from_api_url"""
    logger.info("\n=== 测试便捷函数 get_proxy_from_api_url ===")
    
    config = load_config()
    
    try:
        import ip_provider
        
        # 配置ip_provider
        ip_provider.init_from_config(config)
        
        # 使用配置中的代理池第一个代理
        proxy_pool = config.get("proxy_pool", [])
        if proxy_pool:
            proxy = proxy_pool[0]
            api_url = proxy.get("proxy_api_url")
            api_user = proxy.get("proxy_user", "")
            api_pwd = proxy.get("proxy_pwd", "")
            country_code = proxy.get("country_code", "US")
            
            if api_url:
                logger.info(f"测试代理: {country_code} - {api_url[:60]}...")
                result = ip_provider.get_proxy_from_api_url(
                    api_url=api_url,
                    api_user=api_user,
                    api_pwd=api_pwd,
                    country_code=country_code
                )
                
                if result.get("success"):
                    logger.info(f"✓ 便捷函数测试成功")
                    logger.info(f"  - 出口IP: {result.get('ip_info', {}).get('ip', '未知')}")
                    logger.info(f"  - 代理: {result.get('proxy_host')}:{result.get('proxy_port')}")
                    return True
                else:
                    logger.error(f"✗ 便捷函数测试失败: {result.get('error', '未知错误')}")
                    return False
            else:
                logger.warning("⚠️ 代理API URL为空，跳过测试")
                return True
        else:
            logger.warning("⚠️ 代理池为空，跳过测试")
            return True
            
    except Exception as e:
        logger.error(f"✗ 便捷函数测试失败: {type(e).__name__}: {e}", exc_info=True)
        return False

def test_adsl_mode():
    """测试ADSL模式（仅测试配置，不实际拨号）"""
    logger.info("\n=== 测试ADSL模式配置 ===")
    
    try:
        import ip_provider
        
        provider = ip_provider.IPProvider("adsl")
        provider.configure_adsl(
            profile="pppoe",
            username="test_user",
            password="test_pass",
            interface="ppp0"
        )
        
        logger.info(f"✓ ADSL模式配置成功: provider_type={provider.provider_type}")
        logger.info(f"  - profile: {provider.adsl_profile}")
        logger.info(f"  - interface: {provider.adsl_interface}")
        
        return True
        
    except Exception as e:
        logger.error(f"✗ ADSL模式测试失败: {type(e).__name__}: {e}", exc_info=True)
        return False

def main():
    """主测试函数"""
    logger.info("=" * 60)
    logger.info("IP Provider 测试套件")
    logger.info("=" * 60)
    
    results = []
    
    # 测试1: 代理API方式
    results.append(("代理API方式", test_proxy_api()))
    
    # 测试2: 便捷函数
    results.append(("便捷函数", test_get_proxy_from_api_url()))
    
    # 测试3: ADSL模式配置
    results.append(("ADSL模式配置", test_adsl_mode()))
    
    # 输出总结
    logger.info("\n" + "=" * 60)
    logger.info("测试总结")
    logger.info("=" * 60)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        logger.info(f"{status} - {name}")
    
    logger.info(f"\n总测试数: {total}, 通过: {passed}, 失败: {total - passed}")
    
    if passed == total:
        logger.info("✓ 所有测试通过！")
        return 0
    else:
        logger.error("✗ 部分测试失败")
        return 1

if __name__ == "__main__":
    sys.exit(main())