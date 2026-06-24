FROM python:3.12-slim

# ffmpeg is required for thumbnail embedding/conversion and metadata.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg cifs-utils \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /mnt/smb

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

# Container defaults. Volumes /downloads and /config are mounted from the host.
ENV HOST=0.0.0.0 \
    PORT=8765 \
    OUTPUT_DIR=/downloads \
    CONFIG_DIR=/config \
    XDG_CACHE_HOME=/tmp

EXPOSE 8765
VOLUME ["/downloads", "/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/config')" || exit 1

CMD ["python", "app.py"]
