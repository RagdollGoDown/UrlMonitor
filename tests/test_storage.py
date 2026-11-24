import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from url_monitor.models import CheckResult
from url_monitor.storage import Storage, to_iso

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "monitor.db"


@pytest.fixture
def storage(db_path):
    """A Storage on a fresh database file, closed after the test."""
    store = Storage(db_path)
    yield store
    store.close()


def rows(db_path):
    """Read the table back with a separate connection, as an outside observer would."""
    # closing() closes the connection; `with conn:` alone only ends the transaction.
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT url, checked_at, response_time_ms, status_code, error FROM checks ORDER BY id"
        ).fetchall()


def result(checked_at=NOW, **outcome):
    outcome = outcome or {"status_code": 200}
    return CheckResult(url="https://example.com", checked_at=checked_at, response_time_ms=42.0, **outcome)


# --- to_iso ---------------------------------------------------------------


def test_to_iso_always_includes_microseconds():
    assert to_iso(NOW) == "2026-11-24T12:00:00.000000+00:00"


def test_to_iso_converts_other_timezones_to_utc():
    paris = timezone(timedelta(hours=1))
    assert to_iso(datetime(2026, 11, 24, 13, 0, tzinfo=paris)) == "2026-11-24T12:00:00.000000+00:00"


def test_to_iso_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        to_iso(datetime(2026, 11, 24, 12, 0))


# --- Storage --------------------------------------------------------------


def test_creates_missing_parent_folder(tmp_path):
    path = tmp_path / "data" / "nested" / "monitor.db"
    Storage(path).close()
    assert path.is_file()


def test_saves_success_result(storage, db_path):
    storage.save(result(status_code=200))
    assert rows(db_path) == [("https://example.com", "2026-11-24T12:00:00.000000+00:00", 42.0, 200, None)]


def test_saves_failure_result(storage, db_path):
    storage.save(result(error="timeout"))
    assert rows(db_path) == [("https://example.com", "2026-11-24T12:00:00.000000+00:00", 42.0, None, "timeout")]


def test_data_survives_reopening_the_database(db_path):
    first = Storage(db_path)
    first.save(result())
    first.close()
    reopened = Storage(db_path)  # CREATE TABLE IF NOT EXISTS must not wipe anything
    reopened.close()
    assert len(rows(db_path)) == 1


def test_deletes_results_older_than_retention(storage, db_path):
    storage.save(result(checked_at=NOW - timedelta(days=8)))
    storage.save(result(checked_at=NOW - timedelta(days=6)))
    storage.save(result(checked_at=NOW))
    kept = [row[1] for row in rows(db_path)]
    assert kept == [to_iso(NOW - timedelta(days=6)), to_iso(NOW)]


def test_keeps_result_exactly_at_retention_limit(storage, db_path):
    storage.save(result(checked_at=NOW - timedelta(days=7)))
    storage.save(result(checked_at=NOW))
    assert len(rows(db_path)) == 2


def test_retention_period_is_configurable(db_path):
    store = Storage(db_path, retention=timedelta(hours=1))
    store.save(result(checked_at=NOW - timedelta(hours=2)))
    store.save(result(checked_at=NOW))
    store.close()
    assert len(rows(db_path)) == 1


@pytest.mark.parametrize(
    ("status_code", "error"),
    [(None, None), (200, "timeout")],
)
def test_database_rejects_rows_breaking_the_one_outcome_rule(storage, db_path, status_code, error):
    # Bypasses CheckResult on purpose: the database must enforce the rule on its own.
    with closing(sqlite3.connect(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO checks (url, checked_at, response_time_ms, status_code, error) VALUES (?, ?, ?, ?, ?)",
            ("https://example.com", to_iso(NOW), 1.0, status_code, error),
        )


# --- recent ---------------------------------------------------------------


def test_recent_returns_newest_first_up_to_limit(storage):
    for minutes in range(5):
        storage.save(result(checked_at=NOW + timedelta(minutes=minutes)))
    checks = storage.recent(["https://example.com"], limit=3)["https://example.com"]
    assert [check.checked_at for check in checks] == [NOW + timedelta(minutes=m) for m in (4, 3, 2)]


def test_recent_rebuilds_full_check_results(storage):
    storage.save(result(error="timeout"))
    [check] = storage.recent(["https://example.com"], limit=10)["https://example.com"]
    assert check == result(error="timeout")  # dataclass equality: every field matches


def test_recent_includes_unchecked_urls_and_keeps_given_order(storage):
    storage.save(result())
    recent = storage.recent(["https://never.example", "https://example.com"], limit=10)
    assert list(recent) == ["https://never.example", "https://example.com"]
    assert recent["https://never.example"] == []
    assert len(recent["https://example.com"]) == 1


def test_recent_leaves_out_urls_not_asked_for(storage):
    storage.save(result())  # saved for https://example.com
    assert storage.recent(["https://other.example"], limit=10) == {"https://other.example": []}


@pytest.mark.parametrize("limit", [0, -1])
def test_recent_rejects_limit_below_one(storage, limit):
    with pytest.raises(ValueError, match="at least 1"):
        storage.recent(["https://example.com"], limit=limit)


# --- ping -----------------------------------------------------------------


def test_ping_succeeds_on_open_database(storage):
    storage.ping()  # no exception means the database answered


def test_ping_fails_on_closed_database(db_path):
    store = Storage(db_path)
    store.close()
    with pytest.raises(sqlite3.Error):
        store.ping()
