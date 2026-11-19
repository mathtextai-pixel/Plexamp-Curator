FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libchromaprint-tools \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    beets \
    requests \
    Pillow \
    pyacoustid

RUN mkdir -p /app /beets-data /inbox /music

COPY config.yaml /app/config.yaml
COPY watcher.py /app/watcher.py

CMD ["python", "-u", "/app/watcher.py"]
