FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py .
COPY app.py .
COPY gemini_curator.py .
COPY music_excavator.py .
COPY discovery_engine.py .
COPY weekly_refresh.py .
COPY http_client.py .
COPY locks.py .
COPY templates/ ./templates/

RUN mkdir -p /app/data

ENV PORT=5050
ENV HOST=0.0.0.0
ENV DATA_DIR=/app/data
EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5050/api/llm-status || exit 1

CMD ["gunicorn", "-w", "2", "--threads", "4", "-b", "0.0.0.0:5050", "--timeout", "120", "app:app"]
