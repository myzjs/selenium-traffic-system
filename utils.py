#!/usr/bin/env python3

import os

def clean_logs():
    """清除以往的日志文件"""
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    if os.path.exists(logs_dir):
        for filename in os.listdir(logs_dir):
            if filename.endswith('.log'):
                try:
                    with open(os.path.join(logs_dir, filename), 'w') as f:
                        f.write('')
                except OSError:
                    pass
