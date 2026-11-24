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
- The query that reads results is left to the `/status` commit, because it depends on what the endpoint returns.

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

- Where the URL file and the database live (paths, Docker volumes), and whether the repository ships an example URL file.
- HTTP client for the checks, timeouts, and the list of `error` categories.
- What `/status` returns, and how the API is protected.
- Language of the README.
