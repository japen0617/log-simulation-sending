#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 &>/dev/null; then
    echo "❌ 錯誤：找不到 python3 指令，請先安裝 Python 3"
    exit 1
fi

echo "🔒 正在以管理員權限 (sudo) 啟動 Multi-Source Raw Log UDP 發送模擬器..."
if [ "$EUID" -eq 0 ]; then
    exec python3 "$SCRIPT_DIR/main.py" "$@"
else
    exec sudo python3 "$SCRIPT_DIR/main.py" "$@"
fi
