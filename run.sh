#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. 檢查系統 Python 3
if ! command -v python3 &>/dev/null; then
    echo "❌ 錯誤：找不到 python3 指令，請先安裝 Python 3.9+（建議 Python 3.10 ~ 3.12）"
    exit 1
fi

# 2. 檢查 Python 版本 (需 >= 3.9)
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
    CURRENT_PY=$(python3 --version 2>&1)
    echo "❌ 錯誤：PySide6 需要 Python 3.9 或更高版本（建議 Python 3.10 ~ 3.12）"
    echo "   目前系統 Python 版本為: $CURRENT_PY"
    exit 1
fi

# 3. 首次執行：若尚未建立 .venv 虛擬環境，自動初始化並安裝依賴
if [ ! -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "🚀 首次執行：正在為您建立獨立虛擬環境 (.venv)..."
    python3 -m venv "$SCRIPT_DIR/.venv"
    echo "📦 正在安裝必要套件 (PySide6)..."
    "$SCRIPT_DIR/.venv/bin/pip install" --upgrade pip >/dev/null 2>&1 || true
    "$SCRIPT_DIR/.venv/bin/pip install" -r "$SCRIPT_DIR/requirements.txt"
    echo "✅ 虛擬環境與套件安裝完成！"
fi

# 4. 確保 PySide6 已安裝（避免上次安裝中斷）
if ! "$SCRIPT_DIR/.venv/bin/python" -c "import PySide6" 2>/dev/null; then
    echo "📦 偵測到套件尚未完整安裝，正在修復安裝依賴..."
    "$SCRIPT_DIR/.venv/bin/pip install" -r "$SCRIPT_DIR/requirements.txt"
fi

# 5. 啟動主程式
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@"
