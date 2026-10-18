#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "正在以 sudo 權限啟動 Multi-Source Raw Log UDP 發送模擬器（支援 Raw Socket 偽造不同來源 IP）..."
sudo "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py" "$@"
