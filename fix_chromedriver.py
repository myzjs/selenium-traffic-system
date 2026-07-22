#!/usr/bin/env python3
"""修复 ChromeDriver 版本不兼容问题"""

with open('selenium_bridge.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换浏览器启动代码，强制使用 Selenium Manager
old_section = '''        # 查找ChromeDriver
        chromedriver_path = _find_chromedriver()

        try:
            if chromedriver_path:
                service = Service(executable_path=chromedriver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
            else:
                # 让Selenium Manager自动管理
                driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            # 重试：不指定binary_location
            if chrome_binary:
                chrome_options.binary_location = None
                try:
                    if chromedriver_path:
                        service = Service(executable_path=chromedriver_path)
                        driver = webdriver.Chrome(service=service, options=chrome_options)
                    else:
                        driver = webdriver.Chrome(options=chrome_options)
                except Exception as e2:
                    raise RuntimeError(f"Chrome启动失败: {e}; 无binary重试失败: {e2}")
            else:
                raise'''

new_section = '''        # 查找ChromeDriver（强制使用Selenium Manager，避免版本不兼容）
        chromedriver_path = _find_chromedriver()
        
        # 强制使用 Selenium Manager 自动下载匹配版本的 chromedriver
        # 系统中的 chromedriver (v108) 与 Chrome (v149) 版本不兼容
        try:
            # 直接创建 driver，让 Selenium Manager 处理 chromedriver
            driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            # 重试：不指定binary_location
            if chrome_binary:
                chrome_options.binary_location = None
                try:
                    driver = webdriver.Chrome(options=chrome_options)
                except Exception as e2:
                    raise RuntimeError(f"Chrome启动失败: {e}; 无binary重试失败: {e2}")
            else:
                raise'''

if old_section in content:
    content = content.replace(old_section, new_section)
    with open('selenium_bridge.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('✅ 修复成功')
else:
    print('❌ 未找到匹配的代码段')
