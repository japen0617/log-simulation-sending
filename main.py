#!/usr/bin/env python3
"""Entry point for Multi-Source Raw Log UDP Sender (GUI & CLI)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    print("❌ 錯誤：PySide6 需要 Python 3.9 或更高版本（建議使用 Python 3.10 ~ 3.12）。", file=sys.stderr)
    print(f"   目前使用的 Python 版本為: {sys.version.split()[0]}", file=sys.stderr)
    sys.exit(1)

# 若使用者直接執行 `./main.py` 或 `python3 main.py`（使用到系統預設 Python），
# 自動切換至專案內建的 `.venv/bin/python` 執行，避免找不到 PySide6 套件。
_PROJECT_DIR = Path(__file__).resolve().parent
_VENV_DIR = (_PROJECT_DIR / ".venv").resolve()
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
if _VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != _VENV_DIR:
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow
except ImportError:
    print("❌ 錯誤：尚未安裝 GUI 依賴套件 PySide6，或尚未建立虛擬環境。", file=sys.stderr)
    print("💡 建議直接執行啟動腳本（首次執行會自動為您建立 .venv 與安裝套件）：", file=sys.stderr)
    print("   ./run.sh        (一般權限模式)", file=sys.stderr)
    print("   ./run_sudo.sh   (具備網卡 IP Alias 與 Raw Socket 偽造權限)", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-Source Raw Log UDP Sender (支援多檔案不同來源 IP 同時發送)"
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="啟動時自動載入的 JSON Profile 設定檔路徑",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    app = QApplication(sys.argv)
    app.setApplicationName("Multi-Source Raw Log UDP Sender")

    win = MainWindow(project_dir=project_dir)
    if args.profile:
        from core.models import ProjectProfile

        profile_path = Path(args.profile).resolve()
        if profile_path.is_file():
            profile = ProjectProfile.load_from_file(profile_path, base_dir=profile_path.parent)
            win._apply_profile_to_ui(profile)

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
