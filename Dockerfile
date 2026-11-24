FROM python:3.13-slim

# Don't write .pyc files, and print logs immediately instead of buffering them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is only rebuilt when requirements.txt changes,
# not on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements.txt

# Only the application code; tests, .venv and local files stay out of the image.
COPY url_monitor/ url_monitor/

EXPOSE 8000

# A single process: the worker runs inside it (see decision 18).
CMD ["uvicorn", "url_monitor.main:app", "--host", "0.0.0.0", "--port", "8000"]
