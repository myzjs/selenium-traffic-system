#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_auto_commit.py (26.8.11.4 全自动测试迭代系统 · 模块②)
=====================================================================
角色：规则三 版本号自动自增 + Gitee 提交 + US 服务器部署 一条龙
职责：每轮 0 错误自动执行，版本号 YY.M.D.N，当日序号+1（跨日重置）
用法：python3 scripts/pipeline_auto_commit.py "commit message prefix" [--push-and-deploy]
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from typing import Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(PROJECT_ROOT, "app.py")
CONFIG_JSON = os.path.join(PROJECT_ROOT, "config.json")
REMOTE_US = "root@104.129.54.64"
REMOTE_PASS = "B4gKZcv15CwlL51Rd8"  # 与部署脚本保持一致
REMOTE_DIR = "/root/selenium_traffic_system"
DEPLOY_FILES = [
    "app.py", "selenium_bridge.py", "ip_provider.py", "ip_info_resolver.py",
    "ip_region_module.py", "popunder_trigger.py", "risk_check.py",
    "seo_query_module.py", "local_proxy_relay.py", "utils.py",
    "scripts/contract_test_pipeline.py", "scripts/pipeline_auto_commit.py",
]


def _today_tuple() -> Tuple[int, int, int]:
    today = _dt.date.today()
    # YY（后两位）· M · D（不补 0），规则三
    return (today.year % 100, today.month, today.day)


def _parse_version(v: str) -> Tuple[int, int, int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$", v.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def _read_current() -> str:
    with open(APP_PY, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'APP_VERSION\s*=\s*"(26\.\d+\.\d+\.\d+)"', src)
    if not m:
        raise RuntimeError("app.py 中找不到 APP_VERSION 行，无法自增版本号")
    return m.group(1)


def _write_new(new_v: str) -> None:
    with open(APP_PY, "r", encoding="utf-8") as f:
        src = f.read()
    new_src = re.sub(
        r'APP_VERSION\s*=\s*"(26\.\d+\.\d+\.\d+)"',
        f'APP_VERSION = "{new_v}"',
        src, count=1,
    )
    with open(APP_PY, "w", encoding="utf-8") as f:
        f.write(new_src)


def next_version(current: str) -> str:
    """规则三：当天前缀不变，序号 N + 1；跨天前缀变新日期，序号从 1。"""
    parts = _parse_version(current)
    if not parts:
        raise ValueError(f"版本号格式非法：{current}")
    yy, mo, dd, n = parts
    ty, tm, td = _today_tuple()
    if (yy, mo, dd) == (ty, tm, td):
        return f"{yy}.{mo}.{dd}.{n + 1}"
    return f"{ty}.{tm}.{td}.1"


def _sh(cmd: str, *, cwd: str | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, shell=True, cwd=cwd or PROJECT_ROOT,
        text=True, capture_output=capture, check=check,
    )


def git_commit_push(new_v: str, msg_prefix: str, *, push: bool = True) -> str:
    msg = f"{new_v}-{msg_prefix}"
    _sh("git add -A")
    # commit 允许失败（无改动时），所以 check=False
    res = _sh(f"git commit -m {json.dumps(msg, ensure_ascii=False)}", check=False)
    if res.returncode != 0 and "nothing to commit" in (res.stdout or "") + (res.stderr or ""):
        return "SKIP_NO_CHANGES"
    if res.returncode != 0:
        raise RuntimeError(f"git commit 失败：{res.stderr}\n{res.stdout}")
    if push:
        pres = _sh("git push gitee main", check=False)
        if pres.returncode != 0:
            raise RuntimeError(f"git push gitee 失败：{pres.stderr}\n{pres.stdout}")
    return "OK"


def scp_deploy_us() -> bool:
    """把 DEPLOY_FILES SCP 到 US 服务器，然后 systemctl restart，30 秒验证 running=true。"""
    try:
        import shutil
        sshpass = shutil.which("sshpass") or ""
        base = f"{sshpass} -p {REMOTE_PASS} " if sshpass else ""
        files_str = " ".join(f for f in DEPLOY_FILES if os.path.exists(os.path.join(PROJECT_ROOT, f)))
        scp_cmd = (
            f"{base}scp -o StrictHostKeyChecking=no -P 22 {files_str} "
            f"{REMOTE_US}:{REMOTE_DIR}/ 2>&1"
        )
        r1 = _sh(scp_cmd, check=False)
        if r1.returncode != 0:
            print(f"  [deploy] SCP 失败：{r1.stderr}\n{r1.stdout}", file=sys.stderr)
            return False
        # 语法检查 + 重启
        restart_cmd = (
            f"{base}ssh -o StrictHostKeyChecking=no {REMOTE_US} "
            f"'python3 -m py_compile {REMOTE_DIR}/app.py {REMOTE_DIR}/selenium_bridge.py "
            f"  && echo PY_COMPILE_OK "
            f"  && systemctl restart selenium_traffic.service "
            f"  && sleep 10 "
            f"  && systemctl is-active selenium_traffic.service "
            f"  && curl -s --max-time 5 http://127.0.0.1:8888/api/status "
            f"  && echo \"\" "
            f"  && curl -s --max-time 5 http://127.0.0.1:8888/ | grep -oE \"v{_read_current()}\" 2>/dev/null | head -1'"
        )
        r2 = _sh(restart_cmd, check=False)
        out = (r2.stdout or "") + "\n" + (r2.stderr or "")
        print(f"  [deploy] 服务器回显（节选 200 字）：\n  {out[-200:]}")
        ok_deploy = (
            "PY_COMPILE_OK" in out and "active" in out
            and '"running":true' in out  # 自恢复生效，restart 后 running=true
        )
        return bool(ok_deploy)
    except Exception as e:  # noqa: BLE001
        print(f"  [deploy] 异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def main() -> int:
    argv = sys.argv[1:]
    msg_prefix = argv[0] if argv else "auto-test-iter-zero-error"
    push_deploy = "--push-and-deploy" in argv
    current = _read_current()
    new_v = next_version(current)
    print(f"[auto-commit] 当前版本号：{current}  →  下一轮：{new_v}")
    _write_new(new_v)
    status = git_commit_push(new_v, msg_prefix, push=push_deploy)
    if status == "SKIP_NO_CHANGES":
        print("[auto-commit] ⚠️ 无代码变更，跳过 commit（版本号已写入 app.py，需要手动提交）")
    else:
        print(f"[auto-commit] ✅ git commit + (push={push_deploy}) 成功：{new_v}")
    if push_deploy:
        ok = scp_deploy_us()
        print(f"[auto-commit] 部署 US 服务器：{'✅ 成功' if ok else '❌ 失败（详见上方）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
