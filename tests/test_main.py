import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from url_monitor.config import UrlListError
from url_monitor.main import create_app
from url_monitor.models import CheckResult
from url_monitor.storage import Storage

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


async def never_finishes(url):
    """A check that never completes, so the worker adds nothing during the test."""
    await asyncio.Event().wait()


@pytest.fixture
def urls_file(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("https://b.example\nhttps://a.example\n")
    return path


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "monitor.db"


def save(db_path, *results):
    """Put results in the database before the app starts."""
    storage = Storage(db_path)
    for result in results:
        storage.save(result)
    storage.close()


def client_for(urls_file, db_path, check=never_finishes, interval=60.0):
    # Used in `with`: entering runs the lifespan startup, leaving runs the shutdown.
    return TestClient(create_app(urls_file, db_path, check=check, interval=interval))


def test_status_returns_recent_results_per_url(urls_file, db_path):
    save(
        db_path,
        CheckResult(url="https://a.example", checked_at=NOW, response_time_ms=111.34, status_code=200),
        CheckResult(url="https://a.example", checked_at=NOW + timedelta(minutes=1), response_time_ms=0.0, error="dns_error"),
    )
    with client_for(urls_file, db_path) as client:
        response = client.get("/status")

    assert response.status_code == 200
    assert response.json() == {
        "urls": [
            {"url": "https://b.example", "checks": []},  # not checked yet; file order is kept
            {
                "url": "https://a.example",
                "checks": [  # newest first
                    {"checked_at": "2026-11-24T12:01:00.000000+00:00", "status_code": None, "error": "dns_error", "response_time_ms": 0.0},
                    {"checked_at": "2026-11-24T12:00:00.000000+00:00", "status_code": 200, "error": None, "response_time_ms": 111.3},
                ],
            },
        ]
    }


def test_urls_removed_from_the_file_are_not_shown(urls_file, db_path):
    save(db_path, CheckResult(url="https://removed.example", checked_at=NOW, response_time_ms=1.0, status_code=200))
    with client_for(urls_file, db_path) as client:
        urls = [entry["url"] for entry in client.get("/status").json()["urls"]]
    assert urls == ["https://b.example", "https://a.example"]


def test_limit_controls_results_per_url(urls_file, db_path):
    save(
        db_path,
        *(
            CheckResult(url="https://a.example", checked_at=NOW + timedelta(minutes=m), response_time_ms=1.0, status_code=200)
            for m in range(15)
        ),
    )
    with client_for(urls_file, db_path) as client:
        default = client.get("/status").json()["urls"][1]["checks"]
        three = client.get("/status?limit=3").json()["urls"][1]["checks"]
    assert len(default) == 10
    assert len(three) == 3


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc"])
def test_invalid_limit_is_rejected(urls_file, db_path, limit):
    with client_for(urls_file, db_path) as client:
        assert client.get(f"/status?limit={limit}").status_code == 422


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_pages_are_disabled(urls_file, db_path, path):
    with client_for(urls_file, db_path) as client:
        assert client.get(path).status_code == 404


def test_worker_runs_in_the_background(urls_file, db_path):
    async def instant_check(url):
        return CheckResult(url=url, checked_at=datetime.now(timezone.utc), response_time_ms=5.0, status_code=204)

    with client_for(urls_file, db_path, check=instant_check, interval=0.05) as client:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            entries = client.get("/status").json()["urls"]
            if all(entry["checks"] for entry in entries):
                break
            time.sleep(0.02)
    assert all(entry["checks"][0]["status_code"] == 204 for entry in entries)


def test_invalid_url_file_prevents_startup(tmp_path, db_path):
    bad_file = tmp_path / "urls.txt"
    bad_file.write_text("ftp://not-allowed.example\n")
    with pytest.raises(UrlListError):
        with client_for(bad_file, db_path):
            pass
