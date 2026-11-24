from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from url_monitor.models import CheckResult

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_success_result_has_status_code_and_no_error():
    result = CheckResult(url="https://example.com", checked_at=NOW, response_time_ms=120.5, status_code=200)
    assert result.status_code == 200
    assert result.error is None


def test_failure_result_has_error_and_no_status_code():
    result = CheckResult(url="https://example.com", checked_at=NOW, response_time_ms=10_000.0, error="timeout")
    assert result.error == "timeout"
    assert result.status_code is None


@pytest.mark.parametrize(
    "outcome",
    [
        {},  # neither status_code nor error
        {"status_code": 200, "error": "timeout"},  # both
    ],
)
def test_result_needs_exactly_one_of_status_code_or_error(outcome):
    with pytest.raises(ValueError, match="exactly one"):
        CheckResult(url="https://example.com", checked_at=NOW, response_time_ms=1.0, **outcome)


def test_result_cannot_be_modified():
    result = CheckResult(url="https://example.com", checked_at=NOW, response_time_ms=1.0, status_code=200)
    with pytest.raises(FrozenInstanceError):
        result.status_code = 500
