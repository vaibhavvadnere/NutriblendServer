"""
utils/network.py — Find this machine's address on the local network.

Used by /api/health in development so you can see which base URL a phone on
the same Wi-Fi should use (the Mac's LAN IP changes between networks).
"""

import socket
from typing import Optional


def lan_ip() -> Optional[str]:
    """This machine's LAN IP (e.g. 192.168.1.5), or None if there is no network.

    Opening a UDP socket towards a public address makes the OS pick the
    outgoing interface; no packet is actually sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip
