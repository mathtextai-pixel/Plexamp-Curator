FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5050 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user. APP_UID should own ./data on the host
# (on Linux: `sudo chown -R 1000:1000 data`, or build with --build-arg APP_UID=$(id -u)).
ARG APP_UID=1000
RUN useradd --uid "${APP_UID}" --create-home --shell /usr/sbin/nologin app

COPY *.py ./
COPY templates/ ./templates/

RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:5050/api/llm-status > /dev/null || exit 1

# ONE worker process with 8 threads, on purpose. On Docker Desktop for Mac, the ./data bind
# mount does not enforce file locks between processes, so two workers could both take
# SQLite's write lock at once and corrupt recommendations.db. Threads within one process
# are locked correctly. Long-running work runs in background job threads (jobs.py), so
# one process is plenty. Only raise -w if DATA_DIR is on a Docker named volume or a
# native Linux filesystem.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5050", "--timeout", "120", "--log-level", "info", "app:app"]
