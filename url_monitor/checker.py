"""Checking one URL: send the request, time it, and turn the outcome into a CheckResult."""

import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone

import httpx

from url_monitor.guard import BlockedTargetError, resolve_public_ip
from url_monitor.models import CheckResult

DEFAULT_TIMEOUT_SECONDS = 5.0
USER_AGENT = "url-monitor/0.1"


async def check_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,  # tests pass a mock here
) -> CheckResult:
    """Check `url` once. Never raises for network problems: they become an `error`."""
    checked_at = datetime.now(timezone.utc)
    started: float | None = None  # set once DNS is done; DNS time is not measured

    def result(**outcome) -> CheckResult:
        elapsed_ms = 0.0 if started is None else (time.perf_counter() - started) * 1000
        return CheckResult(url=url, checked_at=checked_at, response_time_ms=elapsed_ms, **outcome)

    try:
        async with asyncio.timeout(timeout):  # hard limit for the whole check
            target = httpx.URL(url)
            ip = await resolve_public_ip(target.host)
            started = time.perf_counter()
            status_code = await _send(target, ip, timeout, transport)
        return result(status_code=status_code)
    except BlockedTargetError:
        return result(error="blocked")
    except socket.gaierror:
        return result(error="dns_error")
    except (TimeoutError, httpx.TimeoutException):
        return result(error="timeout")
    except httpx.ConnectError as exc:
        return result(error="tls_error" if _caused_by_tls(exc) else "connection_error")
    except (httpx.HTTPError, OSError):
        return result(error="other_error")


async def _send(target: httpx.URL, ip: str, timeout: float, transport) -> int:
    """Send a GET to `ip` while presenting the original hostname, and return the status code."""
    host_header = f"[{target.host}]" if ":" in target.host else target.host
    if target.port is not None:
        host_header += f":{target.port}"

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,  # a 3xx is recorded as-is; it already proves the server answers
        trust_env=False,  # ignore HTTP(S)_PROXY variables: a proxy would bypass the IP pinning
        transport=transport,
    ) as client:
        request = client.build_request(
            "GET",
            target.copy_with(host=ip),  # connect to the address the guard validated
            headers={"Host": host_header, "User-Agent": USER_AGENT},
            # TLS uses this name for SNI and for verifying the certificate.
            extensions={"sni_hostname": target.host},
        )
        # stream=True returns as soon as the headers arrive; closing the
        # response without reading it means the body is never downloaded.
        response = await client.send(request, stream=True)
        await response.aclose()
        return response.status_code


def _caused_by_tls(exc: BaseException) -> bool:
    """True if an ssl.SSLError (e.g. an invalid certificate) is somewhere in the exception chain."""
    while exc is not None:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False
