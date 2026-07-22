import requests
import json

# 测试获取任务计划
print("测试获取任务计划...")
try:
    response = requests.post('http://127.0.0.1:5001/generate_plan')
    print(f"状态码: {response.status_code}")
    print(f"响应内容: {response.text}")
    
    if response.status_code == 200:
        data = response.json()
        print("\n解析后的数据:")
        print(f"状态: {data.get('status')}")
        if data.get('status') == 'ok':
            plan = data.get('plan')
            print(f"计划天数: {plan.get('plan_days')}")
            print(f"总任务数: {plan.get('total_tasks')}")
            print(f"使用模型: {plan.get('model_used')}")
            print(f"任务列表长度: {len(plan.get('tasks', []))}")
            
            if plan.get('tasks'):
                print("\n第一个任务信息:")
                first_task = plan['tasks'][0]
                print(f"日期: {first_task.get('date')}")
                print(f"计划时间: {first_task.get('plan_time')}")
                print(f"开始时间(秒): {first_task.get('actual_start')}")
                print(f"结束时间(秒): {first_task.get('actual_end')}")
                print(f"国家: {first_task.get('proxy_country')}")
                print(f"API地址: {first_task.get('proxy_api_url')}")
                
except Exception as e:
    print(f"请求失败: {e}")
