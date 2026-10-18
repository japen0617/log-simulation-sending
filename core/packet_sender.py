"""Cross-platform UDP & Raw Socket IPv4/UDP packet sender with Source IP spoofing."""

from __future__ import annotations

import os
import platform
import random
import socket
import struct
from datetime import datetime
from typing import Optional


def has_raw_socket_privilege() -> bool:
    """Check whether the current process has privileges to open an IPv4 Raw Socket."""
    # On POSIX (macOS/Linux), root uid is 0, or CAP_NET_RAW on Linux
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
        s.close()
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def compute_checksum(data: bytes) -> int:
    """Compute the RFC 1071 16-bit one's complement internet checksum."""
    if len(data) % 2 == 1:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_spoofed_ipv4_udp_packet(
    src_ip: str,
    dst_ip: str,
    src_port: int,
    dst_port: int,
    payload: bytes,
    ip_id: Optional[int] = None,
    bsd_host_order_len: bool = False,
) -> bytes:
    """
    Construct a raw IPv4 + UDP packet with a custom (spoofed) Source IP.

    Note on macOS / BSD vs Linux:
    On macOS (Darwin) and FreeBSD, when IP_HDRINCL is enabled on a raw socket,
    the kernel expects `ip_len` and `ip_off` in the IPv4 header to be in
    HOST byte order (prior to macOS 11 or depending on socket call), OR let's
    handle Darwin's behavior cleanly. Actually, on modern macOS (Darwin), `sendto`
    on `SOCK_RAW` with `IP_HDRINCL` historically requires `ip_len` and `ip_off` in
    host byte order (`struct.pack` with `=H` for those two fields).
    We support automatic retry/detection if `EINVAL` (Errno 22) occurs on macOS!
    """
    if ip_id is None:
        ip_id = random.randint(1, 65535)

    # Truncate UDP payload if it exceeds safe single IPv4 packet size (65507 bytes)
    if len(payload) > 65000:
        payload = payload[:65000]

    udp_length = 8 + len(payload)
    total_length = 20 + udp_length

    src_ip_bytes = socket.inet_aton(src_ip)
    dst_ip_bytes = socket.inet_aton(dst_ip)

    # 1. Build UDP header with checksum
    # Pseudo-header for UDP checksum: src_ip (4B), dst_ip (4B), zero (1B), proto (1B), udp_len (2B)
    pseudo_header = struct.pack(
        "!4s4sBBH",
        src_ip_bytes,
        dst_ip_bytes,
        0,
        socket.IPPROTO_UDP,
        udp_length,
    )
    udp_header_zero_csum = struct.pack("!HHHH", src_port, dst_port, udp_length, 0)
    udp_csum = compute_checksum(pseudo_header + udp_header_zero_csum + payload)
    if udp_csum == 0:
        udp_csum = 0xFFFF  # In UDP, calculated checksum 0 is transmitted as 0xFFFF
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_length, udp_csum)

    # 2. Build IPv4 header (20 bytes, no options)
    ver_ihl = (4 << 4) | 5  # IPv4, IHL = 5 (20 bytes)
    tos = 0
    flags_frag = 0
    ttl = 64
    proto = socket.IPPROTO_UDP

    if bsd_host_order_len:
        # BSD / macOS Raw Socket format where ip_len and ip_off are in host byte order
        ip_header_without_csum = (
            struct.pack("!BB", ver_ihl, tos)
            + struct.pack("=H", total_length)
            + struct.pack("!H", ip_id)
            + struct.pack("=H", flags_frag)
            + struct.pack("!BBH4s4s", ttl, proto, 0, src_ip_bytes, dst_ip_bytes)
        )
        ip_csum = compute_checksum(ip_header_without_csum)
        ip_header = (
            struct.pack("!BB", ver_ihl, tos)
            + struct.pack("=H", total_length)
            + struct.pack("!H", ip_id)
            + struct.pack("=H", flags_frag)
            + struct.pack("!BBH4s4s", ttl, proto, ip_csum, src_ip_bytes, dst_ip_bytes)
        )
    else:
        # Standard Network Byte Order (Linux / RFC 791)
        ip_header_without_csum = struct.pack(
            "!BBHHHBBH4s4s",
            ver_ihl,
            tos,
            total_length,
            ip_id,
            flags_frag,
            ttl,
            proto,
            0,
            src_ip_bytes,
            dst_ip_bytes,
        )
        ip_csum = compute_checksum(ip_header_without_csum)
        ip_header = struct.pack(
            "!BBHHHBBH4s4s",
            ver_ihl,
            tos,
            total_length,
            ip_id,
            flags_frag,
            ttl,
            proto,
            ip_csum,
            src_ip_bytes,
            dst_ip_bytes,
        )

    return ip_header + udp_header + payload


def format_syslog_payload(
    raw_line: str,
    source_ip: str,
    prepend_header: bool = False,
    facility: int = 16,
    severity: int = 6,
) -> bytes:
    """Format a raw log line into UDP payload bytes, optionally prepending RFC3164 Syslog header."""
    clean_line = raw_line.rstrip("\r\n")
    if prepend_header:
        pri = (facility * 8) + severity
        timestamp = datetime.now().strftime("%b %d %H:%M:%S")
        # If the line already starts with <PRI>, replace or prepend cleanly
        formatted = f"<{pri}>{timestamp} {source_ip} {clean_line}"
        return formatted.encode("utf-8", errors="replace")
    return clean_line.encode("utf-8", errors="replace")


class PacketTransmitter:
    """
    Handles UDP packet transmission supporting three modes:
    1. IP Alias Bound UDP Socket (`ip_alias_bind`): Standard UDP socket explicitly bound to
       `source_ip` (`sock.bind((source_ip, 0))`). Works 100% natively with OS routing & ARP
       once `source_ip` is mounted as a secondary IP alias on the network interface.
    2. Raw Socket Source IP Spoofing (`raw_spoof`): Constructs custom IPv4 + UDP headers.
    3. Standard UDP Socket fallback (`standard_udp`): Unbound UDP socket using default local IP.
    """

    def __init__(self, requested_mode: str = "auto", source_ip: Optional[str] = None) -> None:
        """
        requested_mode:
          - "ip_alias_bind": Bind UDP socket directly to `source_ip` (requires `source_ip` on local interface/alias)
          - "auto": Prefer binding to `source_ip` if present on machine; else Raw Socket Spoofing if root; else standard UDP
          - "raw_spoof": Force Raw Socket Spoofing (raises PermissionError if no root/admin)
          - "standard_udp": Use unbound standard UDP socket (uses machine default IP)
        """
        self.requested_mode = requested_mode
        self.source_ip = source_ip.strip() if source_ip else None
        self.active_mode: str = "standard_udp"
        self._raw_sock: Optional[socket.socket] = None
        self._std_sock: Optional[socket.socket] = None
        self._is_darwin = platform.system() == "Darwin"
        self._bsd_host_order = self._is_darwin
        self._ephemeral_port = random.randint(40000, 60000)
        self._open_sockets()

    def _try_bind_source_ip(self, src_ip: str) -> Optional[socket.socket]:
        """Try creating a standard UDP socket bound to `src_ip`."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            sock.bind((src_ip, 0))
            return sock
        except OSError:
            return None

    def _open_sockets(self) -> None:
        # 1. Explicit IP Alias Bind mode
        if self.requested_mode == "ip_alias_bind":
            if not self.source_ip:
                raise ValueError("IP Alias 綁定模式需要指定來源 IP (source_ip)")
            bound_sock = self._try_bind_source_ip(self.source_ip)
            if bound_sock is not None:
                self._std_sock = bound_sock
                self.active_mode = "ip_alias_bind"
                return
            raise OSError(
                f"無法綁定來源 IP {self.source_ip}（該 IP 尚未掛載於本機網卡）。"
                f"請先點擊「🔗 一鍵掛載虛擬副 IP」或以 sudo 權限執行讓系統自動掛載。"
            )

        # 2. Auto mode: First check if source_ip is already mounted on a local interface/alias!
        if self.requested_mode == "auto" and self.source_ip:
            bound_sock = self._try_bind_source_ip(self.source_ip)
            if bound_sock is not None:
                self._std_sock = bound_sock
                self.active_mode = "ip_alias_bind"
                return

        # 3. Raw Socket Spoofing mode (or Auto fallback when source_ip is not mounted locally)
        if self.requested_mode in ("auto", "raw_spoof"):
            try:
                raw_sock = socket.socket(
                    socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP
                )
                raw_sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
                raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                self._raw_sock = raw_sock
                self.active_mode = "raw_spoof"
                return
            except PermissionError:
                if self.requested_mode == "raw_spoof":
                    raise PermissionError(
                        "Raw Socket 來源 IP 偽造模式需要 root / sudo 權限！請使用 sudo 啟動程式，或切換為「自動 / 一般 UDP 模式」。"
                    )
            except OSError as exc:
                if self.requested_mode == "raw_spoof":
                    raise OSError(f"無法建立 Raw Socket: {exc}") from exc

        # 4. Fallback or explicit standard unbound UDP socket
        std_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        std_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        self._std_sock = std_sock
        self.active_mode = "standard_udp"

    def send_log(
        self,
        source_ip: str,
        target_ip: str,
        target_port: int,
        payload: bytes,
    ) -> None:
        """Send a single UDP log packet."""
        if self.active_mode == "ip_alias_bind" and self._std_sock is not None:
            self._std_sock.sendto(payload, (target_ip, target_port))
            return

        if self.active_mode == "raw_spoof" and self._raw_sock is not None:
            pkt = build_spoofed_ipv4_udp_packet(
                src_ip=source_ip,
                dst_ip=target_ip,
                src_port=self._ephemeral_port,
                dst_port=target_port,
                payload=payload,
                bsd_host_order_len=self._bsd_host_order,
            )
            try:
                self._raw_sock.sendto(pkt, (target_ip, target_port))
            except OSError as exc:
                if exc.errno == 22 and self._is_darwin:
                    self._bsd_host_order = not self._bsd_host_order
                    pkt_alt = build_spoofed_ipv4_udp_packet(
                        src_ip=source_ip,
                        dst_ip=target_ip,
                        src_port=self._ephemeral_port,
                        dst_port=target_port,
                        payload=payload,
                        bsd_host_order_len=self._bsd_host_order,
                    )
                    try:
                        self._raw_sock.sendto(pkt_alt, (target_ip, target_port))
                        return
                    except OSError:
                        pass
                if target_ip.startswith("127.") or target_ip == "localhost":
                    if self._std_sock is None:
                        self._std_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._std_sock.sendto(payload, (target_ip, target_port))
                else:
                    raise
        elif self._std_sock is not None:
            self._std_sock.sendto(payload, (target_ip, target_port))

    def close(self) -> None:
        if self._raw_sock is not None:
            try:
                self._raw_sock.close()
            except OSError:
                pass
            self._raw_sock = None
        if self._std_sock is not None:
            try:
                self._std_sock.close()
            except OSError:
                pass
            self._std_sock = None
