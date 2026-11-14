"""URL guard for the agent's browser: only public web addresses. The host firewall is the second line."""

import ipaddress
import socket
from urllib.parse import urlsplit


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


# Publicly routable-looking range that is the homelab's own container network (see Ansible host_vars).
_EXTRA_BLOCKED = (ipaddress.ip_network("172.60.0.0/16"),)


def _is_global(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _EXTRA_BLOCKED):
        return False
    return ip.is_global and not ip.is_multicast


def is_public_url(url: str, resolve=None) -> bool:
    """http(s) only, no credentials in the URL, and every address the name resolves to must be global."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    try:
        addresses = (resolve or _resolve)(host)
        return bool(addresses) and all(_is_global(a) for a in addresses)
    except (OSError, ValueError):
        return False
