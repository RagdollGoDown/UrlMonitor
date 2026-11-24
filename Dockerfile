FROM python:3.13-slim

# Don't write .pyc files, and print logs immediately instead of buffering them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/monitor.db

# An unprivileged user to run the app: no home folder, no login shell.
# The fixed UID tells the host whom to give mounted folders to.
# /data, for the database, is the only folder it can write to.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app /data

WORKDIR /app

# Dependencies first: this layer is only rebuilt when requirements.txt changes,
# not on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements.txt

# Only the application code; tests, .venv and local files stay out of the image.
COPY url_monitor/ url_monitor/

# A default URL list, so the image runs without a mount. Mounting another file
# at /app/urls.txt replaces it.
COPY urls.txt .

EXPOSE 8000

# Everything above ran as root and stays owned by root: the app can read its
# code but not change it. From here on, the container runs as `app`.
USER app

# Docker calls /health every 30s and marks the container unhealthy after 3
# failures in a row; failures in the first 15s (startup) don't count.
# No curl in the slim image, so Python's standard library does the request:
# a 503 or no answer raises an exception, which makes the command fail.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# A single process: the worker runs inside it (see decision 18).
CMD ["uvicorn", "url_monitor.main:app", "--host", "0.0.0.0", "--port", "8000"]
