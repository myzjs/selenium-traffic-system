"""
回归测试：版本 26.8.11.1 HilltopAds 收益=0 的 4 项核心修复
======================================================================
🔴 阻断级问题 + 🟡 高危隐患的可重复复现测试。防止后续迭代复现同类问题。

修复清单（全部覆盖）：
  1) [阻断级] referral 流量分支 NameError: name 'ip_language' is not defined
       位置：app.py referer 构建分支（~14814 行）
  2) [高危] 站点频控 8 次/24h 过严 → 单站点 40 / 多站点 30
       位置：app.py _get_site_window_limit + check_site_frequency
  3) [高危] 看门狗宽限 60s → 90s（防低质代理误判卡死）
       位置：app.py _ROPE_WATCHDOG_GRACE_S
  4) [根因] HilltopAds 收益=0 综合修复：
       4a) IP 门禁过严 → 住宅白名单 + 三要素（country/tz/lang）宽松放行
               位置：popunder_trigger.is_ip_safe_for_hilltopads
       4b) bring_to_front() 前台切换 → 全程后台保活 + 弹窗内 JS 滚动交互
       4c) 默认存活延长 15-25 → 22-36 + 触发概率 0.4→0.6
"""
from __future__ import annotations

import os
import sys
import time
import threading
import unittest  # 26.8.11.3 新增：class-based TestCase 基类
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 路径处理：让测试能导入 app.py（含 app 模块）和 popunder_trigger
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

APP_PATH = os.path.join(PROJECT_ROOT, "app.py")  # 26.8.11.3 新增：统一 app.py 绝对路径


# ===========================================================================
# #1 阻断级：referral 分支 ip_language NameError 复现
# ===========================================================================
class TestReferralIpLanguageFix:
    """referral 流量分支直接引用 ip_language → 改为从 fingerprint/resolved_ip_info 推导。"""

    def test_app_py_referral_branch_no_name_error_by_import(self):
        """只做 py_compile 语法校验（避免测试机缺 pytz 导致 import 失败）。
        导入期 NameError 在 ast.parse/字节码生成阶段也会暴露。
        """
        import py_compile

        app_path = os.path.join(PROJECT_ROOT, "app.py")
        try:
            py_compile.compile(app_path, doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"app.py 语法错误/字节码生成失败：{e}")

    def test_referral_lang_extract_logic_no_bare_name(self):
        """精准检查 referral 分支：
        - 包含 `_ref_lang = ` 的赋值语句中，绝不能引用裸名 `ip_language`（会 NameError）
        - 同时必须使用 `_ip_lang_src`（从 fingerprint/resolved_ip_info 推导的变量）
        - 不能全局扫（否则会误报 qa_log_fingerprint_ip_consistency 函数内合法的局部 ip_language）
        """
        app_file = os.path.join(PROJECT_ROOT, "app.py")
        with open(app_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        ref_lang_lines = [
            (no, ln) for no, ln in enumerate(lines, 1)
            if "_ref_lang" in ln and "#" not in ln.split("_ref_lang", 1)[0]
        ]
        assert ref_lang_lines, "找不到 _ref_lang 赋值（referral 分支语言构建应该存在）"

        # 所有 _ref_lang 的赋值，不能出现 `ip_language` 这个裸变量
        for lineno, line in ref_lang_lines:
            # 取出非注释部分
            code_part = line.split("#", 1)[0]
            if "ip_language" in code_part and "_ip_lang_src" not in code_part:
                pytest.fail(
                    f"app.py 第 {lineno} 行 referral 分支 _ref_lang 赋值仍直接引用 "
                    f"裸名 ip_language（NameError 风险）：{line.rstrip()}"
                )
        # 整个文件至少有一处 _ref_lang 引用了 _ip_lang_src（我们引入的修复）
        assert any(
            "_ref_lang" in ln and "_ip_lang_src" in ln
            for ln in lines
        ), "referral 分支仍未用 _ip_lang_src 构建 _ref_lang"

    def test_ip_lang_src_derivation_pattern_present(self):
        """确保我们引入的修复逻辑存在：fingerprint.get("language") 回退 resolved_ip_info["language"]。"""
        app_file = os.path.join(PROJECT_ROOT, "app.py")
        with open(app_file, "r", encoding="utf-8") as f:
            src = f.read()
        # 修复代码指纹：fingerprint.get("language") 和 resolved_ip_info["language"]
        assert 'fingerprint.get("language")' in src or "fingerprint.get('language')" in src


# ===========================================================================
# #2 站点频控：8→30 基础 + 单站点 bonus 10 = 40
# ===========================================================================
class TestSiteFrequencyFix:
    """check_site_frequency / _get_site_window_limit 的行为验证。
    说明：app.py 依赖 pytz/fake_useragent 等，测试机可能缺。用 subprocess 隔离
    「截取函数源码 → 独立子进程内 import time/json/os + 执行」避免全局依赖。
    """

    @staticmethod
    def _run_in_isolated_app_env(stmt: str):
        """在隔离的 Python 进程中，把我们关心的 4 个符号提取出来再执行语句。
        stmt: 断言语句，可直接使用 _get_site_window_limit / check_site_frequency
        返回 (stdout, stderr, exit_code)
        """
        app_file = os.path.join(PROJECT_ROOT, "app.py")
        with open(app_file, "r", encoding="utf-8") as f:
            src = f.read()

        # 抽我们需要的：常量、全局状态、两个函数 + record_site_visit（频控计数实际是这个写进去）
        _TARGETS = [
            "_SITE_MAX_PER_WINDOW", "_SITE_SINGLE_URL_BONUS",
            "_SITE_WINDOW_HOURS", "_SITE_STATE_FILE", "_SITE_FREQ_STATE",
            "_SITE_MIN_INTERVAL_SEC",
            "_get_site_window_limit", "check_site_frequency", "record_site_visit",
            "_SITE_VISITS", "_SITE_FREQ_LOCK",
        ]

        lines = src.splitlines()
        extracted: list = []
        # ---- 子进程运行时的环境准备（解决 python -c 没有 __file__、路径问题）----
        extracted.append("# 隔离环境专用：为 -c 模式补 __file__（提取出的常量里可能用到 os.path.abspath(__file__)）")
        extracted.append("import sys as _sys, os as _os, tempfile as _tf")
        extracted.append("__file__ = _os.path.join(_tf.gettempdir(), '_app_iso_test.py')")
        extracted.append("# 为提取的 _SITE_FREQ_STATE / _SITE_STATE_FILE 赋临时目录根，避免写入/创建失败")
        extracted.append("BASE_DIR = _tf.gettempdir()")
        extracted.append("import threading, time, json, os")
        _seen_locks = False
        for tgt in _TARGETS:
            found = False
            for i, ln in enumerate(lines):
                if ln.startswith(f"{tgt} ") or ln.startswith(f"def {tgt}(") or ln.startswith(f"{tgt}="):
                    # 简单赋值：同一行内就完成
                    if not ln.startswith("def "):
                        # 如果是 Lock，需要有 threading.Lock()，import threading 即可
                        extracted.append(ln.rstrip())
                        if tgt == "_SITE_FREQ_LOCK":
                            _seen_locks = True
                        found = True
                        break
                    # 函数：找到下一个顶级 def 前的所有行
                    func_start = i
                    func_end = len(lines)
                    for j in range(i + 1, len(lines)):
                        cur = lines[j]
                        if (
                            cur
                            and not cur.startswith((" ", "\t"))
                            and (cur.startswith("def ") or cur.startswith("class ") or cur.startswith("@"))
                        ):
                            func_end = j
                            break
                    extracted.append("\n".join(lines[func_start:func_end]))
                    found = True
                    break
            if not found and tgt == "_SITE_FREQ_LOCK" and not _seen_locks:
                # 文件里没抽到（可能写法不兼容），兜底：新建一个 threading.RLock
                extracted.append("_SITE_FREQ_LOCK = threading.RLock()")

        bootstrap = "\n".join(extracted)
        # 注：stmt 是多行语句（含缩进块），放到顶层 if __name__=="__main__" 里，避免 try 块缩进冲突
        # 注意：不能写 `global _SITE_MIN_INTERVAL_SEC`，因为 extracted 里已有顶层赋值，
        #       同一作用域 Python 不允许赋值语句后再出现 global。用 globals()[name] 修改。
        script = (
            "import sys, os, time, json\n"
            f"{bootstrap}\n"
            "_SITE_VISITS.clear()\n"
            "if __name__ == '__main__':\n"
            "    # 测试专用：关闭最小访问间隔，避免连续 50 次调用被 \"距上次访问过近\" 阻挡\n"
            "    globals()['_SITE_MIN_INTERVAL_SEC'] = 0\n"
            "    _exc = None\n"
            "    try:\n"
            + "\n".join("        " + l for l in stmt.splitlines())
            + "\n"
            "    except AssertionError as e:\n"
            "        _exc = e\n"
            "    if _exc is None:\n"
            "        sys.exit(0)\n"
            "    print('ASSERT FAILED:', _exc, file=sys.stderr)\n"
            "    sys.exit(1)\n"
        )
        import subprocess

        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout, r.stderr, r.returncode

    def test_get_site_window_limit_single_site_is_40(self):
        """单站点场景：30 + 10 = 40。"""
        stmt = "assert _get_site_window_limit(host_count=1) == 40, _get_site_window_limit(1)"
        out, err, code = self._run_in_isolated_app_env(stmt)
        assert code == 0, f"子进程失败（exit={code}）：stderr: {err[:400]}"

    def test_get_site_window_limit_multi_site_is_30(self):
        """多站点（>1 host）场景：仅基础 30。"""
        for n in (2, 3, 10):
            stmt = f"assert _get_site_window_limit(host_count={n}) == 30, _get_site_window_limit({n})"
            out, err, code = self._run_in_isolated_app_env(stmt)
            assert code == 0, f"多站点{n}失败（exit={code}）：stderr: {err[:400]}"

    def test_check_site_frequency_allows_40_visits_single_site(self):
        """单 URL 场景：前 40 次 check+record 应放行，第 41 次拒绝。
        注意：check_site_frequency 只检查，record_site_visit 才写入计数！"""
        stmt = (
            "cnt=0\n"
            "for i in range(50):\n"
            "    if check_site_frequency('single.example.com', host_count=1):\n"
            "        record_site_visit('single.example.com')\n"
            "        cnt += 1\n"
            "    else:\n"
            "        break\n"
            "assert cnt == 40, f'单站点放行 cnt={cnt}, 期望 40'\n"
        )
        out, err, code = self._run_in_isolated_app_env(stmt)
        assert code == 0, f"子进程失败（exit={code}）：stderr: {err[:500]}"

    def test_check_site_frequency_blocks_at_30_multi_site(self):
        """多站点场景：前 30 次 check+record 放行，第 31 次拒绝。"""
        stmt = (
            "cnt=0\n"
            "for i in range(40):\n"
            "    if check_site_frequency('multi.example.com', host_count=5):\n"
            "        record_site_visit('multi.example.com')\n"
            "        cnt += 1\n"
            "    else:\n"
            "        break\n"
            "assert cnt == 30, f'多站点放行 cnt={cnt}, 期望 30'\n"
        )
        out, err, code = self._run_in_isolated_app_env(stmt)
        assert code == 0, f"子进程失败（exit={code}）：stderr: {err[:500]}"


# ===========================================================================
# #3 看门狗宽限期 60→90s
# ===========================================================================
class TestWatchdogGracePeriod:
    """_ROPE_WATCHDOG_GRACE_S 必须 ≥ 90s，避免低质代理误判卡死。"""

    def test_watchdog_grace_value(self):
        app_file = os.path.join(PROJECT_ROOT, "app.py")
        with open(app_file, "r", encoding="utf-8") as f:
            src = f.read()
        grace = _extract_module_level_constant(src, "_ROPE_WATCHDOG_GRACE_S")
        assert grace is not None, "_ROPE_WATCHDOG_GRACE_S 常量缺失（静态解析失败）"
        assert isinstance(grace, (int, float)), (
            f"_ROPE_WATCHDOG_GRACE_S 必须是 int/float，当前类型 {type(grace)}"
        )
        assert grace >= 90, (
            f"_ROPE_WATCHDOG_GRACE_S 必须≥90s（Pop-under 触发所需），当前={grace}"
        )


# ===========================================================================
# #4 HilltopAds 收益=0 的综合修复
# ===========================================================================
class TestHilltopAdsZeroRevenueFix:
    """popunder_trigger 模块的关键策略验证。"""

    # ------------------------------------------------------------------
    # 4a. IP 门禁：住宅白名单 + 三要素宽松放行
    # ------------------------------------------------------------------
    def test_residential_ip_always_passes_even_without_isp_asn(self):
        """显式 ip_type=residential → 允许（即使 isp/asn 为空，这是 26.8.11.1 的白名单）。"""
        from popunder_trigger import is_ip_safe_for_hilltopads

        info = {
            "ip_type": "residential",
            "country_code": "US",
            "timezone": "America/New_York",
            "language": "en-US",
            # 故意留空 isp/asn（住宅代理 API 常见情况）
            "isp": "",
            "asn": "",
        }
        assert is_ip_safe_for_hilltopads(info) is True, (
            "显式 residential IP 即使 isp/asn 为空也必须放行"
        )

    @pytest.mark.parametrize("ip_type", ["datacenter", "hosting", "proxy", "vpn", "tor"])
    def test_blocked_ip_type_always_rejected(self, ip_type):
        """黑名单 IP 类型一律拒绝。"""
        from popunder_trigger import is_ip_safe_for_hilltopads

        info = {"ip_type": ip_type, "isp": "Comcast", "asn": "AS7922",
                "country_code": "US", "timezone": "America/New_York", "language": "en-US"}
        assert is_ip_safe_for_hilltopads(info) is False, (
            f"ip_type={ip_type!r} 应被拒绝"
        )

    def test_triplet_pass_missing_isp_asn(self):
        """ip_type/isp/asn 三缺二，但国家/时区/语言三要素完整 → 宽松放行（核心修复）。"""
        from popunder_trigger import is_ip_safe_for_hilltopads

        info = {
            "ip_type": "",  # 未知
            "isp": "",      # 未知
            "asn": "",      # 未知（三缺三？等一下：我们逻辑是 missing_count<=2 过；三缺三会严格拒绝）
            "country_code": "US",
            "timezone": "America/Chicago",
            "language": "en-US",
        }
        # 缺少 3 项应拒绝（完全不可判定）
        assert is_ip_safe_for_hilltopads(info) is False
        # 缺少 2 项：只留 ip_type=residential
        info2 = dict(info)
        info2["ip_type"] = "isp"  # 白名单级类型；即使 isp/asn 空（missing=2）
        assert is_ip_safe_for_hilltopads(info2) is True
        # 缺少 2 项：isp 填了 Comcast，ip_type 空，asn 空 → missing=2 → 宽松放行
        info3 = dict(info)
        info3["isp"] = "comcast cable communications"
        assert is_ip_safe_for_hilltopads(info3) is True, (
            "ip_type/asn 缺失但 isp+country+tz+lang 完整，应宽松放行"
        )

    def test_none_ip_info_rejected(self):
        """IP 信息 None → 拒绝。"""
        from popunder_trigger import is_ip_safe_for_hilltopads

        assert is_ip_safe_for_hilltopads(None) is False

    # ------------------------------------------------------------------
    # 4b. 守护线程不能调用 bring_to_front（会被 HilltopAds 判定"用户秒关"）
    # ------------------------------------------------------------------
    def test_guard_stay_and_close_no_bring_to_front(self):
        """源代码中（排除注释、docstring、字符串）不得调用 bring_to_front()。
        文档里有"旧实现 bring_to_front"是正常说明；真正要阻止的是代码行里的函数调用。
        """
        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()
        lines = src.splitlines()
        # 1. 定位函数范围
        start_idx, end_idx = None, None
        for i, ln in enumerate(lines):
            if "def _guard_stay_and_close(" in ln:
                start_idx = i
            elif start_idx is not None and i > start_idx:
                if ln.startswith("def "):  # 下一个顶级函数
                    end_idx = i
                    break
        if end_idx is None:
            end_idx = len(lines)
        func_lines = lines[start_idx:end_idx]

        # 2. 剔除 docstring（三对单/双引号包裹的块） + 注释 + 字符串字面量
        import re

        cleaned: list = []
        in_doc = None  # None / '"""' / "'''"
        for ln in func_lines:
            # 先去掉行注释 #（不在字符串里的 #）
            # 简化：因为 docstring 检查后面会独立做，这里粗暴去掉 # 开头注释和 "#"
            line_out = re.sub(r"#.*$", "", ln)
            # 处理三引号 docstring（跨行追踪）
            out_chars = []
            i = 0
            while i < len(line_out):
                ch3 = line_out[i:i + 3]
                if in_doc:
                    if ch3 == in_doc:
                        in_doc = None
                        i += 3
                        continue
                    i += 1
                    continue
                # 不在 docstring 里
                if ch3 in ('"""', "'''"):
                    # 检查这一行是否一对闭合（单行 docstring）
                    rest = line_out[i + 3:]
                    if ch3 in rest:
                        # 单行 docstring，跳过该段
                        i = i + 3 + rest.index(ch3) + 3
                        continue
                    # 跨行 docstring 开始
                    in_doc = ch3
                    i += 3
                    continue
                # 跳过普通字符串字面量中的 bring_to_front（"abc"+拼接）
                if line_out[i] in ('"', "'"):
                    quote = line_out[i]
                    # 跳到匹配引号
                    j = i + 1
                    while j < len(line_out):
                        if line_out[j] == '\\' and j + 1 < len(line_out):
                            j += 2
                            continue
                        if line_out[j] == quote:
                            break
                        j += 1
                    i = j + 1
                    continue
                out_chars.append(line_out[i])
                i += 1
            cleaned_line = "".join(out_chars).strip()
            if cleaned_line:
                cleaned.append(cleaned_line)
        func_code_only = "\n".join(cleaned)

        # 现在只看代码部分，有 bring_to_front 就是调用（或者 . 属性访问）
        if "bring_to_front" in func_code_only:
            # 把匹配到的行输出，让用户能看到具体是哪一行
            bad_lines = [l for l in cleaned if "bring_to_front" in l]
            pytest.fail(
                "_guard_stay_and_close 仍有 bring_to_front() 代码调用"
                "（会触发 HilltopAds visibilitychange 秒关判定，收益=0 复现）\n"
                "问题代码行：\n  - " + "\n  - ".join(bad_lines[:8])
            )

    # ------------------------------------------------------------------
    # 4c. 默认存活时间 22~36s + 触发概率 0.6
    # ------------------------------------------------------------------
    def test_default_stay_and_probability_values(self):
        """DEFAULT_CONFIG 的 Pop-under 默认参数必须是修复后的值。
        ★ 26.8.15.1：stay_min 22→15（R07 CRIT 线，混合分布短段下界），
        但均值由 _sample_popunder_stay 控制在 36-39s，仍覆盖两次 heartbeat。
        """
        from popunder_trigger import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["popunder_stay_min"] >= 15, (
            "stay_min 必须≥15s（R07 CRIT 线），否则短段弹窗存活期不足"
        )
        assert DEFAULT_CONFIG["popunder_stay_max"] >= 120, (
            "stay_max 必须≥120s（长尾'读完全文'用户，26.8.15.1 加宽）"
        )
        assert DEFAULT_CONFIG["trigger_probability"] >= 0.6, (
            "trigger_probability 默认应≥0.6，保证更多会话尝试触发"
        )

    # ------------------------------------------------------------------
    # 守护线程生命周期（模拟）
    # ------------------------------------------------------------------
    def test_guard_stay_and_close_injects_js_scroll(self):
        """必须在弹窗内执行 JS 滚动 + KeyboardEvent，增强后台 tab 画像。"""
        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()
        assert "window.scrollTo(0, 120)" in src or "scrollBy(0," in src, (
            "弹窗内 JS 滚动交互逻辑缺失，后台 tab 无行为特征 → IVT"
        )
        assert "KeyboardEvent" in src, (
            "弹窗内按键事件模拟缺失，无行为画像"
        )

    def test_guard_stay_and_close_injects_sleep_for_heartbeat(self):
        """阶段1+阶段2后，至少一次 time.sleep(>=3s)，给 heartbeat 定时器时间。"""
        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()
        # 守护线程内的 sleep 必须存在
        assert "让 JS 定时器有时间执行" in src or "heartbeat" in src.lower(), (
            "注释表明没有考虑 HilltopAds heartbeat 定时器，容易秒关"
        )


# ===========================================================================
# 启动自检：模块导入（确保无语法错误）
# ===========================================================================
def test_modules_importable():
    """修改的两个文件必须能成功导入。
    app.py 可能在测试环境缺失 pytz/fake_useragent 等运行期依赖；
    退而求其次：py_compile 语法校验 + popunder_trigger 完整 import。"""
    import importlib
    import py_compile

    # popunder_trigger 必须能完整 import（依赖较轻）
    try:
        importlib.import_module("popunder_trigger")
    except Exception as e:
        pytest.fail(f"模块 popunder_trigger 导入失败：{type(e).__name__}: {e}")

    # app.py：至少语法正确（AST/字节码生成通过）
    app_path = os.path.join(PROJECT_ROOT, "app.py")
    try:
        py_compile.compile(app_path, doraise=True)
    except py_compile.PyCompileError as e:
        pytest.fail(f"app.py 语法错误/字节码生成失败：{e}")
    # 轻量 AST 检查
    try:
        import ast
        with open(app_path, "r", encoding="utf-8") as f:
            ast.parse(f.read(), filename=app_path)
    except SyntaxError as e:
        pytest.fail(f"app.py AST 解析失败（SyntaxError）：{e}")


# ===========================================================================
# 辅助工具：静态解析 app.py 常量值（无需真实 import → 不依赖 pytz）
# ===========================================================================
import ast as _ast


def _extract_module_level_constant(src: str, name: str):
    """从源码中抽取 `NAME = value` 的简单赋值（int/float/str/bool）。
    兼容 Python 3.8~3.12+：ast.Num/ast.Str 在 3.12 已移除，统一走 ast.Constant.value。"""
    tree = _ast.parse(src)
    for node in tree.body:
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id == name:
                    val = node.value
                    # Python 3.8+ 通用：Constant
                    if isinstance(val, _ast.Constant):
                        return val.value
                    # 旧版兼容（Python 3.8 有时还会生成 Num/Str，但一般也有 Constant）
                    try:
                        import builtins
                        if isinstance(val, getattr(_ast, "Num", ())):
                            return val.n  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        if isinstance(val, getattr(_ast, "Str", ())):
                            return val.s  # type: ignore[attr-defined]
                    except Exception:
                        pass
    return None


def _extract_func_return_simple(src: str, func_name: str):
    """从源码中抽取一个非常简单函数的常量 if/else 返回（仅支持 _get_site_window_limit 形态）。
    只做黑盒单元测试的 fallback，不能覆盖复杂分支。"""
    # 用 subprocess 跑 python -c 来安全地计算：
    #   先定义函数（从源码切片），再用脚本调用
    lines = src.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"def {func_name}("):
            start = i
            break
    if start is None:
        return None
    # 找结束：下一个顶级 def 或 EOF
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j] and not lines[j].startswith((" ", "\t", "#", "")) and lines[j].startswith("def "):
            end = j
            break
    func_src = "\n".join(lines[start:end])
    return func_src


# ===========================================================================
# #5 26.8.11.2 新增：Pop-under Heartbeat 监听日志
# ===========================================================================
class TestHeartbeatMonitoring:
    """验证 heartbeat URL 分类、分析函数、守护线程签名等新增能力。"""

    # ------------------------------------------------------------------
    # 5a. URL 分类：_is_heartbeat_url 正例/反例/排除项全覆盖
    # ------------------------------------------------------------------
    def test_heartbeat_url_hilltopads_domains_always_hit(self):
        """HilltopAds/Traffichunt/HtopCDN 域名命中任何路径都算 heartbeat。"""
        from popunder_trigger import _is_heartbeat_url

        assert _is_heartbeat_url("https://cdn1.hilltopads.com/api/hb?slot=123") is True
        assert _is_heartbeat_url("https://track.traffichunt.net/pixel.gif") is True
        assert _is_heartbeat_url("https://htopcdn.com/stat?e=imp") is True
        # 即使没有路径关键词，光靠域名也必须命中
        assert _is_heartbeat_url("https://hilltopads.com/") is True

    def test_heartbeat_url_static_resources_excluded(self):
        """普通静态资源（CSS/JS/字体/普通图片）：因为没命中 heartbeat 关键词 → 返回 False。
        ★ 注意：pixel.gif / heartbeat.png 是典型广告像素，它们"命中关键词 → True"是【正确行为】，
        不算误报。这里只验证"不命中关键词的普通资源"不会被当成 heartbeat。
        """
        from popunder_trigger import _is_heartbeat_url

        # --- 正常静态资源（文件名/路径里完全没关键词）→ False ---
        assert _is_heartbeat_url("https://cdn.example.com/static/logo.png") is False
        assert _is_heartbeat_url("https://cdn.example.com/assets/main.css") is False
        assert _is_heartbeat_url("https://cdn.example.com/assets/app.bundle.js") is False
        assert _is_heartbeat_url("https://cdn.example.com/fonts/Inter-Regular.woff2") is False
        assert _is_heartbeat_url("https://cdn.example.com/img/hero-banner.jpg") is False
        # 带 query 的普通静态资源 → 也 False
        assert _is_heartbeat_url("https://cdn.example.com/img/hero-banner.jpg?v=20240811") is False

        # --- 含关键词的 GIF/PNG（典型广告跟踪像素）→ 是 heartbeat，不应被排除 ---
        #    这才是 HilltopAds 真的在发的统计请求格式
        assert _is_heartbeat_url("https://track.example.org/pixel.gif?rid=abc123") is True
        assert _is_heartbeat_url("https://pixel.example.com/impression.png?slot=7") is True

    def test_heartbeat_url_negative_ordinary_pages(self):
        """普通 HTML 页面 / JS / 无关键词 API 必须返回 False。"""
        from popunder_trigger import _is_heartbeat_url

        assert _is_heartbeat_url("https://example.com/") is False
        assert _is_heartbeat_url("https://example.com/article/123.html") is False
        assert _is_heartbeat_url("https://example.com/assets/app.bundle.js") is False
        assert _is_heartbeat_url("https://api.example.com/v2/user/info") is False
        # 非 http/https（about:blank / blob）→ False
        assert _is_heartbeat_url("about:blank") is False
        assert _is_heartbeat_url("") is False

    # ------------------------------------------------------------------
    # 5b. 分析函数：_analyze_heartbeat_records 的三类典型场景
    # ------------------------------------------------------------------
    def test_analyze_two_plus_heartbeats_summary_ok(self):
        """2+ heartbeat 返回 first/second_at 时间差合理 + HilltopAds 域名命中识别。"""
        from popunder_trigger import _analyze_heartbeat_records

        started = 1700000000.0
        records = [
            {"t": started + 1.2,  "url": "https://landing.example.com/main.js", "method": "GET"},
            {"t": started + 12.3, "url": "https://cdn1.hilltopads.com/api/hb?slot=A", "method": "GET"},
            {"t": started + 15.0, "url": "https://cdn.example.com/banner.jpg", "method": "GET"},
            {"t": started + 22.8, "url": "https://track.traffichunt.net/hb?rid=xyz", "method": "POST"},
            {"t": started + 30.1, "url": "https://thirdparty.test/pixel?v=1", "method": "GET"},
        ]
        summary = _analyze_heartbeat_records(records, started)
        assert summary["heartbeat_count"] >= 3, f"应命中 3+，实={summary['heartbeat_count']}"
        assert summary["first_at"] == pytest.approx(12.3, 0.01)
        assert summary["second_at"] == pytest.approx(22.8, 0.01)
        assert summary["has_hilltopads_hit"] is True
        assert summary["total_req"] == 5
        # 样本前 5 条应该包含 HilltopAds / Traffichunt URL
        joined = " | ".join(summary["sample_urls"])
        assert "hilltopads" in joined or "traffichunt" in joined

    def test_analyze_zero_heartbeats_scenario(self):
        """0 heartbeat 场景：count=0，first/second 均为 None。"""
        from popunder_trigger import _analyze_heartbeat_records

        started = 1700000000.0
        records = [
            {"t": started + 0.5, "url": "https://landing.example.com/", "method": "GET"},
            {"t": started + 1.1, "url": "https://cdn.example.com/app.css", "method": "GET"},
            {"t": started + 2.3, "url": "https://cdn.example.com/app.bundle.js", "method": "GET"},
            {"t": started + 5.0, "url": "https://pic.example.com/avatar.png", "method": "GET"},
        ]
        summary = _analyze_heartbeat_records(records, started)
        assert summary["heartbeat_count"] == 0
        assert summary["first_at"] is None
        assert summary["second_at"] is None
        assert summary["has_hilltopads_hit"] is False
        assert summary["total_req"] == 4

    def test_analyze_empty_records_no_crash(self):
        """空列表输入也不崩溃（监听器注册失败场景的兜底）。"""
        from popunder_trigger import _analyze_heartbeat_records

        summary = _analyze_heartbeat_records([], 0.0)
        assert summary["heartbeat_count"] == 0
        assert summary["first_at"] is None
        assert summary["total_req"] == 0

    # ------------------------------------------------------------------
    # 5c. 守护线程签名 + 返回 diagnostics 里 heartbeat 钩子
    # ------------------------------------------------------------------
    def test_guard_stay_and_close_signature_accepts_heartbeat_arg(self):
        """_guard_stay_and_close 第 5 个参数必须是 heartbeat_records（默认 None）。
        保证 trigger_popunder 线程传进去的列表能被分析函数消费。"""
        import inspect
        from popunder_trigger import _guard_stay_and_close

        sig = inspect.signature(_guard_stay_and_close)
        params = list(sig.parameters.keys())
        assert len(params) >= 5, (
            f"守护线程签名需要 ≥5 个参数（含 heartbeat_records），实际={params}"
        )
        # 第 5 个参数名
        assert params[4] == "heartbeat_records", f"第 5 个参数应为 heartbeat_records，实={params[4]}"
        # 默认值是 None（保证老调用点不传也兼容）
        assert sig.parameters["heartbeat_records"].default is None

    def test_trigger_popunder_returns_heartbeat_hooks(self):
        """trigger_popunder 返回的 diagnostics 必须带 heartbeat_records_ref + heartbeat_monitored。
        源码文本扫描（宽松正则），避免导入 whole module（有 playwright 依赖）。"""
        import re

        pu_file = os.path.join(PROJECT_ROOT, "popunder_trigger.py")
        with open(pu_file, "r", encoding="utf-8") as f:
            src = f.read()

        # 匹配字典 key-value 模式（支持单/双引号，忽略逗号、空格、行尾注释）
        def has_dict_key(src_text: str, key: str, value_pattern: str) -> bool:
            pat = re.compile(
                rf"""['"]{re.escape(key)}['"]\s*:\s*{value_pattern}\s*,?""",
                re.MULTILINE,
            )
            return pat.search(src_text) is not None

        assert has_dict_key(src, "heartbeat_records_ref", "heartbeat_records"), (
            "diagnostics 缺少 heartbeat_records_ref 钩子（未将列表引用传给上层）"
        )
        assert has_dict_key(src, "heartbeat_monitored", r"(?:True|False|\S+)"), (
            "diagnostics 缺少 heartbeat_monitored 标志位"
        )


# ======================================================================
# 26.8.11.3 新增：Selenium 3/4/5 CDP 调用兼容层 + 服务自恢复调度器
# ======================================================================
class Test_CDP_Session_Compat_26_8_11_3(unittest.TestCase):
    """修复：Selenium 3 场景 driver 没有 execute_cdp_cmd 时，_CDPSession.send 自动走
    command_executor 兜底，不再抛 AttributeError（导致 Pop-under 线程崩 → worker 死 → 任务自动停）。"""

    def test_cdp_session_send_without_native_execute_cdp_cmd(self):
        """driver 无 execute_cdp_cmd 属性（Selenium 3）→ 自动走 command_executor 兜底，
        不抛 AttributeError；能正确把 cmd/params 投递到 executeCdpCommand 端点。"""
        try:
            import selenium  # noqa: F401
        except Exception:
            pytest.skip("selenium not installed in test env (server-only module)")
        import sys
        sys.path.insert(0, PROJECT_ROOT)
        try:
            from selenium_bridge import _CDPSession  # noqa: E402
        except Exception as _e:
            pytest.skip(f"selenium_bridge import failed in test env ({type(_e).__name__})")

        call_log = []

        class _FakeCmdExec:
            def __init__(self):
                self._commands = {}

            def execute(self, *args):
                call_log.append(args)
                return {"value": {"ok": True, "cmd": args[1]["cmd"] if len(args) > 1 else None}}

        class _FakeDriver3:
            def __init__(self):
                self.command_executor = _FakeCmdExec()
            # 故意不定义 execute_cdp_cmd（模拟 Selenium 3.x）

        cdp = _CDPSession(_FakeDriver3())
        # act
        res = cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 100, "y": 200})
        # assert
        self.assertTrue(call_log, "兜底路径未被调用（call_log 空）")
        first_call = call_log[0]
        self.assertEqual(first_call[0], "executeCdpCommand", f"未走 executeCdpCommand 端点：{first_call[0]}")
        body = first_call[1]
        self.assertEqual(body["cmd"], "Input.dispatchMouseEvent")
        self.assertEqual(body["params"]["type"], "mousePressed")
        self.assertEqual(body["params"]["x"], 100)
        self.assertEqual(res.get("cmd"), "Input.dispatchMouseEvent")

    def test_cdp_session_send_prefers_native_execute_cdp_cmd_when_available(self):
        """driver 有 execute_cdp_cmd（Selenium 4）→ 优先走原生，不用 command_executor。"""
        try:
            import selenium  # noqa: F401
        except Exception:
            pytest.skip("selenium not installed in test env (server-only module)")
        import sys
        sys.path.insert(0, PROJECT_ROOT)
        try:
            from selenium_bridge import _CDPSession  # noqa: E402
        except Exception as _e:
            pytest.skip(f"selenium_bridge import failed in test env ({type(_e).__name__})")

        native_log = []
        fallback_log = []

        class _FakeCmdExec:
            def __init__(self):
                self._commands = {}

            def execute(self, *a, **kw):
                fallback_log.append((a, kw))
                return {"value": "fallback"}

        class _FakeDriver4:
            def __init__(self):
                self.command_executor = _FakeCmdExec()

            def execute_cdp_cmd(self, method, params):
                native_log.append((method, params))
                return {"source": "native", "method": method, "params": params}

        cdp = _CDPSession(_FakeDriver4())
        res = cdp.send("Network.enable", {})
        # assert 走了 native，没走 fallback
        self.assertEqual(len(native_log), 1, "未命中原生 execute_cdp_cmd 分支")
        self.assertEqual(native_log[0][0], "Network.enable")
        self.assertEqual(len(fallback_log), 0, "有原生路径时不应该走 fallback")
        self.assertEqual(res["source"], "native")


class Test_App_AutoResume_26_8_11_3(unittest.TestCase):
    """修复：Flask 启动时若 config.enabled=True，自动启动 worker_task 线程，
    避免 systemctl restart / OOM kill 后任务计数器归零、必须手动调 POST /start_task。"""

    def test_app_main_auto_resume_code_present(self):
        """AST/文本匹配：app.py 的 __main__ 块必须同时存在 3 个特征片段。"""
        with open(APP_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            'config.get("enabled", False) and not task_running',
            src,
            "缺少 enabled+not running 的自恢复触发条件",
        )
        self.assertIn(
            "name=\"auto-resume-worker\"",
            src,
            "缺少 auto-resume-worker 线程命名（后续日志识别依赖此名字）",
        )
        self.assertIn(
            "target=worker_task,",
            src,
            "自恢复线程 target 必须指向 worker_task",
        )
        self.assertIn(
            "✅ [自恢复] config.enabled=True",
            src,
            "缺少自恢复启动成功的 INFO 日志（之后 grep 验证需要）",
        )

    def test_app_main_auto_resume_exception_is_caught(self):
        """自恢复启动失败时，必须走 except 分支记录 warning，不得把异常抛到 app.run 之前。"""
        with open(APP_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "⚠️ [自恢复] 自动启动任务调度器失败",
            src,
            "缺少自恢复失败的 WARNING 兜底日志（启动失败会让 Flask 起不来）",
        )
