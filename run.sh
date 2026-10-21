#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 &>/dev/null; then
    echo "❌ 錯誤：找不到 python3 指令，請先安裝 Python 3"
    exit 1
fi

exec python3 "$SCRIPT_DIR/main.py" "$@"
