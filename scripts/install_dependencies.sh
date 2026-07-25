#!/usr/bin/env bash
# install_dependencies.sh — 安装所有真实实验依赖
# 用法：bash scripts/install_dependencies.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "========================================"
echo " FederatedSkill — 安装依赖"
echo "========================================"
echo "项目根目录: $PROJECT_ROOT"

# 检查 Python
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "[错误] 未找到 python/python3，请先安装 Python >= 3.10"
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)
echo "Python: $($PYTHON --version)"

# 安装核心依赖
echo ""
echo "[1/2] 安装核心依赖 (requirements.txt)..."
if [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    $PYTHON -m pip install -r "$PROJECT_ROOT/requirements.txt" --quiet
    echo "  ✓ requirements.txt 安装完成"
fi

# 安装真实实验依赖
echo ""
echo "[2/2] 安装真实实验依赖 (requirements-real.txt)..."
if [ -f "$PROJECT_ROOT/requirements-real.txt" ]; then
    $PYTHON -m pip install -r "$PROJECT_ROOT/requirements-real.txt" --quiet
    echo "  ✓ requirements-real.txt 安装完成"
else
    echo "  [警告] requirements-real.txt 不存在，跳过"
fi

echo ""
echo "========================================"
echo " 依赖安装完成！"
echo " 下一步: 配置 API Key"
echo "   cp .env.example .env"
echo "   # 编辑 .env 填入 DASHSCOPE_KEY / MOONSHOT_KEY"
echo " 然后运行: python scripts/preflight_check.py"
echo "========================================"
