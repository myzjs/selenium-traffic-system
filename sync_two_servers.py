#!/usr/bin/env python3
"""多服务器同步+重启脚本：东京(107.148.2.75) + 美国(104.129.54.64) + 新加坡(177.5.74.5)"""
import io
import os
import tarfile
import time

import paramiko

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/root/selenium_traffic_system"

# 排除的文件/目录（缓存、日志、运行时数据、服务器自有配置）
EXCLUDE_DIRS = {"__pycache__", ".git", "qa_sessions", "report", "test_reports",
                "feedback", ".uploads", "node_modules"}
EXCLUDE_FILES = {"app.log", "config.json", ".env", "historical_tasks.json",
                 "ua_usage_history.json", "fingerprint_stats.json",
                 "sync_two_servers.py"}
EXCLUDE_EXTS = {".log", ".pyc"}


def build_tar() -> bytes:
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(LOCAL_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES:
                    continue
                if os.path.splitext(f)[1] in EXCLUDE_EXTS:
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, LOCAL_DIR)
                tar.add(full, arcname=rel)
                count += 1
    data = buf.getvalue()
    print(f"打包完成: {count} 个文件, {len(data)/1024:.1f} KB")
    return data


def run(ssh, cmd, timeout=60):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


def sync_server(host, port, password, restart_fn, name):
    print(f"\n{'='*60}\n开始同步: {name} ({host}:{port})\n{'='*60}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username="root", password=password, timeout=30)

    # 1. 上传 tar 包
    sftp = ssh.open_sftp()
    with sftp.file("/tmp/sync_pkg.tar.gz", "wb") as f:
        f.write(TAR_DATA)
    sftp.close()
    print("✅ 上传完成")

    # 2. 解压到项目目录（保留服务器自有 .env / config.json）
    out, err = run(ssh, f"mkdir -p {REMOTE_DIR} && tar -xzf /tmp/sync_pkg.tar.gz -C {REMOTE_DIR} && rm -f /tmp/sync_pkg.tar.gz && ls {REMOTE_DIR} | head -20")
    print(f"解压结果: {out}\n{err}")

    # 3. 重启服务
    restart_fn(ssh, run)
    ssh.close()


def restart_tokyo(ssh, run):
    """东京：systemctl 管理，失败则手动兜底"""
    out, err = run(ssh, "systemctl restart selenium_traffic && sleep 3 && systemctl status selenium_traffic --no-pager | head -15")
    print(f"[东京] systemctl 重启:\n{out}{err}")
    if "error" in err.lower() or "not found" in out.lower() or "could not" in err.lower():
        print("[东京] systemctl 失败，尝试手动重启...")
        run(ssh, "pkill -9 -f 'app.py' ; sleep 1 ; fuser -k 5001/tcp 2>/dev/null ; sleep 2")
        out, err = run(ssh, f"cd {REMOTE_DIR} && DISPLAY=:105 nohup /usr/bin/python3.11 app.py > /dev/null 2>&1 & sleep 1; echo started")
        print(f"[东京] 手动启动: {out}{err}")
    # 验证
    time.sleep(5)
    out, _ = run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/ ; echo")
    print(f"[东京] 端口5001 HTTP状态: {out.strip()}")


def restart_us(ssh, run):
    """美国：app.py 以 RUN_PORT=8888 运行；pkill 用 [a] 技巧避免误杀 SSH 会话自身"""
    out, _ = run(ssh, "ps aux | grep -E '[a]pp\\.py|[p]roxy_server' || echo '无运行进程'")
    print(f"[美国] 当前运行进程:\n{out}")
    run(ssh, "pkill -9 -f '[a]pp\\.py' ; pkill -9 -f '[p]roxy_server_new' ; sleep 2 ; fuser -k 8888/tcp 2>/dev/null; echo cleaned")
    # 按原方式启动：RUN_PORT=8888，setsid 脱离 SSH 会话，命令中避免 app.py 明文
    out, err = run(ssh, "cd /root/selenium_traffic_system && A=app; setsid env RUN_PORT=8888 python3 ${A}.py >> app_8888.log 2>&1 < /dev/null & sleep 2; echo started")
    print(f"[美国] 启动 app.py(8888): {out}{err}")
    # 验证
    time.sleep(8)
    out, _ = run(ssh, "ps aux | grep '[a]pp\\.py' || echo '无进程'")
    print(f"[美国] 重启后进程:\n{out}")
    out, _ = run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/ ; echo")
    print(f"[美国] 端口8888 HTTP状态: {out.strip()}")


def restart_sg(ssh, run):
    """新加坡：systemctl 管理（首次自动创建 service 文件）"""
    out, _ = run(ssh, "test -f /etc/systemd/system/selenium_traffic.service && echo EXISTS || echo MISSING")
    if "MISSING" in out:
        unit = """[Unit]
Description=Selenium Traffic System
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/selenium_traffic_system
Environment=RUN_HOST=0.0.0.0
ExecStart=/usr/bin/python3 app.py 5001
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
MemoryMax=2G

[Install]
WantedBy=multi-user.target
"""
        sftp = ssh.open_sftp()
        with sftp.file("/etc/systemd/system/selenium_traffic.service", "w") as f:
            f.write(unit)
        sftp.close()
        run(ssh, "systemctl daemon-reload && systemctl enable selenium_traffic")
        print("[新加坡] systemd 服务已创建并启用")
    out, err = run(ssh, "systemctl restart selenium_traffic && sleep 3 && systemctl status selenium_traffic --no-pager | head -12")
    print(f"[新加坡] systemctl 重启:\n{out}{err}")
    time.sleep(5)
    out, _ = run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/ ; echo")
    print(f"[新加坡] 端口5001 HTTP状态: {out.strip()}")


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    TAR_DATA = build_tar()

    # 东京服务器
    if target in ("all", "tokyo"):
        try:
            sync_server("107.148.2.75", 31141, "Zhanjisheng@@7263", restart_tokyo, "东京服务器")
        except Exception as e:
            print(f"❌ 东京服务器同步失败: {e}")

    # 美国服务器
    if target in ("all", "us"):
        try:
            sync_server("104.129.54.64", 22, "B4gKZcv15CwlL51Rd8", restart_us, "美国服务器")
        except Exception as e:
            print(f"❌ 美国服务器同步失败: {e}")

    # 新加坡服务器
    if target in ("all", "sg"):
        try:
            sync_server("177.5.74.5", 22, "Zhanjisheng@@7263", restart_sg, "新加坡服务器")
        except Exception as e:
            print(f"❌ 新加坡服务器同步失败: {e}")

    print("\n全部完成")
