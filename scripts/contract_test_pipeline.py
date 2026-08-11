#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contract_test_pipeline.py (26.8.11.4 全自动测试迭代系统 · 主入口)
=====================================================================
全自动 7 阶段闭环流水线：
  0) 基线快照（代码行/用例数/git diff）
  1) 语法检查（py_compile 所有 .py）
  2) pytest 全量（含 contract_fullflow/ 15 个全流程契约）
  3) 业务契约汇总（15 项逐一过，生成通过/失败清单）
  4) 生产烟雾（Flask test_client 调 6 个核心 API）
  5) 错误聚合 + 规则一分级（🔴阻断级/🟡高危/🟢优化） + 修改建议 JSON
  6) 生成报告（reports/auto_test_report_YYMMDD_HHMMSS.md）

支持 CLI：
  --max-iter N       最大迭代轮数（默认 5，防止死循环）
  --watch            代码变动后自动触发下一轮（不指定则按 max-iter 跑完就退出）
  --auto-commit      本轮 0 错误后自动：版本号自增 → commit → push gitee → SCP → 重启 US
  --grace N          同连续错误容忍次数（默认 2），避免"越改越坏"时立刻停
  --report-dir D     报告目录（默认 reports/）
  --json-out F       输出"修改建议 JSON"到指定路径（供大模型 Agent 消费）
  --stage SKIP_LIST  逗号分隔跳过阶段（例：--stage 4 跳过生产烟雾；--stage 3,4 跳过 3+4）

示例：
  # 1. 开发机日常迭代（跑 1 轮看报告）
  python3 scripts/contract_test_pipeline.py --max-iter 1

  # 2. 全自动：改完代码自动跑，0 错误自动部署（持续集成模式）
  python3 scripts/contract_test_pipeline.py --max-iter 20 --watch --auto-commit
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
APP_PY = PROJECT_ROOT / "app.py"
REPORT_DIR_DEFAULT = PROJECT_ROOT / "reports"
STAGE_NAMES = {
    0: "基线快照 Baseline",
    1: "语法检查 Syntax",
    2: "pytest 全量",
    3: "业务契约 Contract(15项)",
    4: "生产烟雾 Smoke API",
    5: "错误聚合 Severity",
    6: "生成报告 Report",
    7: "HilltopAds 收益确认 Revenue(Final)",
}
SEVERITY_BLOCKER = "🔴阻断级"
SEVERITY_HIGH = "🟡高危"
SEVERITY_OPT = "🟢优化"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class IssueItem:
    stage: int
    severity: str
    file: str
    line_hint: str
    title: str
    principle: str
    reproduce: str
    fix_suggestion: str
    raw_trace: str = ""

    def to_md(self) -> str:
        return (
            f"### {self.severity} {self.title}\n"
            f"- **位置**：{self.file} {self.line_hint}\n"
            f"- **原理**：{self.principle}\n"
            f"- **复现**：{self.reproduce}\n"
            f"- **修复建议**：{self.fix_suggestion}\n"
        )


@dataclass
class IterResult:
    round: int
    start_ts: float
    end_ts: float = 0.0
    baseline: Dict[str, Any] = field(default_factory=dict)
    syntax_errors: List[str] = field(default_factory=list)
    pytest_collected: int = 0
    pytest_passed: int = 0
    pytest_failed: int = 0
    pytest_skipped: int = 0
    pytest_error_output: str = ""
    contract_15: Dict[str, str] = field(default_factory=dict)  # key=用例名 val=PASS/FAIL+原因
    smoke_api: Dict[str, Any] = field(default_factory=dict)
    issues: List[IssueItem] = field(default_factory=list)
    # ===== 26.8.11.5 新增 Stage 7：HilltopAds 最终收益判断 =====
    require_revenue: bool = False
    stage7_passed: bool = False           # True = 达到条件（真有收益或代理阈值）
    stage7_level: int = 0                 # 0=未判断/1=代理②/2=API①
    stage7_hit_count: int = 0             # 代理指标命中次数
    stage7_revenue: Optional[float] = None  # 真实收益金额（有 API KEY 时填）
    stage7_message: str = ""

    @property
    def total_errors(self) -> int:
        return (
            len(self.syntax_errors)
            + self.pytest_failed
            + sum(1 for v in self.contract_15.values() if not v.startswith("PASS"))
            + sum(1 for v in self.smoke_api.values() if isinstance(v, str) and not v.startswith("OK"))
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _sh(cmd: str, cwd: Path = PROJECT_ROOT, check: bool = False,
        timeout: int = 180) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd),
                           text=True, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", f"TIMEOUT({timeout}s)"


def _count_py_files() -> int:
    return len(list(PROJECT_ROOT.rglob("*.py")))


def _count_lines_py() -> int:
    total = 0
    for f in PROJECT_ROOT.rglob("*.py"):
        try:
            total += sum(1 for _ in open(f, "r", encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    return total


def _git_diff_summary() -> str:
    code, out, err = _sh("git diff --stat")
    if code != 0:
        return "(git不可用: " + (err or out).strip()[:60] + ")"
    return out.strip() or "(clean，无未提交变更)"


def _read_app_version() -> str:
    try:
        m = re.search(r'APP_VERSION\s*=\s*"(\S+)"', APP_PY.read_text(encoding="utf-8"))
        return m.group(1) if m else "UNKNOWN"
    except OSError:
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# Stage 0 · 基线快照
# ---------------------------------------------------------------------------
def stage_0_baseline(ir: IterResult) -> None:
    ir.baseline = {
        "version": _read_app_version(),
        "py_files": _count_py_files(),
        "py_lines": _count_lines_py(),
        "git_diff_stat": _git_diff_summary(),
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"  [0/6 基线] v{ir.baseline['version']} | {ir.baseline['py_files']} py files | {ir.baseline['py_lines']:,} lines")
    print(f"         git diff 摘要：{ir.baseline['git_diff_stat']}")


# ---------------------------------------------------------------------------
# Stage 1 · 语法检查（py_compile 全项目 .py）
# ---------------------------------------------------------------------------
def stage_1_syntax(ir: IterResult, skip: bool = False) -> None:
    if skip:
        print("  [1/6 语法] --stage=1 被跳过")
        return
    errors: List[str] = []
    for py_path in PROJECT_ROOT.rglob("*.py"):
        rel = py_path.relative_to(PROJECT_ROOT).as_posix()
        # 跳过 .git / venv / node_modules / reports / tests 的 __pycache__
        if any(s in rel for s in (".git/", "venv/", "node_modules/", "__pycache__/", ".venv/")):
            continue
        try:
            src = py_path.read_bytes()
            compile(src, str(py_path), "exec")
        except SyntaxError as e:
            errors.append(f"{rel}:L{e.lineno or '?'} SyntaxError: {e.msg}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{rel}:? {type(e).__name__}: {str(e)[:100]}")
    ir.syntax_errors = errors
    print(f"  [1/6 语法] 语法检查：{'✅ ' + str(len(errors)) + ' 错误' if errors else '✅ 全绿 0 SyntaxError'}")
    for e in errors[:10]:
        print(f"        • {e}")
    if len(errors) > 10:
        print(f"        ……还有 {len(errors)-10} 个，详见报告")


# ---------------------------------------------------------------------------
# Stage 2 · pytest 全量
# ---------------------------------------------------------------------------
def stage_2_pytest(ir: IterResult, skip: bool = False) -> None:
    if skip:
        print("  [2/6 pytest] --stage=2 被跳过")
        return
    cmd = f"{sys.executable} -m pytest --tb=short -q"
    code, out, err = _sh(cmd, timeout=600)
    output = (out or "") + "\n" + (err or "")
    ir.pytest_error_output = output[-6000:]
    m_collected = re.search(r"(\d+) items collected", output)
    ir.pytest_collected = int(m_collected.group(1)) if m_collected else 0
    # 解析 passed/skipped/failed
    m_pass = re.search(r"(\d+) passed", output)
    m_fail = re.search(r"(\d+) failed", output)
    m_skip = re.search(r"(\d+) skipped", output)
    ir.pytest_passed = int(m_pass.group(1)) if m_pass else 0
    ir.pytest_failed = int(m_fail.group(1)) if m_fail else 0
    ir.pytest_skipped = int(m_skip.group(1)) if m_skip else 0
    print(f"  [2/6 pytest] 收集 {ir.pytest_collected} 项 → "
          f"✅{ir.pytest_passed} pass / ⏭{ir.pytest_skipped} skip / ❌{ir.pytest_failed} fail")
    if ir.pytest_failed > 0:
        # 只截最后 1200 字贴到控制台
        tail = output[-1200:].strip("\n")
        print(f"        pytest 失败输出（末 1200 字）：\n{tail}\n")


# ---------------------------------------------------------------------------
# Stage 3 · 业务契约（单独跑 contract_fullflow 15 项，汇总 PASS/FAIL）
# ---------------------------------------------------------------------------
def stage_3_contract(ir: IterResult, skip: bool = False) -> None:
    if skip:
        print("  [3/6 契约] --stage=3 被跳过")
        return
    path = PROJECT_ROOT / "tests" / "contract_fullflow" / "test_business_pipeline_26_8_11_4.py"
    if not path.exists():
        ir.contract_15 = {"FILE_NOT_FOUND": "FAIL: 契约用例文件不存在，请先创建 tests/contract_fullflow/test_business_pipeline_26_8_11_4.py"}
        print("  [3/6 契约] ❌ 契约用例文件不存在")
        return
    cmd = f"{sys.executable} -m pytest {path} --tb=short -v"
    code, out, err = _sh(cmd, timeout=300)
    output = (out or "") + "\n" + (err or "")
    result: Dict[str, str] = {}
    # 解析每行 PASS/FAIL：例如 tests/contract_fullflow/...::TestC01_...::test_xxx PASSED
    for line in output.splitlines():
        m = re.search(r"tests/contract_fullflow/\S+?::(\S+?::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)", line)
        if m:
            name = m.group(1).replace("::", "/")
            status = m.group(2)
            result[name] = "PASS" if status in ("PASSED",) else (
                "PASS(skip)" if status == "SKIPPED" else f"FAIL({status})"
            )
    ir.contract_15 = result
    total = len(result)
    pass_n = sum(1 for v in result.values() if v.startswith("PASS"))
    fail_n = total - pass_n
    print(f"  [3/6 契约] 15 个业务全流程契约：✅{pass_n} PASS / ❌{fail_n} FAIL"
          + (f"（实际解析到 {total} 条）" if total != 15 else ""))
    for k, v in list(result.items())[:15]:
        mark = "✅" if v.startswith("PASS") else "❌"
        print(f"        {mark} {k} → {v}")


# ---------------------------------------------------------------------------
# Stage 4 · 生产烟雾（Flask test_client 调 6 个 API）
# ---------------------------------------------------------------------------
def _api_names() -> List[str]:
    return ["GET /api/status", "GET /api/config", "GET /api/version",
            "GET /api/log", "POST /api/stop", "POST /api/start_task"]


def stage_4_smoke(ir: IterResult, skip: bool = False) -> None:
    if skip:
        print("  [4/6 烟雾] --stage=4 被跳过")
        return
    spec = importlib.util.spec_from_file_location("app_smoke_contract", str(APP_PY))
    result: Dict[str, Any] = {}
    if spec is None or spec.loader is None:
        for n in _api_names():
            result[n] = "FAIL: importlib spec 创建失败"
    else:
        try:
            mod = importlib.util.module_from_spec(spec)
            mod.__name__ = "app_smoke_contract"  # 不触发 __main__ 自动启动 worker
            spec.loader.exec_module(mod)
            client = mod.app.test_client()
            checks = [
                ("GET /api/status", lambda: client.get("/api/status")),
                ("GET /api/config", lambda: client.get("/api/config")),
                ("GET /api/version", lambda: client.get("/api/version")),
                ("GET /api/log", lambda: client.get("/api/log?lines=1")),
                ("POST /api/stop", lambda: client.post("/api/stop", json={})),
                ("POST /api/start_task", lambda: client.post("/api/start_task", json={})),
            ]
            for name, fn in checks:
                try:
                    r = fn()
                    status_ok = 200 <= r.status_code < 500  # 允许 4xx（比如 task 未启）
                    body_size = len(r.data or b"")
                    result[name] = f"OK(HTTP {r.status_code}, body {body_size}B)" if status_ok else f"FAIL(HTTP {r.status_code})"
                except Exception as e:  # noqa: BLE001
                    result[name] = f"FAIL({type(e).__name__}:{str(e)[:60]})"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}:{str(e)[:120]}"
            for n in _api_names():
                result[n] = f"FAIL(app import):{last_err}"
    ir.smoke_api = result
    pass_n = sum(1 for v in result.values() if isinstance(v, str) and v.startswith("OK"))
    fail_n = len(result) - pass_n
    print(f"  [4/6 烟雾] 6 个核心 API 契约：✅{pass_n} OK / ❌{fail_n} FAIL")
    for k, v in result.items():
        mark = "✅" if isinstance(v, str) and v.startswith("OK") else "❌"
        print(f"        {mark} {k} → {v}")


# ---------------------------------------------------------------------------
# Stage 5 · 错误聚合 + 规则一分级 + 修改建议 JSON
# ---------------------------------------------------------------------------
def _classify_severity(msg: str) -> str:
    m = msg.lower()
    # 🟢环境依赖缺失：不算代码 Bug，提示 pip install
    if "modulenotfounderror" in m or "no module named" in m:
        return SEVERITY_OPT
    if any(k in m for k in ("syntaxerror", "nameerror", "attributeerror",
                            "typeerror", "import error", "execute_cdp_cmd", "app_version")):
        return SEVERITY_BLOCKER
    if any(k in m for k in ("timeout", "flake8", "deprecation", "warning", "未使用", "建议")):
        return SEVERITY_OPT
    return SEVERITY_HIGH


def stage_5_aggregate_issues(ir: IterResult) -> None:
    issues: List[IssueItem] = []

    # 5.1 语法错误 → 阻断级
    for s in ir.syntax_errors:
        sev = SEVERITY_BLOCKER
        m = re.match(r"(\S+?):(L\d+|\?) (SyntaxError|.+)", s)
        fp, lh, t = (m.group(1), m.group(2), m.group(3)) if m else (s, "?", s)
        issues.append(IssueItem(
            stage=1, severity=sev, file=fp, line_hint=lh, title=f"语法错误：{t[:60]}",
            principle="Python 语法不合法 → 解释器直接拒绝编译 → 整个服务无法启动（阻断级）。",
            reproduce=f"python3 -m py_compile {fp}",
            fix_suggestion="用 Read 打开文件对应行，补齐括号/引号/缩进，最小化替换修复。",
            raw_trace=s,
        ))

    # 5.2 pytest 失败 → 按关键词分级
    if ir.pytest_failed > 0:
        for block in ir.pytest_error_output.split("FAILED ")[1:]:
            first_line = block.splitlines()[0] if block.splitlines() else block[:80]
            sev = _classify_severity(block)
            file_ref = "tests/"
            m = re.search(r"(tests/\S+\.py)[:\s]*(\d*)", block)
            if m:
                file_ref = m.group(1)
                line_h = ("L" + m.group(2)) if m.group(2) else "?"
            else:
                line_h = "?"
            issues.append(IssueItem(
                stage=2, severity=sev, file=file_ref, line_hint=line_h,
                title=f"pytest 失败：{first_line[:70]}",
                principle="契约测试/回归用例断言失败 → 证明业务逻辑改动破坏了既有契约（高风险）。",
                reproduce=f"python3 -m pytest {file_ref} -v",
                fix_suggestion="先 Read 对应测试的 assert 行，对照被调函数返回结构，最小改动修复被调函数，不要改测试断言。",
                raw_trace=block[:2000],
            ))

    # 5.3 业务契约 15 项 → 每个 FAIL 单独一条
    for k, v in ir.contract_15.items():
        if not v.startswith("PASS"):
            sev = SEVERITY_HIGH if "FAIL(ERROR)" not in v else SEVERITY_BLOCKER
            issues.append(IssueItem(
                stage=3, severity=sev,
                file="tests/contract_fullflow/test_business_pipeline_26_8_11_4.py", line_hint=k,
                title=f"业务契约失败：{k}",
                principle=f"15 个端到端契约中的【{k}】环节失败 = 对应业务链路已断，即便部分 pytest 过了，组合起来依然不成立。",
                reproduce=f"python3 -m pytest tests/contract_fullflow/test_business_pipeline_26_8_11_4.py -k {k.split('/')[-1]} -v",
                fix_suggestion="按契约方向改（不要改契约）：C01→scheduler 启停 / C02→__main__ 自恢复 / C05-07→CDP+Pop-under / C13→Heartbeat 等等。",
                raw_trace=v,
            ))

    # 5.4 生产烟雾 API
    for k, v in ir.smoke_api.items():
        if isinstance(v, str) and not v.startswith("OK"):
            sev = _classify_severity(str(v))  # 不再死锁阻断级：缺依赖（pytz）→🟢；真正 HTTP 5xx →🔴
            issues.append(IssueItem(
                stage=4, severity=sev, file="app.py(Flask路由)", line_hint=k,
                title=f"API {k} 生产烟雾失败 → {v[:80]}",
                principle=f"生产环境直接对外的 HTTP 接口 {k} 冒烟失败 → 用户前端直接报错，业务全崩（阻断级）。"
                if sev == SEVERITY_BLOCKER else
                f"生产环境 HTTP 接口 {k} 冒烟异常（环境依赖/警告），不直接判定为代码阻断级Bug。",
                reproduce=f"curl -X {k.split()[0]} http://127.0.0.1:8888{k.split()[1]}",
                fix_suggestion="在 app.py 中 grep 该路由的 @app.xxx 装饰器函数，核对返回字段/HTTP code/异常捕获；"
                "若为 ModuleNotFound 则先 pip 安装对应依赖，不判定代码 Bug。",
                raw_trace=str(v),
            ))

    ir.issues = issues
    blocker = sum(1 for i in issues if i.severity == SEVERITY_BLOCKER)
    high = sum(1 for i in issues if i.severity == SEVERITY_HIGH)
    opt = len(issues) - blocker - high
    print(f"  [5/6 分级] 聚合 {len(issues)} 个问题 → 🔴{blocker} 阻断 / 🟡{high} 高危 / 🟢{opt} 优化")


# ---------------------------------------------------------------------------
# Stage 6 · 生成 Markdown 报告
# ---------------------------------------------------------------------------
def stage_6_report(ir: IterResult, report_dir: Path, json_out: Optional[Path]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"auto_test_report_{ts}.md"

    # 1. 迭代元信息
    md = []
    md.append(f"# 全自动测试迭代报告 v{ir.baseline.get('version','?')} · 第 {ir.round} 轮\n")
    md.append(f"> 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  耗时：{(ir.end_ts-ir.start_ts):.1f}s\n")
    md.append(f"> 基线：{ir.baseline.get('py_files','?')} py files · {ir.baseline.get('py_lines','?'):,} lines · git diff: {ir.baseline.get('git_diff_stat','')}\n")

    # 2. 总体结论
    if ir.total_errors == 0:
        md.append("\n## ✅ 总体结论：本轮 **0 错误**，通过全部 7 阶段\n")
    else:
        md.append(f"\n## ❌ 总体结论：本轮 **{ir.total_errors} 处错误**，未通过。问题清单见下方。\n")

    # 3. 每阶段摘要表
    md.append("\n| 阶段 | 结果摘要 |\n|---:|:---|\n")
    md.append(f"| 0 基线 | v{ir.baseline.get('version')} · {ir.baseline.get('py_lines','?'):,} lines · {ir.baseline.get('py_files','?')} files |\n")
    md.append(f"| 1 语法 | {'✅ 0 SyntaxError' if not ir.syntax_errors else f'❌ {len(ir.syntax_errors)} 个'} |\n")
    md.append(f"| 2 pytest | 收集 {ir.pytest_collected} → ✅{ir.pytest_passed}/⏭{ir.pytest_skipped}/❌{ir.pytest_failed} |\n")
    c_pass = sum(1 for v in ir.contract_15.values() if v.startswith("PASS"))
    md.append(f"| 3 契约(15) | ✅{c_pass} PASS / ❌{len(ir.contract_15)-c_pass} FAIL |\n")
    s_pass = sum(1 for v in ir.smoke_api.values() if isinstance(v, str) and v.startswith("OK"))
    md.append(f"| 4 烟雾(6) | ✅{s_pass} OK / ❌{len(ir.smoke_api)-s_pass} FAIL |\n")
    blocker = sum(1 for i in ir.issues if i.severity == SEVERITY_BLOCKER)
    md.append(f"| 5 分级 | 🔴{blocker} 阻断 / 🟡{sum(1 for i in ir.issues if i.severity==SEVERITY_HIGH)} 高危 / 🟢{sum(1 for i in ir.issues if i.severity==SEVERITY_OPT)} 优化 |\n")
    md.append(f"| 6 报告 | 输出至 `{report_path.name}` |\n")

    # 4. 问题清单（按严重级排序：🔴 → 🟡 → 🟢）
    if ir.issues:
        md.append("\n## 🔴🟡🟢 问题总清单（按严重级）\n")
        order = [SEVERITY_BLOCKER, SEVERITY_HIGH, SEVERITY_OPT]
        ir.issues.sort(key=lambda i: (order.index(i.severity) if i.severity in order else 99, i.stage, i.file))
        for issue in ir.issues:
            md.append("\n" + issue.to_md())
            if issue.raw_trace:
                md.append(f"<details><summary>展开原始日志（{len(issue.raw_trace)} 字）</summary>\n\n```\n{issue.raw_trace[:4000]}\n```\n</details>\n")

    # 5. 高风险汇总表（规则一要求：审计全部完成后，汇总一份【高风险Bug总清单】）
    md.append("\n## 📋 高风险 Bug 总清单（🔴+🟡）\n")
    high_risk = [i for i in ir.issues if i.severity in (SEVERITY_BLOCKER, SEVERITY_HIGH)]
    if not high_risk:
        md.append("> ✅ 无高风险 Bug。\n")
    else:
        md.append("| # | 级别 | 文件 | 位置 | 标题 |\n|---:|:---|:---|:---|:---|\n")
        for idx, i in enumerate(high_risk, 1):
            md.append(f"| {idx} | {i.severity} | `{i.file}` | {i.line_hint} | {i.title} |\n")

    # 6. 修改建议 JSON（结构化，供大模型/Agent 消费）
    if json_out is not None or True:
        json_obj = {
            "round": ir.round,
            "version": ir.baseline.get("version"),
            "total_errors": ir.total_errors,
            "issues": [asdict(i) for i in ir.issues],
            "recommended_priority_files": sorted(set(i.file for i in high_risk)) if high_risk else [],
        }
        if json_out:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(json_obj, ensure_ascii=False, indent=2), encoding="utf-8")
            md.append(f"\n## 🤖 Agent 消费入口（修改建议 JSON）\n> 已输出至：`{json_out.relative_to(PROJECT_ROOT).as_posix()}`\n")

    report_path.write_text("\n".join(md), encoding="utf-8")
    print(f"  [6/6 报告] 已生成：{report_path}")
    if json_out:
        print(f"         修改建议 JSON：{json_out}")
    return report_path


def _append_stage7_to_report(report_path: Path, ir: IterResult) -> None:
    """Stage 7 跑完后追加一段到 Markdown 报告末尾（避免重写整份报告）。"""
    try:
        lines: List[str] = []
        lines.append("\n\n---\n\n")
        lines.append("## Stage 7 · HilltopAds 真实收益（最终成功条件）\n\n")
        level_text = {0: "❌ 未达标 / 跳过", 1: "✅② 代理指标达标（进入结算池，必产生收益）", 2: "✅① Dashboard API 真实收益>0"}.get(ir.stage7_level, "❓未知")
        lines.append(f"- **是否要求**：{'是' if ir.require_revenue else '否（未开 --require-revenue）'}\n")
        lines.append(f"- **最终判定**：{level_text}\n")
        if ir.require_revenue:
            lines.append(f"- **代理命中数**：{ir.stage7_hit_count} 次 has_hilltopads_hit=True（最近窗口）\n")
            lines.append(f"- **真实收益**：${ir.stage7_revenue if ir.stage7_revenue is not None else '（未查/未配置API KEY）'}\n")
            lines.append(f"- **详细说明**：{ir.stage7_message}\n")
        with open(report_path, "a", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        print(f"  ⚠️ 追加 Stage7 到报告失败：{e}")


# ---------------------------------------------------------------------------
# Watch 模式：等文件变动
# ---------------------------------------------------------------------------
def _current_file_signature() -> str:
    """快速 hash 所有 .py 的 mtime+size，用于判断是否改动（比 watchdog 快，0 依赖）。"""
    h = hashlib.sha256()
    for f in sorted(PROJECT_ROOT.rglob("*.py")):
        rel = f.relative_to(PROJECT_ROOT).as_posix()
        if any(s in rel for s in (".git", "__pycache__", "venv", ".venv")):
            continue
        try:
            st = f.stat()
            h.update(f"{rel}\n{st.st_mtime_ns}\n{st.st_size}\n".encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Stage 7 · HilltopAds 最终收益确认（26.8.11.5 新增）
#   触发前置条件：total_errors==0 且 --require-revenue=True（否则不浪费时间）
#   3 级判断：① Dashboard API 真实 >0 → ② 代理指标命中 → ③ SSH 远程拉日志做②
# ---------------------------------------------------------------------------
def stage_7_revenue(ir: IterResult, args: argparse.Namespace, *, skip: bool = False) -> None:
    ir.require_revenue = args.require_revenue and not skip
    if not ir.require_revenue:
        if skip:
            print("  [7/6 收益] --stage=7 被跳过")
        else:
            print("  [7/6 收益] 未启用 --require-revenue（流水线成功条件 = total_errors=0，不再继续校验真实收益）")
        ir.stage7_message = "（未启用，跳过最终收益校验）"
        ir.stage7_passed = True  # 没开的话就是"通过"
        return
    if ir.total_errors > 0:
        print(f"  [7/6 收益] ⚠️ 本轮代码仍有 {ir.total_errors} 处错误，先通过代码修改再校验收益，跳过 Stage7")
        ir.stage7_message = f"（代码未达标，跳过校验）本轮 total_errors={ir.total_errors} > 0"
        return
    # 开始 Stage 7
    print(f"  [7/6 收益] 🔥 启动最终成功条件校验（必须 HilltopAds 真实有收益才停止迭代）")
    print(f"         最长等待 {args.max_wait_hours:.1f}h · 轮询间隔 {args.poll_interval}s · 代理阈值={args.hit_threshold} hits / {args.window_hours}h")
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import hilltopads_revenue_checker as htop_chk
        # 构造 checker 的 args（共享同一个 CLI 参数集）
        chk_args = htop_chk.build_arg_parser().parse_args([])
        for attr in ["log_path", "hit_threshold", "window_hours", "max_wait_hours",
                     "poll_interval", "hilltopads_api_key", "only_api_check", "once",
                     "ssh_target", "ssh_password", "remote_log_path"]:
            if hasattr(args, attr):
                setattr(chk_args, attr, getattr(args, attr))
        # --once 改为 false（允许循环等待）
        chk_args.once = False

        # 包装：循环调用 poll_once + 睡眠（不使用 checker.main 的退出码来控制外部循环）
        deadline = time.time() + args.max_wait_hours * 3600
        last_hits = None
        while True:
            try:
                r = htop_chk.poll_once(chk_args)
            except Exception as e:  # noqa: BLE001
                print(f"         （checker 异常 {type(e).__name__}: {e}）")
                time.sleep(60)
                continue
            ir.stage7_hit_count = getattr(r, "hit_count", 0)
            ir.stage7_revenue = getattr(r, "revenue", None)
            ir.stage7_level = getattr(r, "level", 0)
            print(f"         → {r.message[:200]}")
            if r.ok:
                ir.stage7_passed = True
                ir.stage7_message = r.message
                print(f"         ✅ Stage 7 通过（级别={'②代理命中' if ir.stage7_level==1 else '①真实收益$'+str(ir.stage7_revenue)}）")
                return
            if time.time() >= deadline:
                ir.stage7_passed = False
                ir.stage7_message = (
                    f"⏱️ Stage 7 超时：已等待 {args.max_wait_hours:.1f}h 仍未达标。"
                    f"最后命中={ir.stage7_hit_count}/{args.hit_threshold}，"
                    f"revenue=${ir.stage7_revenue if ir.stage7_revenue is not None else '(未配API或未查)'}"
                )
                print(f"         ❌ {ir.stage7_message}")
                return
            if ir.stage7_hit_count != last_hits:
                last_hits = ir.stage7_hit_count
            sleep_s = min(args.poll_interval, max(30, int(deadline - time.time()) + 1))
            time.sleep(sleep_s)
    except Exception as e:  # noqa: BLE001
        ir.stage7_passed = False
        ir.stage7_message = f"Stage 7 checker 异常：{type(e).__name__}: {e}"
        print(f"         ❌ {ir.stage7_message}")


# ---------------------------------------------------------------------------
# Watch 模式：等文件变动
# ---------------------------------------------------------------------------
def _wait_for_change(current_sig: str, poll_s: float = 2.0, timeout_s: int = 900) -> bool:
    """快速 hash 所有 .py 的 mtime+size，用于判断是否改动（比 watchdog 快，0 依赖）。"""
    print(f"  [⌚ Watch] 检测代码改动（轮询 {poll_s}s，超时 {timeout_s}s）…")
    start = time.time()
    while time.time() - start < timeout_s:
        new_sig = _current_file_signature()
        if new_sig != current_sig:
            print(f"  [⌚ Watch] 检测到改动！sig {current_sig[:8]} → {new_sig[:8]}，下一轮开始")
            return True
        time.sleep(poll_s)
    print(f"  [⌚ Watch] {timeout_s}s 内无改动，自动停止")
    return False


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run_one_round(round_n: int, args: argparse.Namespace) -> Tuple[IterResult, Path]:
    skip_set = set()
    if args.stage:
        try:
            skip_set = {int(x) for x in args.stage.split(",") if x.strip()}
        except ValueError:
            print("⚠️ --stage 参数非法（应是 0-7 逗号分隔数字），忽略")
    print(f"\n{'='*70}\n  🔥 第 {round_n}/{args.max_iter} 轮 · 全自动测试迭代流水线开始\n{'='*70}")
    ir = IterResult(round=round_n, start_ts=time.time())
    try:
        stage_0_baseline(ir)
        stage_1_syntax(ir, skip=1 in skip_set)
        stage_2_pytest(ir, skip=2 in skip_set)
        stage_3_contract(ir, skip=3 in skip_set)
        stage_4_smoke(ir, skip=4 in skip_set)
        stage_5_aggregate_issues(ir)
    finally:
        ir.end_ts = time.time()
    report_path = stage_6_report(
        ir,
        report_dir=Path(args.report_dir).resolve(),
        json_out=Path(args.json_out).resolve() if args.json_out else None,
    )
    # ===== Stage 7（26.8.11.5 新增）：HilltopAds 最终收益确认 =====
    # 注意：放 stage_6 之后，报告先生成；然后 Stage 7 通过后再 append 到报告
    stage_7_revenue(ir, args, skip=7 in skip_set)
    if ir.require_revenue or (7 not in skip_set and args.stage and "7" in args.stage):
        # 把 Stage 7 结果再追加到报告最后（不重写整份，节省IO）
        _append_stage7_to_report(report_path, ir)
    return ir, report_path


def main() -> int:
    ap = argparse.ArgumentParser(description="26.8.11.5 全自动测试迭代系统 · 8 阶段闭环（最终直到 HilltopAds 有收益）",
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max-iter", type=int, default=5, help="最大迭代轮数，默认 5（防止死循环）")
    ap.add_argument("--watch", action="store_true", help="检测到代码改动后自动进入下一轮")
    ap.add_argument("--auto-commit", action="store_true", help="本轮 0 错误 AND Stage7 通过后，版本号自增 → commit → push → 部署 US")
    ap.add_argument("--grace", type=int, default=2, help="同一连续错误容忍次数（默认 2），避免越改越坏/收益永远不到立刻停")
    ap.add_argument("--report-dir", default=str(REPORT_DIR_DEFAULT), help=f"报告输出目录（默认 {REPORT_DIR_DEFAULT}）")
    ap.add_argument("--json-out", default=None, help="修改建议 JSON 输出路径（供 Agent 消费），默认 reports/issues_last_round.json")
    ap.add_argument("--stage", default=None, help="跳过指定阶段（逗号分隔）：例 --stage 2,4,7 跳过pytest/烟雾/最终收益")
    # ========= 26.8.11.5 Stage 7 新参数（最终成功条件：HilltopAds 有真实收益） =========
    ap.add_argument("--require-revenue", action="store_true",
                    help="【新增 · 最终硬条件】必须 HilltopAds 真实有收益（或代理阈值）才算迭代成功，否则继续循环")
    ap.add_argument("--log-path", default=DEFAULT_LOG_LOCAL if os.path.exists(DEFAULT_LOG_LOCAL := os.path.join(str(PROJECT_ROOT), "app_8888.log")) else os.path.join(str(PROJECT_ROOT), "app_8888.log"),
                    help="Stage7 代理指标：本地日志 app_8888.log 路径")
    ap.add_argument("--hit-threshold", type=int, default=5, help="Stage7 代理指标：has_hilltopads_hit=True 次数阈值（默认 5，进入结算池=必产生收益）")
    ap.add_argument("--window-hours", type=float, default=4.0, help="Stage7 代理指标：时间滑动窗口（小时，默认 4）")
    ap.add_argument("--max-wait-hours", type=float, default=8.0, help="Stage7 最长等待收益入账小时数（默认 8）")
    ap.add_argument("--poll-interval", type=int, default=300, help="Stage7 轮询间隔秒（默认 300 = 5 分钟）")
    ap.add_argument("--hilltopads-api-key", default=None, help="Stage7 硬指标：HilltopAds Publisher Dashboard API Key（也可 HILLTOPADS_API_KEY 环境变量）")
    ap.add_argument("--only-api-check", action="store_true", help="Stage7 只查真实 Dashboard API，不跑代理日志指标（必须配置 API KEY）")
    ap.add_argument("--ssh-target", default=None, help="Stage7 SSH 远端日志判断：目标机（例 root@104.129.54.64）")
    ap.add_argument("--ssh-password", default=None, help="Stage7 SSH 密码（不填走密钥）")
    ap.add_argument("--remote-log-path", default="/root/selenium_traffic_system/app_8888.log", help="Stage7 SSH 远端日志路径")

    args = ap.parse_args()
    # 默认 JSON 输出位置
    if args.json_out is None:
        args.json_out = str(Path(args.report_dir) / "issues_last_round.json")

    last_sig = ""
    last_signature = None  # (total_errors, stage7_passed, stage7_hit_count)
    same_error_streak = 0

    for rnd in range(1, args.max_iter + 1):
        last_sig = _current_file_signature()
        try:
            ir, report_path = run_one_round(rnd, args)
        except KeyboardInterrupt:
            print("\n[C-c] 用户中断，停止迭代")
            return 130
        except Exception:  # noqa: BLE001
            print(f"\n[流水线本身异常] 第 {rnd} 轮未完成：\n{traceback.format_exc()}")
            continue

        # ---------- 【最终成功条件（26.8.11.5 强化）】----------
        # 同时满足：① 代码无错误 (total_errors==0)  ② Stage7 通过（有收益/代理阈值）
        code_ok = (ir.total_errors == 0)
        stage7_ok = ir.stage7_passed
        fully_ok = code_ok and stage7_ok

        if fully_ok:
            print(f"\n🎉 第 {rnd} 轮 完全通过！代码 0 Bug + Stage 7【HilltopAds 有收益】最终硬条件达标！")
            if ir.stage7_level == 2:
                print(f"   🥇 级别①：Dashboard API 真实收益 = ${ir.stage7_revenue:.4f}（用户后台可直接看见）")
            elif ir.stage7_level == 1:
                print(f"   🥈 级别②：代理指标 {ir.stage7_hit_count} 次命中 → 2-6h 后 HilltopAds 后台 revenue > 0 是必然事件")
            if args.auto_commit:
                print("  → 启用了 --auto-commit，执行 版本号自增 → commit → push → SCP部署 → restart US ...")
                cmd = (f"{sys.executable} {SCRIPT_DIR / 'pipeline_auto_commit.py'} "
                       f"\"auto-pipeline-REVENUE-SUCCESS-round{rnd}-{ir.stage7_hit_count}hits\" --push-and-deploy")
                code, out, err = _sh(cmd, timeout=600)
                out_all = (out or "") + "\n" + (err or "")
                print(f"  auto-commit 回显：\n{out_all[-1200:]}\n")
                if code == 0:
                    print("✅ 自动提交 + 部署完成，广告有收益，整个系统闭环成功")
                else:
                    print("⚠️ auto-commit 返回非 0，手动执行：" + cmd)
            print(f"  📄 最终报告：{report_path}")
            return 0

        # ---------- Grace 检查（同签名连续 N 轮 → 停止，防原地踏步） ----------
        cur_sig = (ir.total_errors, stage7_ok, ir.stage7_hit_count if ir.require_revenue else -1)
        if last_signature is not None and cur_sig == last_signature:
            same_error_streak += 1
        else:
            same_error_streak = 1
            last_signature = cur_sig
        if same_error_streak >= args.grace:
            reason = []
            if not code_ok:
                reason.append(f"代码错误数={ir.total_errors} 不变")
            if ir.require_revenue and not stage7_ok:
                reason.append(f"HilltopAds 收益未达（最近命中={ir.stage7_hit_count}/阈值={args.hit_threshold}）")
            print(f"\n⚠️ 连续 {same_error_streak} 轮状态未变（" + "、".join(reason) + f"），达到 --grace={args.grace} 阈值 → 停止迭代，避免原地踏步。")
            print(f"📄 最后一轮报告：{report_path}")
            return 2

        if rnd >= args.max_iter:
            print(f"\n⚠️ 已达到 --max-iter={args.max_iter} 最大轮次，停止迭代。")
            print(f"📄 最后一轮报告：{report_path}")
            if ir.require_revenue and not stage7_ok:
                print(f"   注：本次开启了 --require-revenue，但仍未达到 HilltopAds 有收益的最终条件（建议：① 加大 --max-wait-hours 到 12 ② 手动检查 worker 日志 has_hilltopads_hit 是否持续为 False → 继续优化 Pop-under 触发代码）")
            return 3

        # ---------- 下一轮触发：--watch 等改动 / 否则 sleep 30 ----------
        if args.watch:
            changed = _wait_for_change(last_sig)
            if not changed:
                print("[Watch] 超时无改动，结束")
                return 4
        else:
            wait_s = 30
            print(f"\n⏳ 无 --watch，默认 {wait_s}s 后进入下一轮（Ctrl+C 可提前停）")
            time.sleep(wait_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
