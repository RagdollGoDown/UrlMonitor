"""The worker: check every URL once per interval and save the results."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from url_monitor.checker import check_url
from url_monitor.models import CheckResult
from url_monitor.storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_CONCURRENCY = 10

CheckFunction = Callable[[str], Awaitable[CheckResult]]


async def run_round(
    urls: list[str],
    storage: Storage,
    concurrency: int = DEFAULT_CONCURRENCY,
    check: CheckFunction = check_url,  # tests pass a fake here
) -> None:
    """Check every URL once, at most `concurrency` at a time, and save each result."""
    semaphore = asyncio.Semaphore(concurrency)

    async def check_and_save(url: str) -> None:
        async with semaphore:  # waits here while `concurrency` checks are already running
            result = await _check_safely(url, check)
        storage.save(result)

    await asyncio.gather(*(check_and_save(url) for url in urls))


async def _check_safely(url: str, check: CheckFunction) -> CheckResult:
    """Run one check; a bug in our own code becomes an `internal_error` result."""
    try:
        return await check(url)
    except Exception:  # not BaseException: CancelledError must still stop the task
        logger.exception("unexpected error while checking %s", url)
        return CheckResult(
            url=url,
            checked_at=datetime.now(timezone.utc),
            response_time_ms=0.0,
            error="internal_error",
        )


async def run_forever(
    urls: list[str],
    storage: Storage,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    check: CheckFunction = check_url,
) -> None:
    """Start a round every `interval` seconds until cancelled.

    Rounds never overlap: if one takes longer than the interval, a warning is
    logged and the next round starts immediately.
    """
    loop = asyncio.get_running_loop()
    next_start = loop.time()  # monotonic clock: unaffected by system clock changes

    while True:
        started = loop.time()
        try:
            await run_round(urls, storage, concurrency, check)
        except Exception:  # e.g. the database is unavailable: keep going, retry next round
            logger.exception("check round failed")

        elapsed = loop.time() - started
        next_start += interval  # scheduled from the previous start, so rounds don't drift
        if elapsed > interval:
            logger.warning("round took %.1fs, longer than the %.0fs interval", elapsed, interval)
            next_start = loop.time()
        await asyncio.sleep(max(0.0, next_start - loop.time()))
