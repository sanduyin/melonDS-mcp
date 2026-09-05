#!/usr/bin/env bash
# build.sh — 构建 libmelonds_mcp 并准备 Python 环境
#
# 用法：
#   ./mcp/scripts/build.sh [--no-python]
#
# 产物：build/libmelonds_mcp.dylib (macOS) / libmelonds_mcp.so (Linux)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$REPO_ROOT/build/mcp"

echo "=== 构建 libmelonds_mcp ==="
echo "仓库根目录: $REPO_ROOT"
echo "构建目录:   $BUILD_DIR"

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

cmake "$REPO_ROOT/mcp" \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_OGLRENDERER=OFF \
    -DENABLE_GDBSTUB=OFF

if command -v nproc >/dev/null 2>&1; then
    JOBS="$(nproc)"
elif [[ "$(uname)" == "Darwin" ]]; then
    JOBS="$(sysctl -n hw.ncpu)"
else
    JOBS=4
fi

cmake --build . -j"$JOBS"

echo ""
echo "=== 构建完成 ==="
ls -lh "$BUILD_DIR"/libmelonds_mcp.* 2>/dev/null || true

# ── Python 环境 ──
if [[ "${1:-}" != "--no-python" ]]; then
    echo ""
    echo "=== 检查 Python 依赖 ==="
    PYTHON="${PYTHON:-python3}"
    VENV_DIR="$REPO_ROOT/mcp/.venv"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    "$VENV_DIR/bin/python" -m pip install -r "$REPO_ROOT/mcp/python/requirements.txt"
fi
