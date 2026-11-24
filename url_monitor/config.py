"""Loading the list of URLs to monitor.

The list is only ever read from a file: there is deliberately no way to add
URLs at runtime.
"""

from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}


class UrlListError(ValueError):
    """Raised when the URL file contains invalid entries or no URLs at all."""


def load_urls(path: Path) -> list[str]:
    """Read one URL per line from `path`.

    Blank lines and lines starting with '#' are ignored. Duplicates are
    dropped, keeping the first occurrence. Raises UrlListError listing every
    invalid line, so all mistakes can be fixed in one go.
    """
    urls: list[str] = []
    problems: list[str] = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        problem = _check_url(url)
        if problem:
            # The URL itself is not echoed: it may contain a password.
            problems.append(f"line {line_number}: URL {problem}")
        else:
            urls.append(url)

    if problems:
        raise UrlListError(f"invalid entries in {path}:\n  " + "\n  ".join(problems))
    if not urls:
        raise UrlListError(f"{path} contains no URLs")

    return list(dict.fromkeys(urls))  # removes duplicates, keeps order


def _check_url(url: str) -> str | None:
    """Return why `url` is rejected, or None if it is acceptable."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        return "must start with http:// or https://"
    if not parts.hostname:
        return "has no host"
    if parts.username or parts.password:
        return "must not contain credentials (they would be exposed by /status)"
    try:
        parts.port  # raises ValueError for a port like :99999 or :abc
    except ValueError:
        return "has an invalid port"
    return None
