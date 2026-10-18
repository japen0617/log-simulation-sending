"""PySide6 Main Window GUI for Multi-Source Raw Log UDP Sender."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ip_alias_manager import IpAliasManager
from core.models import (
    GlobalConfig,
    LogTaskConfig,
    ProjectProfile,
    count_valid_lines,
    generate_sequential_ips,
    preview_file_lines,
    validate_ipv4,
)
from core.packet_sender import has_raw_socket_privilege
from core.worker import LocalUdpReceiverThread, LogTaskWorker


class MainWindow(QMainWindow):
    """Main Desktop GUI for Multi-Source Raw Log UDP Sender."""

    COL_ENABLED = 0
    COL_SYS_NAME = 1
    COL_FILE_PATH = 2
    COL_LINE_COUNT = 3
    COL_SOURCE_IP = 4
    COL_TARGET_PORT = 5
    COL_REPEAT = 6
    COL_EPS = 7
    COL_ROUND_STATS = 8
    COL_PROGRESS = 9
    COL_STATUS = 10

    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.setWindowTitle("Multi-Source Raw Log UDP 發送模擬器 (支援網卡虛擬副 IP 綁定 & Raw Socket 偽造)")
        self.resize(1300, 850)

        self.alias_manager = IpAliasManager()
        self.workers: Dict[int, LogTaskWorker] = {}
        self.row_sent_counts: Dict[int, int] = {}
        self.is_paused: bool = False
        self.receiver_thread: Optional[LocalUdpReceiverThread] = None
        self.received_count: int = 0
        self._updating_table: bool = False

        self._init_ui()
        self._refresh_interfaces_combo()
        self._load_default_profile_if_exists()
        self._refresh_privilege_banner()

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # 1. Privilege & Mode Status Banner
        self.lbl_priv_banner = QLabel()
        self.lbl_priv_banner.setStyleSheet(
            "padding: 6px 10px; border-radius: 4px; font-weight: bold;"
        )
        main_layout.addWidget(self.lbl_priv_banner)

        # 2. Global Settings GroupBox
        global_group = QGroupBox("🌐 全域發送設定 (Global Settings)")
        global_layout = QVBoxLayout(global_group)
        global_layout.setSpacing(6)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("目標伺服器 IP (Destination IP):"))
        self.edit_target_ip = QLineEdit("127.0.0.1")
        self.edit_target_ip.setPlaceholderText("例如 192.168.1.50 或 127.0.0.1")
        self.edit_target_ip.setFixedWidth(160)
        row1.addWidget(self.edit_target_ip)

        row1.addSpacing(12)
        row1.addWidget(QLabel("預設目標 UDP Port:"))
        self.spin_default_port = QSpinBox()
        self.spin_default_port.setRange(1, 65535)
        self.spin_default_port.setValue(514)
        self.spin_default_port.setFixedWidth(90)
        row1.addWidget(self.spin_default_port)

        row1.addSpacing(12)
        row1.addWidget(QLabel("預設重複次數:"))
        self.spin_repeat = QSpinBox()
        self.spin_repeat.setRange(1, 1000000)
        self.spin_repeat.setValue(1)
        self.spin_repeat.setFixedWidth(90)
        row1.addWidget(self.spin_repeat)

        self.chk_infinite = QCheckBox("無限循環發送 (直到手動停止)")
        self.chk_infinite.toggled.connect(self._on_infinite_toggled)
        row1.addWidget(self.chk_infinite)

        row1.addSpacing(12)
        row1.addWidget(QLabel("每輪間隔(秒):"))
        self.spin_loop_interval = QDoubleSpinBox()
        self.spin_loop_interval.setRange(0.0, 3600.0)
        self.spin_loop_interval.setSingleStep(0.5)
        self.spin_loop_interval.setDecimals(1)
        self.spin_loop_interval.setValue(1.0)
        self.spin_loop_interval.setFixedWidth(80)
        row1.addWidget(self.spin_loop_interval)
        row1.addStretch()
        global_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("預設速率 (EPS 每秒筆數, 0=不限速):"))
        self.spin_eps = QSpinBox()
        self.spin_eps.setRange(0, 100000)
        self.spin_eps.setValue(100)
        self.spin_eps.setFixedWidth(95)
        row2.addWidget(self.spin_eps)

        row2.addSpacing(12)
        row2.addWidget(QLabel("封包發送模式:"))
        self.combo_send_mode = QComboBox()
        self.combo_send_mode.addItem(
            "推薦：自動偵測 (優先使用網卡綁定虛擬 IP -> Raw Socket 偽造 -> 一般 UDP)", "auto"
        )
        self.combo_send_mode.addItem(
            "網卡綁定虛擬副 IP 模式 (IP Alias + Bind，最穩定 100% 送達 Log Server，需 sudo)",
            "ip_alias_bind",
        )
        self.combo_send_mode.addItem(
            "強制 Raw Socket 偽造來源 IP (不改網卡，需 sudo/root)", "raw_spoof"
        )
        self.combo_send_mode.addItem(
            "一般 UDP Socket (免權限測試，使用本機預設 IP)", "standard_udp"
        )
        self.combo_send_mode.setFixedWidth(440)
        row2.addWidget(self.combo_send_mode)

        row2.addSpacing(12)
        self.chk_prepend_header = QCheckBox(
            "自動補上 RFC3164 Syslog Header (<134>時間 模擬來源IP)"
        )
        row2.addWidget(self.chk_prepend_header)
        row2.addStretch()
        global_layout.addLayout(row2)

        # Row 3: Network Interface Virtual IP Alias (IP Alias) Bar
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("🔌 虛擬副 IP 綁定網卡 (IP Alias):"))
        self.combo_interface = QComboBox()
        self.combo_interface.setFixedWidth(260)
        self.combo_interface.setToolTip(
            "選擇要掛載模擬來源 IP (虛擬副 IP) 的本機網卡，讓非本機 IP 成為真實合法 IP 以通過交換器與系統路由"
        )
        row3.addWidget(self.combo_interface)

        self.btn_refresh_iface = QPushButton("🔄 重新整理網卡")
        self.btn_refresh_iface.clicked.connect(self._refresh_interfaces_combo)
        row3.addWidget(self.btn_refresh_iface)

        self.btn_mount_aliases = QPushButton("🔗 一鍵掛載清單內虛擬副 IP 至網卡")
        self.btn_mount_aliases.setToolTip(
            "將下方勾選任務的模擬來源 IP 自動加到所選網卡上 (macOS: ifconfig alias / Linux: ip addr add)"
        )
        self.btn_mount_aliases.clicked.connect(self._on_mount_all_aliases)
        row3.addWidget(self.btn_mount_aliases)

        self.btn_unmount_aliases = QPushButton("🧹 一鍵清除已掛載的虛擬副 IP")
        self.btn_unmount_aliases.setToolTip("將本程式掛載於網卡上的虛擬副 IP 全部卸載還原")
        self.btn_unmount_aliases.clicked.connect(self._on_unmount_all_aliases)
        row3.addWidget(self.btn_unmount_aliases)

        self.chk_auto_cleanup_aliases = QCheckBox("關閉程式時自動清除虛擬副 IP")
        self.chk_auto_cleanup_aliases.setChecked(True)
        row3.addWidget(self.chk_auto_cleanup_aliases)
        row3.addStretch()
        global_layout.addLayout(row3)

        main_layout.addWidget(global_group)

        # 3. Vertical Splitter: Top = Task List & Controls, Bottom = Tabs (Console / Preview / Receiver)
        splitter = QSplitter(Qt.Orientation.Vertical)
        main_layout.addWidget(splitter, stretch=1)

        # Top Widget (Task Management)
        task_container = QWidget()
        task_layout = QVBoxLayout(task_container)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.setSpacing(6)

        # Toolbar for files & batch IP
        toolbar = QHBoxLayout()
        self.btn_add_files = QPushButton("➕ 加入 Raw Log 檔案 (可多選)")
        self.btn_add_files.clicked.connect(self._on_add_files)
        toolbar.addWidget(self.btn_add_files)

        self.btn_add_folder = QPushButton("📂 匯入資料夾")
        self.btn_add_folder.clicked.connect(self._on_add_folder)
        toolbar.addWidget(self.btn_add_folder)

        self.btn_batch_ip = QPushButton("🔢 批次自動遞增分配來源 IP")
        self.btn_batch_ip.setToolTip("輸入起始 IP，自動為列表內所有 Raw Log 依序填入不同的來源 IP")
        self.btn_batch_ip.clicked.connect(self._on_batch_assign_ip)
        toolbar.addWidget(self.btn_batch_ip)

        self.btn_check_all = QPushButton("☑️ 全選 / 取消全選")
        self.btn_check_all.clicked.connect(self._on_toggle_select_all)
        toolbar.addWidget(self.btn_check_all)

        self.btn_remove_selected = QPushButton("🗑️ 移除選取列")
        self.btn_remove_selected.clicked.connect(self._on_remove_selected)
        toolbar.addWidget(self.btn_remove_selected)

        toolbar.addStretch()

        self.btn_load_profile = QPushButton("📥 載入設定檔 (.json)")
        self.btn_load_profile.clicked.connect(self._on_load_profile_dialog)
        toolbar.addWidget(self.btn_load_profile)

        self.btn_save_profile = QPushButton("💾 儲存設定檔 (.json)")
        self.btn_save_profile.clicked.connect(self._on_save_profile_dialog)
        toolbar.addWidget(self.btn_save_profile)

        task_layout.addLayout(toolbar)

        # Task Table
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            [
                "啟用",
                "系統 / 任務名稱",
                "Raw Log 檔案路徑",
                "有效行數",
                "模擬來源 IP (必填)",
                "目標 Port\n(空白=預設)",
                "重複次數\n(空白=預設)",
                "速率 EPS\n(空白=預設)",
                "輪數 / 已送筆數",
                "發送進度",
                "狀態",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.itemChanged.connect(self._on_table_item_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SYS_NAME, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_SYS_NAME, 150)
        header.setSectionResizeMode(self.COL_FILE_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_LINE_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SOURCE_IP, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_SOURCE_IP, 135)
        header.setSectionResizeMode(self.COL_TARGET_PORT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_REPEAT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_EPS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_ROUND_STATS, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_ROUND_STATS, 135)
        header.setSectionResizeMode(self.COL_PROGRESS, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_PROGRESS, 120)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_STATUS, 135)

        task_layout.addWidget(self.table, stretch=1)

        # Action Control Bar
        action_bar = QHBoxLayout()
        self.btn_start_all = QPushButton("▶ 開始同時發送勾選的 Raw Log")
        self.btn_start_all.setMinimumHeight(38)
        self.btn_start_all.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; font-weight: bold; font-size: 14px; padding: 6px 18px; border-radius: 5px; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        self.btn_start_all.clicked.connect(self._on_start_all)
        action_bar.addWidget(self.btn_start_all)

        self.btn_pause_all = QPushButton("⏸ 暫停全部")
        self.btn_pause_all.setMinimumHeight(38)
        self.btn_pause_all.setEnabled(False)
        self.btn_pause_all.setStyleSheet(
            "QPushButton { background-color: #d97706; color: white; font-weight: bold; font-size: 13px; padding: 6px 14px; border-radius: 5px; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        self.btn_pause_all.clicked.connect(self._on_pause_resume_all)
        action_bar.addWidget(self.btn_pause_all)

        self.btn_stop_all = QPushButton("⏹ 停止全部")
        self.btn_stop_all.setMinimumHeight(38)
        self.btn_stop_all.setEnabled(False)
        self.btn_stop_all.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; font-weight: bold; font-size: 13px; padding: 6px 14px; border-radius: 5px; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        self.btn_stop_all.clicked.connect(self._on_stop_all)
        action_bar.addWidget(self.btn_stop_all)

        action_bar.addSpacing(16)
        self.lbl_summary = QLabel("執行中任務: 0 | 本次累計發送封包: 0")
        self.lbl_summary.setStyleSheet("font-weight: bold; font-size: 13px;")
        action_bar.addWidget(self.lbl_summary)
        action_bar.addStretch()

        task_layout.addLayout(action_bar)
        splitter.addWidget(task_container)

        # Bottom Widget: Tabs (Execution Log, File Preview, Local Receiver)
        self.tabs = QTabWidget()
        mono_font = QFont("Menlo" if os.uname().sysname == "Darwin" else "Monospace", 11)

        # Tab 1: Execution Log Console
        tab_console = QWidget()
        console_layout = QVBoxLayout(tab_console)
        console_layout.setContentsMargins(6, 6, 6, 6)
        console_top = QHBoxLayout()
        console_top.addWidget(QLabel("即時顯示所有並發發送任務的狀態與統計紀錄："))
        console_top.addStretch()
        btn_clear_log = QPushButton("清除日誌")
        btn_clear_log.clicked.connect(lambda: self.txt_console.clear())
        console_top.addWidget(btn_clear_log)
        console_layout.addLayout(console_top)

        self.txt_console = QPlainTextEdit()
        self.txt_console.setReadOnly(True)
        self.txt_console.setFont(mono_font)
        console_layout.addWidget(self.txt_console)
        self.tabs.addTab(tab_console, "📋 即時執行日誌 (Execution Log)")

        # Tab 2: Raw Log File Content Preview
        tab_preview = QWidget()
        preview_layout = QVBoxLayout(tab_preview)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        self.lbl_preview_title = QLabel("點選上方表格中的任一列，即可預覽該系統的 Raw Log 內容（前 100 行）")
        preview_layout.addWidget(self.lbl_preview_title)
        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setFont(mono_font)
        preview_layout.addWidget(self.txt_preview)
        self.tabs.addTab(tab_preview, "🔍 Raw Log 檔案內容預覽")

        # Tab 3: Built-in Local UDP Test Receiver
        tab_receiver = QWidget()
        recv_layout = QVBoxLayout(tab_receiver)
        recv_layout.setContentsMargins(6, 6, 6, 6)
        recv_bar = QHBoxLayout()
        recv_bar.addWidget(QLabel("本機監聽 UDP Port:"))
        self.spin_recv_port = QSpinBox()
        self.spin_recv_port.setRange(1, 65535)
        self.spin_recv_port.setValue(514 if has_raw_socket_privilege() else 5514)
        self.spin_recv_port.setFixedWidth(90)
        recv_bar.addWidget(self.spin_recv_port)

        self.btn_toggle_recv = QPushButton("📡 啟動本機 UDP 測試接收器")
        self.btn_toggle_recv.clicked.connect(self._on_toggle_receiver)
        recv_bar.addWidget(self.btn_toggle_recv)

        self.btn_use_local_target = QPushButton("⚡ 一鍵將上方全域目標設為本機接收 Port")
        self.btn_use_local_target.clicked.connect(self._on_apply_local_receiver_target)
        recv_bar.addWidget(self.btn_use_local_target)

        self.lbl_recv_status = QLabel("狀態：未啟動")
        recv_bar.addWidget(self.lbl_recv_status)
        recv_bar.addStretch()

        btn_clear_recv = QPushButton("清除接收紀錄")
        btn_clear_recv.clicked.connect(self._on_clear_receiver_log)
        recv_bar.addWidget(btn_clear_recv)
        recv_layout.addLayout(recv_bar)

        self.txt_receiver = QPlainTextEdit()
        self.txt_receiver.setReadOnly(True)
        self.txt_receiver.setFont(mono_font)
        recv_layout.addWidget(self.txt_receiver)
        self.tabs.addTab(tab_receiver, "📡 內建本機 UDP 接收測試器")

        splitter.addWidget(self.tabs)
        splitter.setSizes([470, 240])

    def _refresh_privilege_banner(self) -> None:
        has_priv = has_raw_socket_privilege()
        if has_priv:
            self.lbl_priv_banner.setText(
                "✅ 目前具備 Root / Sudo 權限：支援「網卡自動掛載虛擬副 IP (IP Alias) + 真實綁定發送」與「Raw Socket 來源 IP 偽造模式」。"
            )
            self.lbl_priv_banner.setStyleSheet(
                "background-color: #dcfce7; color: #166534; padding: 6px 10px; border-radius: 4px; font-weight: bold;"
            )
        else:
            self.lbl_priv_banner.setText(
                "ℹ️ 目前以一般權限執行：自動模式將使用「一般 UDP Socket」發送。若需「網卡掛載虛擬副 IP (100% 送達 Log Server)」或「Raw Socket 偽造」，請以 sudo ./main.py 或 ./run_sudo.sh 啟動。"
            )
            self.lbl_priv_banner.setStyleSheet(
                "background-color: #fef3c7; color: #92400e; padding: 6px 10px; border-radius: 4px; font-weight: bold;"
            )

    def _refresh_interfaces_combo(self) -> None:
        prev_iface = self.combo_interface.currentData()
        self.combo_interface.clear()
        interfaces = self.alias_manager.list_interfaces()
        for iface in interfaces:
            self.combo_interface.addItem(iface.display_label, iface.name)
        if prev_iface:
            idx = self.combo_interface.findData(prev_iface)
            if idx >= 0:
                self.combo_interface.setCurrentIndex(idx)

    def _on_mount_all_aliases(self) -> None:
        iface = str(self.combo_interface.currentData() or "en0")
        if not self.alias_manager.is_root():
            QMessageBox.warning(
                self,
                "需要 Sudo 權限",
                "在網卡上掛載虛擬副 IP (IP Alias) 需要管理員權限。\n\n"
                "請在終端機使用以下指令啟動程式：\n"
                "  sudo ./main.py\n"
                "或\n"
                "  ./run_sudo.sh",
            )
            return

        mounted_count = 0
        already_count = 0
        errors: List[str] = []

        for r in range(self.table.rowCount()):
            task = self._collect_row_task_config(r)
            if not task.enabled:
                continue
            if not validate_ipv4(task.source_ip):
                errors.append(f"第 {r + 1} 列 IP 格式錯誤: {task.source_ip}")
                continue
            was_local = self.alias_manager.is_ip_local(task.source_ip)
            ok, msg = self.alias_manager.add_ip_alias(iface, task.source_ip)
            self._append_console_log(f"[虛擬副 IP] {msg}")
            if ok:
                if was_local:
                    already_count += 1
                else:
                    mounted_count += 1
            else:
                errors.append(msg)

        self._refresh_interfaces_combo()
        if errors:
            QMessageBox.warning(
                self,
                "部分虛擬副 IP 掛載失敗",
                "\n".join(errors[:5]),
            )
        else:
            QMessageBox.information(
                self,
                "虛擬副 IP 掛載完成",
                f"網卡 [{iface}] 虛擬副 IP 準備完成！\n"
                f"・新增掛載：{mounted_count} 個 IP\n"
                f"・原本已存在：{already_count} 個 IP\n\n"
                f"現在發送時將直接使用真實網卡綁定 (socket.bind)，可 100% 穩定送達 Log Server！",
            )

    def _on_unmount_all_aliases(self) -> None:
        if not self.alias_manager.mounted_aliases:
            QMessageBox.information(
                self, "提示", "目前沒有由本程式動態掛載的虛擬副 IP 需要清除。"
            )
            return
        logs = self.alias_manager.remove_all_mounted()
        for msg in logs:
            self._append_console_log(f"[虛擬副 IP] {msg}")
        self._refresh_interfaces_combo()
        QMessageBox.information(
            self, "清除完成", f"已卸載 {len(logs)} 個虛擬副 IP，網卡已還原。"
        )

    def _on_infinite_toggled(self, checked: bool) -> None:
        self.spin_repeat.setEnabled(not checked)

    def _append_console_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_console.appendPlainText(f"[{ts}] {msg}")

    def _load_default_profile_if_exists(self) -> None:
        default_json = self.project_dir / "default_profile.json"
        if default_json.is_file():
            try:
                profile = ProjectProfile.load_from_file(default_json, base_dir=self.project_dir)
                self._apply_profile_to_ui(profile)
                self._append_console_log(
                    f"已自動載入預設設定檔 default_profile.json（共 {len(profile.tasks)} 個範例系統 Raw Log）"
                )
            except Exception as exc:
                self._append_console_log(f"載入 default_profile.json 失敗: {exc}")

    def _collect_global_config(self) -> GlobalConfig:
        return GlobalConfig(
            target_ip=self.edit_target_ip.text().strip() or "127.0.0.1",
            default_port=self.spin_default_port.value(),
            default_repeat_count=self.spin_repeat.value(),
            infinite_loop=self.chk_infinite.isChecked(),
            loop_interval_sec=self.spin_loop_interval.value(),
            default_eps=self.spin_eps.value(),
            send_mode=str(self.combo_send_mode.currentData() or "auto"),
            bind_interface=str(self.combo_interface.currentData() or ""),
            auto_cleanup_aliases=self.chk_auto_cleanup_aliases.isChecked(),
            prepend_syslog_header=self.chk_prepend_header.isChecked(),
        )

    def _collect_row_task_config(self, row: int) -> LogTaskConfig:
        chk_item = self.table.item(row, self.COL_ENABLED)
        enabled = chk_item.checkState() == Qt.CheckState.Checked if chk_item else True

        sys_name = self._get_cell_text(row, self.COL_SYS_NAME)
        file_path = self._get_cell_text(row, self.COL_FILE_PATH)
        source_ip = self._get_cell_text(row, self.COL_SOURCE_IP)

        port_str = self._get_cell_text(row, self.COL_TARGET_PORT)
        target_port = int(port_str) if port_str.isdigit() and int(port_str) > 0 else None

        repeat_str = self._get_cell_text(row, self.COL_REPEAT)
        repeat_count = int(repeat_str) if repeat_str.isdigit() and int(repeat_str) > 0 else None

        eps_str = self._get_cell_text(row, self.COL_EPS)
        eps = int(eps_str) if eps_str.isdigit() and int(eps_str) >= 0 else None

        return LogTaskConfig(
            enabled=enabled,
            system_name=sys_name,
            file_path=file_path,
            source_ip=source_ip,
            target_port=target_port,
            repeat_count=repeat_count,
            eps=eps,
        )

    def _get_cell_text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _add_task_row(self, task: LogTaskConfig) -> None:
        self._updating_table = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)

            # 0. Enabled checkbox
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk_item.setCheckState(Qt.CheckState.Checked if task.enabled else Qt.CheckState.Unchecked)
            self.table.setItem(row, self.COL_ENABLED, chk_item)

            # 1. System Name
            sys_name = task.system_name or Path(task.file_path).stem
            self.table.setItem(row, self.COL_SYS_NAME, QTableWidgetItem(sys_name))

            # 2. File Path (read-only)
            path_item = QTableWidgetItem(task.file_path)
            path_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            path_item.setToolTip(task.file_path)
            self.table.setItem(row, self.COL_FILE_PATH, path_item)

            # 3. Valid Line Count (read-only)
            lines = count_valid_lines(task.file_path)
            line_item = QTableWidgetItem(str(lines))
            line_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            line_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, self.COL_LINE_COUNT, line_item)

            # 4. Source IP
            ip_item = QTableWidgetItem(task.source_ip)
            ip_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, self.COL_SOURCE_IP, ip_item)

            # 5. Target Port (optional override)
            port_text = str(task.target_port) if task.target_port is not None else ""
            port_item = QTableWidgetItem(port_text)
            port_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            port_item.setToolTip("留空代表使用上方全域預設 Port (例如 514)")
            self.table.setItem(row, self.COL_TARGET_PORT, port_item)

            # 6. Repeat Count (optional override)
            rep_text = str(task.repeat_count) if task.repeat_count is not None else ""
            rep_item = QTableWidgetItem(rep_text)
            rep_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            rep_item.setToolTip("留空代表使用上方全域預設重複次數")
            self.table.setItem(row, self.COL_REPEAT, rep_item)

            # 7. EPS (optional override)
            eps_text = str(task.eps) if task.eps is not None else ""
            eps_item = QTableWidgetItem(eps_text)
            eps_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            eps_item.setToolTip("留空代表使用上方全域預設速率 EPS")
            self.table.setItem(row, self.COL_EPS, eps_item)

            # 8. Round / Sent stats (read-only)
            stats_item = QTableWidgetItem("0 / 0 (0 筆)")
            stats_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            stats_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, self.COL_ROUND_STATS, stats_item)

            # 9. Progress Bar widget
            pbar = QProgressBar()
            pbar.setRange(0, 100)
            pbar.setValue(0)
            pbar.setTextVisible(True)
            self.table.setCellWidget(row, self.COL_PROGRESS, pbar)

            # 10. Status (read-only)
            status_item = QTableWidgetItem("就緒 (Ready)")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, self.COL_STATUS, status_item)
        finally:
            self._updating_table = False

    def _next_available_source_ip(self) -> str:
        """Suggest the next sequential IPv4 address based on existing rows."""
        existing_ips: List[str] = []
        for r in range(self.table.rowCount()):
            ip_txt = self._get_cell_text(r, self.COL_SOURCE_IP)
            if validate_ipv4(ip_txt):
                existing_ips.append(ip_txt)
        if not existing_ips:
            return "192.168.10.101"
        try:
            seq = generate_sequential_ips(existing_ips[-1], 2)
            return seq[1]
        except Exception:
            return "192.168.10.101"

    def _on_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "選擇一或多個 Raw Log 檔案",
            str(self.project_dir),
            "Log Files (*.log *.txt *.raw *.csv *.json *);;All Files (*)",
        )
        if not files:
            return
        for fpath in files:
            next_ip = self._next_available_source_ip()
            self._add_task_row(
                LogTaskConfig(
                    enabled=True,
                    system_name=Path(fpath).stem,
                    file_path=fpath,
                    source_ip=next_ip,
                )
            )
        self._append_console_log(f"已加入 {len(files)} 個 Raw Log 檔案。")

    def _on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "選擇包含 Raw Log 的資料夾", str(self.project_dir)
        )
        if not folder:
            return
        folder_path = Path(folder)
        added = 0
        for p in sorted(folder_path.iterdir()):
            if p.is_file() and p.suffix.lower() in (".log", ".txt", ".raw", ".syslog"):
                next_ip = self._next_available_source_ip()
                self._add_task_row(
                    LogTaskConfig(
                        enabled=True,
                        system_name=p.stem,
                        file_path=str(p),
                        source_ip=next_ip,
                    )
                )
                added += 1
        if added > 0:
            self._append_console_log(f"已從資料夾 {folder} 匯入 {added} 個 Log 檔案。")
        else:
            QMessageBox.information(
                self, "匯入結果", "該資料夾內未找到 .log / .txt / .raw / .syslog 檔案。"
            )

    def _on_batch_assign_ip(self) -> None:
        row_count = self.table.rowCount()
        if row_count == 0:
            QMessageBox.information(self, "提示", "請先加入 Raw Log 檔案至清單中。")
            return

        iface_name = str(self.combo_interface.currentData() or "en0")
        default_start = self._get_cell_text(0, self.COL_SOURCE_IP) or "192.168.10.101"
        for iface in self.alias_manager.list_interfaces():
            if iface.name == iface_name and iface.ipv4_addresses:
                primary_ip = iface.ipv4_addresses[0]
                parts = primary_ip.split(".")
                if len(parts) == 4 and parts[0] != "127":
                    default_start = f"{parts[0]}.{parts[1]}.{parts[2]}.201"
                break
        if not validate_ipv4(default_start):
            default_start = "192.168.10.101"

        start_ip, ok = QInputDialog.getText(
            self,
            "批次自動遞增分配來源 IP",
            f"請輸入起始來源 IPv4 位址（預設已自動帶入目前網卡 {iface_name} 同網段建議值，將為全部 {row_count} 個檔案依序分配）：",
            text=default_start,
        )
        if not ok or not start_ip.strip():
            return

        if not validate_ipv4(start_ip.strip()):
            QMessageBox.warning(self, "IP 格式錯誤", f"「{start_ip}」不是有效的 IPv4 位址！")
            return

        ips = generate_sequential_ips(start_ip.strip(), row_count)
        self._updating_table = True
        try:
            for r, ip_val in enumerate(ips):
                item = self.table.item(r, self.COL_SOURCE_IP)
                if item:
                    item.setText(ip_val)
                    item.setBackground(QColor(0, 0, 0, 0))
        finally:
            self._updating_table = False

        self._append_console_log(
            f"已自動分配來源 IP：{ips[0]} ~ {ips[-1]}（共 {len(ips)} 個不同來源 IP）"
        )

    def _on_toggle_select_all(self) -> None:
        row_count = self.table.rowCount()
        if row_count == 0:
            return
        all_checked = all(
            self.table.item(r, self.COL_ENABLED).checkState() == Qt.CheckState.Checked
            for r in range(row_count)
            if self.table.item(r, self.COL_ENABLED) is not None
        )
        new_state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        for r in range(row_count):
            item = self.table.item(r, self.COL_ENABLED)
            if item:
                item.setCheckState(new_state)

    def _on_remove_selected(self) -> None:
        if self.workers:
            QMessageBox.warning(self, "執行中", "請先停止正在發送的任務後再移除項目。")
            return
        selected_rows = sorted(
            {idx.row() for idx in self.table.selectedIndexes()}, reverse=True
        )
        if not selected_rows:
            QMessageBox.information(self, "提示", "請先在表格中點選要移除的列。")
            return
        for r in selected_rows:
            self.table.removeRow(r)

    def _on_table_selection_changed(self) -> None:
        selected = self.table.selectedIndexes()
        if not selected:
            return
        row = selected[0].row()
        file_path = self._get_cell_text(row, self.COL_FILE_PATH)
        sys_name = self._get_cell_text(row, self.COL_SYS_NAME)
        lines = preview_file_lines(file_path, max_lines=100)
        self.lbl_preview_title.setText(
            f"預覽系統 [{sys_name}] — 檔案：{file_path}（顯示前 {len(lines)} 行）"
        )
        self.txt_preview.setPlainText("\n".join(lines))

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table:
            return
        if item.column() == self.COL_SOURCE_IP:
            ip_val = item.text().strip()
            if not validate_ipv4(ip_val):
                item.setBackground(QColor("#fecaca"))
                item.setToolTip("無效的 IPv4 位址格式")
            else:
                item.setBackground(QColor(0, 0, 0, 0))
                item.setToolTip("")

    def _apply_profile_to_ui(self, profile: ProjectProfile) -> None:
        g = profile.global_config
        self.edit_target_ip.setText(g.target_ip)
        self.spin_default_port.setValue(g.default_port)
        self.spin_repeat.setValue(g.default_repeat_count)
        self.chk_infinite.setChecked(g.infinite_loop)
        self.spin_loop_interval.setValue(g.loop_interval_sec)
        self.spin_eps.setValue(g.default_eps)
        self.chk_prepend_header.setChecked(g.prepend_syslog_header)
        if g.bind_interface:
            if_idx = self.combo_interface.findData(g.bind_interface)
            if if_idx >= 0:
                self.combo_interface.setCurrentIndex(if_idx)

        idx = self.combo_send_mode.findData(g.send_mode)
        if idx >= 0:
            self.combo_send_mode.setCurrentIndex(idx)

        self.table.setRowCount(0)
        for task in profile.tasks:
            self._add_task_row(task)

        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _on_save_profile_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "儲存設定檔 (JSON Profile)",
            str(self.project_dir / "my_log_profile.json"),
            "JSON Profile (*.json)",
        )
        if not path:
            return
        profile = ProjectProfile(
            global_config=self._collect_global_config(),
            tasks=[self._collect_row_task_config(r) for r in range(self.table.rowCount())],
        )
        try:
            profile.save_to_file(path)
            self._append_console_log(f"設定檔已儲存至：{path}")
        except Exception as exc:
            QMessageBox.critical(self, "儲存失敗", f"無法儲存設定檔：{exc}")

    def _on_load_profile_dialog(self) -> None:
        if self.workers:
            QMessageBox.warning(self, "執行中", "請先停止目前發送任務再載入設定檔。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "載入設定檔 (JSON Profile)",
            str(self.project_dir),
            "JSON Profile (*.json)",
        )
        if not path:
            return
        try:
            profile = ProjectProfile.load_from_file(path, base_dir=Path(path).parent)
            self._apply_profile_to_ui(profile)
            self._append_console_log(f"已載入設定檔：{path}（共 {len(profile.tasks)} 個任務）")
        except Exception as exc:
            QMessageBox.critical(self, "載入失敗", f"無法讀取設定檔：{exc}")

    def _validate_before_start(self) -> Optional[List[tuple[int, LogTaskConfig]]]:
        global_cfg = self._collect_global_config()
        if not validate_ipv4(global_cfg.target_ip):
            QMessageBox.warning(
                self, "目標 IP 錯誤", f"全域目標 IP「{global_cfg.target_ip}」不是有效的 IPv4 位址！"
            )
            return None

        enabled_tasks: List[tuple[int, LogTaskConfig]] = []
        seen_source_ips: Dict[str, str] = {}

        for r in range(self.table.rowCount()):
            task = self._collect_row_task_config(r)
            if not task.enabled:
                continue
            if not validate_ipv4(task.source_ip):
                QMessageBox.warning(
                    self,
                    "來源 IP 錯誤",
                    f"第 {r + 1} 列 [{task.system_name}] 的模擬來源 IP「{task.source_ip}」格式不正確！",
                )
                return None
            if not os.path.isfile(task.file_path):
                QMessageBox.warning(
                    self,
                    "檔案不存在",
                    f"第 {r + 1} 列 [{task.system_name}] 的檔案不存在：\n{task.file_path}",
                )
                return None

            if task.source_ip in seen_source_ips:
                prev_sys = seen_source_ips[task.source_ip]
                ans = QMessageBox.question(
                    self,
                    "來源 IP 重複提醒",
                    f"第 {r + 1} 列 [{task.system_name}] 與 [{prev_sys}] 使用了相同的模擬來源 IP ({task.source_ip})。\n\n"
                    f"您確定要繼續發送嗎？（也可點選「否」並使用「🔢 批次自動遞增分配來源 IP」按鈕）",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return None
            seen_source_ips[task.source_ip] = task.system_name
            enabled_tasks.append((r, task))

        if not enabled_tasks:
            QMessageBox.information(self, "無啟用任務", "請至少勾選一個要發送的 Raw Log 任務！")
            return None

        return enabled_tasks

    def _on_start_all(self) -> None:
        if self.workers:
            return

        validated = self._validate_before_start()
        if not validated:
            return

        global_cfg = self._collect_global_config()
        if global_cfg.send_mode == "raw_spoof" and not has_raw_socket_privilege():
            QMessageBox.critical(
                self,
                "需要 Sudo / Root 權限",
                "您選擇了「強制 Raw Socket 偽造來源 IP」，但目前未以 sudo / root 權限執行程式。\n\n"
                "請使用終端機執行 sudo ./main.py 或 ./run_sudo.sh 啟動，或將發送模式改為「自動偵測 / 一般 UDP Socket」。",
            )
            return

        # Automatically mount missing IP aliases when in ip_alias_bind mode (or auto mode with sudo)
        if global_cfg.send_mode in ("ip_alias_bind", "auto"):
            iface = global_cfg.bind_interface or "en0"
            missing_ips = [
                t.source_ip
                for _, t in validated
                if not self.alias_manager.is_ip_local(t.source_ip)
            ]
            if missing_ips:
                if self.alias_manager.is_root():
                    for ip_str in missing_ips:
                        ok, msg = self.alias_manager.add_ip_alias(iface, ip_str)
                        self._append_console_log(f"[自動掛載虛擬副 IP] {msg}")
                        if not ok and global_cfg.send_mode == "ip_alias_bind":
                            QMessageBox.critical(self, "掛載虛擬副 IP 失敗", msg)
                            return
                    self._refresh_interfaces_combo()
                elif global_cfg.send_mode == "ip_alias_bind":
                    QMessageBox.critical(
                        self,
                        "需要 Sudo 權限掛載虛擬副 IP",
                        f"清單中有 {len(missing_ips)} 個模擬來源 IP 尚未掛載於本機網卡：\n"
                        f"{', '.join(missing_ips[:5])}\n\n"
                        f"請使用 sudo ./main.py (或 ./run_sudo.sh) 啟動，即可自動將這些 IP 掛載為網卡虛擬副 IP 並 100% 穩定送達 Log Server！",
                    )
                    return

        self.row_sent_counts.clear()
        self.is_paused = False
        self.btn_pause_all.setText("⏸ 暫停全部")
        self._set_running_ui_state(True)

        self._append_console_log(
            f"========== 啟動同時發送任務 (共 {len(validated)} 個 Raw Log 檔案並發執行) =========="
        )

        for row_idx, task_cfg in validated:
            pbar = self.table.cellWidget(row_idx, self.COL_PROGRESS)
            if isinstance(pbar, QProgressBar):
                pbar.setValue(0)
            stats_item = self.table.item(row_idx, self.COL_ROUND_STATS)
            if stats_item:
                stats_item.setText("準備中...")

            worker = LogTaskWorker(row_idx, task_cfg, global_cfg, self)
            worker.progress_updated.connect(self._on_worker_progress)
            worker.status_changed.connect(self._on_worker_status)
            worker.log_message.connect(self._append_console_log)
            worker.task_finished.connect(self._on_worker_finished)
            self.workers[row_idx] = worker
            self.row_sent_counts[row_idx] = 0
            worker.start()

        self._update_summary_label()

    def _on_pause_resume_all(self) -> None:
        if not self.workers:
            return
        self.is_paused = not self.is_paused
        for w in self.workers.values():
            w.set_paused(self.is_paused)
        if self.is_paused:
            self.btn_pause_all.setText("▶ 繼續發送")
            self._append_console_log("⏸ 已暫停所有發送任務。")
        else:
            self.btn_pause_all.setText("⏸ 暫停全部")
            self._append_console_log("▶ 已恢復所有發送任務。")

    def _on_stop_all(self) -> None:
        if not self.workers:
            return
        self._append_console_log("⏹ 正在停止所有發送任務...")
        for w in list(self.workers.values()):
            w.request_stop()

    def _on_worker_progress(
        self,
        row: int,
        sent_in_round: int,
        total_lines: int,
        total_sent_all_rounds: int,
        current_round: int,
        total_rounds: int,
        progress_pct: int,
    ) -> None:
        self.row_sent_counts[row] = total_sent_all_rounds
        round_str = f"第 {current_round}/{'∞' if total_rounds == 0 else total_rounds} 輪 ({total_sent_all_rounds} 筆)"
        stats_item = self.table.item(row, self.COL_ROUND_STATS)
        if stats_item:
            stats_item.setText(round_str)

        pbar = self.table.cellWidget(row, self.COL_PROGRESS)
        if isinstance(pbar, QProgressBar):
            pbar.setValue(progress_pct)

        self._update_summary_label()

    def _on_worker_status(self, row: int, status_text: str, is_error: bool) -> None:
        status_item = self.table.item(row, self.COL_STATUS)
        if status_item:
            status_item.setText(status_text)
            if is_error:
                status_item.setForeground(QColor("#dc2626"))
            elif "完成" in status_text:
                status_item.setForeground(QColor("#16a34a"))
            elif "發送中" in status_text:
                status_item.setForeground(QColor("#2563eb"))
            else:
                status_item.setForeground(QColor("#4b5563"))

    def _on_worker_finished(self, row: int, success: bool, total_sent: int) -> None:
        self.row_sent_counts[row] = total_sent
        worker = self.workers.pop(row, None)
        if worker is not None:
            worker.deleteLater()

        self._update_summary_label()
        if not self.workers:
            self._set_running_ui_state(False)
            total_all = sum(self.row_sent_counts.values())
            self._append_console_log(
                f"========== 所有任務已結束（本次總計發送 {total_all} 筆 UDP Log 封包） =========="
            )

    def _update_summary_label(self) -> None:
        active = len(self.workers)
        total_pkts = sum(self.row_sent_counts.values())
        self.lbl_summary.setText(
            f"執行中並發任務: {active} | 本次累計已發送封包: {total_pkts}"
        )

    def _set_running_ui_state(self, running: bool) -> None:
        self.btn_start_all.setEnabled(not running)
        self.btn_pause_all.setEnabled(running)
        self.btn_stop_all.setEnabled(running)
        self.btn_add_files.setEnabled(not running)
        self.btn_add_folder.setEnabled(not running)
        self.btn_batch_ip.setEnabled(not running)
        self.btn_remove_selected.setEnabled(not running)
        self.btn_load_profile.setEnabled(not running)

    # --- Local UDP Test Receiver methods ---
    def _on_toggle_receiver(self) -> None:
        if self.receiver_thread is not None and self.receiver_thread.isRunning():
            self.receiver_thread.stop_listening()
            self.receiver_thread.wait(1000)
            self.receiver_thread = None
            self.btn_toggle_recv.setText("📡 啟動本機 UDP 測試接收器")
            return

        port = self.spin_recv_port.value()
        self.receiver_thread = LocalUdpReceiverThread(bind_ip="0.0.0.0", port=port, parent=self)
        self.receiver_thread.packet_received.connect(self._on_local_packet_received)
        self.receiver_thread.listener_status.connect(self._on_receiver_status)
        self.receiver_thread.start()

    def _on_receiver_status(self, is_running: bool, msg: str) -> None:
        self.lbl_recv_status.setText(f"狀態：{msg}")
        if is_running:
            self.btn_toggle_recv.setText("⏹ 停止本機 UDP 接收器")
            self.spin_recv_port.setEnabled(False)
        else:
            self.btn_toggle_recv.setText("📡 啟動本機 UDP 測試接收器")
            self.spin_recv_port.setEnabled(True)

    def _on_local_packet_received(self, src_ip: str, src_port: int, payload_text: str) -> None:
        self.received_count += 1
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.txt_receiver.appendPlainText(
            f"[#{self.received_count} | {ts} | 來自 {src_ip}:{src_port}] {payload_text}"
        )

    def _on_clear_receiver_log(self) -> None:
        self.received_count = 0
        self.txt_receiver.clear()

    def _on_apply_local_receiver_target(self) -> None:
        port = self.spin_recv_port.value()
        self.edit_target_ip.setText("127.0.0.1")
        self.spin_default_port.setValue(port)
        if self.receiver_thread is None or not self.receiver_thread.isRunning():
            self._on_toggle_receiver()
        self._append_console_log(
            f"已將全域目標設為本機測試接收器 127.0.0.1:{port}，可直接點擊「▶ 開始同時發送」觀看接收結果！"
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        for w in list(self.workers.values()):
            w.request_stop()
            w.wait(500)
        if self.receiver_thread is not None and self.receiver_thread.isRunning():
            self.receiver_thread.stop_listening()
            self.receiver_thread.wait(500)
        if self.chk_auto_cleanup_aliases.isChecked() and self.alias_manager.mounted_aliases:
            self.alias_manager.remove_all_mounted()
        super().closeEvent(event)
