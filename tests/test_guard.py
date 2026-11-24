import asyncio
import ipaddress
import socket

import pytest

from url_monitor.guard import BlockedTargetError, is_public, resolve_public_ip


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",  # private
        "172.16.0.1",  # private
        "192.168.1.10",  # private (a typical home LAN, e.g. a Proxmox host)
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local, cloud metadata endpoint
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # unspecified
        "224.0.1.1",  # multicast, which ipaddress counts as global
        "255.255.255.255",  # broadcast
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 private (unique local)
        "ff0e::1",  # IPv6 multicast
        "::ffff:127.0.0.1",  # IPv4 loopback hidden inside IPv6
        "::ffff:192.168.1.10",  # IPv4 private hidden inside IPv6
    ],
)
def test_non_public_addresses_are_rejected(address):
    assert not is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
def test_public_addresses_are_allowed(address):
    assert is_public(ipaddress.ip_address(address))


def fake_dns(monkeypatch, addresses):
    """Make DNS lookups return `addresses`, so tests never touch the network."""

    async def fake_getaddrinfo(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)


def test_hostname_resolving_to_public_address_returns_that_address(monkeypatch):
    fake_dns(monkeypatch, ["93.184.215.14"])
    assert asyncio.run(resolve_public_ip("example.com")) == "93.184.215.14"


def test_hostname_resolving_to_private_address_is_blocked(monkeypatch):
    fake_dns(monkeypatch, ["192.168.1.10"])
    with pytest.raises(BlockedTargetError):
        asyncio.run(resolve_public_ip("innocent-looking.example"))


def test_one_private_record_among_public_ones_is_enough_to_block(monkeypatch):
    fake_dns(monkeypatch, ["93.184.215.14", "127.0.0.1"])
    with pytest.raises(BlockedTargetError):
        asyncio.run(resolve_public_ip("mixed.example"))


def test_ip_literal_is_checked_without_dns(monkeypatch):
    fake_dns(monkeypatch, ["93.184.215.14"])  # would allow it, if it were used
    with pytest.raises(BlockedTargetError):
        asyncio.run(resolve_public_ip("127.0.0.1"))


def test_unknown_hostname_raises_dns_error(monkeypatch):
    async def failing_getaddrinfo(self, host, port, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", failing_getaddrinfo)
    with pytest.raises(socket.gaierror):
        asyncio.run(resolve_public_ip("does-not-exist.example"))
