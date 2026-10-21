"""Central thread-safe TaskManager coordinating workers, IP aliases, and local UDP receiver."""

from __future__ import annotations

import collections
import copy
from datetime import datetime
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from core.ip_alias_manager import IpAliasManager
from core.models import (
    GlobalConfig,
    LogTaskConfig,
    ProjectProfile,
    count_valid_lines,
    validate_ipv4,
)
from core.packet_sender import has_raw_socket_privilege
from core.worker import LocalUdpReceiverThread, LogTaskWorker


class TaskManager:
    """
    Coordinates global configuration, multi-task workers, execution logs,
    network interface IP alias mounting, and the local test UDP receiver.
    Thread-safe and independent of any GUI framework.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.lock = threading.RLock()
        self.alias_manager = IpAliasManager()

        self.global_config = GlobalConfig()
        self.tasks: List[LogTaskConfig] = []
        self.task_states: Dict[int, Dict[str, Any]] = {}
        self.workers: Dict[int, LogTaskWorker] = {}
        self.is_paused: bool = False

        self.logs: collections.deque[str] = collections.deque(maxlen=1000)
        self.receiver_thread: Optional[LocalUdpReceiverThread] = None
        self.receiver_logs: collections.deque[Dict[str, Any]] = collections.deque(maxlen=1000)
        self.receiver_status: Dict[str, Any] = {
            "running": False,
            "port": 514 if has_raw_socket_privilege() else 5514,
            "message": "未啟動",
        }
        self.received_count: int = 0

        self._load_default_profile()

    def _append_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        with self.lock:
            self.logs.append(entry)

    def _load_default_profile(self) -> None:
        default_file = self.project_dir / "default_profile.json"
        if default_file.is_file():
            try:
                profile = ProjectProfile.load_from_file(default_file, base_dir=self.project_dir)
                self.global_config = profile.global_config
                self.tasks = profile.tasks
                self._init_task_states()
                self._append_log(
                    f"已載入預設設定檔 default_profile.json（包含 {len(self.tasks)} 個任務）"
                )
            except Exception as exc:
                self._append_log(f"載入 default_profile.json 發生錯誤: {exc}")

    def _init_task_states(self) -> None:
        with self.lock:
            self.task_states.clear()
            for idx, task in enumerate(self.tasks):
                lines = count_valid_lines(task.file_path)
                self.task_states[idx] = {
                    "sent_in_round": 0,
                    "total_lines": lines,
                    "total_sent": 0,
                    "current_round": 0,
                    "total_rounds": task.get_effective_repeat(self.global_config),
                    "progress_pct": 0,
                    "status": "就緒 (Ready)",
                    "is_error": False,
                    "is_running": False,
                }

    def update_config_and_tasks(
        self, global_cfg_dict: Dict[str, Any], tasks_list: List[Dict[str, Any]]
    ) -> None:
        """Update global config and task list from Web API payload."""
        with self.lock:
            if self.workers:
                raise RuntimeError("任務執行中，無法修改設定！請先停止任務。")

            self.global_config = GlobalConfig(
                target_ip=str(global_cfg_dict.get("target_ip", "127.0.0.1")).strip(),
                default_port=int(global_cfg_dict.get("default_port", 514)),
                default_repeat_count=int(global_cfg_dict.get("default_repeat_count", 1)),
                infinite_loop=bool(global_cfg_dict.get("infinite_loop", False)),
                loop_interval_sec=float(global_cfg_dict.get("loop_interval_sec", 1.0)),
                default_eps=int(global_cfg_dict.get("default_eps", 100)),
                send_mode=str(global_cfg_dict.get("send_mode", "auto")),
                bind_interface=str(global_cfg_dict.get("bind_interface", "")),
                auto_cleanup_aliases=bool(global_cfg_dict.get("auto_cleanup_aliases", True)),
                prepend_syslog_header=bool(global_cfg_dict.get("prepend_syslog_header", False)),
            )

            new_tasks: List[LogTaskConfig] = []
            for t_data in tasks_list:
                fpath = str(t_data.get("file_path", "")).strip()
                if not os.path.isabs(fpath):
                    resolved = (self.project_dir / fpath).resolve()
                    if resolved.exists():
                        fpath = str(resolved)

                target_port = t_data.get("target_port")
                if target_port is not None and str(target_port).isdigit() and int(target_port) > 0:
                    target_port = int(target_port)
                else:
                    target_port = None

                repeat_count = t_data.get("repeat_count")
                if repeat_count is not None and str(repeat_count).isdigit() and int(repeat_count) > 0:
                    repeat_count = int(repeat_count)
                else:
                    repeat_count = None

                eps = t_data.get("eps")
                if eps is not None and str(eps).isdigit() and int(eps) >= 0:
                    eps = int(eps)
                else:
                    eps = None

                new_tasks.append(
                    LogTaskConfig(
                        enabled=bool(t_data.get("enabled", True)),
                        system_name=str(t_data.get("system_name", "")).strip(),
                        file_path=fpath,
                        source_ip=str(t_data.get("source_ip", "192.168.1.101")).strip(),
                        target_port=target_port,
                        repeat_count=repeat_count,
                        eps=eps,
                    )
                )

            self.tasks = new_tasks
            self.task_states.clear()
            for idx, task in enumerate(self.tasks):
                lines = count_valid_lines(task.file_path)
                self.task_states[idx] = {
                    "sent_in_round": 0,
                    "total_lines": lines,
                    "total_sent": 0,
                    "current_round": 0,
                    "total_rounds": task.get_effective_repeat(self.global_config),
                    "progress_pct": 0,
                    "status": "就緒 (Ready)",
                    "is_error": False,
                    "is_running": False,
                }

    def start_tasks(self) -> Tuple[bool, str]:
        """Start all enabled tasks concurrently."""
        with self.lock:
            if self.workers:
                return False, "任務已在執行中！"

            if not validate_ipv4(self.global_config.target_ip):
                return False, f"全域目標 IP「{self.global_config.target_ip}」格式不正確！"

            enabled_tasks: List[Tuple[int, LogTaskConfig]] = []
            for idx, task in enumerate(self.tasks):
                if not task.enabled:
                    continue
                if not validate_ipv4(task.source_ip):
                    return False, f"任務 [{task.system_name or idx + 1}] 來源 IP「{task.source_ip}」格式錯誤！"
                if not os.path.isfile(task.file_path):
                    return False, f"任務 [{task.system_name or idx + 1}] 檔案不存在：{task.file_path}"
                enabled_tasks.append((idx, task))

            if not enabled_tasks:
                return False, "請至少勾選一個要發送的 Raw Log 任務！"

            # Check raw_spoof permissions
            if self.global_config.send_mode == "raw_spoof" and not has_raw_socket_privilege():
                return False, "您選擇了強制 Raw Socket 模式，但目前未以 sudo 啟動！請以 sudo ./run_sudo.sh 或 sudo python3 main.py 執行。"

            # If running as non-root (run.sh), resolve the chosen interface's real IP for transmission
            effective_source_ip: Optional[str] = None
            bound_iface_name: str = ""
            if not self.alias_manager.is_root():
                interfaces = self.alias_manager.list_interfaces()
                selected_name = self.global_config.bind_interface
                target_iface = next((i for i in interfaces if i.name == selected_name), None)
                if not target_iface:
                    target_iface = next((i for i in interfaces if not i.is_loopback and i.ipv4_addresses), None)
                if not target_iface and interfaces:
                    target_iface = interfaces[0]
                if target_iface and target_iface.ipv4_addresses:
                    effective_source_ip = target_iface.ipv4_addresses[0]
                    bound_iface_name = target_iface.name

            # Handle IP Alias auto-mounting (only active under root/sudo)
            if self.alias_manager.is_root() and self.global_config.send_mode in ("ip_alias_bind", "auto"):
                iface = self.global_config.bind_interface
                if not iface:
                    interfaces = self.alias_manager.list_interfaces()
                    iface = interfaces[0].name if interfaces else "en0"

                missing_ips = [
                    t.source_ip for _, t in enabled_tasks if not self.alias_manager.is_ip_local(t.source_ip)
                ]
                if missing_ips:
                    for ip_str in missing_ips:
                        ok, msg = self.alias_manager.add_ip_alias(iface, ip_str)
                        self._append_log(f"[自動掛載虛擬副 IP] {msg}")
                        if not ok and self.global_config.send_mode == "ip_alias_bind":
                            return False, f"無法掛載虛擬副 IP {ip_str}: {msg}"

            self.is_paused = False
            self.workers.clear()

            self._append_log(
                f"========== 啟動發送任務 (共 {len(enabled_tasks)} 個 Raw Log 檔案同時發送) =========="
            )
            if not self.alias_manager.is_root():
                if effective_source_ip:
                    self._append_log(
                        f"ℹ️ 提示：目前以一般使用者權限 (run.sh) 執行，已自動鎖定綁定網卡【{bound_iface_name}】之真實 IP ({effective_source_ip}) 進行 UDP 發送。"
                        "如需掛載多個虛擬副 IP (192.168.10.101~104) 模擬多來源，請以 sudo ./run_sudo.sh 啟動。"
                    )
                else:
                    self._append_log("ℹ️ 提示：目前以一般使用者權限 (run.sh) 執行，所有任務統一由本機真實 IP 發送。如需多來源 IP，請以 sudo ./run_sudo.sh 啟動。")

            for idx, task_cfg in enabled_tasks:
                state = self.task_states.setdefault(idx, {})
                state["sent_in_round"] = 0
                state["total_sent"] = 0
                state["progress_pct"] = 0
                state["status"] = "發送中 (Sending)"
                state["is_error"] = False
                state["is_running"] = True

                worker_task_cfg = task_cfg
                if not self.alias_manager.is_root() and effective_source_ip:
                    # Make a shallow copy of task_cfg with source_ip set to the real interface IP.
                    # This ensures workers bind and send via the real IP, while preserving
                    # original configured source_ip in self.tasks for sudo mode!
                    worker_task_cfg = copy.copy(task_cfg)
                    worker_task_cfg.source_ip = effective_source_ip

                worker = LogTaskWorker(
                    task_id=idx,
                    task_cfg=worker_task_cfg,
                    global_cfg=self.global_config,
                    on_progress=self._on_worker_progress,
                    on_status=self._on_worker_status,
                    on_log=self._append_log,
                    on_finished=self._on_worker_finished,
                )
                self.workers[idx] = worker
                worker.start()

        return True, f"已成功啟動 {len(enabled_tasks)} 個並發發送任務！"

    def _on_worker_progress(
        self,
        task_id: int,
        sent_in_round: int,
        total_lines: int,
        total_sent: int,
        current_round: int,
        total_rounds: int,
        progress_pct: int,
    ) -> None:
        with self.lock:
            state = self.task_states.setdefault(task_id, {})
            state["sent_in_round"] = sent_in_round
            state["total_lines"] = total_lines
            state["total_sent"] = total_sent
            state["current_round"] = current_round
            state["total_rounds"] = total_rounds
            state["progress_pct"] = progress_pct

    def _on_worker_status(self, task_id: int, status_text: str, is_error: bool) -> None:
        with self.lock:
            state = self.task_states.setdefault(task_id, {})
            state["status"] = status_text
            state["is_error"] = is_error

    def _on_worker_finished(self, task_id: int, success: bool, total_sent: int) -> None:
        with self.lock:
            state = self.task_states.setdefault(task_id, {})
            state["is_running"] = False
            state["total_sent"] = total_sent
            self.workers.pop(task_id, None)

            if not self.workers:
                total_all = sum(s.get("total_sent", 0) for s in self.task_states.values())
                self._append_log(f"========== 所有任務已結束（累計發送 {total_all} 筆 UDP 封包） ==========")

    def pause_tasks(self) -> bool:
        """Toggle pause/resume for all workers."""
        with self.lock:
            if not self.workers:
                return False
            self.is_paused = not self.is_paused
            for worker in self.workers.values():
                worker.set_paused(self.is_paused)
            if self.is_paused:
                self._append_log("⏸ 已暫停所有發送任務。")
            else:
                self._append_log("▶ 已恢復所有發送任務。")
            return self.is_paused

    def stop_tasks(self) -> None:
        """Stop all workers."""
        with self.lock:
            if not self.workers:
                return
            self._append_log("⏹ 正在停止所有發送任務...")
            for worker in list(self.workers.values()):
                worker.request_stop()

    def mount_all_aliases(self, iface: Optional[str] = None) -> Tuple[bool, str, List[str]]:
        """Mount IP aliases for all enabled tasks onto specified interface."""
        if not self.alias_manager.is_root():
            return False, "掛載網卡虛擬副 IP 需要 root / sudo 權限！請以 sudo 啟動程式。", []

        with self.lock:
            if not iface:
                iface = self.global_config.bind_interface
            if not iface:
                interfaces = self.alias_manager.list_interfaces()
                iface = interfaces[0].name if interfaces else "en0"

            results: List[str] = []
            count = 0
            for task in self.tasks:
                if not task.enabled or not validate_ipv4(task.source_ip):
                    continue
                ok, msg = self.alias_manager.add_ip_alias(iface, task.source_ip)
                self._append_log(f"[虛擬副 IP] {msg}")
                results.append(msg)
                if ok:
                    count += 1

            return True, f"已在網卡 [{iface}] 處理 {len(results)} 個虛擬副 IP", results

    def unmount_all_aliases(self, iface: Optional[str] = None) -> List[str]:
        """Unmount all mounted IP aliases and any secondary task source IPs on interface."""
        if not self.alias_manager.is_root():
            return ["卸載虛擬副 IP 需要 root / sudo 權限！請以 sudo 啟動程式。"]

        with self.lock:
            ifaces = self.alias_manager.list_interfaces()
            target_ifaces = []
            if iface:
                target_info = next((i for i in ifaces if i.name == iface), None)
                if target_info:
                    target_ifaces.append(target_info)
            if not target_ifaces:
                # Target all active non-loopback interfaces
                target_ifaces = [i for i in ifaces if not i.is_loopback]

            to_remove: Set[Tuple[str, str, int]] = set(self.alias_manager.mounted_aliases)

            for iface_info in target_ifaces:
                if len(iface_info.ipv4_addresses) > 1:
                    primary_ip = iface_info.ipv4_addresses[0]
                    secondary_ips = set(iface_info.ipv4_addresses[1:])
                    for sec_ip in secondary_ips:
                        if sec_ip != primary_ip and sec_ip != "127.0.0.1":
                            to_remove.add((iface_info.name, sec_ip, 24))

            if not to_remove:
                msg = f"網卡 [{iface or '全部'}] 上沒有需要清除的虛擬副 IP。"
                self._append_log(f"[虛擬副 IP] {msg}")
                return [msg]

            logs: List[str] = []
            for target_iface, ip_str, prefix_len in sorted(list(to_remove)):
                ok, msg = self.alias_manager.remove_ip_alias(target_iface, ip_str, prefix_len)
                logs.append(msg)
                self._append_log(f"[虛擬副 IP] {msg}")

            self.alias_manager.mounted_aliases.clear()
            return logs

    def toggle_receiver(self, port: int) -> Tuple[bool, str]:
        """Start or stop the local UDP test receiver."""
        with self.lock:
            if self.receiver_thread is not None and self.receiver_thread.is_alive():
                self.receiver_thread.stop_listening()
                self.receiver_thread.join(timeout=1.0)
                self.receiver_thread = None
                self.receiver_status = {"running": False, "port": port, "message": "本機 UDP 測試接收器已停止"}
                return True, "本機接收器已停止"

            def _on_pkt(src_ip: str, src_port: int, text: str) -> None:
                self.received_count += 1
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                with self.lock:
                    self.receiver_logs.append({
                        "id": self.received_count,
                        "time": ts,
                        "src_ip": src_ip,
                        "src_port": src_port,
                        "payload": text,
                    })

            def _on_stat(running: bool, msg: str) -> None:
                with self.lock:
                    self.receiver_status = {"running": running, "port": port, "message": msg}

            self.receiver_thread = LocalUdpReceiverThread(
                bind_ip="0.0.0.0",
                port=port,
                on_packet=_on_pkt,
                on_status=_on_stat,
            )
            self.receiver_thread.start()
            self.receiver_status = {"running": True, "port": port, "message": f"正在監聽 UDP 0.0.0.0:{port}"}
            return True, f"正在監聽 UDP 0.0.0.0:{port}"

    def clear_receiver_logs(self) -> None:
        with self.lock:
            self.received_count = 0
            self.receiver_logs.clear()

    def clear_logs(self) -> None:
        with self.lock:
            self.logs.clear()

    def get_state(self) -> Dict[str, Any]:
        """Return a complete JSON-serializable status snapshot for the Web UI."""
        with self.lock:
            interfaces = [
                {
                    "name": iface.name,
                    "display_label": iface.display_label,
                    "ipv4_addresses": iface.ipv4_addresses,
                    "is_loopback": iface.is_loopback,
                }
                for iface in self.alias_manager.list_interfaces()
            ]

            tasks_data: List[Dict[str, Any]] = []
            for idx, task in enumerate(self.tasks):
                state = self.task_states.get(idx, {})
                tasks_data.append({
                    "id": idx,
                    "enabled": task.enabled,
                    "system_name": task.system_name or Path(task.file_path).stem,
                    "file_path": task.file_path,
                    "source_ip": task.source_ip,
                    "target_port": task.target_port,
                    "effective_port": task.get_effective_port(self.global_config),
                    "repeat_count": task.repeat_count,
                    "effective_repeat": task.get_effective_repeat(self.global_config),
                    "eps": task.eps,
                    "effective_eps": task.get_effective_eps(self.global_config),
                    "sent_in_round": state.get("sent_in_round", 0),
                    "total_lines": state.get("total_lines", count_valid_lines(task.file_path)),
                    "total_sent": state.get("total_sent", 0),
                    "current_round": state.get("current_round", 0),
                    "total_rounds": state.get("total_rounds", 0),
                    "progress_pct": state.get("progress_pct", 0),
                    "status": state.get("status", "就緒 (Ready)"),
                    "is_error": state.get("is_error", False),
                    "is_running": state.get("is_running", False),
                })

            mounted = [f"{iface}:{ip}" for iface, ip, _ in sorted(list(self.alias_manager.mounted_aliases))]

            total_sent_all = sum(s.get("total_sent", 0) for s in self.task_states.values())

            return {
                "is_root": self.alias_manager.is_root(),
                "interfaces": interfaces,
                "global_config": {
                    "target_ip": self.global_config.target_ip,
                    "default_port": self.global_config.default_port,
                    "default_repeat_count": self.global_config.default_repeat_count,
                    "infinite_loop": self.global_config.infinite_loop,
                    "loop_interval_sec": self.global_config.loop_interval_sec,
                    "default_eps": self.global_config.default_eps,
                    "send_mode": self.global_config.send_mode,
                    "bind_interface": self.global_config.bind_interface,
                    "auto_cleanup_aliases": self.global_config.auto_cleanup_aliases,
                    "prepend_syslog_header": self.global_config.prepend_syslog_header,
                },
                "tasks": tasks_data,
                "is_running": bool(self.workers),
                "is_paused": self.is_paused,
                "active_workers": len(self.workers),
                "total_sent_packets": total_sent_all,
                "mounted_aliases": mounted,
                "logs": list(self.logs),
                "receiver": {
                    "running": self.receiver_status["running"],
                    "port": self.receiver_status["port"],
                    "message": self.receiver_status["message"],
                    "packets": list(self.receiver_logs),
                },
            }

    def shutdown(self) -> None:
        """Gracefully stop all workers, receiver, and cleanup aliases."""
        self.stop_tasks()
        if self.receiver_thread is not None and self.receiver_thread.is_alive():
            self.receiver_thread.stop_listening()
            self.receiver_thread.join(timeout=1.0)
        if self.alias_manager.is_root() and self.global_config.auto_cleanup_aliases:
            iface = self.global_config.bind_interface or None
            logs = self.unmount_all_aliases(iface)
            for log_msg in logs:
                if "已從網卡" in log_msg or "已卸載" in log_msg:
                    print(f"🧹 [虛擬副 IP] {log_msg}", flush=True)
