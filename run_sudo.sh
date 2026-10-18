#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. 若尚未建立 .venv，先初始化虛擬環境（若透過 sudo 呼叫，優先以 SUDO_USER 建立以避免檔案被 root 鎖定）
if [ ! -f "$SCRIPT_DIR/.venv/bin/python" ] || ! "$SCRIPT_DIR/.venv/bin/python" -c "import PySide6" 2>/dev/null; then
    echo "🚀 首次執行：正在為您自動初始化虛擬環境與安裝依賴..."
    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
        sudo -u "$SUDO_USER" bash "$SCRIPT_DIR/run.sh" --help >/dev/null
    else
        bash "$SCRIPT_DIR/run.sh" --help >/dev/null
    fi
fi

# 2. 以 sudo / root 權限啟動主程式
echo "🔒 正在以管理員權限 (sudo) 啟動 Multi-Source Raw Log UDP 發送模擬器..."
if [ "$EUID" -eq 0 ]; then
    exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@"
else
    exec sudo "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@"
fi
