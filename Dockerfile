FROM python:3.12-slim

# ffmpeg is required for thumbnail embedding/conversion and metadata.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py state.py downloads.py ./
COPY static ./static

# Persistent settings, queue, history, and resumable staging are mounted from the host.
ENV HOST=0.0.0.0 \
    PORT=8765 \
    OUTPUT_DIR=/downloads \
    CONFIG_DIR=/config \
    STAGING_DIR=/staging \
    XDG_CACHE_HOME=/staging/cache \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/downloads", "/config", "/staging"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/config')" || exit 1

CMD ["python", "app.py"]
