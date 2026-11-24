"""SQLite storage for check results."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from url_monitor.models import CheckResult

DEFAULT_RETENTION = timedelta(days=7)

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id               INTEGER PRIMARY KEY,
    url              TEXT    NOT NULL,
    checked_at       TEXT    NOT NULL,   -- ISO 8601 in UTC
    response_time_ms REAL    NOT NULL,
    status_code      INTEGER,
    error            TEXT,
    CHECK ((status_code IS NULL) <> (error IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_checks_url_time ON checks (url, checked_at);
CREATE INDEX IF NOT EXISTS idx_checks_time ON checks (checked_at);
"""


def to_iso(moment: datetime) -> str:
    """Format a timezone-aware datetime as fixed-width ISO 8601 in UTC.

    The fixed width matters: timestamps are compared as text in SQL, which
    only sorts correctly if every value has exactly the same format.
    """
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Storage:
    def __init__(self, path: Path | str, retention: timedelta = DEFAULT_RETENTION) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._retention = retention
        with self._conn:
            self._conn.executescript(SCHEMA)

    def save(self, result: CheckResult) -> None:
        """Insert one result and delete everything older than the retention period."""
        cutoff = result.checked_at - self._retention
        with self._conn:  # one transaction: both statements apply, or neither
            self._conn.execute(
                "INSERT INTO checks (url, checked_at, response_time_ms, status_code, error)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    result.url,
                    to_iso(result.checked_at),
                    result.response_time_ms,
                    result.status_code,
                    result.error,
                ),
            )
            self._conn.execute("DELETE FROM checks WHERE checked_at < ?", (to_iso(cutoff),))

    def recent(self, urls: list[str], limit: int) -> dict[str, list[CheckResult]]:
        """Return the latest `limit` results for each URL, newest first.

        Every URL in `urls` appears in the result, in the same order, with an
        empty list if it has not been checked yet. URLs not in `urls` are left out.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")  # SQLite treats LIMIT -1 as "no limit"
        recent: dict[str, list[CheckResult]] = {}
        for url in urls:
            # One small query per URL; each one is served by the (url, checked_at) index.
            rows = self._conn.execute(
                "SELECT checked_at, response_time_ms, status_code, error FROM checks"
                " WHERE url = ? ORDER BY checked_at DESC, id DESC LIMIT ?",
                (url, limit),
            ).fetchall()
            recent[url] = [
                CheckResult(
                    url=url,
                    checked_at=datetime.fromisoformat(checked_at),
                    response_time_ms=response_time_ms,
                    status_code=status_code,
                    error=error,
                )
                for checked_at, response_time_ms, status_code, error in rows
            ]
        return recent

    def close(self) -> None:
        self._conn.close()
