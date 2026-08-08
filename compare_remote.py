#!/usr/bin/env python3
import paramiko
import os
import hashlib
import sys
import datetime

HOST = '107.148.2.75'
PORT = 31141
USER = 'root'
PASS = 'Zhanjisheng@@7263'
REMOTE_DIR = '/root/selenium_traffic_system'
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(LOCAL_DIR, 'compare_result.txt')

EXCLUDE_DIRS = {'__pycache__', '.git', 'qa_sessions', 'report', 'test_reports',
                'feedback', '.uploads', 'node_modules', 'trae_feedback'}
EXCLUDE_FILES = {'app.log', 'config.json', '.env', 'historical_tasks.json',
                 'ua_usage_history.json', 'fingerprint_stats.json',
                 'sync_two_servers.py', '.DS_Store', 'compare_remote.py',
                 'compare_result.txt', '_step1_remote.sh', '_step2_local.sh', '_step3_compare.sh'}
EXCLUDE_EXTS = {'.log', '.pyc'}

def log(msg):
    print(msg, flush=True)

def should_exclude(rel_path, filename):
    parts = rel_path.replace('\\', '/').split('/')
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if filename in EXCLUDE_FILES:
        return True
    ext = os.path.splitext(filename)[1]
    if ext in EXCLUDE_EXTS:
        return True
    return False

def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def main():
    log('开始执行对比...')
    report_lines = []
    report_lines.append('=' * 60)
    report_lines.append('  本地代码 与 东京VPS(107.148.2.75) 代码一致性对比报告')
    report_lines.append('  生成时间: ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    report_lines.append('=' * 60)
    report_lines.append('')

    # 1. 本地 md5
    log('[1/3] 计算本地文件 MD5...')
    local_md5 = {}
    for root, dirs, files in os.walk(LOCAL_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, LOCAL_DIR).replace('\\', '/')
            if should_exclude(rel, f):
                continue
            try:
                local_md5[rel] = md5_file(full)
            except Exception as e:
                local_md5[rel] = 'ERROR'
    log(f'  本地文件数: {len(local_md5)}')

    # 2. 远程 md5
    log('[2/3] 连接东京VPS获取远程 MD5...')
    remote_md5 = {}
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30,
                    banner_timeout=30, auth_timeout=30)
        log('  SSH 连接成功')

        find_filter = '-type f'
        for d in EXCLUDE_DIRS:
            find_filter += f' -not -path "*/{d}/*"'
        for f in EXCLUDE_FILES:
            find_filter += f' -not -name "{f}"'
        for e in EXCLUDE_EXTS:
            find_filter += f' -not -name "*{e}"'

        cmd = f'cd {REMOTE_DIR} && find . {find_filter} -exec md5sum {{}} \\; 2>/dev/null'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=180)
        out = stdout.read().decode(errors='replace')
        err = stderr.read().decode(errors='replace')
        for line in out.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                md5_val = parts[0]
                rel_path = parts[1].lstrip('./').lstrip('/').replace('\\', '/')
                remote_md5[rel_path] = md5_val
        log(f'  远程文件数: {len(remote_md5)}')
        if err.strip():
            log(f'  远程stderr片段: {err[:300]}')
    except Exception as e:
        log(f'  SSH失败: {type(e).__name__}: {e}')
        report_lines.append(f'SSH连接失败: {type(e).__name__}: {e}')
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        log(f'报告已保存: {OUTPUT_FILE}')
        return
    finally:
        if ssh:
            try:
                ssh.close()
            except:
                pass

    # 3. 对比
    log('[3/3] 执行对比...')
    only_local = sorted(set(local_md5.keys()) - set(remote_md5.keys()))
    only_remote = sorted(set(remote_md5.keys()) - set(local_md5.keys()))
    diff_files = []
    common = sorted(set(local_md5.keys()) & set(remote_md5.keys()))
    for k in common:
        if local_md5[k] != remote_md5[k]:
            diff_files.append((k, local_md5[k], remote_md5[k]))

    report_lines.append('【统计】')
    report_lines.append(f'  本地文件数: {len(local_md5)}')
    report_lines.append(f'  远程文件数: {len(remote_md5)}')
    report_lines.append(f'  共同文件数: {len(common)}')
    report_lines.append('')

    report_lines.append(f'【仅本地存在】 {len(only_local)} 个文件:')
    if only_local:
        for f in only_local:
            report_lines.append(f'  + {f}')
    else:
        report_lines.append('  (无)')
    report_lines.append('')

    report_lines.append(f'【仅远程存在】 {len(only_remote)} 个文件:')
    if only_remote:
        for f in only_remote:
            report_lines.append(f'  - {f}')
    else:
        report_lines.append('  (无)')
    report_lines.append('')

    report_lines.append(f'【MD5内容不一致】 {len(diff_files)} 个文件:')
    if diff_files:
        for f, lm, rm in diff_files:
            report_lines.append(f'  ! {f}')
            report_lines.append(f'      本地: {lm}')
            report_lines.append(f'      远程: {rm}')
    else:
        report_lines.append('  (无)')
    report_lines.append('')

    report_lines.append('=' * 60)
    total = len(only_local) + len(only_remote) + len(diff_files)
    if total == 0:
        report_lines.append('✅ 结论: 本地代码 与 东京VPS 完全一致！')
    else:
        report_lines.append('❌ 结论: 本地代码 与 东京VPS 存在差异')
        report_lines.append(f'   本地独有: {len(only_local)}')
        report_lines.append(f'   远程独有: {len(only_remote)}')
        report_lines.append(f'   内容不同: {len(diff_files)}')
        report_lines.append(f'   差异总计: {total}')
    report_lines.append('=' * 60)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines) + '\n')
    log(f'完成！报告已保存: {OUTPUT_FILE}')
    log('')
    for line in report_lines:
        log(line)

if __name__ == '__main__':
    main()
