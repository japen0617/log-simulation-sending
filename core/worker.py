"""Concurrent worker threads for sending multiple Raw Log files and local UDP test listener.

100% pure Python standard library (Zero external dependencies).
"""

from __future__ import annotations

import collections
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.models import GlobalConfig, LogTaskConfig, count_valid_lines
from core.packet_sender import PacketTransmitter, format_syslog_payload


class LogTaskWorker(threading.Thread):
    """
    Dedicated worker thread for sending a single Raw Log file concurrently with other files.
    Uses pure Python threading and standard callbacks.
    """

    def __init__(
        self,
        task_id: int,
        task_cfg: LogTaskConfig,
        global_cfg: GlobalConfig,
        on_progress: Optional[Callable[[int, int, int, int, int, int, int], None]] = None,
        on_status: Optional[Callable[[int, str, bool], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_finished: Optional[Callable[[int, bool, int], None]] = None,
    ) -> None:
        super().__init__(name=f"LogWorker-{task_id}", daemon=True)
        self.task_id = task_id
        self.task_cfg = task_cfg
        self.global_cfg = global_cfg
        self.on_progress = on_progress
        self.on_status = on_status
        self.on_log = on_log
        self.on_finished = on_finished

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Set means NOT paused (running)

    def request_stop(self) -> None:
        """Signal the worker to stop immediately."""
        self._stop_event.set()
        self._pause_event.set()

    def set_paused(self, paused: bool) -> None:
        """Pause or resume the worker."""
        if paused:
            self._pause_event.clear()
            self._emit_status("已暫停 (Paused)", False)
        else:
            self._pause_event.set()
            self._emit_status("發送中 (Sending)", False)

    def _emit_status(self, text: str, is_error: bool = False) -> None:
        if self.on_status:
            self.on_status(self.task_id, text, is_error)

    def _emit_log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def run(self) -> None:
        file_path = self.task_cfg.file_path
        sys_name = self.task_cfg.system_name or os.path.basename(file_path)
        source_ip = self.task_cfg.source_ip.strip()
        target_ip = self.global_cfg.target_ip.strip()
        target_port = self.task_cfg.get_effective_port(self.global_cfg)
        repeat_count = self.task_cfg.get_effective_repeat(self.global_cfg)  # 0 = infinite
        eps = self.task_cfg.get_effective_eps(self.global_cfg)  # 0 = unlimited
        loop_interval = max(0.0, self.global_cfg.loop_interval_sec)

        if not file_path or not os.path.isfile(file_path):
            msg = f"[{sys_name}] 錯誤：找不到檔案 {file_path}"
            self._emit_status("檔案不存在", True)
            self._emit_log(msg)
            if self.on_finished:
                self.on_finished(self.task_id, False, 0)
            return

        total_lines = count_valid_lines(file_path)
        if total_lines == 0:
            msg = f"[{sys_name}] 警告：檔案內沒有可發送的有效行 ({file_path})"
            self._emit_status("空檔案 (0 行)", True)
            self._emit_log(msg)
            if self.on_finished:
                self.on_finished(self.task_id, False, 0)
            return

        transmitter: Optional[PacketTransmitter] = None
        try:
            transmitter = PacketTransmitter(
                requested_mode=self.global_cfg.send_mode,
                source_ip=source_ip,
            )
        except Exception as exc:
            self._emit_status("Socket 錯誤", True)
            self._emit_log(f"[{sys_name}] 無法初始化發送器: {exc}")
            if self.on_finished:
                self.on_finished(self.task_id, False, 0)
            return

        if transmitter.active_mode == "ip_alias_bind":
            mode_label = f"網卡 IP 綁定 (bind {source_ip})"
            src_display = f"來源 IP: {source_ip}"
        elif transmitter.active_mode == "raw_spoof":
            mode_label = "Raw Socket 偽造來源 IP"
            src_display = f"來源 IP: {source_ip} (Raw 偽造)"
        else:
            mode_label = "一般 UDP Socket (未綁定特定來源 IP)"
            if target_ip.startswith("127.") or target_ip == "localhost":
                actual_ip = "127.0.0.1"
            else:
                try:
                    s_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s_probe.connect((target_ip, target_port or 514))
                    actual_ip = s_probe.getsockname()[0]
                    s_probe.close()
                except Exception:
                    actual_ip = "本機網卡預設 IP"
            src_display = f"實際來源 IP: {actual_ip} (一般權限模式，忽略模擬 IP {source_ip})"

        repeat_label = "無限循環" if repeat_count == 0 else f"{repeat_count} 次"
        eps_label = "全速不限速" if eps == 0 else f"{eps} EPS"

        self._emit_log(
            f"[{sys_name}] 開始發送 | {src_display} -> 目標: {target_ip}:{target_port} | "
            f"模式: {mode_label} | 重複: {repeat_label} | 速率: {eps_label} | 總行數: {total_lines}"
        )
        self._emit_status("發送中 (Sending)", False)

        total_sent_all_rounds = 0
        current_round = 0
        interval_per_pkt = (1.0 / eps) if eps > 0 else 0.0

        try:
            while not self._stop_event.is_set():
                current_round += 1
                sent_in_round = 0
                next_send_time = time.perf_counter()

                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        if self._stop_event.is_set():
                            break

                        self._pause_event.wait()
                        if self._stop_event.is_set():
                            break

                        if not raw_line.strip():
                            continue

                        payload = format_syslog_payload(
                            raw_line=raw_line,
                            source_ip=source_ip,
                            prepend_header=self.global_cfg.prepend_syslog_header,
                            facility=self.global_cfg.syslog_facility,
                            severity=self.global_cfg.syslog_severity,
                        )

                        transmitter.send_log(
                            source_ip=source_ip,
                            target_ip=target_ip,
                            target_port=target_port,
                            payload=payload,
                        )

                        sent_in_round += 1
                        total_sent_all_rounds += 1

                        if repeat_count > 0:
                            overall_target = total_lines * repeat_count
                            progress_pct = int(min(100, (total_sent_all_rounds * 100) / overall_target))
                        else:
                            progress_pct = int(min(100, (sent_in_round * 100) / total_lines))

                        if (
                            sent_in_round == 1
                            or sent_in_round == total_lines
                            or sent_in_round % max(1, min(50, total_lines // 10 or 1)) == 0
                        ):
                            if self.on_progress:
                                self.on_progress(
                                    self.task_id,
                                    sent_in_round,
                                    total_lines,
                                    total_sent_all_rounds,
                                    current_round,
                                    repeat_count,
                                    progress_pct,
                                )

                        if interval_per_pkt > 0:
                            next_send_time += interval_per_pkt
                            sleep_duration = next_send_time - time.perf_counter()
                            if sleep_duration > 0:
                                end_sleep = time.perf_counter() + sleep_duration
                                while time.perf_counter() < end_sleep and not self._stop_event.is_set():
                                    remaining = end_sleep - time.perf_counter()
                                    time.sleep(min(0.02, max(0.0, remaining)))
                            else:
                                if sleep_duration < -0.2:
                                    next_send_time = time.perf_counter()

                if self._stop_event.is_set():
                    break

                if repeat_count > 0 and current_round >= repeat_count:
                    if self.on_progress:
                        self.on_progress(
                            self.task_id,
                            sent_in_round,
                            total_lines,
                            total_sent_all_rounds,
                            current_round,
                            repeat_count,
                            100,
                        )
                    break

                if loop_interval > 0 and not self._stop_event.is_set():
                    self._emit_status(
                        f"等待下輪 ({current_round}/{repeat_count if repeat_count > 0 else '∞'})",
                        False,
                    )
                    end_wait = time.perf_counter() + loop_interval
                    while time.perf_counter() < end_wait and not self._stop_event.is_set():
                        self._pause_event.wait()
                        remaining = end_wait - time.perf_counter()
                        if remaining > 0:
                            time.sleep(min(0.05, remaining))
                    if not self._stop_event.is_set():
                        self._emit_status("發送中 (Sending)", False)

            if self._stop_event.is_set():
                self._emit_status("已停止 (Stopped)", False)
                self._emit_log(f"[{sys_name}] 已手動停止（累計發送 {total_sent_all_rounds} 筆封包）")
                if self.on_finished:
                    self.on_finished(self.task_id, False, total_sent_all_rounds)
            else:
                self._emit_status("已完成 (Completed)", False)
                self._emit_log(
                    f"[{sys_name}] 發送完成！共執行 {current_round} 輪，總計發送 {total_sent_all_rounds} 筆封包。"
                )
                if self.on_finished:
                    self.on_finished(self.task_id, True, total_sent_all_rounds)

        except Exception as exc:
            self._emit_status(f"錯誤: {exc}", True)
            self._emit_log(f"[{sys_name}] 發送過程發生例外錯誤: {exc}")
            if self.on_finished:
                self.on_finished(self.task_id, False, total_sent_all_rounds)
        finally:
            if transmitter is not None:
                transmitter.close()


class LocalUdpReceiverThread(threading.Thread):
    """
    Pure Python standard library local UDP listener thread for self-testing.
    """

    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        port: int = 5514,
        on_packet: Optional[Callable[[str, int, str], None]] = None,
        on_status: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        super().__init__(name="LocalUdpReceiver", daemon=True)
        self.bind_ip = bind_ip
        self.port = port
        self.on_packet = on_packet
        self.on_status = on_status
        self._stop_flag = False
        self._sock: Optional[socket.socket] = None

    def stop_listening(self) -> None:
        self._stop_flag = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.3)
            sock.bind((self.bind_ip, self.port))
            self._sock = sock
            if self.on_status:
                self.on_status(True, f"正在監聽本機 UDP {self.bind_ip}:{self.port}")
        except OSError as exc:
            if self.on_status:
                self.on_status(
                    False,
                    f"無法綁定 UDP Port {self.port}: {exc} (小於 1024 Port 需要 sudo 權限，測試建議改用 5514)",
                )
            return

        while not self._stop_flag:
            try:
                data, addr = self._sock.recvfrom(65535)
                text = data.decode("utf-8", errors="replace").rstrip("\r\n")
                if self.on_packet:
                    self.on_packet(addr[0], addr[1], text)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_flag:
                    break
                continue

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.on_status:
            self.on_status(False, "本機 UDP 測試接收器已停止")
