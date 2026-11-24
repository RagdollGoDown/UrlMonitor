"""Data structures shared by the worker, the storage and the API."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """The outcome of checking one URL once.

    A check either got an HTTP response (status_code is set) or failed before
    getting one (error is set). Exactly one of the two is filled in.
    """

    url: str
    checked_at: datetime  # always timezone-aware, in UTC
    response_time_ms: float  # time until the response, or until the failure
    status_code: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.status_code is None) == (self.error is None):
            raise ValueError("exactly one of status_code or error must be set")
