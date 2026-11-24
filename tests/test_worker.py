import asyncio
import logging
from datetime import datetime, timezone

import pytest

from url_monitor.models import CheckResult
from url_monitor.worker import run_forever, run_round

URLS = ["https://a.example", "https://b.example", "https://c.example"]


class FakeStorage:
    """Collects saved results in a list instead of writing to a database."""

    def __init__(self):
        self.saved = []

    def save(self, result):
        self.saved.append(result)


async def ok_check(url):
    return CheckResult(url=url, checked_at=datetime.now(timezone.utc), response_time_ms=1.0, status_code=200)


def run_for(seconds, **kwargs):
    """Run run_forever for `seconds`, then stop it the way the app will: by cancelling it."""

    async def main():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_forever(**kwargs), timeout=seconds)

    asyncio.run(main())


# --- run_round ------------------------------------------------------------


def test_round_checks_and_saves_every_url():
    storage = FakeStorage()
    asyncio.run(run_round(URLS, storage, check=ok_check))
    assert sorted(result.url for result in storage.saved) == URLS


def test_round_never_runs_more_checks_than_the_limit():
    running = 0
    highest = 0

    async def slow_check(url):
        nonlocal running, highest
        running += 1
        highest = max(highest, running)
        await asyncio.sleep(0.01)
        running -= 1
        return await ok_check(url)

    urls = [f"https://{n}.example" for n in range(25)]
    storage = FakeStorage()
    asyncio.run(run_round(urls, storage, concurrency=10, check=slow_check))
    assert highest == 10
    assert len(storage.saved) == 25


def test_bug_in_one_check_is_saved_as_internal_error_and_logged(caplog):
    async def buggy_check(url):
        if url == "https://b.example":
            raise KeyError("oops")
        return await ok_check(url)

    storage = FakeStorage()
    asyncio.run(run_round(URLS, storage, check=buggy_check))

    by_url = {result.url: result for result in storage.saved}
    assert by_url["https://b.example"].error == "internal_error"
    assert by_url["https://a.example"].status_code == 200  # the others still ran
    assert by_url["https://c.example"].status_code == 200
    assert "unexpected error while checking https://b.example" in caplog.text
    assert "KeyError" in caplog.text  # the traceback is in the log


def test_cancellation_is_not_swallowed():
    async def cancelled_check(url):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_round(URLS, FakeStorage(), check=cancelled_check))


# --- run_forever ----------------------------------------------------------


def test_starts_a_new_round_every_interval():
    storage = FakeStorage()
    run_for(0.25, urls=["https://a.example"], storage=storage, interval=0.1, check=ok_check)
    assert len(storage.saved) == 3  # rounds at 0.0s, 0.1s and 0.2s


def test_overrunning_round_logs_a_warning_and_next_round_starts_at_once(caplog):
    async def slow_check(url):
        await asyncio.sleep(0.1)
        return await ok_check(url)

    storage = FakeStorage()
    run_for(0.25, urls=["https://a.example"], storage=storage, interval=0.05, check=slow_check)
    assert "longer than the" in caplog.text
    assert len(storage.saved) == 2  # finished at about 0.1s and 0.2s, with no waiting in between


def test_failed_round_is_logged_and_the_loop_keeps_going(caplog):
    class BrokenStorage:
        def save(self, result):
            raise OSError("disk full")

    calls = []

    async def counting_check(url):
        calls.append(url)
        return await ok_check(url)

    run_for(0.15, urls=["https://a.example"], storage=BrokenStorage(), interval=0.05, check=counting_check)
    assert "check round failed" in caplog.text
    assert len(calls) >= 2  # a second round ran despite the first one failing
