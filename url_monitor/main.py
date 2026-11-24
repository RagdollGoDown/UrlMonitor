"""The FastAPI app: starts the worker on startup and serves GET /status.

Run with a single process (the worker lives inside it):
    uvicorn url_monitor.main:app
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request

from url_monitor.checker import check_url
from url_monitor.config import load_urls
from url_monitor.models import CheckResult
from url_monitor.storage import Storage, to_iso
from url_monitor.worker import DEFAULT_INTERVAL_SECONDS, CheckFunction, run_forever

DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def create_app(
    urls_file: Path,
    db_path: Path,
    check: CheckFunction = check_url,  # tests pass a fake here
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: an invalid URL file raises here, so the app refuses to start.
        app.state.urls = load_urls(urls_file)
        app.state.storage = Storage(db_path)
        worker = asyncio.create_task(run_forever(app.state.urls, app.state.storage, interval, check=check))
        try:
            yield  # the app serves requests while paused here
        finally:
            # Shutdown: stop the worker, wait for it to finish, then close the database.
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            app.state.storage.close()

    # docs_url, redoc_url and openapi_url disabled: they would publish a map of the API.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/status")
    async def status(  # async def: runs in the same thread that opened the SQLite connection
        request: Request,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> dict:
        recent = request.app.state.storage.recent(request.app.state.urls, limit)
        return {"urls": [{"url": url, "checks": [_to_json(check) for check in checks]} for url, checks in recent.items()]}

    return app


def _to_json(result: CheckResult) -> dict:
    return {
        "checked_at": to_iso(result.checked_at),
        "status_code": result.status_code,
        "error": result.error,
        "response_time_ms": round(result.response_time_ms, 1),
    }


app = create_app(
    urls_file=Path(os.environ.get("URLS_FILE", "urls.txt")),
    db_path=Path(os.environ.get("DB_PATH", "data/monitor.db")),
)
