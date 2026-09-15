"""Provider-specific network routing helpers.

The small server uses a transparent sing-box TUN for ordinary public traffic.
DeepInfra is reachable from the physical WLAN path but can be reset by the
selected VPN egress, so its requests may bind to the configured WLAN address.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from urllib.parse import urlsplit


def _interface_ipv4(interface: str) -> str | None:
    """Read one interface address without invoking a shell."""
    if os.name != "posix" or not interface:
        return None
    try:
        import fcntl

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            request = struct.pack("256s", interface[:15].encode("utf-8"))
            result = fcntl.ioctl(sock.fileno(), 0x8915, request)
        address = socket.inet_ntoa(result[20:24])
        parsed = ipaddress.ip_address(address)
        return address if parsed.version == 4 and not parsed.is_loopback else None
    except (OSError, ValueError, ImportError):
        return None


def direct_local_addr(base_url: str) -> tuple[str, int] | None:
    """Return the physical source address for DeepInfra, if configured."""
    host = (urlsplit(str(base_url or "")).hostname or "").casefold()
    if not (host == "api.deepinfra.com" or host.endswith(".deepinfra.com")):
        return None

    configured = os.environ.get("BROKER_DEEPINFRA_SOURCE_IP", "").strip()
    if configured:
        try:
            parsed = ipaddress.ip_address(configured)
            if parsed.version == 4 and not parsed.is_loopback:
                return configured, 0
        except ValueError:
            pass

    interface = os.environ.get("BROKER_DEEPINFRA_INTERFACE", "wlp3s0").strip()
    address = _interface_ipv4(interface)
    return (address, 0) if address else None
