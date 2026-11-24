import asyncio
import socket
import ssl

import httpx
import pytest

from url_monitor import checker
from url_monitor.guard import BlockedTargetError

PUBLIC_IP = "93.184.215.14"


@pytest.fixture(autouse=True)
def fake_guard(monkeypatch):
    """By default every host resolves to a public IP, without touching the network."""

    async def resolve(host):
        return PUBLIC_IP

    monkeypatch.setattr(checker, "resolve_public_ip", resolve)


def check(url, handler, timeout=5.0):
    """Run check_url with a fake server: `handler` receives each request and returns a response."""
    return asyncio.run(checker.check_url(url, timeout=timeout, transport=httpx.MockTransport(handler)))


def test_records_status_code_and_time():
    result = check("https://example.com/health", lambda request: httpx.Response(200))
    assert result.status_code == 200
    assert result.error is None
    assert result.url == "https://example.com/health"
    assert result.response_time_ms >= 0
    assert result.checked_at.tzinfo is not None


@pytest.mark.parametrize("status", [404, 500, 503])
def test_error_status_codes_are_results_not_errors(status):
    result = check("https://example.com", lambda request: httpx.Response(status))
    assert result.status_code == status
    assert result.error is None


def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(301, headers={"Location": "http://192.168.1.10:8006/"})

    result = check("https://example.com", handler)
    assert result.status_code == 301
    assert len(calls) == 1  # the redirect target was never requested


def test_connects_to_validated_ip_but_presents_original_hostname():
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers["Host"]
        seen["sni"] = request.extensions["sni_hostname"]
        return httpx.Response(200)

    check("https://example.com:8443/path", handler)
    assert seen == {"host": PUBLIC_IP, "host_header": "example.com:8443", "sni": "example.com"}


def test_blocked_host_is_recorded_without_sending_a_request(monkeypatch):
    async def blocked(host):
        raise BlockedTargetError(host)

    monkeypatch.setattr(checker, "resolve_public_ip", blocked)
    result = check("http://192.168.1.10:8006", lambda request: pytest.fail("request was sent"))
    assert result.error == "blocked"
    assert result.status_code is None
    assert result.response_time_ms == 0.0


def test_dns_failure(monkeypatch):
    async def no_such_name(host):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(checker, "resolve_public_ip", no_such_name)
    result = check("https://does-not-exist.example", lambda request: httpx.Response(200))
    assert result.error == "dns_error"
    assert result.response_time_ms == 0.0


def test_slow_server_times_out():
    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    result = check("https://example.com", slow, timeout=0.05)
    assert result.error == "timeout"
    assert 40 <= result.response_time_ms < 1000


def test_httpx_timeout_is_recorded_as_timeout():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    assert check("https://example.com", handler).error == "timeout"


def test_connection_refused():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert check("https://example.com", handler).error == "connection_error"


def test_invalid_certificate_is_a_tls_error():
    def handler(request):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as exc:
            raise httpx.ConnectError("TLS failed", request=request) from exc

    assert check("https://example.com", handler).error == "tls_error"


def test_other_http_errors_are_recorded():
    def handler(request):
        raise httpx.RemoteProtocolError("server sent garbage", request=request)

    assert check("https://example.com", handler).error == "other_error"
