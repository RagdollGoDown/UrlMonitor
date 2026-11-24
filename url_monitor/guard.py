"""SSRF guard: only allow requests to public internet addresses.

The worker sends requests from inside the network it runs in. Without this
guard, a URL pointing at (or resolving to) 192.168.x.x, 127.0.0.1 or a cloud
metadata address would let it probe private services.
"""

import asyncio
import ipaddress
import socket


class BlockedTargetError(Exception):
    """Raised when a host is, or resolves to, a non-public address."""


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for addresses reachable on the public internet."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is really 127.0.0.1
    # is_global excludes private, loopback, link-local, reserved and
    # documentation ranges, but counts multicast as global, so reject it too.
    return ip.is_global and not ip.is_multicast


async def resolve_public_ip(host: str) -> str:
    """Resolve `host` and return an address that is safe to connect to.

    The caller must connect to the returned address rather than the hostname:
    resolving the name a second time could give a different answer (DNS
    rebinding). Raises BlockedTargetError if any address is not public, and
    socket.gaierror if the name does not resolve.
    """
    try:
        candidates = [ipaddress.ip_address(host)]  # host is already an IP
    except ValueError:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        candidates = [ipaddress.ip_address(info[4][0]) for info in infos]

    # Block if *any* address is private: otherwise a name with one public and
    # one private record could still be used to reach the private one.
    for ip in candidates:
        if not is_public(ip):
            raise BlockedTargetError(f"{host} resolves to a non-public address")
    return str(candidates[0])
