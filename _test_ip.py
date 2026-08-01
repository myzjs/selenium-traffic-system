"""临时测试脚本 - 测试IP获取"""
import sys, json, logging
sys.path.insert(0, '.')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')

from ip_provider import get_proxy_from_api_url

with open('config.json', 'r') as f:
    cfg = json.load(f)

api_url = cfg.get('ip_proxy_api', '')
api_user = cfg.get('ip_proxy_user', '')
api_pwd = cfg.get('ip_proxy_pwd', '')

print(f'Testing IPDeep API: {api_url[:80]}...')
print(f'User: {api_user[:30]}...')

result = get_proxy_from_api_url(api_url, api_user, api_pwd, 'US')
print('RESULT:')
print(json.dumps(result, indent=2, ensure_ascii=False))