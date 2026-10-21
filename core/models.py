"""Data models for Multi-Source Raw Log UDP Sender."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class GlobalConfig:
    """Global default settings for all log sending tasks."""

    target_ip: str = "127.0.0.1"
    default_port: int = 514
    default_repeat_count: int = 1
    infinite_loop: bool = False
    loop_interval_sec: float = 1.0
    default_eps: int = 100  # 0 means unlimited speed
    send_mode: str = "ip_alias_bind"  # "ip_alias_bind", "auto", "raw_spoof", or "standard_udp"
    bind_interface: str = ""  # e.g. "en0" or "eth0" for IP Alias mounting
    auto_cleanup_aliases: bool = True
    prepend_syslog_header: bool = False
    syslog_facility: int = 16  # local0 (16)
    syslog_severity: int = 6  # info (6) -> PRI = 16*8 + 6 = 134


@dataclass
class LogTaskConfig:
    """Configuration for a single Raw Log file task."""

    enabled: bool = True
    system_name: str = ""
    file_path: str = ""
    source_ip: str = "192.168.1.101"
    target_port: Optional[int] = None  # None = inherit GlobalConfig.default_port
    repeat_count: Optional[int] = None  # None = inherit GlobalConfig.default_repeat_count
    eps: Optional[int] = None  # None = inherit GlobalConfig.default_eps

    def get_effective_port(self, global_cfg: GlobalConfig) -> int:
        if self.target_port is not None and self.target_port > 0:
            return self.target_port
        return global_cfg.default_port

    def get_effective_repeat(self, global_cfg: GlobalConfig) -> int:
        if global_cfg.infinite_loop:
            return 0  # 0 indicates infinite loop when global_cfg.infinite_loop is True
        if self.repeat_count is not None and self.repeat_count > 0:
            return self.repeat_count
        return global_cfg.default_repeat_count

    def get_effective_eps(self, global_cfg: GlobalConfig) -> int:
        if self.eps is not None and self.eps >= 0:
            return self.eps
        return global_cfg.default_eps


@dataclass
class ProjectProfile:
    """Complete save/load profile containing global config and task list."""

    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    tasks: List[LogTaskConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "global_config": asdict(self.global_config),
            "tasks": [asdict(t) for t in self.tasks],
        }

    def save_to_file(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_from_file(cls, path: str | Path, base_dir: Optional[Path] = None) -> "ProjectProfile":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        g_data = data.get("global_config", {})
        global_cfg = GlobalConfig(
            target_ip=str(g_data.get("target_ip", "127.0.0.1")),
            default_port=int(g_data.get("default_port", 514)),
            default_repeat_count=int(g_data.get("default_repeat_count", 1)),
            infinite_loop=bool(g_data.get("infinite_loop", False)),
            loop_interval_sec=float(g_data.get("loop_interval_sec", 1.0)),
            default_eps=int(g_data.get("default_eps", 100)),
            send_mode=str(g_data.get("send_mode", "auto")),
            bind_interface=str(g_data.get("bind_interface", "")),
            auto_cleanup_aliases=bool(g_data.get("auto_cleanup_aliases", True)),
            prepend_syslog_header=bool(g_data.get("prepend_syslog_header", False)),
            syslog_facility=int(g_data.get("syslog_facility", 16)),
            syslog_severity=int(g_data.get("syslog_severity", 6)),
        )

        tasks: List[LogTaskConfig] = []
        for t_data in data.get("tasks", []):
            raw_path = str(t_data.get("file_path", ""))
            if base_dir and raw_path and not os.path.isabs(raw_path):
                resolved = (base_dir / raw_path).resolve()
                if resolved.exists():
                    raw_path = str(resolved)
            tasks.append(
                LogTaskConfig(
                    enabled=bool(t_data.get("enabled", True)),
                    system_name=str(t_data.get("system_name", "")),
                    file_path=raw_path,
                    source_ip=str(t_data.get("source_ip", "192.168.1.101")),
                    target_port=t_data.get("target_port"),
                    repeat_count=t_data.get("repeat_count"),
                    eps=t_data.get("eps"),
                )
            )
        return cls(global_config=global_cfg, tasks=tasks)


def validate_ipv4(ip_str: str) -> bool:
    """Return True if ip_str is a valid IPv4 address."""
    try:
        ip = ipaddress.ip_address(ip_str.strip())
        return isinstance(ip, ipaddress.IPv4Address)
    except ValueError:
        return False


def generate_sequential_ips(start_ip: str, count: int) -> List[str]:
    """Generate `count` sequential IPv4 addresses starting from `start_ip`."""
    base = ipaddress.IPv4Address(start_ip.strip())
    result: List[str] = []
    current = int(base)
    for _ in range(count):
        addr = ipaddress.IPv4Address(current)
        # Skip .0 and .255 host addresses when incrementing across subnets if convenient
        last_octet = int(addr) & 0xFF
        if last_octet == 0:
            current += 1
            addr = ipaddress.IPv4Address(current)
        elif last_octet == 255:
            current += 2
            addr = ipaddress.IPv4Address(current)
        result.append(str(addr))
        current += 1
    return result


def count_valid_lines(file_path: str) -> int:
    """Count non-empty lines in a raw log file."""
    if not file_path or not os.path.isfile(file_path):
        return 0
    count = 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def preview_file_lines(file_path: str, max_lines: int = 100) -> List[str]:
    """Read up to max_lines non-empty lines from a raw log file for preview."""
    if not file_path or not os.path.isfile(file_path):
        return [f"[找不到檔案: {file_path}]"]
    lines: List[str] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.rstrip("\r\n")
                if stripped.strip():
                    lines.append(stripped)
                    if len(lines) >= max_lines:
                        break
    except OSError as exc:
        return [f"[讀取檔案錯誤: {exc}]"]
    return lines
