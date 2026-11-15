"""URL guard for the agent's browser: only public web addresses. The host firewall is the second line."""

import ipaddress
import os
import socket
from urllib.parse import urlsplit

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
# IPv6 transition ranges that tunnel to an IPv4 host we cannot judge from here (6to4, Teredo): refused outright.
_TUNNELS = (ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))


def _parse_blocked_nets(value: str) -> tuple:
    nets = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry))
        except ValueError:
            raise ValueError(f"BLOCKED_NETS: {entry!r} is not a network in CIDR notation") from None
    return tuple(nets)


# Site-specific networks that look public but must not be reached (for example the homelab's own container
# network). Comma-separated CIDRs, read once at start-up; an invalid entry stops the start.
BLOCKED_NETS = _parse_blocked_nets(os.environ.get("BLOCKED_NETS", ""))


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address inside ::ffff:a.b.c.d, ::a.b.c.d and 64:ff9b::a.b.c.d (NAT64); None for other IPv6."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or (ip in _IPV4_COMPATIBLE and int(ip) > 1):    # :: and ::1 stay IPv6 (both not global)
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_global(address: str, blocked: tuple) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _TUNNELS):
            return False
        ip = _embedded_ipv4(ip) or ip
    if any(ip.version == net.version and ip in net for net in blocked):
        return False
    return ip.is_global and not ip.is_multicast


def public_addresses(url: str, resolve=None, blocked=None) -> list[str]:
    """The addresses the URL's host resolves to if the URL passes every rule, otherwise an empty list.

    Rules: http(s) only, no credentials in the URL, and every address the name resolves to must be global and
    outside blocked (an iterable of networks; default BLOCKED_NETS). Callers connect only to the returned
    addresses and never resolve the name again: a second lookup could answer differently (DNS rebinding).
    """
    blocked = BLOCKED_NETS if blocked is None else tuple(blocked)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return []
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return []
    try:
        addresses = list((resolve or _resolve)(host))
        if addresses and all(_is_global(a, blocked) for a in addresses):
            return addresses
    except (OSError, ValueError):
        pass
    return []


def is_public_url(url: str, resolve=None, blocked=None) -> bool:
    return bool(public_addresses(url, resolve, blocked))
