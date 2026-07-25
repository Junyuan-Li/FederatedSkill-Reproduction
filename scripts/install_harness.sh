#!/usr/bin/env bash
# install_harness.sh — 检查 Agent Harness CLI 工具是否已安装
#
# 本脚本**不会自动执行 npm install**，仅检查现有状态并打印安装命令。
# 原因：npm 全局安装需要特定网络权限，且版本要求因项目而异。
#
# 用法：
#   bash scripts/install_harness.sh
#
# 所需工具：
#   - claude-code   (@anthropic-ai/claude-code)  — Setting 1-3, Ablation
#   - qwen-code     (@qwen-code/qwen-code)        — Setting 4
#   - kimi-cli      (kimi)                        — Setting 3-4

set -e

echo "========================================"
echo " FederatedSkill — Agent Harness 检查"
echo "========================================"
echo ""

HAS_NODE=$(command -v node 2>/dev/null || true)
HAS_NPM=$(command -v npm 2>/dev/null || true)

if [ -z "$HAS_NODE" ] || [ -z "$HAS_NPM" ]; then
    echo "[警告] 未找到 node / npm"
    echo "  Agent Harness 需要 Node.js >= 18"
    echo "  安装: https://nodejs.org/"
    echo ""
fi

# ── claude-code ──────────────────────────────────────────
echo "[1/3] claude-code (@anthropic-ai/claude-code)"
if command -v claude &>/dev/null; then
    VER=$(claude --version 2>/dev/null | head -n1 || echo "版本未知")
    echo "  ✓ 已安装: $VER"
else
    echo "  ✗ 未找到"
    echo "  安装命令: npm install -g @anthropic-ai/claude-code"
    echo "  官方文档: https://docs.anthropic.com/claude-code"
fi
echo ""

# ── qwen-code ────────────────────────────────────────────
echo "[2/3] qwen-code (@qwen-code/qwen-code)"
if command -v qwen-code &>/dev/null; then
    VER=$(qwen-code --version 2>/dev/null | head -n1 || echo "版本未知")
    echo "  ✓ 已安装: $VER"
else
    echo "  ✗ 未找到"
    echo "  安装命令: npm install -g @qwen-code/qwen-code"
    echo "  官方文档: https://help.aliyun.com/qwen-code"
fi
echo ""

# ── kimi-cli ─────────────────────────────────────────────
echo "[3/3] kimi-cli"
if command -v kimi &>/dev/null; then
    VER=$(kimi --version 2>/dev/null | head -n1 || echo "版本未知")
    echo "  ✓ 已安装: $VER"
else
    echo "  ✗ 未找到"
    echo "  安装命令: pip install kimi-cli"
    echo "  或参考 Moonshot 官方文档"
fi
echo ""

echo "========================================"
echo " 安装完成后请再次运行本脚本验证"
echo " 然后运行: python scripts/preflight_check.py"
echo "========================================"
