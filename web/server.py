"""Zero-dependency HTTP server with RESTful API and static file serving for Web UI.

Uses Python standard library http.server.ThreadingHTTPServer.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
import urllib.parse

from core.models import (
    ProjectProfile,
    count_valid_lines,
    generate_sequential_ips,
    preview_file_lines,
    validate_ipv4,
)
from core.task_manager import TaskManager


class AppRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for Web UI and REST API."""

    server_version = "LogSimulationServer/2.0"
    task_manager: TaskManager
    web_dir: Path
    project_dir: Path

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard HTTP access logs in terminal, keep console clean
        pass

    def send_error(
        self,
        code: int,
        message: Optional[str] = None,
        explain: Optional[str] = None,
    ) -> None:
        """Safe send_error preventing latin-1 header encoding errors when message has non-ASCII characters."""
        ascii_message = None
        if message is not None:
            try:
                message.encode("latin-1")
                ascii_message = message
            except UnicodeEncodeError:
                explain = explain or message
                ascii_message = None
        super().send_error(code, ascii_message, explain)

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"success": False, "error": message}, status=status)

    def _read_json_body(self) -> Dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            return {}
        raw = self.rfile.read(content_len)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"無效的 JSON 請求內容: {exc}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. API: /api/status
        if path == "/api/status":
            state = self.task_manager.get_state()
            self._send_json({"success": True, "data": state})
            return

        # 2. API: /api/preview
        if path == "/api/preview":
            file_path = query.get("file", [""])[0].strip()
            max_lines = int(query.get("lines", [100])[0])
            if not file_path:
                self._send_error_json("未指定檔案路徑 (file)")
                return
            if not os.path.isabs(file_path):
                file_path = str((self.project_dir / file_path).resolve())
            lines = preview_file_lines(file_path, max_lines=max_lines)
            self._send_json({"success": True, "file": file_path, "lines": lines, "count": len(lines)})
            return

        # 3. API: /api/fs/browse
        if path == "/api/fs/browse":
            target_dir = query.get("dir", [""])[0].strip()
            if not target_dir:
                target_dir = str(self.project_dir)
            target_path = Path(target_dir).resolve()
            if not target_path.exists() or not target_path.is_dir():
                target_path = self.project_dir

            subdirs = []
            log_files = []
            try:
                for entry in sorted(target_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if entry.name.startswith(".") and entry.name != "..":
                        continue
                    if entry.is_dir():
                        subdirs.append({
                            "name": entry.name,
                            "path": str(entry.resolve()),
                        })
                    elif entry.is_file():
                        ext = entry.suffix.lower()
                        if ext in (".log", ".txt", ".raw", ".csv", ".syslog", ".json") or "log" in entry.name.lower():
                            try:
                                size_bytes = entry.stat().st_size
                                if size_bytes < 1024 * 1024:
                                    size_str = f"{size_bytes / 1024:.1f} KB"
                                elif size_bytes < 1024 * 1024 * 1024:
                                    size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                                else:
                                    size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
                            except OSError:
                                size_str = "未知"
                            log_files.append({
                                "name": entry.name,
                                "path": str(entry.resolve()),
                                "size": size_str,
                            })
            except Exception as exc:
                self._send_error_json(f"無法讀取目錄內容: {exc}")
                return

            parent_dir = str(target_path.parent) if target_path.parent != target_path else None
            self._send_json({
                "success": True,
                "current_dir": str(target_path),
                "parent_dir": parent_dir,
                "subdirs": subdirs,
                "log_files": log_files,
                "is_project_root": (target_path == self.project_dir),
            })
            return

        # 4. Handle browser automatic requests (favicon / apple-touch-icon)
        if path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        # 5. Static file serving (Index.html and assets)
        if path in ("/", "/index.html", "/index.htm"):
            index_file = self.web_dir / "index.html"
            if index_file.is_file():
                self._serve_file(index_file)
                return

        # Fallback to serving files from web_dir
        rel_path = path.lstrip("/")
        file_candidate = (self.web_dir / rel_path).resolve()
        if str(file_candidate).startswith(str(self.web_dir)) and file_candidate.is_file():
            self._serve_file(file_candidate)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found", "找不到請求的頁面")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(str(exc))
            return

        # 1. API: /api/tasks/update
        if path == "/api/tasks/update":
            g_cfg = body.get("global_config", {})
            tasks = body.get("tasks", [])
            try:
                self.task_manager.update_config_and_tasks(g_cfg, tasks)
                self._send_json({"success": True, "message": "任務與全域設定已更新"})
            except Exception as exc:
                self._send_error_json(str(exc))
            return

        # 2. API: /api/tasks/start
        if path == "/api/tasks/start":
            g_cfg = body.get("global_config")
            tasks = body.get("tasks")
            if g_cfg is not None and tasks is not None:
                try:
                    self.task_manager.update_config_and_tasks(g_cfg, tasks)
                except Exception as exc:
                    self._send_error_json(str(exc))
                    return

            ok, msg = self.task_manager.start_tasks()
            if ok:
                self._send_json({"success": True, "message": msg})
            else:
                self._send_error_json(msg)
            return

        # 3. API: /api/tasks/pause
        if path == "/api/tasks/pause":
            is_paused = self.task_manager.pause_tasks()
            self._send_json({"success": True, "is_paused": is_paused})
            return

        # 4. API: /api/tasks/stop
        if path == "/api/tasks/stop":
            self.task_manager.stop_tasks()
            self._send_json({"success": True, "message": "已發出停止指令"})
            return

        # 5. API: /api/alias/mount
        if path == "/api/alias/mount":
            iface = body.get("interface")
            ok, msg, logs = self.task_manager.mount_all_aliases(iface)
            if ok:
                self._send_json({"success": True, "message": msg, "logs": logs})
            else:
                self._send_error_json(msg)
            return

        # 6. API: /api/alias/unmount
        if path == "/api/alias/unmount":
            iface = body.get("interface")
            logs = self.task_manager.unmount_all_aliases(iface)
            removed_count = sum(1 for m in logs if "已從網卡" in m or "已卸載" in m)
            if removed_count > 0:
                msg = f"已成功卸載 {removed_count} 個虛擬副 IP"
            else:
                msg = logs[0] if logs else "沒有虛擬副 IP 需要卸載"
            self._send_json({"success": True, "message": msg, "logs": logs, "count": removed_count})
            return

        # 7. API: /api/fs/scan_folder
        if path == "/api/fs/scan_folder":
            folder = body.get("folder", "sample_logs").strip()
            folder_path = Path(folder)
            if not folder_path.is_absolute():
                folder_path = (self.project_dir / folder).resolve()

            if not folder_path.is_dir():
                self._send_error_json(f"找不到資料夾: {folder}")
                return

            found_files = []
            for p in sorted(folder_path.iterdir()):
                if p.is_file() and (p.suffix.lower() in (".log", ".txt", ".raw", ".syslog") or "log" in p.name.lower()):
                    lines = count_valid_lines(str(p))
                    found_files.append({
                        "system_name": p.stem,
                        "file_path": str(p),
                        "lines": lines,
                        "size_bytes": p.stat().st_size,
                    })

            self._send_json({"success": True, "folder": str(folder_path), "files": found_files})
            return

        # 8. API: /api/profile/save
        if path == "/api/profile/save":
            file_name = body.get("file_name", "my_log_profile.json").strip()
            profile_path = Path(file_name)
            if not profile_path.is_absolute():
                profile_path = (self.project_dir / file_name).resolve()

            try:
                profile = ProjectProfile(
                    global_config=self.task_manager.global_config,
                    tasks=self.task_manager.tasks,
                )
                profile.save_to_file(profile_path)
                self.task_manager._append_log(f"設定檔已儲存至：{profile_path}")
                self._send_json({"success": True, "path": str(profile_path), "message": f"設定檔已成功儲存至 {profile_path.name}"})
            except Exception as exc:
                self._send_error_json(f"儲存設定檔失敗: {exc}")
            return

        # 9. API: /api/profile/load
        if path == "/api/profile/load":
            file_name = body.get("file_name", "default_profile.json").strip()
            profile_path = Path(file_name)
            if not profile_path.is_absolute():
                profile_path = (self.project_dir / file_name).resolve()

            if not profile_path.is_file():
                self._send_error_json(f"找不到設定檔：{profile_path}")
                return

            try:
                profile = ProjectProfile.load_from_file(profile_path, base_dir=self.project_dir)
                self.task_manager.global_config = profile.global_config
                self.task_manager.tasks = profile.tasks
                self.task_manager._init_task_states()
                self.task_manager._append_log(f"已成功載入設定檔：{profile_path.name}")
                self._send_json({"success": True, "message": f"已載入 {profile_path.name}", "state": self.task_manager.get_state()})
            except Exception as exc:
                self._send_error_json(f"讀取設定檔失敗: {exc}")
            return

        # 10. API: /api/receiver/toggle
        if path == "/api/receiver/toggle":
            port = int(body.get("port", 5514))
            ok, msg = self.task_manager.toggle_receiver(port)
            self._send_json({"success": ok, "message": msg, "running": self.task_manager.receiver_status["running"]})
            return

        # 11. API: /api/receiver/clear
        if path == "/api/receiver/clear":
            self.task_manager.clear_receiver_logs()
            self._send_json({"success": True, "message": "接收紀錄已清除"})
            return

        # 12. API: /api/logs/clear
        if path == "/api/logs/clear":
            self.task_manager.clear_logs()
            self._send_json({"success": True, "message": "日誌已清除"})
            return

        self._send_error_json("未知的 API 請求路徑", status=404)

    def _serve_file(self, file_path: Path) -> None:
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            mime, _ = mimetypes.guess_type(str(file_path))
            if not mime:
                mime = "text/html" if file_path.suffix == ".html" else "application/octet-stream"
            if mime.startswith("text/"):
                mime += "; charset=utf-8"

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal Server Error", f"無法讀取檔案: {exc}")


def create_server(host: str, port: int, project_dir: Path, task_manager: TaskManager) -> ThreadingHTTPServer:
    """Create and configure the ThreadingHTTPServer instance."""
    web_dir = project_dir / "web"

    class BoundHandler(AppRequestHandler):
        pass

    BoundHandler.task_manager = task_manager
    BoundHandler.web_dir = web_dir
    BoundHandler.project_dir = project_dir

    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    server.block_on_close = False
    return server
