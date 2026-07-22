#!/usr/bin/env python3

with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 找到 start_task 函数的位置
start_line = None
for i, line in enumerate(lines):
    if '@app.route(\'/start_task\', methods=[\'POST\'])' in line:
        start_line = i
        break

if start_line is not None:
    # 在 start_task 函数前插入 clean_logs 函数
    clean_logs_lines = [
        '\n',
        'def clean_logs():\n',
        '    """清除以往的日志文件"""\n',
        '    import os\n',
        '\n',
        '    # 清除应用日志目录\n',
        '    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), \'logs\')\n',
        '    if os.path.exists(logs_dir):\n',
        '        for filename in os.listdir(logs_dir):\n',
        '            if filename.endswith(\'.log\'):\n',
        '                log_file = os.path.join(logs_dir, filename)\n',
        '                try:\n',
        '                    with open(log_file, \'w\') as f:\n',
        '                        f.write(\'\')\n',
        '                    log.info(f"✅ 已清除日志文件: {filename}")\n',
        '                except Exception as e:\n',
        '                    log.warning(f"⚠️ 清除日志文件 {filename} 失败: {e}")\n',
        '\n',
        '    # 清除临时日志文件\n',
        '    temp_logs = [\'/tmp/flask.log\', \'/tmp/app.log\']\n',
        '    for log_path in temp_logs:\n',
        '        if os.path.exists(log_path):\n',
        '            try:\n',
        '                with open(log_path, \'w\') as f:\n',
        '                    f.write(\'\')\n',
        '                log.info(f"✅ 已清除日志文件: {log_path}")\n',
        '            except Exception as e:\n',
        '                log.warning(f"⚠️ 清除日志文件 {log_path} 失败: {e}")\n',
        '\n',
    ]
    
    new_lines = lines[:start_line] + clean_logs_lines + lines[start_line:]
    
    with open('app.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print('✅ clean_logs 函数已添加')
else:
    print('❌ 未找到 start_task 函数')
