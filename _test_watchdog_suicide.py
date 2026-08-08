"""
本地单元测试脚本 - 验证 P2-5：单任务 watchdog suicide Timer（30 分钟硬上限，杜绝卡死）
运行方式（本机 MacOS/Linux，无需 Playwright/任何外网依赖）：
    cd /Users/mac/Documents/www-jb/626/selenium_traffic_system
    python3 _test_watchdog_suicide.py

测试 4 个场景：
  Case 1：3s 的 watchdog + 正常任务 1.2s 执行完 -> 任务结束后 cancel，进程 NOT suicide -> 判定 PASS
  Case 2：2s 的 watchdog + 任务卡死 time.sleep(15s) -> 到 2s 触发 os._exit(24)，父进程收到 returncode=24 -> 判定 PASS
  Case 3：5s 的 watchdog + 任务发生异常 -> finally 里 cancel，进程 NOT suicide -> 判定 PASS
  Case 4：嵌套异常（外层 catch 也必须 cancel）-> 验证 cancel 不会被跳过 -> 判定 PASS

注意：测试 Case2 会 spawn 子进程触发真正的 os._exit(24)，不会 kill 本脚本进程。
"""

from __future__ import annotations

import os
import sys
import time
import threading
import subprocess
import tempfile
import textwrap
import json
import traceback
from typing import Callable

# ---------------------------------------------------------------------------
# 先把 worker_task 里真正在用的 watchdog 实现 "镜像" 一份到这里，保持与 app.py 一致
# ---------------------------------------------------------------------------
def _build_watchdog_factory(exit_code: int = 24, log: Callable[[str], None] = print):
    """返回 (_start, _cancel) 闭包，行为与 app.py 里实现严格一致。"""
    _task_global_watchdog: list = [None]  # list 方便闭包修改，和 app.py 相同模式

    def _start_task_global_watchdog(task_label: str, seconds: int = 1800):
        try:
            _tid = [None]

            def _suicide_fn():
                try:
                    log(
                        f"💀 P2-5 watchdog: 单任务[{task_label}]执行超过 {seconds}s，"
                        f"认定为死锁/卡死，立即 os._exit({exit_code})"
                    )
                except Exception:
                    pass
                os._exit(exit_code)

            _t = threading.Timer(interval=seconds, function=_suicide_fn)
            _t.daemon = True
            _tid[0] = _t
            _t.start()
            _task_global_watchdog[0] = _tid
            log(f"[watchdog.start] 已为 {task_label} 启动 suicide Timer，阈值={seconds}s")
        except Exception as _e:
            log(f"watchdog 启动失败（不影响任务）: {type(_e).__name__}")

    def _cancel_task_global_watchdog():
        try:
            if _task_global_watchdog and _task_global_watchdog[0]:
                _t = _task_global_watchdog[0][0]
                if _t and _t.is_alive():
                    _t.cancel()
                    _task_global_watchdog[0] = None
                    log(f"[watchdog.cancel] 已取消 suicide Timer")
        except Exception:
            _task_global_watchdog[0] = None

    return _start_task_global_watchdog, _cancel_task_global_watchdog


# ---------------------------------------------------------------------------
# Case 1/3/4（在本进程直接跑，不会触发 _exit）
# ---------------------------------------------------------------------------
def run_case_1_normal_complete() -> bool:
    """Case 1：任务在 watchdog 阈值内正常完成 -> Timer 被 cancel。"""
    print("\n========== Case 1：任务正常完成（1.2s < 阈值 3s） ==========")
    start, cancel = _build_watchdog_factory(exit_code=24)
    pid_before = os.getpid()
    try:
        start("case1-task#1/3@US", seconds=3)
        # 模拟任务业务耗时 1.2s
        time.sleep(1.2)
        print("   [业务] 任务执行完成，1.2s < 3s，进入 finally cancel")
        cancel()
        # 再多等 2.5s（超过阈值）确认 watchdog 已 cancel 没触发
        time.sleep(2.5)
        assert os.getpid() == pid_before, "BUG: Case1 进程居然被 suicide 了"
        print("✅ Case 1 PASS：正常完成 + 成功 cancel，wait>阈值后仍存活")
        return True
    except Exception as e:
        traceback.print_exc()
        print(f"❌ Case 1 FAIL：{e}")
        return False
    finally:
        cancel()  # 保险


def run_case_3_exception_path() -> bool:
    """Case 3：任务中途抛异常 -> except/finally 仍然能 cancel watchdog。"""
    print("\n========== Case 3：任务中途异常（必须在 finally 中 cancel） ==========")
    start, cancel = _build_watchdog_factory(exit_code=24)
    alive_flag = {"ok": True}
    try:
        start("case3-task#3/3@JP", seconds=4)
        # 模拟抛异常
        raise RuntimeError("模拟业务异常：page.goto 代理超时")
    except RuntimeError as e:
        print(f"   [业务] 捕获到 {type(e).__name__}: {e}，进入外层异常 cancel 流程")
        cancel()
    finally:
        cancel()
    # 再等 3.5s（阈值内都 cancel 完了，所以不会触发）
    time.sleep(3.5)
    if alive_flag["ok"]:
        print("✅ Case 3 PASS：异常分支 cancel 成功，进程存活")
        return True
    print("❌ Case 3 FAIL")
    return False


def run_case_4_outer_exception() -> bool:
    """Case 4：双层 try/except 结构，验证外层异常 cancel 也能走到。"""
    print("\n========== Case 4：双层 try/except 嵌套异常路径 cancel ==========")
    start, cancel = _build_watchdog_factory(exit_code=24)
    try:
        start("case4-task@GB", seconds=4)
        try:
            # 业务里模拟内层直接 return/break 后，外层也能 cancel
            for _ in range(3):
                time.sleep(0.1)
            raise ValueError("模拟 page.evaluate 返回 TypeError")
        except ValueError as _ve:
            print(f"   [业务] 内层捕获 {_ve}，继续向上冒泡")
            raise
    except Exception as e:
        print(f"   [业务] 外层捕获 {type(e).__name__}，cancel watchdog")
        cancel()
    finally:
        cancel()
    time.sleep(3.5)
    print("✅ Case 4 PASS：双层异常路径下 watchdog 正常 cancel")
    return True


# ---------------------------------------------------------------------------
# Case 2：必须用子进程跑，因为真的会 os._exit(24)
# ---------------------------------------------------------------------------
_CHILD_CODE_TMPL = textwrap.dedent("""
import os, threading, time

def build():
    _task_global_watchdog = [None]
    def start(task_label, seconds=1800):
        _tid = [None]
        def _suicide_fn():
            print(f"💀 P2-5 watchdog: 单任务[{{task_label}}]执行超过 {{seconds}}s，死锁，立即 os._exit(24)", flush=True)
            os._exit(24)
        _t = threading.Timer(interval=seconds, function=_suicide_fn)
        _t.daemon = True
        _tid[0] = _t
        _t.start()
        _task_global_watchdog[0] = _tid
        print(f"[watchdog.start] 为 {{task_label}} 启动，阈值={{seconds}}s", flush=True)
    def cancel():
        try:
            if _task_global_watchdog and _task_global_watchdog[0]:
                _t = _task_global_watchdog[0][0]
                if _t and _t.is_alive():
                    _t.cancel()
                    _task_global_watchdog[0] = None
        except Exception:
            _task_global_watchdog[0] = None
    return start, cancel

start, cancel = build()
# 故意卡死 15 秒，阈值 2s -> 到 2s 必 suicide
start("CASE2-SUBPROCESS-killme", seconds=2)
try:
    for i in range(15):
        time.sleep(1)
        print(f"  [卡住] 第 {{i+1}}s，仍然还活着...", flush=True)
finally:
    cancel()
""")


def run_case_2_stuck_task_suicide() -> bool:
    """Case 2：2s watchdog + 15s 卡死 -> 必须 returncode=24（由子进程触发）。"""
    print("\n========== Case 2：任务卡死（sleep 15s > 阈值 2s） -> suicide ==========")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as _f:
        _f.write(_CHILD_CODE_TMPL)
        _path = _f.name
    try:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, _path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        dt = time.time() - t0
        print("---- 子进程 stdout ----")
        print(proc.stdout.decode("utf-8", errors="replace"))
        print("----------------------")
        print(f"   子进程实际耗时：{dt:.2f}s，exitcode={proc.returncode}")
        # 真正有效：returncode == 24（os._exit(24)），且实际退出发生在 2.0~3.5s 内（Timer 有最小调度误差）
        if proc.returncode == 24 and 1.9 < dt < 4.0:
            print(f"✅ Case 2 PASS：子进程 {dt:.2f}s 时 os._exit(24)，与阈值 2s 吻合（定时器调度允许±0.5s）")
            return True
        print(
            f"❌ Case 2 FAIL：期望 returncode=24 & 耗时≈2s；实际 returncode={proc.returncode}，"
            f"耗时={dt:.2f}s（如果 returncode=0，说明 suicide 没触发）"
        )
        return False
    finally:
        try:
            os.unlink(_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 汇总 + 结果 JSON
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("📋 P2-5 Watchdog Suicide Timer 本地单元测试（无外网依赖，全离线）")
    print("=" * 72)
    cases = [
        ("Case1: 任务正常完成后 cancel", run_case_1_normal_complete),
        ("Case2: 卡死 -> os._exit(24)（子进程验证）", run_case_2_stuck_task_suicide),
        ("Case3: 异常分支 -> finally cancel", run_case_3_exception_path),
        ("Case4: 双层异常嵌套 -> outer cancel", run_case_4_outer_exception),
    ]
    results = []
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as _e:
            traceback.print_exc()
            ok = False
        results.append({"case": name, "passed": ok})
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print("\n" + "=" * 72)
    print(f"📊 测试结果：{passed}/{total} 通过")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
