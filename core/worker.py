"""Concurrent worker threads for sending multiple Raw Log files and local UDP test listener."""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from core.models import GlobalConfig, LogTaskConfig, count_valid_lines
from core.packet_sender import PacketTransmitter, format_syslog_payload


class LogTaskWorker(QThread):
    """
    Dedicated worker thread for sending a single Raw Log file concurrently with other files.
    Signals report real-time progress, round status, log events, and completion to the GUI.
    """

    # Signals: (row_index, sent_lines_in_round, total_lines_in_file, total_sent_all_rounds, current_round, total_rounds, progress_pct)
    progress_updated = Signal(int, int, int, int, int, int, int)
    # Signals: (row_index, status_text, is_error)
    status_changed = Signal(int, str, bool)
    # Signals: (log_message)
    log_message = Signal(str)
    # Signals: (row_index, success_bool, total_packets_sent)
    task_finished = Signal(int, bool, int)

    def __init__(
        self,
        row_index: int,
        task_cfg: LogTaskConfig,
        global_cfg: GlobalConfig,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.row_index = row_index
        self.task_cfg = task_cfg
        self.global_cfg = global_cfg

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Set means NOT paused (running)

    def request_stop(self) -> None:
        """Signal the worker to stop immediately."""
        self._stop_event.set()
        self._pause_event.set()  # Unblock if paused

    def set_paused(self, paused: bool) -> None:
        """Pause or resume the worker."""
        if paused:
            self._pause_event.clear()
            self.status_changed.emit(self.row_index, "已暫停 (Paused)", False)
        else:
            self._pause_event.set()
            self.status_changed.emit(self.row_index, "發送中 (Sending)", False)

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
            self.status_changed.emit(self.row_index, "檔案不存在", True)
            self.log_message.emit(msg)
            self.task_finished.emit(self.row_index, False, 0)
            return

        total_lines = count_valid_lines(file_path)
        if total_lines == 0:
            msg = f"[{sys_name}] 警告：檔案內沒有可發送的有效行 ({file_path})"
            self.status_changed.emit(self.row_index, "空檔案 (0 行)", True)
            self.log_message.emit(msg)
            self.task_finished.emit(self.row_index, False, 0)
            return

        transmitter: Optional[PacketTransmitter] = None
        try:
            transmitter = PacketTransmitter(
                requested_mode=self.global_cfg.send_mode,
                source_ip=source_ip,
            )
        except Exception as exc:
            self.status_changed.emit(self.row_index, f"Socket 錯誤", True)
            self.log_message.emit(f"[{sys_name}] 無法初始化發送器: {exc}")
            self.task_finished.emit(self.row_index, False, 0)
            return

        if transmitter.active_mode == "ip_alias_bind":
            mode_label = f"網卡虛擬副 IP 綁定 (bind {source_ip})"
        elif transmitter.active_mode == "raw_spoof":
            mode_label = "Raw Socket 偽造來源 IP"
        else:
            mode_label = "一般 UDP Socket (未綁定特定來源 IP)"
        repeat_label = "無限循環" if repeat_count == 0 else f"{repeat_count} 次"
        eps_label = "全速不限速" if eps == 0 else f"{eps} EPS"

        self.log_message.emit(
            f"[{sys_name}] 開始發送 | 來源 IP: {source_ip} -> 目標: {target_ip}:{target_port} | "
            f"模式: {mode_label} | 重複: {repeat_label} | 速率: {eps_label} | 總行數: {total_lines}"
        )
        self.status_changed.emit(self.row_index, "發送中 (Sending)", False)

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

                        # Wait if paused
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

                        # Calculate progress percentage
                        if repeat_count > 0:
                            overall_target = total_lines * repeat_count
                            progress_pct = int(min(100, (total_sent_all_rounds * 100) / overall_target))
                        else:
                            progress_pct = int(min(100, (sent_in_round * 100) / total_lines))

                        # Throttle UI updates slightly when EPS is high to keep GUI responsive
                        if sent_in_round == 1 or sent_in_round == total_lines or sent_in_round % max(1, min(50, total_lines // 10 or 1)) == 0:
                            self.progress_updated.emit(
                                self.row_index,
                                sent_in_round,
                                total_lines,
                                total_sent_all_rounds,
                                current_round,
                                repeat_count,
                                progress_pct,
                            )

                        # Rate limiting (EPS)
                        if interval_per_pkt > 0:
                            next_send_time += interval_per_pkt
                            sleep_duration = next_send_time - time.perf_counter()
                            if sleep_duration > 0:
                                # Sleep in small chunks so stop_event is responsive
                                end_sleep = time.perf_counter() + sleep_duration
                                while time.perf_counter() < end_sleep and not self._stop_event.is_set():
                                    remaining = end_sleep - time.perf_counter()
                                    time.sleep(min(0.02, max(0.0, remaining)))
                            else:
                                # If behind schedule by more than 0.2s, reset timer to avoid burst catch-up
                                if sleep_duration < -0.2:
                                    next_send_time = time.perf_counter()

                if self._stop_event.is_set():
                    break

                # Check if all rounds completed
                if repeat_count > 0 and current_round >= repeat_count:
                    self.progress_updated.emit(
                        self.row_index,
                        sent_in_round,
                        total_lines,
                        total_sent_all_rounds,
                        current_round,
                        repeat_count,
                        100,
                    )
                    break

                # Wait loop_interval between rounds
                if loop_interval > 0 and not self._stop_event.is_set():
                    self.status_changed.emit(
                        self.row_index,
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
                        self.status_changed.emit(self.row_index, "發送中 (Sending)", False)

            if self._stop_event.is_set():
                self.status_changed.emit(self.row_index, "已停止 (Stopped)", False)
                self.log_message.emit(
                    f"[{sys_name}] 已手動停止（累計發送 {total_sent_all_rounds} 筆封包）"
                )
                self.task_finished.emit(self.row_index, False, total_sent_all_rounds)
            else:
                self.status_changed.emit(self.row_index, "已完成 (Completed)", False)
                self.log_message.emit(
                    f"[{sys_name}] 發送完成！共執行 {current_round} 輪，總計發送 {total_sent_all_rounds} 筆封包。"
                )
                self.task_finished.emit(self.row_index, True, total_sent_all_rounds)

        except Exception as exc:
            self.status_changed.emit(self.row_index, f"錯誤: {exc}", True)
            self.log_message.emit(f"[{sys_name}] 發送過程發生例外錯誤: {exc}")
            self.task_finished.emit(self.row_index, False, total_sent_all_rounds)
        finally:
            if transmitter is not None:
                transmitter.close()


class LocalUdpReceiverThread(QThread):
    """
    Built-in local UDP listener thread for quick self-testing in the GUI.
    Allows listening on a UDP port (e.g., 5514 or 514 if root) and displaying incoming packets.
    """

    packet_received = Signal(str, int, str)  # (src_ip, src_port, decoded_payload)
    listener_status = Signal(bool, str)  # (is_running, status_msg)

    def __init__(self, bind_ip: str = "0.0.0.0", port: int = 5514, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.bind_ip = bind_ip
        self.port = port
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
            self.listener_status.emit(True, f"正在監聽本機 UDP {self.bind_ip}:{self.port}")
        except OSError as exc:
            self.listener_status.emit(
                False,
                f"無法綁定 UDP Port {self.port}: {exc} (注意：小於 1024 的 Port 如 514 需要 sudo 權限，一般測試可改用 5514)",
            )
            return

        while not self._stop_flag:
            try:
                data, addr = self._sock.recvfrom(65535)
                text = data.decode("utf-8", errors="replace").rstrip("\r\n")
                self.packet_received.emit(addr[0], addr[1], text)
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
        self.listener_status.emit(False, "本機 UDP 測試接收器已停止")
