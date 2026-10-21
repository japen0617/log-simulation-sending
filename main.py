#!/usr/bin/env python3
"""Entry point for Multi-Source Raw Log UDP Sender (v2.0 Zero-Dependency Web UI).

Pure Python 3 standard library (no external packages or pip install required).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from core.models import ProjectProfile
from core.packet_sender import has_raw_socket_privilege
from core.task_manager import TaskManager
from web.server import create_server


def get_local_primary_ip() -> str:
    """Best-effort guess for the primary LAN IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def open_browser_safely(url: str) -> None:
    """Open browser in a separate thread, taking into account Linux sudo SUDO_USER."""
    def _open() -> None:
        time.sleep(0.6)
        # On Linux when running as sudo, opening browser under root often fails or warns.
        # If SUDO_USER is present, run xdg-open as the original desktop user.
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sys.platform.startswith("linux") and sudo_user != "root":
            try:
                subprocess.Popen(
                    ["sudo", "-u", sudo_user, "xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-Source Raw Log UDP 發送模擬器 (v2.0 Web版 - 零依賴跨平台架構)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Web 伺服器監聽 IP (預設 0.0.0.0，允許同網段其他主機遠端連線)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web 伺服器監聽 Port (預設 8080)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="啟動時不要自動在桌面開啟預設瀏覽器",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="啟動時自動載入的 JSON Profile 設定檔路徑",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    task_manager = TaskManager(project_dir=project_dir)

    if args.profile:
        profile_path = Path(args.profile).resolve()
        if profile_path.is_file():
            try:
                profile = ProjectProfile.load_from_file(profile_path, base_dir=profile_path.parent)
                task_manager.global_config = profile.global_config
                task_manager.tasks = profile.tasks
                task_manager._init_task_states()
                print(f"✅ 已載入指定設定檔: {profile_path.name}")
            except Exception as exc:
                print(f"⚠️ 載入設定檔失敗: {exc}", file=sys.stderr)

    try:
        server = create_server(args.host, args.port, project_dir, task_manager)
    except OSError as exc:
        print(f"❌ 錯誤：無法在 {args.host}:{args.port} 啟動伺服器 ({exc})", file=sys.stderr)
        print("💡 請嘗試更換其他 Port，例如: python3 main.py --port 8081", file=sys.stderr)
        return 1

    local_ip = get_local_primary_ip()
    is_root = has_raw_socket_privilege()

    print("==================================================================")
    print("🚀 Multi-Source Raw Log UDP 發送模擬器 (v2.0 零依賴 Web 版)")
    print("==================================================================")
    print(f"📡 本機操作網址： http://127.0.0.1:{args.port}")
    if local_ip != "127.0.0.1":
        print(f"🌐 區網連線網址： http://{local_ip}:{args.port} (可從其他電腦遠端連線)")
    if is_root:
        print("🔒 執行權限：    ✅ Root / Sudo (支援網卡虛擬副 IP 與 Raw Socket)")
    else:
        print("ℹ️ 執行權限：    ⚠️ 一般使用者 (一般 UDP 模式，若需偽造 IP 請以 sudo 執行)")
    print("==================================================================")
    print("💡 提示：按 Ctrl+C 可停止服務（若有自動掛載副 IP 將自動清除還原）\n")

    # Open browser automatically unless requested not to
    if not args.no_browser:
        open_browser_safely(f"http://127.0.0.1:{args.port}")

    # Run HTTP server in a dedicated background daemon thread so the main thread
    # remains completely responsive to SIGINT (Ctrl+C) and KeyboardInterrupt.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="WebServer")
    server_thread.start()

    stop_event = threading.Event()

    def _shutdown_handler(signum=None, frame=None):
        if stop_event.is_set():
            # If user presses Ctrl+C repeatedly during cleanup, force terminate immediately
            print("\n⚠️ 偵測到多次中斷指令，立即強制退出...", flush=True)
            os._exit(1)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass

    print("\n正在安全關閉服務並清理資源...", flush=True)

    # Safety watchdog: guarantee process exits within 2.5 seconds
    def _safety_watchdog() -> None:
        time.sleep(3.5)
        print("⚠️ 資源釋放逾時，強制退出...", flush=True)
        os._exit(0)

    threading.Thread(target=_safety_watchdog, daemon=True, name="ShutdownWatchdog").start()

    try:
        task_manager.shutdown()
    except Exception as exc:
        print(f"⚠️ 清理任務時發生例外: {exc}", file=sys.stderr)

    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass

    print("✅ 服務已安全停止，感謝使用！", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
