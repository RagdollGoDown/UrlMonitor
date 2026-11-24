# Design decisions

A running log of the choices made while building this project, why they were made, and what was considered instead. Newest entries go at the bottom of each section.

## Stack and tooling

**1. Python with FastAPI, shipped as a Docker image**
- Why: FastAPI is a small, well-known framework for a JSON API; a Docker image runs the same on any server.
- Deployment intent: self-hosted on Proxmox, exposed through a Cloudflare Tunnel rather than an open port.

**2. Python 3.13 in the Docker image**
- Why: security fixes until October 2029.
- Rejected: 3.11, whose security support ends in October 2027.
- Note: local development runs on 3.14, so the code must avoid 3.14-only features.

**3. Dependencies in `requirements.txt`, with exact versions for direct dependencies**
- Why: the simplest format to read and install; pinning makes installs repeatable.
- Trade-off: transitive dependencies (pydantic, starlette) are not pinned yet.
- Rejected: `pyproject.toml` (more ceremony), uv (an extra tool), conda (heavy for a slim image).

**4. Flat package layout: `url_monitor/` at the repository root**
- Why: the folder name is the import name (`from url_monitor.config import ...`).
- Rejected: plain `src/` as the package, which makes every import read `src.`; a `src/url_monitor/` layout, which guards against packaging problems this project does not have.

**5. Local environment with `python3 -m venv .venv`**
- Why: built into Python, and matches how dependencies are installed in Docker.
- Rejected: conda.

## Data model

**6. One `CheckResult` frozen dataclass per check**
- Fields: `url`, `checked_at` (timezone-aware, UTC), `response_time_ms`, `status_code`, `error`.
- `frozen` because a result records something that already happened; `kw_only` so every field is named at creation; the time unit is in the field name.

**7. Failures are recorded, not dropped**
- A check has either a `status_code` or an `error`, never both and never neither. This is enforced in `__post_init__` and by a `CHECK` constraint in the database.
- `response_time_ms` is kept for failures too: the time until the failure tells a timeout apart from an instant DNS error.
- Rejected: dropping failures (a down site would look like a site never checked); separate success and failure classes (too much for this size).

## Configuration

**8. URLs come from a text file only, one URL per line**
- Blank lines and full-line `#` comments are ignored; inline comments are not supported because `#` is valid inside a URL.
- Why: the file is the better home for a list, and one source keeps the loader simple. The brief asks for a file *or* an environment variable.
- Rejected: an environment variable as well (more code and a precedence rule); YAML, TOML or JSON (more than a list needs).
- There is deliberately no API to add URLs at runtime: anyone with API access could make the server send requests into the local network.

**9. Invalid URL files stop the service from starting**
- Rejected: anything other than `http`/`https`, a missing host, embedded credentials, an invalid port, an empty list. Duplicates are dropped silently.
- All invalid lines are reported at once, by line number only: the URL is not echoed because it may contain a password.

## Storage

**10. SQLite through the standard `sqlite3` module**
- Why: no dependency, and the SQL for one table stays easy to read.
- Rejected: aiosqlite (a dependency for tiny queries), SQLAlchemy or SQLModel (too much machinery for one table).

**11. Timestamps stored as fixed-width ISO 8601 text in UTC**
- Why: readable in the database, and sorts correctly as text because every value has the same format (microseconds always included).
- Rejected: Unix timestamps (compact but unreadable).

**12. Results are kept for 7 days**
- Rows older than 7 days are deleted on every save, measured from the new result's own timestamp. An index on `checked_at` keeps this cheap.
- Rejected: keeping the last N rows per URL; no limit at all.
- Reading: `recent(urls, limit)` returns the latest `limit` results for each given URL, newest first. It runs one small query per URL, each served by the `(url, checked_at)` index; a single query with a window function (`ROW_NUMBER() OVER (PARTITION BY url ...)`) was rejected as harder to read for no real gain at this size.

## Checking URLs

**15. httpx, with one fresh client per check**
- Why: async (fits FastAPI), and built-in mocking (`MockTransport`) so tests never use the network.
- A new client per check means a new connection every time, so every measurement includes the same TCP and TLS setup. Reusing connections would make the first check slow and later ones fast.
- `trust_env=False`: proxy settings from the environment are ignored, because a proxy would bypass the IP pinning.

**16. What a check does and measures**
- A 5-second hard limit for the whole check, DNS included.
- A `GET` request; the body is never downloaded (the response is closed once the headers arrive). This avoids huge or malicious bodies.
- Redirects are not followed: a 3xx is recorded as-is. It already proves the server answers, and following it could lead to an internal address.
- `response_time_ms` is the time from sending the request to receiving the response headers. DNS lookup time is not included.
- Failures before the request is sent (`dns_error`, `blocked`) therefore have a time of `0.0`.
- Rejected: `HEAD` requests, which some servers answer wrongly (405) or differently from `GET`.

**17. Error categories**
- `timeout`, `dns_error`, `connection_error`, `tls_error`, `blocked` (SSRF guard), `other_error`, and `internal_error` (a bug in our own code, see 20).
- Fixed categories rather than exception messages: stable, easy to filter, and they don't leak internal details through `/status`.

## Worker

**18. The worker runs as a background task in FastAPI's `lifespan`**
- Why: one process to deploy, and the API and the worker share the same database connection.
- Trade-off: the app must run as a single uvicorn process, otherwise each process would run its own worker.
- Rejected: a separate worker process or container (more robust, more to deploy).

**19. Rounds start every 60 seconds, at most 10 checks at a time**
- URLs are checked concurrently, capped at 10 by an `asyncio.Semaphore`, so one slow URL doesn't delay the others and the network isn't flooded.
- Rounds are scheduled from the previous round's start on a monotonic clock, so they don't drift.
- Rounds never overlap. If one takes longer than the interval, a warning is logged and the next starts immediately. With a 5-second timeout, this only happens with more than about 120 URLs that all time out.

**20. A bug in our own code doesn't stop the worker**
- If a check raises unexpectedly, the traceback is logged and an `internal_error` result is saved, so `/status` shows something is wrong instead of a stale result. The other URLs are still checked.
- If a whole round fails (e.g. the database is unavailable), it is logged and the next round runs as planned.
- Only `Exception` is caught, never `BaseException`: `CancelledError` must get through so the app can shut down.
- `blocked` stays a single category with no detail, in `/status` or in the logs.

## API

**21. `GET /status` returns the last N results for each URL, grouped by URL**
- Shape: `{"urls": [{"url": ..., "checks": [{"checked_at", "status_code", "error", "response_time_ms"}, ...]}]}`, newest check first.
- `?limit=` sets N: 10 by default, from 1 to 100. Anything else gets a 422 error.
- Only URLs in the current URL file are listed, in file order. A URL not checked yet has `"checks": []`; a URL removed from the file disappears, even though its rows remain until the 7-day cleanup.
- Rejected: only the latest result per URL (no trend); a flat list with filters (harder to read).

**22. `/status` is the only endpoint**
- FastAPI's `/docs`, `/redoc` and `/openapi.json` are turned off: they publish a map of the API. The README documents `/status` instead.

**23. Endpoints are `async def`**
- They run in the event loop's thread, the same thread that opened the SQLite connection, which `sqlite3` requires. A `def` endpoint would run in a thread pool and fail.
- Acceptable because the query takes well under a millisecond, so it never holds up the worker.

**24. File locations come from `URLS_FILE` and `DB_PATH`**
- Defaults: `urls.txt` and `data/monitor.db`. These only say *where* the files are, so Docker can point them at mounted volumes; the URLs themselves still come only from the file.
- `create_app(...)` takes the paths and the check function as arguments, so tests can build an app with temporary files and a fake check.

## Security

**14. SSRF guard: only public internet addresses can be checked**
- Blocks private, loopback, link-local (including cloud metadata), carrier-grade NAT, reserved, multicast and broadcast addresses, in IPv4 and IPv6, including IPv4 addresses hidden inside IPv6 (`::ffff:127.0.0.1`).
- Uses `ipaddress.is_global`, plus an explicit multicast check because `is_global` counts multicast as global.
- Runs on every check, not when the file is loaded, because what a name resolves to can change.
- If any resolved address is non-public, the host is blocked.
- Returns the validated IP so the checker connects to exactly that address (protection against DNS rebinding).
- Cannot be turned off: no setting to allow private addresses for now, so it is safe by default.

## Testing

**13. pytest, as a development-only dependency**
- Why: the de facto standard; plain `assert` statements; FastAPI's test client is built to work with it.
- Rejected: unittest (built in, but more verbose).
- Test tools live in `requirements-dev.txt`, which includes `requirements.txt`, so the Docker image only installs what the service needs.

## Still open

- Docker volumes for the URL file and the database, and whether the repository ships an example URL file.
- How the API is protected (Cloudflare Access is the plan), and a `/health` endpoint for Docker.
- Logging configuration.
- Starlette's test client now warns that `httpx` is deprecated in favour of `httpx2`.
- Language of the README.
