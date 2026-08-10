#!/usr/bin/env bash
# ============================================================
# 契约测试运行脚本
# 用法：./scripts/run_contract_tests.sh
# 作用：每次代码变动后运行契约测试，验证接口契约未被破坏
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "=========================================="
echo "  契约测试套件 - Contract Test Suite"
echo "  项目根目录: $PROJECT_ROOT"
echo "=========================================="
echo ""

# 检查 pytest 是否可用
if ! python3 -m pytest --version >/dev/null 2>&1; then
    echo "❌ 错误：未找到 pytest，请先安装：pip install pytest"
    exit 1
fi

# 运行契约测试
echo "▶  运行契约测试 (tests/test_contract.py)..."
echo ""

if python3 -m pytest tests/test_contract.py -q; then
    echo ""
    echo "✅ 所有契约测试通过"
    exit 0
else
    echo ""
    echo "❌ 契约测试失败，请检查上方错误信息"
    echo "   常见原因："
    echo "   - 函数签名被修改（参数名/数量变化）"
    echo "   - 返回结构字段被删除/重命名"
    echo "   - 模块导出函数被移除"
    echo "   修复后重新运行本脚本验证"
    exit 1
fi
