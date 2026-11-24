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

**3. Requirements files, with every dependency locked**
- `requirements.in` lists the direct dependencies, edited by hand. `requirements.txt` is generated from it with `pip freeze` and locks every package, including transitive ones (pydantic, starlette...), to an exact version.
- The lock is generated inside a `python:3.13-slim` container, because some packages only need certain dependencies on certain Python versions; a lock made on 3.14 could miss one.
- Why: the simplest format to read and install; locking means every build installs exactly the same code.
- Rejected: `pyproject.toml` (more ceremony), uv or pip-tools (an extra tool), conda (heavy for a slim image).

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

**22. `/status` is the only data endpoint** (plus `/health`, see 29)
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

## Docker

**25. One-stage image based on `python:3.13-slim`**
- The tag follows Python 3.13 patch releases, so rebuilding picks up security fixes. Pinning the image digest was rejected for now: stricter, but it has to be updated by hand.
- One stage: every dependency installs from a prebuilt wheel, with nothing to compile, so a multi-stage build would save little.
- Dependencies are installed before the code is copied, so code changes don't reinstall them.
- Only `requirements.txt` and `url_monitor/` are copied into the image.
- uvicorn runs a single process listening on `0.0.0.0:8000` inside the container (see 18).

**26. The container runs as an unprivileged user, `app` (UID 10001)**
- Why: if the app were compromised, the attacker could not change its code (owned by root), install tools, or write anywhere except `/data`; and escaping the container would be much harder and would not land as root on the host.
- A fixed UID, so the host knows whom to give a mounted folder to. 10001 is well above the IDs of real users (1000 and up).
- The database lives in `/data` (`DB_PATH=/data/monitor.db`), the only folder `app` can write to. A named volume (`-v monitor-data:/data`) keeps results across restarts and takes its ownership from the image; a host folder would need `chown 10001` first.
- Complementary, not a replacement: rootless Docker or user namespaces, configured on the server.

**27. The image includes a default `urls.txt`**
- The repository's `urls.txt` (public URLs) is copied into the image, so `docker run url-monitor` works with no mount. Mounting another file at `/app/urls.txt` replaces it.
- Like the code, it belongs to root: the app can read it but not change it.
- Rejected: requiring a mount (stricter, but a reviewer would have to create a file before the first run).

**28. `.dockerignore` is an allowlist**
- Everything is excluded (`*`), then only `requirements.txt`, `url_monitor/` and `urls.txt` are let back in; Python caches are excluded even inside them.
- Why: a new file, such as a secret or a local database, stays out of the build until it is added on purpose. It also keeps `.venv/` and `.git/` from being sent on every build.
- Rejected: a blocklist, which has to predict every file to exclude.
- Cost: a new file the image needs must be added here, or `COPY` fails.

**29. `GET /health` and a Docker `HEALTHCHECK`**
- Healthy means the worker task is still running and the database answers `SELECT 1`. Anything else returns 503.
- The response is only `{"status": "ok"}` or `{"status": "unhealthy"}`: the endpoint is public, so the reason is never given.
- Rejected: "the process answers" (misses a dead worker or a broken database); "a round finished recently" (extra code for a stuck worker, which the 5-second timeout already prevents).
- Docker calls it every 30s with a 5s timeout, marks the container unhealthy after 3 failures in a row, and ignores failures in the first 15s. The image has no `curl`, so the check uses Python's `urllib`.
- Docker alone only marks the container unhealthy; it does not restart it. `--restart` only acts when the container exits.

## Documentation

**30. The README is in French and short**
- French, like the brief; the code and this log stay in English.
- It describes how to run, configure, test and deploy the service, and lists the limitations openly; the design choices are kept out of it and live only in this log.
- It starts with the two public URLs of the running instance, so the reviewer can try it straight away.

## Operations

**31. No logging configuration**
- Without configuration, Python still prints warnings and errors (with tracebacks) to stderr, so the worker's existing messages (a bug during a check, a failed round, a round longer than the interval) reach `docker logs`, without a timestamp or level. uvicorn keeps its own logs, including one line per request.
- Why: enough for a service this size; anything more is configuration to maintain for little gain.
- Rejected for now: a shared format for our logs and uvicorn's, a `LOG_LEVEL` setting, a summary line per round, hiding `/health` from the access log.

## Deployment

**32. A dedicated Proxmox VM running Ubuntu 24.04**
- Cloned (full clone) from an existing cloud-init template; 1 vCPU, 1 GB of RAM.
- Why a VM: better isolation from the Proxmox host than an LXC container.
- Docker comes from Docker's official repository and is always run with `sudo`; the user is not in the `docker` group, which amounts to root.
- The image is built on the VM from a clone of the (now public) repository: no registry and no credentials needed.
- Reached over SSH through Tailscale (jumping through the Proxmox host), so the VM itself gets no extra way in.

**33. Hardened `docker run`**
- `--read-only` (plus `--tmpfs /tmp:size=16m`), `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory 256m`, `--pids-limit 100`, log rotation (`max-size=10m`, `max-file=3`), `--restart unless-stopped`.
- The port is published on `127.0.0.1` only, so nothing on the LAN can reach the app directly.
- Tested before deployment: the container is healthy, has no capabilities, cannot write outside `/data`, and uses about 44 MB of memory.

**34. Cloudflare Tunnel with `cloudflared` as a systemd service**
- A remotely managed tunnel (created in the dashboard, routes edited there), with one route: the public hostname to `http://localhost:8000`. HTTP, not HTTPS: that hop stays inside the VM, and uvicorn does not speak TLS.
- Rejected: cloudflared in a second container (slightly tighter, two containers to manage); a locally managed tunnel (more files holding secrets for the same result).
- The tunnel token lives only in the systemd service, and was typed with a leading space to keep it out of the shell history.

**35. Public access, without authentication**
- The reviewer opens the public URL directly. Acceptable because the API is read-only and lists only public URLs; Cloudflare absorbs abusive traffic.
- Rejected for now: Cloudflare Access with an email allowlist (needs the reviewer's email) or a service token (awkward in a browser). Either can be added later without changing the code.

## Still open

- A Cloudflare rate-limiting rule on the public hostname.
- A Proxmox firewall rule blocking the VM from the Proxmox management interface, as a backstop to the SSRF guard.
- Starlette's test client now warns that `httpx` is deprecated in favour of `httpx2`.
