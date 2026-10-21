"""Cross-platform Network Interface IP Alias (Virtual Secondary IP) Manager for macOS & Linux."""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass
from typing import List, Set, Tuple


@dataclass
class NetworkInterfaceInfo:
    """Information about a local network interface and its IPv4 addresses."""

    name: str
    ipv4_addresses: List[str]
    is_loopback: bool = False

    @property
    def display_label(self) -> str:
        ips_str = ", ".join(self.ipv4_addresses) if self.ipv4_addresses else "無 IPv4"
        loop_tag = " [本機迴路]" if self.is_loopback else ""
        return f"{self.name}{loop_tag} ({ips_str})"


class IpAliasManager:
    """
    Manages mounting and unmounting virtual secondary IPs (IP Aliases) on macOS and Linux.
    Tracks all IPs mounted during this session so they can be cleanly removed on demand.
    """

    def __init__(self) -> None:
        self._system = platform.system()  # 'Darwin', 'Linux', 'Windows'
        # Set of (interface_name, ip_str, prefix_len) mounted by this manager
        self.mounted_aliases: Set[Tuple[str, str, int]] = set()

    @staticmethod
    def is_root() -> bool:
        """Check if running with root/sudo privileges."""
        if hasattr(os, "geteuid"):
            return os.geteuid() == 0
        return False

    def list_interfaces(self) -> List[NetworkInterfaceInfo]:
        """List active network interfaces and their current IPv4 addresses."""
        if self._system == "Darwin":
            return self._list_interfaces_macos()
        elif self._system == "Linux":
            return self._list_interfaces_linux()
        else:
            return self._list_interfaces_fallback()

    def get_all_local_ipv4s(self) -> Set[str]:
        """Return a set of all IPv4 addresses currently assigned to any local interface."""
        ips: Set[str] = set()
        for iface in self.list_interfaces():
            for ip in iface.ipv4_addresses:
                ips.add(ip)
        return ips

    def is_ip_local(self, ip_str: str) -> bool:
        """Check whether `ip_str` can already be bound on a local socket."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((ip_str, 0))
            s.close()
            return True
        except OSError:
            return False

    def add_ip_alias(
        self, interface: str, ip_str: str, prefix_len: int = 24
    ) -> Tuple[bool, str]:
        """
        Mount a virtual secondary IP (`ip_str`) onto `interface`.
        If the IP is already bound locally, returns (True, "已存在於本機網卡").
        """
        ip_str = ip_str.strip()
        if self.is_ip_local(ip_str):
            for iface in self.list_interfaces():
                if iface.name == interface and ip_str in iface.ipv4_addresses:
                    if iface.ipv4_addresses and ip_str != iface.ipv4_addresses[0]:
                        self.mounted_aliases.add((interface, ip_str, prefix_len))
            return True, f"IP {ip_str} 已存在於網卡 {interface}，可直接綁定發送"

        if not self.is_root():
            return (
                False,
                f"掛載虛擬副 IP ({ip_str}) 需要 root / sudo 權限，請以 sudo 啟動程式",
            )

        try:
            if self._system == "Darwin":
                netmask = self._prefix_to_netmask(prefix_len)
                cmd = ["ifconfig", interface, "alias", ip_str, "netmask", netmask]
            elif self._system == "Linux":
                cmd = ["ip", "addr", "add", f"{ip_str}/{prefix_len}", "dev", interface]
            elif self._system == "Windows":
                netmask = self._prefix_to_netmask(prefix_len)
                cmd = [
                    "netsh",
                    "interface",
                    "ip",
                    "add",
                    "address",
                    interface,
                    ip_str,
                    netmask,
                ]
            else:
                return False, f"不支援的作業系統: {self._system}"

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout).strip()
                # On Linux, if address already exists, RTNETLINK answers: File exists
                if "File exists" in err:
                    return True, f"IP {ip_str} 已掛載於 {interface}"
                return False, f"指令執行失敗 ({' '.join(cmd)}): {err}"

            self.mounted_aliases.add((interface, ip_str, prefix_len))
            return True, f"已成功在網卡 {interface} 掛載虛擬副 IP: {ip_str}/{prefix_len}"
        except Exception as exc:
            return False, f"掛載虛擬副 IP 發生錯誤: {exc}"

    def remove_ip_alias(
        self, interface: str, ip_str: str, prefix_len: int = 24
    ) -> Tuple[bool, str]:
        """Remove a virtual secondary IP (`ip_str`) from `interface`."""
        ip_str = ip_str.strip()
        if not self.is_root():
            return False, "卸載虛擬副 IP 需要 root / sudo 權限"

        try:
            if self._system == "Darwin":
                cmd = ["ifconfig", interface, "-alias", ip_str]
            elif self._system == "Linux":
                cmd = ["ip", "addr", "del", f"{ip_str}/{prefix_len}", "dev", interface]
            elif self._system == "Windows":
                cmd = [
                    "netsh",
                    "interface",
                    "ip",
                    "delete",
                    "address",
                    interface,
                    ip_str,
                ]
            else:
                return False, f"不支援的作業系統: {self._system}"

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            self.mounted_aliases.discard((interface, ip_str, prefix_len))
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout).strip()
                return False, f"卸載 {ip_str} 失敗: {err}"
            return True, f"已從網卡 {interface} 卸載虛擬副 IP: {ip_str}"
        except Exception as exc:
            return False, f"卸載虛擬副 IP 發生錯誤: {exc}"

    def remove_all_mounted(self) -> List[str]:
        """Unmount all IP aliases mounted by this manager instance."""
        logs: List[str] = []
        for interface, ip_str, prefix_len in sorted(list(self.mounted_aliases)):
            ok, msg = self.remove_ip_alias(interface, ip_str, prefix_len)
            logs.append(msg)
        return logs

    @staticmethod
    def _prefix_to_netmask(prefix_len: int) -> str:
        mask = (0xFFFFFFFF >> (32 - prefix_len)) << (32 - prefix_len)
        return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"

    def _list_interfaces_macos(self) -> List[NetworkInterfaceInfo]:
        result: List[NetworkInterfaceInfo] = []
        try:
            proc = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5
            )
            current_name: str | None = None
            current_ips: List[str] = []
            current_up: bool = False

            for line in proc.stdout.splitlines():
                m_iface = re.match(r"^([a-zA-Z0-9_]+):\s+flags=\d+<([^>]*)>", line)
                if m_iface:
                    if current_name and (current_ips or current_name.startswith("en") or current_name == "lo0"):
                        if current_up or current_ips:
                            result.append(
                                NetworkInterfaceInfo(
                                    name=current_name,
                                    ipv4_addresses=current_ips,
                                    is_loopback=(current_name == "lo0"),
                                )
                            )
                    current_name = m_iface.group(1)
                    flags_str = m_iface.group(2)
                    current_up = "UP" in flags_str.split(",")
                    current_ips = []
                elif current_name is not None:
                    m_inet = re.search(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+", line)
                    if m_inet:
                        current_ips.append(m_inet.group(1))

            if current_name and (current_ips or current_name.startswith("en") or current_name == "lo0"):
                if current_up or current_ips:
                    result.append(
                        NetworkInterfaceInfo(
                            name=current_name,
                            ipv4_addresses=current_ips,
                            is_loopback=(current_name == "lo0"),
                        )
                    )
        except Exception:
            pass

        # Sort so active physical interfaces with IPv4 come first, then lo0
        result.sort(
            key=lambda x: (
                0 if (x.ipv4_addresses and not x.is_loopback) else (1 if x.is_loopback else 2),
                x.name,
            )
        )
        return result or self._list_interfaces_fallback()

    def _list_interfaces_linux(self) -> List[NetworkInterfaceInfo]:
        result: List[NetworkInterfaceInfo] = []
        try:
            proc = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            by_name: dict[str, List[str]] = {}
            for line in proc.stdout.splitlines():
                # Format: 2: eth0    inet 192.168.1.10/24 brd ...
                m = re.match(r"^\d+:\s+([^\s]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    iface_name, ip_addr = m.group(1), m.group(2)
                    by_name.setdefault(iface_name, []).append(ip_addr)
            for name, ips in by_name.items():
                result.append(
                    NetworkInterfaceInfo(
                        name=name,
                        ipv4_addresses=ips,
                        is_loopback=(name == "lo"),
                    )
                )
        except Exception:
            pass
        result.sort(key=lambda x: (0 if not x.is_loopback else 1, x.name))
        return result or self._list_interfaces_fallback()

    @staticmethod
    def _list_interfaces_fallback() -> List[NetworkInterfaceInfo]:
        return [
            NetworkInterfaceInfo(name="en0", ipv4_addresses=[], is_loopback=False),
            NetworkInterfaceInfo(name="lo0", ipv4_addresses=["127.0.0.1"], is_loopback=True),
        ]
