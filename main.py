#!/usr/bin/env python3
"""Entry point for Multi-Source Raw Log UDP Sender (GUI & CLI)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 若使用者直接執行 `./main.py` 或 `python3 main.py`（使用到系統預設 Python），
# 自動切換至專案內建的 `.venv/bin/python` 執行，避免找不到 PySide6 套件。
_PROJECT_DIR = Path(__file__).resolve().parent
_VENV_DIR = (_PROJECT_DIR / ".venv").resolve()
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
if _VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != _VENV_DIR:
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


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
